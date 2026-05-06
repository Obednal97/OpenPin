#!/usr/bin/env python3
"""Plaud device sync — connect, record, transfer, stay paired.

NO DEPAIR. Device stays bonded. User can walk away and come back.
All session data logged persistently.
"""
import asyncio, struct, base64, json, time, os, sys
from pathlib import Path
from datetime import datetime
from bleak import BleakScanner, BleakClient
from src.crypto import get_public_key_full_pem, rsa_decrypt, load_rsa_private_key
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

# Device credentials — read from production app config
CONFIG_PATH = Path.home() / "Library/Application Support/Plaud V2/.plaud_v2_config.json"
try:
    with open(CONFIG_PATH) as _f:
        _cfg = json.load(_f)
except FileNotFoundError:
    sys.exit(f"Config not found at {CONFIG_PATH}. Run the Plaud V2 app first to pair a device.")

SN = "8820B50256947317"
BIND_TOKEN = _cfg.get("device_bind_tokens", {}).get(SN)
SN_SIG = _cfg.get("device_sn_signatures", {}).get(SN)
if not BIND_TOKEN or not SN_SIG:
    sys.exit(f"No credentials for SN {SN} in config — re-pair device via the Plaud V2 app first.")

W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"

SESSIONS_DIR = Path("sessions")
RECORDINGS_DIR = Path("recordings")


class SessionLog:
    """Persistent session logger. Never overwrites."""

    def __init__(self):
        SESSIONS_DIR.mkdir(exist_ok=True)
        self.ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.data = {
            "timestamp": datetime.now().isoformat(),
            "device_sn": "8820B50256947317",
            "events": [],
        }
        self.path = SESSIONS_DIR / f"session_{self.ts}.json"

    def log(self, event, **kwargs):
        entry = {"event": event, "time": datetime.now().isoformat(), **kwargs}
        self.data["events"].append(entry)
        print(f"  [{event}] {' '.join(f'{k}={v}' for k,v in kwargs.items() if k != 'raw')}")

    def save_keys(self, key, nonce, aad):
        self.data["chacha_key"] = key.hex()
        self.data["chacha_nonce"] = nonce.hex()
        self.data["chacha_aad"] = aad.hex()
        self._write()
        # Also save as latest for emergency depair
        json.dump({
            "key": key.hex(), "nonce": nonce.hex(), "aad": aad.hex(),
            "status": "ACTIVE", "timestamp": datetime.now().isoformat(),
        }, open("session_keys.json", "w"), indent=2)

    def save_final(self, status="OK"):
        self.data["status"] = status
        self.data["completed"] = datetime.now().isoformat()
        self._write()

    def _write(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)


