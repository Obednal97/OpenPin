# Reverse engineering

How the Plaud NotePin S, its Android SDK and the Plaud Cloud API were worked out, and what Plaud V2
relies on. The code is the ground truth; these docs describe it and mark what is still a guess.

## Reading order

1. [PROTOCOL.md](PROTOCOL.md): discovery, handshake, encryption, BLE and WiFi file transfer, the
   recording file format and decryption, Plaud Cloud calls.
2. [COMMANDS.md](COMMANDS.md): one entry per BLE command, with request and reply layouts.
3. [CAPTURES.md](CAPTURES.md): what the iOS packet captures showed, and earlier conclusions that
   turned out wrong.
4. [Open questions](#open-questions) below: what the next real-hardware test should settle.

Code these docs describe:

| File | Covers |
|---|---|
| `app/state/device.py` | Scan, connect, handshake, `send_cmd()`, commands, BLE sync, completeness checks, auto-delete |
| `app/utils/wifi_sync_worker.py` | WiFi sync process: its own BLE handshake, hotspot, WebSocket transfer, Mac network restore |
| `app/utils/audio.py` | File decryption, Opus checks, Ogg wrapping |
| `app/utils/constants.py` | UUIDs, key path (`APP_KEY_PATH`), cloud domains |
| `app/state/cloud.py` | Plaud Cloud sign-in, `/device/bind`, `/device/sn-sign`, import and upload |
| `src/crypto.py`, `src/scanner.py` | RSA helpers, advertising-data parser |

`src/protocol.py`, `src/client.py` and `src/constants.py` are an older standalone client. They still
send the static `APP_KEY` handshake and use the old command names; don't take protocol facts from
them.

## Status legend

| Tag | Meaning |
|---|---|
| **Confirmed** | The app does this and it has worked against a real NotePin S, or a capture shows it |
| **Implemented** | The app does this, but its behaviour on hardware isn't confirmed yet |
| **APK** | Read from the decompiled Android app only. The app doesn't use it |
| **Unverified** | Inferred or guessed. Treat as a hypothesis |

## Sources and method

- **Android app** (Plaud v3.13.0), decompiled with [jadx](https://github.com/skylot/jadx). The
  BLE layer is TinnoTech's PenBLE SDK: reply parsers in package `p279oh`, request builders in
  `p257nh`, and the opcode tables, CRC and integer packing in the native `libtnt_ble_utils.so`. The
  app RSA key pair came from the APK assets. Each reply class's opcode method and constructor give
  the reply layouts in COMMANDS.md.
- **iOS packet captures** made with Apple's PacketLogger while the official app connected, synced
  and disconnected. See CAPTURES.md.
- **Official Plaud desktop app** (Electron, macOS). It records computer audio only and never talks
  to the device. It showed the browser sign-in flow and where the bearer token is stored (LevelDB
  under `~/Library/Application Support/Plaud/`), which `cloud.py` can read.
- **Plaud Cloud API**, from the apps' traffic and by calling endpoints with a token.
- **Live testing** with Plaud V2 against the owner's device, for example by calling the running
  backend on `127.0.0.1:5123` (`/device/status`, `/device/info`) to see what the device returned.

Tools: jadx, PacketLogger, Wireshark, and [bleak](https://github.com/hbldh/bleak) (the app's BLE
library).

### Not in the repo

| Item | Why | Where |
|---|---|---|
| Decompiled Java and `AndroidManifest.xml` | Third-party code, redistribution risk; the facts are in these docs | Git history: `docs/reverse-engineering/decompiled/` at commit 9738cc1 |
| Earlier analyses (`FINDINGS.md`, `BLE_CAPTURE_ANALYSIS.md`, `BLE_COMPREHENSIVE_ANALYSIS.md`, `COMMAND_REFERENCE.md`, `METHODOLOGY.md`) | Merged into the files above | Git history at commit 9738cc1 |
| `keys/e2ee_private.pem`, `keys/e2ee_public.pem` | Extracted from the APK; not committed | Local `keys/` folder |
| `.pklg` captures | Large, contain device identifiers | Not kept in git |

## Open questions

Each of these is a protocol assumption the code makes without hardware proof. The code fails safe
on all of them (keeps data, never auto-deletes on doubt), but the next real-hardware test should
answer them. Log lines to look for are in quotes.

1. **CRC-16 variant (cmd 29).** Which variant does the device use, and does it cover the 512-byte
   header? Check "[BLE] CRC session … matches …". If one name matches every time, set
   `CRC16_ENFORCED_VARIANT` in `device.py`.
2. **Delete reply (cmd 31).** Does status 0 mean the file is gone? Is the session ID always echoed
   at 3? After an auto-delete, list the files again and check.
3. **Download start reply (cmd 28).** What does byte 7 mean, and is 0 always "accepted"? Check
   "cmd-28 reply status … (meaning unconfirmed — logged only)".
4. **End marker.** Does the `0xFFFFFFFF` data packet always arrive, is its byte 9 an error code
   (0 = OK), and which comes first, it or cmd 29?
5. **Size from the file list.** Does the byte count of a full download always equal it? Check for
   "size mismatch accepted". If it never happens, the `size_mismatch` path is dead weight; if it
   always happens, the size means something else.
6. **File list entries (cmd 26).** What do entry bytes 8–9 and header bytes 3–6 mean? What selects
   the 8-, 9- or 10-byte entry width?
7. **Handshake reply (cmd 1).** What does a rejected `BIND_TOKEN` look like? The app doesn't check
   the status byte.
8. **Closing the hotspot.** Does `[1,10,0,0]` close it, or is cmd 13 needed? Watch whether the
   `Plaud…` network disappears after a WiFi sync.
9. **WiFi file list paging.** Can WiFi cmd 11 arrive in several messages for many files? The worker
   restarts the current download when a second list message arrives.
10. **WebSocket port.** Does the device only try port 8081, or does it find 8082/8083 when the
    worker falls back?
11. **Time zone (cmd 4).** The app always sends UTC. Does the device use the offset for anything?
12. **Smaller fields.** Firmware version width in cmd 2 (u24 assumed), units of the cmd 6 duration
    field, whether session IDs are recording start times, which files need the continuous
    keystream, and the `BIND_TOKEN` format and length.
