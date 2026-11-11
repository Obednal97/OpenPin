"""Settings panel layout and shared frontend helpers.

Settings has five tabs (Device, Transcription & AI, Automation, Accounts &
storage, General). Controls were moved there, not rebuilt: these tests pin each
control's tab and that every element settings.js looks up still exists. The JS
helpers (summary markdown, time and device-name formatting) run in node.
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

TABS = ["device", "ai", "automation", "accounts", "general"]


def _tab_panel(name):
    """Markup of one settings tab (up to the next tab or the end of the body)."""
    start = HTML.index(f'<div class="settings-tab-content{" active" if name == "device" else ""}" data-tab="{name}"')
    rest = HTML[start + 1 :]
    nxt = rest.find('<div class="settings-tab-content')
    end = rest.find("<!-- Toast notifications -->") if nxt == -1 else nxt
    return rest[:end]


def _ids(markup):
    return set(re.findall(r'\bid="([^"]+)"', markup))


class TestSettingsTabs:
    def test_five_tabs_in_order(self):
        strip = HTML[HTML.index('<div class="settings-tabs"') : HTML.index('<div class="settings-body">')]
        assert re.findall(r'class="settings-tab[^"]*" data-tab="([^"]+)"', strip) == TABS
        labels = re.findall(r'role="tab"[^>]*>([^<]+)</button>', strip)
        assert labels == ["Device", "Transcription &amp; AI", "Automation", "Accounts &amp; storage", "General"]
        for name in TABS:
            assert f'data-tab="{name}" role="tabpanel"' in HTML

    @pytest.mark.parametrize(
        "tab, ids",
        [
            (
                "device",
                {
                    "dm-scan-btn", "dm-sync-btn", "dm-disconnect-btn", "auto-wifi-toggle", "dm-auto-delete-toggle",
                    "device-advanced", "dm-info-panel", "pairing-export-btn", "pairing-import-btn", "dm-depair-btn",
                },
            ),
            (
                "ai",
                {
                    "language-select", "english-variant-select", "transcription-provider", "transcription-key-area",
                    "model-selector", "diarize-toggle", "hf-token-area", "hf-token-input", "summarization-provider",
                    "summarization-key-area", "profile-name", "profile-job", "profile-industry", "profile-seniority",
                    "profile-usecases", "profile-instructions", "vocab-input", "vocab-add-btn", "api-keys-overview",
                },
            ),
            (
                "automation",
                {
                    "auto-sync-toggle", "auto-transcribe-toggle", "auto-summarise-toggle", "automation-notion-slot",
                    "gdrive-auto-sync-toggle", "webhook-list", "webhook-add-btn", "cloud-sync-interval",
                },
            ),
            (
                "accounts",
                {
                    "save-dir-path", "change-save-dir-btn", "historical-dirs", "gdrive-connect-btn", "storage-mode-selector",
                    "cloud-login-browser-btn", "cloud-sync-btn", "cloud-logout-btn", "notion-connect-btn",
                    "notion-auto-push-toggle", "notion-db-search",
                },
            ),
            (
                "general",
                {
                    "launch-login-toggle", "show-dock-toggle", "theme-setting", "check-updates-btn",
                    "settings-export-bug-report-btn", "open-diagnostics-log-btn",
                    "mcp-enabled-toggle", "mcp-desktop-connect-btn", "mcp-code-connect-btn",
                },
            ),
        ],
    )
    def test_each_tab_holds_its_controls(self, tab, ids):
        assert ids <= _ids(_tab_panel(tab))

    def test_sections_have_headings(self):
        for tab, headings in {
            "device": ["NotePin", "Transfer"],
            "ai": ["Language", "Transcription", "Speaker identification", "Your voice &amp; coaching", "People", "Summarisation",
                   "Personalisation", "Other saved keys"],
            "automation": ["New recordings", "Webhooks", "Plaud cloud sync"],
            "accounts": ["Recordings folder", "Google Drive", "Plaud account", "Notion"],
            "general": ["System", "Appearance", "AI tools (MCP)", "About"],
        }.items():
            assert re.findall(r"<h3>([^<]+)</h3>", _tab_panel(tab)) == headings, tab

    def test_advanced_groups_are_collapsed(self):
        device = _tab_panel("device")
        adv = device[device.index('<details class="settings-section settings-advanced" id="device-advanced">') :]
        assert "open" not in adv[: adv.index(">")]
        for control in ("dm-info-panel", "pairing-export-btn", "dm-depair-btn"):
            assert f'id="{control}"' in adv
        # Erase is kept away from the everyday Disconnect button
        assert 'id="dm-disconnect-btn"' not in adv
        assert '<details id="hf-token-area" class="settings-advanced' in _tab_panel("ai")

    def test_removed_and_renamed(self):
        assert 'id="profile-save-btn"' not in HTML
        assert "Force Sync" not in HTML  # same as the main Sync (performSync): now "Sync now"
        assert re.search(r'id="dm-sync-btn"[\s\S]{0,800}<span>Sync now</span>', HTML)
        modes = re.findall(r'data-mode="(\w+)">\s*<svg[^>]*>.*?</svg>\s*([^<]+?)\s*</button>', HTML, re.S)
        assert modes == [("local", "Local"), ("cloud", "Google Drive"), ("both", "Both")]

    def test_notion_block_is_one_section_in_accounts(self):
        accounts = _tab_panel("accounts")
        block = accounts[accounts.index("<h3>Notion</h3>") :]
        for el in ("notion-section", "notion-connected", "notion-db-card", "notion-status-badge", "notion-disconnect-btn"):
            assert f'id="{el}"' in block

    def test_panel_width_fits_five_tabs(self):
        layouts = (APP / "css" / "layouts.css").read_text()
        assert "width: min(520px, 100vw);" in layouts
        assert ".settings-panel { width: 100%; }" not in (APP / "css" / "utilities.css").read_text()


class TestSettingsScript:
    SRC = (APP / "ui" / "settings.js").read_text()
    # Built at runtime by settings.js itself
    DYNAMIC = {"notion-queue-notice"}

    def test_every_looked_up_id_exists(self):
        looked_up = set(re.findall(r"\$\('#([\w-]+)'\)", self.SRC))
        missing = looked_up - _ids(HTML) - self.DYNAMIC
        assert not missing

    def test_profile_saves_itself(self):
        assert "profile-save-btn" not in self.SRC
        assert "addEventListener('input', scheduleProfileSave)" in self.SRC
        assert "addEventListener('blur', saveProfileNow)" in self.SRC

    def test_hf_token_hidden_with_deepgram(self):
        assert "area.classList.toggle('hidden', !enabled || transProv === 'deepgram')" in self.SRC

    def test_other_keys_cover_every_provider(self):
        # A key saved for a provider that isn't selected can still be removed
        assert "for (const [id, meta] of Object.entries(PROVIDERS))" in self.SRC
        for provider in ("deepgram", "groq", "openai", "anthropic", "openrouter"):
            assert f"  {provider}: {{ name:" in self.SRC
        assert "remove_hf_token" in self.SRC

    def test_status_chip_opens_device_tab(self):
        assert "showSettingsTab('device')" in (APP / "renderer.js").read_text()


class TestWifiNeedManual:
    """The "reconnect to Wi-Fi" state is final: it isn't replaced by "Transfer complete"."""

    def test_need_manual_and_success_are_exclusive(self):
        src = (APP / "renderer.js").read_text()
        branch = src[src.index("if (result.wifi_restore && result.wifi_restore.need_manual) {") :]
        branch = branch[: branch.index("// Add done button") if "// Add done button" in branch else 2000]
        assert "'warning'" in branch
        assert "} else {\n      _renderWifiProgress('check', 'Transfer complete', syncedText, 100, 'success');" in branch


