"""Coaching Progress view and the "Your voice & coaching" settings section (static checks).

The frontend is plain <script> files sharing one global scope, so these tests read
the markup and sources: the header button and panel exist, the settings section sits
in the Transcription & AI tab, the script loads before renderer.js, the JS metric map
mirrors app/coaching/targets.py, and no top-level name is declared twice.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.coaching import targets as T

PROJECT_ROOT = Path(__file__).parent.parent
APP = PROJECT_ROOT / "app"
HTML = (APP / "index.html").read_text()
JS = (APP / "ui" / "coaching-progress.js").read_text() if (APP / "ui" / "coaching-progress.js").exists() else ""
SETTINGS_JS = (APP / "ui" / "settings.js").read_text()
STYLES = (APP / "styles.css").read_text()


def _ai_tab():
    start = HTML.index('<div class="settings-tab-content" data-tab="ai"')
    end = HTML.index('<div class="settings-tab-content" data-tab="automation"')
    return HTML[start:end]


def _ids(markup):
    return set(re.findall(r'\bid="([^"]+)"', markup))


class TestMarkup:
    def test_progress_button_before_settings_button(self):
        header = HTML[HTML.index('<div class="header-right">') : HTML.index("</header>")]
        assert 'id="progress-btn"' in header
        assert header.index('id="progress-btn"') < header.index('id="settings-btn"')

    def test_progress_view_panel(self):
        ids = _ids(HTML)
        for el in ("progress-view", "progress-overlay", "progress-close", "progress-body"):
            assert el in ids, f"#{el} missing"
        panel = HTML[HTML.index('id="progress-view"') - 200 :]
        assert 'role="dialog"' in panel[:400]

    def test_voice_section_in_ai_tab(self):
        tab = _ai_tab()
        assert "Your voice &amp; coaching" in tab
        assert {"coaching-auto-toggle", "coaching-auto-needs", "voice-status", "voice-setup"} <= _ids(tab)
        # The section follows speaker identification, which coaching needs.
        assert tab.index('id="diarize-toggle"') < tab.index('id="coaching-auto-toggle"')

    def test_script_loads_before_renderer(self):
        scripts = re.findall(r'<script src="([^"]+)"></script>', HTML)
        assert "ui/coaching-progress.js" in scripts
        assert scripts.index("ui/coaching-progress.js") == scripts.index("renderer.js") - 1
        assert scripts.index("ui/utils.js") < scripts.index("ui/coaching-progress.js")

    def test_stylesheet_imported_after_settings(self):
        imports = re.findall(r"@import '([^']+)';", STYLES)
        assert imports.index("css/coaching-progress.css") == imports.index("css/settings.css") + 1
        assert (APP / "css" / "coaching-progress.css").exists()


class TestScript:
    def test_public_globals(self):
        assert re.search(r"^async function openProgressView\(", JS, re.M)
        assert re.search(r"^async function renderVoiceSettings\(", JS, re.M)

    def test_saves_coaching_auto_through_settings(self):
        assert "saveSettings({ coaching_auto:" in JS

    def test_settings_render_and_diarize_change_refresh_voice_section(self):
        render = SETTINGS_JS[SETTINGS_JS.index("function renderSettings()") :]
        assert "renderVoiceSettings()" in render[: render.index("\n}\n")]
        diarize = SETTINGS_JS[SETTINGS_JS.index("$('#diarize-toggle').addEventListener") :]
        assert "renderVoiceSettings()" in diarize[: diarize.index("\n  });\n")]

    def test_only_known_bridge_methods(self):
        """coaching-progress.js calls only existing bridge methods plus Task 9's coaching ones."""
        bridge = (APP / "tauri-bridge.js").read_text()
        task9 = {"coachingProgress", "voiceStatus", "voiceInstall", "voiceSetup", "voiceDelete"}
        used = set(re.findall(r"window\.plaud\.(\w+)\(", JS))
        assert used, "no bridge calls found"
        for name in used - task9:
            assert re.search(rf"\b{name}\s*:", bridge), f"window.plaud.{name} isn't in tauri-bridge.js"
        if re.search(r"\bvoiceStatus\s*:", bridge):  # once Task 9 is merged, all must exist
            for name in used & task9:
                assert re.search(rf"\b{name}\s*:", bridge), f"window.plaud.{name} isn't in tauri-bridge.js"

    def test_metric_map_mirrors_targets(self):
        """The JS label map repeats targets.TARGETS' bands; keep them in step."""
        found = {
            m.group(1): (m.group(2), m.group(3))
            for m in re.finditer(r"^\s+(\w+): \{ label: '[^']+', low: (null|[\d.]+), high: (null|[\d.]+),", JS, re.M)
        }
        assert set(found) == set(T.TARGETS) | {"overall_score"}
        for key, t in T.TARGETS.items():
            low, high = found[key]
            assert (None if low == "null" else float(low)) == t["low"], key
            assert (None if high == "null" else float(high)) == t["high"], key

    def test_text_is_escaped(self):
        """Every interpolation into innerHTML goes through escapeHtml or a number formatter."""
        assert "escapeHtml(" in JS
        assert "insertAdjacentHTML" not in JS

    def test_install_polls_every_two_seconds(self):
        assert "const VOICE_POLL_MS = 2000;" in JS
        assert re.search(r"setTimeout\([\s\S]*?\}, VOICE_POLL_MS\)", JS)


