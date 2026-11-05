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
    # An older sidecar with inline words, plus the words file: neither is ever returned.
    store.update(SID, lambda d: d.update(status="done", words=[{"w": "secret"}]))
    store.save_words(SID, [{"w": "verbatim"}], "h")
    store.update(SID, lambda d: d.update(words=[{"w": "secret"}]))
    monkeypatch.setattr(runner, "start_set_owner", lambda sid, labels, learn: store.load(sid))
    for resp in (
        env.get(f"/coaching/{SID}"),
        env.post(f"/coaching/{SID}/mixed-up", json={"mixed_up": True}),
        env.post(f"/coaching/{SID}/owner", json={"labels": ["Speaker 1"]}),
    ):
        assert resp.status_code == 200
        text = resp.get_data(as_text=True)
        assert "secret" not in text and "verbatim" not in text
        assert "words" not in resp.get_json()


def test_every_sidecar_response_has_the_same_shape(env, monkeypatch):
    store.update(SID, lambda d: d.update(status="done", mixed_up=False))
    monkeypatch.setattr(runner, "start_set_owner", lambda sid, labels, learn: store.load(sid))
    got = env.get(f"/coaching/{SID}").get_json()
    mixed = env.post(f"/coaching/{SID}/mixed-up", json={"mixed_up": False}).get_json()
    owner = env.post(f"/coaching/{SID}/owner", json={"labels": ["Speaker 1"]}).get_json()
    assert got == mixed
    assert owner.pop("started") is True
    assert owner == got
    assert got["targets"]["pace_wpm"]["high"] == 160.0


def test_run_starts_forced(env, monkeypatch):
    start = MagicMock()
    monkeypatch.setattr(runner, "start_coaching_async", start)
    r = env.post(f"/coaching/{SID}/run")
    assert r.get_json() == {"started": True}
    start.assert_called_once_with(SID, None, force=True)


def test_owner_response_already_says_running(env, monkeypatch):
    store.update(SID, lambda d: d.update(status="needs_owner", status_reason="no voice profile"))
    held = []
    monkeypatch.setattr(runner, "Thread", lambda target, kwargs, daemon: MagicMock(start=lambda: held.append(kwargs)))
    r = env.post(f"/coaching/{SID}/owner", json={"labels": ["Speaker 1"], "learn": False})
    assert r.status_code == 200
    body = r.get_json()
    assert body["started"] is True
    assert body["status"] == "running" and body["status_reason"] == ""
    assert body["owner"]["labels"] == ["Speaker 1"]
    assert "targets" in body
    assert held[0]["learn"] is False and held[0]["chosen"] == ["Speaker 1"]
    runner._running.clear()


