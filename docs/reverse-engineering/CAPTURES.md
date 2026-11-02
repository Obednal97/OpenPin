# iOS BLE captures

What two PacketLogger captures of the official iOS app showed, reread against the protocol as the
code now implements it ([PROTOCOL.md](PROTOCOL.md)). Raw dumps, addresses and per-session byte
values are left out; the original analyses are in git history (see the [README](README.md)).

## The captures

| Capture | Recorded | Size | Contents |
|---|---|---|---|
| A: `plaud-disconnect-reconnect-ios-2.pklg` | March 2026 | 0.7 MB, 1 012 ATT operations | 3 app connections in about 91 s; one downloads about 141 KB |
| B: `plaud-connect-features-disconnect-ios-4.pklg` | 2026-03-27 | 3.6 MB, 1 210 ATT operations | 5 app connections in about 4 min; 4 download files, the last does a WiFi transfer |

The `.pklg` files are not in the repo. PacketLogger records Bluetooth HCI only, so the WiFi
transfer itself is not visible.

## Link and GATT

- iOS requests an ATT MTU of 527; the device grants **247**.
- Service `1910` sits at handles `0x0032`–`0x003D` and its handles are the same on every connection:
  `0x0037` write (`2BB1`), `0x0039` notify (`2BB0`), `0x003A` its CCCD, `0x0034` read (`B001`),
  `0x003C` indicate (`B004`). `B001` and `B004` are never used.
- The device also exposes Generic Access (name `Plaud NotePin S`), Service Changed, Tx Power,
  Battery (`2A19`), SIG service `FD44`, and a custom service of unknown purpose.
- iOS writes the `2BB0` CCCD (`01 00`) twice and enables Service Changed indications. It runs full
  service discovery on every connection.
- No SMP pairing appears: the phone was already bonded and link-layer encryption is on. The Plaud
  handshake is an extra layer on top.
- The device has a fixed identity address (used by the iOS system connection) and a new resolvable
  private address for each app connection. This is why the app identifies devices by serial.

## Handshake

Read with the framing in [PROTOCOL.md section 5](PROTOCOL.md#5-handshake):

- **Every connection**: iOS sends `SN_SIG` as 3 chunks (100 + 100 + 56 = 256 bytes), and sends the
  set twice. The bytes are identical on every connection.
- **First connection** (device doesn't have the app key): the device replies `11 FE 00` (ACK), iOS
  sends its PEM public key in 5 chunks (about 450 bytes), and the device sends the 256-byte blob in
  3 chunks.
- **Reconnections**: no ACK and no public key. The device sends the blob straight away.
- The handshake takes 375–610 ms (reconnections are about 35 % faster). iOS reaches its first
  command about 1.5 s after the link comes up.

## Encrypted traffic

Packet sizes match `4 (seq) + plaintext + 16 (tag)` exactly:

| Wire size | Plaintext | Matches |
|---|---|---|
| 58 B (first write after the handshake) | 38 B | cmd 1 with a 32-byte token |
| 23 B | 3 B | A command with no payload, e.g. `[1,3,0]` |
| 29 B | 9 B | cmd 4 time sync |
| 35 B | 15 B | cmd 28 download request (seen during transfers) |
| 27 B | 7 B | Same size as a cmd 31 delete request (**Unverified** that it is one) |
| 190 B (notify) | 170 B | Data packet: 10-byte header + 160 bytes of file data |

- After the handshake iOS sends cmd 1 (58 B) and gets a 36 B reply, then runs about 18–20 commands
  in the same order on every reconnection.
- In each session the first ciphertext byte changes per packet while the next three stay fixed.
  That is the encrypted `seq` counter under a reused nonce: only its low byte changes.

## Transfer behaviour

| | Observed |
|---|---|
| Throughput | 310–390 kbit/s (about 40–48 KB/s) at a 15 ms connection interval |
| Slow case | 32 kbit/s, with gaps up to 10 s, while the iOS system connection competed for airtime |
| Connection interval | The device asks for 15 ms when active and 150–165 ms when idle; supervision timeout 5 s |
| Flow control | The device streams notifications in bursts. No per-burst acknowledgements: the few host writes during a transfer are commands, some of them the size of a cmd 28 request |
| Session length | 5–30 s: connect, sync, disconnect. No keep-alive traffic |
| Disconnects | Usually the app ends the link; sometimes the device does, after switching to the idle interval |
| WiFi | The last connection in capture B sent 18 commands and received no file data, consistent with handing the transfer to WiFi |

## Disproven or historical

Earlier analyses drew these conclusions. They are wrong or no longer relevant.

| Earlier claim | What is true |
|---|---|
| Byte 0 of each encrypted packet is a plaintext sequence number and bytes 1–3 a session ID | The whole packet is AEAD ciphertext. The fixed bytes come from the reused nonce |
| Pre-encryption packets are `[seq][0xFE][subcmd][chunk]` with sub-commands 0x00, 0x03, 0x05 | They are `[opcode u16][total][index]` with opcodes 0xFE10, 0xFE11, 0xFE12. "0x03" and "0x05" were chunk counts |
| `SN_SIG` is 259 bytes | 256 bytes; the 4-byte header was miscounted as 3 |
| A failed first connection in capture A: the device answered with its own SN signature | It sent the session blob (0xFE12) without an ACK because it already had the key. Why iOS then gave up is unknown |
| The device sends its own ephemeral RSA public key | It sends the session parameters encrypted with the app's public key |
| The traffic is AES-encrypted | ChaCha20-Poly1305 |
| The handshake carries the static `APP_KEY` | It carries the per-device `BIND_TOKEN` |
| The host acknowledges each burst of file data | No acknowledgements; see above |
| WiFi transfer is TCP/HTTP over an open hotspot | The hotspot is WPA (password from the serial) and the device connects to a WebSocket server on the Mac |
| cmd 9 is device settings, cmd 13 common settings | cmd 9 is battery, cmd 8 common settings, cmd 13 close WiFi |
| Read battery from GATT `0x2A19` | That read hangs on macOS; use cmd 9 |
