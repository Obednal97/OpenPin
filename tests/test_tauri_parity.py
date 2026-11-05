"""Integration tests for Tauri migration parity.

Verifies:
- Flask API endpoints return valid JSON with expected fields
- Config persistence round-trip through API
- Auto-tagging case insensitivity
- Tauri bridge API surface matches Flask routes
- Settings save/load cycle
- Recording tag/rename operations
- All route blueprints register correctly
- Transcript CRUD operations
- Audio file serving
- Sidecar management code paths
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _invoked_commands(js_src: str) -> set[str]:
    """Command names passed as a string literal to invoke(...) / invokeRust(...)."""
    return set(re.findall(r"""\binvoke(?:Rust)?\(\s*['"]([a-z_][a-z0-9_]*)['"]""", js_src))


def _handler_block() -> str:
    """The body of lib.rs's tauri::generate_handler![...]."""
    lib = (PROJECT_ROOT / "src-tauri" / "src" / "lib.rs").read_text()
    m = re.search(r"\.invoke_handler\(\s*tauri::generate_handler!\[(.*?)\]\s*\)", lib, re.S)
    assert m, "lib.rs has no invoke_handler(tauri::generate_handler![...])"
    return m.group(1)


def _registered_commands() -> set[str]:
    """Command names registered in lib.rs (last path segment of each entry)."""
    entries = [e.strip() for e in _handler_block().split(",")]
    return {e.split("::")[-1] for e in entries if e and not e.startswith("//")}


# ---------------------------------------------------------------------------
# Blueprint registration
# ---------------------------------------------------------------------------


class TestBlueprintRegistration:
    """All route blueprints must be registered on the Flask app."""

    EXPECTED_BLUEPRINTS = {"core", "device", "cloud", "notion", "recordings", "transcribe", "settings", "diagnostics"}

    def test_all_blueprints_registered(self, flask_client):
        """Every expected blueprint is present in app.blueprints."""
        from app.api_server import app

        registered = set(app.blueprints.keys())
        for bp_name in self.EXPECTED_BLUEPRINTS:
            assert bp_name in registered, f"Blueprint '{bp_name}' not registered"

    def test_no_duplicate_routes(self, flask_client):
        """No two rules map to the same endpoint path + method combo."""
        from app.api_server import app

        seen = {}
        for rule in app.url_map.iter_rules():
            for method in rule.methods - {"OPTIONS", "HEAD"}:
                key = (rule.rule, method)
                assert key not in seen, f"Duplicate route: {method} {rule.rule}"
                seen[key] = rule.endpoint


# ---------------------------------------------------------------------------
# Tauri bridge API surface
# ---------------------------------------------------------------------------


