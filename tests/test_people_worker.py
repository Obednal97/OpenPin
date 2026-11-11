"""Learning voices from typed names, matching new recordings, and the people worker."""

import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from app.coaching import runner, voice
from app.coaching import store as coaching_store
from app.people import apply, store, worker
from app.state import speakers
from app.utils.config import update_config

SID = "1715000000"
SID2 = "1715000100"
SEGMENTS = [
    {"start": 0.0, "end": 20.0, "text": "Hello there.", "speaker": "Speaker 1"},
    {"start": 21.0, "end": 60.0, "text": "Hi, how are you?", "speaker": "Speaker 2"},
    {"start": 61.0, "end": 70.0, "text": "Fine.", "speaker": "Speaker 3"},
]
LABELLING = runner.labelling_fingerprint(SEGMENTS)
HARRY = [1.0, 0.0, 0.0, 0.0]
SAM = [0.0, 1.0, 0.0, 0.0]
OWNER = [0.0, 0.0, 1.0, 0.0]
# What the voice model "hears" in each recording.
EMB = {
    SID: {"Speaker 1": HARRY, "Speaker 2": SAM, "Speaker 3": OWNER},
    SID2: {"Speaker 1": [0.99, 0.1, 0.0, 0.0], "Speaker 2": [0.0, 0.0, 0.0, 1.0]},
}


@pytest.fixture
def env(tmp_path, tmp_config):
    worker.wait_idle(10)  # jobs another test left behind run before anything is patched
    trn = tmp_path / "transcripts"
    trn.mkdir()
    calls = {"get": [], "cached": []}

    def fake_get(session_id, segments, audio_path=None):
        calls["get"].append(session_id)
        return env_data["emb"].get(session_id)

    def fake_cached(session_id, segments):
        calls["cached"].append(session_id)
        return env_data["cache"].get(session_id)

    env_data = {"trn": trn, "calls": calls, "emb": dict(EMB), "cache": {}}
    with ExitStack() as stack:
        stack.enter_context(patch("app.state.speakers.TRANSCRIPTS_DIR", trn))
        stack.enter_context(patch("app.coaching.store.COACHING_DIR", tmp_path / "coaching"))
        stack.enter_context(patch.object(store, "VOICES_PATH", tmp_path / "people" / "voices.json"))
        stack.enter_context(patch.object(voice, "PROFILE_PATH", tmp_path / "coaching" / "voice_profile.json"))
        stack.enter_context(patch("app.people.embeddings.get", side_effect=fake_get))
        stack.enter_context(patch("app.people.embeddings.cached", side_effect=fake_cached))
        stack.enter_context(patch("app.routes.notion._get_notion_page_url", return_value=""))
        env_data["enqueue"] = stack.enter_context(patch("app.utils.notion_queue.enqueue"))
        yield env_data
        assert worker.wait_idle(10)


def _write(trn, sid=SID, segments=SEGMENTS, **extra):
    data = {"text": " ".join(s["text"] for s in segments), "segments": segments, **extra}
    (trn / f"{sid}.json").write_text(json.dumps(data))
    return data


def _read(trn, sid=SID):
    return json.loads((trn / f"{sid}.json").read_text())


def _settle():
    assert worker.wait_idle(10)


def _threshold(value=0.7):
    update_config(lambda c: c.__setitem__("people_match_threshold", value))


def _profile(name):
    update_config(lambda c: c.setdefault("profile", {}).__setitem__("name", name))


def _sample(vec, session_id, label="Speaker 1"):
    return {"vec": vec, "session_id": session_id, "label": label, "seconds": 20.0, "added_at": "2026-01-01T00:00:00"}


def _person(name, *samples) -> str:
    out = {}

    def fn(d):
        out["id"] = store.create_person(d, name)
        for s in samples:
            store.add_sample(d, out["id"], s)

    store.mutate(fn)
    return out["id"]


def _people():
    return {p["name"]: p for pid, p in store.load()["people"].items() if pid != store.ME}


def _samples(name):
    return [(s["session_id"], s["label"]) for s in _people()[name]["samples"]]


def _me_samples():
    return [(s["session_id"], s["label"]) for s in store.load()["people"][store.ME]["samples"]]


def _sidecar(sid, **fields):
    coaching_store.update(sid, lambda d: d.update(fields))


# ---------------------------------------------------------------- learning on rename


def test_rename_learns_sample(env):
    _write(env["trn"])
    assert speakers.set_names(SID, {"Speaker 1": "Harry"}) == {"Speaker 1": "Harry"}
    _settle()
    harry = _people()["Harry"]
    assert [s["vec"] for s in harry["samples"]] == [HARRY]
    s = harry["samples"][0]
    assert (s["session_id"], s["label"], s["seconds"]) == (SID, "Speaker 1", 20.0)
    assert s["added_at"]


def test_rename_under_15s_learns_nothing(env):
    _write(env["trn"])
    speakers.set_names(SID, {"Speaker 3": "Tom"})
    _settle()
    assert _people() == {}


