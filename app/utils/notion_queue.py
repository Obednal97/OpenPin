"""Persistent, retrying background queue for Notion tag syncs.

The UI treats tag edits as instant — the renderer updates locally and fires
/recordings/tag, which saves to config and enqueues a sync here. This module
owns the actual Notion call, so the user never waits on the network.

Design:
- Keyed by session_id (last-write-wins — rapid add/remove collapses to the
  final desired state).
- Persisted to disk on every state change so the queue survives restarts.
- Single worker thread with exponential backoff on transient failures.
- Global pause flag when Notion returns 401 (token expired); cleared when
  the token is refreshed or the user reconnects.
- On permanent validation errors we drop the entry rather than loop forever.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.utils.constants import NOTION_QUEUE_PATH

log = logging.getLogger(__name__)

# Exponential backoff schedule in seconds, indexed by attempt count.
# After the last entry we stay at 1h.
_BACKOFF_SCHEDULE = [2, 5, 15, 45, 180, 900, 3600]

# Errors that indicate "don't retry — the request was bad". Everything else
# is treated as transient (network blip, 5xx, rate limit) and retried.
_PERMANENT_ERROR_MARKERS = (
    "not connected",
    "no database selected",
    "validation_error",
    "could not parse",
    "invalid session_id",
)


class _State:
    """Thread-safe wrapper around the on-disk queue file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._wake = threading.Event()
        # entries: {session_id: {"attempts": int, "next_retry_at": float, "last_error": str|None, "enqueued_at": str}}
        self.entries: dict[str, dict] = {}
        self.paused: bool = False
        self.pause_reason: str | None = None
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except Exception as e:
            log.warning(f"[notion-queue] could not read {self.path}: {e}")
            return
        self.entries = {k: v for k, v in (data.get("entries") or {}).items() if isinstance(v, dict)}
        self.paused = bool(data.get("paused"))
        self.pause_reason = data.get("pause_reason")

    def _save_locked(self) -> None:
        """Write under self._lock. Atomic via tmp + rename."""
        payload = {
            "entries": self.entries,
            "paused": self.paused,
            "pause_reason": self.pause_reason,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(prefix=".notion_queue.", dir=str(self.path.parent))
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, self.path)
        except Exception as e:
            log.warning(f"[notion-queue] could not write {self.path}: {e}")

    # ---- public API ----

    def enqueue(self, session_id: str) -> None:
        """Register that `session_id` needs to be pushed to Notion.

        If an entry already exists we keep it but reset the backoff so the new
        desired state is tried immediately.
        """
        if not session_id:
            return
        with self._lock:
            self.entries[session_id] = {
                "session_id": session_id,
                "enqueued_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 (requires-python >=3.10)
                "attempts": 0,
                "next_retry_at": time.time(),
                "last_error": None,
            }
            self._save_locked()
        self._wake.set()

    def pop_due(self) -> dict | None:
        """Return one due entry (and remove it from the queue) or None.

        The caller is responsible for re-enqueueing via record_failure on
        transient errors.
        """
        now = time.time()
        with self._lock:
            if self.paused:
                return None
            for sid, entry in list(self.entries.items()):
                if entry.get("next_retry_at", 0) <= now:
                    del self.entries[sid]
                    self._save_locked()
                    return entry
        return None

    def record_failure(self, entry: dict, error: str, permanent: bool) -> None:
        sid = entry.get("session_id")
        if not sid:
            return
        if permanent:
            log.warning(f"[notion-queue] dropping {sid} — permanent error: {error}")
            return
        attempts = int(entry.get("attempts", 0)) + 1
        delay = _BACKOFF_SCHEDULE[min(attempts - 1, len(_BACKOFF_SCHEDULE) - 1)]
        with self._lock:
            # Don't overwrite a newer enqueue that happened while we were working.
            existing = self.entries.get(sid)
            if existing and existing.get("attempts", 0) == 0 and existing.get("next_retry_at", 0) <= time.time():
                # A newer enqueue landed — leave it alone.
                return
            self.entries[sid] = {
                "session_id": sid,
                "enqueued_at": entry.get("enqueued_at") or datetime.now(timezone.utc).isoformat(),  # noqa: UP017 (requires-python >=3.10)
                "attempts": attempts,
                "next_retry_at": time.time() + delay,
                "last_error": error,
            }
            self._save_locked()

    def pause(self, reason: str) -> None:
        with self._lock:
            self.paused = True
            self.pause_reason = reason
            self._save_locked()

    def resume(self) -> None:
        with self._lock:
            if not self.paused:
                return
            self.paused = False
            self.pause_reason = None
            # Reset all backoffs so the unpause retries everything immediately.
            now = time.time()
            for entry in self.entries.values():
                entry["next_retry_at"] = now
            self._save_locked()
        self._wake.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "pending": len(self.entries),
                "paused": self.paused,
                "pause_reason": self.pause_reason,
                "last_error": self._last_error_locked(),
            }

    def _last_error_locked(self) -> str | None:
        latest = None
        for entry in self.entries.values():
            err = entry.get("last_error")
            if err:
                latest = err
        return latest


