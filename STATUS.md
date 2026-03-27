# Plaud NotePin S — Project Status

> Last updated: 2026-03-27

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
| List files on device | cmd_id=26, returns session_id + file_size | ✅ |
| Download files via BLE | cmd_id=28 (sync file start), ~33 KB/s | ✅ |
| Device settings | cmd_id=8 (passthrough), e.g. raw wave mode | ✅ |
| Depair/unbind | cmd_id=5 | ✅ |
| Battery level | GATT read 0x2A19 | ✅ |
| Session key logging | Every session saved to sessions/ with timestamp | ✅ |
| Cloud API download | All 41 recordings as MP3 via api-euc1.plaud.ai | ✅ |
| Local transcription | OpenAI Whisper (base/medium/large models) | ✅ |
| Summarization pipeline | Ollama/OpenAI/Anthropic ready | ✅ |

### Remaining
| Feature | Blocker | Notes |
|---------|---------|-------|
| Audio decryption | E2EE file_key needed | ASR files encrypted with per-file ChaCha20 key |
| Persistent connection | Need to stop using depair after each session | Device should stay paired |
| Auto-reconnect | Not built yet | Should detect device and sync automatically |
| WiFi fast transfer | Not implemented | Device supports WiFi hotspot transfer |
| File download as WAV | Raw wave enabled but BLE always sends ASR | May need different cmd |

## Architecture

### Connection Flow (Current — needs rework for persistence)
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
10. Send encrypted commands (sync time, get state, list files, etc.)
11. Depair (cmd_id=5) ← REMOVE THIS for persistent connection
12. Disconnect
```

### Desired Flow (persistent)
```
1. Background scanner detects device
2. Connect + handshake (if not already connected)
3. Sync: list files → download new recordings → transcribe → summarize
4. Stay connected (or disconnect gracefully WITHOUT depair)
5. Auto-reconnect when device returns in range
```

### Encryption Layers
```
Layer 1: BLE Transport (ChaCha20-Poly1305)
  - Session key from RSA exchange (new per connection)
  - Encrypts all commands and responses
  - We handle this ✅

Layer 2: E2EE File Encryption (ChaCha20-Poly1305)
  - Per-file key from E2EE system
  - Encrypts Opus audio frames inside ASR files
  - Key from Plaud server API, protected by user PIN
  - NOT YET CRACKED ❌
```

### ASR File Format
```
Stored (512-byte header):
  [0x00] "PLAUD.AI" magic
  [0x08] Version (01 00 00 02)
  [0x0C] Size field
  [0x10] User ID (32 bytes)
  [0x30] Audio params (codec, channels)
  [0x84] 12-byte nonce
  [0x100] 256-byte RSA-encrypted file key
  [0x200+] Encrypted Opus frames (80B + 16B MAC = 96B each)

BLE-transferred (132-byte header):
  [0x00] "PLAUD.AI" magic
  [0x08] Version
  [0x10] User ID
  [0x30] Audio params
  [0x84+] Encrypted audio data (no RSA blob in header)
```

## Project Files

```
plaud-v2/
├── FINDINGS.md              # Reverse engineering findings (device, API, apps)
├── PROTOCOL.md              # BLE protocol specification
├── BLE_CAPTURE_ANALYSIS.md  # PacketLogger capture analysis
├── HANDSHAKE_PROGRESS.md    # Handshake debugging history
├── STATUS.md                # This file
├── plaud.py                 # CLI tool (scan, connect, cloud-list, transcribe, etc.)
├── pair_device.py           # Pairing script (RSA handshake)
├── session_manager.py       # Full session: handshake → commands → depair
├── connect_device.py        # Encrypted command session
├── handshake_test.py        # Handshake testing
├── src/
│   ├── constants.py         # BLE UUIDs, command IDs, timeouts
│   ├── protocol.py          # Packet building/parsing
│   ├── scanner.py           # BLE device scanner
│   ├── client.py            # BLE client (needs update for persistent mode)
│   ├── cloud_api.py         # Plaud cloud API client
│   ├── crypto.py            # RSA + ChaCha20 encryption
│   ├── transcribe.py        # Whisper transcription
│   └── summarize.py         # LLM summarization
├── keys/                    # RSA keys (gitignored)
├── sessions/                # Session logs with keys (gitignored)
├── recordings/              # Downloaded audio files (gitignored)
├── cloud_recordings/        # Cloud-downloaded MP3s (gitignored)
└── .plaud_config.json       # API auth token (gitignored)
```

## API Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /device/list | List bound devices |
| POST | /device/bind | Bind device, get access_token |
| POST | /device/unbind | Unbind device |
| POST | /device/sn-sign | Get SN signature for BLE handshake |
| GET | /device/state/list | Device state details |
| GET | /file/simple/web | List cloud recordings |
| GET | /file/temp-url/{id} | Get S3 download URL |
| POST | /e2ee/gen_key | Generate E2EE RSA key pair |

## Key Discoveries

1. **Full PEM required**: Device only accepts RSA public key with `-----BEGIN/END PUBLIC KEY-----` headers (450 bytes, 5 chunks). Body-only fails silently.

2. **SN sig sent twice**: The real app sends the signature chunks twice (6 writes total for 3 chunks). This is a retry mechanism.

3. **Packet format**: Pre-encryption packets use `[seq][0xFE][subcmd][chunk_idx][data]`, NOT `[cmd_id u16 LE]` as initially assumed.

4. **Two handshake commands needed**: First handshake (subtype=0) AND second handshake (subtype=1) with bind token before other commands work.

5. **Depair is NOT erase**: cmd_id=5 unbinds from the current session, allowing re-pairing. "Erase and remove" in the iOS app is a different, more destructive command.

6. **Audio is double-encrypted**: BLE transport encryption (which we handle) wraps E2EE-encrypted ASR files. The E2EE layer needs a separate key.

7. **Hardcoded fallback key exists**: `plaud2023_log_chacha20_key_32bit` found in the Dart binary — potentially a debug/test key for audio decryption.