def test_clear_name_unlearns(env):
    _write(env["trn"])
    speakers.set_names(SID, {"Speaker 1": "Harry"})
    _settle()
    assert _samples("Harry") == [(SID, "Speaker 1")]
    speakers.set_names(SID, {"Speaker 1": None})
    _settle()
    assert _samples("Harry") == []


def test_rename_moves_sample_to_new_person(env):
    _write(env["trn"])
    speakers.set_names(SID, {"Speaker 1": "Harry"})
    speakers.set_names(SID, {"Speaker 1": "Harold"})
    _settle()
    assert _samples("Harold") == [(SID, "Speaker 1")]
    assert _people().get("Harry", {"samples": []})["samples"] == []


def test_typing_over_match_moves_sample_and_rejects(env):
    harry = _person("Harry", _sample(HARRY, "1700000000"), _sample(HARRY, SID))
    block = {"labelling": LABELLING, "matches": {"Speaker 1": {"person": harry, "score": 0.9}}, "rejected": {}}
    _write(env["trn"], speaker_voice_matches=block)
    assert speakers.get_names(SID) == {"Speaker 1": "Harry"}
    speakers.set_names(SID, {"Speaker 1": "Sam"})
    _settle()
    assert _samples("Sam") == [(SID, "Speaker 1")]
    assert _samples("Harry") == [("1700000000", "Speaker 1")]
    saved = _read(env["trn"])["speaker_voice_matches"]
    assert saved["rejected"] == {"Speaker 1": [harry]}
    assert "Speaker 1" not in saved["matches"]
    # Clearing the typed name later doesn't bring the wrong match back.
    speakers.set_names(SID, {"Speaker 1": None})
    _settle()
    assert speakers.get_names(SID) == {}


def test_typing_the_matched_name_accepts_it(env):
    harry = _person("Harry", _sample(HARRY, "1700000000"))
    block = {"labelling": LABELLING, "matches": {"Speaker 1": {"person": harry, "score": 0.9}}, "rejected": {}}
    _write(env["trn"], speaker_voice_matches=block)
    speakers.set_names(SID, {"Speaker 1": " harry "})
    _settle()
    assert _samples("Harry") == [("1700000000", "Speaker 1"), (SID, "Speaker 1")]
    saved = _read(env["trn"])["speaker_voice_matches"]
    assert saved["rejected"] == {}
    assert saved["matches"]["Speaker 1"]["person"] == harry


def test_profile_name_teaches_me(env):
    _profile("Ollie")
    _write(env["trn"])
    speakers.set_names(SID, {"Speaker 2": "ollie"})
    _settle()
    assert _me_samples() == [(SID, "Speaker 2")]
    assert _people() == {}


def test_excluded_recording_never_teaches_me(env):
    _profile("Ollie")
    _write(env["trn"])
    _sidecar(SID, excluded=True)
    speakers.set_names(SID, {"Speaker 2": "Ollie", "Speaker 1": "Harry"})
    _settle()
    assert _me_samples() == []
    assert set(_people()) == {"Harry"}  # other people still learn; no person called Ollie
    assert _samples("Harry") == [(SID, "Speaker 1")]


def test_mixed_up_recording_teaches_nobody(env):
    _profile("Ollie")
    _write(env["trn"])
    _sidecar(SID, mixed_up=True)
    speakers.set_names(SID, {"Speaker 1": "Harry", "Speaker 2": "Ollie"})
    _settle()
    assert _people() == {}
    assert _me_samples() == []


def test_frozen_label_never_teaches_until_renamed(env):
    _write(env["trn"], speaker_names={"Speaker 1": "Harry"}, speaker_names_frozen=["Speaker 1"])
    worker.enqueue("learn", session_id=SID, label="Speaker 1", name="Harry")
    _settle()
    assert _people() == {}
    speakers.set_names(SID, {"Speaker 1": "Harold"})
    _settle()
    assert _read(env["trn"]).get("speaker_names_frozen", []) == []
    assert _samples("Harold") == [(SID, "Speaker 1")]


def test_set_names_without_model_still_saves(env):
    _write(env["trn"])
    env["emb"].clear()
    with patch.object(voice, "model_available", return_value=False):
        assert speakers.set_names(SID, {"Speaker 1": "Harry"}) == {"Speaker 1": "Harry"}
        _settle()
    assert _read(env["trn"])["speaker_names"] == {"Speaker 1": "Harry"}
    assert _people() == {}


def test_set_names_survives_a_broken_worker(env):
    _write(env["trn"])
    with patch.object(worker, "enqueue", side_effect=RuntimeError("boom")):
        assert speakers.set_names(SID, {"Speaker 1": "Harry"}) == {"Speaker 1": "Harry"}


# ---------------------------------------------------------------- matching


def test_new_recording_matched_after_transcribe(env):
    from app.routes import transcribe

    _threshold()
    harry = _person("Harry", _sample(HARRY, SID))
    _write(env["trn"], sid=SID2)
    with patch.object(transcribe.coaching_runner, "start_coaching_async"):
        transcribe._after_transcript_final(SID2, "/nowhere.mp3")
    _settle()
    saved = _read(env["trn"], SID2)["speaker_voice_matches"]
    assert saved["labelling"] == LABELLING
    assert set(saved["matches"]) == {"Speaker 1"}
    assert saved["matches"]["Speaker 1"]["person"] == harry
    assert saved["rejected"] == {}
    assert speakers.get_names(SID2) == {"Speaker 1": "Harry"}


