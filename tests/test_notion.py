"""Notion integration: session-id validation, locked/atomic writes, config
updates, MCP tool-error handling, URL → id extraction and the tag-sync queue.

No live Notion calls — ``notion.mcp_call`` / ``http_requests.post`` are mocked.
"""

import json
import threading
import time
import types
from unittest.mock import patch

import pytest

SID = "1715000000"
PAGE_URL = "https://www.notion.so/myteam/Meeting-Recorded-2e80f387aa1b4c3d9e8f0123456789ab"


def _schema(**over):
    s = {
        "db_id": "db-id",
        "data_source_id": "ds-id",
        "tags_exists": True,
        "tags_conflict": False,
        "tag_options": {"A": "blue"},
        "session_prop": True,
        "session_conflict": False,
    }
    s.update(over)
    return s


@pytest.fixture
def env(flask_client, tmp_path):
    """Push plumbing with schema handling and the Session ID lookup stubbed
    (they have their own tests below); tests may re-patch either."""
    from app.routes import notion as rn

    real = types.SimpleNamespace(find=rn._find_page_by_session_id, ensure_schema=rn._ensure_schema)
    trn = tmp_path / "transcripts"
    trn.mkdir()
    with (
        patch("app.routes.notion.TRANSCRIPTS_DIR", trn),
        patch("app.state.notion.is_connected", return_value=True),
        patch("app.state.notion.get_database_id", return_value="db-id"),
        patch("app.routes.notion._ensure_schema", return_value=_schema()),
        patch("app.routes.notion._find_page_by_session_id", return_value=None),
        patch("app.routes.notion._sleep"),
    ):
        yield types.SimpleNamespace(client=flask_client, trn=trn, root=tmp_path, real=real)


def _cfg():
    from app.utils.config import load_config

    return load_config()


def _write_transcript(trn, sid, data):
    (trn / f"{sid}.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------

# A backslash alone is not a separator on macOS (older cloud ids can contain
# one; see app/utils/paths.py), so "..\\b" is the backslash case here.
TRAVERSAL = ["../victim/secret", "../../etc/passwd", "/etc/passwd", "..\\b", ".hidden", "a:b", "..", "a\x00b"]


@pytest.mark.parametrize("sid", TRAVERSAL)
def test_push_rejects_traversal_ids(env, sid):
    with patch("app.state.notion.mcp_call") as mcp:
        r = env.client.post("/notion/push", json={"session_id": sid})
    assert r.status_code == 400
    assert r.get_json() == {"error": "Invalid session_id"}
    mcp.assert_not_called()


@pytest.mark.parametrize("sid", [".hidden", "a:b", "..%5Cb", "..evil", "x" * 129])
def test_page_url_rejects_bad_ids(env, sid):
    r = env.client.get(f"/notion/page-url/{sid}")
    assert r.status_code == 400
    assert r.get_json() == {"error": "Invalid session_id"}


def test_page_url_valid_id(env):
    from app.utils.config import save_config

    save_config({"notion_page_urls": {SID: PAGE_URL}})
    r = env.client.get(f"/notion/page-url/{SID}")
    assert r.get_json() == {"url": PAGE_URL, "has_page": True}


def test_crafted_id_cannot_write_outside_transcripts(env):
    from app.routes import notion as rn
    from app.utils.paths import InvalidSessionIdError

    victim = env.root / "victim"
    victim.mkdir()
    (victim / "secret.json").write_text('{"keep": "me"}')
    with pytest.raises(InvalidSessionIdError):
        rn._save_notion_page_url("../victim/secret", "https://evil")
    assert push_result(rn, "../victim/secret") == {"error": "Invalid session_id"}
    assert json.loads((victim / "secret.json").read_text()) == {"keep": "me"}
    assert "notion_page_urls" not in _cfg()


def push_result(rn, sid):
    with patch("app.state.notion.mcp_call") as mcp:
        out = rn.push_to_notion(sid)
    mcp.assert_not_called()
    return out


# ---------------------------------------------------------------------------
# Transcript writes: merged, locked, atomic
# ---------------------------------------------------------------------------


def test_save_page_url_merges_into_transcript_atomically(env):
    from app.routes import notion as rn

    _write_transcript(env.trn, SID, {"text": "hello", "tags": ["A"], "summary": "S"})
    rn._save_notion_page_url(SID, PAGE_URL)
    data = json.loads((env.trn / f"{SID}.json").read_text())
    assert data == {"text": "hello", "tags": ["A"], "summary": "S", "notion_page_url": PAGE_URL}
    assert sorted(p.name for p in env.trn.iterdir()) == [f"{SID}.json"]  # no temp files left
    assert _cfg()["notion_page_urls"] == {SID: PAGE_URL}


def test_save_page_url_waits_for_transcript_lock(env):
    from app.routes import notion as rn
    from app.utils.transcript_locks import get_transcript_lock

    _write_transcript(env.trn, SID, {"text": "hello"})
    lock = get_transcript_lock(SID)
    lock.acquire()
    try:
        t = threading.Thread(target=rn._save_notion_page_url, args=(SID, PAGE_URL))
        t.start()
        time.sleep(0.2)
        # Another writer holds the lock: our write must not have happened yet,
        # and a write it makes now must survive the merge.
        assert "notion_page_url" not in json.loads((env.trn / f"{SID}.json").read_text())
        _write_transcript(env.trn, SID, {"text": "hello", "diarized": True})
    finally:
        lock.release()
    t.join(5)
    data = json.loads((env.trn / f"{SID}.json").read_text())
    assert data == {"text": "hello", "diarized": True, "notion_page_url": PAGE_URL}


def test_get_page_url_recovers_from_transcript(env):
    from app.routes import notion as rn

    _write_transcript(env.trn, SID, {"text": "x", "notion_page_url": PAGE_URL})
    assert rn._get_notion_page_url(SID) == PAGE_URL
    assert _cfg()["notion_page_urls"] == {SID: PAGE_URL}


# ---------------------------------------------------------------------------
# Config writes go through update_config
# ---------------------------------------------------------------------------


def test_notion_config_writes_use_update_config(env):
    from app.routes import notion as rn
    from app.utils import config as config_mod

    with patch("app.routes.notion.update_config", wraps=config_mod.update_config) as uc:
        rn._save_notion_page_url(SID, PAGE_URL)
        rn._save_notion_pushed_title(SID, "Title")
    assert uc.call_count == 2


def test_push_does_not_clobber_config_written_during_network_call(env):
    from app.utils.config import save_config, update_config

    save_config({"recording_names": {SID: "My Title"}, "deleted_ids": ["a"]})
    _write_transcript(env.trn, SID, {"text": "hello"})

    def fake_mcp(method, params=None, timeout=None):
        # While the request is "in flight", other code writes to the config.
        update_config(lambda c: c["deleted_ids"].append("written_meanwhile"))
        update_config(lambda c: c.__setitem__("deepgram_api_key", "new-key"))
        return {"content": [{"type": "text", "text": json.dumps({"url": PAGE_URL})}]}

    with patch("app.state.notion.mcp_call", side_effect=fake_mcp):
        r = env.client.post("/notion/push", json={"session_id": SID})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["url"] == PAGE_URL
    cfg = _cfg()
    assert cfg["deleted_ids"] == ["a", "written_meanwhile"]
    assert cfg["deepgram_api_key"] == "new-key"
    assert cfg["notion_page_urls"] == {SID: PAGE_URL}
    assert cfg["notion_pushed_titles"] == {SID: "My Title"}
    assert cfg["recording_names"] == {SID: "My Title"}


# ---------------------------------------------------------------------------
# Notion URL → id extraction
# ---------------------------------------------------------------------------

DB_HEX = "2e80f387aa1b4c3d9e8f0123456789ab"
DB_UUID = "2e80f387-aa1b-4c3d-9e8f-0123456789ab"


@pytest.mark.parametrize(
    "url",
    [
        # workspace/slug-<32hex>; "Recorded-" ends in hex letters ("ded")
        f"https://www.notion.so/myteam/Meeting-Recorded-{DB_HEX}",
        f"https://www.notion.so/myteam/Meeting-Recorded-{DB_HEX}?pvs=4",
        f"https://www.notion.so/myteam/Cafe-Bead-Notes-{DB_HEX}#abc123",
        f"https://www.notion.so/myteam/Meeting-Recorded-{DB_HEX}/",
        # bare 32-hex with a view id in the query
        f"https://www.notion.so/{DB_HEX}?v=0123456789abcdef0123456789abcdef",
        f"https://notion.so/myteam/{DB_HEX}?v=ffffffffffffffffffffffffffffffff&pvs=4",
        # dashed UUID forms
        f"https://www.notion.so/{DB_UUID}",
        f"https://www.notion.so/myteam/Meeting-Recorded-{DB_UUID}",
        f"https://myteam.notion.site/Feed-{DB_HEX}",
        DB_HEX,
        DB_UUID,
        DB_HEX.upper(),
        f"  {DB_HEX}  ",
    ],
)
def test_extract_notion_id(url):
    from app.routes.notion import _extract_notion_id, _extract_page_id

    assert _extract_notion_id(url) == DB_UUID
    assert _extract_page_id(url) == DB_UUID


@pytest.mark.parametrize(
    "url",
    ["", None, "https://www.notion.so/", "https://www.notion.so/myteam/Just-A-Slug", "abc123", 42],
)
def test_extract_notion_id_none(url):
    from app.routes.notion import _extract_notion_id

    assert _extract_notion_id(url) is None


def test_extract_notion_id_ignores_view_id_and_earlier_segments():
    from app.routes.notion import _extract_notion_id

    other = "ffffffffffffffffffffffffffffffff"
    assert _extract_notion_id(f"https://www.notion.so/{other}/Child-{DB_HEX}?v={other}") == DB_UUID


def test_set_database_url_fetches_by_id(env):
    url = f"https://www.notion.so/myteam/Meeting-Recorded-{DB_HEX}?v=0123456789abcdef0123456789abcdef"
    fetched = {"content": [{"type": "text", "text": "Meetings\nmore"}]}
    with (
        patch("app.state.notion.mcp_call", return_value=fetched) as mcp,
        patch("app.state.notion.set_database") as set_db,
    ):
        r = env.client.post("/notion/set-database-url", json={"url": url})
    assert r.status_code == 200, r.get_json()
    mcp.assert_called_once_with("notion-fetch", {"id": DB_UUID})
    set_db.assert_called_once_with(DB_UUID, "Meetings")
    assert r.get_json() == {"saved": True, "database_id": DB_UUID, "database_name": "Meetings"}


def test_set_database_url_tool_error_is_reported(env):
    url = f"https://www.notion.so/{DB_HEX}"
    with (
        patch("app.state.notion.mcp_call", side_effect=ValueError("Could not find database")),
        patch("app.state.notion.set_database") as set_db,
    ):
        r = env.client.post("/notion/set-database-url", json={"url": url})
    assert r.status_code == 500
    assert "Could not find database" in r.get_json()["error"]
    set_db.assert_not_called()


def test_set_database_url_without_id_is_400(env):
    with patch("app.state.notion.mcp_call") as mcp:
        r = env.client.post("/notion/set-database-url", json={"url": "https://www.notion.so/myteam/No-Id"})
    assert r.status_code == 400
    mcp.assert_not_called()


# ---------------------------------------------------------------------------
# mcp_call: tool errors (result.isError) must raise
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body, sse=False, status_code=200):
        self.status_code = status_code
        self.headers = {}
        if sse:
            self.text = f"event: message\ndata: {json.dumps(body)}\n\n"
        else:
            self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        pass


