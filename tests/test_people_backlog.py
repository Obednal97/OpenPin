"""The backlog: learn voices from past typed names, calibrate, then name past recordings."""

import json
import os
import threading
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from app.coaching import runner, voice
from app.coaching import store as coaching_store
from app.people import backlog, matching, store, worker
from app.state import speakers
from app.utils.config import load_config, update_config

SEGMENTS = [
    {"start": 0.0, "end": 20.0, "text": "Hello there.", "speaker": "Speaker 1"},
    {"start": 21.0, "end": 60.0, "text": "Hi, how are you?", "speaker": "Speaker 2"},
    {"start": 61.0, "end": 70.0, "text": "Fine.", "speaker": "Speaker 3"},
]
LABELLING = runner.labelling_fingerprint(SEGMENTS)


def _sid(i: int) -> str:
    return str(1715000000 + i * 1000)  # a bigger i is a newer recording


def _vec(axis: int, wobble: float = 0.0) -> list[float]:
    v = [0.0, 0.0, 0.0, 0.0]
    v[axis] = 1.0
    v[3] = wobble
    return v


HARRY, SAM, OWNER = 0, 1, 2


@pytest.fixture
def env(tmp_path, tmp_config):
    worker.wait_idle(10)
    trn = tmp_path / "transcripts"
    trn.mkdir()
    data = {"trn": trn, "emb": {}, "cache": {}, "gets": [], "on_get": None}

    def fake_get(session_id, segments, audio_path=None):
        data["gets"].append(session_id)
        if data["on_get"]:
            data["on_get"](session_id)
        emb = data["emb"].get(session_id)
        if emb is not None:
            data["cache"][session_id] = emb  # as the real one: computed once, then cached
        return emb

    def fake_cached(session_id, segments):
        return data["cache"].get(session_id)

    with ExitStack() as stack:
        stack.enter_context(patch("app.state.speakers.TRANSCRIPTS_DIR", trn))
        stack.enter_context(patch("app.coaching.store.COACHING_DIR", tmp_path / "coaching"))
        stack.enter_context(patch.object(store, "VOICES_PATH", tmp_path / "people" / "voices.json"))
        stack.enter_context(patch.object(backlog, "STATE_PATH", tmp_path / "people" / "backlog.json"))
        stack.enter_context(patch.object(voice, "PROFILE_PATH", tmp_path / "coaching" / "voice_profile.json"))
        stack.enter_context(patch.object(voice, "model_available", return_value=True))
        stack.enter_context(patch("app.people.embeddings.get", side_effect=fake_get))
        stack.enter_context(patch("app.people.embeddings.cached", side_effect=fake_cached))
        stack.enter_context(patch("app.routes.notion._get_notion_page_url", return_value="https://www.notion.so/p"))
        stack.enter_context(patch("app.state.notion.is_connected", return_value=True))
        data["enqueue"] = stack.enter_context(patch("app.utils.notion_queue.enqueue"))
        yield data
        backlog.cancel()
        assert worker.wait_idle(10)


def _write(env, i, names=None, emb=None, segments=SEGMENTS, **extra):
    sid = _sid(i)
    t = {"text": "x", "segments": segments, **extra}
    if names:
        t["speaker_names"] = names
    (env["trn"] / f"{sid}.json").write_text(json.dumps(t))
    if emb is not None:
        env["emb"][sid] = emb
    return sid


def _named(env, i, harry_name="Harry", sam_name="Sam", **extra):
    """A recording where Speaker 1 (Harry) and Speaker 2 (Sam) have typed names."""
    w = i / 100
    emb = {"Speaker 1": _vec(HARRY, w), "Speaker 2": _vec(SAM, w), "Speaker 3": _vec(OWNER, w)}
    return _write(env, i, {"Speaker 1": harry_name, "Speaker 2": sam_name}, emb, **extra)


def _read(env, sid):
    return json.loads((env["trn"] / f"{sid}.json").read_text())


