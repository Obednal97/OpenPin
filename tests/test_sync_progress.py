"""Sync progress counts only the recordings being downloaded and moves with
their bytes, over Bluetooth and Wi-Fi (the device may hold many recordings
this Mac already has)."""

import asyncio

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

import app.state.device as dev
import app.utils.wifi_sync_worker as worker
from app.utils import sync_progress
from tests.plaud_fakes import make_plaud_file
from tests.test_ble_sync_integrity import FakeDevice, app_key, env, run_sync  # noqa: F401 — fixtures
from tests.test_wifi_worker_protocol import FakeWs, _fast_sleep, wifi_chunk, wifi_list

# -- wording -------------------------------------------------------------------------


def test_format_mb():
    assert sync_progress.format_mb(0) == "0.0 MB"
    assert sync_progress.format_mb(1) == "0.1 MB"
    assert sync_progress.format_mb(5_430_000) == "5.4 MB"
    assert sync_progress.format_mb(None) == "0.0 MB"


def test_plan_message():
    assert sync_progress.plan_message(0, 0) == "No new recordings to sync"
    assert sync_progress.plan_message(1, 1_200_000) == "1 new recording to sync (1.2 MB)"
    assert sync_progress.plan_message(2, 5_400_000) == "2 new recordings to sync (5.4 MB)"


def test_progress_follows_bytes_across_recordings():
    msg, frac = sync_progress.progress(1, 2, 0, 4_000_000)
    assert msg == "Syncing recording 1 of 2 · 0.0 MB of 4.0 MB"
    assert frac == 0
    msg, frac = sync_progress.progress(2, 2, 3_000_000, 4_000_000)
    assert msg == "Syncing recording 2 of 2 · 3.0 MB of 4.0 MB"
    assert frac == 0.75
    # Clamped: more bytes than planned (size mismatch) never passes 100 %.
    assert sync_progress.progress(2, 2, 9_000_000, 4_000_000)[1] == 1.0


def test_progress_without_sizes_falls_back_to_counting():
    msg, frac = sync_progress.progress(2, 4, 0, 0)
    assert msg == "Syncing recording 2 of 4"
    assert frac == 0.25


# -- Wi-Fi -------------------------------------------------------------------------


