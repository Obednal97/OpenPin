"""MCP access: the key, the /mcp/v1 guard, the connection file, the launcher and the change counter.

The guard is security-critical: the MCP key opens only /mcp/v1/*, and /mcp/v1/*
accepts nothing but the MCP key (never the app token, never a browser).
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from flask import jsonify

# The real function: tests/conftest.py redirects access.support_dir for every test.
from app.mcp.access import support_dir as real_support_dir
from tests.conftest import TEST_API_TOKEN

MCP_HEADER = "X-Open-Pin-MCP-Key"
OFF_MESSAGE = "AI tool access is turned off in Open Pin"


@pytest.fixture
def access():
    from app.mcp import access as mod

    yield mod
    mod.disable()


@pytest.fixture
def mcp_client(flask_client, access):
    """flask_client with a throwaway GET /mcp/v1/ping route mounted."""
    from app.api_server import app

    if "test_mcp_ping" not in app.view_functions:
        # The shared app has served requests already; Flask refuses new routes
        # after that, so lift the flag just long enough to add this one.
        got_first = app._got_first_request
        app._got_first_request = False
        try:
            app.add_url_rule("/mcp/v1/ping", "test_mcp_ping", lambda: jsonify({"pong": True}))
        finally:
            app._got_first_request = got_first
    return flask_client


def _no_app_token(client):
    client.environ_base.pop("HTTP_X_PLAUD_TOKEN", None)


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_mcp_key_opens_mcp_route(mcp_client, access):
    access.enable(5123)
    _no_app_token(mcp_client)
    r = mcp_client.get("/mcp/v1/ping", headers={MCP_HEADER: access.current_key()})
    assert r.status_code == 200
    assert r.get_json() == {"pong": True}


def test_mcp_route_rejects_app_token(mcp_client, access):
    access.enable(5123)
    # The fixture sends the app token by default; on /mcp/v1 it counts for nothing.
    r = mcp_client.get("/mcp/v1/ping")
    assert r.status_code == 401
    r = mcp_client.get("/mcp/v1/ping", headers={MCP_HEADER: TEST_API_TOKEN})
    assert r.status_code == 401


def test_mcp_route_rejects_wrong_or_missing_key(mcp_client, access):
    access.enable(5123)
    _no_app_token(mcp_client)
    assert mcp_client.get("/mcp/v1/ping").status_code == 401
    assert mcp_client.get("/mcp/v1/ping", headers={MCP_HEADER: ""}).status_code == 401
    assert mcp_client.get("/mcp/v1/ping", headers={MCP_HEADER: access.current_key() + "x"}).status_code == 401
    # Not as a query parameter either.
    assert mcp_client.get(f"/mcp/v1/ping?token={access.current_key()}").status_code == 401
    assert mcp_client.get(f"/mcp/v1/ping?key={access.current_key()}").status_code == 401


def test_mcp_guard_applies_even_without_app_token_configured(mcp_client, access, monkeypatch):
    """Running api_server.py by hand skips the app token; /mcp/v1 still needs the key."""
    monkeypatch.delenv("PLAUD_API_TOKEN", raising=False)
    access.enable(5123)
    _no_app_token(mcp_client)
    assert mcp_client.get("/mcp/v1/ping").status_code == 401
    assert mcp_client.get("/mcp/v1/ping", headers={MCP_HEADER: access.current_key()}).status_code == 200


def test_unknown_mcp_path_needs_the_key_before_404(mcp_client, access):
    access.enable(5123)
    assert mcp_client.get("/mcp/v1/nope").status_code == 401
    assert mcp_client.get("/mcp/v1").status_code == 401
    r = mcp_client.get("/mcp/v1/nope", headers={MCP_HEADER: access.current_key()})
    assert r.status_code == 404


@pytest.mark.parametrize(
    "method, path",
    [("GET", "/settings"), ("POST", "/settings"), ("GET", "/changes"), ("GET", "/recordings")],
)
def test_mcp_key_rejected_elsewhere(mcp_client, access, method, path):
    access.enable(5123)
    _no_app_token(mcp_client)
    r = mcp_client.open(path, method=method, headers={MCP_HEADER: access.current_key()}, json={})
    assert r.status_code == 401


def test_mcp_key_as_app_token_rejected(mcp_client, access):
    access.enable(5123)
    _no_app_token(mcp_client)
    r = mcp_client.get("/settings", headers={"X-Plaud-Token": access.current_key()})
    assert r.status_code == 401


def test_mcp_off_is_403_with_message(mcp_client, access):
    access.enable(5123)
    old_key = access.current_key()
    access.disable()
    assert access.current_key() is None
    _no_app_token(mcp_client)
    for headers in ({MCP_HEADER: old_key}, {}):
        r = mcp_client.get("/mcp/v1/ping", headers=headers)
        assert r.status_code == 403
        assert r.get_json() == {"error": OFF_MESSAGE}


def test_mcp_off_by_default(mcp_client, access):
    assert access.current_key() is None
    r = mcp_client.get("/mcp/v1/ping")
    assert r.status_code == 403
    assert r.get_json() == {"error": OFF_MESSAGE}
    assert mcp_client.get("/settings").get_json()["mcp_enabled"] is False


@pytest.mark.parametrize("origin", ["tauri://localhost", "http://tauri.localhost", "https://evil.example", "null"])
def test_origin_refused_on_mcp(mcp_client, access, origin):
    access.enable(5123)
    _no_app_token(mcp_client)
    r = mcp_client.get("/mcp/v1/ping", headers={MCP_HEADER: access.current_key(), "Origin": origin})
    assert r.status_code == 403
    assert "Access-Control-Allow-Origin" not in r.headers


def test_browser_fetch_metadata_refused_on_mcp(mcp_client, access):
    """Browsers mark every request with Sec-Fetch-*, even no-cors GETs that carry no Origin."""
    access.enable(5123)
    _no_app_token(mcp_client)
    r = mcp_client.get("/mcp/v1/ping", headers={MCP_HEADER: access.current_key(), "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_preflight_refused_on_mcp(mcp_client, access):
    access.enable(5123)
    r = mcp_client.options(
        "/mcp/v1/ping",
        headers={"Origin": "tauri://localhost", "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code == 403
    assert "Access-Control-Allow-Origin" not in r.headers


def test_bad_host_refused_on_mcp(mcp_client, access):
    access.enable(5123)
    r = mcp_client.get("/mcp/v1/ping", headers={MCP_HEADER: access.current_key(), "Host": "evil.example"})
    assert r.status_code == 403
    assert r.get_json() == {"error": "Forbidden host"}


def test_app_routes_unchanged(mcp_client, access):
    access.enable(5123)
    assert mcp_client.get("/settings").status_code == 200
    _no_app_token(mcp_client)
    assert mcp_client.get("/settings").status_code == 401


# ---------------------------------------------------------------------------
# Key and connection file
# ---------------------------------------------------------------------------


def test_connection_file_0600_and_removed_on_disable(access):
    access.enable(5123)
    path = access.connection_path()
    assert path.name == "mcp-connection.json"
    from app.utils import constants

    assert path.parent == Path(constants.DATA_DIR)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data == {"port": 5123, "key": access.current_key(), "pid": os.getpid()}
    assert len(data["key"]) >= 32

    access.disable()
    assert not path.exists()
    assert access.current_key() is None
    # The launcher stays: Claude's settings still point at it.
    assert access.launcher_path().exists()


def test_connection_file_mode_fixed_when_already_present(access):
    path = access.connection_path()
    path.write_text("{}")
    path.chmod(0o644)
    access.enable(5123)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_enable_twice_keeps_key_and_updates_port(access):
    access.enable(5123)
    key = access.current_key()
    access.enable(5124)
    assert access.current_key() == key
    assert json.loads(access.connection_path().read_text())["port"] == 5124


def test_new_key_after_disable(access):
    access.enable(5123)
    key = access.current_key()
    access.disable()
    access.enable(5123)
    assert access.current_key() not in (None, key)


def test_disable_leaves_another_process_file(access):
    """A restart's new backend may have written the file before the old one exits."""
    path = access.connection_path()
    path.write_text(json.dumps({"port": 1, "key": "k", "pid": os.getpid() + 1}))
    access.disable()
    assert path.exists()
    access.remove_connection_file()
    assert not path.exists()


