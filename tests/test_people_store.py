"""Voice store: the owner plus other people, with the v1 profile mirrored."""

import json
import os
import stat

import pytest

from app.coaching import voice
from app.people import store


@pytest.fixture(autouse=True)
def _paths(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "PROFILE_PATH", tmp_path / "coaching" / "voice_profile.json")
    monkeypatch.setattr(store, "VOICES_PATH", tmp_path / "people" / "voices.json")


def _sample(vec, session_id="s1", label="Speaker 1", added_at="2026-01-01T00:00:00+00:00"):
    return {"vec": vec, "session_id": session_id, "label": label, "seconds": 5.0, "added_at": added_at}


def test_migrates_v1_profile_into_me():
    voice.PROFILE_PATH.parent.mkdir(parents=True)
    voice.PROFILE_PATH.write_text(json.dumps({"version": 1, "exemplars": [[1.0, 0], [0, 1.0], [1, 1]]}))
    me = store.load()["people"]["me"]
    assert len(me["samples"]) == 3
    assert all(s["session_id"] is None and s["seconds"] == 0 for s in me["samples"])
    assert (voice.PROFILE_PATH.parent / "voice_profile.json.v1-backup").exists()


def test_mutate_mirrors_me_to_v1_file():
    store.mutate(lambda d: store.add_sample(d, store.ME, _sample([0.1, 0.2])))
    assert voice._load_exemplars() == [[0.1, 0.2]]
    assert json.loads(voice.PROFILE_PATH.read_text())["version"] == 1


def test_add_sample_moves_between_people():
    def fn(d):
        harry = store.create_person(d, "Harry")
        nadia = store.create_person(d, "Nadia")
        store.add_sample(d, harry, _sample([1.0, 0], "s1", "Speaker 2"))
        store.add_sample(d, nadia, _sample([0, 1.0], "s1", "Speaker 2"))

    data = store.mutate(fn)
    counts = {p["name"]: p["samples"] for p in store.person_summary(data)}
    assert counts["Harry"] == 0 and counts["Nadia"] == 1


def test_remove_sample_returns_owner():
    def fn(d):
        pid = store.create_person(d, "Harry")
        store.add_sample(d, pid, _sample([1.0, 0]))
        assert store.remove_sample(d, "s1", "Speaker 1") == pid
        assert store.remove_sample(d, "s1", "Speaker 1") is None

    store.mutate(fn)


def test_trim_keeps_varied():
    a, b = [1.0, 0.0], [0.0, 1.0]
    samples = [_sample(a, f"s{i}", added_at=f"2026-01-{i + 1:02d}") for i in range(20)] + [_sample(b, "sb")]
    kept = store.trim_varied(samples)
    assert len(kept) == 20
    assert any(s["vec"] == b for s in kept)
    assert sum(s["vec"] == a for s in kept) == 19
    assert "s0" not in {s["session_id"] for s in kept}  # the oldest duplicate went


def test_find_person_ignores_case_and_spaces():
    data = store.load()
    pid = store.create_person(data, "Harry Fisher")
    assert store.find_person(data, "  harry   fisher") == pid
    assert store.find_person(data, "nobody") is None


def test_create_person_rejects_duplicate_name():
    data = store.load()
    store.create_person(data, "Harry")
    with pytest.raises(ValueError):
        store.create_person(data, " HARRY ")


def test_files_are_0600():
    store.mutate(lambda d: store.add_sample(d, store.ME, _sample([1.0, 0])))
    for p in (store.VOICES_PATH, voice.PROFILE_PATH):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert not list(store.VOICES_PATH.parent.glob("*.tmp"))


def test_summary_has_no_vectors():
    data = store.mutate(lambda d: store.add_sample(d, store.ME, _sample([1.0, 0])))
    rows = store.person_summary(data)
    assert "vec" not in json.dumps(rows)
    assert rows[0]["samples"] == 1 and rows[0]["is_me"] and rows[0]["bytes"] > 0


def test_corrupt_voices_file_is_kept_and_me_recovered_from_v1():
    voice.PROFILE_PATH.parent.mkdir(parents=True)
    voice.PROFILE_PATH.write_text(json.dumps({"version": 1, "exemplars": [[1.0, 0]]}))
    store.VOICES_PATH.parent.mkdir(parents=True)
    store.VOICES_PATH.write_text("{nope")
    data = store.mutate(lambda d: None)
    assert len(data["people"]["me"]["samples"]) == 1
    assert voice._load_exemplars() == [[1.0, 0.0]]
    assert len(list(store.VOICES_PATH.parent.glob("voices.json.corrupt-*"))) == 1


def test_read_error_propagates(monkeypatch):
    store.mutate(lambda d: store.add_sample(d, store.ME, _sample([1.0, 0])))
    real = type(store.VOICES_PATH).read_text

    def boom(self, *a, **k):
        if self == store.VOICES_PATH:
            raise PermissionError("denied")
        return real(self, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(type(store.VOICES_PATH), "read_text", boom)
        with pytest.raises(OSError):
            store.load()
    assert len(store.load()["people"]["me"]["samples"]) == 1


def test_delete_profile_removes_the_v1_backup():
    voice.PROFILE_PATH.parent.mkdir(parents=True)
    voice.PROFILE_PATH.write_text(json.dumps({"version": 1, "exemplars": [[1.0, 0]]}))
    store.load()
    backup = voice.PROFILE_PATH.with_name("voice_profile.json.v1-backup")
    assert backup.exists()
    voice.delete_profile()
    assert not backup.exists() and not voice.PROFILE_PATH.exists()


def test_relearning_same_session_and_label_leaves_one_sample(monkeypatch):
    monkeypatch.setattr(voice, "speaker_embeddings", lambda a, s: {"Speaker 1": [1.0, 0.0]})
    voice.add_exemplars("a.wav", [], ["Speaker 1"], session_id="s1")
    monkeypatch.setattr(voice, "speaker_embeddings", lambda a, s: {"Speaker 1": [0.0, 1.0]})
    voice.add_exemplars("a.wav", [], ["Speaker 1"], session_id="s1")
    samples = store.load()["people"]["me"]["samples"]
    assert [s["vec"] for s in samples] == [[0.0, 1.0]]


def test_add_exemplars_reads_and_writes_in_one_transaction(monkeypatch):
    """Samples another writer adds while embeddings compute are not lost."""

    def embeddings(a, s):
        store.mutate(lambda d: store.add_sample(d, store.ME, _sample([5.0, 5.0], "other", "Speaker 9")))
        return {"Speaker 1": [1.0, 0.0]}

    monkeypatch.setattr(voice, "speaker_embeddings", embeddings)
    voice.add_exemplars("a.wav", [], ["Speaker 1"], session_id="s1")
    vecs = [s["vec"] for s in store.load()["people"]["me"]["samples"]]
    assert [5.0, 5.0] in vecs and [1.0, 0.0] in vecs
