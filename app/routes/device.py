"""Device routes: BLE scanning, connecting, syncing, recording."""

import logging
import traceback

from flask import Blueprint, jsonify, request

from app.state import device as dev
from app.utils import pairing_file, pairing_store
from app.utils.config import load_config
from app.utils.constants import get_all_recordings_dirs


def _error_json(base, exc):
    """Return error JSON with traceback for debugging (auto-reported to GitHub)."""
    base["error"] = str(exc)
    base["traceback"] = traceback.format_exc()
    return base


bp = Blueprint("device", __name__, url_prefix="/device")


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _background_flag(default):
    """The caller's `background` flag: JSON body {"background": true} or ?background=1.

    Automatic syncs (45 s poll, wake, after pairing) are "background"; an
    explicit user Sync is not. Background requests skip sessions that hit the
    retry cap (dev.RETRY_CAP consecutive failed downloads).
    """
    body = request.get_json(silent=True)
    if isinstance(body, dict) and "background" in body:
        return _truthy(body["background"])
    if "background" in request.args:
        return _truthy(request.args["background"])
    return default


def _busy_response():
    """409 returned while a BLE sync owns the link (it holds _ble_lock for its
    whole run, so other BLE calls would just queue behind it)."""
    return jsonify({"error": dev.SYNC_BUSY_ERROR, "busy": True}), 409


@bp.route("/scan", methods=["POST"])
def scan():
    try:
        result = dev.run_async(dev.scan_device())
        return jsonify(result)
    except Exception as e:
        return jsonify(_error_json({"found": False}, e)), 500


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
    serial_number = None
    address = None
    try:
        data = request.get_json(silent=True) or {}
        address = data.get("address")
        serial_number = data.get("serial_number") or dev.paired_serial_for_address(address)
        # Set SN before connect so handshake can look up per-device credentials
        if serial_number:
            dev.set_serial_number(serial_number)
        result = dev.run_async(dev.connect_device(address))
        if result.get("connected"):
            dev.save_known_device(address, result.get("device_name"), serial_number)
        return jsonify(result)
    except Exception as e:
        dev.set_connected(False)
        err = _error_json({"connected": False}, e)
        if serial_number:
            err["device_sn"] = serial_number
        # Translate raw BLE errors to friendly messages for the UI
        raw = str(e).lower()
        if "service discovery" in raw:
            err["error"] = (
                "Couldn't establish a stable connection. Try moving the device closer and restarting Bluetooth."
            )
            err["raw_error"] = str(e)
        elif "not found" in raw:
            err["error"] = "Device went out of range before connecting. Try moving it closer and scanning again."
            err["raw_error"] = str(e)
        elif "timeout" in raw or isinstance(e, TimeoutError):
            # run_async's own timeout is a TimeoutError whose str() is empty.
            err["error"] = "Connection timed out. Try moving the device closer and reconnecting."
            err["raw_error"] = str(e) or type(e).__name__
        return jsonify(err), 500


@bp.route("/disconnect", methods=["POST"])
def disconnect():
    try:
        result = dev.run_async(dev.disconnect_device())
        return jsonify(result)
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


@bp.route("/verify", methods=["POST"])
def verify():
    """Probe the live BLE link (used after system wake). Unlike /status this does
    NOT trust the cached connected flag — it sends a real command. If the link is
    dead (or the lock is wedged by a hung op), state is reset so the caller can
    reconnect.

    During a sync the link is demonstrably alive (the sync holds _ble_lock for
    its whole run), so we answer {connected: true, busy: true} instead of
    probing — the probe's lock timeout used to trigger force_reset() and wipe
    the state mid-transfer."""
    if not dev.is_connected():
        return jsonify({"connected": False})
    if dev.is_syncing():
        return jsonify({"connected": True, "busy": True})
    try:
        ok = dev.run_async(dev.verify_connection(), timeout=10, lock_timeout=5)
        return jsonify({"connected": bool(ok)})
    except Exception:
        # A sync may have started between the check above and the lock wait.
        if dev.is_syncing():
            return jsonify({"connected": True, "busy": True})
        # Couldn't acquire the lock in time (wedged) or the probe errored —
        # treat the link as dead, clear the stale state and drop the old client.
        dev.force_reset()
        return jsonify({"connected": False})


