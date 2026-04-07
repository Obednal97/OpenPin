"""Audio processing utilities — OGG wrapping, BLE decryption."""

import struct
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

from app.utils.constants import CRC_LOOKUP


def _ogg_crc(data):
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ CRC_LOOKUP[((crc >> 24) & 0xFF) ^ b]) & 0xFFFFFFFF
    return crc


def _opus_packet_samples(packet):
    """Calculate the number of 48kHz samples in an Opus packet by parsing the TOC byte.

    Returns the granule increment for this packet at 48kHz.
    Handles single-frame, 2-frame, and multi-frame (code 3) packets.
    """
    if len(packet) < 1:
        return 960  # fallback: assume 20ms

    toc = packet[0]
    config = (toc >> 3) & 0x1F
    code = toc & 0x3

    # Frame duration in 48kHz samples based on config
    # Configs 0-3: SILK-only NB (10/20/40/60ms)
    # Configs 4-7: SILK-only MB (10/20/40/60ms)
    # Configs 8-11: SILK-only WB (10/20/40/60ms)
    # Configs 12-13: Hybrid SWB (10/20ms)
    # Configs 14-15: Hybrid FB (10/20ms)
    # Configs 16-19: CELT-only NB (2.5/5/10/20ms)
    # Configs 20-23: CELT-only WB (2.5/5/10/20ms)
    # Configs 24-27: CELT-only SWB (2.5/5/10/20ms)
    # Configs 28-31: CELT-only FB (2.5/5/10/20ms)
    _frame_samples = {
        0: 480, 1: 960, 2: 1920, 3: 2880,      # SILK NB
        4: 480, 5: 960, 6: 1920, 7: 2880,      # SILK MB
        8: 480, 9: 960, 10: 1920, 11: 2880,    # SILK WB
        12: 480, 13: 960,                        # Hybrid SWB
        14: 480, 15: 960,                        # Hybrid FB
        16: 120, 17: 240, 18: 480, 19: 960,    # CELT NB
        20: 120, 21: 240, 22: 480, 23: 960,    # CELT WB
        24: 120, 25: 240, 26: 480, 27: 960,    # CELT SWB
        28: 120, 29: 240, 30: 480, 31: 960,    # CELT FB
    }
    samples_per_frame = _frame_samples.get(config, 960)

    if code == 0:
        return samples_per_frame
    elif code == 1:
        return samples_per_frame * 2
    elif code == 2:
        return samples_per_frame * 2
    elif code == 3:
        if len(packet) < 2:
            return samples_per_frame
        M = packet[1] & 0x3F
        return samples_per_frame * max(M, 1)

    return 960


def wrap_opus_ogg(raw_data, frame_size=80):
    """Wrap raw Opus frames into an OGG container."""
    frames = [raw_data[i : i + frame_size] for i in range(0, len(raw_data), frame_size)]
    serial = 1
    page_seq = [0]
    pages = bytearray()

    def make_page(segments, bos=False, eos=False, granule=0):
        flags = (0x02 if bos else 0) | (0x04 if eos else 0)
        seg_table = bytearray()
        for seg in segments:
            sz = len(seg)
            while sz >= 255:
                seg_table.append(255)
                sz -= 255
            seg_table.append(sz)
        body = b"".join(segments)
        hdr = bytearray(27)
        hdr[0:4] = b"OggS"
        hdr[5] = flags
        struct.pack_into("<Q", hdr, 6, granule)
        struct.pack_into("<I", hdr, 14, serial)
        struct.pack_into("<I", hdr, 18, page_seq[0])
        hdr[26] = len(seg_table)
        page = bytes(hdr) + bytes(seg_table) + body
        crc = _ogg_crc(page)
        page = page[:22] + struct.pack("<I", crc) + page[26:]
        page_seq[0] += 1
        return page

    head = bytearray(19)
    head[0:8] = b"OpusHead"
    head[8] = 1
    head[9] = 1
    struct.pack_into("<I", head, 12, 48000)  # Output rate always 48kHz per Opus spec
    pages.extend(make_page([bytes(head)], bos=True))
    vendor = b"plaud-v2"
    tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
    pages.extend(make_page([tags]))
    granule = 0
    batch = 50
    for i in range(0, len(frames), batch):
        chunk = frames[i : i + batch]
        granule += len(chunk) * 960  # Each frame = 20ms = 960 samples at 48kHz
        pages.extend(make_page(chunk, eos=(i + batch >= len(frames)), granule=granule))
    return bytes(pages)


