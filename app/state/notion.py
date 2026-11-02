"""Notion MCP integration state and operations."""

import base64
import hashlib
import html as _html
import json
import logging
import os
import queue
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import requests as http_requests

from app.utils.constants import NOTION_CALLBACK_PORT, NOTION_CONFIG_PATH, NOTION_MCP_URL

# Module-level state
_notion_state = {
    "connected": False,
    "access_token": None,
    "refresh_token": None,
    "client_id": None,
    "client_secret": None,
    "token_endpoint": None,
    "code_verifier": None,
    "state": None,
    "database_id": None,
    "database_name": None,
    # RFC 8707 resource indicator (the MCP server's canonical URI), sent on
    # every authorize/token request. From the protected-resource metadata.
    "resource": None,
    # True once the authorization server rejected our refresh token: retrying
    # cannot help, the user has to reconnect. Persisted so a restart doesn't
    # hammer the token endpoint again.
    "refresh_dead": False,
}

# MCP protocol version we ask for in ``initialize``. 2025-11-25 is the newest
# version whose initialize + Mcp-Session-Id model this client implements, and
# Notion's endpoint supports it. The server may answer with another version it
# supports; we then send whatever it negotiated in MCP-Protocol-Version.
MCP_PROTOCOL_VERSION = "2025-11-25"

_notion_session_id = None
_negotiated_protocol_version = None

# Guards the token refresh and the MCP session globals. mcp_call runs on both
# Flask request threads and the notion_queue worker thread, so two threads
# could otherwise refresh with the same (single-use, rotating) refresh token,
# or initialise two sessions and overwrite each other's ids.
_auth_lock = threading.RLock()


def get_state():
    """Get a read-only snapshot of Notion state."""
    return {
        "connected": _notion_state["connected"],
        "database_id": _notion_state.get("database_id"),
        "database_name": _notion_state.get("database_name"),
        "needs_reconnect": needs_reconnect(),
    }


def is_connected():
    return _notion_state["connected"]


def needs_reconnect():
    """True when we hold (or held) a Notion login that can no longer be renewed.

    The UI then shows "Reconnect Notion", and the push queue stops probing.
    """
    if _notion_state.get("refresh_dead"):
        return True
    return bool(_notion_state.get("access_token")) and not _notion_state["connected"]


def verify_connection():
    """Check if the Notion token is still valid. Returns True if connected, False if not.
    Proactively refreshes if expiring soon; mcp_init refreshes once on a 401."""
    if not _notion_state.get("access_token"):
        _notion_state["connected"] = False
        return False
    _ensure_fresh_token()
    try:
        mcp_init()
        _notion_state["connected"] = True
        return True
    except Exception:
        _notion_state["connected"] = False
        return False


def load():
    """Load saved Notion OAuth tokens."""
    if NOTION_CONFIG_PATH.exists():
        try:
            data = json.loads(NOTION_CONFIG_PATH.read_text())
            _notion_state.update(data)
            if _notion_state.get("access_token") and not _notion_state.get("refresh_dead"):
                _notion_state["connected"] = True
        except Exception:
            pass


def save():
    """Persist Notion tokens and config."""
    save_data = {k: v for k, v in _notion_state.items() if k != "connected" and k != "code_verifier" and k != "state"}
    NOTION_CONFIG_PATH.write_text(json.dumps(save_data, indent=2))


