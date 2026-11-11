#!/usr/bin/env python3
"""Open Pin — Flask API server.

Wraps the existing BLE/crypto/transcription code as HTTP endpoints.
Spawned by the Tauri Rust shell as a child process on localhost:5123.
"""

import hmac
import logging
import os
import sys
from pathlib import Path

# Launched as a Tauri sidecar — sys.path is configured here so that
# "from app.routes import ..." resolves against PROJECT_ROOT.
_this_dir = Path(__file__).parent
PROJECT_ROOT = _this_dir.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Auto-install core dependencies if missing (diarize is optional, lazy-imported at transcription time)
from app.utils.system import ensure_installed

ensure_installed("flask", required=True)
ensure_installed("requests", required=True)

from flask import Flask, jsonify, request

from app.mcp import access as mcp_access
from app.routes import (
    cloud,
    coaching,
    core,
    device,
    diagnostics,
    gdrive,
    mcp_api,
    notion,
    people,
    recordings,
    settings,
    transcribe,
)
from app.utils import changes

app = Flask(__name__)
log = logging.getLogger("werkzeug")
log.setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Request hardening: CORS allow-list, Host check, per-launch API token
# ---------------------------------------------------------------------------
#
# The backend listens on 127.0.0.1, which any web page in any browser on this
# machine can reach. Three layers keep those pages out:
#
# 1. CORS: only the Tauri webview origins get an Access-Control-Allow-Origin
#    header, so other pages can't read responses (and can't send the token
#    header, which forces a preflight that fails).
# 2. Host check: requests whose Host isn't 127.0.0.1/localhost are refused,
#    which blocks DNS rebinding (attacker.example resolving to 127.0.0.1).
# 3. Token: the Rust shell generates a random token per launch and passes it in
#    PLAUD_API_TOKEN. Every request must send it in the X-Plaud-Token header
#    (GET /audio and POST /device/disconnect may use ?token= instead, since
#    <audio src> and navigator.sendBeacon can't set headers).
#    /health and CORS preflights are exempt.
#
# If PLAUD_API_TOKEN is unset (tests, or running api_server.py by hand) the
# token check is skipped with a loud warning; CORS and Host checks still apply.
#
# /mcp/v1/* (AI tools through the MCP connector, app/mcp/) is a separate world:
# - It needs the MCP key in X-Open-Pin-MCP-Key and nothing else opens it: not
#   the app token, not ?token=, and not a missing PLAUD_API_TOKEN.
# - Anything a browser sends (Origin, or Sec-Fetch-* fetch metadata, which
#   browsers add even to no-cors GETs) is refused, preflights included, and
#   gets no CORS headers.
# - While AI tool access is off there is no key, and it answers 403 with
#   MCP_OFF_MESSAGE.
# The MCP key is never looked at on any other path, so it opens nothing else.
# The Host check above applies to /mcp/v1 as everywhere.

API_TOKEN_HEADER = "X-Plaud-Token"
# Tauri 2 serves the app from tauri://localhost on macOS/Linux and
# http(s)://tauri.localhost on Windows.
ALLOWED_ORIGINS = frozenset({"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"})
ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost"})
# Endpoints reachable without the token (Rust's readiness probe uses /health).
_TOKEN_EXEMPT_PATHS = frozenset({"/health"})
MCP_PATH_PREFIX = "/mcp/v1"
MCP_OFF_MESSAGE = "AI tool access is turned off in Open Pin"


def _expected_token() -> str:
    # Read on each request so tests can set/unset the env var.
    return os.environ.get("PLAUD_API_TOKEN", "")


if not _expected_token():
    logging.warning(
        "!!! PLAUD_API_TOKEN is not set: API token auth is DISABLED. "
        "Only acceptable in tests or when running api_server.py by hand. !!!"
    )


def _query_token_allowed(method: str, path: str) -> bool:
    """Where ``?token=`` may stand in for the X-Plaud-Token header.

    - GET /audio/<file>: <audio src> can't set headers.
    - POST /device/disconnect: sent with navigator.sendBeacon on page unload
      (Cmd+R). A beacon can't set headers either, and a header would force a
      CORS preflight that WKWebView may drop while the page unloads.

    Werkzeug's access log is silenced (WARNING), so these URLs aren't logged.

    Every other route requires the header.
    """
    if method == "GET" and path.startswith("/audio/"):
        return True
    return method == "POST" and path == "/device/disconnect"


def _host_allowed(host: str) -> bool:
    """Accept 127.0.0.1 / localhost with any (or no) port."""
    if not host:
        return False
    hostname, sep, port = host.rpartition(":")
    if not sep:
        hostname, port = host, ""
    if port and not port.isdigit():
        return False
    return hostname.lower() in ALLOWED_HOSTNAMES


def _is_mcp_path(path: str) -> bool:
    return path == MCP_PATH_PREFIX or path.startswith(MCP_PATH_PREFIX + "/")


