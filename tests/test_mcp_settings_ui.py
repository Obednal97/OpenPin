"""Settings › General › "AI tools (MCP)" and the library change poll (app/ui/mcp-settings.js).

Static checks read the HTML and JS as text, like tests/test_settings_ui.py. The
section's render and the change poll run in node against a small fake DOM.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
APP = PROJECT_ROOT / "app"
HTML = (APP / "index.html").read_text()
JS = APP / "ui" / "mcp-settings.js"
TOP_LEVEL = re.compile(
    r"^(?:async\s+)?(?:function\s+([A-Za-z_$][\w$]*)|(?:const|let|var|class)\s+([A-Za-z_$][\w$]*))", re.M
)

SWITCH = "Let AI tools on this Mac use Open Pin"
HINT = (
    "Claude and other AI tools on this Mac can read your recordings' details, transcripts, summaries and "
    "coaching, and can name speakers and change tags. Only while Open Pin is open. Your API keys, voice and "
    "audio files are never shared."
)
NOT_STARTED = "AI tool access couldn't start. Check the log, or turn it off and on again."


def _tab_panel(name):
    start = HTML.index(f'data-tab="{name}" role="tabpanel"')
    rest = HTML[start:]
    nxt = rest.find('role="tabpanel"', 40)
    return rest if nxt == -1 else rest[:nxt]


def _section():
    general = _tab_panel("general")
    start = general.index('id="mcp-section"')
    return general[start : general.index("<h3>About</h3>")]


def _flat(text):
    return re.sub(r"\s+", " ", text)


class TestMarkup:
    def test_section_sits_in_general_before_about(self):
        general = _tab_panel("general")
        assert re.findall(r"<h3>([^<]+)</h3>", general) == ["System", "Appearance", "AI tools (MCP)", "About"]

    def test_switch_and_hint_copy(self):
        section = _flat(_section())
        assert f"<span>{SWITCH}</span>" in section
        assert HINT in section
        assert NOT_STARTED in section
        assert re.search(r'<input type="checkbox" id="mcp-enabled-toggle" class="toggle-input"', section)

    def test_buttons_start_disabled(self):
        section = _section()
        for btn, label in (
            ("mcp-desktop-connect-btn", "Connect to Claude Desktop"),
            ("mcp-code-connect-btn", "Connect to Claude Code"),
        ):
            m = re.search(rf'<button id="{btn}"[^>]*>([^<]+)</button>', section)
            assert m, btn
            assert " disabled" in m.group(0)
            assert m.group(1) == label

    def test_restart_hints(self):
        section = _flat(_section())
        assert "Quit and reopen Claude Desktop to start using Open Pin." in section

    def test_no_em_dashes(self):
        assert "—" not in _section()
        assert "—" not in JS.read_text()

    def test_script_and_css_wired(self):
        scripts = re.findall(r'<script src="([^"]+)"></script>', HTML)
        assert scripts.index("ui/settings.js") < scripts.index("ui/mcp-settings.js") < scripts.index("renderer.js")
        imports = re.findall(r"@import '([^']+)';", (APP / "styles.css").read_text())
        assert "css/mcp-settings.css" in imports
        assert (APP / "css" / "mcp-settings.css").exists()

    def test_tick_rows_are_big_click_targets(self):
        """The label (tick box plus file name) stretches across the row up to its badge
        and is at least 32px tall, so a click anywhere on the row ticks it."""
        css = (APP / "css" / "mcp-settings.css").read_text()
        rule = re.search(r"\.mcp-client-row\.mcp-tickable \.mcp-code-label \{([^}]*)\}", css)
        assert rule, "tickable rows must stretch their label"
        assert "flex: 1" in rule.group(1) and "min-height: 32px" in rule.group(1)
        assert re.search(r"\.mcp-code-label input \{[^}]*width: 16px", css)


class TestWiring:
    SRC = JS.read_text()

    def test_globals_are_prefixed_and_unique(self):
        mine = {m.group(1) or m.group(2) for m in TOP_LEVEL.finditer(self.SRC)}
        public = {"renderMcpSettings", "initMcpSettingsEvents", "startMcpChangePoll"}
        assert {n for n in mine if not n.lower().startswith("_mcp")} == public
        others = [p for p in (APP / "ui").glob("*.js") if p.name != JS.name] + [APP / "renderer.js"]
        for path in others:
            theirs = {m.group(1) or m.group(2) for m in TOP_LEVEL.finditer(path.read_text())}
            assert not (mine & theirs), path.name

    def test_settings_renders_and_binds_the_section(self):
        src = (APP / "ui" / "settings.js").read_text()
        render = src[src.index("function renderSettings()") :]
        assert "renderMcpSettings();" in render[: render.index("\n}\n")]
        assert re.search(r"^initMcpSettingsEvents\(\);$", self.SRC, re.M)

    def test_renderer_starts_the_poll(self):
        src = (APP / "renderer.js").read_text()
        init = src[src.index("async function init()") :]
        init = init[: init.index("\n}\n")]
        assert (
            init.index("await loadRecordings();") < init.index("startMcpChangePoll();") < init.index("if (isFirstRun)")
        )

    def test_bridge_methods(self):
        bridge = (APP / "tauri-bridge.js").read_text()
        assert "libraryChanges: () => apiCall('GET', '/changes')" in bridge
        assert "mcpSetupStatus: () => apiCall('GET', '/mcp/setup/status')" in bridge
        assert "mcpConnect: (kind, path) => apiCall('POST', '/mcp/setup/connect', { kind, path })" in bridge
        assert "mcpDisconnect: (kind, path) => apiCall('POST', '/mcp/setup/disconnect', { kind, path })" in bridge

    def test_poll_every_five_seconds(self):
        assert "const _MCP_CHANGES_POLL_MS = 5000;" in self.SRC

    def test_switch_off_never_touches_claude_files(self):
        body = self.SRC[self.SRC.index("async function _mcpSetToggle(") :]
        body = body[: body.index("\n}\n")]
        assert "mcpDisconnect" not in body
        assert "saveSettings({ mcp_enabled: on })" in body


_RUN_JS = r"""
const fs = require('fs'); const vm = require('vm');
function el(id) {
  const cls = new Set();
  const listeners = {};
  return {
    id, disabled: false, checked: false, textContent: '', innerHTML: '', style: {}, dataset: {}, paused: true,
    classList: { add: (...c) => c.forEach((x) => cls.add(x)), remove: (...c) => c.forEach((x) => cls.delete(x)),
      toggle: (c, on) => { if (on === undefined ? !cls.has(c) : on) cls.add(c); else cls.delete(c); },
      contains: (c) => cls.has(c) },
    addEventListener: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); },
    listeners, setAttribute() {}, appendChild() {}, querySelector: () => null, querySelectorAll: () => [], contains: () => false,
  };
}
const els = {};
const byId = (id) => (els[id] = els[id] || el(id));
let focused = true;
const ctx = {
  console, setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
  window: { plaud: {}, addEventListener() {} },
  document: { getElementById: byId, querySelector: () => null, querySelectorAll: () => [], createElement: () => el(''),
    body: el('body'), hasFocus: () => focused, hidden: false, activeElement: null, addEventListener() {} },
};
ctx.window.document = ctx.document;
vm.createContext(ctx);
for (const f of process.argv.slice(1, 3)) vm.runInContext(fs.readFileSync(f, 'utf8'), ctx);
const r = (code) => vm.runInContext(code, ctx);
const out = {};

