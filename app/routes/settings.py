"""Settings routes."""

from flask import Blueprint, jsonify, request

from app.utils.config import load_config, update_config
from app.utils.constants import DEFAULT_RECORDINGS_DIR, DEFAULT_TAGS, WHISPER_MODELS, get_recordings_dir
from app.utils.system import get_ram_info

bp = Blueprint("settings", __name__)


@bp.route("/settings", methods=["GET"])
def get_settings():
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
            "diarization_provider": config.get("diarization_provider", "deepgram"),
            "summarization_provider": config.get("summarization_provider", "anthropic"),
            "has_paired_devices": bool(config.get("paired_devices", {})),
            "setup_dismissed": config.get("setup_dismissed", False),
            "gdrive_auto_sync": config.get("gdrive_auto_sync", False),
            "storage_mode": config.get("storage_mode", "local"),
            "save_directory": str(get_recordings_dir()),
            "default_save_directory": str(DEFAULT_RECORDINGS_DIR),
            "historical_directories": config.get("historical_directories", []),
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

    def _apply(config):
        if "model" in data:
            config["whisper_model"] = data["model"]
        if "diarize" in data:
            config["diarize"] = data["diarize"]
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
        if "diarization_provider" in data:
            config["diarization_provider"] = data["diarization_provider"]
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
    return jsonify({"saved": True})
