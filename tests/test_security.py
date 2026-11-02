"""Security tests for the local API: CORS allow-list, Host check, API token, /audio containment."""

import os
from unittest.mock import patch

import pytest

from tests.conftest import TEST_API_TOKEN

TAURI_ORIGIN = "tauri://localhost"


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "https://localhost.evil.com",
        "http://127.0.0.1.nip.io",
        "http://localhost:3000",
        "http://localhost",
        "http://127.0.0.1",
        "tauri://localhost.evil.com",
        "null",
    ],
)
def test_hostile_origins_get_no_cors_header(flask_client, origin):
    r = flask_client.get("/settings", headers={"Origin": origin})
    assert "Access-Control-Allow-Origin" not in r.headers


@pytest.mark.parametrize("origin", ["tauri://localhost", "http://tauri.localhost"])
def test_tauri_origin_gets_cors_header(flask_client, origin):
    r = flask_client.get("/settings", headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers["Access-Control-Allow-Origin"] == origin
    assert "X-Plaud-Token" in r.headers["Access-Control-Allow-Headers"]
    assert "Origin" in r.headers.get("Vary", "")


def test_preflight_from_tauri_origin_succeeds_without_token(flask_client):
    r = flask_client.options(
        "/settings",
        headers={
            "Origin": TAURI_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-plaud-token",
            "X-Plaud-Token": "",
        },
    )
    assert r.status_code == 200
    assert r.headers["Access-Control-Allow-Origin"] == TAURI_ORIGIN
    allowed = r.headers["Access-Control-Allow-Headers"].lower()
    assert "x-plaud-token" in allowed and "content-type" in allowed
    assert "POST" in r.headers["Access-Control-Allow-Methods"]


def test_preflight_from_hostile_origin_gets_no_cors_header(flask_client):
    r = flask_client.options(
        "/settings",
        headers={"Origin": "https://localhost.evil.com", "Access-Control-Request-Method": "POST"},
    )
    assert "Access-Control-Allow-Origin" not in r.headers


def test_preflight_does_not_run_the_view(flask_client):
    with patch("app.routes.settings.update_config") as save:
        flask_client.options("/settings", headers={"Origin": TAURI_ORIGIN})
    save.assert_not_called()


# ---------------------------------------------------------------------------
# Host header (DNS rebinding)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["attacker.example", "attacker.example:5123", "127.0.0.1.nip.io:5123", "localhost.evil.com", "localhost:abc"],
)
def test_bad_host_rejected(flask_client, host):
    r = flask_client.get("/settings", headers={"Host": host})
    assert r.status_code == 403


def test_bad_host_rejected_even_for_health(flask_client):
    r = flask_client.get("/health", headers={"Host": "attacker.example:5123"})
    assert r.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:5123", "127.0.0.1:5130", "localhost:5123", "127.0.0.1", "LOCALHOST:5124"])
def test_loopback_hosts_allowed(flask_client, host):
    r = flask_client.get("/settings", headers={"Host": host})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# API token
# ---------------------------------------------------------------------------


def test_missing_token_rejected(flask_client):
    r = flask_client.get("/settings", headers={"X-Plaud-Token": ""})
    assert r.status_code == 401


def test_wrong_token_rejected(flask_client):
    r = flask_client.get("/settings", headers={"X-Plaud-Token": "wrong"})
    assert r.status_code == 401


def test_wrong_token_rejected_on_post(flask_client):
    with patch("app.routes.settings.update_config") as save:
        r = flask_client.post("/settings", json={"language": "fr"}, headers={"X-Plaud-Token": "nope"})
    assert r.status_code == 401
    save.assert_not_called()


def test_correct_token_accepted(flask_client):
    r = flask_client.get("/settings", headers={"X-Plaud-Token": TEST_API_TOKEN})
    assert r.status_code == 200


def test_health_open_without_token(flask_client):
    r = flask_client.get("/health", headers={"X-Plaud-Token": ""})
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_query_token_not_accepted_outside_audio(flask_client):
    r = flask_client.get(f"/settings?token={TEST_API_TOKEN}", headers={"X-Plaud-Token": ""})
    assert r.status_code == 401