def test_disable_without_file_is_fine(access):
    access.disable()
    access.disable()


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


def test_launcher_is_executable_and_points_at_bundle_server(access):
    import sys

    from app.utils import constants

    path = access.write_launcher()
    assert path == access.launcher_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    text = path.read_text()
    assert text.startswith("#!/bin/sh\n")
    server = Path(access.__file__).resolve().parent / "server.py"
    assert str(server) in text
    assert sys.executable in text
    assert str(constants.DATA_DIR) in text
    assert "PLAUD_DATA_DIR" in text
    exec_line = [line for line in text.splitlines() if line.startswith("exec ")]
    assert len(exec_line) == 1
    # Bytecode never goes into the signed bundle.
    assert "PYTHONDONTWRITEBYTECODE=1" in text


def test_launcher_runs_server_with_data_dir(access, tmp_path, monkeypatch):
    """Execute the launcher against a stand-in server.py that reports what it got."""
    fake_bundle = tmp_path / "bundle" / "mcp"
    fake_bundle.mkdir(parents=True)
    (fake_bundle / "server.py").write_text(
        "import os, sys\nprint(os.environ['PLAUD_DATA_DIR'])\nprint(sys.executable)\n"
    )
    monkeypatch.setattr(access, "server_script_path", lambda: fake_bundle / "server.py")
    path = access.write_launcher()
    out = subprocess.run([str(path)], capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    from app.utils import constants

    lines = out.stdout.splitlines()
    assert lines[0] == str(constants.DATA_DIR)


def test_launcher_fails_clearly_when_app_missing(access, tmp_path, monkeypatch):
    monkeypatch.setattr(access, "server_script_path", lambda: tmp_path / "gone" / "server.py")
    path = access.write_launcher()
    out = subprocess.run([str(path)], capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    assert out.returncode != 0
    assert "Open Pin" in out.stderr


def test_launcher_quotes_awkward_paths(access, tmp_path, monkeypatch):
    odd = tmp_path / 'it\'s a "bundle" $HOME `x`' / "mcp"
    odd.mkdir(parents=True)
    (odd / "server.py").write_text("print('ok')\n")
    monkeypatch.setattr(access, "server_script_path", lambda: odd / "server.py")
    path = access.write_launcher()
    out = subprocess.run([str(path)], capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_enable_writes_launcher(access):
    path = access.launcher_path()
    path.unlink(missing_ok=True)
    access.enable(5123)
    assert path.exists()


@pytest.mark.parametrize(
    "executable, folder",
    [
        ("/Users/x/Library/Application Support/com.plaud.v2/venv/bin/python3", "com.plaud.v2"),
        ("/Users/x/Library/Application Support/com.plaud.v2.dev/venv/bin/python3", "com.plaud.v2.dev"),
        ("/Users/x/.venvs/plaud-v2/bin/python", "com.plaud.v2"),
        ("/opt/homebrew/bin/python3", "com.plaud.v2"),
        ("/Users/x/Library/Application Support/other/venv/bin/python3", "com.plaud.v2"),
    ],
)
def test_support_dir_follows_the_build(executable, folder):
    got = real_support_dir(executable=executable, home=Path("/Users/x"))
    assert got == Path("/Users/x/Library/Application Support") / folder


def test_launcher_path_shape(monkeypatch):
    from app.mcp import access as mod

    # The conftest redirects support_dir for safety; check the real shape here.
    monkeypatch.setattr(mod, "support_dir", lambda: Path("/Users/x/Library/Application Support/com.plaud.v2"))
    assert mod.launcher_path() == Path("/Users/x/Library/Application Support/com.plaud.v2/mcp/open-pin-mcp")


def test_tests_never_touch_real_application_support(access):
    real = Path.home() / "Library" / "Application Support"
    assert real not in access.launcher_path().parents


# ---------------------------------------------------------------------------
# Settings switch
# ---------------------------------------------------------------------------


def test_settings_switch_on_and_off(mcp_client, access):
    access.set_port(5999)
    r = mcp_client.post("/settings", json={"mcp_enabled": True})
    assert r.status_code == 200
    assert mcp_client.get("/settings").get_json()["mcp_enabled"] is True
    key = access.current_key()
    assert key
    assert json.loads(access.connection_path().read_text())["port"] == 5999

    r = mcp_client.post("/settings", json={"mcp_enabled": False})
    assert r.status_code == 200
    assert mcp_client.get("/settings").get_json()["mcp_enabled"] is False
    assert access.current_key() is None
    assert not access.connection_path().exists()
    _no_app_token(mcp_client)
    r = mcp_client.get("/mcp/v1/ping", headers={MCP_HEADER: key})
    assert r.status_code == 403


def test_settings_switch_only_true_turns_it_on(mcp_client, access):
    access.set_port(5999)
    for value in ("true", 1, "yes"):
        mcp_client.post("/settings", json={"mcp_enabled": value})
        assert mcp_client.get("/settings").get_json()["mcp_enabled"] is False
        assert access.current_key() is None


def test_settings_switch_failure_leaves_it_off(mcp_client, access, monkeypatch):
    access.set_port(5999)

    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(access, "write_launcher", boom)
    r = mcp_client.post("/settings", json={"mcp_enabled": True})
    assert r.status_code == 500
    assert "error" in r.get_json()
    assert mcp_client.get("/settings").get_json()["mcp_enabled"] is False
    assert access.current_key() is None
    assert not access.connection_path().exists()


def test_settings_other_fields_dont_touch_mcp(mcp_client, access):
    access.enable(5123)
    mcp_client.post("/settings", json={"language": "en"})
    assert access.current_key() is not None


def test_startup_enables_when_setting_on(access, flask_client):
    from app import api_server
    from app.utils.config import update_config

    update_config(lambda c: c.__setitem__("mcp_enabled", True))
    api_server._start_mcp_access(6001)
    assert access.current_key()
    assert json.loads(access.connection_path().read_text())["port"] == 6001


def test_startup_removes_stale_file_when_off(access, flask_client):
    from app import api_server

    access.connection_path().write_text(json.dumps({"port": 1, "key": "old", "pid": 999999}))
    api_server._start_mcp_access(6001)
    assert access.current_key() is None
    assert not access.connection_path().exists()


def test_shutdown_removes_file_keeps_setting(access, flask_client):
    from app import api_server
    from app.utils.config import load_config, update_config

    update_config(lambda c: c.__setitem__("mcp_enabled", True))
    api_server._start_mcp_access(6001)
    api_server._stop_mcp_access()
    assert not access.connection_path().exists()
    assert load_config()["mcp_enabled"] is True


# ---------------------------------------------------------------------------
# Change counter
# ---------------------------------------------------------------------------


def test_change_counter_route(flask_client):
    from app.utils import changes

    before = flask_client.get("/changes")
    assert before.status_code == 200
    n = before.get_json()["counter"]
    assert n == changes.current()
    changes.bump()
    assert flask_client.get("/changes").get_json() == {"counter": n + 1}


def test_change_counter_needs_app_token(flask_client):
    flask_client.environ_base.pop("HTTP_X_PLAUD_TOKEN", None)
    assert flask_client.get("/changes").status_code == 401


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


def test_bundle_ships_mcp_folder():
    conf = json.loads((Path(__file__).parent.parent / "src-tauri" / "tauri.conf.json").read_text())
    assert conf["bundle"]["resources"].get("../app/mcp/*") == "app/mcp/"
