"""Recordings and transcripts routes."""

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

from app.coaching import store as coaching_store
from app.people import embeddings as voice_embeddings
from app.state import speakers
from app.utils import redownload_backup
from app.utils import speaker_names as sn
from app.utils import tags as tags_store
from app.utils.config import _config_lock, load_config, save_config, update_config
from app.utils.constants import TRANSCRIPTS_DIR, get_all_recordings_dirs, get_recordings_dir
from app.utils.paths import (
    InvalidSessionIdError,
    PathOutsideError,
    is_auxiliary_stem,
    resolve_inside,
    validate_session_id,
    validate_session_ids,
)
from app.utils.recording_utils import extract_recording_date
from app.utils.system import ensure_ffmpeg
from app.utils.transcript_locks import get_transcript_lock
from app.utils.trash import APP_TRASH_DIR, move_to_trash

bp = Blueprint("recordings", __name__)

NO_SUCH_RECORDING = "No recording with that id"


def _invalid(e):
    """400 response for a rejected session id / path."""
    return jsonify({"error": str(e) or "Invalid session_id"}), 400


def _find_recording(session_id, ext="ogg"):
    """Find a recording file across all known directories.

    Returns None for an invalid session id or a file that resolves (via
    symlink) outside the recordings directories.
    """
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError:
        return None
    dirs = get_all_recordings_dirs()
    for d in dirs:
        p = d / f"{session_id}.{ext}"
        if p.exists():
            try:
                resolve_inside(dirs, p, must_exist=True)
            except (PathOutsideError, FileNotFoundError):
                continue
            return p
    return None


def recover_metadata_from_transcripts():
    """Recover tags, custom names, and auto_titles from transcript files.

    Runs on startup — if the config was reset/wiped, this restores
    metadata that was persisted in the transcript JSON files.
    """
    if not TRANSCRIPTS_DIR.exists():
        return

    with _config_lock:
        _recover_metadata_locked()


def _recover_metadata_locked():
    config = load_config()
    rec_tags = config.get("recording_tags", {})
    rec_names = config.get("recording_names", {})
    notion_urls = config.get("notion_page_urls", {})
    custom_tags = set(config.get("custom_tags", []))
    recovered = 0

    for f in TRANSCRIPTS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            sid = f.stem

            # Recover tags
            tags = data.get("tags", [])
            if tags and sid not in rec_tags:
                rec_tags[sid] = tags
                recovered += 1
                # Discover custom tags not in defaults
                from app.utils.constants import DEFAULT_TAGS

                for t in tags:
                    if t not in DEFAULT_TAGS:
                        custom_tags.add(t)

            # Recover custom name
            name = data.get("custom_name", "")
            if name and sid not in rec_names:
                rec_names[sid] = name
                recovered += 1

            # Recover auto_title as name if no custom name
            auto_title = data.get("auto_title", "")
            if auto_title and sid not in rec_names and not name:
                rec_names[sid] = auto_title

            # Recover Notion page URL
            notion_url = data.get("notion_page_url", "")
            if notion_url and sid not in notion_urls:
                notion_urls[sid] = notion_url
                recovered += 1

        except Exception:
            pass

    # Recover deleted_ids from .deleted marker files
    dismissed = set(config.get("deleted_ids", []))
    for rec_dir in get_all_recordings_dirs():
        for marker in rec_dir.glob("*.deleted"):
            sid = marker.stem
            if sid not in dismissed:
                dismissed.add(sid)
                recovered += 1

    if recovered > 0:
        config["recording_tags"] = rec_tags
        config["recording_names"] = rec_names
        config["notion_page_urls"] = notion_urls
        config["custom_tags"] = list(custom_tags)
        config["deleted_ids"] = list(dismissed)
        save_config(config)
        print(f"[Recovery] Recovered {recovered} metadata entries from transcript/marker files")


def init_recordings():
    """Explicit startup hook — call once from api_server after blueprint registration.

    Runs recover_metadata_from_transcripts() without triggering the scan at
    module import time, avoiding synchronous filesystem I/O during blueprint
    loading on machines with many transcripts.
    """
    recover_metadata_from_transcripts()


def recording_exists(session_id) -> bool:
    """True when ``session_id`` is a recording the app lists (see ``collect_recordings``).

    That is: an audio file in a recordings folder, an interrupted download, a copy set
    aside for re-download, or a recording only in Google Drive. False for an invalid id
    (checked before any path is built), a crop backup, or a transcript with no recording.
    Shared by the app's tag routes and the AI tool routes (/mcp/v1), so neither writes
    tags or names for a recording that doesn't exist.
    """
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError:
        return False
    if is_auxiliary_stem(session_id):
        return False
    if _find_recording(session_id) is not None:
        return True
    for rec_dir in get_all_recordings_dirs():
        if any((rec_dir / f"{session_id}{suffix}").exists() for suffix in _PARTIAL_SUFFIXES):
            return True
    try:
        deleted = set(load_config().get("deleted_ids", []))
        if session_id not in deleted and redownload_backup.backup_ogg_path(session_id) is not None:
            return True
    except Exception:
        pass
    try:
        from app.state.gdrive import get_cloud_only_recordings

        return any(cr.get("session_id") == session_id for cr in get_cloud_only_recordings())
    except Exception:
        return False


