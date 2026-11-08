"""WiFi sync orchestration in the app process: worker budget, timeout/kill,
the sync slot, keep-awake, and startup recovery from the restore record.

The worker subprocess, the cloud and the Mac's network tools are all faked.
"""

import io
import json
import subprocess
import threading
import time
from unittest.mock import patch

import pytest

import app.state.device as dev
import app.utils.wifi_sync_worker as worker
from tests.plaud_fakes import FakeMac


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class KeptBytesIO(io.BytesIO):
    """Keeps its contents readable after close() (what the parent wrote to stdin)."""

    def close(self):
        self.written = self.getvalue()


class FakeProc:
    """A worker process. `exits_after` = how many wait() polls before it exits
    by itself (None = never); `obeys_sigterm` = whether SIGTERM ends it."""

    def __init__(self, events, exits_after=None, obeys_sigterm=True, stdout=b"", stderr_lines=(), returncode=0):
        self.events = events
        self.pid = 4242
        self.exits_after = exits_after
        self.obeys_sigterm = obeys_sigterm
        self.returncode = None
        self._exit_code = returncode
        self.stdin = KeptBytesIO()
        self.stdout = io.BytesIO(stdout)
        self.stderr = [json.dumps(line).encode() + b"\n" for line in stderr_lines]
        self.waits = 0

    def _exit(self, code):
        if self.returncode is None:
            self.returncode = code
            self.events.append(f"worker exited {code}")

    def wait(self, timeout=None):
        self.waits += 1
        if self.returncode is not None:
            return self.returncode
        if self.exits_after is not None and self.waits > self.exits_after:
            self._exit(self._exit_code)
            return self.returncode
        time.sleep(0.001)
        raise subprocess.TimeoutExpired("worker", timeout)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.events.append("SIGTERM")
        if self.obeys_sigterm:
            self._exit(-15)

    def kill(self):
        self.events.append("SIGKILL")
        self._exit(-9)


# ── _supervise_wifi_worker / deadline ────────────────────────────────────────


def test_supervise_returns_when_worker_exits():
    events = []
    proc = FakeProc(events, exits_after=3)
    deadline = dev._WifiDeadline(600)
    assert dev._supervise_wifi_worker(proc, deadline, events.append, poll_s=0) == "exited"
    assert events == ["worker exited 0"]


def test_supervise_terminates_over_budget_worker_and_waits(monkeypatch):
    events = []
    clock = FakeClock()
    deadline = dev._WifiDeadline(10, clock=clock)
    proc = FakeProc(events)
    polls = []

    def wait(timeout=None):
        polls.append(timeout)
        clock.now += 60  # each poll passes a minute
        return FakeProc.wait(proc, timeout)

    proc.wait = wait
    assert dev._supervise_wifi_worker(proc, deadline, lambda m: None, poll_s=1) == "terminated"
    assert events == ["SIGTERM", "worker exited -15"]
    assert polls[-1] == dev.WIFI_TERM_GRACE_S  # waited for its restore after SIGTERM


def test_supervise_kills_worker_that_ignores_sigterm():
    events = []
    proc = FakeProc(events, obeys_sigterm=False)
    assert dev._supervise_wifi_worker(proc, dev._WifiDeadline.now(), lambda m: None, poll_s=0) == "killed"
    assert events == ["SIGTERM", "SIGKILL", "worker exited -9"]
    assert proc.returncode is not None


def test_worker_progress_extends_deadline():
    clock = FakeClock()
    deadline = dev._WifiDeadline(100, clock=clock)
    assert deadline.at == 1000 + 100 + dev.WIFI_BUDGET_MARGIN_S
    with patch.dict(dev._wifi_sync_status, {}, clear=False):
        dev._apply_wifi_progress(
            {"status": "3 file(s) found", "percent": 35, "budget_s": 900, "remaining_s": 700}, deadline
        )
        assert dev._wifi_sync_status["message"] == "3 file(s) found"
        assert dev._wifi_sync_status["budget_s"] == 900
        assert dev._wifi_sync_status["deadline"] == pytest.approx(time.time() + 700 + dev.WIFI_BUDGET_MARGIN_S, abs=5)
    assert deadline.at == 1000 + 700 + dev.WIFI_BUDGET_MARGIN_S
    clock.now = deadline.at + 1
    assert deadline.expired()