def test_wifi_progress_counts_only_missing_recordings(tmp_path, monkeypatch, app_key):  # noqa: F811
    monkeypatch.setattr(worker, "get_wifi_ip", lambda: "192.168.84.2")
    monkeypatch.setattr(worker.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(worker, "PROGRESS_INTERVAL_S", 0)
    lines = []
    monkeypatch.setattr(worker, "status", lambda msg, pct=None, **kw: lines.append((msg, pct)))

    have, a, b = (make_plaud_file(app_key, frames=n) for n in (150, 200, 220))
    (tmp_path / "1.ogg").write_bytes(b"OggS")  # already on this Mac
    key, nonce, aad = b"k" * 32, b"n" * 12, b"a" * 12
    cipher = ChaCha20Poly1305(key)
    s = worker.WifiSession(cipher, nonce, aad, "tok", tmp_path, set(), app_key, original_ip="10.0.0.5")
    half = len(a) // 2
    script = [
        lambda ws: worker.build_wifi_packet(2, {}),
        lambda ws: worker.build_wifi_packet(1, {}),
        lambda ws: wifi_list((1, len(have)), (2, len(a)), (3, len(b))),
        lambda ws: wifi_chunk(a[:half], last=False),
        lambda ws: wifi_chunk(a[half:], last=True),
        lambda ws: wifi_chunk(b, last=True),
    ]
    asyncio.run(s.handler(FakeWs(cipher, nonce, aad, script)))

    total = len(a) + len(b)
    msgs = [m for m, _ in lines]
    assert msgs[0] == sync_progress.plan_message(2, total)
    assert any(m.startswith("Syncing recording 1 of 2") for m in msgs)
    assert any(m.startswith("Syncing recording 2 of 2") for m in msgs)
    assert not any("of 3" in m for m in msgs), "the recording already on this Mac must not be counted"
    assert msgs[-1] == "Synced 2 of 2 recordings"
    pcts = [p for _, p in lines]
    assert pcts == sorted(pcts), f"progress went backwards: {pcts}"
    # Half of the first recording moved the bar part of the way, not to a fixed step.
    mid = [p for m, p in lines if m.startswith("Syncing recording 1 of 2") and p > 35]
    assert mid and max(mid) < 35 + 55 * len(a) / total + 1
    assert pcts[-1] == 90


def test_wifi_with_nothing_new_says_so(tmp_path, monkeypatch, app_key):  # noqa: F811
    monkeypatch.setattr(worker, "get_wifi_ip", lambda: "192.168.84.2")
    monkeypatch.setattr(worker.asyncio, "sleep", _fast_sleep)
    lines = []
    monkeypatch.setattr(worker, "status", lambda msg, pct=None, **kw: lines.append((msg, pct)))
    (tmp_path / "1.ogg").write_bytes(b"OggS")
    key, nonce, aad = b"k" * 32, b"n" * 12, b"a" * 12
    cipher = ChaCha20Poly1305(key)
    s = worker.WifiSession(cipher, nonce, aad, "tok", tmp_path, set(), app_key, original_ip="10.0.0.5")
    script = [
        lambda ws: worker.build_wifi_packet(2, {}),
        lambda ws: worker.build_wifi_packet(1, {}),
        lambda ws: wifi_list((1, 1000)),
    ]
    asyncio.run(s.handler(FakeWs(cipher, nonce, aad, script)))
    assert lines == [("No new recordings to sync", 90)]
    assert s.all_done.is_set()


def test_status_remembers_the_last_line(monkeypatch, capsys):
    monkeypatch.setattr(worker, "_last_msg", "")
    worker.status("Syncing recording 1 of 2 · 0.5 MB of 4.0 MB", 40)
    worker.status(worker._last_msg, None, budget_s=10)
    err = capsys.readouterr().err.strip().splitlines()
    assert '"status": "Syncing recording 1 of 2 \\u00b7 0.5 MB of 4.0 MB", "percent": 40, "budget_s": 10' in err[-1]


# -- Bluetooth ---------------------------------------------------------------------


class _Recorder(dict):
    """The BLE status dict, keeping a copy of every update."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.history = []

    def update(self, *a, **k):
        super().update(*a, **k)
        self.history.append(dict(self))


def test_ble_progress_counts_only_missing_recordings(env, app_key, monkeypatch):  # noqa: F811
    rec = _Recorder(dev._ble_sync_status)
    monkeypatch.setattr(dev, "_ble_sync_status", rec)
    (env["rec"] / "900.ogg").write_bytes(b"x")
    (env["rec"] / "903.ogg").write_bytes(b"x")
    a, b = make_plaud_file(app_key), make_plaud_file(app_key)
    device = FakeDevice({901: a, 902: b})
    # Recordings already on the Mac come first: the old count said "3 of 4" (50 %) here.
    result = run_sync(device, [(900, 5000), (903, 5000), (901, len(a)), (902, len(b))])
    assert {f["session_id"]: f["status"] for f in result["files"]} == {
        900: "exists",
        903: "exists",
        901: "new",
        902: "new",
    }

    during = [h for h in rec.history if h["active"]]
    msgs = [h["message"] for h in during]
    assert msgs[0] == "Checking the device for new recordings…"
    assert msgs[1] == sync_progress.plan_message(2, len(a) + len(b))
    syncing = [h for h in during if h["message"].startswith("Syncing recording")]
    assert [h["file_index"] for h in syncing][0] == 1 and syncing[-1]["file_index"] == 2
    assert all(h["file_count"] == 2 and h["bytes_total"] == len(a) + len(b) for h in syncing)
    assert syncing[0]["percent"] == 0
    # Starting the second recording: the first one's bytes are done.
    second = next(h for h in syncing if h["file_index"] == 2)
    assert second["bytes_done"] == len(a)
    assert second["percent"] == round(len(a) / (len(a) + len(b)) * 100)
    pcts = [h["percent"] for h in during]
    assert pcts == sorted(pcts)
    # Reset afterwards.
    assert rec["active"] is False and rec["file_count"] == 0 and rec["message"] == ""


def test_ble_nothing_new(env, app_key, monkeypatch):  # noqa: F811
    rec = _Recorder(dev._ble_sync_status)
    monkeypatch.setattr(dev, "_ble_sync_status", rec)
    (env["rec"] / "900.ogg").write_bytes(b"x")
    device = FakeDevice({})
    run_sync(device, [(900, 5000)])
    assert any(h["message"] == "No new recordings to sync" and h["percent"] == 100 for h in rec.history)
    assert 28 not in device.sent
