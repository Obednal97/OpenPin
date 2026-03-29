"""WiFi fast file transfer for Plaud NotePin S.

Protocol (reverse-engineered from decompiled APK):
1. BLE session already established with ChaCha20-Poly1305 keys
2. Open device hotspot via BLE cmd [1, 10, 0]
3. Connect Mac to hotspot (Plaud{last4SN} / last8SN) via CoreWLAN
4. Start WebSocket server on port 8081
5. Device connects to our WebSocket as client
6. Handshake: device sends cmd_id=2 (SayHello), we respond cmd_id=1
7. File list: cmd_id=11
8. File download: cmd_id=12 request, cmd_id=13 data chunks
9. Close hotspot, restore internet via power-cycle
"""

import asyncio
import hashlib
import json
import logging
import struct
import subprocess
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from app.utils.audio import decrypt_ble_file, wrap_opus_ogg
from app.utils.config import load_config
from app.utils.constants import APP_KEY_PATH, BIND_TOKEN, RECORDINGS_DIR

WS_PORT = 8081
WIFI_SIZE_THRESHOLD = 500_000  # Use WiFi if total new files > 500KB


# ── WiFi packet protocol ──


def build_wifi_packet(cmd_id, json_data, protocol_version=2):
    """Build: [total_len u24 LE][pver u8][cmd_id u16 LE][json_len u16 LE][json]"""
    json_bytes = json.dumps(json_data, separators=(",", ":")).encode("ascii")
    total_len = 8 + len(json_bytes)
    header = bytearray(8)
    header[0] = total_len & 0xFF
    header[1] = (total_len >> 8) & 0xFF
    header[2] = (total_len >> 16) & 0xFF
    header[3] = protocol_version
    struct.pack_into("<H", header, 4, cmd_id)
    struct.pack_into("<H", header, 6, len(json_bytes))
    return bytes(header) + json_bytes


def parse_wifi_packet(data):
    """Parse WiFi packet → (cmd_id, json_data)."""
    if len(data) < 8:
        return None, None
    cmd_id = struct.unpack_from("<H", data, 4)[0]
    json_len = struct.unpack_from("<H", data, 6)[0]
    json_bytes = data[8 : 8 + json_len]
    try:
        json_data = json.loads(json_bytes)
    except Exception:
        json_data = {}
    return cmd_id, json_data


# ── CoreWLAN + network helpers ──


def _get_en0_ip():
    try:
        r = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""


