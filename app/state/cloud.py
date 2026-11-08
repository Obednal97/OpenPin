"""Plaud Cloud account state and operations."""

import base64
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import requests as http_requests

from app.utils import pairing_store
from app.utils.audio import unwrap_ogg_to_raw_opus, wrap_opus_ogg
from app.utils.config import load_config, update_config
from app.utils.constants import (
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
    "login_started_at": None,  # time.time() when get_login_url issued desktop_uuid
    "device_id": None,  # stable per-install x-device-id (lazy, see _client_device_id)
    "timezone": None,  # system IANA timezone (lazy, see _system_timezone)
    "ut": None,  # pld_ut user token (~24h): what sn-sign wants. See refresh_user_token
    "urt": None,  # pld_urt refresh token (~30 days)
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


class PlaudAuthError(Exception):
    """Plaud answered, and said no: the token or sign-in code isn't valid.

    Raised only when Plaud itself rejects the credential (a non-zero
    ``status`` in the reply, or HTTP 401/403), never for network trouble or
    server errors, so callers can tell "signed out" from "offline".
    """


_DEVICE_ID_KEY = "plaud_client_device_id"


def _client_device_id():
    """Stable per-install id for the x-device-id header (16 hex, like the web app's).

    Generated once and kept in the config, so Plaud sees the same client
    across launches. Falls back to a per-process id if the config can't be written.
    """
    cached = _cloud_state.get("device_id")
    if cached:
        return cached
    device_id = ""
    try:
        device_id = load_config().get(_DEVICE_ID_KEY) or ""
    except Exception:
        pass
    if not device_id:
        device_id = uuid.uuid4().hex[:16]
        try:
            update_config(lambda c: c.setdefault(_DEVICE_ID_KEY, device_id))
        except Exception as e:
            logging.warning("[Cloud] Couldn't save the client device id: %s", e)
    _cloud_state["device_id"] = device_id
    return device_id


def _system_timezone():
    """The system's IANA timezone name (e.g. "Europe/London"), or "UTC" if unknown."""
    cached = _cloud_state.get("timezone")
    if cached:
        return cached
    tz = os.environ.get("TZ", "").lstrip(":")
    if not tz:
        try:
            target = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in target:
                tz = target.split("zoneinfo/", 1)[1]
        except Exception:
            tz = ""
    _cloud_state["timezone"] = tz or "UTC"
    return _cloud_state["timezone"]


def _auth_headers(token):
    """Request headers Plaud's API expects. Built fresh per request (new x-request-id)."""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
        "app-platform": "desktop",
        "app-language": "en",
        "edit-from": "desktop",
        "x-device-id": _client_device_id(),
        "timezone": _system_timezone(),
        "x-request-id": uuid.uuid4().hex,
        "User-Agent": "OpenPin",
    }
    id_hash = (_cloud_state.get("user") or {}).get("id_hash")
    if id_hash:  # known once /user/me has answered
        headers["x-pld-user"] = id_hash
    return headers


def _json_or_empty(response):
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


# Plaud's response-encryption key. Fixed and identical for every Plaud user (it
# ships in the Android app and the web app), so it's a constant, not a secret.
PLAUD_FERNET_KEY = b"8rRmMCskL3NaUiGr-UsmEEeI8NKH-ZfeGbpRGqSLpuI="


def _sn_sign_headers(token):
    """The Android app's header set, the only one under which a user (UT) token
    works as a bearer for /device/sn-sign.

    Under the desktop/web headers the same token gets HTTP 401 / -3900, and the
    workspace (WT) token gets -3901. X-Encrypt-Response asks for a Fernet body.
    Built fresh per call (new x-request-id).
    """
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "app-platform": "android",
        "app-version": "4.2.0 (1297)",
        "edit-from": "android",
        "app-language": "en",
        "User-Agent": "PLAUD/4.2.0(build:1297;Android 14)",
        "x-device-id": _client_device_id(),
        "x-request-id": uuid.uuid4().hex,
        "timezone": _system_timezone(),
        "X-Encrypt-Response": "1",
    }