def test_rejected_person_is_not_matched(env):
    _threshold()
    harry = _person("Harry", _sample(HARRY, SID))
    block = {"labelling": LABELLING, "matches": {}, "rejected": {"Speaker 1": [harry]}}
    _write(env["trn"], sid=SID2, speaker_voice_matches=block)
    assert apply.match_recording(SID2) is False
    saved = _read(env["trn"], SID2)["speaker_voice_matches"]
    assert saved["matches"] == {} and saved["rejected"] == {"Speaker 1": [harry]}


def test_learning_rematches_past_recordings_from_cache(env):
    _threshold()
    _write(env["trn"])
    _write(env["trn"], sid=SID2)
    env["cache"][SID2] = EMB[SID2]
    speakers.set_names(SID, {"Speaker 1": "Harry"})
    _settle()
    assert speakers.get_names(SID2) == {"Speaker 1": "Harry"}
    assert SID2 not in env["calls"]["get"]  # no audio work for past recordings
    assert SID2 in env["calls"]["cached"]


def test_apply_matches_drops_stale_labelling(env):
    block = {"labelling": LABELLING, "matches": {}, "rejected": {"Speaker 2": ["p_x"]}}
    _write(env["trn"], speaker_voice_matches=block)
    before = _read(env["trn"])
    assert apply.apply_matches(SID, "stale", {"Speaker 1": {"person": "p_x", "score": 0.9}}) is False
    assert _read(env["trn"]) == before


def test_apply_matches_keeps_rejected(env):
    harry = _person("Harry")
    block = {"labelling": LABELLING, "matches": {}, "rejected": {"Speaker 2": ["p_x"]}}
    _write(env["trn"], speaker_voice_matches=block)
    assert apply.apply_matches(SID, LABELLING, {"Speaker 1": {"person": harry, "score": 0.9}}) is True
    saved = _read(env["trn"])["speaker_voice_matches"]
    assert saved["rejected"] == {"Speaker 2": ["p_x"]}
    assert saved["matches"] == {"Speaker 1": {"person": harry, "score": 0.9}}
    # The same matches again change nothing.
    assert apply.apply_matches(SID, LABELLING, {"Speaker 1": {"person": harry, "score": 0.9}}) is False


def test_add_rejection(env):
    harry = _person("Harry")
    block = {"labelling": LABELLING, "matches": {"Speaker 1": {"person": harry, "score": 0.9}}, "rejected": {}}
    _write(env["trn"], speaker_voice_matches=block)
    apply.add_rejection(SID, "Speaker 1", harry)
    apply.add_rejection(SID, "Speaker 1", harry)
    saved = _read(env["trn"])["speaker_voice_matches"]
    assert saved == {"labelling": LABELLING, "matches": {}, "rejected": {"Speaker 1": [harry]}}
    assert speakers.get_names(SID) == {}


def test_no_threshold_no_matching(env):
    _person("Harry", _sample(HARRY, SID))
    _write(env["trn"], sid=SID2)
    assert apply.match_recording(SID2) is False
    assert env["calls"]["get"] == []
    assert "speaker_voice_matches" not in _read(env["trn"], SID2)


def test_mixed_up_recording_not_matched(env):
    _threshold()
    _person("Harry", _sample(HARRY, SID))
    _write(env["trn"], sid=SID2)
    _sidecar(SID2, mixed_up=True)
    assert apply.match_recording(SID2) is False
    assert "speaker_voice_matches" not in _read(env["trn"], SID2)


def test_match_change_queues_notion_full_update(env):
    from app.utils import notion_queue

    _threshold()
    _person("Harry", _sample(HARRY, SID))
    _write(env["trn"], sid=SID2)
    with (
        patch("app.routes.notion._get_notion_page_url", return_value="https://www.notion.so/p-abc"),
        patch("app.state.notion.is_connected", return_value=True),
    ):
        assert apply.match_recording(SID2) is True
        env["enqueue"].assert_called_once_with(SID2, notion_queue.KIND_FULL)
        env["enqueue"].reset_mock()
        assert apply.match_recording(SID2) is False  # nothing new
        env["enqueue"].assert_not_called()


# ---------------------------------------------------------------- the worker


def test_worker_dedupes_and_survives_errors(env):
    ran = []

    def boom(**kw):
        ran.append(kw)
        raise RuntimeError("boom")

    with patch.dict(worker._HANDLERS, {"match": boom}):
        with worker._cond:  # hold the worker back while queueing
            worker.enqueue("match", session_id=SID, audio_path=None)
            worker.enqueue("match", session_id=SID, audio_path=None)
            worker.enqueue("match", session_id=SID2, audio_path=None)
        _settle()
    assert ran == [{"session_id": SID, "audio_path": None}, {"session_id": SID2, "audio_path": None}]
    with pytest.raises(ValueError):
        worker.enqueue("nonsense")
