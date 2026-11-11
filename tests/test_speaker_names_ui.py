"""Speaker names in the app (app/ui/*.js): transcript, summary, exports, coaching and the list.

The frontend is plain <script> files sharing one global scope, so these tests run the
real files in node against a small fake DOM. This Mac is headless and the app isn't
installed, so the tests are the only check that the UI works:

- ``applySpeakerNames`` / ``speakerNamesToLabels`` (app/ui/utils.js) must give exactly
  what app/utils/speaker_names.py gives, so "Speaker 1" never touches "Speaker 10".
- The transcript shows names (label on hover); clicking a speaker opens an inline
  editor with suggestions, Save, Clear and Cancel, Enter saving and Esc cancelling.
- Your own name, filled in by default, shows as such and can't be cleared, only replaced.
- Summaries show names; an edited summary is sent with names and kept in label form.
- Copy and every export use names.
- Coaching names the speakers in "Which one is you?", the owner line, the speaker table,
  moments and the AI's text.
- The recordings list shows participants and search matches them.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.utils import speaker_names as sn

PROJECT_ROOT = Path(__file__).parent.parent
APP = PROJECT_ROOT / "app"
UI = APP / "ui"
BRIDGE = (APP / "tauri-bridge.js").read_text()
TRANSCRIPT_JS = (UI / "transcript.js").read_text()
CSS = (APP / "css" / "transcript.css").read_text()
TOP_LEVEL = re.compile(
    r"^(?:async\s+)?(?:function\s+([A-Za-z_$][\w$]*)|(?:const|let|var|class)\s+([A-Za-z_$][\w$]*))", re.M
)


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    return node


# A minimal DOM: enough for the real files to render into and for tests to click.
_FAKE_DOM = r"""
const fs = require('fs'); const vm = require('vm');
class El {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this._html = ''; this.textContent = ''; this.value = '';
    this.dataset = {}; this.style = {}; this.attrs = {}; this.children = []; this.handlers = {};
    this.inserted = []; this.isConnected = true; this.disabled = false; this.hidden = false; this._q = new Map();
    const cls = new Set();
    this.classList = {
      add: (...c) => c.forEach((x) => cls.add(x)), remove: (...c) => c.forEach((x) => cls.delete(x)),
      toggle: (c, on) => { const w = on === undefined ? !cls.has(c) : !!on; if (w) cls.add(c); else cls.delete(c); return w; },
      contains: (c) => cls.has(c),
    };
  }
  // Setting the markup replaces the element's children, as in a browser
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this.children = []; this._q = new Map(); }
  addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); }
  removeEventListener() {}
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  // Finds an element only if the markup holds it: '.cls', '#id' or '[attr="v"]'
  querySelector(sel) {
    const key = sel.startsWith('.') ? sel.slice(1) : sel.startsWith('#') ? `id="${sel.slice(1)}"` : sel.replace(/^\[|\]$/g, '');
    if (!this.innerHTML.includes(key)) return null;
    if (!this._q.has(sel)) this._q.set(sel, new El(sel.includes('input') ? 'input' : 'div'));
    return this._q.get(sel);
  }
  querySelectorAll() { return []; }
  append(...els) { this.children.push(...els); }
  prepend(...els) { this.children.unshift(...els); }
  appendChild(el) { this.children.push(el); return el; }
  // An element put next to a detached one isn't in the document either
  insertAdjacentElement(pos, el) { this.inserted.push([pos, el]); el.isConnected = this.isConnected; return el; }
  remove() { this.isConnected = false; }
  focus() { this.focused = true; } select() {} blur() {} scrollIntoView() {}
  closest() { return null; }
}
const docEls = new Map();
const document = {
  createElement: (tag) => new El(tag),
  querySelector: (sel) => { if (!docEls.has(sel)) docEls.set(sel, new El()); return docEls.get(sel); },
  querySelectorAll: () => [],
  getElementById: () => null,
  addEventListener() {}, removeEventListener() {},
  body: new El('body'),
};
const toasts = [];
const calls = [];
const ctx = { console, document, setTimeout, clearTimeout, setInterval, clearInterval, navigator: {},
  window: { plaud: {} } };
