"""Tags as single add and remove operations, with rules (app/utils/tags.py).

The window used to save a recording's whole tag list, so a stale window could
wipe a tag an AI tool had just added. Tags now change one at a time.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from app.utils import changes, tags
from app.utils.config import load_config, save_config

PROJECT_ROOT = Path(__file__).parent.parent
SID = "1715000000"


@pytest.fixture
def env(flask_client, tmp_path):
    trn = tmp_path / "transcripts"
    trn.mkdir()
    # The tag routes only change recordings that exist (app/routes/recordings.py recording_exists).
    rec = tmp_path / "recordings"
    rec.mkdir()
    for sid in (SID, "1715000001"):
        (rec / f"{sid}.ogg").write_bytes(b"\0" * 100)
    with (
        patch("app.utils.tags.TRANSCRIPTS_DIR", trn),
        patch("app.routes.recordings.TRANSCRIPTS_DIR", trn),
        patch("app.routes.notion.TRANSCRIPTS_DIR", trn),
        patch("app.routes.recordings.get_all_recordings_dirs", return_value=[rec]),
    ):
        yield type("Env", (), {"client": flask_client, "trn": trn})


def _transcript(env, sid=SID, **data):
    path = env.trn / f"{sid}.json"
    path.write_text(json.dumps({"text": "hello", **data}))
    return path


def _rec_tags(sid=SID):
    return load_config().get("recording_tags", {}).get(sid, [])


# ---------------------------------------------------------------------------
# normalise_tag
# ---------------------------------------------------------------------------


class TestNormalise:
    def test_trims(self):
        assert tags.normalise_tag("  Work  ", []) == "Work"

    def test_existing_spelling_wins(self):
        assert tags.normalise_tag("group meeting", ["Group Meeting", "x"]) == "Group Meeting"

    @pytest.mark.parametrize("bad", ["", "   ", "a" * 41, "bad\ttag", "bad\nline", "x\x00", "x\x7f", 3, None])
    def test_rejects(self, bad):
        with pytest.raises(ValueError):
            tags.normalise_tag(bad, [])

    def test_forty_characters_is_fine(self):
        assert tags.normalise_tag("a" * 40, []) == "a" * 40


# ---------------------------------------------------------------------------
# add_tags / remove_tags
# ---------------------------------------------------------------------------


def test_add_is_case_insensitive_and_keeps_existing_spelling(env):
    save_config({"recording_tags": {SID: ["Work"]}, "custom_tags": ["Project X"]})
    assert tags.add_tags(SID, ["work", "project x", "1:1", "group MEETING"]) == [
        "Work",
        "Project X",
        "1:1",
        "Group Meeting",
    ]
    # Idempotent: the same call again changes nothing.
    assert tags.add_tags(SID, ["WORK"]) == ["Work", "Project X", "1:1", "Group Meeting"]
    assert load_config()["custom_tags"] == ["Project X"]


def test_add_dedupes_within_a_call(env):
    assert tags.add_tags(SID, ["New", "new", " NEW "]) == ["New"]


def test_limits(env):
    save_config({"recording_tags": {SID: [f"t{i}" for i in range(20)]}})
    with pytest.raises(ValueError, match="20"):
        tags.add_tags(SID, ["one more"])
    assert len(_rec_tags()) == 20
    # A tag it already has is still fine at the limit.
    assert len(tags.add_tags(SID, ["T3"])) == 20

    other = "1715000001"
    with pytest.raises(ValueError, match="40"):
        tags.add_tags(other, ["a" * 41])
    with pytest.raises(ValueError, match="20"):
        tags.add_tags(other, [f"n{i}" for i in range(21)])
    with pytest.raises(ValueError, match="20"):
        tags.remove_tags(other, [f"n{i}" for i in range(21)])
    assert _rec_tags(other) == []
    # Nothing half-applied: a bad tag in the call rejects the whole call.
    with pytest.raises(ValueError):
        tags.add_tags(other, ["good", ""])
    assert _rec_tags(other) == []


def test_new_tag_joins_custom_tags(env):
    save_config({"custom_tags": ["Old"]})
    tags.add_tags(SID, ["Brand new", "Memo", "old"])
    # Defaults and existing tags (any case) aren't duplicated.
    assert load_config()["custom_tags"] == ["Old", "Brand new"]


def test_remove_missing_is_noop(env):
    save_config({"recording_tags": {SID: ["A", "B"]}})
    before = changes.current()
    assert tags.remove_tags(SID, ["Nope"]) == ["A", "B"]
    assert changes.current() == before
    assert tags.remove_tags(SID, ["a"]) == ["B"]
    assert changes.current() == before + 1


def test_remove_allows_legacy_long_tags(env):
    long_tag = "x" * 60
    save_config({"recording_tags": {SID: [long_tag, "B"]}})
    assert tags.remove_tags(SID, [long_tag]) == ["B"]


def test_writes_transcript_json_too(env):
    path = _transcript(env, tags=["Old"])
    save_config({"recording_tags": {SID: ["Old"]}})
    tags.add_tags(SID, ["New"])
    assert json.loads(path.read_text())["tags"] == ["Old", "New"]
    assert json.loads(path.read_text())["text"] == "hello"
    tags.remove_tags(SID, ["old"])
    assert json.loads(path.read_text())["tags"] == ["New"]


def test_change_bumps_counter_and_noop_does_not(env):
    before = changes.current()
    tags.add_tags(SID, ["A"])
    assert changes.current() == before + 1
    tags.add_tags(SID, ["a"])
    assert changes.current() == before + 1


def test_enqueues_notion_sync_for_linked_page(env):
    save_config({"notion_page_urls": {SID: "https://www.notion.so/page-0123456789abcdef0123456789abcdef"}})
    with (
        patch("app.state.notion.is_connected", return_value=True),
        patch("app.utils.notion_queue.enqueue") as enqueue,
    ):
        tags.add_tags(SID, ["A"])
        tags.add_tags(SID, ["A"])  # no change, no sync
        tags.remove_tags(SID, ["A"])
    assert [c.args[0] for c in enqueue.call_args_list] == [SID, SID]


def test_no_notion_sync_without_page(env):
    with (
        patch("app.state.notion.is_connected", return_value=True),
        patch("app.utils.notion_queue.enqueue") as enqueue,
    ):
        tags.add_tags(SID, ["A"])
    enqueue.assert_not_called()


def test_rejects_bad_session_id(env):
    with pytest.raises(ValueError):
        tags.add_tags("../../victim", ["A"])


def test_stale_window_cannot_wipe_ai_tag(env):
    """The window opened with ["y"]; an AI tool then adds "x"; the stale window
    removes "y" through the UI path. "x" must survive."""
    save_config({"recording_tags": {SID: ["y"]}})
    tags.add_tags(SID, ["x"])  # the AI tool
    r = env.client.post("/recordings/tags/remove", json={"session_ids": [SID], "tags": ["y"]})
    assert r.status_code == 200
    assert r.get_json()["tags"] == {SID: ["x"]}
    assert _rec_tags() == ["x"]


# ---------------------------------------------------------------------------
# all_tags
# ---------------------------------------------------------------------------


def test_all_tags_counts(env):
    save_config(
        {
            "custom_tags": ["Unused", "Work"],
            "recording_tags": {"1": ["work", "Memo"], "2": ["Work", "Spoken"], "3": ["Spoken"], "gone": ["Work"]},
            "deleted_ids": ["gone"],
        }
    )
    got = {t["tag"]: t["recordings"] for t in tags.all_tags()}
    assert got["Work"] == 2
    assert got["Memo"] == 1
    assert got["Spoken"] == 2
    assert got["Unused"] == 0
    assert got["1:1"] == 0  # the picker's default tags are listed too
    assert "work" not in got


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_add_route_bulk(env):
    save_config({"recording_tags": {"1715000001": ["Keep"]}})
    r = env.client.post("/recordings/tags/add", json={"session_ids": [SID, "1715000001"], "tags": ["Client"]})
    assert r.status_code == 200
    assert r.get_json()["tags"] == {SID: ["Client"], "1715000001": ["Keep", "Client"]}


def test_add_route_partial_failure(env):
    full = "1715000001"
    save_config({"recording_tags": {full: [f"t{i}" for i in range(20)]}})
    r = env.client.post("/recordings/tags/add", json={"session_ids": [SID, full], "tags": ["New"]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["tags"] == {SID: ["New"]}
    assert "20" in body["errors"][full]
    # All failed: 400 with the reason.
    r = env.client.post("/recordings/tags/add", json={"session_ids": [full], "tags": ["New"]})
    assert r.status_code == 400
    assert "20" in r.get_json()["error"]


@pytest.mark.parametrize(
    "body",
    [
        {"session_ids": [SID], "tags": "A"},
        {"session_ids": [SID], "tags": [""]},
        {"session_ids": [SID], "tags": [1]},
        {"session_ids": SID, "tags": ["A"]},
        {"session_ids": ["../x"], "tags": ["A"]},
        {"session_ids": [], "tags": ["A"]},
        {"session_ids": [SID], "tags": []},
    ],
)
def test_routes_reject_bad_input(env, body):
    for url in ("/recordings/tags/add", "/recordings/tags/remove"):
        r = env.client.post(url, json=body)
        assert r.status_code == 400, (url, body)
        assert r.get_json()["error"]
    assert _rec_tags() == []


def test_add_route_rejects_long_tag(env):
    r = env.client.post("/recordings/tags/add", json={"session_ids": [SID], "tags": ["a" * 41]})
    assert r.status_code == 400
    assert "40" in r.get_json()["error"]
    assert _rec_tags() == []


# ---------------------------------------------------------------------------
# The window only adds and removes single tags
# ---------------------------------------------------------------------------


def test_ui_uses_add_remove_not_whole_list():
    bridge = (PROJECT_ROOT / "app" / "tauri-bridge.js").read_text()
    m = re.search(r"\b(\w+)\s*:\s*\([^)]*\)\s*=>\s*apiCall\('POST',\s*'/recordings/tag'", bridge)
    whole_list_method = m.group(1) if m else "tagRecording"
    assert re.search(r"\baddTags\s*:", bridge) and "'/recordings/tags/add'" in bridge
    assert re.search(r"\bremoveTags\s*:", bridge) and "'/recordings/tags/remove'" in bridge

    ui = {p.name: p.read_text() for p in (PROJECT_ROOT / "app" / "ui").glob("*.js")}
    ui["renderer.js"] = (PROJECT_ROOT / "app" / "renderer.js").read_text()
    for name, src in ui.items():
        assert not re.search(rf"\.{whole_list_method}\(", src), f"{name} still saves a whole tag list"
        assert "custom_tags" not in src, f"{name} still saves the whole tag picker list"
    assert re.search(r"\.addTags\(", ui["transcript.js"])
    assert re.search(r"\.removeTags\(", ui["transcript.js"])
    assert re.search(r"\.addTags\(", ui["recordings.js"])
    assert re.search(r"\.removeTags\(", ui["recordings.js"])


# ---------------------------------------------------------------------------
# The transcript's tag picker works from the recording's tags at click time
# ---------------------------------------------------------------------------

_PICKER_JS = r"""
const app = r('app');
app.settings = { available_tags: ['Work', 'Home', 'Ideas'] };
Object.assign(app, { currentView: 'transcript', currentSessionId: 's1' });
let tagsNow = ['Work'];  // the server's tags
ctx.window.plaud = {
  addTags: async (ids, tags) => { calls.push(['add', tags]); tagsNow = [...new Set([...tagsNow, ...tags])]; return { tags: { s1: tagsNow } }; },
  removeTags: async (ids, tags) => { calls.push(['remove', tags]); tagsNow = tagsNow.filter((t) => !tags.includes(t)); return { tags: { s1: tagsNow } }; },
};
const picker = r("$('#transcript-tags')");
// The elements the markup holds, as the browser would build them
const byId = {};
document.getElementById = (id) => (picker.innerHTML.includes(`id="${id}"`) ? (byId[id] = new El()) : null);
// (the same elements until the markup is set again, so their click handlers stay on them)
let built = null;
Object.defineProperty(picker, 'innerHTML', {
  get() { return this._html; },
  set(v) { this._html = String(v); built = null; },
});
picker.querySelectorAll = () => {
  if (!built) {
    built = [...picker.innerHTML.matchAll(/<button class="tag-option ([^"]*)" data-tag="([^"]*)"/g)].map((m) => {
      const b = new El('button');
      b.dataset.tag = m[2];
      m[1].split(/\s+/).filter(Boolean).forEach((c) => b.classList.add(c));
      return b;
    });
  }
  return built;
};
const click = (tag) => fire(picker.querySelectorAll().find((b) => b.dataset.tag === tag), 'click');
const shown = () => picker.querySelectorAll().map((b) => [b.dataset.tag, b.classList.contains('active')]);
const reload = (tags) => { app.recordings = [{ session_id: 's1', title: 'Chat', tags: [...tags] }]; };
const out = {};

