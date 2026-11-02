"""Fake PLAUD.AI recordings for tests (no device needed)."""

import asyncio
import os
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
