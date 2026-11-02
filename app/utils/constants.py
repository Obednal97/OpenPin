"""Shared constants for Plaud V2."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent

# User data directory — outside the app bundle so it persists across updates.
# 1. PLAUD_DATA_DIR env var, if set (tests/conftest.py points it at a temp dir
#    so the suite never touches real recordings, config or tokens; dev runs can
#    use it too)
# 2. Packaged app: ~/Library/Application Support/Plaud V2/
# 3. Dev mode (running from a git checkout): the project root
DATA_DIR_ENV = "PLAUD_DATA_DIR"
_is_packaged = not (PROJECT_ROOT / ".git").exists()
_env_data_dir = os.environ.get(DATA_DIR_ENV, "").strip()
if _env_data_dir:
    _data_dir = Path(_env_data_dir).expanduser().resolve()
    _data_dir.mkdir(parents=True, exist_ok=True)
elif _is_packaged:
    _data_dir = Path(os.path.expanduser("~/Library/Application Support/Plaud V2"))
    _data_dir.mkdir(parents=True, exist_ok=True)
else:
    _data_dir = PROJECT_ROOT

DATA_DIR = _data_dir

# Python dependency lockfiles (see scripts/lock-python-deps.sh). The Rust shell
# writes the copies embedded in the app binary to Application Support and passes
# that folder in PLAUD_REQUIREMENTS_DIR; otherwise use the repo/bundle copy.
REQUIREMENTS_DIR_ENV = "PLAUD_REQUIREMENTS_DIR"
BUNDLED_REQUIREMENTS_DIR = PROJECT_ROOT / "app" / "requirements"

# BLE UUIDs
BLE_WRITE_UUID = "00002bb1-0000-1000-8000-00805f9b34fb"
BLE_NOTIFY_UUID = "00002bb0-0000-1000-8000-00805f9b34fb"
BLE_BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

# Device authentication — both SN_SIG and BIND_TOKEN are now per-device
# SN_SIG: obtained from POST /device/sn-sign with {"sns": ["<sn>"]}
#         Stored in config as device_sn_signatures[sn]
# BIND_TOKEN: obtained from POST /device/bind
#             Stored in config as device_bind_tokens[sn]

# Directory paths — user data lives in DATA_DIR, keys stay in the app bundle
DEFAULT_RECORDINGS_DIR = DATA_DIR / "recordings"
RECORDINGS_DIR = DEFAULT_RECORDINGS_DIR  # Legacy alias — prefer get_recordings_dir()
CLOUD_RECORDINGS_DIR = DATA_DIR / "cloud_recordings_decoded"


def get_recordings_dir():
    """Return the active recordings directory (from config, or default).

    New recordings are saved here. Reads from config 'save_directory'.
    """
    from app.utils.config import load_config

    config = load_config()
    custom = config.get("save_directory", "")
    if custom:
        p = Path(custom)
        if p.is_absolute():
            p.mkdir(parents=True, exist_ok=True)
            return p
    DEFAULT_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_RECORDINGS_DIR


def get_all_recordings_dirs():
    """Return all directories that may contain recordings (active + historical).

    Used for listing/searching — aggregates files from every known location.
    """
    from app.utils.config import load_config

    dirs = set()
    dirs.add(get_recordings_dir())
    dirs.add(DEFAULT_RECORDINGS_DIR)

    config = load_config()
    for d in config.get("historical_directories", []):
        p = Path(d)
        if p.is_absolute() and p.is_dir():
            dirs.add(p)

    return list(dirs)
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
SESSIONS_DIR = DATA_DIR / "sessions"
APP_KEY_PATH = PROJECT_ROOT / "keys" / "e2ee_private.pem"
CONFIG_PATH = DATA_DIR / ".plaud_v2_config.json"
HF_TOKEN_PATH = DATA_DIR / ".hf_token"
PLAUD_CONFIG_PATH = DATA_DIR / ".plaud_config.json"

# Plaud Cloud API
PLAUD_API_DOMAINS = {
    "eu": "https://api-euc1.plaud.ai",
    "global": "https://api.plaud.ai",
    "cn": "https://api.plaud.cn",
}
PLAUD_WEB_HOST = "https://web.plaud.ai"

# Whisper models
WHISPER_MODELS = {
    "base": {"ram_gb": 1, "size": "74 MB", "speed": "Fast", "accuracy": "Good enough for clear speech"},
    "small": {"ram_gb": 2, "size": "244 MB", "speed": "Moderate", "accuracy": "Good - recommended default"},
    "medium": {"ram_gb": 5, "size": "769 MB", "speed": "Slower", "accuracy": "Very good - handles accents well"},
    "large": {"ram_gb": 10, "size": "1.5 GB", "speed": "Slowest", "accuracy": "Best available"},
}

# Notion MCP
NOTION_MCP_URL = "https://mcp.notion.com/mcp"
NOTION_CONFIG_PATH = DATA_DIR / ".notion_mcp.json"
NOTION_QUEUE_PATH = DATA_DIR / ".notion_queue.json"
NOTION_CALLBACK_PORT = 5124

# Google Drive
GDRIVE_CONFIG_PATH = DATA_DIR / ".gdrive_config.json"
GDRIVE_CALLBACK_PORT = 5125
GDRIVE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GDRIVE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GDRIVE_API_BASE = "https://www.googleapis.com"
GDRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
GDRIVE_CLIENT_ID = "364974909213-m56e0v5la7ivomclvlf22r3selcsn1b2.apps.googleusercontent.com"
GDRIVE_CLIENT_SECRET = "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"  # Desktop OAuth — see SECURITY.md

# Default tags
DEFAULT_TAGS = ["Group Meeting", "1:1", "Memo", "Journal", "Interview", "Lecture"]

# OGG CRC lookup table
CRC_LOOKUP = [0] * 256
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        _r = ((_r << 1) ^ 0x04C11DB7 if _r & 0x80000000 else _r << 1) & 0xFFFFFFFF
    CRC_LOOKUP[_i] = _r
