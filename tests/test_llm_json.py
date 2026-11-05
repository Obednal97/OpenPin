"""Tests for the generic structured-output LLM call (no live API calls)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.providers import llm_json as L
from app.providers._models import CLAUDE_SUMMARIZE, OPENAI_SUMMARIZE

SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}
GOOD = {"verdict": "fine"}
GOOD_JSON = json.dumps(GOOD)


def _http_response(body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    if status >= 400:
        r.raise_for_status.side_effect = requests.exceptions.HTTPError(response=r)
    else:
        r.raise_for_status.return_value = None
    return r


def _chat_body(content=GOOD_JSON, finish_reason="stop", refusal=None):
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": content, "refusal": refusal}}]}


def _ollama_body(content=GOOD_JSON, done_reason="stop"):
    return {"done_reason": done_reason, "message": {"content": content}}


class _FakeStream:
    def __init__(self, message, calls, kwargs):
        self._message = message
        calls.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._message


def _fake_anthropic_client(text=GOOD_JSON, stop_reason="end_turn"):
    calls = []
    message = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])
    client = MagicMock()
    client.beta.messages.stream.side_effect = lambda **kw: _FakeStream(message, calls, kw)
    return client, calls


def _call(config, **kw):
    return L.call_llm_json("SYS", "USER", SCHEMA, config, **kw)


def test_prefers_configured_provider():
    config = {"anthropic_api_key": "a", "openai_api_key": "o", "summarization_provider": "openai"}
    client, anth_calls = _fake_anthropic_client()
    with (
        patch.object(L, "_anthropic_client", return_value=client),
        patch.object(L.http_requests, "post", return_value=_http_response(_chat_body())) as post,
    ):
        parsed, provider, errors = _call(config)
    assert (parsed, provider, errors) == (GOOD, "openai", [])
    assert post.call_count == 1 and not anth_calls


def test_defaults_to_anthropic_first():
    client, calls = _fake_anthropic_client()
    with patch.object(L, "_anthropic_client", return_value=client):
        parsed, provider, errors = _call({"anthropic_api_key": "a", "openai_api_key": "o"})
    assert (parsed, provider, errors) == (GOOD, "anthropic", [])
    assert calls[0]["model"] == CLAUDE_SUMMARIZE
    assert calls[0]["system"] == "SYS" and calls[0]["messages"][0]["content"] == "USER"


def test_falls_back_on_error_and_collects_errors():
    config = {"anthropic_api_key": "a", "openai_api_key": "o"}
    client = MagicMock()
    client.beta.messages.stream.side_effect = RuntimeError("boom")
    with (
        patch.object(L, "_anthropic_client", return_value=client),
        patch.object(L.http_requests, "post", return_value=_http_response(_chat_body())),
    ):
        parsed, provider, errors = _call(config)
    assert parsed == GOOD and provider == "openai"
    assert len(errors) == 1 and "boom" in errors[0] and "Anthropic" in errors[0]


def test_openrouter_used_when_no_direct_keys():
    with patch.object(L.http_requests, "post", return_value=_http_response(_chat_body())) as post:
        parsed, provider, _ = _call({"openrouter_api_key": "or"})
    assert parsed == GOOD and provider == "openrouter-claude"
    assert "openrouter.ai" in post.call_args.args[0]
    assert post.call_args.kwargs["json"]["provider"] == {"require_parameters": True}


def test_never_uses_distilbart():
    with (
        patch.object(L, "ollama_models", return_value=None),
        patch("app.providers.summarize.summarize_local") as local,
    ):
        parsed, provider, errors = _call({"summarization_provider": "local"})
    assert parsed is None and provider == "none"
    assert len(errors) == 1 and "Ollama" in errors[0]
    local.assert_not_called()


def test_schema_passed_to_each_provider():
    client, calls = _fake_anthropic_client()
    with patch.object(L, "_anthropic_client", return_value=client):
        _call({"anthropic_api_key": "a"}, schema_name="coaching_review")
    assert calls[0]["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}

    with patch.object(L.http_requests, "post", return_value=_http_response(_chat_body())) as post:
        _call({"openai_api_key": "o"}, schema_name="coaching_review")
    body = post.call_args.kwargs["json"]
    assert body["model"] == OPENAI_SUMMARIZE
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["response_format"]["json_schema"]["name"] == "coaching_review"
    assert body["messages"][0] == {"role": "system", "content": "SYS"}

    with (
        patch.object(L, "ollama_models", return_value=["qwen3.5:9b"]),
        patch.object(L.http_requests, "post", return_value=_http_response(_ollama_body())) as post,
    ):
        parsed, provider, _ = _call({})
    assert parsed == GOOD and provider == "ollama"
    assert post.call_args.kwargs["json"]["format"] == SCHEMA


@pytest.mark.parametrize("bad", ["not json at all", "{truncated", "[1, 2]", ""])
def test_invalid_json_is_an_error_not_a_crash(bad):
    with patch.object(L.http_requests, "post", return_value=_http_response(_chat_body(content=bad))):
        parsed, provider, errors = _call({"openai_api_key": "o"})
    assert parsed is None and provider == "none"
    assert len(errors) >= 1


def test_code_fenced_json_is_accepted():
    fenced = f"```json\n{GOOD_JSON}\n```"
    with patch.object(L.http_requests, "post", return_value=_http_response(_chat_body(content=fenced))):
        parsed, provider, _ = _call({"openai_api_key": "o"})
    assert parsed == GOOD and provider == "openai"


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_anthropic_refusal_and_truncation_move_on(stop):
    client, _ = _fake_anthropic_client(stop_reason=stop)
    with (
        patch.object(L, "_anthropic_client", return_value=client),
        patch.object(L.http_requests, "post", return_value=_http_response(_chat_body())),
    ):
        parsed, provider, errors = _call({"anthropic_api_key": "a", "openai_api_key": "o"})
    assert parsed == GOOD and provider == "openai" and len(errors) == 1


def test_openai_length_and_refusal_are_errors():
    for body in (_chat_body(finish_reason="length"), _chat_body(content=None, refusal="no")):
        with patch.object(L.http_requests, "post", return_value=_http_response(body)):
            parsed, provider, errors = _call({"openai_api_key": "o"})
        assert parsed is None and provider == "none" and errors


def test_ollama_truncation_is_an_error():
    with (
        patch.object(L, "ollama_models", return_value=["qwen3.5:9b"]),
        patch.object(L.http_requests, "post", return_value=_http_response(_ollama_body(done_reason="length"))),
    ):
        parsed, provider, errors = _call({})
    assert parsed is None and provider == "none" and errors


@pytest.mark.parametrize("preference", ["local", "ollama"])
def test_local_preference_never_falls_through_to_a_cloud_key(preference):
    config = {
        "summarization_provider": preference,
        "anthropic_api_key": "x",
        "openai_api_key": "o",
        "openrouter_api_key": "or",
    }
    client, anth_calls = _fake_anthropic_client()
    with (
        patch.object(L, "ollama_models", return_value=None),
        patch.object(L, "_anthropic_client", return_value=client) as make_client,
        patch.object(L.http_requests, "post") as post,
    ):
        parsed, provider, errors = _call(config)
    assert (parsed, provider) == (None, "none")
    assert len(errors) == 1 and errors[0].startswith("Ollama (local): not running")
    assert L.ollama_unavailable(errors[0])
    make_client.assert_not_called()
    assert not anth_calls
    post.assert_not_called()


def test_local_preference_uses_ollama_when_running():
    with (
        patch.object(L, "ollama_models", return_value=["qwen3.5:9b"]),
        patch.object(L.http_requests, "post", return_value=_http_response(_ollama_body())) as post,
    ):
        parsed, provider, errors = _call({"summarization_provider": "local", "anthropic_api_key": "x"})
    assert (parsed, provider, errors) == (GOOD, "ollama", [])
    assert post.call_args.args[0].endswith("/api/chat")


def test_ollama_unavailable_only_for_not_running_or_no_model():
    with patch.object(L, "ollama_models", return_value=["nomic-embed-text"]):
        _, _, errors = _call({"summarization_provider": "local"})
    assert L.ollama_unavailable(errors[0]) and "no chat model" in errors[0]
    with (
        patch.object(L, "ollama_models", return_value=["qwen3.5:9b"]),
        patch.object(L.http_requests, "post", return_value=_http_response(_ollama_body(content="{broken"))),
    ):
        _, _, errors = _call({"summarization_provider": "local"})
    assert errors and not L.ollama_unavailable(errors[0])
    assert not L.ollama_unavailable("Anthropic (x): not running on y")
