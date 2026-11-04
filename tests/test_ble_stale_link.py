"""A NotePin left connected to the Mac by a dropped BleakClient (e.g. after sleep).

A connected NotePin stops advertising, so scans find nothing until the app
restarts. Dropped clients get their CoreBluetooth link cancelled directly (bleak
skips the cancel unless the peripheral reports "connected"), and a scan that
finds nothing asks CoreBluetooth for NotePins still connected to this Mac.
"""

import asyncio
from types import SimpleNamespace

import bleak
import pytest

import app.state.device as dev

SN = "8820A00012345678"
ADDR = "5AF00B65-0000-0000-0000-000000000001"

# conftest stubs this for every test (no real Bluetooth); keep the real one.
_REAL_LOOKUP = dev._system_connected_notepins


class FakeCentral:
    def __init__(self):
        self.cancelled = []

    def cancelPeripheralConnection_(self, peripheral):
        self.cancelled.append(peripheral)


class FakeClient:
    """A BleakClient whose bleak-level disconnect does nothing (as after sleep)."""

    def __init__(self):
        self.central = FakeCentral()
        self.peripheral = object()
        self._backend = SimpleNamespace(
            _peripheral=self.peripheral,
            _central_manager_delegate=SimpleNamespace(central_manager=self.central),
        )

    async def stop_notify(self, _uuid):
        pass

    async def disconnect(self):
        pass  # bleak returns early: peripheral state isn't "connected"


def test_cancel_link_cancels_even_when_bleak_skips_disconnect():
    cl = FakeClient()
    assert dev._cancel_link(cl) is True
    assert cl.central.cancelled == [cl.peripheral]


def test_cancel_link_without_backend_is_a_no_op():
    assert dev._cancel_link(object()) is False
    assert dev._cancel_link(SimpleNamespace(_backend=SimpleNamespace(_peripheral=None))) is False


def test_disconnect_quietly_cancels_and_remembers_the_client():
    cl = FakeClient()
    asyncio.run(dev._disconnect_quietly(cl))
    assert cl.central.cancelled == [cl.peripheral]
    assert dev._orphaned_clients == [cl]


def test_disconnect_device_cancels_the_link():
    cl = FakeClient()
    dev._device_state["client"] = cl
    dev._device_state["connected"] = True
    asyncio.run(dev.disconnect_device())
    assert cl.central.cancelled == [cl.peripheral]
    assert dev._device_state["connected"] is False


def test_orphan_list_is_bounded():
    for _ in range(dev._ORPHAN_LIMIT + 5):
        dev._orphan(FakeClient())
    assert len(dev._orphaned_clients) == dev._ORPHAN_LIMIT


@pytest.fixture
def empty_scan(monkeypatch):
    """BleakScanner that sees no advertising at all."""

    class FakeScanner:
        def __init__(self, detection_callback):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(bleak, "BleakScanner", FakeScanner)
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        return await real_sleep(0, *a, **k)

    monkeypatch.setattr(dev.asyncio, "sleep", fast_sleep)


@pytest.fixture
def stuck_connected(monkeypatch):
    """CoreBluetooth reports the NotePin as connected to this Mac."""
    ble_device = SimpleNamespace(address=ADDR, name="Plaud NotePin S", details=("peripheral", "manager"))
    calls = []

    async def connected():
        calls.append(1)
        return [ble_device]

    monkeypatch.setattr(dev, "_system_connected_notepins", connected)
    monkeypatch.setitem(dev._device_state, "connected", False)
    return SimpleNamespace(device=ble_device, calls=calls)


@pytest.fixture
def paired(monkeypatch):
    config = {"paired_devices": {SN: {"name": "Plaud", "last_address": ADDR}}}
    monkeypatch.setattr(dev, "load_config", lambda: config)


def test_empty_scan_finds_the_stuck_notepin_and_releases_dropped_links(empty_scan, stuck_connected):
    old = FakeClient()
    dev._orphan(old)
    found = asyncio.run(dev.scan_devices(1))
    assert found == [{"name": "Plaud NotePin S", "address": ADDR, "connected_to_mac": True}]
    assert old.central.cancelled == [old.peripheral]
    assert dev._orphaned_clients == []
    # connect_device will connect through the retrieved peripheral, not search again.
    assert dev._fresh_ble_device(ADDR) is stuck_connected.device


def test_scan_device_matches_the_stuck_notepin_to_its_paired_serial(empty_scan, stuck_connected, paired):
    result = asyncio.run(dev.scan_device())
    assert result["found"] is True
    assert result["address"] == ADDR
    assert result["serial_number"] == SN


def test_no_lookup_while_the_app_is_connected(empty_scan, stuck_connected, monkeypatch):
    monkeypatch.setitem(dev._device_state, "connected", True)
    assert asyncio.run(dev.scan_devices(1)) == []
    assert stuck_connected.calls == []


def test_no_lookup_when_the_scan_finds_something(stuck_connected, monkeypatch):
    class FakeScanner:
        def __init__(self, detection_callback):
            self.cb = detection_callback

        async def start(self):
            self.cb(
                SimpleNamespace(address="other", name="PLAUD_NOTEPIN"),
                SimpleNamespace(local_name="PLAUD_NOTEPIN", manufacturer_data={}, rssi=-50),
            )

        async def stop(self):
            pass

    monkeypatch.setattr(bleak, "BleakScanner", FakeScanner)
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        return await real_sleep(0, *a, **k)

    monkeypatch.setattr(dev.asyncio, "sleep", fast_sleep)
    found = asyncio.run(dev.scan_devices(1))
    assert [d["address"] for d in found] == ["other"]
    assert stuck_connected.calls == []


def test_system_lookup_is_empty_off_macos(monkeypatch):
    monkeypatch.setattr(dev.sys, "platform", "linux")
    assert asyncio.run(_REAL_LOOKUP()) == []
