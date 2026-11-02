"""Finding a paired NotePin when its advertising packets don't carry the serial number.

macOS reports each advertising packet separately, and not every packet has the
manufacturer data with the serial in it. Without a serial the connect handshake
can't look up the device's credentials ("Device serial number unknown"), so the
scan merges later packets and falls back to the paired device's last address.
"""

import asyncio
import base64
import struct
from types import SimpleNamespace

import bleak
import pytest

import app.state.device as dev
from tests.plaud_fakes import FakeNotePin

SN = "8820A00012345678"
OTHER_SN = "8820A00087654321"
ADDR = "5AF00B65-0000-0000-0000-000000000001"
OTHER_ADDR = "5AF00B65-0000-0000-0000-000000000002"


def mfr(sn_hex):
    raw = bytes.fromhex(sn_hex)
    # [len][V][version u16][unknown][sn_len][sn...][pad][port_version] (see src/scanner.py)
    return {0x5D: bytes([0, 0x56]) + struct.pack("<H", 1) + bytes([0, len(raw)]) + raw + bytes([0, 9])}


def adv(name="PLAUD_NOTEPIN", mfr_data=None, rssi=-50):
    return SimpleNamespace(local_name=name, manufacturer_data=mfr_data or {}, rssi=rssi)


def device(address, name="PLAUD_NOTEPIN"):
    return SimpleNamespace(address=address, name=name)


@pytest.fixture
def fake_scanner(monkeypatch):
    """BleakScanner that delivers the given (device, adv) packets on start()."""
    packets = []

    class FakeScanner:
        def __init__(self, detection_callback):
            self.cb = detection_callback

        async def start(self):
            for d, a in packets:
                self.cb(d, a)

        async def stop(self):
            pass

    monkeypatch.setattr(bleak, "BleakScanner", FakeScanner)
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        return await real_sleep(0, *a, **k)

    monkeypatch.setattr(dev.asyncio, "sleep", fast_sleep)
    return packets


@pytest.fixture
def paired(monkeypatch):
    config = {"paired_devices": {SN: {"name": "Plaud", "last_address": ADDR}}}
    monkeypatch.setattr(dev, "load_config", lambda: config)
    return config


def test_serial_from_a_later_packet_is_kept(fake_scanner):
    """First packet has no manufacturer data; the second one has the serial."""
    fake_scanner += [(device(ADDR), adv(mfr_data={})), (device(ADDR), adv(mfr_data=mfr(SN), rssi=-40))]
    found = asyncio.run(dev.scan_devices(1))
    assert len(found) == 1
    assert found[0]["serial_number"] == SN
    assert found[0]["rssi"] == -40


def test_first_serial_is_not_overwritten(fake_scanner):
    fake_scanner += [(device(ADDR), adv(mfr_data=mfr(SN))), (device(ADDR), adv(mfr_data=mfr(OTHER_SN)))]
    found = asyncio.run(dev.scan_devices(1))
    assert found[0]["serial_number"] == SN


def test_paired_serial_for_address(paired):
    assert dev.paired_serial_for_address(ADDR) == SN
    assert dev.paired_serial_for_address(OTHER_ADDR) is None
    assert dev.paired_serial_for_address(None) is None


def test_paired_serial_for_address_ambiguous(monkeypatch):
    config = {"paired_devices": {SN: {"last_address": ADDR}, OTHER_SN: {"last_address": ADDR}}}
    monkeypatch.setattr(dev, "load_config", lambda: config)
    assert dev.paired_serial_for_address(ADDR) is None


def test_scan_device_matches_paired_device_by_address(fake_scanner, paired):
    """No serial in any packet, but the address is the paired device's last one."""
    fake_scanner += [(device(OTHER_ADDR), adv(rssi=-30)), (device(ADDR), adv(rssi=-60))]
    result = asyncio.run(dev.scan_device())
    assert result["found"] is True
    assert result["address"] == ADDR
    assert result["serial_number"] == SN


def test_scan_device_assumes_the_only_device_is_the_only_paired_one(fake_scanner, paired):
    fake_scanner += [(device(OTHER_ADDR), adv())]
    result = asyncio.run(dev.scan_device())
    assert result["address"] == OTHER_ADDR
    assert result["serial_number"] == SN


def test_scan_device_does_not_guess_with_several_devices(fake_scanner, paired):
    fake_scanner += [(device(OTHER_ADDR), adv(rssi=-30)), (device("third"), adv(rssi=-40))]
    result = asyncio.run(dev.scan_device())
    assert result["address"] == OTHER_ADDR
    assert "serial_number" not in result


def test_scan_device_does_not_relabel_another_devices_serial(fake_scanner, paired):
    """A device that says it's someone else's keeps its own serial."""
    fake_scanner += [(device(OTHER_ADDR), adv(mfr_data=mfr(OTHER_SN)))]
    result = asyncio.run(dev.scan_device())
    assert result["serial_number"] == OTHER_SN


def test_scan_device_unpaired_returns_advertised_serial(fake_scanner, monkeypatch):
    monkeypatch.setattr(dev, "load_config", lambda: {})
    fake_scanner += [(device(ADDR), adv(mfr_data=mfr(SN)))]
    result = asyncio.run(dev.scan_device())
    assert result["serial_number"] == SN


