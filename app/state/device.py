"""BLE device state management and operations."""

import asyncio
import base64
import json
import logging
import os
import struct
import sys
import threading
import time
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from app.utils.audio import decrypt_ble_file, wrap_opus_ogg
from app.utils.config import load_config, save_config
from app.utils.constants import (
    APP_KEY_PATH,
    BLE_BATTERY_UUID,
    DATA_DIR,
    PROJECT_ROOT,
    RECORDINGS_DIR,
    SESSIONS_DIR,
    get_recordings_dir,
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
_syncing = threading.Lock()  # Held while a file sync is in progress — blocks concurrent syncs
_sync_abort = False  # Set True to abort current BLE sync (e.g. switching to WiFi)
_wifi_sync_status = {"message": "", "percent": 0}  # Progress for WiFi sync polling
# Progress for BLE sync polling — the frontend reads this via /device/sync-status
# so the UI updates per-file instead of freezing on one blocking call.
_ble_sync_status = {"message": "", "percent": 0, "active": False}
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
    _device_state["expected_cmd_id"] = None
    _device_state["cmd_evt"] = None
    _device_state["cmd_resp"] = None
    _device_state["file_data"] = None
    _device_state["file_done"] = None


def _on_disconnect(client):
    """Callback fired by bleak when BLE connection drops unexpectedly."""
    logging.warning("BLE device disconnected unexpectedly")
    _reset_state()


async def _setup_notify(cl):
    """Re-register the BLE notification handler using existing state."""
    cipher = _device_state["cipher"]
    key, nonce, aad = _device_state["session_keys"]
    cmd_evt = _device_state["cmd_evt"]
    cmd_resp = _device_state["cmd_resp"]
    file_data = _device_state["file_data"]
    file_done = _device_state["file_done"]

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
            # cmd_id 26 = file list response (may span multiple packets)
            if cid == 26:
                _device_state.setdefault("file_list_packets", []).append(bytes(dd))
            # Only wake send_cmd() if this is the response it's waiting for.
            expected = _device_state.get("expected_cmd_id")
            if expected is None or cid == expected:
                cmd_resp[0] = dd
                cmd_evt.set()

    await cl.start_notify(N, handler)


def get_state():
    """Get a read-only snapshot of device state."""
    return {
        "connected": _device_state["connected"],
        "device_name": _device_state["device_name"],
        "device_address": _device_state["device_address"],
        "battery": _device_state["battery"],
        "charging": _device_state.get("charging", False),
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


async def read_battery():
    """Read battery % + charging from cmd 9 (BattStatusRsp: charging@3, level@4).

    cmd 9 is the source the official app uses, and the cmd_id matching in
    send_cmd() ensures we get the cmd-9 reply rather than an unrelated
    notification (the bug that previously produced false readings).

    The standard 0x2A19 GATT characteristic is deliberately NOT used: on this
    device its read_gatt_char hangs indefinitely, which pinned the value to the
    connect-time reading and stalled the BLE channel. cmd 9 goes through
    send_cmd (3s timeout), so it can neither hang nor stall the lock.

    Returns (level, charging), or (None, None) on failure.
    """
    if not _device_state["connected"]:
        return None, None
    try:
        r = await send_cmd(bytes([1, 9, 0]), timeout=3)
        if r and len(r) >= 5:
            level = r[4]
            charging = bool(r[3])
            _device_state["battery"] = level
            _device_state["charging"] = charging
            return level, charging
    except Exception as e:
        logging.debug(f"Battery read (cmd 9) failed: {e}")
    return None, None


class _keep_awake:
    """Context manager that disables macOS App Nap for its duration.

    When the app window loses focus, macOS throttles the process and coalesces
    timers, which delays CoreBluetooth notification delivery. During a long BLE
    file transfer that stall can exceed the watchdog window and abort the sync.
    NSProcessInfo's begin/endActivity keeps the process responsive while syncing.
    No-op on non-macOS or if pyobjc Foundation is unavailable.
    """

    # NSActivityUserInitiated — user-initiated work; disables App Nap and idle sleep.
    _OPTIONS = 0x00FFFFFF

    def __init__(self, reason="Plaud BLE sync"):
        self._reason = reason
        self._token = None

    def __enter__(self):
        try:
            from Foundation import NSProcessInfo

            self._token = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
                self._OPTIONS, self._reason
            )
        except Exception as e:
            logging.debug(f"App Nap suppression unavailable: {e}")
            self._token = None
        return self

    def __exit__(self, *exc):
        if self._token is not None:
            try:
                from Foundation import NSProcessInfo

                NSProcessInfo.processInfo().endActivity_(self._token)
            except Exception:
                pass
            self._token = None
        return False


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


def run_async(coro, timeout=120, lock_timeout=None):
    """Run an async coroutine from sync Flask context. Serialized via _ble_lock.

    `lock_timeout` lets callers fail fast instead of stalling behind a
    long-running operation (e.g. a 30-min sync that holds _ble_lock for the
    full duration). Used by the /status poll so the UI doesn't freeze on the
    `is_syncing()` → `run_async()` window race.
    """
    if lock_timeout is not None:
        if not _ble_lock.acquire(timeout=lock_timeout):
            raise TimeoutError("BLE busy — lock held by long-running operation")
        try:
            loop = _get_loop()
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=timeout)
        finally:
            _ble_lock.release()
    else:
        with _ble_lock:
            loop = _get_loop()
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=timeout)


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

    # Connect with retry — includes service discovery (bleak 3.x does it inside connect())
    # Note: cl.services is a property that RAISES BleakError if services aren't discovered,
    # so we must use _has_services() to check safely.
    def _has_services(client):
        try:
            return bool(client.services)
        except Exception:
            return False

    cl = None
    for attempt in range(3):
        try:
            cl = BC(address, timeout=30.0, disconnected_callback=_on_disconnect)
            await cl.connect()
            await asyncio.sleep(1)

            if _has_services(cl):
                break  # Success — services discovered

            # Services empty after connect — disconnect and retry
            print(f"[BLE] Connect attempt {attempt + 1}/3: connected but service discovery failed, retrying...")
            try:
                await cl.disconnect()
            except Exception:
                pass
            cl = None
            await asyncio.sleep(2 + attempt)

        except Exception as e:
            err_msg = str(e).lower()
            print(f"[BLE] Connect attempt {attempt + 1}/3 failed: {e}")
            if cl:
                try:
                    await cl.disconnect()
                except Exception:
                    pass
                cl = None

            if attempt < 2 and "not found" in err_msg:
                await asyncio.sleep(2)
                scan_result = await scan_device()
                if scan_result["found"]:
                    address = scan_result["address"]
                else:
                    raise
            elif attempt == 2:
                raise
            else:
                await asyncio.sleep(2 + attempt)

    if not cl or not _has_services(cl):
        if cl:
            try:
                await cl.disconnect()
            except Exception:
                pass
        sn = _device_state.get("serial_number", "unknown")
        return {"connected": False, "error": f"Service discovery failed after 3 connection attempts (device: {sn}). Try moving the device closer and reconnecting."}

    _device_state["client"] = cl

    # Handshake — wrapped to catch BLE disconnects mid-handshake
    try:
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
        # Get per-device SN_SIG from cloud API (stored in config)
        # If not cached, try fetching from cloud on the spot
        from app.state.cloud import get_device_sn_signature, get_device_bind_token, is_logged_in, fetch_sn_signature, bind_device
        sn = _device_state.get("serial_number", "")
        sn_sig_b64 = get_device_sn_signature(sn) if sn else None
        if not sn_sig_b64 and sn and is_logged_in():
            try:
                bind_result = bind_device(sn)
                sig_result = fetch_sn_signature(sn)
                sn_sig_b64 = get_device_sn_signature(sn)
                if not sn_sig_b64:
                    logging.warning(f"Auto-fetch failed: bind={bind_result}, sig={sig_result}")
            except Exception as e:
                logging.warning(f"Auto-fetch exception: {e}")
        if not sn_sig_b64:
            try:
                await cl.stop_notify(N)
            except Exception:
                pass
            # Specific error message depending on what's missing
            if not sn:
                return {"connected": False, "error": "Device serial number unknown. Try scanning for the device first."}
            if not is_logged_in():
                return {"connected": False, "error": "Not logged into Plaud Cloud. Go to Settings → Plaud Cloud to sign in, then try connecting again."}
            return {"connected": False, "error": f"Failed to get device credentials from Plaud Cloud for SN {sn}. Check your cloud account and try again."}
        sig = base64.b64decode(sn_sig_b64)
        tc = (len(sig) + 99) // 100
        # Send SN_SIG 3 times for reliability on weak connections
        for _ in range(3):
            for i in range(tc):
                await cl.write_gatt_char(
                    W, struct.pack("<H", 65040) + bytes([tc, i]) + sig[i * 100 : (i + 1) * 100], response=False
                )
                await asyncio.sleep(0.02)
        # Wait up to 10 seconds for device response (was 5s — too tight for weak signals)
        for _ in range(100):
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
            try:
                await cl.stop_notify(N)
            except Exception:
                pass
            return {"connected": False, "error": "Handshake failed — device did not respond. Try moving it closer and reconnecting."}

        blob = bytearray()
        for i in sorted(rsa.keys()):
            blob.extend(rsa[i])
        dec = rsa_decrypt(bytes(blob), load_rsa_private_key())
        key, nonce, aad, ct = dec[:32], dec[32:44], dec[44:56], dec[56:]
        if ChaCha20Poly1305(key).decrypt(nonce, ct, aad) != b"PLAUD.AI":
            raise ValueError("BLE handshake identity check failed: decrypted token mismatch")
        try:
            await cl.stop_notify(N)
        except Exception:
            pass
    except Exception as e:
        _reset_state()
        err_msg = str(e)
        # Translate raw BLE errors to user-friendly messages
        if "service discovery" in err_msg.lower():
            return {"connected": False, "error": "Connection was lost during setup. Try moving the device closer and reconnecting."}
        if "disconnected" in err_msg.lower() or "not connected" in err_msg.lower():
            return {"connected": False, "error": "Device disconnected during setup. Try reconnecting."}
        if "timeout" in err_msg.lower():
            return {"connected": False, "error": "Device took too long to respond. Try moving it closer and reconnecting."}
        return {"connected": False, "error": f"Connection failed during handshake: {err_msg}"}

    # Save session (atomic write — prevents corruption on mid-write crash)
    SESSIONS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_path = str(SESSIONS_DIR / f"session_{ts}.json")
    tmp_path = session_path + ".tmp"
    session_data = {
        "key": key.hex(),
        "nonce": nonce.hex(),
        "aad": aad.hex(),
        "status": "ACTIVE",
        "timestamp": datetime.now().isoformat(),
    }
    try:
        with open(tmp_path, "w") as f:
            json.dump(session_data, f, indent=2)
        os.replace(tmp_path, session_path)
    except Exception as e:
        logging.warning("Failed to save session: %s", e)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

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
        except Exception as e:
            logging.warning(f"[BLE] Notification decrypt failed ({len(d)}B): {e}")
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
            logging.debug(f"[BLE] cmd response: cmd_id={cid}, len={len(dd)}")
            if cid == 29:
                file_done.set()
            # cmd_id 26 = file list response (may span multiple packets)
            if cid == 26:
                _device_state.setdefault("file_list_packets", []).append(bytes(dd))
            # Only wake send_cmd() if this is the response it's waiting for.
            # Unsolicited cmd_type==1 packets (record events, late replies) carry a
            # different cmd_id and must not be returned as the current command's reply.
            expected = _device_state.get("expected_cmd_id")
            if expected is None or cid == expected:
                cmd_resp[0] = dd
                cmd_evt.set()

    await cl.start_notify(N, handler)
    _device_state["cmd_evt"] = cmd_evt
    _device_state["cmd_resp"] = cmd_resp
    _device_state["file_data"] = file_data
    _device_state["file_done"] = file_done
    _device_state["connected"] = True

    # Send BIND_TOKEN handshake (cmd_id=1) — required for device to accept commands.
    # Format matches the official Android app (nh/a0.java) and the WiFi sync worker:
    #   [cmd_type=1][cmd_id=1 LE][version=2][audioChannel=0][flag=0/1] + token32
    # Two commands are sent: flag=0 then flag=1 (both required).
    # The token is the per-device BIND_TOKEN from the cloud API (not the static APP_KEY).
    from app.state.cloud import get_device_bind_token, bind_device as cloud_bind_device, is_logged_in as cloud_logged_in
    device_token = get_device_bind_token(sn)
    if not device_token and sn and cloud_logged_in():
        # BIND_TOKEN missing — try creating it now (handles first-time pairing)
        logging.info(f"[BLE] No BIND_TOKEN for {sn}, attempting cloud bind...")
        try:
            result = cloud_bind_device(sn)
            device_token = get_device_bind_token(sn)
            logging.info(f"[BLE] Cloud bind result: token={'yes' if device_token else 'NO'}")
        except Exception as e:
            logging.warning(f"[BLE] Cloud bind failed: {e}")
    if not device_token:
        raise ValueError("BIND_TOKEN missing — cannot proceed with BLE handshake")
    if len(device_token) > 32:
        logging.warning(
            "[BLE] BIND_TOKEN length %d exceeds 32-byte handshake field; truncating "
            "(this likely indicates a configuration error — the device-bound token should be ≤32 ASCII chars).",
            len(device_token),
        )
    try:
        token32 = device_token[:32].encode("ascii").ljust(32, b"\x00")
    except UnicodeEncodeError as e:
        raise ValueError(f"BIND_TOKEN contains non-ASCII characters: {e}") from e
    r1 = await send_cmd(bytes([1, 1, 0, 2, 0, 0]) + token32)
    r2 = await send_cmd(bytes([1, 1, 0, 2, 0, 1]) + token32)
    logging.info(f"[BLE] Handshake responses: HS1={r1 is not None}, HS2={r2 is not None}")
    await send_cmd(bytes([1, 4, 0]) + struct.pack("<I", int(time.time())) + bytes([0, 0]))
    await send_cmd(bytes([1, 3, 0]), timeout=3)
    await send_cmd(bytes([1, 6, 0]), timeout=3)

    # Battery + charging from cmd 9 (BattStatusRsp: charging@3, level@4).
    battery, charging = await read_battery()
    charging = bool(charging)
    logging.info(f"[Battery] cmd9 BattStatusRsp level={battery} charging={charging}")
    _device_state["battery"] = battery
    _device_state["charging"] = charging

    return {
        "connected": True,
        "battery": battery,
        "device_name": _device_state["device_name"] or "Plaud NotePin S",
    }


