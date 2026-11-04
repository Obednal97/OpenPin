"""Generic structured-output LLM call.

Same provider cascade and preference order as ``summarize()`` (Anthropic ->
OpenAI -> OpenRouter -> local Ollama), minus DistilBART: a seq2seq news
summariser cannot follow a JSON schema, so local-only users get Ollama or
nothing. Each provider is asked for a JSON object matching the caller's schema
(structured outputs); a refusal, a response cut off at the token limit or
unparseable JSON is an error and the cascade moves on.
"""

import json
import logging

import requests as http_requests

from app.providers._models import (
    CLAUDE_SUMMARIZE,
    CLAUDE_SUMMARIZE_EFFORT,
    CLAUDE_SUMMARIZE_MAX_TOKENS,
    OLLAMA_SUGGESTED_MODEL,
    OLLAMA_URL,
    OPENAI_REASONING_EFFORT,
    OPENAI_SUMMARIZE,
    OPENAI_SUMMARIZE_MAX_TOKENS,
    OPENROUTER_CLAUDE_SUMMARIZE,
    OPENROUTER_OPENAI_SUMMARIZE,
)
from app.providers.summarize import (
    _PREFERENCE_GROUP,
    _PROVIDER_GROUP,
    OLLAMA_MAX_CTX,
    SummaryError,
    _anthropic_client,
    _describe_error,
    ollama_models,
    pick_ollama_model,
)
from app.utils.constants import PROJECT_URL


def _parse_json_object(content):
    """Parse a provider's answer into a dict, tolerating a code fence or preamble."""
    content = (content or "").strip()
    if not content:
        raise SummaryError("empty response")
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        content = content.rsplit("```", 1)[0].strip()
    candidates = [content]
    idx = content.find("{")
    if idx > 0:
        candidates.append(content[idx:])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise SummaryError("response was not a valid JSON object")


def _chat_response_format(schema, schema_name):
    return {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}}


def _parse_chat_completion(r, provider):
    r.raise_for_status()
    choice = r.json()["choices"][0]
    message = choice.get("message") or {}
    if message.get("refusal"):
        raise SummaryError(f"{provider} declined: {message['refusal'][:100]}")
    if choice.get("finish_reason") == "length":
        raise SummaryError("response was cut off at the output limit")
    return _parse_json_object(message.get("content"))


def _anthropic_json(api_key, system, user, schema):
    client = _anthropic_client(api_key)
    # Streamed so the large max_tokens can't hit an HTTP timeout; see summarize.py
    # for the fallbacks/betas rationale.
    with client.beta.messages.stream(
        model=CLAUDE_SUMMARIZE,
        max_tokens=CLAUDE_SUMMARIZE_MAX_TOKENS,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={
            "effort": CLAUDE_SUMMARIZE_EFFORT,
            "format": {"type": "json_schema", "schema": schema},
        },
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise SummaryError("Claude declined this request")
    if message.stop_reason == "max_tokens":
        raise SummaryError("response was cut off at the output limit")
    text = "".join(b.text for b in message.content if b.type == "text")
    return _parse_json_object(text)


def _openai_json(api_key, system, user, schema, schema_name):
    r = http_requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": OPENAI_SUMMARIZE,
            "reasoning_effort": OPENAI_REASONING_EFFORT,
            "max_completion_tokens": OPENAI_SUMMARIZE_MAX_TOKENS,
            "response_format": _chat_response_format(schema, schema_name),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
        timeout=(10, 600),
    )
    return _parse_chat_completion(r, "OpenAI")


def _openrouter_json(api_key, model, system, user, schema, schema_name):
    r = http_requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": PROJECT_URL,
            "X-Title": "Open Pin",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": OPENAI_SUMMARIZE_MAX_TOKENS,
            "reasoning": {"effort": "low"},
            "response_format": _chat_response_format(schema, schema_name),
            # Only route to upstreams that honour structured outputs.
            "provider": {"require_parameters": True},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
        timeout=(10, 600),
    )
    return _parse_chat_completion(r, "OpenRouter")


def _ollama_json(model, system, user, schema):
    models = ollama_models()
    if models is None:
        raise SummaryError(f"not running on {OLLAMA_URL}")
    chosen = pick_ollama_model(models, model)
    if not chosen:
        raise SummaryError(f"no chat model pulled (try: ollama pull {OLLAMA_SUGGESTED_MODEL})")
    num_ctx = min(OLLAMA_MAX_CTX, max(8192, (len(system) + len(user)) // 3 + 8192))
    r = http_requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": chosen,
            "stream": False,
            "format": schema,
            "options": {"num_ctx": num_ctx},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
        timeout=(3, 1800),
    )
    r.raise_for_status()
    body = r.json()
    if body.get("done_reason") == "length":
        raise SummaryError("response was cut off at the context limit")
    return _parse_json_object((body.get("message") or {}).get("content"))


def call_llm_json(system, user, schema, config, schema_name="result"):
    """Ask the configured AI providers for a JSON object matching ``schema``.

    Returns ``(parsed, provider_name, errors)``; ``parsed`` is None and the
    provider name is "none" when every provider failed. Provider selection and
    order follow ``summarize()`` (``summarization_provider`` preference, API
    keys and ``ollama_model`` from ``config``), without DistilBART.
    """
    preferred = config.get("summarization_provider", "anthropic")
    or_key = config.get("openrouter_api_key")

    all_providers = []
    if config.get("anthropic_api_key"):
        all_providers.append(
            (
                f"Anthropic ({CLAUDE_SUMMARIZE})",
                "anthropic",
                lambda: _anthropic_json(config["anthropic_api_key"], system, user, schema),
            )
        )
    elif or_key and preferred in ("anthropic", "openrouter"):
        all_providers.append(
            (
                f"OpenRouter ({OPENROUTER_CLAUDE_SUMMARIZE})",
                "openrouter-claude",
                lambda: _openrouter_json(or_key, OPENROUTER_CLAUDE_SUMMARIZE, system, user, schema, schema_name),
            )
        )
    if config.get("openai_api_key"):
        all_providers.append(
            (
                f"OpenAI ({OPENAI_SUMMARIZE})",
                "openai",
                lambda: _openai_json(config["openai_api_key"], system, user, schema, schema_name),
            )
        )
    elif or_key:
        all_providers.append(
            (
                f"OpenRouter ({OPENROUTER_OPENAI_SUMMARIZE})",
                "openrouter-gpt",
                lambda: _openrouter_json(or_key, OPENROUTER_OPENAI_SUMMARIZE, system, user, schema, schema_name),
            )
        )
    all_providers.append(
        ("Ollama (local)", "ollama", lambda: _ollama_json(config.get("ollama_model"), system, user, schema))
    )

    preferred_group = _PREFERENCE_GROUP.get(preferred, 0)
    attempts = [
        p
        for _, p in sorted(
            enumerate(all_providers),
            key=lambda ip: (
                0 if _PROVIDER_GROUP.get(ip[1][1], 9) == preferred_group else 1,
                _PROVIDER_GROUP.get(ip[1][1], 9),
                ip[0],
            ),
        )
    ]

    errors = []
    for display_name, prov_name, fn in attempts:
        try:
            return fn(), prov_name, errors
        except Exception as e:
            errors.append(_describe_error(display_name, e))
            logging.warning(f"Structured LLM call with {prov_name} failed: {e}")
    return None, "none", errors
