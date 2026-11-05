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
  let calls = { changes: 0, load: 0, open: [] };
  ctx.window.plaud.libraryChanges = async () => { calls.changes++; return { counter }; };
  r('globalThis').loadRecordings = async () => { calls.load++; };
  r('globalThis').openTranscript = (sid) => { calls.open.push(sid); };
  r('app').currentView = 'transcript';
  r('app').currentSessionId = 'S1';
  const tick = r('_mcpChangesTick');
  const step = async () => { await tick(); return JSON.parse(JSON.stringify(calls)); };
  out.poll = [];
  out.poll.push(await step());          // first read: baseline only
  out.poll.push(await step());          // no change
  counter = 5;
  out.poll.push(await step());          // change: reload + re-render
  focused = false; counter = 6;
  out.poll.push(await step());          // window not focused: no request
  focused = true;
  // An editor is open in the transcript view: reload, but don't re-render
  const view = byId('transcript-view');
  view.querySelector = (sel) => (sel.includes('.summary-editor') ? {} : null);
  out.poll.push(await step());
  out.poll.push(await step());          // still editing: still waiting
  view.querySelector = () => null;
  out.poll.push(await step());          // editor closed: the pending re-render happens
  counter = 7;
  r('app').currentView = 'recordings';
  out.poll.push(await step());          // list view: reload only
  // Task 7's speaker-name editor (ui/transcript.js) is open: wait for it too
  r('app').currentView = 'transcript';
  let naming = true;
  r('globalThis').speakerNameEditorOpen = () => naming;
  counter = 8;
  out.poll.push(await step());
  naming = false;
  out.poll.push(await step());
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
        # Desktop already connected: Disconnect offered, Connect not needed
        assert "Connected" in s["desktopHtml"]
        assert 'data-mcp-disconnect="desktop"' in s["desktopHtml"]
        assert s["desktopBtn"] is True


class TestChangePoll:
    def test_first_read_is_a_baseline(self, ui):
        assert ui["poll"][0] == {"changes": 1, "load": 0, "open": []}
        assert ui["poll"][1] == {"changes": 2, "load": 0, "open": []}

    def test_change_reloads_and_rerenders(self, ui):
        assert ui["poll"][2] == {"changes": 3, "load": 1, "open": ["S1"]}

    def test_unfocused_window_does_not_poll(self, ui):
        assert ui["poll"][3]["changes"] == 3

    def test_open_editor_defers_the_rerender(self, ui):
        assert ui["poll"][4] == {"changes": 4, "load": 2, "open": ["S1"]}
        assert ui["poll"][5] == {"changes": 5, "load": 2, "open": ["S1"]}
        assert ui["poll"][6] == {"changes": 6, "load": 2, "open": ["S1", "S1"]}

    def test_list_view_only_reloads(self, ui):
        assert ui["poll"][7] == {"changes": 7, "load": 3, "open": ["S1", "S1"]}

    def test_speaker_name_editor_defers_the_rerender(self, ui):
        assert ui["poll"][8] == {"changes": 8, "load": 4, "open": ["S1", "S1"]}
        assert ui["poll"][9] == {"changes": 9, "load": 4, "open": ["S1", "S1", "S1"]}
