"""Plaud Cloud account state and operations."""

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import requests as http_requests

from app.utils import pairing_store
from app.utils.audio import unwrap_ogg_to_raw_opus, wrap_opus_ogg
from app.utils.config import load_config, update_config
from app.utils.constants import (
    CLOUD_RECORDINGS_DIR,
    PLAUD_API_DOMAINS,
    PLAUD_CONFIG_PATH,
    PLAUD_WEB_HOST,
    TRANSCRIPTS_DIR,
    get_all_recordings_dirs,
    get_recordings_dir,
)
from app.utils.paths import InvalidSessionIdError, is_auxiliary_stem, validate_session_id
from app.utils.recording_utils import extract_recording_date

# Module-level state
_cloud_state = {
    "logged_in": False,
    "token": None,
    "api_domain": PLAUD_API_DOMAINS["eu"],
    "user": None,
    "desktop_uuid": None,
}
_cloud_state_lock = threading.Lock()


def get_state():
    """Get a read-only snapshot of cloud state."""
    return {
        "logged_in": _cloud_state["logged_in"],
        "user": _cloud_state["user"],
        "api_domain": _cloud_state["api_domain"],
    }


def is_logged_in():
    return _cloud_state["logged_in"]


def _try_token(token, api_domain=None):
    """Validate a token against the Plaud API. Returns user data or None."""
    domain = api_domain or PLAUD_API_DOMAINS["eu"]
    try:
        r = http_requests.get(
            f"{domain}/user/me",
            headers={
                "Authorization": f"bearer {token}",
                "Content-Type": "application/json",
                "app-platform": "desktop",
                "app-language": "en",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _extract_plaud_desktop_token():
    """Try to extract the auth token from the official Plaud desktop app's LevelDB storage."""
    ldb_dir = Path.home() / "Library" / "Application Support" / "Plaud" / "Local Storage" / "leveldb"
    if not ldb_dir.exists():
        return None, None

    plaud_config = Path.home() / "Library" / "Application Support" / "Plaud" / "config.json"
    api_domain = PLAUD_API_DOMAINS["eu"]
    if plaud_config.exists():
        try:
            cfg = json.loads(plaud_config.read_text())
            api_domain = cfg.get("apiDomain", api_domain)
        except Exception:
            pass

    for f in sorted(ldb_dir.glob("*.ldb")):
        try:
            data = f.read_bytes()
            idx = data.find(b"authToken")
            if idx < 0:
                continue
            chunk = data[idx : idx + 600]
            bearer_idx = chunk.find(b"bearer ")
            if bearer_idx < 0:
                continue
            token_bytes = bytearray()
            for b in chunk[bearer_idx + 7 :]:
                if 32 <= b < 127 and b != ord(" "):
                    token_bytes.append(b)
                elif token_bytes:
                    break
            token = bytes(token_bytes).decode("ascii", errors="ignore")
            if token.startswith("eyJ") and len(token) > 50:
                return token, api_domain
        except Exception:
            continue
    return None, None


def _validate_token_async(token, api_domain, *, save_on_success=False):
    """Background validation; updates user info or clears logged_in if rejected."""
    try:
        user_data = _try_token(token, api_domain)
        if user_data:
            with _cloud_state_lock:
                _cloud_state["user"] = user_data
            if save_on_success:
                save_cloud_token(token, api_domain)
        else:
            with _cloud_state_lock:
                # Only clear logged_in if this token is still the active one
                if _cloud_state.get("token") == token:
                    _cloud_state["logged_in"] = False
            logging.warning("[Cloud] Saved token rejected by server; logged_in cleared.")
    except Exception as e:
        logging.warning("[Cloud] Token validation failed (network?): %s", e)


def load_cloud_token():
    """Load Plaud cloud token. Priority: 1) our saved config, 2) Plaud desktop app.

    Trusts saved tokens without online validation — they'll be checked
    lazily when an API call actually needs them. This allows offline
    startup and avoids losing login state due to network issues.

    Validation is deferred to a daemon thread so startup never blocks on a
    network call. Runs from init() at server startup, not at import.
    """
    if PLAUD_CONFIG_PATH.exists():
        try:
            data = json.loads(PLAUD_CONFIG_PATH.read_text())
            token = data.get("token", "")
            api_domain = data.get("api_domain", PLAUD_API_DOMAINS["eu"])
            if token:
                with _cloud_state_lock:
                    _cloud_state["token"] = token
                    _cloud_state["api_domain"] = api_domain
                    _cloud_state["logged_in"] = True
                # Validate in background — update user info if token is still valid
                threading.Thread(
                    target=_validate_token_async,
                    args=(token, api_domain),
                    kwargs={"save_on_success": False},
                    daemon=True,
                ).start()
                return True
        except Exception:
            pass

    token, api_domain = _extract_plaud_desktop_token()
    if token:
        # Trust auto-detected tokens immediately; validate in background and
        # persist them if the server confirms they are valid.
        with _cloud_state_lock:
            _cloud_state["token"] = token
            _cloud_state["api_domain"] = api_domain
            _cloud_state["logged_in"] = True
        threading.Thread(
            target=_validate_token_async,
            args=(token, api_domain),
            kwargs={"save_on_success": True},
            daemon=True,
        ).start()
        return True

    return False


def save_cloud_token(token, api_domain=None):
    """Save Plaud cloud token atomically via a temp-file rename."""
    with _cloud_state_lock:
        if api_domain:
            _cloud_state["api_domain"] = api_domain
    data = {"token": token}
    if api_domain:
        data["api_domain"] = api_domain
    # Write atomically: write to a sibling temp file then rename so a crash
    # mid-write cannot leave a partial config file.
    tmp_path = PLAUD_CONFIG_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2))
    tmp_path.replace(PLAUD_CONFIG_PATH)


