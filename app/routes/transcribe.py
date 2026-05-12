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
from app.utils.transcript_locks import get_transcript_lock
from app.utils.webhooks import fire_webhooks


def _extract_spoken_tags(text):
    """Extract tags from spoken trigger phrases at the start of a recording.

    Scans the first ~500 characters for phrases like:
    - "Tag: X, Y, Z"
    - "Tags: X, Y"
    - "Add tag X"
    - "Add tags X, Y, Z"
    - "Add the tag X"
    - "Add the tags X, Y, Z"

    Returns list of tag strings, or empty list if no triggers found.
    """
    import re
    if not text:
        return []
    # Only scan the first ~500 chars (roughly first 30 seconds of speech)
    prefix = text[:500].strip()
    tags = []
    # Match various trigger patterns (case-insensitive)
    patterns = [
        r"(?:add\s+)?(?:the\s+)?tags?\s*[:\-]\s*(.+?)(?:\.|$)",
        r"(?:add\s+)?(?:the\s+)?tags?\s+(.+?)(?:\.|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, prefix, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            # Split on commas or "and"
            parts = re.split(r"\s*,\s*|\s+and\s+", raw)
            for part in parts:
                tag = part.strip().rstrip(".")
                if tag and len(tag) < 50:
                    tags.append(tag.title())
            break
    return tags


def _apply_spoken_tags(session_id, text):
    """Detect and apply spoken tags from transcript text. Returns list of applied tags."""
    tags = _extract_spoken_tags(text)
    if not tags:
        return []
    config = load_config()
    existing = config.get("recording_tags", {}).get(session_id, [])
    # Merge — don't duplicate (case-insensitive comparison)
    merged = list(existing)
    existing_lower = {x.lower() for x in existing}
    custom_tags = set(config.get("custom_tags", []))
    for t in tags:
        if t.lower() not in existing_lower:
            merged.append(t)
        # Auto-add to custom tags list if not already there (case-insensitive)
        if t.lower() not in {x.lower() for x in custom_tags}:
            custom_tags.add(t)
    rec_tags = config.get("recording_tags", {})
    rec_tags[session_id] = merged
    config["recording_tags"] = rec_tags
    config["custom_tags"] = sorted(custom_tags)
    save_config(config)
    logging.info(f"Auto-tagged {session_id} with spoken tags: {tags}")
    return tags


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
    if not path:
        return jsonify({"error": "No path"}), 400

    # The local API has no auth (binds 127.0.0.1) so the caller can submit any
    # path on disk. Without containment, /transcribe is a primitive to ship
    # arbitrary readable files (e.g. ~/.ssh/id_rsa) to a cloud transcription
    # provider. Resolve symlinks + reject anything outside the known dirs.
    try:
        resolved = Path(path).resolve(strict=True)
    except (FileNotFoundError, OSError):
        return jsonify({"error": "File not found"}), 400
    from app.utils.constants import CLOUD_RECORDINGS_DIR, get_all_recordings_dirs

    allowed_dirs = [d.resolve() for d in get_all_recordings_dirs()]
    allowed_dirs.append(CLOUD_RECORDINGS_DIR.resolve())
    if not any(resolved.is_relative_to(base) for base in allowed_dirs):
        return jsonify({"error": "Path is outside the recordings directories"}), 400
    path = str(resolved)

    config = load_config()
    model = data.get("model", config.get("whisper_model", "small"))
    diarize = data.get("diarize", config.get("diarize", False))
    language = data.get("language", config.get("language", "en"))
    if language == "auto":
        language = None
    # For Deepgram: use regional variant (en-GB vs en-US) when language is English
    eng_variant = config.get("english_variant", "us")
    deepgram_language = language
    if language == "en":
        deepgram_language = f"en-{'GB' if eng_variant == 'gb' else 'US'}"
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
        # Build transcription prompt: vocab + profile context for Groq/OpenAI Whisper
        prompt_parts = []
        if vocab:
            prompt_parts.append(", ".join(vocab))
        profile = config.get("profile", {})
        if profile.get("name"):
            prompt_parts.append(profile["name"])
        if profile.get("industry"):
            prompt_parts.append(f"Industry: {profile['industry']}")
        if profile.get("job"):
            prompt_parts.append(f"Role: {profile['job']}")
        vocab_prompt = ". ".join(prompt_parts) if prompt_parts else None

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
                    # Combine custom vocab with profile-derived terms for Deepgram keywords
                    dg_keywords = list(vocab)
                    if profile.get("industry"):
                        dg_keywords.append(profile["industry"])
                    result = transcribe_deepgram(path, key, deepgram_language or "en", diarize=diarize, keywords=dg_keywords or None)
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
            with get_transcript_lock(session_id):
                transcript_path.write_text(json.dumps(t_data, indent=2, ensure_ascii=False))
            txt_path = TRANSCRIPTS_DIR / f"{session_id}.txt"
            txt_path.write_text(text, encoding="utf-8")

            # Auto-detect spoken tags from the beginning of the transcript
            spoken_tags = _apply_spoken_tags(session_id, text)

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
                        with get_transcript_lock(_bg_sid):
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

            resp = {
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
            if spoken_tags:
                resp["spoken_tags"] = spoken_tags
            return jsonify(resp)

        # Local transcription via Whisper
        vocab = config.get("custom_vocabulary", [])
        vocab_prompt = ", ".join(vocab) if vocab else None

        # Re-check the source file exists — user may have deleted it while cloud
        # providers were running, or a sync may have removed it. Whisper will
        # call ffmpeg which fails with an ugly 500 if the file is gone.
        if not Path(path).exists():
            return jsonify({"error": "Source audio file not found — it may have been deleted"}), 400

        from src.transcribe import transcribe as do_transcribe

        try:
            t = do_transcribe(str(path), model=model, language=language, diarize=diarize, initial_prompt=vocab_prompt)
        except RuntimeError as e:
            # whisper.load_audio wraps ffmpeg failures as RuntimeError — convert
            # file-missing failures into a clean 400 instead of a reported 500.
            msg = str(e)
            if "No such file" in msg or "Failed to load audio" in msg:
                return jsonify({"error": "Source audio file not found or unreadable"}), 400
            raise

        if t.text:
            with get_transcript_lock(session_id):
                t.save(str(transcript_path))
            txt_path = TRANSCRIPTS_DIR / f"{session_id}.txt"
            txt_path.write_text(t.formatted, encoding="utf-8")

            # Auto-detect spoken tags
            local_spoken_tags = _apply_spoken_tags(session_id, t.text)

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

            resp = {
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
            if local_spoken_tags:
                resp["spoken_tags"] = local_spoken_tags
            return jsonify(resp)
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
        with get_transcript_lock(session_id):
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
            if profile.get("name"):
                parts.append(f"User's name: {profile['name']}")
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

        # English variant preference
        eng_variant = config.get("english_variant", "us")
        if eng_variant == "gb":
            profile_context += "\n\nIMPORTANT: Use British English spelling and phrasing throughout (e.g. colour, organisation, summarise, centre, analysed)."

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

        # Safety net: if provider returned raw JSON string, try to parse it
        if summary_text and summary_text.strip().startswith("{"):
            try:
                parsed = json.loads(summary_text)
                if isinstance(parsed, dict):
                    summary_text = parsed.get("summary", summary_text)
                    if not title:
                        title = parsed.get("title", "")
            except json.JSONDecodeError:
                pass

        if not title:
            title = summary_text.split(".")[0].strip()
            if len(title) > 60:
                title = title[:57] + "..."
            if len(title) < 5:
                title = summary_text[:60].strip()

        with get_transcript_lock(session_id):
            # Re-read under the lock so any concurrent writes (e.g. diarization
            # completing while the LLM call was in flight) are not overwritten.
            t_data = json.loads(transcript_path.read_text())
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
                from app.routes.notion import push_to_notion

                result = push_to_notion(session_id, t_data=t_data, config=config)
                if result.get("error"):
                    notion_error = result["error"]
                else:
                    notion_url = result.get("url", "")
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