vm.createContext(ctx);
for (const f of process.argv.slice(1)) vm.runInContext(fs.readFileSync(f, 'utf8'), ctx);
const r = (code) => vm.runInContext(code, ctx);
r('toast = (m, k) => globalThis.__toast(m, k)');
ctx.__toast = (m, k) => toasts.push([m, k]);
const settle = () => new Promise((res) => setTimeout(res, 5));
const ev = (extra = {}) => ({ preventDefault() { this.prevented = true; }, stopPropagation() {}, ...extra });
const fire = async (el, type, extra = {}) => {
  const e = ev({ target: el, ...extra });
  for (const fn of el.handlers[type] || []) await fn(e);
  if (typeof el['on' + type] === 'function') await el['on' + type](e);
  await settle();
  return e;
};
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
"""


def _run(script, files, payload):
    res = subprocess.run(
        [_node(), "-e", _FAKE_DOM + script, *[str(UI / f) for f in files]],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


# ---------------------------------------------------------------- the matching rule

NAMES = {"Speaker 1": "Sarah", "Speaker 2": "Sarah Jones", "Speaker 10": "Tom"}
APPLY_CASES = [
    # The cases in tests/test_speaker_names.py
    ("Speaker 1 and Speaker 10 met", {"Speaker 1": "Sarah"}),
    ("Speaker 12, Speaker 1.", {"Speaker 1": "Sarah"}),
    ("**Speaker 1**: hi", {"Speaker 1": "Sarah"}),
    ("Speaker 1 to Speaker 2", {"Speaker 1": "Speaker 2", "Speaker 2": "Bob"}),
    ("Speaker 1", {}),
    ("", {"Speaker 1": "Sarah"}),
    ("Speaker 1 asked Speaker 2 and Speaker 10. Speaker 12 left.", NAMES),
    # A label inside a longer one, either way round
    ("Speaker 10 then Speaker 1 then Speaker 100 and Speaker 11", {"Speaker 1": "A", "Speaker 10": "B"}),
    ("Speaker 1Speaker 1 Speaker 1 Speaker 1", {"Speaker 1": "A"}),
    # Word characters on either side, as Python's \w sees them (Unicode letters, digits, _)
    ("xSpeaker 1 Speaker 1x Speaker 1_ _Speaker 1 Speaker 1", {"Speaker 1": "A"}),
    ("éSpeaker 1 Speaker 1é 日Speaker 1 Speaker 1٣ Speaker 1² ²Speaker 1", {"Speaker 1": "A"}),
    ('(Speaker 1) [Speaker 1] Speaker 1\'s "Speaker 1" Speaker 1-2 -Speaker 1', {"Speaker 1": "A"}),
    ("😀Speaker 1 Speaker 1😀 \U0001d400Speaker 1 Speaker 1\U0001d400", {"Speaker 1": "A"}),
    ("Speaker 1\nSpeaker 2\tSpeaker 1", {"Speaker 1": "Ann", "Speaker 2": "Bo"}),
    # Case matters
    ("speaker 1 SPEAKER 1 Speaker 1", {"Speaker 1": "A"}),
    # Other label styles
    ("SPEAKER_00 and SPEAKER_001 and SPEAKER_00.", {"SPEAKER_00": "Ann"}),
    # Labels with regex characters, and names with replacement patterns
    ("a.b axb (A) [x] $1 a+b", {"a.b": "Dot", "(A)": "Paren", "[x]": "Br", "$1": "Dollar", "a+b": "Plus"}),
    ("Speaker 1 said", {"Speaker 1": "$& $1 $$ $` $'"}),
    # Empty and missing names are ignored
    ("Speaker 1 and Speaker 2", {"Speaker 1": "", "Speaker 2": None}),
    ("Speaker 1 and Speaker 2", {"Speaker 1": "", "Speaker 2": "Bo"}),
    # Names that hold other labels aren't renamed again (one pass)
    ("Speaker 1, Speaker 2", {"Speaker 1": "Speaker 2 Jr", "Speaker 2": "Speaker 1 Sr"}),
]
TO_LABELS_CASES = [
    ("Sarah asked Sarah Jones and Tom. Speaker 12 left.", NAMES),
    ("Tommy and Tom", NAMES),
    ("Ann spoke", {"Speaker 1": "Ann", "Speaker 2": "Ann"}),
    ("Ann and Bo", {"Speaker 1": "Ann", "Speaker 2": "Ann", "Speaker 3": "Bo"}),
    ("Sarah Jonesy, Sarah, Sarah Jones", NAMES),
    ("Dr. Who met Dr. Whom", {"Speaker 1": "Dr. Who"}),
    ("Jo (PM) and Jo", {"Speaker 1": "Jo (PM)", "Speaker 2": "Jo"}),
    ("Zoë and Zoëy", {"Speaker 1": "Zoë"}),
    ("", NAMES),
    ("Sarah", {}),
]

_MATCH_JS = r"""
const out = {
  apply: data.apply.map(([text, names]) => r('applySpeakerNames')(text, names)),
  toLabels: data.toLabels.map(([text, names]) => r('speakerNamesToLabels')(text, names)),
  nullText: r('applySpeakerNames')(null, { 'Speaker 1': 'A' }),
  nullNames: r('applySpeakerNames')('Speaker 1', null),
  name: [r('speakerName')('Speaker 1', { 'Speaker 1': 'A' }), r('speakerName')('Speaker 2', { 'Speaker 1': 'A' }),
    r('speakerName')('Speaker 3', null)],
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def match_out():
    return _run(_MATCH_JS, ["utils.js"], {"apply": APPLY_CASES, "toLabels": TO_LABELS_CASES})


@pytest.mark.parametrize("i", range(len(APPLY_CASES)))
def test_apply_matches_python(match_out, i):
    text, names = APPLY_CASES[i]
    assert match_out["apply"][i] == sn.apply_names(text, names)


def test_apply_whole_word_examples(match_out):
    # The review-focus case, spelled out rather than only compared with Python
    assert match_out["apply"][0] == "Sarah and Speaker 10 met"
    assert match_out["apply"][7] == "B then A then Speaker 100 and Speaker 11"


@pytest.mark.parametrize("i", range(len(TO_LABELS_CASES)))
def test_to_labels_matches_python(match_out, i):
    text, names = TO_LABELS_CASES[i]
    assert match_out["toLabels"][i] == sn.names_to_labels(text, names)


def test_missing_input_left_alone(match_out):
    assert match_out["nullText"] is None
    assert match_out["nullNames"] == "Speaker 1"
    assert match_out["name"] == ["A", "Speaker 2", "Speaker 3"]


def test_no_lookbehind():
    # WebKit on macOS 12 (the app's minimum) has no regex lookbehind: it would throw.
    for f in ("utils.js", "transcript.js", "coaching.js", "recordings.js"):
        assert "(?<" not in (UI / f).read_text(), f


# ---------------------------------------------------------------- bridge and wiring


def test_bridge_methods():
    assert re.search(
        r"setSpeakerNames: \(session_id, names\) =>\s*apiCall\('POST', '/transcripts/speaker-names', \{ session_id, names \}\)",
        BRIDGE,
    )
    assert re.search(r"people: \(\) => apiCall\('GET', '/people'\)", BRIDGE)
    assert re.search(
        r"ackSpeakerNamesCleared: \(session_id\) =>\s*apiCall\('POST', '/transcripts/speaker-names/ack-cleared', \{ session_id \}\)",
        BRIDGE,
    )


def test_top_level_names_unique_across_scripts():
    files = sorted(UI.glob("*.js")) + [APP / "renderer.js"]
    seen = {}
    for path in files:
        for name in {m.group(1) or m.group(2) for m in TOP_LEVEL.finditer(path.read_text())}:
            assert name not in seen, f"{name} declared in both {seen[name]} and {path.name}"
            seen[name] = path.name


def test_exports_and_copy_use_the_shared_builders():
    body = TRANSCRIPT_JS[
        TRANSCRIPT_JS.index("function openTranscript(") : TRANSCRIPT_JS.index("function showTranscriptTab(")
    ]
    assert "_txCopyText(t, " in body
    assert "_txExportContent(format, t, " in body
    # No export or copy builds speaker lines from raw labels any more
    assert "s.speaker ?" not in body and "seg.speaker ?" not in body


def test_editor_styles():
    for sel in (".speaker-name-editor", ".speaker-name-input", ".speaker-name-hint", ".speaker-name-error"):
        assert sel in CSS, sel
    # The speaker is a button now: no browser button chrome
    block = CSS[CSS.index("button.segment-speaker") :]
    block = block[: block.index("}")]
    assert "background: none" in block and "border: none" in block


# ---------------------------------------------------------------- transcript view

SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Morning.", "speaker": "Speaker 1"},
    {"start": 4.5, "end": 9.0, "text": "Hi <b>there</b>.", "speaker": "Speaker 2"},
    {"start": 9.5, "end": 12.0, "text": "And me.", "speaker": "Speaker 10"},
    {"start": 12.5, "end": 14.0, "text": "No label."},
]
# Speaker 1 is you (your profile name, applied by default), Speaker 2 was typed.
TRANSCRIPT = {
    "session_id": "s1",
    "text": "Morning. Hi there. And me. No label.",
    "segments": SEGMENTS,
    "speakers": ["Speaker 1", "Speaker 2", "Speaker 10"],
    "speaker_names": {"Speaker 1": "Ollie", "Speaker 2": "Sarah <Jones>"},
    "stored_speaker_names": {"Speaker 2": "Sarah <Jones>"},
    "speaker_names_cleared": False,
    "summary": "## Notes\n- Speaker 1 asked Speaker 2 about pricing.\n- Speaker 10 agreed.",
    "auto_title": "Pricing chat",
    "duration": 14,
    "language": "en",
}

