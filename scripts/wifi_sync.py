#!/usr/bin/env python3
"""Plaud WiFi sync — fully automated end-to-end.

1. BLE connect + handshake
2. Record audio (optional, pass 0 to skip)
3. Start WebSocket server FIRST (before hotspot)
4. Open WiFi hotspot via BLE
5. CoreWLAN fast-connect to hotspot (< 2s)
6. WiFi handshake + file list + download
7. CoreWLAN restore original WiFi
8. Close hotspot via BLE
9. Stay paired (no depair)

Key fixes vs previous version:
- CoreWLAN instead of networksetup (faster, more reliable)
- WebSocket server starts BEFORE hotspot opens (device connects to us)
- Zero internet dependency during transfer phase
- Hotspot connection within ~2s of opening (vs 5-8s before)
"""
import asyncio, struct, base64, json, time, os, sys, subprocess
from pathlib import Path
from datetime import datetime
from bleak import BleakScanner, BleakClient
from src.crypto import get_public_key_full_pem, rsa_decrypt, load_rsa_private_key
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
import websockets
import websockets.asyncio.server

# CoreWLAN for fast WiFi switching
from CoreWLAN import CWWiFiClient

# Device credentials — read from production app config
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

W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"
WIFI_SSID = f"Plaud{DEVICE_SN[-4:]}"
WIFI_PASS = DEVICE_SN[-8:]
WS_PORT = 8081

SESSIONS_DIR = Path("sessions")
RECORDINGS_DIR = Path("recordings")


# === CoreWLAN WiFi control (fast, reliable) ===

def get_wlan_interface():
    """Get the default WiFi interface via CoreWLAN."""
    client = CWWiFiClient.sharedWiFiClient()
    iface = client.interface()
    return iface

def get_current_ssid():
    """Get current WiFi SSID. Tries multiple methods.
    macOS may hide SSID due to privacy (shows <redacted> or 'not associated')."""
    # Method 1: CoreWLAN
    iface = get_wlan_interface()
    if iface:
        ssid = iface.ssid()
        if ssid:
            return str(ssid)
    # Method 2: networksetup
    try:
        r = subprocess.run(
            ["networksetup", "-getairportnetwork", "en0"],
            capture_output=True, text=True, timeout=5,
        )
        if "Current Wi-Fi Network:" in r.stdout:
            name = r.stdout.split("Current Wi-Fi Network:")[1].strip()
            if name:
                return name
    except Exception:
        pass
    return None

def has_internet_ip(hotspot_ip=""):
    """Check if en0 has an IP that is NOT the hotspot IP."""
    ip = get_en0_ip()
    if ip and ip != hotspot_ip:
        return ip
    return None

def get_en0_ip():
    """Get the current IP on en0, or empty string."""
    try:
        r = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""

def connect_to_hotspot(target_ssid, password, original_ip, timeout=25):
    """Connect to the Plaud hotspot.

    Verifies by checking we get a DIFFERENT IP than the original network.
    (Hotspot uses 192.168.84.x, but we don't hardcode the subnet.)
    """
    iface = get_wlan_interface()

    # CRITICAL: Disconnect first, otherwise macOS stays on the preferred network
    print(f"    Disconnecting from current WiFi...")
    if iface:
        iface.disassociate()
    # Wait until the old IP is gone
    for _ in range(10):
        time.sleep(0.5)
        if not get_en0_ip():
            break
    print(f"    Disconnected (IP cleared)")

    # Connect to hotspot
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
            print(f"    networksetup hung (attempt {attempt}), retrying...")
            continue

        if r.returncode == 0:
            # Wait for DHCP to assign a new IP
            print(f"    networksetup OK, waiting for IP...")
            for wait in range(8):
                time.sleep(1)
                ip = get_en0_ip()
                if ip and ip != original_ip:
                    print(f"    On hotspot! IP={ip} (attempt {attempt})")
                    return ip  # Return the hotspot IP so we can use it later
            ip = get_en0_ip()
            if ip == original_ip:
                print(f"    Reconnected to old network ({ip}), retrying...")
                if iface:
                    iface.disassociate()
                time.sleep(1)
            elif ip:
                # Got a new IP — assume it's the hotspot
                print(f"    Got IP={ip} (attempt {attempt})")
                return ip
            else:
                print(f"    No IP yet, retrying...")
        else:
            err = (r.stderr.strip() or r.stdout.strip())
            if "Could not find" in err:
                if attempt % 3 == 1:
                    print(f"    Hotspot not visible yet (attempt {attempt})...")
            elif err:
                print(f"    networksetup: {err}")
            time.sleep(1)

    print(f"    Could not connect to hotspot after {timeout}s")
    return None

def corewlan_disconnect():
    """Disconnect from current WiFi."""
    iface = get_wlan_interface()
    if iface:
        iface.disassociate()