@bp.route("/recordings")
def list_recordings():
    return jsonify(collect_recordings())


def collect_recordings() -> list[dict]:
    """Every recording the app lists, newest first: what ``GET /recordings`` returns.

    The AI tool routes (app/mcp/library.py) build on this, so the window and AI tools
    see the same recordings.
    """
    config = load_config()
    custom_names = config.get("recording_names", {})
    rec_tags = config.get("recording_tags", {})
    recordings = []

    # Aggregate from all known recording directories
    all_dirs = get_all_recordings_dirs()
    complete_ids = set()

    for rec_dir in all_dirs:
        rec_dir.mkdir(exist_ok=True)
        for f in sorted(rec_dir.glob("*.ogg"), reverse=True):
            if is_auxiliary_stem(f.stem):
                continue  # crop backup / temp file, not a recording
            if f.stem in complete_ids:
                continue  # Already seen from another directory
            complete_ids.add(f.stem)
            size = f.stat().st_size
            raw_audio_size = max(size - 2000, 0)
            duration = raw_audio_size / 80 * 0.02

            rec_date = extract_recording_date(f.stem)
            if rec_date is None:
                rec_date = datetime.fromtimestamp(f.stat().st_mtime)

            transcript_path = TRANSCRIPTS_DIR / f"{f.stem}.json"
            has_transcript = transcript_path.exists()

            is_cloud = f.stem.startswith("cloud_") or f.stem in set(config.get("uploaded_session_ids", []))
            cloud_id = config.get("cloud_file_ids", {}).get(f.stem, "")

            recordings.append(
                {
                    "session_id": f.stem,
                    "ogg_path": str(f),
                    "duration": round(duration, 1),
                    "size": size,
                    "date": rec_date.isoformat(),
                    "has_transcript": has_transcript,
                    "title": custom_names.get(f.stem, ""),
                    "tags": rec_tags.get(f.stem, []),
                    "cloud_synced": is_cloud,
                    "cloud_id": cloud_id,
                }
            )

    # Recordings with a re-download backup (see the backup loop below). Deleted
    # ids are left out: their backup belongs in the Trash, not the list.
    deleted_ids = set(config.get("deleted_ids", []))
    try:
        pending_backups = {sid for sid in redownload_backup.list_redownload_backups() if sid not in deleted_ids}
    except Exception:
        pending_backups = set()

    # Include partial downloads (interrupted transfers) — shown with warning in UI
    for rec_dir in all_dirs:
        partial_files = list(rec_dir.glob("*.ogg.partial")) + list(rec_dir.glob("*_partial.bin"))
        for f in sorted(partial_files, key=lambda p: p.name, reverse=True):
            stem = f.name.removesuffix(".ogg.partial").removesuffix("_partial.bin")
            if stem in complete_ids:
                try:
                    f.unlink()
                except Exception:
                    pass
                continue
            complete_ids.add(stem)
            size = f.stat().st_size
            rec_date = extract_recording_date(stem)
            if rec_date is None:
                rec_date = datetime.fromtimestamp(f.stat().st_mtime)
            entry = {
                "session_id": stem,
                "ogg_path": None,
                "duration": 0,
                "size": size,
                "date": rec_date.isoformat(),
                "has_transcript": False,
                "title": custom_names.get(stem, ""),
                "tags": [],
                "cloud_synced": False,
                "cloud_id": "",
                "partial": True,
            }
            # A re-download that was interrupted: the previous copy is still in
            # .redownload_backup/, so the UI must offer "Restore previous copy"
            # (the backup loop below skips this sid as it is already listed).
            if stem in pending_backups:
                entry["redownload_pending"] = True
            recordings.append(entry)

    # Recordings whose local copy was moved aside by /recordings/redownload and
    # not yet re-downloaded. Shown so they never silently vanish; the UI can
    # offer POST /recordings/restore-redownload.
    try:
        for sid in sorted(pending_backups):
            if sid in complete_ids:
                continue
            complete_ids.add(sid)
            bak = redownload_backup.backup_ogg_path(sid)
            size = bak.stat().st_size if bak else 0
            rec_date = extract_recording_date(sid)
            if rec_date is None:
                rec_date = datetime.fromtimestamp(bak.stat().st_mtime) if bak else datetime.now()
            recordings.append(
                {
                    "session_id": sid,
                    "ogg_path": None,
                    "duration": round(max(size - 2000, 0) / 80 * 0.02, 1),
                    "size": size,
                    "date": rec_date.isoformat(),
                    "has_transcript": (TRANSCRIPTS_DIR / f"{sid}.json").exists(),
                    "title": custom_names.get(sid, ""),
                    "tags": rec_tags.get(sid, []),
                    "cloud_synced": False,
                    "cloud_id": "",
                    "redownload_pending": True,
                }
            )
    except Exception:
        pass

    # Include cloud-only recordings (uploaded to Drive but no local file)
    try:
        from app.state.gdrive import get_cloud_only_recordings

        for cr in get_cloud_only_recordings():
            sid = cr["session_id"]
            if sid in complete_ids:
                continue
            transcript_path = TRANSCRIPTS_DIR / f"{sid}.json"
            rec_date = extract_recording_date(sid)
            recordings.append(
                {
                    "session_id": sid,
                    "ogg_path": None,
                    "duration": 0,
                    "size": cr.get("file_size", 0),
                    "date": (rec_date or cr.get("synced_at", "")).isoformat() if rec_date else cr.get("synced_at", ""),
                    "has_transcript": transcript_path.exists(),
                    "title": custom_names.get(sid, ""),
                    "tags": rec_tags.get(sid, []),
                    "cloud_synced": True,
                    "cloud_id": cr.get("drive_file_id", ""),
                    "cloud_only": True,
                }
            )
    except Exception:
        pass

    profile = speakers.profile_name() or ""
    for r in recordings:
        try:
            r["participants"] = speakers.participants(r["session_id"], profile) if r.get("has_transcript") else []
        except Exception:
            r["participants"] = []

    recordings.sort(key=lambda r: r["date"], reverse=True)
    return recordings


