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


@pytest.fixture
def env(flask_client, tmp_path):
    trn = tmp_path / "transcripts"
    trn.mkdir()
    with (
        patch("app.routes.notion.TRANSCRIPTS_DIR", trn),
        patch("app.state.notion.is_connected", return_value=True),
        patch("app.state.notion.get_database_id", return_value="db-id"),
        patch("app.routes.notion._ensure_tags_property_once"),
    ):
        yield types.SimpleNamespace(client=flask_client, trn=trn, root=tmp_path)


def _cfg():
    from app.utils.config import load_config

    return load_config()


def _write_transcript(trn, sid, data):
    (trn / f"{sid}.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------

TRAVERSAL = ["../victim/secret", "../../etc/passwd", "/etc/passwd", "a\\b", ".hidden", "a:b", "..", "a\x00b"]


@pytest.mark.parametrize("sid", TRAVERSAL)
def test_push_rejects_traversal_ids(env, sid):
    with patch("app.state.notion.mcp_call") as mcp:
        r = env.client.post("/notion/push", json={"session_id": sid})
    assert r.status_code == 400
    assert r.get_json() == {"error": "Invalid session_id"}
    mcp.assert_not_called()


@pytest.mark.parametrize("sid", [".hidden", "a:b", "a%5Cb", "..evil", "x" * 129])
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

    def fake_mcp(method, params=None):
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


def test_set_database_url_without_id_is_400(env):
    with patch("app.state.notion.mcp_call") as mcp:
        r = env.client.post("/notion/set-database-url", json={"url": "https://www.notion.so/myteam/No-Id"})
    assert r.status_code == 400
    mcp.assert_not_called()
