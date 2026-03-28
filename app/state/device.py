"""BLE device state management and operations."""

import asyncio
import base64
import json
import logging
import struct
import threading
import time
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from app.utils.audio import decrypt_ble_file, wrap_opus_ogg
from app.utils.config import load_config, save_config
from app.utils.constants import (
    APP_KEY_PATH,
    BIND_TOKEN,
    BLE_BATTERY_UUID,
    PROJECT_ROOT,
    RECORDINGS_DIR,
    SESSIONS_DIR,
    SN_SIG,
)
from app.utils.constants import (
    BLE_NOTIFY_UUID as N,
)
from app.utils.constants import (
    BLE_WRITE_UUID as W,
)
from src.crypto import get_public_key_full_pem, load_rsa_private_key, rsa_decrypt

# Module-level state
_device_state = {
    "connected": False,
    "device_name": None,
    "device_address": None,
    "battery": None,
    "client": None,
    "session_keys": None,
    "cipher": None,
    "seq": 0,
    "cmd_evt": None,
    "cmd_resp": None,
    "file_data": None,
    "file_done": None,
    "serial_number": None,
}

_ble_lock = threading.Lock()
_loop = None
_loop_thread = None


def _reset_state():
    """Reset all connection state to disconnected defaults."""
    _device_state["connected"] = False
    _device_state["client"] = None
    _device_state["session_keys"] = None
    _device_state["cipher"] = None
    _device_state["battery"] = None
    _device_state["device_name"] = None
    _device_state["rssi"] = None
    _device_state["seq"] = 0
    _device_state["cmd_evt"] = None
    _device_state["cmd_resp"] = None
    _device_state["file_data"] = None
    _device_state["file_done"] = None


def _on_disconnect(client):
    """Callback fired by bleak when BLE connection drops unexpectedly."""
    logging.warning("BLE device disconnected unexpectedly")
    _reset_state()


def get_state():
    """Get a read-only snapshot of device state."""
    return {
        "connected": _device_state["connected"],
        "device_name": _device_state["device_name"],
        "device_address": _device_state["device_address"],
        "battery": _device_state["battery"],
        "serial_number": _device_state.get("serial_number"),
        "rssi": _device_state.get("rssi"),
    }


async def read_rssi():
    """Read RSSI for the connected device via CoreBluetooth's readRSSI."""
    cl = _device_state.get("client")
    if not cl or not _device_state["connected"]:
        return None
    try:
        backend = getattr(cl, "_backend", cl)
        rssi = await backend.get_rssi()
        if rssi is not None:
            _device_state["rssi"] = int(rssi)
        return _device_state.get("rssi")
    except Exception:
        return _device_state.get("rssi")


def _get_loop():
    """Get or create a dedicated asyncio event loop running in a background thread."""
    global _loop, _loop_thread
    if _loop is not None and _loop.is_running():
        return _loop
    _loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()

    _loop_thread = threading.Thread(target=run, daemon=True)
    _loop_thread.start()
    return _loop


def run_async(coro):
    """Run an async coroutine from sync Flask context. Serialized via _ble_lock."""
    with _ble_lock:
        loop = _get_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=120)


async def scan_devices(scan_duration=6):
    """Scan for all Plaud devices in range, returning detailed info for each."""
    from bleak import BleakScanner

    from src.scanner import parse_manufacturer_data

    found = []
    seen_addresses = set()

    def callback(device, adv):
        name = device.name or adv.local_name or ""
        is_plaud = any(p in name.upper() for p in ["PLAUD", "IZYREC", "NOTEPIN"])
        mfr_info = parse_manufacturer_data(adv.manufacturer_data) if adv.manufacturer_data else None

        if is_plaud or mfr_info or (0x5D in (adv.manufacturer_data or {})):
            if device.address not in seen_addresses:
                seen_addresses.add(device.address)
                info = mfr_info or {}
                info["name"] = name or "Plaud Device"
                info["address"] = device.address
                info["rssi"] = adv.rssi
                found.append(info)

    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    await asyncio.sleep(scan_duration)
    await scanner.stop()

    found.sort(key=lambda d: d.get("rssi", -100), reverse=True)
    return found


