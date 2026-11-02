#!/usr/bin/env python3
"""Plaud V2 — Flask API server.

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

from app.routes import cloud, core, device, gdrive, notion, recordings, settings, transcribe

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

API_TOKEN_HEADER = "X-Plaud-Token"
# Tauri 2 serves the app from tauri://localhost on macOS/Linux and
# http(s)://tauri.localhost on Windows.
ALLOWED_ORIGINS = frozenset({"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"})
ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost"})
# Endpoints reachable without the token (Rust's readiness probe uses /health).
_TOKEN_EXEMPT_PATHS = frozenset({"/health"})


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


@app.before_request
def _enforce_request_security():
    if not _host_allowed(request.headers.get("Host", "")):
        return jsonify({"error": "Forbidden host"}), 403

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
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = f"Content-Type, Authorization, {API_TOKEN_HEADER}"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "600"
    response.vary.add("Origin")
    return response


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


# Recover WiFi if app previously crashed during WiFi sync (left on Plaud hotspot)
def _recover_wifi():
    """Check if we're stuck on a Plaud hotspot from a crashed WiFi sync and restore internet."""
    try:
        import subprocess
        result = subprocess.run(
            ["networksetup", "-getairportnetwork", "en0"],
            capture_output=True, text=True, timeout=5,
        )
        current_ssid = result.stdout.strip().split(": ", 1)[-1] if ": " in result.stdout else ""
        if current_ssid.startswith("Plaud") or current_ssid.startswith("PLAUD"):
            logging.warning(f"[WiFi Recovery] Stuck on Plaud hotspot '{current_ssid}', power-cycling WiFi...")
            subprocess.run(["networksetup", "-setairportpower", "en0", "off"], timeout=5)
            import time
            time.sleep(3)
            subprocess.run(["networksetup", "-setairportpower", "en0", "on"], timeout=5)
            logging.info("[WiFi Recovery] WiFi power-cycled, should reconnect to preferred network")
    except Exception as e:
        logging.error(f"[WiFi Recovery] Check failed: {e}")

_recover_wifi()


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

_clean_partial_files()

# Register blueprints
app.register_blueprint(core.bp)
app.register_blueprint(device.bp)
app.register_blueprint(cloud.bp)
app.register_blueprint(notion.bp)
app.register_blueprint(gdrive.bp)
app.register_blueprint(recordings.bp)
app.register_blueprint(transcribe.bp)
app.register_blueprint(settings.bp)

if __name__ == "__main__":
    from werkzeug.serving import make_server

    # Recover metadata from transcript files and start background workers
    # *here* (not at module-import time). This guarantees init runs before
    # serve_forever opens the listening socket, and skips re-running these
    # side effects if api_server.py is ever imported from a test/REPL.
    recordings.init_recordings()
    from app.utils import notion_queue as _notion_queue
    _notion_queue.start_worker()

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
        print(f"Plaud V2 API server starting on http://127.0.0.1:{candidate}", flush=True)
        bound = True
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