def _people():
    return {p["name"]: p for pid, p in store.load()["people"].items() if pid != store.ME}


def _sessions(name):
    return sorted(s["session_id"] for s in _people()[name]["samples"])


def _go():
    backlog.run(threading.Event())


# ---------------------------------------------------------------- the three phases


def test_backlog_learns_then_calibrates_then_names(env):
    sids = [_named(env, i) for i in range(12)]
    unnamed = _write(env, 20, emb={"Speaker 1": _vec(HARRY, 0.05), "Speaker 2": _vec(SAM, 0.07)})
    # A recording with no typed names where you picked yourself for coaching.
    solo = [{"start": 0.0, "end": 30.0, "text": "Me.", "speaker": "Speaker 1"}]
    picked = _write(env, 15, emb={"Speaker 1": _vec(OWNER)}, segments=solo)
    pick = {"labels": ["Speaker 1"], "source": "user", "labelling": runner.labelling_fingerprint(solo)}
    coaching_store.update(picked, lambda d: d.update(owner=pick))
    seen = {}
    real_match = backlog.apply.match_recording

    def spy(session_id, *a, **kw):
        seen.setdefault("threshold_when_naming", matching.current_threshold())
        seen.setdefault("harry_samples_when_naming", len(_people()["Harry"]["samples"]))
        return real_match(session_id, *a, **kw)

    with patch.object(backlog.apply, "match_recording", side_effect=spy):
        _go()

    # Learnt: one sample per recording each, before naming began.
    assert _sessions("Harry") == sorted(sids)
    assert _sessions("Sam") == sorted(sids)
    assert seen["harry_samples_when_naming"] == 12
    # Calibrated on 24 typed names plus your coaching pick, before naming.
    cfg = load_config()
    cal = cfg["people_calibration"]
    assert cal["instances"] == 25
    assert cal["named"] >= matching.MIN_NAMED
    assert cal["accuracy"] == 1.0
    assert cal["threshold"] == cfg["people_match_threshold"] == seen["threshold_when_naming"]
    assert cal["at"]
    # Named: the recording with no typed names gets both speakers by voice.
    assert speakers.get_names(unnamed) == {"Speaker 1": "Harry", "Speaker 2": "Sam"}
    assert picked in env["gets"]  # embedded for calibration, though nothing was typed
    assert backlog.state() == {"phase": "done", "done": 14, "total": 14, "running": False}


def test_backlog_groups_names_case_insensitively(env):
    _write(env, 1, {"Speaker 1": "harry", "Speaker 2": "Will"}, {"Speaker 1": _vec(HARRY), "Speaker 2": _vec(SAM)})
    _write(
        env,
        2,
        {"Speaker 1": " Harry ", "Speaker 2": "William Moss"},
        {"Speaker 1": _vec(HARRY), "Speaker 2": _vec(SAM)},
    )
    _go()
    people = _people()
    assert set(people) == {"Harry", "Will", "William Moss"}  # newest spelling of Harry kept
    assert _sessions("Harry") == [_sid(1), _sid(2)]


def test_backlog_profile_name_teaches_me_except_when_excluded(env):
    update_config(lambda c: c.setdefault("profile", {}).__setitem__("name", "Ollie"))
    _write(env, 1, {"Speaker 2": "ollie"}, {"Speaker 2": _vec(OWNER)})
    _write(env, 2, {"Speaker 2": "Ollie"}, {"Speaker 2": _vec(OWNER)})
    coaching_store.update(_sid(2), lambda d: d.update(excluded=True))
    _go()
    me = store.load()["people"][store.ME]["samples"]
    assert [s["session_id"] for s in me] == [_sid(1)]
    assert _people() == {}


