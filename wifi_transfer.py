#!/usr/bin/env python3
"""Record + WiFi transfer — fully automated.

1. Connect via BLE, handshake
2. Record for N seconds
3. Stop recording
4. Open WiFi hotspot on device
5. Auto-connect Mac to device's WiFi hotspot
6. Transfer files over WiFi
7. Reconnect Mac to original WiFi
8. Stay connected via BLE (no depair)
"""
import asyncio, struct, base64, json, time, os, sys, subprocess
from pathlib import Path
from datetime import datetime
from bleak import BleakScanner, BleakClient
from src.crypto import get_public_key_full_pem, rsa_decrypt, load_rsa_private_key
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
import requests

SN_SIG = "<REDACTED_SN_SIGNATURE>"
BIND_TOKEN = "<REDACTED_JWT>"
W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"

DEVICE_SN = "8820A0000000XXXX"
WIFI_SSID = f"Plaud{DEVICE_SN[-4:]}"
WIFI_PASS = DEVICE_SN[-8:]

SESSIONS_DIR = Path("sessions")
RECORDINGS_DIR = Path("recordings")


def get_current_wifi():
    """Get current WiFi network name."""
    try:
        r = subprocess.run(
            ["networksetup", "-getairportnetwork", "en0"],
            capture_output=True, text=True,
        )
        if "Current Wi-Fi Network:" in r.stdout:
            return r.stdout.split("Current Wi-Fi Network:")[1].strip()
    except:
        pass
    return None


def connect_wifi(ssid, password):
    """Connect Mac to a WiFi network."""
    print(f"  Connecting to WiFi '{ssid}'...")
    r = subprocess.run(
        ["networksetup", "-setairportnetwork", "en0", ssid, password],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f"  WiFi connected!")
        return True
    else:
        print(f"  WiFi connect failed: {r.stderr.strip()}")
        return False


def disconnect_wifi():
    """Disconnect from current WiFi."""
    subprocess.run(
        ["networksetup", "-setairportpower", "en0", "off"],
        capture_output=True,
    )
    time.sleep(1)
    subprocess.run(
        ["networksetup", "-setairportpower", "en0", "on"],
        capture_output=True,
    )


async def handshake(cl):
    """RSA handshake — handles both first-time and reconnect."""
    ack = asyncio.Event()
    rsa_chunks = {}
    rsa_done = asyncio.Event()
    pk_sent = [False]

    def h(s, d):
        d = bytes(d)
        cid = struct.unpack_from("<H", d, 0)[0] if len(d) >= 2 else 0
        if cid == 65041:
            ack.set()
        elif cid == 65042 and len(d) >= 4:
            idx = d[3]
            if idx not in rsa_chunks:
                rsa_chunks[idx] = d[4:]
                if len(rsa_chunks) >= d[2]:
                    rsa_done.set()

    await cl.start_notify(N, h)

    sig = base64.b64decode(SN_SIG)
    tc = (len(sig) + 99) // 100
    for _ in range(2):
        for i in range(tc):
            await cl.write_gatt_char(W, struct.pack("<H", 65040) + bytes([tc, i]) + sig[i*100:(i+1)*100], response=False)
            await asyncio.sleep(0.02)

    # Wait for ACK or direct RSA
    for _ in range(50):
        await asyncio.sleep(0.1)
        if ack.is_set() or rsa_done.is_set():
            break

    if ack.is_set() and not rsa_done.is_set():
        # First time — send pubkey
        pk_sent[0] = True
        pem = get_public_key_full_pem().encode()
        pc = (len(pem) + 99) // 100
        for i in range(pc):
            await cl.write_gatt_char(W, struct.pack("<H", 65042) + bytes([pc, i]) + pem[i*100:(i+1)*100], response=True)
            await asyncio.sleep(0.02)
        await asyncio.wait_for(rsa_done.wait(), timeout=15)
    elif not rsa_done.is_set():
        await cl.stop_notify(N)
        return None

    blob = bytearray()
    for i in sorted(rsa_chunks.keys()):
        blob.extend(rsa_chunks[i])
    dec = rsa_decrypt(bytes(blob), load_rsa_private_key())
    key, nonce, aad, ct = dec[:32], dec[32:44], dec[44:56], dec[56:]
    assert ChaCha20Poly1305(key).decrypt(nonce, ct, aad) == b"PLAUD.AI"
    await cl.stop_notify(N)

    # Save session
    SESSIONS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json.dump({
        "key": key.hex(), "nonce": nonce.hex(), "aad": aad.hex(),
        "status": "ACTIVE", "timestamp": datetime.now().isoformat(),
    }, open(f"sessions/session_{ts}.json", "w"), indent=2)
    json.dump({
        "key": key.hex(), "nonce": nonce.hex(), "aad": aad.hex(), "status": "ACTIVE",
    }, open("session_keys.json", "w"), indent=2)

    return key, nonce, aad


