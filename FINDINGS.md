# Plaud NotePin S — Reverse Engineering Findings

## Device Overview

| Property | Value |
|----------|-------|
| Device | Plaud NotePin S |
| Model Code | 882 (project code in BLE advertising) |
| Serial Number | 8820B50256947317 |
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

All community projects use the **cloud API only** — no one has built a direct BLE client yet.

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
