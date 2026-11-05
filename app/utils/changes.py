"""A counter that goes up whenever the library changes outside the window's own edits.

Writes that an AI tool can make (speaker names, tags) call ``bump()``; the window
polls ``GET /changes`` and reloads when the number moves. In-process only: it
restarts at 0 with the backend, and the window reloads on a backend restart anyway.
"""

from threading import Lock

_lock = Lock()
_counter = 0


def bump() -> int:
    """Record a change; return the new counter."""
    global _counter
    with _lock:
        _counter += 1
        return _counter


def current() -> int:
    with _lock:
        return _counter