async def scan_devices_with_retry(attempts=2, scan_duration=6):
    """Scan with retry — if first scan finds nothing, try again."""
    for i in range(attempts):
        devices = await scan_devices(scan_duration)
        if devices:
            return devices
        if i < attempts - 1:
            await asyncio.sleep(1)
    return []


async def scan_device():
    """Scan for a Plaud device — tries paired devices first, then full scan."""
    # Try paired devices first (fast path)
    known_sns = get_paired_serials()
    if known_sns:
        devices = await scan_devices_with_retry(attempts=2, scan_duration=4)
        for d in devices:
            sn = d.get("serial_number", "")
            if sn and sn in known_sns:
                return {
                    "found": True,
                    "name": d.get("name", "Plaud NotePin S"),
                    "address": d["address"],
                    "rssi": d.get("rssi", -60),
                    "serial_number": sn,
                }
        # Paired device not found — fall through to return strongest
        if devices:
            d = devices[0]
            return {
                "found": True,
                "name": d.get("name", "Plaud NotePin S"),
                "address": d["address"],
                "rssi": d.get("rssi", -60),
            }
    else:
        # No paired devices — standard scan with retry
        devices = await scan_devices_with_retry(attempts=2, scan_duration=6)
        if devices:
            d = devices[0]
            return {
                "found": True,
                "name": d.get("name", "Plaud NotePin S"),
                "address": d["address"],
                "rssi": d.get("rssi", -60),
            }
    return {"found": False}


