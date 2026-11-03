"""Summarization provider cascade.

Anthropic -> OpenAI -> OpenRouter -> local Ollama (if running) -> DistilBART.

Cloud providers and Ollama get the full transcript as speaker-labelled lines
(``[mm:ss] Speaker 1: text``) and return a ``{"title", "summary"}`` object
enforced by a JSON schema (structured outputs), so a summary is never a
truncated JSON fragment: a response cut off by the token limit is raised as an
error and the cascade moves on.
"""

import json
import logging
import re
import textwrap
import threading

import requests as http_requests

try:  # Official Anthropic SDK; missing only if the dependency install failed.
    import anthropic
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    anthropic = None

from app.providers._models import (
    CLAUDE_SUMMARIZE,
    CLAUDE_SUMMARIZE_EFFORT,
    CLAUDE_SUMMARIZE_MAX_TOKENS,
    LOCAL_SUMMARIZE,
    OLLAMA_PREFERRED_MODELS,
    OLLAMA_SUGGESTED_MODEL,
    OLLAMA_URL,
    OPENAI_REASONING_EFFORT,
    OPENAI_SUMMARIZE,
    OPENAI_SUMMARIZE_MAX_TOKENS,
    OPENROUTER_CLAUDE_SUMMARIZE,
    OPENROUTER_OPENAI_SUMMARIZE,
)

# Cloud models used here have ~1M-token context windows; a transcript is only
# split (map-reduce) beyond this size (~375k tokens, roughly 40+ hours of talk).
CLOUD_SINGLE_PASS_CHARS = 1_500_000
# Local Ollama models have much smaller windows; split beyond ~15k tokens.
OLLAMA_SINGLE_PASS_CHARS = 60_000
OLLAMA_MAX_CTX = 131_072

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "A concise, descriptive title (5-12 words) that captures the core topic.",
        },
        "summary": {
            "type": "string",
            "description": "The full summary as markdown, following the required structure.",
        },
    },
    "required": ["title", "summary"],
    "additionalProperties": False,
}

SUMMARY_INSTRUCTIONS = """You analyze and summarize transcripts of voice recordings.

The transcript is given one line per segment as `[mm:ss] Speaker: text`. Speaker labels may be generic (Speaker 1, Speaker 2) or missing; when they are present, attribute decisions, opinions and commitments to the right speaker. Timestamps are for reference — cite one only when it helps the reader find a moment.

Return a JSON object with two fields:

"title": A concise, descriptive title (5-12 words) that captures the core topic.

"summary": A thorough, well-structured summary in markdown, following this structure:
## Core Synopsis
A dense paragraph capturing the operative reality — what happened, who was involved, what the outcome was, and what the current status is.

## Key Points
Use ### subheadings to break down the main topics, decisions, or threads discussed. Under each, use bullet points with specifics. Bold key facts and names.

## Action Items
If any tasks, follow-ups, or next steps were discussed, list them as:
- [ ] Task description - [Status/Owner if mentioned]

Be analytical and thorough. Extract the signal from the conversation — don't just paraphrase, synthesize. If the recording is a meeting, focus on decisions made and actions assigned. If it's a monologue or note, focus on the key ideas and structure. Write the summary in the language of the transcript unless told otherwise below."""


class SummaryError(Exception):
    """A provider returned no usable summary (refusal, truncation, bad JSON)."""


