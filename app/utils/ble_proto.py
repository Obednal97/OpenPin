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
HANDSHAKE_NO_REPLY_ERROR = (
    "The NotePin didn't answer the pairing handshake. Move it closer and reconnect; "
    "if this keeps happening, re-pair the device in Settings › Device"
)


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
      no answer + a non-zero status     -> rejected
      no answer + no handshake reply    -> no_reply
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
    returns its matching reply, or None on timeout. Used by both device.py and
    the WiFi worker so they behave the same.

    Returns (verdict, info): info has the parsed replies ("hs"), the port
    version if the device reported one, and the cmd 4 reply.
    """
    log = log or logging.info
    r1 = await send(handshake_request(token32, 0))
    r2 = await send(handshake_request(token32, 1))
    hs = [parse_handshake_reply(r1), parse_handshake_reply(r2)]
    log("[BLE] Handshake replies: " + ", ".join(f"HS{i + 1}={h['raw'] if h else 'none'}" for i, h in enumerate(hs)))
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
    verdict = handshake_verdict(hs, answers)
    statuses = [h["status"] for h in hs if h is not None]
    if verdict == HANDSHAKE_OK and any(s != 0 for s in statuses):
        logging.warning(
            f"[BLE] Handshake status {statuses} but the device answers commands — treating as accepted "
            "(status meaning unconfirmed)"
        )
    port_version = next((h["port_version"] for h in reversed(hs) if h and "port_version" in h), None)
    return verdict, {"hs": hs, "port_version": port_version, "time_sync_reply": r4}


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
