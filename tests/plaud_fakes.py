"""Fakes for tests: PLAUD.AI recordings, the Mac's network tools (no device needed)."""

import asyncio
import os
import struct
import threading
import types

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

TOC = 0xB8  # CELT 20 ms — what the NotePin S records


class FakeNotePin:
    """The NotePin's side of a whole BLE connection: SN-signature handshake,
    session blob, then the encrypted command channel. No hardware, no radio.

    Patch `FakeNotePin.session_plaintext` in as the RSA decrypt (the blob's
    bytes are filler) and `fake.client_factory` over bleak.BleakClient.

      hs_status   status byte of the cmd 1 replies (None = never replies)
      silent      cmd ids the device doesn't answer (e.g. after a rejected token)
      before      cmd id -> plaintext packets sent just before its reply (e.g.
                  an unsolicited record event, a late reply to another command)
      replies     cmd id -> reply body override (bytes after [1, cid lo, cid hi])
      reply_as    cmd id -> the cmd id the reply carries (e.g. 10 -> 13)
    """

    def __init__(self, hs_status=0, port_version=9, silent=(), before=None, replies=None, reply_as=None):
        self.hs_status = hs_status
        self.port_version = port_version
        self.silent = set(silent)
        self.before = before or {}
        self.replies = replies or {}
        self.reply_as = reply_as or {}
        self.key, self.nonce, self.aad = os.urandom(32), os.urandom(12), os.urandom(12)
        self.cipher = ChaCha20Poly1305(self.key)
        self.handler = None
        self.received = []  # decrypted command plaintexts, in order
        self.sig_writes = 0
        self.notify_log = []  # (event, handler) for start/stop_notify
        self._seq = 0
        self._blob_sent = False
        self.encrypted = False
        self.client = None

    # -- what the app's RSA decrypt returns for the session blob
    def session_plaintext(self, *_):
        check = self.cipher.encrypt(self.nonce, b"PLAUD.AI", self.aad)
        return self.key + self.nonce + self.aad + check

    def client_factory(self, *a, **kw):
        self.client = FakeBleakClient(self, kw.get("disconnected_callback"))
        return self.client

    def sent_cmds(self):
        return [struct.unpack_from("<H", pt, 1)[0] for pt in self.received]

    def notify(self, payload):
        self._seq += 1
        self.handler(None, self.cipher.encrypt(self.nonce, struct.pack("<I", self._seq) + payload, self.aad))

    def reply(self, cid, body):
        self.notify(bytes([1]) + struct.pack("<H", cid) + body)

    def _default_body(self, cid):
        if cid == 1:
            # status, port version, tz h/m, audio channel, wifi, 2 flags, "V" + u24 version
            return (
                bytes([self.hs_status])
                + struct.pack("<H", self.port_version)
                + bytes([0, 0, 0, 1, 0, 0])
                + b"V"
                + (65546).to_bytes(3, "little")
            )
        if cid == 4:
            pt = self.received[-1]
            return pt[3:8] + bytes([1])  # echoes the time and time zone hours
        if cid == 3:
            return struct.pack("<I", 1) + bytes(12)
        if cid == 6:
            return struct.pack("<QQ", 1 << 30, 1 << 32)
        if cid == 9:
            return bytes([0, 77])
        return bytes([0])

    def on_write(self, data, response):
        data = bytes(data)
        if not self.encrypted:
            assert data[:2] == b"\x10\xfe", f"unexpected pre-encryption write {data[:4].hex()}"
            self.sig_writes += 1
            if not self._blob_sent and data[3] == data[2] - 1:  # a full signature set: send the blob
                self._blob_sent = True
                for i in range(3):
                    self.handler(None, b"\x12\xfe" + bytes([3, i]) + bytes(100))
            return
        pt = self.cipher.decrypt(self.nonce, data, self.aad)[4:]
        self.received.append(pt)
        cid = struct.unpack_from("<H", pt, 1)[0]
        loop = asyncio.get_running_loop()
        for extra in self.before.get(cid, []):
            loop.call_soon(self.notify, extra)
        if cid == 1 and self.hs_status is None or cid in self.silent:
            return
        body = self.replies.get(cid)
        body = body if body is not None else self._default_body(cid)
        # The real reply comes a little after any `before` packets, so a waiter
        # that takes the first type-1 packet would get the wrong one.
        delay = 0.02 if cid in self.before else 0
        loop.call_later(delay, self.reply, self.reply_as.get(cid, cid), body)


class FakeBleakClient:
    def __init__(self, device, disconnected_callback=None):
        self.device = device
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.services = [object()]

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, uuid, cb):
        self.device.handler = cb
        # Notifications restarted after the session blob = the encrypted channel.
        self.device.encrypted = self.device._blob_sent
        self.device.notify_log.append(("start", cb))

    async def stop_notify(self, uuid):
        self.device.notify_log.append(("stop", None))

    async def write_gatt_char(self, uuid, data, response=False):
        self.device.on_write(data, response)


