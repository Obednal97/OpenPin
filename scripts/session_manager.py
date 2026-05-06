#!/usr/bin/env python3
"""Session manager — connects, saves all keys/state, then cleanly disconnects.

NEVER overwrites session data. Each connection is logged with a timestamp.
All keys, tokens, and device responses are preserved in sessions/ directory.
"""
import asyncio, struct, base64, json, time, os, sys
from datetime import datetime
from pathlib import Path
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

SN = "8820A0000000XXXX"
BIND_TOKEN = _cfg.get("device_bind_tokens", {}).get(SN)
SN_SIG = _cfg.get("device_sn_signatures", {}).get(SN)
if not BIND_TOKEN or not SN_SIG:
    sys.exit(f"No credentials for SN {SN} in config — re-pair device via the Plaud V2 app first.")

W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"

SESSIONS_DIR = Path("sessions")


def save_session(session_data: dict):
    """Save session data with timestamp. NEVER overwrites previous sessions."""
    SESSIONS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = SESSIONS_DIR / f"session_{ts}.json"
    # Ensure unique filename
    counter = 0
    while filepath.exists():
        counter += 1
        filepath = SESSIONS_DIR / f"session_{ts}_{counter}.json"
    with open(filepath, "w") as f:
        json.dump(session_data, f, indent=2)
    # Also update session_keys.json as the "latest" (for quick access)
    with open("session_keys.json", "w") as f:
        json.dump(session_data, f, indent=2)
    print(f"  Session saved: {filepath}")
    return filepath


