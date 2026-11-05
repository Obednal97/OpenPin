"""Coaching older reports again ("Coach them again" in Progress): the runner's bulk re-run and its routes.

The bulk re-run picks every finished report saved before Engagement existed and coaches
them one at a time through ``run_coaching(sid, None, force=True)``. Voice, the AI review
and laughter are stubbed as in tests/test_coaching_runner.py.
"""

import json
import threading
import time

import pytest

from app.coaching import runner, store
from tests import test_coaching_runner as base

REVIEW, SEGMENTS = base.REVIEW, base.SEGMENTS
# The runner tests' fixtures: isolated folders, stubbed voice, review and laughter, clean run state
env = base.env
clean_runner_state = base.clean_runner_state

OLD_REPORT = {"status": "done", "status_reason": "", "metrics": {"owner": {"pace_wpm": 120}}, "review": REVIEW}


@pytest.fixture(autouse=True)
def clean_bulk_state():
    runner._bulk.update(running=False, done=0, total=0, current=None, cancel=False)
    yield
    runner._bulk["cancel"] = True
    _wait_idle()
    runner._bulk.update(running=False, done=0, total=0, current=None, cancel=False)


def _wait_idle(timeout=5.0):
    end = time.time() + timeout
    while runner.rerun_all_status()["running"] and time.time() < end:
        time.sleep(0.01)
    assert not runner.rerun_all_status()["running"], "bulk re-run never finished"


def _transcript(trn, sid):
    text = " ".join(s["text"] for s in SEGMENTS)
    (trn / f"{sid}.json").write_text(json.dumps({"text": text, "duration": 180.0, "segments": SEGMENTS}))


def _sidecar(sid, **fields):
    store.update(sid, lambda d: d.update(fields))


def _old_reports(env, sids):
    for sid in sids:
        _transcript(env["trn"], sid)
        _sidecar(sid, **OLD_REPORT)


@pytest.fixture
def real_threads(monkeypatch):
    """The bulk run on a real daemon thread (env makes runner.Thread synchronous)."""
    monkeypatch.setattr(runner, "Thread", threading.Thread)


# ---------------------------------------------------------------- selection


def test_selects_done_reports_without_engagement_that_can_be_coached(env):
    _old_reports(env, ["1715000001", "1715000009"])
    # Already has Engagement
    _transcript(env["trn"], "1715000002")
    _sidecar("1715000002", **OLD_REPORT, engagement={"score": 50})
    # Excluded ("Don't coach this recording"), even if an old status says done
    _transcript(env["trn"], "1715000003")
    _sidecar("1715000003", **OLD_REPORT, excluded=True)
    # No transcript any more
    _sidecar("1715000004", **OLD_REPORT)
    # Not finished: failed, needs an owner, skipped, running
    for sid, status in (("1715000005", "failed"), ("1715000006", "needs_owner"), ("1715000007", "skipped")):
        _transcript(env["trn"], sid)
        _sidecar(sid, status=status)
    _transcript(env["trn"], "1715000008")
    _sidecar("1715000008", status="running")
    # The store's other files are never recordings
    store.save_words("1715000001", [{"w": "hi"}], "h")
    (store.COACHING_DIR / "voice_profile.json").write_text("{}")

    assert runner.rerun_candidates() == ["1715000001", "1715000009"]


def test_no_coaching_folder_means_nothing_to_do(env):
    assert runner.rerun_candidates() == []


# ---------------------------------------------------------------- running them


def test_runs_one_at_a_time_on_one_thread(env, real_threads, monkeypatch):
    sids = ["1715000001", "1715000002", "1715000003"]
    _old_reports(env, sids)
    calls, threads = [], set()
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    def fake_run(sid, audio_path, force=False):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        calls.append((sid, audio_path, force))
        threads.add(threading.get_ident())
        time.sleep(0.03)
        with lock:
            state["now"] -= 1
        return {}

    monkeypatch.setattr(runner, "run_coaching", fake_run)
    assert runner.start_rerun_all() is True
    _wait_idle()
    assert calls == [(sid, None, True) for sid in sids]
    assert state["max"] == 1
    assert len(threads) == 1 and threading.get_ident() not in threads
    assert runner.rerun_all_status() == {"running": False, "done": 3, "total": 3, "current": None}


def test_status_while_running(env, real_threads, monkeypatch):
    _old_reports(env, ["1715000001", "1715000002"])
    release = threading.Event()
    started = threading.Event()

    def fake_run(sid, audio_path, force=False):
        started.set()
        release.wait(5)
        return {}

    monkeypatch.setattr(runner, "run_coaching", fake_run)
    runner.start_rerun_all()
    assert started.wait(5)
    assert runner.rerun_all_status() == {"running": True, "done": 0, "total": 2, "current": "1715000001"}
    release.set()
    _wait_idle()


