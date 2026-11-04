"""Coaching sidecar store: DATA_DIR/coaching/{session_id}.json.

Holds the verbatim words, owner, metrics and review for one recording, kept
apart from the transcript JSON so the transcript stays free of filler tokens.
Writes are atomic (temp file + os.replace) and serialised per session under a
lock of their own. That lock is deliberately NOT the transcript lock: the
transcript lock is non-reentrant and ``_merge_transcript`` already holds it,
so callers there must call ``mark_stale`` after releasing it.
"""

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

from app.utils.constants import COACHING_DIR
from app.utils.transcript_locks import get_transcript_lock

COACHING_SCHEMA_VERSION = 1


def coaching_path(session_id: str) -> Path:
    return COACHING_DIR / f"{session_id}.json"


def _lock(session_id: str):
    return get_transcript_lock(f"coaching:{session_id}")


def _read(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logging.warning("Coaching sidecar %s unreadable: %s", path.name, e)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load(session_id: str) -> dict:
    """Return the sidecar, or {} if it is missing or unreadable."""
    return _read(coaching_path(session_id))


def update(session_id: str, mutate: Callable[[dict], None]) -> dict:
    """Read-modify-write the sidecar atomically; returns the saved dict.

    ``mutate`` edits the dict in place. ``version`` is set after it runs.
    """
    path = coaching_path(session_id)
    with _lock(session_id):
        data = _read(path)
        mutate(data)
        data["version"] = COACHING_SCHEMA_VERSION
        _write_atomic(path, data)
        return data


def mark_stale(session_id: str) -> None:
    """Flag the report (a review, or status "done") as out of date, if one exists. Never creates a sidecar."""
    path = coaching_path(session_id)
    if not path.exists():
        return
    with _lock(session_id):
        data = _read(path)
        if not (data.get("review") or data.get("status") == "done"):
            return
        if data.get("report_stale"):
            return
        data["report_stale"] = True
        _write_atomic(path, data)