class TestTauriBridgeAPISurface:
    """tauri-bridge.js must expose every Flask endpoint the frontend needs."""

    @pytest.fixture(autouse=True)
    def _load_bridge(self):
        self.bridge_src = (PROJECT_ROOT / "app" / "tauri-bridge.js").read_text()

    # --- HTTP endpoints the bridge must call ---

    REQUIRED_ENDPOINTS = [
        # Device
        "/device/scan",
        "/device/scan-known",
        "/device/scan-all",
        "/device/connect",
        "/device/disconnect",
        "/device/depair",
        "/device/status",
        "/device/rssi",
        "/device/info",
        "/device/find-my",
        "/device/files",
        "/device/check-new",
        "/device/sync",
        "/device/abort-sync",
        "/device/wifi-sync",
        "/device/wifi-sync-status",
        # Transcription
        "/transcribe",
        "/summarize",
        # Data
        "/transcripts",
        "/recordings",
        "/recordings/delete",
        "/recordings/restore-redownload",
        "/recordings/tag",
        "/recordings/rename",
        "/recordings/export",
        "/recordings/crop",
        "/transcripts/update",
        "/transcripts/delete",
        # Settings
        "/settings",
        "/validate-key",
        # Cloud
        "/cloud/status",
        "/cloud/login-url",
        "/cloud/exchange-token",
        "/cloud/login-token",
        "/cloud/logout",
        "/cloud/recordings",
        "/cloud/sync",
        "/cloud/devices",
        "/cloud/bind-device",
        "/cloud/sn-sign",
        "/cloud/trash",
        # Health
        "/health",
        "/diagnostics",
        # Audio
        "/audio/",
        # Notion
        "/notion/status",
        "/notion/connect",
        "/notion/disconnect",
        "/notion/databases",
        "/notion/set-database",
        "/notion/set-database-url",
        "/notion/push",
        "/notion/page-url/",
    ]

    def test_bridge_references_all_endpoints(self):
        """Every required HTTP endpoint appears in tauri-bridge.js."""
        for ep in self.REQUIRED_ENDPOINTS:
            assert ep in self.bridge_src, f"tauri-bridge.js missing endpoint: {ep}"

    # --- Tauri invoke() calls the bridge must make ---

    REQUIRED_INVOKES = [
        "get_api_info",
        "report_error",
        "cycle_theme",
        "get_theme",
        "get_login_item",
        "set_login_item",
        "get_dock_visible",
        "set_dock_visible",
        "open_external",
        "show_notification",
        "install_update",
        "check_for_updates",
        "get_app_version",
        "export_bug_report",
        "reveal_in_finder",
    ]

    def test_bridge_invokes_all_ipc_commands(self):
        """Every required command is actually invoke()d by tauri-bridge.js (not
        just mentioned) and registered in lib.rs's invoke_handler."""
        invoked = _invoked_commands(self.bridge_src)
        registered = _registered_commands()
        for cmd in self.REQUIRED_INVOKES:
            assert cmd in invoked, f"tauri-bridge.js never calls invoke('{cmd}')"
            assert cmd in registered, f"{cmd} not in lib.rs invoke_handler"

    def test_every_invoked_command_is_registered(self):
        """An invoke() of a command missing from generate_handler! fails at
        runtime only; catch it here for the bridge and the startup gate."""
        registered = _registered_commands()
        for js in ("tauri-bridge.js", "ui/health-gate.js"):
            src = (PROJECT_ROOT / "app" / js).read_text()
            for cmd in _invoked_commands(src) - registered:
                pytest.fail(f"{js} invokes '{cmd}', which lib.rs doesn't register")

    def test_startup_gate_uses_the_startup_commands(self):
        """The splash (health-gate.js) polls get_startup_status and calls
        retry_startup; both are #[tauri::command]s in lifecycle.rs and
        registered in lib.rs."""
        gate = (PROJECT_ROOT / "app" / "ui" / "health-gate.js").read_text()
        invoked = _invoked_commands(gate)
        registered = _registered_commands()
        lifecycle = (PROJECT_ROOT / "src-tauri" / "src" / "lifecycle.rs").read_text()
        for cmd in ("get_startup_status", "retry_startup"):
            assert cmd in invoked, f"health-gate.js never invokes {cmd}"
            assert cmd in registered, f"{cmd} not registered in lib.rs invoke_handler"
            assert re.search(rf"#\[tauri::command\]\s*pub fn {cmd}\(", lifecycle), f"{cmd} isn't a command"
        assert "lifecycle::get_startup_status" in _handler_block()
        assert "lifecycle::retry_startup" in _handler_block()

    # --- window.plaud.* methods ---

    REQUIRED_PLAUD_METHODS = [
        "scan",
        "scanKnown",
        "connect",
        "disconnect",
        "status",
        "transcribe",
        "transcripts",
        "recordings",
        "deleteRecordings",
        "restoreRedownload",
        "tagRecording",
        "renameRecording",
        "exportRecording",
        "cropRecording",
        "updateTranscript",
        "summarize",
        "getSettings",
        "saveSettings",
        "validateKey",
        "health",
        "audioUrl",
        "cycleTheme",
        "getTheme",
        "openExternal",
        "showNotification",
        "installUpdate",
        "checkForUpdates",
        "getAppVersion",
        "reportError",
        "exportBugReport",
        "revealInFinder",
        "getDiagnostics",
        "notionStatus",
        "notionConnect",
        "notionDisconnect",
        "notionDatabases",
        "notionSetDatabase",
        "notionSetDatabaseUrl",
        "notionPush",
        "notionPageUrl",
        "cloudStatus",
        "cloudLoginUrl",
        "cloudExchangeToken",
        "cloudLoginToken",
        "cloudLogout",
        "cloudRecordings",
        "cloudSync",
        "cloudDevices",
        "cloudBindDevice",
        "cloudSnSign",
        "cloudTrash",
    ]

    def test_bridge_exposes_all_plaud_methods(self):
        """Every required method is defined on window.plaud."""
        for method in self.REQUIRED_PLAUD_METHODS:
            # Match `methodName:` or `methodName =` inside window.plaud = { ... }
            assert re.search(rf"\b{method}\b\s*[:=(]", self.bridge_src), (
                f"window.plaud.{method} not found in tauri-bridge.js"
            )

    # --- Event listeners ---

    REQUIRED_EVENTS = [
        "startup-error",
        "startup-status",
        "oauth-callback",
        "system-wake",
        "notification-action",
        "update-status",
    ]

    def test_bridge_listens_for_all_events(self):
        """All expected Tauri event names are registered."""
        for event in self.REQUIRED_EVENTS:
            assert event in self.bridge_src, f"tauri-bridge.js missing event listener: {event}"

    def test_bridge_has_ble_disconnect_on_unload(self):
        """Bridge disconnects BLE on page unload to prevent stale connections."""
        assert "beforeunload" in self.bridge_src
        assert "/device/disconnect" in self.bridge_src

    def test_bridge_does_not_report_environmental_errors(self):
        """Issues #92 and #94, and 'Fetch is aborted': none of these are bugs."""
        for marker in ("'turned off'", "'powered_off'", "'not connected'"):
            assert marker in self.bridge_src
        assert "err.name === 'AbortError'" in self.bridge_src
        assert "REPORT_ENDPOINTS.has(endpoint) && !aborted" in self.bridge_src

    def test_bridge_report_endpoints_set(self):
        """REPORT_ENDPOINTS set should include critical operations."""
        critical = ["/device/connect", "/transcribe", "/cloud/sync"]
        for ep in critical:
            assert ep in self.bridge_src

    def test_long_coaching_calls_never_time_out(self):
        """Installing the speaker model and a first voice setup can both run for minutes."""
        block = self.bridge_src[self.bridge_src.index("const NO_TIMEOUT_ENDPOINTS") :]
        block = block[: block.index("]);")]
        for ep in ("'/coaching/voice/install'", "'/coaching/voice/setup'"):
            assert ep in block


# ---------------------------------------------------------------------------
# Flask endpoint JSON shapes
# ---------------------------------------------------------------------------