TOOL_ERROR = {
    "jsonrpc": "2.0",
    "id": "1",
    "result": {
        "isError": True,
        "content": [
            {"type": "text", "text": "validation_error: Tags is not a property"},
            {"type": "text", "text": "that exists."},
        ],
    },
}
TOOL_OK = {"jsonrpc": "2.0", "id": "1", "result": {"content": [{"type": "text", "text": "ok"}]}}


@pytest.fixture
def mcp_http():
    """Patch the Notion HTTP layer; yields a setter for the next response."""
    from app.state import notion as ns

    resp = {}
    with (
        patch.dict(ns._notion_state, {"access_token": "tok", "connected": True}),
        patch.object(ns, "_notion_session_id", "sess"),
        patch.object(ns, "_ensure_fresh_token"),
        patch.object(ns, "mcp_init"),
        patch.object(ns.http_requests, "post", side_effect=lambda *a, **k: resp["r"]) as post,
    ):

        def set_response(r):
            resp["r"] = r
            return post

        yield set_response


@pytest.mark.parametrize("sse", [False, True])
def test_mcp_call_raises_on_tool_error(mcp_http, sse):
    from app.state import notion as ns

    mcp_http(_FakeResp(TOOL_ERROR, sse=sse))
    with pytest.raises(ValueError) as exc:
        ns.mcp_call("notion-create-pages", {})
    assert str(exc.value) == "validation_error: Tags is not a property\nthat exists."


@pytest.mark.parametrize("sse", [False, True])
def test_mcp_call_returns_result_on_success(mcp_http, sse):
    from app.state import notion as ns

    mcp_http(_FakeResp(TOOL_OK, sse=sse))
    assert ns.mcp_call("notion-fetch", {"id": "x"}) == TOOL_OK["result"]


@pytest.mark.parametrize("sse", [False, True])
def test_mcp_call_raises_on_jsonrpc_error(mcp_http, sse):
    from app.state import notion as ns

    mcp_http(_FakeResp({"jsonrpc": "2.0", "id": "1", "error": {"code": -32602, "message": "Bad params"}}, sse=sse))
    with pytest.raises(ValueError, match="^Bad params$"):
        ns.mcp_call("notion-fetch", {})


def test_mcp_call_tool_error_without_text(mcp_http):
    from app.state import notion as ns

    mcp_http(_FakeResp({"jsonrpc": "2.0", "id": "1", "result": {"isError": True, "content": []}}))
    with pytest.raises(ValueError, match="Notion MCP tool call failed"):
        ns.mcp_call("notion-fetch", {})


def test_mcp_call_sse_skips_notifications(mcp_http):
    from app.state import notion as ns

    r = _FakeResp(TOOL_OK)
    note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
    r.text = f"data: {json.dumps(note)}\n\ndata: {json.dumps(TOOL_ERROR)}\n\n"
    mcp_http(r)
    with pytest.raises(ValueError, match="validation_error"):
        ns.mcp_call("notion-fetch", {})


def test_mcp_call_garbage_response(mcp_http):
    from app.state import notion as ns

    mcp_http(_FakeResp("<html>oops</html>"))
    with pytest.raises(ValueError, match="Unexpected MCP response"):
        ns.mcp_call("notion-fetch", {})


def test_push_route_reports_tool_error(env, mcp_http):
    _write_transcript(env.trn, SID, {"text": "hello"})
    mcp_http(_FakeResp(TOOL_ERROR, sse=True))
    r = env.client.post("/notion/push", json={"session_id": SID})
    assert r.status_code == 500
    body = r.get_json()
    assert "pushed" not in body
    assert "validation_error" in body["error"]
    cfg = _cfg()
    assert "notion_page_urls" not in cfg
    assert "notion_pushed_titles" not in cfg
    assert "notion_page_url" not in json.loads((env.trn / f"{SID}.json").read_text())


# ---------------------------------------------------------------------------
# Tag-sync queue: an isError result is a failure, not a success
# ---------------------------------------------------------------------------


@pytest.fixture
def queue_state(tmp_path):
    from app.utils import notion_queue

    st = notion_queue._State(tmp_path / "notion_queue.json")
    with patch.object(notion_queue, "_state", st):
        yield st


def _run_queue_once(queue_state):
    from app.utils import notion_queue

    entry = queue_state.pop_due()
    assert entry is not None
    notion_queue._process_one(entry)


def _setup_existing_page(env):
    from app.utils.config import save_config

    save_config({"notion_page_urls": {SID: PAGE_URL}, "recording_tags": {SID: ["A"]}})
    _write_transcript(env.trn, SID, {"text": "hello"})


def test_queue_retries_transient_tool_error(env, mcp_http, queue_state):
    from app.utils import notion_queue

    _setup_existing_page(env)
    err = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"isError": True, "content": [{"type": "text", "text": "rate limited"}]},
    }
    mcp_http(_FakeResp(err))
    notion_queue.enqueue(SID)
    _run_queue_once(queue_state)
    entry = queue_state.entries[SID]  # still queued: not treated as done
    assert entry["attempts"] == 1
    assert entry["last_error"] == "rate limited"
    assert entry["next_retry_at"] > time.time()
    assert json.loads(queue_state.path.read_text())["entries"][SID]["attempts"] == 1


def test_queue_drops_permanent_tool_error(env, mcp_http, queue_state):
    from app.utils import notion_queue

    _setup_existing_page(env)
    mcp_http(_FakeResp(TOOL_ERROR))
    notion_queue.enqueue(SID)
    with patch.object(queue_state, "record_failure", wraps=queue_state.record_failure) as rf:
        _run_queue_once(queue_state)
    rf.assert_called_once()
    assert rf.call_args.kwargs["permanent"] is True
    assert "validation_error" in rf.call_args.args[1]


def test_queue_success_removes_entry(env, mcp_http, queue_state):
    from app.utils import notion_queue

    _setup_existing_page(env)
    mcp_http(_FakeResp(TOOL_OK))
    notion_queue.enqueue(SID)
    _run_queue_once(queue_state)
    assert queue_state.entries == {}


def test_queue_drops_invalid_session_id(env, queue_state):
    from app.utils import notion_queue

    notion_queue.enqueue("../evil")
    with patch.object(queue_state, "record_failure", wraps=queue_state.record_failure) as rf:
        _run_queue_once(queue_state)
    assert rf.call_args.kwargs["permanent"] is True
    assert queue_state.entries == {}


# ---------------------------------------------------------------------------
# Auth-error detection: only a real 401 pauses the queue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "Could not find page 2e80f387-401a-4c3d-9e8f-0123456789ab",
        "Could not find block 9401c2d3-aa1b-4c3d-9e8f-012345678401",
        "object_not_found: 1d2e3f40-1234-4401-8abc-401def012345",
        "rate limited until 2026-11-02T14:01:00Z (retry after 1401 ms)",
        "Page 3401 of 4010",
        "",
    ],
)
def test_is_auth_error_ignores_401_digits_in_ids_and_dates(msg):
    from app.utils.notion_queue import _is_auth_error

    assert _is_auth_error(msg) is False


@pytest.mark.parametrize(
    "msg",
    [
        "401 Client Error: Unauthorized for url: https://mcp.notion.com/mcp",
        "HTTP 401",
        "status 401: token revoked",
        "invalid_token: The access token expired",
        "Notion session expired, please reconnect in Settings",
    ],
)
def test_is_auth_error_detects_real_auth_failures(msg):
    from app.utils.notion_queue import _is_auth_error

    assert _is_auth_error(msg) is True


def test_is_auth_error_uses_http_status():
    import requests

    from app.utils.notion_queue import _is_auth_error

    resp = requests.Response()
    resp.status_code = 401
    assert _is_auth_error(requests.HTTPError("boom", response=resp)) is True
    resp.status_code = 404
    assert _is_auth_error(requests.HTTPError("Not Found 4401", response=resp)) is False