async def connect_device(address=None):
    """Connect to device, perform handshake, return status."""

    # Clean up any stale connection first
    if _device_state.get("client"):
        try:
            await disconnect_device()
        except Exception:
            _reset_state()

    if not address:
        scan_result = await scan_device()
        if not scan_result["found"]:
            return {"connected": False, "error": "Device not found"}
        address = scan_result["address"]
        _device_state["device_name"] = scan_result["name"]
    _device_state["device_address"] = address

    # Connect with disconnect callback
    from bleak import BleakClient as BC

    cl = BC(address, timeout=30.0, disconnected_callback=_on_disconnect)
    await cl.connect()
    await asyncio.sleep(1)
    if not cl.services:
        await cl.get_services()
    _device_state["client"] = cl

    # Handshake
    ack = asyncio.Event()
    rsa = {}
    done = asyncio.Event()

    def h(s, d):
        d = bytes(d)
        cid = struct.unpack_from("<H", d, 0)[0] if len(d) >= 2 else 0
        if cid == 65041:
            ack.set()
        elif cid == 65042 and len(d) >= 4:
            idx = d[3]
            if idx not in rsa:
                rsa[idx] = d[4:]
            if len(rsa) >= d[2]:
                done.set()

    await cl.start_notify(N, h)
    sig = base64.b64decode(SN_SIG)
    tc = (len(sig) + 99) // 100
    for _ in range(2):
        for i in range(tc):
            await cl.write_gatt_char(
                W, struct.pack("<H", 65040) + bytes([tc, i]) + sig[i * 100 : (i + 1) * 100], response=False
            )
            await asyncio.sleep(0.02)
    for _ in range(50):
        await asyncio.sleep(0.1)
        if ack.is_set() or done.is_set():
            break
    if ack.is_set() and not done.is_set():
        pem = get_public_key_full_pem().encode()
        pc = (len(pem) + 99) // 100
        for i in range(pc):
            await cl.write_gatt_char(
                W, struct.pack("<H", 65042) + bytes([pc, i]) + pem[i * 100 : (i + 1) * 100], response=True
            )
            await asyncio.sleep(0.02)
        await asyncio.wait_for(done.wait(), timeout=15)
    elif not done.is_set():
        await cl.stop_notify(N)
        return {"connected": False, "error": "Handshake failed - device did not respond"}

    blob = bytearray()
    for i in sorted(rsa.keys()):
        blob.extend(rsa[i])
    dec = rsa_decrypt(bytes(blob), load_rsa_private_key())
    key, nonce, aad, ct = dec[:32], dec[32:44], dec[44:56], dec[56:]
    assert ChaCha20Poly1305(key).decrypt(nonce, ct, aad) == b"PLAUD.AI"
    await cl.stop_notify(N)

    # Save session
    SESSIONS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json.dump(
        {
            "key": key.hex(),
            "nonce": nonce.hex(),
            "aad": aad.hex(),
            "status": "ACTIVE",
            "timestamp": datetime.now().isoformat(),
        },
        open(str(SESSIONS_DIR / f"session_{ts}.json"), "w"),
        indent=2,
    )

    _device_state["session_keys"] = (key, nonce, aad)
    cipher = ChaCha20Poly1305(key)
    _device_state["cipher"] = cipher
    _device_state["seq"] = 0

    # Set up encrypted command channel
    cmd_evt = asyncio.Event()
    cmd_resp = [None]
    file_data = bytearray()
    file_done = asyncio.Event()

    def handler(s, d):
        try:
            dd = cipher.decrypt(nonce, bytes(d), aad)[4:]
        except Exception:
            return
        if len(dd) < 1:
            return
        if dd[0] == 2 and len(dd) >= 10:
            off = struct.unpack_from("<I", dd, 5)[0]
            if off == 0xFFFFFFFF:
                file_done.set()
            else:
                file_data.extend(dd[10 : 10 + dd[9]])
        elif dd[0] == 1:
            cid = struct.unpack_from("<H", dd, 1)[0] if len(dd) >= 3 else 0
            if cid == 29:
                file_done.set()
            cmd_resp[0] = dd
            cmd_evt.set()

    await cl.start_notify(N, handler)
    _device_state["cmd_evt"] = cmd_evt
    _device_state["cmd_resp"] = cmd_resp
    _device_state["file_data"] = file_data
    _device_state["file_done"] = file_done
    _device_state["connected"] = True

    # Initial commands
    token32 = BIND_TOKEN[:32].encode("ascii")
    await send_cmd(bytes([1, 1, 0, 2, 0, 0]) + token32)
    await send_cmd(bytes([1, 1, 0, 2, 0, 1]) + token32)
    await send_cmd(bytes([1, 4, 0]) + struct.pack("<I", int(time.time())) + bytes([0, 0]))
    await send_cmd(bytes([1, 3, 0]), timeout=3)
    await send_cmd(bytes([1, 6, 0]), timeout=3)

    # Read battery
    battery = None
    try:
        bat = await cl.read_gatt_char(BLE_BATTERY_UUID)
        battery = bat[0]
    except Exception:
        pass
    _device_state["battery"] = battery

    return {
        "connected": True,
        "battery": battery,
        "device_name": _device_state["device_name"] or "Plaud NotePin S",
    }


async def send_cmd(pt, timeout=5):
    """Send an encrypted command and wait for response."""
    cl = _device_state["client"]
    cipher = _device_state["cipher"]
    key, nonce, aad = _device_state["session_keys"]
    _device_state["seq"] += 1
    ct = cipher.encrypt(nonce, struct.pack("<I", _device_state["seq"]) + pt, aad)
    _device_state["cmd_evt"].clear()
    _device_state["cmd_resp"][0] = None
    await cl.write_gatt_char(W, ct, response=False)
    try:
        await asyncio.wait_for(_device_state["cmd_evt"].wait(), timeout=timeout)
    except TimeoutError:
        pass
    return _device_state["cmd_resp"][0]


async def disconnect_device():
    """Disconnect from BLE device (keeps pairing)."""
    cl = _device_state["client"]
    if cl:
        try:
            await cl.stop_notify(N)
        except Exception:
            pass
        try:
            await cl.disconnect()
        except Exception:
            pass
    _reset_state()
    return {"disconnected": True}


