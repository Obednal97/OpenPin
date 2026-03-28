#!/usr/bin/env python3
"""Download all Plaud cloud recordings and convert to playable audio.

Cloud opus files are raw Opus frames (80 bytes each, TOC=0xb8).
Wraps them in OggS container for playback with any audio player.
"""
import json, struct, os, sys, requests
from pathlib import Path
from datetime import datetime

API = "https://api-euc1.plaud.ai"
OUT_DIR = Path("cloud_recordings_decoded")

# OggS CRC table
CRC_LOOKUP = [0] * 256
for i in range(256):
    r = i << 24
    for _ in range(8):
        r = ((r << 1) ^ 0x04c11db7 if r & 0x80000000 else r << 1) & 0xFFFFFFFF
    CRC_LOOKUP[i] = r

def ogg_crc(data):
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ CRC_LOOKUP[((crc >> 24) & 0xFF) ^ b]) & 0xFFFFFFFF
    return crc

def wrap_opus_ogg(raw_data, frame_size=80, sample_rate=16000):
    """Wrap raw Opus frames in an OggS container."""
    frames = [raw_data[i:i+frame_size] for i in range(0, len(raw_data), frame_size)]
    serial = 1
    page_seq = [0]
    pages = bytearray()

    def make_page(segments, bos=False, eos=False, granule=0):
        flags = (0x02 if bos else 0) | (0x04 if eos else 0)
        seg_table = bytearray()
        for seg in segments:
            sz = len(seg)
            while sz >= 255:
                seg_table.append(255); sz -= 255
            seg_table.append(sz)
        body = b''.join(segments)
        hdr = bytearray(27)
        hdr[0:4] = b'OggS'; hdr[5] = flags
        struct.pack_into('<Q', hdr, 6, granule)
        struct.pack_into('<I', hdr, 14, serial)
        struct.pack_into('<I', hdr, 18, page_seq[0])
        hdr[26] = len(seg_table)
        page = bytes(hdr) + bytes(seg_table) + body
        crc = ogg_crc(page)
        page = page[:22] + struct.pack('<I', crc) + page[26:]
        page_seq[0] += 1
        return page

    # OpusHead
    head = bytearray(19)
    head[0:8] = b'OpusHead'; head[8] = 1; head[9] = 1
    struct.pack_into('<I', head, 12, sample_rate)
    pages.extend(make_page([bytes(head)], bos=True))

    # OpusTags
    vendor = b'plaud-decoder'
    tags = b'OpusTags' + struct.pack('<I', len(vendor)) + vendor + struct.pack('<I', 0)
    pages.extend(make_page([tags]))

    # Audio (960 samples per 20ms frame at 48kHz)
    granule = 0
    batch = 50
    for i in range(0, len(frames), batch):
        chunk = frames[i:i+batch]
        granule += len(chunk) * 960
        pages.extend(make_page(chunk, eos=(i+batch >= len(frames)), granule=granule))

    return bytes(pages)


def main():
    config = json.load(open(".plaud_config.json"))
    s = requests.Session()
    s.headers.update({
        "Authorization": f"bearer {config['token']}",
        "Content-Type": "application/json",
    })

    # Get all recordings
    r = s.get(f"{API}/file/simple/web", params={"limit": 9999, "sort_by": "start_time", "is_desc": "true"})
    data = r.json()
    files = data.get("data_file_list", data.get("data_list", []))
    print(f"Found {len(files)} cloud recordings\n")

    OUT_DIR.mkdir(exist_ok=True)
    downloaded = 0

    for i, f in enumerate(files):
        ts = f.get("start_time", 0)
        if ts > 1e12: ts = ts / 1000
        dt = datetime.fromtimestamp(ts) if ts > 0 else None
        dur = f.get("duration", 0)
        if dur > 10000: dur = dur / 1000
        name = f.get("filename", "unknown").replace("/", "-").replace(":", "-")
        fid = f.get("id", "")

        # Output filename
        date_str = dt.strftime("%Y-%m-%d_%H%M") if dt else "unknown"
        ogg_name = f"{date_str}_{name[:60]}.ogg"
        ogg_path = OUT_DIR / ogg_name

        if ogg_path.exists():
            continue

        # Skip very short recordings (< 5s)
        if dur < 5:
            continue

        print(f"[{i+1}/{len(files)}] {date_str} | {dur:.0f}s | {name[:50]}")

        # Get opus download URL
        try:
            r2 = s.get(f"{API}/file/temp-url/{fid}", params={"is_opus": 1})
            url = r2.json().get("temp_url_opus", "")
            if not url:
                print(f"  No opus URL, skipping")
                continue

            # Download raw opus
            r3 = requests.get(url, timeout=60)
            if r3.status_code != 200:
                print(f"  Download failed: {r3.status_code}")
                continue

            raw = r3.content
            if len(raw) < 160:
                print(f"  Too small ({len(raw)} bytes), skipping")
                continue

            # Check if it's raw Opus frames (TOC byte at every 80 bytes)
            if len(raw) % 80 == 0 and raw[0] == raw[80] if len(raw) > 80 else True:
                ogg = wrap_opus_ogg(raw)
                ogg_path.write_bytes(ogg)
                duration_s = (len(raw) // 80) * 0.02  # 20ms per frame
                print(f"  Saved: {ogg_path.name} ({len(ogg):,} bytes, {duration_s:.0f}s)")
                downloaded += 1
            else:
                # Maybe it's already a proper format (MP3/OGG)
                if raw[:3] == b'ID3' or raw[:4] == b'OggS':
                    ext = 'mp3' if raw[:3] == b'ID3' else 'ogg'
                    alt_path = OUT_DIR / f"{date_str}_{name[:60]}.{ext}"
                    alt_path.write_bytes(raw)
                    print(f"  Saved as {ext}: {alt_path.name}")
                    downloaded += 1
                else:
                    print(f"  Unknown format: {raw[:4].hex()}")

        except Exception as e:
            print(f"  Error: {e}")

    print(f"\nDone! Downloaded {downloaded} new recordings to {OUT_DIR}/")


if __name__ == "__main__":
    main()