# ── do_wifi_sync_blocking end to end (worker faked) ─────────────────────────


class SpyLock:
    """dev._syncing stand-in that logs when the sync slot is released."""

    def __init__(self, events):
        self._lock = threading.Lock()
        self.events = events

    def acquire(self, *a, **k):
        return self._lock.acquire(*a, **k)

    def release(self):
        self.events.append("sync slot released")
        self._lock.release()

    def locked(self):
        return self._lock.locked()


@pytest.fixture
def parent(tmp_path, monkeypatch):
    events = []

    class KeepAwake:
        def __init__(self, reason):
            self.reason = reason

        def __enter__(self):
            events.append(f"awake: {self.reason}")

        def __exit__(self, *exc):
            events.append("awake end")

    monkeypatch.setattr(dev, "_syncing", SpyLock(events))
    monkeypatch.setattr(dev, "keep_awake", KeepAwake)
    monkeypatch.setattr(dev, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dev, "get_recordings_dir", lambda: tmp_path / "rec")
    monkeypatch.setattr(dev, "load_config", lambda: {})
    monkeypatch.setattr(dev, "_reset_state", lambda: None)
    monkeypatch.setattr(dev, "WIFI_BUDGET_MARGIN_S", 0)
    monkeypatch.setitem(dev._device_state, "serial_number", "8810A00012345678")
    monkeypatch.setitem(dev._device_state, "session_keys", (b"k", b"n", b"a"))
    monkeypatch.setitem(dev._device_state, "client", None)
    monkeypatch.setattr("app.state.cloud.get_device_bind_token", lambda sn: "token")
    monkeypatch.setattr("app.state.cloud.get_device_sn_signature", lambda sn: "sig")
    monkeypatch.setattr("app.state.cloud.is_logged_in", lambda: True)

    def recover(state_dir):
        events.append(f"recover {state_dir == tmp_path}")
        return {"status": "restored", "networks": 3}

    monkeypatch.setattr(worker, "recover_from_record", recover)
    return events


def spawn(monkeypatch, proc):
    spawned = []

    def popen(cmd, **kw):
        spawned.append(cmd)
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    return spawned


def test_parent_timeout_terminates_worker_recovers_then_releases(parent, monkeypatch):
    # The worker reports its budget (0 s left) and then hangs.
    proc = FakeProc(parent, stderr_lines=[{"status": "found", "percent": 35, "budget_s": 1, "remaining_s": 0}])
    spawn(monkeypatch, proc)
    result = dev.do_wifi_sync_blocking()
    assert result["timed_out"] is True
    assert result["wifi_recovery"] == {"status": "restored", "networks": 3}
    assert parent == [
        "awake: Plaud WiFi sync",
        "SIGTERM",
        "worker exited -15",
        "recover True",
        "awake end",
        "sync slot released",
    ]
    assert dev._wifi_sync_status["active"] is False


def test_parent_kills_worker_ignoring_sigterm_before_releasing(parent, monkeypatch):
    monkeypatch.setattr(dev, "WIFI_TERM_GRACE_S", 0)
    proc = FakeProc(parent, obeys_sigterm=False, stderr_lines=[{"status": "x", "percent": 9, "remaining_s": 0}])
    spawn(monkeypatch, proc)
    result = dev.do_wifi_sync_blocking()
    assert result["timed_out"] is True
    assert parent.index("SIGKILL") < parent.index("worker exited -9") < parent.index("recover True")
    assert parent[-1] == "sync slot released"


def test_parent_success_passes_state_dir_and_reports_budget(parent, monkeypatch, tmp_path):
    out = {"files": [{"session_id": 5, "status": "new", "ogg_path": "x", "duration": 1}], "method": "wifi"}
    proc = FakeProc(
        parent,
        exits_after=5,
        stdout=json.dumps(out).encode() + b"\n",
        stderr_lines=[{"status": "2 file(s) found", "percent": 35, "budget_s": 777, "remaining_s": 600}],
    )
    spawn(monkeypatch, proc)
    seen = {}
    real_apply = dev._apply_wifi_progress

    def apply(update, deadline):
        real_apply(update, deadline)
        seen.update(dev._wifi_sync_status)

    monkeypatch.setattr(dev, "_apply_wifi_progress", apply)
    monkeypatch.setattr(dev, "_finalize_redownload_backup", lambda sid: None)
    result = dev.do_wifi_sync_blocking()
    assert result == out
    args = json.loads(proc.stdin.written)
    assert args["state_dir"] == str(tmp_path)
    assert seen["active"] is True and seen["budget_s"] == 777
    assert seen["deadline"] == pytest.approx(time.time() + 600, abs=5)
    assert "recover True" not in parent
    assert parent == ["awake: Plaud WiFi sync", "worker exited 0", "awake end", "sync slot released"]
    assert dev._wifi_sync_status["message"] == "Complete: 1 file(s)"