_TX_JS = r"""
const app = r('app');
const T = () => JSON.parse(JSON.stringify(data.transcript));
const out = {};
let people = [{ name: 'Sarah <Jones>', recordings: 3 }, { name: 'Priya', recordings: 1 }];
let setReply = null;
let fresh = null;
ctx.window.plaud = {
  setSpeakerNames: async (sid, names) => { calls.push(['set', sid, names]); return setReply(names); },
  people: async () => { calls.push(['people']); return people; },
  ackSpeakerNamesCleared: async (sid) => { calls.push(['ack', sid]); return { saved: true }; },
  transcripts: async () => { calls.push(['transcripts']); return fresh ? [fresh] : []; },
  updateTranscript: async (sid, upd) => { calls.push(['update', sid, upd]); return { saved: true }; },
};
const rec = { session_id: 's1', title: 'Chat', participants: ['Ollie', 'Sarah <Jones>'] };
app.recordings = [rec];
Object.assign(app, { currentView: 'transcript', currentSessionId: 's1', activeTab: 'transcript' });

// A segment row and its speaker button, as the click handler finds them
const row = new El();
const speakerBtn = (label) => ({ dataset: { label }, closest: (s) => (s === '.segment' ? row : null) });
const clickSpeaker = (container, label) =>
  fire(container, 'click', { target: { closest: (s) => (s === '.segment-speaker' ? speakerBtn(label) : null) } });
const editorOf = () => row.inserted.length ? row.inserted[row.inserted.length - 1][1] : null;
const actionClick = (editor, action) =>
  fire(editor, 'click', { target: { closest: (s) => (s === '[data-speaker-action]' ? { dataset: { speakerAction: action }, disabled: false } : null) } });

(async () => {
  // 1. Segments: names shown, label in the title, escaped; unnamed labels as they are
  let t = T();
  app.transcripts.s1 = t;
  const c = new El();
  r('renderTranscriptSegments')(c, t, 's1');
  out.segments = c.innerHTML;

  // 2. Click Speaker 2 (a typed name): editor with the name, suggestions, Clear enabled
  await clickSpeaker(c, 'Speaker 2');
  let ed = editorOf();
  out.editorTyped = ed.innerHTML;
  out.editorAfterRow = row.inserted[0][0];
  out.datalist = ed.querySelector('.speaker-name-list').innerHTML;
  out.peopleCalls = calls.filter((x) => x[0] === 'people').length;
  out.inputFocused = !!ed.querySelector('.speaker-name-input').focused;
  out.editorOpen = r('speakerNameEditorOpen')();

  // 3. Esc cancels: no request, editor gone
  calls.length = 0;
  const escEv = await fire(ed.querySelector('.speaker-name-input'), 'keydown', { key: 'Escape' });
  out.escCalls = calls.slice();
  out.escClosed = !ed.isConnected && !r('speakerNameEditorOpen')();
  out.escPrevented = !!escEv.prevented;

  // 4. Type a new name, Enter saves {Speaker 2: name}; segments re-render with it
  setReply = (names) => ({ speaker_names: { 'Speaker 2': names['Speaker 2'] } });
  await clickSpeaker(c, 'Speaker 2');
  ed = editorOf();
  ed.querySelector('.speaker-name-input').value = '  Priya  ';
  calls.length = 0;
  await fire(ed.querySelector('.speaker-name-input'), 'keydown', { key: 'Enter' });
  out.enterCalls = calls.slice();
  out.afterEnter = c.innerHTML;
  out.afterEnterNames = t.speaker_names;
  out.afterEnterStored = t.stored_speaker_names;
  out.afterEnterParticipants = rec.participants;
  out.enterClosed = !ed.isConnected;

  // 5. A rejected name: the server's message shows in the editor, which stays open
  setReply = () => ({ error: "A speaker name can't look like a speaker label, such as Speaker 2" });
  await clickSpeaker(c, 'Speaker 10');
  ed = editorOf();
  ed.querySelector('.speaker-name-input').value = 'Speaker 3';
  await actionClick(ed, 'save');
  out.errorText = ed.querySelector('.speaker-name-error').textContent;
  out.errorHidden = ed.querySelector('.speaker-name-error').hidden;
  out.errorStillOpen = ed.isConnected && r('speakerNameEditorOpen')();
  out.errorNamesUnchanged = t.speaker_names;
  await actionClick(ed, 'cancel');
  out.cancelClosed = !ed.isConnected;

  // 6. Your own name (default, not stored): shown as yours, Clear disabled
  await clickSpeaker(c, 'Speaker 1');
  ed = editorOf();
  out.editorDefault = ed.innerHTML;
  // Saving it unchanged sends nothing
  calls.length = 0;
  await actionClick(ed, 'save');
  out.defaultUnchangedCalls = calls.filter((x) => x[0] === 'set');
  // Clear on a default does nothing either
  await clickSpeaker(c, 'Speaker 1');
  ed = editorOf();
  calls.length = 0;
  await actionClick(ed, 'clear');
  out.defaultClearCalls = calls.filter((x) => x[0] === 'set');
  // Emptying the box on your default name says how to change it, rather than doing nothing
  await clickSpeaker(c, 'Speaker 1');
  ed = editorOf();
  ed.querySelector('.speaker-name-input').value = '  ';
  calls.length = 0;
  await actionClick(ed, 'save');
  out.defaultEmptyCalls = calls.filter((x) => x[0] === 'set');
  out.defaultEmptyMessage = ed.querySelector('.speaker-name-error').textContent;
  out.defaultEmptyShown = ed.querySelector('.speaker-name-error').hidden === false;
  out.defaultEmptyOpen = ed.isConnected && r('speakerNameEditorOpen')();
  await actionClick(ed, 'cancel');
  // Typing another name wins
  setReply = (names) => ({ speaker_names: { 'Speaker 1': names['Speaker 1'], 'Speaker 2': 'Priya' } });
  await clickSpeaker(c, 'Speaker 1');
  ed = editorOf();
  ed.querySelector('.speaker-name-input').value = 'Oliver';
  calls.length = 0;
  await actionClick(ed, 'save');
  out.overrideCalls = calls.filter((x) => x[0] === 'set');
  out.overrideNames = t.speaker_names;

  // 7. Clear a typed name: sends null, then re-reads the names (your default may be under it)
  setReply = () => ({ speaker_names: { 'Speaker 2': 'Priya' } });
  fresh = { ...T(), speaker_names: { 'Speaker 1': 'Ollie', 'Speaker 2': 'Priya' }, stored_speaker_names: { 'Speaker 2': 'Priya' } };
  await clickSpeaker(c, 'Speaker 1');
  ed = editorOf();
  out.editorStoredOverDefault = ed.innerHTML;
  calls.length = 0;
  await actionClick(ed, 'clear');
  out.clearCalls = calls.slice();
  out.clearedNames = t.speaker_names;
  out.clearedStored = t.stored_speaker_names;
  out.afterClear = c.innerHTML;
  out.clearedParticipants = rec.participants;

  // 8. An empty name on a typed one clears it too
  setReply = () => ({ speaker_names: {} });
  fresh = { ...T(), speaker_names: { 'Speaker 1': 'Ollie' }, stored_speaker_names: {} };
  await clickSpeaker(c, 'Speaker 2');
  ed = editorOf();
  ed.querySelector('.speaker-name-input').value = '   ';
  calls.length = 0;
  await fire(ed.querySelector('.speaker-name-input'), 'keydown', { key: 'Enter' });
  out.emptyCalls = calls.filter((x) => x[0] === 'set');

  // 9. A failed request (network) keeps the editor open with a message
  ctx.window.plaud.setSpeakerNames = async () => { throw new Error('offline'); };
  await clickSpeaker(c, 'Speaker 10');
  ed = editorOf();
  ed.querySelector('.speaker-name-input').value = 'Tom';
  await actionClick(ed, 'save');
  out.networkError = ed.querySelector('.speaker-name-error').textContent;
  out.networkStillOpen = ed.isConnected;
  await actionClick(ed, 'cancel');

  // 10. Opening another speaker closes the first editor
  await clickSpeaker(c, 'Speaker 1');
  const first = editorOf();
  await clickSpeaker(c, 'Speaker 2');
  out.oneEditorAtATime = !first.isConnected && editorOf().isConnected;
  r('_txCloseSpeakerEditor')();

  // 11. Summary: names shown; the editor shows names; saving sends the text and keeps labels locally
  t = T();
  app.transcripts.s1 = t;
  const sc = new El();
  r('renderEditableSummary')(sc, t, 's1', rec);
  out.summaryHtml = sc.innerHTML;
  const editBtn = sc.children.find((x) => x.className === 'summary-edit-btn');
  await fire(editBtn, 'click');
  const textarea = sc.children.find((x) => x.tagName === 'TEXTAREA');
  out.editorText = textarea.value;
  out.editorHint = sc.children.filter((x) => x.className === 'summary-editor-hint').map((x) => x.textContent);
  const actions = sc.children.find((x) => x.className === 'summary-editor-actions');
  const saveBtn = actions.children.find((x) => x.textContent === 'Save');
  // Unchanged: nothing sent
  calls.length = 0;
  await fire(saveBtn, 'click');
  out.unchangedSummaryCalls = calls.slice();
  await fire(sc.children.find((x) => x.className === 'summary-edit-btn'), 'click');
  const ta2 = sc.children.find((x) => x.tagName === 'TEXTAREA');
  ta2.value = ta2.value.replace('agreed', 'agreed with Sarah <Jones>');
  calls.length = 0;
  const save2 = sc.children.find((x) => x.className === 'summary-editor-actions').children.find((x) => x.textContent === 'Save');
  await fire(save2, 'click');
  out.savedSummaryCalls = calls.slice();
  out.localSummary = t.summary;

  // 12. Copy and exports
  t = T();
  out.copyTranscript = r('_txCopyText')(t, false);
  out.copySummary = r('_txCopyText')(t, true);
  out.exports = {};
  for (const f of ['md', 'txt', 'json', 'pdf']) {
    out.exports[f] = r('_txExportContent')(f, t, 'Chat', 'Chat', false);
    out.exports[f + 'Sum'] = r('_txExportContent')(f, t, 'Chat', 'Chat', true);
  }
  const plain = { ...T(), segments: [], speaker_names: {} };
  out.exportPlain = r('_txExportContent')('txt', plain, 'Chat', 'Chat', false);

  // 13. The once-only "names were cleared" notice
  calls.length = 0; toasts.length = 0;
  t = { ...T(), speaker_names_cleared: true };
  app.transcripts.s1 = t;
  r('_txNoticeNamesCleared')('s1', t);
  r('_txNoticeNamesCleared')('s1', t);
  await settle();
  out.clearedToasts = toasts.slice();
  out.clearedAcks = calls.filter((x) => x[0] === 'ack');
  out.clearedFlag = t.speaker_names_cleared;

  // 14. refreshSpeakerNames updates the cached transcript in place
  t = T();
  app.transcripts.s1 = t;
  fresh = { ...T(), speaker_names: { 'Speaker 2': 'Bo' }, stored_speaker_names: { 'Speaker 2': 'Bo' } };
  out.refreshed = await r('refreshSpeakerNames')('s1');
  out.refreshedNames = app.transcripts.s1.speaker_names;
  out.refreshedSame = app.transcripts.s1 === t;
  out.refreshedParticipants = rec.participants;

  // 15. Picking yourself in Coaching re-reads the names (your profile name now applies)
  // before the report is drawn, so the report and the transcript both show it
  t = { ...T(), speaker_names: { 'Speaker 2': 'Bo' }, stored_speaker_names: { 'Speaker 2': 'Bo' } };
  app.transcripts.s1 = t;
  fresh = { ...T(), speaker_names: { 'Speaker 1': 'Ollie', 'Speaker 2': 'Bo' }, stored_speaker_names: { 'Speaker 2': 'Bo' } };
  const sidecar = { status: 'done', owner: { labels: ['Speaker 2'], source: 'voice' }, targets: {} };
  ctx.window.plaud.coachingSetOwner = async () => ({ ...sidecar, owner: { labels: ['Speaker 1'], source: 'user' } });
  ctx.window.plaud.coachingProgress = async () => ({ baseline: null });
  app.activeTab = 'coaching';
  const cc = new El();
  cc.querySelectorAll = (sel) => (sel.includes('coach-owner') ? [{ value: 'Speaker 1' }] : []);
  cc.querySelector = (sel) => (sel.includes('coach-mixed') ? { checked: false } : null);
  calls.length = 0;
  await r('_coachSaveOwner')(cc, 's1', sidecar, new El());
  out.ownerPickCalls = calls.map((x) => x[0]);
  out.ownerPickNames = app.transcripts.s1.speaker_names;
  out.ownerPickSame = app.transcripts.s1 === t;
  out.ownerPickDrawn = cc.innerHTML;

  // 16. Two speakers sharing a name (one voice split in two): the summary editor keeps
  // their labels, since a shared name can't be turned back into a label on save
  app.activeTab = 'summary';
  t = { ...T(), speaker_names: { 'Speaker 1': 'Sarah', 'Speaker 2': 'Sarah', 'Speaker 10': 'Tom' },
    stored_speaker_names: { 'Speaker 1': 'Sarah', 'Speaker 2': 'Sarah', 'Speaker 10': 'Tom' },
    summary: 'Speaker 1 asked Speaker 2; Speaker 10 agreed.' };
  app.transcripts.s1 = t;
  const sh = new El();
  r('renderEditableSummary')(sh, t, 's1', rec);
  out.sharedView = sh.innerHTML;
  await fire(sh.children.find((x) => x.className === 'summary-edit-btn'), 'click');
  const ta3 = sh.children.find((x) => x.tagName === 'TEXTAREA');
  out.sharedEditorText = ta3.value;
  ta3.value += ' Done.';
  calls.length = 0;
  await fire(sh.children.find((x) => x.className === 'summary-editor-actions').children.find((x) => x.textContent === 'Save'), 'click');
  out.sharedSaveCalls = calls.slice();
  out.sharedLocalSummary = t.summary;

  // 17. Background redraws (re-download, restore, auto-processing, speaker detection)
  // wait while the name editor is open, then run once when it closes
  const opened = [];
  ctx.__opened = opened;
  r('openTranscript = (sid) => globalThis.__opened.push(sid)');
  Object.assign(app, { currentView: 'transcript', currentSessionId: 's1', activeTab: 'transcript' });
  t = T();
  app.transcripts.s1 = t;
  const c2 = new El();
  r('renderTranscriptSegments')(c2, t, 's1');
  await clickSpeaker(c2, 'Speaker 2');
  r('_refreshOpenTranscript')('s1');
  r('redrawOpenTranscript')('s1');
  out.redrawWhileEditing = opened.slice();
  out.editorSurvived = r('speakerNameEditorOpen')();
  await actionClick(editorOf(), 'cancel');
  out.redrawAfterClose = opened.slice();
  r('_refreshOpenTranscript')('s1');
  r('redrawOpenTranscript')('s2');
  out.redrawNoEditor = opened.slice();

  // 18. Switching the editor from one speaker to another while a redraw is held: the
  // redraw (which re-renders the rows) waits for the last editor to close, so the new
  // editor goes in next to a row that's still in the document
  opened.length = 0;
  r('openTranscript = (sid) => { globalThis.__opened.push(sid); globalThis.__row.isConnected = false; }');
  ctx.__row = row;
  await clickSpeaker(c2, 'Speaker 1');
  r('redrawOpenTranscript')('s1');
  await clickSpeaker(c2, 'Speaker 2');
  const edB = editorOf();
  out.switchRedraws = opened.slice();
  out.switchEditorAttached = edB.isConnected && r('speakerNameEditorOpen')();
  out.switchLabel = edB.innerHTML.includes('<strong>Speaker 2</strong>');
  await fire(edB.querySelector('.speaker-name-input'), 'keydown', { key: 'Escape' });
  out.switchRedrawAfterClose = opened.slice();
  row.isConnected = true;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def tx():
    return _run(_TX_JS, ["utils.js", "recordings.js", "transcript.js", "coaching.js"], {"transcript": TRANSCRIPT})


class TestSegments:
    def test_names_shown_with_label_on_hover(self, tx):
        html = tx["segments"]
        assert re.search(r'class="segment-speaker[^"]*"[^>]*data-label="Speaker 1"[^>]*>Ollie<', html)
        assert re.search(r'data-label="Speaker 2"[^>]*title="Sarah &lt;Jones&gt; \(Speaker 2\)[^"]*"', html)
        assert ">Sarah &lt;Jones&gt;<" in html
        # Unnamed: the label, which the title invites you to name
        assert re.search(r'data-label="Speaker 10"[^>]*title="Speaker 10[^"]*name[^"]*"[^>]*>Speaker 10<', html)
        # A line without a speaker has no button
        assert html.count("segment-speaker") == 3

    def test_speaker_is_a_button(self, tx):
        assert '<button type="button" class="segment-speaker' in tx["segments"]


class TestNameEditor:
    def test_opens_after_the_row_with_the_typed_name(self, tx):
        html = tx["editorTyped"]
        assert tx["editorAfterRow"] == "afterend"
        assert "speaker-name-editor" not in html  # the class is on the element itself
        assert 'value="Sarah &lt;Jones&gt;"' in html
        assert "Speaker 2" in html
        for action in ("save", "clear", "cancel"):
            assert f'data-speaker-action="{action}"' in html
        clear = re.search(r'<button[^>]*data-speaker-action="clear"[^>]*>', html).group(0)
        assert "disabled" not in clear
        assert "<datalist" not in html and 'role="combobox"' in html and 'role="listbox"' in html
        assert 'maxlength="80"' in html
        assert tx["inputFocused"]
        assert tx["editorOpen"] is True

    def test_suggestions_from_people(self, tx):
        assert tx["peopleCalls"] == 1
        assert 'data-pick="Sarah &lt;Jones&gt;"' in tx["datalist"]
        assert 'data-pick="Priya"' in tx["datalist"]

    def test_escape_cancels(self, tx):
        assert tx["escCalls"] == []
        assert tx["escClosed"]
        assert tx["escPrevented"]

    def test_enter_saves_trimmed_name(self, tx):
        assert tx["enterCalls"] == [["set", "s1", {"Speaker 2": "Priya"}]]
        assert tx["enterClosed"]
        assert ">Priya<" in tx["afterEnter"]
        assert tx["afterEnterStored"] == {"Speaker 2": "Priya"}
        # Your default name stays on Speaker 1, in order of appearance
        assert tx["afterEnterNames"] == {"Speaker 1": "Ollie", "Speaker 2": "Priya"}
        assert tx["afterEnterParticipants"] == ["Ollie", "Priya"]

    def test_server_error_shown_in_editor(self, tx):
        assert tx["errorText"] == "A speaker name can't look like a speaker label, such as Speaker 2"
        assert tx["errorHidden"] is False
        assert tx["errorStillOpen"]
        assert tx["errorNamesUnchanged"] == {"Speaker 1": "Ollie", "Speaker 2": "Priya"}
        assert tx["cancelClosed"]

    def test_network_error_shown_in_editor(self, tx):
        assert "offline" in tx["networkError"]
        assert tx["networkStillOpen"]

    def test_default_name_is_marked_as_yours_and_cannot_be_cleared(self, tx):
        html = tx["editorDefault"]
        assert 'value="Ollie"' in html
        assert "your name" in html.lower()
        assert "Coaching" in html
        clear = re.search(r'<button[^>]*data-speaker-action="clear"[^>]*>', html).group(0)
        assert "disabled" in clear
        assert tx["defaultUnchangedCalls"] == []
        assert tx["defaultClearCalls"] == []
        # The hint says how to change it: in Coaching, or by typing another name
        hint = re.search(r'<p class="speaker-name-hint">(.*?)</p>', html, re.S).group(1)
        assert "recognised your voice" in hint and "Coaching" in hint and "type another name" in hint.lower()

    def test_emptying_your_default_name_explains_instead_of_doing_nothing(self, tx):
        assert tx["defaultEmptyCalls"] == []
        assert tx["defaultEmptyOpen"]
        assert tx["defaultEmptyShown"]
        msg = tx["defaultEmptyMessage"]
        assert "Coaching" in msg and "type another name" in msg.lower()

    def test_typed_name_wins_over_default(self, tx):
        assert tx["overrideCalls"] == [["set", "s1", {"Speaker 1": "Oliver"}]]
        assert tx["overrideNames"]["Speaker 1"] == "Oliver"

    def test_clear_sends_null_and_rereads_names(self, tx):
        html = tx["editorStoredOverDefault"]
        clear = re.search(r'<button[^>]*data-speaker-action="clear"[^>]*>', html).group(0)
        assert "disabled" not in clear
        assert ["set", "s1", {"Speaker 1": None}] in tx["clearCalls"]
        assert ["transcripts"] in tx["clearCalls"]
        # Your profile name comes back from the backend once the typed one is gone
        assert tx["clearedNames"] == {"Speaker 1": "Ollie", "Speaker 2": "Priya"}
        assert tx["clearedStored"] == {"Speaker 2": "Priya"}
        assert ">Ollie<" in tx["afterClear"]
        assert tx["clearedParticipants"] == ["Ollie", "Priya"]

    def test_empty_name_clears_a_typed_one(self, tx):
        assert tx["emptyCalls"] == [["set", "s1", {"Speaker 2": None}]]

    def test_one_editor_at_a_time(self, tx):
        assert tx["oneEditorAtATime"]


class TestSummary:
    def test_shown_with_names(self, tx):
        html = tx["summaryHtml"]
        assert "Ollie asked Sarah &lt;Jones&gt; about pricing." in html
        assert "Speaker 10 agreed." in html
        assert "Speaker 1 " not in html

    def test_editor_shows_names(self, tx):
        assert tx["editorText"] == "## Notes\n- Ollie asked Sarah <Jones> about pricing.\n- Speaker 10 agreed."
        assert tx["editorHint"] and "label" in tx["editorHint"][0]

    def test_unchanged_save_sends_nothing(self, tx):
        assert tx["unchangedSummaryCalls"] == []

    def test_save_sends_names_and_keeps_labels_locally(self, tx):
        sent = "## Notes\n- Ollie asked Sarah <Jones> about pricing.\n- Speaker 10 agreed with Sarah <Jones>."
        names = TRANSCRIPT["speaker_names"]
        # With the exact names the editor showed, so the backend reverses with those
        # even if an AI tool changed the names while the editor was open
        assert tx["savedSummaryCalls"] == [["update", "s1", {"summary": sent, "summary_names": names}]]
        assert tx["localSummary"] == sn.names_to_labels(sent, names)
        assert tx["localSummary"] == (
            "## Notes\n- Speaker 1 asked Speaker 2 about pricing.\n- Speaker 10 agreed with Speaker 2."
        )


class TestCopyAndExports:
    def test_copy(self, tx):
        lines = tx["copyTranscript"].split("\n")
        assert lines[0] == "[00:00] [Ollie] Morning."
        assert lines[1] == "[00:04] [Sarah <Jones>] Hi <b>there</b>."
        assert lines[2] == "[00:09] [Speaker 10] And me."
        assert lines[3] == "[00:12] No label."
        assert tx["copySummary"].startswith("## Notes\n- Ollie asked Sarah <Jones>")

    def test_markdown(self, tx):
        md = tx["exports"]["md"]
        assert md["filename"] == "Chat.md" and md["mimeType"] == "text/markdown"
        assert "**Ollie**: Morning." in md["content"]
        assert "**Speaker 10**: And me." in md["content"]
        assert "Ollie asked Sarah <Jones>" in tx["exports"]["mdSum"]["content"]

    def test_text(self, tx):
        txt = tx["exports"]["txt"]["content"]
        assert "[00:04] [Sarah <Jones>] Hi <b>there</b>." in txt
        assert "Ollie asked Sarah <Jones>" in tx["exports"]["txtSum"]["content"]
        assert tx["exportPlain"]["content"] == TRANSCRIPT["text"]

    def test_json_keeps_labels_and_adds_names(self, tx):
        body = json.loads(tx["exports"]["json"]["content"])
        assert body["speaker_names"] == TRANSCRIPT["speaker_names"]
        assert body["segments"][0] == {**SEGMENTS[0], "speaker_name": "Ollie"}
        assert body["segments"][2] == SEGMENTS[2]
        assert body["segments"][3] == SEGMENTS[3]
        summary = json.loads(tx["exports"]["jsonSum"]["content"])
        assert summary["summary"].startswith("## Notes\n- Ollie asked Sarah <Jones>")
        assert summary["segments"][1]["speaker_name"] == "Sarah <Jones>"

    def test_pdf_escaped(self, tx):
        html = tx["exports"]["pdf"]["html"]
        assert "<strong>Sarah &lt;Jones&gt;:</strong> Hi &lt;b&gt;there&lt;/b&gt;." in html
        assert "<strong>Ollie:</strong>" in html
        assert "Ollie asked Sarah &lt;Jones&gt;" in tx["exports"]["pdfSum"]["html"]


class TestNamesClearedNotice:
    def test_says_so_once_and_acks(self, tx):
        assert tx["clearedToasts"] == [["Speaker names were cleared because the speakers were detected again", "info"]]
        assert tx["clearedAcks"] == [["ack", "s1"]]
        assert tx["clearedFlag"] is False

    def test_called_when_opening_a_transcript(self):
        body = TRANSCRIPT_JS[
            TRANSCRIPT_JS.index("function openTranscript(") : TRANSCRIPT_JS.index("function showTranscriptTab(")
        ]
        assert "_txNoticeNamesCleared(sessionId, t)" in body


def test_refresh_updates_cached_transcript_in_place(tx):
    assert tx["refreshed"] is True
    assert tx["refreshedSame"]
    assert tx["refreshedNames"] == {"Speaker 2": "Bo"}
    assert tx["refreshedParticipants"] == ["Bo"]


def test_shared_name_keeps_labels_in_summary_editor(tx):
    # Shown with the shared name, which only reads oddly
    assert "Sarah asked Sarah; Tom agreed." in tx["sharedView"]
    # Edited with the labels kept, so the save can't lose them
    assert tx["sharedEditorText"] == "Speaker 1 asked Speaker 2; Tom agreed."
    sent = "Speaker 1 asked Speaker 2; Tom agreed. Done."
    # Only the names the editor showed go with it
    assert tx["sharedSaveCalls"] == [["update", "s1", {"summary": sent, "summary_names": {"Speaker 10": "Tom"}}]]
    names = {"Speaker 1": "Sarah", "Speaker 2": "Sarah", "Speaker 10": "Tom"}
    stored = "Speaker 1 asked Speaker 2; Speaker 10 agreed. Done."
    assert tx["sharedLocalSummary"] == stored
    # The backend stores the same
    assert sn.names_to_labels(sent, names) == stored
    assert sn.names_to_labels(sent, {"Speaker 10": "Tom"}) == stored


def test_background_redraw_waits_for_the_name_editor(tx):
    assert tx["redrawWhileEditing"] == []
    assert tx["editorSurvived"] is True
    assert tx["redrawAfterClose"] == ["s1"]
    # With no editor open it redraws at once, and only the recording on screen
    assert tx["redrawNoEditor"] == ["s1", "s1"]


def test_switching_speakers_keeps_the_redraw_held(tx):
    assert tx["switchRedraws"] == []
    assert tx["switchEditorAttached"] is True
    assert tx["switchLabel"]
    # Runs once, when the last editor closes
    assert tx["switchRedrawAfterClose"] == ["s1"]


def test_background_redraws_go_through_redraw_open_transcript():
    rec_js = (UI / "recordings.js").read_text()
    auto = rec_js[rec_js.index("while (_autoProcessQueue.length > 0)") :]
    auto = auto[: auto.index("\n}\n")]
    assert "redrawOpenTranscript(item.sid)" in auto and "openTranscript(item.sid)" not in auto.replace(
        "redrawOpenTranscript(item.sid)", ""
    )
    refresh = rec_js[rec_js.index("function _refreshOpenTranscript(") :]
    refresh = refresh[: refresh.index("\n}\n")]
    assert "redrawOpenTranscript(sessionId)" in refresh
    diar = TRANSCRIPT_JS[TRANSCRIPT_JS.index("_diarPollHandleTx = setInterval(") :]
    diar = diar[: diar.index("}, 5000);")]
    assert "redrawOpenTranscript(sessionId)" in diar and "  openTranscript(" not in diar


def test_owner_pick_in_coaching_rereads_names(tx):
    assert "transcripts" in tx["ownerPickCalls"]
    assert tx["ownerPickNames"] == {"Speaker 1": "Ollie", "Speaker 2": "Bo"}
    assert tx["ownerPickSame"]
    owner = re.search(r'<div class="coach-owner">(.*?)</div>', tx["ownerPickDrawn"], re.S).group(1)
    assert "Ollie" in owner and "you chose this" in owner


# ---------------------------------------------------------------- coaching

COACH_SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Morning.", "speaker": "Speaker 1"},
    {"start": 4.5, "end": 9.0, "text": "Hi.", "speaker": "Speaker 2"},
    {"start": 9.5, "end": 12.0, "text": "Me too.", "speaker": "Speaker 3"},
]
COACH_NAMES = {"Speaker 1": "Ollie", "Speaker 2": "Sarah <Jones>"}
COACH_DONE = {
    "status": "done",
    "owner": {"labels": ["Speaker 1"], "source": "user", "confidence": 1.0},
    "metrics": {
        "per_speaker": {
            "Speaker 1": {"wpm": 150, "talk_share": 0.5, "turns": 3},
            "Speaker 2": {"wpm": 120, "talk_share": 0.3, "turns": 2},
            "Speaker 3": {"wpm": 110, "talk_share": 0.2, "turns": 1},
        },
        "owner": {"pace_wpm": 150},
        "availability": {},
        "moments": [{"t": 4.5, "kind": "interruption", "speaker": "Speaker 2", "quote": "Hi."}],
    },
    "review": {
        "scores": {"clarity": {"score": 70, "reason": "Clear with Speaker 2.", "evidence": [0.0]}},
        "summary": "Speaker 2 warmed up; Speaker 3 stayed quiet.",
        "next_time": ["Ask Speaker 2 first."],
        "reactions": [
            {
                "t": 4.5,
                "owner_point": "Pricing for Speaker 2",
                "response": "engaged",
                "evidence": "Speaker 2 asked more.",
            }
        ],
        "moments": [{"t": 9.5, "kind": "strong question", "note": "Speaker 3 asked."}],
    },
    "targets": {},
}
COACH_NEEDS_OWNER = {
    "status": "needs_owner",
    "candidates": [
        {"label": "Speaker 1", "first_line": "Morning.", "loud": True},
        {"label": "Speaker 2", "first_line": "Hi.", "loud": False},
    ],
    "targets": {},
}

_COACH_JS = r"""
const app = r('app');
app.settings = {};
const out = {};
out.done = r('_coachReportHtml')(data.done, null, data.segments, data.names);
out.needsOwner = r('_coachReportHtml')(data.needsOwner, null, data.segments, data.names);
out.noNames = r('_coachReportHtml')(data.done, null, data.segments);
// The tab passes the open transcript's names
app.transcripts.s1 = { segments: data.segments, speaker_names: data.names };
app.currentSessionId = 's1';
const tab = new El();
r('_coachDraw')(tab, 's1', data.needsOwner, null);
out.drawn = tab.innerHTML;
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def coach():
    payload = {"done": COACH_DONE, "needsOwner": COACH_NEEDS_OWNER, "segments": COACH_SEGMENTS, "names": COACH_NAMES}
    return _run(_COACH_JS, ["utils.js", "coaching.js"], payload)


