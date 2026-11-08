"""Google Drive integration — OAuth, upload, sync tracking."""

import base64
import hashlib
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests as http_requests

from app.utils import oauth_page
from app.utils.config import load_config, update_config
from app.utils.constants import (
    GDRIVE_API_BASE,
    GDRIVE_AUTH_ENDPOINT,
    GDRIVE_CALLBACK_PORT,
    GDRIVE_CLIENT_ID,
    GDRIVE_CLIENT_SECRET,
    GDRIVE_CONFIG_PATH,
    GDRIVE_SCOPE,
    GDRIVE_TOKEN_ENDPOINT,
)
from app.utils.paths import is_auxiliary_stem

# Module-level state
_gdrive_state = {
    "connected": False,
    "access_token": None,
    "refresh_token": None,
    "token_expires_at": 0,
    "user_email": None,
    "folder_id": None,
    "folder_name": None,
    # Transient (not persisted)
    "code_verifier": None,
    "state": None,
    "client_id": None,
    "client_secret": None,
}


def get_state():
    """Get a read-only snapshot of Google Drive state."""
    return {
        "connected": _gdrive_state["connected"],
        "user_email": _gdrive_state.get("user_email"),
        "folder_id": _gdrive_state.get("folder_id"),
        "folder_name": _gdrive_state.get("folder_name"),
    }


def is_connected():
    return _gdrive_state["connected"]


def get_folder_id():
    return _gdrive_state.get("folder_id")


# ---- Persistence ----


def load():
    """Load saved Google Drive OAuth tokens."""
    if GDRIVE_CONFIG_PATH.exists():
        try:
            data = json.loads(GDRIVE_CONFIG_PATH.read_text())
            for k in (
                "access_token",
                "refresh_token",
                "token_expires_at",
                "user_email",
                "folder_id",
                "folder_name",
                "client_id",
                "client_secret",
            ):
                if k in data:
                    _gdrive_state[k] = data[k]
            if _gdrive_state.get("access_token"):
                _gdrive_state["connected"] = True
        except Exception:
            pass


def save():
    """Persist Google Drive tokens and config."""
    save_data = {}
    for k in (
        "access_token",
        "refresh_token",
        "token_expires_at",
        "user_email",
        "folder_id",
        "folder_name",
        "client_id",
        "client_secret",
    ):
        if _gdrive_state.get(k) is not None:
            save_data[k] = _gdrive_state[k]
    GDRIVE_CONFIG_PATH.write_text(json.dumps(save_data, indent=2))


# ---- PKCE ----


def _generate_pkce():
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    return code_verifier, code_challenge


# ---- Token Management ----


def _get_client_id():
    """Get the OAuth client ID — from state (user-provided) or embedded constant."""
    return _gdrive_state.get("client_id") or GDRIVE_CLIENT_ID


def _get_client_secret():
    return _gdrive_state.get("client_secret") or GDRIVE_CLIENT_SECRET


def refresh_token():
    """Refresh the access token using the refresh token."""
    if not _gdrive_state.get("refresh_token"):
        return False
    try:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": _gdrive_state["refresh_token"],
            "client_id": _get_client_id(),
        }
        secret = _get_client_secret()
        if secret:
            payload["client_secret"] = secret
        r = http_requests.post(GDRIVE_TOKEN_ENDPOINT, data=payload, timeout=15)
        r.raise_for_status()
        tokens = r.json()
        _gdrive_state["access_token"] = tokens["access_token"]
        if tokens.get("refresh_token"):
            _gdrive_state["refresh_token"] = tokens["refresh_token"]
        _gdrive_state["token_expires_at"] = time.time() + tokens.get("expires_in", 3600)
        _gdrive_state["connected"] = True
        save()
        return True
    except Exception as e:
        logging.warning(f"Google Drive token refresh failed: {e}")
        _gdrive_state["connected"] = False
        return False


def _ensure_fresh_token():
    """Proactively refresh if within 5 minutes of expiry."""
    expires = _gdrive_state.get("token_expires_at", 0)
    if expires > 0 and time.time() > expires - 300:
        logging.info("Google Drive token expiring soon, refreshing")
        refresh_token()


# ---- API Calls ----


def _api_call(method, path, **kwargs):
    """Make an authenticated Google API call with auto-refresh."""
    _ensure_fresh_token()

    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {_gdrive_state['access_token']}"

    url = f"{GDRIVE_API_BASE}{path}" if path.startswith("/") else path

    r = http_requests.request(method, url, headers=headers, timeout=30, **kwargs)

    if r.status_code == 401:
        if refresh_token():
            headers["Authorization"] = f"Bearer {_gdrive_state['access_token']}"
            r = http_requests.request(method, url, headers=headers, timeout=30, **kwargs)
        else:
            _gdrive_state["connected"] = False
            raise ValueError("Google Drive session expired — please reconnect")

    r.raise_for_status()
    return r