def test_parent_refuses_while_sync_slot_taken(parent, monkeypatch):
    monkeypatch.setattr(dev._syncing, "acquire", lambda *a, **k: False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("no worker"))
    assert "BLE sync still running" in dev.do_wifi_sync_blocking()["error"]
    assert "sync slot released" not in parent


def test_parent_error_after_spawn_still_stops_worker(parent, monkeypatch):
    proc = FakeProc(parent)  # would run forever
    spawn(monkeypatch, proc)

    def broken_thread(*a, **k):
        raise RuntimeError("can't start thread")

    monkeypatch.setattr(dev.threading, "Thread", broken_thread)
    result = dev.do_wifi_sync_blocking()
    assert result == {"error": "can't start thread"}
    assert parent == [
        "awake: Plaud WiFi sync",
        "SIGTERM",
        "worker exited -15",
        "recover True",
        "awake end",
        "sync slot released",
    ]


# ── Startup recovery (_recover_wifi) ─────────────────────────────────────────


@pytest.fixture
def startup_mac(tmp_path, monkeypatch):
    fake = FakeMac(preferred=["Plaud5678"], ip="192.168.84.2", current_ssid="Plaud5678")
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(worker, "_wifi_iface_cache", "en1")
    monkeypatch.setattr(worker, "_restore_via_corewlan", lambda n: False)
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    monkeypatch.setattr("app.utils.constants.DATA_DIR", tmp_path)
    return fake


SAVED = [
    {"ssid": "Home 5G", "cw_security": 4, "security": "WPA2"},
    {"ssid": "Office", "cw_security": 0, "security": "OPEN"},
]


def test_startup_recovery_uses_the_record(startup_mac, tmp_path):
    from app import api_server

    worker.write_restore_record(
        tmp_path,
        {
            "hotspot_ssid": "Plaud5678",
            "hotspot_ip": "192.168.84.2",
            "isolated": True,
            "saved_networks": SAVED,
            "plaud_was_preferred": False,
        },
    )
    api_server._recover_wifi()
    assert startup_mac.preferred == ["Home 5G", "Office"]  # original order, hotspot forgotten
    assert [c[3] for c in startup_mac.ops("-setairportpower")] == ["off", "on"]
    assert worker.read_restore_record(tmp_path) is None


def test_startup_recovery_keeps_record_if_restore_fails(startup_mac, tmp_path):
    from app import api_server

    worker.write_restore_record(tmp_path, {"hotspot_ssid": "Plaud5678", "isolated": True, "saved_networks": SAVED})
    startup_mac.fail_add = {"Office"}
    api_server._recover_wifi()
    assert worker.read_restore_record(tmp_path)["saved_networks"] == SAVED


def test_startup_recovery_leaves_a_running_worker_alone(startup_mac, tmp_path):
    from app import api_server

    worker.write_restore_record(tmp_path, {"hotspot_ssid": "Plaud5678", "isolated": True, "saved_networks": SAVED})
    lock = worker.WorkerLock(tmp_path)
    assert lock.acquire()
    try:
        api_server._recover_wifi()
    finally:
        lock.release()
    assert startup_mac.calls == []  # no restore, no power-cycle, no legacy check
    assert worker.read_restore_record(tmp_path) is not None


def test_startup_without_record_falls_back_to_hotspot_check(startup_mac):
    from app import api_server

    api_server._recover_wifi()
    assert startup_mac.calls == [
        ["networksetup", "-getairportnetwork", "en1"],
        ["networksetup", "-setairportpower", "en1", "off"],
        ["networksetup", "-setairportpower", "en1", "on"],
    ]