def _from_browser(headers) -> bool:
    return "Origin" in headers or any(name.lower().startswith("sec-fetch-") for name, _ in headers.items())


def _mcp_guard():
    """Auth for /mcp/v1/*: the MCP key only. Returns an error response or None."""
    if _from_browser(request.headers):
        return jsonify({"error": "Forbidden"}), 403
    key = mcp_access.current_key()
    if key is None:
        return jsonify({"error": MCP_OFF_MESSAGE}), 403
    supplied = request.headers.get(mcp_access.MCP_KEY_HEADER, "")
    if not hmac.compare_digest(supplied.encode(), key.encode()):
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.before_request
def _enforce_request_security():
    if not _host_allowed(request.headers.get("Host", "")):
        return jsonify({"error": "Forbidden host"}), 403

    # Before the OPTIONS shortcut and the app-token skip: never falls through.
    if _is_mcp_path(request.path):
        return _mcp_guard()

    if request.method == "OPTIONS":
        # CORS preflight: answer directly (after_request adds the CORS headers
        # only for allowed origins). The view never runs.
        return app.make_default_options_response()

    expected = _expected_token()
    if not expected or request.path in _TOKEN_EXEMPT_PATHS:
        return None

    supplied = request.headers.get(API_TOKEN_HEADER, "")
    if not supplied and _query_token_allowed(request.method, request.path):
        supplied = request.args.get("token", "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.after_request
def add_cors(response):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS and not _is_mcp_path(request.path):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = f"Content-Type, Authorization, {API_TOKEN_HEADER}"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "600"
    response.vary.add("Origin")
    return response


@app.route("/changes", methods=["GET"])
def get_changes():
    """The change counter (app/utils/changes.py): the window reloads when it moves."""
    return jsonify({"counter": changes.current()})


# JSON error handlers — prevent Flask returning HTML error pages
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed"}), 405


@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Internal server error", "detail": str(e)}), 500


# Startup housekeeping. These helpers are called from the __main__ block below,
# never at import time: tests (and anything else that imports this module) must
# not run networksetup or delete files in the real recordings folders.


def _recover_wifi():
    """Undo what a crashed or killed WiFi sync left behind, then restore internet.

    If the worker's restore record exists (wifi_restore.json in the data dir),
    restore from it: saved networks back in their original order, the hotspot
    forgotten, WiFi power-cycled if still on the hotspot; the record is deleted
    only after a verified restore, or given up (kept as wifi_restore.stuck.json,
    shown once in the UI) after repeated or permanent failures. It's skipped
    while a worker (e.g. from an app instance that's still running) holds the
    sync lock. Without a record, fall back to checking for a Plaud hotspot by
    name.

    Slow (re-adding networks, power-cycling): run it via
    _start_wifi_recovery() after the server is listening, not before.
    """
    try:
        import subprocess
        import time

        # en0 on MacBooks, en1 on Mac mini / Studio / iMac (en0 is Ethernet there).
        # Lazy import: the worker module has no import-time side effects.
        from app.utils.constants import DATA_DIR
        from app.utils.wifi_sync_worker import get_wifi_interface, recover_from_record

        recovery = recover_from_record(DATA_DIR)
        if recovery["status"] != "none":
            log_fn = logging.info if recovery["status"] == "restored" else logging.warning
            log_fn(f"[WiFi Recovery] From restore record: {recovery}")
            return

        iface = get_wifi_interface()
        result = subprocess.run(
            ["networksetup", "-getairportnetwork", iface],
            capture_output=True,
            text=True,
            timeout=5,
        )
        current_ssid = result.stdout.strip().split(": ", 1)[-1] if ": " in result.stdout else ""
        if current_ssid.startswith("Plaud") or current_ssid.startswith("PLAUD"):
            logging.warning(f"[WiFi Recovery] Stuck on Plaud hotspot '{current_ssid}' ({iface}), power-cycling WiFi...")
            subprocess.run(["networksetup", "-setairportpower", iface, "off"], timeout=5)
            time.sleep(3)
            subprocess.run(["networksetup", "-setairportpower", iface, "on"], timeout=5)
            logging.info("[WiFi Recovery] WiFi power-cycled, should reconnect to preferred network")
    except Exception as e:
        logging.error(f"[WiFi Recovery] Check failed: {e}")


def _start_wifi_recovery():
    """Run _recover_wifi on a background thread (returns at once).

    Called once the server has bound its port, so a slow restore can't hold up
    /health past the shell's startup budget. A WiFi sync requested meanwhile
    waits for it (app.state.device.run_wifi_recovery_in_background).
    """
    from app.state import device as device_state

    return device_state.run_wifi_recovery_in_background(_recover_wifi)


# Clean up partial files from interrupted syncs
def _clean_partial_files():
    """Remove .ogg.partial files left by interrupted BLE syncs."""
    try:
        from app.utils.constants import get_all_recordings_dirs

        for rec_dir in get_all_recordings_dirs():
            if rec_dir.exists():
                for f in rec_dir.glob("*.partial"):
                    logging.info(f"[Cleanup] Removing partial file: {f.name}")
                    f.unlink(missing_ok=True)
    except Exception as e:
        logging.error(f"[Cleanup] Failed: {e}")


# Register blueprints
app.register_blueprint(core.bp)
app.register_blueprint(device.bp)
app.register_blueprint(cloud.bp)
app.register_blueprint(notion.bp)
app.register_blueprint(gdrive.bp)
app.register_blueprint(recordings.bp)
app.register_blueprint(transcribe.bp)
app.register_blueprint(settings.bp)
app.register_blueprint(diagnostics.bp)
app.register_blueprint(coaching.bp)
app.register_blueprint(people.bp)
# AI tools (MCP): the guard above opens it with the MCP key only.
app.register_blueprint(mcp_api.bp)


def _start_mcp_access(port: int):
    """Once the port is bound: turn AI tool access on if the setting is on.

    With it off, delete any connection file a crash left behind. Never raises:
    the backend runs without AI tool access rather than not at all.
    """
    try:
        from app.utils.config import load_config

        mcp_access.set_port(port)
        if load_config().get("mcp_enabled", False) is True:
            mcp_access.enable(port)
            logging.info("[MCP] AI tool access is on")
        else:
            mcp_access.mark_off(port)
    except Exception as e:
        logging.error(f"[MCP] Couldn't turn on AI tool access: {e}")


# How long the SIGTERM handler waits for another thread inside app/mcp/access.py.
SIGNAL_CLEANUP_TIMEOUT = 0.5


def _stop_mcp_access(timeout: float | None = None):
    """On exit: delete the connection file. The setting stays as it is."""
    try:
        mcp_access.disable(timeout=timeout)
    except Exception as e:
        logging.warning(f"[MCP] Cleanup failed: {e}")


def _install_exit_cleanup():
    """Run _stop_mcp_access on a normal exit and on SIGTERM.

    The shell stops the backend with SIGTERM, which skips atexit. The handler
    cleans up, then re-raises SIGTERM with the default action, so the process
    still ends exactly as it did before.
    """
    import atexit
    import signal

    atexit.register(_stop_mcp_access)

    def _on_sigterm(signum, frame):
        # Bounded: the lock is re-entrant (this thread may be inside enable()),
        # and another thread holding it mustn't stall the quit.
        _stop_mcp_access(timeout=SIGNAL_CLEANUP_TIMEOUT)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)

    signal.signal(signal.SIGTERM, _on_sigterm)


