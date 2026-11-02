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
}

_notion_session_id = None


def get_state():
    """Get a read-only snapshot of Notion state."""
    return {
        "connected": _notion_state["connected"],
        "database_id": _notion_state.get("database_id"),
        "database_name": _notion_state.get("database_name"),
    }


def is_connected():
    return _notion_state["connected"]


def verify_connection():
    """Check if the Notion token is still valid. Returns True if connected, False if not.
    Proactively refreshes if expiring soon, retries if expired."""
    if not _notion_state.get("access_token"):
        _notion_state["connected"] = False
        return False
    # Proactively refresh if near expiry
    _ensure_fresh_token()
    try:
        mcp_init()
        return True
    except Exception:
        # Token expired — try refresh
        if refresh_token():
            try:
                mcp_init()
                return True
            except Exception:
                pass
        _notion_state["connected"] = False
        return False


def load():
    """Load saved Notion OAuth tokens."""
    if NOTION_CONFIG_PATH.exists():
        try:
            data = json.loads(NOTION_CONFIG_PATH.read_text())
            _notion_state.update(data)
            if _notion_state.get("access_token"):
                _notion_state["connected"] = True
        except Exception:
            pass


def save():
    """Persist Notion tokens and config."""
    save_data = {k: v for k, v in _notion_state.items() if k != "connected" and k != "code_verifier" and k != "state"}
    NOTION_CONFIG_PATH.write_text(json.dumps(save_data, indent=2))


def _discover():
    """Discover Notion MCP OAuth endpoints via RFC 8414."""
    base = "https://mcp.notion.com"
    r = http_requests.get(f"{base}/.well-known/oauth-protected-resource", timeout=10)
    r.raise_for_status()
    resource_meta = r.json()
    auth_server = resource_meta.get("authorization_servers", [None])[0]
    if not auth_server:
        raise ValueError("No authorization server found in Notion MCP metadata")

    r = http_requests.get(f"{auth_server}/.well-known/oauth-authorization-server", timeout=10)
    r.raise_for_status()
    return r.json()


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


def refresh_token():
    """Refresh the access token using the refresh token."""
    global _token_expires_at
    if not _notion_state.get("refresh_token") or not _notion_state.get("token_endpoint"):
        return False
    try:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": _notion_state["refresh_token"],
            "client_id": _notion_state["client_id"],
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
        # Track when this token expires (default 1 hour)
        import time

        _token_expires_at = time.time() + tokens.get("expires_in", 3600)
        save()
        return True
    except Exception as e:
        logging.warning(f"Notion token refresh failed: {e}")
        _notion_state["connected"] = False
        return False


def mcp_init():
    """Initialize an MCP session with Notion, returns session ID."""
    global _notion_session_id
    import uuid as uuid_mod

    headers = {
        "Authorization": f"Bearer {_notion_state['access_token']}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid_mod.uuid4()),
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "plaud-v2", "version": "1.0.0"},
        },
    }

    r = http_requests.post(NOTION_MCP_URL, json=payload, headers=headers, timeout=15)
    r.raise_for_status()
    _notion_session_id = r.headers.get("mcp-session-id")
    return _notion_session_id


def _ensure_fresh_token():
    """Proactively refresh the token if it's about to expire (within 5 minutes)."""
    import time

    if _token_expires_at > 0 and time.time() > _token_expires_at - 300:
        logging.info("Notion token expiring soon, refreshing proactively")
        refresh_token()


def mcp_call(method, params=None):
    """Make an MCP tool call to Notion via Streamable HTTP."""
    global _notion_session_id
    if not _notion_state.get("access_token"):
        raise ValueError("Not connected to Notion")

    # Proactively refresh if token is about to expire
    _ensure_fresh_token()

    if not _notion_session_id:
        mcp_init()

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

    headers = {
        "Authorization": f"Bearer {_notion_state['access_token']}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Session-Id": _notion_session_id,
    }

    r = http_requests.post(NOTION_MCP_URL, json=payload, headers=headers, timeout=30)

    if r.status_code == 401:
        if refresh_token():
            _notion_session_id = None
            mcp_init()
            headers["Authorization"] = f"Bearer {_notion_state['access_token']}"
            headers["Mcp-Session-Id"] = _notion_session_id
            r = http_requests.post(NOTION_MCP_URL, json=payload, headers=headers, timeout=30)
        else:
            _notion_state["connected"] = False
            raise ValueError("Notion session expired — please reconnect in Settings")

    if r.status_code == 400:
        _notion_session_id = None
        mcp_init()
        headers["Mcp-Session-Id"] = _notion_session_id
        r = http_requests.post(NOTION_MCP_URL, json=payload, headers=headers, timeout=30)

    r.raise_for_status()

    text = r.text
    for line in text.split("\n"):
        if line.startswith("data: "):
            result = json.loads(line[6:])
            if "error" in result:
                raise ValueError(result["error"].get("message", str(result["error"])))
            return result.get("result", result)

    try:
        result = r.json()
        if "error" in result:
            raise ValueError(result["error"].get("message", str(result["error"])))
        return result.get("result", result)
    except Exception:
        raise ValueError(f"Unexpected MCP response: {text[:200]}")


def start_connect():
    """Start the Notion OAuth flow — returns the authorization URL."""
    meta = _discover()
    auth_endpoint = meta["authorization_endpoint"]
    token_endpoint = meta["token_endpoint"]
    reg_endpoint = meta.get("registration_endpoint")

    _notion_state["token_endpoint"] = token_endpoint

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
    }
    auth_url = f"{auth_endpoint}?{urlencode(params)}"

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
            if parsed.path == "/notion/callback":
                params = parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                error = params.get("error", [None])[0]

                if error:
                    self._respond(f"<h2>Connection failed</h2><p>{_html.escape(error)}</p><p>You can close this tab.</p>")
                    self._signal({"error": error})
                    return

                if state != _notion_state.get("state"):
                    self._respond("<h2>Invalid state</h2><p>CSRF check failed. Try again.</p>")
                    self._signal({"error": "csrf_mismatch"})
                    return

                try:
                    payload = {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": f"http://localhost:{NOTION_CALLBACK_PORT}/notion/callback",
                        "code_verifier": _notion_state["code_verifier"],
                        "client_id": _notion_state["client_id"],
                    }
                    if _notion_state.get("client_secret"):
                        payload["client_secret"] = _notion_state["client_secret"]

                    r = http_requests.post(_notion_state["token_endpoint"], data=payload, timeout=15)
                    r.raise_for_status()
                    tokens = r.json()

                    _notion_state["access_token"] = tokens["access_token"]
                    _notion_state["refresh_token"] = tokens.get("refresh_token", "")
                    _notion_state["connected"] = True
                    _notion_state["code_verifier"] = None
                    _notion_state["state"] = None
                    save()

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
    _notion_state["connected"] = False
    _notion_state["access_token"] = None
    _notion_state["refresh_token"] = None
    _notion_state["database_id"] = None
    _notion_state["database_name"] = None
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
