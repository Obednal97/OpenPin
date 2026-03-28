#!/usr/bin/env python3
"""Connect to paired device, send encrypted commands, list/download files.

Run pair_device.py first to establish session keys.
Then disconnect and reconnect with this script.

Or: this script does the full flow (pair + commands) in one shot.
"""
import asyncio, struct, base64, json, time, os
from pathlib import Path
from bleak import BleakClient, BleakScanner
from src.crypto import (
    get_public_key_full_pem, rsa_decrypt, load_rsa_private_key,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

SN_SIG = "TOb4HuuFPMhEB8bMCR3SFlqTRO9ihuhsHp0Jp3bwBGDYpew7FI9rFykg2JUKGLfe50BJS4z6Esm2Mt7Bqv6e5nz0j6jRCKWVWTuNiFY6a2iGY2lAvghwP8+JrqHb2RlTgoCj+vYXxAlMpZ5eMFCmTudiCmJt4aPzrJFURyM8OJfVJ8IHZi2/WbkR/i0vLrVuuLO3aShZrFjR7Enw0MQYUl4RMnHxk4qEwr7MV7qSTjyA7qeYcZY09gfS9+GmbVhwU4ZS4doiD+8gk6AmOn4zn20C3sCS1EOo3KGJgTNzSdZA59d/YQLql3bTjL3VsJYVvQlP5Zzfa5nHLcd3fN588w=="
BIND_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2NjllNzgwNDE4ZDg0OWRhOGFmMDU2MzIzOTljMjVhZSIsImF1ZCI6IiIsImV4cCI6MjAzMzczMzY5NSwiaWF0IjoxNzc0NTMzNjk1LCJjbGllbnRfaWQiOiJkZXZpY2UiLCJzbiI6Ijg4MjBCNTAyNTY5NDczMTcifQ.UJH_zwN0Imyc4W8dropHXy1_j7kN09aJf-2ZUtyLEJs"
W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"


class EncryptedSession:
    def __init__(self, key_hex, nonce_hex, aad_hex):
        self.key = bytes.fromhex(key_hex)
        self.nonce = bytes.fromhex(nonce_hex)
        self.aad = bytes.fromhex(aad_hex)
        self.cipher = ChaCha20Poly1305(self.key)
        self.send_seq = 0
        self.recv_seq = 0

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt outgoing data: prepend 4-byte LE seq counter, then encrypt."""
        self.send_seq += 1
        prefixed = struct.pack("<I", self.send_seq) + plaintext
        return self.cipher.encrypt(self.nonce, prefixed, self.aad)

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt incoming data, strip 4-byte seq counter."""
        decrypted = self.cipher.decrypt(self.nonce, ciphertext, self.aad)
        # First 4 bytes are the sequence counter
        seq = struct.unpack_from("<I", decrypted, 0)[0]
        return decrypted[4:]


async def do_handshake(cl) -> EncryptedSession | None:
    """Perform full RSA handshake, return encrypted session."""
    got_ack = asyncio.Event()
    rsa_chunks = {}
    rsa_done = asyncio.Event()
    pubkey_sent = [False]

    def h(sender, data):
        data = bytes(data)
        cid = struct.unpack_from("<H", data, 0)[0] if len(data) >= 2 else 0
        if cid == 65041:
            got_ack.set()
        elif cid == 65042 and pubkey_sent[0] and len(data) >= 4:
            idx = data[3]
            total = data[2]
            if idx not in rsa_chunks:
                rsa_chunks[idx] = data[4:]
                if len(rsa_chunks) >= total:
                    rsa_done.set()

    await cl.start_notify(N, h)

    # SN sig (65040)
    sig = base64.b64decode(SN_SIG)
    tc = (len(sig) + 99) // 100
    print("  SN sig...", end="", flush=True)
    for i in range(tc):
        c = sig[i * 100:(i + 1) * 100]
        await cl.write_gatt_char(W, struct.pack("<H", 65040) + bytes([tc, i]) + c, response=False)
        await asyncio.sleep(0.05)
    # Send twice (as capture shows)
    for i in range(tc):
        c = sig[i * 100:(i + 1) * 100]
        await cl.write_gatt_char(W, struct.pack("<H", 65040) + bytes([tc, i]) + c, response=False)
        await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(got_ack.wait(), timeout=10)
    except asyncio.TimeoutError:
        # Fallback to 65056
        for i in range(tc):
            c = sig[i * 100:(i + 1) * 100]
            await cl.write_gatt_char(W, struct.pack("<H", 65056) + bytes([tc, i]) + c, response=False)
            await asyncio.sleep(0.05)
        try:
            await asyncio.wait_for(got_ack.wait(), timeout=10)
        except asyncio.TimeoutError:
            print(" NO ACK")
            return None
    print(" ACK!", flush=True)

    # Pubkey FULL PEM (65042)
    full_pem = get_public_key_full_pem().encode()
    pc = (len(full_pem) + 99) // 100
    print("  Pubkey...", end="", flush=True)
    for i in range(pc):
        c = full_pem[i * 100:(i + 1) * 100]
        await cl.write_gatt_char(W, struct.pack("<H", 65042) + bytes([pc, i]) + c, response=True)
        await asyncio.sleep(0.05)
    pubkey_sent[0] = True

    try:
        await asyncio.wait_for(rsa_done.wait(), timeout=15)
    except asyncio.TimeoutError:
        print(f" RSA timeout ({len(rsa_chunks)} chunks)")
        return None
    print(" RSA received!", flush=True)

    # Decrypt
    blob = bytearray()
    for i in sorted(rsa_chunks.keys()):
        blob.extend(rsa_chunks[i])

    if len(blob) != 256:
        print(f"  RSA blob wrong size: {len(blob)}")
        return None

    dec = rsa_decrypt(bytes(blob), load_rsa_private_key())
    if len(dec) < 56:
        print(f"  Decrypted too short: {len(dec)}")
        return None

    key, nonce, aad, ct = dec[:32], dec[32:44], dec[44:56], dec[56:]
    cipher = ChaCha20Poly1305(key)
    pt = cipher.decrypt(nonce, ct, aad)
    if pt != b"PLAUD.AI":
        print(f"  Validation failed: {pt}")
        return None

    print("  PLAUD.AI validated!")
    await cl.stop_notify(N)
    return EncryptedSession(key.hex(), nonce.hex(), aad.hex())


async def send_command(cl, session: EncryptedSession, cmd: bytes, timeout: float = 5.0) -> bytes | None:
    """Send an encrypted command and wait for encrypted response."""
    response_event = asyncio.Event()
    response_data = [None]

    def h(sender, data):
        data = bytes(data)
        try:
            decrypted = session.decrypt(data)
            response_data[0] = decrypted
            response_event.set()
        except Exception:
            # Might be unencrypted or different format
            pass

    await cl.start_notify(N, h)

    encrypted = session.encrypt(cmd)
    await cl.write_gatt_char(W, encrypted, response=False)

    try:
        await asyncio.wait_for(response_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass

    await cl.stop_notify(N)
    return response_data[0]


async def main():
    # Find device
    print("Scanning...")
    for scan in range(5):
        found = await BleakScanner.discover(timeout=6, return_adv=True)
        t = None
        for a, (d, adv) in found.items():
            for c in adv.manufacturer_data:
                if c == 0x5D:
                    t = d
        if t:
            break
        await asyncio.sleep(3)
    if not t:
        print("Not found"); return

    # Connect
    cl = BleakClient(t, timeout=15.0)
    await cl.connect()
    print(f"Connected! MTU={cl.mtu_size}")

    # Handshake
    print("\nHandshake:")
    session = await do_handshake(cl)
    if not session:
        print("Handshake failed")
        await cl.disconnect()
        return

    # === Send encrypted commands ===
    print("\n=== Encrypted Commands ===\n")

    # Sync time
    now = int(time.time())
    sync_cmd = bytes([1, 4, 0]) + struct.pack("<I", now) + bytes([0, 0])
    resp = await send_command(cl, session, sync_cmd)
    if resp:
        print(f"Sync time: {resp.hex()[:40]}")
    else:
        print("Sync time: no response")

    # Get SSN (cmd=2)
    resp = await send_command(cl, session, bytes([1, 2, 0]))
    if resp:
        print(f"Get SSN: {resp.hex()[:60]}")
        # Try to extract SSN string
        if len(resp) > 3:
            try:
                ssn = resp[3:].decode("ascii", errors="replace").rstrip("\x00")
                print(f"  SSN: {ssn}")
            except:
                pass

    # Get State (cmd=3)
    resp = await send_command(cl, session, bytes([1, 3, 0]))
    if resp:
        print(f"Get State: {resp.hex()[:60]}")
        if len(resp) > 3:
            recording = resp[3] == 1 if len(resp) > 3 else False
            print(f"  Recording: {recording}")

    # Get Storage (cmd=6)
    resp = await send_command(cl, session, bytes([1, 6, 0]))
    if resp:
        print(f"Get Storage: {resp.hex()[:60]}")

    # Get File List (cmd=26)
    resp = await send_command(cl, session, bytes([1, 26, 0]), timeout=10)
    if resp:
        print(f"Get File List: {resp.hex()[:80]}")
        # Parse file list
        if len(resp) > 3:
            offset = 3
            files = []
            while offset + 10 <= len(resp):
                sid = struct.unpack_from("<I", resp, offset)[0]
                fsize = struct.unpack_from("<I", resp, offset + 4)[0]
                codec = resp[offset + 8] if offset + 9 <= len(resp) else 0
                chan = resp[offset + 9] if offset + 10 <= len(resp) else 1
                if sid == 0 and fsize == 0:
                    break
                dur_ms = (fsize / (max(chan, 1) * 80)) * 20
                dur_s = dur_ms / 1000
                files.append((sid, fsize, codec, chan, dur_s))
                offset += 10

            print(f"\n  Found {len(files)} recording(s):")
            for sid, fsize, codec, chan, dur in files:
                mins = int(dur) // 60
                secs = int(dur) % 60
                print(f"    Session {sid}: {fsize} bytes ({mins}m {secs}s)")

    # Battery
    try:
        bat = await cl.read_gatt_char("00002a19-0000-1000-8000-00805f9b34fb")
        print(f"\nBattery: {bat[0]}%")
    except:
        pass

    await cl.disconnect()
    print("\nDone!")


asyncio.run(main())
