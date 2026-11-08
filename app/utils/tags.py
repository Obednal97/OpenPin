"""Recording tags: add and remove single tags, with rules.

A recording's tags live in two places: the config's ``recording_tags[sid]`` (what
the app reads) and the transcript JSON's ``tags`` (so they survive a config
reset). The tag picker's list is the config's ``custom_tags`` on top of
``DEFAULT_TAGS``.

The app and AI tools (through the MCP connector) change tags one at a time, never
by saving a whole list, so a window showing stale tags can't wipe a tag that
something else just added. The same rules apply to both: trimmed, 1 to 40
characters, no control characters, matched case-insensitively against existing
tags (an existing tag's spelling wins), at most 20 tags per recording and per call.
"""

import json
import logging
import unicodedata

from app.utils import changes
from app.utils.config import load_config, update_config
from app.utils.constants import DEFAULT_TAGS, TRANSCRIPTS_DIR
from app.utils.fileio import atomic_write
from app.utils.paths import InvalidSessionIdError, validate_session_id
from app.utils.transcript_locks import get_transcript_lock

MAX_TAG_LENGTH = 40
MAX_TAGS_PER_RECORDING = 20
MAX_TAGS_PER_CALL = 20


def _has_control_char(text: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in text)


def normalise_tag(tag, existing) -> str:
    """Trim and validate ``tag``; return an existing tag's spelling on a
    case-insensitive match. Raises ``ValueError`` with a plain message."""
    if not isinstance(tag, str):
        raise ValueError("Tags must be text")
    tag = tag.strip()
    if not tag:
        raise ValueError("A tag can't be empty")
    if len(tag) > MAX_TAG_LENGTH:
        raise ValueError(f"A tag can be at most {MAX_TAG_LENGTH} characters")
    if _has_control_char(tag):
        raise ValueError("A tag can't contain control characters")
    folded = tag.casefold()
    for known in existing:
        if isinstance(known, str) and known.casefold() == folded:
            return known
    return tag


def _check_call(session_id, tags):
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as e:
        raise ValueError(str(e) or "Invalid session_id") from e
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    if len(tags) > MAX_TAGS_PER_CALL:
        raise ValueError(f"At most {MAX_TAGS_PER_CALL} tags per call")


def _current(config, session_id) -> list[str]:
    current = config.get("recording_tags", {}).get(session_id) or []
    return [t for t in current if isinstance(t, str)]


def _write_transcript_tags(session_id, tags):
    """Mirror the recording's tags into its transcript JSON, if it has one.
    The caller holds the transcript lock."""
    path = TRANSCRIPTS_DIR / f"{session_id}.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("tags") == tags:
            return
        data["tags"] = tags
        atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        logging.warning(f"Could not save tags in transcript {session_id}: {e}")


def enqueue_notion_sync(session_id) -> bool:
    """Queue a Notion property sync if the recording has a linked page.

    The background worker owns the actual call, so callers return at once; if
    Notion is offline or the token expired, the queue retries with backoff.
    """
    try:
        from app.routes.notion import _get_notion_page_url
        from app.state import notion as _notion
        from app.utils import notion_queue

        if _notion.is_connected() and _get_notion_page_url(session_id):
            notion_queue.enqueue(session_id)
            return True
    except Exception as e:
        logging.warning(f"Notion tag enqueue failed: {e}")
    return False


def _change(session_id, compute):
    """Apply ``compute(current, config) -> new`` to the recording's tags.

    The transcript lock is held throughout (lock order: transcript, then config,
    as elsewhere), so the transcript JSON always ends up matching the config.
    """
    result = {}

    def _apply(config):
        current = _current(config, session_id)
        new = compute(current, config)
        result["changed"] = new != current
        result["tags"] = new
        if result["changed"]:
            config.setdefault("recording_tags", {})[session_id] = new

    with get_transcript_lock(session_id):
        update_config(_apply)
        if result["changed"]:
            _write_transcript_tags(session_id, result["tags"])

    if result["changed"]:
        changes.bump()
        enqueue_notion_sync(session_id)
    return list(result["tags"])


def add_tags(session_id: str, new: list[str]) -> list[str]:
    """Add ``new`` to the recording's tags; return its tags.

    Tags the picker doesn't know yet join ``custom_tags``. Adding a tag the
    recording already has (in any case) is a no-op. Raises ``ValueError`` and
    changes nothing if any tag breaks the rules or the recording would end up
    with more than 20 tags.
    """
    _check_call(session_id, new)
    for tag in new:
        normalise_tag(tag, [])  # reject bad input before taking any lock

    def _compute(current, config):
        custom = [t for t in config.get("custom_tags", []) if isinstance(t, str)]
        known = current + DEFAULT_TAGS + custom
        folded = {t.casefold() for t in current}
        added = []
        for raw in new:
            tag = normalise_tag(raw, known)
            if tag.casefold() in folded:
                continue
            added.append(tag)
            folded.add(tag.casefold())
            known.append(tag)
        if added and len(current) + len(added) > MAX_TAGS_PER_RECORDING:
            raise ValueError(f"A recording can have at most {MAX_TAGS_PER_RECORDING} tags")
        picker = {t.casefold() for t in DEFAULT_TAGS + custom}
        new_to_picker = [t for t in added if t.casefold() not in picker]
        if new_to_picker:
            config["custom_tags"] = custom + new_to_picker
        return current + added

    return _change(session_id, _compute)


def remove_tags(session_id: str, gone: list[str]) -> list[str]:
    """Remove ``gone`` from the recording's tags (case-insensitively); return its
    tags. A tag the recording doesn't have is a no-op. The picker list is left
    alone. Tags from before the rules (say, longer than 40 characters) can still
    be removed."""
    _check_call(session_id, gone)
    folded_gone = set()
    for tag in gone:
        if not isinstance(tag, str) or not tag.strip():
            raise ValueError("Tags must be non-empty text")
        folded_gone.add(tag.strip().casefold())

    return _change(session_id, lambda current, _config: [t for t in current if t.casefold() not in folded_gone])


def all_tags() -> list[dict]:
    """Every tag (the picker's defaults, ``custom_tags`` and tags in use) with
    how many recordings carry it, sorted by name."""
    config = load_config()
    deleted = set(config.get("deleted_ids", []))
    spelling: dict[str, str] = {}
    counts: dict[str, int] = {}

    def _see(tag):
        if not isinstance(tag, str) or not tag.strip():
            return None
        key = tag.casefold()
        spelling.setdefault(key, tag)
        counts.setdefault(key, 0)
        return key

    for tag in DEFAULT_TAGS + list(config.get("custom_tags", [])):
        _see(tag)
    for sid, rec_tags in (config.get("recording_tags") or {}).items():
        if sid in deleted or not isinstance(rec_tags, list):
            continue
        for key in {k for k in map(_see, rec_tags) if k}:
            counts[key] += 1
    return [{"tag": spelling[k], "recordings": counts[k]} for k in sorted(spelling, key=lambda k: (k, spelling[k]))]