def _discover():
    """Discover Notion MCP OAuth endpoints (RFC 9728 + RFC 8414).

    Returns the authorization-server metadata plus ``"resource"``: the MCP
    server's canonical URI from its protected-resource metadata, used as the
    RFC 8707 ``resource`` parameter.
    """
    base = "https://mcp.notion.com"
    path = urlparse(NOTION_MCP_URL).path.rstrip("/")
    # MCP spec: try the path-specific well-known URI first, then the root one.
    resource_meta = None
    for url in (f"{base}/.well-known/oauth-protected-resource{path}", f"{base}/.well-known/oauth-protected-resource"):
        try:
            r = http_requests.get(url, timeout=10)
        except Exception:
            continue
        if r.status_code == 200:
            resource_meta = r.json()
            break
    if resource_meta is None:
        raise ValueError("Could not read Notion MCP protected-resource metadata")
    auth_server = (resource_meta.get("authorization_servers") or [None])[0]
    if not auth_server:
        raise ValueError("No authorization server found in Notion MCP metadata")

    r = http_requests.get(f"{auth_server.rstrip('/')}/.well-known/oauth-authorization-server", timeout=10)
    r.raise_for_status()
    meta = r.json()
    meta["resource"] = resource_meta.get("resource") or NOTION_MCP_URL
    return meta


def _resource():
    """The RFC 8707 resource indicator for authorize/token requests."""
    return _notion_state.get("resource") or NOTION_MCP_URL


def _generate_pkce():
    """Generate PKCE code_verifier and code_challenge."""
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    return code_verifier, code_challenge


def _register_client(registration_endpoint):
    """Dynamic client registration (RFC 7591)."""
    r = http_requests.post(
        registration_endpoint,
        json={
            "client_name": "Plaud V2",
            "redirect_uris": [f"http://localhost:{NOTION_CALLBACK_PORT}/notion/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("client_id"), data.get("client_secret", "")


_token_expires_at = 0  # Unix timestamp when the access token expires


def _refresh_rejected(response):
    """True if the token endpoint said the refresh token itself is no good.

    OAuth answers 400 ``invalid_grant`` (or 401 ``invalid_client``) for a
    revoked, expired or already-rotated refresh token. 5xx responses and
    network errors are transient and must not count.
    """
    if response is None:
        return False
    if response.status_code == 401:
        return True
    if response.status_code == 400:
        try:
            err = (response.json() or {}).get("error", "")
        except ValueError:
            err = ""
        return err in ("invalid_grant", "invalid_client", "unauthorized_client")
    return False


def refresh_token(stale_access_token=None):
    """Refresh the access token using the refresh token.

    ``stale_access_token`` is the token a caller just saw rejected. If another
    thread already replaced it, there's nothing to do: refresh tokens rotate,
    so a second refresh with the old one would fail and log the user out.
    """
    global _token_expires_at, _notion_session_id
    with _auth_lock:
        current = _notion_state.get("access_token")
        if stale_access_token and current and current != stale_access_token:
            return True
        if _notion_state.get("refresh_dead"):
            return False
        if not _notion_state.get("refresh_token") or not _notion_state.get("token_endpoint"):
            return False
        r = None
        try:
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": _notion_state["refresh_token"],
                "client_id": _notion_state["client_id"],
                "resource": _resource(),
            }
            if _notion_state.get("client_secret"):
                payload["client_secret"] = _notion_state["client_secret"]
            r = http_requests.post(_notion_state["token_endpoint"], data=payload, timeout=15)
            r.raise_for_status()
            tokens = r.json()
            _notion_state["access_token"] = tokens["access_token"]
            if tokens.get("refresh_token"):
                _notion_state["refresh_token"] = tokens["refresh_token"]
            _notion_state["connected"] = True
            _notion_state["refresh_dead"] = False
            _token_expires_at = time.time() + tokens.get("expires_in", 3600)
            # New token, new MCP session.
            _notion_session_id = None
            save()
            return True
        except Exception as e:
            logging.warning(f"Notion token refresh failed: {e}")
            _notion_state["connected"] = False
            if _refresh_rejected(r):
                _notion_state["refresh_dead"] = True
                try:
                    save()
                except Exception:
                    pass
            return False


def _base_headers():
    return {
        "Authorization": f"Bearer {_notion_state['access_token']}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def _session_headers():
    """Headers for every request after ``initialize`` (MCP Streamable HTTP)."""
    headers = _base_headers()
    if _notion_session_id:
        headers["Mcp-Session-Id"] = _notion_session_id
    headers["MCP-Protocol-Version"] = _negotiated_protocol_version or MCP_PROTOCOL_VERSION
    return headers


def _parse_jsonrpc_response(r):
    """Return the JSON-RPC response object from a JSON or SSE body, or None."""
    text = r.text or ""
    for line in text.split("\n"):
        if line.startswith("data:"):
            try:
                message = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            # Skip server notifications/requests (progress etc.) — we want the response.
            if isinstance(message, dict) and ("result" in message or "error" in message):
                return message
    try:
        message = r.json()
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def _http_error_text(r):
    """The most useful error text from an MCP HTTP error response."""
    try:
        body = r.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return str(body.get("error_description") or err)
        if body.get("message"):
            return str(body["message"])
    return (r.text or "").strip()[:300] or f"HTTP {r.status_code}"


def mcp_init(_retried=False):
    """Initialize an MCP session with Notion, returns session ID.

    Sends ``initialize``, records the session id and the negotiated protocol
    version, then sends ``notifications/initialized`` as the MCP lifecycle
    requires. On a 401 it refreshes the token once and retries.
    """
    global _notion_session_id, _negotiated_protocol_version
    import uuid as uuid_mod

    with _auth_lock:
        token = _notion_state.get("access_token")
        if not token:
            raise ValueError("Not connected to Notion")
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid_mod.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "plaud-v2", "version": "1.0.0"},
            },
        }

        r = http_requests.post(NOTION_MCP_URL, json=payload, headers=_base_headers(), timeout=15)
        if r.status_code == 401:
            if not _retried and refresh_token(stale_access_token=token):
                return mcp_init(_retried=True)
            _notion_state["connected"] = False
            raise ValueError("Notion session expired — please reconnect in Settings")
        if r.status_code >= 400:
            raise ValueError(f"Notion MCP initialize failed ({r.status_code}): {_http_error_text(r)}")

        message = _parse_jsonrpc_response(r) or {}
        if "error" in message:
            err = message["error"]
            raise ValueError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
        result = message.get("result") if isinstance(message.get("result"), dict) else {}
        _negotiated_protocol_version = result.get("protocolVersion") or MCP_PROTOCOL_VERSION
        _notion_session_id = r.headers.get("mcp-session-id")

        # Lifecycle: tell the server we're ready. The server answers 202.
        try:
            n = http_requests.post(
                NOTION_MCP_URL,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=_session_headers(),
                timeout=15,
            )
            if n.status_code >= 400:
                logging.warning(f"Notion MCP rejected notifications/initialized ({n.status_code}): {_http_error_text(n)}")
        except Exception as e:
            logging.warning(f"Could not send notifications/initialized to Notion MCP: {e}")
        return _notion_session_id


