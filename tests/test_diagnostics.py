"""GET /diagnostics: the checks that go into an exported bug report.

It must never contain secrets: tokens, API keys, user ids, emails, full
serial numbers or file contents.
"""

import json

import app.state.cloud as cloud_state
import app.state.device as dev
from app.utils import ble_proto
from tests.plaud_fakes import FakeNotePin
from tests.test_ble_handshake import connect, connect_env  # noqa: F401  (fixture)

SERIAL = "8820A00012345678"
SECRETS = {
    "bind_token": "BINDTOKEN-SECRET-0123456789abcdef",
    "sn_signature": "SNSIG-SECRET-QUJDREVGR0hJSktMTU5PUA==",
    "anthropic": "sk-ant-api03-SECRETSECRETSECRET",
    "deepgram": "dg-SECRET-deepgram-key-9999",
    "openrouter": "sk-or-v1-SECRET-openrouter",
    "cloud_token": "eyJ-CLOUD-SESSION-TOKEN-SECRET",
    "user_token": "pld_ut-USER-TOKEN-SECRET",
    "refresh_token": "pld_urt-REFRESH-TOKEN-SECRET",
    "email": "jane.doe@example.com",
    "user_id": "user-id-SECRET-4242",
}


def _plant_config(tmp_path):
    config = {
        "paired_devices": {SERIAL: {"name": "NotePin S", "last_address": "3F2504E0-4F89-11D3-9A0C-0305E82C3301"}},
        "device_bind_tokens": {SERIAL: SECRETS["bind_token"]},
        "device_sn_signatures": {SERIAL: SECRETS["sn_signature"]},
        "anthropic_api_key": SECRETS["anthropic"],
        "deepgram_api_key": SECRETS["deepgram"],
        "openrouter_api_key": SECRETS["openrouter"],
        "transcription_provider": "deepgram",
        "summarization_provider": "openrouter",
        "profile": {"name": "Jane Doe", "email": SECRETS["email"]},
        # Old configs may still have the retired crash-report setting.
        "crash_reports_enabled": False,
    }
    (tmp_path / ".plaud_v2_config.json").write_text(json.dumps(config))


def _plant_cloud(monkeypatch):
    state = cloud_state._cloud_state  # the flask_client fixture's copy
    monkeypatch.setitem(state, "logged_in", True)
    monkeypatch.setitem(state, "token", SECRETS["cloud_token"])
    monkeypatch.setitem(state, "ut", SECRETS["user_token"])
    monkeypatch.setitem(state, "urt", SECRETS["refresh_token"])
    monkeypatch.setitem(state, "api_domain", "https://api-euc1.plaud.ai")
    monkeypatch.setitem(state, "user", {"id": SECRETS["user_id"], "email": SECRETS["email"], "nickname": "Jane"})


def test_diagnostics_shape(flask_client, monkeypatch):
    monkeypatch.setitem(dev._device_state, "last_handshake", None)
    r = flask_client.get("/diagnostics")
    assert r.status_code == 200
    data = r.get_json()
    assert set(data) == {"cloud", "devices", "connection", "processing", "integrations", "storage"}
    assert data["cloud"] == {"signed_in": False, "has_user_token": False, "region": "api-euc1.plaud.ai"}
    assert data["devices"] == []
    assert data["connection"] == {"connected": False, "port_version": None, "last_handshake": None}
    processing = data["processing"]
    assert processing["transcription_provider"] == "deepgram"
    assert processing["summarization_provider"] == "anthropic"
    assert set(processing["keys_configured"]) == {"anthropic", "deepgram", "groq", "openai", "openrouter"}
    assert all(v is False for v in processing["keys_configured"].values())
    assert isinstance(processing["huggingface_configured"], bool)
    assert set(data["integrations"]) == {"notion_connected", "gdrive_connected"}
    assert all(isinstance(v, bool) for v in data["integrations"].values())
    assert isinstance(data["storage"]["recordings"], int)
    assert isinstance(data["storage"]["free_disk_gb"], float) and data["storage"]["free_disk_gb"] > 0


