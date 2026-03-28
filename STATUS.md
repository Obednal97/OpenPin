# Plaud NotePin S — Project Status

> Last updated: 2026-03-27 (evening)

## What We Built

### Fully Working
| Feature | How | Status |
|---------|-----|--------|
| BLE device scanning | `bleak` library, company_id=0x5D | ✅ |
| BLE connection | GATT connect, MTU=247 | ✅ |
| RSA handshake | SN sig (65040) → ACK → Full PEM pubkey (65042) → RSA blob | ✅ |
| ChaCha20-Poly1305 validation | RSA decrypt → key/nonce/aad → decrypt "PLAUD.AI" | ✅ |
| Encrypted command channel | All commands encrypted with session ChaCha20 key | ✅ |
| Start/stop recording | cmd_id=20 (start), cmd_id=23 (stop) | ✅ |
| List files on device | cmd_id=26, returns session_id + file_size per file | ✅ |
| Download files via BLE | cmd_id=28 (sync file start), ~33 KB/s | ✅ |
| **Offline audio decryption** | **RSA file key + per-frame ChaCha20 XOR** | **✅ NEW** |
| Device settings | cmd_id=8 (passthrough), e.g. raw wave mode | ✅ |
| Depair/unbind | cmd_id=5 | ✅ |
| Persistent pairing | Disconnect without depair, reconnect works | ✅ |
| Battery level | GATT read 0x2A19 | ✅ |
| Session key logging | Every session saved to sessions/ with timestamp | ✅ |
| Cloud API download | 57 recordings as MP3/Opus via api-euc1.plaud.ai | ✅ |
| Cloud Opus → OGG conversion | Raw Opus frames wrapped in OggS container | ✅ |
| Local transcription | OpenAI Whisper (base/medium/large models) | ✅ |
| Summarization pipeline | Ollama/OpenAI/Anthropic ready | ✅ |
| WiFi hotspot connection | BLE cmd_id=10 opens hotspot, Mac connects via networksetup | ✅ |
| WiFi WebSocket handshake | Device connects to our WS server on port 8081 | ✅ partial |

### Remaining
| Feature | Blocker | Notes |
|---------|---------|-------|
| WiFi file transfer | Handshake protocol not accepted | Device sends cmd=2 but rejects our cmd=16 response |
| WiFi restore | networksetup fails with -3900 | Power-cycle (off 3s, on 15s) works but slow |
| Background auto-sync | Not built yet | Should detect device and sync automatically |
| E2EE-encrypted files | Need user's E2EE RSA key if E2EE enabled | Only affects users who set up E2EE with PIN |

## Audio Decryption — SOLVED

### The Breakthrough

Audio files from the Plaud NotePin S are encrypted with a **static RSA key pair** embedded in every copy of the APK — NOT true E2EE. The "E2EE" feature (with user PIN) is an optional additional layer that most users may not have enabled.

### BLE File Format (confirmed)
```
[0x00-0x07]  "PLAUD.AI" magic (8 bytes)
[0x08-0x0B]  Version: 01 00 00 02
[0x0C-0x0F]  Size field (u32 LE)
[0x10-0x2F]  User ID (32 bytes, ASCII padded)
[0x30-0x3F]  Audio params: codec=5 (Opus), channels=1
[0x40-0x83]  Reserved/zeros (68 bytes)
[0x84-0x8F]  12-byte ChaCha20 nonce (unique per file)
[0x90]       Frame size: 0x50 (80 bytes)
[0x91-0xFF]  Reserved/zeros
[0x100-0x1FF] 256-byte RSA-encrypted per-file key
[0x200+]     Encrypted Opus frames (80 bytes each, no auth tag)
```

### Decryption Steps
```
1. Read 256-byte RSA blob at offset 0x100
2. RSA-decrypt with keys/app_private.pem (PKCS1v15) → 32-byte ChaCha20 file key
3. Read 12-byte nonce from offset 0x84
4. Generate 80 bytes of ChaCha20 keystream: ChaCha20(file_key, counter=0 || nonce)
5. XOR every 80-byte frame with the SAME 80-byte keystream
   (per-frame independent encryption, NOT continuous stream)
6. Result: raw Opus frames, TOC byte 0xb8 (CELT 48kHz 20ms mono)
7. Wrap in OggS container → playable audio file
```

