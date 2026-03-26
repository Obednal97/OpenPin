"""Plaud BLE encryption - RSA key exchange + ChaCha20-Poly1305.

Protocol (portVersion >= 20):
1. Client sends RSA public key to device in 100-byte chunks via cmd 65042
2. Device sends RSA-encrypted blob back via notifications
3. Client RSA-decrypts to get: ChaCha20 key (32B) + nonce (12B) + AD (12B) + ciphertext
4. Client ChaCha20-decrypts the ciphertext, expects plaintext "PLAUD.AI"
5. All subsequent BLE data is encrypted with those ChaCha20 parameters
"""

import os
import base64
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import rsa, padding as asym_padding
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key, load_pem_public_key, Encoding, PublicFormat,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305


# Default key paths
KEYS_DIR = Path(__file__).parent.parent / "keys"
PRIVATE_KEY_PATH = KEYS_DIR / "app_private.pem"
PUBLIC_KEY_PATH = KEYS_DIR / "app_public.pem"


def load_rsa_private_key(path: str | Path | None = None):
    """Load RSA private key from PEM file."""
    path = Path(path) if path else PRIVATE_KEY_PATH
    with open(path, "rb") as f:
        return load_pem_private_key(f.read(), password=None)


def load_rsa_public_key(path: str | Path | None = None):
    """Load RSA public key from PEM file."""
    path = Path(path) if path else PUBLIC_KEY_PATH
    with open(path, "rb") as f:
        return load_pem_public_key(f.read())


def get_public_key_der() -> bytes:
    """Get the RSA public key in DER format for sending to device."""
    pubkey = load_rsa_public_key()
    return pubkey.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


def get_public_key_pem_body() -> str:
    """Get the RSA public key PEM body (without headers) for sending to device."""
    with open(PUBLIC_KEY_PATH) as f:
        pem = f.read()
    # Strip PEM headers and join lines
    lines = pem.strip().split("\n")
    body_lines = [l for l in lines if not l.startswith("-----")]
    return "".join(body_lines)


def rsa_decrypt(encrypted_data: bytes, private_key=None) -> bytes:
    """RSA decrypt data using PKCS1v15 padding (RSA/ECB/PKCS1Padding in Java)."""
    if private_key is None:
        private_key = load_rsa_private_key()

    return private_key.decrypt(
        encrypted_data,
        asym_padding.PKCS1v15(),
    )


def parse_handshake_blob(decrypted: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    """Parse the decrypted RSA blob into ChaCha20 parameters.

    Returns: (chacha_key, nonce, additional_data, ciphertext)
    - chacha_key: 32 bytes
    - nonce: 12 bytes
    - additional_data: 12 bytes
    - ciphertext: remaining bytes
    """
    if len(decrypted) < 56:
        raise ValueError(f"Decrypted blob too short: {len(decrypted)} bytes (need >= 56)")

    chacha_key = decrypted[0:32]
    nonce = decrypted[32:44]
    additional_data = decrypted[44:56]
    ciphertext = decrypted[56:]

    return chacha_key, nonce, additional_data, ciphertext


def chacha20_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes = b"") -> bytes:
    """Decrypt data using ChaCha20-Poly1305."""
    cipher = ChaCha20Poly1305(key)
    return cipher.decrypt(nonce, ciphertext, aad)


def chacha20_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """Encrypt data using ChaCha20-Poly1305."""
    cipher = ChaCha20Poly1305(key)
    return cipher.encrypt(nonce, plaintext, aad)


class BleEncryption:
    """Manages BLE encryption state after handshake."""

    def __init__(self):
        self.key: bytes | None = None
        self.nonce: bytes | None = None
        self.aad: bytes | None = None
        self.active = False

    def setup(self, key: bytes, nonce: bytes, aad: bytes):
        """Set up encryption parameters after successful handshake."""
        self.key = key
        self.nonce = nonce
        self.aad = aad
        self.active = True

    def decrypt(self, data: bytes) -> bytes:
        """Decrypt incoming BLE data."""
        if not self.active:
            return data
        return chacha20_decrypt(self.key, self.nonce, data, self.aad)

    def encrypt(self, data: bytes) -> bytes:
        """Encrypt outgoing BLE data."""
        if not self.active:
            return data
        return chacha20_encrypt(self.key, self.nonce, data, self.aad)


def complete_handshake(rsa_encrypted_blob: bytes) -> BleEncryption:
    """Complete the encrypted handshake by decrypting the device's RSA blob.

    Args:
        rsa_encrypted_blob: The RSA-encrypted data received from the device

    Returns:
        BleEncryption instance with active encryption

    Raises:
        ValueError: If handshake validation fails (plaintext != "PLAUD.AI")
    """
    # Step 1: RSA decrypt
    privkey = load_rsa_private_key()
    decrypted = rsa_decrypt(rsa_encrypted_blob, privkey)

    # Step 2: Parse ChaCha20 parameters
    chacha_key, nonce, aad, ciphertext = parse_handshake_blob(decrypted)

    # Step 3: ChaCha20 decrypt the test message
    plaintext = chacha20_decrypt(chacha_key, nonce, ciphertext, aad)

    # Step 4: Validate
    expected = b"PLAUD.AI"
    if plaintext != expected:
        raise ValueError(
            f"Handshake validation failed: got {plaintext!r}, expected {expected!r}"
        )

    # Step 5: Set up encryption
    enc = BleEncryption()
    enc.setup(chacha_key, nonce, aad)
    return enc