def test_startup_recovery_gives_up_after_repeated_failures(startup_mac, tmp_path):
    from app import api_server

    worker.write_restore_record(tmp_path, {"hotspot_ssid": "Plaud5678", "isolated": True, "saved_networks": SAVED})
    startup_mac.fail_add = {"Office"}
    for _ in range(worker.MAX_RESTORE_ATTEMPTS - 1):
        api_server._recover_wifi()
        assert worker.read_restore_record(tmp_path) is not None
    api_server._recover_wifi()
    assert worker.read_restore_record(tmp_path) is None  # no more retries at every launch
    assert worker.read_stuck_record(tmp_path)["saved_networks"] == SAVED
    calls = len(startup_mac.calls)
    api_server._recover_wifi()  # next launch: only the cheap hotspot-by-name check
    assert not [c for c in startup_mac.calls[calls:] if c[1] == "-addpreferredwirelessnetworkatindex"]


def test_startup_recovery_runs_in_background_after_bind():
    """_recover_wifi must not run before the server listens (a slow restore
    blew the shell's startup health budget)."""
    import inspect

    from app import api_server

    src = inspect.getsource(api_server)
    main = src[src.index('if __name__ == "__main__":') :]
    assert "_recover_wifi()" not in main
    assert main.index("make_server(") < main.index("_start_wifi_recovery()") < main.index("serve_forever()")


def test_start_wifi_recovery_returns_immediately(monkeypatch):
    from app import api_server

    release, ran = threading.Event(), threading.Event()

    def slow_recover():
        ran.set()
        release.wait(5)

    monkeypatch.setattr(api_server, "_recover_wifi", slow_recover)
    t0 = time.monotonic()
    thread = api_server._start_wifi_recovery()
    assert time.monotonic() - t0 < 1
    assert ran.wait(5)
    assert not dev._wifi_recovery_idle.is_set()
    release.set()
    thread.join(5)
    assert dev._wifi_recovery_idle.is_set()


def test_wifi_sync_waits_for_startup_recovery(parent, monkeypatch):
    release = threading.Event()

    def slow_recover():
        release.wait(5)
        parent.append("startup recovery done")

    thread = dev.run_wifi_recovery_in_background(slow_recover)
    threading.Timer(0.1, release.set).start()
    out = {"files": [], "method": "wifi"}

    def popen(*a, **k):
        parent.append("worker spawned")
        return FakeProc(parent, exits_after=0, stdout=json.dumps(out).encode())

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert dev.do_wifi_sync_blocking() == out
    thread.join(5)
    assert parent.index("startup recovery done") < parent.index("worker spawned")


def test_wifi_sync_gives_up_waiting_for_a_hung_recovery(parent, monkeypatch):
    release = threading.Event()
    thread = dev.run_wifi_recovery_in_background(lambda: release.wait(5))
    monkeypatch.setattr(dev, "WIFI_RECOVERY_WAIT_S", 0.05)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("no worker while recovery runs"))
    try:
        assert "Still restoring WiFi" in dev.do_wifi_sync_blocking()["error"]
    finally:
        release.set()
        thread.join(5)
    assert parent[-1] == "sync slot released"


def test_status_reports_restore_notice_until_acknowledged(parent, tmp_path):
    assert dev.get_wifi_sync_status()["restore_notice"] is None
    worker.note_failed_restore(
        tmp_path,
        {"hotspot_ssid": "Plaud5678", "isolated": True, "saved_networks": SAVED},
        {"missing": ["Office"], "errors": {"Office": "Network not found"}},
    )
    notice = dev.get_wifi_sync_status()["restore_notice"]
    assert notice["message"] == "Some saved WiFi networks couldn't be restored: Office"
    assert notice["networks"] == ["Office"]
    assert dev.acknowledge_wifi_restore_notice() is True
    assert dev.get_wifi_sync_status()["restore_notice"] is None


def test_restore_notice_ack_route(flask_client, monkeypatch):
    """The UI acknowledges the one-time 'networks couldn't be restored' notice."""
    import app.state.device as dev

    calls = []
    monkeypatch.setattr(dev, "acknowledge_wifi_restore_notice", lambda: calls.append(1) or True)
    r = flask_client.post("/device/wifi-restore-notice/ack")
    assert r.status_code == 200
    assert r.get_json() == {"acknowledged": True}
    assert calls == [1]


