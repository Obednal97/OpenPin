"""BLE download integrity: simulated device, no hardware.

A fake BleakClient decrypts the commands the sync writes and feeds encrypted
notifications back through the REAL notify handler (_make_notify_handler), so
the end-marker / tail / size / delete-reply logic is exercised end to end.
"""

import asyncio
import os
import struct
import threading
import time
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

import app.state.device as dev
from app.utils.audio import BleDecryptError, decrypt_ble_file
from tests.plaud_fakes import TOC, make_plaud_file

CHUNK = 240


@pytest.fixture(scope="module")
def app_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


# ---------------------------------------------------------------------------
# Fake BLE device
# ---------------------------------------------------------------------------


class FakeDevice:
    """Plays the NotePin's side of the encrypted command channel.

    behaviours[sid] controls how cmd 28 (download) is answered:
      complete      all bytes, end packet (err 0), cmd-29 tail with CRC
      end_only      all bytes + end packet, no tail
      truncate_end  40 % of the bytes, then an end packet (device gave up early)
      error_end     all bytes, end packet with error code 3
      stall         40 % of the bytes, then silence (link still up)
      drop          40 % of the bytes, then the link drops
      slow          40 % of the bytes, then silence (used for mid-file abort)
    delete_mode: ok | none (no reply) | fail (status 1) | wrong_sid
    """

    def __init__(self, files, behaviours=None, delete_mode="ok"):
        self.files = files  # sid -> bytes
        self.behaviours = behaviours or {}
        self.delete_mode = delete_mode
        self.key, self.nonce, self.aad = os.urandom(32), os.urandom(12), os.urandom(12)
        self.cipher = ChaCha20Poly1305(self.key)
        self.sent = []  # cmd ids written by the app
        self.handler = None
        self.client = FakeClient(self)
        self._seq = 0
        self._tasks = []

    def notify(self, payload):
        self._seq += 1
        self.handler(None, self.cipher.encrypt(self.nonce, struct.pack("<I", self._seq) + payload, self.aad))

    def reply(self, cid, body=b""):
        self.notify(bytes([1]) + struct.pack("<H", cid) + body)

    async def stream(self, sid, mode):
        data = self.files[sid]
        cut = len(data) if mode in ("complete", "end_only", "error_end") else int(len(data) * 0.4)
        self.reply(28, struct.pack("<I", sid) + bytes([0]))
        for off in range(0, cut, CHUNK):
            chunk = data[off : min(off + CHUNK, cut)]
            self.notify(bytes([2]) + struct.pack("<II", sid, off) + bytes([len(chunk)]) + chunk)
            await asyncio.sleep(0)
        if mode in ("stall", "slow"):
            return
        if mode == "drop":
            await asyncio.sleep(0.01)
            dev._on_disconnect(self.client)  # what bleak's disconnected_callback does
            return
        err = 3 if mode == "error_end" else 0
        self.notify(bytes([2]) + struct.pack("<II", sid, 0xFFFFFFFF) + bytes([err]))
        if mode in ("complete", "error_end"):
            crc = dev.crc16_variants(data)["XMODEM"]
            self.reply(29, struct.pack("<IH", sid, crc))

    def on_write(self, ct):
        pt = self.cipher.decrypt(self.nonce, bytes(ct), self.aad)[4:]
        cid = struct.unpack_from("<H", pt, 1)[0]
        self.sent.append(cid)
        loop = asyncio.get_running_loop()
        if cid == 28:
            sid = struct.unpack_from("<I", pt, 3)[0]
            self._tasks.append(loop.create_task(self.stream(sid, self.behaviours.get(sid, "complete"))))
        elif cid == 30:
            loop.call_soon(self.reply, 30)
        elif cid == 31:
            sid = struct.unpack_from("<I", pt, 3)[0]
            if self.delete_mode == "ok":
                loop.call_soon(self.reply, 31, struct.pack("<I", sid) + bytes([0]))
            elif self.delete_mode == "fail":
                loop.call_soon(self.reply, 31, struct.pack("<I", sid) + bytes([1]))
            elif self.delete_mode == "wrong_sid":
                loop.call_soon(self.reply, 31, struct.pack("<I", sid + 1) + bytes([0]))
            # "none": never answers

    def install(self):
        """Put the device in the state connect_device() leaves after a handshake."""
        cmd_evt, cmd_resp = asyncio.Event(), [None]
        file_data, file_done, file_meta = bytearray(), asyncio.Event(), {}
        self.handler = dev._make_notify_handler(
            self.cipher, self.nonce, self.aad, cmd_evt, cmd_resp, file_data, file_done, file_meta
        )
        dev._device_state.update(
            connected=True,
            client=self.client,
            cipher=self.cipher,
            session_keys=(self.key, self.nonce, self.aad),
            seq=0,
            expected_cmd_id=None,
            cmd_evt=cmd_evt,
            cmd_resp=cmd_resp,
            file_data=file_data,
            file_done=file_done,
            file_meta=file_meta,
        )


