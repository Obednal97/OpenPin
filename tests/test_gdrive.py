"""Google Drive sync tracking: str/int session ids, crop backups skipped,
locked config updates. No live Google calls — upload_file is mocked."""

from unittest.mock import patch

import pytest

SID = "1715000000"


@pytest.fixture
def drive(tmp_path, tmp_config):
    """Connected Drive with one recordings dir in tmp_path."""
    rec = tmp_path / "recordings"
    rec.mkdir()
    uploads = []

    def fake_upload(path):
        uploads.append(path.split("/")[-1])
        return f"drive-{len(uploads)}"

    with (
        patch("app.state.gdrive.is_connected", return_value=True),
        patch("app.state.gdrive.get_folder_id", return_value="folder"),
        patch("app.state.gdrive.upload_file", side_effect=fake_upload),
        patch("app.utils.constants.get_all_recordings_dirs", return_value=[rec]),
    ):
        yield rec, uploads


def _cfg():
    from app.utils.config import load_config

    return load_config()


def _touch(rec, *names):
    for n in names:
        (rec / n).write_bytes(b"OggS" + bytes(16))


# ---------------------------------------------------------------------------
# is_synced / mark_synced: keys normalised to str
# ---------------------------------------------------------------------------


def test_is_synced_accepts_int_and_str(tmp_config):
    from app.state import gdrive
    from app.utils.config import save_config

    # JSON keys always come back as strings
    save_config({"gdrive_synced_files": {SID: {"drive_file_id": "d"}}})
    assert gdrive.is_synced(SID) is True
    assert gdrive.is_synced(int(SID)) is True
    assert gdrive.is_synced(1) is False


def test_mark_synced_int_id_stores_str_key(tmp_config):
    from app.state import gdrive

    gdrive.mark_synced(int(SID), "drive-x", 123)
    synced = _cfg()["gdrive_synced_files"]
    assert list(synced) == [SID]
    assert synced[SID]["drive_file_id"] == "drive-x"
    assert synced[SID]["file_size"] == 123
    assert gdrive.is_synced(int(SID)) and gdrive.is_synced(SID)
    # Marking again via the other type updates the same entry, no duplicate.
    gdrive.mark_synced(SID, "drive-y")
    assert list(_cfg()["gdrive_synced_files"]) == [SID]


def test_mark_synced_uses_update_config(tmp_config):
    from app.state import gdrive
    from app.utils import config as config_mod
    from app.utils.config import save_config

    save_config({"deleted_ids": ["keep"], "gdrive_synced_files": {"other": {"drive_file_id": "o"}}})
    with patch("app.state.gdrive.update_config", wraps=config_mod.update_config) as uc:
        gdrive.mark_synced(SID, "d")
    uc.assert_called_once()
    cfg = _cfg()
    assert cfg["deleted_ids"] == ["keep"]
    assert set(cfg["gdrive_synced_files"]) == {"other", SID}


def test_maybe_auto_upload_cloud_mode_int_id_not_reuploaded(drive):
    from app.state import gdrive
    from app.utils.config import save_config

    rec, uploads = drive
    _touch(rec, f"{SID}.ogg")
    save_config({"storage_mode": "cloud", "gdrive_synced_files": {SID: {"drive_file_id": "d"}}})
    gdrive.maybe_auto_upload(int(SID), rec / f"{SID}.ogg")
    assert uploads == []
    assert (rec / f"{SID}.ogg").exists()


# ---------------------------------------------------------------------------
# Crop backups / temp files are not recordings
# ---------------------------------------------------------------------------


def test_sync_all_skips_crop_backups(drive):
    from app.state import gdrive

    rec, uploads = drive
    _touch(rec, f"{SID}.ogg", f"{SID}_uncropped.ogg", f"{SID}_crop_tmp.ogg")
    result = gdrive.sync_all()
    assert uploads == [f"{SID}.ogg"]
    assert result == {"uploaded": 1, "errors": [], "total": 1}
    assert list(_cfg()["gdrive_synced_files"]) == [SID]


def test_sync_all_preserves_config_written_during_upload(drive):
    from app.state import gdrive
    from app.utils.config import save_config, update_config

    rec, _ = drive
    _touch(rec, "1.ogg", "2.ogg")
    save_config({"deleted_ids": []})

    def slow_upload(path):
        # Another request writes to the config while the upload is in flight.
        update_config(lambda c: c["deleted_ids"].append(path.split("/")[-1]))
        return "d"

    with patch("app.state.gdrive.upload_file", side_effect=slow_upload):
        gdrive.sync_all()
    cfg = _cfg()
    assert sorted(cfg["deleted_ids"]) == ["1.ogg", "2.ogg"]
    assert set(cfg["gdrive_synced_files"]) == {"1", "2"}


def test_sync_all_skips_already_synced_and_dismissed(drive):
    from app.state import gdrive
    from app.utils.config import save_config

    rec, uploads = drive
    _touch(rec, "1.ogg", "2.ogg", "3.ogg")
    save_config({"deleted_ids": [2], "gdrive_synced_files": {"1": {"drive_file_id": "d"}}})
    gdrive.sync_all()
    assert uploads == ["3.ogg"]


def test_sync_status_ignores_crop_backups(drive):
    from app.state import gdrive
    from app.utils.config import save_config

    rec, _ = drive
    _touch(rec, f"{SID}.ogg", f"{SID}_uncropped.ogg", f"{SID}_crop_tmp.ogg", "1700000000.ogg")
    save_config({"gdrive_synced_files": {SID: {"drive_file_id": "d"}}})
    assert gdrive.get_sync_status() == {"total": 2, "synced": 1, "unsynced": 1}


def test_cloud_only_recordings_skip_crop_backups(drive):
    from app.state import gdrive
    from app.utils.config import save_config

    rec, _ = drive
    _touch(rec, "1700000000.ogg")
    save_config(
        {
            "deleted_ids": ["1600000000"],
            "gdrive_synced_files": {
                SID: {"drive_file_id": "d1", "synced_at": "t", "file_size": 5},
                f"{SID}_uncropped": {"drive_file_id": "d2"},
                f"{SID}_crop_tmp": {"drive_file_id": "d3"},
                "1700000000": {"drive_file_id": "d4"},  # still local
                "1600000000": {"drive_file_id": "d5"},  # dismissed
            },
        }
    )
    assert gdrive.get_cloud_only_recordings() == [
        {"session_id": SID, "drive_file_id": "d1", "synced_at": "t", "file_size": 5}
    ]