def _decode_body(raw):
    """Parse a Plaud reply body that is either plain JSON or a Fernet blob.

    A Fernet token is URL-safe base64 starting "gAAAAAB". Returns the decoded
    dict, or {} when the body can't be read (never raises).
    """
    try:
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
        text = text.strip()
        if text.startswith("gAAAAAB"):
            from cryptography.fernet import Fernet

            text = Fernet(PLAUD_FERNET_KEY).decrypt(text.encode()).decode("utf-8")
        body = json.loads(text)
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _cookies_from_response(response):
    """Last non-empty ``pld_ut`` / ``pld_urt`` in a response's Set-Cookie headers.

    Plaud sends several Set-Cookie lines for pld_ut (Max-Age=0 "clearer"
    cookies first, the real token last), and requests' ``headers`` collapses
    repeats, so read urllib3's raw headers. Falls back to ``response.cookies``.
    Returns ``{"ut": str|None, "urt": str|None}``.
    """
    found = {"pld_ut": None, "pld_urt": None}
    lines = []
    try:
        raw_headers = response.raw.headers
        getter = getattr(raw_headers, "getlist", None) or getattr(raw_headers, "get_all", None)
        lines = list(getter("Set-Cookie") or []) if getter else []
    except Exception:
        lines = []
    for line in lines:
        name, sep, rest = str(line).strip().partition("=")
        name = name.strip().lower()
        if sep and name in found:
            value = rest.split(";", 1)[0].strip().strip('"')
            if value:
                found[name] = value  # later lines win
    if not lines:
        for name in found:
            try:
                found[name] = response.cookies.get(name) or None
            except Exception:
                pass
    return {"ut": found["pld_ut"], "urt": found["pld_urt"]}


def _plaud_msg(body):
    """Plaud's own explanation in a reply: ``msg``, or FastAPI's ``detail``."""
    if not isinstance(body, dict):
        return ""
    return str(body.get("msg") or body.get("detail") or "").strip()


def _region_redirect(body, domain):
    """The API domain a "user region mismatch" reply points to, or None."""
    if not isinstance(body, dict):
        return None
    if body.get("msg") == "user region mismatch" or body.get("status") == -302:
        correct = ((body.get("data") or {}).get("domains") or {}).get("api")
        if correct and correct != domain:
            return correct
    return None


def user_from_reply(body):
    """Decide whether a /user/me reply is a real signed-in user.

    Plaud answers HTTP 200 for most failures and puts the verdict in
    ``status`` (0 = OK). A reply only counts as a user when ``status`` is 0
    and ``data_user`` is a dict with an ``id``. Returns the few fields the
    app shows (id, nickname, email); raises PlaudAuthError with Plaud's
    message otherwise.
    """
    body = body if isinstance(body, dict) else {}
    user = body.get("data_user")
    if body.get("status") == 0 and isinstance(user, dict) and user.get("id"):
        summary = {"id": user["id"], "nickname": user.get("nickname") or "", "email": user.get("email") or ""}
        if user.get("id_hash"):  # sent back as the x-pld-user header
            summary["id_hash"] = user["id_hash"]
        return summary
    msg = _plaud_msg(body) or "no account in the reply"
    raise PlaudAuthError(f"Plaud rejected the token ({msg})")


def _try_token(token, api_domain=None):
    """Check a token against Plaud's /user/me.

    Returns ``(user, api_domain)``: the summary from user_from_reply() and
    the domain that answered (follows one "user region mismatch" redirect).
    Raises PlaudAuthError when Plaud rejects the token; network errors and
    5xx replies propagate as requests exceptions.
    """
    domain = api_domain or PLAUD_API_DOMAINS["eu"]
    for attempt in range(2):
        r = http_requests.get(f"{domain}/user/me", headers=_auth_headers(token), timeout=15)
        body = _json_or_empty(r)
        redirect = _region_redirect(body, domain) if attempt == 0 else None
        if redirect:
            logging.info("[Cloud] Region mismatch: checking the token at %s", redirect)
            domain = redirect
            continue
        if r.status_code in (401, 403):
            raise PlaudAuthError(f"Plaud rejected the token ({_plaud_msg(body) or f'HTTP {r.status_code}'})")
        r.raise_for_status()
        return user_from_reply(body), domain
    raise PlaudAuthError("Plaud rejected the token (region redirect loop)")