@pytest.fixture
def connect_env(tmp_path, monkeypatch, paired):
    monkeypatch.setattr(dev, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(dev, "_HANDSHAKE_REPLY_TIMEOUT_S", 0.1)
    monkeypatch.setattr(dev, "SN_SIG_REPLY_WAIT_S", 1)
    sigs = {SN: base64.b64encode(bytes(256)).decode()}
    monkeypatch.setattr("app.state.cloud.get_device_sn_signature", lambda sn: sigs.get(sn))
    monkeypatch.setattr("app.state.cloud.get_device_bind_token", lambda sn: "T" * 32)
    monkeypatch.setattr("app.state.cloud.is_logged_in", lambda: False)
    dev._reset_state()
    monkeypatch.setitem(dev._device_state, "serial_number", None)
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        return await real_sleep(min(delay, 0.001), *a, **k)

    monkeypatch.setattr(dev.asyncio, "sleep", fast_sleep)
    yield
    dev._reset_state()
    dev._device_state["serial_number"] = None


def _connect(monkeypatch, notepin, address):
    monkeypatch.setattr(bleak, "BleakClient", notepin.client_factory)
    monkeypatch.setattr(dev, "rsa_decrypt", notepin.session_plaintext)
    monkeypatch.setattr(dev, "load_rsa_private_key", lambda *a: None)
    return asyncio.run(dev.connect_device(address))


def test_connect_without_serial_uses_paired_address(connect_env, monkeypatch):
    """The v2.4.1 failure: connect called with an address and no serial number."""
    result = _connect(monkeypatch, FakeNotePin(), ADDR)
    assert result["connected"] is True
    assert dev._device_state["serial_number"] == SN


def test_connect_without_serial_unknown_address_still_explains(connect_env, monkeypatch):
    notepin = FakeNotePin()
    result = _connect(monkeypatch, notepin, OTHER_ADDR)
    assert result["connected"] is False
    assert "serial number unknown" in result["error"]
    # The Mac must let go of the link, or the NotePin stops advertising and
    # every later scan says "Device not found".
    assert notepin.client.is_connected is False
    assert dev._device_state["client"] is None


def test_silent_device_is_disconnected(connect_env, monkeypatch):
    """No reply to the SN signature: the failed connect also hangs up."""
    notepin = FakeNotePin()
    notepin.on_write = lambda data, response: None  # never answers
    monkeypatch.setattr(dev, "SN_SIG_REPLY_WAIT_S", 0.1)
    result = _connect(monkeypatch, notepin, ADDR)
    assert result["connected"] is False
    assert "did not respond" in result["error"]
    assert notepin.client.is_connected is False
    assert dev._device_state["client"] is None


def test_connect_route_fills_serial_from_paired_address(monkeypatch, paired):
    from app.routes import device as routes

    seen = {}
    monkeypatch.setattr(routes.dev, "set_serial_number", lambda sn: seen.setdefault("sn", sn))

    async def fake_connect(address):
        return {"connected": False, "error": "x"}

    monkeypatch.setattr(routes.dev, "connect_device", fake_connect)
    monkeypatch.setattr(routes.dev, "run_async", lambda coro, **kw: asyncio.run(coro))
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(routes.bp, url_prefix="/device")
    with app.test_request_context("/device/connect", method="POST", json={"address": ADDR}):
        routes.connect()
    assert seen["sn"] == SN


def test_missing_credentials_signed_out_explains_pairing(connect_env, monkeypatch):
    """No SN signature on this Mac and not signed in: say where it comes from."""
    monkeypatch.setattr("app.state.cloud.get_device_sn_signature", lambda sn: None)
    notepin = FakeNotePin()
    result = _connect(monkeypatch, notepin, ADDR)
    assert result["connected"] is False
    assert result["error"] == (
        f"This Mac doesn't have the pairing credentials for this NotePin (serial ending {SN[-4:]}). "
        "They come from Plaud once per device: sign in under Settings › Plaud Cloud, connect, "
        "then you can sign out."
    )
    assert SN not in result["error"]  # last 4 only
    assert notepin.client.is_connected is False


def test_missing_credentials_signed_in_passes_on_plauds_reason(connect_env, monkeypatch):
    monkeypatch.setattr("app.state.cloud.get_device_sn_signature", lambda sn: None)
    monkeypatch.setattr("app.state.cloud.is_logged_in", lambda: True)
    monkeypatch.setattr("app.state.cloud.bind_device", lambda sn: {"token": "T" * 32, "sn": sn})
    monkeypatch.setattr(
        "app.state.cloud.fetch_sn_signature",
        lambda sn: {"error": "Plaud wouldn't sign this NotePin's serial (device not bound)"},
    )
    result = _connect(monkeypatch, FakeNotePin(), ADDR)
    assert result["connected"] is False
    error = result["error"]
    assert f"serial ending {SN[-4:]}" in error and SN not in error
    assert "Plaud wouldn't sign this NotePin's serial (device not bound)." in error
    assert "T" * 32 not in error
