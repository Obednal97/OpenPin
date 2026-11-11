"""Settings > Transcription & AI > People (app/ui/people.js): static checks.

The frontend is plain <script> files sharing one global scope, so a duplicate top-level
name breaks the whole UI. This pins the section's place in the page, the exact copy,
and that the file ships.
"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
APP = PROJECT_ROOT / "app"
HTML = (APP / "index.html").read_text()
JS = (APP / "ui" / "people.js").read_text() if (APP / "ui" / "people.js").exists() else ""
CSS_PATH = APP / "css" / "people.css"
SETTINGS_JS = (APP / "ui" / "settings.js").read_text()
TOP_LEVEL = re.compile(
    r"^(?:async\s+)?(?:function\s+([A-Za-z_$][\w$]*)|(?:const|let|var|class)\s+([A-Za-z_$][\w$]*))", re.M
)


def _names(text):
    return {m.group(1) or m.group(2) for m in TOP_LEVEL.finditer(text)}


def test_section_follows_voice_coaching_in_the_ai_tab():
    voice = HTML.index('id="voice-coaching-section"')
    people = HTML.index('id="people-section"')
    assert voice < people < HTML.index('data-tab="automation" role="tabpanel"')
    assert 'id="people-section"' in HTML and 'id="people-body"' in HTML


def test_name_hint_under_profile_name():
    field = HTML[HTML.index('id="profile-name"') :][:400]
    assert "Used for your voice in transcripts. See People." in field


def test_script_and_styles_are_loaded_in_order():
    assert '<script src="ui/people.js"></script>' in HTML
    assert HTML.index('src="ui/settings.js"') < HTML.index('src="ui/people.js"') < HTML.index('src="ui/coaching-progress.js"')
    assert "@import 'css/people.css';" in (APP / "styles.css").read_text()
    assert CSS_PATH.exists()


def test_settings_renders_people_next_to_voice():
    assert re.search(r"renderVoiceSettings\(\);\s*renderPeopleSettings\(\);", SETTINGS_JS)


def test_globals_are_unique_and_prefixed():
    mine = _names(JS)
    assert "renderPeopleSettings" in mine
    assert all(n.startswith("_ppl") or n == "renderPeopleSettings" for n in mine), mine
    for path in sorted((APP / "ui").glob("*.js")) + [APP / "renderer.js"]:
        if path.name != "people.js":
            assert not (mine & _names(path.read_text())), path.name


def test_exact_copy():
    for text in [
        "Open Pin recognises ",
        " by voice. Voice data stays on this Mac (about ",
        "Tested on your recordings: ",
        "% of the speakers it names are right.",
        "Open Pin will start naming speakers once it has learnt from your recordings.",
        "Add your name",
        "Your voice &amp; coaching",
        " of 20 samples",
        "Last heard ",
        "Merge into…",
        "Delete ${name}’s voice? Open Pin will stop recognising them. Names in past transcripts stay.",
        "Merge ${source} into ${target}? Their voice samples are combined and ${source} is renamed to ${target} in every transcript.",
        "Learn from past recordings",
        "Learning voices: ${b.done} of ${b.total} recordings",
        "Naming past recordings: ${b.done} of ${b.total}",
        "Testing on your recordings…",
        "Install the voice model to recognise people.",
    ]:
        assert text in JS, text


def test_no_em_dashes():
    assert "—" not in JS and "—" not in CSS_PATH.read_text()


def test_behaviour_wiring():
    assert "window.plaud.peopleDirectory()" in JS
    for call in ["renamePerson", "mergePerson", "deletePerson", "startPeopleBacklog", "cancelPeopleBacklog"]:
        assert f"window.plaud.{call}(" in JS
    assert "showConfirmDialog(" in JS
    assert "#voice-coaching-section" in JS and "#profile-name" in JS
    assert "_voiceStartInstall(" in JS  # reuses the voice section's handler
    assert "cal.named >= _pplMinNamed" in JS and "_pplMinNamed = 10" in JS


def test_install_handler_is_shared():
    src = (APP / "ui" / "coaching-progress.js").read_text()
    assert "async function _voiceStartInstall(" in src
    assert src.count("window.plaud.voiceInstall()") == 1


def test_frontend_copy_script_ships_ui_and_css():
    script = (PROJECT_ROOT / "scripts" / "prepare-frontend.sh").read_text()
    assert '"$APP"/ui/*.js' in script and '"$APP"/css/*.css' in script


def test_rows_are_light_and_dates_have_no_time():
    css = CSS_PATH.read_text()
    assert ":focus-within .ppl-actions" in css and "opacity: 0" in css and "display: none" not in css
    assert "ppl-danger" in css and 'class="ppl-link ppl-danger"' in JS
    assert "formatDate(" not in JS and "toLocaleDateString" in JS
    assert "settings-btn-compact\" data-ppl=\"rename\"" not in JS


def test_failed_poll_keeps_data_and_reschedules():
    body = JS[JS.index("if (!d || d.error) {") :][:500]
    assert "_pplSchedule()" in body


def test_rejected_rename_keeps_the_editor_and_text():
    assert "_pplEditing.value = input.value" in JS
    assert ", true);" in JS and "keepEditor" in JS
    assert "ppl-row-error" in JS


def test_merge_offers_you_once_your_name_is_set():
    assert '<option value="me"' in JS and "You (${escapeHtml(_pplData.me.name)})" in JS
    assert "into === 'me' ? _pplData.me : _pplById(into)" in JS
