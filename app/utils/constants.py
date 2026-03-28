"""Shared constants for Plaud V2."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent

# BLE UUIDs
BLE_WRITE_UUID = "00002bb1-0000-1000-8000-00805f9b34fb"
BLE_NOTIFY_UUID = "00002bb0-0000-1000-8000-00805f9b34fb"
BLE_BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

# Device authentication
SN_SIG = "<REDACTED_SN_SIGNATURE>"
BIND_TOKEN = "<REDACTED_JWT>"

# Directory paths
RECORDINGS_DIR = PROJECT_ROOT / "recordings"
CLOUD_RECORDINGS_DIR = PROJECT_ROOT / "cloud_recordings_decoded"
TRANSCRIPTS_DIR = PROJECT_ROOT / "transcripts"
SESSIONS_DIR = PROJECT_ROOT / "sessions"
APP_KEY_PATH = PROJECT_ROOT / "keys" / "app_private.pem"
CONFIG_PATH = PROJECT_ROOT / ".plaud_v2_config.json"
HF_TOKEN_PATH = PROJECT_ROOT / ".hf_token"
PLAUD_CONFIG_PATH = PROJECT_ROOT / ".plaud_config.json"

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
NOTION_CONFIG_PATH = PROJECT_ROOT / ".notion_mcp.json"
NOTION_CALLBACK_PORT = 5124

# Default tags
DEFAULT_TAGS = ["Group Meeting", "1:1", "Memo", "Journal", "Interview", "Lecture"]

# OGG CRC lookup table
CRC_LOOKUP = [0] * 256
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        _r = ((_r << 1) ^ 0x04C11DB7 if _r & 0x80000000 else _r << 1) & 0xFFFFFFFF
    CRC_LOOKUP[_i] = _r
