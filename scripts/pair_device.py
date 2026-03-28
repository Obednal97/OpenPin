#!/usr/bin/env python3
"""Device pairing — based on ACTUAL captured BLE traffic.

Press device button, disconnect from phone, then run immediately.

Verified flow from PacketLogger capture:
1. SN sig (cmd 0xFE10/65040, 3 chunks) → 65041 ACK
2. Public key FULL PEM with headers (cmd 0xFE12/65042, 5 chunks) → 3 chunks RSA ciphertext
3. RSA decrypt → ChaCha20 key/nonce/aad → validate "PLAUD.AI"
4. Encrypted handshake commands (with 4-byte LE sequence counter prefix)
"""
import asyncio, struct, base64, json
from bleak import BleakClient, BleakScanner
from src.crypto import (
    get_public_key_full_pem, rsa_decrypt, load_rsa_private_key,
    parse_handshake_blob, chacha20_decrypt,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

SN_SIG = "TOb4HuuFPMhEB8bMCR3SFlqTRO9ihuhsHp0Jp3bwBGDYpew7FI9rFykg2JUKGLfe50BJS4z6Esm2Mt7Bqv6e5nz0j6jRCKWVWTuNiFY6a2iGY2lAvghwP8+JrqHb2RlTgoCj+vYXxAlMpZ5eMFCmTudiCmJt4aPzrJFURyM8OJfVJ8IHZi2/WbkR/i0vLrVuuLO3aShZrFjR7Enw0MQYUl4RMnHxk4qEwr7MV7qSTjyA7qeYcZY09gfS9+GmbVhwU4ZS4doiD+8gk6AmOn4zn20C3sCS1EOo3KGJgTNzSdZA59d/YQLql3bTjL3VsJYVvQlP5Zzfa5nHLcd3fN588w=="
BIND_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2NjllNzgwNDE4ZDg0OWRhOGFmMDU2MzIzOTljMjVhZSIsImF1ZCI6IiIsImV4cCI6MjAzMzczMzY5NSwiaWF0IjoxNzc0NTMzNjk1LCJjbGllbnRfaWQiOiJkZXZpY2UiLCJzbiI6Ijg4MjBCNTAyNTY5NDczMTcifQ.UJH_zwN0Imyc4W8dropHXy1_j7kN09aJf-2ZUtyLEJs"
W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"


async def main():
    # Scan
    for scan_attempt in range(5):
        found = await BleakScanner.discover(timeout=6, return_adv=True)
        t = None
        for a, (d, adv) in found.items():
            for c in adv.manufacturer_data:
                if c == 0x5D:
                    t = d
        if t:
            break
        print(f"Scan {scan_attempt+1}: not found, retrying...")
        await asyncio.sleep(3)
    if not t:
        print("NOT FOUND"); return

    # Connect
    for i in range(3):
        try:
            cl = BleakClient(t, timeout=15.0)
            await cl.connect()
            print(f"Connected! MTU={cl.mtu_size}")
            break
        except:
            await asyncio.sleep(2)
    else:
        print("Connect failed"); return

    got_ack = asyncio.Event()
    rsa_chunks = {}
    rsa_total = [0]
    rsa_done = asyncio.Event()
    pubkey_sent = [False]

    def h(sender, data):
        data = bytes(data)
        cid = struct.unpack_from("<H", data, 0)[0] if len(data) >= 2 else 0

        if cid == 65041:
            print(f"  [65041 ACK]")
            got_ack.set()
        elif cid == 65042 and pubkey_sent[0] and len(data) >= 4:
            idx = data[3]
            total = data[2]
            if idx not in rsa_chunks:
                rsa_chunks[idx] = data[4:]
                rsa_total[0] = total
                print(f"  [RSA chunk {idx}/{total}] {len(data)-4}B")
                if len(rsa_chunks) >= total:
                    rsa_done.set()
        elif cid == 65042:
            print(f"  [65042 pre-pubkey ack]")
        else:
            print(f"  [cmd={cid} {len(data)}B]: {data.hex()[:40]}")

    await cl.start_notify(N, h)

    # ================================================
    # STEP 1: SN signature (cmd 65040, like the capture)
    # ================================================
    sig = base64.b64decode(SN_SIG)
    tc = (len(sig) + 99) // 100
    print(f"\n[1] SN sig (cmd 65040, {tc} chunks)...")
    for i in range(tc):
        c = sig[i * 100:(i + 1) * 100]
        await cl.write_gatt_char(W, struct.pack("<H", 65040) + bytes([tc, i]) + c, response=False)
        await asyncio.sleep(0.05)

    # Capture also shows it sent twice — let's do the same
    for i in range(tc):
        c = sig[i * 100:(i + 1) * 100]
        await cl.write_gatt_char(W, struct.pack("<H", 65040) + bytes([tc, i]) + c, response=False)
        await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(got_ack.wait(), timeout=10)
    except asyncio.TimeoutError:
        # Try with 65056 (force clear) as fallback
        print("  No ACK with 65040, trying 65056...")
        for i in range(tc):
            c = sig[i * 100:(i + 1) * 100]
            await cl.write_gatt_char(W, struct.pack("<H", 65056) + bytes([tc, i]) + c, response=False)
            await asyncio.sleep(0.05)
        try:
            await asyncio.wait_for(got_ack.wait(), timeout=10)
        except asyncio.TimeoutError:
            print("  NO ACK at all"); await cl.disconnect(); return

    print("  *** ACK RECEIVED ***")

    # ================================================
    # STEP 2: Public key FULL PEM (cmd 65042, 5 chunks)
    # ================================================
    pubkey_sent[0] = False  # Don't collect RSA yet
    full_pem = get_public_key_full_pem().encode("ascii")
    pc = (len(full_pem) + 99) // 100
    print(f"\n[2] Public key FULL PEM ({len(full_pem)}B, {pc} chunks)...")

    for i in range(pc):
        c = full_pem[i * 100:(i + 1) * 100]
        await cl.write_gatt_char(W, struct.pack("<H", 65042) + bytes([pc, i]) + c, response=True)
        await asyncio.sleep(0.05)

    # NOW start collecting RSA data
    pubkey_sent[0] = True
    print("  Pubkey sent. Waiting for RSA response...")

    try:
        await asyncio.wait_for(rsa_done.wait(), timeout=15)
    except asyncio.TimeoutError:
        print(f"  Timeout. Got {len(rsa_chunks)}/{rsa_total[0]} RSA chunks")
        if not rsa_chunks:
            await cl.disconnect(); return

    # ================================================
    # STEP 3: RSA decrypt and validate
    # ================================================
    blob = bytearray()
    for i in sorted(rsa_chunks.keys()):
        blob.extend(rsa_chunks[i])
    print(f"\n[3] RSA blob: {len(blob)}B")

    if len(blob) != 256:
        print(f"  Wrong size (need 256)"); await cl.disconnect(); return

    dec = rsa_decrypt(bytes(blob), load_rsa_private_key())
    print(f"  Decrypted: {len(dec)}B")

    if len(dec) < 56:
        print(f"  Too short"); await cl.disconnect(); return

    key = dec[:32]
    nonce = dec[32:44]
    aad = dec[44:56]
    ct = dec[56:]
    print(f"  Key:   {key.hex()}")
    print(f"  Nonce: {nonce.hex()}")
    print(f"  AAD:   {aad.hex()}")
    print(f"  CT:    {ct.hex()} ({len(ct)}B)")

    cipher = ChaCha20Poly1305(key)
    try:
        pt = cipher.decrypt(nonce, ct, aad)
        print(f"  Plaintext: {pt}")
        if pt == b"PLAUD.AI":
            print("\n  =============================================")
            print("  === HANDSHAKE VALIDATED — PLAUD.AI ===")
            print("  =============================================")
            json.dump({
                "key": key.hex(), "nonce": nonce.hex(), "aad": aad.hex(),
                "status": "SUCCESS"
            }, open("session_keys.json", "w"), indent=2)
            print("  Session keys saved!")
        else:
            print(f"  Unexpected plaintext: {pt}")
    except Exception as e:
        print(f"  ChaCha20 failed: {e}")
        # Try AES-GCM
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            pt = AESGCM(key).decrypt(nonce, ct, aad)
            print(f"  AES-GCM: {pt}")
        except Exception as e2:
            print(f"  AES-GCM also failed: {e2}")

    await cl.disconnect()
    print("\nDone.")


asyncio.run(main())