async def send_cmd(pt, timeout=5):
    """Send an encrypted command and wait for response."""
    cl = _device_state["client"]
    if not cl or not _device_state["connected"]:
        return None
    cipher = _device_state["cipher"]
    key, nonce, aad = _device_state["session_keys"]
    # Snapshot the event + response holder locally: bleak fires _on_disconnect
    # from its own thread, which calls _reset_state() and replaces these with
    # None. Without local refs, the cmd_resp[0] read below would TypeError if
    # the disconnect lands mid-await.
    cmd_evt = _device_state["cmd_evt"]
    cmd_resp = _device_state["cmd_resp"]
    if cmd_evt is None or cmd_resp is None:
        return None
    _device_state["seq"] += 1
    # Record the cmd_id we're issuing so the notify handler only wakes us for the
    # matching response. The device also emits unsolicited cmd_type==1 packets
    # (button-press record events, late/duplicate replies); without this the first
    # such packet would be returned here and its bytes misread (e.g. a sessionId
    # byte parsed as battery %). The SDK matches responses to requests the same way.
    _device_state["expected_cmd_id"] = (
        struct.unpack_from("<H", pt, 1)[0] if len(pt) >= 3 else None
    )
    ct = cipher.encrypt(nonce, struct.pack("<I", _device_state["seq"]) + pt, aad)
    cmd_evt.clear()
    cmd_resp[0] = None
    try:
        await cl.write_gatt_char(W, ct, response=False)
    except Exception as e:
        logging.warning(f"BLE write failed (device likely disconnected): {e}")
        _reset_state()
        return None
    try:
        await asyncio.wait_for(cmd_evt.wait(), timeout=timeout)
    except TimeoutError:
        pass
    return cmd_resp[0]


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
        from app.state.cloud import get_device_sn_signature
        sn_for_verify = _device_state.get("serial_number", "")
        sn_sig_verify = get_device_sn_signature(sn_for_verify)
        if not sn_sig_verify:
            logging.warning("No SN signature for depair verification — skipping")
        else:
            sig = base64.b64decode(sn_sig_verify)
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
    """List files on the connected device.

    The file list can span multiple BLE notification packets. Each packet has:
    - Offset 0-2: cmd type + cmd_id (3 bytes)
    - Offset 3-6: unknown (4 bytes, maybe timestamp)
    - Offset 7-8: total file count (uint16)
    - Offset 9-10: index offset of this packet (uint16) — how many files before this
    - Offset 11+: file entries (10 bytes each: sid, size, byte, byte)

    We collect multiple packets until we have all `total` files.
    """
    import sys
    files = []
    expected_total = None

    # Clear previous multi-packet buffer
    _device_state["file_list_packets"] = []

    # Send the request
    r = await send_cmd(bytes([1, 26, 0]), timeout=10)
    if r:
        print(f"[BLE] list_files first response: {len(r)} bytes: {r.hex()[:200]}", file=sys.stderr, flush=True)

    def _parse_packet_records(pkt):
        """Extract file records from a single BLE notification packet."""
        records = []
        if len(pkt) < 11:
            return records
        total = struct.unpack_from("<H", pkt, 7)[0]
        pkt_start = struct.unpack_from("<H", pkt, 9)[0]
        print(f"[BLE] packet: total={total} start_idx={pkt_start} bytes={len(pkt)}", file=sys.stderr, flush=True)
        offset = 11
        while offset + 10 <= len(pkt):
            sid = struct.unpack_from("<I", pkt, offset)[0]
            fsize = struct.unpack_from("<I", pkt, offset + 4)[0]
            if 0 < fsize < 500_000_000 and sid > 0:
                duration_est = (fsize - 512) / 80 * 0.02 if fsize > 512 else 0
                records.append(
                    {
                        "session_id": sid,
                        "size": fsize,
                        "duration_est": round(duration_est, 1),
                    }
                )
            offset += 10
        return records

    def _total_from_packet(pkt):
        """Read the expected total file count from a packet header."""
        if len(pkt) < 9:
            return None
        return struct.unpack_from("<H", pkt, 7)[0]

    # The BLE notification handler fires during the `send_cmd` await and appends
    # the first response packet to `file_list_packets` — the same object that
    # `send_cmd` returns as `r`.  Processing `r` directly AND then iterating
    # `file_list_packets` would double-process that first packet, temporarily
    # inflating `len(files)` and risking premature termination of the poll loop.
    #
    # Fix (F-py-019): take a snapshot of `file_list_packets` after send_cmd
    # returns; process every packet from the snapshot (which includes r exactly
    # once).  Do NOT process `r` separately.
    initial_packets = list(_device_state.get("file_list_packets", []))
    prev_consumed = len(initial_packets)
    for pkt in initial_packets:
        if expected_total is None:
            expected_total = _total_from_packet(pkt)
        files.extend(_parse_packet_records(pkt))

    # Wait for additional packets if we expected more files
    if expected_total is not None and len(files) < expected_total:
        print(f"[BLE] Waiting for more packets ({len(files)}/{expected_total})", file=sys.stderr, flush=True)
        # Wait up to 3 seconds for more packets to arrive
        for _ in range(30):
            await asyncio.sleep(0.1)
            new_packets = list(_device_state.get("file_list_packets", []))
            if len(new_packets) > prev_consumed:
                # Process only the newly arrived packets (slice from prev_consumed onward)
                for pkt in new_packets[prev_consumed:]:
                    if expected_total is None:
                        expected_total = _total_from_packet(pkt)
                    files.extend(_parse_packet_records(pkt))
                prev_consumed = len(new_packets)
            if expected_total is not None and len(files) >= expected_total:
                break

    # Dedup by session_id — BLE retransmits can cause the same record to appear
    # in multiple packets. Preserve insertion order; first occurrence wins.
    seen_sids: set = set()
    deduped: list = []
    for rec in files:
        if rec["session_id"] not in seen_sids:
            seen_sids.add(rec["session_id"])
            deduped.append(rec)
    files = deduped

    print(f"[BLE] list_device_files: returning {len(files)} files (expected {expected_total})", file=sys.stderr, flush=True)
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

    # Battery + charging from cmd 9 (BattStatusRsp: charging@3, level@4).
    lvl, chg = await read_battery()
    info["battery_level"] = lvl
    if chg is not None:
        info["charging"] = chg

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
    global _syncing
    if not _syncing.acquire(blocking=False):
        return {"error": "Sync already in progress"}
    _ble_sync_status.update({"message": "Preparing…", "percent": 5, "active": True})
    try:
        # Suppress macOS App Nap so backgrounding the window can't throttle the
        # CoreBluetooth stream and trip the stall watchdog mid-transfer.
        with _keep_awake("Plaud BLE sync"):
            return await _sync_files_inner(progress_callback)
    finally:
        _ble_sync_status.update({"message": "", "percent": 0, "active": False})
        _syncing.release()