async def main():
    hold_seconds = 30
    print("=" * 60)
    print("  PLAUD SESSION MANAGER")
    print(f"  Will: connect → handshake → hold {hold_seconds}s → depair → disconnect")
    print("=" * 60)

    session_log = {
        "timestamp": datetime.now().isoformat(),
        "device_sn": "8820A0000000XXXX",
        "steps": [],
    }

    def log_step(name, data=None):
        entry = {"step": name, "time": datetime.now().isoformat()}
        if data:
            entry["data"] = data
        session_log["steps"].append(entry)
        print(f"\n[{name}]")

    # === SCAN ===
    log_step("SCAN")
    target = None
    for attempt in range(10):
        found = await BleakScanner.discover(timeout=5, return_adv=True)
        for a, (d, adv) in found.items():
            for c in adv.manufacturer_data:
                if c == 0x5D:
                    target = d
                    mfr = adv.manufacturer_data[c].hex()
                    session_log["device_address"] = a
                    session_log["manufacturer_data"] = mfr
                    print(f"  Found: {d.name} {a} RSSI={adv.rssi}")
        if target:
            break
        print(f"  Scan {attempt+1}/10...")
        await asyncio.sleep(3)
    if not target:
        log_step("FAILED", {"reason": "Device not found"})
        save_session(session_log)
        return False

    # === CONNECT ===
    log_step("CONNECT")
    cl = BleakClient(target, timeout=30.0)
    try:
        await cl.connect()
        print(f"  Connected! MTU={cl.mtu_size}")
        session_log["mtu"] = cl.mtu_size
    except Exception as e:
        log_step("FAILED", {"reason": f"Connect: {e}"})
        save_session(session_log)
        return False

    # === HANDSHAKE ===
    log_step("HANDSHAKE")
    ack_event = asyncio.Event()
    rsa_chunks = {}
    rsa_done = asyncio.Event()
    pk_sent = [False]
    all_raw = []

    def notification_handler(sender, data):
        data = bytes(data)
        all_raw.append(data.hex())
        cid = struct.unpack_from("<H", data, 0)[0] if len(data) >= 2 else 0
        if cid == 65041:
            ack_event.set()
        elif cid == 65042 and pk_sent[0] and len(data) >= 4:
            idx = data[3]
            if idx not in rsa_chunks:
                rsa_chunks[idx] = data[4:]
                if len(rsa_chunks) >= data[2]:
                    rsa_done.set()

    await cl.start_notify(N, notification_handler)

    # SN Signature (sent twice as per capture)
    sig = base64.b64decode(SN_SIG)
    tc = (len(sig) + 99) // 100
    print("  Sending SN signature...", end="", flush=True)
    for _ in range(2):
        for i in range(tc):
            await cl.write_gatt_char(
                W, struct.pack("<H", 65040) + bytes([tc, i]) + sig[i*100:(i+1)*100],
                response=False,
            )
            await asyncio.sleep(0.02)
    try:
        await asyncio.wait_for(ack_event.wait(), timeout=10)
        print(" ACK!")
        session_log["steps"].append({"step": "SN_ACK", "time": datetime.now().isoformat()})
    except asyncio.TimeoutError:
        print(" NO ACK")
        log_step("FAILED", {"reason": "No SN ACK"})
        save_session(session_log)
        await cl.disconnect()
        return False

    # RSA Public Key (full PEM, 5 chunks)
    print("  Sending RSA public key...", end="", flush=True)
    full_pem = get_public_key_full_pem().encode()
    pc = (len(full_pem) + 99) // 100
    for i in range(pc):
        await cl.write_gatt_char(
            W, struct.pack("<H", 65042) + bytes([pc, i]) + full_pem[i*100:(i+1)*100],
            response=True,
        )
        await asyncio.sleep(0.02)
    pk_sent[0] = True
    try:
        await asyncio.wait_for(rsa_done.wait(), timeout=15)
        print(" RSA received!")
    except asyncio.TimeoutError:
        print(f" RSA timeout ({len(rsa_chunks)} chunks)")
        log_step("FAILED", {"reason": "RSA timeout", "chunks": len(rsa_chunks)})
        save_session(session_log)
        await cl.disconnect()
        return False

    # RSA Decrypt
    blob = bytearray()
    for i in sorted(rsa_chunks.keys()):
        blob.extend(rsa_chunks[i])

    session_log["rsa_blob_hex"] = blob.hex()
    session_log["rsa_blob_size"] = len(blob)

    dec = rsa_decrypt(bytes(blob), load_rsa_private_key())
    key, nonce, aad, ct = dec[:32], dec[32:44], dec[44:56], dec[56:]

    pt = ChaCha20Poly1305(key).decrypt(nonce, ct, aad)
    assert pt == b"PLAUD.AI", f"Validation failed: {pt}"
    print(f"  PLAUD.AI validated!")

    session_log["chacha_key"] = key.hex()
    session_log["chacha_nonce"] = nonce.hex()
    session_log["chacha_aad"] = aad.hex()
    session_log["validation"] = "PLAUD.AI"

    await cl.stop_notify(N)

    # === SAVE KEYS IMMEDIATELY (before any commands that might fail) ===
    session_log["chacha_key"] = key.hex()
    session_log["chacha_nonce"] = nonce.hex()
    session_log["chacha_aad"] = aad.hex()
    session_log["validation"] = "PLAUD.AI"
    session_log["status"] = "HANDSHAKE_OK"
    save_session(session_log)  # Save NOW so we never lose keys

    # === ENCRYPTED SESSION ===
    log_step("ENCRYPTED_SESSION")
    cipher = ChaCha20Poly1305(key)
    seq = [0]

    def encrypt(plaintext):
        seq[0] += 1
        return cipher.encrypt(nonce, struct.pack("<I", seq[0]) + plaintext, aad)

    def decrypt(ciphertext):
        return cipher.decrypt(nonce, ciphertext, aad)[4:]

    # Use a persistent notification handler instead of start/stop per command
    cmd_responses = []
    cmd_event = asyncio.Event()

    def persistent_handler(sender, data):
        try:
            dec = decrypt(bytes(data))
            cmd_responses.append(dec)
            cmd_event.set()
        except:
            pass

    await cl.start_notify(N, persistent_handler)

    async def send_cmd(name, plaintext, timeout=5.0):
        cmd_event.clear()
        cmd_responses.clear()
        try:
            await cl.write_gatt_char(W, encrypt(plaintext), response=False)
        except Exception as e:
            print(f"  {name}: write failed ({e})")
            return None
        try:
            await asyncio.wait_for(cmd_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

        resp = cmd_responses[0] if cmd_responses else None
        result = resp.hex() if resp else None
        session_log["steps"].append({
            "step": f"CMD_{name}",
            "time": datetime.now().isoformat(),
            "response": result,
        })
        if resp:
            print(f"  {name}: {result[:60]}")
        else:
            print(f"  {name}: no response")
        return resp

    # Send handshake command FIRST (required before other commands work)
    token32 = BIND_TOKEN[:32].encode("ascii")
    hs_cmd = bytes([1, 1, 0, 2, 0, 0]) + token32
    await send_cmd("HANDSHAKE_CMD", hs_cmd)

    # Second handshake (subtype=1)
    hs2_cmd = bytes([1, 1, 0, 2, 0, 1]) + token32
    await send_cmd("HANDSHAKE2_CMD", hs2_cmd)

    # Now send other commands
    await send_cmd("SYNC_TIME", bytes([1, 4, 0]) + struct.pack("<I", int(time.time())) + bytes([0, 0]))
    await send_cmd("GET_STATE", bytes([1, 3, 0]))
    await send_cmd("GET_FILES", bytes([1, 26, 0]), timeout=10)
    await send_cmd("GET_STORAGE", bytes([1, 6, 0]))

    # Battery
    try:
        bat = await cl.read_gatt_char("00002a19-0000-1000-8000-00805f9b34fb")
        session_log["battery"] = bat[0]
        print(f"  Battery: {bat[0]}%")
    except:
        pass

    # === HOLD ===
    log_step("HOLD", {"seconds": hold_seconds})
    print(f"  Holding connection for {hold_seconds}s...")
    for i in range(hold_seconds):
        await asyncio.sleep(1)
        if not cl.is_connected:
            print(f"  Device disconnected at {i}s!")
            session_log["hold_disconnected_at"] = i
            break
    else:
        print(f"  Held for {hold_seconds}s OK")
        session_log["hold_completed"] = True

    # === DEPAIR ===
    if cl.is_connected:
        log_step("DEPAIR")
        try:
            r = await send_cmd("DEPAIR", bytes([1, 5, 0]))
            session_log["depair_response"] = r.hex() if r else None
            if r:
                print(f"  Depair sent successfully!")
            else:
                print(f"  Depair: no response (may still have worked)")
        except Exception as e:
            print(f"  Depair error: {e}")
            session_log["depair_error"] = str(e)
    else:
        print("  Skipping depair (already disconnected)")

    # === VERIFY DEPAIR ===
    if cl.is_connected:
        log_step("VERIFY_DEPAIR")
        print("  Verifying depair by re-sending SN sig...")
        verify_ack = asyncio.Event()
        verify_got_callbacks = [False]

        async def verify_handler_setup():
            def vh(sender, data):
                data = bytes(data)
                cid = struct.unpack_from("<H", data, 0)[0] if len(data) >= 2 else 0
                if cid == 65041:
                    verify_ack.set()
                elif cid == 65042:
                    verify_got_callbacks[0] = True
            try:
                await cl.stop_notify(N)
            except:
                pass
            await cl.start_notify(N, vh)

        try:
            await verify_handler_setup()
            sig = base64.b64decode(SN_SIG)
            tc = (len(sig) + 99) // 100
            for i in range(tc):
                await cl.write_gatt_char(
                    W, struct.pack("<H", 65040) + bytes([tc, i]) + sig[i*100:(i+1)*100],
                    response=False,
                )
                await asyncio.sleep(0.02)
            await asyncio.sleep(3)

            if verify_ack.is_set():
                print("  ✓ DEPAIR CONFIRMED — device accepted fresh SN sig (65041 ACK)")
                session_log["depair_verified"] = True
            elif verify_got_callbacks[0]:
                print("  ✗ DEPAIR FAILED — device sent 65042 callbacks (still bonded)")
                session_log["depair_verified"] = False
            else:
                print("  ? DEPAIR UNCLEAR — no response to verification SN sig")
                session_log["depair_verified"] = "unknown"
        except Exception as e:
            print(f"  Verify error: {e}")
            session_log["depair_verify_error"] = str(e)

    # === DISCONNECT ===
    log_step("DISCONNECT")
    try:
        if cl.is_connected:
            try:
                await cl.stop_notify(N)
            except:
                pass
            await cl.disconnect()
        print("  Disconnected.")
    except Exception as e:
        print(f"  Disconnect: {e}")
        session_log["disconnect_error"] = str(e)

    # === SAVE ===
    session_log["status"] = "SUCCESS"
    session_log["completed"] = datetime.now().isoformat()
    filepath = save_session(session_log)

    print("\n" + "=" * 60)
    print(f"  SESSION COMPLETE — saved to {filepath}")
    print(f"  Key:   {key.hex()[:32]}...")
    print(f"  Nonce: {nonce.hex()}")
    print(f"  AAD:   {aad.hex()}")
    print("=" * 60)
    return True


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)