async def depair_device():
    """Send depair command (erase & disconnect)."""
    cl = _device_state["client"]
    if not _device_state["connected"] or not _device_state["cipher"] or not cl:
        return {"error": "Not connected"}

    depair_ok = False
    depair_verified = False

    try:
        r = await send_cmd(bytes([1, 5, 0]), timeout=5)
        depair_ok = r is not None
    except Exception:
        pass

    try:
        await cl.stop_notify(N)
    except Exception:
        pass
    await asyncio.sleep(0.5)

    try:
        verify_ack = asyncio.Event()

        def verify_handler(sender, data):
            data = bytes(data)
            if len(data) >= 2:
                cid = struct.unpack_from("<H", data, 0)[0]
                if cid == 65041:
                    verify_ack.set()

        await cl.start_notify(N, verify_handler)
        sig = base64.b64decode(SN_SIG)
        tc = (len(sig) + 99) // 100
        for i in range(tc):
            await cl.write_gatt_char(
                W, struct.pack("<H", 65040) + bytes([tc, i]) + sig[i * 100 : (i + 1) * 100], response=False
            )
            await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(verify_ack.wait(), timeout=5)
            depair_verified = True
        except TimeoutError:
            depair_verified = False

        await cl.stop_notify(N)
    except Exception as e:
        logging.warning(f"Depair verification error: {e}")

    await asyncio.sleep(1)
    await disconnect_device()

    if SESSIONS_DIR.exists():
        for sf in SESSIONS_DIR.glob("session_*.json"):
            try:
                sf.unlink()
            except Exception:
                pass

    config = load_config()
    config["paired_devices"] = {}
    config.pop("known_devices_by_sn", None)
    config.pop("known_devices", None)
    save_config(config)

    sk_path = PROJECT_ROOT / "session_keys.json"
    if sk_path.exists():
        try:
            sk_path.unlink()
        except Exception:
            pass

    return {"depaired": True, "depair_response": depair_ok, "depair_verified": depair_verified}


async def list_device_files():
    """List files on the connected device."""
    r = await send_cmd(bytes([1, 26, 0]), timeout=10)
    files = []
    if r and len(r) >= 11:
        struct.unpack_from("<H", r, 7)[0]
        offset = 11
        while offset + 10 <= len(r):
            sid = struct.unpack_from("<I", r, offset)[0]
            fsize = struct.unpack_from("<I", r, offset + 4)[0]
            if 0 < fsize < 100_000_000:
                duration_est = (fsize - 512) / 80 * 0.02 if fsize > 512 else 0
                files.append(
                    {
                        "session_id": sid,
                        "size": fsize,
                        "duration_est": round(duration_est, 1),
                    }
                )
            offset += 10
    return files


