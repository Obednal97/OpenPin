"""Integration tests for Flask API endpoints."""


def test_health_endpoint(flask_client):
    """GET /health returns OK."""
    r = flask_client.get("/health")
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "ok"
    assert "version" in data


def test_settings_get(flask_client):
    """GET /settings returns expected fields."""
    r = flask_client.get("/settings")
    assert r.status_code == 200
    data = r.get_json()
    assert "model" in data
    assert "diarize" in data
    assert "language" in data
    assert "models" in data
    assert "available_tags" in data
    assert "profile" in data
    assert "webhooks" in data


def test_settings_roundtrip(flask_client):
    """POST settings, GET them back."""
    # Save a setting
    r = flask_client.post("/settings", json={"language": "fr", "diarize": True}, content_type="application/json")
    assert r.status_code == 200
    assert r.get_json()["saved"] is True

    # Read it back
    r = flask_client.get("/settings")
    data = r.get_json()
    assert data["language"] == "fr"
    assert data["diarize"] is True


def test_recordings_list(flask_client):
    """GET /recordings returns a list."""
    r = flask_client.get("/recordings")
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list)


def test_transcripts_list(flask_client):
    """GET /transcripts returns a list."""
    r = flask_client.get("/transcripts")
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list)


def test_delete_recording_no_ids(flask_client):
    """POST /recordings/delete with no IDs returns error."""
    r = flask_client.post("/recordings/delete", json={}, content_type="application/json")
    assert r.status_code == 400


def test_delete_recording_empty_ids(flask_client):
    """POST /recordings/delete with empty list returns error."""
    r = flask_client.post("/recordings/delete", json={"session_ids": []}, content_type="application/json")
    assert r.status_code == 400


def test_validate_key_no_key(flask_client):
    """POST /validate-key with no key returns invalid."""
    r = flask_client.post("/validate-key", json={"provider": "openai", "key": ""}, content_type="application/json")
    assert r.status_code == 200
    data = r.get_json()
    assert data["valid"] is False


def test_validate_key_unknown_provider(flask_client):
    """POST /validate-key with unknown provider returns error."""
    r = flask_client.post(
        "/validate-key", json={"provider": "foobar", "key": "sk-test"}, content_type="application/json"
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["valid"] is False
    assert "Unknown" in data.get("error", "")


def test_device_status_disconnected(flask_client):
    """GET /device/status when disconnected."""
    r = flask_client.get("/device/status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["connected"] is False


def test_cloud_status(flask_client):
    """GET /cloud/status returns state."""
    r = flask_client.get("/cloud/status")
    assert r.status_code == 200
    data = r.get_json()
    assert "logged_in" in data


def test_notion_status(flask_client):
    """GET /notion/status returns state."""
    r = flask_client.get("/notion/status")
    assert r.status_code == 200
    data = r.get_json()
    assert "connected" in data


def test_transcribe_no_path(flask_client):
    """POST /transcribe with no path returns error."""
    r = flask_client.post("/transcribe", json={}, content_type="application/json")
    assert r.status_code == 400


def test_summarize_no_session(flask_client):
    """POST /summarize with no session_id returns error."""
    r = flask_client.post("/summarize", json={}, content_type="application/json")
    assert r.status_code == 400


def test_cors_headers(flask_client):
    """All responses have CORS headers (restricted to localhost)."""
    r = flask_client.get("/health")
    origin = r.headers.get("Access-Control-Allow-Origin", "")
    assert "127.0.0.1" in origin or "localhost" in origin or "tauri" in origin


def test_tag_recording_no_session(flask_client):
    """POST /recordings/tag with no session_id returns error."""
    r = flask_client.post("/recordings/tag", json={"tags": ["test"]}, content_type="application/json")
    assert r.status_code == 400


def test_rename_recording_no_session(flask_client):
    """POST /recordings/rename with no session_id returns error."""
    r = flask_client.post("/recordings/rename", json={"title": "test"}, content_type="application/json")
    assert r.status_code == 400


def test_export_recording_not_found(flask_client):
    """POST /recordings/export for missing recording returns 404."""
    r = flask_client.post(
        "/recordings/export", json={"session_id": "nonexistent_12345"}, content_type="application/json"
    )
    assert r.status_code == 404


def test_cloud_trash_not_logged_in(flask_client):
    """POST /cloud/trash when not logged in returns 401."""
    r = flask_client.post("/cloud/trash", json={"ids": ["abc"]}, content_type="application/json")
    assert r.status_code == 401


def test_notion_push_missing_transcript(flask_client):
    """POST /notion/push for nonexistent transcript returns 400 or 404."""
    r = flask_client.post("/notion/push", json={"session_id": "nonexistent_12345"}, content_type="application/json")
    # Returns 401 (not connected), 400 (no database), or 404 (transcript not found)
    assert r.status_code in (400, 401, 404)
