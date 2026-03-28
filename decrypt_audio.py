#!/usr/bin/env python3
"""Attempt to decrypt Plaud BLE-downloaded audio files.

BLE file format (from reverse engineering):
  [0x00] "PLAUD.AI" magic (8 bytes)
  [0x08] Version (4 bytes)
  [0x0C] Size field (4 bytes)
  [0x10] User ID (32 bytes)
  [0x30] Audio params (16 bytes)
  [0x40] Padding (68 bytes)
  [0x84] 12-byte nonce
  [0x90] Frame size? (0x50 = 80)
  [0x100] Encrypted audio data (in stored format: RSA key here, but BLE skips it)

Encryption: ChaCha20-Poly1305, per-file key
"""
import sys, struct
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
import hashlib

# Test files
FILES = sorted(Path("recordings").glob("ble_*.bin"))
if not FILES:
    FILES = sorted(Path("recordings").glob("ble_*.opus"))

# Candidate keys
HARDCODED_KEY = b"plaud2023_log_chacha20_key_32bit"  # 32 bytes ASCII from libapp.so
HARDCODED_KEY_HEX = bytes.fromhex("706c617564323032335f6c6f675f636861636861")[:32]  # truncated hex

def parse_header(data):
    """Parse BLE file header."""
    magic = data[0:8]
    version = struct.unpack_from("<I", data, 8)[0]
    size = struct.unpack_from("<I", data, 12)[0]
    user_id = data[0x10:0x30].rstrip(b'\x00').decode('ascii', errors='replace')
    audio_params = data[0x30:0x40]
    nonce = data[0x84:0x90]  # 12-byte nonce
    frame_size_byte = data[0x90]

    print(f"  Magic: {magic}")
    print(f"  Version: {version:#010x}")
    print(f"  Size field: {size} (0x{size:04x})")
    print(f"  User ID: {user_id}")
    print(f"  Audio params: {audio_params[:8].hex()}")
    print(f"  Nonce (0x84): {nonce.hex()}")
    print(f"  Byte at 0x90: 0x{frame_size_byte:02x} ({frame_size_byte})")

    return {
        'nonce': nonce,
        'size': size,
        'user_id': user_id,
    }

def try_decrypt_aead(key, nonce, ciphertext, aad=b"", label=""):
    """Try ChaCha20-Poly1305 AEAD decryption."""
    try:
        cipher = ChaCha20Poly1305(key)
        pt = cipher.decrypt(nonce, ciphertext, aad)
        print(f"  {label}: DECRYPTED! {len(pt)} bytes, first={pt[:16].hex()}")
        return pt
    except Exception as e:
        print(f"  {label}: failed ({str(e)[:60]})")
        return None

def try_decrypt_stream(key, nonce, ciphertext, label=""):
    """Try ChaCha20 stream cipher (no auth tag)."""
    try:
        # ChaCha20 needs 16-byte nonce (4-byte counter + 12-byte nonce)
        full_nonce = b'\x00' * 4 + nonce  # counter=0
        cipher = Cipher(algorithms.ChaCha20(key, full_nonce), mode=None)
        dec = cipher.decryptor()
        pt = dec.update(ciphertext) + dec.finalize()
        # Check if result looks like Opus
        if pt[:4] == b'OpuS' or pt[:4] == b'OggS':
            print(f"  {label}: OPUS HEADER FOUND! {pt[:16].hex()}")
            return pt
        # Check for any recognizable audio pattern
        if any(pt[i:i+4] in [b'OpuS', b'OggS', b'RIFF', b'fLaC'] for i in range(0, min(len(pt), 100), 1)):
            print(f"  {label}: Audio magic found! {pt[:32].hex()}")
            return pt
        print(f"  {label}: decrypted but no audio magic. first={pt[:16].hex()}")
        return pt
    except Exception as e:
        print(f"  {label}: failed ({str(e)[:60]})")
        return None


