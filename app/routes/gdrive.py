"""Google Drive integration routes."""

from flask import Blueprint, jsonify, request

from app.state import gdrive

bp = Blueprint("gdrive", __name__, url_prefix="/gdrive")


@bp.route("/status")
def status():
    state = gdrive.get_state()
    sync = gdrive.get_sync_status() if state["connected"] else {}
    return jsonify({**state, **sync})


@bp.route("/connect", methods=["POST"])
def connect():
    data = request.get_json(silent=True) or {}
    try:
        url = gdrive.start_connect(
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
        )
        return jsonify({"url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/disconnect", methods=["POST"])
def disconnect():
    return jsonify(gdrive.disconnect())


@bp.route("/folders")
def folders():
    q = request.args.get("q", "")
    try:
        results = gdrive.list_folders(q)
        return jsonify({"folders": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/create-folder", methods=["POST"])
def create_folder():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "No folder name"}), 400
    try:
        folder = gdrive.create_folder(name)
        gdrive.set_folder(folder["id"], folder["name"])
        return jsonify(folder)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/set-folder", methods=["POST"])
def set_folder():
    data = request.get_json(silent=True) or {}
    folder_id = data.get("folder_id", "")
    folder_name = data.get("folder_name", "")
    if not folder_id:
        return jsonify({"error": "No folder_id"}), 400
    gdrive.set_folder(folder_id, folder_name)
    return jsonify({"saved": True})


@bp.route("/upload", methods=["POST"])
def upload():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id:
        return jsonify({"error": "No session_id"}), 400

    from app.routes.recordings import _find_recording

    path = _find_recording(session_id)
    if not path:
        return jsonify({"error": "Recording not found"}), 404

    try:
        drive_id = gdrive.upload_file(str(path))
        gdrive.mark_synced(session_id, drive_id, path.stat().st_size)
        return jsonify({"uploaded": True, "drive_file_id": drive_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/sync", methods=["POST"])
def sync():
    try:
        result = gdrive.sync_all()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/sync-status")
def sync_status():
    return jsonify(gdrive.get_sync_status())


@bp.route("/upload-progress")
def upload_progress():
    return jsonify(gdrive.get_upload_progress())
