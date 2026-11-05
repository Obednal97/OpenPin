"""Coaching tab in the recording view (app/ui/coaching.js).

Static checks parse the HTML and JS as text, like tests/test_settings_ui.py. The
report's HTML builders are pure functions, so they run in node against a
realistic sidecar to check what the user sees (labels, badges, escaping, order).
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
COACH_JS = APP / "ui" / "coaching.js"
TOP_LEVEL = re.compile(
    r"^(?:async\s+)?(?:function\s+([A-Za-z_$][\w$]*)|(?:const|let|var|class)\s+([A-Za-z_$][\w$]*))", re.M
)


def _top_level_names(text):
    return {m.group(1) or m.group(2) for m in TOP_LEVEL.finditer(text)}


def _coach_src():
    return COACH_JS.read_text()


class TestMarkup:
    def test_coaching_tab_button(self):
        tabs = HTML[HTML.index('<div id="transcript-tabs"') : HTML.index('<div id="transcript-content">')]
        assert re.findall(r'data-tab="([^"]+)"', tabs) == ["transcript", "summary", "coaching"]
        assert ">Coaching</button>" in tabs

    def test_coaching_container_in_transcript_content(self):
        content = HTML[HTML.index('<div id="transcript-content">') :]
        content = content[: content.index("</main>")]
        assert 'id="transcript-coaching"' in content
        assert re.search(r'<div id="transcript-coaching" class="[^"]*\bhidden\b', content)

    def test_script_order(self):
        scripts = re.findall(r'<script src="([^"]+)"></script>', HTML)
        i = scripts.index("ui/coaching.js")
        assert scripts[i - 1] == "ui/transcript.js"
        assert i < scripts.index("renderer.js")

    def test_css_imported_after_transcript(self):
        imports = re.findall(r"@import '([^']+)';", (APP / "styles.css").read_text())
        assert imports[imports.index("css/transcript.css") + 1] == "css/coaching.css"
        assert (APP / "css" / "coaching.css").exists()


class TestScript:
    def test_public_functions(self):
        src = _coach_src()
        assert re.search(r"^async function renderCoachingTab\(container, sessionId\)", src, re.M)
        assert re.search(r"^function coachSeek\(t\)", src, re.M)

    def test_no_name_collides_with_other_scripts(self):
        mine = _top_level_names(_coach_src())
        others = [p for p in (APP / "ui").glob("*.js") if p.name != "coaching.js"] + [APP / "renderer.js"]
        for path in others:
            clash = mine & _top_level_names(path.read_text())
            assert not clash, f"{sorted(clash)} also declared in {path.name}"

    def test_globals_are_prefixed(self):
        bad = {n for n in _top_level_names(_coach_src()) if not re.match(r"_?coach", n, re.I)}
        assert bad == {"renderCoachingTab"}

    def test_polls_every_five_seconds(self):
        src = _coach_src()
        assert "const _COACH_POLL_MS = 5000;" in src
        assert "setInterval(" in src

    def test_uses_bridge_methods(self):
        src = _coach_src()
        for call in (
            "window.plaud.coaching(",
            "window.plaud.coachingRun(",
            "window.plaud.coachingSetOwner(",
            "window.plaud.coachingMixedUp(",
            "window.plaud.coachingProgress(",
        ):
            assert call in src


class TestTranscriptWiring:
    SRC = (APP / "ui" / "transcript.js").read_text()

    def test_show_tab_handles_coaching(self):
        body = self.SRC[self.SRC.index("function showTranscriptTab(") :]
        body = body[: body.index("\nfunction ")]
        assert "'coaching'" in body
        assert "renderCoachingTab(coachContainer, sessionId)" in body

    def test_tab_row_shows_with_a_transcript(self):
        body = self.SRC[self.SRC.index("  // Tabs") :]
        body = body[: body.index("function showTranscriptTab(")]
        assert "tabsEl.classList.toggle('hidden', !hasTranscriptText)" in body
        # Clicking a tab must not stack one listener per openTranscript() call
        assert "tab.onclick = " in body
        assert "addEventListener('click'" not in body


_RUN_JS = r"""
const fs = require('fs'); const vm = require('vm');
const el = () => ({ classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } }, setAttribute() {},
  appendChild() {}, style: {}, dataset: {} });