def test_owner_error_in_background_is_swallowed(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(runner, "_run_once", boom)
    monkeypatch.setattr(runner, "Thread", _SyncThread)
    assert env.post(f"/coaching/{SID}/owner", json={"labels": ["Speaker 1"], "learn": False}).status_code == 200
    assert not runner.is_running(SID)


@pytest.mark.parametrize("labels", [[], ["Speaker 9"], "Speaker 1", None])
def test_owner_rejects_bad_labels(env, monkeypatch, labels):
    set_owner = MagicMock()
    monkeypatch.setattr(runner, "start_set_owner", set_owner)
    assert env.post(f"/coaching/{SID}/owner", json={"labels": labels}).status_code == 400
    set_owner.assert_not_called()


def test_owner_missing_transcript_404(env):
    assert env.post("/coaching/999/owner", json={"labels": ["Speaker 1"]}).status_code == 404


def test_run_and_mixed_up_need_a_transcript(env, monkeypatch):
    start = MagicMock()
    monkeypatch.setattr(runner, "start_coaching_async", start)
    assert env.post("/coaching/999/run").status_code == 404
    assert env.post("/coaching/999/mixed-up", json={"mixed_up": True}).status_code == 404
    start.assert_not_called()
    assert not store.coaching_path("999").exists()
    assert list(store.COACHING_DIR.iterdir()) == []


def test_store_files_are_not_session_ids(env, monkeypatch):
    voice._save_exemplars([[0.1, 0.2]])
    mode = voice.PROFILE_PATH.stat().st_mode & 0o777
    store.save_words(SID, [{"w": "x"}], "h")
    start = MagicMock()
    monkeypatch.setattr(runner, "start_coaching_async", start)
    for sid in ("voice_profile", f"{SID}.words"):
        assert env.get(f"/coaching/{sid}").status_code == 404
        assert env.post(f"/coaching/{sid}/mixed-up", json={"mixed_up": True}).status_code == 404
        assert env.post(f"/coaching/{sid}/run").status_code == 404
        assert env.post(f"/coaching/{sid}/owner", json={"labels": ["Speaker 1"]}).status_code == 404
    start.assert_not_called()
    assert mode == 0o600
    assert voice.PROFILE_PATH.stat().st_mode & 0o777 == 0o600
    assert voice._load_exemplars() == [[0.1, 0.2]]
    assert json.loads(store.words_path(SID).read_text())["words"] == [{"w": "x"}]


def test_mixed_up_sets_flag(env):
    r = env.post(f"/coaching/{SID}/mixed-up", json={"mixed_up": True})
    assert r.get_json()["mixed_up"] is True
    assert store.load(SID)["mixed_up"] is True
    assert env.post(f"/coaching/{SID}/mixed-up", json={"mixed_up": "yes"}).status_code == 400


def test_progress_route(env):
    now = time.time()
    # A done session dated by its id (a recent epoch), plus noise that must be skipped.
    recent = str(int(now) - 3600)
    deleted = str(int(now) - 7200)
    done = {"status": "done", "mixed_up": False, "metrics": {"owner": {"pace_wpm": 130.0}}}
    (runner.TRANSCRIPTS_DIR / f"{recent}.json").write_text(json.dumps({"text": "x", "segments": SEGMENTS}))
    store.update(recent, lambda d: d.update(done))
    store.save_words(recent, [{"word": "um"}], "h")
    store.update(deleted, lambda d: d.update(done))  # its transcript is gone: not counted
    voice._save_exemplars([[0.1, 0.2]])
    (store.COACHING_DIR / "notes.txt").write_text("x")
    r = env.get("/coaching/progress?weeks=999")
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == 1
    assert [p["session_id"] for p in body["series"]["pace_wpm"]] == [recent]
    assert env.get("/coaching/progress?weeks=abc").status_code == 200


def test_progress_lists_only_sidecars(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(routes, "compute_progress", lambda sc, now, weeks: seen.update(sids=[s[0] for s in sc]) or {})
    store.update(SID, lambda d: d.update(status="done"))
    store.save_words(SID, [{"word": "um"}], "h")
    voice._save_exemplars([[0.1, 0.2]])
    env.get("/coaching/progress")
    assert seen["sids"] == [SID]


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
    assert env.get("/coaching/voice").get_json()["install_error"] == "no network"
    # A retry clears the stale error before it returns.
    assert env.post("/coaching/voice/install").get_json() == {"started": True}
    now = env.get("/coaching/voice").get_json()
    assert now["installing"] is True and now["install_error"] is None
    started[-1]()
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


def _fake_embeddings(monkeypatch, result):
    monkeypatch.setattr(voice, "model_available", lambda: True)
    monkeypatch.setattr(runner, "_resolve_audio", lambda sid, p: "a.ogg")
    monkeypatch.setattr(voice, "speaker_embeddings", lambda audio, segs: result)


def test_voice_setup_replace_swaps_profile(env, monkeypatch):
    voice._save_exemplars([[1.0, 0.0], [0.0, 1.0]])
    _fake_embeddings(monkeypatch, {"Speaker 1": [0.5, 0.5]})
    r = env.post("/coaching/voice/setup", json={"session_id": SID, "labels": ["Speaker 1"], "replace": True})
    assert r.get_json() == {"exemplars": 1}
    assert voice._load_exemplars() == [[0.5, 0.5]]


def test_voice_setup_replace_failure_keeps_old_voice(env, monkeypatch):
    voice._save_exemplars([[1.0, 0.0]])
    _fake_embeddings(monkeypatch, {})
    r = env.post("/coaching/voice/setup", json={"session_id": SID, "labels": ["Speaker 1"], "replace": True})
    assert r.get_json() == {"exemplars": 0}
    assert voice._load_exemplars() == [[1.0, 0.0]]


def test_voice_setup_without_replace_appends(env, monkeypatch):
    voice._save_exemplars([[1.0, 0.0]])
    _fake_embeddings(monkeypatch, {"Speaker 1": [0.5, 0.5]})
    env.post("/coaching/voice/setup", json={"session_id": SID, "labels": ["Speaker 1"]})
    assert len(voice._load_exemplars()) == 2


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


def test_diarize_off_wins_in_the_same_request(env):
    env.post("/settings", json={"diarize": True, "coaching_auto": True})
    env.post("/settings", json={"diarize": False, "coaching_auto": True})
    body = env.get("/settings").get_json()
    assert body["diarize"] is False and body["coaching_auto"] is False
    env.post("/settings", json={"coaching_auto": False, "diarize": True})
    body = env.get("/settings").get_json()
    assert body["diarize"] is True and body["coaching_auto"] is False


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