def test_diagnostics_reports_facts_not_secrets(flask_client, tmp_path, monkeypatch):
    _plant_config(tmp_path)
    _plant_cloud(monkeypatch)
    r = flask_client.get("/diagnostics")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    for name, secret in SECRETS.items():
        assert secret not in text, f"{name} leaked: {text}"
    for leaked in (SERIAL, SERIAL[:-4], "3F2504E0", "Jane"):
        assert leaked not in text, f"{leaked} leaked: {text}"

    data = r.get_json()
    assert data["cloud"] == {"signed_in": True, "has_user_token": True, "region": "api-euc1.plaud.ai"}
    assert data["devices"] == [
        {"serial_last4": "5678", "has_sn_signature": True, "has_bind_token": True, "name": "NotePin S"}
    ]
    keys = data["processing"]["keys_configured"]
    assert keys == {"anthropic": True, "deepgram": True, "groq": False, "openai": False, "openrouter": True}
    assert data["processing"]["summarization_provider"] == "openrouter"


def test_diagnostics_needs_the_api_token(flask_client):
    r = flask_client.get("/diagnostics", headers={"X-Plaud-Token": "wrong"})
    assert r.status_code in (401, 403)


def test_diagnostics_reads_the_config_only(flask_client, tmp_path, monkeypatch):
    """A device missing its token in the config is reported as such: no Keychain restore."""
    (tmp_path / ".plaud_v2_config.json").write_text(json.dumps({"paired_devices": {SERIAL: {"name": "X"}}}))

    def no_keychain(*a, **k):
        raise AssertionError("diagnostics must not restore from the Keychain")

    monkeypatch.setattr("app.utils.pairing_store.restore_serial", no_keychain)
    data = flask_client.get("/diagnostics").get_json()
    assert data["devices"] == [
        {"serial_last4": "5678", "has_sn_signature": False, "has_bind_token": False, "name": "X"}
    ]


def test_last_handshake_is_recorded_and_survives_disconnect(connect_env, flask_client, monkeypatch):  # noqa: F811
    monkeypatch.setitem(dev._device_state, "last_handshake", None)
    device = FakeNotePin(port_version=8, token_len=16)  # rejects 32 bytes, accepts 16
    assert connect(monkeypatch, device)["connected"] is True

    last = dev.get_last_handshake()
    assert last["verdict"] == ble_proto.HANDSHAKE_OK
    assert last["statuses"] == [0, 0]
    assert last["port_version"] == 8
    assert last["token_len"] == 16
    assert "T" * 16 not in json.dumps(last)  # never the token itself
    assert last["at"][:4].isdigit() and "T" in last["at"]  # ISO time

    dev._reset_state()
    assert dev.get_last_handshake() == last
    data = flask_client.get("/diagnostics").get_json()
    assert data["connection"]["connected"] is False
    assert data["connection"]["last_handshake"] == last


def test_rejected_handshake_is_recorded(connect_env, monkeypatch):  # noqa: F811
    monkeypatch.setitem(dev._device_state, "last_handshake", None)
    monkeypatch.setattr(ble_proto, "FAIL_ON_HANDSHAKE_REJECTED", True)
    device = FakeNotePin(hs_status=1, silent={3, 4, 6, 9})
    assert connect(monkeypatch, device)["connected"] is False
    last = dev.get_last_handshake()
    assert last["verdict"] == ble_proto.HANDSHAKE_REJECTED
    assert last["statuses"] == [1, 1]
    assert last["token_len"] == 32


def test_no_reply_handshake_records_missing_statuses(connect_env, monkeypatch):  # noqa: F811
    monkeypatch.setitem(dev._device_state, "last_handshake", None)
    device = FakeNotePin(hs_status=None, silent={3, 4})
    assert connect(monkeypatch, device)["connected"] is True
    last = dev.get_last_handshake()
    assert last["verdict"] == ble_proto.HANDSHAKE_NO_REPLY
    assert last["statuses"] == [None, None]
    assert last["port_version"] is None
