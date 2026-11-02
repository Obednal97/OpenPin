"""Tests for startup resilience — Python detection, port finding, error handling.

After the Tauri migration, the Electron entrypoints (app/main.js, app/preload.js)
were replaced by Rust (src-tauri/src/python.rs, src-tauri/src/commands/mod.rs)
and the browser-side bridge (app/tauri-bridge.js). These tests assert against
the new locations.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
RUST_PYTHON = ROOT / "src-tauri" / "src" / "python.rs"
RUST_COMMANDS = ROOT / "src-tauri" / "src" / "commands" / "mod.rs"
BRIDGE_JS = ROOT / "app" / "tauri-bridge.js"


class TestDynamicPort:
    """Verify API server port is dynamic, not hardcoded."""

    def test_api_server_reads_port_from_env(self):
        """api_server.py must read PLAUD_API_PORT from environment."""
        src = (ROOT / "app" / "api_server.py").read_text()
        assert "PLAUD_API_PORT" in src

    def test_bridge_gets_port_via_ipc(self):
        """tauri-bridge.js must resolve the API URL from Rust, not hardcode 5123."""
        src = BRIDGE_JS.read_text()
        assert "get_api_info" in src
        assert "_apiInfoReady" in src

    def test_csp_allows_any_localhost_port(self):
        """CSP must allow any localhost port, not just 5123."""
        html = (ROOT / "app" / "index.html").read_text()
        tauri_conf_path = ROOT / "src-tauri" / "tauri.conf.json"
        tauri_conf = tauri_conf_path.read_text() if tauri_conf_path.exists() else ""
        combined = html + tauri_conf
        assert "127.0.0.1:*" in combined
        assert "127.0.0.1:5123" not in combined


class TestPythonValidation:
    """Verify Python minimum version enforcement (now in Rust)."""

    def test_min_python_version_defined(self):
        """python.rs must define MIN_PYTHON_VERSION."""
        src = RUST_PYTHON.read_text()
        assert "MIN_PYTHON_VERSION" in src

    def test_standalone_python_download_url(self):
        """python.rs must have python-build-standalone download for both architectures."""
        src = RUST_PYTHON.read_text()
        assert "aarch64" in src
        assert "x86_64" in src
        assert "astral-sh/python-build-standalone" in src


class TestErrorCodes:
    """Verify structured error codes for startup failures (now in Rust)."""

    def test_all_error_codes_defined(self):
        """python.rs must define all expected startup error codes."""
        src = RUST_PYTHON.read_text()
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
        """Startup errors must carry friendly + hint fields (StartupError struct)."""
        src = RUST_PYTHON.read_text()
        # The StartupError struct + set_startup_error helper supply friendly+hint
        # for every code; require at least one call per error code.
        assert "pub friendly: String" in src
        assert "pub hint: String" in src
        assert src.count("set_startup_error(") >= 5


class TestDeviceID:
    """Verify anonymous device ID in bug reports (now in Rust)."""

    def test_device_id_in_auto_reports(self):
        """commands/mod.rs must derive a device ID from IOPlatformUUID via SHA-256."""
        src = RUST_COMMANDS.read_text()
        assert "device_id" in src
        assert "IOPlatformUUID" in src
        # SHA-256 is computed via the shasum(1) helper
        assert "shasum" in src


class TestAutoReporting:
    """Verify critical endpoints are auto-reported (now in tauri-bridge.js)."""

    def test_critical_endpoints_reported(self):
        """tauri-bridge.js must auto-report errors from critical endpoints."""
        src = BRIDGE_JS.read_text()
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

    def test_abort_errors_are_filtered(self):
        """Rust report_error must short-circuit on legitimate aborts.

        Replaces the old Electron-era 15s startup grace period — instead of
        timing-based suppression, the Tauri build filters aborts by message
        pattern (see is_abort_error) and skips reports when backend_health
        is 'down'.

        Asserts both the definition AND a call site exist, so the test
        catches a regression that removes the gating call but leaves the
        helper function in place.
        """
        src = RUST_COMMANDS.read_text()
        # Definition + at least one caller (currently in report_error)
        assert src.count("is_abort_error(") >= 2, (
            "is_abort_error must be both defined and called by report_error"
        )
        # backend_health() accessor must be checked inside report_error
        assert "backend_health().lock()" in src

    def test_traceback_included_in_reports(self):
        """tauri-bridge.js must include Python traceback in error reports."""
        src = BRIDGE_JS.read_text()
        assert "data.traceback" in src

    def test_device_sn_included_in_reports(self):
        """tauri-bridge.js must include device serial number in connect error reports."""
        src = BRIDGE_JS.read_text()
        assert "device_sn" in src


class TestFlaskErrorResponses:
    """Verify Flask routes return tracebacks for debugging."""

    def test_device_routes_have_traceback(self):
        """device.py error responses must include traceback."""
        src = (ROOT / "app" / "routes" / "device.py").read_text()
        assert "_error_json" in src
        assert "traceback" in src

    def test_cloud_routes_have_traceback(self):
        """cloud.py error responses must include traceback."""
        src = (ROOT / "app" / "routes" / "cloud.py").read_text()
        assert "_error_json" in src

    def test_transcribe_routes_have_traceback(self):
        """transcribe.py error responses must include traceback."""
        src = (ROOT / "app" / "routes" / "transcribe.py").read_text()
        assert "_error_json" in src


class TestWifiRecovery:
    """_recover_wifi uses the detected Wi-Fi interface and never runs on import."""

    def _run(self, ssid_line, iface="en1"):
        from unittest.mock import MagicMock, patch

        from app import api_server

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(stdout=ssid_line, returncode=0)

        with (
            patch("app.utils.wifi_sync_worker.get_wifi_interface", return_value=iface),
            patch("subprocess.run", side_effect=fake_run),
            patch("time.sleep"),
        ):
            api_server._recover_wifi()
        return calls

    def test_power_cycles_detected_interface(self):
        calls = self._run("Current Wi-Fi Network: PLAUD_NOTEPIN_1234\n", iface="en1")
        assert calls == [
            ["networksetup", "-getairportnetwork", "en1"],
            ["networksetup", "-setairportpower", "en1", "off"],
            ["networksetup", "-setairportpower", "en1", "on"],
        ]

    def test_leaves_normal_network_alone(self):
        calls = self._run("Current Wi-Fi Network: Home\n", iface="en0")
        assert calls == [["networksetup", "-getairportnetwork", "en0"]]

    def test_no_hardcoded_en0(self):
        src = (ROOT / "app" / "api_server.py").read_text()
        assert '"en0"' not in src

    def test_startup_helpers_only_run_from_main(self):
        """Importing api_server (tests, REPL) must not run networksetup or delete
        partial files: both helpers are called only under __main__."""
        import ast

        tree = ast.parse((ROOT / "app" / "api_server.py").read_text())
        top_level_calls = [
            node.value.func.id
            for node in tree.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
        ]
        assert "_recover_wifi" not in top_level_calls
        assert "_clean_partial_files" not in top_level_calls
        main = next(n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
        main_src = ast.unparse(main)
        assert "_recover_wifi()" in main_src
        assert "_clean_partial_files()" in main_src