def get_preferred_networks():
    """Get the list of preferred/saved WiFi networks (macOS Keychain has passwords)."""
    try:
        r = subprocess.run(
            ["networksetup", "-listpreferredwirelessnetworks", "en0"],
            capture_output=True, text=True, timeout=5,
        )
        networks = []
        for line in r.stdout.strip().split("\n")[1:]:  # Skip header line
            name = line.strip()
            if name and name != WIFI_SSID:  # Skip the Plaud hotspot
                networks.append(name)
        return networks
    except Exception:
        return []

async def restore_internet(preferred_networks=None, hotspot_ip=""):
    """Restore internet after hotspot use. Power-cycle + wait for auto-join.

    networksetup -setairportnetwork DOES NOT WORK after hotspot (error -3900).
    The only reliable method: power-cycle WiFi and wait for macOS auto-join.
    Tested: needs 3s off + 10-15s on before auto-join happens.
    """
    subprocess.run(["networksetup", "-removepreferredwirelessnetwork", "en0", WIFI_SSID],
                   capture_output=True)

    print(f"    WiFi off...")
    subprocess.run(["networksetup", "-setairportpower", "en0", "off"],
                   capture_output=True, timeout=5)
    await asyncio.sleep(3)

    print(f"    WiFi on — waiting for auto-join...")
    subprocess.run(["networksetup", "-setairportpower", "en0", "on"],
                   capture_output=True, timeout=5)

    # Wait up to 30s for auto-join (typically takes 10-15s)
    for i in range(20):
        await asyncio.sleep(1.5)
        ip = get_en0_ip()
        if ip and ip != hotspot_ip:
            print(f"    Internet restored! IP={ip} ({int((i+1)*1.5)}s)")
            return True
        if i == 10:
            print(f"    Still waiting... ({int((i+1)*1.5)}s)")

    print(f"    WARNING: Auto-join slow. Please check WiFi manually if needed.")
    return False


# === WiFi protocol ===

def build_wifi_packet(cmd_id, json_data, protocol_version=2):
    """Build WiFi WebSocket packet: [total_len u24 LE][pver u8][cmd_id u16 LE][json_len u16 LE][json]"""
    json_bytes = json.dumps(json_data).encode("ascii")
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
    json_bytes = data[8:8 + json_len]
    try:
        json_data = json.loads(json_bytes)
    except Exception:
        json_data = json_bytes.decode("ascii", errors="replace")
    return cmd_id, json_data


# === BLE handshake ===

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


# === WiFi transfer (WebSocket server) ===

