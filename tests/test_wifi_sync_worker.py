"""WiFi sync worker: interface detection, exact-size acceptance, broken-pipe safety.

Nothing here touches the real network: subprocess.run is replaced, and the
networksetup call that's simulated is the read-only -listallhardwareports.
"""

import asyncio
import io
import json
import os
import signal
import subprocess
import threading
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import app.utils.wifi_sync_worker as worker
from tests.plaud_fakes import FakeMac, Gate, make_plaud_file


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


def test_wifi_decrypt_failure_keeps_encrypted(tmp_path, app_key):
    data = make_plaud_file(app_key, decryptable=False)
    e = worker.save_wifi_download(tmp_path, 6, data, len(data), app_key)
    assert e["status"] == "decrypt_failed"
    assert not (tmp_path / "6.ogg").exists()
    assert (tmp_path / "6_encrypted.bin").read_bytes() == data


# ── Saved networks, restore record, lock, SIGTERM, budget ────────────────────
#
# networksetup/ipconfig are replaced by FakeMac; CoreWLAN isn't available here,
# and the CoreWLAN security lookup is patched per test.

HOME = ["Home 5G", "Office", "Cafe Open"]
# CWSecurity per network: WPA2 Personal, WPA3 Transition, open; the hotspot WPA2.
SECURITY = {"Home 5G": 4, "Office": 13, "Cafe Open": 0, "Plaud1234": 4}


def corewlan_profiles(fake, security=SECURITY):
    """CoreWLAN's view of the same list; SSIDs hidden (no Location permission)."""
    return lambda: [(None, security[s]) for s in fake.preferred]


@pytest.fixture
def mac(monkeypatch):
    fake = FakeMac(preferred=HOME, ip="10.0.0.5")
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(worker, "_wifi_iface_cache", "en1")
    monkeypatch.setattr(worker, "_corewlan_profiles", corewlan_profiles(fake))
    monkeypatch.setattr(worker, "_restore_via_corewlan", lambda networks: False)
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    return fake


def record(tmp_path):
    return worker.read_restore_record(tmp_path)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Preferred networks on en1:\n\tHome\n\t Spaced SSID \n", ["Home", " Spaced SSID "]),
        ("Preferred networks on en1:\n", []),
        ("No preferred networks found on en1.\n", []),
        ("en9 is not a Wi-Fi interface.\n** Error: Error obtaining wireless information.\n", None),
        ("", None),
    ],
)
def test_parse_preferred_networks(text, expected):
    assert worker.parse_preferred_networks(text) == expected


def test_snapshot_uses_networksetup_order_and_corewlan_security(mac):
    snap = worker.snapshot_preferred_networks()
    assert [n["ssid"] for n in snap] == HOME
    assert [n["security"] for n in snap] == ["WPA2", "WPA2", "OPEN"]
    assert worker.can_isolate(snap)


def test_snapshot_security_by_name_or_unknown(mac, monkeypatch):
    # With Location permission CoreWLAN names the profiles: matched by SSID.
    monkeypatch.setattr(worker, "_corewlan_profiles", lambda: [("Cafe Open", 0), ("Home 5G", 4)])
    snap = worker.snapshot_preferred_networks()
    assert [n["security"] for n in snap] == ["WPA2", None, "OPEN"]
    assert not worker.can_isolate(snap)
    # Hidden SSIDs and a different count: positions can't be trusted.
    monkeypatch.setattr(worker, "_corewlan_profiles", lambda: [(None, 4), (None, 4)])
    assert all(n["security"] is None for n in worker.snapshot_preferred_networks())
    monkeypatch.setattr(worker, "_corewlan_profiles", lambda: None)  # no pyobjc
    assert not worker.can_isolate(worker.snapshot_preferred_networks())


def test_unrestorable_networks_are_never_removed(mac, tmp_path, monkeypatch):
    # An enterprise network can't be re-added by networksetup without its 802.1X setup.
    monkeypatch.setattr(worker, "_corewlan_profiles", corewlan_profiles(mac, {**SECURITY, "Office": 9}))
    run = worker.WifiRun(tmp_path, "Plaud1234", "10.0.0.5")
    run.begin()
    assert run.isolate() is False
    assert not mac.ops("-removeallpreferredwirelessnetworks")
    assert mac.preferred == HOME
    assert record(tmp_path)["isolated"] is False


def test_unknown_list_is_never_removed(mac, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "list_preferred_networks", lambda: None)  # read failed
    run = worker.WifiRun(tmp_path, "Plaud1234")
    run.begin()
    assert run.isolate() is False
    assert not mac.ops("-removeallpreferredwirelessnetworks")


