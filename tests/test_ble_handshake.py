"""BLE connect: BIND_TOKEN handshake verdict and time sync (no hardware).

FakeNotePin plays the device through the whole connect_device() flow: SN
signature, session blob, then the encrypted cmd 1 / 4 / 3 / 6 / 9 exchange.
"""

import asyncio
import base64
import struct

import bleak
import pytest

import app.state.device as dev
from app.utils import ble_proto
from tests.plaud_fakes import FakeNotePin

TOKEN = "T" * 32


@pytest.fixture
def connect_env(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(dev, "_HANDSHAKE_REPLY_TIMEOUT_S", 0.1)
    monkeypatch.setattr(dev, "SN_SIG_REPLY_WAIT_S", 1)
    monkeypatch.setattr("app.state.cloud.get_device_sn_signature", lambda sn: base64.b64encode(bytes(256)).decode())
    monkeypatch.setattr("app.state.cloud.get_device_bind_token", lambda sn: TOKEN)
    monkeypatch.setattr("app.state.cloud.is_logged_in", lambda: True)
    monkeypatch.setitem(dev._device_state, "serial_number", "8820A00012345678")
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        return await real_sleep(min(delay, 0.001), *a, **k)

    monkeypatch.setattr(dev.asyncio, "sleep", fast_sleep)
    yield
    dev._reset_state()


def connect(monkeypatch, device):
    monkeypatch.setattr(bleak, "BleakClient", device.client_factory)
    monkeypatch.setattr(dev, "rsa_decrypt", device.session_plaintext)
    monkeypatch.setattr(dev, "load_rsa_private_key", lambda *a: None)
    return asyncio.run(dev.connect_device("AA:BB"))


def test_accepted_handshake_connects(connect_env, monkeypatch):
    device = FakeNotePin(hs_status=0, port_version=9)
    result = connect(monkeypatch, device)
    assert result["connected"] is True
    assert result["battery"] == 77
    assert dev._device_state["port_version"] == 9
    # cmd 1 twice (flag 0 then 1) with the token, then cmd 4, 3, 6, 9.
    assert device.sent_cmds() == [1, 1, 4, 3, 6, 9]
    assert [pt[5] for pt in device.received[:2]] == [0, 1]
    assert device.received[0][6:38] == TOKEN.encode()


def test_rejected_bind_token_only_warns_by_default(connect_env, monkeypatch, caplog):
    """Until hardware confirms the status semantics, a 'rejection' is logged, not fatal (as v2.4.0)."""
    assert ble_proto.FAIL_ON_HANDSHAKE_REJECTED is False
    device = FakeNotePin(hs_status=1, silent={3, 4, 6, 9})
    with caplog.at_level("WARNING"):
        result = connect(monkeypatch, device)
    assert result["connected"] is True
    assert "Handshake looks rejected" in caplog.text and "continuing anyway" in caplog.text


def test_rejected_bind_token_fails_connect(connect_env, monkeypatch):
    """With the switch on: non-zero handshake status and no answer to later commands = rejected."""
    monkeypatch.setattr(ble_proto, "FAIL_ON_HANDSHAKE_REJECTED", True)
    device = FakeNotePin(hs_status=1, silent={3, 4, 6, 9})
    result = connect(monkeypatch, device)
    assert result == {"connected": False, "error": ble_proto.HANDSHAKE_REJECTED_ERROR, "pairing_rejected": True}
    assert "re-pair the device in Settings › Device" in result["error"]
    assert dev._device_state["connected"] is False
    assert dev._device_state["client"] is None
    assert device.client.is_connected is False
    assert dev.is_connected() is False


def test_nonzero_status_but_working_device_still_connects(connect_env, monkeypatch, caplog):
    """The status byte's meaning is unconfirmed: a device that answers is working."""
    device = FakeNotePin(hs_status=2)
    with caplog.at_level("WARNING"):
        result = connect(monkeypatch, device)
    assert result["connected"] is True
    assert "treating as accepted" in caplog.text


def test_no_handshake_reply_at_all_still_connects_with_a_warning(connect_env, monkeypatch, caplog):
    """As in v2.4.0: whether the NotePin S answers cmd 1 is unverified, so silence doesn't fail."""
    device = FakeNotePin(hs_status=None, silent={3, 4})
    with caplog.at_level("WARNING"):
        result = connect(monkeypatch, device)
    assert result["connected"] is True and "error" not in result
    assert dev._device_state["connected"] is True
    assert dev._device_state["port_version"] is None
    assert "no reply to cmd 1 (x2), cmd 4 or cmd 3 — continuing the connect" in caplog.text
    # cmd 1 twice, cmd 4, the cmd 3 probe, then the usual cmd 3 / 6 / 9.
    assert device.sent_cmds() == [1, 1, 4, 3, 3, 6, 9]


def test_handshake_waits_are_capped():
    """A device that answers nothing adds at most 4 reply waits of 1.5 s (6 s) to a connect."""
    assert dev._HANDSHAKE_REPLY_TIMEOUT_S == ble_proto.HANDSHAKE_REPLY_TIMEOUT_S
    assert ble_proto.HANDSHAKE_REPLY_TIMEOUT_S * ble_proto.HANDSHAKE_MAX_REPLY_WAITS <= 6
    waits = []

    async def silent_send(pt):
        waits.append(ble_proto.HANDSHAKE_REPLY_TIMEOUT_S)
        return None

    verdict, _info = asyncio.run(ble_proto.bind_handshake(silent_send, bytes(32), log=lambda m: None))
    assert verdict == ble_proto.HANDSHAKE_NO_REPLY
    assert len(waits) == ble_proto.HANDSHAKE_MAX_REPLY_WAITS and sum(waits) <= 6


def test_status_zero_on_a_slow_link_still_connects(connect_env, monkeypatch):
    """Clean handshake status, later replies lost: same as before (connected)."""
    device = FakeNotePin(hs_status=0, silent={4})
    assert connect(monkeypatch, device)["connected"] is True


def test_connect_sends_local_time_zone(connect_env, monkeypatch):
    monkeypatch.setattr(ble_proto, "local_utc_offset_minutes", lambda at=None: -300)
    device = FakeNotePin()
    connect(monkeypatch, device)
    cmd4 = device.received[2]
    assert cmd4[:3] == bytes([1, 4, 0]) and len(cmd4) == 9
    assert struct.unpack_from("<bb", cmd4, 7) == (-5, 0)


def test_sn_signature_sent_the_shared_number_of_times(connect_env, monkeypatch):
    device = FakeNotePin()
    connect(monkeypatch, device)
    # The fake answers after the first full set (3 chunks); the rest still go out.
    assert device.sig_writes == 3 * ble_proto.SN_SIG_SEND_REPEATS


# -- ble_proto units ------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "tz"),
    [(0, (0, 0)), (60, (1, 0)), (-300, (-5, 0)), (330, (5, 30)), (-210, (-3, -30)), (345, (5, 45)), (840, (14, 0))],
)
def test_time_sync_request_bytes(offset, tz):
    pt = ble_proto.time_sync_request(now=1_780_584_913, utc_offset_minutes=offset)
    assert pt == bytes([1, 4, 0]) + struct.pack("<I", 1_780_584_913) + struct.pack("<bb", *tz)