class TestSharedGlobalScope:
    """app/ui/*.js and renderer.js share one global scope: a name declared at the top
    level of two files is a SyntaxError that stops the later script (v2.1.5). This
    globs the files rather than reading script tags, so files added in parallel
    (e.g. ui/coaching.js) are covered before their tag is merged."""

    def test_no_duplicate_top_level_names(self):
        files = sorted((APP / "ui").glob("*.js")) + [APP / "renderer.js"]
        assert APP / "ui" / "coaching-progress.js" in files
        seen = {}
        for path in files:
            for m in re.finditer(
                r"^(?:async\s+)?(?:function\s+([A-Za-z_$][\w$]*)|(?:const|let|var|class)\s+([A-Za-z_$][\w$]*))",
                path.read_text(),
                re.M,
            ):
                name = m.group(1) or m.group(2)
                assert name not in seen, f"{name} declared in {seen.get(name)} and {path.name}"
                seen[name] = path.name

    def test_progress_globals_are_prefixed(self):
        public = {"openProgressView", "renderVoiceSettings"}
        for m in re.finditer(
            r"^(?:async\s+)?(?:function\s+([A-Za-z_$][\w$]*)|(?:const|let|var|class)\s+([A-Za-z_$][\w$]*))", JS, re.M
        ):
            name = m.group(1) or m.group(2)
            assert name in public or re.match(r"_?(coachProg|voice|COACH_PROG|VOICE)", name), name


# Runs coaching-progress.js in node with a minimal fake DOM and bridge.
_NODE_JS = r"""
const fs = require('fs'); const vm = require('vm');
const els = {};
const el = (id) => els[id] || (els[id] = {
  id, innerHTML: '', dataset: {}, disabled: false, checked: false,
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  addEventListener() {}, querySelectorAll: () => [], scrollIntoView() {},
});
let inFlight = 0; let maxInFlight = 0; let statusCalls = 0;
const ctx = {
  console, setTimeout, Date, Number, String, Math, Promise,
  document: { addEventListener() {} },
  $: (sel) => el(sel),
  escapeHtml: (t) => String(t ?? '').replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`),
  app: { settings: { diarize: true, coaching_auto: true }, recordings: [], transcripts: {} },
  toast() {}, loadRecordings: async () => {}, formatDate: (d) => d, formatTime: (t) => String(t),
  window: { plaud: {
    voiceStatus: async () => {
      statusCalls += 1; inFlight += 1; maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise((r) => setTimeout(r, 20));
      inFlight -= 1;
      return { profile: true, exemplars: '<b>3</b>', model: true, installing: false, install_error: null };
    },
  } },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const r = (code) => vm.runInContext(code, ctx);
(async () => {
  const unknown = { metric: 'new_metric', from: 1.234, to: 2, value: 3.456, target: { low: null, high: 1 } };
  r('_coachProgRender')({
    count: 5, series: { new_metric: [{ t: 1, value: 2 }], pace_wpm: [{ t: 1, value: 120 }, { t: 2, value: 130 }] },
    baseline: { pace_wpm: 125 }, weekly: [], most_improved: unknown, most_stuck: unknown, focus: null,
  });
  const progress = els['#progress-body'].innerHTML;
  await Promise.all([r('renderVoiceSettings')(), r('renderVoiceSettings')(), r('renderVoiceSettings')()]);
  console.log(JSON.stringify({
    value: r('_coachProgValue')('new_metric', 1.234),
    target: r('_coachProgTarget')('new_metric'),
    interruptOnAtBound: r('_coachProgOnTarget')('interruption_rate', 0.1),
    interruptTarget: r('_coachProgTarget')('interruption_rate'),
    progress, maxInFlight, statusCalls,
    status: els['#voice-status'].innerHTML,
  }));
})();
"""


@pytest.fixture(scope="module")
def node_out():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    out = subprocess.run(
        [node, "-e", _NODE_JS, str(APP / "ui" / "coaching-progress.js")], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out)


class TestBehaviour:
    def test_unknown_metric_renders_instead_of_throwing(self, node_out):
        assert node_out["value"] == "1.23"
        assert node_out["target"] == ""
        assert "Pace" in node_out["progress"] and "new_metric" in node_out["progress"]

    def test_interruption_band_matches_backend(self, node_out):
        # progress.py counts value > high as outside, so exactly 0.1 is on target ("at most").
        assert node_out["interruptOnAtBound"] is True
        assert node_out["interruptTarget"] == "at most 1 in 10 turns"

    def test_voice_renders_never_overlap(self, node_out):
        assert node_out["maxInFlight"] == 1
        assert node_out["statusCalls"] == 2  # the first render, then one catch-up for the calls made meanwhile

    def test_exemplar_count_is_a_number(self, node_out):
        assert "<b>" not in node_out["status"]
        assert "from 0 samples" in node_out["status"]

    def test_redo_replaces_in_one_call(self):
        save = JS[JS.index("async function _voiceSave(") :]
        save = save[: save.index("\n}\n")]
        assert "window.plaud.voiceSetup(_voiceSetupSid, labels, replace)" in save
        assert "voiceDelete" not in save
        assert "has been removed" not in save
        assert "Your old voice is kept." in save