class LocalModelNotReady(SummaryError):
    """The local summariser isn't installed/downloaded yet (download started)."""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _fmt_ts(seconds):
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    m, s = divmod(max(total, 0), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_transcript(segments, fallback_text=""):
    """Render segments as ``[mm:ss] Speaker: text`` lines.

    Falls back to ``fallback_text`` when there are no usable segments.
    Nothing is escaped: the HTTP clients JSON-encode the request body.
    """
    lines = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker = (seg.get("speaker") or "").strip() if isinstance(seg.get("speaker"), str) else ""
        prefix = f"[{_fmt_ts(seg.get('start'))}]"
        lines.append(f"{prefix} {speaker}: {text}" if speaker else f"{prefix} {text}")
    if lines:
        return "\n".join(lines)
    return (fallback_text or "").strip()


def _system_prompt(context):
    context = (context or "").strip()
    return f"{SUMMARY_INSTRUCTIONS}\n\n{context}" if context else SUMMARY_INSTRUCTIONS


def _transcript_message(transcript, part=None):
    if part:
        i, n = part
        lead = (
            f"This is part {i} of {n} of the transcript of one long recording. Summarize this part "
            f"using the same structure; a later step merges the parts into one summary."
        )
    else:
        lead = "Summarize this transcript."
    return f"{lead}\n\n<transcript>\n{transcript}\n</transcript>"


def _merge_message(part_summaries):
    return (
        "These are summaries of consecutive parts of one long recording, in order. Merge them into a "
        "single summary of the whole recording using the same structure, removing repetition and "
        "keeping every action item.\n\n<part_summaries>\n" + part_summaries + "\n</part_summaries>"
    )


def _chunk_text(text, max_chars):
    """Split on line boundaries into chunks of at most ``max_chars``."""
    chunks, cur, size = [], [], 0
    for line in text.split("\n"):
        pieces = [line] if len(line) <= max_chars else textwrap.wrap(line, max_chars, break_long_words=True)
        for piece in pieces:
            if cur and size + len(piece) + 1 > max_chars:
                chunks.append("\n".join(cur))
                cur, size = [], 0
            cur.append(piece)
            size += len(piece) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _run_summary(call, transcript, context, max_chars):
    """Summarize in one call, or map-reduce if the transcript is truly huge.

    ``call(system, user_content) -> (summary, title)``.
    """
    system = _system_prompt(context)
    if len(transcript) <= max_chars:
        return call(system, _transcript_message(transcript))
    chunks = _chunk_text(transcript, max_chars)
    logging.info("Transcript is %d chars; summarizing in %d parts", len(transcript), len(chunks))
    notes = []
    for i, chunk in enumerate(chunks, 1):
        part_summary, _ = call(system, _transcript_message(chunk, part=(i, len(chunks))))
        notes.append(f"## Part {i} of {len(chunks)}\n{part_summary}")
    combined = "\n\n".join(notes)
    while len(combined) > max_chars:  # pathological: merge the merges
        combined = "\n\n".join(call(system, _merge_message(c))[0] for c in _chunk_text(combined, max_chars))
    return call(system, _merge_message(combined))


def _parse_summary(content):
    """Parse a provider's JSON answer into (summary, title).

    Structured outputs make this a plain ``json.loads``; the tolerant paths
    cover code fences or preamble from providers without schema enforcement.
    A JSON-looking answer that doesn't parse is an error, never a summary.
    """
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
        if isinstance(parsed, dict) and isinstance(parsed.get("summary"), str) and parsed["summary"].strip():
            return parsed["summary"].strip(), str(parsed.get("title") or "").strip()
    if content.startswith("{"):
        raise SummaryError("response was not valid summary JSON")
    return content, ""  # plain markdown answer


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def _anthropic_client(api_key):
    if anthropic is None:
        raise SummaryError("the Anthropic SDK (Python package 'anthropic') isn't installed")
    return anthropic.Anthropic(api_key=api_key, timeout=600.0, max_retries=2)


def _anthropic_call(client):
    def call(system, user_content):
        # Streamed so the large max_tokens can't hit an HTTP timeout.
        # fallbacks="default": if a safety classifier declines, the API retries
        # server-side on Anthropic's recommended fallback model.
        with client.beta.messages.stream(
            model=CLAUDE_SUMMARIZE,
            max_tokens=CLAUDE_SUMMARIZE_MAX_TOKENS,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": CLAUDE_SUMMARIZE_EFFORT,
                "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA},
            },
            system=system,
            messages=[{"role": "user", "content": user_content}],
        ) as stream:
            message = stream.get_final_message()
        if message.stop_reason == "refusal":
            raise SummaryError("Claude declined to summarize this transcript")
        if message.stop_reason == "max_tokens":
            raise SummaryError("summary was cut off at the output limit")
        # All text blocks: after a mid-stream decline the fallback model's text
        # continues the partial text, with a fallback marker block in between.
        text = "".join(b.text for b in message.content if b.type == "text")
        return _parse_summary(text)

    return call


def summarize_anthropic(transcript, api_key, tag_context=""):
    """Summarize with Claude (CLAUDE_SUMMARIZE) via the official SDK."""
    call = _anthropic_call(_anthropic_client(api_key))
    return _run_summary(call, transcript, tag_context, CLOUD_SINGLE_PASS_CHARS)