def do_wifi_sync_blocking():
    """Run WiFi sync as a subprocess (user-initiated, blocking).

    Spawns wifi_sync_worker.py as a standalone process with its own clean
    asyncio event loop — identical to the test script that works reliably.
    """
    global _syncing, _sync_abort
    if not _device_state.get("serial_number"):
        _wifi_sync_status.update({"message": "Device serial number unknown", "percent": 0})
        return {"error": "Device serial number unknown"}
    if not _device_state.get("session_keys"):
        _wifi_sync_status.update({"message": "No BLE session", "percent": 0})
        return {"error": "No BLE session — connect device first"}

    # Signal any running BLE sync to abort, then wait up to 10s for it to release.
    _sync_abort = True
    acquired = _syncing.acquire(timeout=10)
    _sync_abort = False
    if not acquired:
        return {"error": "BLE sync still running after 10s; WiFi sync aborted"}

    sn = _device_state["serial_number"]
    key, nonce, aad = _device_state["session_keys"]

    def _on_status(msg, pct):
        _wifi_sync_status["message"] = msg
        _wifi_sync_status["percent"] = pct

    _on_status("Starting WiFi transfer...", 5)

    # Log WiFi sync to file for production debugging
    _wifi_log = DATA_DIR / "wifi_sync.log" if DATA_DIR else None
    def _wlog(msg):
        print(f"[WiFi] {msg}")
        if _wifi_log:
            try:
                with open(_wifi_log, "a") as f:
                    f.write(f"{__import__('datetime').datetime.now().isoformat()} {msg}\n")
            except Exception:
                pass

    try:
        # Disconnect BLE first — the worker needs to make its own connection.
        # The device invalidates crypto sessions when BLE drops, so the worker
        # must do the full BLE handshake → WiFi flow in one process.
        _on_status("Preparing for WiFi transfer...", 5)
        _wlog(f"Starting WiFi sync for SN={sn}")
        try:
            cl = _device_state.get("client")
            if cl:
                # Don't use run_async here — it acquires _ble_lock which may be
                # held by a background poll. Fire-and-forget the disconnect instead.
                _wlog("Disconnecting BLE (fire-and-forget)...")
                loop = _get_loop()
                fut = asyncio.run_coroutine_threadsafe(cl.disconnect(), loop)
                try:
                    fut.result(timeout=3)
                except Exception:
                    pass
                _wlog("BLE disconnect done")
        except Exception as e:
            _wlog(f"BLE disconnect failed (ok): {e}")
        _reset_state()

        # Build worker args — worker does its own BLE connect + handshake
        from app.state.cloud import get_device_bind_token, get_device_sn_signature, is_logged_in, bind_device, fetch_sn_signature
        device_token = get_device_bind_token(sn) if sn else None
        device_sig = get_device_sn_signature(sn) if sn else None
        # Auto-fetch credentials from cloud if missing (same as BLE pairing flow)
        if (not device_token or not device_sig) and sn and is_logged_in():
            _wlog("Credentials missing — attempting auto-fetch from cloud...")
            try:
                if not device_token:
                    bind_result = bind_device(sn)
                    _wlog(f"bind_device result: {bind_result}")
                    device_token = get_device_bind_token(sn)
                if not device_sig:
                    sig_result = fetch_sn_signature(sn)
                    _wlog(f"fetch_sn_signature result: {sig_result}")
                    device_sig = get_device_sn_signature(sn)
            except Exception as e:
                _wlog(f"Auto-fetch failed: {e}")
        _wlog(f"Credentials: bind_token={'yes' if device_token else 'NO'}, sn_sig={'yes' if device_sig else 'NO'}")
        if not device_token or not device_sig:
            _on_status("Device not registered", 0)
            _wlog("ABORT: missing credentials")
            if not is_logged_in():
                return {"error": "Log into Plaud Cloud in Settings first, then try again."}
            return {"error": "Device not registered. Log into Plaud Cloud in Settings to bind your device."}
        worker_args = json.dumps({
            "sn": sn,
            "recordings_dir": str(get_recordings_dir()),
            "app_key_path": str(APP_KEY_PATH),
            "dismissed": list(load_config().get("deleted_ids", [])),
            "bind_token": device_token,
            "sn_sig": device_sig,
        })

        # Find the worker script
        import importlib.util
        from pathlib import Path as _Path
        worker_path = _Path(importlib.util.find_spec("app.utils.wifi_sync_worker").origin)
        python_bin = sys.executable
        _wlog(f"Spawning worker: {python_bin} {worker_path}")
        _wlog(f"CWD: {APP_KEY_PATH.parent.parent}")

        # Run worker as subprocess — it has its own clean asyncio.run().
        # Credentials go in via stdin, not argv, so `ps aux` can't see them.
        import subprocess as sp
        proc = sp.Popen(
            [python_bin, str(worker_path)],
            stdin=sp.PIPE,
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            cwd=str(APP_KEY_PATH.parent.parent),  # project root
            env={**os.environ, "PYTHONPATH": str(APP_KEY_PATH.parent.parent)},
        )
        try:
            proc.stdin.write(worker_args.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError) as e:
            _wlog(f"Failed to send args to worker: {e}")
        _wlog(f"Worker PID: {proc.pid}")

        # Read stdout and stderr in separate threads to avoid pipe deadlocks.
        # stderr carries JSON progress updates; stdout carries the final result.
        import threading

        _worker_stderr = []
        _worker_stdout = []

        def _read_progress():
            for line in proc.stderr:
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                _wlog(f"Worker stderr: {line}")
                _worker_stderr.append(line)
                try:
                    update = json.loads(line)
                    if "status" in update:
                        _on_status(update["status"], update.get("percent", 0))
                except Exception:
                    pass
            _wlog("Worker stderr EOF")

        def _read_stdout():
            _worker_stdout.append(proc.stdout.read())
            _wlog("Worker stdout EOF")

        stderr_thread = threading.Thread(target=_read_progress, daemon=True)
        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stderr_thread.start()
        stdout_thread.start()

        # Wait for process exit (up to 10 minutes)
        _wlog("Waiting for worker to finish...")
        proc.wait(timeout=600)
        _wlog(f"Worker exited with code {proc.returncode}")
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=5)

        # Parse result from stdout
        stdout_bytes = _worker_stdout[0] if _worker_stdout else b""
        stdout_str = stdout_bytes.decode("utf-8", errors="replace").strip()
        _wlog(f"Worker stdout: {stdout_str[:500]}")

        if proc.returncode != 0:
            _on_status("WiFi transfer failed", 0)
            stderr_tail = "\n".join(_worker_stderr[-15:])
            _wlog(f"Worker FAILED: code={proc.returncode} stderr={stderr_tail[:300]}")
            return {"error": f"WiFi worker exited with code {proc.returncode}", "output": stdout_str[:200], "traceback": stderr_tail}

        if stdout_str:
            result = json.loads(stdout_str)
            done_count = sum(1 for f in result.get("files", []) if f.get("status") == "new")
            _on_status(f"Complete: {done_count} file(s)", 100)
            return result
        _on_status("WiFi transfer failed — no output", 0)
        return {"error": "WiFi worker produced no output"}

    except Exception as e:
        _wlog(f"EXCEPTION: {e}")
        import traceback
        traceback.print_exc()
        _on_status(f"WiFi transfer failed: {e}", 0)
        return {"error": str(e)}
    finally:
        _syncing.release()


