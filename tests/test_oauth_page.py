"""The page shown after Notion / Google Drive send the browser back to the app."""

import re
import socket
import time
import urllib.error
import urllib.request

import pytest

from app.utils import oauth_page


@pytest.mark.parametrize("service", ["notion", "gdrive"])
@pytest.mark.parametrize("outcome", oauth_page.OUTCOMES)
def test_every_page_renders_with_its_outcome(service, outcome):
    page = oauth_page.render(service, outcome, nonce="n0")
    assert page.startswith("<!DOCTYPE html>")
    assert f'<main class="{outcome}">' in page
    name = "Notion" if service == "notion" else "Google Drive"
    assert name in page
    # No outside resources: the URL holds the one-time code
    assert not re.search(r'(src|href)="https?://', page)
    assert '<script nonce="n0">' in page and "history.replaceState" in page


def test_connected_page_says_what_to_do_next():
    page = oauth_page.render("notion", "connected")
    assert "Open Pin is connected to Notion" in page
    assert "choose the Notion database" in page


def test_failure_pages_point_back_to_settings():
    for outcome in ("cancelled", "expired", "failed"):
        page = oauth_page.render("gdrive", outcome)
        assert "Connect Google Drive" in page and "Integrations" in page


def test_detail_is_escaped_and_only_on_failures():
    evil = '<script>alert(1)</script>"&'
    page = oauth_page.render("notion", "failed", evil)
    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&quot;&amp;" in page
    assert "Technical details" in page
    assert "Technical details" not in oauth_page.render("notion", "connected", "ignored")


def test_unknown_service_or_outcome_is_refused():
    with pytest.raises(ValueError):
        oauth_page.render("dropbox", "connected")
    with pytest.raises(ValueError):
        oauth_page.render("notion", "maybe")


class _Handler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = b""

        class W:
            def write(w, data):
                self.body += data

        self.wfile = W()

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers[k] = v

    def end_headers(self):
        pass


def test_send_sets_status_and_strict_headers():
    h = _Handler()
    oauth_page.send(h, "notion", "connected")
    assert h.status == 200
    assert h.headers["Cache-Control"] == "no-store"
    assert h.headers["Referrer-Policy"] == "no-referrer"
    assert h.headers["Content-Type"] == "text/html; charset=utf-8"
    assert int(h.headers["Content-Length"]) == len(h.body)
    csp = h.headers["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{nonce}">'.encode() in h.body
    assert "default-src 'none'" in csp

    h = _Handler()
    oauth_page.send(h, "gdrive", "failed", "boom")
    assert h.status == 400


# --- The real callback servers -------------------------------------------------


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url):
    for _ in range(50):  # the server thread may not be listening yet
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except urllib.error.URLError:
            time.sleep(0.05)
    raise AssertionError("callback server never answered")


@pytest.mark.parametrize(
    "module_name, port_attr, path",
    [
        ("app.state.notion", "NOTION_CALLBACK_PORT", "/notion/callback"),
        ("app.state.gdrive", "GDRIVE_CALLBACK_PORT", "/gdrive/callback"),
    ],
)
@pytest.mark.parametrize(
    "query, status, text",
    [
        ("code=c&state=stale", 400, "This connection link is out of date"),
        ("error=access_denied&state=x", 400, "wasn&#x27;t connected"),
        ("error=server_error&error_description=down&state=x", 400, "server_error: down"),
    ],
)
def test_callback_server_answers_with_the_page(monkeypatch, module_name, port_attr, path, query, status, text):
    import importlib

    mod = importlib.import_module(module_name)
    port = _free_port()
    monkeypatch.setattr(mod, port_attr, port)
    state = mod._notion_state if module_name.endswith("notion") else mod._gdrive_state
    monkeypatch.setitem(state, "state", "the-real-state")
    mod._start_callback_server()
    got_status, body = _get(f"http://127.0.0.1:{port}{path}?{query}")
    assert got_status == status
    assert text in body