def _tool_error(text):
    return {"jsonrpc": "2.0", "id": "1", "result": {"isError": True, "content": [{"type": "text", "text": text}]}}


def test_queue_not_paused_by_uuid_containing_401(env, mcp_http, queue_state):
    from app.utils import notion_queue

    _setup_existing_page(env)
    mcp_http(_FakeResp(_tool_error("Conflict occurred while saving block 2e80f387-401a-4c3d-9e8f-0123456789ab")))
    notion_queue.enqueue(SID)
    _run_queue_once(queue_state)
    assert queue_state.paused is False
    assert queue_state.entries[SID]["attempts"] == 1


def test_queue_paused_by_real_auth_error(env, mcp_http, queue_state):
    from app.utils import notion_queue

    _setup_existing_page(env)
    mcp_http(_FakeResp(_tool_error("unauthorized: invalid_token")))
    notion_queue.enqueue(SID)
    _run_queue_once(queue_state)
    assert queue_state.paused is True


# ---------------------------------------------------------------------------
# MCP lifecycle / Streamable HTTP rules and OAuth (RFC 8707)
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} Error", response=self)


INIT_OK = {"jsonrpc": "2.0", "id": "1", "result": {"protocolVersion": "2025-11-25", "capabilities": {}}}


@pytest.fixture
def scripted_http():
    """Real mcp_init/mcp_call over a scripted ``http_requests.post``.

    Responses are picked by JSON-RPC method ("token" for token-endpoint
    posts): a list per key, popped in order, the last one repeating.
    """
    from app.state import notion as ns

    calls = []
    script = {}

    def fake_post(url, json=None, data=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "data": data, "headers": dict(headers or {}), "timeout": timeout})
        key = json.get("method") if json is not None else "token"
        responses = script.get(key) or [_Resp(202)]
        return responses.pop(0) if len(responses) > 1 else responses[0]

    with (
        patch.dict(
            ns._notion_state,
            {
                "access_token": "tok-1",
                "refresh_token": "ref-1",
                "token_endpoint": "https://auth.example/token",
                "client_id": "cid",
                "client_secret": "",
                "resource": "https://mcp.notion.com/mcp",
                "connected": True,
                "refresh_dead": False,
            },
        ),
        patch.object(ns, "_notion_session_id", None),
        patch.object(ns, "_negotiated_protocol_version", None),
        patch.object(ns, "_token_expires_at", 0),
        patch.object(ns, "save"),
        patch.object(ns.http_requests, "post", side_effect=fake_post),
    ):
        yield types.SimpleNamespace(calls=calls, script=script, ns=ns)


def _by_method(calls, method):
    return [c for c in calls if (c["json"] or {}).get("method") == method]


def _token_posts(calls):
    return [c for c in calls if c["json"] is None]


def test_initialize_sends_initialized_notification_and_protocol_header(scripted_http):
    h = scripted_http
    h.script["initialize"] = [_Resp(200, INIT_OK, headers={"mcp-session-id": "S1"})]
    h.script["notifications/initialized"] = [_Resp(202)]
    h.script["tools/call"] = [_Resp(200, TOOL_OK)]

    assert h.ns.mcp_call("notion-fetch", {"id": "x"}) == TOOL_OK["result"]

    methods = [(c["json"] or {}).get("method") for c in h.calls]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    init = h.calls[0]
    assert "Mcp-Session-Id" not in init["headers"]
    assert init["json"]["params"]["protocolVersion"] == h.ns.MCP_PROTOCOL_VERSION
    note = h.calls[1]
    assert "id" not in note["json"]  # a notification, not a request
    assert note["headers"]["Mcp-Session-Id"] == "S1"
    assert note["headers"]["MCP-Protocol-Version"] == "2025-11-25"
    call = h.calls[2]
    assert call["headers"]["Mcp-Session-Id"] == "S1"
    assert call["headers"]["MCP-Protocol-Version"] == "2025-11-25"


def test_protocol_header_uses_version_server_negotiated(scripted_http):
    h = scripted_http
    init = {"jsonrpc": "2.0", "id": "1", "result": {"protocolVersion": "2025-06-18"}}
    h.script["initialize"] = [_Resp(200, init, headers={"mcp-session-id": "S1"})]
    h.script["tools/call"] = [_Resp(200, TOOL_OK)]
    h.ns.mcp_call("notion-fetch", {})
    assert _by_method(h.calls, "tools/call")[0]["headers"]["MCP-Protocol-Version"] == "2025-06-18"


def test_expired_session_404_reinitialises_once(scripted_http):
    h = scripted_http
    h.script["initialize"] = [
        _Resp(200, INIT_OK, headers={"mcp-session-id": "S1"}),
        _Resp(200, INIT_OK, headers={"mcp-session-id": "S2"}),
    ]
    h.script["tools/call"] = [_Resp(404, text="Session not found"), _Resp(200, TOOL_OK)]
    assert h.ns.mcp_call("notion-fetch", {}) == TOOL_OK["result"]
    tool_calls = _by_method(h.calls, "tools/call")
    assert [c["headers"]["Mcp-Session-Id"] for c in tool_calls] == ["S1", "S2"]
    assert len(_by_method(h.calls, "initialize")) == 2


def test_repeated_404_is_not_retried_forever(scripted_http):
    h = scripted_http
    h.script["initialize"] = [_Resp(200, INIT_OK, headers={"mcp-session-id": "S1"})]
    h.script["tools/call"] = [_Resp(404, text="Session not found")]
    with pytest.raises(Exception, match="404"):
        h.ns.mcp_call("notion-fetch", {})
    assert len(_by_method(h.calls, "tools/call")) == 2


def test_400_surfaces_server_error_without_resending(scripted_http):
    h = scripted_http
    h.script["initialize"] = [_Resp(200, INIT_OK, headers={"mcp-session-id": "S1"})]
    bad = {"jsonrpc": "2.0", "error": {"code": -32602, "message": "Invalid arguments: page_id is required"}}
    h.script["tools/call"] = [_Resp(400, bad)]
    with pytest.raises(ValueError, match="page_id is required"):
        h.ns.mcp_call("notion-update-page", {})
    assert len(_by_method(h.calls, "tools/call")) == 1
    assert len(_by_method(h.calls, "initialize")) == 1


def test_mcp_init_401_refreshes_then_retries(scripted_http):
    h = scripted_http
    h.ns._notion_state["resource_used"] = True
    h.script["initialize"] = [_Resp(401, text="unauthorized"), _Resp(200, INIT_OK, headers={"mcp-session-id": "S9"})]
    h.script["token"] = [_Resp(200, {"access_token": "tok-2", "refresh_token": "ref-2", "expires_in": 28800})]
    assert h.ns.mcp_init() == "S9"
    inits = _by_method(h.calls, "initialize")
    assert [c["headers"]["Authorization"] for c in inits] == ["Bearer tok-1", "Bearer tok-2"]
    token_calls = _token_posts(h.calls)
    assert len(token_calls) == 1
    assert token_calls[0]["data"]["grant_type"] == "refresh_token"
    assert token_calls[0]["data"]["resource"] == "https://mcp.notion.com/mcp"
    assert h.ns._notion_state["refresh_token"] == "ref-2"


def test_dead_refresh_token_needs_reconnect_and_is_not_retried(scripted_http):
    h = scripted_http
    h.script["initialize"] = [_Resp(401, text="unauthorized")]
    h.script["token"] = [_Resp(400, {"error": "invalid_grant"})]
    with pytest.raises(ValueError, match="reconnect"):
        h.ns.mcp_init()
    assert h.ns._notion_state["refresh_dead"] is True
    assert h.ns.needs_reconnect() is True
    assert h.ns.get_state()["needs_reconnect"] is True
    n_token = len(_token_posts(h.calls))
    assert h.ns.refresh_token() is False
    assert len(_token_posts(h.calls)) == n_token  # no new token request


def test_refresh_skipped_when_another_thread_already_refreshed(scripted_http):
    h = scripted_http
    h.ns._notion_state["access_token"] = "tok-new"
    assert h.ns.refresh_token(stale_access_token="tok-1") is True
    assert h.calls == []


def test_concurrent_401s_refresh_once(scripted_http):
    h = scripted_http
    h.script["token"] = [_Resp(200, {"access_token": "tok-2", "refresh_token": "ref-2"})]
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(h.ns.refresh_token(stale_access_token="tok-1")))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == [True] * 4
    assert len(_token_posts(h.calls)) == 1


def test_authorize_and_code_exchange_send_resource(scripted_http):
    from urllib.parse import parse_qs, urlparse

    h = scripted_http
    meta = {
        "authorization_endpoint": "https://auth.example/authorize",
        "token_endpoint": "https://auth.example/token",
        "resource": "https://mcp.notion.com/mcp",
    }
    with patch.object(h.ns, "_discover", return_value=meta), patch.object(h.ns, "_start_callback_server"):
        url = h.ns.start_connect()
    q = parse_qs(urlparse(url).query)
    assert q["resource"] == ["https://mcp.notion.com/mcp"]
    assert q["code_challenge_method"] == ["S256"]

    h.script["token"] = [_Resp(200, {"access_token": "tok-3", "refresh_token": "ref-3"})]
    with patch("app.utils.notion_queue.resume") as resume:
        h.ns._exchange_code("the-code")
    token_call = _token_posts(h.calls)[-1]
    assert token_call["data"]["grant_type"] == "authorization_code"
    assert token_call["data"]["resource"] == "https://mcp.notion.com/mcp"
    assert h.ns._notion_state["access_token"] == "tok-3"
    assert h.ns._notion_state["resource_used"] is True
    resume.assert_called_once()