def test_record_written_before_removal_and_cleared_after_verified_restore(mac, tmp_path):
    run = worker.WifiRun(tmp_path, "Plaud1234", "10.0.0.5", "Home 5G")
    run.begin()
    assert record(tmp_path)["hotspot_ssid"] == "Plaud1234"  # durable before the join

    seen = {}
    mac.on_remove_all = lambda: seen.update(record=record(tmp_path))
    mac.preferred.insert(0, "Plaud1234")  # the join saved the hotspot
    run.set_hotspot_ip("192.168.84.2")
    assert run.isolate() is True

    rec = seen["record"]  # what was on disk at the moment of removal
    assert rec["isolated"] is True
    assert [n["ssid"] for n in rec["saved_networks"]] == HOME  # hotspot excluded
    assert rec["hotspot_ip"] == "192.168.84.2"
    assert mac.preferred == []

    mac.ip = "10.0.0.9"  # back on the home network
    assert run.restore_networks() is True
    assert run.finish(True) is True
    assert record(tmp_path) is None


def test_restore_preserves_original_order_and_security(mac):
    snap = worker.snapshot_preferred_networks()
    mac.preferred = []
    result = worker.restore_preferred_networks(snap)
    assert result == {"verified": True, "missing": [], "order_ok": True, "errors": {}}
    assert mac.preferred == HOME
    adds = mac.ops("-addpreferredwirelessnetworkatindex")
    assert [(c[3], c[4], c[5]) for c in adds] == [
        ("Home 5G", "0", "WPA2"),
        ("Office", "1", "WPA2"),
        ("Cafe Open", "2", "OPEN"),
    ]


def test_restore_skips_networks_already_present(mac):
    snap = worker.snapshot_preferred_networks()
    mac.preferred = ["Office"]
    assert worker.restore_preferred_networks(snap)["verified"]
    assert mac.preferred == HOME
    assert [c[3] for c in mac.ops("-addpreferredwirelessnetworkatindex")] == ["Home 5G", "Cafe Open"]


def test_unverified_restore_keeps_the_record(mac, tmp_path):
    run = worker.WifiRun(tmp_path, "Plaud1234", "10.0.0.5")
    run.begin()
    run.isolate()
    mac.fail_add = {"Office"}
    assert run.restore_networks() is False
    assert run.finish(False) is False
    rec = record(tmp_path)
    assert [n["ssid"] for n in rec["saved_networks"]] == HOME


# -- isolate() vs restore: the lock, the record, fsync -----------------------


def isolating_run(tmp_path):
    run = worker.WifiRun(tmp_path, "Plaud1234", "10.0.0.5")
    run.begin()
    run.set_hotspot_ip("192.168.84.2")
    return run


def test_restore_waits_for_isolation_in_flight(mac, tmp_path):
    """isolate() paused between its record write and the removal; a restore +
    finish() runs meanwhile. Without the lock the restore saw every network
    still there, finish() cleared the record, then the removal left the Mac
    with no saved networks and no record."""
    run = isolating_run(tmp_path)
    gate = Gate()
    mac.on_remove_all = gate
    isolating = threading.Thread(target=run.isolate)
    isolating.start()
    assert gate.reached.wait(5)
    assert record(tmp_path)["isolated"] is True  # durable, removal not yet run

    outcome = {}

    def cleanup():
        ok = run.restore_networks()
        outcome["finished"] = run.finish(ok)

    restoring = threading.Thread(target=cleanup)
    restoring.start()
    restoring.join(0.3)  # without the lock the whole cleanup would be done by now
    gate.open()
    isolating.join(5)
    restoring.join(5)
    assert not isolating.is_alive() and not restoring.is_alive()

    assert mac.preferred == HOME
    assert outcome["finished"] is True and record(tmp_path) is None
    ops = [c[1] for c in mac.calls if c[0] == "networksetup"]
    assert ops.index("-removeallpreferredwirelessnetworks") < ops.index("-addpreferredwirelessnetworkatindex")


def test_isolation_waits_for_restore_then_refuses(mac, tmp_path):
    run = isolating_run(tmp_path)
    gate = Gate()
    mac.hooks["-removepreferredwirelessnetwork"] = gate  # the restore pauses while forgetting the hotspot
    results = {}
    restoring = threading.Thread(target=lambda: results.update(restored=run.restore_networks()))
    restoring.start()
    assert gate.reached.wait(5)
    isolating = threading.Thread(target=lambda: results.update(isolated=run.isolate()))
    isolating.start()
    isolating.join(0.3)
    gate.open()
    restoring.join(5)
    isolating.join(5)
    assert results == {"restored": True, "isolated": False}
    assert not mac.ops("-removeallpreferredwirelessnetworks")
    assert mac.preferred == HOME
    assert run.finish(True) is True
    assert record(tmp_path) is None


