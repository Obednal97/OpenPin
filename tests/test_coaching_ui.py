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
            "window.plaud.coachingExclude(",
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
// "Set up your voice" opens Settings on the AI tab at the voice section, from a click in the tab
const opened = [];
Object.assign(ctx, {
  openSettings: () => opened.push('open'), loadSettings: () => opened.push('load'),
  showSettingsTab: (t) => opened.push(`tab:${t}`),
});
ctx.document.querySelector = (sel) => (sel === '#voice-coaching-section' ? { scrollIntoView: () => opened.push('scroll') } : null);
r('app').transcripts.s1 = { segments: data.segments };
const tab = { innerHTML: '', dataset: {} };
r('_coachDraw')(tab, 's1', data.cases.needs_owner_no_profile.sidecar, null);
const action = { dataset: { coachAction: 'voice-settings' }, disabled: false };
tab.onclick({ target: { closest: (sel) => (sel === '[data-coach-action]' ? action : null) } });
out.voiceSettings = opened;
out.overallPartial = r('_coachOverall')(data.cases.partial.sidecar.review);
out.noEvidence = r('_coachEvidenceHtml')(undefined) + r('_coachEvidenceHtml')(['x', -1, null]);
// "Include an AI review" is off now (the loop above rendered it with the setting on)
r('app').settings.coaching_ai_review = false;
out.reviewoffSettingOff = r('_coachReportHtml')(data.cases.reviewoff.sidecar, null, data.segments);
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
        "clarity": {"score": 70, "reason": "Clear on <script>price</script>.", "evidence": [61.7, 0.0]},
        "warmth": {"score": 80, "reason": "Friendly.", "evidence": [0.0]},
        "charisma": {"score": 60, "reason": "Flat in the middle.", "evidence": [61.7]},
        "listening": {"score": 50, "reason": "Talked over them.", "evidence": [70.1]},
        "reception": {"score": 90, "reason": "They engaged.", "evidence": [4.5]},
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
    "reviewoff": {"sidecar": {**DONE, "review": None, "status_reason": "AI review is off"}},
    "excluded": {"sidecar": {"status": "excluded", "excluded": True, "status_reason": "", "targets": TARGETS}},
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
    **{
        f"needs_owner_{key}": {
            "sidecar": {"status": "needs_owner", "status_reason": reason, "candidates": [], "targets": TARGETS}
        }
        for key, reason in (
            ("no_profile", "no voice profile"),
            ("no_model", "voice model not installed"),
            ("no_match", "no confident match"),
            ("no_audio", "no audio file"),
        )
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
    # A rerun that was interrupted: the review and metrics on file are the previous run's
    "failed_previous": {
        "sidecar": {**DONE, "status": "failed", "status_reason": "Coaching was interrupted. Try again."}
    },
    # The review dropped listening and charisma: none of their evidence matched the transcript
    "partial": {
        "sidecar": {
            **DONE,
            "review": {
                **REVIEW,
                "scores": {k: v for k, v in REVIEW["scores"].items() if k not in ("listening", "charisma")},
            },
        }
    },
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

    def test_overall_is_mean_of_scores_present(self, out):
        # (70 + 80 + 90) / 3: dropped scores neither render nor count
        assert out["overallPartial"] == 80
        html = out["partial"]
        assert ">80<" in html
        assert "Listening" not in html and "Charisma" not in html
        assert "Clarity" in html and "Reception" in html

    def test_score_evidence_chips_seek(self, out):
        html = out["done"]
        scores = html[html.index("coach-scores") : html.index("Next time")]
        clarity = scores[scores.index("Clarity") : scores.index("Warmth")]
        # Sorted, formatted like the transcript, and each one plays from its time
        chips = re.findall(
            r'<button type="button" class="coach-chip" data-coach-seek="([\d.]+)"[^>]*>([^<]+)</button>', clarity
        )
        assert chips == [("0", "00:00"), ("61.7", "01:01")]
        assert scores.count('class="coach-chip"') == 6
        assert out["noEvidence"] == ""

    def test_your_usual_only_with_baseline(self, out):
        assert "Your usual" in out["done"]
        assert "64" in out["done"]
        assert "Your usual" not in out["nobaseline"]

    def test_badges_from_availability(self, out):
        html = out["done"]
        assert html.count('class="coach-badge coach-badge-estimated"') >= 2
        assert 'class="coach-badge coach-badge-unavailable"' in html

    def test_interruption_target_copy(self, out):
        # Same words as the Progress view and the backend focus sentence (inclusive bound)
        assert "at most 1 in 10 turns" in out["done"]

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

    def test_needs_owner_says_why(self, out):
        settings_link = 'data-coach-action="voice-settings"'
        no_profile = out["needs_owner_no_profile"]
        assert "You haven’t set up your voice yet" in no_profile
        assert settings_link in no_profile
        assert "Settings › Transcription &amp; AI › Your voice &amp; coaching" in no_profile
        no_model = out["needs_owner_no_model"]
        assert "The voice model isn’t installed" in no_model
        assert settings_link in no_model
        assert "couldn’t tell from your voice" in out["needs_owner_no_match"]
        assert "no audio for this recording" in out["needs_owner_no_audio"]
        for name in ("needs_owner_no_match", "needs_owner_no_audio"):
            assert settings_link not in out[name]
            assert "set up your voice" not in out[name]
        for name in ("needs_owner_no_profile", "needs_owner_no_model", "needs_owner_no_match", "needs_owner_no_audio"):
            # The picker is always there, whatever the reason
            assert 'data-coach-action="save-owner"' in out[name]

    def test_voice_settings_link_opens_the_voice_section(self, out):
        assert out["voiceSettings"] == ["open", "load", "tab:ai", "scroll"]
        ai_tab = HTML[HTML.index('<div class="settings-tab-content" data-tab="ai"') :]
        ai_tab = ai_tab[: ai_tab.index('<div class="settings-tab-content"', 1)]
        assert 'id="voice-coaching-section"' in ai_tab

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

    def test_failed_rerun_hides_the_previous_report(self, out):
        html = out["failed_previous"]
        assert "Coaching didn’t finish" in html
        assert "Coaching was interrupted. Try again." in html
        assert "previous report is hidden" in html
        assert 'data-coach-action="run"' in html
        # Neither the old review nor the metrics measured with it pass as this run's
        for old in ("The AI review didn’t finish", "still right", "Overall", "Measures", "How they responded"):
            assert old not in html
        assert "This is you:" in html

    def test_not_coached_yet(self, out):
        assert 'data-coach-action="run"' in out["empty"]

    def test_review_off_shows_metrics_and_one_plain_line(self, out):
        html = out["reviewoffSettingOff"]
        line = "AI review is off. Turn it on in "
        assert html.count(line) == 1
        link = html[html.index(line) + len(line) :]
        assert link.startswith('<button type="button" class="coach-link" data-coach-action="voice-settings">')
        assert "Settings › Transcription &amp; AI › Your voice &amp; coaching</button>." in link
        assert "coach-notice coach-notice-info" in html
        assert "coach-failed" not in html and "didn’t finish" not in html
        assert "Measures" in html and "Moments" in html
        assert "Overall" not in html and "How they responded" not in html

    def test_review_off_report_after_turning_it_back_on(self, out):
        html = out["reviewoff"]
        assert "AI review is off. Turn it on" not in html
        assert "made with the AI review off" in html
        assert "Add an AI review" in html and 'data-coach-action="run"' in html
        assert "Measures" in html

    def test_review_off_reason_mirrors_the_runner(self):
        from app.coaching.runner import REVIEW_OFF

        m = re.search(r"^const _COACH_REVIEW_OFF = '([^']+)';", _coach_src(), re.M)
        assert m and m.group(1) == REVIEW_OFF

    def test_excluded(self, out):
        html = out["excluded"]
        assert "This recording isn’t coached." in html
        assert 'data-coach-action="include"' in html and "Coach this recording" in html
        assert 'data-coach-action="exclude"' not in html
        assert "Measures" not in html and "This is you:" not in html

    def test_every_other_state_offers_dont_coach(self, out):
        for name in ("done", "running", "needs_owner", "skipped", "skipped_only_you", "failed", "empty", "reviewoff"):
            html = out[name]
            assert html.count('data-coach-action="exclude"') == 1, name
            assert "Don’t coach this recording" in html, name


_SAVE_JS = r"""
const fs = require('fs'); const vm = require('vm');
const el = () => ({ classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } }, setAttribute() {},
  appendChild() {}, style: {}, dataset: {} });
const timers = new Map(); // fake setInterval: run by tick()
let nextId = 1;
let clock = 1e12; // fake Date.now: tick() moves it on by one poll
class FakeDate extends Date { static now() { return clock; } }
const ctx = { console, window: {}, setTimeout, clearTimeout, Date: FakeDate,
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
// GET /coaching/<sid> answers from the queue; the last answer repeats
let gets = [];
// The route shapes (final-fix-contract.md item 1): every response is the public sidecar plus targets
ctx.window.plaud = {
  coaching: async () => { calls.push('get'); return gets.length > 1 ? gets.shift() : gets[0]; },
  coachingProgress: async () => ({ baseline: null }),
  coachingRun: async () => { calls.push('run'); return { started: true }; },
  coachingSetOwner: async (sid, labels, learn) => { calls.push(['owner', labels, learn]); return data.ownerResponse; },
  coachingMixedUp: async (sid, flag) => { calls.push(['mixed', flag]); return { ...data.after, mixed_up: flag }; },
};
const container = (checked, mixed) => ({
  innerHTML: '', dataset: {}, isConnected: true, classList: { contains: () => false },
  querySelectorAll: (sel) => (sel.includes('coach-owner') ? checked.map((value) => ({ value })) : []),
  querySelector: (sel) => (sel.includes('coach-mixed') ? { checked: mixed } : null),
});
const btn = () => ({ classList: { add() {}, remove() {} }, style: {}, disabled: false });
const settle = () => new Promise((res) => setTimeout(res, 5));
const tick = async () => { clock += 5000; for (const fn of [...timers.values()]) await fn(); await settle(); };
const spinning = (c) => c.innerHTML.includes('coach-spinner');
(async () => {
  const out = {};
  // 1. A new owner pick: the POST response is already "running", so that's drawn at once;
  //    the poll keeps going while GET says running, then draws the new report
  gets = [data.ownerRunning, data.after];
  const c1 = container(['Speaker 1'], false);
  await r('_coachSaveOwner')(c1, 's1', data.before, btn());
  out.ownerCalls = calls.splice(0);
  out.runningAfterSave = spinning(c1);
  out.polling = timers.size === 1;
  await tick();
  out.runningWhileRunning = spinning(c1);
  await tick();
  out.reportAfterRun = c1.innerHTML.includes('coach-overall') && c1.innerHTML.includes('you chose this');
  out.pollStopped = timers.size === 0;
  calls.splice(0);

  // 2. Only the mixed-up flag: drawn from the response (targets included), no GET and no re-run
  const c2 = container(['Speaker 1'], true);
  await r('_coachSaveOwner')(c2, 's1', data.after, btn());
  out.mixedCalls = calls.splice(0);
  out.mixedNote = c2.innerHTML.includes('Marked as speakers mixed up');
  out.mixedTargets = c2.innerHTML.includes('110–160 wpm');
  r('_coachStopPoll')();

  // 3. Try again: POST /run only says started. The sidecar on screen was drawn without
  //    targets, so GET differs from it, but the outcome (status, created_at) is the old
  //    one: keep waiting. Then running, then a new outcome with the same error: draw it.
  const c3 = container([], false);
  gets = [{ ...data.failed, targets: data.targets }, { ...data.failed, status: 'running', status_reason: '' },
    { ...data.failed, created_at: '2026-11-04T11:00:00+00:00', targets: data.targets }];
  await r('_coachRun')(c3, 's1', data.failed, btn());
  out.tryCalls = calls.splice(0);
  out.tryRunning = spinning(c3);
  await tick();
  out.tryWaitsOnOldOutcome = spinning(c3);
  await tick();
  out.tryWaitsWhileRunning = spinning(c3);
  await tick();
  out.tryDrawsNewOutcome = !spinning(c3) && c3.innerHTML.includes('Try again');
  out.tryPollStopped = timers.size === 0;

  // 4. Coach anyway whose run finished before the first poll: a new created_at ends it
  const c4 = container([], false);
  gets = [data.after];
  await r('_coachRun')(c4, 's1', data.skipped, btn());
  await tick();
  out.quickRunDrawn = c4.innerHTML.includes('coach-overall') && timers.size === 0;

  // 5. A run that never reports (dropped as superseded): after the grace period the
  //    unchanged outcome is drawn instead of spinning for half an hour
  const c5 = container([], false);
  gets = [data.skipped];
  await r('_coachRun')(c5, 's1', data.skipped, btn());
  for (let i = 0; i < 3; i++) await tick();
  out.graceStillWaiting = spinning(c5);
  for (let i = 0; i < 2; i++) await tick();
  out.graceDrawn = c5.innerHTML.includes('Coach anyway') && timers.size === 0;
  calls.splice(0);
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def save_out():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    before = {
        **DONE,
        "owner": {"labels": ["Speaker 2"], "source": "voice", "confidence": 0.6},
        "created_at": "2026-11-04T10:00:00+00:00",
    }
    # POST /owner: the owner and status "running" are saved before the run (contract items 1 and 2)
    owner = {"labels": ["Speaker 1"], "source": "user", "confidence": 1.0}
    owner_running = {**before, "owner": owner, "status": "running", "status_reason": ""}
    after = {**DONE, "owner": owner, "created_at": "2026-11-04T10:05:00+00:00"}
    failed = {
        **{k: v for k, v in DONE.items() if k != "targets"},
        "status": "failed",
        "review": None,
        "status_reason": "AI feedback failed: openai: timeout",
        "created_at": "2026-11-04T10:30:00+00:00",
    }
    skipped = {"status": "skipped", "status_reason": "too short", "targets": TARGETS}
    payload = {
        "before": before,
        "ownerResponse": {**owner_running, "started": True},
        "ownerRunning": owner_running,
        "after": after,
        "failed": failed,
        "skipped": skipped,
        "targets": TARGETS,
        "segments": SEGMENTS,
    }
    res = subprocess.run(
        [node, "-e", _SAVE_JS, str(APP / "ui" / "utils.js"), str(COACH_JS)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


class TestOwnerSave:
    def test_new_owner_draws_running_from_the_response(self, save_out):
        assert save_out["ownerCalls"] == [["owner", ["Speaker 1"], True]]
        assert save_out["runningAfterSave"]
        assert save_out["polling"]

    def test_polls_while_running_then_draws_the_new_report(self, save_out):
        assert save_out["runningWhileRunning"]
        assert save_out["reportAfterRun"]
        assert save_out["pollStopped"]

    def test_mixed_up_only_draws_from_response(self, save_out):
        assert save_out["mixedCalls"] == [["mixed", True]]
        assert save_out["mixedNote"]
        assert save_out["mixedTargets"]


class TestRunPoll:
    def test_try_again_waits_for_a_new_outcome(self, save_out):
        assert save_out["tryCalls"] == ["run"]
        assert save_out["tryRunning"]
        # A sidecar that differs only in shape is still the old outcome
        assert save_out["tryWaitsOnOldOutcome"]
        assert save_out["tryWaitsWhileRunning"]
        # Same status and error, new created_at: that's this run's outcome
        assert save_out["tryDrawsNewOutcome"]
        assert save_out["tryPollStopped"]

    def test_run_that_ends_before_the_first_poll(self, save_out):
        assert save_out["quickRunDrawn"]

    def test_grace_period_ends_the_wait(self, save_out):
        assert save_out["graceStillWaiting"]
        assert save_out["graceDrawn"]


# ---------------------------------------------------------------- "Don't coach this recording"

_EXCLUDE_JS = r"""
const fs = require('fs'); const vm = require('vm');
const el = () => ({ classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } }, setAttribute() {},
  appendChild() {}, style: {}, dataset: {} });
