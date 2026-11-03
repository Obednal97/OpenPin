"""BLE protocol helpers shared by the app (app/state/device.py) and the WiFi worker.

The WiFi worker runs as a separate process, but with the project root on
PYTHONPATH (device.py sets it), so both import this module. Keep it free of app
state and heavy imports: plain bytes in, plain values out.

Layouts are in docs/reverse-engineering/COMMANDS.md. What isn't confirmed on
hardware is marked there and in the README's open questions.
"""

import logging
import struct
import time

# -- SN-signature handshake (pre-encryption, opcode 65040) --------------------

# The signature is sent this many times in a row (the iOS app sends it twice;
# a third copy helps on weak links), then the app waits up to
# SN_SIG_REPLY_WAIT_S for the device's ACK (65041) or session blob (65042).
SN_SIG_SEND_REPEATS = 3
SN_SIG_REPLY_WAIT_S = 10


# -- Reply matching ------------------------------------------------------------


def cmd_id_of(pt):
    """cmd_id (bytes 1-2) of a type-1 plaintext packet, or None."""
    if pt is None or len(pt) < 3:
        return None
    return struct.unpack_from("<H", pt, 1)[0]


def reply_matches(reply, expected):
    """True if a decrypted packet is a type-1 reply to one of the `expected` cmd_ids.

    `expected` is a cmd_id or a collection of them. The device also sends
    unsolicited type-1 packets (button-press record events, late replies to an
    earlier command); returning one of those as the current command's reply is
    the bug v2.2.0 fixed in device.py ("stuck 90 % battery").
    """
    if reply is None or len(reply) < 3 or reply[0] != 1:
        return False
    cid = cmd_id_of(reply)
    if isinstance(expected, int):
        return cid == expected
    return cid in expected


# -- cmd 1: handshake (BIND_TOKEN) ----------------------------------------------

HANDSHAKE_REJECTED_ERROR = "The NotePin rejected this Mac's pairing token — re-pair the device in Settings › Device"

# Whether a HANDSHAKE_REJECTED verdict fails the connect. Off until a real
# NotePin has shown what a rejected token looks like: the status byte's meaning
# is unconfirmed, so for now a "rejection" is only logged (a WARNING with the raw
# replies) and the connect carries on exactly as in v2.4.0. Turn on once the
# hardware logs confirm the semantics (docs/reverse-engineering/README.md, Q7).
FAIL_ON_HANDSHAKE_REJECTED = False

# How long to wait for each reply during bind_handshake(). At most
# HANDSHAKE_MAX_REPLY_WAITS replies are awaited (cmd 1 twice, cmd 4, the cmd 3
# probe), so a device that answers none of them adds at most 4 x 1.5 = 6 s to a
# connect. BLE replies normally take well under a second.
HANDSHAKE_REPLY_TIMEOUT_S = 1.5
HANDSHAKE_MAX_REPLY_WAITS = 4

# Plaud's app (kwb.java, built in c03.j()) sends the BIND_TOKEN as 32 bytes from
# port version 9 and as its first 16 below that. The app sends 32; a rejected
# handshake from a device reporting an older port version is retried with 16
# (bind_handshake). That retry only happens when the device answered cmd 1, so
# it can't add to the silent-device worst case above.
SHORT_TOKEN_BELOW_PORT_VERSION = 9
SHORT_TOKEN_LEN = 16


def handshake_request(token32, flag):
    """cmd 1 request: [1,1,0][version 2][audio channel 0][flag] + 32-byte BIND_TOKEN."""
    return bytes([1, 1, 0, 2, 0, flag]) + token32


def parse_handshake_reply(reply):
    """Decode a cmd 1 reply (layout per the SDK's HandshakeRsp), or None if unusable.

    Only `status` (byte 3) is used to make decisions. Its meaning is not
    confirmed on hardware: 0 is taken as "accepted", anything else is only
    suspicious (see handshake_verdict()).
    """
    if reply is None or len(reply) < 4 or cmd_id_of(reply) != 1:
        return None
    info = {"status": reply[3], "raw": bytes(reply).hex()}
    if len(reply) >= 6:
        info["port_version"] = struct.unpack_from("<H", reply, 4)[0]
    if len(reply) >= 7:
        info["tz_hours"] = struct.unpack_from("<b", reply, 6)[0]
    if len(reply) >= 8:
        info["tz_minutes"] = struct.unpack_from("<b", reply, 7)[0]
    if len(reply) >= 10:
        info["wifi_supported"] = reply[9] == 1
    return info


