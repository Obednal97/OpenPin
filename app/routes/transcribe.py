"""Transcription and summarization routes."""

import json
import logging
import threading
import traceback
from pathlib import Path

import requests as http_requests
from flask import Blueprint, jsonify, request

from app.providers.summarize import summarize
from app.providers.transcribe import transcribe_deepgram, transcribe_groq, transcribe_openai
from app.providers.validate import validate_key as do_validate_key
from app.utils.config import load_config, save_config
from app.utils.constants import TRANSCRIPTS_DIR
from app.utils.webhooks import fire_webhooks


def _error_json(base, exc):
    """Return error JSON with traceback for debugging (auto-reported to GitHub)."""
    base["error"] = str(exc)
    base["traceback"] = traceback.format_exc()
    return base

bp = Blueprint("transcribe", __name__)


@bp.route("/transcribe", methods=["POST"])
def transcribe_file():
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    if not path or not Path(path).exists():
        return jsonify({"error": "File not found"}), 400

    config = load_config()
    model = data.get("model", config.get("whisper_model", "small"))
    diarize = data.get("diarize", config.get("diarize", False))
    language = data.get("language", config.get("language", "en"))
    if language == "auto":
        language = None
    session_id = Path(path).stem
    transcript_path = TRANSCRIPTS_DIR / f"{session_id}.json"
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)

    try:
        _used_cloud = False
        _cloud_error = None
        _cloud_provider = None
        _cloud_has_speakers = False
        result = None
        vocab = config.get("custom_vocabulary", [])
        vocab_prompt = ", ".join(vocab) if vocab else None

        # Build provider order based on user preference (preferred first, then fallbacks)
        preferred = config.get("transcription_provider", "deepgram")
        cloud_providers = ["deepgram", "groq", "openai"]
        if preferred in cloud_providers:
            cloud_providers.remove(preferred)
            cloud_providers.insert(0, preferred)

        def _try_provider(name):
            nonlocal _used_cloud, _cloud_provider, _cloud_has_speakers, _cloud_error
            key = config.get(f"{name}_api_key")
            if not key:
                return False
            try:
                nonlocal result
                if name == "deepgram":
                    result = transcribe_deepgram(path, key, language or "en", diarize=diarize, keywords=vocab)
                    _cloud_has_speakers = bool(result.get("speakers"))
                elif name == "groq":
                    result = transcribe_groq(path, key, language or "en", vocab_prompt=vocab_prompt)
                elif name == "openai":
                    result = transcribe_openai(path, key, language or "en", vocab_prompt=vocab_prompt)
                _used_cloud = True
                _cloud_provider = name
                return True
            except http_requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                label = {"deepgram": "Deepgram", "groq": "Groq", "openai": "OpenAI"}[name]
                if status == 401:
                    _cloud_error = f"{label}: Invalid API key — check your key in Settings"
                elif status == 429:
                    _cloud_error = f"{label}: Rate limited — falling back"
                else:
                    _cloud_error = f"{label}: HTTP {status} — falling back"
                logging.warning(f"{label} transcription failed ({status}): {e}")
            except Exception as e:
                label = {"deepgram": "Deepgram", "groq": "Groq", "openai": "OpenAI"}[name]
                _cloud_error = f"{label}: {str(e)[:100]} — falling back"
                logging.warning(f"{label} transcription failed: {e}")
            return False

        # Try preferred provider first, then fall back to others
        if preferred != "local":
            for prov in cloud_providers:
                if _try_provider(prov):
                    break

        if _used_cloud:
            text = result.get("text", "")
            segments = []
            for seg in result.get("segments", []):
                segments.append(
                    {
                        "start": seg.get("start", 0),
                        "end": seg.get("end", 0),
                        "text": seg.get("text", "").strip(),
                        "speaker": seg.get("speaker"),
                    }
                )
            duration = result.get("duration", 0)
            lang = result.get("language", language or "en")
            speakers = result.get("speakers", [])

            # Merge/group segments by speaker + sentence boundaries
            # (same logic used for local diarization — produces clean grouped output)
            if _cloud_has_speakers and segments:
                from src.transcribe import Transcript, TranscriptSegment, _merge_speaker_segments
                t_obj = Transcript(text=text, language=lang, duration=duration)
                for s in segments:
                    t_obj.segments.append(
                        TranscriptSegment(start=s["start"], end=s["end"], text=s["text"], speaker=s.get("speaker", ""))
                    )
                t_obj.segments = _merge_speaker_segments(t_obj.segments)
                segments = [
                    {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker}
                    for s in t_obj.segments
                ]
            prov = _cloud_provider

            t_data = {
                "text": text,
                "duration": duration,
                "language": lang,
                "segments": segments,
                "provider": prov,
            }
            if speakers:
                t_data["speakers"] = speakers
            transcript_path.write_text(json.dumps(t_data, indent=2, ensure_ascii=False))
            txt_path = TRANSCRIPTS_DIR / f"{session_id}.txt"
            txt_path.write_text(text, encoding="utf-8")

            if diarize and text and not _cloud_has_speakers:
                _bg_path = str(path)
                _bg_segs = list(segments)
                _bg_tp = str(transcript_path)
                _bg_sid = str(session_id)
                _bg_text = str(text)
                _bg_lang = str(lang)
                _bg_dur = float(duration)

                def _bg_diarize():
                    try:
                        from src.transcribe import (
                            Transcript,
                            TranscriptSegment,
                            _get_hf_token,
                            diarize_audio,
                            merge_transcript_with_diarization,
                        )

                        hf_token = _get_hf_token()
                        if not hf_token:
                            print(f"  [bg-diarize] No HF token for {_bg_sid}")
                            return
                        print(f"  [bg-diarize] Starting for {_bg_sid}...")
                        diar_segments = diarize_audio(_bg_path, hf_token)
                        if not diar_segments:
                            print(f"  [bg-diarize] No speakers found for {_bg_sid}")
                            return
                        t_obj = Transcript(text=_bg_text, language=_bg_lang, duration=_bg_dur)
                        for s in _bg_segs:
                            t_obj.segments.append(TranscriptSegment(start=s["start"], end=s["end"], text=s["text"]))
                        t_obj = merge_transcript_with_diarization(t_obj, diar_segments)
                        updated_segments = [
                            {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker}
                            for s in t_obj.segments
                        ]
                        speakers = list({s.speaker for s in t_obj.segments if s.speaker})
                        tp = Path(_bg_tp)
                        td = json.loads(tp.read_text())
                        td["segments"] = updated_segments
                        td["provider"] = f"{_cloud_provider}+pyannote"
                        td["speakers"] = speakers
                        tp.write_text(json.dumps(td, indent=2, ensure_ascii=False))
                        print(f"  [bg-diarize] Complete for {_bg_sid}: {len(speakers)} speaker(s)")
                        # Fire webhook after diarization with full segmented transcript
                        fire_webhooks(
                            "transcribe",
                            {
                                "session_id": _bg_sid,
                                "text": _bg_text[:500],
                                "duration": _bg_dur,
                                "language": _bg_lang,
                                "speakers": speakers,
                                "segments": updated_segments,
                            },
                        )
                    except Exception as e:
                        print(f"  [bg-diarize] FAILED for {_bg_sid}: {e}")
                        # Fire webhook anyway on diarization failure, with basic segments
                        fire_webhooks(
                            "transcribe",
                            {
                                "session_id": _bg_sid,
                                "text": _bg_text[:500],
                                "duration": _bg_dur,
                                "language": _bg_lang,
                                "segments": _bg_segs,
                            },
                        )

                threading.Thread(target=_bg_diarize, daemon=True).start()

            else:
                # No diarization — fire webhook immediately with segments
                fire_webhooks(
                    "transcribe",
                    {
                        "session_id": session_id,
                        "text": text[:500],
                        "duration": duration,
                        "language": lang,
                        "segments": segments,
                    },
                )

            return jsonify(
                {
                    "session_id": session_id,
                    "text": text,
                    "duration": duration,
                    "language": lang,
                    "speakers": speakers,
                    "segments": segments,
                    "path": str(transcript_path),
                    "provider": prov,
                    "warning": _cloud_error,
                    "diarizing": diarize and not _cloud_has_speakers,
                }
            )

        # Local transcription via Whisper
        vocab = config.get("custom_vocabulary", [])
        vocab_prompt = ", ".join(vocab) if vocab else None

        from src.transcribe import transcribe as do_transcribe

        t = do_transcribe(str(path), model=model, language=language, diarize=diarize, initial_prompt=vocab_prompt)

        if t.text:
            t.save(str(transcript_path))
            txt_path = TRANSCRIPTS_DIR / f"{session_id}.txt"
            txt_path.write_text(t.formatted, encoding="utf-8")

            speakers = list({s.speaker for s in t.segments if s.speaker})
            local_segments = [
                {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker} for s in t.segments
            ]
            fire_webhooks(
                "transcribe",
                {
                    "session_id": session_id,
                    "text": t.text[:500],
                    "duration": t.duration,
                    "language": t.language,
                    "speakers": speakers,
                    "segments": local_segments,
                },
            )

            return jsonify(
                {
                    "session_id": session_id,
                    "text": t.text,
                    "duration": t.duration,
                    "language": t.language,
                    "speakers": speakers,
                    "segments": [
                        {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker} for s in t.segments
                    ],
                    "path": str(transcript_path),
                    "provider": "local",
                    "warning": _cloud_error,
                }
            )
        else:
            return jsonify({"session_id": session_id, "text": "", "message": "No speech detected"})
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