def _forget_saved_token(token):
    """Delete the saved token file if it still holds ``token``."""
    try:
        if PLAUD_CONFIG_PATH.exists() and json.loads(PLAUD_CONFIG_PATH.read_text()).get("token") == token:
            PLAUD_CONFIG_PATH.unlink()
    except Exception as e:
        logging.warning("[Cloud] Couldn't remove the rejected saved token: %s", e)


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
    """Background check of a token restored at startup.

    Valid: store the user summary (and save the token if asked, or if Plaud
    moved the account to another region). Rejected by Plaud: sign out and
    forget the saved token. Network trouble: leave the login as it is, so
    the app still starts signed in when offline.
    """
    try:
        user, domain = _try_token(token, api_domain)
    except PlaudAuthError as e:
        # The saved token is often just the ~24h pld_ut gone stale overnight:
        # the ~30 day pld_urt can renew it, so try that before signing out
        # (signing out deletes the saved file, refresh token included).
        with _cloud_state_lock:
            can_refresh = _cloud_state.get("token") == token and bool(_cloud_state.get("urt"))
        if can_refresh:
            refreshed = refresh_user_token()
            if refreshed.get("refreshed"):
                logging.info("[Cloud] Saved token had expired; renewed it with the refresh token")
                return
            if not refreshed.get("auth_failed"):
                # Network trouble or an odd reply: keep the login, try again later.
                logging.warning("[Cloud] Saved token rejected and refresh failed: %s", refreshed.get("error"))
                return
        with _cloud_state_lock:
            # Only sign out if this token is still the active one
            if _cloud_state.get("token") == token:
                _cloud_state["logged_in"] = False
                _cloud_state["token"] = None
                _cloud_state["user"] = None
        _forget_saved_token(token)
        logging.warning("[Cloud] Saved token rejected by Plaud; signed out. %s", e)
        return
    except Exception as e:
        logging.warning("[Cloud] Token validation failed (network?): %s", e)
        return
    with _cloud_state_lock:
        still_active = _cloud_state.get("token") == token
        if still_active:
            _cloud_state["user"] = user
    if still_active and (save_on_success or domain != api_domain):
        save_cloud_token(token, domain)


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
                    _cloud_state["ut"] = data.get("ut") or None
                    _cloud_state["urt"] = data.get("urt") or None
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


def save_cloud_token(token, api_domain=None, ut=None, urt=None):
    """Save Plaud cloud token atomically via a temp-file rename.

    ``ut`` / ``urt`` (pld_ut / pld_urt, see refresh_user_token) are optional;
    when omitted the ones already held in state are kept, so re-saving after a
    region switch doesn't drop them. Old files without them still load.
    """
    with _cloud_state_lock:
        if api_domain:
            _cloud_state["api_domain"] = api_domain
        ut = ut or _cloud_state.get("ut")
        urt = urt or _cloud_state.get("urt")
    data = {"token": token}
    if api_domain:
        data["api_domain"] = api_domain
    if ut:
        data["ut"] = ut
    if urt:
        data["urt"] = urt
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
    for attempt in range(2):
        domain = _cloud_state["api_domain"]
        headers = _auth_headers(_cloud_state["token"])  # fresh x-request-id per attempt
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
            correct_domain = _region_redirect(body, domain)
            if correct_domain:
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

        # A 401 with a refresh token on hand is most likely the ~24h pld_ut
        # having lapsed: refresh once and retry with the new token.
        if attempt == 0 and r.status_code == 401 and _cloud_state.get("urt"):
            if refresh_user_token().get("refreshed"):
                continue

        r.raise_for_status()
        return r.json()