# ── wifi_sync.log: rotation and redaction ───────────────────────────────────


def test_wifi_log_rotates_keeping_one_old_file(tmp_path):
    log = tmp_path / "wifi_sync.log"
    for i in range(30):
        dev.append_rotating_log(log, f"line {i:02d} " + "x" * 40 + "\n", max_bytes=200)
    old = tmp_path / "wifi_sync.log.1"
    assert log.stat().st_size < 200 + 60
    assert old.exists() and old.stat().st_size < 200 + 60
    assert sorted(p.name for p in tmp_path.iterdir()) == ["wifi_sync.log", "wifi_sync.log.1"]
    assert "line 29" in log.read_text()


def test_wifi_log_default_limit_is_about_a_megabyte():
    assert 500_000 <= dev.WIFI_LOG_MAX_BYTES <= 2_000_000


def test_wifi_log_write_failure_is_ignored(tmp_path):
    dev.append_rotating_log(tmp_path / "missing-dir" / "wifi_sync.log", "x\n")  # no raise


@pytest.mark.parametrize(
    "line",
    [
        "bind_device result: {'token': 'abcdefgh12345678', 'sn': '8810A00012345678'}",
        '{"access_token": "abcdefgh12345678", "status": 0}',
        "Authorization: bearer eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl",
        "fetch_sn_signature result: {'signature': 'QUJDREVGR0g=', 'sn': 'x'}",
        "token=abcdefgh12345678 next",
    ],
)
def test_redact_secrets_patterns(line):
    out = dev.redact_secrets(line)
    for secret in ("abcdefgh12345678", "eyJhbGciOi", "QUJDREVGR0g="):
        assert secret not in out
    assert "[redacted]" in out


def test_redact_secrets_known_values_and_harmless_text():
    sig = "U0lHTkFUVVJFX0JBU0U2NA" * 10
    out = dev.redact_secrets(f"worker traceback ... {sig} ... TOKEN1234567890XY", [sig, "TOKEN1234567890XY"])
    assert sig not in out and "TOKEN1234567890XY" not in out
    # Ordinary status lines are left alone (and short values are never treated as secrets).
    msg = "Credentials: bind token present, SN signature present"
    assert dev.redact_secrets(msg, ["sig"]) == msg
    assert dev.redact_secrets("Sending SN_SIG (256 bytes, 3 chunks)...") == "Sending SN_SIG (256 bytes, 3 chunks)..."


OLD_TOKEN = "OLDBINDTOKEN0123456789abcdefghij"
OLD_SIG = "T0xEU0lHTkFUVVJF" * 20


def _old_logs(tmp_path):
    """Logs as pre-v2.4.1 versions wrote them: raw cloud replies with credentials."""
    (tmp_path / "wifi_sync.log").write_text(
        "2026-10-01T10:00:00 Starting WiFi sync for SN=8810A00012345678\n"
        f"2026-10-01T10:00:01 bind_device result: {{'token': '{OLD_TOKEN}', 'sn': '8810A00012345678'}}\n"
        f"2026-10-01T10:00:02 Worker stderr: traceback ... {OLD_SIG}\n"
        "2026-10-01T10:00:09 Worker exited with code 0\n"
    )
    (tmp_path / "wifi_sync.log.1").write_text(
        f"2026-09-01T10:00:01 fetch_sn_signature result: {{'signature': '{OLD_SIG}', 'sn': 'x'}}\n"
    )


def test_old_wifi_logs_scrubbed_once(tmp_path, monkeypatch):
    _old_logs(tmp_path)
    monkeypatch.setattr(dev, "load_config", lambda: {"device_sn_signatures": {"8810A00012345678": OLD_SIG}})
    assert dev.scrub_wifi_logs(tmp_path) is True
    for name in ("wifi_sync.log", "wifi_sync.log.1"):
        text = (tmp_path / name).read_text()
        assert OLD_TOKEN not in text and OLD_SIG not in text
        assert "[redacted]" in text
    text = (tmp_path / "wifi_sync.log").read_text()
    assert "Starting WiFi sync for SN=8810A00012345678" in text and "Worker exited with code 0" in text
    assert (tmp_path / "wifi_sync.log").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / dev.WIFI_LOG_SCRUB_MARKER).exists()
    assert not list(tmp_path.glob("*.tmp"))
    # Marked done: a later run leaves the files alone.
    (tmp_path / "wifi_sync.log").write_text(f"token={OLD_TOKEN}\n")
    assert dev.scrub_wifi_logs(tmp_path) is False
    assert OLD_TOKEN in (tmp_path / "wifi_sync.log").read_text()