# ---- OAuth Flow ----


def start_connect(client_id=None, client_secret=None):
    """Start the Google OAuth flow — returns the authorization URL."""
    if client_id:
        _gdrive_state["client_id"] = client_id
    if client_secret:
        _gdrive_state["client_secret"] = client_secret

    cid = _get_client_id()
    if not cid:
        raise ValueError("No Google OAuth Client ID configured")

    code_verifier, code_challenge = _generate_pkce()
    _gdrive_state["code_verifier"] = code_verifier

    import uuid as uuid_mod

    state = str(uuid_mod.uuid4())
    _gdrive_state["state"] = state

    params = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": f"http://localhost:{GDRIVE_CALLBACK_PORT}/gdrive/callback",
        "scope": GDRIVE_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{GDRIVE_AUTH_ENDPOINT}?{urlencode(params)}"

    _start_callback_server()

    return auth_url


def _start_callback_server():
    """Start a temporary HTTP server to handle the OAuth callback."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class CallbackHandler(BaseHTTPRequestHandler):
        def _signal(self, result):
            """Put result onto the queue without blocking if it's already full."""
            try:
                type(self)._result_queue.put_nowait(result)
            except queue.Full:
                pass  # First result already wins; ignore duplicates

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/gdrive/callback":
                params = parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                error = params.get("error", [None])[0]

                if error:
                    if error == "access_denied":
                        oauth_page.send(self, "gdrive", "cancelled")
                    else:
                        desc = params.get("error_description", [""])[0]
                        oauth_page.send(self, "gdrive", "failed", f"{error}: {desc}" if desc else error)
                    self._signal({"error": error})
                    return

                if state != _gdrive_state.get("state"):
                    oauth_page.send(self, "gdrive", "expired")
                    self._signal({"error": "csrf_mismatch"})
                    return

                try:
                    # Exchange code for tokens
                    payload = {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": f"http://localhost:{GDRIVE_CALLBACK_PORT}/gdrive/callback",
                        "code_verifier": _gdrive_state["code_verifier"],
                        "client_id": _get_client_id(),
                    }
                    secret = _get_client_secret()
                    if secret:
                        payload["client_secret"] = secret

                    r = http_requests.post(GDRIVE_TOKEN_ENDPOINT, data=payload, timeout=15)
                    r.raise_for_status()
                    tokens = r.json()

                    _gdrive_state["access_token"] = tokens["access_token"]
                    _gdrive_state["refresh_token"] = tokens.get("refresh_token", "")
                    _gdrive_state["token_expires_at"] = time.time() + tokens.get("expires_in", 3600)
                    _gdrive_state["code_verifier"] = None
                    _gdrive_state["state"] = None

                    # Fetch user email
                    try:
                        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
                        info = http_requests.get(
                            "https://www.googleapis.com/oauth2/v2/userinfo",
                            headers=headers,
                            timeout=10,
                        ).json()
                        _gdrive_state["user_email"] = info.get("email", "")
                    except Exception:
                        pass

                    _gdrive_state["connected"] = True
                    save()

                    oauth_page.send(self, "gdrive", "connected")
                    self._signal({"ok": True})
                except Exception as e:
                    logging.error(f"Google Drive token exchange failed: {e}")
                    oauth_page.send(self, "gdrive", "failed", str(e))
                    self._signal({"error": str(e)})
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    result_queue: queue.Queue = queue.Queue(maxsize=1)
    CallbackHandler._result_queue = result_queue  # type: ignore[attr-defined]

    def run_server():
        server = HTTPServer(("127.0.0.1", GDRIVE_CALLBACK_PORT), CallbackHandler)
        server.timeout = 1
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and result_queue.empty():
            server.handle_request()
        server.server_close()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()


def disconnect():
    """Disconnect from Google Drive."""
    _gdrive_state["connected"] = False
    _gdrive_state["access_token"] = None
    _gdrive_state["refresh_token"] = None
    _gdrive_state["token_expires_at"] = 0
    _gdrive_state["user_email"] = None
    _gdrive_state["folder_id"] = None
    _gdrive_state["folder_name"] = None
    if GDRIVE_CONFIG_PATH.exists():
        GDRIVE_CONFIG_PATH.unlink()
    return {"disconnected": True}


# ---- Folder Operations ----


