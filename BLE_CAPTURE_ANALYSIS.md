# BLE Capture Analysis: Plaud NotePin S Protocol

> Source: `plaud-disconnect-reconnect-ios-2.pklg` (PacketLogger capture, 725,811 bytes)
> Captured from iOS connecting to a Plaud NotePin S device
> Total ATT operations: 1,012
> Total time span: ~91.4 seconds

---

## Capture Overview

The capture contains **3 distinct BLE connection sessions**, identified by MTU Exchange Requests:

| Session | Start Time | ATT Ops | Description |
|---------|-----------|---------|-------------|
| 1 | t=0.000s | 38 | Incomplete handshake (SN sign only, no ACK, disconnect) |
| 2 | t=18.865s | 58 | Full handshake with RSA + encrypted command exchange |
| 3 | t=28.017s | 916 | Full handshake + massive file transfer (~141 KB) |

---

## 1. Connection Setup (identical in all 3 sessions)

### 1.1 MTU Negotiation

```
[+0.000] SEND  Exchange MTU Request  MTU=527
[+0.023] RECV  Exchange MTU Response MTU=247
```

iOS requests MTU 527, device responds with 247. Effective payload per ATT PDU: 244 bytes.

### 1.2 GATT Service Discovery (Read By Group Type, UUID 0x2800)

| Handle Range | UUID | Service |
|-------------|------|---------|
| 0x0001-0x0004 | 1801 | GATT Service Changed |
| 0x0005-0x000B | 1800 | Generic Access |
| 0x000C-0x001E | 8b4738dc-b911-a9a1-b143-513c02012987 | Custom (unknown purpose) |
| 0x001F-0x002E | FD44 | Custom (Apple/unidentified) |
| 0x002F-0x0031 | 1804 | Tx Power |
| 0x0032-0x003D | **1910** | **Primary Plaud Data Service** |
| 0x003E-0xFFFF | 180F | Battery Service |

### 1.3 Characteristic Discovery (Read By Type, UUID 0x2803)

**Generic Access (0x1800):**
```
Handle 0x0007: Device Name = "Plaud NotePin S\0" (ASCII: 506c617564204e6f746550696e205300)
```

**Service Changed (0x1801):**
```
Char @0x0002: props=0x20(Indicate) val_handle=0x0003 UUID=2A05 (Service Changed)
```

**Plaud Data Service (0x1910) -- THE KEY SERVICE:**
```
Char @0x0033: props=0x02(Read)                  val_handle=0x0034 UUID=B001
Char @0x0036: props=0x0C(WriteNoResp,Write)     val_handle=0x0037 UUID=2BB1  <-- WRITE char
Char @0x0038: props=0x10(Notify)                val_handle=0x0039 UUID=2BB0  <-- NOTIFY char
Char @0x003B: props=0x20(Indicate)              val_handle=0x003C UUID=B004
```

**Battery Service (0x180F):**
```
Char @0x003F: props=0x12(Read,Notify) val_handle=0x0040 UUID=2A19 (Battery Level)
```

### 1.4 Descriptor Discovery and CCCD Writes

```
Find Info: handle 0x0004 = UUID 2902 (CCCD for Service Changed)
Find Info: handle 0x003A = UUID 2902 (CCCD for Notify char 2BB0)

Write Req handle=0x0004: 02 00  (Enable Indications on Service Changed)
Write Req handle=0x003A: 01 00  (Enable Notifications on 2BB0)
Write Req handle=0x003A: 01 00  (Enable Notifications on 2BB0 -- sent twice)
```

**Summary of setup sequence:**
1. MTU negotiation (527 requested, 247 granted)
2. Service discovery (Read By Group Type)
3. Read device name from handle 0x0007
4. Characteristic discovery (Read By Type)
5. Descriptor discovery (Find Information) for CCCDs
6. Enable indications on Service Changed (handle 0x0004)
7. Enable notifications on data char 2BB0 (handle 0x003A) -- written twice

---

## 2. SN Signature Exchange (Pre-handshake)

Immediately after CCCD writes, the app sends an "SN signature" -- an encrypted blob identifying the device serial number.

### 2.1 Packet Format

Written to handle **0x0037** (Write Cmd, no response):

```
Byte 0:    Sequence byte (0x10 for sessions 1&3, 0x20 for session 2)
Byte 1:    0xFE  (magic byte indicating pre-encryption command)
Byte 2:    0x03  (cmd_id for SN signature)
Byte 3:    Chunk index (0x00, 0x01, 0x02)
Bytes 4+:  Encrypted SN data
```

