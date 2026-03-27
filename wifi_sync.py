#!/usr/bin/env python3
"""Plaud WiFi sync — fully automated end-to-end.

1. BLE connect + handshake
2. Record audio
3. Open WiFi hotspot
4. Start WebSocket server, auto-connect to hotspot
5. WiFi handshake + file transfer
6. Auto-disconnect hotspot, reconnect original WiFi
7. Stay paired (no depair)
"""
import asyncio, struct, base64, json, time, os, sys, subprocess
from pathlib import Path
from datetime import datetime
from bleak import BleakScanner, BleakClient
from src.crypto import get_public_key_full_pem, rsa_decrypt, load_rsa_private_key
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
import websockets
import websockets.asyncio.server

SN_SIG = "TOb4HuuFPMhEB8bMCR3SFlqTRO9ihuhsHp0Jp3bwBGDYpew7FI9rFykg2JUKGLfe50BJS4z6Esm2Mt7Bqv6e5nz0j6jRCKWVWTuNiFY6a2iGY2lAvghwP8+JrqHb2RlTgoCj+vYXxAlMpZ5eMFCmTudiCmJt4aPzrJFURyM8OJfVJ8IHZi2/WbkR/i0vLrVuuLO3aShZrFjR7Enw0MQYUl4RMnHxk4qEwr7MV7qSTjyA7qeYcZY09gfS9+GmbVhwU4ZS4doiD+8gk6AmOn4zn20C3sCS1EOo3KGJgTNzSdZA59d/YQLql3bTjL3VsJYVvQlP5Zzfa5nHLcd3fN588w=="
BIND_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2NjllNzgwNDE4ZDg0OWRhOGFmMDU2MzIzOTljMjVhZSIsImF1ZCI6IiIsImV4cCI6MjAzMzczMzY5NSwiaWF0IjoxNzc0NTMzNjk1LCJjbGllbnRfaWQiOiJkZXZpY2UiLCJzbiI6Ijg4MjBCNTAyNTY5NDczMTcifQ.UJH_zwN0Imyc4W8dropHXy1_j7kN09aJf-2ZUtyLEJs"
W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"

DEVICE_SN = "8820B50256947317"
WIFI_SSID = f"Plaud{DEVICE_SN[-4:]}"
WIFI_PASS = DEVICE_SN[-8:]
WS_PORT = 8081

SESSIONS_DIR = Path("sessions")
RECORDINGS_DIR = Path("recordings")


def get_wifi():
    try:
        r = subprocess.run(["networksetup", "-getairportnetwork", "en0"], capture_output=True, text=True)
        if "Current Wi-Fi Network:" in r.stdout:
            return r.stdout.split("Current Wi-Fi Network:")[1].strip()
    except: pass
    return None


def join_wifi(ssid, password=""):
    print(f"    Joining WiFi '{ssid}'...")
    r = subprocess.run(["networksetup", "-setairportnetwork", "en0", ssid, password], capture_output=True, text=True)
    return r.returncode == 0


def build_wifi_packet(cmd_id, json_data, protocol_version=2):
    """Build a WiFi WebSocket packet: [total_len u24 LE][pver u8][cmd_id u16 LE][json_len u16 LE][json]"""
    json_bytes = json.dumps(json_data).encode("ascii")
    total_len = 8 + len(json_bytes)
    header = bytearray(8)
    # 24-bit LE total length
    header[0] = total_len & 0xFF
    header[1] = (total_len >> 8) & 0xFF
    header[2] = (total_len >> 16) & 0xFF
    # 8-bit protocol version
    header[3] = protocol_version
    # 16-bit LE cmd_id
    struct.pack_into("<H", header, 4, cmd_id)
    # 16-bit LE json length
    struct.pack_into("<H", header, 6, len(json_bytes))
    return bytes(header) + json_bytes


def parse_wifi_packet(data):
    """Parse a WiFi packet into (cmd_id, json_data)."""
    if len(data) < 8:
        return None, None
    total_len = data[0] | (data[1] << 8) | (data[2] << 16)
    pver = data[3]
    cmd_id = struct.unpack_from("<H", data, 4)[0]
    json_len = struct.unpack_from("<H", data, 6)[0]
    json_bytes = data[8:8 + json_len]
    try:
        json_data = json.loads(json_bytes)
    except:
        json_data = json_bytes.decode("ascii", errors="replace")
    return cmd_id, json_data