def _ensure_fresh_token():
    """Proactively refresh the token if it's about to expire (within 5 minutes)."""
    if _token_expires_at > 0 and time.time() > _token_expires_at - 300:
        logging.info("Notion token expiring soon, refreshing proactively")
        refresh_token()


def _ensure_session():
    """Headers for a tool call, initialising a session first if there is none."""
    with _auth_lock:
        if not _notion_session_id:
            mcp_init()
        return _session_headers()


def _reset_session(stale_session_id):
    """Forget ``stale_session_id`` unless another thread already replaced it."""
    global _notion_session_id
    with _auth_lock:
        if _notion_session_id == stale_session_id:
            _notion_session_id = None


def mcp_call(method, params=None, timeout=30):
    """Make an MCP tool call to Notion via Streamable HTTP.

    - 401: refresh the token once, start a new session, retry once.
    - 404 on a request that carried an Mcp-Session-Id: the server ended that
      session, so start a new one and retry once (MCP spec, session management).
    - 400: raise with the server's own error text. Only when that text is about
      the session do we re-initialise and retry once; re-sending a request the
      server called malformed would just fail the same way.
    """
    if not _notion_state.get("access_token"):
        raise ValueError("Not connected to Notion")

    _ensure_fresh_token()

    import uuid as uuid_mod

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid_mod.uuid4()),
        "method": "tools/call",
        "params": {
            "name": method,
            "arguments": params or {},
        },
    }

    headers = _ensure_session()
    r = http_requests.post(NOTION_MCP_URL, json=payload, headers=headers, timeout=timeout)

    if r.status_code == 401:
        stale = headers.get("Authorization", "")[len("Bearer ") :]
        if refresh_token(stale_access_token=stale):
            _reset_session(headers.get("Mcp-Session-Id"))
            headers = _ensure_session()
            r = http_requests.post(NOTION_MCP_URL, json=payload, headers=headers, timeout=timeout)
        if r.status_code == 401:
            _notion_state["connected"] = False
            raise ValueError("Notion session expired — please reconnect in Settings")

    retry_session = (r.status_code == 404 and bool(headers.get("Mcp-Session-Id"))) or (
        r.status_code == 400 and "session" in _http_error_text(r).lower()
    )
    if retry_session:
        _reset_session(headers.get("Mcp-Session-Id"))
        headers = _ensure_session()
        r = http_requests.post(NOTION_MCP_URL, json=payload, headers=headers, timeout=timeout)

    if r.status_code == 400:
        raise ValueError(f"Notion MCP rejected the request (400): {_http_error_text(r)}")
    if r.status_code >= 400:
        # Keep an HTTPError carrying .response (status-based handling upstream)
        # but put the server's own text in the message.
        raise http_requests.HTTPError(f"Notion MCP HTTP {r.status_code}: {_http_error_text(r)}", response=r)

    message = _parse_jsonrpc_response(r)
    if message is None:
        raise ValueError(f"Unexpected MCP response: {(r.text or '')[:200]}")
    return _unwrap_mcp_message(message)


