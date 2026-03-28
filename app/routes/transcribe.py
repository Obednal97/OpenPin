"""Transcription and summarization routes."""

import json
import logging
import threading
from pathlib import Path

import requests as http_requests
from flask import Blueprint, jsonify, request

from app.providers.summarize import summarize
from app.providers.transcribe import transcribe_openai
from app.providers.validate import validate_key as do_validate_key
from app.utils.config import load_config, save_config
from app.utils.constants import TRANSCRIPTS_DIR
from app.utils.webhooks import fire_webhooks

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
        _used_openai = False
        _openai_error = None
        if config.get("openai_api_key"):
            try:
                vocab = config.get("custom_vocabulary", [])
                result = transcribe_openai(
                    path, config["openai_api_key"], language or "en", vocab_prompt=", ".join(vocab) if vocab else None
                )
                _used_openai = True
            except http_requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status == 401:
                    _openai_error = "OpenAI: Invalid API key — check your key in Settings"
                elif status == 429:
                    _openai_error = "OpenAI: Rate limited — falling back to local Whisper"
                else:
                    _openai_error = f"OpenAI: HTTP {status} — falling back to local Whisper"
                logging.warning(f"OpenAI transcription failed ({status}): {e}")
            except Exception as e:
                _openai_error = f"OpenAI: {str(e)[:100]} — falling back to local Whisper"
                logging.warning(f"OpenAI transcription failed: {e}")

        if _used_openai:
            text = result.get("text", "")
            segments = []
            for seg in result.get("segments", []):
                segments.append(
                    {
                        "start": seg.get("start", 0),
                        "end": seg.get("end", 0),
                        "text": seg.get("text", "").strip(),
                        "speaker": None,
                    }
                )
            duration = result.get("duration", 0)
            lang = result.get("language", language or "en")
            prov = "openai"

            t_data = {
                "text": text,
                "duration": duration,
                "language": lang,
                "segments": segments,
                "provider": prov,
            }
            transcript_path.write_text(json.dumps(t_data, indent=2, ensure_ascii=False))
            txt_path = TRANSCRIPTS_DIR / f"{session_id}.txt"
            txt_path.write_text(text, encoding="utf-8")

            if diarize and text:
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
                        td["provider"] = "openai+pyannote"
                        td["speakers"] = speakers
                        tp.write_text(json.dumps(td, indent=2, ensure_ascii=False))
                        print(f"  [bg-diarize] Complete for {_bg_sid}: {len(speakers)} speaker(s)")
                    except Exception as e:
                        print(f"  [bg-diarize] FAILED for {_bg_sid}: {e}")

                threading.Thread(target=_bg_diarize, daemon=True).start()

            fire_webhooks(
                "transcribe", {"session_id": session_id, "text": text[:500], "duration": duration, "language": lang}
            )

            return jsonify(
                {
                    "session_id": session_id,
                    "text": text,
                    "duration": duration,
                    "language": lang,
                    "speakers": [],
                    "segments": segments,
                    "path": str(transcript_path),
                    "provider": prov,
                    "warning": _openai_error,
                    "diarizing": diarize,
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
            fire_webhooks(
                "transcribe",
                {
                    "session_id": session_id,
                    "text": t.text[:500],
                    "duration": t.duration,
                    "language": t.language,
                    "speakers": speakers,
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
                    "warning": _openai_error,
                }
            )
        else:
            return jsonify({"session_id": session_id, "text": "", "message": "No speech detected"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
            "summarize", {"session_id": session_id, "title": title, "summary": summary_text[:500], "provider": provider}
        )

        return jsonify(
            {
                "summary": summary_text,
                "title": title,
                "provider": provider,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
