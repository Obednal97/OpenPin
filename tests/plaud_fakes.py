"""Fakes for tests: PLAUD.AI recordings, the Mac's network tools (no device needed)."""

import asyncio
import os
import threading
import types

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

TOC = 0xB8  # CELT 20 ms — what the NotePin S records


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
