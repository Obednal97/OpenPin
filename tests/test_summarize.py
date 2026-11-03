"""Tests for the summarization providers (no live API calls)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.providers import summarize as S
from app.providers._models import (
    CLAUDE_SUMMARIZE,
    OPENAI_SUMMARIZE,
    OPENROUTER_CLAUDE_SUMMARIZE,
    OPENROUTER_OPENAI_SUMMARIZE,
)

SEGMENTS = [
    {"start": 0, "end": 4, "text": 'We said "ship it" on Friday.', "speaker": "Speaker 1"},
    {"start": 65.4, "end": 70, "text": "Agreed.\tLine two\nwith newline", "speaker": "Speaker 2"},
    {"start": 3725, "end": 3730, "text": "No speaker here", "speaker": None},
    {"start": 3800, "end": 3801, "text": "   ", "speaker": "Speaker 1"},
]
GOOD_JSON = json.dumps({"title": "Ship decision", "summary": "## Core Synopsis\nShipped."})


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


# ---------------------------------------------------------------------------
# Transcript formatting
# ---------------------------------------------------------------------------


def test_format_transcript_speaker_labelled_lines():
    out = S.format_transcript(SEGMENTS, "fallback")
    lines = out.split("\n")
    assert lines[0] == '[00:00] Speaker 1: We said "ship it" on Friday.'
    assert lines[1] == "[01:05] Speaker 2: Agreed.\tLine two"  # text is not escaped/mangled
    assert lines[2] == "with newline"
    assert lines[3] == "[1:02:05] No speaker here"
    assert len(lines) == 4  # blank segment skipped


def test_format_transcript_falls_back_to_text():
    assert S.format_transcript([], " plain text ") == "plain text"
    assert S.format_transcript(None, "plain") == "plain"


def test_sanitize_for_json_removed():
    assert not hasattr(S, "sanitize_for_json")


# ---------------------------------------------------------------------------
# Anthropic (official SDK, mocked)
# ---------------------------------------------------------------------------


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
    message = SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
    )
    client = MagicMock()
    client.beta.messages.stream.side_effect = lambda **kw: _FakeStream(message, calls, kw)
    return client, calls


def test_anthropic_request_construction():
    client, calls = _fake_anthropic_client()
    transcript = S.format_transcript(SEGMENTS)
    with patch.object(S, "_anthropic_client", return_value=client):
        summary, title = S.summarize_anthropic(transcript, "sk-ant-x", "\n\nUser context: Job: PM")
    assert (summary, title) == ("## Core Synopsis\nShipped.", "Ship decision")
    kw = calls[0]
    assert kw["model"] == CLAUDE_SUMMARIZE == "claude-sonnet-5-5"
    assert kw["max_tokens"] >= 32000
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": S.SUMMARY_SCHEMA}
    assert kw["output_config"]["effort"] == "low"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert "temperature" not in kw and "thinking" not in kw
    assert "User context: Job: PM" in kw["system"]
    content = kw["messages"][0]["content"]
    # Full transcript, speaker labels, quotes/newlines untouched (no sanitize mangling)
    assert transcript in content
    assert '[00:00] Speaker 1: We said "ship it" on Friday.' in content
    assert "Line two\nwith newline" in content


def test_anthropic_long_transcript_not_truncated():
    client, calls = _fake_anthropic_client()
    long_text = "\n".join(f"[00:{i % 60:02d}] Speaker 1: sentence number {i}" for i in range(3000))
    assert len(long_text) > 15000
    with patch.object(S, "_anthropic_client", return_value=client):
        S.summarize_anthropic(long_text, "k")
    assert len(calls) == 1
    assert "sentence number 2999" in calls[0]["messages"][0]["content"]


def test_anthropic_joins_text_after_a_mid_stream_fallback():
    # The fallback model continues the partial text; a marker block sits between.
    head, tail = GOOD_JSON[:20], GOOD_JSON[20:]
    message = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            SimpleNamespace(type="text", text=head),
            SimpleNamespace(type="fallback"),
            SimpleNamespace(type="text", text=tail),
        ],
    )
    client = MagicMock()
    client.beta.messages.stream.side_effect = lambda **kw: _FakeStream(message, [], kw)
    with patch.object(S, "_anthropic_client", return_value=client):
        summary, title = S.summarize_anthropic(S.format_transcript(SEGMENTS), "sk-ant-x")
    assert (summary, title) == ("## Core Synopsis\nShipped.", "Ship decision")


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_anthropic_incomplete_response_raises(stop_reason):
    client, _ = _fake_anthropic_client(text='{"title": "x", "summ', stop_reason=stop_reason)
    with patch.object(S, "_anthropic_client", return_value=client), pytest.raises(S.SummaryError):
        S.summarize_anthropic("[00:00] hi there", "k")


def test_anthropic_missing_sdk_is_clear_error():
    with patch.object(S, "anthropic", None), pytest.raises(S.SummaryError, match="anthropic"):
        S.summarize_anthropic("text", "k")


def test_huge_transcript_is_map_reduced(monkeypatch):
    monkeypatch.setattr(S, "CLOUD_SINGLE_PASS_CHARS", 200)
    client, calls = _fake_anthropic_client()
    text = "\n".join(f"[00:{i:02d}] Speaker 1: line {i} " + "x" * 30 for i in range(20))
    with patch.object(S, "_anthropic_client", return_value=client):
        summary, title = S.summarize_anthropic(text, "k")
    assert len(calls) > 2  # parts + merge
    assert "part 1 of" in calls[0]["messages"][0]["content"]
    assert "<part_summaries>" in calls[-1]["messages"][0]["content"]
    for c in calls[:-1]:
        assert len(c["messages"][0]["content"]) < 200 + 400  # chunk + instructions
    assert title == "Ship decision"


# ---------------------------------------------------------------------------
# OpenAI / OpenRouter (requests, mocked)
# ---------------------------------------------------------------------------


@patch("app.providers.summarize.http_requests.post")
def test_openai_request_construction(mock_post):
    mock_post.return_value = _http_response(_chat_body())
    transcript = S.format_transcript(SEGMENTS)
    summary, title = S.summarize_openai(transcript, "sk-x", "\n\nThis recording is tagged as: 1:1.")
    assert title == "Ship decision"
    body = mock_post.call_args.kwargs["json"]
    assert body["model"] == OPENAI_SUMMARIZE == "gpt-6-luna"
    assert body["reasoning_effort"] == "low"
    assert body["max_completion_tokens"] >= 16000
    assert "temperature" not in body
    rf = body["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == S.SUMMARY_SCHEMA
    assert "tagged as: 1:1" in body["messages"][0]["content"]
    assert transcript in body["messages"][1]["content"]


@patch("app.providers.summarize.http_requests.post")
def test_openai_truncated_completion_raises(mock_post):
    mock_post.return_value = _http_response(_chat_body('{"title": "x", "sum', finish_reason="length"))
    with pytest.raises(S.SummaryError, match="cut off"):
        S.summarize_openai("[00:00] hi", "k")


@patch("app.providers.summarize.http_requests.post")
def test_openrouter_request_construction(mock_post):
    mock_post.return_value = _http_response(_chat_body())
    S.summarize_openrouter("[00:00] Speaker 1: hi", "sk-or", OPENROUTER_CLAUDE_SUMMARIZE)
    body = mock_post.call_args.kwargs["json"]
    assert body["model"] == "anthropic/claude-sonnet-5.5"
    assert body["response_format"]["type"] == "json_schema"
    assert body["provider"] == {"require_parameters": True}
    assert OPENROUTER_OPENAI_SUMMARIZE == "openai/gpt-6-luna"


# ---------------------------------------------------------------------------
# Structured output parsing
# ---------------------------------------------------------------------------


def test_parse_summary_variants():
    assert S._parse_summary(GOOD_JSON) == ("## Core Synopsis\nShipped.", "Ship decision")
    assert S._parse_summary("```json\n" + GOOD_JSON + "\n```")[1] == "Ship decision"
    assert S._parse_summary("Here you go: " + GOOD_JSON)[1] == "Ship decision"
    assert S._parse_summary("## Plain markdown") == ("## Plain markdown", "")
    with pytest.raises(S.SummaryError):
        S._parse_summary('{"title": "Cut", "summary": "half')  # truncated fragment
    with pytest.raises(S.SummaryError):
        S._parse_summary("")


# ---------------------------------------------------------------------------
# Ollama detection + cascade order
# ---------------------------------------------------------------------------


def test_pick_ollama_model():
    assert S.pick_ollama_model(["nomic-embed-text:latest", "llama3.2:latest"]) == "llama3.2:latest"
    assert S.pick_ollama_model(["foo:7b", "qwen3:8b"]) == "qwen3:8b"
    assert S.pick_ollama_model(["foo:7b"], preferred="foo") == "foo:7b"
    assert S.pick_ollama_model([]) is None
    assert S.pick_ollama_model(["nomic-embed-text"]) is None


@patch("app.providers.summarize.http_requests.get")
def test_ollama_detection_short_timeout_and_unreachable(mock_get):
    mock_get.side_effect = requests.exceptions.ConnectionError()
    assert S.ollama_models() is None
    connect, read = mock_get.call_args.kwargs["timeout"]
    assert connect <= 1 and read <= 2
    assert mock_get.call_args.args[0] == "http://localhost:11434/api/tags"


@patch("app.providers.summarize.http_requests.post")
@patch("app.providers.summarize.http_requests.get")
def test_ollama_request_uses_schema_format(mock_get, mock_post):
    mock_get.return_value = _http_response({"models": [{"name": "llama3.2:latest"}]})
    mock_post.return_value = _http_response({"message": {"content": GOOD_JSON}, "done_reason": "stop"})
    summary, title, model = S.summarize_ollama("[00:00] Speaker 1: hi", "\n\nctx")
    assert (title, model) == ("Ship decision", "llama3.2:latest")
    body = mock_post.call_args.kwargs["json"]
    assert body["format"] == S.SUMMARY_SCHEMA and body["stream"] is False
    assert body["options"]["num_ctx"] >= 8192
    assert "[00:00] Speaker 1: hi" in body["messages"][1]["content"]


def _order(config, fail=()):
    """Run the cascade with every provider failing except none; return call order."""
    calls = []

    def mk(name):
        def fn(*a, **k):
            calls.append(name)
            raise S.SummaryError(f"{name} down")

        return fn

    with (
        patch.object(S, "summarize_anthropic", mk("anthropic")),
        patch.object(S, "summarize_openai", mk("openai")),
        patch.object(S, "summarize_openrouter", mk("openrouter")),
        patch.object(S, "summarize_ollama", mk("ollama")),
        patch.object(S, "summarize_local", mk("local")),
    ):
        summary, title, prov, errors = S.summarize("text " * 10, config, "")
    assert summary is None and prov == "none"
    assert len(errors) == len(calls)
    return calls


def test_cascade_order_default():
    cfg = {"anthropic_api_key": "a", "openai_api_key": "o"}
    assert _order(cfg) == ["anthropic", "openai", "ollama", "local"]


def test_cascade_order_prefers_openai():
    cfg = {"anthropic_api_key": "a", "openai_api_key": "o", "summarization_provider": "openai"}
    assert _order(cfg) == ["openai", "anthropic", "ollama", "local"]


def test_cascade_order_prefers_local_ollama_before_distilbart():
    cfg = {"anthropic_api_key": "a", "summarization_provider": "local"}
    assert _order(cfg) == ["ollama", "local", "anthropic"]


def test_cascade_no_keys_tries_ollama_then_distilbart():
    assert _order({}) == ["ollama", "local"]


def test_cascade_falls_back_to_ollama_and_passes_labelled_transcript():
    seen = {}

    def fake_ollama(transcript, ctx, model):
        seen["transcript"] = transcript
        return "Ollama summary", "Ollama title", "llama3.2"

    with (
        patch.object(S, "summarize_anthropic", side_effect=requests.exceptions.ConnectionError()),
        patch.object(S, "summarize_ollama", side_effect=fake_ollama),
        patch.object(S, "summarize_local") as local,
    ):
        summary, title, prov, errors = S.summarize("plain", {"anthropic_api_key": "a"}, "", segments=SEGMENTS)
    assert (summary, title, prov) == ("Ollama summary", "Ollama title", "ollama")
    assert errors == ["Anthropic (claude-sonnet-5-5): Connection failed — check your internet"]
    assert seen["transcript"].startswith("[00:00] Speaker 1:")
    local.assert_not_called()


def test_cascade_anthropic_sdk_errors_are_described():
    import anthropic
    import httpx2

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(401, request=req)
    err = anthropic.AuthenticationError("bad key", response=resp, body={"error": {"message": "invalid x-api-key"}})
    assert "Invalid API key" in S._describe_error("Anthropic", err)
    resp400 = httpx2.Response(400, request=req)
    err400 = anthropic.BadRequestError(
        "low", response=resp400, body={"error": {"message": "Your credit balance is too low"}}
    )
    assert "Insufficient credits" in S._describe_error("Anthropic", err400)


# ---------------------------------------------------------------------------
# DistilBART: never downloads inside the request
# ---------------------------------------------------------------------------


def test_local_model_not_cached_starts_background_download():
    with (
        patch.object(S, "_load_local_model", side_effect=OSError("not cached")),
        patch.object(S, "_start_local_prep", return_value=True) as start,
        pytest.raises(S.LocalModelNotReady, match="background"),
    ):
        S.summarize_local("some text " * 20)
    start.assert_called_once()