async def handshake(cl):
    ack = asyncio.Event(); rsa = {}; done = asyncio.Event()
    def h(s, d):
        d = bytes(d); cid = struct.unpack_from("<H", d, 0)[0] if len(d) >= 2 else 0
        if cid == 65041: ack.set()
        elif cid == 65042 and len(d) >= 4:
            idx = d[3]
            if idx not in rsa: rsa[idx] = d[4:]
            if len(rsa) >= d[2]: done.set()
    await cl.start_notify(N, h)
    sig = base64.b64decode(SN_SIG); tc = (len(sig) + 99) // 100
    for _ in range(2):
        for i in range(tc):
            await cl.write_gatt_char(W, struct.pack("<H", 65040) + bytes([tc, i]) + sig[i*100:(i+1)*100], response=False)
            await asyncio.sleep(0.02)
    for _ in range(50):
        await asyncio.sleep(0.1)
        if ack.is_set() or done.is_set(): break
    if ack.is_set() and not done.is_set():
        pem = get_public_key_full_pem().encode(); pc = (len(pem) + 99) // 100
        for i in range(pc):
            await cl.write_gatt_char(W, struct.pack("<H", 65042) + bytes([pc, i]) + pem[i*100:(i+1)*100], response=True)
            await asyncio.sleep(0.02)
        await asyncio.wait_for(done.wait(), timeout=15)
    elif not done.is_set():
        await cl.stop_notify(N); return None
    blob = bytearray()
    for i in sorted(rsa.keys()): blob.extend(rsa[i])
    dec = rsa_decrypt(bytes(blob), load_rsa_private_key())
    key, nonce, aad, ct = dec[:32], dec[32:44], dec[44:56], dec[56:]
    assert ChaCha20Poly1305(key).decrypt(nonce, ct, aad) == b"PLAUD.AI"
    await cl.stop_notify(N)
    SESSIONS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json.dump({"key": key.hex(), "nonce": nonce.hex(), "aad": aad.hex(), "status": "ACTIVE",
               "timestamp": datetime.now().isoformat()},
              open(f"sessions/session_{ts}.json", "w"), indent=2)
    json.dump({"key": key.hex(), "nonce": nonce.hex(), "aad": aad.hex(), "status": "ACTIVE"},
              open("session_keys.json", "w"), indent=2)
    return key, nonce, aad


async def wifi_transfer(key, nonce, aad):
    """Run WebSocket server, handle WiFi protocol, transfer files."""
    cipher = ChaCha20Poly1305(key)
    ws_send_seq = [0]
    received_files = {}
    transfer_complete = asyncio.Event()
    all_raw_data = []

    def ws_encrypt(payload):
        ws_send_seq[0] += 1
        return cipher.encrypt(nonce, struct.pack("<I", ws_send_seq[0]) + payload, aad)

    def ws_decrypt(data):
        dec = cipher.decrypt(nonce, data, aad)
        return dec[4:]  # strip seq counter

    async def ws_handler(websocket):
        print(f"    Device connected! ({websocket.remote_address})")

        # Send our handshake (cmd_id=16)
        handshake_json = {"sn": DEVICE_SN, "token": BIND_TOKEN[:32], "pVer": 20}
        hs_packet = build_wifi_packet(16, handshake_json)
        encrypted_hs = ws_encrypt(hs_packet)
        await websocket.send(encrypted_hs)
        print(f"    Sent WiFi handshake (cmd=16)")

        msg_count = 0
        try:
            async for message in websocket:
                if not isinstance(message, bytes):
                    continue

                try:
                    decrypted = ws_decrypt(message)
                except:
                    all_raw_data.append(message)
                    continue

                cmd_id, json_data = parse_wifi_packet(decrypted)
                msg_count += 1

                if cmd_id == 2:
                    # Device handshake
                    print(f"    Device handshake: {json_data}")

                    # Send file list request (cmd_id=11 = getFileList)
                    file_req = build_wifi_packet(11, {"page": 0, "pageSize": 100})
                    await websocket.send(ws_encrypt(file_req))
                    print(f"    Sent file list request (cmd=11)")

                elif cmd_id == 11:
                    # File list response
                    print(f"    File list: {json_data}")

                elif cmd_id is not None:
                    print(f"    WS msg #{msg_count}: cmd={cmd_id} data={str(json_data)[:80]}")
                    all_raw_data.append(decrypted)
                else:
                    # Raw data (file content?)
                    all_raw_data.append(decrypted)
                    if msg_count % 50 == 0:
                        total = sum(len(d) for d in all_raw_data)
                        print(f"\r    Receiving: {total} bytes...    ", end="", flush=True)

        except websockets.ConnectionClosed:
            pass

        total = sum(len(d) for d in all_raw_data)
        print(f"\n    Transfer done: {msg_count} messages, {total} bytes raw data")
        transfer_complete.set()

    print(f"    WebSocket server starting on port {WS_PORT}...")
    stop = asyncio.Event()

    async with websockets.asyncio.server.serve(ws_handler, "0.0.0.0", WS_PORT):
        print(f"    Listening on 0.0.0.0:{WS_PORT}")

        try:
            await asyncio.wait_for(transfer_complete.wait(), timeout=60)
        except asyncio.TimeoutError:
            print(f"    Transfer timeout (60s)")

    # Save all received data
    if all_raw_data:
        RECORDINGS_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = RECORDINGS_DIR / f"wifi_{ts}.bin"
        combined = b"".join(all_raw_data)
        fp.write_bytes(combined)
        print(f"    Saved: {fp} ({len(combined)} bytes)")
        return fp
    return None


