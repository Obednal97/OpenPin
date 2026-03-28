"""Device routes: BLE scanning, connecting, syncing, recording."""

import threading
import time

from flask import Blueprint, jsonify, request

from app.state import device as dev
from app.utils.config import load_config
from app.utils.constants import RECORDINGS_DIR

bp = Blueprint("device", __name__, url_prefix="/device")


@bp.route("/scan", methods=["POST"])
def scan():
    try:
        result = dev.run_async(dev.scan_device())
        return jsonify(result)
    except Exception as e:
        return jsonify({"found": False, "error": str(e)}), 500


@bp.route("/scan-all", methods=["POST"])
def scan_all():
    """Scan for all Plaud devices in range with full info."""
    try:
        devices = dev.run_async(dev.scan_devices_with_retry(attempts=2, scan_duration=6))
        return jsonify({"devices": devices})
    except Exception as e:
        return jsonify({"devices": [], "error": str(e)}), 500


@bp.route("/connect", methods=["POST"])
def connect():
    try:
        data = request.get_json(silent=True) or {}
        address = data.get("address")
        serial_number = data.get("serial_number")
        result = dev.run_async(dev.connect_device(address))
        if result.get("connected"):
            dev.set_serial_number(serial_number)
            dev.save_known_device(address, result.get("device_name"), serial_number)
        return jsonify(result)
    except Exception as e:
        dev.set_connected(False)
        return jsonify({"connected": False, "error": str(e)}), 500


@bp.route("/disconnect", methods=["POST"])
def disconnect():
    try:
        result = dev.run_async(dev.disconnect_device())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/depair", methods=["POST"])
def depair():
    """Erase & disconnect — unpairs device so it can pair with another host."""
    try:
        result = dev.run_async(dev.depair_device())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/status")
def status():

    is_recording = False
    state = dev.get_state()
    rssi = state.get("rssi")
    if state["connected"]:
        # Combine state check + RSSI read in a single async call to avoid deadlocks
        async def _status_and_rssi():
            _is_rec = False
            _rssi = None
            try:
                r = await dev.send_cmd(bytes([1, 3, 0]), timeout=3)
                if r and len(r) >= 5:
                    state_byte = r[3] if len(r) > 3 else 0
                    _is_rec = state_byte in (1, 2, 3)
            except Exception:
                pass
            try:
                _rssi = await dev.read_rssi()
            except Exception:
                pass
            return _is_rec, _rssi

        try:
            is_recording, rssi = dev.run_async(_status_and_rssi())
        except Exception:
            pass

    return jsonify(
        {
            "connected": state["connected"],
            "battery": state["battery"],
            "device_name": state["device_name"],
            "address": state["device_address"],
            "serial_number": state.get("serial_number"),
            "is_recording": is_recording,
            "rssi": rssi,
        }
    )


@bp.route("/rssi")
def rssi():
    """Read current RSSI for the connected device via a quick BLE scan."""
    if not dev.is_connected():
        return jsonify({"rssi": None, "error": "Not connected"})
    try:
        rssi_val = dev.run_async(dev.read_rssi())
        return jsonify({"rssi": rssi_val})
    except Exception as e:
        return jsonify({"rssi": None, "error": str(e)})


@bp.route("/scan-known", methods=["POST"])
def scan_known():
    """Scan for a previously paired Plaud device, matched by serial number."""
    try:
        known_sns = dev.get_paired_serials()
        if not known_sns:
            return jsonify({"found": False, "reason": "never_paired"})
        devices = dev.run_async(dev.scan_devices_with_retry(attempts=2, scan_duration=4))
        for d in devices:
            sn = d.get("serial_number", "")
            if sn and sn in known_sns:
                return jsonify(
                    {
                        "found": True,
                        "name": d.get("name", "Plaud Device"),
                        "address": d["address"],
                        "rssi": d.get("rssi", -80),
                        "serial_number": sn,
                    }
                )
        return jsonify({"found": False, "reason": "not_in_range"})
    except Exception as e:
        return jsonify({"found": False, "error": str(e)})


@bp.route("/check-new")
def check_new():
    """Check if device has files not yet synced locally."""
    if not dev.is_connected():
        return jsonify({"new_files": 0})
    try:
        files = dev.run_async(dev.list_device_files())
        RECORDINGS_DIR.mkdir(exist_ok=True)
        existing = {f.stem for f in RECORDINGS_DIR.glob("*.ogg")}
        dismissed = set(load_config().get("deleted_ids", []))
        new_files = [f for f in files if str(f["session_id"]) not in existing and str(f["session_id"]) not in dismissed]
        return jsonify({"new_files": len(new_files), "files": new_files})
    except Exception:
        return jsonify({"new_files": 0})


@bp.route("/files")
def files():
    if not dev.is_connected():
        return jsonify({"error": "Not connected"}), 400
    try:
        result = dev.run_async(dev.list_device_files())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/record", methods=["POST"])
def record():
    if not dev.is_connected():
        return jsonify({"error": "Not connected"}), 400
    data = request.get_json(silent=True) or {}
    seconds = data.get("seconds", 10)
    try:
        dev.run_async(dev.start_recording())

        def record_and_stop():
            time.sleep(seconds)
            try:
                dev.run_async(dev.stop_recording())
            except Exception:
                pass

        t = threading.Thread(target=record_and_stop, daemon=True)
        t.start()
        return jsonify({"recording": True, "seconds": seconds})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/sync", methods=["POST"])
def sync():
    if not dev.is_connected():
        return jsonify({"error": "Not connected"}), 400
    try:
        result = dev.run_async(dev.sync_files())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