_state = _State(NOTION_QUEUE_PATH)
_worker_started = False


def enqueue(session_id: str) -> None:
    _state.enqueue(session_id)


def snapshot() -> dict:
    return _state.snapshot()


def resume() -> None:
    _state.resume()


def _is_permanent_error(msg: str) -> bool:
    lower = (msg or "").lower()
    return any(marker in lower for marker in _PERMANENT_ERROR_MARKERS)


# "401" as a standalone status code, not a digit run inside a UUID, page id,
# timestamp or date that an MCP error message happens to quote (hyphens count
# as part of the token, so "…-401-…" in an id doesn't match either).
_STATUS_401_RE = re.compile(r"(?<![\w-])401(?![\w-])")
_AUTH_ERROR_MARKERS = ("unauthorized", "invalid_token", "session expired")


def _is_auth_error(err) -> bool:
    """True if ``err`` (an exception or message) means the Notion token is no good.

    Used to pause the whole queue, so it must not fire on unrelated errors.
    """
    response = getattr(err, "response", None)
    if getattr(response, "status_code", None) == 401:
        return True
    lower = str(err or "").lower()
    if any(marker in lower for marker in _AUTH_ERROR_MARKERS):
        return True
    return bool(_STATUS_401_RE.search(lower))


def _process_one(entry: dict) -> None:
    # Imported lazily to avoid circular imports at module load.
    from app.routes.notion import push_to_notion
    from app.state import notion

    sid = entry["session_id"]

    # If Notion isn't configured at all, leave the entry for later and stop
    # churning. We re-enqueue ourselves so it's picked up once connected.
    if not notion.is_connected() or not notion.get_database_id():
        _state.record_failure(entry, "Notion not connected", permanent=False)
        return

    try:
        result = push_to_notion(sid)
    except Exception as e:
        msg = str(e)
        if _is_auth_error(e):
            _state.pause(f"Notion auth expired: {msg}")
            _state.record_failure(entry, msg, permanent=False)
            return
        _state.record_failure(entry, msg, permanent=_is_permanent_error(msg))
        return

    err = (result or {}).get("error")
    if err:
        if _is_auth_error(err):
            _state.pause(f"Notion auth expired: {err}")
            _state.record_failure(entry, err, permanent=False)
            return
        _state.record_failure(entry, err, permanent=_is_permanent_error(err))
        return


def _worker_loop() -> None:
    last_pause_check = 0.0
    while True:
        # While paused, occasionally probe Notion to see if auth came back.
        if _state.paused:
            now = time.time()
            if now - last_pause_check > 30:
                last_pause_check = now
                try:
                    from app.state import notion as _notion

                    if _notion.verify_connection():
                        _state.resume()
                except Exception:
                    pass
            _state._wake.wait(timeout=5.0)
            _state._wake.clear()
            continue

        entry = _state.pop_due()
        if entry is None:
            # Sleep until either something is enqueued or 2s elapses (so we
            # re-check backoff timers without needing a scheduler).
            _state._wake.wait(timeout=2.0)
            _state._wake.clear()
            continue
        try:
            _process_one(entry)
        except Exception as e:
            log.exception(f"[notion-queue] unexpected error processing {entry}: {e}")
            _state.record_failure(entry, str(e), permanent=False)


def start_worker() -> None:
    """Spawn the background worker. Idempotent."""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    t = threading.Thread(target=_worker_loop, name="notion-queue-worker", daemon=True)
    t.start()
    log.info("[notion-queue] worker started")