def test_no_isolation_once_restore_started(mac, tmp_path):
    run = isolating_run(tmp_path)
    assert run.restore_networks() is True
    assert run.isolate() is False
    assert not mac.ops("-removeallpreferredwirelessnetworks")
    assert mac.preferred == HOME


def test_restore_uses_the_record_after_isolation(mac, tmp_path):
    run = isolating_run(tmp_path)
    assert run.isolate() is True
    run.saved_networks = run.saved_networks[:1]  # memory can't shrink what the record holds
    assert run.restore_networks() is True
    assert mac.preferred == HOME


def test_isolation_refused_if_record_cannot_be_fsynced(mac, tmp_path, monkeypatch):
    run = isolating_run(tmp_path)

    def broken_fsync(fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(worker.os, "fsync", broken_fsync)
    assert run.isolate() is False
    monkeypatch.undo()
    assert run.isolated is False and run.saved_networks is None
    assert not mac.ops("-removeallpreferredwirelessnetworks")
    assert mac.preferred == HOME
    assert record(tmp_path)["isolated"] is False


def test_isolation_refused_if_directory_cannot_be_fsynced(mac, tmp_path, monkeypatch):
    run = isolating_run(tmp_path)
    monkeypatch.setattr(worker, "_fsync_dir", lambda path: False)
    assert run.isolate() is False
    assert not mac.ops("-removeallpreferredwirelessnetworks")
    assert mac.preferred == HOME


def test_isolation_refused_without_a_state_dir(mac):
    run = worker.WifiRun(None, "Plaud1234", "10.0.0.5")
    run.begin()
    assert run.isolate() is False
    assert not mac.ops("-removeallpreferredwirelessnetworks")


# -- giving up on a restore that can't succeed ---------------------------------


def isolated_record(tmp_path, mac, **extra):
    snap = worker.snapshot_preferred_networks()
    worker.write_restore_record(
        tmp_path, {"hotspot_ssid": "Plaud1234", "isolated": True, "saved_networks": snap, **extra}
    )
    mac.preferred = []
    return snap


def test_recovery_gives_up_after_max_attempts(mac, tmp_path):
    snap = isolated_record(tmp_path, mac)
    mac.fail_add = {"Office"}  # a generic error: retried
    for attempt in range(1, worker.MAX_RESTORE_ATTEMPTS):
        result = worker.recover_from_record(tmp_path)
        assert result == {"status": "incomplete", "missing": ["Office"], "attempts": attempt}
        rec = record(tmp_path)
        assert rec["restore_attempts"] == attempt and rec["saved_networks"] == snap
        assert worker.restore_notice(tmp_path) is None
    result = worker.recover_from_record(tmp_path)
    assert result == {"status": "gave_up", "missing": ["Office"], "attempts": worker.MAX_RESTORE_ATTEMPTS}
    assert record(tmp_path) is None  # syncs aren't refused any more
    stuck = worker.read_stuck_record(tmp_path)
    assert stuck["saved_networks"] == snap and stuck["missing"] == ["Office"]
    assert stuck["last_errors"] == {"Office": "Error"}
    assert worker.recover_from_record(tmp_path) == {"status": "none"}  # no more retries at launch


def test_permanent_error_gives_up_at_once(mac, tmp_path):
    isolated_record(tmp_path, mac)
    mac.fail_add = {"Office"}
    mac.add_errors = {"Office": "** Error: The network Office was not found."}
    result = worker.recover_from_record(tmp_path)
    assert result["status"] == "gave_up" and result["attempts"] == 1
    assert record(tmp_path) is None
    assert mac.preferred == ["Home 5G", "Cafe Open"]  # the others were restored


def test_restore_notice_shown_until_acknowledged(mac, tmp_path):
    isolated_record(tmp_path, mac)
    mac.fail_add = {"Office", "Cafe Open"}
    mac.add_errors = {"Office": "Invalid network", "Cafe Open": "not found"}
    assert worker.restore_notice(tmp_path) is None
    worker.recover_from_record(tmp_path)
    notice = worker.restore_notice(tmp_path)
    assert notice["message"] == "Some saved WiFi networks couldn't be restored: Office, Cafe Open"
    assert notice["networks"] == ["Office", "Cafe Open"]
    assert worker.acknowledge_restore_notice(tmp_path) is True
    assert worker.restore_notice(tmp_path) is None
    assert worker.acknowledge_restore_notice(tmp_path) is False
    assert worker.read_stuck_record(tmp_path)["missing"] == ["Office", "Cafe Open"]  # kept for diagnostics


def test_mixed_errors_are_retried(mac, tmp_path):
    isolated_record(tmp_path, mac)
    mac.fail_add = {"Office", "Cafe Open"}
    mac.add_errors = {"Office": "not found"}  # Cafe Open: a generic error
    assert worker.recover_from_record(tmp_path)["status"] == "incomplete"


def test_worker_gives_up_on_leftover_and_starts_the_sync(mac, tmp_path, monkeypatch, capsys):
    isolated_record(tmp_path, mac, restore_attempts=worker.MAX_RESTORE_ATTEMPTS - 1)
    mac.fail_add = {"Office"}
    started = []

    async def fake_transfer(args):
        started.append(True)
        return {"files": [], "method": "wifi"}

    _worker_stdin(monkeypatch, tmp_path)
    monkeypatch.setattr(worker, "transfer", fake_transfer)
    asyncio.run(worker.main())
    assert started == [True]
    assert record(tmp_path) is None
    assert worker.restore_notice(tmp_path)["networks"] == ["Office"]


def test_in_run_restore_gives_up_on_permanent_errors(mac, tmp_path):
    run = isolating_run(tmp_path)
    assert run.isolate() is True
    mac.fail_add = {"Office"}
    mac.add_errors = {"Office": "Network not found"}
    assert run.restore_networks() is False
    mac.ip = "10.0.0.9"
    assert run.finish(False) is False
    assert run.gave_up is True
    assert record(tmp_path) is None
    assert worker.restore_notice(tmp_path)["networks"] == ["Office"]


def test_in_run_failed_restore_counts_an_attempt(mac, tmp_path):
    run = isolating_run(tmp_path)
    assert run.isolate() is True
    mac.fail_add = {"Office"}
    assert run.restore_networks() is False
    mac.ip = "10.0.0.9"
    assert run.finish(False) is False
    assert record(tmp_path)["restore_attempts"] == 1
    assert not run.gave_up


def test_give_up_while_on_hotspot_keeps_the_hotspot_part(mac, tmp_path):
    run = isolating_run(tmp_path)
    assert run.isolate() is True
    mac.fail_add = {"Office"}
    mac.add_errors = {"Office": "not found"}
    assert run.restore_networks() is False
    mac.ip = "192.168.84.2"  # still on the hotspot
    assert run.finish(False) is False
    rec = record(tmp_path)
    assert rec["isolated"] is False and rec["saved_networks"] is None
    assert rec["hotspot_ip"] == "192.168.84.2"
    mac.ip = "10.0.0.9"
    assert worker.recover_from_record(tmp_path)["status"] == "restored"
    assert record(tmp_path) is None


def test_hold_mode_forgets_hotspot_but_keeps_saved_networks(mac, tmp_path):
    run = worker.WifiRun(tmp_path, "Plaud1234", "10.0.0.5")
    run.begin()
    mac.preferred.insert(0, "Plaud1234")  # networksetup -setairportnetwork saved it
    run.set_hotspot_ip("192.168.84.2")
    mac.ip = "10.0.0.5"
    assert run.restore_networks() is True
    assert run.finish(True) is True
    assert mac.preferred == HOME
    assert not mac.ops("-removeallpreferredwirelessnetworks")
    assert record(tmp_path) is None


def test_hotspot_saved_before_is_kept(mac, tmp_path):
    mac.preferred.append("Plaud1234")
    run = worker.WifiRun(tmp_path, "Plaud1234")
    run.begin()
    assert run.restore_networks() is True
    assert "Plaud1234" in mac.preferred


def test_record_kept_while_still_on_hotspot(mac, tmp_path):
    run = worker.WifiRun(tmp_path, "Plaud1234")
    run.begin()
    run.set_hotspot_ip("192.168.84.2")
    mac.ip = "192.168.84.2"
    assert run.finish(True) is False
    assert record(tmp_path) is not None


def test_record_write_is_atomic(tmp_path):
    worker.write_restore_record(tmp_path, {"a": 1})
    worker.write_restore_record(tmp_path, {"a": 2})
    assert worker.read_restore_record(tmp_path) == {"a": 2}
    assert [p.name for p in tmp_path.iterdir()] == [worker.RESTORE_RECORD_NAME]
    worker.clear_restore_record(tmp_path)
    worker.clear_restore_record(tmp_path)  # idempotent
    assert worker.read_restore_record(tmp_path) is None


def test_corrupt_record_is_set_aside(tmp_path):
    (tmp_path / worker.RESTORE_RECORD_NAME).write_text("{not json")
    assert worker.read_restore_record(tmp_path) is None
    assert (tmp_path / (worker.RESTORE_RECORD_NAME + ".corrupt")).exists()


# -- one worker at a time ----------------------------------------------------


def test_second_worker_refused_by_lockfile(tmp_path):
    first, second = worker.WorkerLock(tmp_path), worker.WorkerLock(tmp_path)
    assert first.acquire()
    assert first.holder_pid() == os.getpid()
    assert not second.acquire()
    first.release()
    assert not (tmp_path / worker.LOCK_NAME).exists()
    assert second.acquire()
    second.release()


def _worker_stdin(monkeypatch, tmp_path):
    args = {"sn": "8810A00012345678", "bind_token": "t", "sn_sig": "s", "state_dir": str(tmp_path)}
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(json.dumps(args)))


