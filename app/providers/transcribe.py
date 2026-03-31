"""Transcription providers: Deepgram Nova-3, Groq Whisper, OpenAI Whisper."""

from pathlib import Path

import requests as http_requests


def transcribe_openai(audio_path, api_key, language="en", vocab_prompt=None):
    """Transcribe using OpenAI Whisper API."""
    data = {
        "model": "whisper-1",
        "language": language or "en",
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
    }
    if vocab_prompt:
        data["prompt"] = vocab_prompt
    with open(audio_path, "rb") as f:
        r = http_requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (Path(audio_path).name, f, "audio/ogg")},
            data=data,
            timeout=120,
        )
    r.raise_for_status()
    return r.json()


def transcribe_groq(audio_path, api_key, language="en", vocab_prompt=None):
    """Transcribe using Groq Whisper (OpenAI-compatible, ~10x faster)."""
    data = {
        "model": "whisper-large-v3",
        "language": language or "en",
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
    }
    if vocab_prompt:
        data["prompt"] = vocab_prompt
    with open(audio_path, "rb") as f:
        r = http_requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (Path(audio_path).name, f, "audio/ogg")},
            data=data,
            timeout=120,
        )
    r.raise_for_status()
    return r.json()


def transcribe_deepgram(audio_path, api_key, language="en", diarize=False, keywords=None):
    """Transcribe using Deepgram Nova-3. Returns OpenAI-compatible result shape."""
    params = {
        "model": "nova-3",
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
    from urllib.parse import quote
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
                "Content-Type": "audio/ogg",
            },
            data=f,
            timeout=120,
        )
    r.raise_for_status()
    data = r.json()

    # Extract results
    channel = data.get("results", {}).get("channels", [{}])[0]
    alt = channel.get("alternatives", [{}])[0]
    full_text = alt.get("transcript", "")
    detected_lang = channel.get("detected_language", language or "en")
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
