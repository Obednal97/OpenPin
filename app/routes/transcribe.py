"""Transcription and summarization routes."""

import json
import logging
import os
import threading
import traceback
from pathlib import Path

import requests as http_requests
from flask import Blueprint, jsonify, request

from app.coaching import runner as coaching_runner
from app.coaching import store as coaching_store
from app.providers.summarize import generate_title, summarize
from app.providers.transcribe import transcribe_deepgram, transcribe_groq, transcribe_openai
from app.providers.validate import validate_key as do_validate_key
from app.utils.config import load_config, update_config
from app.utils.constants import TRANSCRIPTS_DIR
from app.utils.paths import (
    InvalidSessionIdError,
    PathOutsideError,
    is_auxiliary_stem,
    resolve_inside,
    validate_session_id,
)
from app.utils.speaker_names import drop_if_relabelled
from app.utils.transcript_locks import get_transcript_lock
from app.utils.webhooks import fire_webhooks


def _save_coaching_words(session_id, words, text):
    """Replace the recording's verbatim words (the coaching words file) after a transcription.

    Providers without verbatim words (Groq, OpenAI, local Whisper) pass []
    so fillers from an earlier Deepgram run are never reused. The words stay
    out of the transcript JSON, the .txt, the summary input and webhooks.
    """
    coaching_store.save_words(session_id, words or [], coaching_store.text_hash(text))


def _after_transcript_final(session_id, audio_path):
    """Start coaching once the transcript and its speaker labels are final.

    Called next to each ``transcribe`` webhook. Never raises: a coaching
    problem must not break transcription.
    """
    try:
        coaching_runner.start_coaching_async(session_id, audio_path)
    except Exception as e:
        logging.warning("Could not start coaching for %s: %s", session_id, e)


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

    def _apply(config):
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
        config.setdefault("recording_tags", {})[session_id] = merged
        config["custom_tags"] = sorted(custom_tags)

    update_config(_apply)
    logging.info(f"Auto-tagged {session_id} with spoken tags: {tags}")
    return tags


# Keys produced by a transcription run. Re-transcribing replaces these and
# keeps everything else in the transcript JSON (summary, auto_title, tags,
# custom_name, notion_page_url, ...).
_TRANSCRIPTION_KEYS = (
    "text",
    "duration",
    "language",
    "segments",
    "provider",
    "speakers",
    "timestamps_estimated",
    "diarization_status",
    "diarization_message",
)


def _write_text_atomic(path, text):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _merge_transcript(session_id, transcript_path, new_fields, txt_text=None):
    """Merge a fresh transcription into the existing transcript JSON.

    Must be called WITHOUT holding the session's transcript lock (it takes
    it). Replaces the transcription keys, preserves every other key, and sets
    ``summary_stale: true`` if a summary exists and the text changed. Speaker
    names are dropped (``speaker_names_cleared``) when the speakers were
    detected again with a different labelling, so a name never lands on the
    wrong person. Returns the merged dict.
    """
    transcript_path = Path(transcript_path)
    with get_transcript_lock(session_id):
        existing = {}
        if transcript_path.exists():
            try:
                loaded = json.loads(transcript_path.read_text())
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, ValueError) as e:
                logging.warning("Existing transcript for %s unreadable, replacing: %s", session_id, e)
        merged = {k: v for k, v in existing.items() if k not in _TRANSCRIPTION_KEYS}
        merged.update(new_fields)
        if existing.get("summary") and existing.get("text") != new_fields.get("text"):
            merged["summary_stale"] = True
        drop_if_relabelled(merged, existing.get("segments"))
        _write_text_atomic(transcript_path, json.dumps(merged, indent=2, ensure_ascii=False))
        if txt_text is not None:
            _write_text_atomic(transcript_path.with_suffix(".txt"), txt_text)
    # After releasing the transcript lock: the coaching store has its own lock.
    if existing.get("text") != new_fields.get("text"):
        coaching_store.mark_stale(session_id)
    return merged


def _error_json(base, exc):
    """Return error JSON with traceback for debugging (auto-reported to GitHub)."""
    base["error"] = str(exc)
    base["traceback"] = traceback.format_exc()
    return base


