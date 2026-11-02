"""Persistent, retrying background queue for Notion pushes.

Two kinds of job, keyed by session_id:

- ``properties`` — tag / title changes. Only the page's properties are
  updated (notion-update-page ``update_properties``); the page body, including
  anything the user wrote in Notion, is never touched.
- ``full`` — create the page if it doesn't exist yet (upsert by Session ID),
  otherwise update properties and replace only the app-owned section. Used by
  auto-push after summarising.

Design:
- Keyed by session_id; a newer job for the same recording replaces the older
  one (last-write-wins), except that ``full`` is never downgraded to
  ``properties``.
- Persisted to disk on every state change. An entry is removed only after it
  succeeded (or failed permanently), so a crash mid-push leaves it queued and
  it runs again on the next launch. Pushes are idempotent (upsert by Session
  ID), so running one twice is safe.
- Single worker thread with exponential backoff on transient failures, and
  at most ``_MAX_FAILURES`` counted failures per job before it is given up.
- Permanent failures (and jobs over the cap) are kept in a ``failed`` list
  (shown in Settings with a Retry button) instead of disappearing silently.
- A properties-only job whose Notion page was deleted or archived is turned
  into a full push once, which recreates the page.
- Global pause when Notion auth fails. While paused we probe with backoff,
  and stop probing entirely once the refresh token is known to be dead (the
  user must reconnect, which resumes the queue).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.utils.constants import NOTION_QUEUE_PATH

log = logging.getLogger(__name__)

# Exponential backoff schedule in seconds, indexed by attempt count.
# After the last entry we stay at 1h.
_BACKOFF_SCHEDULE = [2, 5, 15, 45, 180, 900, 3600]

# While paused for auth, probe Notion this often (doubling, capped).
_PAUSE_PROBE_FIRST = 30.0
_PAUSE_PROBE_MAX = 1800.0

# Keep at most this many permanently-failed jobs.
_FAILED_CAP = 50

# Give a job up (move it to ``failed``) after this many failed attempts.
# Waiting for auth/connection or for an async create to finish doesn't count.
# With _BACKOFF_SCHEDULE that is roughly 4 hours of retrying.
_MAX_FAILURES = 10

KIND_PROPERTIES = "properties"
KIND_FULL = "full"
_KIND_RANK = {KIND_PROPERTIES: 0, KIND_FULL: 1}

# Errors that indicate "don't retry — the request was bad". Everything else
# is treated as transient (network blip, 5xx, rate limit) and retried.
# Notion's codes/messages: https://developers.notion.com/reference/status-codes
# (object_not_found: "Could not find database with ID: …"; restricted_resource).
_PERMANENT_ERROR_MARKERS = (
    "not connected",
    "no database selected",
    "validation_error",
    "could not parse",
    "invalid session_id",
    "transcript not found",
    "object_not_found",
    "could not find",
    "restricted_resource",
    "that is archived",
    "is archived",
    "rejected the tag options",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017 (requires-python >=3.10)


def _stronger_kind(a: str | None, b: str | None) -> str:
    a = a if a in _KIND_RANK else KIND_PROPERTIES
    b = b if b in _KIND_RANK else KIND_PROPERTIES
    return a if _KIND_RANK[a] >= _KIND_RANK[b] else b


class _State:
    """Thread-safe wrapper around the on-disk queue file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._wake = threading.Event()
        # entries: {session_id: {"session_id", "kind", "gen", "attempts",
        #            "next_retry_at", "last_error", "enqueued_at"}}
        self.entries: dict[str, dict] = {}
        # Jobs that failed permanently: [{"session_id", "kind", "error", "failed_at"}]
        self.failed: list[dict] = []
        # session_ids currently being processed (memory only: after a crash
        # nothing is in flight, so everything on disk is picked up again).
        self._in_flight: set[str] = set()
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
        entries = {}
        for sid, v in (data.get("entries") or {}).items():
            if not isinstance(v, dict):
                continue
            v.setdefault("session_id", sid)
            v.setdefault("kind", KIND_PROPERTIES)  # pre-2.3.1 entries were tag syncs
            v.setdefault("gen", uuid.uuid4().hex)
            entries[sid] = v
        self.entries = entries
        self.failed = [f for f in (data.get("failed") or []) if isinstance(f, dict)][-_FAILED_CAP:]
        self.paused = bool(data.get("paused"))
        self.pause_reason = data.get("pause_reason")

    def _save_locked(self) -> None:
        """Write under self._lock. Atomic via tmp + rename."""
        payload = {
            "entries": self.entries,
            "failed": self.failed,
            "paused": self.paused,
            "pause_reason": self.pause_reason,
        }
        tmp_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(prefix=".notion_queue.", dir=str(self.path.parent))
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, self.path)
        except Exception as e:
            log.warning(f"[notion-queue] could not write {self.path}: {e}")
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ---- public API ----

    def enqueue(self, session_id: str, kind: str = KIND_PROPERTIES) -> None:
        """Register that `session_id` needs to be pushed to Notion.

        Replaces any pending job for the same recording (keeping the stronger
        kind) and resets its backoff so the new state is tried immediately.
        """
        if not session_id:
            return
        with self._lock:
            existing = self.entries.get(session_id) or {}
            self.entries[session_id] = {
                "session_id": session_id,
                "kind": _stronger_kind(existing.get("kind") if existing else None, kind),
                "gen": uuid.uuid4().hex,
                "enqueued_at": _now_iso(),
                "attempts": 0,
                "next_retry_at": time.time(),
                "last_error": None,
            }
            self._save_locked()
        self._wake.set()

    def pop_due(self) -> dict | None:
        """Claim one due entry and return a copy of it, or None.

        The entry stays in the queue (and on disk) until ``complete`` or
        ``record_failure`` is called for it, so a crash mid-push loses nothing.
        """
        now = time.time()
        with self._lock:
            if self.paused:
                return None
            for sid, entry in self.entries.items():
                if sid in self._in_flight:
                    continue
                if entry.get("next_retry_at", 0) <= now:
                    self._in_flight.add(sid)
                    return dict(entry)
        return None

    def _is_current_locked(self, entry: dict) -> bool:
        existing = self.entries.get(entry.get("session_id"))
        return bool(existing) and existing.get("gen") == entry.get("gen")

    def complete(self, entry: dict) -> None:
        """The claimed job succeeded: drop it, unless a newer job replaced it."""
        sid = entry.get("session_id")
        with self._lock:
            self._in_flight.discard(sid)
            if self._is_current_locked(entry):
                del self.entries[sid]
                self._save_locked()
        self._wake.set()

    def record_failure(self, entry: dict, error: str, permanent: bool, counts: bool = True) -> None:
        """Record a failed attempt: give up if ``permanent`` (or the job is
        over ``_MAX_FAILURES``), otherwise back off and retry.

        ``counts=False`` for waits that aren't the job's fault (Notion not
        connected, auth paused, async create still running): they back off
        but don't use up the job's failure budget.
        """
        sid = entry.get("session_id")
        if not sid:
            return
        with self._lock:
            self._in_flight.discard(sid)
            if not self._is_current_locked(entry):
                # A newer enqueue landed while we were working — it supersedes
                # this attempt and will run with a fresh backoff.
                return
            failures = int(entry.get("failures", 0)) + (1 if counts else 0)
            if not permanent and failures >= _MAX_FAILURES:
                permanent = True
                error = f"Gave up after {failures} failed attempts: {error}"
            if permanent:
                log.warning(f"[notion-queue] giving up on {sid} — {error}")
                del self.entries[sid]
                self.failed = [f for f in self.failed if f.get("session_id") != sid]
                self.failed.append(
                    {
                        "session_id": sid,
                        "kind": entry.get("kind", KIND_PROPERTIES),
                        "error": error,
                        "failed_at": _now_iso(),
                    }
                )
                self.failed = self.failed[-_FAILED_CAP:]
                self._save_locked()
                return
            attempts = int(entry.get("attempts", 0)) + 1
            delay = _BACKOFF_SCHEDULE[min(attempts - 1, len(_BACKOFF_SCHEDULE) - 1)]
            current = self.entries[sid]
            current["attempts"] = attempts
            current["failures"] = failures
            current["next_retry_at"] = time.time() + delay
            current["last_error"] = error
            self._save_locked()

    def convert_to_full(self, entry: dict, error: str) -> bool:
        """Turn a claimed properties-only job into a full push, once.

        Used when the recording's Notion page is gone (deleted/archived): only
        a full push can recreate it. Returns False if the job was already
        converted or is already full (the caller then records a failure).
        """
        sid = entry.get("session_id")
        with self._lock:
            if not self._is_current_locked(entry):
                self._in_flight.discard(sid)
                return True  # superseded by a newer job; nothing to do
            current = self.entries[sid]
            if current.get("kind") == KIND_FULL or current.get("converted_from"):
                return False
            self._in_flight.discard(sid)
            log.info(f"[notion-queue] Notion page for {sid} is gone; switching to a full push ({error})")
            current["kind"] = KIND_FULL
            current["converted_from"] = KIND_PROPERTIES
            current["gen"] = uuid.uuid4().hex
            current["next_retry_at"] = time.time()
            current["last_error"] = error
            self._save_locked()
        self._wake.set()
        return True

    def retry_failed(self) -> int:
        """Re-queue every permanently-failed job. Returns how many."""
        with self._lock:
            failed, self.failed = self.failed, []
            now = time.time()
            for f in failed:
                sid = f.get("session_id")
                if not sid:
                    continue
                existing = self.entries.get(sid) or {}
                self.entries[sid] = {
                    "session_id": sid,
                    "kind": _stronger_kind(existing.get("kind") if existing else None, f.get("kind")),
                    "gen": uuid.uuid4().hex,
                    "enqueued_at": _now_iso(),
                    "attempts": 0,
                    "next_retry_at": now,
                    "last_error": None,
                }
            self._save_locked()
        self._wake.set()
        return len(failed)

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
                "failed": [dict(f) for f in self.failed],
                "failed_count": len(self.failed),
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


