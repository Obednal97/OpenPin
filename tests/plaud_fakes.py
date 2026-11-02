"""Fake PLAUD.AI recordings for tests (no device needed)."""

import os

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
