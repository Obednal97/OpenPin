"""AI tool (MCP) access: the MCP key, the connection file and the launcher.

While "Let AI tools on this Mac use Open Pin" is on and the backend is running:

- ``enable(port)`` makes a random MCP key (kept in memory only), writes
  ``{"port", "key", "pid"}`` to ``<DATA_DIR>/mcp-connection.json`` (mode 0600)
  and rewrites the launcher script.
- The backend accepts the key only on ``/mcp/v1/*``, and those routes accept
  nothing else (the guard is in ``app/api_server.py``).
- ``disable()`` drops the key and deletes the connection file, so access stops
  at once. The launcher stays: Claude's settings point at it, and the connector
  then says access is turned off or Open Pin isn't running.

The launcher lives at a stable path in Application Support, never in the app
bundle (which moves on update). It runs this build's managed venv Python with
the connector script (``server.py``, next to this file) from the current bundle.
"""

import json
import logging
import os
import secrets
import shlex
import sys
import tempfile
import threading
from pathlib import Path

from app.utils import constants

MCP_KEY_HEADER = "X-Open-Pin-MCP-Key"
CONNECTION_FILE_NAME = "mcp-connection.json"
LAUNCHER_NAME = "open-pin-mcp"

# Shell-state folders in Application Support, as in src-tauri/src/paths.rs
# (RELEASE_SUPPORT_DIR / DEV_SUPPORT_DIR). The managed venv is <folder>/venv.
RELEASE_SUPPORT_DIR = "com.plaud.v2"
DEV_SUPPORT_DIR = "com.plaud.v2.dev"

# Re-entrant: a SIGTERM can run disable() on the main thread while that thread
# is inside enable() (startup), and must not deadlock on its own lock.
_lock = threading.RLock()
_key: str | None = None
_port: int | None = None


def current_key() -> str | None:
    """The live MCP key, or None while access is off."""
    with _lock:
        return _key


def set_port(port: int) -> None:
    """Record the port the backend bound (it may differ from PLAUD_API_PORT)."""
    global _port
    with _lock:
        _port = int(port)


def bound_port() -> int | None:
    with _lock:
        return _port


def connection_path() -> Path:
    # Read DATA_DIR at call time (tests patch it).
    return Path(constants.DATA_DIR) / CONNECTION_FILE_NAME


def support_dir(executable: str | None = None, home: Path | None = None) -> Path:
    """This build's shell-state folder in Application Support.

    The Rust shell doesn't tell Python which build it is, but it always runs
    the backend with the managed venv at ``Application Support/<folder>/venv``,
    so the folder comes from ``sys.executable``. Anything else (a checkout's
    own venv, tests) gets the release folder.
    """
    base = (home if home is not None else Path.home()) / "Library" / "Application Support"
    exe = Path(executable if executable is not None else sys.executable)
    # Not resolved: the venv's python is a symlink out of the venv.
    for parent in exe.parents:
        folder = parent.parent
        if (
            parent.name == "venv"
            and folder.name in (RELEASE_SUPPORT_DIR, DEV_SUPPORT_DIR)
            and folder.parent.name == "Application Support"
        ):
            return base / folder.name
    return base / RELEASE_SUPPORT_DIR


def launcher_path() -> Path:
    return support_dir() / "mcp" / LAUNCHER_NAME


def server_script_path() -> Path:
    """The connector script in the current app bundle (or checkout)."""
    return Path(__file__).resolve().parent / "server.py"


def _launcher_text() -> str:
    q = shlex.quote
    python = sys.executable  # the venv's interpreter; not resolved, or it leaves the venv
    server = str(server_script_path())
    data_dir = str(Path(constants.DATA_DIR))
    return (
        "#!/bin/sh\n"
        "# Open Pin's MCP connector for AI tools on this Mac. Open Pin rewrites\n"
        "# this file at each launch while AI tool access is on; don't edit it.\n"
        f"PYTHON={q(python)}\n"
        f"SERVER={q(server)}\n"
        'if [ ! -x "$PYTHON" ] || [ ! -f "$SERVER" ]; then\n'
        "  echo \"Open Pin can't be found where it was installed. "
        'Open the Open Pin app, check AI tool access is on in Settings, and try again." >&2\n'
        "  exit 1\n"
        "fi\n"
        f"PLAUD_DATA_DIR={q(data_dir)}\n"
        "export PLAUD_DATA_DIR\n"
        # No .pyc files in the signed bundle, and no user site or outside paths.
        "PYTHONDONTWRITEBYTECODE=1\n"
        "PYTHONNOUSERSITE=1\n"
        "export PYTHONDONTWRITEBYTECODE PYTHONNOUSERSITE\n"
        "unset PYTHONHOME PYTHONPATH\n"
        'exec "$PYTHON" "$SERVER"\n'
    )


def _atomic_write(path: Path, text: str, mode: int) -> None:
    """Write via a temp file in the same folder, created with ``mode``, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_launcher() -> Path:
    """(Re)write the launcher script (mode 0755) and return its path."""
    path = launcher_path()
    _atomic_write(path, _launcher_text(), 0o755)
    return path


def enable(port: int) -> None:
    """Turn access on: keep (or make) the key, write the connection file and launcher.

    Raises OSError if a file can't be written; access is then left off.
    """
    global _key, _port
    with _lock:
        key = _key or secrets.token_urlsafe(32)
        try:
            write_launcher()
            payload = json.dumps({"port": int(port), "key": key, "pid": os.getpid()})
            _atomic_write(connection_path(), payload, 0o600)
        except BaseException:
            _key = None
            _remove_file(force=True)
            raise
        _key = key
        _port = int(port)


def _remove_file(force: bool) -> None:
    path = connection_path()
    if not force:
        # A restarted backend may already have written its own file.
        try:
            owner = json.loads(path.read_text()).get("pid")
        except FileNotFoundError:
            return
        except (OSError, ValueError, AttributeError):
            owner = None
        if owner is not None and owner != os.getpid():
            return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logging.warning("[MCP] Couldn't remove %s: %s", path.name, e)


def disable(timeout: float | None = None) -> bool:
    """Turn access off: drop the key and delete this process's connection file.

    ``timeout`` (seconds) bounds the wait for another thread that holds the
    lock; the signal handler passes one so a quit can't hang. Returns False if
    it gave up (the file may then stay behind, which is harmless: the
    connector gets a refused connection).
    """
    global _key
    if not _lock.acquire(timeout=-1 if timeout is None else timeout):
        logging.warning("[MCP] Skipped cleanup: access is busy")
        return False
    try:
        _key = None
        _remove_file(force=False)
    finally:
        _lock.release()
    return True


def remove_connection_file() -> None:
    """Delete the connection file whoever wrote it (a stale one left by a crash)."""
    with _lock:
        _remove_file(force=True)