def test_no_token_configured_skips_token_check(flask_client):
    """Dev mode: without PLAUD_API_TOKEN the token check is off, Host/CORS still apply."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PLAUD_API_TOKEN", None)
        r = flask_client.get("/settings", headers={"X-Plaud-Token": ""})
        assert r.status_code == 200
        r = flask_client.get("/settings", headers={"Host": "attacker.example"})
        assert r.status_code == 403
        r = flask_client.get("/settings", headers={"Origin": "http://localhost:3000"})
        assert "Access-Control-Allow-Origin" not in r.headers


# ---------------------------------------------------------------------------
# /audio
# ---------------------------------------------------------------------------


@pytest.fixture
def audio_env(tmp_path, sample_ogg):
    """A recordings dir with one valid recording, plus a secret file outside it."""
    ogg_data, _ = sample_ogg
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    (rec_dir / "20260101_120000.ogg").write_bytes(ogg_data)
    (rec_dir / "cloud_2026-01-01_1200_Team sync (v2).ogg").write_bytes(ogg_data)

    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.ogg"
    secret.write_bytes(b"TOP-SECRET")
    (rec_dir / "link.ogg").symlink_to(secret)

    with (
        patch("app.routes.core.get_all_recordings_dirs", return_value=[rec_dir]),
        patch("app.state.gdrive.get_synced_files", return_value={}),
    ):
        yield {"rec_dir": rec_dir, "secret": secret, "ogg": ogg_data}


def test_audio_valid_recording_served(flask_client, audio_env):
    r = flask_client.get("/audio/20260101_120000.ogg", headers={"Accept": "*/*"})
    assert r.status_code == 200
    assert r.data == audio_env["ogg"]


def test_audio_cloud_name_with_spaces_served(flask_client, audio_env):
    r = flask_client.get("/audio/cloud_2026-01-01_1200_Team%20sync%20(v2).ogg", headers={"Accept": "*/*"})
    assert r.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/audio/../outside/secret.ogg",
        "/audio/..%2Foutside%2Fsecret.ogg",
        "/audio/%2E%2E%2Foutside%2Fsecret.ogg",
        "/audio/..%5Coutside%5Csecret.ogg",
        "/audio/link.ogg",  # symlink escaping the recordings dir
        "/audio/.hidden.ogg",
        "/audio/20260101_120000.txt",
        "/audio/" + "a" * 300 + ".ogg",
    ],
)
def test_audio_traversal_rejected(flask_client, audio_env, path):
    r = flask_client.get(path, headers={"Accept": "*/*"})
    assert r.status_code == 404
    assert b"TOP-SECRET" not in r.data


def test_audio_absolute_path_rejected(flask_client, audio_env):
    secret = str(audio_env["secret"])
    for path in (f"/audio/{secret}", f"/audio/{secret.replace('/', '%2F')}", f"/audio//{secret.lstrip('/')}"):
        r = flask_client.get(path, headers={"Accept": "*/*"}, follow_redirects=True)
        assert r.status_code == 404, path
        assert b"TOP-SECRET" not in r.data


def test_audio_view_refuses_absolute_filename(flask_client, audio_env):
    """Call the view directly: URL routing merges '//' so it can't deliver an absolute path in tests."""
    from werkzeug.exceptions import NotFound

    from app.api_server import app
    from app.routes import core

    with app.test_request_context("/audio/x", headers={"Accept": "*/*"}), pytest.raises(NotFound):
        core.serve_audio(str(audio_env["secret"]))
    assert core._validate_audio_name("/etc/passwd.ogg") is None
    assert core._validate_audio_name("20260101_120000.ogg") == "20260101_120000.ogg"


def test_audio_token_via_query(flask_client, audio_env):
    r = flask_client.get(
        f"/audio/20260101_120000.ogg?format=ogg&token={TEST_API_TOKEN}",
        headers={"Accept": "*/*", "X-Plaud-Token": ""},
    )
    assert r.status_code == 200


def test_audio_without_token_rejected(flask_client, audio_env):
    r = flask_client.get("/audio/20260101_120000.ogg", headers={"Accept": "*/*", "X-Plaud-Token": ""})
    assert r.status_code == 401


def test_audio_wrong_query_token_rejected(flask_client, audio_env):
    r = flask_client.get(
        "/audio/20260101_120000.ogg?token=wrong", headers={"Accept": "*/*", "X-Plaud-Token": ""}
    )
    assert r.status_code == 401


def test_audio_m4a_requested_falls_back_or_transcodes(flask_client, audio_env, tmp_path):
    """?format=m4a still works (transcode result served, or OGG if ffmpeg is missing)."""
    m4a = tmp_path / "out.m4a"
    m4a.write_bytes(b"fake-m4a")
    with patch("app.routes.core._get_or_transcode_m4a", return_value=str(m4a)) as tx:
        r = flask_client.get(f"/audio/20260101_120000.ogg?format=m4a&token={TEST_API_TOKEN}")
    assert r.status_code == 200
    assert r.mimetype == "audio/mp4"
    assert r.data == b"fake-m4a"
    # Called with the resolved in-directory file, never a user-controlled path
    called_with = tx.call_args[0][0]
    assert called_with.parent == audio_env["rec_dir"].resolve()


def test_audio_gdrive_fallback_only_for_valid_names(flask_client, audio_env):
    synced = {"remote_only": {"drive_file_id": "abc"}}
    with (
        patch("app.state.gdrive.get_synced_files", return_value=synced),
        patch("app.state.gdrive.is_connected", return_value=True),
        patch("app.state.gdrive.proxy_stream", return_value=(b"DRIVE", "audio/mpeg")) as stream,
    ):
        r = flask_client.get("/audio/remote_only.ogg", headers={"Accept": "*/*"})
        assert r.status_code == 200
        assert r.data == b"DRIVE"
        r = flask_client.get("/audio/..%2Fremote_only.ogg", headers={"Accept": "*/*"})
        assert r.status_code == 404
    assert stream.call_count == 1