HANDSHAKE_OK = "ok"
HANDSHAKE_REJECTED = "rejected"
HANDSHAKE_NO_REPLY = "no_reply"


def handshake_verdict(replies, device_answers):
    """Decide whether the device accepted the BIND_TOKEN.

    replies: the parsed cmd 1 replies (parse_handshake_reply; None = no reply).
    device_answers: whether the device answered a later command (cmd 4 or cmd 3).

    The status byte's meaning is unconfirmed (README open question 7), so a
    non-zero status alone doesn't fail the connection: a device that still
    answers commands is working. A rejected token is what the docs describe as
    "later commands just get no reply", so:

      device answers                    -> ok (a non-zero status is only logged)
      no answer + a non-zero status     -> rejected (the only verdict that fails a connect)
      no answer + no handshake reply    -> no_reply (logged; the connect continues,
                                           as in v2.4.0: whether the NotePin S
                                           answers cmd 1 at all is unverified)
      no answer + status 0              -> ok (weak link; same as before)
    """
    parsed = [r for r in replies if r is not None]
    if device_answers:
        return HANDSHAKE_OK
    if any(r["status"] != 0 for r in parsed):
        return HANDSHAKE_REJECTED
    if not parsed:
        return HANDSHAKE_NO_REPLY
    return HANDSHAKE_OK


async def bind_handshake(send, token32, log=None, now=None, utc_offset_minutes=None):
    """Send cmd 1 twice (flag 0, then 1), then cmd 4, and judge the result.

    `send(pt)` is an async function that writes one encrypted command and
    returns its matching reply, or None on timeout (callers use
    HANDSHAKE_REPLY_TIMEOUT_S, so at most HANDSHAKE_MAX_REPLY_WAITS timeouts
    add up). Used by both device.py and the WiFi worker so they behave the same.

    Returns (verdict, info): info has the parsed replies ("hs"), the port
    version if the device reported one, the cmd 4 reply and the token length
    that was used ("token_len"). Only HANDSHAKE_REJECTED should fail the
    connect; HANDSHAKE_NO_REPLY is logged here as a warning and the caller
    carries on.

    `log` defaults to logging.warning so the replies reach the app log (the
    backend's log level is WARNING): they are what README open questions 7
    and 14 need from real hardware.

    If the token looks rejected and the device reports a port version below
    SHORT_TOKEN_BELOW_PORT_VERSION, the handshake is tried once more with the
    token cut to SHORT_TOKEN_LEN bytes, as Plaud's own app does for those
    versions (it sends 32 bytes only from port version 9).
    """
    log = log or logging.warning

    async def attempt(token):
        r1 = await send(handshake_request(token, 0))
        r2 = await send(handshake_request(token, 1))
        hs = [parse_handshake_reply(r1), parse_handshake_reply(r2)]
        port = next((h["port_version"] for h in reversed(hs) if h and "port_version" in h), None)
        log(
            "[BLE] Handshake replies: "
            + ", ".join(f"HS{i + 1}={h['raw'] if h else 'none'}" for i, h in enumerate(hs))
            + f" (port version {port if port is not None else 'unknown'}, {len(token)}-byte token)"
        )
        r4 = await send(time_sync_request(now, utc_offset_minutes))
        answers = r4 is not None
        if r4 is not None:
            tz = parse_time_sync_reply(r4)
            if tz is not None:
                log(f"[BLE] Time sync reply: device time zone {tz['tz_hours']}h")
        clean = all(h is not None and h["status"] == 0 for h in hs)
        if not answers and not clean:
            # Something looked off and cmd 4 got no reply: one more probe (cmd 3,
            # get state) before deciding, so a single lost reply doesn't fail it.
            answers = await send(bytes([1, 3, 0])) is not None
        return handshake_verdict(hs, answers), hs, port, r4

    token = token32
    verdict, hs, port_version, r4 = await attempt(token)
    if (
        verdict == HANDSHAKE_REJECTED
        and port_version is not None
        and port_version < SHORT_TOKEN_BELOW_PORT_VERSION
        and len(token32) > SHORT_TOKEN_LEN
    ):
        logging.warning(
            f"[BLE] Handshake rejected on port version {port_version}: retrying with a "
            f"{SHORT_TOKEN_LEN}-byte token (Plaud's app sends 32 bytes only from port version "
            f"{SHORT_TOKEN_BELOW_PORT_VERSION})"
        )
        token = token32[:SHORT_TOKEN_LEN]
        verdict, hs, port_version, r4 = await attempt(token)
        logging.warning(
            f"[BLE] {SHORT_TOKEN_LEN}-byte token "
            + ("accepted" if verdict == HANDSHAKE_OK else f"didn't help either ({verdict})")
        )
    statuses = [h["status"] for h in hs if h is not None]
    if verdict == HANDSHAKE_OK and any(s != 0 for s in statuses):
        logging.warning(
            f"[BLE] Handshake status {statuses} but the device answers commands — treating as accepted "
            "(status meaning unconfirmed)"
        )
    elif verdict == HANDSHAKE_NO_REPLY:
        logging.warning(
            "[BLE] Handshake: no reply to cmd 1 (x2), cmd 4 or cmd 3 — continuing the connect "
            "(whether the device answers cmd 1 is unverified; later commands decide)"
        )
    elif verdict == HANDSHAKE_REJECTED:
        logging.warning(
            f"[BLE] Handshake looks rejected: status {statuses}, no reply to cmd 4 or cmd 3"
            + (
                ""
                if FAIL_ON_HANDSHAKE_REJECTED
                else " — continuing anyway (FAIL_ON_HANDSHAKE_REJECTED is off); please report"
            )
        )
    return verdict, {"hs": hs, "port_version": port_version, "time_sync_reply": r4, "token_len": len(token)}


