"""The app was renamed from "Plaud V2" to Open Pin. Everything stored under the
old name must stay where it is, or installs lose recordings, settings, the
pairing backup or their logs. These tests pin those names; the Rust side has
the same checks in src-tauri/src/paths.rs and migrate.rs."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_user_facing_name_is_open_pin():
    from app.utils import constants

    assert constants.APP_NAME == "Open Pin"
    conf = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())
    assert conf["productName"] == constants.APP_NAME
    assert conf["app"]["windows"][0]["title"] == constants.APP_NAME


def test_data_folder_and_config_keep_the_old_name():
    from app.utils import constants

    assert Path("~/Library/Application Support/Plaud V2").expanduser() == constants.PACKAGED_DATA_DIR
    assert constants.PACKAGED_DATA_DIR.name != constants.APP_NAME
    assert constants.DATA_DIR_ENV == "PLAUD_DATA_DIR"
    assert constants.CONFIG_PATH.name == ".plaud_v2_config.json"
    assert constants.PLAUD_CONFIG_PATH.name == ".plaud_config.json"
    # The Rust shell passes the same folder (release build) in PLAUD_DATA_DIR.
    paths_rs = (ROOT / "src-tauri/src/paths.rs").read_text()
    assert 'const RELEASE_DATA_DIR: &str = "Plaud V2";' in paths_rs
    assert 'const DEV_DATA_DIR: &str = "Plaud V2 (Dev)";' in paths_rs
    assert 'const RELEASE_LOG_DIR: &str = "Plaud V2";' in paths_rs
    assert 'const RELEASE_SUPPORT_DIR: &str = "com.plaud.v2";' in paths_rs


def test_identifier_scheme_and_support_folder_are_unchanged():
    conf = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())
    assert conf["identifier"] == "com.plaud.v2"
    assert conf["plugins"]["deep-link"]["desktop"]["schemes"] == ["plaud"]
    from app.utils import system

    assert system._FFMPEG_DIR.endswith("/Library/Application Support/com.plaud.v2/bin")
    for rel in ("app/routes/core.py", "app/routes/recordings.py"):
        assert '"com.plaud.v2"' in (ROOT / rel).read_text(), rel


def test_keychain_service_and_pairing_format_keep_the_old_name():
    from app.utils import pairing_file, pairing_store

    assert pairing_store.RELEASE_SERVICE == "Plaud V2 pairing"
    assert pairing_store.DEV_SERVICE == "Plaud V2 (Dev) pairing"
    assert pairing_file.FORMAT == "plaud-v2-pairing"


def test_notion_sections_written_under_either_name_are_found():
    from app.routes import notion as rn

    new = rn._build_app_section({"text": "hello"}, "s1")
    assert new.startswith("## Open Pin — transcript & summary")
    assert new.rstrip().endswith("*End of Open Pin section*")
    old = new.replace("Open Pin", "Plaud V2")
    for section in (new, old):
        page = f"My notes\n\n{section}\n\nMore notes"
        assert rn._locate_app_section(page) == section


def test_notion_transcript_part_stops_at_either_end_marker():
    from app.utils import notion_legacy as nl

    for name in ("Open Pin", "Plaud V2"):
        body = f"## Full Transcript\n\n[00:00] hi\n\n---\n*End of {name} section*\n\nMy notes after"
        part = nl.transcript_part(body)
        assert "[00:00] hi" in part and "My notes after" not in part


def test_no_user_facing_old_name_left_in_the_frontend():
    # Comments and strings in the UI say Open Pin; only the repo URL may still
    # carry the old slug until the repo is renamed.
    for path in [ROOT / "app/index.html", *(ROOT / "app/ui").glob("*.js"), ROOT / "app/renderer.js"]:
        text = re.sub(r"https://github\.com/\S+", "", path.read_text())
        assert "Plaud V2" not in text, path
