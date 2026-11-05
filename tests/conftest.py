"""Shared test fixtures for Plaud V2."""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Isolate the suite from real user data BEFORE anything imports app.*:
# app/utils/constants.py resolves DATA_DIR at import time, and without
# PLAUD_DATA_DIR it would be the installed app's folder (real recordings, config
# and cloud token). Always overridden, even if the developer has PLAUD_DATA_DIR
# set for dev runs.
TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="plaud-test-data-")).resolve()
os.environ["PLAUD_DATA_DIR"] = str(TEST_DATA_DIR)
# Never read or write the developer's login Keychain (app/utils/pairing_store.py).
# tests/test_pairing_backup.py switches it back on with `security` faked.
os.environ["PLAUD_KEYCHAIN_BACKUP"] = "0"

import pytest  # noqa: E402

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def pytest_configure(config):
    # (The `slow` marker is registered in pyproject.toml.)
    # Something imported app.utils.constants before this conftest ran, so its
    # DATA_DIR may point at real data. Stop rather than risk it.
    constants = sys.modules.get("app.utils.constants")
    if constants is not None and Path(constants.DATA_DIR).resolve() != TEST_DATA_DIR:
        raise pytest.UsageError(
            f"app.utils.constants was imported before tests/conftest.py set PLAUD_DATA_DIR "
            f"(DATA_DIR={constants.DATA_DIR}); refusing to run against real data."
        )


def pytest_unconfigure(config):
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


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


@pytest.fixture(autouse=True)
def _no_real_bluetooth_address_check(monkeypatch):
    """connect_device() checks a saved address with a real BLE scan first; tests
    that fake the device answer "in range" (tests that need otherwise patch it)."""
    import app.state.device as dev

    async def visible(address):
        return True

    monkeypatch.setattr(dev, "_address_visible", visible)
    # Scanned BLEDevices are remembered module-wide: none leak between tests.
    monkeypatch.setattr(dev, "_recent_ble_devices", {})
    # A scan that finds nothing asks CoreBluetooth for already-connected
    # NotePins: never the real Mac's Bluetooth in tests. Dropped clients are
    # module-wide too.

    async def none_connected():
        return []

    monkeypatch.setattr(dev, "_system_connected_notepins", none_connected)
    monkeypatch.setattr(dev, "_orphaned_clients", [])


@pytest.fixture(autouse=True)
def _no_real_title_generation(monkeypatch):
    """/transcribe names unsummarised recordings with an AI call (generate_title).
    Tests never reach a real provider, local Ollama included; tests of the
    title code patch it themselves."""
    import app.routes.transcribe as transcribe_routes

    monkeypatch.setattr(transcribe_routes, "generate_title", lambda *a, **k: ("", "none"))


@pytest.fixture(autouse=True)
def _no_real_voice_embeddings(monkeypatch):
    """Tests never load the WeSpeaker model; voice tests inject a fake embedder."""
    import app.coaching.voice as voice

    def refuse(path):
        raise AssertionError("no real embeddings in tests")

    refuse.real = voice._embed_wav  # for the one test that covers the real wrapper
    monkeypatch.setattr(voice, "_embed_wav", refuse)


@pytest.fixture(autouse=True)
def _no_real_mcp_launcher(monkeypatch):
    """AI tool access never writes into the real Application Support, and no
    test leaves it on for the next (app/mcp/access.py keeps the key module-wide)."""
    import app.mcp.access as mcp_access

    support = TEST_DATA_DIR / "app-support" / "com.plaud.v2"
    monkeypatch.setattr(mcp_access, "support_dir", lambda *a, **k: support)
    yield
    mcp_access.disable()