def refresh_user_token():
    """Renew the ~24h pld_ut using the ~30 day pld_urt.

    POST /auth/refresh-user-token with the two cookies; the new pld_ut comes
    back in Set-Cookie (last non-empty one wins, see _cookies_from_response).
    The new token is checked on /user/me, then stored as ``ut`` and promoted to
    the primary token (the REST path accepts it) and saved.

    Returns ``{"refreshed": True, "user": ...}`` or ``{"refreshed": False, "error": ...}``;
    a failure also has ``auth_failed: True`` when Plaud itself refused (as
    opposed to network trouble).
    """
    with _cloud_state_lock:
        ut = _cloud_state.get("ut")
        urt = _cloud_state.get("urt")
        domain = _cloud_state["api_domain"]
    if not urt:
        return {"refreshed": False, "error": "no refresh token"}
    try:
        r = http_requests.post(
            f"{domain}/auth/refresh-user-token",
            headers={"Content-Type": "application/json", "app-platform": "desktop", "User-Agent": "OpenPin"},
            cookies={"pld_ut": ut or "", "pld_urt": urt},
            json={},
            timeout=15,
        )
        new_ut = _cookies_from_response(r)["ut"]
        if not new_ut:
            body = _decode_body(getattr(r, "content", b"") or b"")
            reason = _plaud_msg(body) or f"HTTP {r.status_code}"
            logging.warning("[Cloud] Token refresh gave no new token (%s)", reason)
            return {
                "refreshed": False,
                "error": f"Plaud didn't issue a new token ({reason})",
                # Plaud answered and said no (vs. a server error we can retry later)
                "auth_failed": r.status_code < 500,
            }
        user, domain = _try_token(new_ut, domain)
    except PlaudAuthError as e:
        return {"refreshed": False, "error": str(e), "auth_failed": True}
    except Exception as e:
        logging.warning("[Cloud] Token refresh failed: %s", e)
        return {"refreshed": False, "error": _cloud_call_error(e)}
    with _cloud_state_lock:
        _cloud_state["ut"] = new_ut
        _cloud_state["token"] = new_ut
        _cloud_state["api_domain"] = domain
        _cloud_state["user"] = user
    try:
        save_cloud_token(new_ut, domain, ut=new_ut, urt=urt)
    except Exception as e:
        logging.warning("[Cloud] Couldn't save the refreshed token: %s", e)
    logging.info("[Cloud] Refreshed the user token")
    return {"refreshed": True, "user": user}


_LOGIN_UUID_KEY = "plaud_login_desktop_uuid"
_LOGIN_STARTED_KEY = "plaud_login_started_at"
# How long a plaud://login link is accepted after the user starts a sign-in.
LOGIN_LINK_TTL_S = 15 * 60
UNREQUESTED_LINK_MESSAGE = (
    "Ignored a Plaud sign-in link because no sign-in was started from Open Pin "
    "in the last 15 minutes. To sign in, use Settings › Plaud Cloud."
)


def _pending_sign_in():
    """The desktop_uuid of a sign-in started in the last LOGIN_LINK_TTL_S, else None.

    Checks memory first, then the copy on disk (the link can arrive after a restart).
    """
    now = time.time()
    with _cloud_state_lock:
        uuid_mem = _cloud_state.get("desktop_uuid")
        started_mem = _cloud_state.get("login_started_at")
    if uuid_mem and isinstance(started_mem, (int, float)) and 0 <= now - started_mem <= LOGIN_LINK_TTL_S:
        return uuid_mem
    try:
        cfg = load_config()
    except Exception:
        return None
    uuid_disk = cfg.get(_LOGIN_UUID_KEY)
    started_disk = cfg.get(_LOGIN_STARTED_KEY)
    if (
        isinstance(uuid_disk, str)
        and uuid_disk
        and isinstance(started_disk, (int, float))
        and 0 <= now - started_disk <= LOGIN_LINK_TTL_S
    ):
        return uuid_disk
    return None


def _clear_pending_sign_in():
    """Forget the sign-in id once it has been used, so a later link can't reuse it."""
    with _cloud_state_lock:
        _cloud_state["desktop_uuid"] = None
        _cloud_state["login_started_at"] = None

    def _drop(c):
        c.pop(_LOGIN_UUID_KEY, None)
        c.pop(_LOGIN_STARTED_KEY, None)

    try:
        update_config(_drop)
    except Exception as e:
        logging.warning("[Cloud] Couldn't clear the sign-in id: %s", e)


