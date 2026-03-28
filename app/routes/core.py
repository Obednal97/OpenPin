"""Core routes: health check and audio serving."""

from pathlib import Path

from flask import Blueprint, abort, jsonify, send_file

from app.utils.constants import RECORDINGS_DIR

bp = Blueprint("core", __name__)


@bp.route("/health")
def health():
    return jsonify({"status": "ok", "version": "1.0.0"})


@bp.route("/audio/<path:filepath>")
def serve_audio(filepath):
    full_path = RECORDINGS_DIR / filepath
    if not full_path.exists():
        full_path = Path(filepath)
    if not full_path.exists():
        abort(404)
    return send_file(str(full_path), mimetype="audio/ogg")
