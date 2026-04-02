"""Summarization provider cascade: Anthropic -> OpenAI -> OpenRouter -> Local."""

import json
import logging
import subprocess
import sys

import requests as http_requests


def sanitize_for_json(text):
    """Sanitize text to be safely embedded in JSON prompts."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", "").replace("\t", " ")


def _extract_summary_json(content):
    """Parse AI response to extract (summary, title) from various formats.

    Handles: raw JSON, code-fenced JSON, JSON with leading text,
    and plain markdown responses.
    """
    content = content.strip()
    # Strip code fences
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        content = content.rsplit("```", 1)[0].strip()
    # Try parsing as JSON
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed.get("summary", content), parsed.get("title", "")
    except json.JSONDecodeError:
        pass
    # Try extracting JSON from within the text (AI sometimes adds preamble)
    idx = content.find("{")
    if idx > 0:
        try:
            parsed = json.loads(content[idx:])
            if isinstance(parsed, dict) and "summary" in parsed:
                return parsed["summary"], parsed.get("title", "")
        except json.JSONDecodeError:
            pass
    return content, ""


def summarize_anthropic(full_text, api_key, tag_context=""):
    """Summarize using Claude Sonnet 4.6."""
    safe_text = sanitize_for_json(full_text[:15000])
    r = http_requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 2048,
            "messages": [
                {
                    "role": "user",
                    "content": f"Analyze and summarize this transcript. Return ONLY a raw JSON object (no code fences) with two fields:\n\n"
                    f'"title": A concise, descriptive title (5-12 words) that captures the core topic.\n\n'
                    f'"summary": A thorough, well-structured summary in markdown format. Follow this structure:\n'
                    f"## Core Synopsis\n"
                    f"A dense paragraph capturing the operative reality — what happened, who was involved, "
                    f"what the outcome was, and what the current status is.\n\n"
                    f"## Key Points\n"
                    f"Use ### subheadings to break down the main topics, decisions, or threads discussed. "
                    f"Under each, use bullet points with specifics. Bold key facts and names.\n\n"
                    f"## Action Items\n"
                    f"If any tasks, follow-ups, or next steps were discussed, list them as:\n"
                    f"- [ ] Task description - [Status/Owner if mentioned]\n\n"
                    f"Be analytical and thorough. Extract the signal from the conversation — don't just "
                    f"paraphrase, synthesize. If the recording is a meeting, focus on decisions made and "
                    f"actions assigned. If it's a monologue or note, focus on the key ideas and structure.\n\n"
                    f"{tag_context}\n\nTranscript:\n{safe_text}",
                }
            ],
        },
        timeout=60,
    )
    r.raise_for_status()
    content = r.json()["content"][0]["text"]
    content = _extract_summary_json(content)
    return content


def summarize_openai(full_text, api_key, tag_context=""):
    """Summarize using GPT-5.4 mini."""
    safe_text = sanitize_for_json(full_text[:15000])
    r = http_requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-5.4-mini",
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": 'You analyze and summarize transcripts. Return JSON with "title" (5-12 word descriptive title) and "summary" (thorough markdown with ## Core Synopsis, ## Key Points with ### subheadings and bullets, and ## Action Items with - [ ] checkboxes). Be analytical — synthesize, don\'t just paraphrase.',
                },
                {"role": "user", "content": f"Analyze and summarize this transcript:{tag_context}\n\n{safe_text}"},
            ],
            "max_completion_tokens": 2048,
        },
        timeout=60,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return _extract_summary_json(content)


def summarize_openrouter(full_text, api_key, model, tag_context=""):
    """Summarize using OpenRouter (OpenAI-compatible API for any model)."""
    safe_text = sanitize_for_json(full_text[:15000])
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
            "messages": [
                {
                    "role": "system",
                    "content": 'You analyze and summarize transcripts. Return JSON with "title" (5-12 word descriptive title) and "summary" (thorough markdown with ## Core Synopsis, ## Key Points with ### subheadings and bullets, and ## Action Items with - [ ] checkboxes). Be analytical — synthesize, don\'t just paraphrase.',
                },
                {"role": "user", "content": f"Analyze and summarize this transcript:{tag_context}\n\n{safe_text}"},
            ],
            "max_tokens": 2048,
        },
        timeout=60,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return _extract_summary_json(content)


def summarize_local(full_text):
    """Summarize using local distilbart model."""
    try:
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    except ImportError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--break-system-packages", "-q", "transformers", "sentencepiece"]
        )
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    model_name = "sshleifer/distilbart-cnn-12-6"
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_name)

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


def summarize(full_text, config, tag_context=""):
    """Run the summarization cascade. Returns (summary, title, provider, errors).

    Respects config['summarization_provider'] preference — tries the preferred
    provider first, then falls back to others.
    """
    preferred = config.get("summarization_provider", "anthropic")
    or_key = config.get("openrouter_api_key")

    # Build all available providers
    all_providers = []
    if config.get("anthropic_api_key"):
        all_providers.append(("Anthropic (Claude Sonnet 4.6)", "anthropic",
            lambda: summarize_anthropic(full_text, config["anthropic_api_key"], tag_context)))
    elif or_key and preferred in ("anthropic", "openrouter"):
        all_providers.append(("OpenRouter (Claude Sonnet 4.6)", "openrouter-claude",
            lambda: summarize_openrouter(full_text, or_key, "anthropic/claude-sonnet-4.6", tag_context)))
    if config.get("openai_api_key"):
        all_providers.append(("OpenAI (GPT-5.4 mini)", "openai",
            lambda: summarize_openai(full_text, config["openai_api_key"], tag_context)))
    elif or_key:
        all_providers.append(("OpenRouter (GPT-5.4 mini)", "openrouter-gpt",
            lambda: summarize_openrouter(full_text, or_key, "openai/gpt-5.4-mini", tag_context)))
    all_providers.append(("Local (DistilBART)", "local", lambda: (summarize_local(full_text), "")))

    # Sort so preferred provider is first
    prov_order = {"anthropic": 0, "openrouter-claude": 0, "openai": 1, "openrouter-gpt": 1, "openrouter": 0, "local": 2}
    preferred_rank = {"anthropic": 0, "openai": 1, "openrouter": 0, "local": 2}.get(preferred, 0)
    attempts = sorted(all_providers, key=lambda p: (0 if prov_order.get(p[1], 9) == preferred_rank else 1, prov_order.get(p[1], 9)))

    errors = []
    for display_name, prov_name, fn in attempts:
        try:
            result = fn()
            if isinstance(result, tuple):
                summary, title = result
            else:
                summary = result
                title = ""
            return summary, title, prov_name, errors
        except http_requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 401:
                errors.append(f"{display_name}: Invalid API key — check your key in Settings")
            elif status == 403:
                errors.append(f"{display_name}: Access denied — your key may lack permissions")
            elif status == 429:
                errors.append(f"{display_name}: Rate limited — too many requests, try again shortly")
            elif status in (402, 400):
                body = ""
                try:
                    body = e.response.json().get("error", {}).get("message", "")
                except Exception:
                    pass
                if any(w in body.lower() for w in ("credit", "billing", "insufficient")):
                    errors.append(f"{display_name}: Insufficient credits — add billing at your provider's dashboard")
                else:
                    errors.append(f"{display_name}: Request error — {body or str(e)}")
            else:
                errors.append(f"{display_name}: HTTP {status} — {str(e)[:100]}")
            logging.warning(f"Summarize with {prov_name} failed: {e}")
        except http_requests.exceptions.ConnectionError:
            errors.append(f"{display_name}: Connection failed — check your internet")
        except http_requests.exceptions.Timeout:
            errors.append(f"{display_name}: Timed out — the service took too long to respond")
        except Exception as e:
            errors.append(f"{display_name}: {str(e)[:120]}")
            logging.warning(f"Summarize with {prov_name} failed: {e}")

    return None, "", "none", errors
