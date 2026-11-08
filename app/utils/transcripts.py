"""Read and update a transcript JSON (TRANSCRIPTS_DIR/<session_id>.json) under its lock.

Both take the lock, so never call them while holding get_transcript_lock for the
same recording (it isn't re-entrant).
"""

import json
from pathlib import Path

from app.utils.fileio import atomic_write
from app.utils.transcript_locks import get_transcript_lock


def load(path) -> dict | None:
    """The transcript, or None when it is missing, unreadable or not an object."""
    path = Path(path)
    try:
        with get_transcript_lock(path.stem):
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def update(path, mutate) -> bool:
    """Apply ``mutate(data)`` in place and write the transcript back atomically.

    Nothing is written when the file is missing or not an object, or when ``mutate``
    returns False. A file that isn't JSON raises. Returns True if it was written.
    """
    path = Path(path)
    with get_transcript_lock(path.stem):
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or mutate(data) is False:
            return False
        atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))
        return True