def _chat_json_response_format():
    return {
        "type": "json_schema",
        "json_schema": {"name": "transcript_summary", "strict": True, "schema": SUMMARY_SCHEMA},
    }


def _parse_chat_completion(r, provider):
    r.raise_for_status()
    choice = r.json()["choices"][0]
    message = choice.get("message") or {}
    if message.get("refusal"):
        raise SummaryError(f"{provider} declined to summarize: {message['refusal'][:100]}")
    if choice.get("finish_reason") == "length":
        raise SummaryError("summary was cut off at the output limit")
    return _parse_summary(message.get("content"))


def summarize_openai(transcript, api_key, tag_context=""):
    """Summarize with OpenAI (OPENAI_SUMMARIZE) via Chat Completions."""

    def call(system, user_content):
        r = http_requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": OPENAI_SUMMARIZE,
                "reasoning_effort": OPENAI_REASONING_EFFORT,
                # Includes reasoning tokens, so it's well above the summary size.
                "max_completion_tokens": OPENAI_SUMMARIZE_MAX_TOKENS,
                "response_format": _chat_json_response_format(),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
            },
            timeout=(10, 600),
        )
        return _parse_chat_completion(r, "OpenAI")

    return _run_summary(call, transcript, tag_context, CLOUD_SINGLE_PASS_CHARS)


def summarize_openrouter(transcript, api_key, model, tag_context=""):
    """Summarize with OpenRouter (OpenAI-compatible API for any model)."""

    def call(system, user_content):
        r = http_requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/Obednal97/Plaud-V2",
                "X-Title": "Plaud V2",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": OPENAI_SUMMARIZE_MAX_TOKENS,
                "reasoning": {"effort": "low"},
                "response_format": _chat_json_response_format(),
                # Only route to upstreams that honour structured outputs.
                "provider": {"require_parameters": True},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
            },
            timeout=(10, 600),
        )
        return _parse_chat_completion(r, "OpenRouter")

    return _run_summary(call, transcript, tag_context, CLOUD_SINGLE_PASS_CHARS)


def ollama_models(timeout=1.0):
    """Return the models pulled into a local Ollama server, or None if unreachable."""
    try:
        r = http_requests.get(f"{OLLAMA_URL}/api/tags", timeout=(0.5, timeout))
        r.raise_for_status()
        return [m.get("name") or m.get("model") for m in r.json().get("models", []) if m.get("name") or m.get("model")]
    except Exception:
        return None


def pick_ollama_model(models, preferred=None):
    """Choose a chat model: the configured one, then a known-good family, then any."""
    if not models:
        return None
    if preferred:
        for m in models:
            if m == preferred or m.split(":")[0] == preferred:
                return m
    # Never pick an embedding model, or a "-cloud" tag (that runs on Ollama's
    # servers, so the transcript would leave the Mac) unless the user chose it.
    chat_models = [m for m in models if "embed" not in m.lower() and "cloud" not in _ollama_tag(m)]
    for family in OLLAMA_PREFERRED_MODELS:
        in_family = [m for m in chat_models if m.split(":")[0] == family]
        if in_family:
            return max(in_family, key=_ollama_size_b)  # the biggest one pulled
    return chat_models[0] if chat_models else None


def _ollama_tag(model):
    return model.split(":", 1)[1].lower() if ":" in model else "latest"


def _ollama_size_b(model):
    """Billions of parameters from the tag ("27b", "e4b", "0.6b"); 8 for "latest" and the like."""
    m = re.search(r"(\d+(?:\.\d+)?)b", _ollama_tag(model))
    return float(m.group(1)) if m else 8.0


def summarize_ollama(transcript, tag_context="", model=None):
    """Summarize with a local Ollama server (only if one is running)."""
    models = ollama_models()
    if models is None:
        raise SummaryError(f"not running on {OLLAMA_URL}")
    chosen = pick_ollama_model(models, model)
    if not chosen:
        raise SummaryError(f"no chat model pulled (try: ollama pull {OLLAMA_SUGGESTED_MODEL})")

    def call(system, user_content):
        prompt_chars = len(system) + len(user_content)
        num_ctx = min(OLLAMA_MAX_CTX, max(8192, prompt_chars // 3 + 8192))
        r = http_requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": chosen,
                "stream": False,
                "format": SUMMARY_SCHEMA,
                "options": {"num_ctx": num_ctx},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
            },
            timeout=(3, 1800),
        )
        r.raise_for_status()
        body = r.json()
        if body.get("done_reason") == "length":
            raise SummaryError("summary was cut off at the context limit")
        return _parse_summary((body.get("message") or {}).get("content"))

    summary, title = _run_summary(call, transcript, tag_context, OLLAMA_SINGLE_PASS_CHARS)
    return summary, title, chosen


