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
    battery = state["battery"]
    charging = False
    syncing = dev.is_syncing()
    if state["connected"] and not syncing:
        # Combine state + battery + RSSI in a single async call
        # Skip when syncing — BLE commands would interfere with file transfer
        async def _poll_device():
            _is_rec = False
            _rssi = None
            _battery = None
            _charging = False
            _cmd_ok = False
            try:
                r = await dev.send_cmd(bytes([1, 3, 0]), timeout=3)
                if r and len(r) >= 5:
                    _cmd_ok = True
                    state_byte = r[3] if len(r) > 3 else 0
                    _is_rec = state_byte in (1, 2, 3)
            except Exception:
                pass
            try:
                r = await dev.send_cmd(bytes([1, 9, 0]), timeout=3)
                if r and len(r) >= 5:
                    _cmd_ok = True
                    _charging = bool(r[3])
                    _battery = r[4]
            except Exception:
                pass
            try:
                _rssi = await dev.read_rssi()
            except Exception:
                pass
            return _is_rec, _battery, _charging, _rssi, _cmd_ok

        try:
            is_recording, live_battery, charging, rssi, cmd_ok = dev.run_async(_poll_device())
            if live_battery is not None:
                battery = live_battery
            # If all commands failed and no RSSI, device is gone — mark disconnected
            if not cmd_ok and rssi is None and not dev.is_connected():
                # send_cmd already reset state via _reset_state()
                state = dev.get_state()
            elif not cmd_ok and rssi is None:
                logging.warning("All BLE commands timed out — marking device as disconnected")
                dev.set_connected(False)
                state = dev.get_state()
        except Exception:
            pass

    return jsonify(
        {
            "connected": state["connected"],
            "battery": battery,
            "charging": charging,
            "device_name": state["device_name"],
            "address": state["device_address"],
            "serial_number": state.get("serial_number"),
            "is_recording": is_recording,
            "rssi": rssi,
            "syncing": syncing,
        }
    )


@bp.route("/find-my", methods=["POST"])
def find_my():
    """Enable or disable Find My on the device."""
    if not dev.is_connected():
        return jsonify({"error": "Not connected"}), 400
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", False)
    try:
        result = dev.run_async(dev.set_find_my(enabled))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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


@bp.route("/info")
def info():
    """Get comprehensive device information (storage, firmware, state, accelerometer)."""
    if not dev.is_connected():
        return jsonify({"error": "Not connected"}), 400
    try:
        data = dev.run_async(dev.get_device_info())
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        total_size = sum(f.get("size", 0) for f in new_files)
        return jsonify({"new_files": len(new_files), "total_size": total_size, "files": new_files})
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
        result = dev.run_async(dev.sync_files(), timeout=1800)  # 30 min for large multi-file syncs
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/wifi-sync", methods=["POST"])
def wifi_sync():
    """Sync files via WiFi (user-initiated). Faster but interrupts internet."""
    if not dev.is_connected():
        return jsonify({"error": "Not connected"}), 400
    try:
        result = dev.run_async(dev.do_wifi_sync(), timeout=600)  # 10 min max
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