### Why This Works for All Users
- The `app_private.pem` key is from the APK's assets — identical for every installation
- No cloud account, internet, or device modification needed
- Works on any Plaud NotePin S out of the box
- Users who enabled E2EE (optional PIN-based) may need additional handling

## Architecture

### Current Connection Flow
```
1. Scan for BLE device (company_id=0x5D)
2. Connect (GATT, MTU=247)
3. SN Signature (cmd 65040, 3 chunks, sent twice)
4. Wait for 65041 ACK
5. RSA Public Key (cmd 65042, 5 chunks, FULL PEM with headers)
6. Receive RSA encrypted blob (3 chunks, 256 bytes)
7. RSA decrypt → 80 bytes: key(32) + nonce(12) + aad(12) + ciphertext(24)
8. ChaCha20-Poly1305 decrypt ciphertext → "PLAUD.AI"
9. Send encrypted handshake (cmd_id=1, subtype=0 then subtype=1)
10. Send encrypted commands (sync time, list files, download, etc.)
11. Disconnect gracefully (NO depair — device stays paired)
```

### Target Flow (Plaud V2)
```
1. Background scanner detects device
2. Connect + handshake
3. List files → download new recordings
4. Decrypt audio (RSA + ChaCha20, fully offline)
5. Transcribe (Whisper) → Summarize (LLM)
6. Disconnect gracefully, auto-reconnect when device returns
```

### Encryption Layers
```
Layer 1: BLE Transport (ChaCha20-Poly1305) — SOLVED ✅
  - Session key from RSA exchange (new per connection)
  - Encrypts all commands and responses
  - Stripped during BLE download

Layer 2: File Encryption (ChaCha20 stream cipher) — SOLVED ✅
  - Per-file key, RSA-wrapped with app public key
  - RSA blob at offset 0x100 in file (256 bytes)
  - ChaCha20 keystream XOR'd per-frame (80 bytes each)
  - Key: app_private.pem (static, from APK)

Layer 3: E2EE (optional, user PIN) — NOT NEEDED for most users
  - Additional RSA key pair per user, stored in cloud
  - Only active if user explicitly enabled E2EE in app
  - Retrieved via /e2ee/gen_key API (returns same key each call)
```

## WiFi Transfer — Partially Working

### What Works
- BLE command cmd_id=10 opens device WiFi hotspot (SSID: `Plaud7317`, pass: `56947317`)
- Mac connects to hotspot via `networksetup` (takes ~15-25s)
- Device connects to our WebSocket server on port 8081
- Messages decrypt with BLE session ChaCha20 key
- Device sends handshake: cmd_id=2, `{"token":"494A9257...","pVer":20}`

### What Doesn't Work Yet
- Our handshake response (cmd_id=16) is not accepted — device retries cmd=2 thirty times
- After hotspot disconnect, Mac WiFi doesn't auto-restore
  - `networksetup -setairportnetwork` fails with error -3900
  - Power-cycle (off 3s, on 15s) restores via macOS auto-join
  - SSID hidden by macOS privacy — can't detect by name, only by IP

### WiFi Protocol
```
Packet: [total_len u24 LE][pver u8][cmd_id u16 LE][json_len u16 LE][JSON]
All packets encrypted with BLE session ChaCha20 key (same as transport layer)

Device hotspot: 192.168.84.x subnet
WebSocket: port 8081 (device is WS client, we are server)

Commands:
  cmd_id=2:  Device handshake (device → server)
  cmd_id=16: Server handshake response (server → device)
  cmd_id=11: Get file list
  cmd_id=12: Download file
  cmd_id=13: File sync stop
```

## Project Files

