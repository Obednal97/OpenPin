"""Owner voice profile and matching (embeddings are faked; no model, no audio)."""

import json
import math
import os
import stat

import pytest

from app.coaching import voice


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


OWNER = _unit([1.0, 0.0, 0.0])
OTHER = _unit([0.0, 1.0, 0.0])


@pytest.fixture(autouse=True)
def _profile_path(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "PROFILE_PATH", tmp_path / "coaching" / "voice_profile.json")


def _fake(monkeypatch, vectors):
    """vectors: label -> embedding. Clips are named after their label."""
    monkeypatch.setattr(voice, "model_available", lambda: True)
    monkeypatch.setattr(
        voice,
        "_speaker_clips",
        lambda audio, segments, max_seconds: {s["speaker"]: [f"clip-{s['speaker']}.wav"] for s in segments},
    )
    monkeypatch.setattr(voice, "_embed_wav", lambda path: vectors[path[len("clip-") : -len(".wav")]])


def _segs(*labels):
    return [{"start": i * 10.0, "end": i * 10.0 + 5.0, "speaker": lab} for i, lab in enumerate(labels)]


def _save_profile(vec=OWNER):
    voice._save_exemplars([vec])


def test_match_owner_needs_profile(monkeypatch):
    _fake(monkeypatch, {"A": OWNER})
    r = voice.match_owner("a.wav", _segs("A"))
    assert r["source"] == "none" and r["reason"] == "no voice profile" and r["labels"] == []


def test_match_owner_model_missing_never_installs(monkeypatch):
    _save_profile()
    monkeypatch.setattr(voice, "model_available", lambda: False)
    called = []
    monkeypatch.setattr(voice, "install_model", lambda: called.append(1) or (True, ""))
    r = voice.match_owner("a.wav", _segs("A"))
    assert not called
    assert r["reason"] == "voice model not installed" and r["source"] == "none"


def test_match_owner_picks_closest_above_threshold(monkeypatch):
    _save_profile()
    _fake(monkeypatch, {"A": OWNER, "B": OTHER})
    r = voice.match_owner("a.wav", _segs("A", "B"))
    assert r["labels"] == ["A"] and r["source"] == "voice"
    assert r["confidence"] == pytest.approx(1.0)


def test_split_owner_returns_both_labels(monkeypatch):
    _save_profile()
    near = _unit([1.0, 0.2, 0.0])
    _fake(monkeypatch, {"A": OWNER, "B": near, "C": OTHER})
    r = voice.match_owner("a.wav", _segs("A", "B", "C"))
    assert sorted(r["labels"]) == ["A", "B"]


def test_ambiguous_match_returns_none(monkeypatch):
    _save_profile()
    # cosine about 0.45 each: close to each other, below the threshold
    mid = _unit([0.45, 0.89, 0.0])
    mid2 = _unit([0.47, 0.88, 0.0])
    _fake(monkeypatch, {"A": mid, "B": mid2})
    r = voice.match_owner("a.wav", _segs("A", "B"))
    assert r["labels"] == [] and r["reason"] == "no confident match"


def test_match_owner_never_raises(monkeypatch):
    _save_profile()
    monkeypatch.setattr(voice, "model_available", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("bad audio")

    monkeypatch.setattr(voice, "_speaker_clips", boom)
    r = voice.match_owner("a.wav", _segs("A"))
    assert r["source"] == "none" and r["reason"] == "no confident match"


def test_add_exemplars_caps_at_20(monkeypatch):
    _fake(monkeypatch, {"A": OWNER})
    for _ in range(25):
        assert voice.add_exemplars("a.wav", _segs("A"), ["A"]) == 1
    data = json.loads(voice.PROFILE_PATH.read_text())
    assert data["version"] == 1 and len(data["exemplars"]) == voice.MAX_EXEMPLARS == 20
    assert "updated_at" in data


def test_add_exemplars_drops_oldest(monkeypatch):
    voice._save_exemplars([[float(i), 0.0] for i in range(20)])
    _fake(monkeypatch, {"A": OWNER})
    voice.add_exemplars("a.wav", _segs("A"), ["A"])
    ex = json.loads(voice.PROFILE_PATH.read_text())["exemplars"]
    assert len(ex) == 20 and ex[0][0] == 1.0 and ex[-1] == OWNER


def test_profile_file_is_0600(monkeypatch):
    _fake(monkeypatch, {"A": OWNER})
    voice.add_exemplars("a.wav", _segs("A"), ["A"])
    assert stat.S_IMODE(os.stat(voice.PROFILE_PATH).st_mode) == 0o600
    assert not list(voice.PROFILE_PATH.parent.glob("*.tmp"))


def test_delete_profile_removes_file(monkeypatch):
    _save_profile()
    assert voice.PROFILE_PATH.exists()
    voice.delete_profile()
    assert not voice.PROFILE_PATH.exists()
    voice.delete_profile()  # idempotent


def test_speaker_embeddings_mean_is_normalised(monkeypatch):
    monkeypatch.setattr(voice, "_speaker_clips", lambda a, s, m: {"A": ["clip-A.wav", "clip-B.wav"]})
    monkeypatch.setattr(voice, "_embed_wav", lambda p: [2.0, 0.0] if p == "clip-A.wav" else [0.0, 2.0])
    emb = voice.speaker_embeddings("a.wav", _segs("A"))
    assert emb["A"] == pytest.approx(_unit([1.0, 1.0]))


def test_corrupt_profile_is_treated_as_missing(monkeypatch):
    voice.PROFILE_PATH.parent.mkdir(parents=True)
    voice.PROFILE_PATH.write_text("{nope")
    _fake(monkeypatch, {"A": OWNER})
    assert voice.match_owner("a.wav", _segs("A"))["reason"] == "no voice profile"