const ctx = { console, window: {}, setTimeout, clearTimeout, setInterval, clearInterval,
  document: { querySelector: () => null, querySelectorAll: () => [], createElement: el, body: el() } };
vm.createContext(ctx);
for (const f of process.argv.slice(1, 3)) vm.runInContext(fs.readFileSync(f, 'utf8'), ctx);
const r = (code) => vm.runInContext(code, ctx);
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const app = r('app');
Object.assign(app, { currentView: 'transcript', currentSessionId: 's1', activeTab: 'coaching' });
app.transcripts.s1 = { segments: [] };
const calls = [];
const dialogs = [];
let answer = 'cancel';
ctx.showConfirmDialog = async (opts) => { dialogs.push(opts); return answer; };
ctx.toast = (msg, kind) => calls.push(['toast', kind]);
ctx.window.plaud = {
  coaching: async () => ({ status: 'running' }),
  coachingProgress: async () => ({ baseline: null }),
  coachingRun: async () => { calls.push('run'); return { started: true }; },
  coachingExclude: async (sid, flag) => { calls.push(['exclude', sid, flag]); return flag ? data.excluded : data.cleared; },
};
const container = () => ({ innerHTML: '', dataset: {}, isConnected: true, classList: { contains: () => false } });
const btn = () => ({ classList: { add() {}, remove() {} }, style: {}, disabled: false });
const click = (c, action) => c.onclick({ target: { closest: (sel) => (sel === '[data-coach-action]'
  ? { dataset: { coachAction: action }, disabled: false, classList: { add() {}, remove() {} }, style: {} } : null) } });
