# BLE Encrypted Handshake — Progress & State

> Last updated: 2026-03-26

## What Works

### Pre-Handshake Flow (portVersion >= 20)

The encrypted handshake is **NOT** a simple cmd_id=1 handshake. It's a multi-step RSA key exchange:

```
1. API: POST /device/sn-sign  {"sns":["8820B50256947317"]}
   → Returns SN signature (Base64-encoded, 256 bytes decoded)

2. API: POST /device/bind  {sn, name, model, version_number}
   → Returns access_token (255-char JWT = bind token)

3. BLE: Send SN signature via cmd_id=65040 in 100-byte chunks
   Packet format: [cmd_id u16 LE][total_chunks u8][chunk_index u8][data]
   NO cmd_type prefix byte!

4. BLE: Device responds immediately with RSA-encrypted blob
   Response: cmd_id=65042, 3 chunks totaling 256 bytes
   Device uses a stored/pre-provisioned public key

5. BLE: Client RSA-decrypts the 256-byte blob
   → Gets 36 bytes of decrypted data (unexpected — Java code expects 56+)

6. TODO: Parse the 36 bytes to extract ChaCha20 parameters
7. TODO: Send RSA public key (for future sessions)
8. TODO: Send regular handshake (cmd_id=1) with bind token, encrypted
```

### Key Discovery: Packet Format

The pre-handshake packets do **NOT** have a `cmd_type` byte prefix. They start directly with the `cmd_id`:

```
Standard commands:  [cmd_type=1][cmd_id u16 LE][payload]     (3+ bytes header)
Pre-handshake:      [cmd_id u16 LE][total u8][index u8][data] (4 bytes header)
```

This is because `C9559c0.mo35317b()` overrides the parent's `m35321e()` header builder and returns raw concatenation instead.

### Command IDs (Corrected)

| Decompiled | Actual cmd_id | Purpose |
|------------|---------------|---------|
| `65041` (in callback arrays) | `65040` (0xFE10) | Send SN signature TO device |
| `65042` (0xFE12) | `65042` (0xFE12) | RSA data exchange |
| The cmd_id 65040 vs 65041 depends on `f30017h` boolean (false=65040, true=65056) |

### RSA Decryption Result

```
Input:  256 bytes (RSA-2048 ciphertext from device)
Output: 36 bytes
Hex:    837249eaf141f0d754368c4bb117f195
        db3cfac9bf38065d1ced6919bd0d52b5
        48126a86

Using: RSA/PKCS1v15 with app_private.pem (2048-bit)
```

### Open Question: 36-byte Blob Structure

The Java code at C10595r4.java:413-421 expects:
- bytes[0:32]  = ChaCha20 key
- bytes[32:44] = nonce (12 bytes)
- bytes[44:56] = additional data (12 bytes)
- bytes[56:]   = ciphertext (8 bytes "PLAUD.AI" + 16 bytes Poly1305 tag = 24 bytes)

Total expected: 80 bytes minimum. We got 36.

**IMPORTANT DISCOVERY: Dual cipher support (AbstractC11067m.java)**

The crypto module supports TWO ciphers and tries both:
1. **AES/GCM/NoPadding** (method `m40006i` / `m39998a`) — GCM with 128-bit tag, 12-byte nonce
2. **ChaCha20-Poly1305** (method `m40007j` / `m39999b`) — via Conscrypt provider

Method `m40000c` tries one and falls back to the other. The device might use AES-GCM.

**Possible explanations for 36 bytes:**
1. The RSA data arrived before we sent pubkey — device used a stored/factory key
2. More RSA data arrives AFTER we send the pubkey (second wave)
3. The 36 bytes are: key(32) + 4-byte flag/status — encryption not fully negotiated
4. We're assembling the wrong notification packets as the RSA blob
5. The packet header strip (4 bytes) should be different (2 or 3 bytes)

### Hypothesis to Test Next

