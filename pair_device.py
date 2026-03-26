#!/usr/bin/env python3
"""Device pairing — exact flow from decompiled Android app.

Press device button, then run this immediately.

Flow (portVersion >= 20, isForceClear=true):
1. Connect + enable notifications on 0x2BB0
2. Send SN signature (cmd 0xFE20/65056) in chunks
3. Wait for 0xFE11 (65041) ACK on last chunk callback
4. Send RSA public key (cmd 0xFE12/65042) in chunks
5. Device sends RSA encrypted session keys via 0xFE12 notifications
6. RSA decrypt -> ChaCha20 key/nonce/aad + validate "PLAUD.AI"
7. Send encrypted first handshake (cmd 1, subtype 0)
8. Send encrypted second handshake (cmd 1, subtype 1)
"""
import asyncio, struct, base64, json, time
from bleak import BleakClient, BleakScanner
from src.crypto import get_public_key_pem_body, rsa_decrypt, load_rsa_private_key
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

SN_SIG = "<REDACTED_SN_SIGNATURE>"
BIND_TOKEN = "<REDACTED_JWT>"
W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"


async def main():
    print("Scanning 10s...")
    found = await BleakScanner.discover(timeout=10, return_adv=True)
    t = None
    for a, (d, adv) in found.items():
        for c in adv.manufacturer_data:
            if c == 0x5D:
                t = d
    if not t:
        print("NOT FOUND — press button and retry")
        return

    for i in range(3):
        try:
            cl = BleakClient(t, timeout=30.0)
            await cl.connect()
            print(f"Connected! MTU: {cl.mtu_size}")
            break
        except:
            await asyncio.sleep(2)
    else:
        return

    # State
    got_ack = asyncio.Event()
    rsa_chunks = {}  # idx -> payload (without 4-byte header)
    rsa_total = [0]
    rsa_done = asyncio.Event()
    pubkey_sent = [False]
    all_log = []

    def notification_handler(sender, data):
        data = bytes(data)
        cid = struct.unpack_from("<H", data, 0)[0] if len(data) >= 2 else 0
        ts = time.time()
        all_log.append((ts, cid, data))

        # 65041 = SN sig accepted
        if cid == 65041:
            print(f"  [{ts:.1f}] *** 65041 ACK — SN sig accepted ***")
            got_ack.set()
            return

        # 65042 = either pubkey write confirmation OR RSA encrypted keys
        if cid == 65042 and len(data) >= 4:
            total = data[2]
            idx = data[3]
            payload = data[4:]

            # Only collect AFTER pubkey has been fully sent
            if pubkey_sent[0]:
                if idx not in rsa_chunks:
                    rsa_chunks[idx] = payload
                    rsa_total[0] = total
                    print(f"  [{ts:.1f}] RSA chunk [{idx}/{total}] {len(payload)}B (post-pubkey)")
                    if len(rsa_chunks) >= total:
                        rsa_done.set()
            else:
                print(f"  [{ts:.1f}] pubkey ack [{idx}/{total}] {len(payload)}B (pre-pubkey-done)")
            return

        print(f"  [{ts:.1f}] cmd={cid}(0x{cid:04X}) [{len(data)}B]")

    await cl.start_notify(N, notification_handler)
    try:
        await cl.start_notify("0000b004-0000-1000-8000-00805f9b34fb", notification_handler)
    except:
        pass

    # ============================================================
    # STEP 2: Send SN signature (cmd 0xFE20 = 65056, force clear)
    # ============================================================
    sig = base64.b64decode(SN_SIG)
    tc = (len(sig) + 99) // 100
    print(f"\n[Step 2] SN Sig: {len(sig)}B, {tc} chunks, cmd=0xFE20...")
    for i in range(tc):
        c = sig[i * 100:(i + 1) * 100]
        pkt = struct.pack("<H", 65056) + bytes([tc, i]) + c
        await cl.write_gatt_char(W, pkt, response=False)
        await asyncio.sleep(0.05)

    # Wait for 65041 ACK
    print("[Step 3] Waiting for 65041 ACK...")
    try:
        await asyncio.wait_for(got_ack.wait(), timeout=15)
    except asyncio.TimeoutError:
        print("NO ACK. Device needs fresh button press.")
        await cl.disconnect()
        return

    print("*** SN SIGNATURE ACCEPTED ***")

    # ============================================================
    # STEP 4: Send RSA public key (cmd 0xFE12 = 65042)
    # ============================================================
    pk = get_public_key_pem_body().encode()
    pc = (len(pk) + 99) // 100
    print(f"\n[Step 4] RSA Public Key: {len(pk)}B, {pc} chunks, cmd=0xFE12...")
    for i in range(pc):
        c = pk[i * 100:(i + 1) * 100]
        pkt = struct.pack("<H", 65042) + bytes([pc, i]) + c
        await cl.write_gatt_char(W, pkt, response=False)
        await asyncio.sleep(0.1)

    # Mark pubkey as fully sent — now 65042 notifications are RSA data
    await asyncio.sleep(1)  # Brief pause for write confirmations to arrive
    pubkey_sent[0] = True
    print("Pubkey sent. Now collecting RSA data...")

    # ============================================================
    # STEP 5: Wait for RSA encrypted session keys
    # ============================================================
    print(f"\n[Step 5] Waiting for RSA encrypted keys (up to 30s)...")
    try:
        await asyncio.wait_for(rsa_done.wait(), timeout=30)
    except asyncio.TimeoutError:
        print(f"Timeout. Got {len(rsa_chunks)} RSA chunks (needed {rsa_total[0] or '?'})")

    if not rsa_chunks:
        # Maybe the RSA data arrived during pubkey send (before flag was set)
        # Check all_log for 65042 packets that look like RSA (not memory addresses)
        print("\nChecking all logged 65042 packets for RSA data...")
        for ts, cid, data in all_log:
            if cid == 65042 and len(data) >= 4:
                idx = data[3]
                payload = data[4:]
                # RSA data should look random, not have 0x10 addresses
                has_arm_addrs = any(
                    payload[i:i+2] == b'\x05\x10' or payload[i:i+2] == b'\x04\x10'
                    for i in range(0, min(len(payload), 20), 2)
                )
                if not has_arm_addrs and payload[:4] != b'\x00\x00\x00\x00':
                    print(f"  Potential RSA [{idx}]: {payload[:20].hex()}")
                    if idx not in rsa_chunks:
                        rsa_chunks[idx] = payload
                        rsa_total[0] = data[2]

    print(f"\nTotal RSA chunks: {len(rsa_chunks)}")

    # ============================================================
    # STEP 6: RSA decrypt and validate
    # ============================================================
    if rsa_chunks:
        blob = bytearray()
        for i in sorted(rsa_chunks.keys()):
            blob.extend(rsa_chunks[i])
        print(f"RSA blob: {len(blob)}B")

        if len(blob) == 256:
            privkey = load_rsa_private_key()
            dec = rsa_decrypt(bytes(blob), privkey)
            print(f"Decrypted: {len(dec)}B")
            print(f"Hex: {dec.hex()}")

            if len(dec) >= 80:
                key = dec[:32]
                nonce = dec[32:44]
                aad = dec[44:56]
                ct = dec[56:]
                print(f"\nkey({len(key)}):   {key.hex()}")
                print(f"nonce({len(nonce)}): {nonce.hex()}")
                print(f"aad({len(aad)}):   {aad.hex()}")
                print(f"ct({len(ct)}):    {ct.hex()}")

                try:
                    cipher = ChaCha20Poly1305(key)
                    pt = cipher.decrypt(nonce, ct, aad)
                    print(f"\nDecrypted validation: {pt}")

                    if pt == b"PLAUD.AI":
                        print("\n=============================================")
                        print("=== HANDSHAKE VALIDATED — PLAUD.AI ===")
                        print("=============================================")

                        # Save session
                        session = {
                            "key": key.hex(), "nonce": nonce.hex(), "aad": aad.hex(),
                            "status": "SUCCESS",
                        }
                        json.dump(session, open("session_keys.json", "w"), indent=2)

                        # STEP 7+8: Send encrypted handshakes
                        # ... (implement next)
                except Exception as e:
                    print(f"ChaCha20 validation failed: {e}")
            else:
                print(f"Decrypted data too short ({len(dec)}B, need 80+)")
        else:
            print(f"RSA blob wrong size ({len(blob)}B, need 256)")
    else:
        print("\nNo RSA data received.")
        print("\nFull notification log:")
        for ts, cid, data in all_log:
            print(f"  {ts:.1f} cmd={cid}(0x{cid:04X}) [{len(data)}B]: {data.hex()[:60]}")

    await cl.disconnect()
    print("\nDone.")


asyncio.run(main())