### 2.2 Captured SN Signature Data (Session 3)

The SN signature is **259 bytes** total, sent in 3 chunks:

```
Chunk 0 (101B): 4ce6f81eeb853cc84407c6cc091dd2165a9344ef6286e86c1e9d09a776f0
                0460d8a5ec3b148f6b172920d8950a18b7dee740494b8cfa12c9b632dec1
                aafe9ee67cf48fa8d108a595593b8d88563a6b6886636940be08703fcf89
                aea1dbd919538280a3fa
Chunk 1 (101B): f617c4094ca59e5e3050a64ee7620a626de1a3f3ac915447233c3897d527
                c207662dbf59b911fe2d2f2eb56eb8b3b7692859ac58d1ec49f0d0c41852
                5e113271f1938a84c2becc57ba924e3c80eea798719634f607d2f7e1a66d
                5870538652e1da220fef
Chunk 2 (57B):  2093a0263a7e339f6d02dec092d443a8dca18981337349d640e7d77f6102
                ea9776d38cbdd5b09615bd094fe59cdf6b99c72dc7777cde7cf3
```

**Key observations:**
- The SN signature data is **IDENTICAL across all 3 sessions** (only the sequence byte changes)
- The signature is sent **twice** in each session (retry/redundancy mechanism)
- Total: 6 Write Cmds per session for SN (3 chunks x 2 attempts)

### 2.3 Device Response to SN Signature

**Session 1 (failed):** Device responds with SN_SIGN_RESP but no ACK:
```
[+0.759] RECV Notify 0x0039: seq=0x12 cmd=0xFE subcmd=0x03 chunk=0x00 (104B)
[+0.759] RECV Notify 0x0039: seq=0x12 cmd=0xFE subcmd=0x03 chunk=0x01 (104B)
[+0.761] RECV Notify 0x0039: seq=0x12 cmd=0xFE subcmd=0x03 chunk=0x02 (60B)
```
The device returns its own SN signature (different data, 3 chunks, same 0xFE/0x03 format).
Session 1 ends with the app disabling notifications (Write Req 0x003A = 0x0000) at t=7.419s.

**Sessions 2 & 3 (successful):** Device responds with ACK:
```
[+0.192] RECV Notify 0x0039: 11 FE 00  (3 bytes: seq=0x11, cmd=0xFE, subcmd=0x00 = ACK)
```
The `0x00` subcmd indicates ACK/success, allowing the handshake to proceed.

---

## 3. RSA Public Key Exchange

After receiving the SN ACK, the app sends its RSA-2048 public key.

### 3.1 RSA Key Send Format

Written to handle **0x0037**:
```
Byte 0:    Sequence byte (0x12)
Byte 1:    0xFE  (magic byte)
Byte 2:    0x05  (cmd_id for RSA public key)
Byte 3:    Chunk index (0x00-0x04)
Bytes 4+:  ASCII PEM key data (100 bytes per chunk, 50 for last)
```

### 3.2 Captured RSA Public Key

5 chunks, 450 bytes total:
```
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzkzQ59vIMXQpL7CRs8Py
8cCaJDJZJ2UqiqEMf3Y8NAiYvlUgtZ1/+k9S+7RvAMZeU5jHuXCa2gVB04+q4qp7
OczRFCo49mpkWZuDk96U8/CX6Pr43HBEmtY/uZ3tZ0YuEmJPgsjeGCF0IXaILbCj
4YNVEd9xxQoSGaDB9BMUL2loSFHiIs7VkElkBS4ibf7MkEDmY8CG1bnmqmU46n+K
QfE4wVQxAWbxmuTeauWnlcJ4gmNH5oV+V4IXDM3gLqhTu4R5sNRPO4PtiY5EEMui
Hgqkb2nP2ltXfyGcmS6IG1o+BmR8OiUC5hhBfpOxBSGaEPb9t00wgYyhOud8XBQO
9QIDAQAB
-----END PUBLIC KEY-----
```

This is a standard RSA-2048 public key in PEM format. The full PEM (including headers) is sent.

### 3.3 Device RSA Response

