"""Transcription provider logic: OpenAI Whisper API."""

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
