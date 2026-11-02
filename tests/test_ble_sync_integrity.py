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
from tests.plaud_fakes import TOC, make_plaud_file, py310_asyncio

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
      refuse        cmd-28 reply only (status from start_status, default 1), no data
      duplicate     like complete, but every data packet is sent twice (retransmits)
      gap           like complete, but the third data packet never arrives
    start_status[sid]: byte 7 of the cmd-28 reply (default 0).
    delete_mode: ok | none (no reply) | fail (status 1) | wrong_sid
    inject[sid]: plaintext packets sent mid-stream while `sid` downloads (e.g. a
      late data packet, end marker or tail from another session).
    """

    def __init__(self, files, behaviours=None, delete_mode="ok", start_status=None, inject=None):
        self.files = files  # sid -> bytes
        self.behaviours = behaviours or {}
        self.start_status = start_status or {}
        self.delete_mode = delete_mode
        self.inject = inject or {}
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
        full = ("complete", "end_only", "error_end", "duplicate", "gap")
        cut = len(data) if mode in full else int(len(data) * 0.4)
        if mode == "refuse":
            self.reply(28, struct.pack("<I", sid) + bytes([self.start_status.get(sid, 1)]))
            return
        self.reply(28, struct.pack("<I", sid) + bytes([self.start_status.get(sid, 0)]))
        for n, off in enumerate(range(0, cut, CHUNK)):
            chunk = data[off : min(off + CHUNK, cut)]
            pkt = bytes([2]) + struct.pack("<II", sid, off) + bytes([len(chunk)]) + chunk
            if not (mode == "gap" and n == 2):
                self.notify(pkt)
            if mode == "duplicate":
                self.notify(pkt)
            if n == 1:
                for extra in self.inject.get(sid, []):
                    self.notify(extra)
            await asyncio.sleep(0)
        if mode in ("stall", "slow"):
            return
        if mode == "drop":
            await asyncio.sleep(0.01)
            dev._on_disconnect(self.client)  # what bleak's disconnected_callback does
            return
        err = 3 if mode == "error_end" else 0
        self.notify(bytes([2]) + struct.pack("<II", sid, 0xFFFFFFFF) + bytes([err]))
        if mode in ("complete", "error_end", "duplicate", "gap"):
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
    monkeypatch.setattr(dev, "_download_failures", {})
    yield {"rec": rec, "config": config, "uploads": uploads}
    dev._reset_state()
    if dev._syncing.locked():
        dev._syncing.release()


def run_sync(device, file_list, background=False):
    async def go():
        device.install()

        async def fake_list():
            return [{"session_id": sid, "size": size, "duration_est": 0} for sid, size in file_list]

        dev.list_device_files = fake_list
        return await dev.sync_files(background=background)

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


def test_redownload_backup_hook_only_after_ogg_written(env, app_key, monkeypatch):
    calls = []
    monkeypatch.setattr(dev, "_finalize_redownload_backup", lambda sid: calls.append(sid))
    good, bad = make_plaud_file(app_key), make_plaud_file(app_key)
    run_sync(FakeDevice({111: good, 112: bad}, {112: "error_end"}), [(111, len(good)), (112, len(bad))])
    assert calls == [111]


def test_complete_with_end_packet_but_no_tail_is_accepted(env, app_key):
    data = make_plaud_file(app_key)
    device = FakeDevice({102: data}, {102: "end_only"})
    result = run_sync(device, [(102, len(data))])
    assert entry(result, 102)["status"] == "new"


@pytest.mark.parametrize("mode", ["drop", "stall", "error_end"])
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


def _data_pkt(sid, off, chunk):
    return bytes([2]) + struct.pack("<II", sid, off) + bytes([len(chunk)]) + chunk


def _end_pkt(sid, err=0):
    return bytes([2]) + struct.pack("<II", sid, 0xFFFFFFFF) + bytes([err])


def _tail_pkt(sid, crc=0):
    return bytes([1]) + struct.pack("<H", 29) + struct.pack("<IH", sid, crc)


def test_packets_from_another_session_are_ignored(env, app_key, caplog):
    """A late data packet, end marker and tail from an earlier (stalled) file
    arriving mid-download neither corrupt nor end the current file."""
    data = make_plaud_file(app_key)
    stale = [_data_pkt(999, CHUNK * 2, b"\xee" * CHUNK), _end_pkt(999), _tail_pkt(999)]
    device = FakeDevice({1001: data}, inject={1001: stale})
    with caplog.at_level("INFO"):
        result = run_sync(device, [(1001, len(data))])
    e = entry(result, 1001)
    assert e["status"] == "new"
    assert result["deleted_from_device"] == [1001]
    assert "ignored 3 packet(s) for other sessions" in caplog.text


def test_late_packets_from_a_stalled_file_do_not_reach_the_next(env, app_key):
    a, b = make_plaud_file(app_key), make_plaud_file(app_key)
    # File 1101 stalls; its last packets turn up while 1102 downloads.
    late = [_data_pkt(1101, int(len(a) * 0.4), a[int(len(a) * 0.4) :][:CHUNK]), _end_pkt(1101)]
    device = FakeDevice({1101: a, 1102: b}, {1101: "stall"}, inject={1102: late})
    result = run_sync(device, [(1101, len(a)), (1102, len(b))])
    assert entry(result, 1101)["status"] == "partial"
    assert entry(result, 1102)["status"] == "new"
    assert result["deleted_from_device"] == [1102]


def test_end_marker_for_another_session_does_not_end_the_download(env, app_key):
    data = make_plaud_file(app_key)
    device = FakeDevice({1201: data}, {1201: "stall"}, inject={1201: [_end_pkt(4242), _tail_pkt(4242)]})
    result = run_sync(device, [(1201, len(data))])
    e = entry(result, 1201)
    assert e["status"] == "partial" and e["reason"] == "stalled"
    assert 31 not in device.sent


def test_duplicate_data_packets_are_dropped(env, app_key, caplog):
    data = make_plaud_file(app_key)
    device = FakeDevice({1301: data}, {1301: "duplicate"})
    with caplog.at_level("INFO"):
        result = run_sync(device, [(1301, len(data))])
    e = entry(result, 1301)
    assert e["status"] == "new"
    assert "size_mismatch" not in e
    assert result["deleted_from_device"] == [1301]
    assert "dropped" in caplog.text and "retransmitted" in caplog.text


def test_gap_in_offsets_is_partial_not_concatenated(env, app_key):
    data = make_plaud_file(app_key)
    device = FakeDevice({1401: data}, {1401: "gap"})
    result = run_sync(device, [(1401, len(data))])
    e = entry(result, 1401)
    assert e["status"] == "partial"
    assert e["reason"].startswith("data gap")
    # Only the contiguous prefix is kept: the bytes after the gap never land
    # next to the bytes before it.
    assert (env["rec"] / "1401_partial.bin").read_bytes() == data[: 2 * CHUNK]
    assert 30 in device.sent, "the download is stopped once a gap shows up"
    assert 31 not in device.sent


def test_handler_offset_rules():
    """Unit check of the data-packet rules on the real handler."""
    file_data, file_done, meta = bytearray(), asyncio.Event(), {"sid": 7}
    dev._on_file_data(_data_pkt(7, 0, b"abc"), file_data, file_done, meta)
    dev._on_file_data(_data_pkt(7, 0, b"abc"), file_data, file_done, meta)  # retransmit
    dev._on_file_data(_data_pkt(7, 1, b"bcde"), file_data, file_done, meta)  # overlap: keep "de"
    dev._on_file_data(_data_pkt(8, 5, b"zz"), file_data, file_done, meta)  # other session
    assert bytes(file_data) == b"abcde"
    assert meta["duplicates"] == 2 and meta["stray"] == 1
    assert not file_done.is_set()
    dev._on_file_data(_data_pkt(7, 9, b"xy"), file_data, file_done, meta)  # gap
    assert meta["gap"] == (9, 5) and file_done.is_set()
    dev._on_file_data(_data_pkt(7, 5, b"fg"), file_data, file_done, meta)  # after a gap: ignored
    assert bytes(file_data) == b"abcde"
    # No download in progress: everything is stray.
    idle = {"sid": None}
    dev._on_file_data(_data_pkt(7, 0, b"abc"), bytearray(), asyncio.Event(), idle)
    assert idle["stray"] == 1


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


@pytest.mark.parametrize(("mode", "expected"), [("ok", True), ("none", False), ("fail", False), ("wrong_sid", False)])
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
    ok, mismatch, partial = dev.DOWNLOAD_OK, dev.DOWNLOAD_SIZE_MISMATCH, dev.DOWNLOAD_PARTIAL
    meta_ok = {"end": True, "end_error": 0}
    assert dev.evaluate_download(1, 3, b"abc", meta_ok) == (ok, None)
    # Clean end packet but size != file list → only kept if it decrypts (caller's job)
    assert dev.evaluate_download(1, 4, b"abc", meta_ok)[0] == mismatch
    assert dev.evaluate_download(1, 2, b"abc", meta_ok)[0] == mismatch
    # Tail-only (no end packet) or no data at all with a size mismatch → partial
    assert dev.evaluate_download(1, 4, b"abc", {"tail": True})[0] == partial
    assert dev.evaluate_download(1, 4, b"", meta_ok)[0] == partial
    assert dev.evaluate_download(1, 3, b"abc", {})[0] == partial
    assert dev.evaluate_download(1, 3, b"abc", {"end": True, "end_error": 5})[0] == partial
    assert dev.evaluate_download(1, 4, b"abc", {"end": True, "end_error": 5})[0] == partial
    # cmd-28 reply status: meaning unconfirmed → never rejects on its own
    assert dev.evaluate_download(1, 3, b"abc", {"tail": True, "start_status": 2}) == (ok, None)
    assert dev.evaluate_download(1, 3, b"abc", meta_ok, interrupted="stalled") == (partial, "stalled")
    assert dev.evaluate_download(1, 4, b"abc", meta_ok, interrupted="stalled") == (partial, "stalled")
    # A gap in the data offsets is never OK, even if the byte count happens to match.
    assert dev.evaluate_download(1, 3, b"abc", {**meta_ok, "gap": (6, 3)})[0] == partial


def test_crc_logged_not_enforced_by_default(monkeypatch, caplog):
    data = b"123456789"
    v = dev.crc16_variants(data)
    # Standard check values for the catalogued CRC-16 variants
    assert v == {"XMODEM": 0x31C3, "CCITT-FALSE": 0x29B1, "ARC": 0xBB3D, "MODBUS": 0x4B37, "KERMIT": 0x2189}
    assert set(dev.crc16_variants(data, slow_limit=4)) == {"XMODEM", "CCITT-FALSE"}
    meta = {"tail": True, "tail_crc": 0xDEAD}
    assert dev.evaluate_download(1, len(data), data, meta) == (dev.DOWNLOAD_OK, None)
    monkeypatch.setattr(dev, "CRC16_ENFORCED_VARIANT", "XMODEM")
    assert dev.evaluate_download(1, len(data), data, meta) == (dev.DOWNLOAD_PARTIAL, "CRC mismatch")
    assert dev.evaluate_download(1, len(data), data, {"tail": True, "tail_crc": 0x31C3}) == (dev.DOWNLOAD_OK, None)
    with caplog.at_level("INFO"):
        dev._log_tail_crc(7, data, 0xBB3D)
    assert "CRC session 7: device=0xbb3d matches ARC" in caplog.text


def test_crc_diagnostic_runs_off_the_event_loop(env, app_key, monkeypatch):
    """The pure-Python CRC variants must not run on the BLE loop thread."""
    threads = []
    real = dev._log_tail_crc

    def spy(sid, data, crc):
        threads.append(threading.get_ident())
        real(sid, data, crc)

    monkeypatch.setattr(dev, "_log_tail_crc", spy)
    data = make_plaud_file(app_key)
    loop_thread = []

    async def go():
        loop_thread.append(threading.get_ident())
        device = FakeDevice({121: data})
        device.install()

        async def fake_list():
            return [{"session_id": 121, "size": len(data), "duration_est": 0}]

        dev.list_device_files = fake_list
        return await dev.sync_files()

    original = dev.list_device_files
    try:
        result = asyncio.run(go())  # asyncio.run waits for the default executor
    finally:
        dev.list_device_files = original
    assert entry(result, 121)["status"] == "new"
    assert len(threads) == 1 and threads[0] != loop_thread[0]


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

    async def fake_inner(progress_callback=None, background=False):
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


# ---------------------------------------------------------------------------
# Fail-safe acceptance: the protocol assumptions are unconfirmed on hardware
# ---------------------------------------------------------------------------


def test_nonzero_start_status_with_full_data_is_accepted(env, app_key, caplog):
    data = make_plaud_file(app_key)
    device = FakeDevice({131: data}, start_status={131: 2})
    with caplog.at_level("WARNING"):
        result = run_sync(device, [(131, len(data))])
    e = entry(result, 131)
    assert e["status"] == "new"
    assert "size_mismatch" not in e
    assert result["deleted_from_device"] == [131]  # exact size: still eligible
    assert "cmd-28 reply status 2" in caplog.text


def test_refused_download_without_data_fails_fast(env, app_key, monkeypatch):
    monkeypatch.setattr(dev, "_STALL_TIMEOUT_S", 30)  # would hang the test if refusal weren't detected
    monkeypatch.setattr(dev, "_REFUSED_GRACE_S", 0.05)
    data = make_plaud_file(app_key)
    device = FakeDevice({132: data}, {132: "refuse"}, start_status={132: 4})
    t0 = time.time()
    result = run_sync(device, [(132, len(data))])
    assert time.time() - t0 < 5
    e = entry(result, 132)
    assert e["status"] == "partial"
    assert e["reason"] == "device refused download (status 4)"
    assert e["received"] == 0
    assert not (env["rec"] / "132.ogg").exists()
    assert 31 not in device.sent


@pytest.mark.parametrize("case", ["list_larger", "list_smaller", "truncate_end"])
def test_size_mismatch_accepted_if_it_decrypts_and_never_auto_deleted(env, app_key, caplog, case):
    assert env["config"]["auto_delete_after_sync"] is True
    data, exact = make_plaud_file(app_key), make_plaud_file(app_key)
    if case == "truncate_end":
        device = FakeDevice({141: data, 142: exact}, {141: "truncate_end"})
        listed, received = len(data), int(len(data) * 0.4)
    else:
        device = FakeDevice({141: data, 142: exact})
        listed, received = len(data) + (1000 if case == "list_larger" else -1000), len(data)
    with caplog.at_level("WARNING"):
        result = run_sync(device, [(141, listed), (142, len(exact))])

    e = entry(result, 141)
    assert e["status"] == "new"
    assert e["size_mismatch"] == {"received": received, "expected": listed}
    assert (env["rec"] / "141.ogg").exists()
    assert (env["rec"] / "141_encrypted.bin").read_bytes() == data[:received]  # kept for diagnosis
    assert not (env["rec"] / "141_partial.bin").exists()
    assert "[BLE] size mismatch accepted for session 141" in caplog.text
    # Only the exact-size file is auto-deleted; the mismatched one stays on the device.
    assert result["deleted_from_device"] == [142]
    assert 141 not in result.get("delete_failed", [])
    assert dev.download_failures(141) == 0


def test_size_mismatch_that_does_not_decrypt_is_partial(env, app_key):
    data = make_plaud_file(app_key, decryptable=False)
    device = FakeDevice({151: data})
    result = run_sync(device, [(151, len(data) + 500)])
    e = entry(result, 151)
    assert e["status"] == "partial"
    assert "size mismatch" in e["reason"] and "decrypt failed" in e["reason"]
    assert not (env["rec"] / "151.ogg").exists()
    assert (env["rec"] / "151_partial.bin").read_bytes() == data
    assert 31 not in device.sent


# ---------------------------------------------------------------------------
# Background retry cap
# ---------------------------------------------------------------------------


def test_retry_counter_rules(env):
    track = dev.track_download_result
    track({"session_id": 5, "status": "partial", "reason": "aborted"})  # user switched to WiFi
    assert dev.download_failures(5) == 0
    track({"session_id": 5, "status": "partial", "reason": "stalled"})
    track({"session_id": "5", "status": "decrypt_failed"})  # str and int ids share a counter
    track({"session_id": 5, "status": "exists"})
    assert dev.download_failures(5) == 2 and not dev.is_retry_capped(5)
    track({"session_id": 5, "status": "partial", "reason": "size mismatch"})
    assert dev.is_retry_capped(5)
    track({"session_id": 5, "status": "new"})
    assert dev.download_failures(5) == 0


def _fake_listing(monkeypatch, env, listing):
    import app.routes.device as routes

    monkeypatch.setitem(dev._device_state, "connected", True)
    monkeypatch.setattr(routes, "get_all_recordings_dirs", lambda: [env["rec"]])
    monkeypatch.setattr(routes, "load_config", lambda: {})

    def fake_run_async(coro, **kw):
        coro.close()
        return listing

    monkeypatch.setattr(dev, "run_async", fake_run_async)


def test_retry_cap_stops_background_but_not_manual_sync(env, app_key, flask_client, monkeypatch):
    data, other = make_plaud_file(app_key), make_plaud_file(app_key)
    for _ in range(dev.RETRY_CAP):
        r = run_sync(FakeDevice({161: data}, {161: "error_end"}), [(161, len(data))], background=True)
        assert entry(r, 161)["status"] == "partial"
    assert dev.is_retry_capped(161)

    # Background sync skips it without requesting it from the device…
    bg = FakeDevice({161: data, 162: other})
    r = run_sync(bg, [(161, len(data)), (162, len(other))], background=True)
    e = entry(r, 161)
    assert e["status"] == "skipped" and e["attempts"] == dev.RETRY_CAP
    assert r["retry_capped"] == [161]
    assert bg.sent.count(28) == 1  # only 162
    assert entry(r, 162)["status"] == "new"

    # …and /device/check-new (background by default) stops counting it.
    _fake_listing(monkeypatch, env, [{"session_id": 161, "size": len(data)}, {"session_id": 163, "size": 7}])
    body = flask_client.get("/device/check-new").get_json()
    assert body["new_files"] == 1 and body["total_size"] == 7
    assert [f["session_id"] for f in body["files"]] == [163]
    assert body["retry_capped"] == [{"session_id": 161, "size": len(data), "attempts": dev.RETRY_CAP}]
    body = flask_client.get("/device/check-new?background=0").get_json()
    assert body["new_files"] == 2 and "retry_capped" not in body

    # An explicit (manual) sync still retries it; success resets the counter.
    manual = FakeDevice({161: data})
    r = run_sync(manual, [(161, len(data))])
    assert 28 in manual.sent
    assert entry(r, 161)["status"] == "new"
    assert dev.download_failures(161) == 0 and not dev.is_retry_capped(161)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, False),
        ({"json": {}}, False),
        ({"json": {"background": True}}, True),
        ({"json": {"background": False}}, False),
        ({"query_string": {"background": "1"}}, True),
    ],
)
def test_sync_route_background_flag(flask_client, monkeypatch, kwargs, expected):
    monkeypatch.setitem(dev._device_state, "connected", True)
    seen = {}

    def fake_run_sync_blocking(timeout=1800, background=False):
        seen["background"] = background
        return {"files": []}

    monkeypatch.setattr(dev, "run_sync_blocking", fake_run_sync_blocking)
    r = flask_client.post("/device/sync", **kwargs)
    assert r.status_code == 200
    assert seen["background"] is expected


# ---------------------------------------------------------------------------
# Python 3.10: asyncio.TimeoutError is not the builtin TimeoutError
# ---------------------------------------------------------------------------


def test_send_cmd_timeout_handled_with_py310_asyncio(env, monkeypatch):
    monkeypatch.setattr(dev, "asyncio", py310_asyncio())
    device = FakeDevice({})

    async def go():
        device.install()
        return await dev.send_cmd(bytes([1, 9, 0]), timeout=0.05)  # fake device never answers cmd 9

    assert asyncio.run(go()) is None


def test_stall_detected_with_py310_asyncio(env, app_key, monkeypatch):
    monkeypatch.setattr(dev, "asyncio", py310_asyncio())
    data = make_plaud_file(app_key)
    result = run_sync(FakeDevice({171: data}, {171: "stall"}), [(171, len(data))])
    e = entry(result, 171)
    assert e["status"] == "partial" and e["reason"] == "stalled"
