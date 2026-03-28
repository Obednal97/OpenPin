"""Settings routes."""

from flask import Blueprint, jsonify, request

from app.utils.config import load_config, save_config
from app.utils.constants import DEFAULT_TAGS, WHISPER_MODELS
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
            "auto_delete_after_sync": config.get("auto_delete_after_sync", False),
            "auto_sync": config.get("auto_sync", True),
            "auto_transcribe": config.get("auto_transcribe", True),
            "auto_summarise": config.get("auto_summarise", False),
            "notion_auto_push": config.get("notion_auto_push", False),
        }
    )


@bp.route("/settings", methods=["POST"])
def save_settings():
    data = request.get_json(silent=True) or {}
    config = load_config()

    if "model" in data:
        config["whisper_model"] = data["model"]
    if "diarize" in data:
        config["diarize"] = data["diarize"]
    if "language" in data:
        config["language"] = data["language"]
    if "custom_tags" in data:
        config["custom_tags"] = data["custom_tags"]
    if "profile" in data:
        config["profile"] = data["profile"]
    if "custom_vocabulary" in data:
        config["custom_vocabulary"] = data["custom_vocabulary"]
    if "cloud_sync_direction" in data:
        config["cloud_sync_direction"] = data["cloud_sync_direction"]
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
    if "anthropic_api_key" in data:
        key = data["anthropic_api_key"].strip()
        config["anthropic_api_key"] = key
    if "openai_api_key" in data:
        key = data["openai_api_key"].strip()
        config["openai_api_key"] = key
    if "openrouter_api_key" in data:
        key = data["openrouter_api_key"].strip()
        config["openrouter_api_key"] = key
    if "deepgram_api_key" in data:
        key = data["deepgram_api_key"].strip()
        config["deepgram_api_key"] = key
    if "groq_api_key" in data:
        key = data["groq_api_key"].strip()
        config["groq_api_key"] = key

    save_config(config)
    return jsonify({"saved": True})
