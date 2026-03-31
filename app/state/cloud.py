"""Plaud Cloud account state and operations."""

import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

import requests as http_requests

from app.utils.audio import unwrap_ogg_to_raw_opus, wrap_opus_ogg
from app.utils.config import load_config, save_config
from app.utils.constants import (
    CLOUD_RECORDINGS_DIR,
    PLAUD_API_DOMAINS,
    PLAUD_CONFIG_PATH,
    PLAUD_WEB_HOST,
    RECORDINGS_DIR,
    TRANSCRIPTS_DIR,
)

# Module-level state
_cloud_state = {
    "logged_in": False,
    "token": None,
    "api_domain": PLAUD_API_DOMAINS["eu"],
    "user": None,
    "desktop_uuid": None,
}


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


def load_cloud_token():
    """Load Plaud cloud token. Priority: 1) our saved config, 2) Plaud desktop app."""
    if PLAUD_CONFIG_PATH.exists():
        try:
            data = json.loads(PLAUD_CONFIG_PATH.read_text())
            token = data.get("token", "")
            api_domain = data.get("api_domain", PLAUD_API_DOMAINS["eu"])
            if token:
                user_data = _try_token(token, api_domain)
                if user_data:
                    _cloud_state["token"] = token
                    _cloud_state["api_domain"] = api_domain
                    _cloud_state["logged_in"] = True
                    _cloud_state["user"] = user_data
                    return True
        except Exception:
            pass

    token, api_domain = _extract_plaud_desktop_token()
    if token:
        user_data = _try_token(token, api_domain)
        if user_data:
            _cloud_state["token"] = token
            _cloud_state["api_domain"] = api_domain
            _cloud_state["logged_in"] = True
            _cloud_state["user"] = user_data
            save_cloud_token(token, api_domain)
            return True

    return False


def save_cloud_token(token, api_domain=None):
    """Save Plaud cloud token."""
    if api_domain:
        _cloud_state["api_domain"] = api_domain
    data = {"token": token}
    if api_domain:
        data["api_domain"] = api_domain
    PLAUD_CONFIG_PATH.write_text(json.dumps(data, indent=2))


def cloud_api(endpoint, method="GET", params=None, json_body=None):
    """Make authenticated request to Plaud cloud API."""
    headers = {
        "Authorization": f"bearer {_cloud_state['token']}",
        "Content-Type": "application/json",
        "app-platform": "desktop",
        "app-language": "en",
    }
    if method == "GET":
        r = http_requests.get(f"{_cloud_state['api_domain']}{endpoint}", headers=headers, params=params, timeout=30)
    else:
        r = http_requests.post(
            f"{_cloud_state['api_domain']}{endpoint}", headers=headers, params=params, json=json_body, timeout=30
        )
    r.raise_for_status()
    return r.json()


def get_login_url(region="eu"):
    """Generate the OAuth login URL for Plaud web login."""
    import uuid as uuid_mod

    desktop_uuid = str(uuid_mod.uuid4())
    _cloud_state["desktop_uuid"] = desktop_uuid
    api_domain = PLAUD_API_DOMAINS.get(region, PLAUD_API_DOMAINS["eu"])
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

    _cloud_state["token"] = token
    save_cloud_token(token, _cloud_state["api_domain"])

    user = cloud_api("/user/me")
    _cloud_state["logged_in"] = True
    _cloud_state["user"] = user
    return {"logged_in": True, "user": user}


def login_with_token(token, region="eu"):
    """Login with a manually provided bearer token."""
    api_domain = PLAUD_API_DOMAINS.get(region, PLAUD_API_DOMAINS["eu"])
    _cloud_state["api_domain"] = api_domain
    _cloud_state["token"] = token

    try:
        user = cloud_api("/user/me")
        _cloud_state["logged_in"] = True
        _cloud_state["user"] = user
        save_cloud_token(token, api_domain)
        return {"logged_in": True, "user": user}
    except Exception:
        _cloud_state["logged_in"] = False
        _cloud_state["token"] = None
        raise