def _change_tags(op):
    """Shared body of /recordings/tags/add and /recordings/tags/remove.

    ``{session_ids: [...], tags: [...]}`` -> ``{tags: {sid: [...]}}``, plus
    ``errors: {sid: message}`` for recordings that couldn't take the change
    (say, already at 20 tags, or no such recording). 400 if the input is bad or
    nothing could change; 404 if none of the recordings exists.
    """
    data = request.get_json(silent=True) or {}
    session_ids = data.get("session_ids")
    tags = data.get("tags")
    try:
        validate_session_ids(session_ids)
    except InvalidSessionIdError as e:
        return _invalid(e)
    if not session_ids:
        return jsonify({"error": "No session_ids"}), 400
    if not isinstance(tags, list) or not tags:
        return jsonify({"error": "tags must be a non-empty list"}), 400

    result, errors, missing = {}, {}, set()
    for sid in dict.fromkeys(session_ids):
        if not recording_exists(sid):
            errors[sid] = NO_SUCH_RECORDING
            missing.add(sid)
            continue
        try:
            result[sid] = op(sid, tags)
        except ValueError as e:
            errors[sid] = str(e)
    if not result:
        status = 404 if missing == set(errors) else 400
        return jsonify({"error": next(iter(errors.values())), "errors": errors}), status
    body = {"tags": result}
    if errors:
        body["errors"] = errors
    return jsonify(body)


@bp.route("/recordings/tags/add", methods=["POST"])
def add_recording_tags():
    """Add tags to one or more recordings (new tags join the picker)."""
    return _change_tags(tags_store.add_tags)


@bp.route("/recordings/tags/remove", methods=["POST"])
def remove_recording_tags():
    """Remove tags from one or more recordings."""
    return _change_tags(tags_store.remove_tags)


@bp.route("/tags", methods=["GET"])
def list_tags():
    """Every tag with how many recordings carry it."""
    return jsonify({"tags": tags_store.all_tags()})


@bp.route("/recordings/tag", methods=["POST"])
def tag_recording():
    """Set a recording's whole tag list.

    Kept for compatibility only: the app adds and removes single tags through
    /recordings/tags/add and /recordings/tags/remove, so a stale window can't
    wipe a tag something else added.
    """
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    tags = data.get("tags", [])
    if not session_id:
        return jsonify({"error": "No session_id"}), 400
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as e:
        return _invalid(e)
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return jsonify({"error": "tags must be a list of strings"}), 400

    update_config(lambda c: c.setdefault("recording_tags", {}).__setitem__(session_id, tags))

    # Also persist tags in the transcript JSON for recovery after config resets
    transcript_path = TRANSCRIPTS_DIR / f"{session_id}.json"
    if transcript_path.exists():
        try:
            import json as _json

            with get_transcript_lock(session_id):
                t_data = _json.loads(transcript_path.read_text())
                t_data["tags"] = tags
                transcript_path.write_text(_json.dumps(t_data, indent=2, ensure_ascii=False))
        except Exception:
            pass

    from app.utils import changes

    changes.bump()
    notion_queued = tags_store.enqueue_notion_sync(session_id)

    return jsonify({"saved": True, "notion_queued": notion_queued})