def test_backlog_skips_frozen_and_mixed_up(env):
    _write(env, 1, {"Speaker 1": "Harry"}, {"Speaker 1": _vec(HARRY)}, speaker_names_frozen=["Speaker 1"])
    _write(env, 2, {"Speaker 2": "Sam"}, {"Speaker 2": _vec(SAM)})
    coaching_store.update(_sid(2), lambda d: d.update(mixed_up=True))
    _write(env, 3, {"Speaker 3": "Tom"}, {"Speaker 3": _vec(OWNER)})  # 9 s: too short to learn
    _go()
    assert _people() == {}
    assert load_config()["people_calibration"]["instances"] == 0
    assert "speaker_voice_matches" not in _read(env, _sid(2))  # never named either


# ---------------------------------------------------------------- resume and cancel


class _Killed(BaseException):
    """Stands in for the app quitting mid-run."""


def test_backlog_resumes_after_restart(env):
    sids = [_named(env, i) for i in range(4)]  # newest first: sids[3], sids[2], ...

    def die_on_third(session_id):
        if session_id == sids[1]:
            raise _Killed

    env["on_get"] = die_on_third
    with pytest.raises(_Killed):
        _go()
    assert backlog.state() == {"phase": "learning", "done": 2, "total": 4, "running": False}
    assert _sessions("Harry") == sorted(sids[2:])

    env["on_get"] = None
    env["gets"].clear()
    learn_gets = []
    real = backlog._calibrate

    def snap(*a, **kw):
        learn_gets.extend(env["gets"])
        return real(*a, **kw)

    with patch.object(backlog, "_calibrate", side_effect=snap):
        _go()
    assert list(dict.fromkeys(learn_gets)) == [sids[1], sids[0]]  # only the two it hadn't done
    assert _sessions("Harry") == sorted(sids)
    assert backlog.state()["phase"] == "done"


def test_backlog_cancel_stops_after_current(env):
    sids = [_named(env, i) for i in range(4)]
    cancel = threading.Event()

    def cancel_during_second(session_id):
        if session_id == sids[2]:
            cancel.set()

    env["on_get"] = cancel_during_second
    backlog.run(cancel)
    assert _sessions("Harry") == sorted(sids[2:])  # the current one still finished
    assert backlog.state() == {"phase": "cancelled", "done": 2, "total": 4, "running": False}
    assert matching.current_threshold() is None

    env["on_get"] = None
    assert backlog.start() is True
    assert worker.wait_idle(10)
    assert _sessions("Harry") == sorted(sids)
    assert backlog.state() == {"phase": "done", "done": 4, "total": 4, "running": False}


def test_cancel_through_the_worker(env):
    [_named(env, i) for i in range(3)]
    env["on_get"] = lambda sid: backlog.cancel()
    assert backlog.start() is True
    assert worker.wait_idle(10)
    assert backlog.state() == {"phase": "cancelled", "done": 1, "total": 3, "running": False}


def test_backlog_yields_to_other_jobs(env):
    sids = [_named(env, i) for i in range(3)]
    assert backlog.run(threading.Event(), should_yield=lambda: True) is True
    assert backlog.state() == {"phase": "learning", "done": 1, "total": 3, "running": False}
    assert _sessions("Harry") == [sids[2]]


def test_start_without_the_model_stays_pending(env):
    _named(env, 1)
    with patch.object(voice, "model_available", return_value=False):
        assert backlog.start() is False
    assert worker.wait_idle(10)
    assert backlog.state() == {"phase": "idle", "done": 0, "total": 0, "running": False}
    assert env["gets"] == []


def test_start_again_after_done_runs_again(env):
    _named(env, 1)
    _go()
    assert backlog.state()["phase"] == "done"
    _named(env, 2)
    assert backlog.start() is True
    assert worker.wait_idle(10)
    assert backlog.state() == {"phase": "done", "done": 2, "total": 2, "running": False}
    assert _sessions("Harry") == [_sid(1), _sid(2)]


def test_state_file_is_private(env):
    _named(env, 1)
    _go()
    assert os.stat(backlog.STATE_PATH).st_mode & 0o777 == 0o600


