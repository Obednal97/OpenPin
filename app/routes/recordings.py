"""Recordings and transcripts routes."""

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

from app.utils.config import load_config, save_config
from app.utils.constants import RECORDINGS_DIR, TRANSCRIPTS_DIR, get_all_recordings_dirs, get_recordings_dir
from app.utils.system import ensure_ffmpeg

bp = Blueprint("recordings", __name__)


def _find_recording(session_id, ext="ogg"):
    """Find a recording file across all known directories."""
    for d in get_all_recordings_dirs():
        p = d / f"{session_id}.{ext}"
        if p.exists():
            return p
    return None


def recover_metadata_from_transcripts():
    """Recover tags, custom names, and auto_titles from transcript files.

    Runs on startup — if the config was reset/wiped, this restores
    metadata that was persisted in the transcript JSON files.
    """
    if not TRANSCRIPTS_DIR.exists():
        return

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


# Run recovery on import (server startup)
recover_metadata_from_transcripts()


def _extract_recording_date(stem):
    """Extract the actual recording timestamp from the session ID or filename."""
    import re

    try:
        ts = int(stem)
        if 1700000000 < ts < 2000000000:
            return datetime.fromtimestamp(ts)
    except ValueError:
        pass

    m = re.match(r"cloud_(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})_", stem)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}", "%Y-%m-%d %H:%M")
        except ValueError:
            pass

    m = re.search(r"(\d{4}-\d{2}-\d{2})", stem)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            pass

    return None