async def main():
    record_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 15

    print("=" * 60)
    print("  PLAUD RECORD + WIFI TRANSFER")
    print("=" * 60)

    # Remember current WiFi to reconnect later
    original_wifi = get_current_wifi()
    print(f"\n  Current WiFi: {original_wifi}")
    print(f"  Device hotspot: {WIFI_SSID} / {WIFI_PASS}")

    # === SCAN & CONNECT ===
    print("\nScanning...")
    target = None
    for attempt in range(10):
        found = await BleakScanner.discover(timeout=5, return_adv=True)
        for a, (d, adv) in found.items():
            if 0x5D in adv.manufacturer_data:
                target = d
        if target:
            break
        print(f"  scan {attempt + 1}...")
        await asyncio.sleep(3)
    if not target:
        print("Device not found.")
        return

    cl = BleakClient(target, timeout=30.0)
    await cl.connect()
    print(f"Connected! MTU={cl.mtu_size}")

    # === HANDSHAKE ===
    print("\nHandshake...")
    keys = await handshake(cl)
    if not keys:
        print("Handshake failed!")
        await cl.disconnect()
        return
    key, nonce, aad = keys
    print("  PLAUD.AI!\n")

    cipher = ChaCha20Poly1305(key)
    seq = [0]

    def enc(pt):
        seq[0] += 1
        return cipher.encrypt(nonce, struct.pack("<I", seq[0]) + pt, aad)

    def dec2(ct2):
        return cipher.decrypt(nonce, ct2, aad)[4:]

    cmd_evt = asyncio.Event()
    cmd_resp = [None]
    file_data = bytearray()
    file_done = asyncio.Event()

    def handler(sender, data):
        try:
            d = dec2(bytes(data))
        except:
            return
        if len(d) < 1:
            return
        if d[0] == 2 and len(d) >= 10:
            off = struct.unpack_from("<I", d, 5)[0]
            if off == 0xFFFFFFFF:
                file_done.set()
            else:
                file_data.extend(d[10:10 + d[9]])
        elif d[0] == 1:
            cid = struct.unpack_from("<H", d, 1)[0] if len(d) >= 3 else 0
            if cid == 29:
                file_done.set()
            cmd_resp[0] = d
            cmd_evt.set()

    await cl.start_notify(N, handler)

    async def cmd(name, pt, to=5):
        cmd_evt.clear()
        cmd_resp[0] = None
        await cl.write_gatt_char(W, enc(pt), response=False)
        try:
            await asyncio.wait_for(cmd_evt.wait(), timeout=to)
        except:
            pass
        r = cmd_resp[0]
        print(f"  {name}: {r.hex()[:40] if r else 'no response'}")
        return r

    # Setup
    token32 = BIND_TOKEN[:32].encode("ascii")
    await cmd("HS1", bytes([1, 1, 0, 2, 0, 0]) + token32)
    await cmd("HS2", bytes([1, 1, 0, 2, 0, 1]) + token32)
    await cmd("Sync", bytes([1, 4, 0]) + struct.pack("<I", int(time.time())) + bytes([0, 0]))

    # === RECORD ===
    print(f"\n--- RECORDING ({record_seconds}s) ---")
    await cmd("Start", bytes([1, 20, 0]))
    print(f"  Recording... (speak now!)")
    await asyncio.sleep(record_seconds)
    r = await cmd("Stop", bytes([1, 23, 0]))
    print(f"  Recording stopped.")
    await asyncio.sleep(2)

    # === LIST FILES ===
    print(f"\n--- FILES ---")
    r = await cmd("Files", bytes([1, 26, 0]), to=10)
    files = []
    if r and len(r) >= 9:
        total = struct.unpack_from("<H", r, 7)[0]
        print(f"  Total: {total}")
        if total > 0 and len(r) >= 21:
            sid = struct.unpack_from("<I", r, 11)[0]
            fsize = struct.unpack_from("<I", r, 15)[0]
            files.append((sid, fsize))
            print(f"  Session {sid}: {fsize}B ({(fsize/80)*20/1000:.0f}s)")

    # === OPEN WIFI HOTSPOT ===
    print(f"\n--- WIFI TRANSFER ---")
    print(f"  Opening WiFi hotspot on device...")
    r = await cmd("WiFi_Open", bytes([1, 10, 0]))

    if r and len(r) >= 4:
        print(f"  WiFi mode active! Hotspot: {WIFI_SSID}")

        # Wait for hotspot to come up
        await asyncio.sleep(3)

        # Stop BLE notifications during WiFi (they may conflict)
        await cl.stop_notify(N)

        # Auto-connect Mac to device hotspot
        print(f"\n  Switching Mac WiFi to {WIFI_SSID}...")
        wifi_ok = connect_wifi(WIFI_SSID, WIFI_PASS)

        if wifi_ok:
            # Wait for connection to establish
            await asyncio.sleep(3)

            # The device should be at a known IP (192.168.x.1 typically)
            # Try to discover the device's HTTP server
            print(f"\n  Looking for device HTTP server...")
            for ip in ["192.168.4.1", "192.168.1.1", "10.0.0.1", "172.16.0.1"]:
                try:
                    resp = requests.get(f"http://{ip}/", timeout=3)
                    print(f"  Found at {ip}! Status: {resp.status_code}")
                    print(f"  Response: {resp.text[:200]}")

                    # Try to list files
                    for path in ["/files", "/api/files", "/list", "/", "/recordings"]:
                        try:
                            resp2 = requests.get(f"http://{ip}{path}", timeout=3)
                            if resp2.status_code == 200:
                                print(f"  {path}: {resp2.text[:200]}")
                        except:
                            pass
                    break
                except:
                    pass
            else:
                print(f"  No HTTP server found at common IPs")
                # Try to find any device on the network
                print(f"  Scanning for devices...")
                r2 = subprocess.run(["arp", "-a"], capture_output=True, text=True)
                for line in r2.stdout.split("\n"):
                    if "192.168" in line or "10.0" in line:
                        print(f"    {line.strip()}")

            # Reconnect to original WiFi
            print(f"\n  Reconnecting to {original_wifi}...")
            if original_wifi:
                connect_wifi(original_wifi, "")  # Saved networks don't need password
                await asyncio.sleep(3)
        else:
            print(f"  Could not connect to hotspot. Falling back to BLE download.")

    # === BLE DOWNLOAD (fallback or primary) ===
    if files:
        print(f"\n--- BLE DOWNLOAD ---")
        await cl.start_notify(N, handler)
        sid, fsize = files[0]
        file_data.clear()
        file_done.clear()
        sync = bytes([1, 28, 0]) + struct.pack("<I", sid) + struct.pack("<I", 0) + struct.pack("<I", fsize)
        await cl.write_gatt_char(W, enc(sync), response=False)
        start = time.time()
        while not file_done.is_set():
            try:
                await asyncio.wait_for(file_done.wait(), timeout=2)
            except:
                if time.time() - start > 120:
                    break
                pct = len(file_data) * 100 // max(fsize, 1)
                print(f"\r  {len(file_data)}/{fsize} ({pct}%)    ", end="", flush=True)
        elapsed = time.time() - start
        print(f"\r  Downloaded: {len(file_data)}B in {elapsed:.1f}s ({len(file_data)/max(elapsed,1)/1024:.1f} KB/s)")

        if file_data:
            RECORDINGS_DIR.mkdir(exist_ok=True)
            fp = RECORDINGS_DIR / f"ble_{sid}.bin"
            fp.write_bytes(file_data)
            print(f"  Saved: {fp}")
            print(f"  Header: {file_data[:16].hex()}")

    # === CLOSE WIFI ON DEVICE ===
    print(f"\n--- CLOSING WIFI ---")
    try:
        await cl.start_notify(N, handler)
    except:
        pass
    # cmd_id=10 with param to close
    await cmd("WiFi_Close", bytes([1, 10, 0, 0]))

    # === STAY CONNECTED ===
    print(f"\n--- SESSION COMPLETE ---")
    print(f"  Device stays paired. No depair.")

    try:
        bat = await cl.read_gatt_char("00002a19-0000-1000-8000-00805f9b34fb")
        print(f"  Battery: {bat[0]}%")
    except:
        pass

    try:
        await cl.stop_notify(N)
        await cl.disconnect()
    except:
        pass

    print(f"\n{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