# RFC 8707 on refresh: only send ``resource`` if the grant was made with it.

REFRESH_OK = {"access_token": "tok-2", "refresh_token": "ref-2", "expires_in": 3600}


def test_refresh_of_pre_resource_login_omits_resource(scripted_http):
    """v2.3.0 logins were authorised without ``resource``; their saved token
    file has no ``resource_used`` key."""
    h = scripted_http
    h.ns._notion_state.pop("resource_used", None)
    h.script["token"] = [_Resp(200, REFRESH_OK)]
    assert h.ns.refresh_token() is True
    (call,) = _token_posts(h.calls)
    assert "resource" not in call["data"]
    assert h.ns._notion_state["access_token"] == "tok-2"


def test_refresh_sends_resource_when_grant_used_it(scripted_http):
    h = scripted_http
    h.ns._notion_state["resource_used"] = True
    h.script["token"] = [_Resp(200, REFRESH_OK)]
    assert h.ns.refresh_token() is True
    assert _token_posts(h.calls)[0]["data"]["resource"] == "https://mcp.notion.com/mcp"


def test_refresh_invalid_target_retries_once_without_resource(scripted_http):
    h = scripted_http
    h.ns._notion_state["resource_used"] = True
    h.script["token"] = [_Resp(400, {"error": "invalid_target"}), _Resp(200, REFRESH_OK)]
    assert h.ns.refresh_token() is True
    first, second = _token_posts(h.calls)
    assert "resource" in first["data"] and "resource" not in second["data"]
    assert h.ns._notion_state["refresh_dead"] is False
    assert h.ns._notion_state["resource_used"] is False  # later refreshes skip it
    assert h.ns.needs_reconnect() is False


def test_refresh_invalid_target_twice_is_dead(scripted_http):
    h = scripted_http
    h.ns._notion_state["resource_used"] = True
    h.script["token"] = [_Resp(400, {"error": "invalid_target"})]
    assert h.ns.refresh_token() is False
    assert len(_token_posts(h.calls)) == 2
    assert h.ns._notion_state["refresh_dead"] is True
    assert h.ns.needs_reconnect() is True


def test_refresh_invalid_target_without_resource_is_dead_at_once(scripted_http):
    h = scripted_http
    h.script["token"] = [_Resp(400, {"error": "invalid_target"})]
    assert h.ns.refresh_token() is False
    assert len(_token_posts(h.calls)) == 1
    assert h.ns._notion_state["refresh_dead"] is True


def test_refresh_5xx_after_invalid_target_is_not_dead(scripted_http):
    h = scripted_http
    h.ns._notion_state["resource_used"] = True
    h.script["token"] = [_Resp(400, {"error": "invalid_target"}), _Resp(503, {"error": "unavailable"})]
    assert h.ns.refresh_token() is False
    assert h.ns._notion_state["refresh_dead"] is False
    assert h.ns._notion_state["resource_used"] is True


# ---------------------------------------------------------------------------
# A fake Notion MCP server at the mcp_call level
# ---------------------------------------------------------------------------

DS_UUID = "0a1b2c3d-4e5f-4a6b-8c7d-0123456789ab"
OTHER_URL = "https://www.notion.so/myteam/Other-ffffffffffffffffffffffffffffffff"


def _text_result(obj):
    return {"content": [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj)}]}


def _db_fetch(schema_props):
    state = json.dumps({"schema": schema_props})
    text = (
        f'<database url="https://www.notion.so/{DB_HEX}"><data-source url="collection://{DS_UUID}">'
        f"<data-source-state>{state}</data-source-state></data-source></database>"
    )
    return _text_result({"metadata": {"type": "database"}, "title": "Meetings", "url": "x", "text": text})


def _page_fetch(title, content, props=None, truncated=False):
    props = props if props is not None else {"title": title}
    text = f'<page url="{PAGE_URL}"><properties>{json.dumps(props)}</properties><content>\n{content}\n</content></page>'
    obj = {"metadata": {"type": "page"}, "title": title, "url": PAGE_URL, "text": text}
    if truncated:
        obj["truncated"] = True
    return _text_result(obj)


class FakeNotion:
    """Stand-in for notion.mcp_call: records calls; per-tool canned results."""

    def __init__(self):
        self.calls = []
        self.handlers = {}

    def __call__(self, method, params=None, timeout=None):
        self.calls.append((method, params or {}))
        h = self.handlers.get(method)
        if h is None:
            return _text_result({"ok": True})
        return h(params or {}) if callable(h) else h

    def methods(self):
        return [m for m, _ in self.calls]

    def of(self, method):
        return [p for m, p in self.calls if m == method]


def _raise(msg):
    def f(_params):
        raise ValueError(msg)

    return f


@pytest.fixture
def fake():
    f = FakeNotion()
    with patch("app.state.notion.mcp_call", side_effect=f):
        yield f


def _save_page(sid=SID, url=PAGE_URL, **extra):
    from app.utils.config import save_config

    save_config({"notion_page_urls": {sid: url}, **extra})


# ---------------------------------------------------------------------------
# 1. Tag/rename changes touch properties only; re-push replaces only our section
# ---------------------------------------------------------------------------


def test_tag_change_updates_properties_only(env, fake, queue_state):
    from app.utils import notion_queue

    _setup_existing_page(env)
    notion_queue.enqueue(SID)  # what /recordings/tag does
    _run_queue_once(queue_state)

    assert fake.methods() == ["notion-update-page"]
    p = fake.of("notion-update-page")[0]
    assert p["command"] == "update_properties"
    assert json.loads(p["properties"]["Tags"]) == ["A"]
    assert p["properties"]["Session ID"] == SID
    assert "new_str" not in p and "allow_deleting_content" not in p
    assert queue_state.entries == {}


def test_repush_replaces_only_app_section(env, fake):
    from app.routes import notion as rn

    _save_page()
    _write_transcript(env.trn, SID, {"text": "new words", "summary": "Fresh summary"})
    old_section = rn._build_app_section({"text": "old words"}, SID)
    content = f"My own notes at the top\n\n{old_section}\n\nAction items I added later"
    fake.handlers["notion-fetch"] = _page_fetch("T", content)

    r = env.client.post("/notion/push", json={"session_id": SID})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["content"] == "replaced"

    upd = fake.of("notion-update-page")
    assert [p["command"] for p in upd] == ["update_properties", "update_content"]
    op = upd[1]["content_updates"][0]
    assert op["old_str"] == old_section
    assert "My own notes" not in op["old_str"] and "Action items" not in op["old_str"]
    assert "new words" in op["new_str"] and "Fresh summary" in op["new_str"]
    assert rn.APP_SECTION_HEADING in op["new_str"] and rn.APP_SECTION_END in op["new_str"]
    assert all("allow_deleting_content" not in p for p in upd)


def test_repush_unchanged_section_makes_no_content_write(env, fake):
    from app.routes import notion as rn

    _save_page()
    t = {"text": "same words"}
    _write_transcript(env.trn, SID, t)
    fake.handlers["notion-fetch"] = _page_fetch("T", "notes\n\n" + rn._build_app_section(t, SID))
    r = env.client.post("/notion/push", json={"session_id": SID})
    assert r.get_json()["content"] == "unchanged"
    assert [p["command"] for p in fake.of("notion-update-page")] == ["update_properties"]


def test_repush_legacy_page_appends_dated_section(env, fake):
    from app.routes import notion as rn

    _save_page()
    _write_transcript(env.trn, SID, {"text": "new words"})
    content = "Old summary\n\n---\n\n## Full Transcript\n\n[00:00] hello\n\nMy notes"
    fake.handlers["notion-fetch"] = _page_fetch("T", content)

    r = env.client.post("/notion/push", json={"session_id": SID})
    body = r.get_json()
    assert body["content"] == "appended" and "kept" in body["content_note"]
    upd = fake.of("notion-update-page")
    assert [p["command"] for p in upd] == ["update_properties", "update_content"]
    op = upd[1]["content_updates"][0]
    assert op["old_str"] == "My notes"
    assert op["new_str"].startswith("My notes\n\n## " + rn.APP_SECTION_HEADING + " (")
    assert "new words" in op["new_str"]


def test_repush_falls_back_to_append_when_in_place_replace_fails(env, fake):
    from app.routes import notion as rn

    _save_page()
    _write_transcript(env.trn, SID, {"text": "new words"})
    fake.handlers["notion-fetch"] = _page_fetch("T", rn._build_app_section({"text": "old"}, SID) + "\n\nmine")
    seen = []

    def update(p):
        seen.append(p)
        if p["command"] == "update_content" and len([s for s in seen if s["command"] == "update_content"]) == 1:
            raise ValueError("validation_error: old_str not found")
        return _text_result({"ok": True})

    fake.handlers["notion-update-page"] = update
    r = env.client.post("/notion/push", json={"session_id": SID})
    assert r.get_json()["content"] == "appended"
    assert seen[-1]["content_updates"][0]["old_str"] == "mine"


def test_repush_truncated_page_without_section_only_updates_properties(env, fake):
    _save_page()
    _write_transcript(env.trn, SID, {"text": "x"})
    fake.handlers["notion-fetch"] = _page_fetch("T", "huge page …", truncated=True)
    body = env.client.post("/notion/push", json={"session_id": SID}).get_json()
    assert body["content"] == "skipped"
    assert [p["command"] for p in fake.of("notion-update-page")] == ["update_properties"]