class TestSharedGlobalScope:
    """app/ui/*.js and renderer.js are plain <script>s sharing one global scope:
    a second top-level declaration of a name is a SyntaxError that stops the
    later script (v2.1.5)."""

    def test_no_duplicate_top_level_names(self):
        scripts = re.findall(r'<script src="([^"]+)"></script>', HTML)
        seen = {}
        for rel in scripts:
            text = (APP / rel).read_text()
            for m in re.finditer(
                r"^(?:async\s+)?(?:function\s+([A-Za-z_$][\w$]*)|(?:const|let|var|class)\s+([A-Za-z_$][\w$]*))",
                text,
                re.M,
            ):
                name = m.group(1) or m.group(2)
                assert name not in seen, f"{name} declared in {seen.get(name)} and {rel}"
                seen[name] = rel


_UTILS_JS = r"""
const fs = require('fs'); const vm = require('vm');
const el = () => ({ classList: { add() {}, remove() {} }, setAttribute() {}, appendChild() {}, style: {} });
const ctx = { console, window: { plaud: {} }, document: { querySelector: () => null, querySelectorAll: () => [], createElement: el, body: el() } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const r = (code) => vm.runInContext(code, ctx);
const md = (s) => r('renderMarkdown')(s);
console.log(JSON.stringify({
  ordered: md('1. One\n2. Two\n\n3. Three'),
  nested: md('- a\n  1. b\n- c'),
  tasks: md('- [ ] open\n- [x] done'),
  table: md('| A | B |\n|:--|--:|\n| 1 | <b>2</b> |'),
  escaped: md('<img src=x onerror=alert(1)> **bold** `<code>`'),
  heading: md('## Key points'),
  times: [r('formatTime')(65), r('formatTime')(7400), r('formatTime')(65, true), r('formatTime')(NaN)],
  names: [r('prettyDeviceName')('PLAUD_NOTEPIN_S'), r('prettyDeviceName')('Kitchen pin'), r('prettyDeviceName')(null)],
}));
"""