def get_login_url(region="eu"):
    """Generate the OAuth login URL for Plaud web login."""
    import uuid as uuid_mod

    desktop_uuid = str(uuid_mod.uuid4())
    started_at = time.time()
    api_domain = PLAUD_API_DOMAINS.get(region, PLAUD_API_DOMAINS["eu"])
    with _cloud_state_lock:
        _cloud_state["desktop_uuid"] = desktop_uuid
        _cloud_state["login_started_at"] = started_at
        _cloud_state["api_domain"] = api_domain

    # Also on disk: the browser's plaud:// link can arrive after the backend
    # (or the whole app) restarted, and the exchange needs this id. It isn't a
    # secret (it's in the sign-in URL); it's only good with a fresh code.
    def _save(c):
        c[_LOGIN_UUID_KEY] = desktop_uuid
        c[_LOGIN_STARTED_KEY] = started_at

    try:
        update_config(_save)
    except Exception as e:
        logging.warning("[Cloud] Couldn't save the sign-in id: %s", e)
    login_url = f"{PLAUD_WEB_HOST}/launch-desktop?from=desktop&desktop_uuid={desktop_uuid}"
    return login_url, desktop_uuid


# Region in a sign-in code's suffix ("aws:eu-central-1") -> PLAUD_API_DOMAINS key.
# Only "eu-" has been checked against a real code; the others follow Plaud's
# domain names, and anything unrecognised keeps the current domain.
_AUTH_CODE_REGIONS = (("eu-", "eu"), ("us-", "global"), ("cn-", "cn"))

AUTH_CODE_EXPIRED_MESSAGE = "The sign-in code is invalid or has expired. Sign in again and use the new code."
# Plaud's messages for a sign-in code, and what the app says instead.
_AUTH_CODE_MESSAGES = {
    "授权码无效或已过期": AUTH_CODE_EXPIRED_MESSAGE,  # "auth code invalid or expired"
}


def _normalise_auth_code(code):
    """Trim a pasted sign-in code and undo URL encoding (``%3A%3A`` -> ``::``)."""
    code = (code or "").strip()
    if "%" in code:
        code = urllib.parse.unquote(code)
    return code


def looks_like_auth_code(text):
    """True for a Plaud sign-in code (``<random>::<base64 region>``), not a bearer token."""
    text = (text or "").strip()
    return not text.startswith("eyJ") and ("::" in text or "%3a%3a" in text.lower())


def _domain_for_auth_code(code):
    """The API domain named by a sign-in code's region suffix, or None if unsure."""
    if "::" not in code:
        return None
    suffix = code.rsplit("::", 1)[1].strip()
    if not suffix:
        return None
    try:
        padded = suffix + "=" * (-len(suffix) % 4)
        region = base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True).decode("ascii")
    except Exception:
        return None
    region = region.rsplit(":", 1)[-1].strip().lower()  # "aws:eu-central-1" -> "eu-central-1"
    for prefix, key in _AUTH_CODE_REGIONS:
        if region.startswith(prefix):
            return PLAUD_API_DOMAINS[key]
    return None


def _auth_code_error(body, http_status):
    msg = _plaud_msg(body)
    if msg in _AUTH_CODE_MESSAGES:
        return _AUTH_CODE_MESSAGES[msg]
    if msg:
        return f"Plaud didn't accept the sign-in code ({msg})."
    return f"Plaud didn't return a token for this sign-in code (HTTP {http_status})."


def _sign_in(token, api_domain, user, ut=None, urt=None):
    """Make a checked token the active login and save it (with pld_ut/pld_urt if known)."""
    with _cloud_state_lock:
        _cloud_state["ut"] = ut or None
        _cloud_state["urt"] = urt or None
        _cloud_state["token"] = token
        _cloud_state["api_domain"] = api_domain
        _cloud_state["logged_in"] = True
        _cloud_state["user"] = user
    save_cloud_token(token, api_domain, ut=ut, urt=urt)
    return {"logged_in": True, "user": user}


