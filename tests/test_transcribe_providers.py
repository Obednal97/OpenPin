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
        patch("app.coaching.store.COACHING_DIR", tmp_path / "coaching"),
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


# ---------------------------------------------------------------------------
# Provider limits: file size, Groq prompt, Deepgram keyterms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn, label", [(T.transcribe_openai, "OpenAI"), (T.transcribe_groq, "Groq")])
@patch("app.providers.transcribe.http_requests.post")
def test_too_big_recording_is_refused_before_upload(mock_post, fn, label, tmp_path):
    big = tmp_path / "long.ogg"
    with open(big, "wb") as f:
        f.truncate(26_000_000)  # sparse file, 26 MB
    with pytest.raises(T.AudioTooLargeError, match=f"26 MB and {label} accepts up to 25 MB"):
        fn(str(big), "sk")
    mock_post.assert_not_called()


@patch("app.providers.transcribe.http_requests.post")
def test_groq_prompt_is_clipped_at_a_separator(mock_post, ogg):
    mock_post.return_value = _resp({"text": "", "segments": []})
    vocab = ", ".join(f"Term{i:03d}" for i in range(200))  # ~1,600 chars
    T.transcribe_groq(str(ogg), "sk", vocab_prompt=vocab)
    prompt = mock_post.call_args.kwargs["data"]["prompt"]
    assert len(prompt) <= T.GROQ_PROMPT_MAX_CHARS
    assert prompt.startswith("Term000, Term001") and prompt.endswith(tuple(f"{i}" for i in range(10)))
    assert not prompt.endswith(",")


@patch("app.providers.transcribe.http_requests.post")
def test_short_groq_prompt_is_unchanged(mock_post, ogg):
    mock_post.return_value = _resp({"text": "", "segments": []})
    T.transcribe_groq(str(ogg), "sk", vocab_prompt="Acme, Plaud")
    assert mock_post.call_args.kwargs["data"]["prompt"] == "Acme, Plaud"


@patch("app.providers.transcribe.http_requests.post")
def test_deepgram_keyterms_are_capped_and_deduped(mock_post, ogg):
    mock_post.return_value = _resp({"results": {"channels": [{"alternatives": [{"transcript": ""}]}]}})
    words = ["Acme", "Acme", " ", "Plaud V2"] + [f"term{i}" for i in range(100)]
    T.transcribe_deepgram(str(ogg), "dg", keywords=words)
    url = mock_post.call_args.args[0]
    terms = [p.split("=", 1)[1] for p in url.split("?", 1)[1].split("&") if p.startswith("keyterm=")]
    assert terms[:2] == ["Acme", "Plaud%20V2"]
    assert len(terms) == T.DEEPGRAM_MAX_KEYTERMS


# ---------------------------------------------------------------------------
# Deepgram verbatim words (coaching) vs. clean transcript
# ---------------------------------------------------------------------------


def _dg_word(word, start, speaker=0):
    return {
        "word": word.lower().strip(".,"),
        "punctuated_word": word,
        "start": start,
        "end": start + 0.3,
        "confidence": 0.9,
        "speaker": speaker,
    }


DG_VERBATIM_WORDS = [_dg_word(w, i * 0.4) for i, w in enumerate(["So", "um", "pricing", "uh", "works."])]
DG_VERBATIM_BODY = {
    "metadata": {"duration": 2.0},
    "results": {
        "channels": [
            {
                "alternatives": [{"transcript": "So um pricing uh works.", "words": DG_VERBATIM_WORDS}],
                "detected_language": "en",
            }
        ],
        "utterances": [
            {
                "start": 0,
                "end": 2,
                "transcript": "So um pricing uh works.",
                "speaker": 0,
                "words": DG_VERBATIM_WORDS,
            }
        ],
    },
}


@patch("app.providers.transcribe.http_requests.post")
def test_deepgram_requests_filler_words(mock_post, ogg):
    mock_post.return_value = _resp(DG_VERBATIM_BODY)
    T.transcribe_deepgram(str(ogg), "dg", "en")
    assert "filler_words=true" in mock_post.call_args.args[0]


@patch("app.providers.transcribe.http_requests.post")
def test_deepgram_clean_text_has_no_fillers(mock_post, ogg):
    mock_post.return_value = _resp(DG_VERBATIM_BODY)
    out = T.transcribe_deepgram(str(ogg), "dg", "en", diarize=True)
    assert out["segments"][0]["text"] == "So pricing works."
    assert out["text"] == "So pricing works."


@patch("app.providers.transcribe.http_requests.post")
def test_deepgram_returns_verbatim_words(mock_post, ogg):
    mock_post.return_value = _resp(DG_VERBATIM_BODY)
    out = T.transcribe_deepgram(str(ogg), "dg", "en", diarize=True)
    assert [w["word"] for w in out["words"]] == ["So", "um", "pricing", "uh", "works."]
    assert {w["speaker"] for w in out["words"]} == {"Speaker 1"}
    assert out["words"][1].keys() == {"word", "start", "end", "speaker", "confidence"}


@patch("app.providers.transcribe.http_requests.post")
def test_deepgram_words_have_no_speaker_without_diarize(mock_post, ogg):
    mock_post.return_value = _resp(DG_VERBATIM_BODY)
    out = T.transcribe_deepgram(str(ogg), "dg", "en")
    assert all(w["speaker"] is None for w in out["words"])


def test_cloud_route_saves_words_to_coaching_sidecar_not_transcript(route_env):
    import hashlib

    from app.coaching import store
    from app.utils.config import save_config

    client, rec, trn = route_env
    (rec / "1715000000.ogg").write_bytes(b"OggS" + bytes(100))
    save_config({"deepgram_api_key": "k", "transcription_provider": "deepgram"})
    words = [{"word": "um", "start": 0, "end": 0.2, "speaker": None, "confidence": 0.9}]
    result = {**RESULT, "words": words}
    with patch("app.routes.transcribe.transcribe_deepgram", return_value=result):
        r = _transcribe(client, rec / "1715000000.ogg")
    assert r.status_code == 200, r.get_json()
    assert "words" not in r.get_json()
    side = store.load("1715000000")
    assert side["words"] == words
    assert side["transcript_text_hash"] == hashlib.sha1(RESULT["text"].encode()).hexdigest()
    assert "words" not in json.loads((trn / "1715000000.json").read_text())
    assert "um" not in json.loads((trn / "1715000000.json").read_text())["text"].split()


def test_retranscribe_with_non_deepgram_provider_clears_words(route_env):
    from app.coaching import store
    from app.utils.config import save_config

    client, rec, _ = route_env
    (rec / "1715000000.ogg").write_bytes(b"OggS" + bytes(100))
    words = [{"word": "um", "start": 0, "end": 0.2, "speaker": None, "confidence": 0.9}]
    save_config({"deepgram_api_key": "k", "transcription_provider": "deepgram"})
    with patch("app.routes.transcribe.transcribe_deepgram", return_value={**RESULT, "words": words}):
        _transcribe(client, rec / "1715000000.ogg")
    assert store.load("1715000000")["words"] == words

    save_config({"groq_api_key": "k", "transcription_provider": "groq"})
    with patch("app.routes.transcribe.transcribe_groq", return_value=dict(RESULT)):
        _transcribe(client, rec / "1715000000.ogg")
    assert store.load("1715000000")["words"] == []