def test_worker_refuses_to_start_while_another_runs(tmp_path, monkeypatch, capsys):
    held = worker.WorkerLock(tmp_path)
    assert held.acquire()
    _worker_stdin(monkeypatch, tmp_path)
    monkeypatch.setattr(worker, "transfer", lambda a: pytest.fail("must not start a transfer"))
    try:
        asyncio.run(worker.main())
    finally:
        held.release()
    assert json.loads(capsys.readouterr().out) == {"error": "Another WiFi transfer is already running", "busy": True}


def test_worker_restores_leftover_record_before_starting(mac, tmp_path, monkeypatch, capsys):
    # A killed worker left the saved networks removed: a new worker must put them
    # back before doing anything (and never snapshot the empty list).
    snap = worker.snapshot_preferred_networks()
    worker.write_restore_record(tmp_path, {"hotspot_ssid": "Plaud1234", "isolated": True, "saved_networks": snap})
    mac.preferred = []
    started = []

    async def fake_transfer(args):
        started.append(list(mac.preferred))
        return {"files": [], "method": "wifi"}

    _worker_stdin(monkeypatch, tmp_path)
    monkeypatch.setattr(worker, "transfer", fake_transfer)
    asyncio.run(worker.main())
    assert started == [HOME]
    assert record(tmp_path) is None
    assert json.loads(capsys.readouterr().out) == {"files": [], "method": "wifi"}