async def main():
    record_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    print("=" * 60)
    print("  PLAUD WiFi SYNC — FULLY AUTOMATED")
    print("=" * 60)

    original_wifi = get_wifi()
    print(f"\n  WiFi: {original_wifi or 'None'}")

    # [1] BLE CONNECT
    print("\n[1] BLE Connect + Handshake...")
    target = None
    for attempt in range(10):
        found = await BleakScanner.discover(timeout=5, return_adv=True)
        for a, (d, adv) in found.items():
            if 0x5D in adv.manufacturer_data: target = d
        if target: break
        await asyncio.sleep(3)
    if not target:
        print("    Not found."); return

    cl = BleakClient(target, timeout=30.0)
    await cl.connect()
    keys = await handshake(cl)
    if not keys:
        print("    Failed!"); await cl.disconnect(); return
    key, nonce, aad = keys
    print("    PLAUD.AI!")

    cipher = ChaCha20Poly1305(key); seq = [0]
    def enc(pt): seq[0] += 1; return cipher.encrypt(nonce, struct.pack("<I", seq[0]) + pt, aad)
    def dec2(ct2): return cipher.decrypt(nonce, ct2, aad)[4:]

    cmd_evt = asyncio.Event(); cmd_resp = [None]
    file_data = bytearray(); file_done = asyncio.Event()
    def handler(s, d):
        try: dd = dec2(bytes(d))
        except: return
        if len(dd) < 1: return
        if dd[0] == 2 and len(dd) >= 10:
            off = struct.unpack_from("<I", dd, 5)[0]
            if off == 0xFFFFFFFF: file_done.set()
            else: file_data.extend(dd[10:10 + dd[9]])
        elif dd[0] == 1:
            cid = struct.unpack_from("<H", dd, 1)[0] if len(dd) >= 3 else 0
            if cid == 29: file_done.set()
            cmd_resp[0] = dd; cmd_evt.set()
    await cl.start_notify(N, handler)

    async def cmd(name, pt, to=5):
        cmd_evt.clear(); cmd_resp[0] = None
        await cl.write_gatt_char(W, enc(pt), response=False)
        try: await asyncio.wait_for(cmd_evt.wait(), timeout=to)
        except: pass
        r = cmd_resp[0]
        print(f"    {name}: {'OK' if r else 'no response'}")
        return r

    token32 = BIND_TOKEN[:32].encode("ascii")
    await cmd("HS1", bytes([1, 1, 0, 2, 0, 0]) + token32)
    await cmd("HS2", bytes([1, 1, 0, 2, 0, 1]) + token32)
    await cmd("Sync", bytes([1, 4, 0]) + struct.pack("<I", int(time.time())) + bytes([0, 0]))

    # [2] RECORD
    print(f"\n[2] Recording ({record_seconds}s) — speak now!")
    await cmd("Start", bytes([1, 20, 0]))
    await asyncio.sleep(record_seconds)
    await cmd("Stop", bytes([1, 23, 0]))
    await asyncio.sleep(2)

    # [3] OPEN WIFI
    print(f"\n[3] Opening WiFi hotspot...")
    await cmd("WiFi", bytes([1, 10, 0]))

    # Stop BLE notifications
    try: await cl.stop_notify(N)
    except: pass

    print(f"    Waiting 5s for hotspot...")
    await asyncio.sleep(5)

    # [4] START WEBSOCKET + CONNECT TO HOTSPOT
    print(f"\n[4] WiFi transfer...")

    # Start WebSocket server as a background task
    ws_task = asyncio.create_task(wifi_transfer(key, nonce, aad))
    await asyncio.sleep(1)

    # Connect to device hotspot
    if join_wifi(WIFI_SSID, WIFI_PASS):
        print(f"    Connected to {WIFI_SSID}")
        await asyncio.sleep(2)
        my_ip = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True).stdout.strip()
        print(f"    Our IP: {my_ip}")

        # Wait for transfer
        try:
            await asyncio.wait_for(ws_task, timeout=90)
        except asyncio.TimeoutError:
            print(f"    Transfer timeout")
            ws_task.cancel()
    else:
        print(f"    Failed to join hotspot")
        ws_task.cancel()

    # [5] RECONNECT ORIGINAL WIFI
    print(f"\n[5] Restoring WiFi...")
    # Force disconnect from hotspot
    subprocess.run(["networksetup", "-removepreferredwirelessnetwork", "en0", WIFI_SSID],
                   capture_output=True)
    if original_wifi:
        join_wifi(original_wifi)
        await asyncio.sleep(3)
    print(f"    WiFi: {get_wifi() or 'None'}")

    # [6] BLE DOWNLOAD (comparison/fallback)
    print(f"\n[6] BLE download for comparison...")
    # Reconnect BLE
    try:
        for attempt in range(5):
            found = await BleakScanner.discover(timeout=5, return_adv=True)
            for a, (d, adv) in found.items():
                if 0x5D in adv.manufacturer_data: target = d
            if target: break
            await asyncio.sleep(3)

        cl2 = BleakClient(target, timeout=30.0)
        await cl2.connect()
        keys2 = await handshake(cl2)
        if keys2:
            key2, nonce2, aad2 = keys2
            cipher2 = ChaCha20Poly1305(key2); seq2 = [0]
            def enc2(pt): seq2[0] += 1; return cipher2.encrypt(nonce2, struct.pack("<I", seq2[0]) + pt, aad2)
            def dec3(ct2): return cipher2.decrypt(nonce2, ct2, aad2)[4:]

            fd2 = bytearray(); fd2_done = asyncio.Event()
            ce2 = asyncio.Event(); cr2 = [None]
            def h2(s, d):
                try: dd = dec3(bytes(d))
                except: return
                if dd[0] == 2 and len(dd) >= 10:
                    off = struct.unpack_from("<I", dd, 5)[0]
                    if off == 0xFFFFFFFF: fd2_done.set()
                    else: fd2.extend(dd[10:10 + dd[9]])
                elif dd[0] == 1:
                    cr2[0] = dd; ce2.set()
                    if struct.unpack_from("<H", dd, 1)[0] == 29: fd2_done.set()
            await cl2.start_notify(N, h2)

            t32 = BIND_TOKEN[:32].encode("ascii")
            for c in [bytes([1,1,0,2,0,0])+t32, bytes([1,1,0,2,0,1])+t32,
                      bytes([1,4,0])+struct.pack("<I",int(time.time()))+bytes([0,0])]:
                ce2.clear(); cr2[0] = None
                await cl2.write_gatt_char(W, enc2(c), response=False)
                await asyncio.sleep(0.5)

            # Get file list
            ce2.clear(); cr2[0] = None
            await cl2.write_gatt_char(W, enc2(bytes([1, 26, 0])), response=False)
            try: await asyncio.wait_for(ce2.wait(), timeout=10)
            except: pass
            r = cr2[0]
            if r and len(r) >= 21:
                sid = struct.unpack_from("<I", r, 11)[0]
                fsize = struct.unpack_from("<I", r, 15)[0]
                print(f"    Latest file: session {sid}, {fsize}B")

                # Download
                fd2.clear(); fd2_done.clear()
                sync = bytes([1, 28, 0]) + struct.pack("<I", sid) + struct.pack("<I", 0) + struct.pack("<I", fsize)
                await cl2.write_gatt_char(W, enc2(sync), response=False)
                start = time.time()
                while not fd2_done.is_set():
                    try: await asyncio.wait_for(fd2_done.wait(), timeout=2)
                    except:
                        if time.time() - start > 120: break
                elapsed = time.time() - start
                print(f"    BLE: {len(fd2)}B in {elapsed:.1f}s ({len(fd2)/max(elapsed,1)/1024:.1f} KB/s)")
                if fd2:
                    RECORDINGS_DIR.mkdir(exist_ok=True)
                    (RECORDINGS_DIR / f"ble_{sid}.bin").write_bytes(fd2)

            await cl2.stop_notify(N)
            await cl2.disconnect()
    except Exception as e:
        print(f"    BLE reconnect failed: {e}")

    print(f"\n{'=' * 60}")
    print(f"  DONE! Device stays paired.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