def decrypt_ble_file(data, app_key):
    """Decrypt a BLE-downloaded ASR file -> raw Opus frames."""
    if data[:8] != b"PLAUD.AI" or len(data) < 0x300:
        return None
    nonce = data[0x84:0x90]
    rsa_blob = data[0x100:0x200]
    try:
        file_key = app_key.decrypt(rsa_blob, padding.PKCS1v15())
    except Exception:
        return None
    if len(file_key) > 32:
        file_key = file_key[:32]
    if len(file_key) != 32:
        return None
    full_nonce = b"\x00\x00\x00\x00" + nonce
    audio = data[0x200:]

    # Try repeating-keystream first (works for most recordings)
    cipher = Cipher(algorithms.ChaCha20(file_key, full_nonce), mode=None)
    keystream_80 = cipher.decryptor().update(b"\x00" * 80)
    decrypted_repeat = bytearray()
    for i in range(0, len(audio), 80):
        frame = audio[i : i + 80]
        decrypted_repeat.extend(bytes(a ^ b for a, b in zip(frame, keystream_80[: len(frame)], strict=False)))

    if len(decrypted_repeat) >= 160:
        toc = decrypted_repeat[0]
        # Check that first two frames have the same TOC — confirms correct decrypt
        if toc == decrypted_repeat[80] and (toc >> 3) & 0x1F in range(16, 32):
            # Also verify a few more frames to catch longer recordings
            if len(decrypted_repeat) < 800 or decrypted_repeat[640] == toc:
                return bytes(decrypted_repeat)

    # Repeating keystream failed — try continuous keystream
    cipher2 = Cipher(algorithms.ChaCha20(file_key, full_nonce), mode=None)
    full_keystream = cipher2.decryptor().update(b"\x00" * len(audio))
    decrypted_continuous = bytes(a ^ b for a, b in zip(audio, full_keystream))

    if len(decrypted_continuous) >= 160:
        toc = decrypted_continuous[0]
        if toc == decrypted_continuous[80] and (toc >> 3) & 0x1F in range(16, 32):
            return bytes(decrypted_continuous)

    # Fall back to repeating keystream even if TOC check failed
    return bytes(decrypted_repeat)


def unwrap_ogg_to_raw_opus(ogg_path):
    """Extract raw Opus frames from an OGG container.
    The Plaud cloud expects raw Opus frames (80 bytes each), not OGG-wrapped files.
    """
    data = Path(ogg_path).read_bytes()
    if data[:4] != b"OggS":
        return data  # Already raw, not OGG

    frames = bytearray()
    pos = 0
    page_num = 0
    while pos < len(data):
        if data[pos : pos + 4] != b"OggS":
            break
        num_segments = data[pos + 26] if pos + 26 < len(data) else 0
        seg_table_start = pos + 27
        seg_table = data[seg_table_start : seg_table_start + num_segments]
        body_start = seg_table_start + num_segments

        if page_num >= 2:
            offset = body_start
            for seg_size in seg_table:
                if seg_size > 0 and offset + seg_size <= len(data):
                    frames.extend(data[offset : offset + seg_size])
                offset += seg_size

        body_size = sum(seg_table)
        pos = body_start + body_size
        page_num += 1

    return bytes(frames) if frames else data
