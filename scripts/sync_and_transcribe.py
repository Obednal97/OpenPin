#!/usr/bin/env python3
"""Plaud NotePin S — Sync, Decrypt & Transcribe.

Connect to device via BLE, download new recordings, decrypt audio,
and transcribe with Whisper. Fully offline — no cloud needed.

Usage:
    python3 sync_and_transcribe.py                    # sync + transcribe
    python3 sync_and_transcribe.py --model medium      # use medium model this run
    python3 sync_and_transcribe.py --set-model large   # save 'large' as your default
    python3 sync_and_transcribe.py --no-transcribe     # download + decrypt only
    python3 sync_and_transcribe.py --record 10         # record 10s first, then sync
    python3 sync_and_transcribe.py --language en        # force English
"""
import asyncio, struct, base64, json, time, sys, argparse
from pathlib import Path
from datetime import datetime
from bleak import BleakScanner, BleakClient
from src.crypto import get_public_key_full_pem, rsa_decrypt, load_rsa_private_key
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

SN_SIG = "<REDACTED_SN_SIGNATURE>"
BIND_TOKEN = "<REDACTED_JWT>"
W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"

RECORDINGS_DIR = Path("recordings")
TRANSCRIPTS_DIR = Path("transcripts")
SESSIONS_DIR = Path("sessions")
APP_KEY_PATH = Path("keys/app_private.pem")
CONFIG_PATH = Path(".plaud_v2_config.json")

WHISPER_MODELS = {
    "base":   {"ram_gb": 1,  "size": "74 MB",  "speed": "Fast",      "accuracy": "Good enough for clear speech"},
    "small":  {"ram_gb": 2,  "size": "244 MB", "speed": "Moderate",  "accuracy": "Good — recommended default"},
    "medium": {"ram_gb": 5,  "size": "769 MB", "speed": "Slower",    "accuracy": "Very good — handles accents and noise well"},
    "large":  {"ram_gb": 10, "size": "1.5 GB", "speed": "Slowest",   "accuracy": "Best available — use for important recordings"},
}
DEFAULT_MODEL = "small"
RAM_THRESHOLD = 0.85  # Don't offer models that would push total usage above 85%

def get_ram_info():
    """Get total and used RAM in GB. macOS only, no external dependencies."""
    import subprocess
    total_bytes = int(subprocess.run(
        ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True
    ).stdout.strip())
    # Parse vm_stat for active + wired + speculative pages
    r = subprocess.run(["vm_stat"], capture_output=True, text=True)
    stats = {}
    for line in r.stdout.strip().split("\n")[1:]:
        if ":" in line:
            key, val = line.split(":")
            try: stats[key.strip()] = int(val.strip().rstrip("."))
            except ValueError: pass
    page_size = 16384  # macOS Apple Silicon default
    used_bytes = (stats.get("Pages active", 0) + stats.get("Pages wired down", 0)
                  + stats.get("Pages speculative", 0)) * page_size
    return total_bytes / 1024**3, used_bytes / 1024**3

def get_available_models():
    """Return dict of models that fit within the RAM threshold."""
    total_gb, used_gb = get_ram_info()
    headroom_gb = (total_gb * RAM_THRESHOLD) - used_gb
    available = {}
    for name, info in WHISPER_MODELS.items():
        fits = info["ram_gb"] <= headroom_gb
        available[name] = {**info, "fits": fits, "headroom_gb": headroom_gb}
    return available, total_gb, used_gb

def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}

def save_config(config):
    CONFIG_PATH.write_text(json.dumps(config, indent=2))

def get_default_model():
    return load_config().get("whisper_model", DEFAULT_MODEL)

def set_default_model(model):
    config = load_config()
    config["whisper_model"] = model
    save_config(config)

def print_model_table():
    available, total_gb, used_gb = get_available_models()
    headroom = total_gb * RAM_THRESHOLD - used_gb
    print(f"\n  System: {total_gb:.0f} GB RAM, {used_gb:.1f} GB in use, {headroom:.1f} GB available for Whisper")
    print()
    print(f"  {'Model':<8} {'Download':<10} {'Needs':<9} {'Speed':<12} {'Accuracy'}")
    print(f"  {'─'*8} {'─'*10} {'─'*9} {'─'*12} {'─'*50}")
    current = get_default_model()
    for name, info in available.items():
        ram_str = f"~{info['ram_gb']} GB"
        if info["fits"]:
            marker = " << current default" if name == current else ""
            print(f"  {name:<8} {info['size']:<10} {ram_str:<9} {info['speed']:<12} {info['accuracy']}{marker}")
        else:
            print(f"  {name:<8} {info['size']:<10} {ram_str:<9} {'─':<12} Unavailable (would exceed {RAM_THRESHOLD:.0%} RAM)")
    print()

