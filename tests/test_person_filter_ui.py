"""The recordings list's person filter (app/ui/recordings.js) and the People links to it.

Runs the real files in node against the fake DOM from test_speaker_names_ui.
"""

import re

import pytest

from tests.test_speaker_names_ui import APP, UI, _run

_JS = r"""
const app = r('app');
const dom = r('dom');
app.settings = {};
app.recordings = data.recordings;
app.transcripts = {};
const out = {};
const ids = () => r('getFilteredRecordings')().map((x) => x.session_id);
r('renderPersonFilter')();
out.options = document.querySelector('#person-filter').innerHTML;
out.hiddenWithPeople = document.querySelector('#person-filter').classList.contains('hidden');
out.all = ids();
for (const name of ['Sam Carter', 'sam  carter', 'Alex']) {
  r('showRecordingsWith')(name);
  out[name] = ids();
}
app.searchQuery = 'budget';
r('showRecordingsWith')('Alex');
out.both = ids();
app.searchQuery = 'nothing here';
r('renderRecordingsList')();
out.emptyBoth = dom.recordingsList.innerHTML;
app.searchQuery = '';
r('showRecordingsWith')('Eve Gone');
out.emptyPerson = dom.recordingsList.innerHTML;
r('renderPersonFilter')();
out.goneOptions = document.querySelector('#person-filter').innerHTML;
out.active = document.querySelector('#person-filter').classList.contains('active');
r('showRecordingsWith')('');
out.cleared = ids();
out.inactive = !document.querySelector('#person-filter').classList.contains('active');
app.recordings = [{ session_id: 'z', date: '2026-11-01', participants: [] }];
r('renderPersonFilter')();
out.hiddenWithoutPeople = document.querySelector('#person-filter').classList.contains('hidden');
console.log(JSON.stringify(out));
"""

RECORDINGS = [
    {"session_id": "a", "date": "2026-11-03", "title": "Budget", "participants": ["Alex", "Sam Carter"]},
    {"session_id": "b", "date": "2026-11-02", "title": "Walk", "participants": ["Sam carter", "Priya"]},
    {"session_id": "c", "date": "2026-11-01", "title": "Budget too", "participants": ["Alex"]},
    {"session_id": "d", "date": "2026-10-01", "title": "Sam mentioned", "participants": []},
]


@pytest.fixture(scope="module")
def out():
    return _run(_JS, ["utils.js", "recordings.js"], {"recordings": RECORDINGS})


def test_picker_lists_everyone_named_most_recordings_first(out):
    opts = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', out["options"])
    assert opts == [("", "Everyone"), ("Alex", "Alex (2)"), ("Sam Carter", "Sam Carter (2)"), ("Priya", "Priya (1)")]
    assert not out["hiddenWithPeople"] and out["hiddenWithoutPeople"]


def test_filters_by_person_ignoring_case_and_spacing(out):
    assert out["all"] == ["a", "b", "c", "d"]
    # A title mentioning Sam isn't a recording Sam was in
    assert out["Sam Carter"] == ["a", "b"] == out["sam  carter"]
    assert out["Alex"] == ["a", "c"]
    assert out["cleared"] == ["a", "b", "c", "d"] and out["inactive"]


def test_combines_with_search(out):
    assert out["both"] == ["a", "c"]
    assert 'No recordings with Alex that match "nothing here"' in out["emptyBoth"]


def test_someone_no_longer_named_stays_selected(out):
    assert "No recordings with Eve Gone" in out["emptyPerson"]
    assert '<option value="Eve Gone" selected>Eve Gone (0)</option>' in out["goneOptions"]
    assert out["active"]


def test_wiring():
    html = (APP / "index.html").read_text()
    assert html.index('id="person-filter"') < html.index('id="tag-filters"')
    assert "$('#person-filter').addEventListener('change', (e) => showRecordingsWith(e.target.value));" in (
        APP / "renderer.js"
    ).read_text()
    people = (UI / "people.js").read_text()
    assert "case 'recordings': closeSettings(); return showRecordingsWith(el.dataset.name);" in people
    assert 'data-ppl="recordings" data-name="${escapeHtml(p.name)}"' in people
    assert "${me.name ? `${_pplRecordings(me)} · ` : ''}" in people
