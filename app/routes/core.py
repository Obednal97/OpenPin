"""Core routes: health check and audio serving."""

import os
import subprocess
import tempfile
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

from flask import Blueprint, Response, abort, jsonify, request, send_file

from app.utils.constants import RECORDINGS_DIR, get_all_recordings_dirs
from app.utils.system import ensure_ffmpeg

bp = Blueprint("core", __name__)

try:
    _APP_VERSION = _pkg_version("plaud-v2")
except PackageNotFoundError:
    _APP_VERSION = "unknown"

# Cache transcoded M4A files so we only convert once per recording
_M4A_CACHE_DIR = None


def _m4a_cache_dir():
    global _M4A_CACHE_DIR
    if _M4A_CACHE_DIR is None:
        _M4A_CACHE_DIR = os.path.join(
            os.path.expanduser("~"),
            "Library", "Application Support", "com.plaud.v2", "cache", "m4a"
        )
        os.makedirs(_M4A_CACHE_DIR, exist_ok=True)
    return _M4A_CACHE_DIR


@bp.route("/health")
def health():
    return jsonify({"status": "ok", "version": _APP_VERSION})


@bp.route("/audio/<path:filepath>")
def serve_audio(filepath):
    # Search all known recording directories for the file
    full_path = None
    for rec_dir in get_all_recordings_dirs():
        candidate = rec_dir / filepath
        if candidate.exists():
            full_path = candidate
            break
    if full_path is None:
        # Try as absolute path
        full_path = Path(filepath)
    if not full_path.exists():
        # Fall back to Google Drive streaming for cloud-only recordings
        session_id = Path(filepath).stem
        try:
            from app.state import gdrive
            synced = gdrive.get_synced_files()
            info = synced.get(session_id)
            if info and info.get("drive_file_id") and gdrive.is_connected():
                data, content_type = gdrive.proxy_stream(info["drive_file_id"])

                # Check if M4A transcoding needed
                want_m4a = request.args.get("format") == "m4a"
                if want_m4a and content_type == "audio/ogg":
                    # Cache the OGG locally, transcode, serve M4A
                    cache_dir = _m4a_cache_dir()
                    ogg_cache = os.path.join(cache_dir, f"{session_id}.ogg")
                    m4a_cache = os.path.join(cache_dir, f"{session_id}.m4a")
                    if not os.path.exists(m4a_cache):
                        with open(ogg_cache, "wb") as f:
                            f.write(data)
                        m4a_path = _get_or_transcode_m4a(Path(ogg_cache))
                        if m4a_path and os.path.exists(m4a_path):
                            return send_file(m4a_path, mimetype="audio/mp4")
                    else:
                        return send_file(m4a_cache, mimetype="audio/mp4")

                return Response(data, mimetype=content_type)
        except Exception:
            pass
        abort(404)

    # WebKit (Tauri/Safari) can't play OGG — serve M4A (AAC) instead
    # Check for ?format=m4a query param or Accept header lacking ogg
    want_m4a = request.args.get("format") == "m4a"
    if not want_m4a:
        accept = request.headers.get("Accept", "")
        # If client explicitly doesn't accept ogg, or is a WebKit UA
        if "audio/ogg" not in accept and "audio/*" not in accept and "*/*" not in accept:
            want_m4a = True

    if want_m4a and str(full_path).endswith(".ogg"):
        m4a_path = _get_or_transcode_m4a(full_path)
        if m4a_path and os.path.exists(m4a_path):
            return send_file(m4a_path, mimetype="audio/mp4")
        # Fallback to OGG if transcode fails

    return send_file(str(full_path), mimetype="audio/ogg")


def _get_or_transcode_m4a(ogg_path):
    """Get cached M4A or transcode from OGG. Returns M4A path or None."""
    cache_dir = _m4a_cache_dir()
    stem = Path(ogg_path).stem
    m4a_path = os.path.join(cache_dir, f"{stem}.m4a")

    # Return cached version if it exists and is newer than the OGG
    if os.path.exists(m4a_path):
        if os.path.getmtime(m4a_path) >= os.path.getmtime(str(ogg_path)):
            return m4a_path

    # Transcode with FFmpeg
    ffmpeg = ensure_ffmpeg()
    if not ffmpeg:
        return None

    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-i", str(ogg_path), "-c:a", "aac", "-b:a", "128k", m4a_path],
            capture_output=True,
            timeout=60,
        )
        if result.returncode == 0:
            return m4a_path
    except Exception:
        pass

    return None
