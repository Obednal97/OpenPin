"""Transcription providers: Deepgram Nova-3, Groq Whisper, OpenAI gpt-transcribe.

Language handling: ``language=None`` means auto-detect. Groq and OpenAI then
omit the language field entirely; Deepgram sends ``detect_language=true``.
"""

import mimetypes
import re
from pathlib import Path
from urllib.parse import quote

import requests as http_requests

from app.providers._models import DEEPGRAM_TRANSCRIBE, GROQ_TRANSCRIBE, OPENAI_TRANSCRIBE

# Speaking-rate guess used only when the audio duration can't be read.
_WORDS_PER_SECOND = 2.5
# Target length of an estimated segment (gpt-transcribe returns no timestamps).
_EST_SEGMENT_SECONDS = 20.0


def _audio_mime(audio_path):
    mime, _ = mimetypes.guess_type(str(audio_path))
    if mime and mime.startswith("audio/"):
        return mime
    return "audio/ogg"  # recordings from the device are Ogg Opus


def _ogg_duration(audio_path):
    """Duration of an Ogg Opus file from its last page's granule position.

    Returns seconds, or None if the file isn't Ogg Opus or can't be read.
    """
    try:
        path = Path(audio_path)
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(4096)
            f.seek(max(0, size - 65536))
            tail = f.read()
    except OSError:
        return None
    if not head.startswith(b"OggS"):
        return None
    op = head.find(b"OpusHead")
    if op < 0 or len(head) < op + 12:
        return None
    pre_skip = int.from_bytes(head[op + 10 : op + 12], "little")
    idx = tail.rfind(b"OggS")
    if idx < 0 or len(tail) < idx + 14:
        return None
    granule = int.from_bytes(tail[idx + 6 : idx + 14], "little")
    if granule <= pre_skip or granule == 0xFFFFFFFFFFFFFFFF:
        return None
    return (granule - pre_skip) / 48000.0  # Opus granule rate is always 48 kHz


def estimate_segments(text, duration):
    """Split text into sentence-grouped segments with estimated timestamps.

    Used for providers that return text without timestamps. Times are spread
    over ``duration`` in proportion to character count, so they are
    approximate (good enough to seek near the right place).
    """
    text = (text or "").strip()
    if not text:
        return []
    if not duration or duration <= 0:
        duration = max(1.0, len(text.split()) / _WORDS_PER_SECOND)
    sentences = [s for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip()]
    total_chars = sum(len(s) for s in sentences) or 1
    target_chars = max(1, int(total_chars * _EST_SEGMENT_SECONDS / duration))

    groups, current, current_len = [], [], 0
    for s in sentences:
        current.append(s)
        current_len += len(s)
        if current_len >= target_chars:
            groups.append(current)
            current, current_len = [], 0
    if current:
        groups.append(current)

    segments, consumed = [], 0
    for g in groups:
        g_len = sum(len(s) for s in g)
        start = duration * consumed / total_chars
        consumed += g_len
        end = duration * consumed / total_chars
        segments.append({"start": round(start, 2), "end": round(end, 2), "text": " ".join(g).strip(), "speaker": None})
    return segments


def _clean_keywords(keywords):
    """Keywords must be single-line and free of '<' / '>' (OpenAI guide)."""
    out = []
    for kw in keywords or []:
        kw = re.sub(r"[<>\r\n]", " ", str(kw)).strip()
        if kw and kw not in out:
            out.append(kw)
    return out


def transcribe_openai(audio_path, api_key, language=None, vocab_prompt=None, keywords=None):
    """Transcribe using OpenAI gpt-transcribe.

    gpt-transcribe returns text (and detected languages) but no timestamps, so
    the segments are estimated from the audio duration and flagged with
    ``timestamps_estimated``.
    """
    data = {"model": OPENAI_TRANSCRIBE}
    if language:
        data["languages[]"] = [language]
    if vocab_prompt:
        data["prompt"] = vocab_prompt
    kws = _clean_keywords(keywords)
    if kws:
        data["keywords[]"] = kws
    with open(audio_path, "rb") as f:
        r = http_requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (Path(audio_path).name, f, _audio_mime(audio_path))},
            data=data,
            timeout=300,
        )
    r.raise_for_status()
    body = r.json()
    text = (body.get("text") or "").strip()

    duration = _ogg_duration(audio_path)
    usage = body.get("usage") or {}
    if not duration and usage.get("type") == "duration":
        duration = usage.get("seconds")
    detected = body.get("languages") or []
    segments = estimate_segments(text, duration)
    if not duration and segments:
        duration = segments[-1]["end"]

    return {
        "text": text,
        "segments": segments,
        "duration": duration or 0,
        "language": detected[0] if detected else (language or ""),
        "timestamps_estimated": True,
    }


def transcribe_groq(audio_path, api_key, language=None, vocab_prompt=None):
    """Transcribe using Groq Whisper (OpenAI-compatible, returns segment timestamps)."""
    data = {
        "model": GROQ_TRANSCRIBE,
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
    }
    if language:
        data["language"] = language
    if vocab_prompt:
        data["prompt"] = vocab_prompt
    with open(audio_path, "rb") as f:
        r = http_requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (Path(audio_path).name, f, _audio_mime(audio_path))},
            data=data,
            timeout=300,
        )
    r.raise_for_status()
    return r.json()


def transcribe_deepgram(audio_path, api_key, language=None, diarize=False, keywords=None):
    """Transcribe using Deepgram Nova-3. Returns OpenAI-compatible result shape."""
    params = {
        "model": DEEPGRAM_TRANSCRIBE,
        "punctuate": "true",
        "smart_format": "true",
        "utterances": "true",
    }
    if language and language != "auto":
        params["language"] = language
    else:
        params["detect_language"] = "true"
    if diarize:
        params["diarize"] = "true"
    # Build query string manually — keywords need special handling
    # (repeated params with colons that shouldn't be encoded by requests lib)
    qs_parts = [f"{k}={v}" for k, v in params.items()]
    if keywords:
        for kw in keywords:
            qs_parts.append(f"keyterm={quote(kw)}")
    query_string = "&".join(qs_parts)

    with open(audio_path, "rb") as f:
        r = http_requests.post(
            f"https://api.deepgram.com/v1/listen?{query_string}",
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": _audio_mime(audio_path),
            },
            data=f,
            timeout=300,
        )
    r.raise_for_status()
    data = r.json()

    # Extract results
    channel = data.get("results", {}).get("channels", [{}])[0]
    alt = channel.get("alternatives", [{}])[0]
    full_text = alt.get("transcript", "")
    detected_lang = channel.get("detected_language") or (language if language != "auto" else None) or ""
    metadata = data.get("metadata", {})
    duration = metadata.get("duration", 0)

    # Build segments from utterances (includes speaker labels if diarize=true)
    segments = []
    for utt in data.get("results", {}).get("utterances", []):
        speaker = None
        if diarize and utt.get("speaker") is not None:
            speaker = f"Speaker {utt['speaker'] + 1}"
        segments.append({
            "start": utt.get("start", 0),
            "end": utt.get("end", 0),
            "text": utt.get("transcript", "").strip(),
            "speaker": speaker,
        })

    # Collect unique speakers
    speakers = list({s["speaker"] for s in segments if s.get("speaker")})

    return {
        "text": full_text,
        "segments": segments,
        "duration": duration,
        "language": detected_lang,
        "speakers": speakers,
    }