# -- cmd 4: time sync ---------------------------------------------------------------


def local_utc_offset_minutes(at=None):
    """This Mac's UTC offset in minutes at `at` (Unix time; now if None), DST included."""
    lt = time.localtime(time.time() if at is None else at)
    return int(lt.tm_gmtoff // 60)


def split_utc_offset(offset_minutes):
    """Minutes east of UTC -> (hours, minutes), both signed with the offset's sign.

    +330 (India) -> (5, 30); -300 (New York, winter) -> (-5, 0);
    -210 (Newfoundland) -> (-3, -30).
    """
    sign = -1 if offset_minutes < 0 else 1
    h, m = divmod(abs(int(offset_minutes)), 60)
    return sign * h, sign * m


def time_sync_request(now=None, utc_offset_minutes=None):
    """cmd 4 request: [1,4,0] + Unix time (u32) + time zone hours (i8) + minutes (i8).

    The time zone is the Mac's local offset at that moment (DST included; the
    format has no separate DST field). Before this the app always sent 0, 0.
    """
    t = int(time.time() if now is None else now) & 0xFFFFFFFF
    if utc_offset_minutes is None:
        utc_offset_minutes = local_utc_offset_minutes(t)
    hours, minutes = split_utc_offset(utc_offset_minutes)
    return bytes([1, 4, 0]) + struct.pack("<Ibb", t, hours, minutes)


def parse_time_sync_reply(reply):
    """cmd 4 reply (SDK TimeSyncRsp): Unix time at 3, time zone (signed) at 7."""
    if reply is None or len(reply) < 8 or cmd_id_of(reply) != 4:
        return None
    return {"time": struct.unpack_from("<I", reply, 3)[0], "tz_hours": struct.unpack_from("<b", reply, 7)[0]}


# -- cmd 10 / 13: hotspot ---------------------------------------------------------

WIFI_OPEN_REQUEST = bytes([1, 10, 0])
# The SDK has a CloseWiFi reply (cmd 13), but its request layout isn't decoded
# (COMMANDS.md: "probably [1,13,0]", unverified). Until hardware settles it the
# app keeps closing with [1,10,0,0] and logs which reply (cmd 10 or 13) comes back.
WIFI_CLOSE_REQUEST = bytes([1, 10, 0, 0])
WIFI_CLOSE_REPLY_IDS = (10, 13)


def describe_status_reply(reply):
    """'cmd N status S' for a reply with a status at byte 3 (cmd 10, 13), for logs."""
    if reply is None:
        return "no reply"
    cid = cmd_id_of(reply)
    st = reply[3] if len(reply) >= 4 else None
    return f"cmd {cid} status {st} ({bytes(reply).hex()})"


# -- cmd 26: file list ---------------------------------------------------------------

FILE_LIST_HEADER = 11  # [1,26,0] + 4 unknown + total u16 + start index u16
FILE_LIST_MAX_SIZE = 500_000_000


def file_list_entry_width(port_version):
    """Entry width the SDK would use for this mode (COMMANDS.md cmd 26).

    The SDK picks it from an integer it's given, probably the port version
    (unverified): 1 -> 8 bytes, 2-6 -> 9, 7 and up -> 10. 10 is confirmed for
    the NotePin S.
    """
    if port_version == 1:
        return 8
    if port_version is not None and 2 <= port_version <= 6:
        return 9
    return 10


class FileListAssembler:
    """Collects the cmd 26 reply, which can span several notifications.

    Each notification carries the total file count (u16 at 7) and the index of
    its first entry (u16 at 9). Like the SDK, a notification is only used when
    its start index continues the entries already collected; a retransmitted
    one is skipped, and one that leaves a gap is held until the gap is filled.

    Entry width: 10 bytes (confirmed for the NotePin S). If a notification's
    entry area isn't a multiple of 10 but is a multiple of the width the port
    version suggests, that width is used. Otherwise, like the SDK and v2.4.0,
    the whole entries are parsed and the trailing bytes ignored (with a
    warning); a notification without even one whole entry is dropped.
    """

    def __init__(self, port_version=None):
        self.widths = [10]
        alt = file_list_entry_width(port_version)
        if alt not in self.widths:
            self.widths.append(alt)
        self.width = None  # fixed by the first notification that has entries
        self.total = None
        self.count = 0  # entries consumed in order, including skipped ones
        self.records = []
        self.problems = []
        self._pending = {}  # start index -> entry bytes, waiting for a gap to fill
        self._seen = set()

    @property
    def complete(self):
        return self.total is not None and self.count >= self.total

    def _problem(self, msg, pkt):
        self.problems.append(msg)
        logging.warning(f"[BLE] file list: {msg} — packet ignored ({bytes(pkt[:64]).hex()})")

    def add(self, pkt):
        """Feed one decrypted cmd 26 notification. Returns True if it was used."""
        if len(pkt) < FILE_LIST_HEADER:
            self._problem(f"short packet ({len(pkt)} bytes)", pkt)
            return False
        total = struct.unpack_from("<H", pkt, 7)[0]
        start = struct.unpack_from("<H", pkt, 9)[0]
        body = bytes(pkt[FILE_LIST_HEADER:])
        if self.total is None:
            self.total = total
        elif total != self.total:
            self._problem(f"total changed from {self.total} to {total}", pkt)
            return False
        if body:
            # An exact fit decides the width; otherwise the confirmed 10 bytes.
            width = self.width or next((w for w in self.widths if len(body) % w == 0), self.widths[0])
            whole = len(body) // width * width
            if not whole:
                self._problem(f"{len(body)} entry bytes is less than one {width}-byte entry", pkt)
                return False
            if whole < len(body):
                # Like the SDK (and v2.4.0): parse the whole entries, ignore the rest.
                logging.warning(
                    f"[BLE] file list: {len(body)} entry bytes is not a multiple of {width}; "
                    f"ignoring the last {len(body) - whole} ({bytes(pkt[:64]).hex()})"
                )
                body = body[:whole]
            if self.width is None:
                if width != 10:
                    logging.warning(f"[BLE] file list: using {width}-byte entries (not the usual 10)")
                self.width = width
        if start > self.count:
            logging.info(f"[BLE] file list: entries from index {start} arrived before index {self.count}; held")
            self._pending[start] = body
            return True
        self._take(start, body)
        while self.count in self._pending:
            self._take(self.count, self._pending.pop(self.count))
        return True

    def _take(self, start, body):
        width = self.width or 10
        n = len(body) // width
        skip = self.count - start  # entries already collected (retransmission)
        if skip >= n and n:
            logging.debug(f"[BLE] file list: duplicate packet from index {start} skipped")
        for i in range(max(skip, 0), n):
            entry = body[i * width : (i + 1) * width]
            sid, fsize = struct.unpack_from("<II", entry, 0)
            self.count += 1
            if not (0 < fsize < FILE_LIST_MAX_SIZE and sid > 0) or sid in self._seen:
                continue
            self._seen.add(sid)
            duration_est = (fsize - 512) / 80 * 0.02 if fsize > 512 else 0
            self.records.append({"session_id": sid, "size": fsize, "duration_est": round(duration_est, 1)})
