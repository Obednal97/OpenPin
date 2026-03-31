#!/usr/bin/env python3
"""WiFi sync worker — runs as standalone subprocess.

Called by the app with session keys and serial number as JSON arg.
Outputs JSON result to stdout. Progress updates to stderr as JSON lines.

Usage: python3 wifi_sync_worker.py '{"key":"...","nonce":"...","aad":"...","sn":"...","recordings_dir":"..."}'
"""

import asyncio
import json
import struct
import subprocess
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import load_pem_private_key

WS_PORT = 8081
BIND_TOKEN = None  # Set from args — per-device token from cloud API


def status(msg, pct):
    """Send progress update to parent via stderr."""
    print(json.dumps({"status": msg, "percent": pct}), file=sys.stderr, flush=True)


def build_wifi_packet(cmd_id, json_data, protocol_version=2):
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


def get_en0_ip():
    try:
        r = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or ""
    except Exception:
        return ""


def connect_to_hotspot(ssid, password, original_ip, timeout=25):
    from CoreWLAN import CWWiFiClient
    iface = CWWiFiClient.sharedWiFiClient().interface()
    if iface:
        iface.disassociate()
    for _ in range(10):
        time.sleep(0.5)
        if not get_en0_ip():
            break

    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = subprocess.run(
                ["networksetup", "-setairportnetwork", "en0", ssid, password],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0:
            for _ in range(8):
                time.sleep(1)
                ip = get_en0_ip()
                if ip and ip != original_ip:
                    return ip
            ip = get_en0_ip()
            if ip and ip != original_ip:
                return ip
            if iface:
                iface.disassociate()
            time.sleep(1)
        else:
            time.sleep(1)
    return None


def get_preferred_networks(exclude):
    try:
        r = subprocess.run(["networksetup", "-listpreferredwirelessnetworks", "en0"], capture_output=True, text=True, timeout=5)
        return [n.strip() for n in r.stdout.strip().split("\n")[1:] if n.strip() and n.strip() != exclude]
    except Exception:
        return []


async def restore_internet(ssid, hotspot_ip, original_ip="", original_ssid=""):
    """Restore internet after Plaud hotspot.

    Strategy (ordered by least disruption first):
    1. Remove Plaud hotspot from preferred, try networksetup rejoin (uses keychain password)
    2. If that fails, power-cycle WiFi (10s off) + CoreWLAN scan + associate
    3. Second power-cycle (5s off) + networksetup again
    4. If all fail, open WiFi settings for manual reconnection
    """
    # Step 0: Remove Plaud hotspot from preferred networks
    subprocess.run(["networksetup", "-removepreferredwirelessnetwork", "en0", ssid], capture_output=True)

    try:
        from CoreWLAN import CWWiFiClient
        iface = CWWiFiClient.sharedWiFiClient().interface()
    except Exception:
        iface = None

    # Step 1: Try networksetup first (uses saved keychain password, no power-cycle needed)
    if original_ssid:
        status(f"Reconnecting to {original_ssid}...", 92)
        subprocess.run(
            ["networksetup", "-setairportnetwork", "en0", original_ssid],
            capture_output=True, timeout=15,
        )
        await asyncio.sleep(5)
        ip = get_en0_ip()
        if ip and ip != hotspot_ip:
            status(f"Restored to {original_ssid} ({ip})", 98)
            return {"restored": True, "ssid": original_ssid, "ip": ip}

    # Step 2-3: Power-cycle WiFi radio + try multiple rejoin methods
    for cycle in range(2):
        off_time = 10 if cycle == 0 else 5
        settle_time = 5 if cycle == 0 else 8
        status(f"Power-cycling WiFi (attempt {cycle + 1}/2)...", 93 + cycle * 2)
        subprocess.run(["networksetup", "-setairportpower", "en0", "off"], capture_output=True, timeout=5)
        await asyncio.sleep(off_time)
        subprocess.run(["networksetup", "-setairportpower", "en0", "on"], capture_output=True, timeout=5)
        await asyncio.sleep(settle_time)

        # Try networksetup rejoin (uses keychain)
        if original_ssid:
            status(f"Rejoining {original_ssid}...", 94 + cycle * 2)
            subprocess.run(
                ["networksetup", "-setairportnetwork", "en0", original_ssid],
                capture_output=True, timeout=15,
            )
            await asyncio.sleep(3)
            ip = get_en0_ip()
            if ip and ip != hotspot_ip:
                status(f"Restored to {original_ssid} ({ip})", 98)
                return {"restored": True, "ssid": original_ssid, "ip": ip}

        # Try CoreWLAN scan + join as fallback
        if original_ssid and iface:
            status(f"Scanning for {original_ssid}...", 95 + cycle)
            try:
                networks, _ = iface.scanForNetworksWithName_error_(original_ssid, None)
                if networks:
                    best = None
                    enumerator = networks.objectEnumerator()
                    while True:
                        obj = enumerator.nextObject()
                        if obj is None:
                            break
                        if obj.ssid() and str(obj.ssid()) == original_ssid:
                            if best is None or obj.rssiValue() > best.rssiValue():
                                best = obj
                    if best:
                        success, error = iface.associateToNetwork_password_error_(best, None, None)
                        if success:
                            await asyncio.sleep(3)
                            ip = get_en0_ip()
                            if ip and ip != hotspot_ip:
                                status(f"Restored to {original_ssid} ({ip})", 98)
                                return {"restored": True, "ssid": original_ssid, "ip": ip}
            except Exception:
                pass

        # Check if macOS auto-joined on its own
        ip = get_en0_ip()
        if ip and ip != hotspot_ip:
            current = str(iface.ssid()) if iface and iface.ssid() else ""
            if not original_ip or ip == original_ip:
                status(f"Restored to {current or 'network'} ({ip})", 98)
                return {"restored": True, "ssid": current, "ip": ip}

    # All automatic methods failed — open WiFi settings for user
    status("Opening WiFi settings...", 96)
    subprocess.run(["open", "x-apple.systempreferences:com.apple.wifi-settings"], capture_output=True)
    msg = f"Please reconnect to {original_ssid}" if original_ssid else "Please reconnect to your WiFi"
    status(msg, 97)
    return {"restored": False, "need_manual": True, "ssid": original_ssid, "message": msg}


SN_SIG = "<REDACTED_SN_SIGNATURE>"
W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"


async def ble_connect_and_setup():
    """Full BLE connect + handshake + setup, returns (client, key, nonce, aad, cipher, enc_func)."""
    import base64

    from bleak import BleakClient, BleakScanner

    from src.crypto import get_public_key_full_pem, load_rsa_private_key, rsa_decrypt

    status("BLE scanning...", 2)
    target = None
    for attempt in range(10):
        found = await BleakScanner.discover(timeout=5, return_adv=True)
        for a, (d, adv) in found.items():
            if 0x5D in adv.manufacturer_data:
                target = d
        if target:
            break
        status(f"BLE scan {attempt + 1}...", 2)
    if not target:
        return None

    cl = BleakClient(target, timeout=30.0)
    await cl.connect()
    status("BLE connected, handshaking...", 3)

    # RSA handshake
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
            await cl.write_gatt_char(W, struct.pack("<H", 65040) + bytes([tc, i]) + sig[i*100:(i+1)*100], response=False)
            await asyncio.sleep(0.02)
    for _ in range(50):
        await asyncio.sleep(0.1)
        if ack.is_set() or done.is_set():
            break
    if ack.is_set() and not done.is_set():
        pem = get_public_key_full_pem().encode()
        pc = (len(pem) + 99) // 100
        for i in range(pc):
            await cl.write_gatt_char(W, struct.pack("<H", 65042) + bytes([pc, i]) + pem[i*100:(i+1)*100], response=True)
            await asyncio.sleep(0.02)
        await asyncio.wait_for(done.wait(), timeout=15)
    elif not done.is_set():
        await cl.stop_notify(N)
        return None
    blob = bytearray()
    for i in sorted(rsa.keys()):
        blob.extend(rsa[i])
    dec = rsa_decrypt(bytes(blob), load_rsa_private_key())
    key, nonce, aad, ct = dec[:32], dec[32:44], dec[44:56], dec[56:]
    cipher = ChaCha20Poly1305(key)
    assert cipher.decrypt(nonce, ct, aad) == b"PLAUD.AI"
    await cl.stop_notify(N)

    # Setup encrypted command channel
    seq = [0]

    def enc(pt):
        seq[0] += 1
        return cipher.encrypt(nonce, struct.pack("<I", seq[0]) + pt, aad)

    cmd_evt = asyncio.Event()
    cmd_resp = [None]

    def handler(s, d):
        try:
            dd = cipher.decrypt(nonce, bytes(d), aad)[4:]
        except Exception:
            return
        if len(dd) >= 1 and dd[0] == 1:
            cmd_resp[0] = dd
            cmd_evt.set()
    await cl.start_notify(N, handler)

    async def cmd(name, pt):
        cmd_evt.clear()
        cmd_resp[0] = None
        await cl.write_gatt_char(W, enc(pt), response=False)
        try:
            await asyncio.wait_for(cmd_evt.wait(), timeout=5)
        except Exception:
            pass
        return cmd_resp[0]

    # BLE setup commands
    token32 = BIND_TOKEN[:32].encode("ascii")
    await cmd("HS1", bytes([1, 1, 0, 2, 0, 0]) + token32)
    await cmd("HS2", bytes([1, 1, 0, 2, 0, 1]) + token32)
    await cmd("Sync", bytes([1, 4, 0]) + struct.pack("<I", int(time.time())) + bytes([0, 0]))
    status("BLE ready", 4)

    return cl, key, nonce, aad, cipher, cmd


async def main():
    global BIND_TOKEN
    args = json.loads(sys.argv[1])
    sn = args["sn"]
    recordings_dir = Path(args["recordings_dir"])
    app_key_path = Path(args["app_key_path"])
    dismissed = set(args.get("dismissed", []))
    BIND_TOKEN = args.get("bind_token")
    if not BIND_TOKEN:
        print(json.dumps({"error": "No BIND_TOKEN — user must log into Plaud cloud first"}))
        return

    # Do our own BLE connection (device invalidates sessions when BLE drops)
    ble = await ble_connect_and_setup()
    if not ble:
        print(json.dumps({"error": "BLE connection failed"}))
        return
    cl, key, nonce, aad, cipher, ble_cmd = ble

    app_rsa_key = load_pem_private_key(app_key_path.read_bytes(), password=None)

    wifi_ssid = f"Plaud{sn[-4:]}"
    wifi_pass = sn[-8:]
    original_ip = get_en0_ip()

    # Save current SSID before transfer (requires Location permission)
    original_ssid = ""
    try:
        from CoreWLAN import CWWiFiClient
        _iface = CWWiFiClient.sharedWiFiClient().interface()
        if _iface and _iface.ssid():
            original_ssid = str(_iface.ssid())
            status(f"Current network: {original_ssid}", 3)
    except Exception:
        pass

    ws_seq = 0
    handshake_done = asyncio.Event()
    file_list_received = asyncio.Event()
    all_downloads_done = asyncio.Event()
    wifi_files = []
    new_files = []

    def ws_encrypt(payload):
        nonlocal ws_seq
        ws_seq += 1
        return cipher.encrypt(nonce, struct.pack("<I", ws_seq) + payload, aad)

    def ws_decrypt(data):
        return cipher.decrypt(nonce, data, aad)[4:]

    import websockets.asyncio.server

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
                ogg_path = recordings_dir / f"{sid}.ogg"
                if str(sid) in dismissed or ogg_path.exists():
                    new_files.append({"session_id": sid, "ogg_path": str(ogg_path) if ogg_path.exists() else None, "duration": 0, "status": "exists"})
                    continue
                current_sid = sid
                dl_req = build_wifi_packet(12, {"session": sid, "scene": 0, "start": 0, "end": f["size"]})
                await websocket.send(ws_encrypt(dl_req))
                status(f"Downloading session {sid}...", 40)
                return True
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
                    stamp = int(time.time())
                    response = build_wifi_packet(1, {"token": BIND_TOKEN, "stamp": stamp})
                    await websocket.send(ws_encrypt(response))

                elif cmd_id == 1:
                    handshake_done.set()
                    await asyncio.sleep(0.3)
                    uid = int(time.time())
                    fl_req = build_wifi_packet(11, {"uid": uid, "start": 0, "single": 0})
                    await websocket.send(ws_encrypt(fl_req))

                elif cmd_id == 11:
                    count = json_data.get("count", 0) if isinstance(json_data, dict) else 0
                    s = json_data.get("status", -1) if isinstance(json_data, dict) else -1
                    if s == 0 and count > 0:
                        json_len_val = struct.unpack_from("<H", decrypted, 6)[0]
                        bin_data = decrypted[8 + json_len_val:]
                        for i in range(0, len(bin_data) - 9, 10):
                            sid = struct.unpack_from("<I", bin_data, i)[0]
                            fsize = struct.unpack_from("<I", bin_data, i + 4)[0]
                            codec = struct.unpack_from("<H", bin_data, i + 8)[0]
                            wifi_files.append({"session_id": sid, "size": fsize, "codec": codec})
                    file_list_received.set()
                    file_queue = list(wifi_files)
                    status(f"{len(wifi_files)} file(s) found", 35)
                    if not file_queue:
                        all_downloads_done.set()
                    else:
                        await download_next()

                elif cmd_id == 12:
                    pass

                elif cmd_id == 13:
                    json_len_c = struct.unpack_from("<H", decrypted, 6)[0]
                    chunk = decrypted[8 + json_len_c:]
                    if chunk:
                        download_buf.extend(chunk)
                    is_last = json_data.get("last", 0) if isinstance(json_data, dict) else 0
                    if is_last:
                        # Decrypt and save
                        raw = bytes(download_buf)
                        recordings_dir.mkdir(exist_ok=True)

                        # Import decrypt from project
                        sys.path.insert(0, str(app_key_path.parent.parent))
                        from app.utils.audio import decrypt_ble_file, wrap_opus_ogg

                        frames = decrypt_ble_file(raw, app_rsa_key)
                        if frames:
                            ogg = wrap_opus_ogg(frames)
                            ogg_path = recordings_dir / f"{current_sid}.ogg"
                            ogg_path.write_bytes(ogg)
                            duration = len(frames) // 80 * 0.02
                            new_files.append({"session_id": current_sid, "ogg_path": str(ogg_path), "duration": round(duration, 1), "status": "new"})
                            done = sum(1 for f in new_files if f["status"] == "new")
                            total = len(wifi_files)
                            pct = 35 + int(55 * done / max(total, 1))
                            status(f"Downloaded {done}/{total}", pct)
                        else:
                            raw_path = recordings_dir / f"{current_sid}_encrypted.bin"
                            raw_path.write_bytes(raw)
                            new_files.append({"session_id": current_sid, "ogg_path": None, "duration": 0, "status": "decrypt_failed"})
                        await download_next()

                elif cmd_id == 3:
                    pong = build_wifi_packet(16, {})
                    await websocket.send(ws_encrypt(pong))

        except Exception:
            if not all_downloads_done.is_set():
                all_downloads_done.set()

    # ── Execute ──
    hotspot_ip = None
    server = None
    restore_result = None
    try:
        status("Starting WebSocket server...", 5)
        server = await websockets.asyncio.server.serve(ws_handler, "0.0.0.0", WS_PORT)

        status("Opening device hotspot...", 10)
        await ble_cmd("WiFi_Open", bytes([1, 10, 0]))

        # Stop BLE notifications — they conflict with WiFi
        try:
            await cl.stop_notify(N)
        except Exception:
            pass

        status(f"Connecting to {wifi_ssid}...", 15)
        hotspot_ip = connect_to_hotspot(wifi_ssid, wifi_pass, original_ip, timeout=25)
        if not hotspot_ip:
            print(json.dumps({"error": "Could not connect to hotspot", "files": new_files}))
            return

        status("Waiting for handshake...", 25)
        try:
            await asyncio.wait_for(handshake_done.wait(), timeout=30)
        except TimeoutError:
            print(json.dumps({"error": "Handshake timed out", "files": new_files}))
            return

        status("Getting file list...", 30)
        try:
            await asyncio.wait_for(file_list_received.wait(), timeout=15)
        except TimeoutError:
            print(json.dumps({"error": "File list timed out", "files": new_files}))
            return

        total_size = sum(f["size"] for f in wifi_files)
        dl_timeout = max(total_size / 50000, 60)
        try:
            await asyncio.wait_for(all_downloads_done.wait(), timeout=dl_timeout)
        except TimeoutError:
            done_so_far = sum(1 for f in new_files if f["status"] == "new")
            total_expected = len(wifi_files)
            status(f"Download timed out: got {done_so_far}/{total_expected} files", 88)

        done_count = sum(1 for f in new_files if f["status"] == "new")
        status(f"Download complete: {done_count} file(s)", 90)

    finally:
        if server:
            await asyncio.sleep(1)
            server.close()
        if hotspot_ip:
            restore_result = await restore_internet(wifi_ssid, hotspot_ip, original_ip, original_ssid)

        # Close hotspot + disconnect BLE
        try:
            await cl.start_notify(N, lambda s, d: None)
            await ble_cmd("WiFi_Close", bytes([1, 10, 0, 0]))
            await cl.stop_notify(N)
        except Exception:
            pass
        try:
            await cl.disconnect()
        except Exception:
            pass

    result = {"files": new_files, "method": "wifi"}
    if restore_result:
        result["wifi_restore"] = restore_result
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
