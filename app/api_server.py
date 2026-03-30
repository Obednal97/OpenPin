#!/usr/bin/env python3
"""Plaud V2 — Flask API server for the Electron app.

Wraps the existing BLE/crypto/transcription code as HTTP endpoints.
Spawned by Electron main.js as a child process on localhost:5123.
"""

import logging
import sys
from pathlib import Path

# Ensure we can import from the parent project
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Auto-install dependencies if missing
from app.utils.system import ensure_installed

ensure_installed("flask")
ensure_installed("requests")
ensure_installed("diarize")

from flask import Flask

from app.routes import cloud, core, device, notion, recordings, settings, transcribe

app = Flask(__name__)
log = logging.getLogger("werkzeug")
log.setLevel(logging.WARNING)


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response


# Register blueprints
app.register_blueprint(core.bp)
app.register_blueprint(device.bp)
app.register_blueprint(cloud.bp)
app.register_blueprint(notion.bp)
app.register_blueprint(recordings.bp)
app.register_blueprint(transcribe.bp)
app.register_blueprint(settings.bp)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PLAUD_API_PORT", "5123"))
    print(f"Plaud V2 API server starting on http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