def test_worker_refuses_to_start_if_leftover_restore_fails(mac, tmp_path, monkeypatch, capsys):
    snap = worker.snapshot_preferred_networks()
    worker.write_restore_record(tmp_path, {"hotspot_ssid": "Plaud1234", "isolated": True, "saved_networks": snap})
    mac.preferred = []
    mac.fail_add = {"Office"}
    _worker_stdin(monkeypatch, tmp_path)
    monkeypatch.setattr(worker, "transfer", lambda a: pytest.fail("must not start a transfer"))
    asyncio.run(worker.main())
    out = json.loads(capsys.readouterr().out)
    assert out["wifi_restore_pending"] is True and "Office" in out["error"]
    assert record(tmp_path)["saved_networks"] == snap  # kept for the next try


# -- recovery from a leftover record (startup / after a kill) ----------------


def test_recover_from_record_restores_and_clears(mac, tmp_path):
    snap = worker.snapshot_preferred_networks()
    worker.write_restore_record(
        tmp_path,
        {"hotspot_ssid": "Plaud1234", "hotspot_ip": "192.168.84.2", "isolated": True, "saved_networks": snap},
    )
    mac.preferred = ["Plaud1234"]
    mac.ip = "192.168.84.2"  # still stuck on the hotspot
    result = worker.recover_from_record(tmp_path)
    assert result == {"status": "restored", "networks": 3}
    assert mac.preferred == HOME
    assert [c[3] for c in mac.ops("-setairportpower")] == ["off", "on"]
    assert record(tmp_path) is None


def test_recover_skips_while_worker_holds_lock(mac, tmp_path):
    worker.write_restore_record(tmp_path, {"hotspot_ssid": "Plaud1234", "isolated": False})
    lock = worker.WorkerLock(tmp_path)
    assert lock.acquire()
    try:
        assert worker.recover_from_record(tmp_path) == {"status": "busy", "pid": os.getpid()}
    finally:
        lock.release()
    assert not mac.calls
    assert record(tmp_path) is not None