def test_repush_empty_page_uses_replace_without_deleting(env, fake):
    _save_page()
    _write_transcript(env.trn, SID, {"text": "x"})
    fake.handlers["notion-fetch"] = _page_fetch("T", "")
    body = env.client.post("/notion/push", json={"session_id": SID}).get_json()
    assert body["content"] == "appended"
    upd = fake.of("notion-update-page")
    assert upd[1]["command"] == "replace_content"
    assert "allow_deleting_content" not in upd[1]


def test_create_writes_marked_section_and_session_id(env, fake):
    from app.routes import notion as rn

    _write_transcript(env.trn, SID, {"text": "hello", "auto_title": "Standup"})
    fake.handlers["notion-create-pages"] = _text_result({"pages": [{"id": DB_UUID, "url": PAGE_URL}]})
    r = env.client.post("/notion/push", json={"session_id": SID})
    assert r.get_json() == {"pushed": True, "updated": False, "url": PAGE_URL}
    args = fake.of("notion-create-pages")[0]
    page = args["pages"][0]
    assert args["allow_async"] is True
    assert page["properties"] == {"title": "Standup", "Tags": "[]", "Session ID": SID}
    assert page["content"].startswith("## " + rn.APP_SECTION_HEADING)
    assert f"Session ID: {SID}" in page["content"]
    assert _cfg()["notion_page_urls"][SID] == PAGE_URL


def test_extract_page_url_ignores_async_status_url():
    from app.routes.notion import _extract_page_url

    res = _text_result({"async_task": {"id": "t", "status_url": "https://api.notion.com/v1/tasks/t"}})
    assert _extract_page_url(res) is None
    assert _extract_page_url(_text_result({"result": {"pages": [{"url": PAGE_URL}]}})) == PAGE_URL


# ---------------------------------------------------------------------------
# 2. Schema: add-only, from a fresh fetch, never from a stale/empty cache
# ---------------------------------------------------------------------------

TAGS_AB = {"type": "multi_select", "options": [{"name": "A", "color": "blue"}, {"name": "B", "color": "red"}]}


@pytest.fixture
def schema_fake():
    from app.routes import notion as rn

    f = FakeNotion()
    with (
        patch("app.state.notion.get_database_id", return_value="db-id"),
        patch("app.state.notion.mcp_call", side_effect=f),
        patch.object(rn, "_schema_cache", None),
    ):
        yield f


def _ddl(f):
    return [p["statements"] for p in f.of("notion-update-data-source")]


def test_schema_adds_missing_options_as_union_keeping_colours(schema_fake):
    from app.routes import notion as rn

    schema_fake.handlers["notion-fetch"] = _db_fetch({"Tags": TAGS_AB, "Session ID": {"type": "text"}})
    s = rn._ensure_schema(["A", "C"])
    assert _ddl(schema_fake) == ["ALTER COLUMN \"Tags\" SET MULTI_SELECT('A':blue, 'B':red, 'C':default)"]
    assert schema_fake.of("notion-update-data-source")[0]["data_source_id"] == DS_UUID
    assert set(s["tag_options"]) == {"A", "B", "C"}


def test_schema_never_sets_from_stale_cache(schema_fake):
    from app.routes import notion as rn

    # Cache from earlier in the session: only "Z" (since deleted in Notion),
    # and it lacks C, so a change is needed — it must come from a fresh fetch.
    rn._schema_cache = {**_schema(), "tag_options": {"Z": "default"}}
    schema_fake.handlers["notion-fetch"] = _db_fetch({"Tags": TAGS_AB, "Session ID": {"type": "text"}})
    rn._ensure_schema(["C"])
    assert schema_fake.methods() == ["notion-fetch", "notion-update-data-source"]
    assert _ddl(schema_fake) == ["ALTER COLUMN \"Tags\" SET MULTI_SELECT('A':blue, 'B':red, 'C':default)"]


def test_schema_fetch_failure_changes_nothing(schema_fake):
    from app.routes import notion as rn

    schema_fake.handlers["notion-fetch"] = _raise("connection reset")
    with pytest.raises(ValueError, match="Could not read the Notion database schema"):
        rn._ensure_schema(["A"])
    assert _ddl(schema_fake) == []
    assert rn._schema_cache is None


def test_schema_unreadable_layout_changes_nothing(schema_fake):
    from app.routes import notion as rn

    schema_fake.handlers["notion-fetch"] = _text_result("Meetings database, no state block")
    assert rn._ensure_schema(["A"]) is None
    assert _ddl(schema_fake) == []


def test_schema_no_tags_column_for_untagged_recording(schema_fake):
    from app.routes import notion as rn

    schema_fake.handlers["notion-fetch"] = _db_fetch({"Name": {"type": "title"}})
    s = rn._ensure_schema([])
    assert _ddl(schema_fake) == ['ADD COLUMN "Session ID" RICH_TEXT']
    assert s["tags_exists"] is False and s["session_prop"] is True
    assert rn._page_properties("T", [], SID, s) == {"title": "T", "Session ID": SID}


def test_schema_adds_tags_column_with_options(schema_fake):
    from app.routes import notion as rn

    schema_fake.handlers["notion-fetch"] = _db_fetch({"Session ID": {"type": "text"}})
    rn._ensure_schema(["It's"])
    assert _ddl(schema_fake) == ["ADD COLUMN \"Tags\" MULTI_SELECT('It''s':default)"]


def test_schema_option_add_failure_blocks_the_write(schema_fake):
    from app.routes import notion as rn

    schema_fake.handlers["notion-fetch"] = _db_fetch({"Tags": TAGS_AB, "Session ID": {"type": "text"}})
    schema_fake.handlers["notion-update-data-source"] = _raise("rate limited")
    with pytest.raises(ValueError, match="Could not add tag options"):
        rn._ensure_schema(["C"])


def test_schema_cache_skips_fetch_when_satisfied_and_is_per_database(schema_fake):
    from app.routes import notion as rn

    rn._schema_cache = _schema()
    assert rn._ensure_schema(["A"]) is rn._schema_cache
    assert schema_fake.calls == []
    rn._schema_cache = {**_schema(), "db_id": "old-db"}
    schema_fake.handlers["notion-fetch"] = _db_fetch({"Tags": TAGS_AB, "Session ID": {"type": "text"}})
    rn._ensure_schema(["A"])
    assert schema_fake.methods() == ["notion-fetch"]


def test_schema_cache_reset_when_database_changes(env):
    from app.routes import notion as rn

    with patch.object(rn, "_schema_cache", _schema()), patch("app.state.notion.set_database"):
        r = env.client.post("/notion/set-database", json={"database_id": "new-db", "database_name": "N"})
        assert r.status_code == 200
        assert rn._schema_cache is None


# ---------------------------------------------------------------------------
# 3. Duplicates: upsert by Session ID, per-session lock, async creates
# ---------------------------------------------------------------------------


def test_upsert_finds_existing_page_by_session_id(env, fake):
    from app.routes import notion as rn

    t = {"text": "hello"}
    _write_transcript(env.trn, SID, t)
    rows = {"results": [{"url": OTHER_URL, "Session ID": "999"}, {"url": PAGE_URL, "properties": {"Session ID": SID}}]}
    fake.handlers["notion-query-data-sources"] = _text_result(rows)
    fake.handlers["notion-fetch"] = _page_fetch("T", rn._build_app_section(t, SID))
    with patch("app.routes.notion._find_page_by_session_id", env.real.find):
        body = env.client.post("/notion/push", json={"session_id": SID}).get_json()
    assert body["updated"] is True and body["url"] == PAGE_URL
    assert "notion-create-pages" not in fake.methods()
    q = fake.of("notion-query-data-sources")[0]["data"]
    assert q["mode"] == "rows" and q["data_source_url"] == "collection://ds-id"
    assert _cfg()["notion_page_urls"][SID] == PAGE_URL


def test_upsert_falls_back_to_search_and_verifies_candidate(env, fake):
    from app.routes import notion as rn

    t = {"text": "hello"}
    _write_transcript(env.trn, SID, t)
    fake.handlers["notion-query-data-sources"] = _raise("plan_required")
    fake.handlers["notion-search"] = _text_result({"results": [{"url": PAGE_URL, "title": "x"}]})
    fake.handlers["notion-fetch"] = _page_fetch("T", rn._build_app_section(t, SID), props={"Session ID": SID})
    with patch("app.routes.notion._find_page_by_session_id", env.real.find):
        body = env.client.post("/notion/push", json={"session_id": SID}).get_json()
    assert body["url"] == PAGE_URL and body["updated"] is True
    assert "notion-create-pages" not in fake.methods()


def test_upsert_does_not_trust_unverified_candidates(env, fake):
    _write_transcript(env.trn, SID, {"text": "hello"})
    fake.handlers["notion-query-data-sources"] = _text_result({"results": [{"url": OTHER_URL, "Session ID": "42"}]})
    fake.handlers["notion-search"] = _text_result({"results": [{"url": OTHER_URL}]})
    fake.handlers["notion-fetch"] = _page_fetch("Other", "unrelated page")
    fake.handlers["notion-create-pages"] = _text_result({"pages": [{"url": PAGE_URL}]})
    with patch("app.routes.notion._find_page_by_session_id", env.real.find):
        body = env.client.post("/notion/push", json={"session_id": SID}).get_json()
    assert body["url"] == PAGE_URL and body["updated"] is False
    assert fake.methods().count("notion-create-pages") == 1


def test_upsert_lookup_failure_does_not_create_blindly(env, fake):
    _write_transcript(env.trn, SID, {"text": "hello"})
    fake.handlers["notion-query-data-sources"] = _raise("boom")
    fake.handlers["notion-search"] = _raise("boom")
    with patch("app.routes.notion._find_page_by_session_id", env.real.find):
        r = env.client.post("/notion/push", json={"session_id": SID})
    assert r.status_code == 500
    assert "notion-create-pages" not in fake.methods()


