"""Tests for per-device cloud credentials (SN_SIG + BIND_TOKEN)."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestNoHardcodedCredentials:
    """Verify no device-specific credentials are hardcoded."""

    def test_constants_no_hardcoded_bind_token(self):
        """constants.py must not contain a hardcoded BIND_TOKEN."""
        src = (Path(__file__).parent.parent / "app" / "utils" / "constants.py").read_text()
        assert "eyJhbGciOiJIUzI1NiI" not in src, "Hardcoded BIND_TOKEN JWT found in constants.py"

    def test_constants_no_hardcoded_sn_sig(self):
        """constants.py must not contain a hardcoded SN_SIG."""
        src = (Path(__file__).parent.parent / "app" / "utils" / "constants.py").read_text()
        assert "TOb4Huu" not in src, "Hardcoded SN_SIG found in constants.py"

    def test_device_py_no_import_bind_token(self):
        """device.py must not import BIND_TOKEN from constants."""
        src = (Path(__file__).parent.parent / "app" / "state" / "device.py").read_text()
        assert "import" not in src or "BIND_TOKEN" not in src.split("from app.utils.constants import")[1].split(")")[0] if "from app.utils.constants import" in src else True

    def test_device_py_no_import_sn_sig(self):
        """device.py must not import SN_SIG from constants."""
        src = (Path(__file__).parent.parent / "app" / "state" / "device.py").read_text()
        # Check that SN_SIG is not in any import from constants
        for line in src.split("\n"):
            if "from app.utils.constants import" in line:
                assert "SN_SIG" not in line, f"SN_SIG imported from constants: {line}"

    def test_wifi_worker_no_hardcoded_sn_sig(self):
        """wifi_sync_worker.py must not have a hardcoded SN_SIG."""
        src = (Path(__file__).parent.parent / "app" / "utils" / "wifi_sync_worker.py").read_text()
        assert "TOb4Huu" not in src, "Hardcoded SN_SIG in wifi_sync_worker.py"

    def test_wifi_worker_no_hardcoded_bind_token(self):
        """wifi_sync_worker.py must not have a hardcoded BIND_TOKEN."""
        src = (Path(__file__).parent.parent / "app" / "utils" / "wifi_sync_worker.py").read_text()
        assert "eyJhbGciOiJIUzI1NiI" not in src, "Hardcoded BIND_TOKEN in wifi_sync_worker.py"


class TestCloudDeviceCredentials:
    """Test per-device credential storage and retrieval."""

    def test_get_device_bind_token_returns_none_when_missing(self, tmp_config):
        """get_device_bind_token returns None for unknown device."""
        from app.state.cloud import get_device_bind_token
        assert get_device_bind_token("UNKNOWN_SN") is None

    def test_get_device_bind_token_returns_stored_value(self, tmp_config):
        """get_device_bind_token returns the stored token."""
        from app.utils.config import save_config
        save_config({"device_bind_tokens": {"TEST_SN_123": "test_token_value"}})

        from app.state.cloud import get_device_bind_token
        assert get_device_bind_token("TEST_SN_123") == "test_token_value"

    def test_get_device_sn_signature_returns_none_when_missing(self, tmp_config):
        """get_device_sn_signature returns None for unknown device."""
        from app.state.cloud import get_device_sn_signature
        assert get_device_sn_signature("UNKNOWN_SN") is None

    def test_get_device_sn_signature_returns_stored_value(self, tmp_config):
        """get_device_sn_signature returns the stored signature."""
        from app.utils.config import save_config
        save_config({"device_sn_signatures": {"TEST_SN_123": "base64sigdata=="}})

        from app.state.cloud import get_device_sn_signature
        assert get_device_sn_signature("TEST_SN_123") == "base64sigdata=="

    def test_bind_device_stores_token(self, tmp_config):
        """bind_device should store the access_token in config."""
        mock_response = {
            "status": 0,
            "data": {"access_token": "new_device_token_123", "sn": "SN_ABC"}
        }
        with patch("app.state.cloud.cloud_api", return_value=mock_response), \
             patch("app.state.cloud._cloud_state", {"logged_in": True, "token": "user_token"}):
            from app.state.cloud import bind_device, get_device_bind_token
            result = bind_device("SN_ABC")
            assert result["token"] == "new_device_token_123"
            assert get_device_bind_token("SN_ABC") == "new_device_token_123"

    def test_fetch_sn_signature_stores_signature(self, tmp_config):
        """fetch_sn_signature should store the signature in config."""
        mock_response = {
            "status": 0,
            "data": {"signature": {"SN_ABC": "test_sig_base64=="}}
        }
        with patch("app.state.cloud.cloud_api", return_value=mock_response), \
             patch("app.state.cloud._cloud_state", {"logged_in": True, "token": "user_token"}):
            from app.state.cloud import fetch_sn_signature, get_device_sn_signature
            result = fetch_sn_signature("SN_ABC")
            assert result["signature"] == "test_sig_base64=="
            assert get_device_sn_signature("SN_ABC") == "test_sig_base64=="

    def test_bind_device_requires_login(self, tmp_config):
        """bind_device should fail if not logged in."""
        with patch("app.state.cloud._cloud_state", {"logged_in": False, "token": None}):
            from app.state.cloud import bind_device
            result = bind_device("SN_ABC")
            assert "error" in result

    def test_fetch_sn_signature_requires_login(self, tmp_config):
        """fetch_sn_signature should fail if not logged in."""
        with patch("app.state.cloud._cloud_state", {"logged_in": False, "token": None}):
            from app.state.cloud import fetch_sn_signature
            result = fetch_sn_signature("SN_ABC")
            assert "error" in result

    def test_sn_sign_endpoint_uses_sns_array(self, tmp_config):
        """The API call must use 'sns' (array) not 'sn' (string)."""
        call_args = {}
        def mock_api(endpoint, method="GET", json_body=None, **kwargs):
            call_args["endpoint"] = endpoint
            call_args["method"] = method
            call_args["json_body"] = json_body
            return {"status": 0, "data": {"signature": {"SN_ABC": "sig=="}}}

        with patch("app.state.cloud.cloud_api", side_effect=mock_api), \
             patch("app.state.cloud._cloud_state", {"logged_in": True, "token": "tok"}):
            from app.state.cloud import fetch_sn_signature
            fetch_sn_signature("SN_ABC")

        assert call_args["endpoint"] == "/device/sn-sign"
        assert call_args["method"] == "POST"
        assert call_args["json_body"] == {"sns": ["SN_ABC"]}