class TestEndpointJSONShapes:
    """Verify each endpoint returns JSON with the expected top-level fields."""

    def test_health_shape(self, flask_client):
        data = flask_client.get("/health").get_json()
        assert "status" in data
        assert "version" in data

    def test_settings_get_shape(self, flask_client):
        data = flask_client.get("/settings").get_json()
        required = {
            "model",
            "diarize",
            "language",
            "english_variant",
            "models",
            "ram_total_gb",
            "ram_used_gb",
            "ram_headroom_gb",
            "available_tags",
            "profile",
            "custom_vocabulary",
            "webhooks",
            "cloud_sync_direction",
            "cloud_sync_interval",
            "auto_delete_after_sync",
            "auto_sync",
            "auto_transcribe",
            "auto_summarise",
            "notion_auto_push",
            "auto_prefer_wifi",
            "transcription_provider",
            "hf_token_set",
            "summarization_provider",
            "has_paired_devices",
            "setup_dismissed",
        }
        for field in required:
            assert field in data, f"GET /settings missing field: {field}"

    def test_settings_get_key_flags_are_booleans(self, flask_client):
        """API key flags should be booleans, not the actual key values."""
        data = flask_client.get("/settings").get_json()
        for key_flag in [
            "anthropic_key_set",
            "openai_key_set",
            "openrouter_key_set",
            "deepgram_key_set",
            "groq_key_set",
        ]:
            assert key_flag in data, f"Missing key flag: {key_flag}"
            assert isinstance(data[key_flag], bool), f"{key_flag} should be bool"

    def test_settings_post_shape(self, flask_client):
        r = flask_client.post("/settings", json={"language": "de"}, content_type="application/json")
        data = r.get_json()
        assert data["saved"] is True

    def test_recordings_list_shape(self, flask_client):
        data = flask_client.get("/recordings").get_json()
        assert isinstance(data, list)

    def test_transcripts_list_shape(self, flask_client):
        data = flask_client.get("/transcripts").get_json()
        assert isinstance(data, list)

    def test_tag_recording_shape(self, flask_client):
        r = flask_client.post(
            "/recordings/tag",
            json={"session_id": "test_123", "tags": ["Meeting"]},
            content_type="application/json",
        )
        data = r.get_json()
        assert data["saved"] is True

    def test_rename_recording_shape(self, flask_client):
        r = flask_client.post(
            "/recordings/rename",
            json={"session_id": "test_123", "title": "My Meeting"},
            content_type="application/json",
        )
        data = r.get_json()
        assert data["saved"] is True

    def test_delete_recordings_shape(self, flask_client):
        r = flask_client.post(
            "/recordings/delete",
            json={"session_ids": ["fake_id_999"]},
            content_type="application/json",
        )
        data = r.get_json()
        assert "deleted" in data
        assert "count" in data

    def test_delete_transcript_shape(self, flask_client):
        r = flask_client.post(
            "/transcripts/delete",
            json={"session_id": "fake_id_999"},
            content_type="application/json",
        )
        data = r.get_json()
        assert "deleted" in data

    def test_transcribe_missing_file(self, flask_client):
        r = flask_client.post(
            "/transcribe",
            json={"path": "/nonexistent/audio.ogg"},
            content_type="application/json",
        )
        assert r.status_code == 400
        data = r.get_json()
        assert "error" in data

    def test_summarize_missing_transcript(self, flask_client):
        r = flask_client.post(
            "/summarize",
            json={"session_id": "nonexistent_12345"},
            content_type="application/json",
        )
        assert r.status_code == 404
        data = r.get_json()
        assert "error" in data

    def test_validate_key_shape(self, flask_client):
        r = flask_client.post(
            "/validate-key",
            json={"provider": "openai", "key": ""},
            content_type="application/json",
        )
        data = r.get_json()
        assert "valid" in data
        assert isinstance(data["valid"], bool)


# ---------------------------------------------------------------------------
# Settings save/load round-trip (full cycle through API)
# ---------------------------------------------------------------------------


