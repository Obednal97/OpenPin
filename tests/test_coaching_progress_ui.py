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


# Spec 2026-11-05-engagement-design.md: the three engagement parts and their targets (low, high)
ENGAGEMENT_PARTS = {"laughs_per_hour": (6.0, None), "follow_up_share": (0.25, None), "built_on_share": (0.5, None)}


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
        # The engagement parts join TARGETS with the engagement backend; their bands are the spec's either way
        bands = {**ENGAGEMENT_PARTS, **{k: (t["low"], t["high"]) for k, t in T.TARGETS.items()}}
        assert set(found) == set(bands) | {"overall_score", "engagement_score"}
        for key, (want_low, want_high) in bands.items():
            low, high = found[key]
            assert (None if low == "null" else float(low)) == want_low, key
            assert (None if high == "null" else float(high)) == want_high, key
        assert found["engagement_score"] == ("null", "null")

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
  // Engagement: the score next to the overall score, its parts with the measures
  const pts = (...v) => v.map((value, i) => ({ t: 1700000000 + i * 86400, value }));
  r('_coachProgRender')({
    count: 4,
    series: {
      overall_score: pts(60, 70), engagement_score: pts(40, 55), pace_wpm: pts(120, 130),
      laughs_per_hour: pts(2.5, 7.25), follow_up_share: pts(0.2, 0.125), built_on_share: pts(0.5),
    },
    baseline: { engagement_score: 47.5, laughs_per_hour: 4.875 }, weekly: [], most_improved: null, most_stuck: null, focus: null,
  });
  const engaged = els['#progress-body'].innerHTML;
  r('_coachProgRender')({
    count: 4, series: { overall_score: pts(60, 70), pace_wpm: pts(120, 130) },
    baseline: {}, weekly: [], most_improved: null, most_stuck: null, focus: null,
  });
  const notEngaged = els['#progress-body'].innerHTML;
  await Promise.all([r('renderVoiceSettings')(), r('renderVoiceSettings')(), r('renderVoiceSettings')()]);
  console.log(JSON.stringify({
    value: r('_coachProgValue')('new_metric', 1.234),
    target: r('_coachProgTarget')('new_metric'),
    interruptOnAtBound: r('_coachProgOnTarget')('interruption_rate', 0.1),
    interruptTarget: r('_coachProgTarget')('interruption_rate'),
    progress, engaged, notEngaged, maxInFlight, statusCalls,
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

    def test_engagement_with_the_scores_and_its_parts_with_the_measures(self, node_out):
        html = node_out["engaged"]
        labels = re.findall(r'<div class="coach-prog-row-label">([^<]+?) ?(?:<|$)', html, re.M)
        assert labels == [
            "Overall score",
            "Engagement score",
            "Pace",
            "Laughs after your lines",
            "Follow-up questions to you",
            "Your points others built on",
        ]

    def test_engagement_values_and_targets(self, node_out):
        html = node_out["engaged"]
        assert "55 / 100" in html and "Your usual 48 / 100" in html
        assert "7.3 an hour" in html and "target at least 6 an hour" in html
        assert "13% of your turns" in html and "target at least 25% of your turns" in html
        assert "target at least 50%" in html
        # Laughs at 7.3 an hour is on target; follow-ups at 13% are not; the score has no band
        rows = html.split('<div class="coach-prog-row">')[1:]
        state = {
            re.search(r'row-label">([^<]+?) ?<', r).group(1): re.search(r"coach-prog-state (on|off)", r) for r in rows
        }
        assert state["Engagement score"] is None
        assert state["Laughs after your lines"].group(1) == "on"
        assert state["Follow-up questions to you"].group(1) == "off"

    def test_no_engagement_series_no_engagement_rows(self, node_out):
        html = node_out["notEngaged"]
        assert "Overall score" in html and "Pace" in html
        for label in ("Engagement", "Laughs", "Follow-up", "built on"):
            assert label not in html

    def test_redo_replaces_in_one_call(self):
        save = JS[JS.index("async function _voiceSave(") :]
        save = save[: save.index("\n}\n")]
        assert "window.plaud.voiceSetup(_voiceSetupSid, labels, replace)" in save
        assert "voiceDelete" not in save
        assert "has been removed" not in save
        assert "Your old voice is kept." in save


# ---------------------------------------------------------------- the two "coach automatically" switches
#
# settings.js and coaching-progress.js run together in node with fake elements that keep
# their classes and listeners, so a change event on one switch can be fired and the other
# one read back.

_SYNC_JS = r"""
const fs = require('fs'); const vm = require('vm');
const els = {};
const STARTS_HIDDEN = ['#auto-coach-needs', '#automation-coach-hint', '#coaching-auto-needs'];
const mk = (id) => {
  const classes = new Set(STARTS_HIDDEN.includes(id) ? ['hidden'] : []);
  const listeners = {};
  const base = {
    id, innerHTML: '', textContent: '', value: '', dataset: {}, style: {}, disabled: false, checked: false,
    classList: {
      add: (...c) => c.forEach((x) => classes.add(x)), remove: (...c) => c.forEach((x) => classes.delete(x)),
      toggle: (c, on) => {
        const v = on === undefined ? !classes.has(c) : !!on;
        if (v) classes.add(c); else classes.delete(c);
        return v;
      },
      contains: (c) => classes.has(c),
    },
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    fire: async (type) => { for (const fn of listeners[type] || []) await fn({ target: proxy, currentTarget: proxy }); },
    classes: () => [...classes].sort(),
  };
  // Anything else (closest, querySelector...) is a function returning another fake element
  const proxy = new Proxy(base, {
    get: (t, k) => (k in t ? t[k] : typeof k === 'symbol' ? undefined : () => mk(`${id}>${String(k)}`)),
  });
  return proxy;
};
const el = (sel) => els[sel] || (els[sel] = mk(sel));
const saved = [];
const ctx = {
  console, setTimeout, clearTimeout, Date, Number, String, Math, Promise,
  document: { addEventListener() {}, querySelector: (s) => el(s), querySelectorAll: () => [], createElement: () => mk('new'),
    body: mk('body') },
  $: (sel) => el(sel), $$: () => [],
  escapeHtml: (t) => String(t ?? ''),
  app: { settings: {}, recordings: [], transcripts: {} },
  toast() {}, loadRecordings: async () => {}, initTooltips() {},
  window: { plaud: new Proxy({ saveSettings: async (d) => { saved.push(d); return {}; } },
    { get: (t, k) => t[k] || (async () => ({})) }) },
};
vm.createContext(ctx);
for (const f of process.argv.slice(1)) vm.runInContext(fs.readFileSync(f, 'utf8'), ctx);
const r = (code) => vm.runInContext(code, ctx);
const S = ctx.app.settings;
const state = () => ({
  auto: el('#auto-coach-toggle').checked,
  voice: el('#coaching-auto-toggle').checked,
  autoDisabled: el('#auto-coach-toggle').disabled,
  row: el('#auto-coach-row').classes(),
  needs: !el('#auto-coach-needs').classList.contains('hidden'),
  hint: !el('#automation-coach-hint').classList.contains('hidden'),
});
const flip = async (sel, on) => { el(sel).checked = on; await el(sel).fire('change'); };
(async () => {
  r('initSettingsEvents')();
  Object.assign(S, { diarize: true, coaching_auto: true, auto_sync: true, auto_transcribe: true });
  el('#auto-sync-toggle').checked = true;
  el('#auto-transcribe-toggle').checked = true;
  r('updateAutoPipelineState')();
  r('_voiceRenderAutoToggle')();
  const out = { start: state() };
  await flip('#coaching-auto-toggle', false);
  out.voiceOff = { ...state(), setting: S.coaching_auto };
  await flip('#auto-coach-toggle', true);
  out.autoOn = { ...state(), setting: S.coaching_auto };
  el('#auto-transcribe-toggle').checked = false;
  r('updateAutoPipelineState')();
  out.noTranscribe = state();
  el('#auto-transcribe-toggle').checked = true;
  await flip('#diarize-toggle', false);
  out.noLabels = state();
  S.coaching_ai_review = false;
  r('_voiceRenderAutoToggle')();
  out.reviewShownOff = el('#coaching-ai-review-toggle').checked === false;
  await flip('#coaching-ai-review-toggle', true);
  out.reviewSetting = S.coaching_ai_review;
  out.saved = saved;
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def sync_out():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    res = subprocess.run(
        [node, "-e", _SYNC_JS, str(APP / "ui" / "settings.js"), str(APP / "ui" / "coaching-progress.js")],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _automation_pipeline():
    start = HTML.index('<div class="auto-pipeline" id="automation-pipeline">')
    return HTML[start : HTML.index("</div>", start)]


class TestCoachingSwitches:
    def test_automation_row_sits_between_transcribe_and_summarise(self):
        pipeline = _automation_pipeline()
        order = [pipeline.index(f'id="{i}"') for i in ("auto-transcribe-row", "auto-coach-row", "auto-summarise-row")]
        assert order == sorted(order)
        row = pipeline[pipeline.index('id="auto-coach-row"') :]
        row = row[: row.index("</label>")]
        assert "<span>Coach my conversations</span>" in row
        assert 'id="auto-coach-needs" class="voice-needs hidden">Needs speaker labels<' in row
        assert 'id="automation-coach-hint"' in pipeline

    def test_ai_review_switch_in_voice_section(self):
        tab = _ai_tab()
        section = tab[tab.index('id="voice-coaching-section"') :]
        assert section.index('id="coaching-auto-toggle"') < section.index('id="coaching-ai-review-toggle"')
        assert "<span>Include an AI review</span>" in section
        assert "Sends the transcript to your AI provider for scores and tips." in section
        assert "nothing from coaching leaves your Mac" in section
        assert "\u2014" not in section  # no em dashes in user-facing copy

    def test_both_switches_start_from_the_setting(self, sync_out):
        assert sync_out["start"] == {
            "auto": True,
            "voice": True,
            "autoDisabled": False,
            "row": [],
            "needs": False,
            "hint": False,
        }

    def test_changing_either_switch_updates_the_other(self, sync_out):
        assert sync_out["voiceOff"]["auto"] is False and sync_out["voiceOff"]["setting"] is False
        assert sync_out["autoOn"]["voice"] is True and sync_out["autoOn"]["setting"] is True
        assert {"coaching_auto": False} in sync_out["saved"]
        assert {"coaching_auto": True} in sync_out["saved"]

    def test_stays_usable_without_automatic_transcription(self, sync_out):
        # Coaching runs after any transcription, including one the user starts, so the
        # switch must not look disabled while it still takes effect.
        s = sync_out["noTranscribe"]
        assert s["row"] == [] and not s["autoDisabled"] and s["hint"]
        assert s["needs"] is False

    def test_needs_speaker_labels(self, sync_out):
        s = sync_out["noLabels"]
        assert s["auto"] is False and s["voice"] is False and s["autoDisabled"]
        assert s["row"] == ["voice-auto-off"] and s["needs"] and not s["hint"]

    def test_ai_review_switch_saves_its_own_key(self, sync_out):
        assert sync_out["reviewShownOff"]
        assert sync_out["reviewSetting"] is True
        assert {"coaching_ai_review": True} in sync_out["saved"]