@pytest.fixture(scope="module")
def utils_out():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    out = subprocess.run(
        [node, "-e", _UTILS_JS, str(APP / "ui" / "utils.js")], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out)


class TestSummaryMarkdown:
    def test_ordered_list_survives_a_blank_line(self, utils_out):
        assert utils_out["ordered"] == "<ol>\n<li>One\n</li>\n<li>Two\n</li>\n<li>Three\n</li>\n</ol>"

    def test_nested_lists_sit_inside_their_item(self, utils_out):
        assert utils_out["nested"] == "<ul>\n<li>a\n<ol>\n<li>b\n</li>\n</ol>\n</li>\n<li>c\n</li>\n</ul>"

    def test_todo_items_have_no_bullet_class(self, utils_out):
        assert '<li class="md-task"><input type="checkbox" disabled> open' in utils_out["tasks"]
        assert '<li class="md-task"><input type="checkbox" disabled checked> done' in utils_out["tasks"]
        css = (APP / "css" / "transcript.css").read_text()
        assert ".transcript-summary li.md-task { list-style: none;" in css

    def test_gfm_table(self, utils_out):
        t = utils_out["table"]
        assert t.startswith('<div class="md-table-wrap"><table><thead><tr><th>A</th>')
        assert '<td style="text-align:right">&lt;b&gt;2&lt;/b&gt;</td>' in t

    def test_html_is_escaped(self, utils_out):
        assert "<img" not in utils_out["escaped"]
        assert "&lt;img src=x onerror=alert(1)&gt;" in utils_out["escaped"]
        assert "<strong>bold</strong>" in utils_out["escaped"]
        assert "<code>&lt;code&gt;</code>" in utils_out["escaped"]

    def test_heading(self, utils_out):
        assert utils_out["heading"] == "<h2>Key points</h2>"


class TestFormatters:
    def test_times_over_an_hour(self, utils_out):
        assert utils_out["times"] == ["01:05", "2:03:20", "0:01:05", "00:00"]

    def test_device_names(self, utils_out):
        assert utils_out["names"] == ["Plaud NotePin S", "Kitchen pin", ""]