class TestSettingsRoundTrip:
    """POST settings then GET them back — every supported field."""

    def test_language_roundtrip(self, flask_client):
        flask_client.post("/settings", json={"language": "ja"}, content_type="application/json")
        data = flask_client.get("/settings").get_json()
        assert data["language"] == "ja"

    def test_model_roundtrip(self, flask_client):
        flask_client.post("/settings", json={"model": "large"}, content_type="application/json")
        data = flask_client.get("/settings").get_json()
        assert data["model"] == "large"

    def test_diarize_roundtrip(self, flask_client):
        flask_client.post("/settings", json={"diarize": True}, content_type="application/json")
        data = flask_client.get("/settings").get_json()
        assert data["diarize"] is True

    def test_english_variant_roundtrip(self, flask_client):
        flask_client.post("/settings", json={"english_variant": "gb"}, content_type="application/json")
        data = flask_client.get("/settings").get_json()
        assert data["english_variant"] == "gb"

    def test_profile_roundtrip(self, flask_client):
        profile = {"name": "Alice", "job": "Engineer", "industry": "Tech"}
        flask_client.post("/settings", json={"profile": profile}, content_type="application/json")
        data = flask_client.get("/settings").get_json()
        assert data["profile"]["name"] == "Alice"
        assert data["profile"]["job"] == "Engineer"

    def test_custom_vocabulary_roundtrip(self, flask_client):
        vocab = ["Plaud", "NotePin", "diarization"]
        flask_client.post("/settings", json={"custom_vocabulary": vocab}, content_type="application/json")
        data = flask_client.get("/settings").get_json()
        assert data["custom_vocabulary"] == vocab

    def test_webhooks_roundtrip(self, flask_client):
        hooks = [{"url": "https://example.com/hook", "triggers": {"transcribe": True}}]
        flask_client.post("/settings", json={"webhooks": hooks}, content_type="application/json")
        data = flask_client.get("/settings").get_json()
        assert len(data["webhooks"]) == 1
        assert data["webhooks"][0]["url"] == "https://example.com/hook"

    def test_custom_tags_roundtrip(self, flask_client):
        tags = ["Standup", "Brainstorm"]
        flask_client.post("/settings", json={"custom_tags": tags}, content_type="application/json")
        data = flask_client.get("/settings").get_json()
        # Custom tags are merged into available_tags alongside DEFAULT_TAGS
        assert "Standup" in data["available_tags"]
        assert "Brainstorm" in data["available_tags"]

    def test_provider_preferences_roundtrip(self, flask_client):
        flask_client.post(
            "/settings",
            json={
                "transcription_provider": "groq",
                "summarization_provider": "openai",
            },
            content_type="application/json",
        )
        data = flask_client.get("/settings").get_json()
        assert data["transcription_provider"] == "groq"
        assert data["summarization_provider"] == "openai"

    def test_auto_flags_roundtrip(self, flask_client):
        flask_client.post(
            "/settings",
            json={
                "auto_sync": False,
                "auto_transcribe": False,
                "auto_summarise": True,
                "auto_prefer_wifi": True,
                "notion_auto_push": True,
            },
            content_type="application/json",
        )
        data = flask_client.get("/settings").get_json()
        assert data["auto_sync"] is False
        assert data["auto_transcribe"] is False
        assert data["auto_summarise"] is True
        assert data["auto_prefer_wifi"] is True
        assert data["notion_auto_push"] is True

    def test_retired_crash_reports_setting_is_ignored(self, flask_client, tmp_path):
        """The app no longer files issues, so the old toggle is gone: not read, not saved."""
        config_path = tmp_path / ".plaud_v2_config.json"
        config_path.write_text(json.dumps({"crash_reports_enabled": False}))
        assert "crash_reports_enabled" not in flask_client.get("/settings").get_json()
        flask_client.post("/settings", json={"crash_reports_enabled": True}, content_type="application/json")
        assert json.loads(config_path.read_text())["crash_reports_enabled"] is False
        assert "crash_reports_enabled" not in flask_client.get("/settings").get_json()

    def test_setup_dismissed_roundtrip(self, flask_client):
        flask_client.post("/settings", json={"setup_dismissed": True}, content_type="application/json")
        data = flask_client.get("/settings").get_json()
        assert data["setup_dismissed"] is True

    def test_api_key_save_and_remove(self, flask_client):
        """Save an API key, verify flag is True, remove it, verify False."""
        flask_client.post("/settings", json={"deepgram_api_key": "dg-test-key-12345"}, content_type="application/json")
        data = flask_client.get("/settings").get_json()
        assert data["deepgram_key_set"] is True

        flask_client.post("/settings", json={"remove_deepgram_key": True}, content_type="application/json")
        data = flask_client.get("/settings").get_json()
        assert data["deepgram_key_set"] is False

    def test_multiple_settings_in_one_post(self, flask_client):
        """Multiple settings in a single POST all take effect."""
        flask_client.post(
            "/settings",
            json={"language": "fr", "diarize": True, "model": "medium", "english_variant": "gb"},
            content_type="application/json",
        )
        data = flask_client.get("/settings").get_json()
        assert data["language"] == "fr"
        assert data["diarize"] is True
        assert data["model"] == "medium"
        assert data["english_variant"] == "gb"


# ---------------------------------------------------------------------------
# Recording tag & rename operations
# ---------------------------------------------------------------------------