def _background_diarize(
    *, audio_path, session_id, transcript_path, segments, text, language, duration, provider, write_txt=False
):
    """Label speakers in a saved transcript on this Mac, then update its JSON.

    Runs after the transcript text is already saved (cloud or local Whisper),
    so a slow, hung or crashing diarization never costs the user their text.
    ``write_txt`` rewrites the .txt with speaker labels too (the local path
    saves a timestamped .txt; the cloud path saves plain text).

    Always leaves ``diarization_status`` as "done", "none" or "failed" (with a
    ``diarization_message`` for the user) so the frontend's poll can stop, and
    fires the ``transcribe`` webhook once at the end.
    """
    from src.transcribe import Transcript, TranscriptSegment, merge_transcript_with_diarization, run_diarization

    print(f"  [bg-diarize] Starting for {session_id}...")
    try:
        result = run_diarization(audio_path)
    except Exception as e:  # run_diarization doesn't raise; belt and braces
        logging.warning("[bg-diarize] crashed for %s: %s", session_id, e)
        result = None

    fields = {
        "diarization_status": "failed",
        "diarization_message": "Speaker labels skipped: speaker detection crashed",
    }
    webhook_segments = segments
    speakers = []
    t_obj = None
    if result is not None and result.status != "done":
        fields = {"diarization_status": result.status, "diarization_message": result.message}
    elif result is not None:
        try:
            t_obj = Transcript(text=text, language=language, duration=duration)
            for s in segments:
                t_obj.segments.append(TranscriptSegment(start=s["start"], end=s["end"], text=s["text"]))
            t_obj = merge_transcript_with_diarization(t_obj, result.segments)
        except Exception as e:
            # A bad merge must still end the job (and the frontend's poll).
            logging.warning("[bg-diarize] merge failed for %s: %s", session_id, e)
            t_obj = None
            fields["diarization_message"] = "Speaker labels skipped: could not match speakers to the text"
        if t_obj is not None:
            webhook_segments = [
                {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker} for s in t_obj.segments
            ]
            speakers = sorted({s.speaker for s in t_obj.segments if s.speaker})
            fields = {
                "diarization_status": result.status,
                "diarization_message": result.message,
                "segments": webhook_segments,
                "speakers": speakers,
                "provider": f"{provider}+{result.engine}",
            }

    tp = Path(transcript_path)
    try:
        with get_transcript_lock(session_id):
            td = json.loads(tp.read_text())
            if td.get("text") != text:
                # Re-transcribed (or edited) while this ran: don't clobber it.
                print(f"  [bg-diarize] {session_id} changed meanwhile, result dropped")
                return
            old_segments = td.get("segments")
            td.update(fields)
            if "segments" in fields:
                drop_if_relabelled(td, old_segments)
            _write_text_atomic(tp, json.dumps(td, indent=2, ensure_ascii=False))
            if write_txt and t_obj is not None:
                _write_text_atomic(tp.with_suffix(".txt"), t_obj.formatted)
    except (OSError, ValueError) as e:
        logging.warning("[bg-diarize] Could not update transcript %s: %s", session_id, e)
        return
    print(f"  [bg-diarize] {session_id}: {fields['diarization_status']} ({len(speakers)} speaker(s))")

    payload = {
        "session_id": session_id,
        "text": text[:500],
        "duration": duration,
        "language": language,
        "segments": webhook_segments,
    }
    if speakers:
        payload["speakers"] = speakers
    fire_webhooks("transcribe", payload)
    _after_transcript_final(session_id, audio_path)


def _plan_local_diarization(t_data):
    """Set the initial diarization status on a transcript about to be saved.

    Returns ``(run_background, message)``: either the background job should
    start ("running"), or it's known it can't run and ``message`` says why
    ("failed"). Never runs the model itself, so it can't block a request.
    """
    from src.transcribe import known_unavailable_reason

    reason = known_unavailable_reason()
    if reason:
        t_data["diarization_status"] = "failed"
        t_data["diarization_message"] = reason
        return False, reason
    t_data["diarization_status"] = "running"
    return True, ""


def _start_background_diarize(**kwargs):
    threading.Thread(target=_background_diarize, kwargs=kwargs, daemon=True).start()


bp = Blueprint("transcribe", __name__)


def _auto_title(session_id, transcript_path, text, segments):
    """Name a freshly transcribed recording when no summary will.

    Skipped when auto-summarise is on (the summary brings a better title), when
    the recording already has a name or title, or when no AI provider answers.
    The title is marked as transcript-made, so a later summary may replace it;
    a name the user typed is never replaced. Returns the title or "".
    """
    try:
        config = load_config()
        if config.get("auto_summarise"):
            return ""
        if (config.get("recording_names") or {}).get(session_id):
            return ""
        with get_transcript_lock(session_id):
            if json.loads(transcript_path.read_text()).get("auto_title"):
                return ""
        title, provider = generate_title(text, config, segments=segments)
        if not title:
            return ""
        with get_transcript_lock(session_id):
            t_data = json.loads(transcript_path.read_text())
            if t_data.get("auto_title"):
                return ""
            t_data["auto_title"] = title
            t_data["auto_title_source"] = "transcript"
            _write_text_atomic(transcript_path, json.dumps(t_data, indent=2, ensure_ascii=False))

        def _set_name(cfg):
            names = cfg.setdefault("recording_names", {})
            if not names.get(session_id):
                names[session_id] = title

        update_config(_set_name)
        logging.info(f"Auto title for {session_id} from {provider}")
        return title
    except Exception as e:
        logging.warning(f"Auto title for {session_id} failed: {e}")
        return ""