class TestCoaching:
    def test_which_one_is_you_candidates_show_names(self, coach):
        html = coach["needsOwner"]
        assert "Which one is you?" in html
        rows = re.findall(r'<label class="coach-picker-row">(.*?)</label>', html, re.S)
        assert len(rows) == 3
        assert 'value="Speaker 1"' in rows[0] and "Ollie" in rows[0] and "Speaker 1" in rows[0]
        assert 'value="Speaker 2"' in rows[1] and "Sarah &lt;Jones&gt;" in rows[1]
        # An unnamed speaker keeps its label
        assert 'value="Speaker 3"' in rows[2] and "Speaker 3" in rows[2]
        assert coach["drawn"] == html

    def test_owner_line_names_you(self, coach):
        owner = re.search(r'<div class="coach-owner">(.*?)</div>', coach["done"], re.S).group(1)
        assert "This is you:" in owner and "Ollie" in owner

    def test_speaker_table_names(self, coach):
        table = coach["done"][coach["done"].index("By speaker") :]
        assert "Sarah &lt;Jones&gt;" in table
        assert re.search(r"<th scope=\"row\">Ollie.*?<span class=\"coach-you\">you</span>", table, re.S)

    def test_moments_and_reactions_use_names(self, coach):
        html = coach["done"]
        assert '<span class="coach-row-speaker">Sarah &lt;Jones&gt;</span>' in html
        assert "Sarah &lt;Jones&gt; asked more." in html
        assert "Pricing for Sarah &lt;Jones&gt;" in html
        assert "Sarah &lt;Jones&gt; warmed up; Speaker 3 stayed quiet." in html
        assert "Ask Sarah &lt;Jones&gt; first." in html
        assert "Clear with Sarah &lt;Jones&gt;." in html
        assert "Speaker 3 asked." in html
        assert "Speaker 2" not in re.sub(r'(value|title)="[^"]*"', "", html.replace('coach-label">Speaker 2', ""))

    def test_without_names_labels_stay(self, coach):
        html = coach["noNames"]
        assert '<span class="coach-row-speaker">Speaker 2</span>' in html
        assert "Speaker 2 warmed up" in html