def cloud_api(endpoint, method="GET", params=None, json_body=None):
    """Make authenticated request to Plaud cloud API.

    Auto-recovers from region mismatch errors by switching to the correct
    API domain indicated in the error response and retrying once.
    """
    headers = {
        "Authorization": f"bearer {_cloud_state['token']}",
        "Content-Type": "application/json",
        "app-platform": "desktop",
        "app-language": "en",
    }

    for attempt in range(2):
        domain = _cloud_state["api_domain"]
        if method == "GET":
            r = http_requests.get(f"{domain}{endpoint}", headers=headers, params=params, timeout=30)
        else:
            r = http_requests.post(f"{domain}{endpoint}", headers=headers, params=params, json=json_body, timeout=30)

        # Check for region mismatch before raise_for_status
        if attempt == 0:
            try:
                body = r.json()
            except Exception:
                body = {}
            if body.get("msg") == "user region mismatch" or body.get("status") == -302:
                correct_domain = (body.get("data") or {}).get("domains", {}).get("api")
                if correct_domain and correct_domain != domain:
                    logging.info(f"Region mismatch: switching API domain from {domain} to {correct_domain}")
                    with _cloud_state_lock:
                        _cloud_state["api_domain"] = correct_domain
                        current_token = _cloud_state["token"]
                    # Persist via the atomic helper (temp-rename, no lock re-entry).
                    try:
                        save_cloud_token(current_token, correct_domain)
                    except Exception:
                        pass
                    continue  # retry with correct domain

        r.raise_for_status()
        return r.json()


def get_login_url(region="eu"):
    """Generate the OAuth login URL for Plaud web login."""
    import uuid as uuid_mod

    desktop_uuid = str(uuid_mod.uuid4())
    api_domain = PLAUD_API_DOMAINS.get(region, PLAUD_API_DOMAINS["eu"])
    with _cloud_state_lock:
        _cloud_state["desktop_uuid"] = desktop_uuid
        _cloud_state["api_domain"] = api_domain
    login_url = f"{PLAUD_WEB_HOST}/launch-desktop?from=desktop&desktop_uuid={desktop_uuid}"
    return login_url, desktop_uuid


def auto_detect():
    """Check if we can auto-detect the Plaud desktop app token."""
    token, api_domain = _extract_plaud_desktop_token()
    if token:
        return {"found": True, "api_domain": api_domain}
    return {"found": False}


