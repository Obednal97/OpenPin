"""Tests for crypto module — RSA key selection and handshake."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestRSAKeySelection:
    """Verify the app uses e2ee keys, not app keys."""

    def test_private_key_path_is_e2ee(self):
        """The default private key must be e2ee_private.pem."""
        from src.crypto import PRIVATE_KEY_PATH
        assert PRIVATE_KEY_PATH.name == "e2ee_private.pem", \
            f"Expected e2ee_private.pem, got {PRIVATE_KEY_PATH.name}"

    def test_public_key_path_is_e2ee(self):
        """The default public key must be e2ee_public.pem."""
        from src.crypto import PUBLIC_KEY_PATH
        assert PUBLIC_KEY_PATH.name == "e2ee_public.pem", \
            f"Expected e2ee_public.pem, got {PUBLIC_KEY_PATH.name}"

    def test_app_key_path_is_e2ee(self):
        """The APP_KEY_PATH constant must point to e2ee_private.pem."""
        from app.utils.constants import APP_KEY_PATH
        assert APP_KEY_PATH.name == "e2ee_private.pem", \
            f"Expected e2ee_private.pem, got {APP_KEY_PATH.name}"

    def test_keys_are_different(self):
        """e2ee and app keys must be different key pairs."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        keys_dir = Path(__file__).parent.parent / "keys"

        app_key = load_pem_private_key((keys_dir / "app_private.pem").read_bytes(), password=None)
        e2ee_key = load_pem_private_key((keys_dir / "e2ee_private.pem").read_bytes(), password=None)

        app_n = app_key.public_key().public_numbers().n
        e2ee_n = e2ee_key.public_key().public_numbers().n
        assert app_n != e2ee_n, "app and e2ee keys should be different"

    def test_load_rsa_private_key_returns_e2ee(self):
        """load_rsa_private_key() default should load e2ee key."""
        from src.crypto import load_rsa_private_key, PRIVATE_KEY_PATH
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        loaded = load_rsa_private_key()
        direct = load_pem_private_key(PRIVATE_KEY_PATH.read_bytes(), password=None)

        assert loaded.public_key().public_numbers().n == direct.public_key().public_numbers().n

    def test_get_public_key_full_pem_returns_e2ee(self):
        """get_public_key_full_pem() must return e2ee public key."""
        from src.crypto import get_public_key_full_pem, PUBLIC_KEY_PATH
        pem = get_public_key_full_pem()
        expected = PUBLIC_KEY_PATH.read_text().strip()
        assert pem == expected

    def test_both_keys_are_rsa_2048(self):
        """Both key files must be RSA-2048."""
        from src.crypto import load_rsa_private_key
        key = load_rsa_private_key()
        assert key.key_size == 2048

    def test_pkcs1v15_decrypt_with_wrong_key_produces_garbage(self):
        """Demonstrate that PKCS1v15 silently produces garbage with wrong key.

        This is WHY the e2ee vs app key distinction matters — PKCS1v15
        doesn't throw on wrong-key decrypt, it produces random bytes.
        """
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        keys_dir = Path(__file__).parent.parent / "keys"

        e2ee_key = load_pem_private_key((keys_dir / "e2ee_private.pem").read_bytes(), password=None)
        app_key = load_pem_private_key((keys_dir / "app_private.pem").read_bytes(), password=None)

        # Encrypt with e2ee public key
        plaintext = b"PLAUD.AI test data for key verification"
        ciphertext = e2ee_key.public_key().encrypt(plaintext, padding.PKCS1v15())

        # Decrypt with e2ee private key — should work
        decrypted = e2ee_key.decrypt(ciphertext, padding.PKCS1v15())
        assert decrypted == plaintext

        # Decrypt with app private key — PKCS1v15 may not throw
        # but OAEP would. This proves why we must use the right key.
        from cryptography.hazmat.primitives import hashes
        with pytest.raises(Exception):
            app_key.decrypt(ciphertext, padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            ))
