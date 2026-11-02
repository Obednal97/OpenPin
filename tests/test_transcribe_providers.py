"""Transcription provider request construction and language handling (no live calls)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.providers import transcribe as T
from app.providers._models import DEEPGRAM_TRANSCRIBE, GROQ_TRANSCRIBE, OPENAI_TRANSCRIBE
from app.utils.audio import wrap_opus_ogg
from app.utils.constants import WHISPER_MODELS


def _resp(body):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = body
    r.raise_for_status.return_value = None
    return r


@pytest.fixture
def ogg(tmp_path):
    """A 1-second Ogg Opus file (50 x 20 ms frames)."""
    frame = bytes([0xB8]) + bytes(79)  # TOC 0xb8: 20 ms CELT frame
    path = tmp_path / "rec.ogg"
    path.write_bytes(wrap_opus_ogg(frame * 50, frame_size=80))
    return path


# ---------------------------------------------------------------------------
# OpenAI gpt-transcribe
# ---------------------------------------------------------------------------


@patch("app.providers.transcribe.http_requests.post")
def test_openai_auto_language_omits_language_fields(mock_post, ogg):
    mock_post.return_value = _resp({"text": "Bonjour. Ça va?", "languages": ["fr"]})
    out = T.transcribe_openai(str(ogg), "sk", None, vocab_prompt="Acme", keywords=["Acme", "Plaud\nV2", "<x>"])
    data = mock_post.call_args.kwargs["data"]
    assert data["model"] == OPENAI_TRANSCRIBE == "gpt-transcribe"
    assert "language" not in data and "languages[]" not in data
    assert data["prompt"] == "Acme"
    assert data["keywords[]"] == ["Acme", "Plaud V2", "x"]
    assert "timestamp_granularities[]" not in data  # whisper-1 only
    assert out["language"] == "fr"
    assert out["timestamps_estimated"] is True
    assert out["segments"] and out["segments"][0]["start"] == 0


@patch("app.providers.transcribe.http_requests.post")
def test_openai_explicit_language(mock_post, ogg):
    mock_post.return_value = _resp({"text": "Hello.", "languages": []})
    out = T.transcribe_openai(str(ogg), "sk", "de")
    assert mock_post.call_args.kwargs["data"]["languages[]"] == ["de"]
    assert out["language"] == "de"


def test_ogg_duration(ogg):
    assert T._ogg_duration(str(ogg)) == pytest.approx(1.0, abs=0.05)


def test_ogg_duration_non_ogg(tmp_path):
    p = tmp_path / "x.mp3"
    p.write_bytes(b"ID3" + bytes(100))
    assert T._ogg_duration(str(p)) is None


def test_estimate_segments_spread_over_duration():
    text = " ".join(f"Sentence number {i} is here." for i in range(40))
    segs = T.estimate_segments(text, 120.0)
    assert len(segs) > 3
    assert segs[0]["start"] == 0
    assert segs[-1]["end"] == pytest.approx(120.0)
    assert all(a["end"] == b["start"] for a, b in zip(segs, segs[1:], strict=False))
    assert " ".join(s["text"] for s in segs) == text
    assert T.estimate_segments("", 10) == []


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------


@patch("app.providers.transcribe.http_requests.post")
def test_groq_auto_language_omitted(mock_post, ogg):
    mock_post.return_value = _resp({"text": "hi", "segments": []})
    T.transcribe_groq(str(ogg), "gsk", None, vocab_prompt="Acme")
    data = mock_post.call_args.kwargs["data"]
    assert data["model"] == GROQ_TRANSCRIBE
    assert "language" not in data
    assert data["response_format"] == "verbose_json"
    assert data["timestamp_granularities[]"] == "segment"


@patch("app.providers.transcribe.http_requests.post")
def test_groq_explicit_language(mock_post, ogg):
    mock_post.return_value = _resp({"text": "hi", "segments": []})
    T.transcribe_groq(str(ogg), "gsk", "es")
    assert mock_post.call_args.kwargs["data"]["language"] == "es"


# ---------------------------------------------------------------------------
# Deepgram
# ---------------------------------------------------------------------------

DG_BODY = {
    "metadata": {"duration": 3.0},
    "results": {
        "channels": [{"alternatives": [{"transcript": "hola"}], "detected_language": "es"}],
        "utterances": [{"start": 0, "end": 3, "transcript": "hola", "speaker": 0}],
    },
}


@pytest.mark.parametrize("lang", [None, "auto"])
@patch("app.providers.transcribe.http_requests.post")
def test_deepgram_auto_detects(mock_post, lang, ogg):
    mock_post.return_value = _resp(DG_BODY)
    out = T.transcribe_deepgram(str(ogg), "dg", lang)
    url = mock_post.call_args.args[0]
    assert f"model={DEEPGRAM_TRANSCRIBE}" in url
    assert "detect_language=true" in url
    assert "language=" not in url.replace("detect_language=", "")
    assert out["language"] == "es"


@patch("app.providers.transcribe.http_requests.post")
def test_deepgram_explicit_language(mock_post, ogg):
    body = json.loads(json.dumps(DG_BODY))
    del body["results"]["channels"][0]["detected_language"]
    mock_post.return_value = _resp(body)
    out = T.transcribe_deepgram(str(ogg), "dg", "en-GB", diarize=True)
    url = mock_post.call_args.args[0]
    assert "language=en-GB" in url and "detect_language" not in url
    assert out["language"] == "en-GB"
    assert out["speakers"] == ["Speaker 1"]


# ---------------------------------------------------------------------------
# Route: "auto" reaches every cloud provider as None
# ---------------------------------------------------------------------------


@pytest.fixture
def route_env(flask_client, tmp_path):
    rec = tmp_path / "recordings"
    trn = tmp_path / "transcripts"
    rec.mkdir()
    trn.mkdir()
    with (
        patch("app.routes.transcribe.TRANSCRIPTS_DIR", trn),
        patch("app.utils.constants.get_all_recordings_dirs", return_value=[rec]),
        patch("app.utils.constants.CLOUD_RECORDINGS_DIR", tmp_path / "cloud"),
        patch("app.routes.transcribe.fire_webhooks"),
    ):
        yield flask_client, rec, trn


def _transcribe(client, path, **body):
    return client.post("/transcribe", json={"path": str(path), **body}, content_type="application/json")


RESULT = {"text": "hello there friend", "segments": [{"start": 0, "end": 1, "text": "hello"}], "duration": 1}


@pytest.mark.parametrize("provider", ["deepgram", "groq", "openai"])
def test_route_default_language_is_auto(route_env, provider):
    from app.utils.config import save_config

    client, rec, _ = route_env
    (rec / "1715000000.ogg").write_bytes(b"OggS" + bytes(100))
    save_config({f"{provider}_api_key": "k", "transcription_provider": provider})  # no "language" key
    with patch(f"app.routes.transcribe.transcribe_{provider}", return_value=dict(RESULT)) as fn:
        r = _transcribe(client, rec / "1715000000.ogg")
    assert r.status_code == 200, r.get_json()
    assert fn.call_args.args[2] is None
    assert r.get_json()["language"] == ""  # not silently "en"


@pytest.mark.parametrize("provider", ["deepgram", "groq", "openai"])
def test_route_explicit_auto_language(route_env, provider):
    from app.utils.config import save_config

    client, rec, _ = route_env
    (rec / "1715000000.ogg").write_bytes(b"OggS" + bytes(100))
    save_config({f"{provider}_api_key": "k", "transcription_provider": provider, "language": "en"})
    with patch(f"app.routes.transcribe.transcribe_{provider}", return_value=dict(RESULT)) as fn:
        _transcribe(client, rec / "1715000000.ogg", language="auto")
    assert fn.call_args.args[2] is None


def test_route_english_variant_for_deepgram(route_env):
    from app.utils.config import save_config

    client, rec, _ = route_env
    (rec / "1715000000.ogg").write_bytes(b"OggS" + bytes(100))
    save_config({"deepgram_api_key": "k", "language": "en", "english_variant": "gb"})
    with patch("app.routes.transcribe.transcribe_deepgram", return_value=dict(RESULT)) as fn:
        _transcribe(client, rec / "1715000000.ogg")
    assert fn.call_args.args[2] == "en-GB"


def test_route_openai_estimated_timestamps_flag_saved(route_env):
    from app.utils.config import save_config

    client, rec, trn = route_env
    (rec / "1715000000.ogg").write_bytes(b"OggS" + bytes(100))
    save_config({"openai_api_key": "k", "transcription_provider": "openai", "custom_vocabulary": ["Acme"]})
    result = {**RESULT, "language": "en", "timestamps_estimated": True}
    with patch("app.routes.transcribe.transcribe_openai", return_value=result) as fn:
        _transcribe(client, rec / "1715000000.ogg")
    assert fn.call_args.kwargs["keywords"] == ["Acme"]
    saved = json.loads((trn / "1715000000.json").read_text())
    assert saved["timestamps_estimated"] is True

    # Re-transcribing with a provider that has real timestamps clears the flag.
    save_config({"groq_api_key": "k", "transcription_provider": "groq"})
    with patch("app.routes.transcribe.transcribe_groq", return_value=dict(RESULT)):
        _transcribe(client, rec / "1715000000.ogg")
    saved = json.loads((trn / "1715000000.json").read_text())
    assert "timestamps_estimated" not in saved


def test_summarize_route_passes_segments(route_env):
    client, _, trn = route_env
    segs = [{"start": 0, "end": 2, "text": "A long enough transcript to summarise.", "speaker": "Speaker 1"}]
    (trn / "1715000000.json").write_text(json.dumps({"text": segs[0]["text"], "segments": segs}))
    with patch("app.routes.transcribe.summarize", return_value=("S.", "Title", "anthropic", [])) as fn:
        r = client.post("/summarize", json={"session_id": "1715000000"}, content_type="application/json")
    assert r.status_code == 200, r.get_json()
    assert fn.call_args.kwargs["segments"] == segs


# ---------------------------------------------------------------------------
# Local Whisper model table
# ---------------------------------------------------------------------------


def test_whisper_models_include_turbo_and_are_sorted_by_size():
    assert "turbo" in WHISPER_MODELS
    sizes = [m["download_mb"] for m in WHISPER_MODELS.values()]
    assert sizes == sorted(sizes)
    assert list(WHISPER_MODELS) == ["base", "small", "medium", "turbo", "large"]
    # "size" is a download size, not a parameter count
    assert WHISPER_MODELS["base"]["size"] == "145 MB"
    assert WHISPER_MODELS["large"]["size"] == "3.1 GB"