async def _sync_files_inner(progress_callback=None):
    """Internal BLE sync implementation."""
    if not APP_KEY_PATH.exists():
        return {"error": f"Missing {APP_KEY_PATH}"}
    app_rsa_key = load_pem_private_key(APP_KEY_PATH.read_bytes(), password=None)

    files = await list_device_files()
    if not files:
        return {"files": [], "message": "No files on device"}

    active_rec_dir = get_recordings_dir()
    active_rec_dir.mkdir(exist_ok=True)
    dismissed = set(load_config().get("deleted_ids", []))

    new_files = []
    total = len(files)
    for i, f in enumerate(files):
        # Check abort flag (user switching to WiFi sync)
        global _sync_abort
        if _sync_abort:
            _sync_abort = False
            logging.info("BLE sync aborted (switching to WiFi)")
            break

        sid = f["session_id"]
        fsize = f["size"]
        ogg_path = active_rec_dir / f"{sid}.ogg"

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
        _ble_sync_status.update(
            {
                "message": f"Downloading recording {i + 1} of {total}…",
                "percent": round(i / total * 100),
                "active": True,
            }
        )
        file_data = _device_state["file_data"]
        file_done = _device_state["file_done"]
        if file_data is None or file_done is None:
            # Device disconnected (or never fully handshaked) — abort cleanly.
            logging.warning("BLE sync: device state reset mid-sync — aborting")
            _reset_state()
            break
        file_data.clear()
        file_done.clear()

        cipher = _device_state["cipher"]
        key, nonce, aad = _device_state["session_keys"]
        _device_state["seq"] += 1
        sync_cmd = bytes([1, 28, 0]) + struct.pack("<I", sid) + struct.pack("<I", 0) + struct.pack("<I", fsize)
        ct = cipher.encrypt(nonce, struct.pack("<I", _device_state["seq"]) + sync_cmd, aad)
        try:
            await _device_state["client"].write_gatt_char(W, ct, response=False)
        except Exception as e:
            logging.warning(f"BLE write failed during sync for session {sid}: {e}")
            _reset_state()
            break

        last_progress_len = 0
        last_progress_time = time.time()
        # Abort only when data genuinely stops arriving. 60s tolerates background
        # throttling / transient BLE pauses that the old 15s window cut short
        # mid-transfer on large files. A real disconnect is caught immediately
        # below, so this no longer needs to be aggressive.
        stall_timeout = 60
        while not file_done.is_set():
            try:
                await asyncio.wait_for(file_done.wait(), timeout=2)
            except TimeoutError:
                now = time.time()
                # Connection genuinely dropped — stop immediately rather than
                # waiting out the stall window.
                if not _device_state["connected"] or _device_state["file_data"] is None:
                    logging.warning(f"Sync: device disconnected mid-file for session {sid}")
                    break
                # Check if data is still flowing
                current_len = len(file_data)
                if current_len > last_progress_len:
                    last_progress_len = current_len
                    last_progress_time = now
                    if fsize > 0:
                        frac = min(1.0, current_len / fsize)
                        _ble_sync_status.update(
                            {
                                "message": f"Downloading recording {i + 1} of {total}…",
                                "percent": round((i + frac) / total * 100),
                            }
                        )
                elif now - last_progress_time > stall_timeout:
                    logging.warning(f"Sync stalled for session {sid} — no data for {stall_timeout}s, device likely out of range")
                    break

        if not file_data:
            # If we also lost connection, stop trying further files
            if not _device_state["connected"]:
                break
            continue

        raw_data = bytes(file_data)
        # Save encrypted backup for long recordings (allows re-decrypt if needed)
        if len(raw_data) > 500_000:
            enc_path = active_rec_dir / f"{sid}_encrypted.bin"
            enc_path.write_bytes(raw_data)
        opus_frames = decrypt_ble_file(raw_data, app_rsa_key)
        if opus_frames:
            ogg = wrap_opus_ogg(opus_frames)
            tmp_path = ogg_path.with_suffix(".ogg.partial")
            tmp_path.write_bytes(ogg)
            tmp_path.rename(ogg_path)
            duration = len(opus_frames) // 80 * 0.02
            new_files.append(
                {
                    "session_id": sid,
                    "ogg_path": str(ogg_path),
                    "duration": round(duration, 1),
                    "status": "new",
                }
            )
            # Auto-upload to Google Drive if storage mode requires it
            try:
                from app.state.gdrive import maybe_auto_upload
                maybe_auto_upload(sid, str(ogg_path))
            except Exception:
                pass
        else:
            raw_path = active_rec_dir / f"{sid}_encrypted.bin"
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


def is_syncing():
    return _syncing.locked()


def abort_sync():
    global _sync_abort
    _sync_abort = True


def get_wifi_sync_status():
    return dict(_wifi_sync_status)


def get_ble_sync_status():
    return dict(_ble_sync_status)


def set_connected(val):
    _device_state["connected"] = val