async def get_device_info():
    """Query all available device information in one call."""
    info = {}

    # cmd 2 — Get SSN (serial number + firmware version)
    try:
        r = await send_cmd(bytes([1, 2, 0]), timeout=3)
        if r and len(r) >= 10:
            # Serial number: null-terminated string starting at offset 3 (relative to payload)
            # But our send_cmd strips the cmd_type prefix, so payload starts at byte 0
            # Actually send_cmd returns the decrypted payload dd[1:] — let's check
            # The handler strips the first byte (dd[0] = type), so we get from byte 1 onward
            # cmd_resp = dd = decrypted[4:] then response is dd
            # dd[0] = type (1=cmd, 2=file), dd[1:3] = cmd_id, dd[3:] = payload
            # So offset 3 in raw = index 3 in our response
            sn_bytes = r[3:59] if len(r) >= 59 else r[3:]
            null_idx = sn_bytes.find(0)
            if null_idx >= 0:
                sn_bytes = sn_bytes[:null_idx]
            sn = bytes(sn_bytes).decode("ascii", errors="ignore").strip("\x00")
            if sn:
                info["serial_number_full"] = sn

            if len(r) >= 63:
                ver_num = r[60] | (r[61] << 8) | (r[62] << 16)
                # Version is typically encoded as major*65536 + minor*256 + patch
                # e.g. 65546 = 1*65536 + 0*256 + 10 = v1.0.10
                major = (ver_num >> 16) & 0xFF
                minor = (ver_num >> 8) & 0xFF
                patch = ver_num & 0xFF
                info["firmware_version"] = f"{major}.{minor}.{patch}"
                info["firmware_version_raw"] = ver_num
    except Exception:
        pass

    # cmd 3 — Get State
    try:
        r = await send_cmd(bytes([1, 3, 0]), timeout=3)
        if r and len(r) >= 8:
            state_val = struct.unpack_from("<I", r, 3)[0] if len(r) >= 7 else 0
            info["recording_state"] = state_val
            # state_val: 0=idle, 1=recording, 2=paused, 3=resuming
            info["is_recording"] = state_val in (1, 3)
            info["is_paused"] = state_val == 2
            if len(r) >= 8:
                info["privacy_enabled"] = bool(r[7])
            if len(r) >= 9:
                info["key_state"] = r[8]
                info["button_pressed"] = r[8] == 1
            if len(r) >= 10:
                info["usb_connected"] = bool(r[9])
            if len(r) >= 11:
                scene = r[10]
                info["scene"] = scene
                scene_names = {0: "Idle", 1: "Recording", 2: "Meeting", 3: "Call", 4: "Lecture"}
                info["scene_name"] = scene_names.get(scene, f"Mode {scene}")
            if len(r) >= 15:
                info["active_session_id"] = struct.unpack_from("<I", r, 11)[0]
            if len(r) >= 16:
                info["find_my_state"] = r[15]
                info["find_my_enabled"] = r[15] > 0
    except Exception:
        pass

    # cmd 6 — Storage
    try:
        r = await send_cmd(bytes([1, 6, 0]), timeout=3)
        if r and len(r) >= 19:
            free_bytes = struct.unpack_from("<Q", r, 3)[0]
            total_bytes = struct.unpack_from("<Q", r, 11)[0]
            info["storage_free_bytes"] = free_bytes
            info["storage_total_bytes"] = total_bytes
            info["storage_used_bytes"] = total_bytes - free_bytes
            if total_bytes > 0:
                info["storage_used_pct"] = round((total_bytes - free_bytes) / total_bytes * 100, 1)
            # Estimate remaining recording time (80 bytes per 20ms frame = 4000 bytes/sec)
            info["storage_remaining_hours"] = round(free_bytes / 4000 / 3600, 1)
            if len(r) >= 27:
                info["storage_field3_raw"] = struct.unpack_from("<Q", r, 19)[0]
    except Exception:
        pass

    # cmd 9 — Battery/charging status
    try:
        r = await send_cmd(bytes([1, 9, 0]), timeout=3)
        if r and len(r) >= 5:
            info["charging"] = bool(r[3])
            info["battery_level"] = r[4]
    except Exception:
        pass

    # cmd 36 — Accelerometer / orientation
    try:
        r = await send_cmd(bytes([1, 36, 0]), timeout=3)
        if r and len(r) >= 15:
            pitch = struct.unpack_from("<i", r, 3)[0]
            roll = struct.unpack_from("<i", r, 7)[0]
            yaw = struct.unpack_from("<i", r, 11)[0]
            info["accelerometer"] = {"pitch": pitch, "roll": roll, "yaw": yaw}
    except Exception:
        pass

    return info


async def set_find_my(enabled):
    """Enable or disable Find My on the device (cmd 109)."""
    r = await send_cmd(bytes([1, 109, 0, 1 if enabled else 0]), timeout=3)
    # Read back to confirm
    if r and len(r) >= 4:
        return {"find_my_enabled": bool(r[3])}
    return {"find_my_enabled": enabled}


async def delete_device_file(session_id):
    """Delete a file from the device by session ID (cmd 31)."""
    r = await send_cmd(
        bytes([1, 31, 0]) + struct.pack("<I", session_id),
        timeout=5,
    )
    if r and len(r) >= 8:
        status = r[7] if len(r) > 7 else r[3]
        return {"deleted": status == 0, "session_id": session_id}
    return {"deleted": True, "session_id": session_id}


async def start_recording():
    """Start recording on the device."""
    await send_cmd(bytes([1, 20, 0]))
    return {"recording": True}