# OggS wrapper for raw Opus frames
CRC_LOOKUP = [0] * 256
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        _r = ((_r << 1) ^ 0x04c11db7 if _r & 0x80000000 else _r << 1) & 0xFFFFFFFF
    CRC_LOOKUP[_i] = _r

def _ogg_crc(data):
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ CRC_LOOKUP[((crc >> 24) & 0xFF) ^ b]) & 0xFFFFFFFF
    return crc

def wrap_opus_ogg(raw_data, frame_size=80):
    frames = [raw_data[i:i+frame_size] for i in range(0, len(raw_data), frame_size)]
    serial = 1; page_seq = [0]; pages = bytearray()
    def make_page(segments, bos=False, eos=False, granule=0):
        flags = (0x02 if bos else 0) | (0x04 if eos else 0)
        seg_table = bytearray()
        for seg in segments:
            sz = len(seg)
            while sz >= 255: seg_table.append(255); sz -= 255
            seg_table.append(sz)
        body = b''.join(segments)
        hdr = bytearray(27)
        hdr[0:4] = b'OggS'; hdr[5] = flags
        struct.pack_into('<Q', hdr, 6, granule)
        struct.pack_into('<I', hdr, 14, serial)
        struct.pack_into('<I', hdr, 18, page_seq[0])
        hdr[26] = len(seg_table)
        page = bytes(hdr) + bytes(seg_table) + body
        crc = _ogg_crc(page)
        page = page[:22] + struct.pack('<I', crc) + page[26:]
        page_seq[0] += 1
        return page
    head = bytearray(19)
    head[0:8] = b'OpusHead'; head[8] = 1; head[9] = 1
    struct.pack_into('<I', head, 12, 16000)
    pages.extend(make_page([bytes(head)], bos=True))
    vendor = b'plaud-v2'
    tags = b'OpusTags' + struct.pack('<I', len(vendor)) + vendor + struct.pack('<I', 0)
    pages.extend(make_page([tags]))
    granule = 0; batch = 50
    for i in range(0, len(frames), batch):
        chunk = frames[i:i+batch]
        granule += len(chunk) * 960
        pages.extend(make_page(chunk, eos=(i+batch >= len(frames)), granule=granule))
    return bytes(pages)


def decrypt_ble_file(data, app_key):
    """Decrypt a BLE-downloaded ASR file → raw Opus frames."""
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

    # Generate 80-byte keystream (counter=0)
    full_nonce = b'\x00\x00\x00\x00' + nonce
    cipher = Cipher(algorithms.ChaCha20(file_key, full_nonce), mode=None)
    keystream = cipher.decryptor().update(b'\x00' * 80)

    # Decrypt each 80-byte frame with the same keystream
    audio = data[0x200:]
    decrypted = bytearray()
    for i in range(0, len(audio), 80):
        frame = audio[i:i+80]
        decrypted.extend(bytes(a ^ b for a, b in zip(frame, keystream[:len(frame)])))

    # Verify TOC consistency
    if len(decrypted) >= 160:
        toc = decrypted[0]
        if decrypted[80] != toc:
            return None  # Decryption failed

    return bytes(decrypted)


async def ble_handshake(cl):
    """RSA handshake → session keys."""
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
    return key, nonce, aad


