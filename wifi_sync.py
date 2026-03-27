#!/usr/bin/env python3
"""Plaud WiFi sync — record, transfer via WiFi hotspot, fully automated.

1. Connect via BLE, handshake
2. Record audio
3. Open device WiFi hotspot (BLE command)
4. Start WebSocket server on port 8081
5. Auto-connect Mac to device hotspot
6. Device connects to our WebSocket server
7. Receive files via WebSocket
8. Disconnect from hotspot, reconnect to original WiFi
9. Stay paired via BLE (no depair)
"""
import asyncio, struct, base64, json, time, os, sys, subprocess
from pathlib import Path
from datetime import datetime
from bleak import BleakScanner, BleakClient
from src.crypto import get_public_key_full_pem, rsa_decrypt, load_rsa_private_key
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
import websockets
import websockets.server

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


def get_current_wifi():
    try:
        r = subprocess.run(["networksetup", "-getairportnetwork", "en0"], capture_output=True, text=True)
        if "Current Wi-Fi Network:" in r.stdout:
            return r.stdout.split("Current Wi-Fi Network:")[1].strip()
    except:
        pass
    return None


def connect_to_wifi(ssid, password):
    """Connect Mac to a WiFi network. Returns True on success."""
    print(f"    Connecting to '{ssid}'...")
    r = subprocess.run(["networksetup", "-setairportnetwork", "en0", ssid, password], capture_output=True, text=True)
    if r.returncode == 0:
        return True
    print(f"    Failed: {r.stderr.strip()}")
    return False


def reconnect_wifi(ssid):
    """Reconnect to a known WiFi network (no password needed for saved networks)."""
    if not ssid:
        return
    print(f"    Reconnecting to '{ssid}'...")
    # For saved networks, just specify the SSID
    subprocess.run(["networksetup", "-setairportnetwork", "en0", ssid], capture_output=True)


async def handshake(cl):
    ack = asyncio.Event(); rsa = {}; done = asyncio.Event(); pk = [False]
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
    json.dump({"key": key.hex(), "nonce": nonce.hex(), "aad": aad.hex(), "status": "ACTIVE", "timestamp": datetime.now().isoformat()},
              open(f"sessions/session_{ts}.json", "w"), indent=2)
    json.dump({"key": key.hex(), "nonce": nonce.hex(), "aad": aad.hex(), "status": "ACTIVE"}, open("session_keys.json", "w"), indent=2)
    return key, nonce, aad


async def run_wifi_transfer():
    """Start WebSocket server, wait for device to connect, receive files."""
    received_files = []
    all_data = bytearray()
    device_connected = asyncio.Event()
    transfer_done = asyncio.Event()

    async def ws_handler(websocket):
        print(f"    Device connected to WebSocket! ({websocket.remote_address})")
        device_connected.set()

        async for message in websocket:
            if isinstance(message, bytes):
                all_data.extend(message)
                print(f"\r    WS received: {len(all_data)} bytes total    ", end="", flush=True)

                # Check if this is a command or file data
                if len(message) >= 3:
                    cmd_type = message[0]
                    cmd_id = struct.unpack_from("<H", message, 1)[0] if len(message) >= 3 else 0

                    if cmd_type == 1:
                        print(f"\n    WS cmd: type={cmd_type} id={cmd_id} [{len(message)}B]: {message.hex()[:40]}")

                        # Respond to handshake (cmd_id=16)
                        if cmd_id == 16:
                            # Send handshake response
                            print(f"    Sending WS handshake response...")
                            # The response format needs investigation
                            # For now just ACK
                            pass
            elif isinstance(message, str):
                print(f"    WS text: {message[:100]}")

        print(f"\n    Device disconnected from WebSocket")
        transfer_done.set()

    # Start WebSocket server
    print(f"    Starting WebSocket server on port {WS_PORT}...")
    try:
        async with websockets.server.serve(ws_handler, "0.0.0.0", WS_PORT):
            print(f"    WebSocket server listening on 0.0.0.0:{WS_PORT}")

            # Wait for device to connect (timeout 30s)
            try:
                await asyncio.wait_for(device_connected.wait(), timeout=30)
                print(f"    Device connected!")
            except asyncio.TimeoutError:
                print(f"    Timeout — device didn't connect to WebSocket")
                return None

            # Wait for transfer to complete (timeout 120s)
            try:
                await asyncio.wait_for(transfer_done.wait(), timeout=120)
            except asyncio.TimeoutError:
                print(f"    Transfer timeout")

    except OSError as e:
        if "Address already in use" in str(e):
            print(f"    Port {WS_PORT} already in use!")
        else:
            print(f"    WebSocket error: {e}")
        return None

    if all_data:
        RECORDINGS_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = RECORDINGS_DIR / f"wifi_{ts}.bin"
        fp.write_bytes(all_data)
        print(f"    Saved WiFi data: {fp} ({len(all_data)} bytes)")
        return fp
    return None