@bp.route("/recordings/rename", methods=["POST"])
def rename_recording():
    """Set a custom title for a recording."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    title = data.get("title", "").strip()
    if not session_id:
        return jsonify({"error": "No session_id"}), 400
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as e:
        return _invalid(e)

    def _apply(cfg):
        names = cfg.setdefault("recording_names", {})
        if title:
            names[session_id] = title
        else:
            names.pop(session_id, None)

    update_config(_apply)

    # Also persist name in the transcript JSON for recovery after config resets
    transcript_path = TRANSCRIPTS_DIR / f"{session_id}.json"
    if transcript_path.exists():
        try:
            import json as _json

            with get_transcript_lock(session_id):
                t_data = _json.loads(transcript_path.read_text())
                if title:
                    t_data["custom_name"] = title
                else:
                    t_data.pop("custom_name", None)
                transcript_path.write_text(_json.dumps(t_data, indent=2, ensure_ascii=False))
        except Exception:
            pass

    # Rename reaches Notion as a properties-only update (never raises).
    from app.routes.notion import enqueue_property_sync

    return jsonify({"saved": True, "notion_queued": enqueue_property_sync(session_id)})


@bp.route("/recordings/export", methods=["POST"])
def export_recording():
    """Convert recording to MP3 and save to Downloads."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id:
        return jsonify({"error": "No session_id"}), 400
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as e:
        return _invalid(e)

    ogg_path = _find_recording(session_id)
    if not ogg_path:
        return jsonify({"error": "Recording not found"}), 404

    downloads_dir = Path.home() / "Downloads"
    config = load_config()
    custom_name = config.get("recording_names", {}).get(session_id, session_id)
    safe_name = custom_name.replace("/", "-").replace(":", "-").replace("\\", "-")
    mp3_path = downloads_dir / f"{safe_name}.mp3"

    # Ensure FFmpeg is available (downloads on first use if needed)
    ffmpeg = ensure_ffmpeg()
    try:
        result = subprocess.run(
            [ffmpeg or "ffmpeg", "-y", "-i", str(ogg_path), "-codec:a", "libmp3lame", "-q:a", "2", str(mp3_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return jsonify({"path": str(mp3_path), "size": mp3_path.stat().st_size})
        else:
            ogg_export = downloads_dir / f"{safe_name}.ogg"
            shutil.copy2(str(ogg_path), str(ogg_export))
            return jsonify(
                {
                    "path": str(ogg_export),
                    "size": ogg_export.stat().st_size,
                    "note": "Exported as OGG (ffmpeg not available for MP3 conversion)",
                }
            )
    except FileNotFoundError:
        ogg_export = downloads_dir / f"{safe_name}.ogg"
        shutil.copy2(str(ogg_path), str(ogg_export))
        return jsonify(
            {
                "path": str(ogg_export),
                "size": ogg_export.stat().st_size,
                "note": "Exported as OGG (install ffmpeg for MP3)",
            }
        )


@bp.route("/recordings/crop", methods=["POST"])
def crop_recording():
    """Crop a recording to a time range using FFmpeg."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    start = data.get("start", 0)
    end = data.get("end", 0)
    mode = data.get("mode", "overwrite")  # "overwrite" or "new"

    if not session_id:
        return jsonify({"error": "No session_id"}), 400
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as e:
        return _invalid(e)
    if not _is_number(start) or not _is_number(end):
        return jsonify({"error": "Invalid time range"}), 400
    if end <= start:
        return jsonify({"error": "Invalid time range"}), 400

    ogg_path = _find_recording(session_id)
    if not ogg_path:
        return jsonify({"error": "Recording not found"}), 404

    rec_dir = ogg_path.parent
    if mode == "new":
        new_id = str(int(datetime.now().timestamp()))
        out_path = rec_dir / f"{new_id}.ogg"
    else:
        new_id = None
        # Back up original before overwriting
        backup_path = rec_dir / f"{session_id}_uncropped.ogg"
        if not backup_path.exists():
            shutil.copy2(str(ogg_path), str(backup_path))
        out_path = rec_dir / f"{session_id}_crop_tmp.ogg"

    ffmpeg = ensure_ffmpeg()
    try:
        result = subprocess.run(
            [
                ffmpeg or "ffmpeg",
                "-y",
                "-i",
                str(ogg_path),
                "-ss",
                str(start),
                "-to",
                str(end),
                "-c",
                "copy",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            out_path.unlink(missing_ok=True)
            return jsonify({"error": f"FFmpeg error: {result.stderr[:200]}"}), 500

        if mode == "new":
            # Save a title for the new recording
            def _name_crop(cfg):
                names = cfg.setdefault("recording_names", {})
                names[new_id] = f"{names.get(session_id, session_id)} (cropped)"

            update_config(_name_crop)
            return jsonify(
                {
                    "cropped": True,
                    "session_id": new_id,
                    "duration": round(end - start, 2),
                }
            )
        else:
            out_path.replace(ogg_path)
            # The transcript no longer matches the audio — move it to the
            # Trash (not unlink) so a summary/notes can still be recovered.
            with get_transcript_lock(session_id):
                _trash_files([TRANSCRIPTS_DIR / f"{session_id}{ext}" for ext in (".json", ".txt")])
            return jsonify(
                {
                    "cropped": True,
                    "session_id": session_id,
                    "duration": round(end - start, 2),
                }
            )
    except FileNotFoundError:
        return jsonify({"error": "ffmpeg not found — install ffmpeg to crop audio"}), 500
    except subprocess.TimeoutExpired:
        out_path.unlink(missing_ok=True)
        return jsonify({"error": "Crop timed out"}), 500


# Files an interrupted BLE / Wi-Fi transfer leaves next to the recording.
_PARTIAL_SUFFIXES = ("_partial.bin", ".ogg.partial")


def _trash_files(paths):
    """Move each existing path to the Trash.

    Returns (trashed_paths, locations, errors). ``locations`` is the set of
    "system"/"app" values from move_to_trash.
    """
    trashed, locations, errors = [], set(), []
    for p in paths:
        if not (p.exists() or p.is_symlink()):
            continue
        try:
            locations.add(move_to_trash(p))
            trashed.append(str(p))
        except OSError as e:
            errors.append({"path": str(p), "error": str(e)})
    return trashed, locations, errors


def _trash_coaching_files(session_id):
    """Move a recording's coaching data (sidecar and verbatim words) to the Trash.

    Takes the coaching store's own lock, so call it after releasing the transcript lock.
    """
    with coaching_store.session_lock(session_id):
        result = _trash_files(coaching_store.session_files(session_id))
    voice_embeddings.delete(session_id)  # derived data: plain delete, not trash
    return result


def _trash_location(locations):
    if not locations:
        return None
    if locations == {"system"}:
        return "system"
    if locations == {"app"}:
        return "app"
    return "mixed"


@bp.route("/recordings/delete", methods=["POST"])
def delete_recordings():
    """Move one or more recordings and their transcripts to the Trash.

    Response: {"deleted": [paths], "count": n, "trashed": bool,
    "trash_location": "system"|"app"|"mixed"|null, "errors": [...]}.
    ``trashed`` is true when every file found was moved to the macOS Trash
    (or, as a fallback, DATA_DIR/.trash) — nothing is unlinked.
    """
    data = request.get_json(silent=True) or {}
    session_ids = data.get("session_ids", [])
    if not session_ids:
        return jsonify({"error": "No session_ids provided"}), 400
    try:
        session_ids = validate_session_ids(session_ids)
    except InvalidSessionIdError as e:
        return _invalid(e)

    deleted, locations, errors = [], set(), []
    gone = []  # session ids whose files are all out of the way
    for sid in session_ids:
        # Write .deleted marker in active dir so config reset doesn't re-sync
        try:
            (get_recordings_dir() / f"{sid}.deleted").write_text("")
        except Exception:
            pass
        paths = [rec_dir / f"{sid}.{ext}" for rec_dir in get_all_recordings_dirs() for ext in ("ogg", "mp3", "raw")]
        # Interrupted/undecryptable BLE downloads keep their raw data next to the .ogg
        paths += [
            rec_dir / f"{sid}{suffix}"
            for rec_dir in get_all_recordings_dirs()
            for suffix in ("_partial.bin", ".ogg.partial", "_encrypted.bin")
        ]
        paths += [TRANSCRIPTS_DIR / f"{sid}.{ext}" for ext in ("json", "txt")]
        # A copy set aside by "Re-download" belongs to this recording too: trash
        # it as well, or the card would come back as "Waiting for re-download".
        paths += redownload_backup.backup_files(sid)
        with get_transcript_lock(sid):
            t, loc, errs = _trash_files(paths)
            if not errs:
                redownload_backup.remove_backup_dir(sid)
        deleted += t
        locations |= loc
        errors += [{"session_id": sid, **e} for e in errs]
        if not errs:
            gone.append(sid)
        # Coaching data has its own lock, never taken inside the transcript lock. A failure
        # here is reported, but the recording itself is gone (and the progress view skips
        # coaching data without a transcript).
        ct, cloc, cerrs = _trash_coaching_files(sid)
        deleted += ct
        locations |= cloc
        errors += [{"session_id": sid, **e} for e in cerrs]

    def _apply(cfg):
        dismissed = set(cfg.get("deleted_ids", []))
        for sid in gone:
            dismissed.add(sid)
            for key in ("recording_names", "recording_tags"):
                cfg.get(key, {}).pop(sid, None)
        cfg["deleted_ids"] = list(dismissed)

    update_config(_apply)
    return jsonify(
        {
            "deleted": deleted,
            "count": len(gone),
            "trashed": not errors,
            "trash_location": _trash_location(locations),
            "errors": errors,
        }
    )


@bp.route("/recordings/redownload", methods=["POST"])
def redownload_recording():
    """Mark a recording for re-download.

    The local .ogg / _encrypted.bin / .ogg.partial are MOVED to
    DATA_DIR/.redownload_backup/<sid>/ (not deleted), so the recording is not
    lost if the device no longer has it. The next sync that writes a fresh
    {sid}.ogg should call redownload_backup.finalize_redownload_backup(sid);
    POST /recordings/restore-redownload puts the backup back.
    """
    data = request.get_json(silent=True) or {}
    session_ids = data.get("session_ids", [])
    if not session_ids:
        return jsonify({"error": "No session_ids provided"}), 400
    try:
        session_ids = validate_session_ids(session_ids)
    except InvalidSessionIdError as e:
        return _invalid(e)

    cleared, backed_up, errors = [], {}, []
    for sid in session_ids:
        files = []
        for rec_dir in get_all_recordings_dirs():
            files += [
                rec_dir / f"{sid}.ogg",
                rec_dir / f"{sid}.ogg.partial",
                rec_dir / f"{sid}_partial.bin",
                rec_dir / f"{sid}_encrypted.bin",
            ]
            # The .deleted marker is only a flag — removing it is safe.
            (rec_dir / f"{sid}.deleted").unlink(missing_ok=True)
        try:
            backed_up[sid] = redownload_backup.backup_for_redownload(sid, files)
        except OSError as e:
            errors.append({"session_id": sid, "error": str(e)})
            continue

        # Clear M4A cache (derived from the .ogg; regenerated on demand)
        m4a_cache = Path.home() / "Library" / "Application Support" / "com.plaud.v2" / "cache" / "m4a" / f"{sid}.m4a"
        m4a_cache.unlink(missing_ok=True)
        cleared.append(sid)

    # Remove from deleted_ids so sync will pick it up again
    def _apply(cfg):
        cfg["deleted_ids"] = [s for s in cfg.get("deleted_ids", []) if s not in cleared]

    update_config(_apply)

    return jsonify(
        {
            "cleared": cleared,
            "count": len(cleared),
            "backed_up": backed_up,
            "errors": errors,
            "message": "Connect device and sync to re-download",
        }
    )


@bp.route("/recordings/restore-redownload", methods=["POST"])
def restore_redownload():
    """Put back the local copy that /recordings/redownload moved aside.

    Body: {"session_ids": [...]}. Response: {"restored": {sid: [paths]}, "count": n, "errors": [...]}.
    A backup is never restored over an existing (freshly downloaded) file.
    """
    data = request.get_json(silent=True) or {}
    session_ids = data.get("session_ids", [])
    if not session_ids:
        return jsonify({"error": "No session_ids provided"}), 400
    try:
        session_ids = validate_session_ids(session_ids)
    except InvalidSessionIdError as e:
        return _invalid(e)

    restored, errors = {}, []
    for sid in session_ids:
        # An interrupted re-download leaves {sid}_partial.bin / {sid}.ogg.partial
        # behind. Once the backup .ogg is back, /recordings would delete that
        # partial outright, so move it to the Trash first (recoverable).
        if redownload_backup.backup_ogg_path(sid) is not None:
            partials = [
                rec_dir / f"{sid}{suffix}" for rec_dir in get_all_recordings_dirs() for suffix in _PARTIAL_SUFFIXES
            ]
            _t, _loc, errs = _trash_files(partials)
            if errs:
                errors += [{"session_id": sid, **e} for e in errs]
                continue
        try:
            paths = redownload_backup.restore_redownload_backup(sid)
        except OSError as e:
            errors.append({"session_id": sid, "error": str(e)})
            continue
        if paths:
            restored[sid] = paths
    return jsonify({"restored": restored, "count": len(restored), "errors": errors})


@bp.route("/recordings/move", methods=["POST"])
def move_recordings():
    """Move recordings to a different directory."""
    data = request.get_json(silent=True) or {}
    session_ids = data.get("session_ids", [])
    destination = data.get("destination", "")
    destination = destination.strip() if isinstance(destination, str) else ""

    if not session_ids:
        return jsonify({"error": "No session_ids provided"}), 400
    if not destination:
        return jsonify({"error": "No destination provided"}), 400
    try:
        session_ids = validate_session_ids(session_ids)
    except InvalidSessionIdError as e:
        return _invalid(e)

    if "\x00" in destination:
        return jsonify({"error": "Invalid destination"}), 400
    dest = Path(destination)
    if not dest.is_absolute():
        return jsonify({"error": "Destination must be an absolute path"}), 400
    if ".." in dest.parts:
        return jsonify({"error": "Destination must not contain '..'"}), 400
    if dest.exists() and not dest.is_dir():
        return jsonify({"error": "Destination is not a directory"}), 400
    # Never move recordings into the app's own bookkeeping folders.
    try:
        resolved_dest = dest.resolve()
        for internal in (TRANSCRIPTS_DIR, redownload_backup.REDOWNLOAD_BACKUP_DIR, APP_TRASH_DIR):
            internal = internal.resolve()
            if resolved_dest == internal or resolved_dest.is_relative_to(internal):
                return jsonify({"error": "Destination is an internal app folder"}), 400
    except (OSError, RuntimeError):
        return jsonify({"error": "Invalid destination"}), 400
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"Cannot create destination: {e}"}), 400

    # Track destination in historical directories
    def _track(cfg):
        historical = cfg.setdefault("historical_directories", [])
        if destination not in historical:
            historical.append(destination)

    update_config(_track)

    moved = []
    errors = []
    for sid in session_ids:
        src_path = _find_recording(sid)
        if not src_path:
            errors.append({"session_id": sid, "error": "Not found"})
            continue

        dest_path = dest / src_path.name
        if dest_path == src_path:
            errors.append({"session_id": sid, "error": "Already in destination"})
            continue

        if dest_path.exists():
            errors.append({"session_id": sid, "error": "File exists in destination"})
            continue

        try:
            shutil.move(str(src_path), str(dest_path))
            moved.append({"session_id": sid, "from": str(src_path), "to": str(dest_path)})
            # Also move related files (uncropped backup, encrypted bin)
            for suffix in ["_uncropped.ogg", "_encrypted.bin"]:
                related = src_path.parent / f"{sid}{suffix}"
                if related.exists():
                    shutil.move(str(related), str(dest / related.name))
        except OSError as e:
            errors.append({"session_id": sid, "error": str(e)})

    return jsonify({"moved": moved, "errors": errors, "count": len(moved)})


def load_transcript(session_id) -> dict | None:
    """A recording's transcript JSON, or None when it has none (or it can't be read).

    Read under the transcript lock: without it, json.loads can catch a partial write
    from a diarization or summarization background task. The caller validates the id.
    """
    path = TRANSCRIPTS_DIR / f"{session_id}.json"
    try:
        with get_transcript_lock(session_id):
            data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def transcript_item(session_id, data: dict, profile: str) -> dict:
    """One ``GET /transcripts`` item. ``profile`` is ``speakers.profile_name() or ""``."""
    labels = list({s.get("speaker", "") for s in data.get("segments", []) if s.get("speaker")})
    path = TRANSCRIPTS_DIR / f"{session_id}.json"
    names, name_sources = speakers.names_and_sources(session_id, data, profile=profile)
    return {
        "session_id": session_id,
        "text_preview": (data.get("text", "")[:150] + "...")
        if len(data.get("text", "")) > 150
        else data.get("text", ""),
        "text": data.get("text", ""),
        "duration": data.get("duration", 0),
        "language": data.get("language", ""),
        "speakers": labels,
        "segments": data.get("segments", []),
        # Names over the labels: the effective ones to show (your profile name on
        # your speaker once you picked it), the ones typed, and whether a
        # re-transcription just cleared them (the UI says so once).
        "speaker_names": names,
        "stored_speaker_names": sn.stored_names(data),
        "speaker_name_sources": name_sources,
        "speaker_names_cleared": bool(data.get("speaker_names_cleared")),
        "summary": data.get("summary", ""),
        "summary_stale": bool(data.get("summary_stale", False)),
        "auto_title": data.get("auto_title", ""),
        # Speaker labels: "running" | "done" | "none" | "failed" (with a message)
        "diarization_status": data.get("diarization_status", ""),
        "diarization_message": data.get("diarization_message", ""),
        "path": str(path),
    }


@bp.route("/transcripts")
def list_transcripts():
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    transcripts = []
    profile = speakers.profile_name() or ""
    for f in sorted(TRANSCRIPTS_DIR.glob("*.json"), reverse=True):
        data = load_transcript(f.stem)
        if data is None:
            continue
        try:
            transcripts.append(transcript_item(f.stem, data, profile))
        except Exception:
            continue
    return jsonify(transcripts)


def _is_number(v):
    """Return True iff v is a numeric type but not a bool.

    bool is a subclass of int in Python, so isinstance(True, (int, float))
    returns True.  We explicitly reject bools to avoid corrupt segment timing.
    """
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_segments(segments):
    """Return (ok: bool, error_msg: str) after checking segment list structure.

    Each segment must be a dict with numeric start/end and a string text.
    speaker is optional but, when present, must be a string or null.
    """
    if not isinstance(segments, list):
        return False, "segments must be a list"
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            return False, f"segment[{i}] must be an object"
        if not _is_number(seg.get("start")):
            return False, f"segment[{i}].start must be a number"
        if not _is_number(seg.get("end")):
            return False, f"segment[{i}].end must be a number"
        if not isinstance(seg.get("text"), str):
            return False, f"segment[{i}].text must be a string"
        # speaker is optional — only validate if present
        if "speaker" in seg and not isinstance(seg["speaker"], (str, type(None))):
            return False, f"segment[{i}].speaker must be a string or null"
    return True, ""


def _keep_names_for_edited_segments(transcript):
    """After the user edited segments: keep names only for labels still there, for the new labelling."""
    if not transcript.get("speaker_names"):
        return
    from app.coaching.runner import labelling_fingerprint

    transcript["speaker_names"] = sn.stored_names(transcript)
    transcript["speaker_names_labelling"] = labelling_fingerprint(transcript.get("segments") or [])


@bp.route("/transcripts/update", methods=["POST"])
def update_transcript():
    """Update transcript segments and/or summary."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id:
        return jsonify({"error": "No session_id provided"}), 400
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as e:
        return _invalid(e)

    json_path = TRANSCRIPTS_DIR / f"{session_id}.json"
    txt_path = TRANSCRIPTS_DIR / f"{session_id}.txt"
    if not json_path.exists():
        return jsonify({"error": "Transcript not found"}), 404

    # Validate segments before acquiring the lock so we fail fast on bad input
    # without holding the lock during validation work.
    if "segments" in data:
        ok, err = _validate_segments(data["segments"])
        if not ok:
            return jsonify({"error": err}), 400

    # The {label: name} map the summary editor showed. The names can change while the
    # editor is open (an AI tool renames, clears or shares one), so reverse with these.
    summary_names = data.get("summary_names")
    if summary_names is not None and (
        not isinstance(summary_names, dict) or not all(isinstance(v, str) for v in summary_names.values())
    ):
        return jsonify({"error": "summary_names must map speaker labels to names"}), 400

    profile = speakers.profile_name() or ""
    with get_transcript_lock(session_id):
        transcript = json.loads(json_path.read_text())
        old_text = transcript.get("text")
        # The editor shows names; stored summaries stay in label form so renaming keeps working.
        if summary_names is None:
            # Older callers: the recording's current names.
            names = speakers.names_for(session_id, transcript, profile=profile)
        else:
            labels = set(sn.speaker_labels(transcript.get("segments")))
            unknown = [label for label in summary_names if label not in labels]
            if unknown:
                return jsonify({"error": f"No speaker called {unknown[0]} in this recording"}), 400
            names = summary_names

        if "segments" in data:
            transcript["segments"] = data["segments"]
            transcript["text"] = " ".join(s.get("text", "") for s in data["segments"])
            _keep_names_for_edited_segments(transcript)

        if "summary" in data:
            summary = data["summary"]
            transcript["summary"] = sn.names_to_labels(summary, names) if isinstance(summary, str) else summary
            # A summary the user saved by hand is current by definition.
            transcript.pop("summary_stale", None)

        json_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2))
        txt_path.write_text(transcript.get("text", ""))
        text_changed = transcript.get("text") != old_text

    # After releasing the transcript lock: the coaching store has its own lock.
    if text_changed:
        coaching_store.mark_stale(session_id)
    return jsonify({"saved": True})


