# Plaud NotePin S - Comprehensive BLE Protocol Analysis

**Source**: PacketLogger capture `plaud-connect-features-disconnect-ios-4.pklg`
**Date captured**: 2026-03-27, 08:44-08:48 (approximately 4 minutes of active capture)
**File size**: 3.6 MB, 23,067 records, 1,210 ATT operations
**Analysis date**: 2026-03-27

---

## 1. Connection Architecture Overview

The Plaud NotePin S uses a **dual-connection architecture** on iOS:

### System-Level Connection (iOS Bluetooth stack)
- **Address**: `XX:XX:XX:XX:XX:XX` (static, likely the device's identity/public address)
- **MTU**: 293 (iOS default)
- **Connection interval**: 30ms, latency=5, timeout=2000ms
- **Services discovered**: Generic Access (0x1800), Generic Attribute (0x1801), Device Information (0x180A), ANCS-related, Current Time Service (0x1805), Battery (0x180F), and several Apple-proprietary services
- **Activity**: Service Changed indication (`0x0008 = 0a00ffff`), CCCD writes for indications
- **Purpose**: iOS system bonding, ANCS notifications, device info, battery level

### App-Level Connection (Plaud app)
- **Address**: Rotating RPA (Resolvable Private Address) - changes each connection:
  - Session 1: `XX:XX:XX:XX:XX:XX`
  - Session 2: `XX:XX:XX:XX:XX:XX`
  - Session 3: `XX:XX:XX:XX:XX:XX`
  - Session 4: `XX:XX:XX:XX:XX:XX`
  - Session 5: `XX:XX:XX:XX:XX:XX`
- **MTU**: Requested 527, negotiated to **247** (device limit)
- **Connection interval**: Initially 15ms (fast), later adjusted
- **Services**: Custom Plaud service (0x1910) with protocol characteristics
- **Purpose**: All app communication - handshake, commands, file transfer

### Key GATT Structure (App Connection, Service 0x1910)

| Handle | UUID   | Properties | Purpose |
|--------|--------|-----------|---------|
| 0x0034 | 0xB001 | Read      | Status/info (never observed being read) |
| 0x0037 | 0x2BB1 | Write, Write-No-Response | **Command TX** (app -> device) |
| 0x0039 | 0x2BB0 | Notify    | **Command RX** (device -> app) |
| 0x003A | CCCD   | Write     | Enable notifications |
| 0x003C | 0xB004 | Indicate  | Secondary channel (not used in this capture) |
| 0x0040 | 0x2A19 | Read, Notify | Battery Level |

---

## 2. Connection Sessions Identified

### 11 Total Connections (5 app-level, 6 system-level)

| # | Handle | Type | Address | Time | Duration | Disconnect Reason |
|---|--------|------|---------|------|----------|-------------------|
| 1 | 0x0047 | System | XX:XX:XX:XX:XX:XX | 08:44:27 | 17.4s | Timeout |
| 2 | **0x0048** | **App** | XX:XX:XX:XX:XX:XX | 08:44:34 | 29.3s | **Local** |
| 3 | 0x0049 | System | XX:XX:XX:XX:XX:XX | 08:44:46 | 30.0s | Timeout |
| 4 | **0x004B** | **App** | XX:XX:XX:XX:XX:XX | 08:45:17 | 17.0s | **Local** |
| 5 | 0x004A | System | XX:XX:XX:XX:XX:XX | 08:45:17 | ~15s | (see note) |
| 6 | **0x004C** | **App** | XX:XX:XX:XX:XX:XX | 08:45:49 | 12.2s | **Local** |
| 7 | **0x0050** | **App** | XX:XX:XX:XX:XX:XX | 08:46:56 | 11.2s | **Timeout** |
| 8 | 0x0052 | System | XX:XX:XX:XX:XX:XX | 08:46:59 | 23.9s | Timeout |
| 9 | 0x0054 | System | XX:XX:XX:XX:XX:XX | 08:47:27 | 13.2s | Timeout |
| 10 | 0x0056 | System | XX:XX:XX:XX:XX:XX | 08:48:06 | 9.6s | Timeout |
| 11 | **0x0057** | **App** | XX:XX:XX:XX:XX:XX | 08:48:15 | 16.4s | **Remote** |

---

## 3. Protocol Handshake Sequence

Every app connection follows this exact sequence:

### Phase 0: BLE Connection Setup (~0.5s)
1. Connection parameter update request from device: 15ms interval, 0 latency, 5000ms timeout
2. GATT service discovery (ReadByGroupType)
3. Characteristic discovery (ReadByType)
4. Descriptor discovery (FindInformation)
5. Enable Service Changed indication: WriteReq to 0x0004 = `0200`
6. Enable notifications on 0x2BB0: WriteReq to 0x003A = `0100` (sent TWICE)

### Phase 1: SN Signature (0x10FE) - Always Sent
The app sends the **same** SN signature every connection:
```
Command: 0x10FE
Sequence: 3
Chunks: 3 (idx 0, 1, 2) - totaling 256 bytes
Data: 4ce6f81eeb853cc8... (IDENTICAL across all 5 sessions)
Sent via: WriteCommand (no response) to handle 0x0037
IMPORTANT: Sent TWICE (6 total writes) - appears to be intentional retry
```

### Phase 2: SN Signature ACK (0x11FE) - First Connection Only
**Only on the FIRST connection** (0x0048), the device responds:
```
Response: 0x11FE00 (3 bytes: cmd=0x11, sub=0xFE, status=0x00)
Via: Notification on handle 0x0039
```
On subsequent connections, this response is **skipped** - the device already knows this app.

### Phase 3: RSA Public Key Exchange (0x12FE)

**First connection (0x0048):**
1. App sends its RSA-2048 public key in PEM format:
   - Command 0x12FE, sequence=5, 5 chunks (450 bytes total)
   - Full PEM including `-----BEGIN PUBLIC KEY-----` / `-----END PUBLIC KEY-----`
2. Device responds with its RSA-2048 public key in binary (DER):
   - Command 0x12FE, sequence=3, 3 chunks (256 bytes total)
   - **Different key every connection** (ephemeral)

**Subsequent connections (0x004B-0x0057):**
1. App does NOT send its RSA key (device already has it cached)
2. Device sends a **new** ephemeral RSA key each time
3. No 0x11FE ACK needed

### Phase 4: Session Key Establishment
After RSA exchange, the first encrypted message is always **TX 58 bytes** followed by **RX 36 bytes**.
This is likely the encrypted session key exchange using the RSA keys.

### Handshake Timing

| Session | Handle | Duration |
|---------|--------|----------|
| First (full) | 0x0048 | 610ms |
| Reconnect | 0x004B | 398ms |
| Reconnect | 0x004C | 513ms |
| Reconnect | 0x0050 | 394ms |
| Reconnect | 0x0057 | 375ms |

**Key finding**: Reconnection handshake is ~35% faster because the app RSA key and SN_SIG_RESP are cached.

---

## 4. Encrypted Command Protocol

After the handshake, all traffic is AES-encrypted with session-specific keys derived from the RSA exchange.

### Command Structure (Encrypted)
Each encrypted packet has a **4-byte header** that appears to contain:
- Byte 0: Sequence/command counter (increments per session, wraps around)
- Bytes 1-3: Session identifier (same 3 bytes for all packets in a session)
  - Session 0x0048: `6aa31e`
  - Session 0x004B: `4019b3`
  - Session 0x004C: `e14a3d`
  - Session 0x0050: `e206f9`
  - Session 0x0057: `3659a8`

### Command Exchange Pattern (Identical Across All Sessions)

After the initial 58-byte/36-byte exchange, a **deterministic command sequence** follows:

```
TX 29 -> TX 26 -> TX 23 -> RX 29 -> RX 29 -> RX 55 ->
TX 23 -> RX 55 -> TX 23 -> TX 24 -> RX 25 -> RX 24 ->
TX 23 -> RX 38 -> TX 23 -> TX 26 -> RX 25 -> TX 23 ->
RX 29 -> TX 26 -> RX 24 -> TX 26 -> RX 29 -> ...
```

This pattern is nearly **identical across sessions 0x004B-0x0057**, suggesting these are:
1. Device status query (battery, storage, recording state)
2. File list request
3. Recording metadata sync
4. Configuration exchange
5. WiFi credentials/status

The first session (0x0048) has a slightly different pattern (TX 58, RX 36, TX 29, TX 26, **TX 23**... vs starting with RX), likely because it includes the full RSA key exchange step.

### Command Size Distribution
- **23 bytes**: Most common write (status queries, simple commands)
- **24-26 bytes**: Parameter commands
- **29 bytes**: Common response size
- **55 bytes**: Extended response (likely file metadata)
- **38 bytes**: File list entry
- **58 bytes**: Session init command

---

## 5. File Transfer Analysis

### Transfer Mechanism
Files are transferred as a stream of **190-byte notification packets** from the device to the app.
This uses the negotiated MTU of 247: 190 payload + 3 ATT header + overhead aligns with the ATT_MTU.

### File Transfer Chunk Format
Each 190-byte chunk has an 8-byte header:
- Byte 0: Sequence counter
- Bytes 1-3: Session identifier (same as encrypted commands)
- Bytes 4-7: Appear to be file/transfer identifiers (consistent within a transfer)

### Transfer Performance Comparison

| Session | Handle | Chunks | Bytes | Duration | Throughput | Type |
|---------|--------|--------|-------|----------|------------|------|
| 1 | 0x0048 | 231 | 43,890 | 10.80s | **31.7 kbps** | BLE (with interruptions) |
| 2 | 0x004B | 163 | 30,970 | 0.78s | **309.8 kbps** | BLE (fast burst) |
| 3 | 0x004C | 184 | 34,960 | 0.81s | **336.8 kbps** | BLE (fast burst) |
| 4 | 0x0050 | 12 | 2,280 | 0.05s | **387.2 kbps** | BLE (small file) |
| 5 | 0x0057 | 0 | 0 | - | - | No transfer |

### Critical Observation: Session 1 vs Others

**Session 1 (0x0048)** had dramatically slower transfer because:
- Inter-packet gaps up to **10.2 seconds** (!)
- Average gap: 47ms (vs 4-5ms for sessions 2-4)
- The connection parameter was at 15ms interval but suffered from the system-level connection on the same device competing for airtime
- The system connection (0x0047) timed out at 08:44:44, after which 0x0048's interval was briefly changed to 165ms (idle), then back to 15ms at 08:44:48

**Sessions 2-4 (0x004B, 0x004C, 0x0050)** achieved near-maximum BLE throughput:
- Inter-packet gap: 4-5ms average
- Maximum throughput: ~340-390 kbps
- These ran cleanly with the 15ms connection interval

### Flow Control
- Device sends notifications in bursts (multiple back-to-back)
- No explicit ACK protocol visible - the device just streams
- The encrypted command exchange before the bulk transfer likely negotiates the file and offset
- Small gaps (~800ms) appear periodically, possibly waiting for controller buffer space

---

## 6. Connection Parameter Management

### Device Requests (All from device, never from app)

| Time | Handle | Min | Max | Latency | Timeout | Context |
|------|--------|-----|-----|---------|---------|---------|
| 08:44:34 | 0x0048 | 15ms | 15ms | 0 | 5000ms | Initial fast mode |
| 08:44:45 | 0x0048 | 150ms | 165ms | 0 | 5000ms | **Idle/power save** |
| 08:44:48 | 0x0048 | 15ms | 15ms | 0 | 5000ms | Back to fast mode |
| 08:45:17 | 0x004B | 15ms | 15ms | 0 | 5000ms | Fast mode |
| 08:45:49 | 0x004C | 15ms | 15ms | 0 | 5000ms | Fast mode |
| 08:46:56 | 0x0050 | 15ms | 15ms | 0 | 5000ms | Fast mode |
| 08:48:15 | 0x0057 | 15ms | 15ms | 0 | 5000ms | Fast mode |
| 08:48:29 | 0x0057 | 150ms | 165ms | 0 | 5000ms | **Idle/power save** |

**Key finding**: The device switches to 150-165ms interval when idle (power saving), and back to 15ms when data transfer is needed. This is initiated by the **device**, not the app.

---

## 7. Disconnect Patterns

### Pattern 1: Graceful Local Disconnect
- **Sessions 0x0048, 0x004B, 0x004C**: Disconnect reason = `Local Host Terminated` (0x16)
- App initiates disconnect after file transfer completes
- Connection parameter switches to idle (150-165ms) before disconnect (seen in 0x0048, 0x0057)

### Pattern 2: Timeout (Range Loss / Device Disconnect)
- **Sessions 0x0050, 0x0052, 0x0054, 0x0056**: Disconnect reason = `Connection Timeout` (0x08)
- The system-level connections always timeout (never gracefully closed)
- This suggests the system BLE connection is maintained by iOS itself and only drops when the device goes out of range

### Pattern 3: Remote Disconnect
- **Session 0x0057**: Disconnect reason = `Remote User Terminated` (0x13)
- Device initiated the disconnect
- This followed a connection parameter change to idle mode (150-165ms)
- Likely represents the device saying "no more data, goodbye"

### Pattern 4: Failed Connection Attempts
- Handles 0x0046, 0x0053, 0x0058, 0x005A: `Connection Failed to Establish` (0x3E)
- These are iOS retrying connections that failed at the radio level
- iOS automatically retries until successful

---

## 8. Reconnection Behavior

### After disconnect (not erase):

1. **iOS automatically reconnects** the system-level connection to the known bonded device
2. The Plaud device's **RPA rotates** on each new advertisement cycle
3. iOS resolves the new RPA using the stored IRK (Identity Resolving Key) from initial pairing
4. The app connection uses the **same GATT handle layout** (0x0037, 0x0039, 0x003A) every time
5. Full GATT service discovery is performed each connection (no caching observed)
6. The handshake is **abbreviated**: SN signature sent (same data), device skips the ACK, sends new ephemeral RSA key
7. App does NOT resend its RSA public key (device caches it)

### Reconnection Timeline (typical):
```
T+0.0s  : LE Extended Connection established
T+0.3s  : Connection parameter update (15ms)
T+0.5s  : MTU exchange (527 -> 247)
T+0.5s  : GATT service discovery begins
T+1.0s  : CCCD writes (enable notifications)
T+1.0s  : SN signature sent (2x3 chunks)
T+1.3s  : Device RSA key received (3 chunks)
T+1.4s  : Session key exchange (TX 58, RX 36)
T+1.5s  : Command exchange begins
T+2.0s  : File transfer starts (if files pending)
```

Total reconnection to first command: **~1.5 seconds**

---

## 9. No SMP Pairing Observed

**Zero SMP packets** were found in the capture. This means:
- The devices were already bonded before this capture
- iOS stores the bonding information (LTK, IRK) and handles encryption transparently
- The encryption events in the HCI log (sub-event 0x0C with data `0202`) confirm LE encryption is active on every connection
- The app-level RSA handshake is an **additional layer** of application-level security on top of BLE encryption

---

## 10. WiFi Fast Transfer Protocol

### WiFi transfer is device-to-phone directly, NOT cloud-based

PacketLogger only captures Bluetooth HCI, not WiFi traffic, so the actual WiFi transfer isn't visible. From the decompiled app code, WiFi fast transfer works as follows:

1. App sends WiFi mode command over encrypted BLE (`action/connectWiFi` with token, SSID, password)
2. **Device creates a WiFi hotspot** (open network, 2.4GHz)
3. Phone connects to the device's WiFi hotspot directly
4. Files transfer over WiFi (TCP/HTTP) — **device-to-phone, no cloud involved**
5. After transfer, device exits WiFi mode and returns to BLE

From decompiled Flutter routes: `device-transfer-hotspot`, `device-transfer-setup-wifi`
BLE commands: `connectWiFi`, `getWiFiFileList`, `syncWiFiFile`, `extendWifiExitTime`

**Evidence**: Session 0x0057 (the last connection) had **zero BLE file transfer chunks** but the same command exchange pattern — consistent with the file transfer happening over WiFi instead of BLE:
- 17 notifications received (563 bytes) - command responses only
- 18 writes sent (484 bytes) - command exchanges (likely WiFi setup + file sync commands)
- The connection parameter switched to idle (150-165ms) then device disconnected remotely

**For Mac implementation**: We can trigger WiFi hotspot mode via BLE, connect the Mac to the device's hotspot, and download files much faster than BLE (potentially MB/s over WiFi vs ~33 KB/s over BLE).

---

## 11. Keepalive / Heartbeat

**No explicit keepalive packets observed.** The BLE connection is maintained by:

1. **Connection supervision timeout**: Set to 5000ms for app connections (10x higher than system's 2000ms)
2. **Empty PDU exchanges**: BLE controllers automatically send empty PDUs to keep the connection alive (below the HCI level, not visible in PacketLogger)
3. **Connection interval switching**: Device requests 150-165ms interval when idle (reduces power, still maintains connection)
4. **Connection duration**: Sessions lasted 10-30 seconds, all terminated by either:
   - Local disconnect (app chose to disconnect after transfer)
   - Timeout (device went out of range)
   - Remote disconnect (device initiated after completing work)

**The app does NOT keep connections open long-term.** It appears to connect, sync, transfer, disconnect.

---

## 12. File Sync Trigger

Based on the captured sessions:

1. **App-initiated**: The app connects, discovers the device, runs the handshake, then queries for pending files
2. **Device-driven notification**: The device does NOT push notifications to trigger sync - the app must poll
3. **Immediate after connect**: File transfer begins within 2-3 seconds of connection establishment
4. The command exchange before file transfer is likely:
   - Query device status / file list
   - Select file to transfer
   - Begin transfer at offset 0

---

## 13. Session-by-Session Reconstruction

### Session 1 (0x0048) - 08:44:34 to 08:44:59 (24.8s)
**First connection / Live recording scenario**
- Full handshake with RSA exchange (610ms)
- 45 encrypted commands sent, 283 notifications received
- File transfer: 231 chunks x 190 bytes = **43.9 KB** at 31.7 kbps (slow - system connection contention)
- Device requested idle interval at 08:44:45 (during transfer gap)
- App disconnected gracefully (Local)

### Session 2 (0x004B) - 08:45:18 to 08:45:29 (10.7s)
**Reconnect after disconnect / Delayed transfer**
- Abbreviated handshake (398ms)
- 19 encrypted commands, 184 notifications
- File transfer: 163 chunks x 190 bytes = **31.0 KB** at 309.8 kbps (fast!)
- App disconnected gracefully (Local)

### Session 3 (0x004C) - 08:45:49 to 08:45:54 (4.4s)
**Another reconnect / Fast transfer**
- Abbreviated handshake (513ms)
- 18 encrypted commands, 204 notifications
- File transfer: 184 chunks x 190 bytes = **35.0 KB** at 336.8 kbps (fastest BLE)
- App disconnected gracefully (Local)

### Session 4 (0x0050) - 08:46:56 to 08:47:05 (8.8s)
**Reconnect with small file**
- Abbreviated handshake (394ms)
- 20 encrypted commands, 31 notifications
- File transfer: 12 chunks x 190 bytes = **2.3 KB** at 387.2 kbps (small but fast)
- Connection timed out (device went out of range or was disconnected)

### Session 5 (0x0057) - 08:48:15 to 08:48:31 (15.8s)
**WiFi transfer setup or status check**
- Abbreviated handshake (375ms)
- 18 encrypted commands, 17 notifications (NO file transfer)
- All traffic is small command/response exchanges
- Device requested idle interval (150-165ms) then disconnected remotely
- This is consistent with either WiFi transfer initiation or a "no new files" response

---

## 14. Implications for Background Service

### Connection Strategy
1. **Use CoreBluetooth's connect-on-discovery**: iOS will automatically reconnect when the device is in range
2. **Handle RPA rotation**: Store the device's IRK from bonding, iOS resolves RPAs automatically
3. **The system-level connection is separate**: Your app won't see it or control it. Focus on the app-level connection only.

### Handshake Optimization
1. **Cache the SN signature** - it's the same every time (256 bytes)
2. **Cache the app's RSA public key** - only needed on first connection
3. **Expect new device RSA key each connection** - ephemeral keys for forward secrecy
4. **Handshake takes 375-610ms** - budget for this on every reconnect
5. **Send SN_SIG twice** (6 writes total) - this appears intentional, possibly for reliability

### File Transfer
1. **Use Write-Without-Response** (0x52) for TX, **Notifications** (0x1B) for RX
2. **190 bytes per notification** is the max payload with MTU 247
3. **Request MTU 527** even though device only supports 247 - let negotiation handle it
4. **Achievable throughput**: 310-390 kbps with 15ms connection interval
5. **Files stream as continuous notifications** - no explicit ACK protocol visible
6. **Connection parameter**: Device will request 15ms interval for transfers, 150-165ms when idle

### Disconnect Handling
1. **Graceful**: App disconnects after completing transfer (most common)
2. **Timeout**: Set supervision timeout to 5000ms to tolerate brief range loss
3. **Remote**: Device may disconnect when done (handle this gracefully)
4. **iOS auto-reconnect**: If using `connect()` without timeout, iOS will keep trying

### Background Execution
1. **No keepalive needed** - connections are short (5-30s for typical sync)
2. **No polling needed** - connect when device is in range, sync, disconnect
3. **State Restoration**: Use CoreBluetooth state restoration for background reconnection
4. **Budget ~2s** for handshake + command exchange before file transfer begins

---

## 15. Protocol Summary

```
Connection Established
    |
    v
MTU Exchange (527 -> 247)
    |
    v
GATT Discovery + CCCD Enable
    |
    v
SN Signature (0x10FE, 3 chunks x2)
    |
    v
[First time only: SN ACK (0x11FE) + App RSA Key (0x12FE, 5 chunks)]
    |
    v
Device RSA Key (0x12FE, 3 chunks, ephemeral)
    |
    v
Session Key Exchange (TX 58 bytes, RX 36 bytes)
    |
    v
Encrypted Command Exchange (~18-20 commands)
    |
    v
[If files pending: Bulk Notification Transfer (190-byte chunks)]
    |
    v
[Device switches to idle interval 150-165ms]
    |
    v
Disconnect (local, remote, or timeout)
```

---

## Appendix A: ATT Opcode Distribution

| Opcode | Name | Count |
|--------|------|-------|
| 0x52 | Write Command (no response) | 155 |
| 0x1B | Handle Value Notification | 735 |
| 0x12 | Write Request | 27 |
| 0x13 | Write Response | 27 |
| 0x10 | Read By Group Type Request | 63 |
| 0x11 | Read By Group Type Response | 51 |
| 0x08 | Read By Type Request | 37 |
| 0x09 | Read By Type Response | 32 |
| 0x04 | Find Information Request | 10 |
| 0x05 | Find Information Response | 10 |
| 0x1D | Handle Value Indication | 12 |
| 0x1E | Handle Value Confirmation | 12 |
| 0x02 | Exchange MTU Request | 11 |
| 0x03 | Exchange MTU Response | 11 |
| 0x01 | Error Response | 17 |

## Appendix B: Connection Handle to Device Address Mapping

| Handle | Address | Type | Plaud Device? |
|--------|---------|------|---------------|
| 0x0047 | XX:XX:XX:XX:XX:XX | System | Yes (identity addr) |
| 0x0048 | XX:XX:XX:XX:XX:XX | App | Yes (RPA) |
| 0x0049 | XX:XX:XX:XX:XX:XX | System | Yes (identity addr) |
| 0x004A | XX:XX:XX:XX:XX:XX | System | Yes (identity addr) |
| 0x004B | XX:XX:XX:XX:XX:XX | App | Yes (RPA) |
| 0x004C | XX:XX:XX:XX:XX:XX | App | Yes (RPA) |
| 0x0050 | XX:XX:XX:XX:XX:XX | App | Yes (RPA) |
| 0x0052 | XX:XX:XX:XX:XX:XX | System | Yes (identity addr) |
| 0x0054 | XX:XX:XX:XX:XX:XX | System | Yes (identity addr) |
| 0x0056 | XX:XX:XX:XX:XX:XX | System | Yes (identity addr) |
| 0x0057 | XX:XX:XX:XX:XX:XX | App | Yes (RPA) |

## Appendix C: Plaud Custom Service UUID

Service UUID: `0x1910` (handles 0x0032-0x003D)

Custom Service (on system connection):
- `87290102-3c51-43b1-a1a9-11b9dc38478b` (handles 0x000C-0x001E)
- `d0611e78-bbb4-4591-a5f8-487910ae4366` (ANCS-related)
- `9fa480e0-4967-4542-9390-d343dc5d04ae` (ANCS-related)

Registered Bluetooth SIG Service:
- `0xFD44` (handles 0x001F-0x002E) - Plaud registered company service
