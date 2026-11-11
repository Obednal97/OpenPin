"""Per-recording embedding cache (the model and audio are faked)."""

import os
import stat

import pytest

from app.coaching import runner, voice
from app.people import embeddings

SEGS = [
    {"start": 0.0, "end": 5.0, "speaker": "Speaker 1"},
    {"start": 5.0, "end": 5.5, "speaker": "Speaker 2"},
    {"start": 6.0, "end": 9.0, "speaker": "Speaker 2"},
]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "PEOPLE_DIR", tmp_path / "people")
    monkeypatch.setattr(voice, "model_available", lambda: True)
    monkeypatch.setattr(runner, "_resolve_audio", lambda sid, p: "audio.wav")


@pytest.fixture
def calls(monkeypatch):
    n = []

    def fake(audio, segments, *a, **k):
        n.append(1)
        return {"Speaker 1": [1.0, 0.0], "Speaker 2": [0.0, 1.0]}

    monkeypatch.setattr(voice, "speaker_embeddings", fake)
    return n


def test_get_computes_once_then_reads_cache(calls, tmp_path):
    a = embeddings.get("s1", SEGS)
    b = embeddings.get("s1", SEGS)
    assert a == b and len(calls) == 1
    f = tmp_path / "people" / "embeddings" / "s1.json"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert embeddings.cached("s1", SEGS) == a


def test_relabelled_transcript_recomputes(calls):
    embeddings.get("s1", SEGS)
    relabelled = [{**SEGS[0], "speaker": "Speaker 2"}, *SEGS[1:]]
    assert embeddings.cached("s1", relabelled) is None
    embeddings.get("s1", relabelled)
    assert len(calls) == 2


def test_no_model_returns_none(calls, monkeypatch):
    monkeypatch.setattr(voice, "model_available", lambda: False)
    assert embeddings.get("s1", SEGS) is None and not calls


def test_no_audio_or_failure_returns_none(calls, monkeypatch):
    monkeypatch.setattr(runner, "_resolve_audio", lambda sid, p: None)
    assert embeddings.get("s1", SEGS) is None
    monkeypatch.setattr(runner, "_resolve_audio", lambda sid, p: "a.wav")
    monkeypatch.setattr(voice, "speaker_embeddings", lambda *a, **k: 1 / 0)
    assert embeddings.get("s1", SEGS) is None
    assert embeddings.get("../evil", SEGS) is None


def test_speech_seconds_ignores_short_segments():
    assert embeddings.speech_seconds(SEGS) == {"Speaker 1": 5.0, "Speaker 2": 3.0}


def test_delete_removes_file(calls, tmp_path):
    embeddings.get("s1", SEGS)
    embeddings.delete("s1")
    assert not (tmp_path / "people" / "embeddings" / "s1.json").exists()
    embeddings.delete("s1")


def test_match_owner_uses_cache_with_session_id(calls, monkeypatch):
    monkeypatch.setattr(voice, "_load_exemplars", lambda: [[1.0, 0.0]])
    r1 = voice.match_owner("audio.wav", SEGS, "s1")
    r2 = voice.match_owner("audio.wav", SEGS, "s1")
    assert r1["labels"] == r2["labels"] == ["Speaker 1"]
    assert len(calls) == 1
    voice.match_owner("audio.wav", SEGS)
    assert len(calls) == 2
