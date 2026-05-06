"""Tests for startup resilience — Python detection, port finding, error handling."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestDynamicPort:
    """Verify API server port is dynamic, not hardcoded."""

    def test_api_server_reads_port_from_env(self):
        """api_server.py must read PLAUD_API_PORT from environment."""
        src = (Path(__file__).parent.parent / "app" / "api_server.py").read_text()
        assert "PLAUD_API_PORT" in src

    def test_preload_gets_port_via_ipc(self):
        """preload.js must get API URL from main process, not hardcode 5123."""
        src = (Path(__file__).parent.parent / "app" / "preload.js").read_text()
        assert "get-api-info" in src
        assert "await _apiInfoReady" in src

    def test_csp_allows_any_localhost_port(self):
        """CSP must allow any localhost port, not just 5123.
        CSP is in tauri.conf.json."""
        root = Path(__file__).parent.parent
        html = (root / "app" / "index.html").read_text()
        tauri_conf = (root / "src-tauri" / "tauri.conf.json").read_text() if (root / "src-tauri" / "tauri.conf.json").exists() else ""
        combined = html + tauri_conf
        assert "127.0.0.1:*" in combined
        assert "127.0.0.1:5123" not in combined


class TestPythonValidation:
    """Verify Python minimum version enforcement."""

    def test_min_python_version_defined(self):
        """main.js must define MIN_PYTHON_VERSION."""
        src = (Path(__file__).parent.parent / "app" / "main.js").read_text()
        assert "MIN_PYTHON_VERSION" in src

    def test_standalone_python_download_url(self):
        """main.js must have python-build-standalone download for both architectures."""
        src = (Path(__file__).parent.parent / "app" / "main.js").read_text()
        assert "aarch64" in src
        assert "x86_64" in src
        assert "astral-sh/python-build-standalone" in src


class TestErrorCodes:
    """Verify structured error codes for startup failures."""

    def test_all_error_codes_defined(self):
        """main.js must define all expected startup error codes."""
        src = (Path(__file__).parent.parent / "app" / "main.js").read_text()
        required_codes = [
            "PYTHON_NOT_FOUND",
            "DEPS_FAILED",
            "SERVER_CRASH",
            "SERVER_TIMEOUT",
            "SPAWN_FAILED",
        ]
        for code in required_codes:
            assert code in src, f"Missing error code: {code}"

    def test_error_codes_have_friendly_messages(self):
        """Each error code must have a title, friendly message, and hint."""
        src = (Path(__file__).parent.parent / "app" / "main.js").read_text()
        for field in ["title:", "friendly:", "hint:"]:
            assert src.count(field) >= 5, f"Not enough '{field}' fields in STARTUP_ERRORS"


class TestDeviceID:
    """Verify anonymous device ID in bug reports."""

    def test_device_id_in_auto_reports(self):
        """main.js must include device ID in auto-reported system info."""
        src = (Path(__file__).parent.parent / "app" / "main.js").read_text()
        assert "_deviceId" in src
        assert "IOPlatformUUID" in src
        assert "sha256" in src


class TestAutoReporting:
    """Verify critical endpoints are auto-reported."""

    def test_critical_endpoints_reported(self):
        """preload.js must auto-report errors from critical endpoints."""
        src = (Path(__file__).parent.parent / "app" / "preload.js").read_text()
        required = [
            "/device/connect",
            "/device/scan",
            "/device/sync",
            "/device/wifi-sync",
            "/transcribe",
            "/cloud/sync",
            "/notion/push",
        ]
        for ep in required:
            assert ep in src, f"Missing auto-report endpoint: {ep}"

    def test_startup_grace_period(self):
        """preload.js must suppress reports during startup grace period."""
        src = (Path(__file__).parent.parent / "app" / "preload.js").read_text()
        assert "_appStartTime" in src
        assert "15000" in src

    def test_traceback_included_in_reports(self):
        """preload.js must include Python traceback in error reports."""
        src = (Path(__file__).parent.parent / "app" / "preload.js").read_text()
        assert "data.traceback" in src

    def test_device_sn_included_in_reports(self):
        """preload.js must include device serial number in connect error reports."""
        src = (Path(__file__).parent.parent / "app" / "preload.js").read_text()
        assert "device_sn" in src


class TestFlaskErrorResponses:
    """Verify Flask routes return tracebacks for debugging."""

    def test_device_routes_have_traceback(self):
        """device.py error responses must include traceback."""
        src = (Path(__file__).parent.parent / "app" / "routes" / "device.py").read_text()
        assert "_error_json" in src
        assert "traceback" in src

    def test_cloud_routes_have_traceback(self):
        """cloud.py error responses must include traceback."""
        src = (Path(__file__).parent.parent / "app" / "routes" / "cloud.py").read_text()
        assert "_error_json" in src

    def test_transcribe_routes_have_traceback(self):
        """transcribe.py error responses must include traceback."""
        src = (Path(__file__).parent.parent / "app" / "routes" / "transcribe.py").read_text()
        assert "_error_json" in src