const ctx = { console, window: { plaud: {} }, setTimeout, clearTimeout, setInterval, clearInterval,
  document: { querySelector: () => null, querySelectorAll: () => [], createElement: el, body: el() } };
vm.createContext(ctx);
for (const f of process.argv.slice(1, 3)) vm.runInContext(fs.readFileSync(f, 'utf8'), ctx);
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const r = (code) => vm.runInContext(code, ctx);
const out = {};
for (const [name, c] of Object.entries(data.cases)) {
  out[name] = r('_coachReportHtml')(c.sidecar, c.baseline || null, data.segments);
}
out.moments = r('_coachMoments')(data.cases.done.sidecar);
out.overall = r('_coachOverall')(data.cases.done.sidecar.review);
const aiKinds = ['interrupted_them', 'talked over', 'Cut in', 'cuts off', 'speaking over them', 'strong question'];
out.aiKinds = aiKinds.map((kind) => r('_coachMoments')({ review: { moments: [{ t: 5, kind, note: 'x' }] } })[0].label);
out.overallNone = r('_coachOverall')(null);
console.log(JSON.stringify(out));
"""

TARGETS = {
    "pace_wpm": {"low": 110.0, "high": 160.0, "label": "Pace (words per minute)"},
    "talk_share": {"low": 0.4, "high": 0.6, "label": "Talk share"},
    "longest_monologue_s": {"low": None, "high": 150.0, "label": "Longest monologue (seconds)"},
    "interruption_rate": {"low": None, "high": 0.1, "label": "Likely interruptions per owner turn"},
    "patience_s": {"low": 0.5, "high": 1.5, "label": "Patience"},
    "questions_per_hour": {"low": 10.0, "high": 18.0, "label": "Questions per hour"},
    "fillers_per_min": {"low": 0.6, "high": 3.0, "label": "Filler words per speaking minute"},
    "interactivity": {"low": 5.0, "high": None, "label": "Speaker switches per 5 minutes"},
}

SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Morning, thanks for making time.", "speaker": "Speaker 1"},
    {"start": 4.5, "end": 9.0, "text": "No problem <b>at all</b>.", "speaker": "Speaker 2"},
    {"start": 61.7, "end": 70.0, "text": "So the price is", "speaker": "Speaker 1"},
    {"start": 70.1, "end": 80.0, "text": "Hang on, can we go back?", "speaker": "Speaker 2"},
]

METRICS = {
    "per_speaker": {
        "Speaker 1": {"wpm": 171.2, "talk_s": 400.0, "talk_share": 0.62, "turns": 20},
        "Speaker 2": {"wpm": 128.4, "talk_s": 245.0, "talk_share": 0.38, "turns": 19},
    },
    "owner": {
        "pace_wpm": 128.4,
        "talk_share": 0.38,
        "longest_monologue_s": 95.0,
        "interruptions_made": 2,
        "interruption_rate": 0.105,
        "interrupted_by_others": 1,
        "patience_s": 0.8,
        "questions": 6,
        "questions_per_hour": 33.2,
        "fillers": None,
        "fillers_per_min": None,
        "interactivity": 7.5,
    },
    "availability": {
        "pace_wpm": "estimated",
        "talk_share": "estimated",
        "longest_monologue_s": "exact",
        "interruptions_made": "exact",
        "interruption_rate": "exact",
        "interrupted_by_others": "exact",
        "patience_s": "exact",
        "questions": "exact",
        "questions_per_hour": "exact",
        "fillers": "unavailable",
        "fillers_per_min": "unavailable",
        "interactivity": "estimated",
    },
    "moments": [
        {
            "t": 70.1,
            "kind": "interruption",
            "speaker": "Speaker 2",
            "quote": "Hang on, can we go back?",
            "likely": True,
        },
        {"t": 4.5, "kind": "monologue", "speaker": "Speaker 2", "quote": "No problem <b>at all</b>.", "likely": False},
    ],
}

REVIEW = {
    "scores": {
        "clarity": {"score": 70, "reason": "Clear on <script>price</script>."},
        "warmth": {"score": 80, "reason": "Friendly."},
        "charisma": {"score": 60, "reason": "Flat in the middle."},
        "listening": {"score": 50, "reason": "Talked over them."},
        "reception": {"score": 90, "reason": "They engaged."},
    },
    "summary": "You came across as <i>warm</i> but rushed the pricing.",
    "next_time": ["Pause after the price.", "Ask one open question first."],
    "reactions": [
        {"t": 61.7, "owner_point": "Pricing", "response": "pushed_back", "evidence": "They asked to go back."}
    ],
    "moments": [{"t": 0.0, "kind": "strong question", "note": "Opened with thanks."}],
    "interruptions_confirmed": [70.1],
}

DONE = {
    "status": "done",
    "owner": {"labels": ["Speaker 2"], "source": "voice", "confidence": 0.81},
    "metrics": METRICS,
    "review": REVIEW,
    "report_stale": False,
    "mixed_up": False,
    "targets": TARGETS,
}

CASES = {
    "done": {"sidecar": DONE, "baseline": {"pace_wpm": 140.0, "overall_score": 64.0, "talk_share": None}},
    "nobaseline": {"sidecar": DONE},
    "stale": {"sidecar": {**DONE, "report_stale": True}},
    "metricsonly": {"sidecar": {**DONE, "review": None, "status_reason": "measured metrics only (no AI provider)"}},
    "running": {"sidecar": {"status": "running", "targets": TARGETS}},
    "needs_owner": {
        "sidecar": {
            "status": "needs_owner",
            "candidates": [
                {"label": "Speaker 1", "first_line": "Morning, <img src=x>", "loud": False},
                {"label": "Speaker 2", "first_line": "No problem at all.", "loud": True},
            ],
            "targets": TARGETS,
        }
    },
    "skipped": {"sidecar": {"status": "skipped", "status_reason": "too short", "targets": TARGETS}},
    "skipped_no_labels": {"sidecar": {"status": "skipped", "status_reason": "no speaker labels", "targets": TARGETS}},
    "skipped_only_you": {
        "sidecar": {
            "status": "skipped",
            "status_reason": "only you spoke",
            "owner": {"labels": ["Speaker 1", "Speaker 2"], "source": "voice", "confidence": 0.7},
            "targets": TARGETS,
        }
    },
    "interrupted": {
        "sidecar": {"status": "failed", "status_reason": "Coaching was interrupted. Try again.", "targets": TARGETS}
    },
    "failed": {
        "sidecar": {
            **DONE,
            "status": "failed",
            "review": None,
            "status_reason": "anthropic: 401 <bad key>; openai: timeout",
        }
    },
    "empty": {"sidecar": {"targets": TARGETS}},
}


@pytest.fixture(scope="module")
def out():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    payload = json.dumps({"cases": CASES, "segments": SEGMENTS})
    res = subprocess.run(
        [node, "-e", _RUN_JS, str(APP / "ui" / "utils.js"), str(COACH_JS)],
        input=payload,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


class TestReport:
    def test_render_order(self, out):
        html = out["done"]
        marks = ["This is you", "Overall", "Clarity", "Next time", "Measures", "Moments", "How they responded"]
        positions = [html.index(m) for m in marks]
        assert positions == sorted(positions)

    def test_owner_line(self, out):
        assert "This is you:" in out["done"]
        assert "Speaker 2" in out["done"]
        assert 'data-coach-action="change-owner"' in out["done"]

    def test_overall_is_mean_of_five(self, out):
        assert out["overall"] == 70
        assert out["overallNone"] is None
        assert ">70<" in out["done"]

    def test_your_usual_only_with_baseline(self, out):
        assert "Your usual" in out["done"]
        assert "64" in out["done"]
        assert "Your usual" not in out["nobaseline"]

    def test_badges_from_availability(self, out):
        html = out["done"]
        assert html.count('class="coach-badge coach-badge-estimated"') >= 2
        assert 'class="coach-badge coach-badge-unavailable"' in html

    def test_wpm_per_speaker(self, out):
        assert "171" in out["done"] and "128" in out["done"]
        assert "62%" in out["done"] and "38%" in out["done"]

    def test_moments_sorted_and_labelled(self, out):
        times = [m["t"] for m in out["moments"]]
        assert times == sorted(times)
        assert [m["t"] for m in out["moments"]] == [0.0, 4.5, 70.1]
        inter = next(m for m in out["moments"] if m["t"] == 70.1)
        assert inter["label"] == "Likely interruption"
        assert "Likely interruption" in out["done"]
        assert 'data-coach-seek="70.1"' in out["done"]
        # An AI-written interruption moment is "likely" too
        assert out["aiKinds"] == ["Likely interruption"] * 5 + ["Strong question"]

    def test_reactions(self, out):
        assert "Pushed back" in out["done"]
        assert 'data-coach-seek="61.7"' in out["done"]

    def test_text_is_escaped(self, out):
        for name in ("done", "needs_owner", "failed"):
            assert "<script>" not in out[name]
            assert "<img" not in out[name]
            assert "<b>at all</b>" not in out[name]
            assert "<i>warm</i>" not in out[name]
        assert "&lt;script&gt;" in out["done"]
        assert "&lt;bad key&gt;" in out["failed"]

    def test_stale_banner(self, out):
        assert "Transcript changed" in out["stale"]
        assert 'data-coach-action="run"' in out["stale"]
        assert "Transcript changed" not in out["done"]

    def test_metrics_only_report(self, out):
        html = out["metricsonly"]
        assert "measured metrics only" in html
        assert "Measures" in html
        assert "Overall" not in html

    def test_running(self, out):
        assert "coach-spinner" in out["running"]

    def test_needs_owner_picker_inline(self, out):
        html = out["needs_owner"]
        assert "Which one is you?" in html
        assert html.count('type="checkbox" name="coach-owner"') == 2
        assert "Speakers mixed up" in html
        assert "Morning, &lt;img src=x&gt;" in html
        assert "Measures" not in html

    def test_skipped(self, out):
        assert "Coach anyway" in out["skipped"]
        assert 'data-coach-action="run"' in out["skipped"]
        assert "under 2 minutes" in out["skipped"]

    def test_skips_that_force_cannot_fix_offer_no_run(self, out):
        # force only bypasses the minimum length (app/coaching/runner.py)
        for name in ("skipped_no_labels", "skipped_only_you"):
            assert 'data-coach-action="run"' not in out[name]
            assert "Coach anyway" not in out[name]
        assert "Turn on speaker labels in Settings" in out["skipped_no_labels"]
        assert "re-transcribe" in out["skipped_no_labels"]

    def test_only_you_offers_owner_picker(self, out):
        html = out["skipped_only_you"]
        assert "This is you:" in html
        assert 'data-coach-action="change-owner"' in html
        assert html.count('type="checkbox" name="coach-owner"') == 2

    def test_min_length_copy_matches_backend(self):
        from app.coaching.targets import MIN_COACHING_SECONDS

        m = re.search(r"^const _COACH_MIN_COACHING_SECONDS = (\d+);", _coach_src(), re.M)
        assert m and int(m.group(1)) == MIN_COACHING_SECONDS
        assert "2 minutes" not in _coach_src()

    def test_interrupted_run_offers_try_again(self, out):
        html = out["interrupted"]
        assert "Coaching was interrupted. Try again." in html
        assert 'data-coach-action="run"' in html
        assert "Measures" not in html
        assert "still right" not in html

    def test_failed_shows_errors_metrics_and_retry(self, out):
        html = out["failed"]
        assert "openai: timeout" in html
        assert "Measures" in html
        assert "Try again" in html

    def test_not_coached_yet(self, out):
        assert 'data-coach-action="run"' in out["empty"]


_SAVE_JS = r"""
const fs = require('fs'); const vm = require('vm');
const el = () => ({ classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } }, setAttribute() {},
  appendChild() {}, style: {}, dataset: {} });