def test_nothing_named_before_calibration(env):
    sids = [_named(env, i) for i in range(3)]
    unnamed = _write(env, 10, emb={"Speaker 1": _vec(HARRY)})
    # A live name typed while the backlog is learning queues a re-match: it must name nothing.
    real = backlog._calibrate

    def check(*a, **kw):
        for sid in [*sids, unnamed]:
            assert "speaker_voice_matches" not in _read(env, sid)
        env["enqueue"].assert_not_called()  # nothing to Notion
        return real(*a, **kw)

    def live_rename(session_id):
        if session_id == sids[1]:
            worker._rematch()

    env["on_get"] = live_rename
    with patch.object(backlog, "_calibrate", side_effect=check) as cal:
        _go()
    assert cal.called
    assert speakers.get_names(unnamed) == {"Speaker 1": "Harry"}
    env["enqueue"].assert_called()


# ---------------------------------------------------------------- recalibration


def test_recalibrates_when_instances_double(env):
    for i in range(4):
        _named(env, i)
        env["cache"][_sid(i)] = env["emb"][_sid(i)]
    store.mutate(lambda d: (store.create_person(d, "Harry"), store.create_person(d, "Sam")))
    cal = {"instances": 4, "accuracy": None, "threshold": 0.7, "named": 0, "at": "2026-01-01T00:00:00+00:00"}
    update_config(lambda c: c.update(people_calibration=cal, people_match_threshold=0.7))

    backlog.maybe_recalibrate()  # 8 instances: doubled
    after = load_config()["people_calibration"]
    assert after["instances"] == 8 and after["at"] != cal["at"]
    assert env["gets"] == []  # cached embeddings only, no audio work

    with patch.object(matching, "calibrate") as again:
        backlog.maybe_recalibrate()  # still 8: not doubled since
    again.assert_not_called()


def test_no_recalibration_before_the_backlog_calibrates(env):
    _named(env, 1)
    env["cache"][_sid(1)] = env["emb"][_sid(1)]
    backlog.maybe_recalibrate()
    assert "people_calibration" not in load_config()
    assert matching.current_threshold() is None


def test_learning_a_name_checks_for_recalibration(env):
    sid = _write(env, 1, emb={"Speaker 1": _vec(HARRY)})
    with patch.object(backlog, "maybe_recalibrate") as maybe:
        speakers.set_names(sid, {"Speaker 1": "Harry"})
        assert worker.wait_idle(10)
    maybe.assert_called_once_with()


# ---------------------------------------------------------------- at launch


def test_launch_starts_a_pending_backlog_in_the_background(env):
    _named(env, 1)
    backlog.start_on_launch().join(10)
    assert worker.wait_idle(10)
    assert backlog.state()["phase"] == "done"


@pytest.mark.parametrize("how", ["done", "cancelled", "no model"])
def test_launch_leaves_it_alone(env, how):
    _named(env, 1)
    if how == "done":
        _go()
    elif how == "cancelled":
        cancel = threading.Event()
        cancel.set()
        backlog.run(cancel)
    env["gets"].clear()
    with patch.object(voice, "model_available", return_value=how != "no model"):
        backlog.start_on_launch().join(10)
        assert worker.wait_idle(10)
    assert env["gets"] == []
    assert backlog.state()["phase"] == {"done": "done", "cancelled": "cancelled", "no model": "idle"}[how]


def test_api_server_starts_it_only_when_run_as_the_app():
    from pathlib import Path

    src = Path(backlog.__file__).parents[1].joinpath("api_server.py").read_text()
    main = src.split('if __name__ == "__main__":', 1)
    assert "start_on_launch()" in main[1] and "start_on_launch" not in main[0]


# ---------------------------------------------------------------- review fixes