```
plaud-v2/
├── FINDINGS.md              # Reverse engineering findings (device, API, apps)
├── PROTOCOL.md              # BLE protocol specification
├── BLE_CAPTURE_ANALYSIS.md  # PacketLogger capture analysis
├── BLE_COMPREHENSIVE_ANALYSIS.md  # 5-session capture analysis
├── HANDSHAKE_PROGRESS.md    # Handshake debugging history
├── STATUS.md                # This file
├── plaud.py                 # CLI tool (scan, connect, cloud-list, transcribe, etc.)
├── pair_device.py           # Pairing script (RSA handshake)
├── session_manager.py       # Full session: handshake → commands → depair
├── connect_device.py        # Encrypted command session
├── sync_device.py           # Persistent pairing sync (no depair)
├── wifi_sync.py             # WiFi hotspot transfer (WIP)
├── wifi_transfer.py         # Earlier WiFi attempt (HTTP-based, deprecated)
├── decrypt_audio.py         # Audio decryption test script
├── download_all.py          # Cloud recording downloader (Opus → OGG)
├── handshake_test.py        # Handshake testing
├── src/
│   ├── constants.py         # BLE UUIDs, command IDs, timeouts
│   ├── protocol.py          # Packet building/parsing
│   ├── scanner.py           # BLE device scanner
│   ├── client.py            # BLE client
│   ├── cloud_api.py         # Plaud cloud API client
│   ├── crypto.py            # RSA + ChaCha20 encryption
│   ├── transcribe.py        # Whisper transcription
│   └── summarize.py         # LLM summarization
├── keys/
│   ├── app_private.pem      # APK RSA private key (for file decryption)
│   ├── app_public.pem       # APK RSA public key
│   ├── e2ee_private.pem     # User E2EE private key (from cloud API)
│   └── e2ee_public.pem      # User E2EE public key
├── sessions/                # Session logs with keys (gitignored)
├── recordings/              # Downloaded + decrypted audio files (gitignored)
├── cloud_recordings/        # Cloud-downloaded files (gitignored)
├── cloud_recordings_decoded/# Cloud opus → OGG converted (gitignored)
└── .plaud_config.json       # API auth token (gitignored)
```

## Cloud API Status

The Plaud cloud API at `api-euc1.plaud.ai` is **still live** as of 2026-03-27.

| Endpoint | Status | Notes |
|----------|--------|-------|
| /user/me | ✅ Working | Returns user profile |
| /device/list | ✅ Working | Returns empty (device unbound) |
| /file/simple/web | ✅ Working | 57 recordings available |
| /file/temp-url/{id} | ✅ Working | S3 presigned URLs (some opus, some MP3 only) |
| /e2ee/gen_key | ✅ Working | Returns consistent RSA key pair (per-user) |
| /device/e2ee | ⚠️ "device not found" | Device unbound |

Cloud opus files are **raw Opus frames** (80 bytes each, TOC=0xb8). Not OggS-wrapped. Wrap in OggS container for playback. Cloud "wav" files are actually MP3 (ID3 header).

## Key Discoveries

1. **Full PEM required**: Device only accepts RSA public key with `-----BEGIN/END PUBLIC KEY-----` headers (450 bytes, 5 chunks). Body-only fails silently.

2. **SN sig sent twice**: The real app sends the signature chunks twice (6 writes total for 3 chunks). This is a retry mechanism.

3. **Two handshake commands needed**: First handshake (subtype=0) AND second handshake (subtype=1) with bind token before other commands work.

4. **Depair is NOT erase**: cmd_id=5 unbinds from the current session, allowing re-pairing. "Erase and remove" in the iOS app is a different, more destructive command.

5. **Audio encryption uses static app key**: The per-file ChaCha20 key is RSA-encrypted with a key pair from the APK assets — same for ALL users. This is NOT true E2EE. Decryption is fully offline with `app_private.pem`.

6. **Per-frame independent encryption**: Each 80-byte Opus frame is XOR'd with the SAME 80-byte keystream (ChaCha20 counter=0). Not a continuous stream cipher — the keystream resets for every frame.

7. **Hardcoded key is for logs, not audio**: `plaud2023_log_chacha20_key_32bit` found in the Dart binary is for device log encryption, not audio files.

8. **WiFi hotspot uses 192.168.84.x**: Not 192.168.4.x as initially assumed. Device is at 192.168.84.1. DHCP assigns Mac an address in the same subnet.

9. **macOS hides WiFi SSID**: `networksetup -getairportnetwork` returns "not associated" even when connected. Must use IP-based detection (`ipconfig getifaddr en0`).

10. **Cloud opus = raw frames**: Cloud API serves decrypted audio as raw 80-byte Opus frames (no OggS container). TOC byte 0xb8 = CELT 48kHz 20ms mono.