# --- DistilBART (last resort) ------------------------------------------------

_local_prep_lock = threading.Lock()
_local_prep_thread = None
_local_prep_error = None


def _prepare_local_model():
    """Install transformers and download DistilBART (background thread)."""
    global _local_prep_error
    try:
        try:
            import transformers  # noqa: F401
        except ImportError:
            logging.info("Installing transformers for the local summariser...")
            from app.utils.system import ensure_installed

            # Pinned extra (transformers + sentencepiece + torch), installed into the
            # app's managed venv; raises with pip's error if it fails.
            ensure_installed("transformers", required=True, extra="local-summary")
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        logging.info("Downloading local summary model %s (~1.2 GB)...", LOCAL_SUMMARIZE)
        AutoTokenizer.from_pretrained(LOCAL_SUMMARIZE)
        AutoModelForSeq2SeqLM.from_pretrained(LOCAL_SUMMARIZE)
        _local_prep_error = None
        logging.info("Local summary model ready")
    except Exception as e:
        _local_prep_error = str(e)[:200]
        logging.warning("Preparing the local summary model failed: %s", e)


def _start_local_prep():
    """Start the background download once; returns True if it is running."""
    global _local_prep_thread
    with _local_prep_lock:
        if _local_prep_thread is not None and _local_prep_thread.is_alive():
            return True
        _local_prep_thread = threading.Thread(target=_prepare_local_model, daemon=True, name="distilbart-prep")
        _local_prep_thread.start()
        return True


def _load_local_model():
    """Load DistilBART from the local cache only — never downloads."""
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(LOCAL_SUMMARIZE, local_files_only=True)
    model = AutoModelForSeq2SeqLM.from_pretrained(LOCAL_SUMMARIZE, local_files_only=True)
    return tokenizer, model


def summarize_local(full_text):
    """Summarize using the local DistilBART model (news-style, no title).

    The model (~1.2 GB) is never downloaded inside the request: if it isn't
    cached yet, a background download starts and LocalModelNotReady explains
    what's happening.
    """
    try:
        tokenizer, model = _load_local_model()
    except Exception:
        previous_error = _local_prep_error
        _start_local_prep()
        msg = (
            "the local summary model (DistilBART, ~1.2 GB) is being downloaded in the background — "
            "try again in a few minutes. For better summaries, add an API key or run Ollama."
        )
        if previous_error:
            msg += f" (last attempt failed: {previous_error})"
        raise LocalModelNotReady(msg)

    max_chunk = 2500
    chunks = []
    words = full_text.split()
    current = []
    current_len = 0
    for w in words:
        current.append(w)
        current_len += len(w) + 1
        if current_len >= max_chunk:
            chunks.append(" ".join(current))
            current = []
            current_len = 0
    if current:
        chunks.append(" ".join(current))

    summaries = []
    for chunk in chunks:
        if len(chunk.strip()) < 30:
            continue
        inputs = tokenizer(chunk, return_tensors="pt", max_length=1024, truncation=True)
        output_ids = model.generate(inputs["input_ids"], max_length=120, min_length=20, do_sample=False)
        summaries.append(tokenizer.decode(output_ids[0], skip_special_tokens=True))

    return " ".join(summaries).strip()


# ---------------------------------------------------------------------------
# Cascade
# ---------------------------------------------------------------------------

# Provider group: 0 = Claude, 1 = GPT, 2 = local (Ollama, then DistilBART).
_PROVIDER_GROUP = {"anthropic": 0, "openrouter-claude": 0, "openai": 1, "openrouter-gpt": 1, "ollama": 2, "local": 2}
_PREFERENCE_GROUP = {"anthropic": 0, "openrouter": 0, "openai": 1, "local": 2, "ollama": 2}


