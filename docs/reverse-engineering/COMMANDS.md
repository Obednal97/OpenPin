# BLE command reference

Every known BLE command (`cmd_type` 1), with request and reply layouts. Framing, encryption and the
pre-encryption handshake opcodes (65040–65042) are in [PROTOCOL.md](PROTOCOL.md). WiFi commands
are a separate namespace: see [PROTOCOL.md section 8](PROTOCOL.md#8-file-transfer-over-wifi).

Status tags are defined in the [README](README.md#status-legend):

- **Confirmed**: the app uses it and it has worked against a real NotePin S.
- **Implemented**: the app uses it; behaviour on hardware not yet confirmed.
- **APK**: read from the decompiled Android SDK only. The app doesn't use it.
- **Unverified**: a name or guess with no layout evidence.

Conventions:

- Offsets are into the decrypted plaintext: byte 0 is `cmd_type` (1), bytes 1–2 are `cmd_id`.
- A request written `[1,6,0]` is the 3-byte header with no payload.
- Reply layouts marked **APK** come from the Android SDK's reply classes (Plaud Android app v3.13.0,
  package `p279oh`). The SDK rejects a reply whose bytes 1–2 don't match the expected `cmd_id`.
  "jadx name" is the obfuscated class name in that build; it helps when re-decompiling the same
  version and means nothing in any other.
- The SDK's read helpers are `k()` u8, `l()` u16, `n()` u32, `o()` u64, and `m()`, whose width is
  unknown (it returns a long; see cmd 2).

## Summary

| cmd | Name (SDK) | App use | Status | jadx name |
|---:|---|---|---|---|
| 1 | Handshake (`HandshakeRsp`) | Every connect | Confirmed | C9774d0 |
| 2 | Get SSN (`GetSsnRsp`) | Device info | Confirmed | C9813x |
| 3 | Get state (`GetStateRsp`) | Connect, status, link probe | Confirmed | C9815y |
| 4 | Time sync (`TimeSyncRsp`) | Every connect | Confirmed | C9768a1 |
| 5 | Depair (`DepairRsp`) | Forget device | Implemented | C9795o |
| 6 | Storage (`StorageRsp`) | Connect, device info | Confirmed | C9800q0 |
| 8 | Common settings (`CommonSettingsRsp`) | — | APK | C9789l |
| 9 | Battery (`BattStatusRsp`) | Battery polling | Confirmed | C9779g |
| 10 | Open WiFi (`OpenWiFiRsp`) | WiFi sync | Confirmed (open) | C9776e0, C9783i |
| 13 | Close WiFi (`CloseWiFiRsp`) | — | APK | C9787k |
| 20 | Record start (`RecordStartRsp`) | Start recording | Implemented | C9784i0 |
| 21 | Record pause (`RecordPauseRsp`) | — | APK | C9780g0 |
| 22 | Record resume (`RecordResumeRsp`) | — | APK | C9782h0 |
| 23 | Record stop (`RecordStopRsp`) | Stop recording | Implemented | C9786j0 |
| 26 | File list (`GetRecSessionsRsp`) | Every sync | Confirmed | C9809v |
| 28 | Sync file head (`SyncFileHeadRsp`) | Start each download | Confirmed | C9806t0 |
| 29 | Sync file tail (`SyncFileTailRsp`) | End of each download | Implemented | C9808u0 |
| 30 | Stop sync (`SyncRecFileStopRsp`) | Interrupted download | Implemented | C9812w0 |
| 31 | Delete file (`SyncRecFileDelRsp`) | Auto-delete after sync | Implemented | C9810v0 |
| 33, 34 | LED status (`GetLedStatusRsp`) | — | APK | C9807u, C9790l0 |
| 36 | Angles (`AnglesRsp`) | Device info | Implemented | C9769b |
| 38 | Record tag (`RecordTagInfo`) | — | APK | C9811w |
| 50 | FOTA push (`AppFotaPushRsp`) | — | APK | C9775e |
| 51 | FOTA pack finish (`AppFotaPackFinishRsp`) | — | APK | C9771c |
| 52 | FOTA pack (`AppFotaPackRsp`) | — | APK | C9773d |
| 61 | Sync stats file (`SyncStatFileRsp`) | — | APK | C9814x0 |
| 103 | Privacy set (`PrivacySetRsp`) | — | APK | C9778f0 |
| 104 | Clear record files (`ClearRecordFileRsp`) | — | APK | C9785j |
| 108 | Raw UTF-8 passthrough | — | APK | C9781h |
| 109 | Find My (`FindMyRsp`) | Find My toggle | Implemented | C9766a |
| 122 | WiFi list (`GetWifiListRsp`) | — | APK | C9770b0 |
| 128 | Reset Find My (`ResetFindMyRsp`) | — | APK | C9788k0 |
| 141 | Device status (`GetDeviceStatusRsp`) | — | APK | C9803s |

Reply classes exist for these, but their payloads are not decoded (**Unverified**): 113 (C9799q),
117 (C9797p), 120 (C9767a0), 121 (C9794n0), 123 (C9816y0), 124 (C9818z0), 125 (C9793n),
130 (C9792m0), 131 (C9805t), 142–146 (C9801r, C9796o0, C9804s0, C9798p0, C9791m). Earlier notes
named some of them from APK method names: 121 WiFi config set, 123 WiFi idle sync test,
130 SoundPlus token, 131 SD/flash CID, 141–146 device logs.

The request formats of commands the app doesn't send are not known. The request builders live in
the SDK package `p257nh` and in the native `libtnt_ble_utils.so`; they were not decoded.

---

## Core

### cmd 1 — Handshake

**Confirmed.** Sent twice right after the encrypted channel is up (`connect_device()`,
`ble_connect_and_setup()`). Request layout follows the Android app (`nh/a0.java`, per a code
comment).

Request:

| Offset | Size | Value |
|---|---|---|
| 0–2 | 3 | `01 01 00` |
| 3 | 1 | 2 (protocol version) |
| 4 | 1 | 0 (audio channel) |
| 5 | 1 | Flag: 0 in the first request, 1 in the second. Both are required |
| 6 | 32 | `BIND_TOKEN`, ASCII, zero-padded. Longer tokens are truncated with a warning |

Reply (APK):

| Offset | Size | Field |
|---|---|---|
| 3 | 1 | Status |
| 4 | 2 | Port version |
| 6 | 1 | Time zone (hours) |
| 7 | 1 | Time zone minutes |
| 8 | 1 | Audio channel |
| 9 | 1 | WiFi supported (== 1) |
| 10 | 1 | Flag (named `noNsAgc` in earlier notes) |
| 11 | 1 | Flag (named `isOggAudio` in earlier notes) |
| len−4 | 1 | Version type, ASCII |
| len−3 | 3 | Version (u24) |

The app does not read the reply. It only logs whether each one arrived. A rejected token is
therefore not detected here; later commands just get no reply.

### cmd 2 — Get SSN

**Confirmed.** Request `[1,2,0]`. Used by `get_device_info()`.

| Offset | Size | Field |
|---|---|---|
| 3 | ≤56 | Serial number, ASCII, ends at the first 0 byte (searched up to offset 58) |
| 59 | 1 | Version type, ASCII |
| 60 | 3? | Version |

The SDK reads the version with `m()` and prints it as 4 decimal digits. The app reads 3 bytes (u24)
and gets 65546 on the test device, the same number `bind_device()` sends as `version_number`; a
u16 read would give 10. So u24 is probably right (**Unverified**). The app shows it as
`major.minor.patch` from the three bytes (`1.0.10`).

### cmd 3 — Get state

**Confirmed.** Request `[1,3,0]`. Used at connect, by `get_device_info()`, by the `/device/status`
route, and as the live link probe in `verify_connection()`.

| Offset | Size | Field | Notes |
|---|---|---|---|
| 3 | 4 | State | A bit field, not an enum. Seen: idle `1`, recording `0x1003` |
| 7 | 1 | Privacy on (== 1) | |
| 8 | 1 | Key state | App: `button_pressed` when 1 |
| 9 | 1 | USB connected (== 1) | |
| 10 | 1 | Scene | Seen: 0 idle, 1 recording. The app's names for 2–4 (Meeting, Call, Lecture) are guesses |
| 11 | 4 | Active session ID | 0 when idle |
| 15 | 1 | Find My state | |
| 16, 17 | 1 each | Unknown | Optional |

**Recording state comes from the session ID, not the state field** (since v2.2.3, checked live in
both states): `is_recording = sessionId != 0`. Before v2.2.3 the app read the state field as an
enum (0 idle, 1 recording, 2 paused, 3 resumed) and got it wrong both ways. The session ID seen
while recording (1780584913) is a plausible Unix start time, so session IDs may be timestamps
(**Unverified**).

### cmd 4 — Time sync

**Confirmed** (the device accepts it). Sent at every connect.

Request:

| Offset | Size | Value |
|---|---|---|
| 0–2 | 3 | `01 04 00` |
| 3 | 4 | Unix time, seconds (u32) |
| 7 | 1 | Time zone hours (signed) |
| 8 | 1 | Time zone minutes (signed) |

The app always sends time zone 0, 0 (UTC). Whether the device uses the time zone for anything is
**Unverified**.

Reply (APK): Unix time at 3 (u32), time zone at 7 (signed byte), `hasStatistics` at 8 (== 1,
optional).

### cmd 5 — Depair

**Implemented.** Request `[1,5,0]`. Reply (APK): status at 3.

`depair_device()` sends it, then re-sends `SN_SIG` and waits 5 s for a 65041 ACK. An ACK means the
device has forgotten the app's public key, which the app reports as `depair_verified`. It then
disconnects and clears saved sessions and paired devices.

### cmd 6 — Storage

**Confirmed** (free and total). Request `[1,6,0]`.

| Offset | Size | Field |
|---|---|---|
| 3 | 8 | Free bytes (u64) |
| 11 | 8 | Total bytes (u64) |
| 19 | 8 | Duration (u64, optional, units unknown) |

The app stores byte 19 as `storage_field3_raw`. Its remaining-time estimate assumes 4000 bytes per
second (80 bytes per 20 ms).

### cmd 8 — Common settings

**APK.** Generic get/set for device settings. Reply: type at 3 (u16), value at 5 (u32). Earlier
analysis of a fuller decompile listed setting types `AUTO_POWER_OFF` = 23 (off, 15 min, 30 min,
1 h, 5 h) and `BATTERY_MODE` = 32 (**Unverified**: that source is not kept). Request format
unknown. Not used.

### cmd 9 — Battery

**Confirmed** (v2.2.1). Request `[1,9,0]`. Read by `read_battery()` with a 3 s timeout.

| Offset | Size | Field |
|---|---|---|
| 3 | 1 | Charging (== 1) |
| 4 | 1 | Level, percent |

This is the battery source. Do not use GATT `0x2A19` (see [PROTOCOL.md](PROTOCOL.md#3-gatt)).

## WiFi hotspot

### cmd 10 — Open WiFi

**Confirmed** for opening: `[1,10,0]` makes the device start its hotspot. Reply (APK): status at 3.
The app doesn't read the reply.

The app closes the hotspot with `[1,10,0,0]` (`_close_hotspot()`). Whether that closes it is
**Unverified**: the SDK has a separate CloseWiFi reply (cmd 13), and the worker never sees the reply
to its close request.

### cmd 13 — Close WiFi

**APK.** Reply: status at 3. The request is probably `[1,13,0]` (**Unverified**). Not used.

## Recording

All **APK** reply layouts. The app sends `[1,20,0]` to start and `[1,23,0]` to stop and ignores the
replies (**Implemented**). Pause and resume are not used.

| cmd | Offset 3 | Then |
|---|---|---|
| 20 Start | Session ID (u32) | start position at 7 (u32), status at 11, scene at 12 (optional), start time at 13 (u32, optional) |
| 21 Pause | Session ID (u32) | reason at 7, file exists at 8, file size at 9 (u32) |
| 22 Resume | Session ID (u32) | same as 20 |
| 23 Stop | Session ID (u32) | same as 21 |

The device also sends start and stop replies on its own when the button is pressed. `send_cmd()`
ignores them unless it is waiting for that `cmd_id`.

cmd 38 Record tag (**APK**): two u16 values at 7 and 9, then tag entries of a timestamp (u32), type,
status and two reserved bytes. Not decoded further.

## File transfer

Flow and completeness rules: [PROTOCOL.md section 7](PROTOCOL.md#7-file-transfer-over-ble).

### cmd 26 — File list

**Confirmed.** Request `[1,26,0]`, 10 s timeout. The reply can span several notifications.

Per notification:

| Offset | Size | Field |
|---|---|---|
| 3 | 4 | Unknown |
| 7 | 2 | Total number of files (u16) |
| 9 | 2 | Index of this notification's first entry (u16) |
| 11 | | Entries |

Entry width depends on an integer the SDK is given, probably the port version (**Unverified**):

| SDK mode | Entry | Layout |
|---|---|---|
| 1 | 8 bytes | Session ID (u32), size (u32) |
| 2–6 | 9 bytes | + one byte |
| 7 and up | 10 bytes | + byte 8, byte 9 (meaning unknown; the WiFi list reads them as a u16 "codec") |

The app always parses 10-byte entries (right for the NotePin S), ignores bytes 8–9, skips entries
with session ID 0 or size outside 1 byte–500 MB, and removes duplicates. Unlike the SDK, it doesn't
check the start index at 9 against the entries it already has.

Size is the number of bytes the download should deliver, 512-byte header included (assumed; see
the open questions in the [README](README.md#open-questions)).

### cmd 28 — Sync file head (start download)

**Confirmed.**

| Offset | Size | Value |
|---|---|---|
| 0–2 | 3 | `01 1C 00` |
| 3 | 4 | Session ID |
| 7 | 4 | Start offset, always 0 |
| 11 | 4 | End offset = size from the file list |

Reply (APK): session ID at 3, status at 7. The app assumes 0 means accepted (**Unverified**). It
only treats a non-zero status as a refusal if no data arrives within 5 s.

### cmd 29 — Sync file tail

**Implemented.** Sent by the device at the end of a download, before or after the end-marker data
packet.

| Offset | Size | Field |
|---|---|---|
| 3 | 4 | Session ID |
| 7 | 2 | CRC-16 of the file (variant unknown) |

The CRC is logged against five candidate variants, never enforced (`CRC16_ENFORCED_VARIANT`).

### cmd 30 — Stop sync

**Implemented.** Request `[1,30,0]`, 2 s timeout. Sent when a download is interrupted while still
connected. Reply has no payload (APK).

### cmd 31 — Delete file

**Implemented.** Used only for auto-delete after an exact-size download.

Request: `[1,31,0]` + session ID (u32).

Reply (APK):

| Port version | Layout |
|---|---|
| ≥ 7 | Session ID at 3 (u32), status at 7 |
| < 7 | Status at 3 |

`delete_device_file()` waits 5 s and reports `deleted: true` only when the reply is at least 8
bytes, names the same session ID, and has status 0. Status meaning and whether the file is really
gone are **Unverified**.

### cmd 61 — Sync stats file

**APK.** Device statistics file. Earlier notes called it "OTA data push"; that was wrong.

## Settings and sensors

| cmd | Request | Reply | Status |
|---|---|---|---|
| 33, 34 LED status | unknown | on/off at 3, stored inverted (`!= 1`) | APK |
| 36 Angles | `[1,36,0]` | pitch at 3, roll at 7, yaw at 11 (4 bytes each; the SDK reads u32, the app reads signed). Units unknown | Implemented |
| 103 Privacy set | unknown | on/off at 3 | APK |
| 104 Clear record files | unknown | status at 3 | APK |
| 108 Passthrough | unknown | UTF-8 text from 3 | APK |
| 109 Find My | `[1,109,0, on]` with on = 0 or 1 | enabled at 3 | Implemented |
| 122 WiFi list | unknown | count at 3, then entries | APK |
| 128 Reset Find My | unknown | on/off at 3 | APK |
| 141 Device status | unknown | 4 opaque bytes at 3 (the SDK only logs them) | APK |

## Firmware update (FOTA)

**APK**, not implemented. No firmware is available from Plaud (see
[PROTOCOL.md section 10](PROTOCOL.md#10-plaud-cloud-api)).

| cmd | Reply |
|---|---|
| 50 FOTA push | uid at 3 (u32), status at 7, version type at 8 and version at 9 (optional) |
| 51 FOTA pack finish | uid at 3, status at 7 |
| 52 FOTA pack | uid at 3, two u32 values at 7 and 11 |

The SDK's OTA error and status code tables (`Constants$OtaPushError`,
`Constants$OtaUpgradeStatus`) are in git history (`docs/reverse-engineering/PROTOCOL.md` at commit
9738cc1).
