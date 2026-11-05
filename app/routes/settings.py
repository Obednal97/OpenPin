"""Settings routes."""

import requests as http_requests
from flask import Blueprint, jsonify, request

from app.mcp import access as mcp_access
from app.mcp import clients as mcp_clients
from app.utils.config import load_config, update_config
from app.utils.constants import DEFAULT_RECORDINGS_DIR, DEFAULT_TAGS, HF_WHOAMI_URL, WHISPER_MODELS, get_recordings_dir
from app.utils.system import get_ram_info
from src.transcribe import delete_hf_token, get_hf_token, save_hf_token

bp = Blueprint("settings", __name__)

_HF_TOKEN_MAX_LEN = 256


def validate_hf_token(token):
    """Check a Hugging Face token with GET whoami-v2 (free, read-only).

    Returns (valid: bool, error: str | None). Never logs or echoes the token.
    """
    if not token:
        return False, "No token provided"
    if not token.startswith("hf_") or len(token) > _HF_TOKEN_MAX_LEN or any(c.isspace() for c in token):
        return False, "That doesn't look like a Hugging Face token (they start with hf_)"
    try:
        r = http_requests.get(HF_WHOAMI_URL, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except http_requests.RequestException:
        return False, "Couldn't reach huggingface.co: check your internet connection"
    if r.status_code == 200:
        return True, None
    if r.status_code in (401, 403):
        return False, "Hugging Face rejected this token"
    return False, f"Hugging Face returned HTTP {r.status_code}"


@bp.route("/settings", methods=["GET"])
def get_settings():
    """The settings the window shows.

    AI tool access has two fields:
    - ``mcp_enabled``: the saved switch ("Let AI tools on this Mac use Open Pin").
    - ``mcp_active``: whether access is actually on now (an MCP key exists).
      It can be False while ``mcp_enabled`` is True if turning it on failed at
      startup (the connection file or launcher couldn't be written); the
      window shows an error when they differ.
    """
    config = load_config()
    total_gb, used_gb = get_ram_info()
    headroom_gb = total_gb * 0.85 - used_gb
    models = {}
    for name, info in WHISPER_MODELS.items():
        models[name] = {**info, "fits": info["ram_gb"] <= headroom_gb}

    custom_tags = config.get("custom_tags", [])
    all_tags = DEFAULT_TAGS + [t for t in custom_tags if t not in DEFAULT_TAGS]

    return jsonify(
        {
            "model": config.get("whisper_model", "small"),
            "diarize": config.get("diarize", False),
            "coaching_auto": config.get("coaching_auto", True),
            "coaching_ai_review": config.get("coaching_ai_review", True),
            "language": config.get("language", "auto"),
            "english_variant": config.get("english_variant", "us"),
            "models": models,
            "ram_total_gb": round(total_gb, 1),
            "ram_used_gb": round(used_gb, 1),
            "ram_headroom_gb": round(headroom_gb, 1),
            "anthropic_key_set": bool(config.get("anthropic_api_key", "")),
            "openai_key_set": bool(config.get("openai_api_key", "")),
            "openrouter_key_set": bool(config.get("openrouter_api_key", "")),
            "deepgram_key_set": bool(config.get("deepgram_api_key", "")),
            "groq_key_set": bool(config.get("groq_api_key", "")),
            "available_tags": all_tags,
            "profile": config.get("profile", {}),
            "custom_vocabulary": config.get("custom_vocabulary", []),
            "webhooks": config.get("webhooks", []),
            "cloud_sync_direction": config.get("cloud_sync_direction", "download"),
            "cloud_sync_interval": config.get("cloud_sync_interval", "manual"),
            "auto_delete_after_sync": config.get("auto_delete_after_sync", False),
            "auto_sync": config.get("auto_sync", True),
            "auto_transcribe": config.get("auto_transcribe", True),
            "auto_summarise": config.get("auto_summarise", False),
            "notion_auto_push": config.get("notion_auto_push", False),
            "auto_prefer_wifi": config.get("auto_prefer_wifi", False),
            "transcription_provider": config.get("transcription_provider", "deepgram"),
            # Only whether a Hugging Face token is saved; the token never leaves the backend.
            "hf_token_set": get_hf_token() is not None,
            "summarization_provider": config.get("summarization_provider", "anthropic"),
            "has_paired_devices": bool(config.get("paired_devices", {})),
            "setup_dismissed": config.get("setup_dismissed", False),
            "gdrive_auto_sync": config.get("gdrive_auto_sync", False),
            "storage_mode": config.get("storage_mode", "local"),
            "save_directory": str(get_recordings_dir()),
            "default_save_directory": str(DEFAULT_RECORDINGS_DIR),
            "historical_directories": config.get("historical_directories", []),
            # "Let AI tools on this Mac use Open Pin" (app/mcp/access.py). Off by default.
            "mcp_enabled": config.get("mcp_enabled", False) is True,
            "mcp_active": mcp_access.current_key() is not None,
        }
    )


@bp.route("/settings", methods=["POST"])
def save_settings():
    data = request.get_json(silent=True) or {}

    # Validate / prepare the save directory before touching the config.
    new_dir = None
    if "save_directory" in data:
        new_dir = data["save_directory"].strip()
        if new_dir:
            from pathlib import Path

            p = Path(new_dir)
            if not p.is_absolute():
                return jsonify({"error": "Path must be absolute"}), 400
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return jsonify({"error": f"Cannot create directory: {e}"}), 400

    # Hugging Face token (optional pyannote fallback): validated, then stored in
    # its own 0600 file in the data dir, never in the config or a response.
    if "hf_token" in data:
        token = str(data.get("hf_token") or "").strip()
        valid, error = validate_hf_token(token)
        if not valid:
            return jsonify({"error": error}), 400
        save_hf_token(token)
    if data.get("remove_hf_token"):
        delete_hf_token()

    def _apply(config):
        if "model" in data:
            config["whisper_model"] = data["model"]
        # coaching_auto first, diarize last: coaching needs speaker labels, so turning
        # diarize off wins even when the same request asks for coaching_auto on.
        if "coaching_auto" in data:
            config["coaching_auto"] = data["coaching_auto"]
        if "diarize" in data:
            config["diarize"] = data["diarize"]
            if not data["diarize"]:
                config["coaching_auto"] = False
        if "coaching_ai_review" in data:
            config["coaching_ai_review"] = bool(data["coaching_ai_review"])
        if "language" in data:
            config["language"] = data["language"]
        if "english_variant" in data:
            config["english_variant"] = data["english_variant"]
        if "custom_tags" in data:
            config["custom_tags"] = data["custom_tags"]
        if "profile" in data:
            config["profile"] = data["profile"]
        if "custom_vocabulary" in data:
            config["custom_vocabulary"] = data["custom_vocabulary"]
        if "cloud_sync_direction" in data:
            config["cloud_sync_direction"] = data["cloud_sync_direction"]
        if "cloud_sync_interval" in data:
            config["cloud_sync_interval"] = data["cloud_sync_interval"]
        if "auto_delete_after_sync" in data:
            config["auto_delete_after_sync"] = data["auto_delete_after_sync"]
        if "auto_sync" in data:
            config["auto_sync"] = data["auto_sync"]
        if "auto_transcribe" in data:
            config["auto_transcribe"] = data["auto_transcribe"]
        if "auto_summarise" in data:
            config["auto_summarise"] = data["auto_summarise"]
        if "notion_auto_push" in data:
            config["notion_auto_push"] = data["notion_auto_push"]
        if "webhooks" in data:
            config["webhooks"] = data["webhooks"]
        if "auto_prefer_wifi" in data:
            config["auto_prefer_wifi"] = data["auto_prefer_wifi"]
        if "setup_dismissed" in data:
            config["setup_dismissed"] = data["setup_dismissed"]
        if "gdrive_auto_sync" in data:
            config["gdrive_auto_sync"] = data["gdrive_auto_sync"]
        if "storage_mode" in data:
            if data["storage_mode"] in ("local", "cloud", "both"):
                config["storage_mode"] = data["storage_mode"]
        if "transcription_provider" in data:
            config["transcription_provider"] = data["transcription_provider"]
        # Retired: who labels speakers follows the transcription provider
        # (Deepgram labels its own transcripts; otherwise the local model).
        config.pop("diarization_provider", None)
        if "summarization_provider" in data:
            config["summarization_provider"] = data["summarization_provider"]
        if "remove_openai_key" in data:
            config.pop("openai_api_key", None)
        if "remove_anthropic_key" in data:
            config.pop("anthropic_api_key", None)
        if "remove_openrouter_key" in data:
            config.pop("openrouter_api_key", None)
        if "remove_deepgram_key" in data:
            config.pop("deepgram_api_key", None)
        if "remove_groq_key" in data:
            config.pop("groq_api_key", None)
        for key_name in (
            "anthropic_api_key",
            "openai_api_key",
            "openrouter_api_key",
            "deepgram_api_key",
            "groq_api_key",
        ):
            if key_name in data:
                config[key_name] = data[key_name].strip()

        if new_dir is not None:
            # Track the old directory in the historical list (get_recordings_dir
            # re-reads the config; fine — the config lock is re-entrant).
            old_dir = str(get_recordings_dir())
            historical = config.get("historical_directories", [])
            if old_dir not in historical:
                historical.append(old_dir)
            config["historical_directories"] = historical
            if new_dir:
                config["save_directory"] = new_dir
            else:
                # Reset to default
                config.pop("save_directory", None)

    update_config(_apply)

    if "mcp_enabled" in data:
        error = _apply_mcp_enabled(data["mcp_enabled"] is True)
        if error:
            return jsonify({"error": error}), 500
    return jsonify({"saved": True})


def _request_port() -> int | None:
    try:
        return int(request.environ.get("SERVER_PORT", ""))
    except ValueError:
        return None


def _apply_mcp_enabled(on: bool) -> str | None:
    """Turn AI tool access on or off now and save the setting. Returns an error or None.

    Only a JSON true turns it on. If the key files can't be written, access
    stays off and the setting is saved as off.
    """
    if not on:
        update_config(lambda c: c.__setitem__("mcp_enabled", False))
        mcp_access.disable()
        return None
    port = mcp_access.bound_port() or _request_port()
    try:
        if port is None:
            raise OSError("the backend's port isn't known")
        mcp_access.enable(port)
    except OSError as e:
        mcp_access.disable()
        update_config(lambda c: c.__setitem__("mcp_enabled", False))
        return f"Couldn't turn on AI tool access: {e}"
    update_config(lambda c: c.__setitem__("mcp_enabled", True))
    return None


# ---------------------------------------------------------------------------
# One-click setup for Claude Desktop and Claude Code (app/mcp/clients.py).
# App-token routes like the rest of the window's API (not /mcp/v1, which only
# AI tools use). The window may only name files that status() lists.
# ---------------------------------------------------------------------------


@bp.route("/mcp/setup/status", methods=["GET"])
def mcp_setup_status():
    return jsonify(mcp_clients.status())


def _mcp_setup_target():
    """(kind, path) from the request body, or an error response."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}  # a list, string or number: no kind or path, so a 400 below
    kind = data.get("kind")
    path = mcp_clients.resolve(kind, data.get("path"))
    if path is None:
        return None, (
            jsonify({"error": "Open Pin didn't find that settings file. Reopen Settings and try again."}),
            400,
        )
    return (kind, path), None


def _mcp_setup_change(action):
    target, error = _mcp_setup_target()
    if error:
        return error
    kind, path = target
    try:
        action(path, kind)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"Couldn't save {mcp_clients.shown_path(path)}: {e.strerror or e}"}), 500
    return jsonify(mcp_clients.status())


@bp.route("/mcp/setup/connect", methods=["POST"])
def mcp_setup_connect():
    if load_config().get("mcp_enabled", False) is not True:
        return jsonify({"error": 'Turn on "Let AI tools on this Mac use Open Pin" first.'}), 409
    return _mcp_setup_change(mcp_clients.connect)


@bp.route("/mcp/setup/disconnect", methods=["POST"])
def mcp_setup_disconnect():
    # Allowed with access off: removing Open Pin's entry is always fine.
    return _mcp_setup_change(mcp_clients.disconnect)