def test_per_session_lock_prevents_double_create(env, fake):
    from app.routes import notion as rn

    t = {"text": "hello"}
    _write_transcript(env.trn, SID, t)

    def slow_create(_p):
        time.sleep(0.3)
        return _text_result({"pages": [{"id": DB_UUID, "url": PAGE_URL}]})

    fake.handlers["notion-create-pages"] = slow_create
    fake.handlers["notion-fetch"] = _page_fetch("T", rn._build_app_section(t, SID))
    results = []
    threads = [threading.Thread(target=lambda: results.append(rn.push_to_notion(SID))) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    assert fake.methods().count("notion-create-pages") == 1
    assert sorted(bool(r.get("updated")) for r in results) == [False, True]


def test_async_create_is_resumed_not_duplicated(env, fake):
    from app.routes import notion as rn

    t = {"text": "hello"}
    _write_transcript(env.trn, SID, t)
    fake.handlers["notion-create-pages"] = _text_result(
        {"async_task": {"id": "task-1", "status": "queued", "poll_after_seconds": 2}}
    )
    # First push: Notion is still working when our wait runs out.
    out = rn.push_to_notion(SID, poll_budget=0)
    assert out == {"pending": True, "message": "Notion is still creating the page"}
    assert _cfg()["notion_pending_creates"][SID]["task_id"] == "task-1"

    # Next attempt polls the same task instead of creating again.
    fake.handlers["notion-get-async-task"] = _text_result(
        {"status": "succeeded", "result": {"pages": [{"id": DB_UUID, "url": PAGE_URL}]}}
    )
    fake.handlers["notion-fetch"] = _page_fetch("T", rn._build_app_section(t, SID))
    out = rn.push_to_notion(SID, poll_budget=10)
    assert out["url"] == PAGE_URL and out["updated"] is True
    assert fake.methods().count("notion-create-pages") == 1
    assert fake.of("notion-get-async-task")[0] == {"task_id": "task-1"}
    assert SID not in _cfg().get("notion_pending_creates", {})
    assert _cfg()["notion_page_urls"][SID] == PAGE_URL


def test_push_route_hands_pending_create_to_queue(env, fake):
    _write_transcript(env.trn, SID, {"text": "hello"})
    fake.handlers["notion-create-pages"] = _text_result({"async_task": {"id": "t", "status": "queued"}})
    fake.handlers["notion-get-async-task"] = _text_result({"status": "running"})
    with patch("app.routes.notion._POLL_BUDGET_REQUEST", 0), patch("app.utils.notion_queue.enqueue") as enq:
        body = env.client.post("/notion/push", json={"session_id": SID}).get_json()
    assert body["queued"] is True and body["pushed"] is False
    enq.assert_called_once_with(SID, "full")


def test_create_uses_long_timeout(env):
    from app.routes import notion as rn

    _write_transcript(env.trn, SID, {"text": "hello"})
    seen = {}

    def mcp(method, params=None, timeout=30):
        seen[method] = timeout
        return _text_result({"pages": [{"url": PAGE_URL}]})

    with patch("app.state.notion.mcp_call", side_effect=mcp):
        rn.push_to_notion(SID)
    assert seen["notion-create-pages"] >= 120


# ---------------------------------------------------------------------------
# 4. Auto-push goes through the queue
# ---------------------------------------------------------------------------


def test_summarize_auto_push_enqueues_instead_of_pushing(env):
    from app.utils.config import save_config

    save_config({"notion_auto_push": True})
    _write_transcript(env.trn, SID, {"text": "A long enough transcript to summarise here."})
    with (
        patch("app.routes.transcribe.TRANSCRIPTS_DIR", env.trn),
        patch("app.routes.transcribe.summarize", return_value=("Sum.", "Title", "x", [])),
        patch("app.utils.notion_queue.enqueue") as enq,
        patch("app.routes.notion.push_to_notion") as push,
    ):
        r = env.client.post("/summarize", json={"session_id": SID})
    body = r.get_json()
    assert r.status_code == 200, body
    assert body["notion_queued"] is True and body["notion_error"] == ""
    enq.assert_called_once_with(SID, "full")
    push.assert_not_called()


def test_summarize_auto_push_reports_missing_database(env):
    from app.utils.config import save_config

    save_config({"notion_auto_push": True})
    _write_transcript(env.trn, SID, {"text": "A long enough transcript to summarise here."})
    with (
        patch("app.routes.transcribe.TRANSCRIPTS_DIR", env.trn),
        patch("app.routes.transcribe.summarize", return_value=("Sum.", "Title", "x", [])),
        patch("app.state.notion.get_database_id", return_value=None),
        patch("app.utils.notion_queue.enqueue") as enq,
    ):
        body = env.client.post("/summarize", json={"session_id": SID}).get_json()
    assert body["notion_queued"] is False and "No database" in body["notion_error"]
    enq.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Queue durability, visible failures, auth-pause backoff
# ---------------------------------------------------------------------------


def test_queue_entry_survives_crash_mid_push(queue_state):
    from app.utils import notion_queue

    notion_queue.enqueue(SID, "full")
    entry = queue_state.pop_due()
    assert entry["session_id"] == SID
    # Still queued in memory and on disk while the push runs...
    assert SID in queue_state.entries
    # ...so a crash now (no complete/record_failure) loses nothing.
    reloaded = notion_queue._State(queue_state.path)
    assert reloaded.entries[SID]["kind"] == "full"
    assert reloaded.pop_due()["session_id"] == SID


def test_queue_claimed_entry_not_handed_out_twice(queue_state):
    queue_state.enqueue(SID)
    assert queue_state.pop_due() is not None
    assert queue_state.pop_due() is None


def test_queue_newer_enqueue_during_push_survives_completion(queue_state):
    queue_state.enqueue(SID)
    entry = queue_state.pop_due()
    queue_state.enqueue(SID)  # e.g. another tag edit while pushing
    queue_state.complete(entry)
    assert SID in queue_state.entries
    assert queue_state.pop_due() is not None


def test_queue_full_kind_not_downgraded(queue_state):
    queue_state.enqueue(SID, "full")
    queue_state.enqueue(SID, "properties")
    assert queue_state.entries[SID]["kind"] == "full"


def test_queue_permanent_failure_is_recorded_and_retryable(env, mcp_http, queue_state):
    from app.utils import notion_queue

    _setup_existing_page(env)
    mcp_http(_FakeResp(TOOL_ERROR))
    notion_queue.enqueue(SID, "full")
    _run_queue_once(queue_state)
    assert queue_state.entries == {}

    snap = env.client.get("/notion/queue-status").get_json()
    assert snap["failed_count"] == 1
    assert snap["failed"][0]["session_id"] == SID and "validation_error" in snap["failed"][0]["error"]
    assert json.loads(queue_state.path.read_text())["failed"][0]["session_id"] == SID

    snap = env.client.post("/notion/queue-resume").get_json()
    assert snap["failed_count"] == 0 and snap["pending"] == 1
    assert queue_state.entries[SID]["kind"] == "full"


def test_queue_pending_async_create_is_retried(env, queue_state):
    from app.utils import notion_queue

    notion_queue.enqueue(SID, "full")
    with patch("app.routes.notion.push_to_notion", return_value={"pending": True, "message": "still creating"}) as p:
        _run_queue_once(queue_state)
    p.assert_called_once_with(SID, mode="full")
    assert queue_state.entries[SID]["attempts"] == 1
    assert queue_state.entries[SID]["failures"] == 0  # waiting doesn't use up the cap
    assert queue_state.failed == []


def test_pause_probe_backs_off_and_stops_for_dead_login(queue_state):
    from app.utils import notion_queue

    queue_state.pause("Notion auth expired")
    with (
        patch.dict(notion_queue._probe, {"last": 0.0, "interval": 30.0}),
        patch("app.state.notion.login_is_dead", return_value=False),
        patch("app.state.notion.verify_connection", return_value=False) as verify,
    ):
        assert notion_queue._probe_while_paused(now=100.0) is True
        assert notion_queue._probe["interval"] == 60.0
        assert notion_queue._probe_while_paused(now=130.0) is False  # backing off
        assert notion_queue._probe_while_paused(now=161.0) is True
        assert notion_queue._probe["interval"] == 120.0
        assert verify.call_count == 2

    with (
        patch.dict(notion_queue._probe, {"last": 0.0, "interval": 30.0}),
        patch("app.state.notion.login_is_dead", return_value=True),
        patch("app.state.notion.verify_connection") as verify,
    ):
        assert notion_queue._probe_while_paused(now=10_000.0) is False
        verify.assert_not_called()
    assert queue_state.paused is True

    with (
        patch.dict(notion_queue._probe, {"last": 0.0, "interval": 30.0}),
        patch("app.state.notion.login_is_dead", return_value=False),
        patch("app.state.notion.verify_connection", return_value=True),
    ):
        notion_queue._probe_while_paused(now=10_000.0)
    assert queue_state.paused is False


def test_queue_status_reports_needs_reconnect(env, queue_state):
    with patch("app.state.notion.needs_reconnect", return_value=True):
        snap = env.client.get("/notion/queue-status").get_json()
    assert snap["needs_reconnect"] is True


# ---------------------------------------------------------------------------
# 7. Rename reaches Notion (properties only), with the real page title
# ---------------------------------------------------------------------------


def test_fetch_page_title_parses_title_not_whole_text(fake):
    from app.routes import notion as rn

    fake.handlers["notion-fetch"] = _page_fetch("Real Title", "the title of this talk was…")
    assert rn._fetch_notion_page_title(PAGE_URL) == "Real Title"
    assert fake.of("notion-fetch")[0] == {"id": DB_UUID}


def test_rename_enqueues_property_sync(env):
    _save_page()
    with (
        patch("app.routes.recordings.TRANSCRIPTS_DIR", env.trn),
        patch("app.utils.notion_queue.enqueue") as enq,
    ):
        r = env.client.post("/recordings/rename", json={"session_id": SID, "title": "New name"})
    assert r.get_json() == {"saved": True, "notion_queued": True}
    enq.assert_called_once_with(SID, "properties")


def test_rename_without_notion_page_does_not_enqueue(env):
    with (
        patch("app.routes.recordings.TRANSCRIPTS_DIR", env.trn),
        patch("app.utils.notion_queue.enqueue") as enq,
    ):
        r = env.client.post("/recordings/rename", json={"session_id": SID, "title": "New name"})
    assert r.get_json() == {"saved": True, "notion_queued": False}
    enq.assert_not_called()


def test_rename_sync_sends_new_title_when_notion_title_unchanged(env, fake, queue_state):
    from app.utils import notion_queue

    _save_page(recording_names={SID: "New name"}, notion_pushed_titles={SID: "Old name"})
    _write_transcript(env.trn, SID, {"text": "x"})
    fake.handlers["notion-fetch"] = _page_fetch("Old name", "body")
    notion_queue.enqueue(SID, "properties")
    _run_queue_once(queue_state)
    upd = fake.of("notion-update-page")
    assert len(upd) == 1 and upd[0]["command"] == "update_properties"
    assert upd[0]["properties"]["title"] == "New name"
    assert _cfg()["notion_pushed_titles"][SID] == "New name"


def test_rename_sync_keeps_title_user_changed_in_notion(env, fake, queue_state):
    from app.utils import notion_queue

    _save_page(recording_names={SID: "New name"}, notion_pushed_titles={SID: "Old name"})
    _write_transcript(env.trn, SID, {"text": "x"})
    fake.handlers["notion-fetch"] = _page_fetch("Renamed in Notion", "body")
    notion_queue.enqueue(SID, "properties")
    _run_queue_once(queue_state)
    props = fake.of("notion-update-page")[0]["properties"]
    assert "title" not in props


# ---------------------------------------------------------------------------
# Review 2 / 1c: attempt cap — no job retries hourly forever
# ---------------------------------------------------------------------------


def _fail_once(queue_state, error="rate limited", **kw):
    for e in queue_state.entries.values():
        e["next_retry_at"] = 0
    entry = queue_state.pop_due()
    assert entry is not None
    queue_state.record_failure(entry, error, permanent=False, **kw)


def test_queue_gives_up_after_max_failures(queue_state):
    from app.utils import notion_queue

    notion_queue.enqueue(SID, "full")
    for _ in range(notion_queue._MAX_FAILURES - 1):
        _fail_once(queue_state)
    assert queue_state.entries[SID]["failures"] == notion_queue._MAX_FAILURES - 1
    assert queue_state.failed == []

    _fail_once(queue_state)
    assert queue_state.entries == {}
    (failed,) = queue_state.failed
    assert failed["session_id"] == SID and failed["kind"] == "full"
    assert failed["error"].startswith(f"Gave up after {notion_queue._MAX_FAILURES} failed attempts: rate limited")
    on_disk = json.loads(queue_state.path.read_text())
    assert on_disk["entries"] == {} and on_disk["failed"][0]["session_id"] == SID

    # "Retry" gives it a fresh budget.
    assert queue_state.retry_failed() == 1
    assert queue_state.entries[SID].get("failures", 0) == 0


def test_queue_failure_count_survives_restart(queue_state):
    from app.utils import notion_queue

    notion_queue.enqueue(SID)
    _fail_once(queue_state)
    _fail_once(queue_state)
    reloaded = notion_queue._State(queue_state.path)
    assert reloaded.entries[SID]["failures"] == 2


def test_queue_waits_do_not_count_towards_cap(queue_state):
    from app.utils import notion_queue

    notion_queue.enqueue(SID)
    for _ in range(notion_queue._MAX_FAILURES * 2):
        _fail_once(queue_state, "Notion not connected", counts=False)
    assert queue_state.entries[SID]["failures"] == 0
    assert queue_state.failed == []


def test_queue_auth_pause_does_not_count(env, mcp_http, queue_state):
    _setup_existing_page(env)
    mcp_http(_FakeResp(_tool_error("unauthorized: invalid_token")))
    from app.utils import notion_queue

    notion_queue.enqueue(SID)
    _run_queue_once(queue_state)
    assert queue_state.paused is True
    assert queue_state.entries[SID]["failures"] == 0


# ---------------------------------------------------------------------------
# Review 2 / 1a: the saved page was deleted or archived in Notion
# ---------------------------------------------------------------------------

NEW_PAGE_URL = "https://www.notion.so/myteam/Recreated-abcdefabcdefabcdefabcdefabcdef12"
NOT_FOUND = (
    "object_not_found: Could not find page with ID: 2e80f387-aa1b-4c3d-9e8f-0123456789ab. "
    "Make sure the relevant pages and databases are shared with your integration."
)
ARCHIVED = "validation_error: Can't edit block that is archived. You must unarchive the block before editing."


def _setup_page_in_transcript(env, **cfg):
    from app.utils.config import save_config

    save_config({"notion_page_urls": {SID: PAGE_URL}, "recording_tags": {SID: ["A"]}, **cfg})
    _write_transcript(env.trn, SID, {"text": "hello", "auto_title": "Standup", "notion_page_url": PAGE_URL})


def _transcript(env):
    return json.loads((env.trn / f"{SID}.json").read_text())


@pytest.mark.parametrize("error", [NOT_FOUND, ARCHIVED])
def test_full_push_recreates_deleted_or_archived_page(env, fake, error):
    from app.routes import notion as rn

    _setup_page_in_transcript(env)
    fake.handlers["notion-update-page"] = _raise(error)
    fake.handlers["notion-create-pages"] = _text_result({"pages": [{"url": NEW_PAGE_URL}]})
    seen = []

    def lookup(sid, schema):
        # By now the stale URL is gone from both config and transcript.
        seen.append((_cfg().get("notion_page_urls", {}).get(sid), _transcript(env).get("notion_page_url")))
        return None

    with patch.object(rn, "_find_page_by_session_id", side_effect=lookup):
        out = rn.push_to_notion(SID, mode="full")
    assert out == {"pushed": True, "updated": False, "url": NEW_PAGE_URL}
    assert seen == [(None, None)]
    assert len(fake.of("notion-update-page")) == 1  # tried once, then recreated
    assert len(fake.of("notion-create-pages")) == 1
    assert _cfg()["notion_page_urls"][SID] == NEW_PAGE_URL
    assert _transcript(env)["notion_page_url"] == NEW_PAGE_URL


def test_full_push_ignores_lookup_hit_on_the_gone_page(env, fake):
    from app.routes import notion as rn

    _setup_page_in_transcript(env)
    fake.handlers["notion-update-page"] = _raise(NOT_FOUND)
    fake.handlers["notion-create-pages"] = _text_result({"pages": [{"url": NEW_PAGE_URL}]})
    with patch.object(rn, "_find_page_by_session_id", return_value=PAGE_URL):
        out = rn.push_to_notion(SID, mode="full")
    assert out["url"] == NEW_PAGE_URL
    assert len(fake.of("notion-create-pages")) == 1


def test_full_push_uses_other_existing_page_found_by_session_id(env, fake):
    from app.routes import notion as rn

    _setup_page_in_transcript(env)
    calls = {"n": 0}

    def update(params):
        calls["n"] += 1
        if params["page_id"] == rn._extract_page_id(PAGE_URL):
            raise ValueError(NOT_FOUND)
        return _text_result({"ok": True})

    fake.handlers["notion-update-page"] = update
    fake.handlers["notion-fetch"] = _page_fetch("Standup", "")
    with patch.object(rn, "_find_page_by_session_id", return_value=OTHER_URL):
        out = rn.push_to_notion(SID, mode="full")
    assert out["url"] == OTHER_URL and out["updated"] is True
    assert fake.of("notion-create-pages") == []
    assert _cfg()["notion_page_urls"][SID] == OTHER_URL


def test_forget_page_url_keeps_a_url_saved_meanwhile(env):
    from app.routes import notion as rn
    from app.utils.config import save_config

    save_config({"notion_page_urls": {SID: NEW_PAGE_URL}})
    _write_transcript(env.trn, SID, {"text": "x", "notion_page_url": NEW_PAGE_URL})
    rn._forget_notion_page_url(SID, PAGE_URL)
    assert _cfg()["notion_page_urls"][SID] == NEW_PAGE_URL
    assert _transcript(env)["notion_page_url"] == NEW_PAGE_URL


def test_properties_job_on_gone_page_becomes_full_push_once(env, fake, queue_state):
    from app.utils import notion_queue

    _setup_page_in_transcript(env)
    fake.handlers["notion-update-page"] = _raise(NOT_FOUND)
    fake.handlers["notion-create-pages"] = _text_result({"pages": [{"url": NEW_PAGE_URL}]})

    notion_queue.enqueue(SID, "properties")
    _run_queue_once(queue_state)
    entry = queue_state.entries[SID]
    assert entry["kind"] == "full" and entry["converted_from"] == "properties"
    assert entry["attempts"] == 0 and entry["next_retry_at"] <= time.time()
    assert fake.of("notion-create-pages") == []  # properties mode never creates
    assert _cfg()["notion_page_urls"][SID] == PAGE_URL  # untouched until the full push

    _run_queue_once(queue_state)  # the full push recreates the page
    assert queue_state.entries == {} and queue_state.failed == []
    assert _cfg()["notion_page_urls"][SID] == NEW_PAGE_URL


def test_converted_job_is_not_converted_again(queue_state):
    from app.utils import notion_queue

    notion_queue.enqueue(SID, "properties")
    entry = queue_state.pop_due()
    assert queue_state.convert_to_full(entry, "gone") is True
    entry = queue_state.pop_due()
    assert queue_state.convert_to_full(entry, "gone") is False  # already full


def test_full_push_gives_up_when_database_is_gone(env, fake, queue_state):
    from app.utils import notion_queue

    _setup_page_in_transcript(env)
    fake.handlers["notion-update-page"] = _raise(NOT_FOUND)
    fake.handlers["notion-create-pages"] = _raise(
        "object_not_found: Could not find database with ID: be907abe-510e-4116-a3d1-7ea71018c06f."
    )
    notion_queue.enqueue(SID, "full")
    _run_queue_once(queue_state)
    assert queue_state.entries == {}
    assert "Could not find database" in queue_state.failed[0]["error"]


@pytest.mark.parametrize(
    "msg",
    [
        NOT_FOUND,
        ARCHIVED,
        "Notion MCP rejected the request (400): Could not find block with ID: x",
        '{"object":"error","status":404,"code":"object_not_found","message":"Could not find page with ID: x"}',
    ],
)
def test_page_gone_detection(msg):
    from app.routes import notion as rn

    assert rn._is_page_gone_error(ValueError(msg))


def test_page_gone_detection_reads_http_error_body():
    import requests

    from app.routes import notion as rn

    resp = types.SimpleNamespace(status_code=404, text='{"code":"object_not_found"}')
    assert rn._is_page_gone_error(requests.HTTPError("Notion MCP HTTP 404: x", response=resp))


@pytest.mark.parametrize("msg", ["rate limited", "Notion MCP HTTP 404: Session not found", "Gateway Timeout"])
def test_page_gone_detection_ignores_other_errors(msg):
    from app.routes import notion as rn

    assert not rn._is_page_gone_error(ValueError(msg))


@pytest.mark.parametrize(
    "msg",
    [
        "object_not_found: Could not find database with ID: x",
        "restricted_resource: API token does not have access to this resource.",
        ARCHIVED,
    ],
)
def test_not_found_and_archived_errors_are_permanent(msg):
    from app.utils import notion_queue

    assert notion_queue._is_permanent_error(msg)


# ---------------------------------------------------------------------------
# Review 2 / 1b: tag names Notion rejects
# ---------------------------------------------------------------------------


def test_sanitize_tags():
    from app.routes import notion as rn

    tags = ["Smith, John", "x" * 150, "Work", "work", "  ", ",", 7, "ok"]
    assert rn._sanitize_tags(tags) == ["Smith John", "x" * 100, "Work", "ok"]


def test_push_sends_sanitized_tags(env, fake):
    from app.routes import notion as rn
    from app.utils.config import save_config

    save_config({"notion_page_urls": {SID: PAGE_URL}, "recording_tags": {SID: ["Q1,Q2", "B"]}})
    _write_transcript(env.trn, SID, {"text": "x"})
    with patch.object(rn, "_ensure_schema", return_value=_schema()) as ens:
        rn.push_to_notion(SID, mode="properties")
    ens.assert_called_once_with(["Q1 Q2", "B"])
    assert json.loads(fake.of("notion-update-page")[0]["properties"]["Tags"]) == ["Q1 Q2", "B"]


def test_schema_sanitises_option_names_in_ddl(schema_fake):
    from app.routes import notion as rn

    schema_fake.handlers["notion-fetch"] = _db_fetch({"Tags": TAGS_AB, "Session ID": {"type": "text"}})
    rn._ensure_schema(["a, b"])
    assert _ddl(schema_fake) == ["ALTER COLUMN \"Tags\" SET MULTI_SELECT('A':blue, 'B':red, 'a b':default)"]


def test_schema_matches_existing_options_case_insensitively(schema_fake):
    from app.routes import notion as rn

    schema_fake.handlers["notion-fetch"] = _db_fetch({"Tags": TAGS_AB, "Session ID": {"type": "text"}})
    rn._ensure_schema(["a", "b"])
    assert _ddl(schema_fake) == []


def test_schema_rejection_is_permanent(schema_fake):
    from app.routes import notion as rn
    from app.utils import notion_queue

    schema_fake.handlers["notion-fetch"] = _db_fetch({"Tags": TAGS_AB, "Session ID": {"type": "text"}})
    schema_fake.handlers["notion-update-data-source"] = _raise(
        "Notion MCP rejected the request (400): Invalid select option, commas not allowed"
    )
    with pytest.raises(ValueError, match="rejected the tag options") as exc:
        rn._ensure_schema(["C"])
    assert notion_queue._is_permanent_error(str(exc.value))


def test_schema_transient_failure_is_retryable(schema_fake):
    from app.routes import notion as rn
    from app.utils import notion_queue

    schema_fake.handlers["notion-fetch"] = _db_fetch({"Tags": TAGS_AB, "Session ID": {"type": "text"}})
    schema_fake.handlers["notion-update-data-source"] = _raise("service_unavailable: Notion is unavailable")
    with pytest.raises(ValueError, match="Could not add tag options") as exc:
        rn._ensure_schema(["C"])
    assert not notion_queue._is_permanent_error(str(exc.value))


# ---------------------------------------------------------------------------
# Review 2 / 2: never SET the option list from an unreadable view
# ---------------------------------------------------------------------------


def _db_fetch_with_extra(schema_props, extra):
    state = json.dumps({"schema": schema_props})
    text = (
        f'<database url="https://www.notion.so/{DB_HEX}"><data-source url="collection://{DS_UUID}">'
        f"<data-source-state>{state}</data-source-state>\n{extra}\n</data-source></database>"
    )
    return _text_result({"metadata": {"type": "database"}, "title": "Meetings", "url": "x", "text": text})


@pytest.mark.parametrize(
    "tags_prop",
    [
        {"type": "multi_select"},  # no options key
        {"type": "multi_select", "options": None},
        {"type": "multi_select", "options": "A, B"},
        {"type": "multi_select", "options": [{"name": "A", "color": "blue"}, "B"]},  # one unreadable entry
        {"type": "multi_select", "options": [{"label": "A"}]},
    ],
)
def test_schema_unparseable_options_are_unknown_and_not_altered(schema_fake, tags_prop):
    from app.routes import notion as rn

    schema_fake.handlers["notion-fetch"] = _db_fetch({"Tags": tags_prop, "Session ID": {"type": "text"}})
    s = rn._ensure_schema(["A", "C"])
    assert _ddl(schema_fake) == []
    assert s["tags_exists"] is True and s["tag_options_known"] is False
    # Not cached as good enough: the next push re-reads the schema.
    assert rn._schema_satisfies(s, {"A"}) is False


def test_unknown_options_page_gets_only_known_tags(schema_fake):
    from app.routes import notion as rn

    tags_prop = {"type": "multi_select", "options": [{"name": "A", "color": "blue"}, 42]}
    schema_fake.handlers["notion-fetch"] = _db_fetch({"Tags": tags_prop, "Session ID": {"type": "text"}})
    s = rn._ensure_schema(["A", "C"])
    assert rn._writable_tags(["A", "C"], s) == ["A"]
    props = rn._page_properties("T", rn._writable_tags(["A", "C"], s), SID, s)
    assert json.loads(props["Tags"]) == ["A"]
    # None known: leave the page's tags alone rather than clearing them.
    assert rn._writable_tags(["C"], s) is None
    assert "Tags" not in rn._page_properties("T", None, SID, s)


@pytest.mark.parametrize(
    "extra",
    [
        '<sqlite-table>CREATE TABLE t ("Tags" TEXT -- multi_select, options: "A", "B")</sqlite-table>',
        '{"Tags": {"type": "multi_select", "options": [{"name": "A", "color": "blue"}]}}',
        "ALTER: \"Tags\" MULTI_SELECT('A':blue)",
    ],
)
def test_schema_empty_options_contradicted_by_text_not_altered(schema_fake, extra):
    from app.routes import notion as rn

    props = {"Tags": {"type": "multi_select", "options": []}, "Session ID": {"type": "text"}}
    schema_fake.handlers["notion-fetch"] = _db_fetch_with_extra(props, extra)
    s = rn._ensure_schema(["C"])
    assert _ddl(schema_fake) == []
    assert s["tag_options_known"] is False


def test_schema_genuinely_empty_options_are_added(schema_fake):
    from app.routes import notion as rn

    # Other columns' options in the state block mustn't count as Tags options.
    props = {
        "Tags": {"type": "multi_select", "options": []},
        "Status": {"type": "select", "options": [{"name": "Done", "color": "green"}]},
        "Session ID": {"type": "text"},
    }
    schema_fake.handlers["notion-fetch"] = _db_fetch(props)
    s = rn._ensure_schema(["C"])
    assert _ddl(schema_fake) == ["ALTER COLUMN \"Tags\" SET MULTI_SELECT('C':default)"]
    assert s["tag_options_known"] is True


def test_push_with_unknown_options_still_pushes_page(env, fake):
    from app.routes import notion as rn

    _setup_existing_page(env)  # recording tagged ["A"]
    fake.handlers["notion-fetch"] = _db_fetch({"Tags": {"type": "multi_select"}, "Session ID": {"type": "text"}})
    with patch.object(rn, "_ensure_schema", env.real.ensure_schema), patch.object(rn, "_schema_cache", None):
        out = rn.push_to_notion(SID, mode="properties")
    assert out["pushed"] is True
    assert fake.of("notion-update-data-source") == []
    props = fake.of("notion-update-page")[0]["properties"]
    assert "Tags" not in props and props["Session ID"] == SID