def _unwrap_mcp_message(message):
    """Return the ``result`` of a JSON-RPC response, raising ValueError on failure.

    Two kinds of failure exist: a JSON-RPC ``error`` (protocol level), and a
    tool failure, which MCP reports as a *successful* response (HTTP 200) whose
    result has ``isError: true`` and the error text in ``content``. Treating the
    latter as success made failed pushes report "Pushed to Notion!".
    """
    if "error" in message:
        err = message["error"]
        raise ValueError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
    result = message.get("result", message)
    if isinstance(result, dict) and result.get("isError"):
        raise ValueError(_mcp_error_text(result))
    return result


def _mcp_error_text(result):
    """Join the text parts of an MCP tool-error result into one message."""
    content = result.get("content")
    if not isinstance(content, list):
        content = [content] if content else []
    parts = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text")
            if text:
                parts.append(str(text))
        elif item:
            parts.append(str(item))
    msg = "\n".join(parts).strip()
    return msg or "Notion MCP tool call failed"


def start_connect():
    """Start the Notion OAuth flow — returns the authorization URL."""
    meta = _discover()
    auth_endpoint = meta["authorization_endpoint"]
    token_endpoint = meta["token_endpoint"]
    reg_endpoint = meta.get("registration_endpoint")

    _notion_state["token_endpoint"] = token_endpoint
    _notion_state["resource"] = meta.get("resource") or NOTION_MCP_URL

    if reg_endpoint:
        client_id, client_secret = _register_client(reg_endpoint)
        _notion_state["client_id"] = client_id
        _notion_state["client_secret"] = client_secret

    code_verifier, code_challenge = _generate_pkce()
    _notion_state["code_verifier"] = code_verifier

    import uuid as uuid_mod

    state = str(uuid_mod.uuid4())
    _notion_state["state"] = state

    params = {
        "response_type": "code",
        "client_id": _notion_state["client_id"],
        "redirect_uri": f"http://localhost:{NOTION_CALLBACK_PORT}/notion/callback",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": _resource(),
    }
    auth_url = f"{auth_endpoint}?{urlencode(params)}"

    _start_callback_server()

    return auth_url