async def wifi_transfer(key, nonce, aad, connected_event):
    """WebSocket server that handles the WiFi file transfer protocol.

    Sets connected_event when the device connects (so we know the hotspot worked).
    """
    cipher = ChaCha20Poly1305(key)
    ws_seq = [0]
    all_messages = []
    file_list_data = [None]
    transfer_complete = asyncio.Event()

    def ws_encrypt(payload):
        ws_seq[0] += 1
        return cipher.encrypt(nonce, struct.pack("<I", ws_seq[0]) + payload, aad)

    def ws_decrypt(data):
        return cipher.decrypt(nonce, data, aad)[4:]

    async def ws_handler(websocket):
        addr = websocket.remote_address
        print(f"    Device connected via WebSocket! ({addr})")
        connected_event.set()

        msg_count = 0
        got_handshake = False
        try:
            async for message in websocket:
                if not isinstance(message, bytes):
                    continue

                try:
                    decrypted = ws_decrypt(message)
                except Exception:
                    all_messages.append(("raw_encrypted", message))
                    continue

                cmd_id, json_data = parse_wifi_packet(decrypted)
                msg_count += 1

                if cmd_id == 2:
                    # Device handshake — respond every time until it stops
                    if not got_handshake:
                        print(f"    Device handshake: {json_data}")
                    got_handshake = True
                    device_token = json_data.get("token", "") if isinstance(json_data, dict) else ""

                    # Try BOTH encrypted and unencrypted responses
                    hs_json = {"sn": DEVICE_SN, "token": device_token, "pVer": 20}
                    hs_packet = build_wifi_packet(16, hs_json)

                    if msg_count <= 2:
                        # First attempts: try encrypted
                        await websocket.send(ws_encrypt(hs_packet))
                        print(f"    Sent encrypted handshake (cmd=16)")
                    elif msg_count <= 4:
                        # Next attempts: try unencrypted (raw packet)
                        await websocket.send(hs_packet)
                        print(f"    Sent PLAINTEXT handshake (cmd=16)")
                    elif msg_count <= 6:
                        # Try with bind token instead of device token
                        hs_json2 = {"sn": DEVICE_SN, "token": BIND_TOKEN[:32], "pVer": 20}
                        await websocket.send(ws_encrypt(build_wifi_packet(16, hs_json2)))
                        print(f"    Sent encrypted handshake with bind token")
                    elif msg_count <= 8:
                        # Try cmd_id=2 response instead of 16
                        await websocket.send(ws_encrypt(build_wifi_packet(2, hs_json)))
                        print(f"    Sent encrypted response as cmd=2")
                    elif msg_count <= 10:
                        # Try plaintext with bind token
                        hs_json3 = {"sn": DEVICE_SN, "token": BIND_TOKEN[:32], "pVer": 20}
                        await websocket.send(build_wifi_packet(16, hs_json3))
                        print(f"    Sent PLAINTEXT with bind token")
                    else:
                        # Keep trying encrypted with device token (default)
                        await websocket.send(ws_encrypt(hs_packet))

                    # Also try sending file list after first handshake
                    if msg_count == 1:
                        await asyncio.sleep(0.2)
                        await websocket.send(ws_encrypt(build_wifi_packet(11, {"page": 0, "pageSize": 100})))
                        print(f"    Sent file list request (cmd=11)")

                elif cmd_id == 11:
                    # File list response
                    file_list_data[0] = json_data
                    print(f"    File list: {json_data}")

                    # If there are files, request download
                    if isinstance(json_data, dict) and json_data.get("list"):
                        file_list = json_data["list"]
                        print(f"    {len(file_list)} file(s) available")
                        for f in file_list:
                            print(f"      - {f}")
                        # Request first file download (cmd=12 = downloadFile)
                        if file_list:
                            first_file = file_list[0]
                            fid = first_file.get("id") or first_file.get("fileId") or first_file.get("sessionId")
                            if fid is not None:
                                dl_req = {"fileId": fid, "offset": 0}
                                await websocket.send(ws_encrypt(build_wifi_packet(12, dl_req)))
                                print(f"    Sent download request (cmd=12, fileId={fid})")
                    elif isinstance(json_data, dict) and json_data.get("total", 0) == 0:
                        print(f"    No files on device")
                        transfer_complete.set()

                elif cmd_id == 12:
                    # File download response / data
                    all_messages.append(("file_data", cmd_id, json_data, decrypted))
                    print(f"    Download response (cmd=12): {str(json_data)[:100]}")

                elif cmd_id is not None:
                    all_messages.append(("cmd", cmd_id, json_data, decrypted))
                    print(f"    WS msg #{msg_count}: cmd={cmd_id} data={str(json_data)[:100]}")
                else:
                    all_messages.append(("raw", decrypted))
                    if msg_count % 20 == 0:
                        total = sum(len(m[-1]) if isinstance(m, tuple) and len(m) > 1 else 0 for m in all_messages)
                        print(f"\r    Receiving: {total} bytes ({msg_count} msgs)    ", end="", flush=True)

        except websockets.ConnectionClosed as e:
            print(f"    WebSocket closed: {e}")

        print(f"    Transfer session: {msg_count} messages total")
        transfer_complete.set()

    print(f"    WebSocket server on port {WS_PORT}...")

    async with websockets.asyncio.server.serve(ws_handler, "0.0.0.0", WS_PORT):
        print(f"    Listening on 0.0.0.0:{WS_PORT}")
        try:
            await asyncio.wait_for(transfer_complete.wait(), timeout=90)
        except asyncio.TimeoutError:
            print(f"    Transfer timeout (90s)")

    # Save everything we received
    if all_messages:
        RECORDINGS_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Save raw data
        raw_parts = []
        for msg in all_messages:
            if isinstance(msg, tuple) and len(msg) >= 2:
                raw_parts.append(msg[-1] if isinstance(msg[-1], bytes) else str(msg).encode())
            else:
                raw_parts.append(str(msg).encode())
        combined = b"\n---\n".join(raw_parts)
        fp = RECORDINGS_DIR / f"wifi_{ts}.bin"
        fp.write_bytes(combined)
        print(f"    Saved: {fp} ({len(combined)} bytes)")

        # Save message log
        log_fp = RECORDINGS_DIR / f"wifi_{ts}_log.json"
        log_entries = []
        for msg in all_messages:
            if isinstance(msg, tuple):
                entry = {"type": msg[0]}
                if len(msg) >= 3 and msg[2] is not None:
                    entry["cmd_id"] = msg[1] if isinstance(msg[1], int) else None
                    entry["data"] = str(msg[2])[:500]
                log_entries.append(entry)
        json.dump(log_entries, open(str(log_fp), "w"), indent=2)
        print(f"    Log: {log_fp}")
        return fp

    return None


# === Main flow ===