async def main():
    parser = argparse.ArgumentParser(description="Plaud NotePin S — Sync, Decrypt & Transcribe")
    parser.add_argument("--record", type=int, default=0, help="Record N seconds first (0=skip)")
    parser.add_argument("--model", default=None, choices=list(WHISPER_MODELS.keys()),
                        help="Whisper model for this run (overrides saved default)")
    parser.add_argument("--set-model", choices=list(WHISPER_MODELS.keys()), metavar="MODEL",
                        help="Save a new default Whisper model (base/small/medium/large)")
    parser.add_argument("--list-models", action="store_true", help="Show available models and exit")
    parser.add_argument("--language", default=None, help="Force language (e.g. en)")
    parser.add_argument("--diarize", action="store_true", help="Identify speakers (Speaker 1, Speaker 2, etc.)")
    parser.add_argument("--setup-diarize", action="store_true", help="Set up speaker diarization (HuggingFace token)")
    parser.add_argument("--no-transcribe", action="store_true", help="Skip transcription")
    args = parser.parse_args()

    # Handle --setup-diarize
    if args.setup_diarize:
        from src.transcribe import setup_diarization
        setup_diarization()
        return

    # Handle --list-models
    if args.list_models:
        print_model_table()
        return

    # Handle --set-model
    if args.set_model:
        available, _, _ = get_available_models()
        if not available[args.set_model]["fits"]:
            print(f"  Cannot set '{args.set_model}' as default — needs ~{WHISPER_MODELS[args.set_model]['ram_gb']} GB but only {available[args.set_model]['headroom_gb']:.1f} GB headroom.")
            print(f"  Close some applications or choose a smaller model.")
            print_model_table()
            return
        set_default_model(args.set_model)
        info = WHISPER_MODELS[args.set_model]
        print(f"  Default model set to '{args.set_model}' ({info['accuracy']})")
        print(f"  Download: {info['size']}, RAM: ~{info['ram_gb']} GB, Speed: {info['speed']}")
        if not args.record and not any(a for a in sys.argv[1:] if a not in ('--set-model', args.set_model)):
            return

    # Resolve which model to use: --model flag > saved config > DEFAULT_MODEL
    model = args.model or get_default_model()
    model_info = WHISPER_MODELS[model]

    # Check RAM availability for the selected model
    if not args.no_transcribe:
        available, total_gb, used_gb = get_available_models()
        if not available[model]["fits"]:
            # Fall back to the largest model that fits
            fallback = None
            for name in reversed(list(WHISPER_MODELS.keys())):
                if available[name]["fits"]:
                    fallback = name
                    break
            if fallback:
                headroom = available[model]["headroom_gb"]
                print(f"  '{model}' needs ~{model_info['ram_gb']} GB but only {headroom:.1f} GB headroom — using '{fallback}' instead.")
                model = fallback
                model_info = WHISPER_MODELS[model]
            else:
                print(f"  Not enough RAM for any Whisper model ({used_gb:.1f}/{total_gb:.0f} GB used). Skipping transcription.")
                args.no_transcribe = True

    print("=" * 60)
    print("  PLAUD V2 — Sync, Decrypt & Transcribe")
    print("=" * 60)
    if not args.no_transcribe:
        print(f"  Whisper model: {model} ({model_info['accuracy']})")

    # Load app RSA key for audio decryption
    if not APP_KEY_PATH.exists():
        print(f"  Missing {APP_KEY_PATH} — cannot decrypt audio")
        return
    app_rsa_key = load_pem_private_key(APP_KEY_PATH.read_bytes(), password=None)

    # === BLE CONNECT ===
    print("\n[1/6] Scanning for device...")
    target = None
    for attempt in range(10):
        found = await BleakScanner.discover(timeout=5, return_adv=True)
        for a, (d, adv) in found.items():
            if 0x5D in adv.manufacturer_data: target = d
        if target: break
        print(f"  scan {attempt + 1}...")
        await asyncio.sleep(3)
    if not target:
        print("  Device not found."); return

    cl = BleakClient(target, timeout=30.0)
    await cl.connect()
    print(f"  Connected (MTU={cl.mtu_size})")

    # === HANDSHAKE ===
    print("\n[2/6] Handshake...")
    keys = await ble_handshake(cl)
    if not keys:
        print("  Failed!"); await cl.disconnect(); return
    key, nonce, aad = keys
    print("  PLAUD.AI verified!")

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
        return cmd_resp[0]

    token32 = BIND_TOKEN[:32].encode("ascii")
    await cmd("HS1", bytes([1, 1, 0, 2, 0, 0]) + token32)
    await cmd("HS2", bytes([1, 1, 0, 2, 0, 1]) + token32)
    await cmd("Sync", bytes([1, 4, 0]) + struct.pack("<I", int(time.time())) + bytes([0, 0]))

    # === OPTIONAL RECORDING ===
    if args.record > 0:
        print(f"\n[3/6] Recording ({args.record}s) — speak now!")
        await cmd("Start", bytes([1, 20, 0]))
        await asyncio.sleep(args.record)
        await cmd("Stop", bytes([1, 23, 0]))
        await asyncio.sleep(2)
    else:
        print(f"\n[3/6] Skipping recording")

    # === LIST FILES ===
    print(f"\n[4/6] Listing files on device...")
    r = await cmd("Files", bytes([1, 26, 0]), to=10)
    files = []
    if r and len(r) >= 11:
        total = struct.unpack_from("<H", r, 7)[0]
        # Entries start at offset 11: type(1) + cmd_id(2) + status(4) + total(2) + padding(2)
        # Each entry: sessionId(4) + fileSize(4) + flags(2) = 10 bytes
        offset = 11
        while offset + 10 <= len(r):
            sid = struct.unpack_from("<I", r, offset)[0]
            fsize = struct.unpack_from("<I", r, offset + 4)[0]
            if 0 < fsize < 100_000_000:  # sanity check: skip if > 100MB
                files.append((sid, fsize))
            offset += 10
        print(f"  {total} file(s) on device, {len(files)} in this page")
    else:
        print(f"  No files or no response")

    # === DOWNLOAD & DECRYPT ===
    RECORDINGS_DIR.mkdir(exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    new_ogg_files = []

    print(f"\n[5/6] Downloading & decrypting...")
    for i, (sid, fsize) in enumerate(files):
        ogg_path = RECORDINGS_DIR / f"{sid}.ogg"
        if ogg_path.exists():
            print(f"  [{i+1}/{len(files)}] {sid}: already exists, skipping")
            continue

        duration_est = (fsize - 512) / 80 * 0.02 if fsize > 512 else 0
        print(f"  [{i+1}/{len(files)}] Session {sid}: {fsize:,}B (~{duration_est:.0f}s)")

        # Download via BLE
        file_data.clear(); file_done.clear()
        sync_cmd = bytes([1, 28, 0]) + struct.pack("<I", sid) + struct.pack("<I", 0) + struct.pack("<I", fsize)
        await cl.write_gatt_char(W, enc(sync_cmd), response=False)
        start = time.time()
        max_time = max(fsize / 3000, 30)  # ~33KB/s BLE speed, min 30s
        while not file_done.is_set():
            try: await asyncio.wait_for(file_done.wait(), timeout=2)
            except:
                if time.time() - start > max_time: break
                pct = len(file_data) * 100 // max(fsize, 1)
                print(f"\r    Downloading: {len(file_data):,}/{fsize:,} ({pct}%)    ", end="", flush=True)
        elapsed = time.time() - start
        speed = len(file_data) / max(elapsed, 0.1) / 1024
        print(f"\r    Downloaded: {len(file_data):,}B in {elapsed:.1f}s ({speed:.1f} KB/s)    ")

        if not file_data:
            print(f"    No data received, skipping")
            continue

        raw_data = bytes(file_data)

        # Decrypt
        opus_frames = decrypt_ble_file(raw_data, app_rsa_key)
        if opus_frames:
            ogg = wrap_opus_ogg(opus_frames)
            ogg_path.write_bytes(ogg)
            duration = len(opus_frames) // 80 * 0.02
            print(f"    Decrypted: {ogg_path.name} ({duration:.1f}s)")
            new_ogg_files.append(ogg_path)
        else:
            # Save raw for debugging
            raw_path = RECORDINGS_DIR / f"{sid}_encrypted.bin"
            raw_path.write_bytes(raw_data)
            print(f"    Decryption failed — saved raw: {raw_path.name}")

    # === TRANSCRIBE ===
    if new_ogg_files and not args.no_transcribe:
        print(f"\n[6/6] Transcribing {len(new_ogg_files)} recording(s)...")
        from src.transcribe import transcribe

        for ogg_path in new_ogg_files:
            transcript_path = TRANSCRIPTS_DIR / f"{ogg_path.stem}.json"
            txt_path = TRANSCRIPTS_DIR / f"{ogg_path.stem}.txt"

            if transcript_path.exists():
                print(f"  {ogg_path.stem}: already transcribed")
                continue

            print(f"\n  Transcribing {ogg_path.name}...")
            t = transcribe(str(ogg_path), model=model, language=args.language, diarize=args.diarize)

            if t.text:
                t.save(str(transcript_path))
                txt_path.write_text(t.formatted, encoding="utf-8")
                print(f"  Saved: {transcript_path.name}")
                print(f"\n  --- Transcript ---")
                print(f"  {t.text[:500]}")
                if len(t.text) > 500:
                    print(f"  ... ({len(t.text)} chars total)")
            else:
                print(f"  No speech detected")
    elif args.no_transcribe:
        print(f"\n[6/6] Skipping transcription (--no-transcribe)")
    else:
        print(f"\n[6/6] No new recordings to transcribe")

    # === CLEANUP ===
    try:
        bat = await cl.read_gatt_char("00002a19-0000-1000-8000-00805f9b34fb")
        print(f"\n  Battery: {bat[0]}%")
    except: pass

    try:
        await cl.stop_notify(N)
        await cl.disconnect()
    except: pass

    print(f"\n{'=' * 60}")
    print(f"  Done. Device stays paired.")
    if new_ogg_files:
        print(f"  {len(new_ogg_files)} recording(s) in {RECORDINGS_DIR}/")
        if not args.no_transcribe:
            print(f"  Transcripts in {TRANSCRIPTS_DIR}/")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
