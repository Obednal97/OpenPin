# Plaud NotePin S — Reverse Engineering Findings

## Device Overview

| Property | Value |
|----------|-------|
| Device | Plaud NotePin S |
| Model Code | 882 (project code in BLE advertising) |
| Serial Number | 8820A0000000XXXX |
| Firmware Version | 65546 (v1.0.10) |
| Chipset | MediaTek (USB VID 0x0E8D) |
| Manufacturer | Shenzhen Smart Connect Technology Co., LTD (TinnoTech) |
| FCC ID | 2A6T3-PN0200 |
| Storage | 64GB internal (~480 hours) |
| Connectivity | BLE (2402-2480 MHz) + WiFi 2.4GHz (2412-2462 MHz) |
| Audio Format | Opus encoded, 20ms frames, 80 bytes/frame/channel |

## USB Connection

When connected via USB, the device presents as:
- **USB CDC ACM** (serial/modem device)
- Port: `/dev/cu.usbmodem0123456789AB1`
- Vendor ID: 0x0E8D (MediaTek)
- Product ID: 0x0023
- Speed: USB 2.0 High Speed (480 Mbps)
- Serial: `0123456789AB` (generic placeholder)

This is a **firmware debug/update interface**, not a mass storage connection. The NotePin series does NOT support USB mass storage mode (only the original Plaud Note does). The port does not respond to AT commands, BROM handshake, or standard serial protocols — it likely requires the TinnoTech proprietary protocol.

## Mobile App (Android APK)

- **Framework**: Flutter (Dart) + native Android (Kotlin/Java)
- **BLE SDK**: TinnoTech PenBLE SDK (`com.tinnotech.penblesdk`)
- **Native Libraries**:
  - `libtnt_ble_utils.so` — BLE protocol utilities, CRC, integer packing (JNI)
  - `libopusJni.so` — Opus audio decoder (JNI)
  - `libffmpegkit.so` + codec libs — Audio format conversion
- **Communication**: Flutter MethodChannel `"plaud.flutter/deviceManager"`
- **State Management**: Uses Isar (local DB), Room/SQLite, SharedPreferences

## Desktop App (Electron)

- **Purpose**: Records computer audio ONLY — does NOT communicate with the physical device
- **Technology**: Electron + React, TRTC SDK for audio capture
- **Audio**: Records PCM 48kHz/16-bit/mono → converts to MP3 16kHz via FFmpeg WASM
- **Upload**: Multipart upload to S3 via presigned URLs
- **Identifier**: `ai.plaud.desktop.plaud` (version 1.0.10)

## Cloud API

### Domains
| Region | API Domain | Web Domain |
|--------|-----------|------------|
| Global | `https://api.plaud.ai` | `https://web.plaud.ai` |
| EU | `https://api-euc1.plaud.ai` | `https://web.plaud.ai` |
| China | `https://api.plaud.cn` | `https://web.plaud.cn` |
| CDN | `https://client-download.plaud.ai` | — |

### Authentication
- OAuth browser-based flow: app opens `{webHost}/launch-desktop?from=desktop&desktop_uuid={uuid}`
- Token exchange: `POST /auth/access-token-auth-code` with `{client_id, auth_code, desktop_uuid}`
- Token format: `bearer {JWT}` (HS256, long-lived ~10 months)
- Region lock: API returns `-302` with redirect if region mismatches

### Request Headers
```
Authorization: bearer {token}
X-Device-Id: {system UUID}
X-Request-Id: {nanoid(11)}
edit-from: desktop | ios | android
app-platform: desktop | ios | android
app-versionNumber: {version}
app-language: en | zh-cn | ja | ko | ...
```

### Key Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/auth/access-token-auth-code` | Exchange auth code for bearer token |
| POST | `/auth/access-token-logout` | Logout |
| GET | `/user/me` | Current user info |
| GET | `/device/list` | List bound devices (SN, model, firmware version) |
| GET | `/device/state/list?sn=` | Device state and details |
| POST | `/device/check` | Validate device `{sn}` |
| POST | `/device/bind` | Bind device to account |
| POST | `/device/unbind` | Unbind device |
| GET | `/device/version/{model}` | Check firmware update (currently returns nothing) |
| POST | `/device/report` | Report device info |
| GET | `/file/version` | Get recording file versions |
| GET | `/file/temp-url/{fileId}?is_opus=1` | Get S3 presigned download URL |
| GET | `/file/simple/web?skip=0&limit=99999&is_trash=0` | List all recordings |
| POST | `/file/get_s3_upload_link` | Get S3 upload link (small files) |
| POST | `/file/get_upload_presigned_url` | Get multipart upload URLs `{filesize, file_type}` |
| POST | `/file/merge_multipart` | Merge multipart upload parts |
| POST | `/file/confirm_upload` | Confirm upload and create record |
| POST | `/ai/transsumm/{fileId}` | Trigger transcription + summarization |
| POST | `/ai/mark/create_task` | Create highlight mark task |
| POST | `/ai/mark/get_mark_result` | Get highlight mark result |
| GET | `/ai/chatllm/{file_id}` | Chat with recording |
| GET | `/ai/chatllm/templates` | Get chat templates |

### Upload Flow
1. `POST /file/get_upload_presigned_url` → `{upload_id, object_name, part_urls[]}`
2. `PUT` 5MB chunks to each presigned URL (3 concurrent)
3. `POST /file/merge_multipart` → `{upload_id, object_name, parts: [{Etag, PartNumber}]}`
4. `POST /file/confirm_upload` → `{upload_id, object_name, scene: 102, file_type: "MP3", ...}`