def test_malformed_transcript_still_calibrates(env):
    for i in range(3):
        _named(env, i)
    bad = [{"start": "soon", "end": "later", "speaker": "Speaker 1"}, "not a segment"]
    sid = _write(env, 9, {"Speaker 1": "Harry"}, {"Speaker 1": _vec(HARRY)}, segments=bad)
    env["cache"][sid] = env["emb"][sid]  # reaches the speech count in calibration
    _go()
    assert load_config()["people_calibration"]["instances"] == 6
    assert matching.current_threshold() == matching.DEFAULT_THRESHOLD
    assert backlog.state()["phase"] == "done"


def test_learn_still_rematches_when_recalibration_fails(env):
    sid = _write(env, 1, emb={"Speaker 1": _vec(HARRY)})
    passes = []
    with (
        patch.object(backlog, "maybe_recalibrate", side_effect=RuntimeError("boom")),
        patch.dict(worker._HANDLERS, {"rematch": lambda: passes.append(1)}),
    ):
        speakers.set_names(sid, {"Speaker 1": "Harry"})
        assert worker.wait_idle(10)
    assert passes == [1]
    assert _sessions("Harry") == [sid]


def test_state_says_whether_it_is_running(env):
    _named(env, 1)
    with worker._cond:  # the worker can't pick the job up yet
        assert backlog.start() is True
        assert backlog.state()["running"] is True
    assert worker.wait_idle(10)
    assert backlog.state()["running"] is False
    # A partial run stopped by a quit: in progress, but not running.
    _named(env, 2)
    backlog._update(lambda s: s.update(phase="learning", learned=[_sid(2)]))
    assert backlog.state() == {"phase": "learning", "done": 1, "total": 2, "running": False}


def test_progress_ignores_deleted_recordings(env):
    sids = [_named(env, i) for i in range(4)]
    backlog.run(threading.Event(), should_yield=lambda: True)
    backlog.run(threading.Event(), should_yield=lambda: True)
    assert backlog.state()["done"] == 2  # sids[3], sids[2]
    (env["trn"] / f"{sids[3]}.json").unlink()
    (env["trn"] / f"{sids[0]}.json").unlink()
    assert backlog.state() == {"phase": "learning", "done": 1, "total": 2, "running": False}
    _go()
    assert backlog._load()["learned"] == [sids[2], sids[1]]
    assert backlog.state() == {"phase": "done", "done": 2, "total": 2, "running": False}


def test_recordings_added_mid_phase_are_picked_up(env):
    _named(env, 1)

    def add_one(session_id):
        if session_id == _sid(1) and _sid(5) not in env["emb"]:
            _named(env, 5)

    env["on_get"] = add_one
    _go()
    assert set(backlog._load()["learned"]) == {_sid(1), _sid(5)}
    assert _sessions("Harry") == [_sid(1), _sid(5)]


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"phase": "learning", "total": "lots", "learned": 5, "named": {"a": 1}, "cancelled": "yes"}),
        json.dumps({"phase": ["learning"]}),
        json.dumps({"phase": "sideways"}),
    ],
)
def test_odd_state_file_never_breaks_state(env, content):
    _named(env, 1)
    backlog.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    backlog.STATE_PATH.write_text(content)
    st = backlog.state()
    assert st["phase"] in ("idle", "learning") and st["done"] == 0
    _go()
    assert backlog.state()["phase"] == "done"


def test_naming_tells_notion_per_recording_and_the_window_once(env):
    from app.utils import changes

    [_named(env, i) for i in range(12)]
    unnamed = [_write(env, 20 + i, emb={"Speaker 1": _vec(HARRY, 0.05)}) for i in range(5)]
    real = backlog.apply.match_recording
    steps = []

    def spy(session_id, *a, **kw):
        steps.append(session_id)
        return real(session_id, *a, **kw)

    with patch.object(backlog.apply, "match_recording", side_effect=spy), patch.object(changes, "bump") as bump:
        _go()
    assert {c.args[0] for c in env["enqueue"].call_args_list} >= set(unnamed)
    assert len(steps) == 17 and bump.call_count == 1