def _error_status_and_body(e):
    """(HTTP status, provider error message) for requests or Anthropic SDK errors."""
    if isinstance(e, http_requests.exceptions.HTTPError):
        resp = e.response
        body = ""
        try:
            body = resp.json().get("error", {}).get("message", "")
        except Exception:
            pass
        return (resp.status_code if resp is not None else 0), body
    if anthropic is not None and isinstance(e, anthropic.APIStatusError):
        body = ""
        if isinstance(e.body, dict):
            err = e.body.get("error")
            body = err.get("message", "") if isinstance(err, dict) else ""
        return e.status_code, body or str(e)
    return None, ""


def _describe_error(display_name, e):
    status, body = _error_status_and_body(e)
    if status is not None:
        if status == 401:
            return f"{display_name}: Invalid API key — check your key in Settings"
        if status == 403:
            return f"{display_name}: Access denied — your key may lack permissions"
        if status == 429:
            return f"{display_name}: Rate limited — too many requests, try again shortly"
        if status in (400, 402):
            if any(w in body.lower() for w in ("credit", "billing", "insufficient")):
                return f"{display_name}: Insufficient credits — add billing at your provider's dashboard"
            return f"{display_name}: Request error — {(body or str(e))[:150]}"
        return f"{display_name}: HTTP {status} — {str(e)[:100]}"
    timeout_types = (http_requests.exceptions.Timeout,)
    conn_types = (http_requests.exceptions.ConnectionError,)
    if anthropic is not None:
        timeout_types += (anthropic.APITimeoutError,)
        conn_types += (anthropic.APIConnectionError,)
    if isinstance(e, timeout_types):
        return f"{display_name}: Timed out — the service took too long to respond"
    if isinstance(e, conn_types):
        return f"{display_name}: Connection failed — check your internet"
    return f"{display_name}: {str(e)[:200]}"


def summarize(full_text, config, tag_context="", segments=None):
    """Run the summarization cascade. Returns (summary, title, provider, errors).

    ``segments`` (the transcript's timed, speaker-labelled segments) are sent
    to the LLM providers as ``[mm:ss] Speaker: text`` lines; ``full_text`` is
    used when there are none, and always for DistilBART.

    Respects config['summarization_provider'] — tries the preferred provider
    first, then falls back to the others. "local" means Ollama (if running)
    and then DistilBART.
    """
    preferred = config.get("summarization_provider", "anthropic")
    or_key = config.get("openrouter_api_key")
    transcript = format_transcript(segments, full_text)

    all_providers = []
    if config.get("anthropic_api_key"):
        all_providers.append(
            (
                f"Anthropic ({CLAUDE_SUMMARIZE})",
                "anthropic",
                lambda: summarize_anthropic(transcript, config["anthropic_api_key"], tag_context),
            )
        )
    elif or_key and preferred in ("anthropic", "openrouter"):
        all_providers.append(
            (
                f"OpenRouter ({OPENROUTER_CLAUDE_SUMMARIZE})",
                "openrouter-claude",
                lambda: summarize_openrouter(transcript, or_key, OPENROUTER_CLAUDE_SUMMARIZE, tag_context),
            )
        )
    if config.get("openai_api_key"):
        all_providers.append(
            (
                f"OpenAI ({OPENAI_SUMMARIZE})",
                "openai",
                lambda: summarize_openai(transcript, config["openai_api_key"], tag_context),
            )
        )
    elif or_key:
        all_providers.append(
            (
                f"OpenRouter ({OPENROUTER_OPENAI_SUMMARIZE})",
                "openrouter-gpt",
                lambda: summarize_openrouter(transcript, or_key, OPENROUTER_OPENAI_SUMMARIZE, tag_context),
            )
        )
    all_providers.append(
        ("Ollama (local)", "ollama", lambda: summarize_ollama(transcript, tag_context, config.get("ollama_model"))[:2])
    )
    all_providers.append(("Local (DistilBART)", "local", lambda: (summarize_local(full_text), "")))

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
            result = fn()
            if isinstance(result, tuple):
                summary, title = result
            else:
                summary, title = result, ""
            if not summary or not str(summary).strip():
                raise SummaryError("empty summary")
            return summary, title, prov_name, errors
        except Exception as e:
            errors.append(_describe_error(display_name, e))
            logging.warning(f"Summarize with {prov_name} failed: {e}")

    return None, "", "none", errors
