# Plaud NotePin S — BLE Protocol Specification

> Reverse engineered from the Android APK (TinnoTech PenBLE SDK + Flutter bridge).
> All integers are **little-endian** unless stated otherwise.

## 1. BLE Service & Characteristic UUIDs

| UUID | Type | Purpose |
|------|------|---------|
| `00001910-0000-1000-8000-00805f9b34fb` | Service | Primary Plaud service |
| `00002BB0-0000-1000-8000-00805f9b34fb` | Characteristic | **Notify** — device → client data |
| `00002BB1-0000-1000-8000-00805f9b34fb` | Characteristic | **Write** — client → device commands |
| `0000180f-0000-1000-8000-00805f9b34fb` | Service | Battery Service (standard BLE) |
| `00002a19-0000-1000-8000-00805f9b34fb` | Characteristic | Battery Level (standard BLE, uint8 0-100%) |
| `00002902-0000-1000-8000-00805f9b34fb` | Descriptor | CCCD — write `01 00` to enable notifications |

## 2. Device Discovery

### BLE Scanning
- No scan filter — scan all BLE devices
- Scan mode: `SCAN_MODE_LOW_LATENCY`
- Identify Plaud devices by parsing **manufacturer-specific data** from advertising packets

### Advertising Data Format
```
Offset  Size  Field
0       1     Length of project code field
1       2     Project code (uint16 LE) — device model identifier
3       1     Version type character (ASCII, e.g., 'V')
4       var   Version number
var     var   Serial number string
var     2     Port version (uint16 LE)
```

### Project Codes (Model Identification)
| Code | Device | BLE Name Format |
|------|--------|-----------------|
| 712 | IzyRec | `IzyRec{last4SN}` (v<22) / `iZYREC{last4SN}` (v≥22) |
| 880 | PLAUD NotePin | `PLAUD{last4SN}` |
| 881 | PLAUD Note Pro | `PLAUD{last4SN}` (portVer<20) / `Plaud{last4SN}` (portVer≥20) |
| 882 | Plaud NotePin S | `Plaud{last4SN}` |
| 888 | Plaud Note | `PLAUD{last4SN}` |
| 901 | G101 Glasses | — |

## 3. Connection Flow

```
1. Scan for BLE devices
2. Identify Plaud by manufacturer data (project code)
3. Connect GATT (timeout: 30s, max retry on error 133: 5)
4. Negotiate MTU (request 255)
5. Discover services
6. Find service 0x1910 → characteristics 0x2BB0 (notify) + 0x2BB1 (write)
7. Enable notifications on 0x2BB0 (write 0x0100 to CCCD 0x2902)
8. Handshake (cmd_id=1)
   ├── For portVersion < 20: Plain handshake → connected
   └── For portVersion ≥ 20: RSA + ChaCha20-Poly1305 encrypted handshake
9. Sync time (cmd_id=4)
10. Get SSN (cmd_id=2) for verification
11. Ready for commands
```

### Handshake (cmd_id=1)

**Request:**
```
Byte 0:    cmd_type = 1
Bytes 1-2: cmd_id = 1 (uint16 LE)
Bytes 3+:  appKey = "0b1ef6247326fc79b200163e0a0431dc" (32 bytes hex string)
           audioChannel (uint8)
           serialNumber (string)
```

**Response:**
```
Byte 0:    cmd_type = 1
Bytes 1-2: cmd_id = 1
Byte 3:    status (0 = success)
Byte 4-5:  portVersion (uint16 LE)
Bytes 6+:  timezone, audioChannel, supportWifi (bool)
           isOggAudio (bool), versionType, version
```

### Encrypted Handshake (portVersion ≥ 20)

After the initial handshake, if portVersion ≥ 20:
1. Device sends RSA-encrypted key material via cmd_id 65042 (multiple BLE packets)
2. Client decrypts with stored RSA private key
3. Extracts: ChaCha20 key (32 bytes), nonce (12 bytes), AD (12 bytes)
4. Validates by decrypting test ciphertext → expects plaintext `"PLAUD.AI"`
5. All subsequent data on 0x2BB0 is encrypted with ChaCha20-Poly1305

## 4. Packet Format

### Command Packets (cmd_type=1)
```
Byte 0:    cmd_type (1)
Bytes 1-2: cmd_id (uint16 LE)
Bytes 3+:  payload (varies by command)
```

### File Data Packets (cmd_type=2)
```
For portVersion ≥ 7:
  Byte 0:    cmd_type (2)
  Bytes 1-4: sessionId (uint32 LE)
  Bytes 5-8: offset (uint32 LE)
  Byte 9:    data length (uint8)
  Bytes 10+: audio data (Opus)

For portVersion < 7:
  Byte 0:    cmd_type (2)
  Bytes 1-4: offset (uint32 LE)
  Byte 5:    data length (uint8)
  Bytes 6+:  audio data (Opus)

Special: offset = 0xFFFFFFFF → empty/end packet with error code
```

### Log Packets (cmd_type=5)
```
Byte 0:    cmd_type (5)
Bytes 1-4: 0xFFFFFFFF marker
Bytes 5+:  log data
```