@bp.route("/depair", methods=["POST"])
def depair():
    """Erase & disconnect — unpairs device so it can pair with another host."""
    try:
        result = dev.run_async(dev.depair_device())
        return jsonify(result)
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


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
                    # Recording = an active session exists (sessionId @ bytes 11-14),
                    # NOT the `state` field — measured: idle state=1 / session=0,
                    # recording state=0x1003 / session!=0. The old byte-3 check read
                    # 1 when idle and always reported "recording".
                    sid = (r[11] | (r[12] << 8) | (r[13] << 16) | (r[14] << 24)) if len(r) >= 15 else 0
                    _is_rec = sid != 0
            except Exception:
                pass
            try:
                # Battery + charging from cmd 9 (BattStatusRsp: charging@3, level@4).
                _battery, _chg = await dev.read_battery()
                if _battery is not None:
                    _cmd_ok = True
                    _charging = bool(_chg)
            except Exception:
                pass
            try:
                _rssi = await dev.read_rssi()
            except Exception:
                pass
            return _is_rec, _battery, _charging, _rssi, _cmd_ok

        try:
            # lock_timeout: closes the is_syncing()→run_async race window. If
            # sync acquired _ble_lock between our is_syncing() check and now,
            # bail rather than stall the status poll for the full sync (≤30 min).
            is_recording, live_battery, charging, rssi, cmd_ok = dev.run_async(_poll_device(), lock_timeout=0.5)
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
    """Get device information for the Device panel (storage, firmware, state, battery)."""
    if not dev.is_connected():
        return jsonify({"error": "Not connected"}), 400
    if dev.is_syncing():
        return _busy_response()
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
        devices = dev.run_async(dev.scan_unless_connected(attempts=2, scan_duration=4))
        if devices is None:
            # Connected while this request waited for Bluetooth: nothing to find.
            return jsonify({"found": False, "reason": "already_connected", "connected": True})
        for d in devices:
            sn = d.get("serial_number") or dev.paired_serial_for_address(d["address"])
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
    """Check if device has files not yet synced locally.

    Background by default (every automatic caller uses this count to decide
    whether to start a sync): sessions that failed dev.RETRY_CAP times in a row
    are left out of new_files / total_size / files and listed under
    retry_capped instead, so the poll stops re-triggering syncs for them.
    ?background=0 counts them too.
    """
    background = _background_flag(default=True)
    if not dev.is_connected():
        return jsonify({"new_files": 0})
    if dev.is_syncing():
        return _busy_response()
    try:
        files = dev.run_async(dev.list_device_files())
        rec_dirs = list(get_all_recordings_dirs())
        for rec_dir in rec_dirs:
            rec_dir.mkdir(exist_ok=True)
        dismissed = set(load_config().get("deleted_ids", []))
        # Same "already synced" rule as the sync itself: a complete .ogg in any
        # recordings dir, or (cloud storage mode) uploaded to Google Drive.
        # Partial downloads count as new so they get retried.
        cloud_ids = dev.gdrive_cloud_synced_ids()
        unsynced = [
            f
            for f in files
            if str(f["session_id"]) not in dismissed and not dev.is_already_synced(f["session_id"], rec_dirs, cloud_ids)
        ]
        new_files, retry_capped = [], []
        for f in unsynced:
            if background and dev.is_retry_capped(f["session_id"]):
                retry_capped.append({**f, "attempts": dev.download_failures(f["session_id"])})
            else:
                new_files.append(f)
        total_size = sum(f.get("size", 0) for f in new_files)
        body = {"new_files": len(new_files), "total_size": total_size, "files": new_files}
        if retry_capped:
            body["retry_capped"] = retry_capped
        return jsonify(body)
    except Exception:
        return jsonify({"new_files": 0})


