"""Coaching routes, settings and the speaker-labels migration. Runner, voice and AI are stubbed."""

import json
import time
from unittest.mock import MagicMock

import pytest

from app.coaching import runner, store, voice
from app.routes import coaching as routes
from app.utils.config import load_config, update_config

SID = "1715000000"
SEGMENTS = [
    {"start": 0.0, "end": 20.0, "text": "Hello there.", "speaker": "Speaker 1"},
    {"start": 21.0, "end": 60.0, "text": "Hi, how are you?", "speaker": "Speaker 2"},
]


@pytest.fixture
def env(tmp_path, monkeypatch, flask_client):
    coaching_dir = tmp_path / "coaching"
    coaching_dir.mkdir()
    trn = tmp_path / "transcripts"
    trn.mkdir()
    monkeypatch.setattr(store, "COACHING_DIR", coaching_dir)
    monkeypatch.setattr(runner, "TRANSCRIPTS_DIR", trn)
    monkeypatch.setattr(voice, "PROFILE_PATH", coaching_dir / "voice_profile.json")
    monkeypatch.setattr(runner, "current", lambda sid: store.load(sid), raising=False)
    monkeypatch.setattr(routes, "_voice_state", {"installing": False, "error": None})
    (trn / f"{SID}.json").write_text(json.dumps({"text": "x", "segments": SEGMENTS}))
    return flask_client


def test_get_coaching_drops_words_and_adds_targets(env):
    store.update(SID, lambda d: d.update(status="done", words=[{"w": "um"}], metrics={"pace_wpm": 120}))
    r = env.get(f"/coaching/{SID}")
    assert r.status_code == 200
    body = r.get_json()
    assert "words" not in body
    assert body["status"] == "done"
    assert "pace_wpm" in body["targets"]


def test_words_never_returned(env, monkeypatch):
    store.update(SID, lambda d: d.update(status="done", words=[{"w": "secret"}]))
    monkeypatch.setattr(runner, "set_owner", MagicMock())
    for resp in (
        env.get(f"/coaching/{SID}"),
        env.post(f"/coaching/{SID}/mixed-up", json={"mixed_up": True}),
        env.post(f"/coaching/{SID}/owner", json={"labels": ["Speaker 1"]}),
    ):
        assert resp.status_code == 200
        assert "secret" not in resp.get_data(as_text=True)


def test_run_starts_forced(env, monkeypatch):
    start = MagicMock()
    monkeypatch.setattr(runner, "start_coaching_async", start)
    r = env.post(f"/coaching/{SID}/run")
    assert r.get_json() == {"started": True}
    start.assert_called_once_with(SID, None, force=True)


def test_owner_runs_set_owner_in_background(env, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "set_owner", lambda *a: calls.append(a))
    monkeypatch.setattr(routes, "Thread", _SyncThread)
    r = env.post(f"/coaching/{SID}/owner", json={"labels": ["Speaker 1"], "learn": False})
    assert r.status_code == 200
    assert r.get_json()["started"] is True
    assert calls == [(SID, ["Speaker 1"], False)]


def test_owner_error_in_background_is_swallowed(env, monkeypatch):
    def boom(*a):
        raise RuntimeError("nope")

    monkeypatch.setattr(runner, "set_owner", boom)
    monkeypatch.setattr(routes, "Thread", _SyncThread)
    assert env.post(f"/coaching/{SID}/owner", json={"labels": ["Speaker 1"]}).status_code == 200


@pytest.mark.parametrize("labels", [[], ["Speaker 9"], "Speaker 1", None])
def test_owner_rejects_bad_labels(env, monkeypatch, labels):
    set_owner = MagicMock()
    monkeypatch.setattr(runner, "set_owner", set_owner)
    assert env.post(f"/coaching/{SID}/owner", json={"labels": labels}).status_code == 400
    set_owner.assert_not_called()


def test_owner_missing_transcript_404(env):
    assert env.post("/coaching/999/owner", json={"labels": ["Speaker 1"]}).status_code == 404


def test_mixed_up_sets_flag(env):
    r = env.post(f"/coaching/{SID}/mixed-up", json={"mixed_up": True})
    assert r.get_json()["mixed_up"] is True
    assert store.load(SID)["mixed_up"] is True
    assert env.post(f"/coaching/{SID}/mixed-up", json={"mixed_up": "yes"}).status_code == 400


def test_progress_route(env):
    now = time.time()
    # A done session dated by its id (a recent epoch), plus noise that must be skipped.
    recent = str(int(now) - 3600)
    store.update(recent, lambda d: d.update(status="done", mixed_up=False, metrics={"pace_wpm": 130.0}))
    (store.COACHING_DIR / "notes.txt").write_text("x")
    r = env.get("/coaching/progress?weeks=999")
    assert r.status_code == 200
    body = r.get_json()
    assert "baseline" in body or "weekly" in body or "series" in body
    assert env.get("/coaching/progress?weeks=abc").status_code == 200