def exchange_token(auth_code, desktop_uuid=None):
    """Exchange auth code for bearer token after OAuth flow."""
    desktop_uuid = desktop_uuid or _cloud_state.get("desktop_uuid", "")
    r = http_requests.post(
        f"{_cloud_state['api_domain']}/auth/access-token-auth-code",
        json={"client_id": "web", "auth_code": auth_code, "desktop_uuid": desktop_uuid},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    token_data = r.json()
    token = token_data.get("access_token", token_data.get("token", ""))
    if not token:
        return {"error": "No token in response", "raw": token_data}

    with _cloud_state_lock:
        _cloud_state["token"] = token
        saved_domain = _cloud_state["api_domain"]
    save_cloud_token(token, saved_domain)

    user = cloud_api("/user/me")
    with _cloud_state_lock:
        _cloud_state["logged_in"] = True
        _cloud_state["user"] = user
    return {"logged_in": True, "user": user}


def login_with_token(token, region="eu"):
    """Login with a manually provided bearer token."""
    # Strip "Bearer " prefix if pasted from browser local storage
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    api_domain = PLAUD_API_DOMAINS.get(region, PLAUD_API_DOMAINS["eu"])
    with _cloud_state_lock:
        _cloud_state["api_domain"] = api_domain
        _cloud_state["token"] = token

    try:
        user = cloud_api("/user/me")
        with _cloud_state_lock:
            _cloud_state["logged_in"] = True
            _cloud_state["user"] = user
        save_cloud_token(token, api_domain)
        return {"logged_in": True, "user": user}
    except Exception:
        with _cloud_state_lock:
            _cloud_state["logged_in"] = False
            _cloud_state["token"] = None
        raise


def logout():
    """Log out from Plaud cloud."""
    with _cloud_state_lock:
        _cloud_state["logged_in"] = False
        _cloud_state["token"] = None
        _cloud_state["user"] = None
    if PLAUD_CONFIG_PATH.exists():
        PLAUD_CONFIG_PATH.unlink()
    return {"logged_out": True}


# Characters replaced in the name part of a new cloud session id: path
# separators (/ and the classic Mac :), backslash, and control characters.
_CLOUD_NAME_UNSAFE_RE = re.compile(r"[/:\\\x00-\x1f\x7f]")
_CLOUD_NAME_MAX = 40


def legacy_cloud_session_id(date_str, filename):
    """The id older builds gave a cloud recording (only / and : replaced).

    Kept so files already on disk under that name are matched, not
    downloaded a second time under the newer, cleaner id.
    """
    name = str(filename).replace("/", "-").replace(":", "-")
    return f"cloud_{date_str}_{name[:_CLOUD_NAME_MAX]}".replace(" ", "_")


def cloud_session_id(date_str, filename):
    """Session id for a Plaud Cloud recording: ``cloud_<date>_<name[:40]>``.

    The name is free text from the Plaud app, so besides / and : this also
    replaces backslashes and control characters, and drops leading dots and
    trailing dots/spaces ("Plan." -> "Plan", "Wait..." -> "Wait"), which
    makes for plain, Finder-friendly filenames.
    """
    name = _CLOUD_NAME_UNSAFE_RE.sub("-", str(filename)).strip()
    name = name[:_CLOUD_NAME_MAX].lstrip(".").rstrip(". ") or "recording"
    return f"cloud_{date_str}_{name}".replace(" ", "_")


def _resolve_cloud_session_id(date_str, filename, rec_dir, dismissed, cloud_file_ids):
    """New-style id, unless the recording is already known under its legacy id."""
    sid = cloud_session_id(date_str, filename)
    legacy = legacy_cloud_session_id(date_str, filename)
    if legacy == sid:
        return sid
    try:
        validate_session_id(legacy)
    except InvalidSessionIdError:
        return sid
    if (
        legacy in dismissed
        or legacy in cloud_file_ids
        or any((rec_dir / f"{legacy}{ext}").exists() for ext in (".ogg", ".mp3"))
    ):
        return legacy
    return sid


def download_cloud_recordings():
    """Download all cloud recordings, convert raw Opus to OGG."""
    if not _cloud_state["logged_in"] or not _cloud_state["token"]:
        return {"error": "Not logged in to Plaud cloud"}

    data = cloud_api(
        "/file/simple/web",
        params={
            "skip": 0,
            "limit": 9999,
            "is_trash": 0,
            "sort_by": "start_time",
            "is_desc": "true",
        },
    )
    files = data.get("data_file_list", data.get("data_list", []))
    if not files:
        return {"files": [], "message": "No recordings in cloud"}

    CLOUD_RECORDINGS_DIR.mkdir(exist_ok=True)
    active_rec_dir = get_recordings_dir()
    active_rec_dir.mkdir(exist_ok=True)
    config = load_config()
    dismissed = set(config.get("deleted_ids", []))
    cloud_file_ids = config.get("cloud_file_ids", {})
    uploaded_names = set(config.get("uploaded_filenames", []))
    results = []

    for i, f in enumerate(files):
        ts = f.get("start_time", 0)
        if ts > 1e12:
            ts = ts / 1000
        dur = f.get("duration", 0)
        if dur > 10000:
            dur = dur / 1000
        raw_name = f.get("filename", "unknown")
        name = str(raw_name).replace("/", "-").replace(":", "-")
        fid = f.get("id", "")

        from datetime import datetime as dt

        date_str = dt.fromtimestamp(ts).strftime("%Y-%m-%d_%H%M") if ts > 0 else "unknown"
        session_id = _resolve_cloud_session_id(date_str, raw_name, active_rec_dir, dismissed, cloud_file_ids)
        ogg_path = active_rec_dir / f"{session_id}.ogg"

        if session_id in dismissed:
            continue

        cloud_filename = f.get("filename", "")
        if cloud_filename in uploaded_names:
            continue

        if ogg_path.exists():
            results.append(
                {
                    "session_id": session_id,
                    "ogg_path": str(ogg_path),
                    "duration": dur,
                    "status": "exists",
                    "cloud_name": name,
                    "cloud_date": date_str,
                }
            )
            continue

        # Re-check with a fresh config: the user may have deleted this
        # recording while earlier files were downloading.
        if session_id in set(load_config().get("deleted_ids", [])):
            continue

        try:
            url_data = cloud_api(f"/file/temp-url/{fid}", params={"is_opus": 1})
            url = url_data.get("temp_url_opus") or url_data.get("temp_url") or ""
            if not url:
                results.append({"session_id": session_id, "status": "no_url", "cloud_name": name})
                continue

            r = http_requests.get(url, timeout=60)
            if r.status_code != 200:
                results.append({"session_id": session_id, "status": "download_failed", "cloud_name": name})
                continue

            raw = r.content
            if len(raw) < 160:
                results.append({"session_id": session_id, "status": "too_small", "cloud_name": name})
                continue

            if raw[:4] == b"OggS":
                ogg_path.write_bytes(raw)
                duration = dur
            elif raw[:3] == b"ID3":
                mp3_path = active_rec_dir / f"{session_id}.mp3"
                mp3_path.write_bytes(raw)
                results.append(
                    {
                        "session_id": session_id,
                        "ogg_path": str(mp3_path),
                        "duration": dur,
                        "status": "new",
                        "cloud_name": name,
                        "cloud_date": date_str,
                    }
                )
                continue
            elif len(raw) % 80 == 0:
                ogg = wrap_opus_ogg(raw)
                tmp_path = ogg_path.with_suffix(".ogg.partial")
                tmp_path.write_bytes(ogg)
                tmp_path.rename(ogg_path)
                duration = (len(raw) // 80) * 0.02
            else:
                raw_path = active_rec_dir / f"{session_id}.raw"
                raw_path.write_bytes(raw)
                results.append({"session_id": session_id, "status": "unknown_format", "cloud_name": name})
                continue

            update_config(lambda c, sid=session_id, fid=fid: c.setdefault("cloud_file_ids", {}).__setitem__(sid, fid))

            results.append(
                {
                    "session_id": session_id,
                    "ogg_path": str(ogg_path),
                    "duration": round(duration, 1),
                    "status": "new",
                    "cloud_name": name,
                    "cloud_date": date_str,
                    "cloud_id": fid,
                }
            )

        except Exception as e:
            results.append({"session_id": session_id, "status": f"error: {e}", "cloud_name": name})

    return {"files": results}


def upload_to_cloud(ogg_path, filename, duration, file_type="OPUS", scene=1, start_time_ms=None):
    """Upload a local recording to Plaud Cloud via multipart S3 upload."""
    if not _cloud_state.get("token"):
        return {"error": "Not logged in"}

    raw_opus = unwrap_ogg_to_raw_opus(ogg_path)
    tmp = tempfile.NamedTemporaryFile(suffix=".opus", delete=False)
    tmp.write(raw_opus)
    tmp.close()
    upload_path = tmp.name
    file_size = len(raw_opus)

    # try/finally so the temp file is unlinked on every exit path. Previously
    # the early-return at `if not upload_id` and any HTTPError leaked tens of
    # MB per failure into the system temp dir.
    try:
        if file_size < 100:
            return {"error": "File too small to upload"}

        headers = {
            "Authorization": f"bearer {_cloud_state['token']}",
            "Content-Type": "application/json",
            "app-platform": "desktop",
            "app-language": "en",
        }
        api = _cloud_state["api_domain"]

        r = http_requests.post(
            f"{api}/file/get_upload_presigned_url",
            json={"filesize": file_size, "file_type": file_type},
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json()
        upload_data = raw.get("data", raw)
        upload_id = upload_data.get("upload_id", "")
        object_name = upload_data.get("object_name", "")
        part_urls = upload_data.get("part_urls", [])

        if not upload_id or not part_urls:
            return {"error": "Failed to get upload URLs from cloud"}

        parts = []
        chunk_size = 5 * 1024 * 1024
        with open(upload_path, "rb") as f:
            for part_num, part_url in enumerate(part_urls, start=1):
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                resp = http_requests.put(part_url, data=chunk, timeout=60)
                resp.raise_for_status()
                etag = resp.headers.get("ETag", "")
                parts.append({"PartNumber": part_num, "ETag": etag})

        r = http_requests.post(
            f"{api}/file/merge_multipart",
            json={"upload_id": upload_id, "object_name": object_name, "parts": parts},
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()

        duration_ms = int(duration * 1000) if duration < 100000 else int(duration)
        if not start_time_ms:
            start_time_ms = int(time.time() * 1000)

        r = http_requests.post(
            f"{api}/file/confirm_upload",
            json={
                "upload_id": upload_id,
                "object_name": object_name,
                "scene": scene,
                "file_type": file_type,
                "duration": duration_ms,
                "filename": filename,
                "start_time": start_time_ms,
            },
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()

        return r.json()
    finally:
        try:
            os.unlink(upload_path)
        except OSError:
            pass


def upload_local_recordings_to_cloud():
    """Upload all local recordings that aren't already in the cloud."""
    if not _cloud_state.get("token"):
        return {"error": "Not logged in"}

    try:
        cloud_data = cloud_api(
            "/file/simple/web",
            params={
                "skip": 0,
                "limit": 9999,
                "is_trash": 0,
            },
        )
        cloud_files = cloud_data.get("data_file_list", cloud_data.get("data_list", []))
        cloud_filenames = {f.get("filename", "") for f in cloud_files}
    except Exception:
        cloud_filenames = set()

    config = load_config()
    custom_names = config.get("recording_names", {})
    uploaded = []

    # Scan all known recording directories for uploadable files
    all_ogg_files = []
    for rec_dir in get_all_recordings_dirs():
        rec_dir.mkdir(exist_ok=True)
        all_ogg_files.extend(rec_dir.glob("*.ogg"))
    for f in sorted(all_ogg_files):
        session_id = f.stem
        if session_id.startswith("cloud_"):
            continue
        if is_auxiliary_stem(session_id):
            continue  # crop backup / temp file, not a recording

        rec_date = extract_recording_date(session_id)
        if rec_date:
            display_name = rec_date.strftime("%Y-%m-%d %H:%M:%S")
        else:
            display_name = session_id

        title = custom_names.get(session_id, "")
        if title:
            display_name = f"{display_name} - {title}"

        if display_name in cloud_filenames:
            continue

        transcript_path = TRANSCRIPTS_DIR / f"{session_id}.json"
        duration = 0
        if transcript_path.exists():
            try:
                t_data = json.loads(transcript_path.read_text())
                duration = t_data.get("duration", 0)
            except Exception:
                pass
        if not duration:
            size = f.stat().st_size
            raw_audio = max(size - 2000, 0)
            duration = raw_audio / 80 * 0.02

        rec_date = extract_recording_date(session_id)
        rec_ts_ms = int(rec_date.timestamp() * 1000) if rec_date else None

        try:
            result = upload_to_cloud(str(f), display_name, duration, start_time_ms=rec_ts_ms)
            if not result.get("error"):
                # Track cloud file ID for deletion support
                cloud_fid = result.get("data", result).get("id", result.get("id", ""))

                def _record_upload(conf, sid=session_id, name=display_name, cloud_fid=cloud_fid):
                    uploaded_names = conf.setdefault("uploaded_filenames", [])
                    if name not in uploaded_names:
                        uploaded_names.append(name)
                    uploaded_ids = conf.setdefault("uploaded_session_ids", [])
                    if sid not in uploaded_ids:
                        uploaded_ids.append(sid)
                    if cloud_fid:
                        conf.setdefault("cloud_file_ids", {})[sid] = cloud_fid

                update_config(_record_upload)
            if result.get("error"):
                uploaded.append({"session_id": session_id, "status": "error", "error": result["error"]})
            else:
                uploaded.append({"session_id": session_id, "status": "uploaded", "filename": display_name})
        except Exception as e:
            uploaded.append({"session_id": session_id, "status": "error", "error": str(e)[:100]})

    return {"uploaded": uploaded}


def trash_cloud_files(cloud_ids):
    """Move files to trash on Plaud Cloud."""
    r = http_requests.post(
        f"{_cloud_state['api_domain']}/file/trash/",
        json=cloud_ids,
        headers={
            "Authorization": f"bearer {_cloud_state['token']}",
            "Content-Type": "application/json",
            "app-platform": "desktop",
            "app-language": "en",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def list_cloud_recordings():
    """List recordings available in the Plaud cloud."""
    data = cloud_api(
        "/file/simple/web",
        params={
            "skip": 0,
            "limit": 9999,
            "is_trash": 0,
            "sort_by": "start_time",
            "is_desc": "true",
        },
    )
    files = data.get("data_file_list", data.get("data_list", []))
    result = []
    for f in files:
        dur = f.get("duration", 0)
        if dur > 10000:
            dur = dur / 1000
        ts = f.get("start_time", 0)
        if ts > 1e12:
            ts = ts / 1000
        result.append(
            {
                "file_id": f.get("id", ""),
                "filename": f.get("filename", "unknown"),
                "duration": dur,
                "start_time": ts,
                "scene": f.get("scene", 0),
            }
        )
    return result


def list_cloud_devices():
    """List devices bound to the Plaud account."""
    data = cloud_api("/device/list")
    return data.get("data_devices", [])


def bind_device(sn, name="Plaud NotePin S", model="882", version_number=65546):
    """Bind a device to the user's Plaud cloud account.

    Returns the device-specific access_token (BIND_TOKEN) which is needed
    for the BLE handshake. Each device needs its own token containing
    the user's account ID and device serial number.
    """
    if not _cloud_state["logged_in"] or not _cloud_state["token"]:
        return {"error": "Not logged in to Plaud cloud"}

    try:
        data = cloud_api(
            "/device/bind",
            method="POST",
            json_body={
                "sn": sn,
                "name": name,
                "model": model,
                "version_number": version_number,
            },
        )
        if data.get("status") == 0 and data.get("data", {}).get("access_token"):
            token = data["data"]["access_token"]
            # Save per-device token in config
            update_config(lambda c: c.setdefault("device_bind_tokens", {}).__setitem__(sn, token))
            pairing_store.backup_serial_async(sn, name)  # Keychain copy once both credentials exist
            return {"token": token, "sn": sn}
        return {"error": data.get("msg", "Bind failed"), "raw": data}
    except Exception as e:
        return {"error": str(e)}


def get_device_bind_token(sn):
    """Get the stored BIND_TOKEN for a device, or None if not available.

    If the config has lost it (e.g. a cleaner wiped the data folder), restore
    the device's pairing from the Keychain backup (app/utils/pairing_store.py).
    """
    config = load_config()
    token = config.get("device_bind_tokens", {}).get(sn)
    if token or not sn:
        return token
    return (pairing_store.restore_serial(sn) or {}).get("bind_token")


def fetch_sn_signature(sn):
    """Fetch the SN_SIG (snSignature) for a device from the cloud API.

    POST /device/sn-sign with {"sns": ["<serial_number>"]}
    Returns the per-device RSA signature needed for BLE handshake.
    Stores it in config for offline use.
    """
    if not _cloud_state["logged_in"] or not _cloud_state["token"]:
        return {"error": "Not logged in to Plaud cloud"}

    try:
        data = cloud_api("/device/sn-sign", method="POST", json_body={"sns": [sn]})
        if data.get("status") == 0:
            signatures = data.get("data", {}).get("signature", {})
            sig = signatures.get(sn)
            if sig:
                update_config(lambda c: c.setdefault("device_sn_signatures", {}).__setitem__(sn, sig))
                pairing_store.backup_serial_async(sn)  # Keychain copy once both credentials exist
                return {"signature": sig, "sn": sn}
            return {"error": "No signature returned for this device"}
        return {"error": data.get("msg", "Failed to get signature"), "raw": data}
    except Exception as e:
        return {"error": str(e)}


def get_device_sn_signature(sn):
    """Get the stored SN_SIG for a device, or None if not available.

    Restores from the Keychain backup when the config lacks it, like
    get_device_bind_token.
    """
    config = load_config()
    sig = config.get("device_sn_signatures", {}).get(sn)
    if sig or not sn:
        return sig
    return (pairing_store.restore_serial(sn) or {}).get("sn_signature")


def init():
    """Load the saved cloud token into _cloud_state.

    Called once from api_server.py's startup (``__main__``), never at import:
    importing this module (tests, REPL, tooling) must not read the Plaud
    desktop app's storage or start a validation request to the Plaud API.
    """
    return load_cloud_token()