(async () => {
  reload(['Work']);
  r('_txMountTagPicker')('s1', app.recordings[0]);
  out.collapsed = shown();
  await fire(byId['tag-picker-open'], 'click');
  out.open = shown();

  // A click adds, and the server's reply lands on the recording in app.recordings
  await click('Home');
  out.addCalls = calls.splice(0);
  out.afterAdd = shown();
  out.liveAfterAdd = [...app.recordings[0].tags];

  // An AI tool removes Work and Home and adds Ideas; the list reloads (new objects)
  // while the picker is open, so it isn't drawn again
  tagsNow = ['Ideas'];
  reload(tagsNow);
  // The user clicks Work, still shown as on, to remove it: the AI tool already did,
  // so nothing is sent (never an add) and the picker shows the recording's real tags
  await click('Work');
  out.staleRemoveCalls = calls.splice(0);
  out.afterStaleRemove = shown();
  // Likewise a click on an off tag the AI tool already added
  tagsNow = ['Ideas', 'Home'];
  reload(tagsNow);
  await click('Ideas');  // shown on: removes it (the server has it too)
  out.removeCalls = calls.splice(0);
  out.liveAfterRemove = [...app.recordings[0].tags];
  out.afterRemove = shown();
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_tag_picker_decides_from_the_recordings_current_tags():
    from tests.test_speaker_names_ui import _run

    out = _run(_PICKER_JS, ["utils.js", "recordings.js", "transcript.js"], {})
    assert out["collapsed"] == [["Work", True]]
    assert out["open"] == [["Work", True], ["Home", False], ["Ideas", False]]
    assert out["addCalls"] == [["add", ["Home"]]]
    assert out["afterAdd"] == [["Work", True], ["Home", True], ["Ideas", False]]
    assert out["liveAfterAdd"] == ["Work", "Home"]
    # Work was already gone: no request, least of all an add
    assert out["staleRemoveCalls"] == []
    assert out["afterStaleRemove"] == [["Work", False], ["Home", False], ["Ideas", True]]
    assert out["removeCalls"] == [["remove", ["Ideas"]]]
    assert out["liveAfterRemove"] == ["Home"]
    assert out["afterRemove"] == [["Work", False], ["Home", True], ["Ideas", False]]