# ---------------------------------------------------------------- recordings list

_LIST_JS = r"""
const app = r('app');
const dom = r('dom');
app.settings = {};
app.recordings = data.recordings;
app.transcripts = data.transcripts;
const out = {};
const ids = () => r('getFilteredRecordings')().map((x) => x.session_id);
for (const q of ['sarah', 'JONES', 'ollie', 'speaker 2', 'nobody']) {
  app.searchQuery = q;
  out[q] = ids();
}
app.searchQuery = '';
r('renderRecordingsList')();
out.list = dom.recordingsList.innerHTML;
console.log(JSON.stringify(out));
"""

LIST_RECORDINGS = [
    {
        "session_id": "a1",
        "date": "2026-11-01T10:00:00",
        "duration": 600,
        "has_transcript": True,
        "participants": ["Ollie", "Sarah <Jones>"],
        "tags": [],
    },
    {"session_id": "b2", "date": "2026-11-02T10:00:00", "duration": 60, "has_transcript": True, "participants": []},
]
LIST_TRANSCRIPTS = {
    "a1": {
        "session_id": "a1",
        "text": "Hello.",
        "text_preview": "Hello.",
        "speakers": ["Speaker 1", "Speaker 2"],
        "speaker_names": {"Speaker 1": "Ollie", "Speaker 2": "Sarah <Jones>"},
    },
    "b2": {"session_id": "b2", "text": "Speaker 2 said hi.", "text_preview": "x", "speakers": [], "speaker_names": {}},
}