@bp.route("/summarize", methods=["POST"])
def summarize_transcript():
    """Generate a summary and auto-title for a transcript."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id:
        return jsonify({"error": "No session_id"}), 400

    transcript_path = TRANSCRIPTS_DIR / f"{session_id}.json"
    if not transcript_path.exists():
        return jsonify({"error": "Transcript not found"}), 404

    try:
        t_data = json.loads(transcript_path.read_text())
        full_text = t_data.get("text", "")
        if not full_text or len(full_text.strip()) < 20:
            return jsonify({"error": "Transcript too short to summarize"}), 400

        config = load_config()

        # Build user profile context
        profile = config.get("profile", {})
        profile_context = ""
        if any(profile.values()):
            parts = []
            if profile.get("job"):
                parts.append(f"Job: {profile['job']}")
            if profile.get("industry"):
                parts.append(f"Industry: {profile['industry']}")
            if profile.get("seniority"):
                parts.append(f"Seniority: {profile['seniority']}")
            if profile.get("usecases"):
                parts.append(f"Primary use: {profile['usecases']}")
            if profile.get("instructions"):
                parts.append(f"Custom instructions: {profile['instructions']}")
            profile_context = "\n\nUser context: " + "; ".join(parts)

        # Get tags for context-aware summarization
        rec_tags = config.get("recording_tags", {}).get(session_id, [])
        tag_context = ""
        if rec_tags:
            tag_str = ", ".join(rec_tags)
            tag_context = (
                f"\n\nThis recording is tagged as: {tag_str}. "
                f"Tailor the summary style accordingly:\n"
                f"- Group Meeting: focus on decisions, action items, who said what\n"
                f"- 1:1: focus on discussion points, feedback, agreements, follow-ups\n"
                f"- Memo: focus on key information, directives, context\n"
                f"- Journal: focus on reflections, ideas, personal insights, mood\n"
                f"- Interview: focus on questions asked, answers given, candidate assessment\n"
                f"- Lecture: focus on key concepts, definitions, examples, takeaways\n"
            )

        extra_context = profile_context + tag_context
        summary_text, title, provider, errors = summarize(full_text, config, extra_context)

        if summary_text is None:
            error_detail = " → ".join(errors) if errors else "Unknown error"
            return jsonify({"error": f"Summarization failed. {error_detail}"}), 500

        if not title:
            title = summary_text.split(".")[0].strip()
            if len(title) > 60:
                title = title[:57] + "..."
            if len(title) < 5:
                title = summary_text[:60].strip()

        t_data["summary"] = summary_text
        t_data["auto_title"] = title
        transcript_path.write_text(json.dumps(t_data, indent=2, ensure_ascii=False))

        names = config.get("recording_names", {})
        if not names.get(session_id):
            names[session_id] = title
            config["recording_names"] = names
            save_config(config)

        fire_webhooks(
            "summarize", {"session_id": session_id, "title": title, "summary": summary_text, "provider": provider}
        )

        # Auto-push to Notion if enabled
        notion_url = ""
        notion_error = ""
        if config.get("notion_auto_push"):
            try:
                from app.routes.notion import (
                    _build_notion_content,
                    _extract_page_url,
                    _get_notion_page_url,
                    _save_notion_page_url,
                )
                from app.state import notion as notion_state

                if notion_state.is_connected() and notion_state.get_database_id():
                    content = _build_notion_content(t_data)
                    existing = _get_notion_page_url(session_id)
                    if existing:
                        notion_state.mcp_call(
                            "notion-update-page",
                            {"url": existing, "properties": {"title": title}, "content": content},
                        )
                        notion_url = existing
                    else:
                        result = notion_state.mcp_call(
                            "notion-create-pages",
                            {
                                "parent": {"database_id": notion_state.get_database_id()},
                                "pages": [
                                    {
                                        "properties": {"title": title},
                                        "content": content,
                                        "icon": "\U0001f399\ufe0f",
                                    }
                                ],
                            },
                        )
                        page_url = _extract_page_url(result)
                        if page_url:
                            _save_notion_page_url(session_id, page_url)
                            notion_url = page_url
                elif not notion_state.is_connected():
                    notion_error = "Notion not connected"
                elif not notion_state.get_database_id():
                    notion_error = "No Notion database selected"
            except Exception as e:
                notion_error = f"Notion push failed: {e}"
                logging.warning(f"Notion auto-push failed: {e}")

        return jsonify(
            {
                "summary": summary_text,
                "title": title,
                "provider": provider,
                "notion_url": notion_url,
                "notion_error": notion_error,
            }
        )

    except Exception as e:
        return jsonify(_error_json({}, e)), 500


@bp.route("/validate-key", methods=["POST"])
def validate_key():
    """Validate an API key by making a lightweight test request."""
    data = request.get_json(silent=True) or {}
    provider = data.get("provider", "")
    key = data.get("key", "").strip()
    valid, error = do_validate_key(provider, key)
    if valid:
        return jsonify({"valid": True})
    return jsonify({"valid": False, "error": error})