if __name__ == "__main__":
    from werkzeug.serving import make_server

    from app.utils import parent_watch

    # Exit if the Tauri shell that spawned us dies (SIGTERM/crash/force quit)
    # instead of lingering on the old port with the old token.
    parent_watch.start(on_exit=_stop_mcp_access)
    _install_exit_cleanup()

    _clean_partial_files()

    # Recover metadata from transcript files and start background workers
    # *here* (not at module-import time). This guarantees init runs before
    # serve_forever opens the listening socket, and skips re-running these
    # side effects if api_server.py is ever imported from a test/REPL.
    recordings.init_recordings()
    coaching.migrate_speaker_labels()
    # Saved Plaud cloud token (+ background validation). Not done at import so
    # tests and tooling that import the app make no network calls.
    from app.state import cloud as _cloud_state

    _cloud_state.init()
    # Keychain copy of the NotePin pairing credentials: back up what the config
    # has, restore what a wiped data folder lost (background thread).
    from app.utils import pairing_store as _pairing_store

    _pairing_store.start_startup_sync()
    from app.utils import notion_queue as _notion_queue

    _notion_queue.start_worker()
    # Learn voices from names typed in past recordings, then name them: once, in the
    # background, and only with the voice model installed (otherwise it stays pending).
    from app.people import backlog as _people_backlog

    _people_backlog.start_on_launch()

    start_port = int(os.environ.get("PLAUD_API_PORT", "5123"))
    # Walk past occupied ports if our initial pick was claimed in the window
    # between Rust's find_free_port probe and this bind. The Rust stdout drain
    # watches for the PLAUD_PORT_BOUND marker to learn the actual port.
    last_err: Exception | None = None
    bound = False
    for offset in range(8):
        candidate = start_port + offset
        try:
            server = make_server("127.0.0.1", candidate, app, threaded=True)
        except OSError as e:
            last_err = e
            continue
        print(f"PLAUD_PORT_BOUND: {candidate}", flush=True)
        print(f"Open Pin API server starting on http://127.0.0.1:{candidate}", flush=True)
        bound = True
        # The MCP connection file needs the port actually bound.
        _start_mcp_access(candidate)
        # Listening now: WiFi recovery from a crashed sync runs in the background.
        _start_wifi_recovery()
        try:
            server.serve_forever()
        finally:
            server.server_close()
        break
    if not bound:
        print(
            f"FATAL: could not bind any port in {start_port}..{start_port + 7}: {last_err}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
