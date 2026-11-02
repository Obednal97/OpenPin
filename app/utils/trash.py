"""Move files to the macOS Trash instead of deleting them.

Uses ``NSFileManager.trashItemAtURL:resultingItemURL:error:`` (pyobjc's
Foundation, which bleak already pulls in on macOS). If Foundation is missing
or the call fails, the file is moved into ``DATA_DIR/.trash/`` so it can
still be recovered by hand.
"""

import logging
import shutil
import time
from pathlib import Path

from app.utils.constants import DATA_DIR

APP_TRASH_DIR = DATA_DIR / ".trash"


def _system_trash(path: Path) -> bool:
    """Try the macOS Trash. Returns True on success."""
    try:
        from Foundation import NSURL, NSFileManager
    except Exception:
        return False
    try:
        url = NSURL.fileURLWithPath_(str(path))
        ok, _resulting_url, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(url, None, None)
        if ok:
            return True
        logging.warning("[Trash] macOS Trash refused %s: %s", path.name, err)
    except Exception as e:
        logging.warning("[Trash] macOS Trash failed for %s: %s", path.name, e)
    return False


def _app_trash(path: Path) -> Path:
    """Move ``path`` into DATA_DIR/.trash, never overwriting an earlier copy."""
    APP_TRASH_DIR.mkdir(parents=True, exist_ok=True)
    dest = APP_TRASH_DIR / path.name
    if dest.exists():
        dest = APP_TRASH_DIR / f"{path.stem}.{int(time.time() * 1000)}{path.suffix}"
    shutil.move(str(path), str(dest))
    return dest


def move_to_trash(path) -> str:
    """Move ``path`` to the Trash.

    Returns ``"system"`` (macOS Trash) or ``"app"`` (DATA_DIR/.trash).
    Raises ``OSError`` if neither worked; the file is then left in place.
    """
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        raise FileNotFoundError(str(path))
    if _system_trash(path):
        return "system"
    _app_trash(path)
    return "app"
