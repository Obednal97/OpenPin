"""WiFi sync worker: interface detection, exact-size acceptance, broken-pipe safety.

Nothing here touches the real network: subprocess.run is replaced, and the
networksetup call that's simulated is the read-only -listallhardwareports.
"""

import asyncio
import subprocess
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import app.utils.wifi_sync_worker as worker
from tests.plaud_fakes import make_plaud_file, py310_asyncio


@pytest.fixture(scope="module")
def app_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


MAC_MINI_PORTS = """
Hardware Port: Ethernet
Device: en0
Ethernet Address: 00:00:00:00:00:01

Hardware Port: Thunderbolt Bridge
Device: bridge0
Ethernet Address: N/A

Hardware Port: Wi-Fi
Device: en1
Ethernet Address: 00:00:00:00:00:02

VLAN Configurations
===================
"""

MACBOOK_PORTS = """
Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 00:00:00:00:00:03
"""

OLD_MACOS_PORTS = """
Hardware Port: Ethernet
Device: en0

Hardware Port: AirPort
Device: en1
"""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (MAC_MINI_PORTS, "en1"),
        (MACBOOK_PORTS, "en0"),
        (OLD_MACOS_PORTS, "en1"),
        ("Hardware Port: Ethernet\nDevice: en0\n", None),
        ("", None),
    ],
)
def test_parse_wifi_device(text, expected):
    assert worker.parse_wifi_device(text) == expected


@pytest.fixture
def fresh_iface_cache(monkeypatch):
    monkeypatch.setattr(worker, "_wifi_iface_cache", None)


def test_interface_falls_back_to_networksetup_listing(monkeypatch, fresh_iface_cache):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        assert cmd == ["networksetup", "-listallhardwareports"], "only the read-only listing may run"
        return SimpleNamespace(stdout=MAC_MINI_PORTS, returncode=0)

    monkeypatch.setattr(worker, "_corewlan_interface_name", lambda: None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert worker.get_wifi_interface() == "en1"
    assert worker.get_wifi_interface() == "en1"
    assert len(calls) == 1, "detected once, then cached"


def test_interface_prefers_corewlan(monkeypatch, fresh_iface_cache):
    monkeypatch.setattr(worker, "_corewlan_interface_name", lambda: "en7")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("networksetup should not be needed"))
    assert worker.get_wifi_interface() == "en7"


def test_interface_defaults_to_en0(monkeypatch, fresh_iface_cache):
    monkeypatch.setattr(worker, "_corewlan_interface_name", lambda: None)

    def boom(*a, **k):
        raise OSError("no networksetup")

    monkeypatch.setattr(subprocess, "run", boom)
    assert worker.get_wifi_interface() == "en0"


def test_status_survives_broken_pipe(monkeypatch):
    class DeadPipe:
        def write(self, s):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    monkeypatch.setattr(worker.sys, "stderr", DeadPipe())
    worker.status("still running restore", 90)  # must not raise


def test_wifi_exact_size_download_saved(tmp_path, app_key):
    data = make_plaud_file(app_key)
    (tmp_path / "5_partial.bin").write_bytes(data[:100])
    e = worker.save_wifi_download(tmp_path, 5, data, len(data), app_key)
    assert e["status"] == "new"
    assert "size_mismatch" not in e
    assert (tmp_path / "5.ogg").exists()
    assert not (tmp_path / "5_partial.bin").exists()
    assert not (tmp_path / "5_encrypted.bin").exists()  # small file: backup dropped


def test_wifi_size_mismatch_kept_if_it_decrypts(tmp_path, app_key, caplog):
    data = make_plaud_file(app_key)
    short = data[: int(len(data) * 0.95)]
    with caplog.at_level("WARNING"):
        e = worker.save_wifi_download(tmp_path, 7, short, len(data), app_key)
    assert e["status"] == "new"
    assert e["size_mismatch"] == {"received": len(short), "expected": len(data)}
    assert (tmp_path / "7.ogg").exists()
    assert (tmp_path / "7_encrypted.bin").read_bytes() == short  # kept for diagnosis
    assert "[WiFi] size mismatch accepted for session 7" in caplog.text


def test_wifi_size_mismatch_that_does_not_decrypt_is_partial(tmp_path, app_key):
    data = make_plaud_file(app_key, decryptable=False)
    short = data[: int(len(data) * 0.95)]
    e = worker.save_wifi_download(tmp_path, 8, short, len(data), app_key)
    assert e["status"] == "partial"
    assert (e["received"], e["expected"]) == (len(short), len(data))
    assert "decrypt failed" in e["reason"]
    assert not (tmp_path / "8.ogg").exists()
    assert (tmp_path / "8_partial.bin").read_bytes() == short

    e = worker.save_wifi_download(tmp_path, 9, b"", 1000, app_key)
    assert e["status"] == "partial"
    assert not (tmp_path / "9_partial.bin").exists()


def test_wifi_worker_never_deletes_an_existing_ogg(tmp_path):
    """A cropped recording is far smaller than the device copy; it must survive."""
    cropped = tmp_path / "11.ogg"
    cropped.write_bytes(b"OggS" + b"\0" * 96)
    (tmp_path / "13.ogg.partial").write_bytes(b"legacy")
    queue = [
        {"session_id": 11, "size": 5_000_000},
        {"session_id": 12, "size": 10},
        {"session_id": 13, "size": 20},
    ]
    new_files = []
    nxt = worker.next_wifi_download(queue, tmp_path, {"12"}, new_files)
    assert nxt == {"session_id": 13, "size": 20}
    assert cropped.read_bytes() == b"OggS" + b"\0" * 96
    assert new_files == [
        {"session_id": 11, "ogg_path": str(cropped), "duration": 0, "status": "exists"},
        {"session_id": 12, "ogg_path": None, "duration": 0, "status": "exists"},
    ]
    assert not (tmp_path / "13.ogg.partial").exists()  # legacy leftovers (never an .ogg) still cleaned
    assert worker.next_wifi_download(queue, tmp_path, set(), new_files) is None


def test_wait_event_handles_py310_asyncio_timeout(monkeypatch):
    monkeypatch.setattr(worker, "asyncio", py310_asyncio())

    async def go():
        evt = asyncio.Event()
        timed_out = await worker.wait_event(evt, 0.01)
        evt.set()
        return timed_out, await worker.wait_event(evt, 0.01)

    assert asyncio.run(go()) == (False, True)


def test_wifi_decrypt_failure_keeps_encrypted(tmp_path, app_key):
    data = make_plaud_file(app_key, decryptable=False)
    e = worker.save_wifi_download(tmp_path, 6, data, len(data), app_key)
    assert e["status"] == "decrypt_failed"
    assert not (tmp_path / "6.ogg").exists()
    assert (tmp_path / "6_encrypted.bin").read_bytes() == data