The device sends back an RSA-encrypted blob (encrypted with the app's public key):

```
[+0.315] RECV Notify 0x0039: seq=0x12 cmd=0xFE subcmd=0x03 chunk=0x00 (100B data)
[+0.330] RECV Notify 0x0039: seq=0x12 cmd=0xFE subcmd=0x03 chunk=0x01 (100B data)
[+0.330] RECV Notify 0x0039: seq=0x12 cmd=0xFE subcmd=0x03 chunk=0x02 (56B data)
```

Total: **256 bytes** of RSA-encrypted data (matches RSA-2048 output size).

When decrypted with the app's private key, this blob contains:
- ChaCha20-Poly1305 key (32 bytes)
- Nonce (12 bytes)
- Additional authenticated data (12 bytes)
- Ciphertext (remaining bytes, decrypts to "PLAUD.AI" for verification)

---

## 4. Encrypted Communication

After the RSA exchange, ALL subsequent communication on handles 0x0037/0x0039 is encrypted with ChaCha20-Poly1305.

### 4.1 Encrypted Message Format

Every encrypted message on the wire follows this structure:
```
Byte 0:    Sequence number (wrapping counter)
Bytes 1+:  ChaCha20-Poly1305 encrypted payload
```

The encrypted payload, when decrypted, contains the Plaud protocol command:
```
Byte 0:    cmd_type (1=command, 2=file_data, 4=BLE_rate_test, 5=log_data)
Byte 1-2:  cmd_id (uint16 LE)
Bytes 3+:  Command-specific payload
```

### 4.2 Session 2 Encrypted Exchange (command-only, no file transfer)

Session 2 bytes[1:4] pattern: `D764E7` (per-session constant, part of encryption).

After the RSA handshake completes at t=19.844s:
```
[19.844] SEND 0x0037: seq=0xC9 (58B encrypted) -- First handshake cmd
[19.910] RECV 0x0039: seq=0xCA (36B encrypted) -- Response
[19.913] SEND 0x0037: seq=0xC8 (29B encrypted) -- Second handshake cmd
[19.942] SEND 0x0037: seq=0xCF (26B encrypted) -- Sync time
[19.976] SEND 0x0037: seq=0xCE (23B encrypted) -- Get state
... (bidirectional encrypted commands) ...
[20.430] SEND 0x0037: seq=0xC2 (24B encrypted) -- Last command before disconnect
```

The first encrypted message (58B) is likely the handshake command (`cmd_type=1, cmd_id=1`) carrying the APP_KEY `0b1ef6247326fc79b200163e0a0431dc`.

### 4.3 Session 3 Encrypted Exchange (with file transfer)

Session 3 bytes[1:4] pattern: `122066` (different session key).

Post-RSA encrypted commands (t=29.029s - t=41.095s):
- 46 encrypted SEND messages (device commands)
- 827 encrypted RECV messages total
  - 53 small encrypted responses (command responses)
  - 774 large 190-byte notifications (file data)

Sequence of encrypted commands (probable mapping based on sizes and timing):
```
[29.029] SEND (58B) -- Handshake (cmd_id=1 with APP_KEY, 32-byte key + 1B channel)
[29.094] RECV (36B) -- Handshake response (status + port_version)
[29.099] SEND (29B) -- Sync time (cmd_id=4, timestamp + tz)
[29.129] SEND (26B) -- Get state (cmd_id=3)
[29.161] SEND (23B) -- Get SSN (cmd_id=2)
[29.194] SEND (23B) -- ... more queries
... continuing with 30+ command-response pairs ...
[31.274] SEND (24B) -- Last pre-file-list command
```

From t=40.795s, the device begins sending file data (large 190B notifications start appearing at t=41.095s).

---

## 5. File Transfer

### 5.1 Overview

File transfer begins in Session 3 at approximately t=41.095s and ends at t=72.381s.

| Metric | Value |
|--------|-------|
| Transfer start | t=41.095s |
| Transfer end | t=72.381s |
| Duration | 31.3 seconds |
| 190-byte chunks received | 774 |
| Estimated raw data | ~141.3 KB |
| Throughput | ~4.5 KB/s |

### 5.2 File Data Notification Format (Encrypted)

Each 190-byte notification on handle 0x0039:
```
Byte 0:     Sequence number
Bytes 1+:   ChaCha20 encrypted payload (189 bytes)
```

When decrypted, the payload follows the file data protocol:
```
Byte 0:     cmd_type = 2 (file data)
Bytes 1-4:  session_id (uint32 LE)
Bytes 5-8:  offset (uint32 LE)
Byte 9:     data_length (uint8)
Bytes 10+:  file data (Opus audio)
```

### 5.3 File Data Header Pattern

Looking at the encrypted (wire) headers of 190B chunks:
```
seq=0x3C hdr: 3c 12 20 66 02 79 75 70 c3 97
seq=0x3D hdr: 3d 12 20 66 02 79 75 70 c3 37
seq=0x3E hdr: 3e 12 20 66 02 79 75 70 c3 d7
seq=0x35 hdr: 35 12 20 66 02 79 75 70 c3 77
```

Note: bytes[1:4] = `122066` (session encryption constant), byte[4] = `02` (likely cmd_type=2 after XOR/encryption artifact).

### 5.4 ACK Messages During File Transfer

The app sends 16 ACK/flow-control messages back to the device during the 31-second file transfer:

```
[41.102] SEND (23B)  -- ACK for first batch of file chunks
[41.130] SEND (26B)
[41.162] SEND (23B)
[41.195] SEND (26B)
[41.222] SEND (26B)
[41.256] SEND (26B)
[43.151] SEND (27B)  -- gap ~2s (processing?)
[43.192] SEND (35B)
[43.852] SEND (35B)
[71.438] SEND (23B)  -- near end of transfer
[71.469] SEND (32B)
[71.847] SEND (35B)
[72.204] SEND (35B)
[72.418] SEND (27B)  -- final ACK
[87.172] SEND (26B)  -- post-transfer command (15s after last data)
[91.372] SEND (24B)  -- final command (~19s later)
```

The file transfer protocol appears to use windowed flow control: the device sends batches of ~10-15 chunks, then waits for ACKs.

### 5.5 End of File Transfer

The last file data chunks arrive at t=72.381s. The final notifications include:
- Multiple 190B chunks with repeating padding pattern (likely zero-padded final sector)
- A 62B chunk (shorter than normal -- final partial chunk)
- A final 190B chunk and a 29B control message

After the file transfer, there are two more SEND commands:
```
[87.172] SEND (26B) -- ~15s after last data (possible file sync end / cmd_id=29 or 30)
[91.372] SEND (24B) -- ~19s later (possible disconnect/cleanup)
```

---

## 6. Session 1 Analysis (Failed Connection)

Session 1 demonstrates what happens when the SN signature exchange fails:

```
[0.000]  MTU exchange (527 -> 247)
[0.025]  Service discovery
[0.118]  Characteristic discovery
[0.204]  Enable indications on Service Changed (0x0004 = 0x0200)
[0.354]  Enable notifications on 2BB0 (0x003A = 0x0100, sent twice)
[0.414]  SEND SN signature (3 chunks, seq=0x10)
[0.479]  SEND SN signature retry (3 chunks, seq=0x10, identical data)
[0.759]  RECV SN signature from device (3 chunks, 0xFE/0x03)
          -- Device responds with its OWN SN signature instead of ACK (0xFE/0x00)
          -- NO RSA exchange follows
[7.419]  SEND disable notifications (0x003A = 0x0000) -- app gives up
```

The app waits ~7 seconds after the device's SN_SIGN_RESP before disabling notifications and disconnecting. This suggests the device responded with its SN signature (mutual auth attempt?) but the app expected an ACK.

---

## 7. Complete Protocol Sequence (Session 3 -- Successful)

```
t=28.017  MTU: 527 -> 247
t=28.045  Service discovery (Read By Group Type x3)
t=28.165  Characteristic discovery (Read By Type x5)
t=28.226  Descriptor discovery (Find Info x2)
t=28.256  CCCD: Enable indications on 0x0004 (Service Changed)
t=28.406  CCCD: Enable notifications on 0x003A (data char 2BB0, x2)
t=28.496  SN Sign: 3 chunks to 0x0037 [0xFE][0x03][chunk_idx][259B encrypted data]
t=28.563  SN Sign: retry (3 chunks, identical)
t=28.689  SN ACK: 3 bytes from 0x0039 [0x11][0xFE][0x00]
t=28.692  RSA Key: 5 chunks to 0x0037 [0xFE][0x05][chunk_idx][PEM ASCII]
          (450 bytes total = full PEM public key with headers)
t=29.006  RSA Response: 3 chunks from 0x0039 [0xFE][0x03][chunk_idx][256B RSA-encrypted blob]
          (256 bytes = RSA-2048 encrypted ChaCha20 params)
t=29.029  >> Encryption active (ChaCha20-Poly1305) <<
t=29.029  First encrypted cmd (58B) -> Handshake cmd (cmd_id=1 with APP_KEY)
t=29.094  Handshake response (36B)
t=29.099  Sync time (cmd_id=4)
t=29.129+ Device state queries, file list request, etc.
t=41.095  File transfer begins (190B encrypted data chunks)
t=72.381  File transfer ends (774 chunks, ~141 KB)
t=87.172  Post-transfer command
t=91.372  Final command
```

---

## 8. Handle Map Summary

| Handle | UUID | Direction | Purpose |
|--------|------|-----------|---------|
| 0x0003 | 2A05 | RECV (Indicate) | Service Changed |
| 0x0004 | 2902 | SEND (Write Req) | CCCD for Service Changed |
| 0x0007 | 2A00 | READ | Device Name ("Plaud NotePin S") |
| 0x0034 | B001 | READ | Unknown (not accessed in capture) |
| **0x0037** | **2BB1** | **SEND (Write Cmd)** | **App -> Device commands** |
| **0x0039** | **2BB0** | **RECV (Notify)** | **Device -> App responses/data** |
| 0x003A | 2902 | SEND (Write Req) | CCCD for 2BB0 notifications |
| 0x003C | B004 | RECV (Indicate) | Unknown (not used in capture) |
| 0x0040 | 2A19 | READ | Battery Level |

---

## 9. Pre-Encryption Command IDs (0xFE prefix)

| Byte 2 (subcmd) | Direction | Purpose |
|------------------|-----------|---------|
| 0x00 | RECV | SN ACK (handshake approved) |
| 0x03 | SEND/RECV | SN Signature (encrypted serial number, 259 bytes in 3 chunks) |
| 0x05 | SEND | RSA Public Key (PEM, 450 bytes in 5 chunks) |

### Chunk framing for pre-encryption commands:
```
[sequence_byte] [0xFE] [subcmd] [chunk_index] [data...]
```

Max chunk payload: 101 bytes (fits in 104B with 3-byte header on a 244B ATT payload).

---

## 10. Post-Encryption Command IDs (from decompiled source)

Once ChaCha20-Poly1305 is active, all messages are encrypted. The plaintext follows:
```
[cmd_type: u8] [cmd_id: u16 LE] [payload...]
```

| cmd_type | cmd_id | Name | Purpose |
|----------|--------|------|---------|
| 1 | 1 | Handshake | APP_KEY + audio_channel + serial_number |
| 1 | 2 | Get SSN | Get device serial number |
| 1 | 3 | Get State | Recording state, privacy, USB state |
| 1 | 4 | Sync Time | Timestamp + timezone |
| 1 | 5 | Depair | Remove pairing |
| 1 | 6 | Storage Op | Get storage info |
| 1 | 9 | Device Settings | Configuration |
| 1 | 13 | Common Settings | General settings |
| 1 | 20 | Start Record | Begin recording |
| 1 | 21 | Pause Record | Pause recording |
| 1 | 22 | Resume Record | Resume recording |
| 1 | 23 | Stop Record | Stop recording |
| 1 | 26 | Get File List | List recordings |
| 1 | 28 | Sync File Start | Begin file transfer (session_id, offset, end) |
| 1 | 29 | Sync File End | End file transfer |
| 1 | 30 | Stop Sync File | Abort file transfer |
| 1 | 31 | Delete File | Delete recording |
| 1 | 33 | Record Tags | Set recording markers/highlights |
| 2 | - | File Data | [session_id u32][offset u32][len u8][data...] |

---

## 11. Encryption Details

### RSA Key Exchange
- Algorithm: RSA-2048
- Padding: PKCS1v15 (`RSA/ECB/PKCS1Padding` in Java)
- Key format: Full PEM with `-----BEGIN/END PUBLIC KEY-----` headers
- Response: 256 bytes RSA-encrypted blob

### RSA Blob Contents (after decryption)
```
Bytes 0-31:   ChaCha20-Poly1305 key (32 bytes)
Bytes 32-43:  Nonce (12 bytes)
Bytes 44-55:  Additional Authenticated Data (12 bytes)
Bytes 56+:    ChaCha20-Poly1305 ciphertext (decrypts to "PLAUD.AI")
```

### Subsequent Encryption
- Algorithm: ChaCha20-Poly1305 (AEAD)
- Key: 32 bytes from RSA blob
- Nonce: 12 bytes from RSA blob (appears to be reused per-session)
- AAD: 12 bytes from RSA blob

### Wire Format (encrypted)
```
[sequence_byte: u8] [ChaCha20-Poly1305 encrypted payload]
```

The sequence byte is NOT encrypted and increments/wraps for each message.

---

## 12. File Transfer Protocol Details

### Request
```
cmd_type=1, cmd_id=28 (Sync File Start)
Payload: [session_id: u32 LE] [start_offset: u32 LE] [end_offset: u32 LE]
```

### Data Chunks (from device)
```
cmd_type=2 (File Data)
[session_id: u32 LE] [offset: u32 LE] [data_length: u8] [data: N bytes]
```

When `offset == 0xFFFFFFFF`, it signals end of transfer.

### Flow Control
- Device sends bursts of ~10-15 chunks (~1.9 KB per burst)
- App sends ACK after each burst
- Transfer rate: ~4.5 KB/s over BLE
- Total transfer time for ~141 KB: ~31 seconds

### File Format
- Audio codec: Opus
- Frame size: 80 bytes per 20ms frame per channel
- Duration calculation: `(file_size / (channels * 80)) * 20ms`

---

## 13. Notes on Session Differences

### Session 1 (t=0.0s) -- SN signature mismatch
- SN signature sent and retried
- Device responds with its own SN signature (mutual authentication?)
- No ACK received; app disables notifications and disconnects after 7s
- Possible cause: device was paired to a different phone/account

### Session 2 (t=18.9s) -- Successful handshake, no file transfer
- Full SN -> ACK -> RSA -> encrypted handshake
- Encrypted command exchange (sync time, get state, etc.)
- Session terminates after ~1.5s of encrypted traffic
- No file transfer (probably just a sync/check session)

### Session 3 (t=28.0s) -- Full session with file download
- Complete handshake identical to Session 2
- Extended encrypted command exchange (~12s)
- File transfer of ~141 KB (~31s)
- Post-transfer cleanup commands over ~19s

---

## Appendix A: Raw SN Signature Bytes

All sessions send identical SN data (259 bytes across 3 chunks):

```
Chunk 0 (101B):
4ce6f81eeb853cc84407c6cc091dd2165a9344ef6286e86c1e9d09a776f0
0460d8a5ec3b148f6b172920d8950a18b7dee740494b8cfa12c9b632dec1
aafe9ee67cf48fa8d108a595593b8d88563a6b6886636940be08703fcf89
aea1dbd919538280a3fa

Chunk 1 (101B):
f617c4094ca59e5e3050a64ee7620a626de1a3f3ac915447233c3897d527
c207662dbf59b911fe2d2f2eb56eb8b3b7692859ac58d1ec49f0d0c41852
5e113271f1938a84c2becc57ba924e3c80eea798719634f607d2f7e1a66d
5870538652e1da220fef

Chunk 2 (57B):
2093a0263a7e339f6d02dec092d443a8dca18981337349d640e7d77f6102
ea9776d38cbdd5b09615bd094fe59cdf6b99c72dc7777cde7cf3
```

## Appendix B: Session 1 Device SN Response (different from app SN)

```
Chunk 0 (101B): 7a79edd7d48b9602d872425ca5984c95c3e61e3986b70c9f5cb96b49...
Chunk 1 (101B): 6a0bea160dfc0bdaa4411f464715b381fb8eb841fede4993f84af071...
Chunk 2 (57B):  d119b28c0ade72669f5ec2ec91bd0a1caa97e3dd82e74ac81bc9097f...
```

## Appendix C: RSA Public Key (full PEM)

```
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzkzQ59vIMXQpL7CRs8Py
8cCaJDJZJ2UqiqEMf3Y8NAiYvlUgtZ1/+k9S+7RvAMZeU5jHuXCa2gVB04+q4qp7
OczRFCo49mpkWZuDk96U8/CX6Pr43HBEmtY/uZ3tZ0YuEmJPgsjeGCF0IXaILbCj
4YNVEd9xxQoSGaDB9BMUL2loSFHiIs7VkElkBS4ibf7MkEDmY8CG1bnmqmU46n+K
QfE4wVQxAWbxmuTeauWnlcJ4gmNH5oV+V4IXDM3gLqhTu4R5sNRPO4PtiY5EEMui
Hgqkb2nP2ltXfyGcmS6IG1o+BmR8OiUC5hhBfpOxBSGaEPb9t00wgYyhOud8XBQO
9QIDAQAB
-----END PUBLIC KEY-----
```