@bp.route("/transcripts/delete", methods=["POST"])
def delete_transcript():
    """Move just the transcript for a recording to the Trash (keeps the audio).

    Response: {"deleted": [paths], "trashed": bool, "trash_location": ..., "errors": [...]}.
    """
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id:
        return jsonify({"error": "No session_id provided"}), 400
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as e:
        return _invalid(e)

    paths = [TRANSCRIPTS_DIR / f"{session_id}.{ext}" for ext in ("json", "txt")]
    with get_transcript_lock(session_id):
        deleted, locations, errors = _trash_files(paths)
    # Coaching is built from the transcript, so its data goes with it (after the
    # transcript lock is released: the coaching store has its own lock).
    ct, cloc, cerrs = _trash_coaching_files(session_id)
    deleted, locations, errors = deleted + ct, locations | cloc, errors + cerrs

    return jsonify(
        {
            "deleted": deleted,
            "trashed": not errors,
            "trash_location": _trash_location(locations),
            "errors": errors,
        }
    )


# ---------------------------------------------------------------- speaker names


@bp.route("/transcripts/speaker-names", methods=["POST"])
def set_speaker_names():
    """Name speakers in a recording: ``{session_id, names: {label: name or null}}``.

    Null clears a name. Returns ``{speaker_names}`` (the names as stored). 404 without a
    transcript, 400 for an unknown label or a bad name.
    """
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id or not isinstance(session_id, str):
        return jsonify({"error": "No session_id"}), 400
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as e:
        return _invalid(e)
    names = data.get("names")
    if not isinstance(names, dict):
        return jsonify({"error": "names must map speaker labels to names"}), 400
    try:
        stored = speakers.set_names(session_id, names)
    except KeyError:
        return jsonify({"error": "Transcript not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"speaker_names": stored})


@bp.route("/transcripts/speaker-names/ack-cleared", methods=["POST"])
def ack_speaker_names_cleared():
    """The UI has told the user that re-transcribing cleared the names: stop flagging it."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id or not isinstance(session_id, str):
        return jsonify({"error": "No session_id"}), 400
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as e:
        return _invalid(e)
    try:
        speakers.ack_cleared(session_id)
    except KeyError:
        return jsonify({"error": "Transcript not found"}), 404
    return jsonify({"saved": True})


@bp.route("/people")
def list_people():
    """Name suggestions: every speaker name used across the library and everyone known by voice,
    ``[{name, recordings, person_id, has_voice}]``, people with a voice first."""
    from app.people import directory

    return jsonify(directory.suggestions())