@bp.route("/transcribe", methods=["POST"])
def transcribe_file():
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"error": "No path"}), 400

    # Even an authenticated caller (every request carries the per-launch
    # X-Plaud-Token) must not be able to submit any path on disk: without
    # containment, /transcribe would ship arbitrary readable files (e.g.
    # ~/.ssh/id_rsa) to a cloud transcription provider. Resolve symlinks and
    # reject anything outside the known recordings dirs.
    from app.utils.constants import CLOUD_RECORDINGS_DIR, get_all_recordings_dirs

    if not isinstance(path, str) or not Path(path).is_absolute():
        return jsonify({"error": "Path must be absolute"}), 400
    allowed_dirs = [*get_all_recordings_dirs(), CLOUD_RECORDINGS_DIR]
    try:
        resolved = resolve_inside(allowed_dirs, path, must_exist=True)
    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 400
    except PathOutsideError:
        return jsonify({"error": "Path is outside the recordings directories"}), 400
    if not resolved.is_file():
        return jsonify({"error": "File not found"}), 400
    try:
        validate_session_id(resolved.stem)
    except InvalidSessionIdError as e:
        return jsonify({"error": str(e)}), 400
    if is_auxiliary_stem(resolved.stem):
        return jsonify({"error": "Not a recording (crop backup/temp file)"}), 400
    path = str(resolved)

    config = load_config()
    model = data.get("model", config.get("whisper_model", "small"))
    diarize = data.get("diarize", config.get("diarize", False))
    # "auto" (the Settings default) and empty mean auto-detect: None is passed
    # to every provider so none of them forces English.
    language = data.get("language") or config.get("language") or "auto"
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
                    result = transcribe_deepgram(
                        path, key, deepgram_language, diarize=diarize, keywords=dg_keywords or None
                    )
                    _cloud_has_speakers = bool(result.get("speakers"))
                elif name == "groq":
                    result = transcribe_groq(path, key, language, vocab_prompt=vocab_prompt)
                elif name == "openai":
                    result = transcribe_openai(
                        path, key, language, vocab_prompt=vocab_prompt, keywords=list(vocab) or None
                    )
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
            verbatim_words = result.pop("words", None) or []
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
            lang = result.get("language") or language or ""
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
                    {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker} for s in t_obj.segments
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
            if result.get("timestamps_estimated"):
                t_data["timestamps_estimated"] = True

            # Speaker labels: Deepgram adds them while transcribing; for any
            # other provider the local model runs in the background.
            run_local_diarization = False
            diarization_message = ""
            if diarize and text:
                if _cloud_has_speakers:
                    t_data["diarization_status"] = "done"
                else:
                    run_local_diarization, diarization_message = _plan_local_diarization(t_data)
            _merge_transcript(session_id, transcript_path, t_data, txt_text=text)
            _save_coaching_words(session_id, verbatim_words, text)

            # Auto-detect spoken tags from the beginning of the transcript
            spoken_tags = _apply_spoken_tags(session_id, text)

            if run_local_diarization:
                _start_background_diarize(
                    audio_path=str(path),
                    session_id=str(session_id),
                    transcript_path=str(transcript_path),
                    segments=list(segments),
                    text=str(text),
                    language=str(lang),
                    duration=float(duration),
                    provider=prov,
                )
            else:
                # No background diarization: fire the webhook now
                fire_webhooks(
                    "transcribe",
                    {
                        "session_id": session_id,
                        "text": text[:500],
                        "duration": duration,
                        "language": lang,
                        "speakers": speakers,
                        "segments": segments,
                    },
                )
                _after_transcript_final(session_id, str(path))

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
                # True only while the background job runs; the frontend polls
                # the transcript's diarization_status until it isn't "running".
                "diarizing": run_local_diarization,
            }
            if diarization_message:
                resp["diarization_message"] = diarization_message
            if spoken_tags:
                resp["spoken_tags"] = spoken_tags
            title = _auto_title(session_id, transcript_path, text, segments)
            if title:
                resp["auto_title"] = title
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
            # Whisper only: speaker labels run in the background below, after
            # the text is saved, exactly as on the cloud path. Inline they
            # could wait on another diarization, a pip install or a model
            # download, and a crash there would lose the finished text.
            t = do_transcribe(str(path), model=model, language=language, diarize=False, initial_prompt=vocab_prompt)
        except RuntimeError as e:
            # whisper.load_audio wraps ffmpeg failures as RuntimeError — convert
            # file-missing failures into a clean 400 instead of a reported 500.
            msg = str(e)
            if "No such file" in msg or "Failed to load audio" in msg:
                return jsonify({"error": "Source audio file not found or unreadable"}), 400
            raise

        if t.text:
            local_segments = [
                {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker} for s in t.segments
            ]
            # Same fields Transcript.save() writes, merged into the existing JSON
            local_fields = {
                "text": t.text,
                "language": t.language,
                "duration": t.duration,
                "segments": local_segments,
                "provider": "local",
            }
            run_local_diarization = False
            diarization_message = ""
            if diarize:
                run_local_diarization, diarization_message = _plan_local_diarization(local_fields)
            _merge_transcript(session_id, transcript_path, local_fields, txt_text=t.formatted)
            _save_coaching_words(session_id, [], t.text)

            # Auto-detect spoken tags
            local_spoken_tags = _apply_spoken_tags(session_id, t.text)

            if run_local_diarization:
                _start_background_diarize(
                    audio_path=str(path),
                    session_id=str(session_id),
                    transcript_path=str(transcript_path),
                    segments=list(local_segments),
                    text=str(t.text),
                    language=str(t.language or ""),
                    duration=float(t.duration or 0),
                    provider="local",
                    write_txt=True,
                )
            else:
                # No background diarization: fire the webhook now
                fire_webhooks(
                    "transcribe",
                    {
                        "session_id": session_id,
                        "text": t.text[:500],
                        "duration": t.duration,
                        "language": t.language,
                        "speakers": [],
                        "segments": local_segments,
                    },
                )
                _after_transcript_final(session_id, str(path))

            resp = {
                "session_id": session_id,
                "text": t.text,
                "duration": t.duration,
                "language": t.language,
                "speakers": [],
                "segments": local_segments,
                "path": str(transcript_path),
                "provider": "local",
                "warning": _cloud_error,
                # Same contract as the cloud path: the frontend polls the
                # transcript's diarization_status while this is True.
                "diarizing": run_local_diarization,
            }
            if diarization_message:
                resp["diarization_message"] = diarization_message
            if local_spoken_tags:
                resp["spoken_tags"] = local_spoken_tags
            title = _auto_title(session_id, transcript_path, t.text, local_segments)
            if title:
                resp["auto_title"] = title
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
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as e:
        return jsonify({"error": str(e)}), 400

    transcript_path = TRANSCRIPTS_DIR / f"{session_id}.json"
    if not transcript_path.exists():
        return jsonify({"error": "Transcript not found"}), 404

    try:
        with get_transcript_lock(session_id):
            t_data = json.loads(transcript_path.read_text())
        full_text = t_data.get("text", "")
        if not full_text or len(full_text.strip()) < 20:
            return jsonify({"error": "Transcript too short to summarize"}), 400

        # Read-only snapshot for building the prompt. Never saved back: the AI
        # call below can take minutes and other writers must not be undone.
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
        summary_text, title, provider, errors = summarize(
            full_text, config, extra_context, segments=t_data.get("segments")
        )

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
            # A title made from the transcript alone (_auto_title) may be
            # replaced by the summary's, unless the user has renamed it since.
            transcript_title = t_data.get("auto_title") if t_data.get("auto_title_source") == "transcript" else ""
            t_data["summary"] = summary_text
            t_data["auto_title"] = title
            t_data.pop("auto_title_source", None)
            # Stale only if the transcript changed while the AI call ran.
            if t_data.get("text", "") != full_text:
                t_data["summary_stale"] = True
            else:
                t_data.pop("summary_stale", None)
            _write_text_atomic(transcript_path, json.dumps(t_data, indent=2, ensure_ascii=False))

        def _set_default_name(cfg):
            names = cfg.setdefault("recording_names", {})
            current = names.get(session_id)
            if not current or (transcript_title and current == transcript_title):
                names[session_id] = title

        # Fresh, locked read-modify-write; also gives Notion the current config.
        config = update_config(_set_default_name)

        fire_webhooks(
            "summarize", {"session_id": session_id, "title": title, "summary": summary_text, "provider": provider}
        )

        # Auto-push to Notion if enabled: handed to the persistent queue (same
        # retry/backoff as other pushes), so a failure is retried, not lost,
        # and this response doesn't wait on Notion.
        notion_url = ""
        notion_error = ""
        notion_queued = False
        if config.get("notion_auto_push"):
            from app.routes.notion import enqueue_auto_push

            queued = enqueue_auto_push(session_id)
            notion_url = queued["notion_url"]
            notion_error = queued["notion_error"]
            notion_queued = queued["notion_queued"]

        return jsonify(
            {
                "summary": summary_text,
                "title": title,
                "provider": provider,
                "notion_url": notion_url,
                "notion_error": notion_error,
                "notion_queued": notion_queued,
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