def list_folders(query=""):
    """Search for folders in the user's Drive."""
    q = "mimeType='application/vnd.google-apps.folder' and trashed=false"
    if query:
        escaped_query = query.replace("\\", "\\\\").replace("'", "\\'")
        q += f" and name contains '{escaped_query}'"
    r = _api_call(
        "GET",
        "/drive/v3/files",
        params={"q": q, "fields": "files(id,name)", "pageSize": 20},
    )
    return r.json().get("files", [])


def create_folder(name):
    """Create a new folder in the user's Drive root."""
    r = _api_call(
        "POST",
        "/drive/v3/files",
        json={"name": name, "mimeType": "application/vnd.google-apps.folder"},
    )
    data = r.json()
    return {"id": data["id"], "name": data.get("name", name)}


def set_folder(folder_id, folder_name=""):
    """Set the target folder for uploads."""
    _gdrive_state["folder_id"] = folder_id
    _gdrive_state["folder_name"] = folder_name
    save()


# ---- Upload ----


def upload_file(file_path):
    """Upload a file to the configured Google Drive folder.

    Uses simple upload for files < 5MB, resumable for larger.
    Returns the Drive file ID.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    folder_id = _gdrive_state.get("folder_id")
    if not folder_id:
        raise ValueError("No Google Drive folder selected")

    file_size = path.stat().st_size
    file_name = path.name

    if file_size < 5 * 1024 * 1024:
        # Simple multipart upload for small files
        return _simple_upload(path, file_name, folder_id)
    else:
        # Resumable upload for larger files
        return _resumable_upload(path, file_name, folder_id, file_size)


def _simple_upload(path, file_name, folder_id):
    """Simple multipart upload for files < 5MB."""
    _ensure_fresh_token()

    metadata = json.dumps({"name": file_name, "parents": [folder_id]})
    file_data = path.read_bytes()

    boundary = "plaud_v2_boundary"
    body = (
        (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{metadata}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: audio/ogg\r\n\r\n"
        ).encode()
        + file_data
        + f"\r\n--{boundary}--".encode()
    )

    r = http_requests.post(
        f"{GDRIVE_API_BASE}/upload/drive/v3/files?uploadType=multipart",
        headers={
            "Authorization": f"Bearer {_gdrive_state['access_token']}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        data=body,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["id"]


def _resumable_upload(path, file_name, folder_id, file_size):
    """Resumable upload for larger files (> 5MB)."""
    _ensure_fresh_token()

    # Initiate resumable session
    r = http_requests.post(
        f"{GDRIVE_API_BASE}/upload/drive/v3/files?uploadType=resumable",
        headers={
            "Authorization": f"Bearer {_gdrive_state['access_token']}",
            "Content-Type": "application/json",
            "X-Upload-Content-Type": "audio/ogg",
            "X-Upload-Content-Length": str(file_size),
        },
        json={"name": file_name, "parents": [folder_id]},
        timeout=15,
    )
    r.raise_for_status()
    upload_url = r.headers["Location"]

    # Upload in 256KB chunks
    chunk_size = 256 * 1024
    sent = 0
    with open(path, "rb") as f:
        while sent < file_size:
            chunk = f.read(chunk_size)
            end = sent + len(chunk) - 1
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {sent}-{end}/{file_size}",
            }
            r = http_requests.put(upload_url, headers=headers, data=chunk, timeout=60)
            sent += len(chunk)

            if r.status_code == 200 or r.status_code == 201:
                return r.json()["id"]
            elif r.status_code != 308:
                r.raise_for_status()

    raise ValueError("Upload did not complete")


# ---- Sync Tracking ----


def get_synced_files():
    """Get the dict of session_id → sync info from config.

    Keys are always ``str``: the config is JSON, so ids come back as strings
    even if a caller once stored an int, and callers may pass either.
    """
    synced = load_config().get("gdrive_synced_files", {})
    if not isinstance(synced, dict):
        return {}
    return {str(k): v for k, v in synced.items()}


def is_synced(session_id):
    """True if ``session_id`` (int or str) has been uploaded to Drive.

    Device session ids are ints at some call sites; comparing an int against
    the JSON string keys was always False, so "Cloud" storage mode re-uploaded
    (and re-downloaded) the same recording forever.
    """
    return str(session_id) in get_synced_files()


def mark_synced(session_id, drive_file_id, file_size=0):
    entry = {
        "drive_file_id": drive_file_id,
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "file_size": file_size,
    }

    def _mark(cfg):
        synced = cfg.get("gdrive_synced_files")
        if not isinstance(synced, dict):
            synced = cfg["gdrive_synced_files"] = {}
        synced[str(session_id)] = entry

    update_config(_mark)


def _dismissed_ids(config):
    return {str(x) for x in config.get("deleted_ids", [])}


def sync_all():
    """Upload all un-synced recordings. Returns summary."""
    from app.utils.constants import get_all_recordings_dirs

    if not is_connected() or not get_folder_id():
        return {"error": "Not connected or no folder selected"}

    dismissed = _dismissed_ids(load_config())
    synced = get_synced_files()

    all_ogg = []
    for rec_dir in get_all_recordings_dirs():
        if rec_dir.exists():
            # Skip crop backups / temp files (<sid>_uncropped.ogg, <sid>_crop_tmp.ogg)
            all_ogg.extend(f for f in rec_dir.glob("*.ogg") if not is_auxiliary_stem(f.stem))

    unsynced = [f for f in all_ogg if f.stem not in dismissed and f.stem not in synced]

    uploaded = []
    errors = []
    for f in unsynced:
        try:
            drive_id = upload_file(str(f))
            mark_synced(f.stem, drive_id, f.stat().st_size)
            uploaded.append(f.stem)
        except Exception as e:
            logging.warning(f"Google Drive upload failed for {f.stem}: {e}")
            errors.append({"session_id": f.stem, "error": str(e)})

    return {"uploaded": len(uploaded), "errors": errors, "total": len(unsynced)}


def get_sync_status():
    """Get counts of synced vs total recordings."""
    from app.utils.constants import get_all_recordings_dirs

    dismissed = _dismissed_ids(load_config())
    synced = get_synced_files()

    total = 0
    synced_count = 0
    for rec_dir in get_all_recordings_dirs():
        if rec_dir.exists():
            for f in rec_dir.glob("*.ogg"):
                if is_auxiliary_stem(f.stem):
                    continue
                if f.stem not in dismissed:
                    total += 1
                    if f.stem in synced:
                        synced_count += 1

    return {"total": total, "synced": synced_count, "unsynced": total - synced_count}


# ---- Auto-Upload + Cloud Streaming ----


def maybe_auto_upload(session_id, ogg_path):
    """Auto-upload a recording if storage mode requires it.

    Called after BLE/WiFi sync saves a new file.
    In 'cloud' mode, deletes the local file after upload.
    """
    config = load_config()
    mode = config.get("storage_mode", "local")
    if mode == "local":
        return

    if not is_connected() or not get_folder_id():
        return

    if is_synced(session_id):
        return

    try:
        drive_id = upload_file(str(ogg_path))
        file_size = Path(ogg_path).stat().st_size if Path(ogg_path).exists() else 0
        mark_synced(session_id, drive_id, file_size)
        logging.info(f"[GDrive] Auto-uploaded {session_id} (mode={mode})")

        if mode == "cloud":
            # Delete local file to save space — transcripts stay
            p = Path(ogg_path)
            if p.exists():
                p.unlink()
                logging.info(f"[GDrive] Deleted local {session_id}.ogg (cloud mode)")
    except Exception as e:
        logging.warning(f"[GDrive] Auto-upload failed for {session_id}: {e}")


def proxy_stream(drive_file_id):
    """Stream a file from Google Drive. Returns (data_bytes, content_type)."""
    _ensure_fresh_token()
    r = http_requests.get(
        f"{GDRIVE_API_BASE}/drive/v3/files/{drive_file_id}?alt=media",
        headers={"Authorization": f"Bearer {_gdrive_state['access_token']}"},
        timeout=60,
    )
    r.raise_for_status()
    return r.content, r.headers.get("Content-Type", "audio/ogg")


def get_cloud_only_recordings():
    """Return recordings that exist in Drive but not locally.

    These need to be shown in the recording list with a cloud-only flag.
    """
    from app.utils.constants import get_all_recordings_dirs

    synced = get_synced_files()
    if not synced:
        return []

    # Find all local session IDs
    local_ids = set()
    for rec_dir in get_all_recordings_dirs():
        if rec_dir.exists():
            for f in rec_dir.glob("*.ogg"):
                local_ids.add(f.stem)

    dismissed = _dismissed_ids(load_config())

    cloud_only = []
    for sid, info in synced.items():
        # Crop backups uploaded by older versions are not recordings of their own.
        if is_auxiliary_stem(sid) or not isinstance(info, dict):
            continue
        if sid not in local_ids and sid not in dismissed:
            cloud_only.append(
                {
                    "session_id": sid,
                    "drive_file_id": info.get("drive_file_id", ""),
                    "synced_at": info.get("synced_at", ""),
                    "file_size": info.get("file_size", 0),
                }
            )

    return cloud_only


# Load saved state on import
load()
