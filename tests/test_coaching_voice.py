"""Owner voice profile and matching (embeddings are faked; no model, no audio)."""

import json
import math
import os
import stat

import pytest

from app.coaching import voice
from app.people import store as people_store


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


OWNER = _unit([1.0, 0.0, 0.0])
OTHER = _unit([0.0, 1.0, 0.0])


@pytest.fixture(autouse=True)
def _profile_path(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "PROFILE_PATH", tmp_path / "coaching" / "voice_profile.json")
    monkeypatch.setattr(people_store, "VOICES_PATH", tmp_path / "people" / "voices.json")


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
    voice._learn([vec], None, [], replace=True)


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


def test_add_exemplars_keeps_the_most_varied(monkeypatch):
    odd = _unit([0.0, 0.0, 1.0])  # the oldest, and unlike the rest: kept
    voice._learn([odd] + [_unit([1.0, i / 100, 0.0]) for i in range(19)], None, [], replace=True)
    _fake(monkeypatch, {"A": OWNER})
    voice.add_exemplars("a.wav", _segs("A"), ["A"])
    ex = json.loads(voice.PROFILE_PATH.read_text())["exemplars"]
    assert len(ex) == 20 and ex[0] == odd


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


def _stub_runtime(monkeypatch, shape):
    import sys
    import types

    np = pytest.importorskip("numpy")

    class Speaker:
        def __init__(self, lang):
            assert lang == "en"

        def extract_embedding(self, path):
            return np.full(shape, 0.5, dtype=np.float32)

    fake = types.ModuleType("wespeakerruntime")
    fake.Speaker = Speaker
    monkeypatch.setitem(sys.modules, "wespeakerruntime", fake)
    monkeypatch.setattr(voice, "_speaker_model", None)
    return voice._embed_wav.real


def test_embed_wav_flattens_real_library_shape(monkeypatch):
    """The real runtime returns an array shaped [1, 256]."""
    emb = _stub_runtime(monkeypatch, (1, 256))("x.wav")
    assert isinstance(emb, list) and len(emb) == 256 and emb[0] == 0.5


def test_embed_wav_rejects_wrong_size(monkeypatch):
    with pytest.raises(ValueError):
        _stub_runtime(monkeypatch, (1, 10))("x.wav")


def test_speaker_clips_cuts_real_audio(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    wav = tmp_path / "c.wav"
    sf.write(wav, (0.1 * np.random.default_rng(0).standard_normal(16000 * 12)).astype("float32"), 16000)
    import src.transcribe as tr

    monkeypatch.setattr(
        tr,
        "_to_wav16k",
        lambda p: str(tmp_path / "copy.wav") if (tmp_path / "copy.wav").write_bytes(wav.read_bytes()) else None,
    )
    segs = [
        {"start": 0.0, "end": 5.0, "speaker": "A"},
        {"start": 5.0, "end": 5.5, "speaker": "A"},  # too short
        {"start": 6.0, "end": 11.0, "speaker": "B"},
        {"start": 11.0, "end": 12.0, "speaker": None},  # no label: never a clip
    ]
    clips = voice._speaker_clips(str(wav), segs, 60.0)
    try:
        assert {k: len(v) for k, v in clips.items()} == {"A": 1, "B": 1}
        assert sf.info(clips["A"][0]).frames == 16000 * 5
    finally:
        voice._remove_clips(clips)


def _wav_copy(tmp_path, monkeypatch, data, rate=16000):
    sf = pytest.importorskip("soundfile")
    src = tmp_path / "src.wav"
    sf.write(src, data, rate)
    import src.transcribe as tr

    copy = tmp_path / "copy.wav"
    monkeypatch.setattr(tr, "_to_wav16k", lambda p: str(copy) if copy.write_bytes(src.read_bytes()) else None)
    return copy


def test_loudest_label_reads_float32_per_segment_and_skips_unlabelled(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    quiet, loud, louder = 0.05, 0.3, 0.9
    data = np.concatenate([np.full(16000 * 2, quiet), np.full(16000 * 2, loud), np.full(16000 * 2, louder)]).astype(
        "float32"
    )
    copy = _wav_copy(tmp_path, monkeypatch, data)
    reads = []
    real_read = sf.read

    def spy(path, *args, **kwargs):
        reads.append(kwargs)
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(sf, "read", spy)
    segs = [
        {"start": 0.0, "end": 2.0, "speaker": "A"},
        {"start": 2.0, "end": 4.0, "speaker": "B"},
        {"start": 4.0, "end": 6.0, "speaker": None},  # loudest audio, but unlabelled
    ]
    assert voice.loudest_label("x.ogg", segs) == "B"
    assert len(reads) == 2  # one read per labelled segment, never the whole file
    assert all(r.get("dtype") == "float32" and r.get("frames") == 32000 for r in reads)
    assert not copy.exists()  # the temporary WAV is removed


def test_loudest_label_none_when_only_unlabelled(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    _wav_copy(tmp_path, monkeypatch, np.full(16000, 0.5).astype("float32"))
    assert voice.loudest_label("x.ogg", [{"start": 0.0, "end": 1.0, "speaker": None}]) is None


def test_speaker_embeddings_skip_unlabelled_segments(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    _wav_copy(tmp_path, monkeypatch, (0.1 * np.ones(16000 * 4)).astype("float32"))
    monkeypatch.setattr(voice, "_embed_wav", lambda path: OWNER)
    segs = [{"start": 0.0, "end": 2.0, "speaker": "A"}, {"start": 2.0, "end": 4.0, "speaker": None}]
    assert set(voice.speaker_embeddings("x.ogg", segs)) == {"A"}
