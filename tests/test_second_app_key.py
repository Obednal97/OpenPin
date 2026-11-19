"""A NotePin keeps whichever app public key it was last given, and Plaud's apps
have shipped two key pairs. Harry's NotePin (19 Nov 2026) had the other one
cached, so our key alone gave "ChaCha20Poly1305 key must be 32 bytes"."""

import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

import src.crypto as crypto
from app.utils.audio import decrypt_ble_file
from tests.plaud_fakes import make_plaud_file


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write(path, key):
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return path


def _session_blob(public_key):
    key, nonce, aad = os.urandom(32), os.urandom(12), os.urandom(12)
    check = ChaCha20Poly1305(key).encrypt(nonce, b"PLAUD.AI", aad)
    return public_key.encrypt(key + nonce + aad + check, padding.PKCS1v15()), key


def test_session_blob_for_the_second_key_opens(tmp_path, monkeypatch):
    ours, other = _key(), _key()
    paths = (_write(tmp_path / "e2ee_private.pem", ours), _write(tmp_path / "app_private.pem", other))
    monkeypatch.setattr(crypto, "KNOWN_PRIVATE_KEY_PATHS", paths)
    blob, session_key = _session_blob(other.public_key())
    key, _nonce, _aad, path = crypto.open_session_blob(blob)
    assert key == session_key and path == paths[1]


def test_session_blob_for_an_unknown_key_is_none(tmp_path, monkeypatch):
    paths = (_write(tmp_path / "e2ee_private.pem", _key()), tmp_path / "missing.pem")
    monkeypatch.setattr(crypto, "KNOWN_PRIVATE_KEY_PATHS", paths)
    blob, _ = _session_blob(_key().public_key())
    assert crypto.open_session_blob(blob) is None


def test_recording_decrypts_with_the_key_listed_first():
    """BLE sync lists the key that opened the session first; that one decides."""
    ours, other = _key(), _key()
    data = make_plaud_file(other)
    assert decrypt_ble_file(data, [other, ours]) == decrypt_ble_file(data, other)