async def main():
    record_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 15

    print("=" * 60)
    print("  PLAUD WiFi SYNC")
    print(f"  Record {record_seconds}s → WiFi hotspot transfer → BLE fallback")
    print("=" * 60)

    original_wifi = get_current_wifi()
    print(f"\n  Current WiFi: {original_wifi or 'None'}")
    print(f"  Device hotspot: {WIFI_SSID} / pass: {WIFI_PASS}")

    # === BLE: SCAN + CONNECT + HANDSHAKE ===
    print("\n[1] BLE Connect...")
    target = None
    for attempt in range(10):
        found = await BleakScanner.discover(timeout=5, return_adv=True)
        for a, (d, adv) in found.items():
            if 0x5D in adv.manufacturer_data: target = d
        if target: break
        print(f"    scan {attempt + 1}...")
        await asyncio.sleep(3)
    if not target:
        print("    Device not found.")
        return

    cl = BleakClient(target, timeout=30.0)
    await cl.connect()
    keys = await handshake(cl)
    if not keys:
        print("    Handshake failed!")
        await cl.disconnect()
        return
    key, nonce, aad = keys
    print("    PLAUD.AI! Connected.\n")

    cipher = ChaCha20Poly1305(key); seq = [0]
    def enc(pt): seq[0] += 1; return cipher.encrypt(nonce, struct.pack("<I", seq[0]) + pt, aad)
    def dec2(ct2): return cipher.decrypt(nonce, ct2, aad)[4:]

    cmd_evt = asyncio.Event(); cmd_resp = [None]
    file_data = bytearray(); file_done = asyncio.Event()
    def handler(sender, data):
        try: d = dec2(bytes(data))
        except: return
        if len(d) < 1: return
        if d[0] == 2 and len(d) >= 10:
            off = struct.unpack_from("<I", d, 5)[0]
            if off == 0xFFFFFFFF: file_done.set()
            else: file_data.extend(d[10:10 + d[9]])
        elif d[0] == 1:
            cid = struct.unpack_from("<H", d, 1)[0] if len(d) >= 3 else 0
            if cid == 29: file_done.set()
            cmd_resp[0] = d; cmd_evt.set()
    await cl.start_notify(N, handler)

    async def cmd(name, pt, to=5):
        cmd_evt.clear(); cmd_resp[0] = None
        await cl.write_gatt_char(W, enc(pt), response=False)
        try: await asyncio.wait_for(cmd_evt.wait(), timeout=to)
        except: pass
        r = cmd_resp[0]
        print(f"    {name}: {r.hex()[:40] if r else 'no response'}")
        return r

    token32 = BIND_TOKEN[:32].encode("ascii")
    await cmd("HS1", bytes([1, 1, 0, 2, 0, 0]) + token32)
    await cmd("HS2", bytes([1, 1, 0, 2, 0, 1]) + token32)
    await cmd("Sync", bytes([1, 4, 0]) + struct.pack("<I", int(time.time())) + bytes([0, 0]))

    # === [2] RECORD ===
    print(f"\n[2] Recording ({record_seconds}s) — speak now!")
    await cmd("Start", bytes([1, 20, 0]))
    await asyncio.sleep(record_seconds)
    await cmd("Stop", bytes([1, 23, 0]))
    await asyncio.sleep(2)

    # === [3] LIST FILES ===
    print(f"\n[3] File list...")
    r = await cmd("Files", bytes([1, 26, 0]), to=10)
    files = []
    if r and len(r) >= 9:
        total = struct.unpack_from("<H", r, 7)[0]
        off = 11
        while off + 8 <= len(r):
            sid = struct.unpack_from("<I", r, off)[0]
            fsize = struct.unpack_from("<I", r, off + 4)[0]
            if sid == 0 and fsize == 0: break
            files.append((sid, fsize))
            off += 10
        print(f"    {len(files)} file(s): {', '.join(f'{s}({f}B)' for s,f in files)}")

    # === [4] OPEN WIFI HOTSPOT ===
    print(f"\n[4] Opening WiFi hotspot on device...")
    await cmd("WiFi_Open", bytes([1, 10, 0]))

    # Stop BLE notifications (device may drop BLE when WiFi active)
    try: await cl.stop_notify(N)
    except: pass

    # Wait for hotspot to come up
    print(f"    Waiting 5s for hotspot...")
    await asyncio.sleep(5)

    # === [5] CONNECT TO HOTSPOT + START WEBSOCKET SERVER ===
    print(f"\n[5] Connecting to hotspot + starting WebSocket server...")

    # Start WebSocket server FIRST (device will connect to us)
    ws_task = asyncio.create_task(run_wifi_transfer())

    # Give server a moment to start
    await asyncio.sleep(1)

    # Connect Mac to device hotspot
    wifi_connected = connect_to_wifi(WIFI_SSID, WIFI_PASS)

    if wifi_connected:
        print(f"    WiFi connected! Waiting for device WebSocket connection...")
        await asyncio.sleep(3)

        # Check our IP on the hotspot network
        r = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True)
        my_ip = r.stdout.strip()
        print(f"    Our IP: {my_ip}")

        # Wait for WiFi transfer to complete (or timeout)
        try:
            result = await asyncio.wait_for(ws_task, timeout=60)
            if result:
                print(f"\n    WiFi transfer complete!")
        except asyncio.TimeoutError:
            print(f"\n    WiFi transfer timeout")
            ws_task.cancel()
    else:
        print(f"    Could not connect to hotspot. Using BLE download...")
        ws_task.cancel()

    # === [6] RECONNECT TO ORIGINAL WIFI ===
    print(f"\n[6] Reconnecting to original WiFi...")
    reconnect_wifi(original_wifi)
    await asyncio.sleep(3)
    print(f"    WiFi: {get_current_wifi() or 'None'}")

    # === [7] BLE DOWNLOAD (fallback or for comparison) ===
    print(f"\n[7] BLE download...")
    # Reconnect BLE if it dropped
    if not cl.is_connected:
        print(f"    BLE disconnected. Reconnecting...")
        # Rescan + reconnect
        for attempt in range(5):
            found = await BleakScanner.discover(timeout=5, return_adv=True)
            for a, (d, adv) in found.items():
                if 0x5D in adv.manufacturer_data: target = d
            if target: break
            await asyncio.sleep(3)
        try:
            cl = BleakClient(target, timeout=30.0)
            await cl.connect()
            keys = await handshake(cl)
            if keys:
                key, nonce, aad = keys
                cipher = ChaCha20Poly1305(key); seq = [0]
                def enc2(pt): seq[0] += 1; return cipher.encrypt(nonce, struct.pack("<I", seq[0]) + pt, aad)
                def dec3(ct2): return cipher.decrypt(nonce, ct2, aad)[4:]
                enc = enc2; dec2 = dec3

                cmd_evt2 = asyncio.Event(); cmd_resp2 = [None]
                file_data2 = bytearray(); file_done2 = asyncio.Event()
                def handler2(sender, data):
                    try: d = dec2(bytes(data))
                    except: return
                    if len(d) < 1: return
                    if d[0] == 2 and len(d) >= 10:
                        off = struct.unpack_from("<I", d, 5)[0]
                        if off == 0xFFFFFFFF: file_done2.set()
                        else: file_data2.extend(d[10:10 + d[9]])
                    elif d[0] == 1:
                        cmd_resp2[0] = d; cmd_evt2.set()
                await cl.start_notify(N, handler2)

                await cl.write_gatt_char(W, enc(bytes([1, 1, 0, 2, 0, 0]) + token32), response=False)
                await asyncio.sleep(0.5)
                await cl.write_gatt_char(W, enc(bytes([1, 1, 0, 2, 0, 1]) + token32), response=False)
                await asyncio.sleep(0.5)

                # Download latest file
                if files:
                    sid, fsize = files[-1]  # Latest file
                    print(f"    Downloading session {sid} ({fsize}B)...")
                    file_data2.clear(); file_done2.clear()
                    sync = bytes([1, 28, 0]) + struct.pack("<I", sid) + struct.pack("<I", 0) + struct.pack("<I", fsize)
                    await cl.write_gatt_char(W, enc(sync), response=False)
                    start = time.time()
                    while not file_done2.is_set():
                        try: await asyncio.wait_for(file_done2.wait(), timeout=2)
                        except:
                            if time.time() - start > 120: break
                            print(f"\r    {len(file_data2)}/{fsize}    ", end="", flush=True)
                    elapsed = time.time() - start
                    print(f"\r    Downloaded: {len(file_data2)}B in {elapsed:.1f}s")
                    if file_data2:
                        RECORDINGS_DIR.mkdir(exist_ok=True)
                        fp = RECORDINGS_DIR / f"ble_{sid}.bin"
                        fp.write_bytes(file_data2)
                        print(f"    Saved: {fp}")

                await cl.stop_notify(N)
            print("    BLE reconnected.")
        except Exception as e:
            print(f"    BLE reconnect failed: {e}")

    # === [8] DONE — STAY PAIRED ===
    print(f"\n[8] Done! Device stays paired (no depair).")
    try:
        bat = await cl.read_gatt_char("00002a19-0000-1000-8000-00805f9b34fb")
        print(f"    Battery: {bat[0]}%")
    except: pass
    try: await cl.disconnect()
    except: pass

    print(f"\n{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