The RSA chunks arrived BEFORE we sent the public key. This means:
- The device already had a public key stored (from factory or prior pairing)
- Our public key send might trigger a SECOND RSA exchange with MORE data
- Or: the device's response format differs from what the old Java code expects

## Current API Tokens

### User Token (web)
```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2NjllNzgwNDE4ZDg0OWRhOGFmMDU2MzIzOTljMjVhZSIsImF1ZCI6IiIsImV4cCI6MTc5OTc4MDkyNCwiaWF0IjoxNzczODYwOTI0LCJjbGllbnRfaWQiOiJ3ZWIiLCJyZWdpb24iOiJhd3M6ZXUtY2VudHJhbC0xIn0.dyX51IMTJc_sIX487bOq5Sa7TyXiy33T4qut7Rq-X3Y
```

### Bind Token (device)
```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2NjllNzgwNDE4ZDg0OWRhOGFmMDU2MzIzOTljMjVhZSIsImF1ZCI6IiIsImV4cCI6MjAzMzczMzY5NSwiaWF0IjoxNzc0NTMzNjk1LCJjbGllbnRfaWQiOiJkZXZpY2UiLCJzbiI6Ijg4MjBCNTAyNTY5NDczMTcifQ.UJH_zwN0Imyc4W8dropHXy1_j7kN09aJf-2ZUtyLEJs
```

### SN Signature
```
TOb4HuuFPMhEB8bMCR3SFlqTRO9ihuhsHp0Jp3bwBGDYpew7FI9rFykg2JUKGLfe50BJS4z6Esm2Mt7Bqv6e5nz0j6jRCKWVWTuNiFY6a2iGY2lAvghwP8+JrqHb2RlTgoCj+vYXxAlMpZ5eMFCmTudiCmJt4aPzrJFURyM8OJfVJ8IHZi2/WbkR/i0vLrVuuLO3aShZrFjR7Enw0MQYUl4RMnHxk4qEwr7MV7qSTjyA7qeYcZY09gfS9+GmbVhwU4ZS4doiD+8gk6AmOn4zn20C3sCS1EOo3KGJgTNzSdZA59d/YQLql3bTjL3VsJYVvQlP5Zzfa5nHLcd3fN588w==
```

## Device State

- Device was factory erased ("Erase and remove" from iOS app)
- Re-bound via API: POST /device/bind → success
- BLE advertising: company_id=0x005D, name="Plaud NotePin S"
- Battery: 90%
- B001 characteristic: 0x0102 (protocol v1, state 2)
- GATT service 0x1910 with chars 0x2BB0 (notify), 0x2BB1 (write), 0xB001 (read), 0xB004 (indicate)

## Key Source Files

| File | What it contains |
|------|-----------------|
| `C10558l3.java:4419` (m38460w3) | Pre-handshake: sends SN sig via C9559c0 |
| `C9559c0.java` | Packet builder for pre-handshake (cmd 65040/65056) |
| `C10595r4.java:340` (onCharacteristicChanged) | RSA data collection from notifications |
| `C10595r4.java:395` | RSA blob assembly (strips 4-byte headers, concatenates) |
| `C10595r4.java:405` | RSA decrypt + ChaCha20 key extraction |
| `AbstractC11067m.java:98` (m40001d) | RSA decrypt implementation |
| `AbstractC11067m.java:160` (m40005h) | PEM key parsing |
| `C10558l3.java:2909` (m38431A3) | Public key send (cmd 65042, after RSA ack) |
| `C10558l3.java:3920` (m38447j3) | Regular handshake (cmd 1, for unencrypted) |
| `C10558l3.java:3265` | Second handshake (cmd 1, for encrypted, post-RSA) |

## RSA Keys

- Private: `keys/app_private.pem` (PKCS#8, RSA 2048-bit)
- Public: `keys/app_public.pem` (RSA 2048-bit)
- Source: APK assets at `flutter_assets/packages/plaud_flutter_common/assets/audio/`
