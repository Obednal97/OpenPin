#!/usr/bin/env python3
"""WiFi handshake test — full flow: BLE connect → open hotspot → WebSocket handshake.

Corrected handshake from decompiled APK (sh/t0.java, ph/g.java):
- Device sends cmd_id=2 ("SayHello") with {"token": MD5(SN), "pVer": 20}
- App responds with cmd_id=1 with {"token": "<bind_token>", "stamp": <unix_ts>}
- Encrypted with same ChaCha20-Poly1305 keys as BLE session
"""

import asyncio
import base64
import hashlib
import json
import struct
import subprocess
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from src.crypto import get_public_key_full_pem, load_rsa_private_key, rsa_decrypt

# CoreWLAN for fast WiFi switching
from CoreWLAN import CWWiFiClient

import websockets.asyncio.server

# ── Device credentials — read from production app config ──
CONFIG_PATH = Path.home() / "Library/Application Support/Plaud V2/.plaud_v2_config.json"
try:
    with open(CONFIG_PATH) as _f:
        _cfg = json.load(_f)
except FileNotFoundError:
    sys.exit(f"Config not found at {CONFIG_PATH}. Run the Plaud V2 app first to pair a device.")

DEVICE_SN = "8820A0000000XXXX"
BIND_TOKEN = _cfg.get("device_bind_tokens", {}).get(DEVICE_SN)
SN_SIG = _cfg.get("device_sn_signatures", {}).get(DEVICE_SN)
if not BIND_TOKEN or not SN_SIG:
    sys.exit(f"No credentials for SN {DEVICE_SN} in config — re-pair device via the Plaud V2 app first.")

APP_KEY = "0b1ef6247326fc79b200163e0a0431dc"

W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"
WIFI_SSID = f"Plaud{DEVICE_SN[-4:]}"
WIFI_PASS = DEVICE_SN[-8:]
WS_PORT = 8081


# ── WiFi helpers ──

def get_en0_ip():
    try:
        r = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""


def connect_to_hotspot(target_ssid, password, original_ip, timeout=25):
    iface = CWWiFiClient.sharedWiFiClient().interface()
    print(f"    Disconnecting from current WiFi...")
    if iface:
        iface.disassociate()
    for _ in range(10):
        time.sleep(0.5)
        if not get_en0_ip():
            break

    print(f"    Connecting to '{target_ssid}'...")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = subprocess.run(
                ["networksetup", "-setairportnetwork", "en0", target_ssid, password],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            continue

        if r.returncode == 0:
            for _ in range(8):
                time.sleep(1)
                ip = get_en0_ip()
                if ip and ip != original_ip:
                    print(f"    On hotspot! IP={ip} (attempt {attempt})")
                    return ip
            ip = get_en0_ip()
            if ip and ip != original_ip:
                return ip
            if iface:
                iface.disassociate()
            time.sleep(1)
        else:
            err = r.stderr.strip() or r.stdout.strip()
            if attempt % 3 == 1:
                print(f"    Waiting for hotspot... (attempt {attempt})")
            time.sleep(1)
    return None


async def restore_internet(hotspot_ip=""):
    subprocess.run(["networksetup", "-removepreferredwirelessnetwork", "en0", WIFI_SSID], capture_output=True)
    print(f"    WiFi off (10s)...")
    subprocess.run(["networksetup", "-setairportpower", "en0", "off"], capture_output=True, timeout=5)
    await asyncio.sleep(10)
    print(f"    WiFi on — waiting for auto-join...")
    subprocess.run(["networksetup", "-setairportpower", "en0", "on"], capture_output=True, timeout=5)
    for i in range(20):
        await asyncio.sleep(1.5)
        ip = get_en0_ip()
        if ip and ip != hotspot_ip:
            print(f"    Internet restored! IP={ip} ({int((i+1)*1.5)}s)")
            return True
    print(f"    WARNING: Auto-join slow. Check WiFi manually.")
    return False


# ── WiFi protocol ──

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
        json_data = json_bytes.decode("ascii", errors="replace")
    return cmd_id, json_data


# ── BLE handshake ──

async def ble_handshake(cl):
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
                W,
                struct.pack("<H", 65040) + bytes([tc, i]) + sig[i * 100 : (i + 1) * 100],
                response=False,
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
                W,
                struct.pack("<H", 65042) + bytes([pc, i]) + pem[i * 100 : (i + 1) * 100],
                response=True,
            )
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
    assert ChaCha20Poly1305(key).decrypt(nonce, ct, aad) == b"PLAUD.AI"
    await cl.stop_notify(N)
    return key, nonce, aad


# ── Main flow ──