def test_time_sync_request_exact_bytes_for_negative_offset():
    # UTC-5: hours byte is 0xFB (two's complement), minutes 0.
    assert ble_proto.time_sync_request(now=0x01020304, utc_offset_minutes=-300).hex() == "010400040302" + "01fb00"
    # +5:30
    assert ble_proto.time_sync_request(now=0x01020304, utc_offset_minutes=330).hex() == "010400040302" + "01051e"


def test_local_offset_follows_the_mac_time_zone(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        assert ble_proto.local_utc_offset_minutes(1_780_584_913) == 330
        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()
        assert ble_proto.local_utc_offset_minutes(1_767_225_600) == -300  # January: EST
        assert ble_proto.local_utc_offset_minutes(1_783_000_000) == -240  # July: EDT (DST included)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_handshake_verdict_rules():
    ok, rej, none = ble_proto.HANDSHAKE_OK, ble_proto.HANDSHAKE_REJECTED, ble_proto.HANDSHAKE_NO_REPLY
    s0, s1 = {"status": 0}, {"status": 1}
    assert ble_proto.handshake_verdict([s0, s0], True) == ok
    assert ble_proto.handshake_verdict([s1, s1], True) == ok  # answers commands → working
    assert ble_proto.handshake_verdict([s0, s1], False) == rej
    assert ble_proto.handshake_verdict([None, None], False) == none
    assert ble_proto.handshake_verdict([None, None], True) == ok
    assert ble_proto.handshake_verdict([s0, None], False) == ok


def test_parse_handshake_reply():
    reply = bytes([1, 1, 0, 0]) + struct.pack("<H", 9) + bytes([0xFB, 0, 0, 1, 0, 0]) + b"V" + bytes([10, 0, 1])
    info = ble_proto.parse_handshake_reply(reply)
    assert info["status"] == 0 and info["port_version"] == 9 and info["tz_hours"] == -5
    assert info["wifi_supported"] is True
    assert ble_proto.parse_handshake_reply(None) is None
    assert ble_proto.parse_handshake_reply(bytes([1, 4, 0, 0])) is None  # another command's reply


def test_handshake_replies_reach_the_warning_log(connect_env, monkeypatch, caplog):
    """The backend logs at WARNING: the raw replies and port version must get there."""
    device = FakeNotePin(hs_status=0, port_version=9)
    with caplog.at_level("WARNING"):
        assert connect(monkeypatch, device)["connected"] is True
    assert "[BLE] Handshake replies: HS1=" in caplog.text
    assert "(port version 9, 32-byte token)" in caplog.text


def test_rejected_on_old_port_version_retries_with_16_byte_token(connect_env, monkeypatch, caplog):
    """Plaud's app sends the token as 16 bytes below port version 9."""
    device = FakeNotePin(port_version=8, token_len=16)
    with caplog.at_level("WARNING"):
        result = connect(monkeypatch, device)
    assert result["connected"] is True and result["battery"] == 77
    # 32-byte try (cmd 1 x2, cmd 4, cmd 3 probe), then the 16-byte try, then 3 / 6 / 9.
    assert device.sent_cmds() == [1, 1, 4, 3, 1, 1, 4, 3, 6, 9]
    assert [len(pt) - 6 for pt in device.received if pt[1] == 1] == [32, 32, 16, 16]
    assert device.received[4][6:] == TOKEN[:16].encode()
    assert "retrying with a 16-byte token" in caplog.text and "16-byte token accepted" in caplog.text


def test_rejected_on_port_version_9_does_not_retry_short(connect_env, monkeypatch, caplog):
    device = FakeNotePin(port_version=9, token_len=16)
    with caplog.at_level("WARNING"):
        connect(monkeypatch, device)
    assert [len(pt) - 6 for pt in device.received if pt[1] == 1] == [32, 32]
    assert "retrying with a 16-byte token" not in caplog.text


def test_short_token_that_also_fails_is_reported(connect_env, monkeypatch, caplog):
    device = FakeNotePin(port_version=7, token_len=20)  # accepts neither 32 nor 16
    with caplog.at_level("WARNING"):
        result = connect(monkeypatch, device)
    assert result["connected"] is True  # FAIL_ON_HANDSHAKE_REJECTED is off
    assert "16-byte token didn't help either (rejected)" in caplog.text
    assert "Handshake looks rejected" in caplog.text