const status1 = { desktop: { path: '~/Library/Application Support/Claude/claude_desktop_config.json', exists: false, connected: false, stale: false },
  code: [{ path: '~/.claude-personal/.claude.json', connected: false, stale: false, error: null }] };
const status2 = { desktop: { path: '~/Library/Application Support/Claude/claude_desktop_config.json', exists: true, connected: true, stale: false },
  code: [{ path: '~/.claude.json', connected: false, stale: false, error: null },
         { path: '~/.claude-<b>/.claude.json', connected: true, stale: false, error: null },
         { path: '~/.claude-bad/.claude.json', connected: false, stale: false, error: "~/.claude-bad/.claude.json isn't valid JSON" }] };

function snap() {
  return { toggle: byId('mcp-enabled-toggle').checked, error: !byId('mcp-error').classList.contains('hidden'),
    desktopBtn: byId('mcp-desktop-connect-btn').disabled, codeBtn: byId('mcp-code-connect-btn').disabled,
    desktopHtml: byId('mcp-desktop-status').innerHTML, codeHtml: byId('mcp-code-list').innerHTML };
}

(async () => {
  // --- Render ---
  let status = status1;
  ctx.window.plaud.mcpSetupStatus = async () => status;
  r('app').settings = { mcp_enabled: false, mcp_active: false };
  await r('renderMcpSettings')();
  out.off = snap();
  r('app').settings = { mcp_enabled: true, mcp_active: true };
  await r('renderMcpSettings')();
  out.on = snap();
  r('app').settings = { mcp_enabled: true, mcp_active: false };
  await r('renderMcpSettings')();
  out.broken = snap();
  status = status2;
  r('app').settings = { mcp_enabled: true, mcp_active: true };
  await r('renderMcpSettings')();
  out.many = snap();

  // --- Change poll ---
  let counter = 4;
  let calls = { changes: 0, load: 0, open: [], redraw: [] };
  // The library on the backend; loadRecordings() copies it into app, as the real one does.
  const lib = { tags: ['work'], names: {}, summary: 'Speaker 1 spoke.' };
  const app = r('app');
  const reload = () => {
    app.recordings = [{ session_id: 'S1', tags: [...lib.tags] }];
    app.transcripts = { S1: { session_id: 'S1', segments: [{ speaker: 'Speaker 1', text: 'Hi' }], text: 'Hi',
      summary: lib.summary, speaker_names: { ...lib.names } } };
  };
  reload();
  ctx.window.plaud.libraryChanges = async () => { calls.changes++; return { counter }; };
  r('globalThis').loadRecordings = async () => { calls.load++; reload(); };
  // The audio player: openTranscript() reloads it, which rewinds and pauses it.
  const audio = { currentTime: 0, paused: true, readyState: 4, played: 0, listeners: {},
    getAttribute: (a) => (a === 'src' ? 'http://x/S1.ogg' : null),
    play() { this.paused = false; this.played++; return Promise.resolve(); },
    addEventListener(ev, fn) { this.listeners[ev] = fn; } };
  r('dom').audioEl = audio;
  r('globalThis').openTranscript = (sid) => {
    calls.open.push(sid);
    audio.currentTime = 0; audio.paused = true; audio.readyState = 0;
  };
  app.currentView = 'transcript';
  app.currentSessionId = 'S1';
  const tick = r('_mcpChangesTick');
  const step = async () => { await tick(); return JSON.parse(JSON.stringify(calls)); };
  out.poll = [];
  out.poll.push(await step());          // 0: first read: baseline only
  out.poll.push(await step());          // 1: no change
  counter = 5;
  out.poll.push(await step());          // 2: counter moved but this recording didn't change (the window's own edit)
  counter = 6; lib.tags.push('ai-tag');
  out.poll.push(await step());          // 3: a tag changed: reload + redraw
  focused = false; counter = 7;
  out.poll.push(await step());          // 4: window not focused: no request
  focused = true;
  // An editor is open in the transcript view: reload, but don't redraw
  const view = byId('transcript-view');
  view.querySelector = (sel) => (sel.includes('.summary-editor') ? {} : null);
  lib.names = { 'Speaker 1': 'Sarah' };
  out.poll.push(await step());          // 5
  out.poll.push(await step());          // 6: still editing: still waiting
  view.querySelector = () => null;
  out.poll.push(await step());          // 7: editor closed: the pending redraw happens
  counter = 8; lib.summary = 'Speaker 1 spoke at length.';
  app.currentView = 'recordings';
  out.poll.push(await step());          // 8: list view: reload only
  // Task 7's speaker-name editor (ui/transcript.js) is open: wait for it too
  app.currentView = 'transcript';
  let naming = true;
  r('globalThis').speakerNameEditorOpen = () => naming;
  counter = 9; lib.names = {};
  out.poll.push(await step());          // 9
  naming = false;
  out.poll.push(await step());          // 10
  // The player keeps its place: paused at 42 s stays paused at 42 s
  audio.currentTime = 42; audio.paused = true; audio.readyState = 4; audio.played = 0;
  counter = 10; lib.tags = ['other'];
  await step();
  out.audioPaused = { before: [audio.currentTime, audio.paused, audio.played] };
  audio.listeners.loadedmetadata && audio.listeners.loadedmetadata();
  out.audioPaused.after = [audio.currentTime, audio.paused, audio.played];
  // Playing at 90 s: seeks back and plays again once the new source has loaded
  audio.currentTime = 90; audio.paused = false; audio.readyState = 4; audio.played = 0; audio.listeners = {};
  counter = 11; lib.tags = ['third'];
  await step();
  audio.listeners.loadedmetadata && audio.listeners.loadedmetadata();
  out.audioPlaying = [audio.currentTime, audio.paused, audio.played];
  // Task 7's redrawOpenTranscript() draws it when it exists
  r('globalThis').redrawOpenTranscript = (sid) => { calls.redraw.push(sid); };
  counter = 12; lib.tags = ['fourth'];
  out.poll.push(await step());          // 11
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def ui():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    proc = subprocess.run(
        [node, "-e", _RUN_JS, str(APP / "ui" / "utils.js"), str(JS)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestRender:
    def test_off_disables_buttons(self, ui):
        s = ui["off"]
        assert s["toggle"] is False
        assert s["desktopBtn"] is True
        assert s["codeBtn"] is True
        assert s["error"] is False

    def test_on_enables_buttons_and_pre_ticks_a_single_file(self, ui):
        s = ui["on"]
        assert s["toggle"] is True
        assert s["desktopBtn"] is False
        assert s["codeBtn"] is False
        assert re.search(
            r'<input type="checkbox"[^>]*data-mcp-path="~/.claude-personal/.claude.json"[^>]*checked', s["codeHtml"]
        )
        assert "Not connected" in s["desktopHtml"]
        assert "Connecting creates it" in s["desktopHtml"]

    def test_enabled_but_not_active_shows_error(self, ui):
        assert ui["broken"]["error"] is True
        assert ui["on"]["error"] is False

    def test_many_files(self, ui):
        s = ui["many"]
        html = s["codeHtml"]
        # Several files: nothing ticked, so nothing to connect yet
        assert "checked" not in html
        assert s["codeBtn"] is True
        # Connected file: a Disconnect link, no tick box; names are escaped
        assert "~/.claude-&lt;b&gt;/.claude.json" in html
        assert 'data-mcp-disconnect="code"' in html
        assert "<b>" not in html
        # Unreadable file: its message, no tick box
        assert "isn&#39;t valid JSON" in html or "isn't valid JSON" in html
        assert html.count('type="checkbox"') == 1
        # Only the row with a tick box is a whole-row click target
        assert html.count("mcp-client-row mcp-tickable") == 1
        # Desktop already connected: Disconnect offered, Connect not needed
        assert "Connected" in s["desktopHtml"]
        assert 'data-mcp-disconnect="desktop"' in s["desktopHtml"]
        assert s["desktopBtn"] is True


class TestChangePoll:
    @staticmethod
    def _c(changes, load, open_, redraw=()):
        return {"changes": changes, "load": load, "open": list(open_), "redraw": list(redraw)}

    def test_first_read_is_a_baseline(self, ui):
        assert ui["poll"][0] == self._c(1, 0, [])
        assert ui["poll"][1] == self._c(2, 0, [])

    def test_own_edit_reloads_without_redrawing(self, ui):
        # The window's own tag save moves the counter too: same data, no redraw
        assert ui["poll"][2] == self._c(3, 1, [])

    def test_change_reloads_and_redraws(self, ui):
        assert ui["poll"][3] == self._c(4, 2, ["S1"])

    def test_unfocused_window_does_not_poll(self, ui):
        assert ui["poll"][4]["changes"] == 4

    def test_open_editor_defers_the_redraw(self, ui):
        assert ui["poll"][5] == self._c(5, 3, ["S1"])
        assert ui["poll"][6] == self._c(6, 3, ["S1"])
        assert ui["poll"][7] == self._c(7, 3, ["S1", "S1"])

    def test_list_view_only_reloads(self, ui):
        assert ui["poll"][8] == self._c(8, 4, ["S1", "S1"])

    def test_speaker_name_editor_defers_the_redraw(self, ui):
        assert ui["poll"][9] == self._c(9, 5, ["S1", "S1"])
        assert ui["poll"][10] == self._c(10, 5, ["S1", "S1", "S1"])

    def test_paused_player_keeps_its_place(self, ui):
        assert ui["audioPaused"]["after"] == [42, True, 0]

    def test_playing_player_seeks_back_and_plays_on(self, ui):
        assert ui["audioPlaying"] == [90, False, 1]

    def test_uses_redraw_open_transcript_when_there(self, ui):
        last = ui["poll"][11]
        assert last["redraw"] == ["S1"]
        assert last["open"] == ui["poll"][10]["open"] + ["S1", "S1"]  # the two audio redraws, not this one