@bp.route("/files")
def files():
    if not dev.is_connected():
        return jsonify({"error": "Not connected"}), 400
    if dev.is_syncing():
        return _busy_response()
    try:
        result = dev.run_async(dev.list_device_files())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/sync", methods=["POST"])
def sync():
    """BLE sync. Body {"background": true} for automatic syncs: sessions at the
    retry cap are skipped (status "skipped"). Without it (the Sync button) every
    unsynced session is retried."""
    if not dev.is_connected():
        return jsonify({"error": "Not connected"}), 400
    background = _background_flag(default=False)
    try:
        # Claims the sync slot before queueing for the BLE lock: a concurrent
        # /sync gets 409 at once instead of running a second full sync later.
        result = dev.run_sync_blocking(timeout=1800, background=background)  # 30 min for large multi-file syncs
        if result is None:
            return _busy_response()
        return jsonify(result)
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


@bp.route("/abort-sync", methods=["POST"])
def abort_sync():
    """Abort current BLE sync (e.g. to switch to WiFi). aborted is False when no
    sync was running (nothing to abort, and no flag is left behind)."""
    return jsonify({"aborted": dev.abort_sync()})


@bp.route("/wifi-sync", methods=["POST"])
def wifi_sync():
    """Sync files via WiFi (user-initiated). Faster but interrupts internet."""
    if not dev.is_connected():
        return jsonify({"error": "Not connected"}), 400
    try:
        # Runs on its own fresh event loop — NOT via run_async (websockets needs clean loop)
        result = dev.do_wifi_sync_blocking()
        return jsonify(result)
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


@bp.route("/wifi-sync-status")
def wifi_sync_status():
    """Poll WiFi sync progress."""
    return jsonify(dev.get_wifi_sync_status())


@bp.route("/wifi-restore-notice/ack", methods=["POST"])
def wifi_restore_notice_ack():
    """The UI has shown the 'some saved networks couldn't be restored' notice."""
    return jsonify({"acknowledged": bool(dev.acknowledge_wifi_restore_notice())})


@bp.route("/sync-status")
def sync_status():
    """Poll BLE sync progress so the UI updates per-file during a transfer."""
    return jsonify(dev.get_ble_sync_status())


# --- Pairing backup (Settings › Device) -------------------------------------
# Responses never carry credentials: device names and serial tails only.


@bp.route("/pairing")
def pairing_status():
    """Paired devices that can be exported, and which are backed up in the Keychain."""
    devices = pairing_file.exportable_devices()
    in_keychain = set(pairing_store.list_serials()) if devices else set()
    summary = pairing_file.summary(devices)
    for row, device in zip(summary, devices, strict=True):
        row["in_keychain"] = device["serial"] in in_keychain
    return jsonify(
        {
            "devices": summary,
            "exportable": bool(devices),
            "keychain_available": pairing_store.available(),
        }
    )


def _pairing_path_arg():
    body = request.get_json(silent=True)
    return body.get("path") if isinstance(body, dict) else None


def _pairing_failure(action, exc):
    if isinstance(exc, pairing_file.PairingFileError):
        return jsonify({"error": str(exc)}), 400
    detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
    logging.warning("[Pairing] %s failed: %s", action, detail)
    return jsonify({"error": f"{action} failed: {detail}"}), 500


@bp.route("/pairing/export", methods=["POST"])
def pairing_export():
    """Write the paired devices' credentials to a JSON file the user chose (mode 0600)."""
    try:
        return jsonify(pairing_file.export_to(_pairing_path_arg()))
    except Exception as e:
        return _pairing_failure("Export", e)


@bp.route("/pairing/import", methods=["POST"])
def pairing_import():
    """Read a pairing export, validate it, and save it to the config and the Keychain."""
    try:
        return jsonify(pairing_file.import_from(_pairing_path_arg()))
    except Exception as e:
        return _pairing_failure("Import", e)