@bp.route("/recordings")
def list_recordings():
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
            if f.stem in complete_ids:
                continue  # Already seen from another directory
            complete_ids.add(f.stem)
            size = f.stat().st_size
            raw_audio_size = max(size - 2000, 0)
            duration = raw_audio_size / 80 * 0.02

            rec_date = _extract_recording_date(f.stem)
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

    # Include partial downloads (interrupted transfers) — shown with warning in UI
    for rec_dir in all_dirs:
        for f in sorted(rec_dir.glob("*.ogg.partial"), reverse=True):
            stem = f.name.replace(".ogg.partial", "")
            if stem in complete_ids:
                try:
                    f.unlink()
                except Exception:
                    pass
                continue
            complete_ids.add(stem)
            size = f.stat().st_size
            rec_date = _extract_recording_date(stem)
            if rec_date is None:
                rec_date = datetime.fromtimestamp(f.stat().st_mtime)
            recordings.append(
                {
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
            )

    # Include cloud-only recordings (uploaded to Drive but no local file)
    try:
        from app.state.gdrive import get_cloud_only_recordings
        for cr in get_cloud_only_recordings():
            sid = cr["session_id"]
            if sid in complete_ids:
                continue
            transcript_path = TRANSCRIPTS_DIR / f"{sid}.json"
            rec_date = _extract_recording_date(sid)
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

    recordings.sort(key=lambda r: r["date"], reverse=True)
    return jsonify(recordings)


@bp.route("/recordings/tag", methods=["POST"])
def tag_recording():
    """Set tags for a recording."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    tags = data.get("tags", [])
    if not session_id:
        return jsonify({"error": "No session_id"}), 400
    config = load_config()
    rec_tags = config.get("recording_tags", {})
    rec_tags[session_id] = tags
    config["recording_tags"] = rec_tags
    save_config(config)

    # Also persist tags in the transcript JSON for recovery after config resets
    from app.utils.constants import TRANSCRIPTS_DIR
    transcript_path = TRANSCRIPTS_DIR / f"{session_id}.json"
    if transcript_path.exists():
        try:
            import json as _json
            t_data = _json.loads(transcript_path.read_text())
            t_data["tags"] = tags
            transcript_path.write_text(_json.dumps(t_data, indent=2, ensure_ascii=False))
        except Exception:
            pass

    # Sync tags to Notion if page already exists (tags-only update, no title change)
    notion_synced = False
    try:
        from app.routes.notion import (
            _build_notion_properties,
            _ensure_tags_property_once,
            _extract_page_id,
            _get_notion_page_url,
        )
        from app.state import notion
        existing_url = _get_notion_page_url(session_id)
        if existing_url and notion.is_connected():
            page_id = _extract_page_id(existing_url)
            if page_id:
                _ensure_tags_property_once(tags)
                # Notion MCP stores multi-select as a JSON-array string (see the
                # SQLite annotation on a multi-select column from notion-fetch).
                import json as _json
                properties = {"Tags": _json.dumps(tags)}
                notion.mcp_call(
                    "notion-update-page",
                    {
                        "page_id": page_id,
                        "command": "update_properties",
                        "properties": properties,
                        "content_updates": [],
                    },
                )
                notion_synced = True
    except Exception as e:
        import logging as _logging
        _logging.warning(f"Notion tag sync failed: {e}")

    return jsonify({"saved": True, "notion_synced": notion_synced})


@bp.route("/recordings/rename", methods=["POST"])
def rename_recording():
    """Set a custom title for a recording."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    title = data.get("title", "").strip()
    if not session_id:
        return jsonify({"error": "No session_id"}), 400
    config = load_config()
    names = config.get("recording_names", {})
    if title:
        names[session_id] = title
    elif session_id in names:
        del names[session_id]
    config["recording_names"] = names
    save_config(config)

    # Also persist name in the transcript JSON for recovery after config resets
    transcript_path = TRANSCRIPTS_DIR / f"{session_id}.json"
    if transcript_path.exists():
        try:
            import json as _json
            t_data = _json.loads(transcript_path.read_text())
            if title:
                t_data["custom_name"] = title
            else:
                t_data.pop("custom_name", None)
            transcript_path.write_text(_json.dumps(t_data, indent=2, ensure_ascii=False))
        except Exception:
            pass

    return jsonify({"saved": True})


@bp.route("/recordings/export", methods=["POST"])
def export_recording():
    """Convert recording to MP3 and save to Downloads."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id:
        return jsonify({"error": "No session_id"}), 400

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
                ffmpeg or "ffmpeg", "-y",
                "-i", str(ogg_path),
                "-ss", str(start),
                "-to", str(end),
                "-c", "copy",
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
            config = load_config()
            original_name = config.get("recording_names", {}).get(session_id, session_id)
            config.setdefault("recording_names", {})[new_id] = f"{original_name} (cropped)"
            save_config(config)
            return jsonify({
                "cropped": True,
                "session_id": new_id,
                "duration": round(end - start, 2),
            })
        else:
            out_path.replace(ogg_path)
            # Delete existing transcript — it no longer matches the audio
            for ext in (".json", ".txt"):
                t_path = TRANSCRIPTS_DIR / f"{session_id}{ext}"
                t_path.unlink(missing_ok=True)
            return jsonify({
                "cropped": True,
                "session_id": session_id,
                "duration": round(end - start, 2),
            })
    except FileNotFoundError:
        return jsonify({"error": "ffmpeg not found — install ffmpeg to crop audio"}), 500
    except subprocess.TimeoutExpired:
        out_path.unlink(missing_ok=True)
        return jsonify({"error": "Crop timed out"}), 500


@bp.route("/recordings/delete", methods=["POST"])
def delete_recordings():
    """Delete one or more recordings and their transcripts."""
    data = request.get_json(silent=True) or {}
    session_ids = data.get("session_ids", [])
    if not session_ids:
        return jsonify({"error": "No session_ids provided"}), 400

    config = load_config()
    dismissed = set(config.get("deleted_ids", []))

    deleted = []
    for sid in session_ids:
        dismissed.add(sid)
        # Write .deleted marker in active dir so config reset doesn't re-sync
        active_dir = get_recordings_dir()
        marker = active_dir / f"{sid}.deleted"
        try:
            marker.write_text("")
        except Exception:
            pass
        # Remove files from all known directories
        for rec_dir in get_all_recordings_dirs():
            for ext in ["ogg", "mp3", "raw"]:
                p = rec_dir / f"{sid}.{ext}"
                if p.exists():
                    p.unlink()
                    deleted.append(str(p))
        for ext in ["json", "txt"]:
            p = TRANSCRIPTS_DIR / f"{sid}.{ext}"
            if p.exists():
                p.unlink()
                deleted.append(str(p))
        for key in ("recording_names", "recording_tags"):
            if sid in config.get(key, {}):
                del config[key][sid]

    config["deleted_ids"] = list(dismissed)
    save_config(config)
    return jsonify({"deleted": deleted, "count": len(session_ids)})


@bp.route("/recordings/redownload", methods=["POST"])
def redownload_recording():
    """Mark a recording for re-download by removing local files and clearing dismiss markers."""
    data = request.get_json(silent=True) or {}
    session_ids = data.get("session_ids", [])
    if not session_ids:
        return jsonify({"error": "No session_ids provided"}), 400

    config = load_config()
    dismissed = set(config.get("deleted_ids", []))
    cleared = []

    for sid in session_ids:
        # Remove the .ogg, cached M4A, and any .deleted marker
        for rec_dir in get_all_recordings_dirs():
            for pattern in [f"{sid}.ogg", f"{sid}.ogg.partial", f"{sid}.deleted", f"{sid}_encrypted.bin"]:
                p = rec_dir / pattern
                if p.exists():
                    p.unlink(missing_ok=True)

        # Clear M4A cache
        import os
        m4a_cache = os.path.join(
            os.path.expanduser("~"),
            "Library", "Application Support", "com.plaud.v2", "cache", "m4a",
            f"{sid}.m4a",
        )
        if os.path.exists(m4a_cache):
            os.unlink(m4a_cache)

        # Remove from deleted_ids so sync will pick it up again
        dismissed.discard(sid)
        cleared.append(sid)

    config["deleted_ids"] = list(dismissed)
    save_config(config)

    return jsonify({"cleared": cleared, "count": len(cleared), "message": "Connect device and sync to re-download"})


@bp.route("/recordings/move", methods=["POST"])
def move_recordings():
    """Move recordings to a different directory."""
    data = request.get_json(silent=True) or {}
    session_ids = data.get("session_ids", [])
    destination = data.get("destination", "").strip()

    if not session_ids:
        return jsonify({"error": "No session_ids provided"}), 400
    if not destination:
        return jsonify({"error": "No destination provided"}), 400

    dest = Path(destination)
    if not dest.is_absolute():
        return jsonify({"error": "Destination must be an absolute path"}), 400
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"Cannot create destination: {e}"}), 400

    # Track destination in historical directories
    config = load_config()
    historical = config.get("historical_directories", [])
    if destination not in historical:
        historical.append(destination)
        config["historical_directories"] = historical
        save_config(config)

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


@bp.route("/transcripts")
def list_transcripts():
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    transcripts = []
    for f in sorted(TRANSCRIPTS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
            speakers = list({s.get("speaker", "") for s in data.get("segments", []) if s.get("speaker")})
            transcripts.append(
                {
                    "session_id": f.stem,
                    "text_preview": (data.get("text", "")[:150] + "...")
                    if len(data.get("text", "")) > 150
                    else data.get("text", ""),
                    "text": data.get("text", ""),
                    "duration": data.get("duration", 0),
                    "language": data.get("language", ""),
                    "speakers": speakers,
                    "segments": data.get("segments", []),
                    "summary": data.get("summary", ""),
                    "auto_title": data.get("auto_title", ""),
                    "path": str(f),
                }
            )
        except Exception:
            continue
    return jsonify(transcripts)


@bp.route("/transcripts/update", methods=["POST"])
def update_transcript():
    """Update transcript segments and/or summary."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id:
        return jsonify({"error": "No session_id provided"}), 400

    json_path = TRANSCRIPTS_DIR / f"{session_id}.json"
    txt_path = TRANSCRIPTS_DIR / f"{session_id}.txt"
    if not json_path.exists():
        return jsonify({"error": "Transcript not found"}), 404

    transcript = json.loads(json_path.read_text())

    if "segments" in data:
        transcript["segments"] = data["segments"]
        transcript["text"] = " ".join(s.get("text", "") for s in data["segments"])

    if "summary" in data:
        transcript["summary"] = data["summary"]

    json_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2))
    txt_path.write_text(transcript.get("text", ""))

    return jsonify({"saved": True})


@bp.route("/transcripts/delete", methods=["POST"])
def delete_transcript():
    """Delete just the transcript for a recording (keeps the audio)."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id:
        return jsonify({"error": "No session_id provided"}), 400

    deleted = []
    for ext in ["json", "txt"]:
        p = TRANSCRIPTS_DIR / f"{session_id}.{ext}"
        if p.exists():
            p.unlink()
            deleted.append(str(p))

    return jsonify({"deleted": deleted})