const timers = new Map(); // fake setInterval: run by tick()
let nextId = 1;
const ctx = { console, window: {}, setTimeout, clearTimeout, Date,
  setInterval: (fn) => { timers.set(nextId, fn); return nextId++; }, clearInterval: (id) => timers.delete(id),
  document: { querySelector: () => null, querySelectorAll: () => [], createElement: el, body: el() } };
vm.createContext(ctx);
for (const f of process.argv.slice(1, 3)) vm.runInContext(fs.readFileSync(f, 'utf8'), ctx);
const r = (code) => vm.runInContext(code, ctx);
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const app = r('app');
Object.assign(app, { currentView: 'transcript', currentSessionId: 's1', activeTab: 'coaching' });
app.transcripts.s1 = { segments: data.segments };
const calls = [];
let serverSidecar = data.before;
ctx.window.plaud = {
  coaching: async () => { calls.push('get'); return serverSidecar; },
  coachingProgress: async () => ({ baseline: null }),
  coachingSetOwner: async (sid, labels, learn) => { calls.push(['owner', labels, learn]); return { ...data.before, started: true }; },
  coachingMixedUp: async (sid, flag) => { calls.push(['mixed', flag]); return { ...serverSidecar, mixed_up: flag }; },
};
const container = (checked, mixed) => ({
  innerHTML: '', dataset: {}, isConnected: true, classList: { contains: () => false },
  querySelectorAll: (sel) => (sel.includes('coach-owner') ? checked.map((value) => ({ value })) : []),
  querySelector: (sel) => (sel.includes('coach-mixed') ? { checked: mixed } : null),
});
const btn = () => ({ classList: { add() {}, remove() {} }, style: {}, disabled: false });
const settle = () => new Promise((res) => setTimeout(res, 5));
const tick = async () => { for (const fn of [...timers.values()]) await fn(); await settle(); };
(async () => {
  const out = {};
  // 1. A new owner pick: running at once; the poll waits while the sidecar is unchanged,
  //    then draws the finished report
  const c1 = container(['Speaker 1'], false);
  await r('_coachSaveOwner')(c1, 's1', data.before, btn());
  out.ownerCalls = calls.splice(0);
  out.runningAfterSave = c1.innerHTML.includes('coach-spinner');
  out.polling = timers.size === 1;
  await tick();
  out.runningWhileUnchanged = c1.innerHTML.includes('coach-spinner');
  serverSidecar = data.after;
  await tick();
  out.reportAfterRun = c1.innerHTML.includes('coach-overall') && c1.innerHTML.includes('you chose this');
  out.pollStopped = timers.size === 0;
  calls.splice(0);
  // 2. Only the mixed-up flag: drawn from the response, no GET and no re-run
  const c2 = container(['Speaker 1'], true);
  await r('_coachSaveOwner')(c2, 's1', data.after, btn());
  out.mixedCalls = calls.splice(0);
  out.mixedNote = c2.innerHTML.includes('Marked as speakers mixed up');
  r('_coachStopPoll')();
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def save_out():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    before = {**DONE, "owner": {"labels": ["Speaker 2"], "source": "voice", "confidence": 0.6}}
    after = {**DONE, "owner": {"labels": ["Speaker 1"], "source": "user", "confidence": 1.0}}
    res = subprocess.run(
        [node, "-e", _SAVE_JS, str(APP / "ui" / "utils.js"), str(COACH_JS)],
        input=json.dumps({"before": before, "after": after, "segments": SEGMENTS}),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


class TestOwnerSave:
    def test_new_owner_shows_running_and_polls(self, save_out):
        assert save_out["ownerCalls"] == [["owner", ["Speaker 1"], True]]
        assert save_out["runningAfterSave"]
        assert save_out["polling"]
        # The background run hasn't changed the sidecar yet: keep waiting, don't redraw the old one
        assert save_out["runningWhileUnchanged"]

    def test_report_drawn_when_the_run_finishes(self, save_out):
        assert save_out["reportAfterRun"]
        assert save_out["pollStopped"]

    def test_mixed_up_only_draws_from_response(self, save_out):
        assert save_out["mixedCalls"] == [["mixed", True]]
        assert save_out["mixedNote"]