async def main():
    print("=" * 60)
    print("  WIFI HANDSHAKE TEST (corrected from decompiled APK)")
    print("=" * 60)

    expected_token = hashlib.md5(DEVICE_SN.encode()).hexdigest().upper()
    original_ip = get_en0_ip()
    print(f"\n  Device SN: {DEVICE_SN}")
    print(f"  Expected device token: {expected_token}")
    print(f"  Hotspot: {WIFI_SSID} / {WIFI_PASS}")
    print(f"  Current IP: {original_ip}")

    # ── [1] BLE CONNECT ──
    print("\n[1] BLE Connect + Handshake...")
    target = None
    for attempt in range(10):
        found = await BleakScanner.discover(timeout=5, return_adv=True)
        for a, (d, adv) in found.items():
            if 0x5D in adv.manufacturer_data:
                target = d
        if target:
            break
        print(f"    scan {attempt + 1}...")
        await asyncio.sleep(3)
    if not target:
        print("    Device not found!")
        return

    cl = BleakClient(target, timeout=30.0)
    await cl.connect()
    print(f"    Connected (MTU={cl.mtu_size})")

    keys = await ble_handshake(cl)
    if not keys:
        print("    Handshake failed!")
        await cl.disconnect()
        return
    key, nonce, aad = keys
    print("    Session keys established!")

    cipher = ChaCha20Poly1305(key)
    seq = [0]

    def enc(pt):
        seq[0] += 1
        return cipher.encrypt(nonce, struct.pack("<I", seq[0]) + pt, aad)

    # Set up BLE command channel
    cmd_evt = asyncio.Event()
    cmd_resp = [None]

    def handler(s, d):
        try:
            dd = cipher.decrypt(nonce, bytes(d), aad)[4:]
        except Exception:
            return
        if len(dd) < 1:
            return
        if dd[0] == 1:
            cmd_resp[0] = dd
            cmd_evt.set()

    await cl.start_notify(N, handler)

    async def cmd(name, pt, to=5):
        cmd_evt.clear()
        cmd_resp[0] = None
        await cl.write_gatt_char(W, enc(pt), response=False)
        try:
            await asyncio.wait_for(cmd_evt.wait(), timeout=to)
        except Exception:
            pass
        r = cmd_resp[0]
        print(f"    {name}: {'OK' if r else 'no response'}")
        return r

    token32 = BIND_TOKEN[:32].encode("ascii")
    await cmd("HS1", bytes([1, 1, 0, 2, 0, 0]) + token32)
    await cmd("HS2", bytes([1, 1, 0, 2, 0, 1]) + token32)
    await cmd("Sync", bytes([1, 4, 0]) + struct.pack("<I", int(time.time())) + bytes([0, 0]))

    # ── [2] START WEBSOCKET SERVER ──
    print("\n[2] Starting WebSocket server...")
    ws_seq = [0]
    handshake_done = asyncio.Event()
    file_list_received = asyncio.Event()
    file_list_data = [None]
    transfer_complete = asyncio.Event()
    download_buf = bytearray()
    download_complete = asyncio.Event()
    downloading_sid = [0]

    def ws_encrypt(payload):
        ws_seq[0] += 1
        return cipher.encrypt(nonce, struct.pack("<I", ws_seq[0]) + payload, aad)

    def ws_decrypt(data):
        return cipher.decrypt(nonce, data, aad)[4:]

    async def ws_handler(websocket):
        nonlocal ws_seq
        addr = websocket.remote_address
        print(f"\n  [WS] Device connected from {addr}")
        # Reset sequence for each new connection
        ws_seq[0] = 0

        msg_count = 0
        try:
            async for message in websocket:
                if not isinstance(message, bytes):
                    continue

                try:
                    decrypted = ws_decrypt(message)
                except Exception as e:
                    print(f"  [WS] Decrypt failed: {e}")
                    continue

                cmd_id, json_data = parse_wifi_packet(decrypted)
                msg_count += 1

                if cmd_id == 2:
                    # SayHello from device
                    token = json_data.get("token", "") if isinstance(json_data, dict) else ""
                    pver = json_data.get("pVer", 0) if isinstance(json_data, dict) else 0
                    print(f"  [WS] SayHello #{msg_count}: token={token[:16]}..., pVer={pver}")

                    # Always respond to handshake (device may retry on new connection)
                    stamp = int(time.time())
                    response = build_wifi_packet(1, {"token": BIND_TOKEN, "stamp": stamp})
                    await websocket.send(ws_encrypt(response))
                    print(f"  [WS] → Sent handshake: cmd_id=1, stamp={stamp}")

                elif cmd_id == 1:
                    # Handshake ACK from device!
                    print(f"  [WS] ✓ HANDSHAKE ACCEPTED! Response: {json_data}")
                    handshake_done.set()

                    # Request file list — corrected format from decompiled ph/f.java:
                    # {"uid": <unix_ts>, "start": 0, "single": 0}
                    await asyncio.sleep(0.3)
                    uid = int(time.time())
                    fl_req = build_wifi_packet(11, {"uid": uid, "start": 0, "single": 0})
                    await websocket.send(ws_encrypt(fl_req))
                    print(f"  [WS] → Sent file list request (cmd_id=11, uid={uid})")

                elif cmd_id == 11:
                    # File list response: {"uid": ..., "status": 0, "count": N}
                    # May also have binary data with file entries after JSON
                    print(f"  [WS] File list response: {json_data}")
                    if isinstance(json_data, dict):
                        status = json_data.get("status", -1)
                        count = json_data.get("count", 0)
                        print(f"  [WS] File list status={status}, count={count}")
                        if status == 0 and count > 0:
                            file_list_data[0] = json_data
                            # Parse binary file entries after JSON header
                            # Each file: sessionId(u32) + fileSize(u32) + codecType(u16) = 10 bytes
                            total_len_val = decrypted[0] | (decrypted[1] << 8) | (decrypted[2] << 16)
                            json_len_val2 = struct.unpack_from("<H", decrypted, 6)[0]
                            bin_start = 8 + json_len_val2
                            bin_data = decrypted[bin_start:]
                            files = []
                            if bin_data:
                                for i in range(0, len(bin_data) - 9, 10):
                                    sid = struct.unpack_from("<I", bin_data, i)[0]
                                    fsize = struct.unpack_from("<I", bin_data, i + 4)[0]
                                    codec = struct.unpack_from("<H", bin_data, i + 8)[0]
                                    files.append({"session_id": sid, "size": fsize, "codec": codec})
                                    print(f"  [WS]   File: session={sid}, size={fsize}, codec={codec}")
                                file_list_data[0]["files"] = files
                                print(f"  [WS] Parsed {len(files)} file(s) from binary data")
                            file_list_received.set()

                            # Download the SMALLEST file first as a test
                            if files:
                                target = min(files, key=lambda f: f["size"])
                                downloading_sid[0] = target["session_id"]
                                download_buf.clear()
                                await asyncio.sleep(0.2)
                                # cmd_id=12: {"session": id, "scene": 0, "start": 0, "end": fileSize}
                                dl_req = build_wifi_packet(12, {
                                    "session": target["session_id"],
                                    "scene": 0,
                                    "start": 0,
                                    "end": target["size"],
                                })
                                await websocket.send(ws_encrypt(dl_req))
                                print(f"  [WS] → Requested download: session={target['session_id']}, size={target['size']}")
                        elif status == 0 and count == 0:
                            print(f"  [WS] No files on device")
                            file_list_received.set()
                            transfer_complete.set()
                        else:
                            print(f"  [WS] File list error status={status}")
                    else:
                        print(f"  [WS] Unexpected file list format: {json_data}")

                elif cmd_id == 12:
                    # File sync data — binary audio data after JSON header
                    if isinstance(json_data, dict):
                        status = json_data.get("status", -1)
                        session = json_data.get("session", 0)
                        print(f"  [WS] FileSync response: session={session}, status={status}, data={str(json_data)[:150]}")
                    # Accumulate raw binary data after JSON
                    total_len = decrypted[0] | (decrypted[1] << 8) | (decrypted[2] << 16)
                    json_len_val = struct.unpack_from("<H", decrypted, 6)[0]
                    bin_payload = decrypted[8 + json_len_val:]
                    if bin_payload:
                        download_buf.extend(bin_payload)
                        if msg_count % 50 == 0 or len(download_buf) < 1000:
                            print(f"  [WS] FileSync data: +{len(bin_payload)}B, total={len(download_buf)}B")

                elif cmd_id == 13:
                    # File data chunk: {"session": id, "offset": N, "length": 1280, "last": 0|1}
                    # Binary audio data follows after the JSON header
                    total_len_c13 = decrypted[0] | (decrypted[1] << 8) | (decrypted[2] << 16)
                    json_len_c13 = struct.unpack_from("<H", decrypted, 6)[0]
                    chunk_data = decrypted[8 + json_len_c13:]
                    if chunk_data:
                        download_buf.extend(chunk_data)
                    is_last = json_data.get("last", 0) if isinstance(json_data, dict) else 0
                    offset = json_data.get("offset", 0) if isinstance(json_data, dict) else 0
                    if msg_count % 20 == 0 or is_last:
                        print(f"  [WS] FileData: offset={offset}, +{len(chunk_data)}B, total={len(download_buf)}B{' [LAST]' if is_last else ''}")
                    if is_last:
                        print(f"  [WS] Download complete: {len(download_buf)} bytes")
                        out_path = Path("recordings") / f"wifi_{downloading_sid[0]}.bin"
                        out_path.parent.mkdir(exist_ok=True)
                        out_path.write_bytes(bytes(download_buf))
                        print(f"  [WS] Saved: {out_path}")
                        download_complete.set()

                elif cmd_id == 3:
                    # Heartbeat ping — respond with cmd_id=16 (empty pong)
                    pong = build_wifi_packet(16, {})
                    await websocket.send(ws_encrypt(pong))
                    print(f"  [WS] Heartbeat ping → pong")

                elif cmd_id == 0:
                    # Error/status from device
                    print(f"  [WS] Status: {json_data}")

                elif cmd_id is None:
                    # Raw data (no valid packet header) — might be file stream
                    download_buf.extend(decrypted)
                    if len(download_buf) % 50000 < len(decrypted):
                        print(f"  [WS] Raw data stream: total={len(download_buf)}B")

                else:
                    print(f"  [WS] cmd_id={cmd_id}: {str(json_data)[:200]}")
                    print(f"  [WS]   raw ({len(decrypted)}B): {decrypted[:80].hex()}")

        except Exception as e:
            print(f"  [WS] Connection error: {e}")

        print(f"  [WS] Session ended ({msg_count} messages)")
        transfer_complete.set()

    server = await websockets.asyncio.server.serve(ws_handler, "0.0.0.0", WS_PORT)
    print(f"    Listening on 0.0.0.0:{WS_PORT}")
    await asyncio.sleep(0.5)

    # ── [3] OPEN HOTSPOT ──
    print("\n[3] Opening WiFi hotspot...")
    await cmd("WiFi_Open", bytes([1, 10, 0]))

    # Stop BLE notifications during WiFi
    try:
        await cl.stop_notify(N)
    except Exception:
        pass

    # ── [4] CONNECT TO HOTSPOT ──
    print(f"\n[4] Connecting to {WIFI_SSID}...")
    t0 = time.time()
    hotspot_ip = connect_to_hotspot(WIFI_SSID, WIFI_PASS, original_ip, timeout=25)
    elapsed = time.time() - t0

    if hotspot_ip:
        print(f"    Connected in {elapsed:.1f}s, IP={hotspot_ip}")

        # ── [5] WAIT FOR HANDSHAKE + FILE LIST ──
        print(f"\n[5] Waiting for device WebSocket connection + handshake...")
        try:
            await asyncio.wait_for(handshake_done.wait(), timeout=30)
            print(f"\n  ✓ HANDSHAKE COMPLETE! Waiting for file list...")

            # Wait for file list
            try:
                await asyncio.wait_for(file_list_received.wait(), timeout=30)
                print(f"\n  ✓ FILE LIST RECEIVED!")
            except asyncio.TimeoutError:
                print(f"\n  File list not received within 30s")

            # Wait for download to complete (up to 120s for large files)
            if file_list_data[0] and file_list_data[0].get("files"):
                print(f"\n  Downloading file... (waiting up to 120s)")
                try:
                    await asyncio.wait_for(download_complete.wait(), timeout=120)
                    print(f"\n  ✓ DOWNLOAD COMPLETE! {len(download_buf)} bytes")
                except asyncio.TimeoutError:
                    print(f"\n  Download timed out. Got {len(download_buf)} bytes so far")
                    if download_buf:
                        out_path = Path("recordings") / f"wifi_{downloading_sid[0]}_partial.bin"
                        out_path.parent.mkdir(exist_ok=True)
                        out_path.write_bytes(bytes(download_buf))
                        print(f"  Saved partial: {out_path}")
            else:
                # Wait a bit for any remaining data
                try:
                    await asyncio.wait_for(transfer_complete.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
        except asyncio.TimeoutError:
            print(f"\n  ✗ Handshake did NOT complete within 30s")
    else:
        print(f"    Failed to connect to hotspot after {elapsed:.1f}s")

    # ── [6] CLEANUP ──
    print(f"\n[6] Restoring internet...")
    server.close()
    await restore_internet(hotspot_ip=hotspot_ip or "")

    # Close hotspot via BLE
    print(f"\n[7] Closing hotspot...")
    try:
        await cl.start_notify(N, handler)
        await cmd("WiFi_Close", bytes([1, 10, 0, 0]))
        await cl.stop_notify(N)
    except Exception as e:
        print(f"    (BLE may have dropped: {e})")

    try:
        await cl.disconnect()
    except Exception:
        pass

    print(f"\n{'=' * 60}")
    if handshake_done.is_set():
        print(f"  SUCCESS — WiFi handshake works with cmd_id=1 + BIND_TOKEN!")
    else:
        print(f"  FAILED — handshake not accepted. Next: try APP_KEY as token.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
