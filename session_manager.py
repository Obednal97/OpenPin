#!/usr/bin/env python3
"""Session manager — connects, saves all keys/state, then cleanly disconnects.

NEVER overwrites session data. Each connection is logged with a timestamp.
All keys, tokens, and device responses are preserved in sessions/ directory.
"""
import asyncio, struct, base64, json, time, os
from datetime import datetime
from pathlib import Path
from bleak import BleakScanner, BleakClient
from src.crypto import get_public_key_full_pem, rsa_decrypt, load_rsa_private_key
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

SN_SIG = "TOb4HuuFPMhEB8bMCR3SFlqTRO9ihuhsHp0Jp3bwBGDYpew7FI9rFykg2JUKGLfe50BJS4z6Esm2Mt7Bqv6e5nz0j6jRCKWVWTuNiFY6a2iGY2lAvghwP8+JrqHb2RlTgoCj+vYXxAlMpZ5eMFCmTudiCmJt4aPzrJFURyM8OJfVJ8IHZi2/WbkR/i0vLrVuuLO3aShZrFjR7Enw0MQYUl4RMnHxk4qEwr7MV7qSTjyA7qeYcZY09gfS9+GmbVhwU4ZS4doiD+8gk6AmOn4zn20C3sCS1EOo3KGJgTNzSdZA59d/YQLql3bTjL3VsJYVvQlP5Zzfa5nHLcd3fN588w=="
BIND_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2NjllNzgwNDE4ZDg0OWRhOGFmMDU2MzIzOTljMjVhZSIsImF1ZCI6IiIsImV4cCI6MjAzMzczMzY5NSwiaWF0IjoxNzc0NTMzNjk1LCJjbGllbnRfaWQiOiJkZXZpY2UiLCJzbiI6Ijg4MjBCNTAyNTY5NDczMTcifQ.UJH_zwN0Imyc4W8dropHXy1_j7kN09aJf-2ZUtyLEJs"
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
        "device_sn": "8820B50256947317",
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

    # === ENCRYPTED SESSION ===
    log_step("ENCRYPTED_SESSION")
    cipher = ChaCha20Poly1305(key)
    seq = [0]

    def encrypt(plaintext):
        seq[0] += 1
        return cipher.encrypt(nonce, struct.pack("<I", seq[0]) + plaintext, aad)

    def decrypt(ciphertext):
        return cipher.decrypt(nonce, ciphertext, aad)[4:]

    async def send_cmd(name, plaintext, timeout=5.0):
        ev = asyncio.Event()
        resp = [None]

        def h(sender, data):
            try:
                resp[0] = decrypt(bytes(data))
                ev.set()
            except:
                pass

        await cl.start_notify(N, h)
        await cl.write_gatt_char(W, encrypt(plaintext), response=False)
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        await cl.stop_notify(N)

        result = resp[0].hex() if resp[0] else None
        session_log["steps"].append({
            "step": f"CMD_{name}",
            "time": datetime.now().isoformat(),
            "response": result,
        })
        if resp[0]:
            print(f"  {name}: {result[:60]}")
        else:
            print(f"  {name}: no response")
        return resp[0]

    # Send commands
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
            print(f"  DISCONNECTED at {i}s!")
            break
    else:
        print(f"  Held for {hold_seconds}s OK")

    # === DEPAIR ===
    if cl.is_connected:
        log_step("DEPAIR")
        r = await send_cmd("DEPAIR", bytes([1, 5, 0]))
        session_log["depair_response"] = r.hex() if r else None
        if r:
            print(f"  Depair sent successfully!")
        else:
            print(f"  Depair: no response (may still have worked)")

    # === DISCONNECT ===
    log_step("DISCONNECT")
    if cl.is_connected:
        await cl.disconnect()
    print("  Disconnected.")

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