const settle = () => new Promise((res) => setTimeout(res, 5));
(async () => {
  const out = {};
  const c = container();
  r('_coachDraw')(c, 's1', data.done, null);
  out.offered = c.innerHTML.includes('data-coach-action="exclude"');
  // Cancel: nothing is sent
  click(c, 'exclude');
  await settle();
  out.cancelCalls = calls.splice(0);
  out.stillReport = c.innerHTML.includes('Measures');
  // Confirm: excluded, and the response is drawn
  answer = 'exclude';
  click(c, 'exclude');
  await settle();
  out.excludeCalls = calls.splice(0);
  out.dialog = dialogs[1];
  out.excludedShown = c.innerHTML.includes('This recording isn’t coached.') && !c.innerHTML.includes('Measures');
  // Coach this recording: clear the exclusion, then run (forced on the backend)
  click(c, 'include');
  await settle();
  out.includeCalls = calls.splice(0);
  out.running = c.innerHTML.includes('coach-spinner');
  r('_coachStopPoll')();
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def exclude_out():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    payload = {
        "done": DONE,
        "excluded": {"status": "excluded", "excluded": True, "status_reason": "", "targets": TARGETS},
        "cleared": {"targets": TARGETS},
    }
    res = subprocess.run(
        [node, "-e", _EXCLUDE_JS, str(APP / "ui" / "utils.js"), str(COACH_JS)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


class TestExclude:
    def test_asks_first_and_sends_nothing_on_cancel(self, exclude_out):
        assert exclude_out["offered"]
        assert exclude_out["cancelCalls"] == []
        assert exclude_out["stillReport"]

    def test_confirm_dialog_is_destructive(self, exclude_out):
        d = exclude_out["dialog"]
        assert d["title"] == "Don’t coach this recording?"
        assert d["actions"] == [{"id": "exclude", "label": "Don’t coach it", "destructive": True}]
        assert d["cancelLabel"] == "Cancel"
        assert "progress" in d["message"] and "voice" in d["message"]
        assert "\u2014" not in d["message"]

    def test_confirm_excludes_and_draws_the_response(self, exclude_out):
        assert exclude_out["excludeCalls"] == [["exclude", "s1", True], ["toast", "success"]]
        assert exclude_out["excludedShown"]

    def test_coach_this_recording_clears_then_runs(self, exclude_out):
        assert exclude_out["includeCalls"] == [["exclude", "s1", False], "run"]
        assert exclude_out["running"]


# ---------------------------------------------------------------- real backend JSON
#
# The tests above feed hand-written sidecars to the UI. This one builds the sidecar the
# way the app does (runner.set_owner: real metrics, the real ground() over a stubbed
# model answer, the real store) and reads it back through the real GET route, so a shape
# change on either side shows up here (final-fix-contract.md items 1 and 3).

_REAL_JS = r"""
const fs = require('fs'); const vm = require('vm');
const el = () => ({ classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } }, setAttribute() {},
  appendChild() {}, style: {}, dataset: {} });
const ctx = { console, window: { plaud: {} }, setTimeout, clearTimeout, setInterval, clearInterval,
  document: { querySelector: () => null, querySelectorAll: () => [], createElement: el, body: el() } };
vm.createContext(ctx);
for (const f of process.argv.slice(1, 3)) vm.runInContext(fs.readFileSync(f, 'utf8'), ctx);
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const r = (code) => vm.runInContext(code, ctx);
console.log(JSON.stringify({
  html: r('_coachReportHtml')(data.sidecar, null, data.segments),
  overall: r('_coachOverall')(data.sidecar.review),
}));
"""

REAL_SID = "1715000000"
REAL_SEGMENTS = [
    {"start": 0.0, "end": 30.0, "text": "Thanks for coming in. Shall we start?", "speaker": "Speaker 1"},
    {"start": 31.0, "end": 70.0, "text": "Sure. Revenue is up, and um costs are flat.", "speaker": "Speaker 2"},
    {"start": 71.0, "end": 100.0, "text": "Great. What drove the revenue?", "speaker": "Speaker 1"},
    {"start": 100.5, "end": 140.0, "text": "Mostly the new plan. People like it.", "speaker": "Speaker 2"},
    {"start": 141.0, "end": 170.0, "text": "Good. Let's keep going with that then.", "speaker": "Speaker 1"},
]
# What the model answers; ground() then keeps what matches the transcript
MODEL_REVIEW = {
    "scores": {
        "clarity": {"score": 72, "reason": "Clear on the numbers.", "evidence": [31.0, 100.5]},
        "warmth": {"score": 66, "reason": "Brisk but polite.", "evidence": [31.0]},
        "charisma": {"score": 58, "reason": "Flat delivery.", "evidence": [100.5]},
        "listening": {"score": 80, "reason": "Let them finish.", "evidence": [100.5]},
        "reception": {"score": 75, "reason": "They asked a follow-up.", "evidence": [71.0]},
    },
    "summary": "You gave the numbers clearly.",
    "next_time": ["Say why revenue moved."],
    "reactions": [{"t": 71.0, "owner_point": "Revenue", "response": "follow_up", "evidence": "Asked what drove it."}],
    "moments": [],
    "interruptions_confirmed": [],
}


@pytest.fixture
def real_sidecar(tmp_path, monkeypatch, flask_client):
    from app.coaching import review, runner, store

    coaching_dir = tmp_path / "coaching"
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    monkeypatch.setattr(store, "COACHING_DIR", coaching_dir)
    monkeypatch.setattr(runner, "TRANSCRIPTS_DIR", transcripts)
    monkeypatch.setattr(runner, "_resolve_audio", lambda sid, path: None)
    monkeypatch.setattr(review, "call_llm_json", lambda *a, **k: (json.loads(json.dumps(MODEL_REVIEW)), "test", []))
    text = " ".join(s["text"] for s in REAL_SEGMENTS)
    (transcripts / f"{REAL_SID}.json").write_text(json.dumps({"text": text, "segments": REAL_SEGMENTS}))

    runner.set_owner(REAL_SID, ["Speaker 2"], learn=False)
    res = flask_client.get(f"/coaching/{REAL_SID}")
    assert res.status_code == 200
    return res.get_json()


@pytest.fixture
def real_out(real_sidecar):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    res = subprocess.run(
        [node, "-e", _REAL_JS, str(APP / "ui" / "utils.js"), str(COACH_JS)],
        input=json.dumps({"sidecar": real_sidecar, "segments": REAL_SEGMENTS}),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


class TestRealSidecar:
    def test_backend_shape(self, real_sidecar):
        assert real_sidecar["status"] == "done"
        assert "words" not in real_sidecar
        assert set(real_sidecar["targets"]) >= {"pace_wpm", "talk_share", "interruption_rate"}
        assert real_sidecar["review"]["scores"]["clarity"]["evidence"] == [31.0, 100.5]

    def test_report_renders_owner_scores_and_targets(self, real_out):
        html = real_out["html"]
        assert "This is you: <strong>Speaker 2</strong>" in html
        assert "you chose this" in html
        for area in ("Clarity", "Warmth", "Charisma", "Listening", "Reception"):
            assert area in html
        assert real_out["overall"] == round((72 + 66 + 58 + 80 + 75) / 5)
        assert f">{real_out['overall']}<" in html
        assert 'class="coach-chip" data-coach-seek="100.5"' in html
        # Target column from the route's targets, not hard-coded in the UI
        assert "110–160 wpm" in html
        assert "at most 1 in 10 turns" in html
        assert "Asked a follow-up" in html