@pytest.fixture(scope="module")
def listing():
    return _run(
        _LIST_JS, ["utils.js", "recordings.js"], {"recordings": LIST_RECORDINGS, "transcripts": LIST_TRANSCRIPTS}
    )


class TestRecordingsList:
    def test_search_matches_participants(self, listing):
        assert listing["sarah"] == ["a1"]
        assert listing["JONES"] == ["a1"]
        assert listing["ollie"] == ["a1"]
        # Labels in the text still match
        assert listing["speaker 2"] == ["b2"]
        assert listing["nobody"] == []

    def test_row_shows_participants(self, listing):
        html = listing["list"]
        assert re.search(
            r'class="recording-participants" title="With Ollie, Sarah &lt;Jones&gt;">Ollie, Sarah &lt;Jones&gt;<', html
        )
        # A recording without names shows none
        b2 = html[html.index('data-sid="b2"') :]
        assert "recording-participants" not in b2


# ---------------------------------------------------------------- picker, Did you mean, voice hint

_PICK_JS = r"""
const app = r('app');
const out = {};
const people = [
  { name: 'Harry Fisher', recordings: 9, person_id: 'p1', has_voice: true },
  { name: 'William Moss', recordings: 5, person_id: 'p2', has_voice: true },
  { name: 'Priya Shah', recordings: 4, person_id: null, has_voice: false },
  { name: 'Will Moran', recordings: 1, person_id: null, has_voice: false },
];
out.lev = [r('_txPickLevenshtein')('kitten', 'sitting'), r('_txPickLevenshtein')('', 'ab'), r('_txPickLevenshtein')('Priya', 'priya')];
const dym = (typed, known = people) => r('_txPickDidYouMean')(typed, known);
out.dym = {
  firstWord: dym('will'), firstWordTwo: dym('Will', people.slice(0, 2)),
  typo: dym('Priya Shaw'), exactCase: dym('priya shah'), none: dym('Zed'),
  ties: dym('Harry Fishe', [{ name: 'Harry Fisher', recordings: 1 }, { name: 'Harry Fishers', recordings: 7 }]),
};
out.rows = {
  all: r('_txPickRowsHtml')(people, '', -1),
  will: r('_txPickRowsHtml')(people, 'will', 0),
  exact: r('_txPickRowsHtml')(people, 'PRIYA SHAH', -1),
  evil: r('_txPickRowsHtml')([{ name: 'A"<b>', recordings: 1 }], '<b', -1),
  many: r('_txPickRowsHtml')(Array.from({ length: 20 }, (_, i) => ({ name: 'P' + i, recordings: 1 })), '', -1),
};
const T = () => ({
  session_id: 's1', segments: [{ start: 0, text: 'a', speaker: 'Speaker 1' }, { start: 1, text: 'b', speaker: 'Speaker 2' }],
  speaker_names: { 'Speaker 2': 'Harry Fisher' }, stored_speaker_names: {},
  speaker_name_sources: { 'Speaker 2': { source: 'voice', score: 0.8 } },
});
out.button = r('_txSpeakerButtonHtml')('Speaker 2', T().speaker_names, T().speaker_name_sources);
out.buttonTyped = r('_txSpeakerButtonHtml')('Speaker 2', { 'Speaker 2': 'X' }, {});
const t = T();
app.transcripts.s1 = t;
Object.assign(app, { currentView: 'transcript', currentSessionId: 's1', activeTab: 'transcript' });
app.recordings = [{ session_id: 's1', participants: [] }];
let rejectReply;
ctx.window.plaud = {
  people: async () => people,
  setSpeakerNames: async (sid, names) => { calls.push(['set', sid, names]); return { speaker_names: Object.fromEntries(Object.entries(names).filter(([, v]) => v)) }; },
  rejectVoiceMatch: async (sid, label) => { calls.push(['reject', sid, label]); return rejectReply; },
};
const row = new El();
const c = new El();
const open = async (label) => {
  await fire(c, 'click', { target: { closest: (s) => (s === '.segment-speaker' ? { dataset: { label }, closest: () => row } : null) } });
  return row.inserted[row.inserted.length - 1][1];
};
const click = (ed, sel, attrs) => fire(ed, 'click', { target: { closest: (s) => (s === sel ? attrs : null) } });
const action = (ed, a, name) => click(ed, '[data-speaker-action]', { dataset: { speakerAction: a, speakerName: name }, disabled: false });
const typeIn = async (ed, v) => { const i = ed.querySelector('.speaker-name-input'); i.value = v; await fire(i, 'input'); return i; };
const sets = () => calls.filter((x) => x[0] === 'set');
r('renderTranscriptSegments')(c, t, 's1');

// 1. Unknown first name: Did you mean, nothing saved yet
let ed = await open('Speaker 1');
out.editorUnnamed = ed.innerHTML;
await typeIn(ed, 'will');
await action(ed, 'save');
out.dymBox = ed.querySelector('.speaker-name-dym').innerHTML;
out.dymHidden = ed.querySelector('.speaker-name-dym').hidden;
out.dymCalls = sets();
// Pick the known name
await action(ed, 'use-known', 'William Moss');
out.pickKnownCalls = sets();

// 2. Keep what was typed
calls.length = 0;
ed = await open('Speaker 1');
await typeIn(ed, 'Will');
await action(ed, 'save');
await action(ed, 'keep-typed', 'Will');
out.keepCalls = sets();

// 3. Exact match ignoring case: the known spelling, no question
calls.length = 0;
ed = await open('Speaker 1');
await typeIn(ed, 'priya shah');
await action(ed, 'save');
out.exactCalls = sets();

// 4. Clicking a row fills the name and saves; Add new row saves what was typed, no question
calls.length = 0;
ed = await open('Speaker 1');
await click(ed, '[data-pick]', { dataset: { pick: 'Harry Fisher' } });
out.rowCalls = sets();
calls.length = 0;
ed = await open('Speaker 1');
await typeIn(ed, 'will');
await click(ed, '[data-pick-new]', {});
out.addNewCalls = sets();

// 5. Keyboard: ArrowDown selects a row (aria-activedescendant), Enter picks it
calls.length = 0;
ed = await open('Speaker 1');
const input = await typeIn(ed, 'harry');
await fire(input, 'keydown', { key: 'ArrowDown' });
out.activeDescendant = input.attrs['aria-activedescendant'];
await fire(input, 'keydown', { key: 'Enter' });
out.enterPickCalls = sets();

// 6. A voice-matched speaker: editor says so; Not them calls reject and shows the label again
calls.length = 0;
t.speaker_names = { 'Speaker 2': 'Harry Fisher' };
t.stored_speaker_names = {};
t.speaker_name_sources = { 'Speaker 2': { source: 'voice', score: 0.8 } };
r('renderTranscriptSegments')(c, t, 's1');
out.voiceSegments = c.innerHTML;
ed = await open('Speaker 2');
out.voiceEditor = ed.innerHTML;
rejectReply = { speaker_names: {}, speaker_name_sources: {} };
await action(ed, 'reject');
out.rejectCalls = calls.filter((x) => x[0] === 'reject');
out.afterReject = { names: t.speaker_names, sources: t.speaker_name_sources, closed: !ed.isConnected };
out.afterRejectHtml = c.innerHTML;

// 7. A typed name replaces the voice match's hint
t.speaker_names = { 'Speaker 2': 'Harry Fisher' };
t.speaker_name_sources = { 'Speaker 2': { source: 'voice', score: 0.8 } };
ed = await open('Speaker 2');
await typeIn(ed, 'Harry Fisher');
await typeIn(ed, 'Priya Shah');
await action(ed, 'save');
out.typedOverVoice = t.speaker_name_sources;
console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def pick():
    # Reuse the fake DOM; the harness above runs inside an async wrapper like _TX_JS
    script = "(async () => {\n" + _PICK_JS
    return _run(script, ["utils.js", "recordings.js", "transcript.js", "coaching.js"], {})


class TestPicker:
    def test_levenshtein(self, pick):
        assert pick["lev"] == [3, 2, 1]

    def test_did_you_mean_rules(self, pick):
        d = pick["dym"]
        assert d["firstWord"] == "Will Moran"  # exactly one name starts with Will among the unknown spellings
        assert d["firstWordTwo"] is None
        assert d["typo"] == "Priya Shah"
        assert d["exactCase"] is None  # an exact match is not a question
        assert d["none"] is None
        assert d["ties"] == "Harry Fisher"

    def test_rows(self, pick):
        rows = pick["rows"]
        assert rows["all"].index("Harry Fisher") < rows["all"].index("Priya Shah")
        assert rows["all"].count('role="option"') == 4
        assert 'Add new person' not in rows["all"]
        assert "Add new person: “will”" in rows["will"]
        assert "Will Moran" in rows["will"] and "William Moss" in rows["will"] and "Priya" not in rows["will"]
        assert "Add new person" not in rows["exact"]
        assert "A&quot;&lt;b&gt;" in rows["evil"] and "<b>" not in rows["evil"] and "&lt;b&quot;" not in rows["evil"] and "Add new person: “&lt;b”" in rows["evil"]
        assert rows["many"].count('role="option"') == 8
        assert "voice" in rows["all"].split("Priya")[0]  # the voice marker

    def test_editor_markup(self, pick):
        html = pick["editorUnnamed"]
        assert 'placeholder="Full name, such as Harry Fisher"' in html
        assert 'role="combobox"' in html and 'aria-autocomplete="list"' in html and 'role="listbox"' in html
        assert "<datalist" not in html

    def test_did_you_mean_then_choice(self, pick):
        assert pick["dymHidden"] is False
        assert "Did you mean Will Moran?" in pick["dymBox"]
        assert "Keep “will”" in pick["dymBox"]
        assert pick["dymCalls"] == []
        assert pick["pickKnownCalls"] == [["set", "s1", {"Speaker 1": "William Moss"}]]
        assert pick["keepCalls"] == [["set", "s1", {"Speaker 1": "Will"}]]

    def test_exact_match_uses_existing_spelling(self, pick):
        assert pick["exactCalls"] == [["set", "s1", {"Speaker 1": "Priya Shah"}]]

    def test_row_click_and_add_new_save(self, pick):
        assert pick["rowCalls"] == [["set", "s1", {"Speaker 1": "Harry Fisher"}]]
        assert pick["addNewCalls"] == [["set", "s1", {"Speaker 1": "will"}]]

    def test_keyboard(self, pick):
        assert pick["activeDescendant"]
        assert pick["enterPickCalls"] == [["set", "s1", {"Speaker 1": "Harry Fisher"}]]

    def test_voice_matched_name_is_marked(self, pick):
        assert 'title="Matched by voice"' in pick["button"]
        assert 'title="Matched by voice"' not in pick["buttonTyped"]

    def test_voice_editor_and_not_them(self, pick):
        html = pick["voiceEditor"]
        assert "Matched by voice." in html and "Not them" in html
        assert 'data-speaker-action="reject"' in html
        assert "Open Pin recognised your voice" in html
        assert pick["rejectCalls"] == [["reject", "s1", "Speaker 2"]]
        assert pick["afterReject"] == {"names": {}, "sources": {}, "closed": True}
        assert ">Speaker 2<" in pick["afterRejectHtml"]

    def test_typed_name_drops_the_voice_marker(self, pick):
        assert pick["typedOverVoice"] == {}


def test_picker_copy_and_names():
    flat = re.sub(r"'\s*\+\s*'|'\n\s*\+\s*'", "", TRANSCRIPT_JS)
    assert "This is your name because Open Pin recognised your voice, or you picked this speaker in Coaching. Type another name to change it." in flat
    assert "Full name, such as Harry Fisher" in TRANSCRIPT_JS
    for sel in (".speaker-name-list", ".speaker-name-dym", ".speaker-name-voice"):
        assert sel in CSS, sel
    picker = TRANSCRIPT_JS[TRANSCRIPT_JS.index("_TX_PICK_MAX_ROWS") : TRANSCRIPT_JS.index("function renderEditableSummary")]
    assert "—" not in picker