def test_progress_clamps_weeks(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        routes, "compute_progress", lambda sc, now, weeks: seen.update(weeks=weeks, now=now) or {"ok": True}
    )
    env.get("/coaching/progress?weeks=999")
    assert seen["weeks"] == 52
    env.get("/coaching/progress?weeks=0")
    assert seen["weeks"] == 1
    env.get("/coaching/progress")
    assert seen["weeks"] == 12


def test_voice_status(env, monkeypatch):
    monkeypatch.setattr(voice, "model_available", lambda: False)
    r = env.get("/coaching/voice")
    assert r.status_code == 200
    assert r.get_json() == {
        "profile": False,
        "exemplars": 0,
        "model": False,
        "installing": False,
        "install_error": None,
    }


def test_voice_install_runs_once_and_reports_error(env, monkeypatch):
    started = []

    class Holding:
        def __init__(self, target, daemon):
            started.append(target)

        def start(self):
            pass

    monkeypatch.setattr(routes, "Thread", Holding)
    assert env.post("/coaching/voice/install").get_json() == {"started": True}
    assert env.post("/coaching/voice/install").get_json() == {"started": True}
    assert len(started) == 1
    assert env.get("/coaching/voice").get_json()["installing"] is True

    monkeypatch.setattr(voice, "install_model", lambda: (False, "no network"))
    started[0]()
    status = env.get("/coaching/voice").get_json()
    assert status["installing"] is False
    assert status["install_error"] == "no network"


def test_voice_setup_409_without_model(env, monkeypatch):
    monkeypatch.setattr(voice, "model_available", lambda: False)
    r = env.post("/coaching/voice/setup", json={"session_id": SID, "labels": ["Speaker 1"]})
    assert r.status_code == 409
    assert r.get_json() == {"error": "voice model not installed"}


def test_voice_setup_adds_exemplars(env, monkeypatch, tmp_path):
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"x")
    monkeypatch.setattr(voice, "model_available", lambda: True)
    monkeypatch.setattr(runner, "_resolve_audio", lambda sid, p: str(audio))
    add = MagicMock(return_value=1)
    monkeypatch.setattr(voice, "add_exemplars", add)
    r = env.post("/coaching/voice/setup", json={"session_id": SID, "labels": ["Speaker 1"]})
    assert r.get_json() == {"exemplars": 1}
    add.assert_called_once_with(str(audio), SEGMENTS, ["Speaker 1"])
    bad = env.post("/coaching/voice/setup", json={"session_id": SID, "labels": ["Nobody"]})
    assert bad.status_code == 400


def test_voice_delete(env):
    voice._save_exemplars([[0.1, 0.2]])
    assert env.get("/coaching/voice").get_json()["exemplars"] == 1
    assert env.delete("/coaching/voice").get_json() == {"deleted": True}
    assert env.get("/coaching/voice").get_json()["exemplars"] == 0


def test_bad_session_id_404(env):
    assert env.get("/coaching/.hidden").status_code == 404
    assert env.post("/coaching/.hidden/run").status_code == 404
    assert env.post("/coaching/.hidden/owner", json={"labels": ["a"]}).status_code == 404
    assert env.post("/coaching/.hidden/mixed-up", json={"mixed_up": True}).status_code == 404
    assert env.post("/coaching/voice/setup", json={"session_id": "../x", "labels": ["a"]}).status_code == 404


def test_routes_need_the_token(env):
    assert env.get("/coaching/progress", headers={"X-Plaud-Token": "wrong"}).status_code in (401, 403)


def test_migration_turns_on_speaker_labels_once(env):
    update_config(lambda c: c.update(diarize=False))
    routes.migrate_speaker_labels()
    cfg = load_config()
    assert cfg["diarize"] is True and cfg["coaching_auto"] is True and cfg["coaching_migrated"] is True
    update_config(lambda c: c.update(diarize=False))
    routes.migrate_speaker_labels()
    assert load_config()["diarize"] is False


def test_migration_keeps_an_existing_coaching_auto(env):
    update_config(lambda c: c.update(coaching_auto=False))
    routes.migrate_speaker_labels()
    assert load_config()["coaching_auto"] is False


def test_settings_coaching_auto_default_and_whitelist(env):
    assert env.get("/settings").get_json()["coaching_auto"] is True
    env.post("/settings", json={"coaching_auto": False})
    assert env.get("/settings").get_json()["coaching_auto"] is False


def test_diarize_off_turns_coaching_off(env):
    env.post("/settings", json={"diarize": True, "coaching_auto": True})
    env.post("/settings", json={"diarize": False})
    body = env.get("/settings").get_json()
    assert body["diarize"] is False and body["coaching_auto"] is False


class _SyncThread:
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)
