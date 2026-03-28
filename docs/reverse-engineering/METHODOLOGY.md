# Reverse Engineering Methodology

How the Plaud NotePin S BLE protocol, audio format, and cloud API were reverse engineered.

## Sources

### 1. Android APK (Plaud v3.13.0)

The primary source. The APK was decompiled using [jadx](https://github.com/skylot/jadx) to recover the Java/Kotlin source code.

**Key packages discovered:**
- `p279oh` — TinnoTech PenBLE SDK (BLE command protocol, handshake, file transfer)
- `p083ed` — Encryption layer (RSA key exchange, ChaCha20-Poly1305 session, file decryption)
- `p225lh` — Protocol interfaces and constants (command IDs, state machine)

These decompiled files are preserved in `decompiled/` — they are the authoritative source for the BLE protocol implementation.

**What we learned from the APK:**
- BLE GATT service/characteristic UUIDs (0x2BB0 notify, 0x2BB1 write)
- RSA handshake flow (SN signature → ACK → public key exchange → session key derivation)
- ChaCha20-Poly1305 encrypted command channel
- Command IDs for all operations (scan, connect, file list, file sync, record, depair)
- Audio file format (PLAUD.AI magic, RSA-encrypted per-file key, ChaCha20 XOR'd Opus frames)
- Cloud API endpoints and authentication flow

**Native libraries identified:**
- `libtnt_ble_utils.so` — BLE protocol utilities, CRC, integer packing (JNI)
- `libopusJni.so` — Opus audio decoder
- `libffmpegkit.so` — Audio format conversion

### 2. Official Plaud Desktop App (Electron)

The macOS desktop app (`~/Library/Application Support/Plaud/`) was examined to understand:
- OAuth authentication flow (browser-based, token exchange)
- Bearer token storage in LevelDB (`Local Storage/leveldb/*.ldb`)
- API domain configuration
- Cloud upload format and metadata

### 3. iOS BLE Packet Captures

PacketLogger captures from an iPhone connecting to the NotePin S provided ground truth for the BLE protocol:

- `plaud-disconnect-reconnect-ios-2.pklg` — Reconnection flow (725KB, 1,012 ATT ops)
- `plaud-connect-features-disconnect-ios-4.pklg` — Full session (3.6MB, 1,210 ATT ops)

Analysis of these captures is documented in:
- `BLE_CAPTURE_ANALYSIS.md` — Reconnection flow analysis
- `BLE_COMPREHENSIVE_ANALYSIS.md` — Full session with file operations

### 4. Cloud API (web.plaud.ai / api-euc1.plaud.ai)

The cloud API was documented by:
- Intercepting requests from the official mobile and desktop apps
- Examining the APK's API client code
- Testing endpoints directly with extracted bearer tokens

Full API documentation in `FINDINGS.md`.

## Key Discoveries

### Audio Decryption (the breakthrough)

The "E2EE" marketing is misleading. Audio files use a **static RSA key pair** embedded in every copy of the APK — not per-user keys. The decryption flow:

1. File header contains `PLAUD.AI` magic + RSA-encrypted file key at offset 0x100
2. Decrypt file key with the app's RSA private key (PKCS1v15)
3. First 32 bytes of decrypted blob = ChaCha20 key
4. Generate keystream from key + nonce (from header at 0x84)
5. XOR each 80-byte Opus frame with the keystream

This means **any recording from any device can be decrypted without cloud access** using the key pair from the APK.

### BLE Address Randomisation

macOS randomises BLE addresses between connections. The official app uses the device serial number (from manufacturer advertising data) for identification. We do the same — `paired_devices` is keyed by serial number, not BLE address.

### Cloud Upload Format

The cloud expects **raw Opus frames** (80 bytes each), not OGG-wrapped files. Local recordings are stored as OGG but must be unwrapped before upload. Duration must be in milliseconds.

## File Index

```
docs/reverse-engineering/
  METHODOLOGY.md              ← this file
  PROTOCOL.md                 ← BLE protocol specification
  FINDINGS.md                 ← Device overview, cloud API, audio format
  BLE_CAPTURE_ANALYSIS.md     ← iOS packet capture analysis
  BLE_COMPREHENSIVE_ANALYSIS.md ← Extended capture analysis
  AndroidManifest.xml         ← APK manifest
  decompiled/
    ble-sdk/                  ← TinnoTech PenBLE SDK (53 files)
    crypto/                   ← Encryption layer (9 files)
    protocol/                 ← Protocol interfaces (8 files)
```

## Tools Used

- [jadx](https://github.com/skylot/jadx) — Android APK decompiler
- [PacketLogger](https://developer.apple.com/bluetooth/) — macOS Bluetooth packet capture
- [Wireshark](https://www.wireshark.org/) — Network analysis
- [bleak](https://github.com/hbldh/bleak) — Python BLE client (for testing)