def _connect_to_hotspot(ssid, password, original_ip, timeout=25):
    """Connect to device hotspot via CoreWLAN + networksetup."""
    try:
        from CoreWLAN import CWWiFiClient

        iface = CWWiFiClient.sharedWiFiClient().interface()
    except ImportError:
        logging.warning("CoreWLAN not available — cannot connect to hotspot")
        return None

    print(f"[WiFi] disconnecting from current network...")
    if iface:
        iface.disassociate()
    for _ in range(10):
        time.sleep(0.5)
        if not _get_en0_ip():
            break

    print(f"[WiFi] connecting to '{ssid}'...")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = subprocess.run(
                ["networksetup", "-setairportnetwork", "en0", ssid, password],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            continue

        if r.returncode == 0:
            for _ in range(8):
                time.sleep(1)
                ip = _get_en0_ip()
                if ip and ip != original_ip:
                    print(f"[WiFi] on hotspot IP={ip} (attempt {attempt})")
                    return ip
            ip = _get_en0_ip()
            if ip and ip != original_ip:
                return ip
            if iface:
                iface.disassociate()
            time.sleep(1)
        else:
            time.sleep(1)

    print(f"[WiFi] WARNING: could not connect to hotspot after {timeout}s")
    return None


def _get_preferred_networks(exclude_ssid=""):
    """Get list of preferred/saved WiFi networks."""
    try:
        r = subprocess.run(
            ["networksetup", "-listpreferredwirelessnetworks", "en0"],
            capture_output=True, text=True, timeout=5,
        )
        networks = []
        for line in r.stdout.strip().split("\n")[1:]:
            name = line.strip()
            if name and name != exclude_ssid:
                networks.append(name)
        return networks
    except Exception:
        return []


async def _restore_internet(ssid, hotspot_ip="", original_ip=""):
    """Restore internet after Plaud hotspot, preferring the original network."""
    preferred = _get_preferred_networks(exclude_ssid=ssid)
    subprocess.run(["networksetup", "-removepreferredwirelessnetwork", "en0", ssid], capture_output=True)
    print("[WiFi] power-cycling (10s off) to restore internet...")
    subprocess.run(["networksetup", "-setairportpower", "en0", "off"], capture_output=True, timeout=5)
    await asyncio.sleep(10)
    subprocess.run(["networksetup", "-setairportpower", "en0", "on"], capture_output=True, timeout=5)

    # Actively join top preferred network first — don't rely on auto-join
    # (auto-join often picks phone hotspot instead of home WiFi)
    if preferred:
        print(f"[WiFi] trying preferred: {preferred[0]}")
        for net in preferred[:3]:
            try:
                r = subprocess.run(
                    ["networksetup", "-setairportnetwork", "en0", net],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0 and "-3900" not in (r.stdout + r.stderr):
                    await asyncio.sleep(3)
                    ip = _get_en0_ip()
                    if ip and ip != hotspot_ip:
                        if original_ip and ip == original_ip:
                            print(f"[WiFi] restored to original network ({ip})")
                            return True
                        elif not original_ip:
                            print(f"[WiFi] internet restored via '{net}' ({ip})")
                            return True
                        # Wrong network (probably phone hotspot) — disconnect and try next
                        try:
                            from CoreWLAN import CWWiFiClient
                            iface = CWWiFiClient.sharedWiFiClient().interface()
                            if iface:
                                iface.disassociate()
                        except Exception:
                            pass
                        await asyncio.sleep(1)
                        continue
            except Exception:
                pass

    # Fallback: wait for auto-join
    print("[WiFi] waiting for auto-join...")
    for i in range(10):
        await asyncio.sleep(1.5)
        ip = _get_en0_ip()
        if ip and ip != hotspot_ip:
            print(f"[WiFi] internet restored ({ip})")
            return True

    # Last resort — second power cycle
    print("[WiFi] second power-cycle...")
    subprocess.run(["networksetup", "-setairportpower", "en0", "off"], capture_output=True, timeout=5)
    await asyncio.sleep(10)
    subprocess.run(["networksetup", "-setairportpower", "en0", "on"], capture_output=True, timeout=5)
    for i in range(15):
        await asyncio.sleep(2)
        ip = _get_en0_ip()
        if ip and ip != hotspot_ip:
            print(f"[WiFi] internet restored on second cycle ({ip})")
            return True

    print("[WiFi] WARNING: could not restore internet")
    return False


# ── WiFi file transfer ──


async def wifi_sync_files(session_keys, serial_number, send_ble_cmd, status_callback=None):
    """Download all files from device via WiFi.

    Args:
        session_keys: (key, nonce, aad) from BLE handshake
        serial_number: device serial number string
        send_ble_cmd: async callable to send encrypted BLE commands
        status_callback: optional callable(message, percent) for progress updates

    Returns:
        dict with "files" list (same format as BLE sync) or "error"
    """
    import websockets.asyncio.server

    if not APP_KEY_PATH.exists():
        return {"error": f"Missing {APP_KEY_PATH}"}

    key, nonce, aad = session_keys
    cipher = ChaCha20Poly1305(key)
    app_rsa_key = load_pem_private_key(APP_KEY_PATH.read_bytes(), password=None)

    wifi_ssid = f"Plaud{serial_number[-4:]}"
    wifi_pass = serial_number[-8:]
    original_ip = _get_en0_ip()
    expected_token = hashlib.md5(serial_number.encode()).hexdigest().upper()

    # State for WebSocket handler
    ws_seq = 0
    handshake_done = asyncio.Event()
    file_list_received = asyncio.Event()
    all_downloads_done = asyncio.Event()
    wifi_files = []
    new_files = []
    error_msg = [None]

    dismissed = set(load_config().get("deleted_ids", []))

    def ws_encrypt(payload):
        nonlocal ws_seq
        ws_seq += 1
        return cipher.encrypt(nonce, struct.pack("<I", ws_seq) + payload, aad)

    def ws_decrypt(data):
        return cipher.decrypt(nonce, data, aad)[4:]

    async def ws_handler(websocket):
        nonlocal ws_seq
        ws_seq = 0
        download_buf = bytearray()
        current_sid = 0
        file_queue = []

        async def download_next():
            nonlocal current_sid
            download_buf.clear()
            while file_queue:
                f = file_queue.pop(0)
                sid = f["session_id"]
                ogg_path = RECORDINGS_DIR / f"{sid}.ogg"
                if str(sid) in dismissed or ogg_path.exists():
                    print(f"[WiFi] skipping {sid} (exists={ogg_path.exists()}, dismissed={str(sid) in dismissed})")
                    new_files.append(
                        {"session_id": sid, "ogg_path": str(ogg_path) if ogg_path.exists() else None, "duration": 0, "status": "exists"}
                    )
                    continue
                current_sid = sid
                dl_req = build_wifi_packet(12, {"session": sid, "scene": 0, "start": 0, "end": f["size"]})
                await websocket.send(ws_encrypt(dl_req))
                print(f"[WiFi] requesting download session={sid} size={f['size']}")
                return True
            print(f"[WiFi] all downloads done, new_files count={len(new_files)}")
            all_downloads_done.set()
            return False

        try:
            async for message in websocket:
                if not isinstance(message, bytes):
                    continue
                try:
                    decrypted = ws_decrypt(message)
                except Exception:
                    continue

                cmd_id, json_data = parse_wifi_packet(decrypted)

                if cmd_id == 2:
                    # SayHello — respond with handshake
                    stamp = int(time.time())
                    response = build_wifi_packet(1, {"token": BIND_TOKEN, "stamp": stamp})
                    await websocket.send(ws_encrypt(response))

                elif cmd_id == 1:
                    # Handshake accepted
                    print("[WiFi] handshake accepted")
                    handshake_done.set()
                    await asyncio.sleep(0.3)
                    uid = int(time.time())
                    fl_req = build_wifi_packet(11, {"uid": uid, "start": 0, "single": 0})
                    await websocket.send(ws_encrypt(fl_req))

                elif cmd_id == 11:
                    # File list
                    count = json_data.get("count", 0) if isinstance(json_data, dict) else 0
                    status = json_data.get("status", -1) if isinstance(json_data, dict) else -1
                    if status == 0 and count > 0:
                        json_len_val = struct.unpack_from("<H", decrypted, 6)[0]
                        bin_data = decrypted[8 + json_len_val :]
                        for i in range(0, len(bin_data) - 9, 10):
                            sid = struct.unpack_from("<I", bin_data, i)[0]
                            fsize = struct.unpack_from("<I", bin_data, i + 4)[0]
                            codec = struct.unpack_from("<H", bin_data, i + 8)[0]
                            wifi_files.append({"session_id": sid, "size": fsize, "codec": codec})
                        print(f"[WiFi] {len(wifi_files)} file(s) on device")
                    file_list_received.set()
                    file_queue = list(wifi_files)
                    if not file_queue:
                        all_downloads_done.set()
                    else:
                        await download_next()

                elif cmd_id == 12:
                    # Download started ACK
                    pass

                elif cmd_id == 13:
                    # Data chunk
                    json_len_c = struct.unpack_from("<H", decrypted, 6)[0]
                    chunk = decrypted[8 + json_len_c :]
                    if chunk:
                        download_buf.extend(chunk)
                    is_last = json_data.get("last", 0) if isinstance(json_data, dict) else 0
                    if is_last:
                        # File complete — decrypt and save
                        raw = bytes(download_buf)
                        RECORDINGS_DIR.mkdir(exist_ok=True)
                        frames = decrypt_ble_file(raw, app_rsa_key)
                        if frames:
                            ogg = wrap_opus_ogg(frames)
                            ogg_path = RECORDINGS_DIR / f"{current_sid}.ogg"
                            ogg_path.write_bytes(ogg)
                            duration = len(frames) // 80 * 0.02
                            new_files.append(
                                {"session_id": current_sid, "ogg_path": str(ogg_path), "duration": round(duration, 1), "status": "new"}
                            )
                            print(f"[WiFi] saved {current_sid}.ogg ({len(raw)} → {len(ogg)} bytes, {duration:.1f}s)")
                        else:
                            raw_path = RECORDINGS_DIR / f"{current_sid}_encrypted.bin"
                            raw_path.write_bytes(raw)
                            new_files.append({"session_id": current_sid, "ogg_path": None, "duration": 0, "status": "decrypt_failed"})
                            print(f"[WiFi] WARNING: decrypt failed for {current_sid}")
                        await download_next()

                elif cmd_id == 3:
                    # Heartbeat — pong
                    pong = build_wifi_packet(16, {})
                    await websocket.send(ws_encrypt(pong))

        except Exception as e:
            print(f"[WiFi] WARNING: WebSocket error: {e}")
            if not all_downloads_done.is_set():
                error_msg[0] = str(e)
                all_downloads_done.set()

    def _status(msg, pct):
        print(f"[WiFi] {msg}")
        if status_callback:
            try:
                status_callback(msg, pct)
            except Exception:
                pass

    # ── Execute WiFi transfer flow ──
    hotspot_ip = None
    server = None
    try:
        _status("Starting WebSocket server...", 5)
        server = await websockets.asyncio.server.serve(ws_handler, "0.0.0.0", WS_PORT)

        _status("Opening device hotspot...", 10)
        await send_ble_cmd("WiFi_Open", bytes([1, 10, 0]))

        _status(f"Connecting to {wifi_ssid}... (internet will drop)", 15)
        # Run hotspot connection in a thread — it uses time.sleep() which would block
        # the asyncio event loop and prevent the WebSocket server from accepting connections
        loop = asyncio.get_event_loop()
        hotspot_ip = await loop.run_in_executor(
            None, _connect_to_hotspot, wifi_ssid, wifi_pass, original_ip, 25
        )
        if not hotspot_ip:
            _status("Failed to connect to hotspot", 0)
            return {"error": "Could not connect to device WiFi hotspot", "files": new_files}

        _status("Waiting for device handshake...", 25)
        try:
            await asyncio.wait_for(handshake_done.wait(), timeout=30)
        except asyncio.TimeoutError:
            _status("Handshake timed out", 0)
            return {"error": "WiFi handshake timed out", "files": new_files}

        _status("Getting file list...", 30)
        try:
            await asyncio.wait_for(file_list_received.wait(), timeout=15)
        except asyncio.TimeoutError:
            _status("File list timed out", 0)
            return {"error": "WiFi file list timed out", "files": new_files}

        total_size = sum(f["size"] for f in wifi_files)
        total_new = sum(1 for f in wifi_files if str(f["session_id"]) not in dismissed and not (RECORDINGS_DIR / f"{f['session_id']}.ogg").exists())
        _status(f"Downloading {total_new} file(s)...", 35)

        dl_timeout = max(total_size / 50000, 60)
        try:
            # Poll progress while waiting for downloads
            start_dl = asyncio.get_event_loop().time()
            while not all_downloads_done.is_set():
                try:
                    await asyncio.wait_for(all_downloads_done.wait(), timeout=2)
                except asyncio.TimeoutError:
                    elapsed = asyncio.get_event_loop().time() - start_dl
                    if elapsed > dl_timeout:
                        print(f"[WiFi] WARNING: download timed out after {dl_timeout:.0f}s")
                        break
                    done_count = sum(1 for f in new_files if f["status"] == "new")
                    pct = 35 + int(55 * done_count / max(total_new, 1))
                    _status(f"Downloading... {done_count}/{total_new} complete", pct)
        except Exception:
            pass

        done_count = sum(1 for f in new_files if f["status"] == "new")
        total_downloaded = sum(1 for f in new_files if f["status"] in ("new", "decrypt_failed"))
        _status(f"Download complete: {done_count} file(s) saved, {total_downloaded} downloaded", 90)
        print(f"[WiFi] new_files={new_files}")
        print(f"[WiFi] wifi_files={wifi_files}")

    finally:
        # Always restore internet — but keep server alive until downloads are done
        if server:
            # Give WebSocket handler a moment to finish writing any pending files
            await asyncio.sleep(1)
            server.close()
        if hotspot_ip:
            _status("Restoring internet connection...", 92)
            await _restore_internet(wifi_ssid, hotspot_ip)
            _status("Internet restored", 98)

        # Close hotspot via BLE (may fail if BLE dropped during WiFi)
        try:
            await send_ble_cmd("WiFi_Close", bytes([1, 10, 0, 0]))
        except Exception:
            pass

    # Auto-delete synced files from device if enabled
    auto_delete = load_config().get("auto_delete_after_sync", False)
    deleted_from_device = []
    if auto_delete:
        for f in new_files:
            if f["status"] == "new" and f.get("session_id"):
                try:
                    from app.state.device import delete_device_file, run_async

                    run_async(delete_device_file(f["session_id"]))
                    deleted_from_device.append(f["session_id"])
                except Exception:
                    pass

    result = {"files": new_files, "deleted_from_device": deleted_from_device, "method": "wifi"}
    if error_msg[0]:
        result["warning"] = error_msg[0]
    return result