def test_scrub_without_logs_just_marks_done(tmp_path):
    assert dev.scrub_wifi_logs(tmp_path) is True
    assert (tmp_path / dev.WIFI_LOG_SCRUB_MARKER).exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [dev.WIFI_LOG_SCRUB_MARKER]


def test_failed_scrub_is_retried(tmp_path, monkeypatch):
    _old_logs(tmp_path)
    real_replace = dev.os.replace

    def broken_replace(a, b):
        raise OSError("disk full")

    monkeypatch.setattr(dev.os, "replace", broken_replace)
    assert dev.scrub_wifi_logs(tmp_path) is False
    assert not (tmp_path / dev.WIFI_LOG_SCRUB_MARKER).exists()
    monkeypatch.setattr(dev.os, "replace", real_replace)
    assert dev.scrub_wifi_logs(tmp_path) is True
    assert OLD_TOKEN not in (tmp_path / "wifi_sync.log").read_text()


def test_startup_recovery_scrubs_old_logs(tmp_path, monkeypatch):
    _old_logs(tmp_path)
    monkeypatch.setattr(dev, "DATA_DIR", tmp_path)
    ran = []
    dev.run_wifi_recovery_in_background(lambda: ran.append(1))
    assert dev._wifi_recovery_idle.wait(5)
    assert ran == [1]
    assert OLD_TOKEN not in (tmp_path / "wifi_sync.log").read_text()
    assert (tmp_path / dev.WIFI_LOG_SCRUB_MARKER).exists()


def test_wifi_sync_scrubs_old_logs_first(parent, monkeypatch, tmp_path):
    _old_logs(tmp_path)
    # The stray signature in a traceback is only found by exact value, from the config.
    monkeypatch.setattr(dev, "load_config", lambda: {"device_sn_signatures": {"8810A00012345678": OLD_SIG}})
    proc = FakeProc(parent, exits_after=1, stdout=json.dumps({"files": []}).encode() + b"\n")
    spawn(monkeypatch, proc)
    dev.do_wifi_sync_blocking()
    text = (tmp_path / "wifi_sync.log").read_text()
    assert OLD_TOKEN not in text and OLD_SIG not in text
    assert "Starting WiFi sync" in text


def test_wifi_sync_log_never_contains_credentials(parent, monkeypatch, tmp_path):
    """Credentials fetched from the cloud mid-sync, and a worker line echoing
    the token, never reach wifi_sync.log."""
    token, sig = "BINDTOKEN0123456789abcdefghijklm", "U0lHTkFUVVJF" * 30
    have = {"token": None, "sig": None}

    def bind(sn):
        have["token"] = token
        return {"token": token, "sn": sn}

    def fetch(sn):
        have["sig"] = sig
        return {"signature": sig, "sn": sn}

    monkeypatch.setattr("app.state.cloud.get_device_bind_token", lambda sn: have["token"])
    monkeypatch.setattr("app.state.cloud.get_device_sn_signature", lambda sn: have["sig"])
    monkeypatch.setattr("app.state.cloud.bind_device", bind)
    monkeypatch.setattr("app.state.cloud.fetch_sn_signature", fetch)
    out = {"files": [], "method": "wifi"}
    proc = FakeProc(
        parent,
        exits_after=5,
        stdout=json.dumps(out).encode() + b"\n",
        stderr_lines=[{"status": f"debug {token} {sig}", "percent": 3}],
    )
    spawn(monkeypatch, proc)
    assert dev.do_wifi_sync_blocking() == out
    assert json.loads(proc.stdin.written)["bind_token"] == token  # the worker still gets it
    text = (tmp_path / "wifi_sync.log").read_text()
    assert "Starting WiFi sync" in text and "token received" in text and "signature received" in text
    assert token not in text and sig not in text
