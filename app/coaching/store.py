"""Coaching sidecar store: DATA_DIR/coaching/{session_id}.json and {session_id}.words.json.

The sidecar holds the owner, metrics and review for one recording; the verbatim
words (with fillers and per-word timing) live in the separate words file, so
loading or returning a sidecar never carries them. Both are kept apart from the
transcript JSON so the transcript stays free of filler tokens. Sidecars written
before the split may still hold ``words`` inline: ``load_words`` falls back to
them and the routes strip them.

Writes are atomic (temp file + os.replace) and serialised per session under a
lock of their own, one lock for both files. That lock is deliberately NOT the
transcript lock: the transcript lock is non-reentrant and ``_merge_transcript``
already holds it, so callers there must call ``mark_stale`` after releasing it.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path

from app.utils.constants import COACHING_DIR
from app.utils.fileio import atomic_write
from app.utils.transcript_locks import get_transcript_lock

COACHING_SCHEMA_VERSION = 1


def text_hash(text: str | None) -> str:
    """sha1 hex of a transcript's text: what ``transcript_text_hash`` records."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


WORDS_SUFFIX = ".words"
VOICE_PROFILE_STEM = "voice_profile"


def coaching_path(session_id: str) -> Path:
    return COACHING_DIR / f"{session_id}.json"


def words_path(session_id: str) -> Path:
    return COACHING_DIR / f"{session_id}{WORDS_SUFFIX}.json"


def session_files(session_id: str) -> list[Path]:
    """Every file the store keeps for one recording (sidecar and words)."""
    return [coaching_path(session_id), words_path(session_id)]


def is_sidecar_stem(stem: str) -> bool:
    """False for the store's other files in COACHING_DIR (words files, the voice profile)."""
    return stem != VOICE_PROFILE_STEM and not stem.endswith(WORDS_SUFFIX)


def _lock(session_id: str):
    return get_transcript_lock(f"coaching:{session_id}")


def session_lock(session_id: str):
    """The per-recording store lock, for callers that move the store's files (deletion).

    Never take it while holding a transcript lock (see the module docstring)."""
    return _lock(session_id)


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
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def load(session_id: str) -> dict:
    """Return the sidecar, or {} if it is missing or unreadable."""
    return _read(coaching_path(session_id))


def update(session_id: str, mutate: Callable[[dict], bool | None]) -> dict:
    """Read-modify-write the sidecar atomically; returns the saved dict.

    ``mutate`` edits the dict in place. ``version`` is set after it runs. If
    ``mutate`` returns False nothing is written and the sidecar as read is returned.
    """
    path = coaching_path(session_id)
    with _lock(session_id):
        data = _read(path)
        if mutate(data) is False:
            return _read(path)
        data["version"] = COACHING_SCHEMA_VERSION
        _write_atomic(path, data)
        return data


def load_words(session_id: str) -> list | None:
    """The verbatim words: the words file, else (older sidecars) the sidecar's inline ``words``.

    None when neither has a list.
    """
    data = _read(words_path(session_id))
    if "words" not in data:
        data = load(session_id)
    words = data.get("words")
    return words if isinstance(words, list) else None


def save_words(session_id: str, words: list, transcript_text_hash: str) -> None:
    """Replace the verbatim words after a transcription.

    Writes the words file and records ``transcript_text_hash`` in the sidecar, under
    the one store lock; any inline ``words`` left by an older sidecar are dropped.
    """
    with _lock(session_id):
        _write_atomic(words_path(session_id), {"version": COACHING_SCHEMA_VERSION, "words": list(words or [])})
        path = coaching_path(session_id)
        data = _read(path)
        data.pop("words", None)
        data["transcript_text_hash"] = transcript_text_hash
        data["version"] = COACHING_SCHEMA_VERSION
        _write_atomic(path, data)


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