def enqueue(session_id: str, kind: str = KIND_PROPERTIES) -> None:
    _state.enqueue(session_id, kind)


def snapshot() -> dict:
    snap = _state.snapshot()
    try:
        from app.state import notion

        snap["needs_reconnect"] = notion.needs_reconnect()
    except Exception:
        snap["needs_reconnect"] = False
    return snap


def resume() -> None:
    _state.resume()


def retry_failed() -> int:
    return _state.retry_failed()


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

    # If Notion isn't configured at all, keep the entry for later (with
    # backoff) so it's picked up once connected.
    if not notion.is_connected() or not notion.get_database_id():
        _state.record_failure(entry, "Notion not connected", permanent=False, counts=False)
        return

    try:
        result = push_to_notion(sid, mode=entry.get("kind", KIND_PROPERTIES))
    except Exception as e:
        msg = str(e)
        if _is_auth_error(e):
            _state.pause(f"Notion auth expired: {msg}")
            _state.record_failure(entry, msg, permanent=False, counts=False)
            return
        _state.record_failure(entry, msg, permanent=_is_permanent_error(msg))
        return

    result = result or {}
    err = result.get("error")
    if err:
        if _is_auth_error(err):
            _state.pause(f"Notion auth expired: {err}")
            _state.record_failure(entry, err, permanent=False, counts=False)
            return
        if result.get("page_gone") and _state.convert_to_full(entry, err):
            # The page was deleted/archived in Notion: a properties-only job
            # can't recreate it, a full push can (once).
            return
        if result.get("busy"):
            # Another push for this recording (e.g. a click) holds the lock.
            _state.record_failure(entry, err, permanent=False, counts=False)
            return
        _state.record_failure(entry, err, permanent=_is_permanent_error(err))
        return
    if result.get("pending"):
        # Notion accepted an async create that hasn't finished: poll it again
        # later (the task id is persisted, so no second page is created).
        _state.record_failure(
            entry, result.get("message") or "Notion is still creating the page", permanent=False, counts=False
        )
        return
    _state.complete(entry)


# Auth-pause probe state: when we last probed and the current interval.
_probe = {"last": 0.0, "interval": _PAUSE_PROBE_FIRST}


def _probe_while_paused(now: float | None = None) -> bool:
    """While paused for auth, check (with backoff) whether Notion works again.

    Returns True if a probe was made. A dead refresh token can't recover by
    itself, so then we don't probe at all and wait for the user to reconnect
    (which calls resume()).
    """
    from app.state import notion as _notion

    now = time.time() if now is None else now
    try:
        if _notion.login_is_dead():
            return False
    except Exception:
        pass
    if now - _probe["last"] < _probe["interval"]:
        return False
    _probe["last"] = now
    try:
        ok = _notion.verify_connection()
    except Exception:
        ok = False
    if ok:
        _probe["interval"] = _PAUSE_PROBE_FIRST
        _state.resume()
    else:
        _probe["interval"] = min(_probe["interval"] * 2, _PAUSE_PROBE_MAX)
    return True


def _worker_loop() -> None:
    while True:
        if _state.paused:
            _probe_while_paused()
            _state._wake.wait(timeout=5.0)
            _state._wake.clear()
            continue
        _probe["interval"] = _PAUSE_PROBE_FIRST

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