## 5. Command Reference

### Device Information

| cmd_id | Name | Request Payload | Response Payload |
|--------|------|----------------|------------------|
| 1 | Handshake | appKey, audioChannel, SN | status, portVersion, timezone, capabilities |
| 2 | Get SSN | (none) | serialNumber (up to 56 chars), versionName |
| 3 | Get State | (none) | recording state, privacyEnable, keyState, usbState, findMyState, scene, sessionId |
| 4 | Sync Time | unixTimestamp (uint32), tzHour (int8), tzMin (int8) | (ack) |
| 5 | Depair | (none) | (ack) |
| 6 | Storage Op | (varies) | total, free, duration |

### Recording Control

| cmd_id | Name | Request Payload | Response Payload |
|--------|------|----------------|------------------|
| 20 | Start Record | (none) | status, sessionId |
| 21 | Pause Record | (none) | status |
| 22 | Resume Record | (none) | status |
| 23 | Stop Record | (none) | status, sessionId, fileSize |
| 33 | Record Tags | (varies) | tag data |

### File Operations

| cmd_id | Name | Request Payload | Response Payload |
|--------|------|----------------|------------------|
| 26 | Get File List | (none) | Array of: sessionId (uint32), fileSize (uint32), codecType (uint8), channelType (uint8) |
| 28 | Sync File Start | sessionId (uint32), startOffset (uint32), endOffset (uint32) | sessionId, status |
| 29 | Sync File End | — (received, not sent) | sessionId, CRC16 |
| 30 | Stop Sync File | (none) | (ack) |
| 31 | Delete File | sessionId (uint32) | status |

### Device Settings

| cmd_id | Name | Request | Response |
|--------|------|---------|----------|
| 9 | Device Settings | settingType, value | current settings |
| 13 | Common Settings | settingType, actionType, value | (ack) |
| 34 | LED State | (varies) | state |
| 109 | Find My / Auto-delete | (varies) | state |

### WiFi Operations

| cmd_id | Name | Description |
|--------|------|-------------|
| 10 | Switch WiFi Mode | Toggle WiFi on/off |
| 121 | WiFi Config Set | Configure WiFi SSID/password |
| 122 | WiFi Config Get | Read WiFi configuration |
| 123 | WiFi Idle Sync Test | Test WiFi sync in idle mode |

### Firmware Update (OTA)

| cmd_id | Name | Description |
|--------|------|-------------|
| 50 | OTA Control | Start/prepare OTA |
| 51 | OTA Status | Query OTA status |
| 52 | OTA Confirm | Confirm OTA completion |
| 61 | OTA Data Push | Push firmware data chunks |
| 65041 | RSA Pre-Handshake | RSA key exchange (pre) |
| 65042 | RSA Handshake | RSA key exchange (confirm) |

### Miscellaneous

| cmd_id | Name | Description |
|--------|------|-------------|
| 8 | Pass-through | Sub-command with own sub-id at offset 3 |
| 36 | Stick Angles | Accelerometer/orientation data |
| 103-104 | Extended | Extended device operations |
| 128 | Extended 2 | More extended operations |
| 130 | SoundPlus Token | Set SoundPlus license key |
| 131 | SD Flash CID | Get SD/flash card identifier |
| 141-146 | Log Operations | Device log sync and management |

## 6. File Transfer Protocol

### Step-by-step

```
1. Send cmd_id=26 (Get File List)
   → Response: [{sessionId, fileSize, codecType, channelType}, ...]

2. For each file to transfer:
   a. Send cmd_id=28 (Sync File Start)
      Payload: sessionId (uint32), startPosition=0 (uint32), endPosition=fileSize (uint32)

   b. Receive type=2 packets continuously:
      - Parse sessionId, offset, length, data
      - Append data to output buffer
      - If offset != expected: re-send cmd_id=28 from last good position (packet loss recovery)
      - Timeout for packet loss: 5000ms

   c. Receive cmd_id=29 (Sync File End)
      - Contains: sessionId, CRC16

   d. Verify CRC of downloaded data using tntGetFileCrc()

3. Decode Opus audio to PCM:
   - Opus decoder: 16kHz or 48kHz sample rate
   - Channels: mono (1) or stereo (2) based on channelType
   - Frame size: 20ms per frame
   - 80 bytes per frame per channel
```

### Duration Calculation
```
duration_ms = (fileSize / (channels * 80)) * 20
```
Where `channels` is the audio channel count from the file list.

### Audio Format Details
- **On-device**: Opus encoded (possibly OGG-wrapped, indicated by `isOggAudio` flag)
- **Frame size**: 20ms per Opus frame
- **Bytes per frame**: 80 per channel
- **Sample rates**: Typically 16kHz (speech) or 48kHz (high quality)
- **Channels**: 1 (mono) for most recordings; 2 for stereo devices

## 7. OTA Firmware Update Protocol