def make_plaud_file(app_key, frames=300, decryptable=True):
    """A PLAUD.AI container like the device stores: header, RSA file key, 80 B frames.

    With decryptable=False the audio is random bytes, which fails decrypt_ble_file's
    Opus validity checks (an unknown key/layout).
    """
    file_key, nonce12 = os.urandom(32), os.urandom(12)
    hdr = bytearray(0x200)
    hdr[:8] = b"PLAUD.AI"
    hdr[0x84:0x90] = nonce12
    hdr[0x100:0x200] = app_key.public_key().encrypt(file_key, padding.PKCS1v15())
    if not decryptable:
        return bytes(hdr) + os.urandom(80 * frames)
    ks = Cipher(algorithms.ChaCha20(file_key, b"\0" * 4 + nonce12), mode=None).decryptor().update(b"\0" * 80)
    plain = b"".join(bytes([TOC]) + os.urandom(79) for _ in range(frames))
    return bytes(hdr) + bytes(a ^ ks[i % 80] for i, a in enumerate(plain))


class FakeMac:
    """Stands in for `networksetup` and `ipconfig` (patch it over subprocess.run).

    Simulates the saved ("preferred") network list in auto-join order and the
    Wi-Fi IP; records every command. Nothing touches the real network.
    """

    def __init__(self, preferred=(), ip="10.0.0.5", current_ssid="", iface="en1"):
        self.preferred = list(preferred)
        self.ip = ip
        self.current_ssid = current_ssid
        self.iface = iface
        self.calls = []
        self.fail_add = set()  # SSIDs whose re-add fails
        self.add_errors = {}  # SSID -> networksetup's error text for a failing re-add (default "Error")
        self.on_remove_all = None  # hook run before -removeallpreferredwirelessnetworks
        self.hooks = {}  # networksetup op -> hook run before it (e.g. a Gate)

    def _done(self, stdout="", returncode=0, stderr=""):
        return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    def run(self, cmd, **kw):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[0] == "ipconfig":
            return self._done(f"{self.ip}\n" if self.ip else "", 0 if self.ip else 1)
        assert cmd[0] == "networksetup", f"unexpected command {cmd}"
        op = cmd[1]
        if op in self.hooks:
            self.hooks[op]()
        if op == "-listpreferredwirelessnetworks":
            body = "".join(f"\t{s}\n" for s in self.preferred)
            return self._done(f"Preferred networks on {cmd[2]}:\n{body}")
        if op == "-removeallpreferredwirelessnetworks":
            if self.on_remove_all:
                self.on_remove_all()
            self.preferred = []
            return self._done()
        if op == "-addpreferredwirelessnetworkatindex":
            ssid, index = cmd[3], int(cmd[4])
            if ssid in self.fail_add:
                return self._done("", 1, self.add_errors.get(ssid, "Error"))
            self.preferred.insert(index, ssid)
            return self._done()
        if op == "-removepreferredwirelessnetwork":
            if cmd[3] in self.preferred:
                self.preferred.remove(cmd[3])
                return self._done(f"Removed {cmd[3]} from the preferred networks list")
            return self._done("", 1, "not found")
        if op == "-getairportnetwork":
            if self.current_ssid:
                return self._done(f"Current Wi-Fi Network: {self.current_ssid}\n")
            return self._done("You are not associated with an AirPort network.\n")
        if op in ("-setairportpower", "-setairportnetwork", "-listallhardwareports"):
            return self._done()
        raise AssertionError(f"unexpected networksetup op {cmd}")

    def ops(self, op):
        return [c for c in self.calls if len(c) > 1 and c[1] == op]


class Gate:
    """Pauses a fake command on whatever thread runs it until the test opens it.

    Use as a FakeMac hook (e.g. mac.on_remove_all = gate): `reached` is set when
    the command gets there, and it blocks until open() is called.
    """

    def __init__(self, timeout=10):
        self.reached = threading.Event()
        self._opened = threading.Event()
        self._timeout = timeout

    def __call__(self):
        self.reached.set()
        assert self._opened.wait(self._timeout), "gate never opened"

    def open(self):
        self._opened.set()


class Py310AsyncioTimeout(Exception):
    """What asyncio.TimeoutError is on Python 3.10: NOT the builtin TimeoutError."""


def py310_asyncio():
    """A stand-in `asyncio` module that behaves like Python 3.10's on timeouts.

    Monkeypatch it over a module's `asyncio` global: its wait_for raises
    Py310AsyncioTimeout (= asyncio.TimeoutError there), which a bare
    `except TimeoutError` does not catch.
    """
    fake = types.ModuleType("asyncio")
    fake.__dict__.update({k: v for k, v in vars(asyncio).items() if not k.startswith("__")})
    fake.TimeoutError = Py310AsyncioTimeout

    async def wait_for(aw, timeout):
        try:
            return await asyncio.wait_for(aw, timeout)
        except TimeoutError:
            raise Py310AsyncioTimeout from None

    fake.wait_for = wait_for
    return fake
