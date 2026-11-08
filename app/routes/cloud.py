"""Cloud routes: Plaud Cloud login, sync, upload, trash."""

import traceback

from flask import Blueprint, jsonify, request

from app.state import cloud
from app.utils.config import load_config


def _error_json(base, exc):
    """Return error JSON with traceback for debugging (auto-reported to GitHub)."""
    base["error"] = str(exc)
    base["traceback"] = traceback.format_exc()
    return base


bp = Blueprint("cloud", __name__, url_prefix="/cloud")


@bp.route("/status")
def status():
    return jsonify(cloud.get_state())


@bp.route("/login-url", methods=["POST"])
def login_url():
    """Generate the OAuth login URL for Plaud web login."""
    data = request.get_json(silent=True) or {}
    region = data.get("region", "eu")
    url, desktop_uuid = cloud.get_login_url(region)
    return jsonify({"url": url, "desktop_uuid": desktop_uuid})


@bp.route("/exchange-token", methods=["POST"])
def exchange_token():
    """Exchange auth code for bearer token after OAuth flow."""
    data = request.get_json(silent=True) or {}
    auth_code = data.get("auth_code", "")
    desktop_uuid = data.get("desktop_uuid", "")

    if not auth_code:
        return jsonify({"error": "No auth code provided"}), 400

    try:
        # "link": the code came from a plaud:// link (any web page can open one)
        from_link = data.get("source") == "link"
        result = cloud.exchange_token(auth_code, desktop_uuid, from_link=from_link)
        if result.get("error"):
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


@bp.route("/login-token", methods=["POST"])
def login_token():
    """Login with a manually provided bearer token."""
    data = request.get_json(silent=True) or {}
    token = data.get("token", "").strip()
    region = data.get("region", "eu")

    if not token:
        return jsonify({"error": "No token provided"}), 400

    try:
        result = cloud.login_with_token(token, region)
    except Exception as e:
        return jsonify({"error": f"Couldn't check the token with Plaud: {e}"}), 502
    if result.get("error"):
        return jsonify(result), 401
    return jsonify(result)


@bp.route("/logout", methods=["POST"])
def logout():
    return jsonify(cloud.logout())


@bp.route("/sync", methods=["POST"])
def sync():
    """Sync with Plaud Cloud. Downloads by default, uploads too if two-way is enabled."""
    if not cloud.is_logged_in():
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    direction = data.get("direction", load_config().get("cloud_sync_direction", "download"))

    try:
        download_result = cloud.download_cloud_recordings()

        upload_result = None
        if direction == "twoway":
            try:
                upload_result = cloud.upload_local_recordings_to_cloud()
            except Exception as e:
                upload_result = {"error": str(e)}

        return jsonify(
            {
                "download": download_result,
                "upload": upload_result,
                "direction": direction,
            }
        )
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


@bp.route("/bind-device", methods=["POST"])
def bind_device():
    """Bind a device to the user's Plaud cloud account and get its BIND_TOKEN."""
    if not cloud.is_logged_in():
        return jsonify({"error": "Not logged in"}), 401
    data = request.get_json(silent=True) or {}
    sn = data.get("sn", "")
    if not sn:
        return jsonify({"error": "No serial number provided"}), 400
    try:
        result = cloud.bind_device(
            sn=sn,
            name=data.get("name", "Plaud NotePin S"),
            model=data.get("model", "882"),
            version_number=data.get("version_number", 65546),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


@bp.route("/sn-sign", methods=["POST"])
def sn_sign():
    """Fetch the SN signature for a device from the Plaud cloud."""
    if not cloud.is_logged_in():
        return jsonify({"error": "Not logged in"}), 401
    data = request.get_json(silent=True) or {}
    sn = data.get("sn", "")
    if not sn:
        return jsonify({"error": "No serial number provided"}), 400
    try:
        result = cloud.fetch_sn_signature(sn)
        return jsonify(result)
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


@bp.route("/trash", methods=["POST"])
def trash():
    """Move files to trash on Plaud Cloud."""
    if not cloud.is_logged_in():
        return jsonify({"error": "Not logged in"}), 401
    data = request.get_json(silent=True) or {}
    cloud_ids = data.get("ids", [])
    if not cloud_ids:
        return jsonify({"error": "No IDs provided"}), 400
    try:
        result = cloud.trash_cloud_files(cloud_ids)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
