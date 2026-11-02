# Plaud NotePin S protocol

How Plaud V2 talks to the NotePin S: discovery, the BLE handshake, the encrypted channel, file
transfer over BLE and over the device's WiFi hotspot, the recording file format, and the Plaud
Cloud calls that supply per-device credentials. Per-command byte layouts are in
[COMMANDS.md](COMMANDS.md). What the iOS packet captures showed is in [CAPTURES.md](CAPTURES.md).

Status tags (**Confirmed**, **Implemented**, **APK**, **Unverified**) are defined in the
[README](README.md#status-legend). Untagged statements describe what the code does.

Conventions:

- All integers are little-endian.
- Byte offsets are into the decrypted plaintext, where byte 0 is `cmd_type`.
- `SN` is the device serial number, for example `8820A00012345678` (a made-up value).

## 1. Device

| Property | Value | Status |
|---|---|---|
| Model | Plaud NotePin S, project code 882 (the serial starts with `882`) | Confirmed |
| Firmware seen | `65546` = 0x01000A = v1.0.10 | Confirmed |
| Maker | Shenzhen Smart Connect Technology (TinnoTech). FCC ID 2A6T3-PN0200 | Unverified |
| BLE SDK in the Android app | TinnoTech PenBLE SDK (`com.tinnotech.penblesdk`), native `libtnt_ble_utils.so` | APK |
| USB | MediaTek CDC ACM serial port (VID 0x0E8D, PID 0x0023). Firmware debug/update only, no mass storage, no response to AT or BROM probes | Confirmed |
| Radio | BLE plus 2.4 GHz WiFi (hotspot mode only) | Confirmed |
| Audio | Opus (CELT), 20 ms frames, 80 bytes per frame, mono | Confirmed |

## 2. Discovery

`app/state/device.py` `scan_devices()` scans with no filter and accepts a device when any of these
hold:

- its name contains `PLAUD`, `IZYREC` or `NOTEPIN`;
- `src/scanner.py` `parse_manufacturer_data()` recognises its manufacturer data;
- its manufacturer data has company ID `0x005D`.

The WiFi worker (`wifi_sync_worker.ble_connect_and_setup()`) uses only the `0x005D` test.

Manufacturer data under company ID `0x005D`, as `parse_manufacturer_data()` reads it
(**Confirmed**: the app finds paired devices by this serial):

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | Length |
| 1 | 1 | Version type, ASCII `V` (0x56) |
| 2 | 2 | Version number (u16) |
| 4 | 1 | Unknown |
| 5 | 1 | Serial length `n` |
| 6 | n | Serial number as raw bytes. The app renders them as upper-case hex to get `SN` |
| 6+n+1 | 1 | Port version |

macOS gives the device a different BLE address on each connection. The app keys paired devices by
`SN`, never by address.

Project codes (**APK**): 712 IzyRec, 880 PLAUD NotePin, 881 PLAUD Note Pro, 882 Plaud NotePin S,
888 Plaud Note, 901 G101 glasses.

## 3. GATT

| UUID | Properties | Use |
|---|---|---|
| `00001910-0000-1000-8000-00805f9b34fb` | Service | Plaud data service |
| `00002bb1-…` | Write, Write Without Response | Host to device |
| `00002bb0-…` | Notify | Device to host |
| `0000b001-…` | Read | Unused, purpose unknown |
| `0000b004-…` | Indicate | Unused, purpose unknown |
| `00002a19-…` (in service `180F`) | Read, Notify | Standard battery level. **Do not read it** |

UUIDs in code: `app/utils/constants.py` (`BLE_WRITE_UUID`, `BLE_NOTIFY_UUID`).

Reading `0x2A19` from macOS hangs with no timeout (**Confirmed**, v2.2.0). It froze the battery
value and blocked the BLE lock. The app reads battery with cmd 9 instead.

The device grants an ATT MTU of 247 (**Confirmed** in captures). The app does not request an MTU;
CoreBluetooth negotiates it.

## 4. Credentials

The handshake needs three things. None of them is the owner's Plaud account password.

| Credential | What it is | Where the app gets it | Stored as |
|---|---|---|---|
| App RSA key pair | RSA-2048 key pair shipped in the Android app. Identical in every install | Extracted from the APK. Kept in `keys/e2ee_private.pem` and `keys/e2ee_public.pem`, which are not in git | Files |
| `SN_SIG` | 256-byte signature over the serial | `POST /device/sn-sign` | Config `device_sn_signatures[SN]`, base64 |
| `BIND_TOKEN` | Per-device token | `POST /device/bind` (`access_token` in the reply) | Config `device_bind_tokens[SN]` |

`app/state/cloud.py` (`bind_device()`, `fetch_sn_signature()`) fetches `SN_SIG` and `BIND_TOKEN`
once per device, when the user is signed in to Plaud Cloud. After that the app works offline. If
either is missing at connect time, the app tries to fetch it, and fails with "Not logged into Plaud
Cloud" if it can't.

The app uses the same key pair (`APP_KEY_PATH` in `constants.py`, the default in `src/crypto.py`)
for the BLE handshake and to decrypt recordings. Older notes call it `app_private.pem`; that file
name is gone.

## 5. Handshake

### 5.1 Pre-encryption packets

Before encryption starts, packets on `2BB1`/`2BB0` use this framing:

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | Opcode (u16) |
| 2 | 1 | Total number of chunks |
| 3 | 1 | Index of this chunk |
| 4 | ≤100 | Chunk data |

| Opcode | Wire bytes 0–1 | Direction | Content |
|---|---|---|---|
| 65040 (0xFE10) | `10 FE` | Host to device | `SN_SIG`, 256 bytes in 3 chunks |
| 65041 (0xFE11) | `11 FE` | Device to host | ACK: "send me your public key". 3 bytes on the wire: `11 FE 00` |
| 65042 (0xFE12) | `12 FE` | Host to device | App public key, full PEM with header lines (about 450 bytes, 5 chunks) |
| 65042 (0xFE12) | `12 FE` | Device to host | Session blob, RSA-2048 ciphertext, 256 bytes in 3 chunks |

### 5.2 Sequence

`device.py` `connect_device()` (the WiFi worker repeats this in `ble_connect_and_setup()`):

1. Enable notifications on `2BB0`.
2. Send `SN_SIG` as 65040 chunks with Write Without Response. `device.py` sends the full set 3
   times; the worker and the iOS app send it twice.
3. Wait for either a 65041 ACK or the 65042 blob (`device.py` waits up to 10 s, the worker 5 s).
   - **ACK**: the device doesn't know this app's public key (first pairing, or after a depair).
     Send the PEM as 65042 chunks with Write (with response), then wait up to 15 s for the blob.
   - **Blob straight away**: the device already has the public key cached. No ACK is sent.
   - **Neither**: handshake failed.
4. Join the blob chunks in index order and RSA-decrypt them with the app private key (PKCS#1 v1.5).
5. Split the plaintext:

   | Bytes | Field |
   |---|---|
   | 0–31 | ChaCha20-Poly1305 key |
   | 32–43 | Nonce |
   | 44–55 | Associated data (AAD) |
   | 56+ | Check ciphertext |

6. Decrypt the check ciphertext with that key, nonce and AAD. It must be `PLAUD.AI`.
7. Swap the notify handler for the encrypted one (section 6).
8. Send cmd 1 twice with the `BIND_TOKEN` (flag 0, then flag 1). See
   [COMMANDS.md cmd 1](COMMANDS.md#cmd-1--handshake). The static SDK `APP_KEY` (in the
   deleted `src/constants.py`) is **not** sent; sending it makes the device ignore later commands.
9. `device.py` then sends cmd 4 (time), cmd 3 (state), cmd 6 (storage) and cmd 9 (battery). The
   worker sends only cmd 4.

The device sends a fresh key, nonce and AAD on every connection. The session ends when the BLE
link drops (**Confirmed**), so the WiFi worker can't reuse the app's connection and does its own
handshake.

All of the above is **Confirmed**: it is how the app connects today.

## 6. Encrypted channel

After the handshake every packet, in both directions and over WiFi too, is:

```
wire = ChaCha20-Poly1305(key).encrypt(nonce, seq_u32 || plaintext, aad)
```

- `len(wire) = 4 + len(plaintext) + 16`. There is no plaintext header on the wire.
- The nonce and AAD are the same for every packet in the session. The cipher reuses its nonce,
  which is why captures show bytes that stay constant within a session (see
  [CAPTURES.md](CAPTURES.md#disproven-or-historical)).
- `seq` is a u32 counter the host starts at 1 and increments for each packet it sends. The
  receiver decrypts and drops the first 4 bytes. The app does not check the device's counter.
- Packets that fail to decrypt are logged and dropped.

Plaintext:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `cmd_type`: 1 command/response, 2 file data, 4 BLE rate test (**APK**), 5 log data (**APK**) |
| 1 | 2 | `cmd_id` (type 1 only) |
| 3+ | | Payload |

Matching replies to requests: `send_cmd()` stores the `cmd_id` it sent in
`_device_state["expected_cmd_id"]`. The notify handler (`_make_notify_handler()`) wakes it only for
a type-1 packet with that `cmd_id`. The device also sends unsolicited type-1 packets (button-press
recording events, late replies); before v2.2.0 one of them could be returned as the reply and
misread (the "stuck 90 % battery" bug). The Android SDK checks the opcode the same way.

## 7. File transfer over BLE

Code: `device.py` `list_device_files()`, `_sync_files_inner()`, `evaluate_download()`,
`_make_notify_handler()`.

### 7.1 Flow

1. **List**: send cmd 26. The reply can span several notifications; the app collects them for up to
   3 s until it has `total` entries, then removes duplicate session IDs.
2. **Skip** files already present (`{sid}.ogg` in any recordings folder, or in Google Drive in cloud
   mode), files the user dismissed, and (background syncs only) files that failed 3 times in a row.
3. **Request**: send cmd 28 with `sessionId`, start 0, end = size from the list. It is sent
   directly with `write_gatt_char`, not through `send_cmd()`.
4. **Receive** type-2 data packets (7.2) and append their payload.
5. **Finish** when the end marker or the cmd 29 tail arrives. If the end marker comes first, wait
   up to 1.5 s for the tail.
6. If interrupted (abort, disconnect, stall, refusal) while still connected, send cmd 30 so stale
   packets don't leak into the next file.
7. **Decide** with `evaluate_download()` (7.3), then save (7.4).
8. **Auto-delete** (only if the user turned it on): cmd 31 for each file whose verdict was `ok` and
   whose `.ogg` was written.

### 7.2 Data packets (`cmd_type` 2)

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `cmd_type` = 2 |
| 1 | 4 | `sessionId` |
| 5 | 4 | Offset in the file. `0xFFFFFFFF` marks the end of the file |
| 9 | 1 | Data length; in the end packet, the device's error code (0 = OK) |
| 10 | n | File bytes |

Each data notification seen in captures is 190 bytes on the wire, which is 10 header bytes plus
160 bytes of file data (inferred from the sizes, see [CAPTURES.md](CAPTURES.md)).

For port version < 7 the SDK uses an older layout without `sessionId`: offset at 1–4, length at 5,
data from 6 (**APK**).

The app records, but does not act on, the first packet whose offset differs from the bytes received
so far. It does not compare the packet's `sessionId`, the end packet's `sessionId` or the tail's
`sessionId` with the file it asked for.

### 7.3 Completeness rules (`evaluate_download()`)

These rules are **Implemented** but not confirmed on hardware. They fail safe: anything doubtful is
kept and retried, never auto-deleted.

| Condition | Verdict |
|---|---|
| Interrupted: aborted, disconnected, stalled (no data for 60 s), or refused | `partial` |
| No end marker and no cmd 29 tail | `partial` |
| End marker with a non-zero error byte | `partial` |
| `CRC16_ENFORCED_VARIANT` is set and the cmd 29 CRC doesn't match | `partial` |
| Bytes received == size from the file list | `ok` |
| Size differs, end marker arrived with error 0 | `size_mismatch` |
| Size differs, only the tail arrived | `partial` |

- **Refused**: the cmd 28 reply has a non-zero status at byte 7 *and* no data has arrived after
  5 s. A non-zero status with data flowing is only logged.
- **CRC**: cmd 29 carries a CRC-16 computed by the native `tntGetFileCrc()`. The variant is unknown.
  `CRC16_ENFORCED_VARIANT` is `None`, so the CRC is never used to reject a file. After a successful
  save, `_log_tail_crc()` logs which of XMODEM, CCITT-FALSE, ARC, MODBUS and KERMIT matches. Once
  hardware shows a consistent match, set the constant to that name.

### 7.4 What gets saved

| Verdict | Files | Auto-delete from device |
|---|---|---|
| `ok` | `{sid}_encrypted.bin` written first, then `{sid}.ogg`. The `.bin` is removed for files ≤ 500 000 bytes | Yes, if enabled |
| `size_mismatch` | Kept only if it decrypts to valid Opus: `{sid}.ogg` plus `{sid}_encrypted.bin`. Otherwise treated as `partial` | Never |
| `partial` | Raw bytes as `{sid}_partial.bin` (a larger earlier partial is never overwritten by a smaller one). The next sync retries | Never |
| Decrypt fails | `{sid}_encrypted.bin` kept, status `decrypt_failed` | Never |

A file only counts as present when `{sid}.ogg` exists, so partials and failed decrypts are retried.

## 8. File transfer over WiFi

Code: `app/utils/wifi_sync_worker.py` (a separate process), started by `device.py`
`do_wifi_sync_blocking()`. The message flow below is **Confirmed** (it worked in earlier versions).
The reconnect, admission and network-restore logic reworked in v2.4.0 is **Implemented**, not yet
confirmed on hardware.

### 8.1 Flow

1. The app drops its own BLE connection. The worker scans, connects and does the full handshake
   (section 5), then sends cmd 1 twice and cmd 4.
2. The worker starts a WebSocket server on `0.0.0.0:8081`, falling back to 8082 then 8083 if the
   port is busy.
3. It sends BLE cmd 10 (`[1,10,0]`) to open the device's hotspot, then stops BLE notifications.
4. It joins the hotspot:

   | | Value |
   |---|---|
   | SSID | `Plaud` + last 4 characters of `SN` (example: `Plaud5678`) |
   | Password | Last 8 characters of `SN` (example: `12345678`) |
   | Mac's address | `192.168.x.x`; `192.168.84.x` observed |

5. **The device connects to the Mac's WebSocket server**, not the other way round. The worker
   accepts one connection at a time, and only if it arrives on the WiFi interface's hotspot address
   from a peer on the same /24.
6. Messages follow 8.3 until every file is handled or the download budget runs out.
7. Cleanup: restore the Mac's saved networks if they were paused, then send BLE `[1,10,0,0]` to
   close the hotspot, then rejoin the user's network and disconnect BLE.

Closing with `[1,10,0,0]` is **Unverified**. The SDK has a separate CloseWiFi reply (cmd 13), and
`_close_hotspot()` replaces the notify handler with a no-op before sending, so the worker never
sees a reply.

The Mac-side safety net (restore record `wifi_restore.json` in the data folder, lock file, the
"networks couldn't be restored" notice) is app behaviour, not protocol. See the module docstring of
`wifi_sync_worker.py`.

### 8.2 WiFi packet format

Each WebSocket binary message is encrypted exactly as in section 6, with the key, nonce and AAD
from the worker's BLE handshake. Each WebSocket connection has its own `seq` counter, starting at 1.

Plaintext (`build_wifi_packet()`, `parse_wifi_packet()`):

| Offset | Size | Field |
|---|---|---|
| 0 | 3 | Total length, header included (u24) |
| 3 | 1 | Protocol version, 2 |
| 4 | 2 | WiFi `cmd_id` |
| 6 | 2 | JSON length `j` |
| 8 | j | UTF-8 JSON, compact |
| 8+j | | Binary data (file list entries, file bytes) |

WiFi command numbers are their own namespace. WiFi cmd 13 is not BLE cmd 13.

### 8.3 Message flow

| Step | From | WiFi cmd | Payload |
|---|---|---|---|
| 1 | Device | 2 | Handshake request (payload ignored) |
| 2 | Mac | 1 | `{"token": BIND_TOKEN, "stamp": <unix time>}` |
| 3 | Device | 1 | Handshake accepted (payload not checked) |
| 4 | Mac | 11 | `{"uid": <unix time>, "start": 0, "single": 0}` |
| 5 | Device | 11 | `{"status": 0, "count": N, …}` then N × 10-byte entries: `sessionId` u32, size u32, codec u16 |
| 6 | Mac | 12 | `{"session": sid, "scene": 0, "start": 0, "end": size}` |
| 7 | Device | 13 | JSON (with `"last"`) then file bytes. Repeats; `"last": 1` ends the file |
| 8 | | | Back to step 6 for the next file |
| any | Device | 3 | Heartbeat ping. The Mac replies with WiFi cmd 16, `{}` |

WiFi saves use the same rules as BLE (`save_wifi_download()`): exact size gives `{sid}.ogg`; a size
mismatch is kept only if it decrypts; anything else becomes `{sid}_partial.bin`. There is no end
error byte and no CRC over WiFi. WiFi sync never deletes files from the device.

Other WiFi commands seen only in the APK: 14 file delete, 15 sync stop, 20 and 21 OTA control and
data. The heartbeat ping reportedly carries battery level, voltage and charging (**APK**).

## 9. Recording file format

Files arrive the same way over BLE and WiFi. Code: `app/utils/audio.py` `decrypt_ble_file()`.

### 9.1 Header (512 bytes)

| Offset | Size | Field | Status |
|---|---|---|---|
| 0x000 | 8 | Magic `PLAUD.AI` | Confirmed, checked |
| 0x008 | 4 | Version, `01 00 00 02` | Unverified, unused |
| 0x00C | 4 | Size field | Unverified, unused |
| 0x010 | 32 | User ID, ASCII, zero-padded | Unverified, unused |
| 0x030 | 16 | Audio parameters (codec 5 = Opus, 1 channel) | Unverified, unused |
| 0x084 | 12 | ChaCha20 nonce, unique per file | Confirmed |
| 0x090 | 1 | Frame size, 0x50 (80) | Unverified, unused |
| 0x100 | 256 | Per-file key, RSA-2048 PKCS#1 v1.5 encrypted with the app public key | Confirmed |
| 0x200 | | Encrypted Opus frames, 80 bytes each, no authentication tag | Confirmed |

### 9.2 Decryption

1. RSA-decrypt bytes 0x100–0x1FF with the app private key. The first 32 bytes are the file key.
2. Build a ChaCha20 cipher with the file key and nonce `00 00 00 00 || header[0x84:0x90]`
   (counter 0).
3. **Per-frame keystream** (tried first): take the first 80 keystream bytes and XOR every 80-byte
   frame with those same 80 bytes. This works for the recordings seen so far (**Confirmed**).
4. **Continuous keystream** (fallback, **Unverified**: which files need it is unknown): XOR the
   whole audio area with one continuous keystream.
5. Accept a result only if `_looks_like_opus()` passes: at least 160 bytes, frames 0 and 1 (and
   frame 8 for the per-frame layout) share one TOC byte, and that TOC is a CELT configuration
   (16–31). Current firmware writes TOC `0xB8` (configuration 23: CELT, wideband, 20 ms, mono, one
   frame per packet). If neither layout
   passes, `BleDecryptError` is raised and the encrypted original is kept.

Every recording can be decrypted offline with the APK key pair. No cloud call or account is
involved. Users who turned on Plaud's optional PIN-based E2EE reportedly get an extra per-user
encryption layer (**Unverified**, not supported).

### 9.3 After decryption

- `wrap_opus_ogg()` puts the frames in an Ogg container: `OpusHead` with 1 channel and 48 kHz,
  `OpusTags` with vendor `plaud-v2`, then 50 frames per page, 960 granule samples per frame.
- Duration in seconds = frames × 0.02. The file list estimate is `(size − 512) / 80 × 0.02`.
- `unwrap_ogg_to_raw_opus()` reverses the wrapping for Plaud Cloud uploads, which expect raw
  80-byte frames.

## 10. Plaud Cloud API

The owner no longer uses Plaud Cloud for recordings, but the app still calls it for device
credentials, and the cloud import, upload and trash features are still wired up in
`app/routes/cloud.py`.

Domains (`PLAUD_API_DOMAINS`): EU `https://api-euc1.plaud.ai`, global `https://api.plaud.ai`, China
`https://api.plaud.cn`. Web sign-in host: `https://web.plaud.ai`.

Headers (`cloud_api()`): `Authorization: bearer <token>`, `Content-Type: application/json`,
`app-platform: desktop`, `app-language: en`.

Region lock: a reply with `status` −302 or `msg` "user region mismatch" names the right API host in
`data.domains.api`. The app switches to it, saves it and retries once.

### 10.1 Sign-in

- Browser flow: open `https://web.plaud.ai/launch-desktop?from=desktop&desktop_uuid=<uuid>`. The
  callback gives an auth code. `POST /auth/access-token-auth-code` with
  `{"client_id": "web", "auth_code", "desktop_uuid"}` returns `access_token`.
- Or paste a token, or let the app read it from the official Plaud desktop app's LevelDB storage
  (`_extract_plaud_desktop_token()`, looks for `authToken` … `bearer eyJ…`).
- The token is a long-lived JWT.

### 10.2 Endpoints the app calls

| Method | Path | Use |
|---|---|---|
| GET | `/user/me` | Validate the token |
| GET | `/device/list` | Devices on the account (`data_devices`) |
| POST | `/device/bind` | `{"sn", "name", "model": "882", "version_number": 65546}` → `data.access_token` = `BIND_TOKEN` |
| POST | `/device/sn-sign` | `{"sns": [SN]}` → `data.signature[SN]` = `SN_SIG` (base64) |
| GET | `/file/simple/web` | List recordings (`skip`, `limit`, `is_trash=0`, `sort_by=start_time`, `is_desc=true`) |
| GET | `/file/temp-url/{id}?is_opus=1` | Presigned URL (`temp_url_opus` or `temp_url`) |
| POST | `/file/get_upload_presigned_url` | `{"filesize", "file_type"}` → `upload_id`, `object_name`, part URLs |
| PUT | part URLs | 5 MiB parts |
| POST | `/file/merge_multipart` | `{"upload_id", "object_name", "parts"}` |
| POST | `/file/confirm_upload` | `{"upload_id", "object_name", "scene", "file_type", "duration" (ms), "filename", "start_time" (ms)}` |
| POST | `/file/trash/` | List of cloud file IDs |

Cloud audio from `temp-url` is already decrypted. It comes as raw 80-byte Opus frames, as Ogg, or as
MP3 with an ID3 header (files the cloud calls "wav" are MP3). `download_cloud_recordings()` handles
all three.

Seen in the apps but not called (**APK**): `/device/check`, `/device/unbind`,
`/device/state/list?sn=`, `/device/report`, `/device/version/{model}`, `/file/version`,
`/file/get_s3_upload_link`, `/auth/access-token-logout`, `/ai/transsumm/{id}`, `/ai/mark/*`,
`/ai/chatllm/*`.

Firmware: in March 2026 `/device/version/{model}` returned "version not found" for models 880–890 on
every region, and the CDN had no firmware files. Firmware updates are not available.
