"""One-click setup for Claude Desktop and Claude Code (app/mcp/clients.py).

Every test runs against a fake home folder: Path.home() is patched, so nothing
here can read or write the real ~/Library or ~/.claude* files.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from app.mcp import access, clients

LAUNCHER = "/fake/Application Support/com.plaud.v2/mcp/open-pin-mcp"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    monkeypatch.setattr(access, "launcher_path", lambda: Path(LAUNCHER))
    return fake


def _desktop(home):
    return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def _write_json(path, data, mode=0o644, trailing_newline=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + ("\n" if trailing_newline else ""))
    os.chmod(path, mode)
    return path


def _backups(path):
    return sorted(p.name for p in path.parent.glob(path.name + ".open-pin-backup-*"))


# ---------------------------------------------------------------------------
# Paths and discovery
# ---------------------------------------------------------------------------


def test_desktop_path(home):
    assert clients.claude_desktop_path() == _desktop(home)


def test_code_files_discovered(home):
    _write_json(home / ".claude.json", {})
    _write_json(home / ".claude-personal" / ".claude.json", {})
    _write_json(home / ".claude-work" / ".claude.json", {})
    (home / ".claude-empty").mkdir()  # no settings file inside
    _write_json(home / ".config" / ".claude.json", {})  # not a ~/.claude* folder
    _write_json(home / "claude-x" / ".claude.json", {})  # not hidden

    found = clients.claude_code_files()
    assert found == [
        home / ".claude.json",
        home / ".claude-personal" / ".claude.json",
        home / ".claude-work" / ".claude.json",
    ]


def test_code_files_none(home):
    assert clients.claude_code_files() == []


def test_code_file_symlink_listed_once(home):
    real = _write_json(home / ".claude-personal" / ".claude.json", {})
    (home / ".claude-link").mkdir()
    (home / ".claude-link" / ".claude.json").symlink_to(real)
    [found] = clients.claude_code_files()
    assert found.resolve() == real.resolve()


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


def test_status_shows_tilde_paths_and_connection(home):
    _write_json(home / ".claude.json", {"mcpServers": {"open-pin": {"type": "stdio", "command": LAUNCHER}}})
    _write_json(home / ".claude-personal" / ".claude.json", {"mcpServers": {"other": {"command": "x"}}})

    s = clients.status()
    assert s["desktop"] == {
        "path": "~/Library/Application Support/Claude/claude_desktop_config.json",
        "exists": False,
        "connected": False,
        "stale": False,
    }
    assert s["code"] == [
        {"path": "~/.claude.json", "connected": True, "stale": False, "error": None},
        {"path": "~/.claude-personal/.claude.json", "connected": False, "stale": False, "error": None},
    ]


def test_status_flags_entry_for_another_launcher(home):
    _write_json(_desktop(home), {"mcpServers": {"open-pin": {"command": "/elsewhere/open-pin-mcp"}}})
    s = clients.status()
    assert s["desktop"]["connected"] is True
    assert s["desktop"]["stale"] is True


def test_status_reports_invalid_json(home):
    path = home / ".claude.json"
    path.write_text("{not json")
    [entry] = clients.status()["code"]
    assert entry["connected"] is False
    assert "isn't valid JSON" in entry["error"]


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------


def test_connect_desktop_creates_missing_file(home):
    clients.connect(_desktop(home), "desktop")
    path = _desktop(home)
    assert json.loads(path.read_text()) == {"mcpServers": {"open-pin": {"command": LAUNCHER}}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert _backups(path) == []  # nothing to back up


def test_connect_code_entry_shape(home):
    path = _write_json(home / ".claude.json", {"numStartups": 3})
    clients.connect(path, "code")
    data = json.loads(path.read_text())
    assert data["mcpServers"]["open-pin"] == {"type": "stdio", "command": LAUNCHER, "args": [], "env": {}}
    assert data["numStartups"] == 3


def test_connect_code_needs_existing_file(home):
    with pytest.raises(ValueError):
        clients.connect(home / ".claude.json", "code")
    assert not (home / ".claude.json").exists()


def test_invalid_json_refused_untouched(home):
    for kind, path in (("desktop", _desktop(home)), ("code", home / ".claude.json")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"mcpServers": {"x": 1},')
        before = path.read_bytes()
        with pytest.raises(ValueError, match="isn't valid JSON"):
            clients.connect(path, kind)
        with pytest.raises(ValueError, match="isn't valid JSON"):
            clients.disconnect(path, kind)
        assert path.read_bytes() == before
        assert _backups(path) == []
        assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]  # no temp files left


def test_non_object_json_refused(home):
    path = home / ".claude.json"
    path.write_text("[1, 2]")
    with pytest.raises(ValueError):
        clients.connect(path, "code")
    path.write_text('{"mcpServers": []}')
    with pytest.raises(ValueError):
        clients.connect(path, "code")
    assert path.read_text() == '{"mcpServers": []}'


def test_other_servers_kept(home):
    original = {
        "numStartups": 618,
        "mcpServers": {
            "playwright": {"type": "stdio", "command": "npx", "args": ["@playwright/mcp"], "env": {"A": "1"}},
            "mobbin": {"type": "http", "url": "https://api.mobbin.com/mcp"},
        },
        "projects": {"/x": {"mcpServers": {"p": {"command": "y"}}}},
        "unicode": "café ✓",
    }
    path = _write_json(home / ".claude.json", original)
    clients.connect(path, "code")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data) == list(original)  # key order kept
    assert data["mcpServers"]["playwright"] == original["mcpServers"]["playwright"]
    assert data["mcpServers"]["mobbin"] == original["mcpServers"]["mobbin"]
    assert data["projects"] == original["projects"]
    assert data["unicode"] == "café ✓"
    assert "café ✓" in path.read_text(encoding="utf-8")  # not \u-escaped

    desktop = _write_json(_desktop(home), {"mcpServers": {"other": {"command": "z"}}, "globalShortcut": "x"})
    clients.connect(desktop, "desktop")
    data = json.loads(desktop.read_text())
    assert data == {"mcpServers": {"other": {"command": "z"}, "open-pin": {"command": LAUNCHER}}, "globalShortcut": "x"}


def test_connect_replaces_an_old_entry(home):
    path = _write_json(home / ".claude.json", {"mcpServers": {"open-pin": {"command": "/old"}}})
    clients.connect(path, "code")
    assert json.loads(path.read_text())["mcpServers"]["open-pin"]["command"] == LAUNCHER


def test_backup_made_and_only_three_kept(home, monkeypatch):
    path = _write_json(home / ".claude.json", {"n": 0})
    stamps = iter(["20261105-100000", "20261105-100001", "20261105-100002", "20261105-100003", "20261105-100004"])
    monkeypatch.setattr(clients, "_timestamp", lambda: next(stamps))

    clients.connect(path, "code")
    [first] = _backups(path)
    assert first == ".claude.json.open-pin-backup-20261105-100000"
    assert json.loads((home / first).read_text()) == {"n": 0}  # the file as it was before

    clients.disconnect(path, "code")
    clients.connect(path, "code")
    clients.disconnect(path, "code")
    clients.connect(path, "code")
    assert _backups(path) == [
        ".claude.json.open-pin-backup-20261105-100002",
        ".claude.json.open-pin-backup-20261105-100003",
        ".claude.json.open-pin-backup-20261105-100004",
    ]


def test_backups_in_the_same_second_dont_overwrite(home, monkeypatch):
    path = _write_json(home / ".claude.json", {"n": 0})
    monkeypatch.setattr(clients, "_timestamp", lambda: "20261105-100000")
    clients.connect(path, "code")
    clients.disconnect(path, "code")
    names = _backups(path)
    assert len(names) == 2
    assert json.loads((home / names[0]).read_text()) == {"n": 0}


def test_mode_preserved(home):
    path = _write_json(home / ".claude.json", {"secretish": True}, mode=0o600)
    clients.connect(path, "code")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    [backup] = _backups(path)
    assert stat.S_IMODE((home / backup).stat().st_mode) == 0o600

    desktop = _write_json(_desktop(home), {}, mode=0o640)
    clients.connect(desktop, "desktop")
    assert stat.S_IMODE(desktop.stat().st_mode) == 0o640


def test_symlinked_file_stays_a_symlink(home):
    real = _write_json(home / "real" / "claude.json", {"a": 1})
    link = home / ".claude.json"
    link.symlink_to(real)
    clients.connect(link, "code")
    assert link.is_symlink()
    assert json.loads(real.read_text())["mcpServers"]["open-pin"]["command"] == LAUNCHER


def test_formatting_follows_the_original(home):
    path = _write_json(home / ".claude.json", {"a": 1}, trailing_newline=False)
    clients.connect(path, "code")
    text = path.read_text()
    assert text.startswith('{\n  "a": 1,')
    assert not text.endswith("\n")

    desktop = _write_json(_desktop(home), {"a": 1}, trailing_newline=True)
    clients.connect(desktop, "desktop")
    assert desktop.read_text().endswith("}\n")


def test_disconnect_removes_only_open_pin(home):
    original = {
        "mcpServers": {"before": {"command": "a"}, "open-pin": {"command": LAUNCHER}, "after": {"command": "b"}},
        "other": {"open-pin": "not a server"},
    }
    path = _write_json(home / ".claude.json", original)
    clients.disconnect(path, "code")
    data = json.loads(path.read_text())
    assert data == {
        "mcpServers": {"before": {"command": "a"}, "after": {"command": "b"}},
        "other": {"open-pin": "not a server"},
    }
    assert len(_backups(path)) == 1


def test_disconnect_without_entry_writes_nothing(home):
    path = _write_json(home / ".claude.json", {"mcpServers": {"x": {"command": "a"}}})
    before = path.read_bytes()
    clients.disconnect(path, "code")
    clients.disconnect(_desktop(home), "desktop")  # missing file: nothing to do
    assert path.read_bytes() == before
    assert _backups(path) == []
    assert not _desktop(home).exists()


def test_change_during_write_is_not_lost(home, monkeypatch):
    """Claude Code rewrites its file often: a change between our read and our
    rename means reading again, so neither edit is lost."""
    path = _write_json(home / ".claude.json", {"n": 0})
    real_backup = clients._backup
    calls = {"n": 0}

    def racing_backup(p):
        result = real_backup(p)
        calls["n"] += 1
        if calls["n"] == 1:
            # Claude Code saves its own change, a bit bigger, after we read the file
            p.write_text(json.dumps({"n": 1, "theirs": "a longer value"}, indent=2))
        return result

    monkeypatch.setattr(clients, "_backup", racing_backup)
    clients.connect(path, "code")
    data = json.loads(path.read_text())
    assert data["theirs"] == "a longer value"
    assert "open-pin" in data["mcpServers"]


def test_unknown_kind_refused(home):
    path = _write_json(home / ".claude.json", {})
    with pytest.raises(ValueError):
        clients.connect(path, "cursor")


# ---------------------------------------------------------------------------
# resolve(): only paths that status() lists
# ---------------------------------------------------------------------------


def test_resolve_only_listed_paths(home):
    code = _write_json(home / ".claude-personal" / ".claude.json", {})
    assert clients.resolve("code", "~/.claude-personal/.claude.json") == code
    assert clients.resolve("desktop", "~/Library/Application Support/Claude/claude_desktop_config.json") == _desktop(
        home
    )
    assert clients.resolve("code", "~/.claude.json") is None  # doesn't exist
    assert clients.resolve("code", str(code)) is None  # only the shown form
    assert clients.resolve("desktop", "~/.claude-personal/.claude.json") is None
    assert clients.resolve("code", "~/Library/Application Support/Claude/claude_desktop_config.json") is None
    assert clients.resolve("code", "~/.claude-personal/../.ssh/config") is None
    assert clients.resolve("nope", "~/.claude.json") is None


# ---------------------------------------------------------------------------
# Routes (app token, like every other window route)
# ---------------------------------------------------------------------------


def _set_enabled(on):
    from app.utils.config import update_config

    update_config(lambda c: c.__setitem__("mcp_enabled", on))


def test_status_route(flask_client, home):
    _write_json(home / ".claude.json", {})
    res = flask_client.get("/mcp/setup/status")
    assert res.status_code == 200
    body = res.get_json()
    assert body["desktop"]["path"] == "~/Library/Application Support/Claude/claude_desktop_config.json"
    assert body["code"] == [{"path": "~/.claude.json", "connected": False, "stale": False, "error": None}]


def test_routes_need_the_app_token(flask_client):
    flask_client.environ_base.pop("HTTP_X_PLAUD_TOKEN", None)
    assert flask_client.get("/mcp/setup/status").status_code == 401
    assert flask_client.post("/mcp/setup/connect", json={"kind": "desktop", "path": "x"}).status_code == 401


def test_connect_and_disconnect_routes(flask_client, home):
    _set_enabled(True)
    path = _write_json(home / ".claude-personal" / ".claude.json", {"keep": 1})
    res = flask_client.post("/mcp/setup/connect", json={"kind": "code", "path": "~/.claude-personal/.claude.json"})
    assert res.status_code == 200
    assert res.get_json()["code"][0]["connected"] is True
    assert json.loads(path.read_text())["mcpServers"]["open-pin"]["command"] == LAUNCHER

    res = flask_client.post("/mcp/setup/disconnect", json={"kind": "code", "path": "~/.claude-personal/.claude.json"})
    assert res.status_code == 200
    assert res.get_json()["code"][0]["connected"] is False
    assert json.loads(path.read_text()) == {"keep": 1, "mcpServers": {}}


def test_connect_desktop_route_creates_file(flask_client, home):
    _set_enabled(True)
    shown = "~/Library/Application Support/Claude/claude_desktop_config.json"
    res = flask_client.post("/mcp/setup/connect", json={"kind": "desktop", "path": shown})
    assert res.status_code == 200
    assert res.get_json()["desktop"]["connected"] is True
    assert _desktop(home).exists()


def test_connect_rejects_unlisted_path(flask_client, home):
    _set_enabled(True)
    _write_json(home / ".claude.json", {})
    secret = _write_json(home / ".ssh" / "config.json", {})
    for body in (
        {"kind": "code", "path": str(secret)},
        {"kind": "code", "path": "~/.ssh/config.json"},
        {"kind": "code", "path": str(home / ".claude.json")},  # absolute form isn't listed
        {"kind": "desktop", "path": "~/.claude.json"},
        {"kind": "other", "path": "~/.claude.json"},
        {"kind": "code"},
        {"kind": "code", "path": ["~/.claude.json"]},
    ):
        for route in ("/mcp/setup/connect", "/mcp/setup/disconnect"):
            res = flask_client.post(route, json=body)
            assert res.status_code == 400, (route, body)
            assert res.get_json()["error"]
    assert json.loads(secret.read_text()) == {}
    assert json.loads((home / ".claude.json").read_text()) == {}


def test_connect_refused_while_access_is_off(flask_client, home):
    _write_json(home / ".claude.json", {})
    res = flask_client.post("/mcp/setup/connect", json={"kind": "code", "path": "~/.claude.json"})
    assert res.status_code == 409
    assert "Turn on" in res.get_json()["error"]
    assert json.loads((home / ".claude.json").read_text()) == {}


def test_disconnect_allowed_while_access_is_off(flask_client, home):
    path = _write_json(home / ".claude.json", {"mcpServers": {"open-pin": {"command": LAUNCHER}}})
    res = flask_client.post("/mcp/setup/disconnect", json={"kind": "code", "path": "~/.claude.json"})
    assert res.status_code == 200
    assert json.loads(path.read_text()) == {"mcpServers": {}}


def test_invalid_json_route_message(flask_client, home):
    _set_enabled(True)
    path = home / ".claude.json"
    path.write_text("{oops")
    res = flask_client.post("/mcp/setup/connect", json={"kind": "code", "path": "~/.claude.json"})
    assert res.status_code == 400
    assert "isn't valid JSON" in res.get_json()["error"]
    assert path.read_text() == "{oops"


def test_turning_access_off_leaves_claude_files_alone(flask_client, home):
    _set_enabled(True)
    path = _write_json(home / ".claude.json", {"mcpServers": {"open-pin": {"command": LAUNCHER}}})
    before = path.read_bytes()
    res = flask_client.post("/settings", json={"mcp_enabled": False})
    assert res.status_code == 200
    assert path.read_bytes() == before