## Firmware Update Status

**No firmware updates are currently available on any Plaud server.** Tested:
- EU region (`api-euc1.plaud.ai`): "version not found" for all models (880-890)
- US region (`api.plaud.ai`): redirects to EU due to region lock
- CN region (`api.plaud.cn`): "version not found"
- CDN (`client-download.plaud.ai`): 404 for all known bin paths

A hardcoded dev URL was found in the APK: `https://api-dev.plaud.ai/static/bins/MFOTA_V0093_20250724065456.bin` — this file no longer exists.

The firmware update notification shown on the iOS app likely comes from the **device itself via BLE** (`deviceFotaThirdVersion` callback), not from the server. The device's flash may contain metadata about available updates that was written before the company shut down firmware distribution.

## Community Projects

| Project | Language | Approach |
|---------|----------|----------|
| [openplaud/openplaud](https://github.com/openplaud/openplaud) | — | Self-hosted AI transcription via cloud API |
| [arbuzmell/plaud-api](https://github.com/arbuzmell/plaud-api) | Python | Unofficial API client |
| [sergivalverde/plaud-toolkit](https://github.com/sergivalverde/plaud-toolkit) | TypeScript | Core lib + CLI + MCP server |
| [leonardsellem/plaud-sync-for-obsidian](https://github.com/leonardsellem/plaud-sync-for-obsidian) | TypeScript | Obsidian sync plugin |
| [landoncrabtree/applaud](https://github.com/landoncrabtree/applaud) | — | Self-hosted transcription alternative |

All community projects use the **cloud API only** — no one has built a direct BLE client yet. **We are the first to achieve direct BLE control, file download, and offline audio decryption.**

## Audio File Encryption (CRACKED)

### Overview

Audio files on the Plaud NotePin S are encrypted with a **per-file ChaCha20 key**, which is itself **RSA-encrypted with a static key pair from the APK**. This is NOT true end-to-end encryption — the same RSA key pair is embedded in every copy of the app. Decryption is fully offline.

### BLE-Downloaded File Format (512-byte header)

```
Offset   Size   Field
0x000    8      Magic: "PLAUD.AI"
0x008    4      Version: 01 00 00 02 (u32 LE)
0x00C    4      Size field (u32 LE, uncompressed audio size)
0x010    32     User ID (ASCII, zero-padded)
0x030    16     Audio params: codec=5 (Opus), channels=1
0x040    68     Reserved (zeros)
0x084    12     ChaCha20 nonce (unique per file)
0x090    1      Frame size: 0x50 (80 bytes)
0x091    111    Reserved (zeros)
0x100    256    RSA-encrypted per-file key (PKCS1v15, 2048-bit)
0x200+   N×80   Encrypted Opus frames (80 bytes each, no auth tag)
```

### Decryption Algorithm

```python
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

# 1. Load the static app RSA private key (from APK assets)
app_key = load_pem_private_key(open("keys/app_private.pem", "rb").read(), password=None)

# 2. RSA-decrypt the per-file key
file_key = app_key.decrypt(data[0x100:0x200], padding.PKCS1v15())  # → 32 bytes

# 3. Read the per-file nonce
nonce = data[0x84:0x90]  # 12 bytes

# 4. Generate 80 bytes of ChaCha20 keystream (counter=0)
full_nonce = b'\x00\x00\x00\x00' + nonce  # 4-byte counter (0) + 12-byte nonce
cipher = Cipher(algorithms.ChaCha20(file_key, full_nonce), mode=None)
keystream = cipher.decryptor().update(b'\x00' * 80)

# 5. XOR each 80-byte frame with the SAME keystream
audio = data[0x200:]
decrypted = bytearray()
for i in range(0, len(audio), 80):
    frame = audio[i:i+80]
    decrypted.extend(bytes(a ^ b for a, b in zip(frame, keystream)))

# Result: raw Opus frames (TOC 0xb8 = CELT 48kHz 20ms mono)
```

### Key Points

1. **Static key pair**: `keys/app_private.pem` is identical for every Plaud app installation. No account or cloud required.
2. **Per-frame independent encryption**: Every frame uses the SAME keystream (counter=0 reset per frame). This is unusual but confirmed — all encrypted frames have the same first byte (XOR of constant TOC with constant first keystream byte).
3. **No authentication tags**: The encryption is pure XOR (ChaCha20 stream), not AEAD. There are no 16-byte Poly1305 tags per frame.
4. **Optional E2EE layer**: Users who enabled E2EE (PIN-based) have an additional encryption layer using a per-user RSA key pair stored in the cloud. This is separate from the app-level encryption described here.
5. **Hardcoded key is for logs**: `plaud2023_log_chacha20_key_32bit` found in libapp.so is for device log encryption, NOT audio files.

### Cloud File Format

Cloud API (`/file/temp-url/{id}?is_opus=1`) serves audio as **raw Opus frames** (80 bytes each, TOC=0xb8). These are already server-side decrypted. NOT wrapped in OggS — must wrap in Ogg container for playback. Cloud "wav" files are actually MP3 (ID3 header).

## Third-Party Services (from app analysis)

| Service | Usage |
|---------|-------|
| PostHog | Product analytics |
| Datadog | RUM, error tracking, log aggregation |
| Klaviyo | Email marketing |
| Firebase | Crashlytics, analytics, cloud messaging |
| Tencent TRTC | Desktop audio capture (Electron) |
| AWS S3 / CloudFront | File storage and CDN |
| AWS Cognito | Authentication (CN region) |