def logout():
    """Log out from Plaud cloud."""
    _cloud_state["logged_in"] = False
    _cloud_state["token"] = None
    _cloud_state["user"] = None
    if PLAUD_CONFIG_PATH.exists():
        PLAUD_CONFIG_PATH.unlink()
    return {"logged_out": True}


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
    RECORDINGS_DIR.mkdir(exist_ok=True)
    config = load_config()
    dismissed = set(config.get("deleted_ids", []))
    uploaded_names = set(config.get("uploaded_filenames", []))
    results = []

    for i, f in enumerate(files):
        ts = f.get("start_time", 0)
        if ts > 1e12:
            ts = ts / 1000
        dur = f.get("duration", 0)
        if dur > 10000:
            dur = dur / 1000
        name = f.get("filename", "unknown").replace("/", "-").replace(":", "-")
        fid = f.get("id", "")

        from datetime import datetime as dt

        date_str = dt.fromtimestamp(ts).strftime("%Y-%m-%d_%H%M") if ts > 0 else "unknown"
        session_id = f"cloud_{date_str}_{name[:40]}".replace(" ", "_")
        ogg_path = RECORDINGS_DIR / f"{session_id}.ogg"

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

        try:
            url_data = cloud_api(f"/file/temp-url/{fid}", params={"is_opus": 1})
            url = url_data.get("temp_url_opus", url_data.get("temp_url", ""))
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
                mp3_path = RECORDINGS_DIR / f"{session_id}.mp3"
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
                ogg_path.write_bytes(ogg)
                duration = (len(raw) // 80) * 0.02
            else:
                raw_path = RECORDINGS_DIR / f"{session_id}.raw"
                raw_path.write_bytes(raw)
                results.append({"session_id": session_id, "status": "unknown_format", "cloud_name": name})
                continue

            conf = load_config()
            cids = conf.get("cloud_file_ids", {})
            cids[session_id] = fid
            conf["cloud_file_ids"] = cids
            save_config(conf)

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

    if file_size < 100:
        os.unlink(upload_path)
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

    try:
        os.unlink(upload_path)
    except Exception:
        pass

    return r.json()


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

    RECORDINGS_DIR.mkdir(exist_ok=True)
    config = load_config()
    custom_names = config.get("recording_names", {})
    uploaded = []

    for f in sorted(RECORDINGS_DIR.glob("*.ogg")):
        session_id = f.stem
        if session_id.startswith("cloud_"):
            continue

        rec_date = _extract_recording_date(session_id)
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

        rec_date = _extract_recording_date(session_id)
        rec_ts_ms = int(rec_date.timestamp() * 1000) if rec_date else None

        try:
            result = upload_to_cloud(str(f), display_name, duration, start_time_ms=rec_ts_ms)
            if not result.get("error"):
                conf = load_config()
                uploaded_names = conf.get("uploaded_filenames", [])
                if display_name not in uploaded_names:
                    uploaded_names.append(display_name)
                conf["uploaded_filenames"] = uploaded_names
                uploaded_ids = conf.get("uploaded_session_ids", [])
                if session_id not in uploaded_ids:
                    uploaded_ids.append(session_id)
                conf["uploaded_session_ids"] = uploaded_ids
                # Track cloud file ID for deletion support
                cloud_fid = result.get("data", result).get("id", result.get("id", ""))
                if cloud_fid:
                    cids = conf.get("cloud_file_ids", {})
                    cids[session_id] = cloud_fid
                    conf["cloud_file_ids"] = cids
                save_config(conf)
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
        data = cloud_api("/device/bind", method="POST", json_body={
            "sn": sn,
            "name": name,
            "model": model,
            "version_number": version_number,
        })
        if data.get("status") == 0 and data.get("data", {}).get("access_token"):
            token = data["data"]["access_token"]
            # Save per-device token in config
            config = load_config()
            device_tokens = config.get("device_bind_tokens", {})
            device_tokens[sn] = token
            config["device_bind_tokens"] = device_tokens
            save_config(config)
            return {"token": token, "sn": sn}
        return {"error": data.get("msg", "Bind failed"), "raw": data}
    except Exception as e:
        return {"error": str(e)}


def get_device_bind_token(sn):
    """Get the stored BIND_TOKEN for a device, or None if not available."""
    config = load_config()
    return config.get("device_bind_tokens", {}).get(sn)


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
                config = load_config()
                device_sigs = config.get("device_sn_signatures", {})
                device_sigs[sn] = sig
                config["device_sn_signatures"] = device_sigs
                save_config(config)
                return {"signature": sig, "sn": sn}
            return {"error": "No signature returned for this device"}
        return {"error": data.get("msg", "Failed to get signature"), "raw": data}
    except Exception as e:
        return {"error": str(e)}


def get_device_sn_signature(sn):
    """Get the stored SN_SIG for a device, or None if not available."""
    config = load_config()
    return config.get("device_sn_signatures", {}).get(sn)


# Load cloud token on import
load_cloud_token()