def test_recover_without_record_does_nothing(mac, tmp_path):
    assert worker.recover_from_record(tmp_path) == {"status": "none"}
    assert not mac.calls


# -- SIGTERM ------------------------------------------------------------------


def test_run_guarded_sigterm_runs_cleanup_once():
    events = []

    async def body():
        events.append("body")
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(30)
        events.append("body finished")  # must not happen

    async def cleanup():
        events.append("cleanup start")
        os.kill(os.getpid(), signal.SIGTERM)  # a second SIGTERM can't interrupt the restore
        await asyncio.sleep(0.05)
        events.append("cleanup done")

    assert asyncio.run(worker.run_guarded(body, cleanup)) is None
    assert events == ["body", "cleanup start", "cleanup done"]


def test_run_guarded_returns_body_result_and_cleans_up():
    events = []

    async def body():
        return {"ok": 1}

    async def cleanup():
        events.append("cleanup")

    assert asyncio.run(worker.run_guarded(body, cleanup)) == {"ok": 1}
    assert events == ["cleanup"]


def test_sigterm_mid_transfer_restores_saved_networks(mac, tmp_path):
    run = worker.WifiRun(tmp_path, "Plaud1234", "10.0.0.5")

    async def body():
        run.begin()
        run.set_hotspot_ip("192.168.84.2")
        mac.ip = "192.168.84.2"
        assert run.isolate()
        assert mac.preferred == []
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(30)

    async def cleanup():
        ok = run.restore_networks()
        mac.ip = "10.0.0.7"  # macOS auto-joined once the hotspot closed
        run.finish(ok)

    assert asyncio.run(worker.run_guarded(body, cleanup)) is None
    assert mac.preferred == HOME
    assert record(tmp_path) is None


def test_transfer_sigterm_cleans_up_in_order(mac, tmp_path, monkeypatch, capsys):
    """Whole transfer() with BLE, WebSocket and the join faked: SIGTERM while
    waiting for the device → hotspot forgotten, then closed over BLE, then
    internet restored, record deleted, BLE disconnected."""
    events = []

    class Client:
        async def start_notify(self, uuid, cb):
            pass

        async def stop_notify(self, uuid):
            pass

        async def disconnect(self):
            events.append("ble disconnect")

    async def ble_cmd(name, pt, **kw):
        events.append(name)

    async def fake_ble():
        return Client(), b"k" * 32, b"n" * 12, b"a" * 12, None, ble_cmd

    class Server:
        def close(self):
            events.append("ws closed")

    async def fake_server(session):
        return Server()

    async def fake_connect(ssid, password, original_ip, timeout=45):
        events.append(f"join {ssid}")
        mac.preferred.insert(0, ssid)
        mac.ip = "192.168.84.2"
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        return mac.ip

    async def fake_restore_internet(ssid, hotspot_ip, original_ip="", original_ssid=""):
        events.append(f"restore_internet {hotspot_ip}")
        mac.ip = "10.0.0.8"
        return {"restored": True, "ssid": "", "ip": mac.ip}

    real_forget = worker.forget_network

    def forget(ssid):
        events.append(f"forget {ssid}")
        return real_forget(ssid)

    key_file = tmp_path / "keys" / "app.pem"
    key_file.parent.mkdir()
    key_file.write_bytes(b"pem")
    monkeypatch.setattr(worker, "ble_connect_and_setup", fake_ble)
    monkeypatch.setattr(worker, "load_pem_private_key", lambda data, password: None)
    monkeypatch.setattr(worker, "_start_ws_server", fake_server)
    monkeypatch.setattr(worker, "connect_to_hotspot", fake_connect)
    monkeypatch.setattr(worker, "restore_internet", fake_restore_internet)
    monkeypatch.setattr(worker, "forget_network", forget)
    monkeypatch.setattr(worker, "_get_current_ssid", lambda: "")
    monkeypatch.setattr(worker.asyncio, "sleep", _fast_sleep)
    args = {
        "sn": "8810A00012345678",
        "recordings_dir": str(tmp_path / "rec"),
        "app_key_path": str(key_file),
        "state_dir": str(tmp_path),
    }
    result = asyncio.run(worker.transfer(args))
    assert result["terminated"] is True
    assert result["wifi_restore"]["restored"] is True
    assert events == [
        "WiFi_Open",
        "join Plaud5678",
        "ws closed",
        "forget Plaud5678",
        "WiFi_Close",
        "restore_internet 192.168.84.2",
        "ble disconnect",
    ]
    assert mac.preferred == HOME
    assert record(tmp_path) is None


