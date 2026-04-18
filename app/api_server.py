#!/usr/bin/env python3
"""Plaud V2 — Flask API server for the Electron app.

Wraps the existing BLE/crypto/transcription code as HTTP endpoints.
Spawned by Electron main.js as a child process on localhost:5123.
"""

import logging
import sys
from pathlib import Path

# Ensure we can import from the parent project
# In dev: api_server.py is in app/, PROJECT_ROOT is one level up
# In packaged (ASAR): api_server.py is in app.asar.unpacked/ alongside routes/, utils/, etc.
#   but imports use "from app.routes import ..." so we need an "app" package.
_this_dir = Path(__file__).parent
PROJECT_ROOT = _this_dir.parent

# If running from ASAR-unpacked (no app/ subdirectory), create a virtual package
# by adding the parent AND making this directory importable as "app"
if not (PROJECT_ROOT / "app").exists() and (_this_dir / "routes").exists():
    # We're in the unpacked layout — this dir IS the app package
    sys.path.insert(0, str(PROJECT_ROOT))
    sys.path.insert(0, str(_this_dir.parent))
    # Make this directory importable as "app" by creating a symlink or using sys.modules
    import types
    app_module = types.ModuleType("app")
    app_module.__path__ = [str(_this_dir)]
    app_module.__file__ = str(_this_dir / "__init__.py")
    sys.modules["app"] = app_module
else:
    sys.path.insert(0, str(PROJECT_ROOT))

# Auto-install core dependencies if missing (diarize is optional, lazy-imported at transcription time)
from app.utils.system import ensure_installed

ensure_installed("flask")
ensure_installed("requests")

from flask import Flask, jsonify, request

from app.routes import cloud, core, device, gdrive, notion, recordings, settings, transcribe

app = Flask(__name__)
log = logging.getLogger("werkzeug")
log.setLevel(logging.WARNING)


@app.after_request
def add_cors(response):
    # Restrict CORS to localhost origins only (Tauri uses tauri://localhost or http://localhost)
    origin = request.headers.get("Origin", "")
    if origin and ("localhost" in origin or "127.0.0.1" in origin or "tauri://" in origin):
        response.headers["Access-Control-Allow-Origin"] = origin
    else:
        response.headers["Access-Control-Allow-Origin"] = "http://127.0.0.1"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
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

# Start background workers
from app.utils import notion_queue as _notion_queue
_notion_queue.start_worker()


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PLAUD_API_PORT", "5123"))
    print(f"Plaud V2 API server starting on http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
