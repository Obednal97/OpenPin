"""WiFi worker's device protocol: BLE command matching, handshake verdict, time
zone, hotspot close, and the WebSocket file-list flow. No hardware, no network."""

import asyncio
import base64
import struct
from types import SimpleNamespace

import bleak
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import app.state.device as dev
import app.utils.wifi_sync_worker as worker
import src.crypto
from app.utils import ble_proto
from tests.plaud_fakes import FakeNotePin

_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *a, **k):
    return await _real_sleep(min(delay, 0.001), *a, **k)


@pytest.fixture(scope="module")
def app_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def statuses(monkeypatch):
    lines = []
    monkeypatch.setattr(worker, "status", lambda msg, pct=None, **kw: lines.append(msg))
    return lines


@pytest.fixture
def ble(monkeypatch, statuses):
    """Patch bleak + the RSA step so ble_connect_and_setup() talks to a FakeNotePin."""

    def setup(device):
        adv = SimpleNamespace(manufacturer_data={0x5D: b"\x00"})

        class Scanner:
            @staticmethod
            async def discover(timeout=5, return_adv=True):
                return {"AA:BB": ("AA:BB", adv)}

        monkeypatch.setattr(bleak, "BleakScanner", Scanner)
        monkeypatch.setattr(bleak, "BleakClient", device.client_factory)
        monkeypatch.setattr(src.crypto, "rsa_decrypt", device.session_plaintext)
        monkeypatch.setattr(src.crypto, "load_rsa_private_key", lambda *a: None)
        monkeypatch.setattr(worker, "SN_SIG", base64.b64encode(bytes(256)).decode())
        monkeypatch.setattr(worker, "BIND_TOKEN", "T" * 32)
        monkeypatch.setattr(worker.asyncio, "sleep", _fast_sleep)
        return device

    return setup


def record_event(sid=1780584913):
    """An unsolicited cmd 20 (record start) packet, as a button press sends."""
    return bytes([1]) + struct.pack("<H", 20) + struct.pack("<I", sid) + bytes(5)


# -- BLE command channel ----------------------------------------------------------


def test_cmd_returns_only_the_matching_reply(ble):
    # Before the battery reply: a record event and a late cmd 4 reply.
    late_cmd4 = bytes([1]) + struct.pack("<H", 4) + bytes(6)
    device = ble(FakeNotePin(before={9: [record_event(), late_cmd4]}))

    async def go():
        cl, *_rest, cmd = await worker.ble_connect_and_setup()
        return await cmd("battery", bytes([1, 9, 0]))

    reply = asyncio.run(go())
    assert reply[:3] == bytes([1, 9, 0]) and reply[4] == 77
    assert device.sent_cmds() == [1, 1, 4, 9]


def test_cmd_without_reply_times_out_to_none(ble):
    device = ble(FakeNotePin(silent={36}))

    async def go():
        *_rest, cmd = await worker.ble_connect_and_setup()
        return await cmd("angles", bytes([1, 36, 0]), timeout=0.05)

    assert asyncio.run(go()) is None
    assert device.sent_cmds()[-1] == 36


def test_worker_handshake_rejection_raises_clear_error(ble, monkeypatch):
    monkeypatch.setattr(worker, "HANDSHAKE_REPLY_TIMEOUT_S", 0.05)
    device = ble(FakeNotePin(hs_status=1, silent={3, 4}))
    with pytest.raises(ValueError, match="rejected this Mac's pairing token"):
        asyncio.run(worker.ble_connect_and_setup())
    assert device.client.is_connected is False


def test_worker_sn_signature_uses_the_shared_count(ble):
    device = ble(FakeNotePin())
    asyncio.run(worker.ble_connect_and_setup())
    assert device.sig_writes == 3 * ble_proto.SN_SIG_SEND_REPEATS
    assert worker.SN_SIG_SEND_REPEATS is dev.SN_SIG_SEND_REPEATS
    assert worker.SN_SIG_REPLY_WAIT_S == dev.SN_SIG_REPLY_WAIT_S


@pytest.mark.parametrize(("offset", "expected"), [(-300, (-5, 0)), (330, (5, 30)), (0, (0, 0))])
def test_worker_time_sync_bytes_match_the_app(ble, monkeypatch, offset, expected):
    monkeypatch.setattr(ble_proto, "local_utc_offset_minutes", lambda at=None: offset)
    device = ble(FakeNotePin())
    asyncio.run(worker.ble_connect_and_setup())
    cmd4 = device.received[2]
    t = struct.unpack_from("<I", cmd4, 3)[0]
    # The worker and device.py build cmd 4 with the same helper.
    assert cmd4 == ble_proto.time_sync_request(now=t)
    assert struct.unpack_from("<bb", cmd4, 7) == expected


# -- closing the hotspot -----------------------------------------------------------


def _open_then_close(open_first=True):
    async def go():
        cl, *_rest, cmd = await worker.ble_connect_and_setup()
        if open_first:
            await cmd("WiFi_Open", ble_proto.WIFI_OPEN_REQUEST)
        await cl.stop_notify(worker.N)  # as transfer() does once the hotspot is up
        await worker._close_hotspot(cl, cmd)

    asyncio.run(go())


def test_close_hotspot_sees_the_reply(ble, statuses):
    device = ble(FakeNotePin())
    _open_then_close()
    assert device.received[-1] == ble_proto.WIFI_CLOSE_REQUEST
    assert any(s.startswith("Hotspot close reply: cmd 10 status 0") for s in statuses)
    # Notifications were re-armed with the command handler, then stopped again.
    assert device.notify_log[-2][0] == "start" and device.notify_log[-2][1] is not None
    assert device.notify_log[-1] == ("stop", None)


def test_close_hotspot_accepts_a_close_wifi_reply(ble, statuses):
    ble(FakeNotePin(reply_as={10: 13}))  # answers the close with a CloseWiFi (cmd 13) reply
    _open_then_close(open_first=False)
    assert any(s.startswith("Hotspot close reply: cmd 13 status 0") for s in statuses)


def test_close_hotspot_without_reply_is_short(ble, statuses, monkeypatch):
    monkeypatch.setattr(worker, "CLOSE_HOTSPOT_REPLY_S", 0.05)
    ble(FakeNotePin(silent={10}))
    loop_time = []

    async def go():
        cl, *_rest, cmd = await worker.ble_connect_and_setup()
        start = asyncio.get_running_loop().time()
        await worker._close_hotspot(cl, cmd)
        loop_time.append(asyncio.get_running_loop().time() - start)

    asyncio.run(go())
    assert "Hotspot close reply: no reply" in statuses
    assert loop_time[0] < 1
    assert worker.CLOSE_HOTSPOT_TIMEOUT_S <= 10