_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *a, **k):
    """asyncio.sleep, but long waits (polls, settle delays) last at most 10 ms."""
    return await _real_sleep(min(delay, 0.01), *a, **k)


# -- time budget --------------------------------------------------------------


def test_compute_budget():
    fixed = worker.SETUP_BUDGET_S + worker.HOLD_BUDGET_S + worker.RESTORE_BUDGET_S
    assert worker.compute_budget(0) == {"download_s": 60, "total_s": fixed + 60}
    assert worker.compute_budget(1_000_000)["download_s"] == 60  # minimum
    assert worker.compute_budget(30_000_001) == {"download_s": 601, "total_s": fixed + 601}
    assert worker.compute_budget(-5)["download_s"] == 60


def test_status_carries_budget_fields(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(worker.sys, "stderr", buf)
    worker.status("found", 35, budget_s=900, remaining_s=700)
    worker.status("still going")  # keeps the last percent
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert lines == [
        {"status": "found", "percent": 35, "budget_s": 900, "remaining_s": 700},
        {"status": "still going", "percent": 35},
    ]


# -- WebSocket session: who may connect, reconnects ---------------------------


def make_session(tmp_path):
    return worker.WifiSession(None, None, None, "tok", tmp_path, {"9"}, None, original_ip="10.0.0.5")


def test_ws_admits_only_the_hotspot_peer(tmp_path):
    s = make_session(tmp_path)
    hotspot = "192.168.84.2"
    assert s.admit(hotspot, "192.168.84.1", hotspot) == (True, "")
    # Before the Mac has joined the hotspot (WiFi still on the home network).
    assert not s.admit("10.0.0.5", "10.0.0.20", "10.0.0.5")[0]
    # Arrived over another interface (Ethernet / LAN).
    assert not s.admit("10.1.1.4", "10.1.1.9", hotspot)[0]
    # Right interface, but the peer isn't on the hotspot subnet.
    assert not s.admit(hotspot, "192.168.1.50", hotspot)[0]
    s.active = object()
    assert s.admit(hotspot, "192.168.84.1", hotspot) == (False, "another device connection is active")


def test_ws_handler_rejects_second_connection(tmp_path, monkeypatch):
    s = make_session(tmp_path)
    s.active = object()
    monkeypatch.setattr(worker, "get_wifi_ip", lambda: "192.168.84.2")

    class Conn:
        local_address = ("192.168.84.2", 8081)
        remote_address = ("192.168.84.1", 50000)
        closed = None

        async def close(self, code, reason):
            Conn.closed = (code, reason)

        def __aiter__(self):
            pytest.fail("a refused connection must not be read")

    asyncio.run(s.handler(Conn()))
    assert Conn.closed == (1008, "another device connection is active")
    assert s.lost_at is None


def test_reconnect_does_not_duplicate_files(tmp_path):
    s = make_session(tmp_path)
    first = [{"session_id": 1, "size": 10, "codec": 0}, {"session_id": 2, "size": 20, "codec": 0}]
    s.merge_file_list(first)
    s.new_files.append({"session_id": 1, "ogg_path": "x", "duration": 0, "status": "new"})
    # The device reconnects and sends the whole list again (plus a new file).
    s.merge_file_list(first + [{"session_id": 3, "size": 30, "codec": 0}])
    assert [f["session_id"] for f in s.wifi_files] == [1, 2, 3]
    assert [f["session_id"] for f in s.pending_files()] == [2, 3]
    (tmp_path / "3.ogg").write_bytes(b"OggS")
    assert s.bytes_to_download() == 20


def test_lost_for(tmp_path):
    s = make_session(tmp_path)
    assert s.lost_for(now=100.0) == 0.0
    s.lost_at = 90.0
    assert s.lost_for(now=100.0) == 10.0
    s.active = object()
    assert s.lost_for(now=100.0) == 0.0


# -- holding the hotspot ------------------------------------------------------


def run_hold(monkeypatch, ips, rejoin_ips, run):
    """Drive HotspotHold with a scripted sequence of Wi-Fi IPs."""
    ips = iter(ips)
    rejoins = iter(rejoin_ips)
    monkeypatch.setattr(worker, "get_wifi_ip", lambda: next(ips, run.hotspot_ip))

    async def fake_connect(ssid, password, original_ip, timeout=45):
        return next(rejoins)

    monkeypatch.setattr(worker, "connect_to_hotspot", fake_connect)
    hold = worker.HotspotHold(run, "Plaud1234", "pass", poll_s=0, max_rejoins=2)

    async def go():
        task = asyncio.create_task(hold.watch())
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    return hold


def test_hold_rejoins_then_isolates_when_auto_join_keeps_winning(mac, tmp_path, monkeypatch):
    run = worker.WifiRun(tmp_path, "Plaud1234", "10.0.0.5")
    run.begin()
    run.set_hotspot_ip("192.168.84.2")
    hs, home = "192.168.84.2", "10.0.0.5"
    # One blip is ignored; two misses = drift → rejoin; a second drift → isolate + rejoin.
    hold = run_hold(monkeypatch, [hs, home, hs, home, home, hs, home, home], [hs, hs], run)
    assert hold.drifts == 2
    assert hold.failed is None
    assert run.isolated is True
    assert mac.ops("-removeallpreferredwirelessnetworks")
    assert [n["ssid"] for n in record(tmp_path)["saved_networks"]] == HOME


def test_hold_first_drift_only_rejoins(mac, tmp_path, monkeypatch):
    run = worker.WifiRun(tmp_path, "Plaud1234", "10.0.0.5")
    run.begin()
    run.set_hotspot_ip("192.168.84.2")
    hold = run_hold(monkeypatch, ["10.0.0.5", "10.0.0.5"], ["192.168.84.2"], run)
    assert hold.drifts == 1 and hold.failed is None
    assert run.isolated is False  # first drift never pauses saved networks
    assert not mac.ops("-removeallpreferredwirelessnetworks")


def test_hold_gives_up_when_rejoin_fails(mac, tmp_path, monkeypatch):
    run = worker.WifiRun(tmp_path, "Plaud1234", "10.0.0.5")
    run.begin()
    run.set_hotspot_ip("192.168.84.2")
    hold = run_hold(monkeypatch, ["", ""], [None], run)
    assert hold.failed == "Could not rejoin the device hotspot"


def test_hold_keeps_rejoining_if_record_cannot_be_fsynced(mac, tmp_path, monkeypatch):
    run = worker.WifiRun(tmp_path, "Plaud1234", "10.0.0.5")
    run.begin()
    run.set_hotspot_ip("192.168.84.2")
    monkeypatch.setattr(worker, "_fsync_dir", lambda path: False)
    hs, home = "192.168.84.2", "10.0.0.5"
    hold = run_hold(monkeypatch, [hs, home, hs, home, home, hs, home, home], [hs, hs], run)
    assert hold.drifts == 2 and hold.failed is None  # rejoined both times
    assert run.isolated is False
    assert not mac.ops("-removeallpreferredwirelessnetworks")
    assert mac.preferred == HOME


def test_cleanup_waits_for_isolation_thread_after_cancelling_the_watcher(mac, tmp_path, monkeypatch):
    """transfer()'s cleanup cancels HotspotHold.watch, but the isolate() thread
    it started keeps going: cleanup must wait for it, then restore from the record."""
    run = worker.WifiRun(tmp_path, "Plaud1234", "10.0.0.5")
    run.begin()
    run.set_hotspot_ip("192.168.84.2")
    gate = Gate()
    mac.on_remove_all = gate
    monkeypatch.setattr(worker, "get_wifi_ip", lambda: "10.0.0.5")  # auto-joined away, keeps drifting

    async def fake_connect(ssid, password, original_ip, timeout=45):
        return "192.168.84.2"

    monkeypatch.setattr(worker, "connect_to_hotspot", fake_connect)
    hold = worker.HotspotHold(run, "Plaud1234", "pass", poll_s=0, max_rejoins=5)
    hold.drifts = 1  # the next drift isolates
    events = []

    async def go():
        task = asyncio.create_task(hold.watch())
        assert await asyncio.to_thread(gate.reached.wait, 5)  # record written, removal pending
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert not hold._isolation.done()  # cancelling the watcher didn't stop the thread
        asyncio.get_running_loop().call_later(0.05, gate.open)
        await hold.wait_isolation()
        events.append(f"isolation done, {len(mac.preferred)} saved")
        ok = await asyncio.to_thread(run.restore_networks)
        events.append(f"restored {ok}")
        events.append(f"finished {run.finish(ok)}")

    asyncio.run(go())
    assert events == ["isolation done, 0 saved", "restored True", "finished True"]
    assert mac.preferred == HOME
    assert record(tmp_path) is None