async def handshake(cl, log):
    """Full RSA handshake. Handles both first-time and reconnect."""
    ack = asyncio.Event()
    rsa_chunks = {}
    rsa_done = asyncio.Event()
    pk_sent = [False]
    got_65042_before_pk = [False]

    def h(s, d):
        d = bytes(d)
        cid = struct.unpack_from("<H", d, 0)[0] if len(d) >= 2 else 0
        if cid == 65041:
            ack.set()
        elif cid == 65042 and len(d) >= 4:
            idx = d[3]
            if pk_sent[0]:
                if idx not in rsa_chunks:
                    rsa_chunks[idx] = d[4:]
                    if len(rsa_chunks) >= d[2]:
                        rsa_done.set()
            else:
                # Device sent RSA key BEFORE we sent pubkey = reconnect (pubkey cached)
                got_65042_before_pk[0] = True
                if idx not in rsa_chunks:
                    rsa_chunks[idx] = d[4:]
                    if len(rsa_chunks) >= d[2]:
                        rsa_done.set()

    await cl.start_notify(N, h)

    # Send SN signature (twice, as the real app does)
    sig = base64.b64decode(SN_SIG)
    tc = (len(sig) + 99) // 100
    log.log("SN_SIG_SEND", chunks=tc)
    for _ in range(2):
        for i in range(tc):
            await cl.write_gatt_char(W, struct.pack("<H", 65040) + bytes([tc, i]) + sig[i*100:(i+1)*100], response=False)
            await asyncio.sleep(0.02)

    # Wait for either ACK (first time) or direct RSA response (reconnect)
    for _ in range(50):  # 5 seconds
        await asyncio.sleep(0.1)
        if ack.is_set() or rsa_done.is_set():
            break

    if ack.is_set() and not rsa_done.is_set():
        # First-time flow: got ACK, need to send pubkey
        log.log("SN_ACK_RECEIVED", flow="first_time")
        pk_sent[0] = True
        full_pem = get_public_key_full_pem().encode()
        pc = (len(full_pem) + 99) // 100
        log.log("PUBKEY_SEND", chunks=pc, size=len(full_pem))
        for i in range(pc):
            await cl.write_gatt_char(W, struct.pack("<H", 65042) + bytes([pc, i]) + full_pem[i*100:(i+1)*100], response=True)
            await asyncio.sleep(0.02)
        try:
            await asyncio.wait_for(rsa_done.wait(), timeout=15)
        except asyncio.TimeoutError:
            log.log("RSA_TIMEOUT")
            await cl.stop_notify(N)
            return None

    elif rsa_done.is_set():
        # Reconnect flow: device already sent RSA key (pubkey was cached)
        log.log("RSA_RECEIVED", flow="reconnect_cached")

    elif got_65042_before_pk[0] and not rsa_done.is_set():
        # Got some 65042 but not enough — wait more
        log.log("WAITING_MORE_RSA")
        try:
            await asyncio.wait_for(rsa_done.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.log("RSA_INCOMPLETE", chunks=len(rsa_chunks))
            await cl.stop_notify(N)
            return None

    else:
        log.log("NO_RESPONSE")
        await cl.stop_notify(N)
        return None

    # RSA decrypt
    blob = bytearray()
    for i in sorted(rsa_chunks.keys()):
        blob.extend(rsa_chunks[i])
    log.log("RSA_DECRYPT", blob_size=len(blob))

    if len(blob) != 256:
        log.log("RSA_WRONG_SIZE", size=len(blob))
        await cl.stop_notify(N)
        return None

    dec = rsa_decrypt(bytes(blob), load_rsa_private_key())
    key, nonce, aad, ct = dec[:32], dec[32:44], dec[44:56], dec[56:]

    pt = ChaCha20Poly1305(key).decrypt(nonce, ct, aad)
    if pt != b"PLAUD.AI":
        log.log("VALIDATION_FAILED", plaintext=pt.hex())
        await cl.stop_notify(N)
        return None

    log.log("PLAUD_AI_VALIDATED")
    log.save_keys(key, nonce, aad)
    await cl.stop_notify(N)
    return key, nonce, aad


class EncryptedChannel:
    """Manages encrypted command communication."""

    def __init__(self, cl, key, nonce, aad, log):
        self.cl = cl
        self.cipher = ChaCha20Poly1305(key)
        self.nonce = nonce
        self.aad = aad
        self.log = log
        self.seq = 0
        self.cmd_event = asyncio.Event()
        self.cmd_resp = None
        self.file_data = bytearray()
        self.file_done = asyncio.Event()
        self._handler_active = False

    def _encrypt(self, pt):
        self.seq += 1
        return self.cipher.encrypt(self.nonce, struct.pack("<I", self.seq) + pt, self.aad)

    def _decrypt(self, ct):
        return self.cipher.decrypt(self.nonce, ct, self.aad)[4:]

    def _handler(self, sender, data):
        try:
            dec = self._decrypt(bytes(data))
        except:
            return
        if len(dec) < 1:
            return
        if dec[0] == 2 and len(dec) >= 10:
            # File data
            offset = struct.unpack_from("<I", dec, 5)[0]
            if offset == 0xFFFFFFFF:
                self.file_done.set()
            else:
                self.file_data.extend(dec[10:10 + dec[9]])
        elif dec[0] == 1:
            cmd_id = struct.unpack_from("<H", dec, 1)[0] if len(dec) >= 3 else 0
            if cmd_id == 29:
                self.file_done.set()
            self.cmd_resp = dec
            self.cmd_event.set()

    async def start(self):
        if not self._handler_active:
            await self.cl.start_notify(N, self._handler)
            self._handler_active = True

    async def stop(self):
        if self._handler_active:
            try:
                await self.cl.stop_notify(N)
            except:
                pass
            self._handler_active = False

    async def cmd(self, name, plaintext, timeout=5.0):
        self.cmd_event.clear()
        self.cmd_resp = None
        await self.cl.write_gatt_char(W, self._encrypt(plaintext), response=False)
        try:
            await asyncio.wait_for(self.cmd_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        r = self.cmd_resp
        self.log.log(f"CMD_{name}", response=r.hex()[:60] if r else None)
        return r

    async def download_file(self, session_id, file_size):
        self.file_data.clear()
        self.file_done.clear()
        sync = bytes([1, 28, 0]) + struct.pack("<I", session_id) + struct.pack("<I", 0) + struct.pack("<I", file_size)
        await self.cl.write_gatt_char(W, self._encrypt(sync), response=False)
        start = time.time()
        while not self.file_done.is_set():
            try:
                await asyncio.wait_for(self.file_done.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                if time.time() - start > 120:
                    break
                pct = len(self.file_data) * 100 // max(file_size, 1)
                print(f"\r  Downloading: {len(self.file_data)}/{file_size} ({pct}%)    ", end="", flush=True)
        elapsed = time.time() - start
        print(f"\r  Downloaded: {len(self.file_data)} bytes in {elapsed:.1f}s ({len(self.file_data)/max(elapsed,1)/1024:.1f} KB/s)")
        self.log.log("FILE_DOWNLOADED", size=len(self.file_data), seconds=round(elapsed, 1))
        return bytes(self.file_data)


async def main():
    record_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    print("=" * 60)
    print("  PLAUD SYNC — NO DEPAIR, PERSISTENT PAIRING")
    print("=" * 60)

    log = SessionLog()

    # === SCAN ===
    print("\nScanning...")
    target = None
    for attempt in range(10):
        found = await BleakScanner.discover(timeout=5, return_adv=True)
        for a, (d, adv) in found.items():
            if 0x5D in adv.manufacturer_data:
                target = d
                log.log("DEVICE_FOUND", address=a, rssi=adv.rssi)
        if target:
            break
        print(f"  scan {attempt + 1}...")
        await asyncio.sleep(3)
    if not target:
        print("Device not found.")
        log.save_final("NOT_FOUND")
        return

    # === CONNECT ===
    cl = BleakClient(target, timeout=30.0)
    try:
        await cl.connect()
        log.log("CONNECTED", mtu=cl.mtu_size)
    except Exception as e:
        log.log("CONNECT_FAILED", error=str(e))
        log.save_final("CONNECT_FAILED")
        return

    # === HANDSHAKE ===
    print("\nHandshake...")
    keys = await handshake(cl, log)
    if not keys:
        print("Handshake failed!")
        log.save_final("HANDSHAKE_FAILED")
        await cl.disconnect()
        return
    key, nonce, aad = keys
    print("  PLAUD.AI validated!\n")

    # === ENCRYPTED CHANNEL ===
    ch = EncryptedChannel(cl, key, nonce, aad, log)
    await ch.start()

    # Setup handshakes
    token32 = BIND_TOKEN[:32].encode("ascii")
    await ch.cmd("HS1", bytes([1, 1, 0, 2, 0, 0]) + token32)
    await ch.cmd("HS2", bytes([1, 1, 0, 2, 0, 1]) + token32)
    await ch.cmd("SYNC_TIME", bytes([1, 4, 0]) + struct.pack("<I", int(time.time())) + bytes([0, 0]))

    # Get state
    r = await ch.cmd("STATE", bytes([1, 3, 0]))
    if r and len(r) > 3:
        recording = r[3] == 1
        print(f"  Device state: recording={'YES' if recording else 'no'}")

    # Battery
    try:
        bat = await cl.read_gatt_char("00002a19-0000-1000-8000-00805f9b34fb")
        print(f"  Battery: {bat[0]}%")
        log.log("BATTERY", level=bat[0])
    except:
        pass

    # === RECORD ===
    print(f"\n--- RECORDING ({record_seconds}s) ---")
    r = await ch.cmd("START_REC", bytes([1, 20, 0]))
    if r:
        print(f"  Recording started!")
        await asyncio.sleep(record_seconds)
        r = await ch.cmd("STOP_REC", bytes([1, 23, 0]))
        if r:
            print(f"  Recording stopped.")
    else:
        print(f"  Failed to start recording. Checking existing files...")

    await asyncio.sleep(1)

    # === LIST FILES ===
    print(f"\n--- FILES ---")
    r = await ch.cmd("FILES", bytes([1, 26, 0]), timeout=10)
    files = []
    if r and len(r) >= 9:
        total = struct.unpack_from("<H", r, 7)[0]
        print(f"  Total files: {total}")
        if total > 0 and len(r) >= 21:
            sid = struct.unpack_from("<I", r, 11)[0]
            fsize = struct.unpack_from("<I", r, 15)[0]
            dur = (fsize / 80) * 20 / 1000
            files.append((sid, fsize))
            print(f"  Session {sid}: {fsize} bytes ({dur:.0f}s)")

    # === TRY WIFI TRANSFER ===
    print(f"\n--- TRYING WIFI TRANSFER ---")
    # cmd_id=10 (Switch WiFi Mode) to open WiFi
    r = await ch.cmd("WIFI_OPEN", bytes([1, 10, 0]))
    if r:
        print(f"  WiFi mode response: {r.hex()[:40]}")
        # Check if device went into WiFi mode
        # Try getting WiFi file list
        r2 = await ch.cmd("WIFI_FILE_LIST", bytes([1, 10, 0, 1]), timeout=5)
        if r2:
            print(f"  WiFi file list: {r2.hex()[:40]}")
    else:
        print(f"  WiFi mode: no response (may not be supported or different cmd)")

    # === BLE DOWNLOAD (fallback) ===
    if files:
        sid, fsize = files[0]
        print(f"\n--- BLE DOWNLOAD ---")
        data = await ch.download_file(sid, fsize)
        if data:
            RECORDINGS_DIR.mkdir(exist_ok=True)
            filepath = RECORDINGS_DIR / f"ble_{sid}.opus"
            filepath.write_bytes(data)
            print(f"  Saved: {filepath}")
            log.log("FILE_SAVED", path=str(filepath), size=len(data))

    # === GET STORAGE ===
    r = await ch.cmd("STORAGE", bytes([1, 6, 0]))

    # === STAY CONNECTED (no depair!) ===
    await ch.stop()
    print(f"\n--- SESSION COMPLETE ---")
    print(f"  Device stays paired. NO depair sent.")
    print(f"  Disconnecting gracefully...")

    try:
        await cl.disconnect()
    except:
        pass

    log.save_final("SUCCESS_NO_DEPAIR")

    print(f"\n  Session saved: {log.path}")
    print(f"  Keys saved: session_keys.json")
    print(f"  Device is still paired — will reconnect on next run.")
    print(f"\n{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