def main():
    for fp in FILES[:3]:
        data = fp.read_bytes()
        print(f"\n{'='*60}")
        print(f"File: {fp.name} ({len(data)} bytes)")
        print(f"{'='*60}")

        if data[:8] != b"PLAUD.AI":
            print(f"  Not a PLAUD file, skipping")
            continue

        hdr = parse_header(data)
        nonce = hdr['nonce']

        # Try different data offsets
        for data_start in [0x84, 0x90, 0x100]:
            audio_data = data[data_start:]
            remaining = len(audio_data)
            print(f"\n  --- Data from offset 0x{data_start:02x} ({remaining} bytes) ---")

            # Try frame-by-frame (96 bytes = 80 audio + 16 MAC)
            if data_start == 0x90 or data_start == 0x100:
                frame = audio_data[:96]

                # Try with hardcoded key
                for key, kname in [
                    (HARDCODED_KEY, "hardcoded_ascii"),
                    (hashlib.sha256(HARDCODED_KEY).digest(), "sha256(hardcoded)"),
                    (hashlib.md5(HARDCODED_KEY).digest() * 2, "md5(hardcoded)*2"),
                ]:
                    # AEAD: various AAD options
                    for aad, aname in [
                        (b"", "aad=empty"),
                        (nonce, "aad=nonce"),
                        (b"PLAUD.AI", "aad=magic"),
                        (data[0x10:0x30], "aad=userid"),
                        (data[0:0x84], "aad=header"),
                    ]:
                        try_decrypt_aead(key, nonce, frame, aad, f"{kname} frame96 {aname}")

                    # Try whole blob as one AEAD ciphertext
                    try_decrypt_aead(key, nonce, audio_data, b"", f"{kname} whole {aname}")

            # Try as stream cipher (no per-frame MAC)
            if data_start == 0x90 or data_start == 0x100:
                for key, kname in [
                    (HARDCODED_KEY, "hardcoded_ascii"),
                    (hashlib.sha256(HARDCODED_KEY).digest(), "sha256(hardcoded)"),
                ]:
                    result = try_decrypt_stream(key, nonce, audio_data[:1024], f"stream {kname}")

            # If nonce at 0x84, try data starting right after nonce
            if data_start == 0x84:
                # The "nonce" bytes are already extracted, data after nonce:
                after_nonce = data[0x90:]
                print(f"  After-nonce data: {after_nonce[:16].hex()}")

        # Also check: maybe the nonce isn't at 0x84, maybe 0x84 is first frame
        # and the nonce comes from the BLE session
        print(f"\n  --- Checking if 0x84+ is direct encrypted data (no embedded nonce) ---")
        audio_from_84 = data[0x84:]
        print(f"  Data from 0x84: {audio_from_84[:32].hex()}")
        print(f"  Data length from 0x84: {len(audio_from_84)}")
        print(f"  Frames of 96: {len(audio_from_84) / 96:.2f}")
        print(f"  Frames of 80: {len(audio_from_84) / 80:.2f}")

        # Try with zeros nonce / BLE session nonce
        zero_nonce = b'\x00' * 12
        for key, kname in [
            (HARDCODED_KEY, "hardcoded"),
            (hashlib.sha256(HARDCODED_KEY).digest(), "sha256(hk)"),
        ]:
            try_decrypt_aead(key, zero_nonce, audio_from_84[:96], b"", f"{kname} zero_nonce frame96")
            try_decrypt_stream(key, zero_nonce, audio_from_84[:1024], f"stream {kname} zero_nonce")
            try_decrypt_stream(key, nonce, audio_from_84[:1024], f"stream {kname} file_nonce")

    # Also check cloud recordings for comparison
    cloud_dir = Path("cloud_recordings")
    if cloud_dir.exists():
        cloud_files = sorted(cloud_dir.glob("*.opus"))[:1]
        if not cloud_files:
            cloud_files = sorted(cloud_dir.glob("*.wav"))[:1]
        for cf in cloud_files:
            cdata = cf.read_bytes()
            print(f"\n  Cloud file: {cf.name}")
            print(f"  Header: {cdata[:16].hex()}")
            if cdata[:8] == b"PLAUD.AI":
                print(f"  This is also a PLAUD encrypted file!")
            elif cdata[:4] == b"OggS":
                print(f"  This is a standard Ogg/Opus file (already decrypted)")
            elif cdata[:4] == b"RIFF":
                print(f"  This is a standard WAV file (already decrypted)")
            else:
                print(f"  Unknown format: {cdata[:8]}")

if __name__ == "__main__":
    main()
