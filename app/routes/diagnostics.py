"""GET /diagnostics: checks for an exported bug report.

The Rust shell writes these into report.md (Report a bug › Export bug report).
Only facts that help with a bug, never secrets: booleans for tokens, keys and
signatures, the last four characters of serial numbers and the API host. No
tokens, API keys, user ids, emails, full serial numbers or file contents.
"""

import logging
import shutil
from urllib.parse import urlparse

from flask import Blueprint, jsonify

import app.state.cloud as cloud_state
import app.state.device as dev
import app.state.gdrive as gdrive_state
import app.state.notion as notion_state
from app.utils.config import load_config
from app.utils.constants import get_all_recordings_dirs, get_recordings_dir
from app.utils.paths import is_auxiliary_stem
from src.transcribe import get_hf_token

bp = Blueprint("diagnostics", __name__)

# Providers with an API key in the config (`<name>_api_key`, see routes/settings.py).
_KEY_PROVIDERS = ("anthropic", "deepgram", "groq", "openai", "openrouter")
_SETTING_MAX_LEN = 40


def _setting(config, key, default):
    """A provider choice from the config, as a short string."""
    value = config.get(key, default)
    return str(value)[:_SETTING_MAX_LEN] if value is not None else None


def _cloud():
    state = cloud_state._cloud_state
    return {
        "signed_in": bool(state.get("logged_in")),
        # sn-sign needs the user token (pld_ut); see issue #104.
        "has_user_token": bool(state.get("ut")),
        "region": urlparse(state.get("api_domain") or "").hostname or "unknown",
    }


def _devices(config):
    """Paired devices from the config only (no Keychain restore)."""
    paired = config.get("paired_devices") or {}
    bind_tokens = config.get("device_bind_tokens") or {}
    signatures = config.get("device_sn_signatures") or {}
    return [
        {
            "serial_last4": str(sn)[-4:],
            "has_sn_signature": bool(signatures.get(sn)),
            "has_bind_token": bool(bind_tokens.get(sn)),
            "name": (info or {}).get("name") if isinstance(info, dict) else None,
        }
        for sn, info in sorted(paired.items())
    ]


def _connection():
    return {
        "connected": bool(dev.is_connected()),
        "port_version": dev._device_state.get("port_version"),
        "last_handshake": dev.get_last_handshake(),
    }


def _processing(config):
    return {
        "transcription_provider": _setting(config, "transcription_provider", "deepgram"),
        "summarization_provider": _setting(config, "summarization_provider", "anthropic"),
        "keys_configured": {name: bool(config.get(f"{name}_api_key")) for name in _KEY_PROVIDERS},
        "huggingface_configured": get_hf_token() is not None,
    }


def _integrations():
    return {
        "notion_connected": bool(notion_state.is_connected()),
        "gdrive_connected": bool(gdrive_state.is_connected()),
    }


def _storage():
    ids = set()
    for rec_dir in get_all_recordings_dirs():
        if rec_dir.is_dir():
            ids.update(f.stem for f in rec_dir.glob("*.ogg") if not is_auxiliary_stem(f.stem))
    free = shutil.disk_usage(get_recordings_dir()).free
    return {"recordings": len(ids), "free_disk_gb": round(free / 1e9, 1)}


def _section(name, build):
    """Run one section; a failure is reported by type only (messages can hold paths)."""
    try:
        return build()
    except Exception as e:
        logging.warning(f"[Diagnostics] {name} check failed: {type(e).__name__}")
        return {"error": type(e).__name__}


@bp.route("/diagnostics")
def diagnostics():
    config = load_config()
    return jsonify(
        {
            "cloud": _section("cloud", _cloud),
            "devices": _section("devices", lambda: _devices(config)),
            "connection": _section("connection", _connection),
            "processing": _section("processing", lambda: _processing(config)),
            "integrations": _section("integrations", _integrations),
            "storage": _section("storage", _storage),
        }
    )
