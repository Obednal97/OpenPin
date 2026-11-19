"""RSA helpers for the Plaud BLE handshake and audio decryption.

The device RSA-encrypts the per-connection ChaCha20 key with the public key the
client sends it (``get_public_key_full_pem``); the client decrypts it with
``rsa_decrypt``. The ChaCha20-Poly1305 part of the handshake lives in
``app/state/device.py`` and ``app/utils/wifi_sync_worker.py``. Protocol:
docs/reverse-engineering/PROTOCOL.md.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# Default key paths — use e2ee keys (matches official Plaud app)
# The device caches the public key on first pair and uses it for reconnects.
# The official app uses e2ee keys, so we must too for cross-app compatibility.
KEYS_DIR = Path(__file__).parent.parent / "keys"
PRIVATE_KEY_PATH = KEYS_DIR / "e2ee_private.pem"
PUBLIC_KEY_PATH = KEYS_DIR / "e2ee_public.pem"
# Plaud's apps have shipped two key pairs, and a NotePin keeps whichever public
# key an app last gave it (Harry's NotePin, March and November 2026). Ours first.
KNOWN_PRIVATE_KEY_PATHS = (PRIVATE_KEY_PATH, KEYS_DIR / "app_private.pem")


def load_rsa_private_key(path: str | Path | None = None):
    """Load RSA private key from PEM file."""
    path = Path(path) if path else PRIVATE_KEY_PATH
    with open(path, "rb") as f:
        return load_pem_private_key(f.read(), password=None)


def get_public_key_full_pem() -> str:
    """Get the FULL RSA public key PEM (with headers) for sending to device.

    The device requires the full PEM format including -----BEGIN/END PUBLIC KEY-----.
    """
    with open(PUBLIC_KEY_PATH) as f:
        return f.read().strip()


def rsa_decrypt(encrypted_data: bytes, private_key=None) -> bytes:
    """RSA decrypt data using PKCS1v15 padding (RSA/ECB/PKCS1Padding in Java)."""
    if private_key is None:
        private_key = load_rsa_private_key()

    return private_key.decrypt(
        encrypted_data,
        asym_padding.PKCS1v15(),
    )


def open_session_blob(blob: bytes, decrypt=rsa_decrypt, load_key=load_rsa_private_key):
    """(key, nonce, aad, key path) from the NotePin's session blob, or None.

    Tries each known key. With the wrong key PKCS#1 v1.5 returns random bytes of
    random length instead of failing, so each result is checked against the
    PLAUD.AI tag. The NotePin encrypts recordings' file keys with the same
    cached key, so decrypt them with this one first: with the repeating
    keystream layout a wrong key still passes the Opus check about half the
    time. ``decrypt``/``load_key`` are parameters so tests can fake them.
    """
    for path in KNOWN_PRIVATE_KEY_PATHS:
        try:
            dec = decrypt(blob, load_key(path))
        except (OSError, ValueError):
            continue
        key, nonce, aad, ct = dec[:32], dec[32:44], dec[44:56], dec[56:]
        try:
            if len(dec) > 56 and ChaCha20Poly1305(key).decrypt(nonce, ct, aad) == b"PLAUD.AI":
                return key, nonce, aad, path
        except InvalidTag:
            continue
    return None