def test_only_one_bulk_run_at_a_time(env, real_threads, monkeypatch):
    _old_reports(env, ["1715000001"])
    release = threading.Event()
    calls = []

    def fake_run(sid, audio_path, force=False):
        calls.append(sid)
        release.wait(5)
        return {}

    monkeypatch.setattr(runner, "run_coaching", fake_run)
    assert runner.start_rerun_all() is True
    assert runner.start_rerun_all() is False
    release.set()
    _wait_idle()
    assert calls == ["1715000001"]
    # Once it has finished, another can start
    assert runner.start_rerun_all() is True
    _wait_idle()


def test_cancel_stops_after_the_current_recording(env, real_threads, monkeypatch):
    _old_reports(env, ["1715000001", "1715000002", "1715000003"])
    release = threading.Event()
    started = threading.Event()
    calls = []

    def fake_run(sid, audio_path, force=False):
        calls.append(sid)
        started.set()
        release.wait(5)
        return {}

    monkeypatch.setattr(runner, "run_coaching", fake_run)
    runner.start_rerun_all()
    assert started.wait(5)
    assert runner.cancel_rerun_all() is True
    release.set()
    _wait_idle()
    assert calls == ["1715000001"]
    assert runner.rerun_all_status() == {"running": False, "done": 1, "total": 3, "current": None}


def test_cancel_when_nothing_runs(env):
    assert runner.cancel_rerun_all() is False


def test_a_failure_on_one_recording_never_stops_the_rest(env, monkeypatch):
    _old_reports(env, ["1715000001", "1715000002", "1715000003"])
    calls = []

    def fake_run(sid, audio_path, force=False):
        calls.append(sid)
        if sid == "1715000002":
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr(runner, "run_coaching", fake_run)
    runner.start_rerun_all()
    _wait_idle()
    assert calls == ["1715000001", "1715000002", "1715000003"]
    assert runner.rerun_all_status()["done"] == 3


def test_a_recording_that_changed_since_the_list_was_made_is_left_alone(env, monkeypatch):
    _old_reports(env, ["1715000001", "1715000002", "1715000003"])
    calls = []

    def fake_run(sid, audio_path, force=False):
        calls.append(sid)
        # Meanwhile the user excludes one and coaches another again themselves
        runner.set_excluded("1715000002", True)
        _sidecar("1715000003", engagement={"score": 40})
        return {}

    monkeypatch.setattr(runner, "run_coaching", fake_run)
    runner.start_rerun_all()
    _wait_idle()
    assert calls == ["1715000001"]


def test_real_run_adds_engagement_keeps_the_pick_and_never_learns_the_voice(env):
    sid = "1715000001"
    _transcript(env["trn"], sid)
    owner = {
        "labels": ["Speaker 2"],
        "source": "user",
        "confidence": 1.0,
        "labelling": runner.labelling_fingerprint(SEGMENTS),
    }
    _sidecar(sid, **OLD_REPORT, owner=owner)
    runner.start_rerun_all()  # env runs the thread inline
    saved = store.load(sid)
    assert saved["status"] == "done"
    assert "engagement" in saved
    assert saved["owner"] == owner
    env["match_owner"].assert_not_called()
    env["add_exemplars"].assert_not_called()
    env["review"].assert_called_once()
    assert runner.rerun_candidates() == []


def test_ai_review_off_makes_no_ai_calls(env):
    env["config"]["coaching_ai_review"] = False
    _old_reports(env, ["1715000001", "1715000002"])
    runner.start_rerun_all()
    env["review"].assert_not_called()
    for sid in ("1715000001", "1715000002"):
        assert "engagement" in store.load(sid)
        assert store.load(sid)["status_reason"] == runner.REVIEW_OFF


# ---------------------------------------------------------------- routes


@pytest.fixture
def client(env, flask_client, monkeypatch):
    monkeypatch.setattr(runner, "current", lambda sid: store.load(sid), raising=False)
    return flask_client


def test_get_status_counts_what_qualifies_now(client):
    _old_reports({"trn": runner.TRANSCRIPTS_DIR}, ["1715000001", "1715000002"])
    r = client.get("/coaching/rerun-all")
    assert r.status_code == 200
    assert r.get_json() == {"pending_count": 2, "running": False, "done": 0, "total": 0, "current": None}


def test_post_starts_and_409_while_running(client, monkeypatch):
    started = []
    monkeypatch.setattr(runner, "start_rerun_all", lambda: started.append(1) or len(started) == 1)
    first = client.post("/coaching/rerun-all")
    assert first.status_code == 200
    assert first.get_json()["started"] is True
    again = client.post("/coaching/rerun-all")
    assert again.status_code == 409
    assert "error" in again.get_json()


def test_post_cancel(client, monkeypatch):
    cancelled = []
    monkeypatch.setattr(runner, "cancel_rerun_all", lambda: cancelled.append(1) or True)
    r = client.post("/coaching/rerun-all/cancel")
    assert r.status_code == 200
    assert r.get_json()["cancelled"] is True
    assert cancelled == [1]


def test_routes_end_to_end(client):
    _old_reports({"trn": runner.TRANSCRIPTS_DIR}, ["1715000001"])
    assert client.post("/coaching/rerun-all").status_code == 200  # inline thread: already finished
    body = client.get("/coaching/rerun-all").get_json()
    assert body == {"pending_count": 0, "running": False, "done": 1, "total": 1, "current": None}