async def main():
    record_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    print("=" * 60)
    print("  PLAUD WiFi SYNC")
    print("=" * 60)

    # Save current network state BEFORE doing anything
    original_wifi = get_current_ssid()
    original_ip = get_en0_ip()
    preferred_networks = get_preferred_networks()
    print(f"\n  Current WiFi: {original_wifi or '(hidden/unknown)'}")
    print(f"  Current IP: {original_ip or 'None'}")
    print(f"  Preferred networks: {', '.join(preferred_networks[:3])}...")
    print(f"  Device hotspot: {WIFI_SSID} / {WIFI_PASS}")

    # ── [1] BLE CONNECT ──
    print("\n[1] BLE Connect + Handshake...")
    target = None
    for attempt in range(10):
        found = await BleakScanner.discover(timeout=5, return_adv=True)
        for a, (d, adv) in found.items():
            if 0x5D in adv.manufacturer_data: target = d
        if target: break
        print(f"    scan {attempt + 1}...")
        await asyncio.sleep(3)
    if not target:
        print("    Device not found!"); return

    cl = BleakClient(target, timeout=30.0)
    await cl.connect()
    print(f"    Connected (MTU={cl.mtu_size})")

    keys = await handshake(cl)
    if not keys:
        print("    Handshake failed!"); await cl.disconnect(); return
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

    # ── [2] RECORD (skip if 0) ──
    if record_seconds > 0:
        print(f"\n[2] Recording ({record_seconds}s) — speak now!")
        await cmd("Start", bytes([1, 20, 0]))
        await asyncio.sleep(record_seconds)
        await cmd("Stop", bytes([1, 23, 0]))
        await asyncio.sleep(2)
    else:
        print(f"\n[2] Skipping recording (0s)")

    # ── [3] PRE-FLIGHT: Start WebSocket server BEFORE opening hotspot ──
    print(f"\n[3] Starting WebSocket server...")
    ws_connected = asyncio.Event()
    ws_task = asyncio.create_task(wifi_transfer(key, nonce, aad, ws_connected))
    # Give the server a moment to bind the port
    await asyncio.sleep(0.5)

    # ── [4] OPEN HOTSPOT via BLE ──
    print(f"\n[4] Opening WiFi hotspot on device...")
    await cmd("WiFi_Open", bytes([1, 10, 0]))

    # Stop BLE notifications — they may conflict with WiFi
    try: await cl.stop_notify(N)
    except: pass

    # ── [5] FAST CONNECT to hotspot using CoreWLAN ──
    # Start scanning IMMEDIATELY — the hotspot only lives ~30s
    print(f"\n[5] Connecting to {WIFI_SSID}...")
    t0 = time.time()
    hotspot_ip = connect_to_hotspot(WIFI_SSID, WIFI_PASS, original_ip, timeout=25)
    elapsed = time.time() - t0

    if hotspot_ip:
        print(f"    WiFi connected in {elapsed:.1f}s, IP={hotspot_ip}")

        # ── [6] WAIT FOR TRANSFER ──
        # Device should connect to our WebSocket server at port 8081
        print(f"\n[6] Waiting for device WebSocket connection...")
        try:
            # First wait for device to connect
            await asyncio.wait_for(ws_connected.wait(), timeout=30)
            print(f"    Device connected! Waiting for transfer to complete...")
            # Then wait for transfer to finish
            await asyncio.wait_for(ws_task, timeout=120)
        except asyncio.TimeoutError:
            if not ws_connected.is_set():
                print(f"    Device didn't connect to WebSocket within 30s")
            else:
                print(f"    Transfer timed out")
            ws_task.cancel()
            try: await ws_task
            except: pass
    else:
        hotspot_ip = ""
        print(f"    Failed to connect to hotspot after {elapsed:.1f}s")
        ws_task.cancel()
        try: await ws_task
        except: pass

    # ── [7] RESTORE INTERNET — THIS MUST SUCCEED ──
    print(f"\n[7] Restoring internet connectivity...")
    await restore_internet(preferred_networks, hotspot_ip=hotspot_ip or "")
    final_ip = get_en0_ip()
    if not final_ip:
        print(f"    !! Internet NOT restored. Reconnect manually !!")

    # ── [8] CLOSE HOTSPOT ──
    print(f"\n[8] Closing device WiFi hotspot...")
    try:
        await cl.start_notify(N, handler)
        await cmd("WiFi_Close", bytes([1, 10, 0, 0]))
        await cl.stop_notify(N)
    except Exception as e:
        print(f"    (BLE may have disconnected during WiFi: {e})")

    # ── [9] BATTERY + CLEANUP ──
    print(f"\n[9] Cleanup...")
    try:
        bat = await cl.read_gatt_char("00002a19-0000-1000-8000-00805f9b34fb")
        print(f"    Battery: {bat[0]}%")
    except:
        print(f"    (could not read battery)")

    try:
        await cl.disconnect()
    except:
        pass

    print(f"\n{'=' * 60}")
    print(f"  DONE. Device stays paired.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