class TestRecordingTagRename:
    """Tag and rename operations persist through the config."""

    def test_tag_recording_persists(self, flask_client):
        flask_client.post(
            "/recordings/tag",
            json={"session_id": "sess_abc", "tags": ["Meeting", "Important"]},
            content_type="application/json",
        )
        # Tags should appear in the recordings list for that session
        # (no actual OGG file, so we verify via a second tag call)
        flask_client.post(
            "/recordings/tag",
            json={"session_id": "sess_abc", "tags": ["Meeting", "Important", "Follow-up"]},
            content_type="application/json",
        )
        r = flask_client.post(
            "/recordings/tag",
            json={"session_id": "sess_abc", "tags": ["Journal"]},
            content_type="application/json",
        )
        assert r.get_json()["saved"] is True

    def test_rename_recording_persists(self, flask_client):
        flask_client.post(
            "/recordings/rename",
            json={"session_id": "sess_abc", "title": "Weekly Standup"},
            content_type="application/json",
        )
        # Rename again
        flask_client.post(
            "/recordings/rename",
            json={"session_id": "sess_abc", "title": "Renamed Meeting"},
            content_type="application/json",
        )
        r = flask_client.post(
            "/recordings/rename",
            json={"session_id": "sess_abc", "title": ""},
            content_type="application/json",
        )
        assert r.get_json()["saved"] is True

    def test_tag_no_session_returns_400(self, flask_client):
        r = flask_client.post(
            "/recordings/tag",
            json={"tags": ["Test"]},
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_rename_no_session_returns_400(self, flask_client):
        r = flask_client.post(
            "/recordings/rename",
            json={"title": "Test"},
            content_type="application/json",
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Transcript update endpoint
# ---------------------------------------------------------------------------


class TestTranscriptUpdate:
    """POST /transcripts/update must update segments and summary."""

    @pytest.fixture
    def _seed_transcript(self, flask_client, tmp_path):
        """Create a transcript JSON file so we can update it."""
        # We need to patch TRANSCRIPTS_DIR to our tmp_path
        transcripts_dir = tmp_path / "transcripts"
        transcripts_dir.mkdir(exist_ok=True)
        t_data = {
            "text": "Hello world",
            "duration": 5.0,
            "language": "en",
            "segments": [{"start": 0, "end": 5, "text": "Hello world", "speaker": "A"}],
            "summary": "",
        }
        (transcripts_dir / "update_test.json").write_text(json.dumps(t_data))
        (transcripts_dir / "update_test.txt").write_text("Hello world")
        with patch("app.routes.recordings.TRANSCRIPTS_DIR", transcripts_dir):
            yield transcripts_dir

    def test_update_segments(self, flask_client, _seed_transcript):
        new_segments = [
            {"start": 0, "end": 3, "text": "Hello", "speaker": "A"},
            {"start": 3, "end": 5, "text": "world", "speaker": "B"},
        ]
        r = flask_client.post(
            "/transcripts/update",
            json={"session_id": "update_test", "segments": new_segments},
            content_type="application/json",
        )
        data = r.get_json()
        assert data["saved"] is True

        # Verify the file was updated
        updated = json.loads((_seed_transcript / "update_test.json").read_text())
        assert len(updated["segments"]) == 2
        assert updated["text"] == "Hello world"

    def test_update_summary(self, flask_client, _seed_transcript):
        r = flask_client.post(
            "/transcripts/update",
            json={"session_id": "update_test", "summary": "A brief greeting."},
            content_type="application/json",
        )
        assert r.get_json()["saved"] is True
        updated = json.loads((_seed_transcript / "update_test.json").read_text())
        assert updated["summary"] == "A brief greeting."

    def test_update_missing_transcript_returns_404(self, flask_client):
        r = flask_client.post(
            "/transcripts/update",
            json={"session_id": "nonexistent_xyz"},
            content_type="application/json",
        )
        assert r.status_code == 404

    def test_update_no_session_returns_400(self, flask_client):
        r = flask_client.post(
            "/transcripts/update",
            json={},
            content_type="application/json",
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Auto-tagging case insensitivity
# ---------------------------------------------------------------------------


class TestAutoTaggingCaseInsensitivity:
    """_extract_spoken_tags and _apply_spoken_tags handle case correctly."""

    def test_extract_tag_basic(self):
        from app.routes.transcribe import _extract_spoken_tags

        tags = _extract_spoken_tags("Tag: meeting notes")
        assert len(tags) >= 1
        # Should be title-cased
        assert tags[0] == "Meeting Notes"

    def test_extract_tag_case_insensitive_trigger(self):
        """Trigger word casing should not matter."""
        from app.routes.transcribe import _extract_spoken_tags

        for prefix in ["TAG:", "Tag:", "tag:", "TAG :", "tAg:"]:
            tags = _extract_spoken_tags(f"{prefix} standup")
            assert len(tags) >= 1, f"Failed to extract tag from '{prefix} standup'"
            assert tags[0].lower() == "standup"

    def test_extract_add_tag_variant(self):
        from app.routes.transcribe import _extract_spoken_tags

        tags = _extract_spoken_tags("Add tag important review")
        assert len(tags) >= 1

    def test_extract_add_the_tags_variant(self):
        from app.routes.transcribe import _extract_spoken_tags

        tags = _extract_spoken_tags("Add the tags meeting, important")
        assert len(tags) >= 2

    def test_extract_multiple_tags_comma_separated(self):
        from app.routes.transcribe import _extract_spoken_tags

        tags = _extract_spoken_tags("Tags: meeting, important, follow-up")
        assert len(tags) == 3

    def test_extract_tags_with_and(self):
        from app.routes.transcribe import _extract_spoken_tags

        tags = _extract_spoken_tags("Tags: meeting and important")
        assert len(tags) == 2

    def test_extract_no_tags(self):
        from app.routes.transcribe import _extract_spoken_tags

        tags = _extract_spoken_tags("This is a normal conversation about meetings.")
        assert tags == []

    def test_extract_empty_text(self):
        from app.routes.transcribe import _extract_spoken_tags

        assert _extract_spoken_tags("") == []
        assert _extract_spoken_tags(None) == []

    def test_extract_tags_title_cased(self):
        """All extracted tags should be title-cased."""
        from app.routes.transcribe import _extract_spoken_tags

        tags = _extract_spoken_tags("Tag: WEEKLY STANDUP")
        assert len(tags) >= 1
        assert tags[0] == "Weekly Standup"

    def test_extract_tag_too_long_ignored(self):
        """Tags longer than 50 chars are ignored."""
        from app.routes.transcribe import _extract_spoken_tags

        long_tag = "a" * 60
        tags = _extract_spoken_tags(f"Tag: {long_tag}")
        assert all(len(t) < 50 for t in tags)

    def test_apply_spoken_tags_dedup_case_insensitive(self, tmp_config):
        """_apply_spoken_tags does not duplicate tags that differ only in case."""
        from app.routes.transcribe import _apply_spoken_tags
        from app.utils.config import load_config, save_config

        # Pre-seed config with an existing tag
        config = load_config()
        config["recording_tags"] = {"sess_1": ["Meeting"]}
        config["custom_tags"] = ["Meeting"]
        save_config(config)

        # Apply a spoken tag that matches case-insensitively
        with patch("app.routes.transcribe._extract_spoken_tags", return_value=["meeting"]):
            _apply_spoken_tags("sess_1", "Tag: meeting")

        config = load_config()
        session_tags = config["recording_tags"]["sess_1"]
        # "meeting" should NOT be added since "Meeting" already exists
        assert session_tags.count("Meeting") == 1
        lower_tags = [t.lower() for t in session_tags]
        assert lower_tags.count("meeting") == 1

    def test_apply_spoken_tags_adds_to_custom_tags(self, tmp_config):
        """New spoken tags are added to the custom_tags list."""
        from app.routes.transcribe import _apply_spoken_tags
        from app.utils.config import load_config, save_config

        config = load_config()
        config["recording_tags"] = {}
        config["custom_tags"] = []
        save_config(config)

        with patch(
            "app.routes.transcribe._extract_spoken_tags",
            return_value=["New Category"],
        ):
            _apply_spoken_tags("sess_new", "Tag: New Category")

        config = load_config()
        assert "New Category" in config["custom_tags"]

    def test_apply_spoken_tags_custom_tags_case_insensitive(self, tmp_config):
        """Custom tags list does not get case-insensitive duplicates."""
        from app.routes.transcribe import _apply_spoken_tags
        from app.utils.config import load_config, save_config

        config = load_config()
        config["recording_tags"] = {}
        config["custom_tags"] = ["Standup"]
        save_config(config)

        with patch(
            "app.routes.transcribe._extract_spoken_tags",
            return_value=["standup"],
        ):
            _apply_spoken_tags("sess_2", "Tag: standup")

        config = load_config()
        lower_custom = [t.lower() for t in config["custom_tags"]]
        assert lower_custom.count("standup") == 1


# ---------------------------------------------------------------------------
# Audio file serving
# ---------------------------------------------------------------------------


class TestAudioServing:
    """GET /audio/<filename> serves OGG files."""

    def test_serve_existing_audio(self, flask_client, tmp_path, sample_ogg):
        ogg_data, _ = sample_ogg
        recordings_dir = tmp_path / "recordings"
        recordings_dir.mkdir(exist_ok=True)
        audio_file = recordings_dir / "test_audio.ogg"
        audio_file.write_bytes(ogg_data)

        with patch("app.routes.core.get_all_recordings_dirs", return_value=[recordings_dir]):
            # Wildcard accept header — browser default
            r = flask_client.get("/audio/test_audio.ogg", headers={"Accept": "*/*"})
            assert r.status_code == 200
            assert r.content_type in ("audio/ogg", "audio/mp4")
            assert len(r.data) > 0

    def test_serve_missing_audio_returns_404(self, flask_client):
        r = flask_client.get("/audio/nonexistent_file.ogg")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Config persistence round-trip (direct config module)
# ---------------------------------------------------------------------------


class TestConfigPersistenceRoundTrip:
    """Verify config survives save-load-modify-save-load cycles."""

    def test_incremental_updates_preserve_existing(self, tmp_config):
        """Saving new keys does not wipe existing ones."""
        from app.utils.config import load_config, save_config

        save_config({"whisper_model": "small", "language": "en"})
        config = load_config()
        config["diarize"] = True
        save_config(config)

        final = load_config()
        assert final["whisper_model"] == "small"
        assert final["language"] == "en"
        assert final["diarize"] is True

    def test_nested_dict_update(self, tmp_config):
        from app.utils.config import load_config, save_config

        save_config({"profile": {"name": "Alice"}})
        config = load_config()
        config["profile"]["job"] = "Engineer"
        save_config(config)

        final = load_config()
        assert final["profile"]["name"] == "Alice"
        assert final["profile"]["job"] == "Engineer"

    def test_recording_tags_multiple_sessions(self, tmp_config):
        from app.utils.config import load_config, save_config

        config = load_config()
        config["recording_tags"] = {
            "sess_1": ["Meeting"],
            "sess_2": ["Journal", "Personal"],
        }
        save_config(config)

        config = load_config()
        config["recording_tags"]["sess_3"] = ["Lecture"]
        save_config(config)

        final = load_config()
        assert final["recording_tags"]["sess_1"] == ["Meeting"]
        assert final["recording_tags"]["sess_2"] == ["Journal", "Personal"]
        assert final["recording_tags"]["sess_3"] == ["Lecture"]

    def test_api_keys_not_leaked_in_settings_response(self, flask_client):
        """Raw API keys must never appear in GET /settings response."""
        flask_client.post(
            "/settings",
            json={"openai_api_key": "sk-secret-test-key-123"},
            content_type="application/json",
        )
        data = flask_client.get("/settings").get_json()
        response_str = json.dumps(data)
        assert "sk-secret-test-key-123" not in response_str
        assert data["openai_key_set"] is True


# ---------------------------------------------------------------------------
# Python sidecar management
# ---------------------------------------------------------------------------


class TestSidecarManagement:
    """Verify Python detection and dependency management code paths."""

    def test_rust_has_python_finder(self):
        """src-tauri/src/python.rs must have logic to find a Python interpreter."""
        src = (PROJECT_ROOT / "src-tauri" / "src" / "python.rs").read_text()
        assert "python" in src.lower()
        # Must check version
        assert "--version" in src or "sys.version" in src

    def test_rust_installs_deps(self):
        """src-tauri/src/python.rs must pip-install dependencies."""
        src = (PROJECT_ROOT / "src-tauri" / "src" / "python.rs").read_text()
        assert "pip" in src
        assert "install" in src

    def test_runtime_matches_the_lockfile_target(self):
        """python.rs only builds the venv on the Python/arch core.txt is locked
        for (scripts/lock-python-deps.sh), and the pinned standalone build is
        that same minor."""
        src = (PROJECT_ROOT / "src-tauri" / "src" / "python.rs").read_text()
        lock_sh = (PROJECT_ROOT / "scripts" / "lock-python-deps.sh").read_text()
        py = re.search(r"--python-version (\d+)\.(\d+)", lock_sh)
        plat = re.search(r"--python-platform (\w+)-apple-darwin", lock_sh)
        assert py and plat
        assert f"const REQUIRED_PYTHON: (u32, u32) = ({py.group(1)}, {py.group(2)});" in src
        assert f'const SUPPORTED_ARCH: &str = "{plat.group(1)}";' in src
        standalone = re.search(r'const STANDALONE_PY_VERSION: &str = "(\d+)\.(\d+)\.\d+";', src)
        assert standalone and standalone.groups() == py.groups()
        header = (PROJECT_ROOT / "app" / "requirements" / "core.txt").read_text().splitlines()[1]
        assert f"CPython {py.group(1)}.{py.group(2)}, {plat.group(1)}-apple-darwin" in header

    def test_shell_and_backend_agree_on_the_data_dir(self):
        """The shell always passes PLAUD_DATA_DIR, and its release default is
        the backend's packaged folder; debug builds use a separate one."""
        python_rs = (PROJECT_ROOT / "src-tauri" / "src" / "python.rs").read_text()
        paths_rs = (PROJECT_ROOT / "src-tauri" / "src" / "paths.rs").read_text()
        constants = (PROJECT_ROOT / "app" / "utils" / "constants.py").read_text()
        assert ".env(crate::paths::DATA_DIR_ENV, crate::paths::data_dir())" in python_rs
        assert 'pub const DATA_DIR_ENV: &str = "PLAUD_DATA_DIR";' in paths_rs
        release = re.search(r'const RELEASE_DATA_DIR: &str = "([^"]+)";', paths_rs)
        dev = re.search(r'const DEV_DATA_DIR: &str = "([^"]+)";', paths_rs)
        assert release and dev and release.group(1) != dev.group(1)
        assert f'Path("~/Library/Application Support/{release.group(1)}")' in constants

    def test_bundle_ships_every_src_module_the_app_imports(self):
        """tauri.conf.json lists src/ modules one by one; every module the
        backend (or a bundled src/ module) imports must be among them."""
        conf = json.loads((PROJECT_ROOT / "src-tauri" / "tauri.conf.json").read_text())
        bundled = {
            Path(dest).stem
            for dest in conf["bundle"]["resources"].values()
            if dest.startswith("src/") and dest.endswith(".py")
        }
        assert "__init__" in bundled
        pattern = re.compile(r"^\s*(?:from\s+src\.(\w+)\s+import|import\s+src\.(\w+))", re.M)

        def imported(files):
            return {a or b for f in files for a, b in pattern.findall(f.read_text())}

        needed = imported((PROJECT_ROOT / "app").rglob("*.py"))
        needed |= imported(PROJECT_ROOT / "src" / f"{m}.py" for m in needed)
        assert needed, "no src imports found; pattern broken?"
        assert needed <= bundled, f"not bundled: {sorted(needed - bundled)}"
        for m in bundled:
            assert (PROJECT_ROOT / "src" / f"{m}.py").is_file(), m

    def test_tauri_rust_has_sidecar_or_command(self):
        """Tauri config or Rust code must reference Python sidecar."""
        tauri_conf = PROJECT_ROOT / "src-tauri" / "tauri.conf.json"
        if tauri_conf.exists():
            src = tauri_conf.read_text()
            # Tauri may reference sidecar or shell command
            has_sidecar = "sidecar" in src.lower() or "python" in src.lower()
            # Also check Rust source
            rust_files = list((PROJECT_ROOT / "src-tauri" / "src").glob("*.rs"))
            rust_src = ""
            for f in rust_files:
                rust_src += f.read_text()
            has_command = "Command" in rust_src or "python" in rust_src.lower()
            assert has_sidecar or has_command, "Tauri must reference Python sidecar/command"
        else:
            pytest.skip("No tauri.conf.json found — Tauri not yet scaffolded")


# ---------------------------------------------------------------------------
# CORS on all responses
# ---------------------------------------------------------------------------


class TestCORS:
    """Every response to the Tauri webview origin must carry CORS headers for it."""

    ENDPOINTS = ["/health", "/settings", "/recordings", "/transcripts"]

    def test_cors_on_get_endpoints(self, flask_client):
        for ep in self.ENDPOINTS:
            r = flask_client.get(ep, headers={"Origin": "tauri://localhost"})
            origin = r.headers.get("Access-Control-Allow-Origin", "")
            assert origin == "tauri://localhost", f"Missing CORS header on {ep}: got '{origin}'"

    def test_cors_on_post_endpoints(self, flask_client):
        r = flask_client.post(
            "/settings",
            json={"language": "en"},
            content_type="application/json",
            headers={"Origin": "tauri://localhost"},
        )
        assert r.headers.get("Access-Control-Allow-Origin") == "tauri://localhost"


# ---------------------------------------------------------------------------
# Error handling consistency
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Error responses must have an 'error' key."""

    def test_transcribe_error_has_error_key(self, flask_client):
        r = flask_client.post("/transcribe", json={}, content_type="application/json")
        assert "error" in r.get_json()

    def test_summarize_error_has_error_key(self, flask_client):
        r = flask_client.post("/summarize", json={}, content_type="application/json")
        assert "error" in r.get_json()

    def test_tag_error_has_error_key(self, flask_client):
        r = flask_client.post("/recordings/tag", json={}, content_type="application/json")
        assert "error" in r.get_json()

    def test_rename_error_has_error_key(self, flask_client):
        r = flask_client.post("/recordings/rename", json={}, content_type="application/json")
        assert "error" in r.get_json()

    def test_delete_recordings_error_has_error_key(self, flask_client):
        r = flask_client.post("/recordings/delete", json={}, content_type="application/json")
        assert "error" in r.get_json()

    def test_update_transcript_error_has_error_key(self, flask_client):
        r = flask_client.post("/transcripts/update", json={}, content_type="application/json")
        assert "error" in r.get_json()

    def test_export_error_has_error_key(self, flask_client):
        r = flask_client.post("/recordings/export", json={}, content_type="application/json")
        assert "error" in r.get_json()

    def test_crop_no_session_has_error_key(self, flask_client):
        r = flask_client.post("/recordings/crop", json={}, content_type="application/json")
        assert "error" in r.get_json()

    def test_crop_invalid_range_has_error_key(self, flask_client):
        r = flask_client.post(
            "/recordings/crop",
            json={"session_id": "x", "start": 10, "end": 5},
            content_type="application/json",
        )
        assert "error" in r.get_json()


class TestSyncDoesNotWaitForTranscription:
    """v2.5.1: a sync ends when the files are on disk. 47 recordings synced over
    WiFi kept the sync window open, the list empty and 'Syncing' showing for
    hours while each one was transcribed inside the sync."""

    UI = PROJECT_ROOT / "app"

    def _sources(self):
        files = [self.UI / "renderer.js", *sorted((self.UI / "ui").glob("*.js"))]
        return {f.name: f.read_text() for f in files}

    def test_only_the_queue_and_the_transcript_view_transcribe(self):
        callers = {name for name, src in self._sources().items() if "window.plaud.transcribe(" in src}
        assert callers == {"recordings.js", "transcript.js"}

    def test_every_sync_path_uses_the_queue(self):
        src = self._sources()
        assert src["renderer.js"].count("queueAutoProcess(") >= 4  # manual, WiFi, background, reconnect
        assert "queueAutoProcess(" in src["setup.js"]
        assert "queueAutoProcess(" in src["settings.js"]

    def test_queue_honours_auto_transcribe_and_shows_badges(self):
        rec = self._sources()["recordings.js"]
        assert "app.settings.auto_transcribe === false" in rec
        assert "app.processingIds.set(item.sid, 'transcribing')" in rec
        assert "queued: 'Queued'" in rec

    def test_select_bar_can_queue_a_backlog(self):
        src = self._sources()
        html = (self.UI / "index.html").read_text()
        assert 'id="transcribe-selected-btn"' in html
        assert "addEventListener('click', transcribeSelected)" in src["renderer.js"]
        assert "function transcribeSelected()" in src["recordings.js"]


_RANGE_SELECT_JS = r"""
const fs = require('fs'); const vm = require('vm');
const ctx = { console, window: { getSelection: () => ({ removeAllRanges() {} }) } };
ctx.app = { recordings: ['a','b','c','d','e','f'].map((s) => ({ session_id: s, partial: s === 'd' })),
  selectedIds: new Set(), activeTagFilters: new Set(), searchQuery: '', transcripts: {}, selectMode: true };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8') + '\nrenderRecordingsList = () => {};', ctx);
const run = (code) => vm.runInContext(code, ctx);
const out = {};
run("toggleSelection('b')"); out.down = [run("selectRangeTo('f')"), [...ctx.app.selectedIds]];
run("app.selectedIds.clear(); toggleSelection('e')"); out.up = [run("selectRangeTo('a')"), [...ctx.app.selectedIds]];
run("enterSelectMode()"); out.noAnchor = run("selectRangeTo('c')");
console.log(JSON.stringify(out));
"""


class TestShiftClickRangeSelect:
    """Shift-click in select mode selects every recording between the two clicks
    (it used to select only the two clicked and highlight the text between)."""

    def test_range_selection(self):
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        out = subprocess.run(
            [node, "-e", _RANGE_SELECT_JS, str(PROJECT_ROOT / "app" / "ui" / "recordings.js")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        result = json.loads(out)
        assert result["down"] == [True, ["b", "c", "e", "f"]]  # "d" is incomplete: not selectable
        assert result["up"] == [True, ["e", "a", "b", "c"]]
        assert result["noAnchor"] is False

    def test_shift_mousedown_does_not_select_text(self):
        src = (PROJECT_ROOT / "app" / "ui" / "recordings.js").read_text()
        assert "if (app.selectMode && e.shiftKey) e.preventDefault();" in src
