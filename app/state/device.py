"""BLE device state management and operations."""

import asyncio
import base64
import binascii
import inspect
import json
import logging
import os
import re
import struct
import sys
import threading
import time
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from app.utils import ble_proto, pairing_store
from app.utils.audio import BleDecryptError, decrypt_ble_file, wrap_opus_ogg
from app.utils.ble_proto import (
    HANDSHAKE_REJECTED,
    HANDSHAKE_REJECTED_ERROR,
    HANDSHAKE_REPLY_TIMEOUT_S,
    SN_SIG_REPLY_WAIT_S,
    SN_SIG_SEND_REPEATS,
    FileListAssembler,
    bind_handshake,
)
from app.utils.config import load_config, update_config
from app.utils.constants import (
    APP_KEY_PATH,
    DATA_DIR,
    PROJECT_ROOT,
    SESSIONS_DIR,
    get_all_recordings_dirs,
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
    "file_meta": None,
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

# BLE file-receive tuning (module-level so tests can shrink them).
_RECV_POLL_S = 2  # how often the receive loop checks abort / disconnect / stall
_STALL_TIMEOUT_S = 60  # no new bytes for this long = stalled transfer
_TAIL_WAIT_S = 1.5  # after the end packet, how long to wait for the cmd-29 tail (CRC)
_DELETE_REPLY_TIMEOUT_S = 5  # cmd 31: no reply within this = not deleted
_HANDSHAKE_REPLY_TIMEOUT_S = HANDSHAKE_REPLY_TIMEOUT_S  # cmd 1 / 4 / 3 replies during connect (<= 6 s total)
# cmd-28 reply with a non-zero status and still no data after this long = the
# device refused the download; fail fast instead of waiting out the stall window.
_REFUSED_GRACE_S = 5

# Background retry cap: after this many consecutive failed downloads (partial or
# undecryptable) of one session, background syncs stop retrying it (and
# /device/check-new stops counting it). An explicit user sync still retries.
# In memory only: reset on success or app restart.
RETRY_CAP = 3
_download_failures = {}  # str(session id) -> consecutive failed attempts
_download_failures_lock = threading.Lock()


def _update_config(mutate):
    """Load the config, apply `mutate(config)` in place, save it — under the config lock."""
    update_config(mutate)


def _finalize_redownload_backup(sid):
    """Hook called right after a fresh {sid}.ogg is atomically in place."""
    from app.utils.redownload_backup import finalize_redownload_backup

    try:
        finalize_redownload_backup(str(sid))
    except Exception as e:
        logging.warning(f"finalize_redownload_backup({sid}) failed: {e}")


def _reset_state():
    """Reset all connection state to disconnected defaults.

    Leaves "last_handshake" alone: GET /diagnostics reports the last
    handshake even after the device has disconnected.
    """
    _device_state["connected"] = False
    _device_state["handshaking"] = False
    _device_state["port_version"] = None
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
    _device_state["file_meta"] = None


def _on_disconnect(client):
    """Callback fired by bleak when BLE connection drops unexpectedly.

    Only acts for the CURRENT client. After force_reset()/verify replace or drop
    a client, the old BleakClient's callback can still fire later (when its link
    finally dies or our scheduled disconnect completes); without this check it
    would wipe the state of a newer, healthy connection.
    """
    current = _device_state.get("client")
    if client is not current:
        logging.info("BLE disconnect callback from a stale client — ignored")
        return
    logging.warning("BLE device disconnected unexpectedly")
    _reset_state()


async def _disconnect_quietly(cl, timeout=5):
    """Best-effort disconnect of a BleakClient we no longer track."""
    try:
        await asyncio.wait_for(cl.stop_notify(N), timeout=timeout)
    except Exception:
        pass
    try:
        await asyncio.wait_for(cl.disconnect(), timeout=timeout)
    except Exception as e:
        logging.debug(f"Disconnect of stale BLE client failed (ignored): {e}")


_END_OFFSET = 0xFFFFFFFF

# Both checks below are unconfirmed on hardware (README open questions 4 and 13),
# and v2.2.x synced fine without them, so by default ("auto") each download
# first checks that the device's packets fit the assumption and falls back to
# the old accept-everything behaviour (with a warning) when they don't. True
# forces a check on, False turns it off.
#
# Session filtering: data packets, end markers and cmd-29 tails for another
# session are ignored. "auto": enforced for a download whose FIRST data packet
# carries the requested session id; otherwise disabled for that download.
FILTER_DATA_SESSIONS = "auto"
# Offsets: byte offsets must be contiguous. A packet at an offset already
# received is a retransmission and is dropped; one past the end leaves a gap,
# which ends the download as partial. "auto": enforced once the first
# _OFFSET_PROBE_PACKETS packets start at 0 and are contiguous; any other
# pattern (first offset != 0, packet-index-like offsets) disables it for that
# download.
ENFORCE_DATA_OFFSETS = "auto"
_OFFSET_PROBE_PACKETS = 2


def _count_stray(file_meta, what):
    file_meta["stray"] = file_meta.get("stray", 0) + 1
    logging.debug(f"[BLE] ignored {what} (downloading {file_meta.get('sid')})")


def _session_ok(psid, want, file_meta, first_data):
    """Does a data packet / end marker / tail with session id `psid` count?

    `first_data`: this is a (non-end-marker) data packet, which may decide the
    "auto" mode for the download.
    """
    if FILTER_DATA_SESSIONS is False or psid == want and want is not None:
        if first_data and "sid_filter" not in file_meta:
            file_meta["sid_filter"] = True
        return True
    if want is None or FILTER_DATA_SESSIONS is True:
        return False
    state = file_meta.get("sid_filter")
    if state is None and first_data:
        file_meta["sid_filter"] = state = False
        logging.warning(
            f"[BLE] data packets carry session id {psid}, requested {want} — session filtering disabled "
            "for this download; please report"
        )
    return state is False


def _offsets_checked(off, have, file_meta):
    """Whether offset rules apply to this packet (see ENFORCE_DATA_OFFSETS)."""
    if ENFORCE_DATA_OFFSETS is True or ENFORCE_DATA_OFFSETS is False:
        return ENFORCE_DATA_OFFSETS
    state = file_meta.get("offset_check")
    if state is not None:
        return state
    samples = file_meta.setdefault("offset_samples", [])
    samples.append((off, have))
    if off != have:
        file_meta["offset_check"] = False
        logging.warning(
            f"[BLE] session {file_meta.get('sid')}: data offsets don't look like contiguous byte offsets "
            f"(offset, bytes so far: {samples}) — offset checks disabled for this download; please report"
        )
    elif len(samples) >= _OFFSET_PROBE_PACKETS:
        file_meta["offset_check"] = True
    return False  # probe packets are appended as they come (they fit so far)


def _on_file_data(dd, file_data, file_done, file_meta):
    """One type-2 data packet: [2][sessionId u32][offset u32][len u8][bytes].

    Only packets for the session being downloaded (file_meta["sid"]) count; a
    late packet from an earlier or stalled file is ignored and counted. Both
    that and the offset rules adapt per download (FILTER_DATA_SESSIONS,
    ENFORCE_DATA_OFFSETS).
    """
    psid, off = struct.unpack_from("<II", dd, 1)
    want = file_meta.get("sid")
    if not _session_ok(psid, want, file_meta, first_data=off != _END_OFFSET):
        _count_stray(file_meta, f"data packet for session {psid} at offset {off:#x}")
        return
    if off == _END_OFFSET:
        file_meta["end"] = True
        file_meta["end_error"] = dd[9]
        file_meta["end_sid"] = psid
        file_done.set()
        return
    if file_meta.get("gap"):
        return  # already incomplete; the sync loop is stopping this download
    payload = dd[10 : 10 + dd[9]]
    have = len(file_data)
    if off != have and "offset_mismatch" not in file_meta:
        file_meta["offset_mismatch"] = (off, have)
    if not _offsets_checked(off, have, file_meta) or off == have:
        file_data.extend(payload)
    elif off < have:
        # Retransmission: drop what we already have; keep any new bytes past the end.
        file_meta["duplicates"] = file_meta.get("duplicates", 0) + 1
        logging.debug(f"[BLE] session {psid}: retransmitted data at offset {off} (have {have})")
        file_data.extend(payload[have - off :])
    else:
        file_meta["gap"] = (off, have)
        logging.warning(f"[BLE] session {psid}: data gap — packet at offset {off}, only {have} bytes received")
        file_done.set()


def _make_notify_handler(cipher, nonce, aad, cmd_evt, cmd_resp, file_data, file_done, file_meta):
    """Build the encrypted-channel notification handler.

    File transfer bookkeeping goes into `file_meta` (reset by the sync loop per
    file, which then sets "sid" to the session it requests) so the sync can
    decide whether a download really completed. Data packets, the end marker
    and the cmd-29 tail only count when their session id is "sid" (unless
    session filtering fell back for this download: "sid_filter" False); others
    are ignored and counted in "stray".
      - "sid_filter" / "offset_check": per-download outcome of the "auto"
        checks (True enforced, False fell back to accepting everything).
      - "end" / "end_error": data packet with offset 0xFFFFFFFF; byte 9 is the
        device's error code for the transfer (0 = OK).
      - "tail" / "tail_sid" / "tail_crc": cmd 29 SyncFileTail (sessionId@3-6,
        crc16@7-8).
      - "start_status": cmd 28 SyncFileHead reply byte 7. Assumed to be a
        status (0 = accepted) but UNCONFIRMED: logged, never used to reject a
        file that delivered data.
      - "duplicates": retransmitted data packets dropped (offset already had).
      - "gap": (offset, bytes had) of the first packet past the end of the
        data received: the download is incomplete.
      - "offset_mismatch": first data packet whose offset != bytes received so
        far (diagnostic).
    """

    def handler(s, d):
        try:
            dd = cipher.decrypt(nonce, bytes(d), aad)[4:]
        except Exception as e:
            logging.warning(f"[BLE] Notification decrypt failed ({len(d)}B): {e}")
            return
        if len(dd) < 1:
            return
        if dd[0] == 2 and len(dd) >= 10:
            _on_file_data(dd, file_data, file_done, file_meta)
        elif dd[0] == 1:
            cid = struct.unpack_from("<H", dd, 1)[0] if len(dd) >= 3 else 0
            logging.debug(f"[BLE] cmd response: cmd_id={cid}, len={len(dd)}")
            if cid == 29:
                tail_sid = struct.unpack_from("<I", dd, 3)[0] if len(dd) >= 7 else None
                if not _session_ok(tail_sid, file_meta.get("sid"), file_meta, first_data=False):
                    _count_stray(file_meta, f"cmd-29 tail for session {tail_sid}")
                else:
                    file_meta["tail"] = True
                    file_meta["tail_sid"] = tail_sid
                    if len(dd) >= 9:
                        file_meta["tail_crc"] = struct.unpack_from("<H", dd, 7)[0]
                    file_done.set()
            elif cid == 28 and len(dd) >= 8:
                # It's the reply to the request just sent, so it's kept even if
                # its session id differs (as before); the id is kept for the logs.
                file_meta["start_status"] = dd[7]
                file_meta["start_sid"] = struct.unpack_from("<I", dd, 3)[0]
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

    return handler


def _record_handshake(verdict, hs_info):
    """Remember the last BIND_TOKEN handshake's outcome for GET /diagnostics.

    Only the verdict, the reply status bytes (None = no reply), the port
    version and the token's length: never the token or the raw replies.
    """
    _device_state["last_handshake"] = {
        "verdict": verdict,
        "statuses": [h["status"] if h else None for h in hs_info.get("hs") or []],
        "port_version": hs_info.get("port_version"),
        "token_len": hs_info.get("token_len"),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def get_last_handshake():
    """A copy of the last handshake record (see _record_handshake), or None."""
    last = _device_state.get("last_handshake")
    return dict(last) if last else None


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
        # Hard timeout: after a system sleep the link can be dead while bleak
        # still holds the peripheral, and readRSSI then never returns — which
        # would pin _ble_lock for the full run_async window and freeze every
        # status poll. Bound it so a dead link can't wedge the lock.
        rssi = await asyncio.wait_for(backend.get_rssi(), timeout=4)
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

            self._token = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(self._OPTIONS, self._reason)
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
            coro.close()  # never scheduled: close it, or Python warns it was never awaited
            raise TimeoutError("BLE busy — lock held by long-running operation")
        try:
            return _run_on_loop(coro, timeout)
        finally:
            _ble_lock.release()
    with _ble_lock:
        return _run_on_loop(coro, timeout)


def _run_on_loop(coro, timeout):
    try:
        loop = _get_loop()
    except BaseException:
        coro.close()
        raise
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


# Devices the last scans saw: address -> (BLEDevice, time.monotonic()). On macOS
# the address is a CoreBluetooth UUID, and connecting by address makes bleak
# search for it again; that search often misses a NotePin a scan has just
# found ("Device with address … was not found" after the full 30 s timeout).
# Connecting with the scan's BLEDevice skips the second search.
_recent_ble_devices = {}
RECENT_DEVICE_MAX_AGE_S = 60.0


def _remember_ble_device(device):
    _recent_ble_devices[device.address] = (device, time.monotonic())


def _fresh_ble_device(address, max_age=RECENT_DEVICE_MAX_AGE_S):
    """The BLEDevice a scan saw at `address` within `max_age` seconds, or None."""
    seen = _recent_ble_devices.get(address) if address else None
    if seen and time.monotonic() - seen[1] <= max_age:
        return seen[0]
    return None


async def scan_devices(scan_duration=6):
    """Scan for all Plaud devices in range, returning detailed info for each."""
    from bleak import BleakScanner

    from src.scanner import parse_manufacturer_data

    found = []
    by_address = {}

    def callback(device, adv):
        name = device.name or adv.local_name or ""
        is_plaud = any(p in name.upper() for p in ["PLAUD", "IZYREC", "NOTEPIN"])
        mfr_info = parse_manufacturer_data(adv.manufacturer_data) if adv.manufacturer_data else None

        if is_plaud or mfr_info or (0x5D in (adv.manufacturer_data or {})):
            _remember_ble_device(device)
            info = by_address.get(device.address)
            if info is None:
                info = mfr_info or {}
                info["name"] = name or "Plaud Device"
                info["address"] = device.address
                info["rssi"] = adv.rssi
                by_address[device.address] = info
                found.append(info)
                return
            # Seen before. macOS reports each advertising packet separately, so
            # the first one can lack the manufacturer data (and the serial
            # number): fill it in from a later packet instead of losing it.
            if mfr_info and not info.get("serial_number"):
                for k, v in mfr_info.items():
                    info.setdefault(k, v)
                info["serial_number"] = mfr_info.get("serial_number")
            if name and info.get("name") == "Plaud Device":
                info["name"] = name
            if adv.rssi is not None:
                info["rssi"] = adv.rssi

    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    await asyncio.sleep(scan_duration)
    await scanner.stop()

    found.sort(key=lambda d: d.get("rssi", -100), reverse=True)
    summary = ", ".join(
        f"{d.get('name')} ({d.get('rssi')} dBm, serial {'advertised' if d.get('serial_number') else 'not advertised'})"
        for d in found
    )
    print(f"[BLE] Scan found {len(found)} Plaud device(s){': ' + summary if found else ''}")
    return found


async def scan_unless_connected(attempts=2, scan_duration=4):
    """scan_devices_with_retry, or None if a device is connected by the time
    this runs. Scan requests wait behind a connect on the BLE lock; once the
    connect succeeds the NotePin stops advertising, so scanning then only
    finds nothing (and a "found" answer would make the caller reconnect)."""
    if is_connected():
        return None
    return await scan_devices_with_retry(attempts=attempts, scan_duration=scan_duration)


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
                print("[BLE] Paired device found by its advertised serial number")
                return {
                    "found": True,
                    "name": d.get("name", "Plaud NotePin S"),
                    "address": d["address"],
                    "rssi": d.get("rssi", -60),
                    "serial_number": sn,
                }
        # No serial matched (the advertising packets may not have carried it):
        # recognise a paired device by the Bluetooth address it last had.
        for d in devices:
            sn = d.get("serial_number") or paired_serial_for_address(d["address"])
            if sn and sn in known_sns:
                print("[BLE] Paired device found by its last Bluetooth address (no serial advertised)")
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
            result = {
                "found": True,
                "name": d.get("name", "Plaud NotePin S"),
                "address": d["address"],
                "rssi": d.get("rssi", -60),
            }
            if d.get("serial_number"):
                result["serial_number"] = d["serial_number"]
            elif len(known_sns) == 1 and len(devices) == 1:
                # One paired device, one Plaud in range, and it didn't say
                # which it is: assume it's ours. A wrong guess only makes the
                # handshake fail, the same as having no serial.
                result["serial_number"] = next(iter(known_sns))
                print("[BLE] Only one Plaud in range and one paired device: assuming it's the paired one")
            elif not result.get("serial_number"):
                print("[BLE] Paired device not recognised; using the strongest device, serial unknown")
            return result
    else:
        # No paired devices — standard scan with retry
        devices = await scan_devices_with_retry(attempts=2, scan_duration=6)
        if devices:
            d = devices[0]
            result = {
                "found": True,
                "name": d.get("name", "Plaud NotePin S"),
                "address": d["address"],
                "rssi": d.get("rssi", -60),
            }
            if d.get("serial_number"):
                result["serial_number"] = d["serial_number"]
            return result
    return {"found": False}


ADDRESS_CHECK_S = 5.0


async def _address_visible(address):
    """Whether a device is advertising at `address` right now.

    A scan in the last minute counts; otherwise a short scan for it (whose
    BLEDevice is remembered for the connect). If the check itself fails,
    answer True so the connect goes ahead as before.
    """
    if _fresh_ble_device(address):
        return True
    try:
        from bleak import BleakScanner

        device = await BleakScanner.find_device_by_address(address, timeout=ADDRESS_CHECK_S)
    except Exception:
        return True
    if device is None:
        return False
    _remember_ble_device(device)
    return True


def _remember_bind_token(sn, token_bytes):
    """Save the token the NotePin accepted (a fallback in bind_handshake) as its
    BIND_TOKEN, in the config and the Keychain backup, so the next connect and
    an exported pairing file use it."""
    try:
        token = token_bytes.rstrip(b"\x00").decode("ascii")
    except UnicodeDecodeError:
        return
    from app.utils import pairing_store

    update_config(lambda c: c.setdefault("device_bind_tokens", {}).__setitem__(sn, token))
    pairing_store.backup_serial_async(sn)  # Keychain copy, off the BLE loop
    logging.warning(f"[BLE] Saved the token this NotePin accepted (serial ending {sn[-4:]})")


async def connect_device(address=None):
    """Connect to device, perform handshake, return status."""

    # Clean up any stale connection first
    if _device_state.get("client"):
        try:
            await disconnect_device()
        except Exception:
            _reset_state()

    if address and not await _address_visible(address):
        # macOS gives the NotePin a new Bluetooth address from time to time. A
        # stale one makes BleakClient look for it for its full 30 s timeout
        # before failing, so find the paired device by its serial first.
        print("[BLE] Last known address not in range; scanning for the paired device")
        address = None
    if not address:
        scan_result = await scan_device()
        if not scan_result["found"]:
            return {"connected": False, "error": "Device not found"}
        address = scan_result["address"]
        _device_state["device_name"] = scan_result["name"]
        if scan_result.get("serial_number") and not _device_state.get("serial_number"):
            _device_state["serial_number"] = scan_result["serial_number"]
    _device_state["device_address"] = address
    if not _device_state.get("serial_number"):
        sn = paired_serial_for_address(address)
        if sn:
            print("[BLE] No serial number given; using the paired device last seen at this address")
            _device_state["serial_number"] = sn

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
            # The scan's BLEDevice if there is one: no second search (see _recent_ble_devices).
            target = _fresh_ble_device(address) or address
            cl = BC(target, timeout=30.0, disconnected_callback=_on_disconnect)
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
                    if not _device_state.get("serial_number"):
                        _device_state["serial_number"] = scan_result.get("serial_number") or paired_serial_for_address(
                            address
                        )
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
        return {
            "connected": False,
            "error": f"Service discovery failed after 3 connection attempts (device: {sn}). Try moving the device closer and reconnecting.",
        }

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
        from app.state.cloud import (
            SN_SIGN_WRONG_TOKEN_STATUS,
            bind_device,
            fetch_sn_signature,
            get_device_bind_token,
            get_device_sn_signature,
            is_logged_in,
        )

        sn = _device_state.get("serial_number", "")
        sn_sig_b64 = get_device_sn_signature(sn) if sn else None
        cloud_error = None  # Plaud's reason, if fetching the credentials failed
        cloud_status = None  # Plaud's status code for that failure
        if not sn_sig_b64 and sn and is_logged_in():
            try:
                bind_result = bind_device(sn) or {}
                sig_result = fetch_sn_signature(sn) or {}
                sn_sig_b64 = get_device_sn_signature(sn)
                if not sn_sig_b64:
                    # Errors only: a successful bind result carries the device token.
                    cloud_error = sig_result.get("error") or bind_result.get("error")
                    cloud_status = sig_result.get("status")
                    logging.warning(
                        "Auto-fetch failed: bind=%s, sig=%s",
                        bind_result.get("error") or "ok",
                        sig_result.get("error") or "no signature",
                    )
            except Exception as e:
                cloud_error = str(e)
                logging.warning(f"Auto-fetch exception: {e}")
        if not sn_sig_b64:
            # Hang up: a NotePin stays connected (and stops advertising, so
            # scans can't find it) until the Mac lets go of the link.
            await _disconnect_quietly(cl)
            _reset_state()
            # Specific error message depending on what's missing
            if not sn:
                return {"connected": False, "error": "Device serial number unknown. Try scanning for the device first."}
            if not is_logged_in():
                return {
                    "connected": False,
                    "error": (
                        f"This Mac doesn't have the pairing credentials for this NotePin (serial ending {sn[-4:]}). "
                        "They come from Plaud once per device: sign in under Settings › Plaud Cloud, connect, "
                        "then you can sign out."
                    ),
                }
            if cloud_status == SN_SIGN_WRONG_TOKEN_STATUS:
                return {
                    "connected": False,
                    "error": (
                        f"Plaud Cloud won't hand out the pairing for this NotePin (serial ending {sn[-4:]}) "
                        "to this sign-in. Import a pairing file instead: export one in Settings › Device on a "
                        "Mac where this NotePin already works, then choose Import pairing here."
                    ),
                }
            reason = f" {cloud_error.rstrip('.')}." if cloud_error else ""
            return {
                "connected": False,
                "error": (
                    f"Couldn't get the pairing credentials for this NotePin (serial ending {sn[-4:]}) "
                    f"from Plaud Cloud.{reason} Check you're signed in to the Plaud account this NotePin "
                    "belongs to (Settings › Plaud Cloud), then connect again."
                ),
            }
        sig = base64.b64decode(sn_sig_b64)
        tc = (len(sig) + 99) // 100
        # Send SN_SIG several times for reliability on weak connections
        for _ in range(SN_SIG_SEND_REPEATS):
            for i in range(tc):
                await cl.write_gatt_char(
                    W, struct.pack("<H", 65040) + bytes([tc, i]) + sig[i * 100 : (i + 1) * 100], response=False
                )
                await asyncio.sleep(0.02)
        # Wait for the device's ACK or session blob (same limits as the WiFi worker)
        for _ in range(int(SN_SIG_REPLY_WAIT_S * 10)):
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
            await _disconnect_quietly(cl)
            _reset_state()
            return {
                "connected": False,
                "error": "Handshake failed — device did not respond. Try moving it closer and reconnecting.",
            }

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
        await _disconnect_quietly(cl)
        _reset_state()
        err_msg = str(e)
        # Translate raw BLE errors to user-friendly messages
        if "service discovery" in err_msg.lower():
            return {
                "connected": False,
                "error": "Connection was lost during setup. Try moving the device closer and reconnecting.",
            }
        if "disconnected" in err_msg.lower() or "not connected" in err_msg.lower():
            return {"connected": False, "error": "Device disconnected during setup. Try reconnecting."}
        if "timeout" in err_msg.lower():
            return {
                "connected": False,
                "error": "Device took too long to respond. Try moving it closer and reconnecting.",
            }
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
    file_meta = {}

    handler = _make_notify_handler(cipher, nonce, aad, cmd_evt, cmd_resp, file_data, file_done, file_meta)
    await cl.start_notify(N, handler)
    _device_state["cmd_evt"] = cmd_evt
    _device_state["cmd_resp"] = cmd_resp
    _device_state["file_data"] = file_data
    _device_state["file_done"] = file_done
    _device_state["file_meta"] = file_meta
    # send_cmd() works from here, but the device only counts as connected once
    # it has accepted the BIND_TOKEN below.
    _device_state["handshaking"] = True

    # Send BIND_TOKEN handshake (cmd_id=1) — required for device to accept commands.
    # Format matches the official Android app (nh/a0.java) and the WiFi sync worker:
    #   [cmd_type=1][cmd_id=1 LE][version=2][audioChannel=0][flag=0/1] + token32
    # Two commands are sent: flag=0 then flag=1 (both required).
    # The token is the per-device BIND_TOKEN from the cloud API (not the static APP_KEY).
    from app.state.cloud import bind_device as cloud_bind_device
    from app.state.cloud import get_device_bind_token
    from app.state.cloud import is_logged_in as cloud_logged_in

    device_token = get_device_bind_token(sn)
    if not device_token and sn and cloud_logged_in():
        # BIND_TOKEN missing — try creating it now (handles first-time pairing)
        logging.info(f"[BLE] No BIND_TOKEN for {sn}, attempting cloud bind...")
        try:
            cloud_bind_device(sn)
            device_token = get_device_bind_token(sn)
            logging.info(f"[BLE] Cloud bind result: token={'yes' if device_token else 'NO'}")
        except Exception as e:
            logging.warning(f"[BLE] Cloud bind failed: {e}")
    try:
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
    except ValueError:
        await _disconnect_quietly(cl)
        _reset_state()
        raise
    # cmd 1 twice, then cmd 4 (time sync with this Mac's time zone); the replies
    # say whether the device accepted the token (ble_proto.handshake_verdict).
    # Only a rejection fails the connect; "no reply at all" is logged by
    # bind_handshake() and the connect carries on, as in v2.4.0.
    verdict, hs_info = await bind_handshake(lambda pt: send_cmd(pt, timeout=_HANDSHAKE_REPLY_TIMEOUT_S), token32)
    _record_handshake(verdict, hs_info)
    if hs_info.get("accepted_token") and sn:
        _remember_bind_token(sn, hs_info["accepted_token"])
    if verdict == HANDSHAKE_REJECTED and ble_proto.FAIL_ON_HANDSHAKE_REJECTED:
        await _disconnect_quietly(cl)
        _reset_state()
        return {"connected": False, "error": HANDSHAKE_REJECTED_ERROR, "pairing_rejected": True}
    _device_state["port_version"] = hs_info["port_version"]
    if hs_info["port_version"] is not None and hs_info["port_version"] < 7:
        # BLE data packets and the file list use an older layout below port
        # version 7 (PROTOCOL.md 7.2); only the >= 7 layout is implemented.
        logging.warning(f"[BLE] Device port version {hs_info['port_version']} (< 7): file transfer layout untested")
    _device_state["handshaking"] = False
    _device_state["connected"] = True
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
    if not cl or not (_device_state["connected"] or _device_state.get("handshaking")):
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
    _device_state["expected_cmd_id"] = struct.unpack_from("<H", pt, 1)[0] if len(pt) >= 3 else None
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
    except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041 — distinct classes on Python 3.10
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
    depair_sn = _device_state.get("serial_number")

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
            except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041 — distinct classes on Python 3.10
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

    forgotten = {depair_sn} if depair_sn else set()

    def _forget_devices(config):
        forgotten.update((config.get("paired_devices") or {}).keys())
        config["paired_devices"] = {}
        config.pop("known_devices_by_sn", None)
        config.pop("known_devices", None)

    _update_config(_forget_devices)
    # The device is unpaired so it can pair elsewhere: drop the Keychain backup
    # too, or the next launch would restore the pairing. (The credentials stay
    # in the config, as before; only paired devices are backed up.)
    pairing_store.forget_async(forgotten)

    sk_path = PROJECT_ROOT / "session_keys.json"
    if sk_path.exists():
        try:
            sk_path.unlink()
        except Exception:
            pass

    return {"depaired": True, "depair_response": depair_ok, "depair_verified": depair_verified}


async def list_device_files():
    """List files on the connected device (cmd 26).

    The reply can span several notifications. Each one has the total file
    count (u16 at 7), the index of its first entry (u16 at 9), then entries
    (10 bytes on the NotePin S: sid u32, size u32, two unknown bytes).
    ble_proto.FileListAssembler checks the start index (as the SDK does),
    skips retransmissions and duplicate session ids, holds out-of-order
    notifications, and ignores trailing bytes that don't make a whole entry
    (a notification without one whole entry is dropped). A list that stays
    short only means the missing files wait for the next sync.
    """
    lst = FileListAssembler(_device_state.get("port_version"))

    # The notify handler appends every cmd-26 notification to file_list_packets,
    # including the one send_cmd returns as its reply, so only that list is read
    # (reading `r` as well would count the first packet twice).
    _device_state["file_list_packets"] = []
    r = await send_cmd(bytes([1, 26, 0]), timeout=10)
    if r:
        logging.debug(f"[BLE] list_files first response: {len(r)} bytes: {r.hex()[:200]}")

    consumed = 0

    def _consume():
        nonlocal consumed
        packets = list(_device_state.get("file_list_packets") or [])
        for pkt in packets[consumed:]:
            lst.add(pkt)
        consumed = len(packets)

    _consume()
    if r is not None and not lst.complete:
        logging.info(f"[BLE] file list: waiting for more packets ({lst.count}/{lst.total})")
        for _ in range(30):  # up to 3 s
            await asyncio.sleep(0.1)
            _consume()
            if lst.complete:
                break
    if lst.total is not None and not lst.complete:
        problems = f" ({'; '.join(lst.problems)})" if lst.problems else ""
        logging.warning(f"[BLE] file list incomplete: {lst.count} of {lst.total} entries{problems}")
    logging.info(f"[BLE] list_device_files: {len(lst.records)} files (device total {lst.total})")
    return lst.records


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
            info["recording_state"] = (
                state_val  # raw device state bitfield (idle=1, recording=0x1003) — kept for reference, NOT used for is_recording
            )
            # Recording is determined by the active session id, not `state`:
            # measured idle -> session 0, scene 0; recording -> session != 0, scene 1.
            # (The `state` field is a bitfield, not a 0-3 enum, so the old
            # `state in (1,3)` check was wrong both ways.)
            session_id = struct.unpack_from("<I", r, 11)[0] if len(r) >= 15 else 0
            info["active_session_id"] = session_id
            info["is_recording"] = session_id != 0
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
    """Delete a file from the device by session ID (cmd 31).

    Reports deleted:True ONLY when the device replies with a SyncRecFileDelRsp
    for this session (sessionId@3-6, SDK C9810v0 for portVersion >= 7) whose
    status@7 is 0. No reply (timeout, disconnect) or any other status means we
    don't know the file is gone, so deleted:False.
    """
    session_id = int(session_id)
    r = await send_cmd(
        bytes([1, 31, 0]) + struct.pack("<I", session_id),
        timeout=_DELETE_REPLY_TIMEOUT_S,
    )
    if not r or len(r) < 8:
        logging.warning(f"Delete of session {session_id}: no (or short) reply from device")
        return {"deleted": False, "session_id": session_id, "error": "No reply from device"}
    reply_sid = struct.unpack_from("<I", r, 3)[0]
    status = r[7]
    if reply_sid != session_id:
        logging.warning(f"Delete of session {session_id}: reply was for session {reply_sid}")
        return {"deleted": False, "session_id": session_id, "error": "Reply for a different session"}
    if status != 0:
        return {"deleted": False, "session_id": session_id, "status": status, "error": f"Device status {status}"}
    return {"deleted": True, "session_id": session_id}


async def start_recording():
    """Start recording on the device."""
    await send_cmd(bytes([1, 20, 0]))
    return {"recording": True}


async def stop_recording():
    """Stop recording on the device."""
    await send_cmd(bytes([1, 23, 0]))
    return {"recording": False}


SYNC_BUSY_ERROR = "Sync already in progress"


def claim_sync():
    """Try to take the sync slot without blocking. Returns True if acquired.

    A caller that gets True MUST hand ownership to sync_files(_claimed=True)
    (which releases it) or call release_sync() itself.
    """
    global _sync_abort
    if not _syncing.acquire(blocking=False):
        return False
    # A stale abort (e.g. /abort-sync pressed when nothing was running) must not
    # silently cancel this new sync. Cleared at claim time, not when the
    # coroutine starts, so an abort sent while it waits for _ble_lock still counts.
    _sync_abort = False
    return True


def release_sync():
    _syncing.release()


async def sync_files(progress_callback=None, _claimed=False, background=False):
    """Download all files from device, decrypt, save as .ogg.

    `_claimed=True` means the caller already took the sync slot via
    claim_sync() (in the Flask thread, before queueing behind _ble_lock); this
    coroutine then owns it and releases it when done.

    `background=True` (automatic syncs) skips sessions that hit the retry cap;
    an explicit user sync (the default) retries them.
    """
    if not _claimed and not claim_sync():
        return {"error": SYNC_BUSY_ERROR, "busy": True}
    _ble_sync_status.update({"message": "Preparing…", "percent": 5, "active": True})
    try:
        # Suppress macOS App Nap so backgrounding the window can't throttle the
        # CoreBluetooth stream and trip the stall watchdog mid-transfer.
        with _keep_awake("Plaud BLE sync"):
            return await _sync_files_inner(progress_callback, background=background)
    finally:
        _ble_sync_status.update({"message": "", "percent": 0, "active": False})
        _syncing.release()


def run_sync_blocking(timeout=1800, background=False):
    """Run a BLE sync from a Flask thread. Returns None if a sync is already running.

    The sync slot is claimed HERE, before queueing behind _ble_lock, so a second
    /sync arriving while the first is running (or still waiting for the lock)
    is rejected immediately instead of running a full second sync afterwards.
    """
    if not claim_sync():
        return None
    coro = sync_files(_claimed=True, background=background)
    try:
        return run_async(coro, timeout=timeout)
    except BaseException:
        # If the coroutine never started it can't release the slot itself.
        if inspect.getcoroutinestate(coro) == inspect.CORO_CREATED:
            coro.close()
            release_sync()
        raise


# ---------------------------------------------------------------------------
# Download integrity
# ---------------------------------------------------------------------------

# cmd 29 (SyncFileTail) carries a CRC-16 of the file, computed on the phone by
# the native tntGetFileCrc() (libtnt_ble_utils.so) — its exact variant (and
# whether it covers the raw bytes) is NOT visible in the decompiled Java. Until
# a real-device run confirms which variant matches (see the "[BLE] CRC" log
# lines), CRC is logged but never used to reject a file. Set this to the
# confirmed variant name (a key of crc16_variants()) to enforce it.
CRC16_ENFORCED_VARIANT = None


def _crc16_reflected(data, poly_rev, init):
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ poly_rev if crc & 1 else crc >> 1
    return crc & 0xFFFF


_CRC16_FUNCS = {
    # name: (fast, fn). Fast = binascii's C implementation; the reflected
    # variants are pure Python (~1 s per MB) and must stay off the BLE loop.
    "XMODEM": (True, lambda d: binascii.crc_hqx(d, 0)),
    "CCITT-FALSE": (True, lambda d: binascii.crc_hqx(d, 0xFFFF)),
    "ARC": (False, lambda d: _crc16_reflected(d, 0xA001, 0)),
    "MODBUS": (False, lambda d: _crc16_reflected(d, 0xA001, 0xFFFF)),
    "KERMIT": (False, lambda d: _crc16_reflected(d, 0x8408, 0)),
}


def crc16_variant(data, name):
    """One named CRC-16 variant of `data` (a key of crc16_variants())."""
    return _CRC16_FUNCS[name][1](data)


def crc16_variants(data, slow_limit=1_000_000):
    """Common CRC-16 variants of `data` → {name: crc}.

    The reflected (pure-Python) variants are only computed for files up to
    `slow_limit` bytes. This is slow: the sync never calls it on the BLE event
    loop — it runs in an executor via _schedule_crc_diagnostic().
    """
    return {name: fn(data) for name, (fast, fn) in _CRC16_FUNCS.items() if fast or len(data) <= slow_limit}


def _log_tail_crc(sid, data, device_crc):
    """Diagnostic only: log which CRC-16 variant(s) match the device's cmd-29 CRC.

    Runs in a worker thread after the file is saved. Never raises.
    """
    try:
        variants = crc16_variants(data)
        matched = [name for name, v in variants.items() if v == device_crc]
        if matched:
            logging.info(f"[BLE] CRC session {sid}: device=0x{device_crc:04x} matches {','.join(matched)}")
        else:
            logging.info(f"[BLE] CRC session {sid}: device=0x{device_crc:04x} matches none of {sorted(variants)}")
    except Exception as e:
        logging.debug(f"[BLE] CRC diagnostic for session {sid} failed: {e}")


def _schedule_crc_diagnostic(sid, data, device_crc):
    """Fire-and-forget _log_tail_crc on the default executor, off the BLE loop."""
    if device_crc is None:
        return
    try:
        asyncio.get_running_loop().run_in_executor(None, _log_tail_crc, sid, data, device_crc)
    except RuntimeError:  # no running loop (direct call from sync code)
        _log_tail_crc(sid, data, device_crc)


def _crc_enforced_ok(data, device_crc):
    """False only when CRC16_ENFORCED_VARIANT is set and doesn't match the device CRC."""
    if device_crc is None or CRC16_ENFORCED_VARIANT not in _CRC16_FUNCS:
        return True
    return crc16_variant(data, CRC16_ENFORCED_VARIANT) == device_crc


# evaluate_download() verdicts
DOWNLOAD_OK = "ok"  # exact size + clean end: save; eligible for auto-delete
DOWNLOAD_SIZE_MISMATCH = "size_mismatch"  # clean end, size != list: keep only if it decrypts; never auto-delete
DOWNLOAD_PARTIAL = "partial"  # keep as {sid}_partial.bin and retry


def evaluate_download(sid, expected_size, data, meta, interrupted=None):
    """Decide what to do with a received file. Returns (verdict, reason).

    None of these rules is confirmed on real hardware (the protocol notes only
    say the request is start=0/end=fileSize and that offset 0xFFFFFFFF is the
    end packet carrying an error code), so they fail SAFE:

    - interrupted (abort / stall / disconnect / refused / data gap), a gap in
      the data offsets, no end-of-file marker, or an end packet with a non-zero
      error byte → DOWNLOAD_PARTIAL. End markers and tails only count for the
      session requested (the notify handler ignores the rest).
    - The cmd-28 reply status is NOT checked here: its meaning is unconfirmed.
      (The receive loop treats it as a refusal only if no data arrives at all.)
    - Byte count == size from the file list → DOWNLOAD_OK.
    - Otherwise, if the end packet arrived with error 0 → DOWNLOAD_SIZE_MISMATCH:
      the caller keeps the file only if it decrypts to valid Opus, and never
      auto-deletes it from the device. Tail-only (no end packet) → PARTIAL.
    - A CRC mismatch rejects only when CRC16_ENFORCED_VARIANT is set.
    """
    if interrupted:
        return DOWNLOAD_PARTIAL, interrupted
    if meta.get("gap"):
        return DOWNLOAD_PARTIAL, "gap in the received data"
    if not (meta.get("end") or meta.get("tail")):
        return DOWNLOAD_PARTIAL, "no end-of-file marker"
    if meta.get("end") and meta.get("end_error"):
        return DOWNLOAD_PARTIAL, f"device reported transfer error {meta['end_error']}"
    if not _crc_enforced_ok(data, meta.get("tail_crc")):
        return DOWNLOAD_PARTIAL, "CRC mismatch"
    if len(data) == expected_size:
        return DOWNLOAD_OK, None
    reason = f"size mismatch ({len(data)} of {expected_size} bytes)"
    if meta.get("end") and data:
        return DOWNLOAD_SIZE_MISMATCH, reason
    return DOWNLOAD_PARTIAL, reason


# ---------------------------------------------------------------------------
# Background retry cap (in memory; reset on success or app restart)
# ---------------------------------------------------------------------------


def record_download_failure(sid):
    """Count one more consecutive failed download of `sid`; returns the new count."""
    key = str(sid)
    with _download_failures_lock:
        _download_failures[key] = _download_failures.get(key, 0) + 1
        return _download_failures[key]


def clear_download_failures(sid):
    with _download_failures_lock:
        _download_failures.pop(str(sid), None)


def download_failures(sid):
    with _download_failures_lock:
        return _download_failures.get(str(sid), 0)


def is_retry_capped(sid):
    """True once `sid` has failed RETRY_CAP times in a row (background syncs skip it)."""
    return download_failures(sid) >= RETRY_CAP


def track_download_result(entry):
    """Update the retry counter from one download result entry (BLE or WiFi).

    "new" resets it; "partial" / "decrypt_failed" count as a failure, except a
    user abort (switching to WiFi), which says nothing about the file.
    """
    sid = entry.get("session_id")
    status = entry.get("status")
    if sid is None:
        return
    if status == "new":
        clear_download_failures(sid)
    elif status in ("partial", "decrypt_failed") and entry.get("reason") != "aborted":
        n = record_download_failure(sid)
        if n >= RETRY_CAP:
            logging.warning(
                f"[Sync] session {sid} failed {n} times in a row — background sync stops retrying it "
                "(an explicit Sync still does)"
            )


def gdrive_cloud_synced_ids():
    """Session ids (as str) already uploaded to Google Drive in 'cloud' storage mode.

    In cloud mode the local .ogg is deleted after upload, so local presence alone
    would re-download (and re-upload) the same files on every sync.
    """
    try:
        if load_config().get("storage_mode", "local") != "cloud":
            return set()
        from app.state.gdrive import get_synced_files

        return {str(k) for k in get_synced_files()}
    except Exception:
        return set()


def find_local_recording(sid, rec_dirs=None):
    """Return the path of an existing complete .ogg for `sid`, or None.

    Partial downloads ({sid}_partial.bin) and failed decrypts
    ({sid}_encrypted.bin) are deliberately NOT treated as present, so the next
    sync retries them.
    """
    for d in rec_dirs if rec_dirs is not None else get_all_recordings_dirs():
        p = d / f"{sid}.ogg"
        if p.exists():
            return p
    return None


def is_already_synced(sid, rec_dirs=None, cloud_ids=None):
    """True if the recording is present locally or (cloud mode) in Google Drive."""
    if find_local_recording(sid, rec_dirs) is not None:
        return True
    if cloud_ids is None:
        cloud_ids = gdrive_cloud_synced_ids()
    return str(sid) in cloud_ids


def _atomic_write(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


async def _send_stop_sync():
    """Tell the device to stop streaming the current file (cmd 30)."""
    try:
        await send_cmd(bytes([1, 30, 0]), timeout=2)
    except Exception as e:
        logging.debug(f"stop-sync (cmd 30) failed: {e}")


# ---------------------------------------------------------------------------
# WiFi sync (subprocess orchestration)
# ---------------------------------------------------------------------------

# The worker reports its own time budget (remaining_s in its progress lines,
# from wifi_sync_worker.compute_budget). The parent waits that long plus a
# margin, then SIGTERMs it (the worker restores WiFi on SIGTERM), waits for the
# restore, and only SIGKILLs a worker that still won't exit. The sync slot is
# never released while the worker runs.
WIFI_BUDGET_MARGIN_S = 120
WIFI_TERM_GRACE_S = 210  # the worker's RESTORE_BUDGET_S (180) + 30
WIFI_KILL_WAIT_S = 10

_wifi_sync_status.update({"active": False, "budget_s": None, "deadline": None})

# <data dir>/wifi_sync.log: rotated to wifi_sync.log.1 once it reaches this size
# (one old file kept), so it can't grow forever.
WIFI_LOG_NAME = "wifi_sync.log"
WIFI_LOG_MAX_BYTES = 1_000_000

_REDACTED = "[redacted]"
_SECRET_PATTERNS = [
    # JSON / repr / key=value forms of token- or signature-like fields
    re.compile(
        r"""(?i)(["']?\b(?:access_token|bind_token|token|sn_sig|signature|sig|authorization)\b["']?\s*[:=]\s*)"""
        r"""("[^"]*"|'[^']*'|[^\s,;}\]]+)"""
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),  # bearer tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),  # JWTs
]


def redact_secrets(text, secrets=()):
    """Remove credentials from a log line.

    Exact known values (this device's BIND_TOKEN and SN signature) are replaced
    wherever they appear; token / signature fields and bearer tokens / JWTs are
    replaced by pattern, so a value we weren't told about doesn't leak either.
    """
    text = str(text)
    for s in secrets:
        if s and len(s) >= 8:
            text = text.replace(s, _REDACTED)
    text = _SECRET_PATTERNS[0].sub(lambda m: m.group(1) + _REDACTED, text)
    text = _SECRET_PATTERNS[1].sub("bearer " + _REDACTED, text)
    return _SECRET_PATTERNS[2].sub(_REDACTED, text)


WIFI_LOG_SCRUB_MARKER = "wifi_sync.log.scrubbed"


def _configured_secrets():
    """Every BIND_TOKEN and SN signature in the config (for exact-value redaction)."""
    try:
        config = load_config()
    except Exception:
        return []
    found = []
    for key in ("device_bind_tokens", "device_sn_signatures"):
        values = config.get(key) or {}
        if isinstance(values, dict):
            found.extend(v for v in values.values() if isinstance(v, str))
    return found


def scrub_wifi_logs(data_dir=None):
    """One-time clean-up of logs written before redaction existed.

    Before v2.4.1, wifi_sync.log recorded the raw cloud replies, which contain
    the BIND_TOKEN and SN signature. Rewrites wifi_sync.log and wifi_sync.log.1
    through redact_secrets() (temp file + atomic replace), then writes a
    marker file so it never runs again. Returns True if it ran. Never raises;
    on failure the marker isn't written and the next start tries again.
    """
    data_dir = DATA_DIR if data_dir is None else data_dir
    if not data_dir:
        return False
    marker = data_dir / WIFI_LOG_SCRUB_MARKER
    if marker.exists():
        return False
    secrets = None
    try:
        for name in (WIFI_LOG_NAME, WIFI_LOG_NAME + ".1"):
            path = data_dir / name
            if not path.exists():
                continue
            if secrets is None:
                secrets = _configured_secrets()
            text = path.read_text(encoding="utf-8", errors="replace")
            clean = redact_secrets(text, secrets)
            if clean != text:
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(clean, encoding="utf-8")
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
                logging.info(f"[WiFi] removed credentials from {name}")
        marker.write_text("wifi_sync logs redacted\n")
        return True
    except Exception as e:
        logging.warning(f"[WiFi] couldn't scrub the old WiFi sync logs: {e}")
        return False


def append_rotating_log(path, line, max_bytes=None):
    """Append `line` to `path`; first move a file of max_bytes or more to `path`.1.

    Never raises: logging must not break a sync.
    """
    limit = WIFI_LOG_MAX_BYTES if max_bytes is None else max_bytes
    try:
        if path.stat().st_size >= limit:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    try:
        with open(path, "a") as f:
            f.write(line)
    except OSError:
        pass


class _WifiDeadline:
    """When the parent stops waiting for the worker (monotonic clock)."""

    def __init__(self, remaining_s, clock=time.monotonic):
        self._clock = clock
        self.at = 0.0
        self.extend(remaining_s)

    def extend(self, remaining_s):
        """The worker says it needs `remaining_s` more seconds from now."""
        self.at = self._clock() + max(0, remaining_s) + WIFI_BUDGET_MARGIN_S

    def expired(self):
        return self._clock() > self.at

    @classmethod
    def now(cls, clock=time.monotonic):
        """An already-expired deadline (stop the worker immediately)."""
        d = cls(0, clock)
        d.at = clock() - 1
        return d

    def wall_clock(self):
        """The deadline as a UNIX timestamp (for the UI)."""
        return time.time() + (self.at - self._clock())


def _supervise_wifi_worker(proc, deadline, wlog, poll_s=1.0):
    """Wait for the WiFi worker within its budget; stop it if it overruns.

    Returns "exited" (finished by itself), "terminated" (exited after SIGTERM,
    having run its WiFi restore) or "killed" (ignored SIGTERM for
    WIFI_TERM_GRACE_S and was SIGKILLed; its restore record is left for
    recovery). Doesn't return while the worker still runs, so the caller can't
    release the sync slot under a live worker.
    """
    import subprocess as sp

    while True:
        try:
            proc.wait(timeout=poll_s)
            return "exited"
        except sp.TimeoutExpired:
            pass
        if deadline.expired():
            break
    wlog(f"Worker PID {proc.pid} over its time budget; sending SIGTERM (it restores WiFi first)")
    proc.terminate()
    try:
        proc.wait(timeout=WIFI_TERM_GRACE_S)
        return "terminated"
    except sp.TimeoutExpired:
        pass
    wlog(f"Worker PID {proc.pid} still running {WIFI_TERM_GRACE_S}s after SIGTERM; killing it")
    proc.kill()
    try:
        proc.wait(timeout=WIFI_KILL_WAIT_S)
    except sp.TimeoutExpired:
        wlog(f"Worker PID {proc.pid} did not exit after SIGKILL")
    return "killed"


def _apply_wifi_progress(update, deadline):
    """Apply one JSON progress line from the worker to the shared status."""
    if "status" in update:
        _wifi_sync_status["message"] = update["status"]
        _wifi_sync_status["percent"] = update.get("percent", 0)
    remaining = update.get("remaining_s")
    if isinstance(remaining, (int, float)):
        deadline.extend(remaining)
        _wifi_sync_status["deadline"] = deadline.wall_clock()
    if isinstance(update.get("budget_s"), (int, float)):
        _wifi_sync_status["budget_s"] = update["budget_s"]


def _recover_wifi_after_worker(wlog):
    """Restore the Mac's WiFi from the worker's record (after it was stopped)."""
    from app.utils.wifi_sync_worker import recover_from_record

    try:
        result = recover_from_record(DATA_DIR)
    except Exception as e:
        result = {"status": "error", "error": str(e)}
    wlog(f"WiFi recovery from restore record: {result}")
    return result


# Startup WiFi recovery (api_server._recover_wifi) runs on a background thread
# once the server is listening: re-adding networks and power-cycling can take
# a minute, longer than the shell's startup health check allows. A WiFi sync
# started meanwhile waits for it (it holds the worker lock, so the worker would
# otherwise refuse with "already running").
_wifi_recovery_idle = threading.Event()
_wifi_recovery_idle.set()
WIFI_RECOVERY_WAIT_S = 180  # the worker's RESTORE_BUDGET_S


def run_wifi_recovery_in_background(recover):
    """Run recover() on a daemon thread; do_wifi_sync_blocking waits for it."""
    _wifi_recovery_idle.clear()

    def go():
        scrub_wifi_logs()  # once per install: old logs may hold credentials
        try:
            recover()
        except Exception as e:
            logging.error(f"[WiFi Recovery] failed: {e}")
        finally:
            _wifi_recovery_idle.set()

    t = threading.Thread(target=go, name="wifi-recovery", daemon=True)
    try:
        t.start()
    except Exception:
        _wifi_recovery_idle.set()
        raise
    return t


def get_wifi_restore_notice():
    """{"message", "networks", "since"} when saved networks couldn't be restored
    and the UI hasn't acknowledged it yet, else None."""
    from app.utils.wifi_sync_worker import restore_notice

    try:
        return restore_notice(DATA_DIR)
    except Exception:
        return None


def acknowledge_wifi_restore_notice():
    """The UI showed the notice; don't report it again. True if there was one."""
    from app.utils.wifi_sync_worker import acknowledge_restore_notice

    return acknowledge_restore_notice(DATA_DIR)


def do_wifi_sync_blocking():
    """Run WiFi sync as a subprocess (user-initiated, blocking).

    Spawns wifi_sync_worker.py as a standalone process with its own clean
    asyncio event loop — identical to the test script that works reliably.
    Keeps the Mac awake throughout (the Mac must not idle-sleep while it's on
    the device's hotspot) and holds the sync slot until the worker has exited.
    """
    global _sync_abort
    if not _device_state.get("serial_number"):
        _wifi_sync_status.update({"message": "Device serial number unknown", "percent": 0})
        return {"error": "Device serial number unknown"}
    if not _device_state.get("session_keys"):
        _wifi_sync_status.update({"message": "No BLE session", "percent": 0})
        return {"error": "No BLE session — connect device first"}

    # Signal any running BLE sync to abort, then wait up to 10s for it to release.
    _sync_abort = True
    acquired = _syncing.acquire(timeout=10)
    if not acquired:
        # Leave the abort flag set so the still-running BLE sync stops at its
        # next check (every sync clears it at start, so it can't go stale).
        return {"error": "BLE sync still running after 10s; WiFi sync aborted"}
    _sync_abort = False

    if not _wifi_recovery_idle.is_set():
        _wifi_sync_status.update({"message": "Restoring WiFi settings from an earlier transfer...", "percent": 2})
        if not _wifi_recovery_idle.wait(WIFI_RECOVERY_WAIT_S):
            _syncing.release()
            _wifi_sync_status.update({"message": "Still restoring WiFi settings; try again shortly", "percent": 0})
            return {"error": "Still restoring WiFi settings from an earlier transfer; try again shortly"}

    from app.utils.wifi_sync_worker import compute_budget

    deadline = _WifiDeadline(compute_budget(0)["total_s"])
    _wifi_sync_status.update(
        {"active": True, "budget_s": compute_budget(0)["total_s"], "deadline": deadline.wall_clock()}
    )
    try:
        with _keep_awake("Plaud WiFi sync"):
            return _do_wifi_sync_locked(deadline)
    finally:
        _wifi_sync_status["active"] = False
        _syncing.release()


def _do_wifi_sync_locked(deadline):
    sn = _device_state["serial_number"]

    def _on_status(msg, pct):
        _wifi_sync_status["message"] = msg
        _wifi_sync_status["percent"] = pct

    _on_status("Starting WiFi transfer...", 5)

    # Log WiFi sync to file for production debugging (rotated, secrets redacted)
    _wifi_log = DATA_DIR / WIFI_LOG_NAME if DATA_DIR else None
    scrub_wifi_logs()  # no-op once done (normally already at startup)
    secrets = []  # this device's BIND_TOKEN and SN signature, once known

    def _wlog(msg):
        msg = redact_secrets(msg, secrets)
        print(f"[WiFi] {msg}")
        if _wifi_log:
            append_rotating_log(_wifi_log, f"{datetime.now().isoformat()} {msg}\n")

    proc = None
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
        from app.state.cloud import (
            bind_device,
            fetch_sn_signature,
            get_device_bind_token,
            get_device_sn_signature,
            is_logged_in,
        )

        device_token = get_device_bind_token(sn) if sn else None
        device_sig = get_device_sn_signature(sn) if sn else None
        secrets.extend(s for s in (device_token, device_sig) if s)
        # Auto-fetch credentials from cloud if missing (same as BLE pairing flow)
        if (not device_token or not device_sig) and sn and is_logged_in():
            _wlog("Credentials missing — attempting auto-fetch from cloud...")
            try:
                if not device_token:
                    bind_result = bind_device(sn)
                    device_token = get_device_bind_token(sn)
                    secrets.extend(s for s in (device_token, (bind_result or {}).get("token")) if s)
                    _wlog(f"bind_device: {'token received' if device_token else (bind_result or {}).get('error')}")
                if not device_sig:
                    sig_result = fetch_sn_signature(sn)
                    device_sig = get_device_sn_signature(sn)
                    secrets.extend(s for s in (device_sig, (sig_result or {}).get("signature")) if s)
                    _wlog(
                        f"fetch_sn_signature: {'signature received' if device_sig else (sig_result or {}).get('error')}"
                    )
            except Exception as e:
                _wlog(f"Auto-fetch failed: {e}")
        _wlog(
            f"Credentials: bind token {'present' if device_token else 'MISSING'}, "
            f"SN signature {'present' if device_sig else 'MISSING'}"
        )
        if not device_token or not device_sig:
            _on_status("Device not registered", 0)
            _wlog("ABORT: missing credentials")
            if not is_logged_in():
                return {"error": "Log into Plaud Cloud in Settings first, then try again."}
            return {"error": "Device not registered. Log into Plaud Cloud in Settings to bind your device."}
        worker_args = json.dumps(
            {
                "sn": sn,
                "recordings_dir": str(get_recordings_dir()),
                "app_key_path": str(APP_KEY_PATH),
                "dismissed": list(load_config().get("deleted_ids", [])),
                "bind_token": device_token,
                "sn_sig": device_sig,
                # Lock file + WiFi restore record live here (survive a crash).
                "state_dir": str(DATA_DIR),
            }
        )

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
                except Exception:
                    continue
                if isinstance(update, dict):
                    _apply_wifi_progress(update, deadline)
            _wlog("Worker stderr EOF")

        def _read_stdout():
            _worker_stdout.append(proc.stdout.read())
            _wlog("Worker stdout EOF")

        stderr_thread = threading.Thread(target=_read_progress, daemon=True)
        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stderr_thread.start()
        stdout_thread.start()

        # Wait within the worker's own budget (it grows once it knows the sizes).
        _wlog("Waiting for worker to finish...")
        outcome = _supervise_wifi_worker(proc, deadline, _wlog)
        _wlog(f"Worker {outcome} with code {proc.returncode}")
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=5)

        if outcome != "exited":
            # A stopped worker may not have finished its restore; the record says.
            _on_status("WiFi transfer took too long; restoring WiFi...", 95)
            recovery = _recover_wifi_after_worker(_wlog)
            _on_status("WiFi transfer took too long and was stopped", 0)
            return {
                "error": "WiFi transfer took too long and was stopped",
                "timed_out": True,
                "wifi_recovery": recovery,
            }

        # Parse result from stdout
        stdout_bytes = _worker_stdout[0] if _worker_stdout else b""
        stdout_str = stdout_bytes.decode("utf-8", errors="replace").strip()
        _wlog(f"Worker stdout: {stdout_str[:500]}")

        if proc.returncode != 0:
            _on_status("WiFi transfer failed", 0)
            stderr_tail = "\n".join(_worker_stderr[-15:])
            _wlog(f"Worker FAILED: code={proc.returncode} stderr={stderr_tail[:300]}")
            return {
                "error": f"WiFi worker exited with code {proc.returncode}",
                "output": stdout_str[:200],
                "traceback": stderr_tail,
            }

        if stdout_str:
            result = json.loads(stdout_str.splitlines()[-1])
            for f in result.get("files", []):
                track_download_result(f)
                if f.get("status") == "new" and f.get("session_id") is not None:
                    _finalize_redownload_backup(f["session_id"])
            done_count = sum(1 for f in result.get("files", []) if f.get("status") == "new")
            if result.get("error"):
                _on_status(f"WiFi transfer failed: {result['error']}", 0)
            else:
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
        # Never leave a worker running once we stop tracking it (e.g. an
        # exception above): stop it the same way as on a timeout.
        if proc is not None and proc.poll() is None:
            _supervise_wifi_worker(proc, _WifiDeadline.now(), _wlog, poll_s=0)
            _recover_wifi_after_worker(_wlog)


def _save_partial(sid, raw, expected, reason, rec_dir):
    """Keep an incomplete download as raw encrypted bytes ({sid}_partial.bin).

    It is never an .ogg, so the file is not "present" and the next sync retries
    it; it is never auto-deleted from the device. A bigger earlier partial is
    not overwritten by a smaller one.
    """
    partial_path = rec_dir / f"{sid}_partial.bin"
    saved = None
    if raw:
        try:
            if not partial_path.exists() or partial_path.stat().st_size <= len(raw):
                _atomic_write(partial_path, raw)
            saved = str(partial_path)
        except OSError as e:
            logging.warning(f"Could not save partial download for session {sid}: {e}")
    logging.warning(f"BLE sync: session {sid} incomplete — {reason}; kept as partial, will retry next sync")
    return {
        "session_id": sid,
        "ogg_path": None,
        "duration": 0,
        "status": "partial",
        "received": len(raw),
        "expected": expected,
        "reason": reason,
        "partial_path": saved,
    }


def _save_size_mismatch(sid, raw, expected, reason, app_rsa_key, rec_dir):
    """Clean end-of-file but byte count != the file list's size.

    The size semantics are unconfirmed on real hardware, so instead of looping
    forever on a file that may well be complete, keep it if it decrypts to valid
    Opus — flagged with size_mismatch and never auto-deleted from the device
    (the device copy stays as the fallback). If it doesn't decrypt, it's a
    partial like any other incomplete download.
    """
    try:
        frames = decrypt_ble_file(raw, app_rsa_key)
    except BleDecryptError as e:
        return _save_partial(sid, raw, expected, f"{reason}; decrypt failed: {e}", rec_dir)
    logging.warning(
        f"[BLE] size mismatch accepted for session {sid}: received {len(raw)} bytes, file list said {expected} "
        "(end packet OK, decrypts to valid Opus) — kept, NOT auto-deleted from device"
    )
    entry = _save_complete(sid, raw, app_rsa_key, rec_dir, opus_frames=frames, keep_encrypted=True)
    if entry["status"] == "new":
        entry["size_mismatch"] = {"received": len(raw), "expected": expected}
    return entry


def _save_complete(sid, raw, app_rsa_key, rec_dir, opus_frames=None, keep_encrypted=False):
    """Persist a verified download: encrypted original first, then the .ogg.

    The encrypted original ({sid}_encrypted.bin) is written before decrypting
    and kept whenever decrypt or the .ogg write fails (and always when
    keep_encrypted). Only status "new" means the recording is safely on disk.
    `opus_frames` skips the decrypt when the caller already did it.
    """
    enc_path = rec_dir / f"{sid}_encrypted.bin"
    ogg_path = rec_dir / f"{sid}.ogg"
    partial_path = rec_dir / f"{sid}_partial.bin"
    try:
        _atomic_write(enc_path, raw)
    except OSError as e:
        logging.warning(f"Could not save encrypted download for session {sid}: {e}")
        return {"session_id": sid, "ogg_path": None, "duration": 0, "status": "write_failed", "error": str(e)}
    # The complete encrypted copy supersedes any earlier partial.
    partial_path.unlink(missing_ok=True)
    try:
        if opus_frames is None:
            opus_frames = decrypt_ble_file(raw, app_rsa_key)
    except BleDecryptError as e:
        logging.warning(f"Decrypt failed for session {sid}: {e} — encrypted original kept")
        return {
            "session_id": sid,
            "ogg_path": None,
            "duration": 0,
            "status": "decrypt_failed",
            "error": str(e),
            "encrypted_path": str(enc_path),
        }
    try:
        _atomic_write(ogg_path, wrap_opus_ogg(opus_frames))
    except OSError as e:
        logging.warning(f"Could not write {ogg_path}: {e} — encrypted original kept")
        return {"session_id": sid, "ogg_path": None, "duration": 0, "status": "write_failed", "error": str(e)}
    # Fresh .ogg is in place (runs before the caller's maybe_auto_upload).
    _finalize_redownload_backup(sid)
    # Long recordings keep their encrypted backup (allows re-decrypt), as before.
    if len(raw) <= 500_000 and not keep_encrypted:
        enc_path.unlink(missing_ok=True)
    duration = len(opus_frames) // 80 * 0.02
    return {"session_id": sid, "ogg_path": str(ogg_path), "duration": round(duration, 1), "status": "new"}


async def _sync_files_inner(progress_callback=None, background=False):
    """Internal BLE sync implementation."""
    if not APP_KEY_PATH.exists():
        return {"error": f"Missing {APP_KEY_PATH}"}
    app_rsa_key = load_pem_private_key(APP_KEY_PATH.read_bytes(), password=None)

    files = await list_device_files()
    if not files:
        return {"files": [], "message": "No files on device"}

    active_rec_dir = get_recordings_dir()
    active_rec_dir.mkdir(exist_ok=True)
    try:
        rec_dirs = list(get_all_recordings_dirs())
    except Exception:
        rec_dirs = []
    if active_rec_dir not in rec_dirs:
        rec_dirs.insert(0, active_rec_dir)
    dismissed = set(load_config().get("deleted_ids", []))
    cloud_ids = gdrive_cloud_synced_ids()

    new_files = []
    verified_sids = []  # exact size + clean end + written to disk: eligible for auto-delete
    retry_capped = []  # background only: skipped after RETRY_CAP consecutive failures
    aborted = False
    total = len(files)
    for i, f in enumerate(files):
        if _sync_abort:
            aborted = True
            logging.info("BLE sync aborted (between files)")
            break

        sid = f["session_id"]
        fsize = f["size"]

        if str(sid) in dismissed:
            continue

        existing = find_local_recording(sid, rec_dirs)
        if existing is not None:
            new_files.append(
                {
                    "session_id": sid,
                    "ogg_path": str(existing),
                    "duration": f["duration_est"],
                    "status": "exists",
                }
            )
            continue
        if str(sid) in cloud_ids:
            new_files.append(
                {
                    "session_id": sid,
                    "ogg_path": None,
                    "duration": f["duration_est"],
                    "status": "exists",
                    "location": "gdrive",
                }
            )
            continue
        if background and is_retry_capped(sid):
            # Stops the 45 s background poll re-downloading a file that keeps
            # failing (e.g. a protocol assumption that's wrong on real hardware).
            retry_capped.append(sid)
            new_files.append(
                {
                    "session_id": sid,
                    "ogg_path": None,
                    "duration": 0,
                    "status": "skipped",
                    "reason": "retry limit reached — use Sync to retry",
                    "attempts": download_failures(sid),
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
        cl = _device_state["client"]
        file_data = _device_state["file_data"]
        file_done = _device_state["file_done"]
        file_meta = _device_state.get("file_meta")
        if cl is None or file_data is None or file_done is None or file_meta is None:
            # Device disconnected (or never fully handshaked) — abort cleanly.
            logging.warning("BLE sync: device state reset mid-sync — aborting")
            break
        file_data.clear()
        file_done.clear()
        file_meta.clear()
        file_meta["sid"] = sid  # the notify handler ignores packets for any other session

        cipher = _device_state["cipher"]
        key, nonce, aad = _device_state["session_keys"]
        _device_state["seq"] += 1
        sync_cmd = bytes([1, 28, 0]) + struct.pack("<I", sid) + struct.pack("<I", 0) + struct.pack("<I", fsize)
        ct = cipher.encrypt(nonce, struct.pack("<I", _device_state["seq"]) + sync_cmd, aad)
        try:
            await cl.write_gatt_char(W, ct, response=False)
        except Exception as e:
            logging.warning(f"BLE write failed during sync for session {sid}: {e}")
            _reset_state()
            break

        interrupted = None
        last_progress_len = 0
        last_progress_time = time.time()
        request_time = last_progress_time
        # Abort only when data genuinely stops arriving. 60s tolerates background
        # throttling / transient BLE pauses that the old 15s window cut short
        # mid-transfer on large files. A real disconnect is caught immediately
        # below, so this no longer needs to be aggressive.
        stall_timeout = _STALL_TIMEOUT_S
        while not file_done.is_set():
            # Abort is checked inside the receive loop (not only between files)
            # so "switch to WiFi" doesn't wait for a long file to finish.
            if _sync_abort:
                interrupted = "aborted"
                break
            try:
                await asyncio.wait_for(file_done.wait(), timeout=_RECV_POLL_S)
            except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041 — distinct classes on Python 3.10
                now = time.time()
                # Connection genuinely dropped — stop immediately rather than
                # waiting out the stall window.
                if not _device_state["connected"] or _device_state["file_data"] is not file_data:
                    logging.warning(f"Sync: device disconnected mid-file for session {sid}")
                    interrupted = "disconnected"
                    break
                # cmd-28 reply status: meaning unconfirmed, so it only counts as a
                # refusal when NO data has arrived at all — then fail fast
                # instead of waiting out the full stall window.
                start_status = file_meta.get("start_status")
                if start_status not in (None, 0) and not file_data and now - request_time >= _REFUSED_GRACE_S:
                    logging.warning(f"[BLE] session {sid}: device refused download (cmd-28 status {start_status})")
                    interrupted = f"device refused download (status {start_status})"
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
                    logging.warning(
                        f"Sync stalled for session {sid} — no data for {stall_timeout}s, device likely out of range"
                    )
                    interrupted = "stalled"
                    break

        if interrupted is None and file_meta.get("gap"):
            got_off, have = file_meta["gap"]
            interrupted = f"data gap (packet at offset {got_off} after {have} bytes)"
        # The end-of-file data packet and the cmd-29 tail (which carries the CRC)
        # may arrive in either order — give the tail a moment to land.
        if interrupted is None and file_meta.get("end") and not file_meta.get("tail"):
            for _ in range(int(_TAIL_WAIT_S / 0.1)):
                if file_meta.get("tail") or not _device_state["connected"]:
                    break
                await asyncio.sleep(0.1)

        raw_data = bytes(file_data)
        meta = dict(file_meta)
        file_meta["sid"] = None  # late packets for this file are now stray
        if meta.get("offset_mismatch"):
            got_off, have = meta["offset_mismatch"]
            logging.info(f"[BLE] session {sid}: first out-of-sequence data packet offset={got_off}, had {have} bytes")
        if meta.get("duplicates"):
            logging.info(f"[BLE] session {sid}: dropped {meta['duplicates']} retransmitted data packet(s)")
        if meta.get("stray"):
            logging.info(f"[BLE] session {sid}: ignored {meta['stray']} packet(s) for other sessions")
        if meta.get("start_status") not in (None, 0):
            logging.warning(
                f"[BLE] session {sid}: cmd-28 reply status {meta['start_status']} with {len(raw_data)} bytes "
                "received (meaning unconfirmed — logged only)"
            )
        if interrupted is not None and interrupted != "disconnected" and _device_state["connected"]:
            # Stop the device streaming this file so stale packets don't bleed
            # into the next transfer.
            await _send_stop_sync()

        verdict, reason = evaluate_download(sid, fsize, raw_data, meta, interrupted)
        if verdict == DOWNLOAD_PARTIAL:
            entry = _save_partial(sid, raw_data, fsize, reason, active_rec_dir)
            new_files.append(entry)
            track_download_result(entry)
            if interrupted == "aborted":
                aborted = True
                logging.info(f"BLE sync aborted mid-file (session {sid})")
                break
            if not _device_state["connected"]:
                break
            continue

        if verdict == DOWNLOAD_SIZE_MISMATCH:
            entry = _save_size_mismatch(sid, raw_data, fsize, reason, app_rsa_key, active_rec_dir)
        else:
            entry = _save_complete(sid, raw_data, app_rsa_key, active_rec_dir)
        new_files.append(entry)
        track_download_result(entry)
        if entry["status"] != "new":
            continue
        # CRC is diagnostic only (unconfirmed variant): computed off the BLE loop.
        _schedule_crc_diagnostic(sid, raw_data, meta.get("tail_crc"))
        if verdict == DOWNLOAD_OK:
            # Only exact-size downloads may be auto-deleted from the device.
            verified_sids.append(sid)
        # Auto-upload to Google Drive if storage mode requires it. gdrive keys
        # its synced-files map by str (JSON object keys), so pass a str id —
        # an int never matches after a config reload.
        try:
            from app.state.gdrive import maybe_auto_upload

            maybe_auto_upload(str(sid), entry["ogg_path"])
        except Exception:
            pass

    # Auto-delete synced files from the device — only exact-size files that
    # passed every integrity check AND whose .ogg was written successfully
    # (size-mismatch files stay on the device as the fallback copy).
    auto_delete = load_config().get("auto_delete_after_sync", False)
    deleted_from_device = []
    delete_failed = []
    if auto_delete:
        for sid in verified_sids:
            if not _device_state["connected"]:
                delete_failed.append(sid)
                continue
            try:
                r = await delete_device_file(sid)
            except Exception as e:
                logging.warning(f"Delete of session {sid} raised: {e}")
                r = {"deleted": False}
            (deleted_from_device if r.get("deleted") else delete_failed).append(sid)

    result = {"files": new_files, "deleted_from_device": deleted_from_device}
    if delete_failed:
        result["delete_failed"] = delete_failed
    if aborted:
        result["aborted"] = True
    if retry_capped:
        result["retry_capped"] = retry_capped
    return result


def save_known_device(address, name=None, serial_number=None):
    """Remember a device we've successfully paired with from THIS app."""
    sn = serial_number or _device_state.get("serial_number")
    if not sn or sn == "unknown":
        return

    def _remember(config):
        paired = config.get("paired_devices", {})
        paired[sn] = {
            "name": name or "Plaud Device",
            "last_address": address,
            "last_connected": datetime.now().isoformat(),
        }
        config["paired_devices"] = paired

    _update_config(_remember)
    pairing_store.backup_serial_async(sn, name)


def get_paired_serials():
    """Get serial numbers of devices we've successfully paired with."""
    config = load_config()
    return set(config.get("paired_devices", {}).keys())


def paired_serial_for_address(address):
    """Serial number of the paired device last connected at this Bluetooth address.

    On macOS the address is a CoreBluetooth UUID that stays the same for a
    device on this Mac, so it identifies a paired device whose advertising
    packets didn't include its serial number. None if no paired device matches.
    """
    if not address:
        return None
    paired = load_config().get("paired_devices", {}) or {}
    matches = [sn for sn, info in paired.items() if (info or {}).get("last_address") == address]
    return matches[0] if len(matches) == 1 else None


def set_serial_number(sn):
    """Set the serial number for the currently connected device."""
    _device_state["serial_number"] = sn


def is_connected():
    return _device_state["connected"]


async def verify_connection():
    """Actively probe the live BLE link with a bounded command.

    After a macOS sleep the link can drop without bleak firing its disconnect
    callback, leaving `_device_state["connected"]` stuck True. Callers that
    trust that flag (e.g. the wake handler) then skip reconnecting. This issues
    a real cmd 3 with a short timeout: a valid reply means the link is alive; no
    reply means the cached flag is stale, so we reset state to let the caller
    reconnect. Returns True only if the device actually responded.
    """
    if not _device_state["connected"]:
        return False
    try:
        r = await send_cmd(bytes([1, 3, 0]), timeout=4)
        if r and len(r) >= 4:
            return True
    except Exception:
        pass
    logging.info("verify_connection: live probe failed — resetting stale connection")
    old = _device_state.get("client")
    _reset_state()
    if old is not None:
        # Really drop the old link: a half-dead BleakClient left connected keeps
        # the peripheral busy and its late disconnect callback used to clobber a
        # newer connection's state (now ignored by _on_disconnect).
        asyncio.ensure_future(_disconnect_quietly(old))
    return False


def force_reset():
    """Force connection state back to disconnected (used when a probe can't even
    acquire the BLE lock — i.e. a hung op has wedged it). Safe to call from the
    Flask thread: it rewrites the state dict and schedules (without waiting for)
    a real disconnect of the old BleakClient on the BLE loop."""
    old = _device_state.get("client")
    _reset_state()
    if old is not None:
        try:
            asyncio.run_coroutine_threadsafe(_disconnect_quietly(old), _get_loop())
        except Exception as e:
            logging.debug(f"force_reset: could not schedule disconnect: {e}")


def is_syncing():
    return _syncing.locked()


def abort_sync():
    """Ask a running BLE sync to stop. Returns True if a sync was running.

    With nothing running this is a no-op (the flag used to stay set and
    silently cancel the next sync; every sync also clears it on start).
    """
    global _sync_abort
    if not _syncing.locked():
        return False
    _sync_abort = True
    return True


def get_wifi_sync_status():
    """WiFi sync progress, plus restore_notice: saved networks that couldn't be
    restored (show once, then acknowledge_wifi_restore_notice), or None."""
    return {**_wifi_sync_status, "restore_notice": get_wifi_restore_notice()}


def get_ble_sync_status():
    return dict(_ble_sync_status)


def set_connected(val):
    _device_state["connected"] = val