def _exchange_code(code):
    """Exchange an authorization code for tokens (with PKCE + RFC 8707 resource)."""
    global _token_expires_at
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": f"http://localhost:{NOTION_CALLBACK_PORT}/notion/callback",
        "code_verifier": _notion_state["code_verifier"],
        "client_id": _notion_state["client_id"],
        "resource": _resource(),
    }
    if _notion_state.get("client_secret"):
        payload["client_secret"] = _notion_state["client_secret"]

    r = http_requests.post(_notion_state["token_endpoint"], data=payload, timeout=15)
    r.raise_for_status()
    tokens = r.json()

    with _auth_lock:
        _notion_state["access_token"] = tokens["access_token"]
        _notion_state["refresh_token"] = tokens.get("refresh_token", "")
        _notion_state["connected"] = True
        _notion_state["refresh_dead"] = False
        _notion_state["code_verifier"] = None
        _notion_state["state"] = None
        _token_expires_at = time.time() + tokens.get("expires_in", 3600)
        save()
    _on_connected()


def _on_connected():
    """After a successful (re)connect: start a fresh session, un-pause the queue."""
    global _notion_session_id
    with _auth_lock:
        _notion_session_id = None
    try:
        from app.utils import notion_queue

        notion_queue.resume()
    except Exception as e:
        logging.warning(f"Could not resume the Notion queue after connecting: {e}")


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
            if parsed.path == "/notion/callback":
                params = parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                error = params.get("error", [None])[0]

                if error:
                    self._respond(
                        f"<h2>Connection failed</h2><p>{_html.escape(error)}</p><p>You can close this tab.</p>"
                    )
                    self._signal({"error": error})
                    return

                if state != _notion_state.get("state"):
                    self._respond("<h2>Invalid state</h2><p>CSRF check failed. Try again.</p>")
                    self._signal({"error": "csrf_mismatch"})
                    return

                try:
                    _exchange_code(code)
                    self._respond(
                        "<h2 style='color:#30d158;'>Connected to Notion!</h2>"
                        "<p>You can close this tab and return to Plaud V2.</p>"
                        "<script>setTimeout(()=>window.close(),2000)</script>"
                    )
                    self._signal({"ok": True})
                except Exception as e:
                    self._respond(f"<h2>Token exchange failed</h2><p>{_html.escape(str(e))}</p>")
                    self._signal({"error": str(e)})
            else:
                self.send_response(404)
                self.end_headers()

        def _respond(self, html):
            body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
            <style>body{{font-family:system-ui;display:flex;align-items:center;justify-content:center;
            height:100vh;margin:0;background:#1a1a2e;color:#f5f5f7;text-align:center;}}</style>
            </head><body><div>{html}</div></body></html>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    result_queue: queue.Queue = queue.Queue(maxsize=1)
    CallbackHandler._result_queue = result_queue  # type: ignore[attr-defined]

    def run_server():
        server = HTTPServer(("127.0.0.1", NOTION_CALLBACK_PORT), CallbackHandler)
        server.timeout = 1
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and result_queue.empty():
            server.handle_request()
        server.server_close()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()


def disconnect():
    """Disconnect from Notion."""
    with _auth_lock:
        _notion_state["connected"] = False
        _notion_state["access_token"] = None
        _notion_state["refresh_token"] = None
        _notion_state["database_id"] = None
        _notion_state["database_name"] = None
        _notion_state["refresh_dead"] = False
        _reset_session(_notion_session_id)
    if NOTION_CONFIG_PATH.exists():
        NOTION_CONFIG_PATH.unlink()
    return {"disconnected": True}


def set_database(db_id, db_name=""):
    """Set the target database for pushing recordings."""
    _notion_state["database_id"] = db_id
    _notion_state["database_name"] = db_name
    save()


def get_database_id():
    return _notion_state.get("database_id")


def get_database_name():
    return _notion_state.get("database_name")


# Load saved state on import
load()
