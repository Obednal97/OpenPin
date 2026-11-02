"""Exit the backend when the Tauri shell that spawned it goes away.

If the shell is killed (SIGTERM/SIGKILL, crash, force quit) it never gets to
stop Python, which is then re-parented to launchd and keeps serving on the old
port with the old token until the next launch cleans it up. A small daemon
thread watches the original parent PID and exits the process when it changes.
"""

import logging
import os
import threading
import time

_started = False


def _parent_gone(original_ppid: int) -> bool:
    """True once our parent is no longer the process that started us."""
    return os.getppid() != original_ppid


def start(on_exit=None, interval: float = 2.0) -> bool:
    """Start the watcher once. Returns False if it is disabled or already running.

    Only active when launched by the shell (PLAUD_API_TOKEN set) and when the
    parent isn't launchd already (e.g. a manual `python api_server.py` run from
    a terminal keeps its shell as parent, which is fine).
    """
    global _started
    if _started or not os.environ.get("PLAUD_API_TOKEN"):
        return False
    original = os.getppid()
    if original <= 1:
        return False
    _started = True

    def _exit():
        logging.warning("[parent-watch] Plaud V2 shell (pid %s) is gone; stopping the backend", original)
        if on_exit is not None:
            try:
                on_exit()
            except Exception as e:  # never let cleanup block the exit
                logging.warning("[parent-watch] cleanup failed: %s", e)
        os._exit(0)

    def _loop():
        while True:
            time.sleep(interval)
            if _parent_gone(original):
                _exit()

    threading.Thread(target=_loop, name="parent-watch", daemon=True).start()
    return True