async def stop_recording():
    """Stop recording on the device."""
    await send_cmd(bytes([1, 23, 0]))
    return {"recording": False}


async def sync_files(progress_callback=None):
    """Download all files from device, decrypt, save as .ogg."""
    if not APP_KEY_PATH.exists():
        return {"error": f"Missing {APP_KEY_PATH}"}
    app_rsa_key = load_pem_private_key(APP_KEY_PATH.read_bytes(), password=None)

    files = await list_device_files()
    if not files:
        return {"files": [], "message": "No files on device"}

    RECORDINGS_DIR.mkdir(exist_ok=True)
    new_files = []

    dismissed = set(load_config().get("deleted_ids", []))
    for i, f in enumerate(files):
        sid = f["session_id"]
        fsize = f["size"]
        ogg_path = RECORDINGS_DIR / f"{sid}.ogg"

        if str(sid) in dismissed:
            continue

        if ogg_path.exists():
            new_files.append(
                {
                    "session_id": sid,
                    "ogg_path": str(ogg_path),
                    "duration": f["duration_est"],
                    "status": "exists",
                }
            )
            continue

        # Download via BLE
        file_data = _device_state["file_data"]
        file_done = _device_state["file_done"]
        file_data.clear()
        file_done.clear()

        cipher = _device_state["cipher"]
        key, nonce, aad = _device_state["session_keys"]
        _device_state["seq"] += 1
        sync_cmd = bytes([1, 28, 0]) + struct.pack("<I", sid) + struct.pack("<I", 0) + struct.pack("<I", fsize)
        ct = cipher.encrypt(nonce, struct.pack("<I", _device_state["seq"]) + sync_cmd, aad)
        await _device_state["client"].write_gatt_char(W, ct, response=False)

        start = time.time()
        max_time = max(fsize / 3000, 30)
        while not file_done.is_set():
            try:
                await asyncio.wait_for(file_done.wait(), timeout=2)
            except TimeoutError:
                if time.time() - start > max_time:
                    break

        if not file_data:
            continue

        raw_data = bytes(file_data)
        opus_frames = decrypt_ble_file(raw_data, app_rsa_key)
        if opus_frames:
            ogg = wrap_opus_ogg(opus_frames)
            ogg_path.write_bytes(ogg)
            duration = len(opus_frames) // 80 * 0.02
            new_files.append(
                {
                    "session_id": sid,
                    "ogg_path": str(ogg_path),
                    "duration": round(duration, 1),
                    "status": "new",
                }
            )
        else:
            raw_path = RECORDINGS_DIR / f"{sid}_encrypted.bin"
            raw_path.write_bytes(raw_data)
            new_files.append(
                {
                    "session_id": sid,
                    "ogg_path": None,
                    "duration": 0,
                    "status": "decrypt_failed",
                }
            )

    # Auto-delete synced files from device if enabled
    auto_delete = load_config().get("auto_delete_after_sync", False)
    deleted_from_device = []
    if auto_delete:
        for f in new_files:
            if f["status"] == "new" and f.get("session_id"):
                try:
                    await delete_device_file(f["session_id"])
                    deleted_from_device.append(f["session_id"])
                except Exception:
                    pass

    return {"files": new_files, "deleted_from_device": deleted_from_device}


def save_known_device(address, name=None, serial_number=None):
    """Remember a device we've successfully paired with from THIS app."""
    sn = serial_number or _device_state.get("serial_number")
    if not sn or sn == "unknown":
        return
    config = load_config()
    paired = config.get("paired_devices", {})
    paired[sn] = {
        "name": name or "Plaud Device",
        "last_address": address,
        "last_connected": datetime.now().isoformat(),
    }
    config["paired_devices"] = paired
    save_config(config)


def get_paired_serials():
    """Get serial numbers of devices we've successfully paired with."""
    config = load_config()
    return set(config.get("paired_devices", {}).keys())


def set_serial_number(sn):
    """Set the serial number for the currently connected device."""
    _device_state["serial_number"] = sn


def is_connected():
    return _device_state["connected"]


def set_connected(val):
    _device_state["connected"] = val
