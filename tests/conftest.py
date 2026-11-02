"""Shared test fixtures for Plaud V2."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Token the flask_client fixture sets as PLAUD_API_TOKEN and sends by default.
TEST_API_TOKEN = "test-token-0123456789abcdef"


@pytest.fixture
def tmp_config(tmp_path):
    """Provide a temporary config file and patch config module to use it."""
    config_path = tmp_path / ".plaud_v2_config.json"
    with patch("app.utils.constants.CONFIG_PATH", config_path):
        # Also patch the config module's reference
        import app.utils.config as config_mod

        original = config_mod.CONFIG_PATH
        config_mod.CONFIG_PATH = config_path
        yield config_path
        config_mod.CONFIG_PATH = original


@pytest.fixture
def tmp_dirs(tmp_path):
    """Provide temporary recording/transcript directories."""
    recordings = tmp_path / "recordings"
    transcripts = tmp_path / "transcripts"
    recordings.mkdir()
    transcripts.mkdir()
    return {"recordings": recordings, "transcripts": transcripts, "root": tmp_path}


@pytest.fixture
def flask_client(tmp_path):
    """Create a Flask test client with isolated config and mocked cloud/notion state."""
    # Use a temp config file so tests don't pollute the real config
    test_config = tmp_path / ".plaud_v2_config.json"
    with (
        patch("app.utils.config.CONFIG_PATH", test_config),
        patch("app.state.cloud.load_cloud_token", return_value=False),
        patch(
            "app.state.cloud._cloud_state",
            {
                "logged_in": False,
                "token": None,
                "api_domain": "https://api-euc1.plaud.ai",
                "user": None,
                "desktop_uuid": None,
            },
        ),
        patch("app.state.notion.load"),
        # Enforce the per-launch API token in tests, exactly as in production;
        # the client sends it by default (tests can override/omit per request).
        patch.dict("os.environ", {"PLAUD_API_TOKEN": TEST_API_TOKEN}),
    ):
        from app.api_server import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            client.environ_base["HTTP_X_PLAUD_TOKEN"] = TEST_API_TOKEN
            yield client


@pytest.fixture
def sample_ogg():
    """Create a minimal valid OGG file with Opus frames for testing."""
    from app.utils.audio import wrap_opus_ogg

    # Create 10 fake Opus frames (80 bytes each, TOC byte 0xb8)
    frames = b""
    for i in range(10):
        frame = bytes([0xB8]) + bytes(79)
        frames += frame
    ogg_data = wrap_opus_ogg(frames)
    return ogg_data, frames
