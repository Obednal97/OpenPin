"""Phase-1 data-integrity tests: session-id validation, path containment,
locked config updates, transcript merging, re-download backup and Trash."""

import json
import sys
import types
from unittest.mock import patch

import pytest

from app.utils.paths import (
    InvalidSessionIdError,
    PathOutsideError,
    is_auxiliary_stem,
    resolve_inside,
    validate_session_id,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env(flask_client, tmp_path):
    """Flask client with recordings/transcripts/backup/trash dirs in tmp_path.

    ``env.system_trash`` collects paths the (mocked) macOS Trash received.
    """
    rec = tmp_path / "recordings"
    trn = tmp_path / "transcripts"
    cloud_dir = tmp_path / "cloud_recordings_decoded"
    backup = tmp_path / ".redownload_backup"
    app_trash = tmp_path / ".trash"
    sys_trash = tmp_path / "SystemTrash"
    for d in (rec, trn, cloud_dir, sys_trash):
        d.mkdir()

    trashed = []

    def fake_system_trash(path):
        dest = sys_trash / path.name
        path.rename(dest)
        trashed.append(dest)
        return True

    with (
        patch("app.routes.recordings.TRANSCRIPTS_DIR", trn),
        patch("app.routes.transcribe.TRANSCRIPTS_DIR", trn),
        patch("app.routes.recordings.get_all_recordings_dirs", return_value=[rec]),
        patch("app.routes.recordings.get_recordings_dir", return_value=rec),
        patch("app.utils.constants.get_all_recordings_dirs", return_value=[rec]),
        patch("app.utils.constants.CLOUD_RECORDINGS_DIR", cloud_dir),
        patch("app.utils.redownload_backup.get_recordings_dir", return_value=rec),
        patch("app.utils.redownload_backup.REDOWNLOAD_BACKUP_DIR", backup),
        patch("app.routes.recordings.APP_TRASH_DIR", app_trash),
        patch("app.utils.trash.APP_TRASH_DIR", app_trash),
        patch("app.utils.trash._system_trash", side_effect=fake_system_trash),
        patch("app.state.gdrive.get_cloud_only_recordings", return_value=[]),
    ):
        ns = types.SimpleNamespace(
            client=flask_client,
            rec=rec,
            trn=trn,
            backup=backup,
            app_trash=app_trash,
            system_trash=trashed,
            root=tmp_path,
        )
        yield ns


def _post(client, url, body):
    return client.post(url, json=body, content_type="application/json")


def _cfg():
    from app.utils.config import load_config

    return load_config()


# ---------------------------------------------------------------------------
# validate_session_id / resolve_inside
# ---------------------------------------------------------------------------

GOOD_IDS = [
    "1715000000",
    "1715000000_uncropped",
    "cloud_2026-03-27_1234_recording_name",
    "cloud_2026-03-27_1234_Meeting_w-_Bob_(1)",
    "cloud_2026-03-27_1234_Café_Ünïcode,_notes!",
    "fake_id_999",
    "a",
    "x" * 128,
]

BAD_IDS = [
    "",
    ".",
    "..",
    "../x",
    "../../etc/passwd",
    "/etc/passwd",
    "a/b",
    "a\\b",
    ".hidden",
    "a\x00b",
    "a\nb",
    "a:b",
    "x" * 129,
    None,
    123,
    ["1"],
]


@pytest.mark.parametrize("sid", GOOD_IDS)
def test_validate_session_id_accepts_real_ids(sid):
    assert validate_session_id(sid) == sid


@pytest.mark.parametrize("sid", BAD_IDS)
def test_validate_session_id_rejects_unsafe(sid):
    with pytest.raises(InvalidSessionIdError):
        validate_session_id(sid)
    assert issubclass(InvalidSessionIdError, ValueError)


def test_is_auxiliary_stem():
    assert is_auxiliary_stem("123_uncropped")
    assert is_auxiliary_stem("123_crop_tmp")
    assert not is_auxiliary_stem("123")


def test_resolve_inside_contains_and_rejects(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "a.ogg").write_text("x")
    outside = tmp_path / "outside.ogg"
    outside.write_text("y")
    (base / "link.ogg").symlink_to(outside)

    assert resolve_inside([base], "a.ogg") == (base / "a.ogg").resolve()
    assert resolve_inside([base], str(base / "a.ogg"), must_exist=True) == (base / "a.ogg").resolve()
    with pytest.raises(PathOutsideError):
        resolve_inside([base], "../outside.ogg")
    with pytest.raises(PathOutsideError):
        resolve_inside([base], str(outside))
    with pytest.raises(PathOutsideError):
        resolve_inside([base], "link.ogg")  # symlink escaping the base
    with pytest.raises(FileNotFoundError):
        resolve_inside([base], "missing.ogg", must_exist=True)
    with pytest.raises(PathOutsideError):
        resolve_inside([base], "a\x00b")


# ---------------------------------------------------------------------------
# Every route rejects traversal ids with 400
# ---------------------------------------------------------------------------

TRAVERSAL = ["../../victim", "/etc/passwd", "..", "a/b"]


def _route_bodies(sid):
    return [
        ("/recordings/tag", {"session_id": sid, "tags": ["X"]}),
        ("/recordings/rename", {"session_id": sid, "title": "T"}),
        ("/recordings/export", {"session_id": sid}),
        ("/recordings/crop", {"session_id": sid, "start": 0, "end": 1}),
        ("/recordings/delete", {"session_ids": [sid]}),
        ("/recordings/delete", {"session_ids": ["1715000000", sid]}),
        ("/recordings/redownload", {"session_ids": [sid]}),
        ("/recordings/restore-redownload", {"session_ids": [sid]}),
        ("/recordings/move", {"session_ids": [sid], "destination": "/tmp/plaud-test-never-created"}),
        ("/transcripts/update", {"session_id": sid, "summary": "pwned"}),
        ("/transcripts/delete", {"session_id": sid}),
        ("/summarize", {"session_id": sid}),
        ("/gdrive/upload", {"session_id": sid}),
    ]


@pytest.mark.parametrize("sid", TRAVERSAL)
def test_routes_reject_traversal_ids(env, sid):
    for url, body in _route_bodies(sid):
        r = _post(env.client, url, body)
        assert r.status_code == 400, f"{url} accepted {sid!r}: {r.status_code} {r.get_json()}"
        assert "error" in r.get_json()


def test_traversal_delete_does_not_touch_outside_files(env):
    victim_dir = env.root / "victim"
    victim_dir.mkdir()
    for ext in ("ogg", "json", "txt"):
        (victim_dir / f"secret.{ext}").write_text("keep me")
    sid = "../victim/secret"
    _post(env.client, "/recordings/delete", {"session_ids": [sid]})
    _post(env.client, "/transcripts/delete", {"session_id": sid})
    _post(env.client, "/transcripts/update", {"session_id": sid, "summary": "x"})
    _post(env.client, "/recordings/redownload", {"session_ids": [sid]})
    for ext in ("ogg", "json", "txt"):
        assert (victim_dir / f"secret.{ext}").read_text() == "keep me"
    assert env.system_trash == []


def test_non_list_session_ids_rejected(env):
    r = _post(env.client, "/recordings/delete", {"session_ids": "1715000000"})
    assert r.status_code == 400


def test_transcribe_rejects_paths_outside_and_aux_files(env):
    outside = env.root / "outside.ogg"
    outside.write_bytes(b"OggS")
    r = _post(env.client, "/transcribe", {"path": str(outside)})
    assert r.status_code == 400
    r = _post(env.client, "/transcribe", {"path": str(env.rec / ".." / "outside.ogg")})
    assert r.status_code == 400
    (env.rec / "link.ogg").symlink_to(outside)
    r = _post(env.client, "/transcribe", {"path": str(env.rec / "link.ogg")})
    assert r.status_code == 400
    r = _post(env.client, "/transcribe", {"path": "relative.ogg"})
    assert r.status_code == 400
    (env.rec / "1715000000_uncropped.ogg").write_bytes(b"OggS")
    r = _post(env.client, "/transcribe", {"path": str(env.rec / "1715000000_uncropped.ogg")})
    assert r.status_code == 400


def test_move_rejects_bad_destinations(env):
    (env.rec / "1715000000.ogg").write_bytes(b"OggS")
    for dest in ("relative/dir", "/tmp/../tmp/x", str(env.trn)):
        r = _post(env.client, "/recordings/move", {"session_ids": ["1715000000"], "destination": dest})
        assert r.status_code == 400, dest
    assert (env.rec / "1715000000.ogg").exists()


# ---------------------------------------------------------------------------
# Stale config writes
# ---------------------------------------------------------------------------


def test_summarize_does_not_clobber_config_written_during_ai_call(env):
    sid = "1715000000"
    (env.trn / f"{sid}.json").write_text(json.dumps({"text": "A long enough transcript to summarise here."}))

    def slow_summarize(text, config, extra):
        # While the "AI call" runs, another request writes to the config.
        from app.utils.config import update_config

        update_config(lambda c: c.setdefault("deleted_ids", []).append("deleted_meanwhile"))
        update_config(lambda c: c.__setitem__("deepgram_api_key", "new-key"))
        return "Summary text.", "A Title", "anthropic", []

    with patch("app.routes.transcribe.summarize", side_effect=slow_summarize):
        r = _post(env.client, "/summarize", {"session_id": sid})
    assert r.status_code == 200, r.get_json()
    cfg = _cfg()
    assert "deleted_meanwhile" in cfg["deleted_ids"]
    assert cfg["deepgram_api_key"] == "new-key"
    assert cfg["recording_names"][sid] == "A Title"
    t = json.loads((env.trn / f"{sid}.json").read_text())
    assert t["summary"] == "Summary text."
    assert "summary_stale" not in t


def test_summarize_keeps_existing_custom_name(env):
    from app.utils.config import save_config

    sid = "1715000000"
    save_config({"recording_names": {sid: "Mine"}})
    (env.trn / f"{sid}.json").write_text(json.dumps({"text": "A long enough transcript to summarise here."}))
    with patch("app.routes.transcribe.summarize", return_value=("S.", "AI Title", "x", [])):
        _post(env.client, "/summarize", {"session_id": sid})
    assert _cfg()["recording_names"][sid] == "Mine"


def test_tag_and_rename_use_locked_updates(env):
    from app.utils.config import save_config

    save_config({"deleted_ids": ["keep"], "recording_tags": {"other": ["Y"]}})
    _post(env.client, "/recordings/tag", {"session_id": "1715000000", "tags": ["X"]})
    _post(env.client, "/recordings/rename", {"session_id": "1715000000", "title": "T"})
    cfg = _cfg()
    assert cfg["deleted_ids"] == ["keep"]
    assert cfg["recording_tags"] == {"other": ["Y"], "1715000000": ["X"]}
    assert cfg["recording_names"]["1715000000"] == "T"


def test_settings_save_preserves_other_keys(env):
    from app.utils.config import save_config

    save_config({"device_bind_tokens": {"SN1": "tok"}, "deleted_ids": ["a"]})
    r = _post(env.client, "/settings", {"model": "base", "groq_api_key": " k "})
    assert r.get_json()["saved"] is True
    cfg = _cfg()
    assert cfg["whisper_model"] == "base"
    assert cfg["groq_api_key"] == "k"
    assert cfg["device_bind_tokens"] == {"SN1": "tok"}
    assert cfg["deleted_ids"] == ["a"]


# ---------------------------------------------------------------------------
# Re-transcribe merges instead of replacing
# ---------------------------------------------------------------------------

PRESERVED = {
    "summary": "Old summary",
    "auto_title": "Old title",
    "tags": ["Memo"],
    "custom_name": "My name",
    "notion_page_url": "https://notion.so/x",
    "something_new": {"k": 1},
}


def _seed_transcript(env, sid):
    data = {
        "text": "old text",
        "duration": 1,
        "language": "en",
        "segments": [{"start": 0, "end": 1, "text": "old text", "speaker": "A"}],
        "provider": "groq",
        "speakers": ["A"],
        **PRESERVED,
    }
    (env.trn / f"{sid}.json").write_text(json.dumps(data))
    (env.rec / f"{sid}.ogg").write_bytes(b"OggS" + bytes(200))


def test_retranscribe_cloud_preserves_summary_and_tags(env):
    from app.utils.config import save_config

    sid = "1715000000"
    _seed_transcript(env, sid)
    save_config({"deepgram_api_key": "k", "transcription_provider": "deepgram", "diarize": False})
    result = {
        "text": "brand new text",
        "segments": [{"start": 0, "end": 2, "text": "brand new text"}],
        "duration": 2,
        "language": "en",
    }
    with patch("app.routes.transcribe.transcribe_deepgram", return_value=result):
        # Twice: re-transcribing an existing transcript must just work.
        for _ in range(2):
            r = _post(env.client, "/transcribe", {"path": str(env.rec / f"{sid}.ogg")})
            assert r.status_code == 200, r.get_json()
            assert r.get_json()["text"] == "brand new text"

    t = json.loads((env.trn / f"{sid}.json").read_text())
    for k, v in PRESERVED.items():
        assert t[k] == v, k
    assert t["text"] == "brand new text"
    assert t["provider"] == "deepgram"
    assert t["duration"] == 2
    assert "speakers" not in t  # stale speaker list from the old run is dropped
    assert t["summary_stale"] is True
    assert (env.trn / f"{sid}.txt").read_text() == "brand new text"

    listing = env.client.get("/transcripts").get_json()
    assert listing[0]["summary_stale"] is True


def test_retranscribe_same_text_not_stale(env):
    from app.utils.config import save_config

    sid = "1715000001"
    _seed_transcript(env, sid)
    save_config({"groq_api_key": "k", "transcription_provider": "groq"})
    result = {"text": "old text", "segments": [], "duration": 1, "language": "en"}
    with patch("app.routes.transcribe.transcribe_groq", return_value=result):
        _post(env.client, "/transcribe", {"path": str(env.rec / f"{sid}.ogg")})
    t = json.loads((env.trn / f"{sid}.json").read_text())
    assert t["summary"] == "Old summary"
    assert "summary_stale" not in t


def test_retranscribe_local_preserves_summary(env):
    from app.utils.config import save_config
    from src.transcribe import Transcript, TranscriptSegment

    sid = "1715000002"
    _seed_transcript(env, sid)
    save_config({"transcription_provider": "local"})
    t_obj = Transcript(text="local text", language="en", duration=3)
    t_obj.segments.append(TranscriptSegment(start=0, end=3, text="local text", speaker=""))
    with patch("src.transcribe.transcribe", return_value=t_obj):
        r = _post(env.client, "/transcribe", {"path": str(env.rec / f"{sid}.ogg")})
    assert r.status_code == 200, r.get_json()
    t = json.loads((env.trn / f"{sid}.json").read_text())
    for k, v in PRESERVED.items():
        assert t[k] == v, k
    assert t["text"] == "local text"
    assert t["provider"] == "local"
    assert t["summary_stale"] is True


def test_transcript_update_summary_clears_stale(env):
    sid = "1715000003"
    (env.trn / f"{sid}.json").write_text(json.dumps({"text": "t", "summary": "s", "summary_stale": True}))
    _post(env.client, "/transcripts/update", {"session_id": sid, "summary": "edited"})
    t = json.loads((env.trn / f"{sid}.json").read_text())
    assert t["summary"] == "edited"
    assert "summary_stale" not in t


# ---------------------------------------------------------------------------
# Re-download keeps a backup
# ---------------------------------------------------------------------------


def test_redownload_moves_to_backup_and_restore(env):
    from app.utils import redownload_backup
    from app.utils.config import save_config

    sid = "1715000000"
    (env.rec / f"{sid}.ogg").write_bytes(b"OggS-original")
    (env.rec / f"{sid}_encrypted.bin").write_bytes(b"enc")
    (env.rec / f"{sid}.deleted").write_text("")
    save_config({"deleted_ids": [sid, "other"]})

    r = _post(env.client, "/recordings/redownload", {"session_ids": [sid]})
    body = r.get_json()
    assert body["cleared"] == [sid]
    assert sorted(body["backed_up"][sid]) == sorted([f"{sid}.ogg", f"{sid}_encrypted.bin"])
    assert not (env.rec / f"{sid}.ogg").exists()
    assert (env.backup / sid / f"{sid}.ogg").read_bytes() == b"OggS-original"
    assert (env.backup / sid / f"{sid}_encrypted.bin").read_bytes() == b"enc"
    assert not (env.rec / f"{sid}.deleted").exists()
    assert _cfg()["deleted_ids"] == ["other"]

    # Still listed, flagged as pending
    listing = env.client.get("/recordings").get_json()
    entry = next(x for x in listing if x["session_id"] == sid)
    assert entry["redownload_pending"] is True

    # Restore puts the files back where they were
    r = _post(env.client, "/recordings/restore-redownload", {"session_ids": [sid]})
    assert r.get_json()["count"] == 1
    assert (env.rec / f"{sid}.ogg").read_bytes() == b"OggS-original"
    assert (env.rec / f"{sid}_encrypted.bin").exists()
    assert not (env.backup / sid).exists()
    assert not redownload_backup.list_redownload_backups()


def test_finalize_redownload_backup(env):
    from app.utils import redownload_backup

    sid = "1715000000"
    (env.rec / f"{sid}.ogg").write_bytes(b"old")
    _post(env.client, "/recordings/redownload", {"session_ids": [sid]})
    # No fresh .ogg yet → finalize is a no-op and keeps the backup
    assert redownload_backup.finalize_redownload_backup(sid) is False
    assert (env.backup / sid / f"{sid}.ogg").exists()
    # Sync writes a fresh copy → finalize drops the backup
    (env.rec / f"{sid}.ogg").write_bytes(b"fresh")
    assert redownload_backup.finalize_redownload_backup(sid) is True
    assert not (env.backup / sid).exists()
    assert (env.rec / f"{sid}.ogg").read_bytes() == b"fresh"
    # Restore never overwrites a fresh download
    assert redownload_backup.restore_redownload_backup(sid) == []


def test_restore_does_not_overwrite_fresh_download(env):
    from app.utils import redownload_backup

    sid = "1715000000"
    (env.rec / f"{sid}.ogg").write_bytes(b"old")
    _post(env.client, "/recordings/redownload", {"session_ids": [sid]})
    (env.rec / f"{sid}.ogg").write_bytes(b"fresh")
    assert redownload_backup.restore_redownload_backup(sid) == []
    assert (env.rec / f"{sid}.ogg").read_bytes() == b"fresh"
    assert (env.backup / sid / f"{sid}.ogg").read_bytes() == b"old"


# ---------------------------------------------------------------------------
# Delete → Trash
# ---------------------------------------------------------------------------


def test_delete_recordings_moves_to_trash(env):
    sid = "1715000000"
    (env.rec / f"{sid}.ogg").write_bytes(b"audio")
    (env.trn / f"{sid}.json").write_text("{}")
    (env.trn / f"{sid}.txt").write_text("t")
    from app.utils.config import save_config

    save_config({"recording_tags": {sid: ["X"]}, "recording_names": {sid: "N"}})

    r = _post(env.client, "/recordings/delete", {"session_ids": [sid]})
    body = r.get_json()
    assert r.status_code == 200
    assert body["trashed"] is True
    assert body["trash_location"] == "system"
    assert body["count"] == 1
    assert len(body["deleted"]) == 3
    assert {p.name for p in env.system_trash} == {f"{sid}.ogg", f"{sid}.json", f"{sid}.txt"}
    assert all(p.exists() for p in env.system_trash)  # moved, not unlinked
    cfg = _cfg()
    assert sid in cfg["deleted_ids"]
    assert sid not in cfg["recording_tags"]
    assert sid not in cfg["recording_names"]
    assert (env.rec / f"{sid}.deleted").exists()


def test_delete_transcript_moves_to_trash(env):
    sid = "1715000000"
    (env.trn / f"{sid}.json").write_text("{}")
    r = _post(env.client, "/transcripts/delete", {"session_id": sid})
    body = r.get_json()
    assert body["trashed"] is True
    assert body["deleted"] == [str(env.trn / f"{sid}.json")]
    assert [p.name for p in env.system_trash] == [f"{sid}.json"]


def test_delete_nothing_found_shape(env):
    r = _post(env.client, "/recordings/delete", {"session_ids": ["fake_id_999"]})
    body = r.get_json()
    assert body["deleted"] == []
    assert body["trashed"] is True
    assert body["trash_location"] is None


def _fake_foundation(ok, calls):
    """A stand-in Foundation module whose trash call either moves or refuses."""

    class FakeURL:
        def __init__(self, p):
            self.p = p

        @classmethod
        def fileURLWithPath_(cls, p):
            return cls(p)

    class FakeManager:
        def trashItemAtURL_resultingItemURL_error_(self, url, _r, _e):
            calls.append(url.p)
            if ok:
                import os

                os.unlink(url.p)  # pretend it went to ~/.Trash
                return (True, None, None)
            return (False, None, "denied")

    class FakeNSFileManager:
        @staticmethod
        def defaultManager():
            return FakeManager()

    mod = types.ModuleType("Foundation")
    mod.NSURL = FakeURL
    mod.NSFileManager = FakeNSFileManager
    return mod


def test_move_to_trash_uses_foundation(tmp_path):
    from app.utils import trash

    f = tmp_path / "a.ogg"
    f.write_text("x")
    calls = []
    with patch.dict(sys.modules, {"Foundation": _fake_foundation(True, calls)}):
        assert trash.move_to_trash(f) == "system"
    assert calls == [str(f)]


def test_move_to_trash_falls_back_to_app_trash(tmp_path):
    from app.utils import trash

    f = tmp_path / "a.ogg"
    f.write_text("x")
    calls = []
    app_trash = tmp_path / ".trash"
    with (
        patch.dict(sys.modules, {"Foundation": _fake_foundation(False, calls)}),
        patch("app.utils.trash.APP_TRASH_DIR", app_trash),
    ):
        assert trash.move_to_trash(f) == "app"
        # A second file with the same name doesn't overwrite the first
        f.write_text("y")
        assert trash.move_to_trash(f) == "app"
    assert not f.exists()
    contents = sorted(p.read_text() for p in app_trash.iterdir())
    assert contents == ["x", "y"]


def test_move_to_trash_without_foundation(tmp_path):
    from app.utils import trash

    f = tmp_path / "a.ogg"
    f.write_text("x")
    with (
        patch.dict(sys.modules, {"Foundation": None}),  # import raises ImportError
        patch("app.utils.trash.APP_TRASH_DIR", tmp_path / ".trash"),
    ):
        assert trash.move_to_trash(f) == "app"
    assert (tmp_path / ".trash" / "a.ogg").read_text() == "x"


# ---------------------------------------------------------------------------
# Crop backups are not recordings
# ---------------------------------------------------------------------------


def test_listing_excludes_crop_backups(env):
    for name in ("1715000000.ogg", "1715000000_uncropped.ogg", "1715000000_crop_tmp.ogg"):
        (env.rec / name).write_bytes(b"OggS")
    ids = [r["session_id"] for r in env.client.get("/recordings").get_json()]
    assert ids == ["1715000000"]


def test_cloud_upload_skips_crop_backups(tmp_path, tmp_config):
    from app.state import cloud

    rec = tmp_path / "rec"
    rec.mkdir()
    for name in ("1715000000.ogg", "1715000000_uncropped.ogg", "1715000000_crop_tmp.ogg"):
        (rec / name).write_bytes(b"OggS" + bytes(100))
    uploaded = []

    def fake_upload(path, name, duration, start_time_ms=None):
        uploaded.append(path)
        return {"data": {"id": "cid"}}

    with (
        patch.dict(cloud._cloud_state, {"token": "t"}),
        patch("app.state.cloud.cloud_api", return_value={"data_file_list": []}),
        patch("app.state.cloud.upload_to_cloud", side_effect=fake_upload),
        patch("app.state.cloud.get_all_recordings_dirs", return_value=[rec]),
        patch("app.state.cloud.TRANSCRIPTS_DIR", tmp_path / "trn"),
    ):
        cloud.upload_local_recordings_to_cloud()
    assert [p.split("/")[-1] for p in uploaded] == ["1715000000.ogg"]
    from app.utils.config import load_config

    cfg = load_config()
    assert cfg["cloud_file_ids"] == {"1715000000": "cid"}
    assert cfg["uploaded_session_ids"] == ["1715000000"]
