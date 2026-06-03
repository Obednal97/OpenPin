# Plaud NotePin S — Authoritative BLE/WiFi Command Reference

> **Status:** definitive. Supersedes the per-command tables in `PROTOCOL.md` where they
> disagree (notably **cmd 9**, which `PROTOCOL.md` mislabels as "Device Settings" — it is
> **Battery Status**; see [§7](#7-battery-the-corrected-story)).
>
> **How this was built (Mar 2026 RE + 2026-06 cross-check):** every command below is
> reconciled across four independent sources, in descending authority:
> 1. **Decompiled TinnoTech PenBLE SDK response classes** (`decompiled/ble-sdk/C97xx.java`)
>    — each class's `mo35995a()` returns its opcode and its constructor parses the exact
>    byte offsets. This is ground truth for **response** layouts.
> 2. **Raw iOS PacketLogger captures** (`BLE_CAPTURE_ANALYSIS.md`,
>    `BLE_COMPREHENSIVE_ANALYSIS.md`) — ground truth for the **wire** (handshake framing,
>    timings, real bytes).
> 3. **Our working Python** (`app/state/device.py`, `src/protocol.py`) — ground truth for
>    what we actually send and which offsets we read.
> 4. **`PROTOCOL.md`** — earlier hand analysis; correct in outline, wrong in places.
>
> Confidence is tagged per command: **SDK** (decompiled class verified) · **CAP** (seen in a
> capture) · **IMPL** (in our code) · **GUESS** (named only, layout unverified).

---

## 1. Transport & GATT characteristics

| UUID | Role |
|------|------|
| `00001910-…` | Primary service |
| `00002BB0-…` | **Notify** (device → host) |
| `00002BB1-…` | **Write** (host → device) |
| `0000B004-…` | **Indicate** — RSA key-exchange channel (pre-encryption) |
| `00002902-…` | CCCD (enable notifications) |
| `0000180F-…` | Battery **Service** (standard BLE SIG) |
| `00002A19-…` | Battery **Level** (standard, `uint8` 0–100%, read+notify) |

MTU is negotiated to **247** (iOS requests 527). All multi-byte integers are **little-endian**.

---

## 2. Packet framing

### Plaintext command/response (after the encryption envelope is stripped)
```
byte 0      cmd_type   1 = command/response, 2 = file data, 4 = BLE rate test, 5 = log
bytes 1-2   cmd_id     uint16 LE
byte 3+     payload    command-specific (see catalog)
```
The SDK reads fields with `TntBleCommUtils`: `k(b,o)`=u8, `l(b,o)`=u16 LE, `n(b,o)`=u32 LE,
`o(b,o)`=u64 LE. The response dispatcher (`AbstractC9777f`) **validates `bArr[1]` against the
expected `mo35995a()` opcode** — i.e. the official SDK matches each response to its request by
cmd_id. (Our Python does **not** — see [§9](#9-known-divergences--bugs).)

### Encryption envelope (every packet, both directions, after handshake)
```
byte 0      seq        uint32 LE sequence counter, prepended to plaintext BEFORE encryption
            └─ wire is: ChaCha20-Poly1305( nonce, plaintext = seq(4B) ‖ [cmd_type,cmd_id,payload], aad )
```
- **Cipher:** ChaCha20-Poly1305 AEAD (confirmed by SDK `crypto/*.java`, captures, and our impl).
  `BLE_COMPREHENSIVE_ANALYSIS.md` calling it "AES" is a transcription error.
- **Key (32B) / nonce (12B) / aad (12B)** are delivered in the RSA handshake blob (below).
- `app/state/device.py:644-673` `send_cmd()` increments `seq`, encrypts `seq ‖ payload`, writes
  to `2BB1`; the notify handler decrypts and strips the 4-byte seq prefix.

---

## 3. Handshake & key exchange

Two framings describe the **same** exchange; they differ only in how the leading bytes are named.

**As seen on the wire (`BLE_CAPTURE_ANALYSIS.md`, CAP):**
```
SN signature:  [seq][0xFE][0x03][chunk_idx][≤101B encrypted SN]   ×3 chunks, sent twice
RSA pubkey:    [seq][0xFE][0x05][chunk_idx][≤100B ASCII PEM]       ×5 chunks (≈450B PEM)
RSA response:  [seq][0xFE][0x03][chunk_idx][≤100B]                 ×3 chunks = 256B RSA-2048 blob
```
**As our code emits it (`app/state/device.py:465-497`, IMPL):**
```
SN sig:   struct.pack("<H", 65040) + [total_chunks, idx] + sig[idx*100:…]   # 0xFE10 LE = 10 FE
pubkey:   struct.pack("<H", 65042) + [total_chunks, idx] + pem[idx*100:…]   # 0xFE12 LE = 12 FE
```
`65040`/`65041`/`65042` are **our names** for the `0xFE…`-prefixed pre-encryption opcodes; on the
wire byte 1 is `0xFE` and byte 2 is the sub-id (`0x03` SN, `0x05` pubkey, `0x00` ACK). The raw BLE
opcode table lives in the device's native `libtnt_ble_utils.so`, not the Java layer, which is why
these don't appear in the decompiled SDK.

**RSA blob contents** (after decrypt with our static `keys/app_private.pem`, IMPL+CAP):
```
bytes 0-31    ChaCha20-Poly1305 key
bytes 32-43   nonce (12B, fixed per session)
bytes 44-55   aad   (12B, fixed per session)
bytes 56+     ciphertext → decrypts to ASCII "PLAUD.AI"  (identity check)
```
The device sends a **fresh ephemeral RSA key every connection**. After this succeeds, encryption is
active and all subsequent traffic uses the envelope in §2.

---

## 4. Authoritative opcode → response-class map (from SDK `mo35995a()`)

Every row verified by reading the class's opcode return and constructor. "—" = class exists but
payload not yet decoded; treat as a pointer for future work.

| cmd | Name (`toString`) | SDK class | Confidence |
|----:|-------------------|-----------|------------|
| 1 | HandshakeRsp | C9774d0 | SDK |
| 2 | GetSsnRsp | C9813x | SDK |
| 3 | GetStateRsp | C9815y | SDK |
| 4 | TimeSyncRsp | C9768a1 | SDK |
| 5 | DepairRsp | C9795o | SDK |
| 6 | StorageRsp | C9800q0 | SDK |
| 8 | **CommonSettingsRsp** | C9789l | SDK |
| 9 | **BattStatusRsp** | C9779g | SDK |
| 10 | OpenWiFiRsp | C9776e0 / C9783i | SDK |
| 13 | CloseWiFiRsp | C9787k | SDK |
| 20 | RecordStartRsp | C9784i0 | SDK |
| 21 | RecordPauseRsp | C9780g0 | SDK |
| 22 | RecordResumeRsp | C9782h0 | SDK |
| 23 | RecordStopRsp | C9786j0 | SDK |
| 26 | GetRecSessionsRsp (file list) | C9809v | SDK |
| 28 | SyncFileHeadRsp | C9806t0 | SDK |
| 29 | SyncFileTailRsp (CRC) | C9808u0 | SDK |
| 30 | SyncRecFileStopRsp | C9812w0 | SDK |
| 31 | SyncRecFileDelRsp | C9810v0 | SDK |
| 33 | GetLedStatusRsp | C9807u | SDK |
| 34 | GetLedStatusRsp | C9790l0 | SDK |
| 36 | AnglesRsp | C9769b | SDK |
| 38 | RecordTagInfo | C9811w | SDK |
| 50 | AppFotaPushRsp | C9775e | SDK |
| 51 | AppFotaPackFinishRsp | C9771c | SDK |
| 52 | AppFotaPackRsp | C9773d | SDK |
| 61 | SyncStatFileRsp | C9814x0 | SDK |
| 103 | PrivacySetRsp | C9778f0 | SDK |
| 104 | ClearRecordFileRsp | C9785j | SDK |
| 108 | (raw UTF-8 passthrough) | C9781h | SDK |
| 109 | FindMyRsp | C9766a | SDK |
| 113 | — | C9799q | GUESS |
| 117 | — | C9797p | GUESS |
| 120 | — | C9767a0 | GUESS |
| 121 | — | C9794n0 | GUESS |
| 122 | GetWifiListRsp | C9770b0 | SDK |
| 123 | — | C9816y0 | GUESS |
| 124 | — | C9818z0 | GUESS |
| 125 | — | C9793n | GUESS |
| 128 | ResetFindMyRsp | C9788k0 | SDK |
| 130 | — | C9792m0 | GUESS |
| 131 | — | C9805t | GUESS |
| 141 | GetDeviceStatusRsp | C9803s | SDK |
| 142–146 | — | C9801r, C9796o0, C9804s0, C9798p0, C9791m | GUESS |

---

## 5. Command details (request + response byte layouts)

Offsets are into the **plaintext** packet (byte 0 = cmd_type). "Req" = what the host sends after the
3-byte header; "Resp" = device reply. `our impl:` notes where our parsing matches or differs.

### Core / status

**cmd 1 — Handshake** · SDK+CAP+IMPL
- Req (IMPL): we send the 32-byte per-device **BIND_TOKEN** twice — `[1,1,0, 2,0,flag] + token32`
  with `flag` 0 then 1 (both required). (`PROTOCOL.md` claims a static APP_KEY here; we use the
  BIND_TOKEN — see `memory/feedback_ble_handshake`.)
- Resp (SDK C9774d0): `status@3`, `portVersion@4-5 u16`, `timezone@6`, `timezoneMin@7`,
  `audioChannel@8`, `supportWifi@9`, `noNsAgc@10`, `isOggAudio@11`, then `versionType` char +
  `version` u24 at the tail. Encryption begins for the response.

**cmd 2 — Get SSN** · SDK+IMPL — Req `[1,2,0]`.
- Resp: `serialNumber@3..58` (null-terminated ASCII, ≤56B), `versionType` char `@59`,
  `version@60-61 u16`. *our impl reads version as **u24** `@60-62` — minor over-read, harmless.*

**cmd 3 — Get State** · SDK+IMPL — Req `[1,3,0]`.
- Resp (SDK C9815y, confirmed against our parse): `state@3-6 u32` (0 idle/1 rec/2 paused/3 resume),
  `privacyEnable@7`, `keyState@8`, `usbState@9`, `scene@10`, `sessionId@11-14 u32`,
  `findMyState@15`, then optional `@16`,`@17`. *our impl matches exactly* (`device.py:912-935`).

**cmd 4 — Sync Time** · SDK+CAP+IMPL
- Req (IMPL): `[1,4,0] + ts(u32 LE) + tzHour(i8) + tzMin(i8)`.
- Resp (SDK C9768a1): `timestamp@3-6`, `timezone@7`, optional `hasStatistics@8`.

**cmd 5 — Depair** · SDK+IMPL — Req `[1,5,0]`; Resp `status@3` (0 = ok).

**cmd 6 — Storage** · SDK+IMPL — Req `[1,6,0]`.
- Resp (SDK C9800q0): `free@3-10 u64`, `total@11-18 u64`, `duration@19-26 u64`.
  *our impl reads free/total and labels `@19` "storage_field3_raw" — it is **duration** (seconds).*

**cmd 8 — Common Settings** · SDK — generic settings get/set (this is the real "settings" command,
not cmd 9). Resp (C9789l): `type@3-4 u16`, `value@5-8 u32`. The `SettingType` enum includes
`AUTO_POWER_OFF=23` (OFF/15m/30m/1h/5h) and `BATTERY_MODE=32`. *Not currently used by us.*

**cmd 9 — Battery Status** · SDK+IMPL — Req `[1,9,0]`.
- Resp (SDK **C9779g `BattStatusRsp`**): `charging@3` (bool), **`level@4`** (battery %, 0–100).
  ⚠️ **This is genuinely the battery** — see [§7](#7-battery-the-corrected-story).

### Recording

**cmd 20 — Record Start** · SDK — Resp: `sessionId@3-6`, `startPos@7-10`, `status@11`,
optional `scene@12`, `startTime@13-16`.
**cmd 21 — Record Pause** · SDK — `sessionId@3-6`, `reason@7`, `fileExist@8`, `fileSize@9-12`.
**cmd 22 — Record Resume** · SDK — `sessionId@3-6`, `start@7-10`, `status@11`, opt `scene@12`,`startTime@13-16`.
**cmd 23 — Record Stop** · SDK — `sessionId@3-6`, `reason@7`, `fileExist@8`, `fileSize@9-12`.
**cmd 38 — Record Tag Info** · SDK (C9811w) — tags/highlights; layout TODO.

### File transfer

**cmd 26 — Get File List** · SDK+IMPL — Req `[1,26,0]`. Multi-packet response.
- Resp header (SDK C9809v): `totalFiles@7-8 u16`, `packetIndex@9-10 u16`, entries from `@11`.
- **Entry size is firmware-version-dependent:** type 1 → 8B (`sessionId u32`,`fileSize u32`);
  types 2–6 → 9B (+`flag`); type 7+ → 10B (+2 bytes). *our impl assumes the 10B layout
  (`device.py` parses `sid u32`,`size u32`,`codec u16`) — correct for current firmware, but would
  misparse older devices.*

**cmd 28 — Sync File Start (head)** · SDK+IMPL
- Req (IMPL): `[1,28,0] + sessionId(u32) + startOffset(u32) + endOffset(u32)`.
- Resp (SDK C9806t0): `sessionId@3-6`, `status@7`.

**cmd 29 — Sync File End (tail)** · SDK — device→host: `sessionId@3-6`, `crc16@7-8`. Our notify
handler treats receipt of cmd 29 as the file-complete signal.
**cmd 30 — Stop Sync** · SDK+IMPL — Req `[1,30,0]`; empty response body.
**cmd 31 — Delete File** · SDK+IMPL — Req `[1,31,0] + sessionId(u32)`. Resp: `sessionId@3-6` then
`status@7` (or `status@3` on short replies). *our impl reads status at `r[7]` with `r[3]` fallback.*
**cmd 61 — Sync Stat File** · SDK (C9814x0) — device-statistics file sync.

### Settings / WiFi / misc (BLE layer)

**cmd 10 — Open WiFi** · SDK+IMPL — Resp `status@3`. *our impl uses `[1,10,0]` to open and
`[1,10,0,0]` to close; the SDK has a **separate cmd 13 = Close WiFi**. Our "close via cmd 10 + arg"
may or may not match firmware — verify.*
**cmd 13 — Close WiFi** · SDK — Resp `status@3`.
**cmd 33 / 34 — Get LED Status** · SDK — `onOff@3` (**inverted** logic: stored as `!=1`).
**cmd 36 — Angles / accelerometer** · SDK+IMPL — Req `[1,36,0]`. Resp: `pitch@3-6`, `roll@7-10`,
`yaw@11-14` (each i32 LE; units unconfirmed). *our impl matches.*
**cmd 103 — Privacy Set** · SDK (C9778f0).
**cmd 104 — Clear Record File** · SDK — `status@3`.
**cmd 108 — Raw passthrough** · SDK — body decoded as UTF-8 string from `@3`.
**cmd 109 — Find My** · SDK+IMPL — Req `[1,109,0, enable]`. Resp `enabled@3` (bool). *our impl
matches.*
**cmd 122 — Get WiFi List** · SDK (C9770b0) — scan results; layout TODO.
**cmd 128 — Reset Find My** · SDK (C9788k0).
**cmd 141 — Get Device Status** · SDK — `rawStatus@3-6` = 4 opaque bytes the SDK only logs as hex;
purpose undecoded. A possible richer status source worth probing.

### OTA / firmware

**cmd 50 — App FOTA Push** · SDK — `uid@3-6`, `status@7`, optional `versionChar@8`, `versionNum@9-10`.
**cmd 51 — App FOTA Pack Finish** · SDK (C9771c).
**cmd 52 — App FOTA Pack** · SDK (C9773d).
(`PROTOCOL.md` also references cmd 61 for "OTA data push" — that opcode is actually
`SyncStatFileRsp`; OTA data lives in the 50/51/52 family.)

---

## 6. File-data packets (`cmd_type = 2`)

Streamed on Notify during a sync (`device.py` notify handler + `src/protocol.py:189-213`):
```
byte 0       cmd_type = 2
bytes 1-4    sessionId   u32 LE
bytes 5-8    offset      u32 LE   (0xFFFFFFFF = end-of-file marker; byte 9 = error code)
byte 9       dataLen     u8
bytes 10+    payload     encrypted Opus frames (80 B / 20 ms / channel)
```
(Legacy `portVersion < 7`: `offset@1-4`, `dataLen@5`, data from `@6`.)

**Audio decryption** (see `memory/project_audio_decryption_breakthrough`): RSA-decrypt 256 B at file
offset `0x100` with `app_private.pem` → 32 B ChaCha20 key; per-frame keystream (counter 0, nonce
from `0x84`) XORed over each 80 B frame independently.

---

## 7. Battery — the corrected story

**Earlier conclusion was wrong.** I previously read `PROTOCOL.md` ("cmd 9 = Device Settings") and
concluded byte 4 was a settings value misread as battery. The decompiled SDK disproves that:

- **cmd 9 IS battery.** `C9779g` → `mo35995a()==9`, `toString()=="BattStatusRsp{charging,level}"`,
  parsing `charging@3` and **`level@4`**. The original `battery = r[4]` was reading the correct byte.
- The **generic settings** command is **cmd 8** (`CommonSettingsRsp`), not cmd 9.

So why did the device show a false ~90%? With the offset confirmed correct, the real suspects are:
1. **Response/request mismatch (most likely).** Our `send_cmd` returns the *first* `cmd_type==1`
   packet after clearing its event, with **no cmd_id check** ([§9](#9-known-divergences--bugs)). An
   unsolicited cmd_type-1 notify (e.g. a physical button-press `RecordStart/Stop`, a delayed reply)
   landing during a cmd-9 poll returns *that* packet, and its byte 4 (e.g. a `sessionId` byte) is
   shown as battery. The official SDK avoids this by matching responses to requests by opcode.
2. **Staleness** — device updates the value slowly.

**Implication for the fix already shipped:** I switched battery to the standard GATT `0x2A19`
characteristic. That source is independent and *does* bypass the buggy cmd-response path, so it can
incidentally fix a mismatch-induced wrong reading — but the **code comments I added are now
factually wrong** (they assert cmd 9 byte 4 is "a settings value, not charge level"). Those comments
should be corrected, and the proper root-cause fix is to add cmd_id matching in `send_cmd` (which
would make cmd 9 reliable again and fix wrong values across *all* commands).

Battery is available from three places: **cmd 9** (`charging`,`level`), **GATT 0x2A19** (standard
`uint8` %), and the **WiFi HeartbeatPing** response (`batteryLevel`,`batteryVoltage`,`charging`).

---

## 8. WiFi/JSON protocol (separate namespace)

When syncing over the device's WiFi hotspot, a **different** protocol runs over WebSocket with its
**own command numbers** — do not conflate with the BLE opcodes above
(`app/wifi_sync_worker.py`, SDK `sh/t0.java` + `ph/`,`qh/`).

```
bytes 0-2   total length (u24 LE)          bytes 6-7   JSON length (u16 LE)
byte  3     protocol version (=2)          bytes 8+    UTF-8 JSON payload
bytes 4-5   cmd_id (u16 LE)
```
Same ChaCha20-Poly1305 envelope (seq prefix) as BLE. WiFi command numbers (JSON layer):

| wifi cmd | Purpose |
|---------:|---------|
| 1 | Handshake |
| 3 / 16 | HeartbeatPing / Pong (ping carries batteryLevel, batteryVoltage, charging) |
| 11 | Get file list (`{uid,start,single}`) → `{count,status}` + 10-B entries |
| 12 | File sync start (`{session,scene,start,end}`) |
| 13 | File data chunk (device→host) |
| 14 | File delete |
| 15 | File sync stop |
| 20 / 21 | OTA control / OTA data |

BLE cmd 10/13 open/close the hotspot before/after the WiFi session.

---

## 9. Known divergences & bugs (our impl vs SDK)

| # | Finding | Where | Severity |
|---|---------|-------|----------|
| 1 | **No response/request opcode matching.** `send_cmd` returns the first `cmd_type==1` packet after `cmd_evt`; SDK matches `bArr[1]` to the requested opcode. Unsolicited/delayed packets corrupt the result (prime suspect for false battery). | `device.py:644-673` + notify handler | **High** |
| 2 | **cmd 9 mislabeled in PROTOCOL.md** as "Device Settings"; it is Battery Status. My battery-fix comments inherit this error and should be corrected. | `PROTOCOL.md`, `device.py` battery comments | Medium (docs) |
| 3 | **cmd 6 byte 19** is `duration`, not "unknown field 3". | `device.py:952` | Low |
| 4 | **cmd 26 entry size** is version-dependent (8/9/10 B); we hardcode 10 B. Fine for current firmware, breaks on older. | file-list parse | Low |
| 5 | **cmd 2 version** read as u24@60-62; SDK uses u16@60-61. | `device.py` SSN parse | Low |
| 6 | **WiFi close** uses `[1,10,0,0]`; SDK has a dedicated **cmd 13 CloseWiFi**. Verify our close actually takes effect. | `wifi_sync_worker.py` | Low |

---

## 10. Undecoded opcodes (future work)

Classes exist (so the opcodes are real) but payloads aren't decoded yet — pointers for next pass:
**113** (C9799q), **117** (C9797p), **120** (C9767a0), **121** (C9794n0), **123** (C9816y0),
**124** (C9818z0), **125** (C9793n), **130** (C9792m0), **131** (C9805t), **142–146**
(C9801r / C9796o0 / C9804s0 / C9798p0 / C9791m). Decode by reading each class constructor in
`docs/reverse-engineering/decompiled/ble-sdk/` (or the fuller `jadx-output/`). The raw BLE opcode
*request* table is in the device's native `libtnt_ble_utils.so`.