class FakeClient:
    def __init__(self, device):
        self.device = device
        self.disconnected = False

    async def write_gatt_char(self, uuid, ct, response=False):
        self.device.on_write(ct)

    async def stop_notify(self, uuid):
        pass

    async def disconnect(self):
        self.disconnected = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch, app_key):
    """Isolated recordings dir + config + app key; fast receive-loop timings."""
    rec = tmp_path / "recordings"
    rec.mkdir()
    keyfile = tmp_path / "app_private.pem"
    keyfile.write_bytes(
        app_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    config = {"auto_delete_after_sync": True}
    monkeypatch.setattr(dev, "APP_KEY_PATH", keyfile)
    monkeypatch.setattr(dev, "get_recordings_dir", lambda: rec)
    monkeypatch.setattr(dev, "get_all_recordings_dirs", lambda: [rec])
    monkeypatch.setattr(dev, "load_config", lambda: dict(config))
    monkeypatch.setattr(dev, "_RECV_POLL_S", 0.02)
    monkeypatch.setattr(dev, "_STALL_TIMEOUT_S", 0.15)
    monkeypatch.setattr(dev, "_TAIL_WAIT_S", 0.2)
    monkeypatch.setattr(dev, "_DELETE_REPLY_TIMEOUT_S", 0.2)
    uploads = []
    import app.state.gdrive as gdrive

    monkeypatch.setattr(gdrive, "maybe_auto_upload", lambda sid, path: uploads.append((sid, path)))
    monkeypatch.setattr(gdrive, "get_synced_files", lambda: {})
    monkeypatch.setattr(dev, "_sync_abort", False)
    yield {"rec": rec, "config": config, "uploads": uploads}
    dev._reset_state()
    if dev._syncing.locked():
        dev._syncing.release()


def run_sync(device, file_list):
    async def go():
        device.install()

        async def fake_list():
            return [{"session_id": sid, "size": size, "duration_est": 0} for sid, size in file_list]

        dev.list_device_files = fake_list
        return await dev.sync_files()

    original = dev.list_device_files
    try:
        return asyncio.run(go())
    finally:
        dev.list_device_files = original


def entry(result, sid):
    return next(f for f in result["files"] if f["session_id"] == sid)


# ---------------------------------------------------------------------------
# Sync integrity
# ---------------------------------------------------------------------------


def test_complete_file_accepted_and_auto_deleted(env, app_key):
    data = make_plaud_file(app_key)
    device = FakeDevice({101: data})
    result = run_sync(device, [(101, len(data))])

    e = entry(result, 101)
    assert e["status"] == "new"
    assert (env["rec"] / "101.ogg").exists()
    assert not (env["rec"] / "101_partial.bin").exists()
    assert not (env["rec"] / "101_encrypted.bin").exists()  # small file: backup dropped after success
    assert 31 in device.sent
    assert result["deleted_from_device"] == [101]
    # gdrive gets a str id (its synced-files map is keyed by str)
    assert env["uploads"] == [("101", str(env["rec"] / "101.ogg"))]


def test_complete_with_end_packet_but_no_tail_is_accepted(env, app_key):
    data = make_plaud_file(app_key)
    device = FakeDevice({102: data}, {102: "end_only"})
    result = run_sync(device, [(102, len(data))])
    assert entry(result, 102)["status"] == "new"


@pytest.mark.parametrize("mode", ["drop", "stall", "truncate_end", "error_end"])
def test_incomplete_download_kept_as_partial_never_deleted(env, app_key, mode):
    data = make_plaud_file(app_key)
    device = FakeDevice({201: data}, {201: mode})
    result = run_sync(device, [(201, len(data))])

    e = entry(result, 201)
    assert e["status"] == "partial"
    assert e["expected"] == len(data)
    assert e["ogg_path"] is None
    if mode != "error_end":
        assert e["received"] == int(len(data) * 0.4)
    assert not (env["rec"] / "201.ogg").exists()
    partial = env["rec"] / "201_partial.bin"
    assert partial.exists() and partial.read_bytes() == data[: e["received"]]
    assert 31 not in device.sent, "a partial must never trigger a device delete"
    assert result["deleted_from_device"] == []
    if mode == "stall":
        assert 30 in device.sent, "stalled transfer should send stop-sync"


def test_partial_is_retried_on_next_sync(env, app_key):
    data = make_plaud_file(app_key)
    first = run_sync(FakeDevice({301: data}, {301: "drop"}), [(301, len(data))])
    assert entry(first, 301)["status"] == "partial"

    retry_device = FakeDevice({301: data})
    second = run_sync(retry_device, [(301, len(data))])
    assert entry(second, 301)["status"] == "new"
    assert 28 in retry_device.sent, "partial must not be treated as already present"
    assert (env["rec"] / "301.ogg").exists()
    assert not (env["rec"] / "301_partial.bin").exists()


def test_disconnect_stops_remaining_files(env, app_key):
    a, b = make_plaud_file(app_key), make_plaud_file(app_key)
    device = FakeDevice({401: a, 402: b}, {401: "drop"})
    result = run_sync(device, [(401, len(a)), (402, len(b))])
    assert entry(result, 401)["status"] == "partial"
    assert all(f["session_id"] != 402 for f in result["files"])


def test_decrypt_failure_keeps_encrypted_no_ogg_no_delete(env, app_key):
    data = make_plaud_file(app_key, decryptable=False)
    device = FakeDevice({501: data})
    result = run_sync(device, [(501, len(data))])

    e = entry(result, 501)
    assert e["status"] == "decrypt_failed"
    assert not (env["rec"] / "501.ogg").exists()
    assert (env["rec"] / "501_encrypted.bin").read_bytes() == data
    assert 31 not in device.sent


def test_auto_delete_without_reply_is_not_reported_deleted(env, app_key):
    data = make_plaud_file(app_key)
    device = FakeDevice({601: data}, delete_mode="none")
    result = run_sync(device, [(601, len(data))])
    assert entry(result, 601)["status"] == "new"
    assert result["deleted_from_device"] == []
    assert result["delete_failed"] == [601]


@pytest.mark.parametrize(
    ("mode", "expected"), [("ok", True), ("none", False), ("fail", False), ("wrong_sid", False)]
)
def test_delete_device_file_requires_ok_reply(env, mode, expected):
    device = FakeDevice({}, delete_mode=mode)

    async def go():
        device.install()
        return await dev.delete_device_file(77)

    assert asyncio.run(go())["deleted"] is expected


def test_delete_after_disconnect_is_not_deleted(env):
    async def go():
        dev._reset_state()
        return await dev.delete_device_file(78)

    assert asyncio.run(go())["deleted"] is False


def test_stale_abort_flag_does_not_cancel_next_sync(env, app_key):
    # /abort-sync with nothing running: no-op now…
    assert dev.abort_sync() is False
    assert dev._sync_abort is False
    # …and even a leftover flag is cleared when the next sync starts.
    dev._sync_abort = True
    data = make_plaud_file(app_key)
    result = run_sync(FakeDevice({701: data}), [(701, len(data))])
    assert entry(result, 701)["status"] == "new"
    assert "aborted" not in result


def test_abort_mid_file_leaves_partial_and_sends_stop(env, app_key):
    data = make_plaud_file(app_key)
    device = FakeDevice({801: data}, {801: "slow"})

    async def go():
        device.install()

        async def fake_list():
            return [{"session_id": 801, "size": len(data), "duration_est": 0}]

        dev.list_device_files = fake_list

        async def abort_soon():
            await asyncio.sleep(0.05)
            assert dev.abort_sync() is True

        task = asyncio.ensure_future(abort_soon())
        res = await dev.sync_files()
        await task
        return res

    original = dev.list_device_files
    try:
        result = asyncio.run(go())
    finally:
        dev.list_device_files = original
    assert result["aborted"] is True
    e = entry(result, 801)
    assert e["status"] == "partial" and e["reason"] == "aborted"
    assert 30 in device.sent
    assert 31 not in device.sent
    assert not (env["rec"] / "801.ogg").exists()


def test_existing_ogg_and_cloud_synced_files_are_skipped(env, app_key, monkeypatch):
    (env["rec"] / "901.ogg").write_bytes(b"x")
    env["config"]["storage_mode"] = "cloud"
    import app.state.gdrive as gdrive

    monkeypatch.setattr(gdrive, "get_synced_files", lambda: {"902": {"drive_file_id": "d"}})
    device = FakeDevice({})
    result = run_sync(device, [(901, 1000), (902, 1000)])
    assert entry(result, 901)["status"] == "exists"
    assert entry(result, 902)["status"] == "exists"
    assert entry(result, 902)["location"] == "gdrive"
    assert 28 not in device.sent


def test_evaluate_download_rules():
    meta_ok = {"end": True, "end_error": 0}
    assert dev.evaluate_download(1, 3, b"abc", meta_ok) == (True, None)
    assert dev.evaluate_download(1, 4, b"abc", meta_ok)[0] is False
    assert dev.evaluate_download(1, 2, b"abc", meta_ok)[0] is False
    assert dev.evaluate_download(1, 3, b"abc", {})[0] is False
    assert dev.evaluate_download(1, 3, b"abc", {"end": True, "end_error": 5})[0] is False
    assert dev.evaluate_download(1, 3, b"abc", {"tail": True, "start_status": 2})[0] is False
    assert dev.evaluate_download(1, 3, b"abc", meta_ok, interrupted="stalled") == (False, "stalled")


def test_crc_logged_not_enforced_by_default(monkeypatch):
    data = b"123456789"
    v = dev.crc16_variants(data)
    # Standard check values for the catalogued CRC-16 variants
    assert v == {"XMODEM": 0x31C3, "CCITT-FALSE": 0x29B1, "ARC": 0xBB3D, "MODBUS": 0x4B37, "KERMIT": 0x2189}
    meta = {"tail": True, "tail_crc": 0xDEAD}
    assert dev.evaluate_download(1, len(data), data, meta) == (True, None)
    monkeypatch.setattr(dev, "CRC16_ENFORCED_VARIANT", "XMODEM")
    assert dev.evaluate_download(1, len(data), data, meta) == (False, "CRC mismatch")
    assert dev.evaluate_download(1, len(data), data, {"tail": True, "tail_crc": 0x31C3}) == (True, None)


def test_decrypt_ble_file_raises_on_undecryptable(app_key):
    good = make_plaud_file(app_key)
    assert decrypt_ble_file(good, app_key)[0] == TOC
    with pytest.raises(BleDecryptError):
        decrypt_ble_file(make_plaud_file(app_key, decryptable=False), app_key)


# ---------------------------------------------------------------------------
# Stale clients / force_reset
# ---------------------------------------------------------------------------


def test_stale_client_disconnect_callback_is_ignored(env):
    device = FakeDevice({})

    async def go():
        device.install()
        dev._on_disconnect(object())  # an old, replaced BleakClient
        assert dev._device_state["connected"] is True
        dev._on_disconnect(device.client)  # the current one
        assert dev._device_state["connected"] is False

    asyncio.run(go())


def test_force_reset_schedules_real_disconnect(env):
    device = FakeDevice({})
    loop = dev._get_loop()
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(2)

    async def install():
        device.install()

    asyncio.run_coroutine_threadsafe(install(), loop).result(2)
    dev.force_reset()
    assert dev._device_state["client"] is None
    deadline = time.time() + 2
    while not device.client.disconnected and time.time() < deadline:
        time.sleep(0.01)
    assert device.client.disconnected


# ---------------------------------------------------------------------------
# Routes: busy handling
# ---------------------------------------------------------------------------


@pytest.fixture
def syncing(monkeypatch):
    """Pretend a sync is running and the device is connected."""
    monkeypatch.setitem(dev._device_state, "connected", True)
    assert dev._syncing.acquire(blocking=False)
    yield
    dev._syncing.release()


def test_verify_during_sync_reports_busy_without_reset(flask_client, syncing, monkeypatch):
    force_reset = MagicMock()
    monkeypatch.setattr(dev, "force_reset", force_reset)
    monkeypatch.setattr(dev, "run_async", MagicMock(side_effect=AssertionError("must not probe")))
    r = flask_client.post("/device/verify")
    assert r.status_code == 200
    assert r.get_json() == {"connected": True, "busy": True}
    force_reset.assert_not_called()
    assert dev._device_state["connected"] is True


@pytest.mark.parametrize(
    ("method", "path"),
    [("post", "/device/sync"), ("get", "/device/check-new"), ("get", "/device/info"), ("get", "/device/files")],
)
def test_busy_endpoints_return_409(flask_client, syncing, monkeypatch, method, path):
    monkeypatch.setattr(dev, "run_async", MagicMock(side_effect=AssertionError("must not touch BLE")))
    r = getattr(flask_client, method)(path)
    assert r.status_code == 409
    assert r.get_json() == {"error": "Sync already in progress", "busy": True}


def test_abort_sync_route_reports_whether_anything_ran(flask_client):
    assert flask_client.post("/device/abort-sync").get_json() == {"aborted": False}
    assert dev._sync_abort is False


def test_second_sync_rejected_while_first_waits_for_ble_lock(env, monkeypatch):
    """The sync slot is claimed before queueing on _ble_lock, so a second /sync
    can't slip in behind the first and run a full second sync."""
    calls = []

    async def fake_inner(progress_callback=None):
        calls.append(1)
        return {"files": []}

    monkeypatch.setattr(dev, "_sync_files_inner", fake_inner)
    results = {}
    dev._ble_lock.acquire()  # some other BLE op is running
    try:
        t = threading.Thread(target=lambda: results.setdefault("first", dev.run_sync_blocking(timeout=10)))
        t.start()
        deadline = time.time() + 2
        while not dev.is_syncing() and time.time() < deadline:
            time.sleep(0.01)
        assert dev.run_sync_blocking(timeout=10) is None  # busy
    finally:
        dev._ble_lock.release()
    t.join(5)
    assert results["first"] == {"files": []}
    assert calls == [1]
    assert not dev.is_syncing()


def test_check_new_counts_partials_as_new(flask_client, env, monkeypatch):
    rec = env["rec"]
    (rec / "1.ogg").write_bytes(b"x")
    (rec / "2_partial.bin").write_bytes(b"x")
    monkeypatch.setitem(dev._device_state, "connected", True)
    import app.routes.device as routes

    monkeypatch.setattr(routes, "get_all_recordings_dirs", lambda: [rec])
    monkeypatch.setattr(routes, "load_config", lambda: {})
    listing = [{"session_id": 1, "size": 10}, {"session_id": 2, "size": 20}]

    def fake_run_async(coro, **kw):
        coro.close()
        return listing

    monkeypatch.setattr(dev, "run_async", fake_run_async)
    body = flask_client.get("/device/check-new").get_json()
    assert body["new_files"] == 1
    assert body["files"][0]["session_id"] == 2