def exchange_token(auth_code, desktop_uuid=None, *, fallback_domain=None, from_link=False):
    """Exchange a sign-in code for a bearer token, check it, and sign in.

    Returns ``{"logged_in": True, "user": ...}`` or ``{"error": ...}`` with
    Plaud's reason. Nothing is saved, and the current login is untouched,
    unless Plaud both issues a token and accepts it on /user/me.

    ``from_link``: the code came from a plaud://login link, which any web page
    can open. It is only used when the user started a sign-in in the last
    LOGIN_LINK_TTL_S, and with the id that sign-in issued. A code the user
    pasted themselves is accepted any time.
    """
    code = _normalise_auth_code(auth_code)
    if not code:
        return {"error": "No sign-in code provided"}
    if from_link:
        pending = _pending_sign_in()
        if not pending:
            logging.warning("[Cloud] Ignored a plaud:// sign-in link: no sign-in in progress")
            return {"error": UNREQUESTED_LINK_MESSAGE, "unrequested": True}
        desktop_uuid = pending
    with _cloud_state_lock:
        desktop_uuid = desktop_uuid or _cloud_state.get("desktop_uuid") or ""
        domain = _domain_for_auth_code(code) or fallback_domain or _cloud_state["api_domain"]
    if not desktop_uuid:
        try:
            desktop_uuid = load_config().get(_LOGIN_UUID_KEY) or ""
        except Exception:
            desktop_uuid = ""
    r = http_requests.post(
        f"{domain}/auth/access-token-auth-code",
        json={"client_id": "web", "auth_code": code, "desktop_uuid": desktop_uuid},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if r.status_code >= 500:
        r.raise_for_status()
    body = _json_or_empty(r)
    token = body.get("access_token") or body.get("token") or ""
    if not token or body.get("status") not in (None, 0):
        return {"error": _auth_code_error(body, r.status_code)}

    try:
        user, domain = _try_token(token, domain)
    except PlaudAuthError as e:
        return {"error": str(e)}
    cookies = _cookies_from_response(r)  # best effort: not every sign-in sets them
    result = _sign_in(token, domain, user, ut=cookies["ut"], urt=cookies["urt"])
    _clear_pending_sign_in()
    return result


def login_with_token(token, region="eu"):
    """Sign in with a pasted bearer token (or a pasted sign-in code).

    Returns ``{"logged_in": True, "user": ...}`` or ``{"error": ...}`` when
    Plaud rejects it; network errors raise. A rejected token is never saved
    and leaves the current login as it was.
    """
    token = (token or "").strip()
    # Strip "Bearer " prefix if pasted from browser local storage
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    api_domain = PLAUD_API_DOMAINS.get(region, PLAUD_API_DOMAINS["eu"])
    if looks_like_auth_code(token):
        # The sign-in page's code pasted into the token field: exchange it.
        return exchange_token(token, fallback_domain=api_domain)
    try:
        user, api_domain = _try_token(token, api_domain)
    except PlaudAuthError as e:
        return {"error": str(e)}
    return _sign_in(token, api_domain, user)


def logout():
    """Log out from Plaud cloud."""
    with _cloud_state_lock:
        _cloud_state["logged_in"] = False
        _cloud_state["token"] = None
        _cloud_state["user"] = None
        _cloud_state["ut"] = None
        _cloud_state["urt"] = None
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


def _cloud_call_error(exc):
    """Readable text for a failed cloud_api() call, with Plaud's msg when it sent one."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        msg = _plaud_msg(_json_or_empty(resp))
        if resp.status_code in (401, 403):
            reason = msg or "token invalid or expired"
            return f"Plaud rejected the sign-in ({reason}). Sign in again under Settings › Plaud Cloud."
        if msg:
            return f"Plaud Cloud error {resp.status_code} ({msg})"
    return str(exc)


def _resolve_bind_token():
    """The BLE BIND_TOKEN: the account's user id (32 hex), or None if it can't be found.

    Plaud's bind reply carries no token; the token is the user id. It is the
    login JWT's ``sub`` claim (no network needed), with /user/me's
    ``data_user.id`` as the fallback.
    """
    try:
        payload = (_cloud_state["token"] or "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        sub = claims.get("sub")
        if isinstance(sub, str) and sub:
            return sub
    except Exception as e:
        logging.info("[Cloud] No user id in the login token (%s); asking /user/me", e)
    try:
        user_id = (cloud_api("/user/me").get("data_user") or {}).get("id")
        if isinstance(user_id, str) and user_id:
            return user_id
    except Exception as e:
        logging.warning("[Cloud] Couldn't read the user id from /user/me: %s", e)
    return None


def bind_device(sn, name="Plaud NotePin S", model="882", version_number=65546):
    """Bind a device to the user's Plaud cloud account.

    Returns the BIND_TOKEN needed for the BLE handshake. Plaud's bind reply
    has no token field: the bind call registers the device on the account,
    and the token is the account's user id (see _resolve_bind_token).
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
        if data.get("status") == 0:
            token = _resolve_bind_token()
            if not token:
                return {"error": "Plaud registered this NotePin but your account id couldn't be read; try again"}
            # Save per-device token in config
            update_config(lambda c: c.setdefault("device_bind_tokens", {}).__setitem__(sn, token))
            pairing_store.backup_serial_async(sn, name)  # Keychain copy once both credentials exist
            return {"token": token, "sn": sn}
        reason = _plaud_msg(data) or f"status {data.get('status')}"
        return {"error": f"Plaud wouldn't register this NotePin ({reason})", "status": data.get("status")}
    except Exception as e:
        return {"error": _cloud_call_error(e)}


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


# sn-sign's answer to a sign-in token of the wrong kind ("token type does not
# match parse mode"): it wants the user token (pld_ut), not the web session one.
SN_SIGN_WRONG_TOKEN_STATUS = -3901


def fetch_sn_signature(sn):
    """Fetch the SN_SIG (snSignature) for a device from the cloud API.

    POST /device/sn-sign with {"sns": ["<serial_number>"]} (plural: a singular
    "sn" returns status 0 with an empty signature). Returns the per-device RSA
    signature needed for the BLE handshake and stores it in config for offline use.

    Plaud answers -3901 ("token type does not match parse mode") for the web
    app's workspace (WT) token: sn-sign wants the user token (pld_ut), so this
    prefers ``ut`` over the REST token. It only works under the Android app's
    header set (see _sn_sign_headers), and with X-Encrypt-Response the reply is
    a Fernet blob (decoded by _decode_body). A stale ut (HTTP 401 / -3900) is
    refreshed once with the stored pld_urt and the request retried.
    """
    if not _cloud_state["logged_in"] or not _cloud_state["token"]:
        return {"error": "Not logged in to Plaud cloud"}

    try:
        refreshed = False
        while True:
            token = _cloud_state.get("ut") or _cloud_state["token"]
            domain = _cloud_state["api_domain"]
            r = http_requests.post(
                f"{domain}/device/sn-sign",
                headers=_sn_sign_headers(token),
                json={"sns": [sn]},
                timeout=30,
            )
            data = _decode_body(r.content)
            stale = r.status_code == 401 or data.get("status") == -3900
            if stale and not refreshed and _cloud_state.get("urt"):
                refreshed = True
                if refresh_user_token().get("refreshed"):
                    continue
            break
        r.raise_for_status()  # HTTP errors surface as before (e.g. 401 -> "sign in again")
        if data.get("status") == 0:
            signatures = (data.get("data") or {}).get("signature") or {}
            sig = signatures.get(sn)
            if sig:
                update_config(lambda c: c.setdefault("device_sn_signatures", {}).__setitem__(sn, sig))
                pairing_store.backup_serial_async(sn)  # Keychain copy once both credentials exist
                return {"signature": sig, "sn": sn}
            return {"error": "Plaud returned no signature for this NotePin"}
        reason = _plaud_msg(data) or f"status {data.get('status')}"
        return {"error": f"Plaud wouldn't sign this NotePin's serial ({reason})", "status": data.get("status")}
    except Exception as e:
        return {"error": _cloud_call_error(e)}


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
