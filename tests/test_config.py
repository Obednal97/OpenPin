"""Tests for config load/save."""


def test_load_config_creates_default(tmp_config):
    """No config file → returns empty dict."""
    from app.utils.config import load_config

    assert load_config() == {}


def test_save_and_load_roundtrip(tmp_config):
    """save dict, load it back, matches."""
    from app.utils.config import load_config, save_config

    data = {"whisper_model": "small", "diarize": True, "language": "en"}
    save_config(data)
    loaded = load_config()
    assert loaded == data


def test_deleted_ids_tracking(tmp_config):
    """Delete recording, verify ID in deleted_ids."""
    from app.utils.config import load_config, save_config

    config = load_config()
    dismissed = set(config.get("deleted_ids", []))
    dismissed.add("test_session_123")
    config["deleted_ids"] = list(dismissed)
    save_config(config)

    loaded = load_config()
    assert "test_session_123" in loaded["deleted_ids"]


def test_uploaded_session_ids_tracking(tmp_config):
    """Upload, verify session ID tracked."""
    from app.utils.config import load_config, save_config

    config = load_config()
    uploaded = config.get("uploaded_session_ids", [])
    uploaded.append("sess_456")
    config["uploaded_session_ids"] = uploaded
    save_config(config)

    loaded = load_config()
    assert "sess_456" in loaded["uploaded_session_ids"]


def test_config_preserves_nested_data(tmp_config):
    """Config with nested dicts and lists round-trips correctly."""
    from app.utils.config import load_config, save_config

    data = {
        "profile": {"job": "Engineer", "industry": "Tech"},
        "custom_vocabulary": ["Plaud", "NotePin"],
        "webhooks": [{"url": "https://example.com", "triggers": {"transcribe": True}}],
    }
    save_config(data)
    loaded = load_config()
    assert loaded["profile"]["job"] == "Engineer"
    assert loaded["custom_vocabulary"] == ["Plaud", "NotePin"]
    assert loaded["webhooks"][0]["triggers"]["transcribe"] is True


def test_load_corrupt_config(tmp_config):
    """Corrupt JSON → returns empty dict."""
    tmp_config.write_text("not valid json {{{")
    from app.utils.config import load_config

    assert load_config() == {}
