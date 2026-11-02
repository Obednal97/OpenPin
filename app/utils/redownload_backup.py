"""Keep the local copy of a recording safe while it is being re-downloaded.

/recordings/redownload used to delete ``{sid}.ogg`` and ``{sid}_encrypted.bin``
straight away; if the device no longer had the file, the recording was gone.
Now those files are moved to ``DATA_DIR/.redownload_backup/<sid>/`` and a
``manifest.json`` records the directory each came from.

Lifecycle:

- :func:`backup_for_redownload` — called by /recordings/redownload.
- :func:`finalize_redownload_backup` — call after a sync has written a fresh
  ``{sid}.ogg`` (device.py BLE sync and the Wi-Fi sync result handler). Drops
  the backup.
- :func:`restore_redownload_backup` — puts the files back (POST
  /recordings/restore-redownload, or when the device no longer has the file).
"""

import json
import logging
import shutil
from pathlib import Path

from app.utils.constants import DATA_DIR, get_recordings_dir
from app.utils.paths import validate_session_id

REDOWNLOAD_BACKUP_DIR = DATA_DIR / ".redownload_backup"
_MANIFEST = "manifest.json"


def backup_dir_for(sid) -> Path:
    return REDOWNLOAD_BACKUP_DIR / validate_session_id(str(sid))


def has_redownload_backup(sid) -> bool:
    d = backup_dir_for(sid)
    return d.is_dir() and any(p.name != _MANIFEST for p in d.iterdir())


def list_redownload_backups() -> list:
    """Session ids that currently have a backup."""
    if not REDOWNLOAD_BACKUP_DIR.is_dir():
        return []
    out = []
    for d in sorted(REDOWNLOAD_BACKUP_DIR.iterdir()):
        try:
            if d.is_dir() and has_redownload_backup(d.name):
                out.append(d.name)
        except ValueError:
            continue
    return out


def backup_ogg_path(sid):
    """Path of the backed-up .ogg, or None."""
    p = backup_dir_for(sid) / f"{validate_session_id(str(sid))}.ogg"
    return p if p.exists() else None


def _read_manifest(d: Path) -> dict:
    try:
        data = json.loads((d / _MANIFEST).read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def backup_for_redownload(sid, files) -> list:
    """Move ``files`` (paths of this recording) into the backup folder.

    If the backup already holds a file of the same name it is replaced by the
    newer one. Returns the list of backed-up file names.
    """
    d = backup_dir_for(sid)
    d.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest(d)
    moved = []
    for src in files:
        src = Path(src)
        if not src.exists():
            continue
        dest = d / src.name
        if dest.exists():
            dest.unlink()
        shutil.move(str(src), str(dest))
        manifest[src.name] = str(src.parent)
        moved.append(src.name)
    (d / _MANIFEST).write_text(json.dumps(manifest, indent=2))
    return moved


def finalize_redownload_backup(sid) -> bool:
    """Drop the backup once a fresh ``{sid}.ogg`` exists. Returns True if removed.

    Does nothing (returns False) if no backup exists, or if the fresh .ogg is
    missing — so it is safe to call after every successful download.
    """
    sid = str(sid)
    d = backup_dir_for(sid)
    if not d.is_dir():
        return False
    from app.utils.constants import get_all_recordings_dirs

    if not any((rd / f"{sid}.ogg").exists() for rd in get_all_recordings_dirs()):
        return False
    shutil.rmtree(d, ignore_errors=True)
    logging.info("[Redownload] Re-download of %s complete; backup removed", sid)
    return True


def restore_redownload_backup(sid) -> list:
    """Move backed-up files back to where they came from. Returns restored paths.

    A file is never restored over an existing one (a fresh download wins);
    such a backup copy is left in place.
    """
    sid = str(sid)
    d = backup_dir_for(sid)
    if not d.is_dir():
        return []
    manifest = _read_manifest(d)
    restored = []
    for f in sorted(d.iterdir()):
        if f.name == _MANIFEST:
            continue
        target_dir = Path(manifest.get(f.name, "")) if manifest.get(f.name) else get_recordings_dir()
        if not target_dir.is_absolute() or not target_dir.is_dir():
            target_dir = get_recordings_dir()
        dest = target_dir / f.name
        if dest.exists():
            continue
        shutil.move(str(f), str(dest))
        restored.append(str(dest))
    if not any(p.name != _MANIFEST for p in d.iterdir()):
        shutil.rmtree(d, ignore_errors=True)
    return restored