### BLE OTA Flow
```
1. Download firmware .bin file from server (URL from /device/version API)
2. Validate file exists and is readable
3. Send cmd_id=50 (OTA Control) to prepare device
4. Send firmware data via cmd_id=61 (OTA Data Push) in chunks
5. Monitor progress callbacks (otaPushProgress: 0-100%)
6. Wait for cmd_id=52 (OTA Confirm) or otaPushFinish callback
7. Device reboots and verifies firmware
8. On next BLE connection, device reports result via deviceFotaResult callback
```

### WiFi OTA Flow
```
1. Open WiFi on device (cmd_id=10)
2. Connect phone to device's WiFi hotspot
3. Transfer firmware over WiFi (faster than BLE)
4. Monitor OTA upgrade status
5. Device reboots
```

### OTA Error Codes (Constants$OtaPushError)
| Code | Name | Description |
|------|------|-------------|
| 1 | RESTART | Duplicate submission |
| 2 | SEND_BLE_FAIL | BLE send failure |
| 3 | FILE_NOT_EXISTS | Firmware file not found |
| 4 | FILE_ERROR | Firmware file corrupt |
| 5 | USER_INTERRUPT | Manual interruption |
| 6 | DEVICE_UPGRADE_FAIL | Upgrade failed |
| 7 | DEVICE_VERSION_NOT_MATCH | Version mismatch |
| 8 | DEVICE_FLASH_WRITE_FAIL | Flash write error |
| 9 | DEVICE_WRITE_MORE_COUNT | Too many retries |
| 10 | DEVICE_U_DISK_MODE | Device in USB disk mode |
| 11 | OTA_FILE_SIZE_TOO_LARGE | File too large |
| 12 | BT_DISCONNECT | BLE disconnected |
| 13 | DEVICE_RECORDING | Cannot update while recording |
| 14 | SEND_BLE_COMMAND_FAIL | BLE command send failure |
| 15 | DEVICE_SPACE_LOW | Insufficient storage |
| 16 | DEVICE_MODE_NOT_MATCH | Wrong device mode |
| 17 | WORKING_NOW | Device busy |

### OTA Upgrade Status (Constants$OtaUpgradeStatus)
| Code | Status | Description |
|------|--------|-------------|
| 0 | SUCCESS | Firmware applied successfully |
| 1 | REPLACED_BY_NEW_OTA | Replaced by newer OTA |
| 2 | TRANSFER_TIMEOUT | Transfer timed out (1 min) |
| 3 | FILE_SIZE_MISMATCH | Size mismatch |
| 4 | CRC_CHECK_FAILED | CRC verification failed |
| 5 | MAX_RETRY_REACHED | Max retries exceeded |
| 6 | FLUSH_CACHE_FAILED | Cache flush error |

## 8. Encryption Details

### ChaCha20-Poly1305 (portVersion ≥ 20)

After initial handshake:
1. Device sends RSA-encrypted blob (cmd 65041/65042, may span multiple BLE packets)
2. Client decrypts RSA blob to extract:
   - ChaCha20 key: 32 bytes
   - Nonce: 12 bytes
   - Additional Data (AD): 12 bytes
3. Validation: decrypt test ciphertext, expect plaintext `"PLAUD.AI"`
4. All subsequent notify data (0x2BB0) is encrypted
5. Write data (0x2BB1) may also be encrypted

### App Key
```
0b1ef6247326fc79b200163e0a0431dc
```
Used in every handshake request.

## 9. Constants & Timeouts

| Constant | Value |
|----------|-------|
| BLE connect timeout | 30,000 ms |
| Handshake timeout | 60,000 ms |
| MTU request | 255 |
| Max reconnect on error 133 | 5 |
| Max failed writes before disconnect | 30 |
| Packet loss recovery timeout | 5,000 ms |
| OTA transfer timeout | 60,000 ms |
| BLE scan mode | LOW_LATENCY (1) |

## 10. Key Source Files (Decompiled)

| File | Content |
|------|---------|
| `p343rh/InterfaceC10589q4.java` | BLE UUIDs |
| `p343rh/C10558l3.java` | BleAgentImpl — main BLE agent (4577 lines) |
| `p343rh/C10595r4.java` | BluetoothLeOperation — GATT operations |
| `th/AbstractC11066l.java` | Scan result parser (advertising data) |
| `th/AbstractC11067m.java` | RSA + ChaCha20-Poly1305 crypto |
| `p257nh/*.java` | Command request builders (C9554a through C9603z) |
| `p279oh/*.java` | Response parsers (C9766a through C9818z0) |
| `com/tinnotech/penblesdk/utils/TntBleCommUtils.java` | Binary pack/unpack + CRC (JNI) |
| `com/tinnotech/penblesdk/utils/OpusUtils.java` | Opus decoder (JNI) |
| `com/tinnotech/penblesdk/entity/BleDevice.java` | Device model |
| `com/tinnotech/penblesdk/entity/BleFile.java` | File model + duration math |
| `ai/plaud/.../FlutterDeviceManager.java` | Flutter bridge (6713 lines) |
| `kh/InterfaceC8637a.java` | BleAgent interface (full API surface) |
