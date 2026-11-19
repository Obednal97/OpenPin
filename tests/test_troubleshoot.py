"""Settings › Device › Troubleshoot connection: POST /device/troubleshoot.

What it gathers, what it asks and what it advises for each state a NotePin or
Plaud account can be in. Nothing in a reply may carry a full serial number, a
token, a signature or a user id.
"""

import json

import pytest

import app.state.cloud as cloud_state
import app.state.device as dev
from app.routes import troubleshoot as ts

SN = "8820A00012345678"
ADDR = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
TOKEN = "BINDTOKEN-SECRET-0123456789abcdef"
SIGNATURE = "SNSIG-SECRET-QUJDREVGR0hJSktMTU5PUA=="
USER = {"id": "user-id-SECRET-4242", "nickname": "Jane", "email": "jane.doe@example.com"}


def facts(**over):
    """A NotePin in range, paired and signed in: nothing wrong."""
    base = {
        "bluetooth": "on",
        "connected": False,
        "found": True,
        "notepins": [{"name": "PLAUD NotePin S", "rssi": -60, "serial_last4": "5678"}],
        "rssi": -60,
        "serial_last4": "5678",
        "has_sn_signature": True,
        "has_bind_token": True,
        "cloud": {"signed_in": True, "account": "jane.doe@example.com"},
        "binding": {"state": "self", "msg": "device already bind to self"},
        "last_connect": None,
        "last_handshake": None,
    }
    return {**base, **over}


def ids(result):
    return [f["id"] for f in result["findings"]]


def asked(result):
    return [q["id"] for q in result["questions"]]


def failed(code, error="It failed."):
    return {"connected": False, "code": code, "error": error, "at": "2026-11-19T10:00:00+00:00"}


# -- diagnose(): one state at a time --------------------------------------------


@pytest.mark.parametrize(
    ("state", "finding", "says"),
    [
        ("off", "bluetooth_off", "System Settings › Bluetooth"),
        ("denied", "bluetooth_denied", "Privacy & Security › Bluetooth"),
        ("busy", "bluetooth_busy", "Wait a minute"),
        ("unavailable", "bluetooth_unavailable", "off and on again"),
    ],
)
def test_bluetooth_problems_come_first_and_ask_nothing(state, finding, says):
    result = ts.diagnose(facts(bluetooth=state, found=False, notepins=[]))
    assert ids(result) == [finding]
    assert says in " ".join(result["findings"][0]["steps"])
    assert result["questions"] == []
    assert result["checks"][0]["state"] in ("bad", "warn")


def test_connected_has_nothing_to_fix():
    result = ts.diagnose(facts(connected=True))
    assert ids(result) == ["connected"] and result["questions"] == []


def test_not_found_asks_about_the_light_and_the_phone_app():
    result = ts.diagnose(facts(found=False, notepins=[], rssi=None))
    assert asked(result) == ["led", "phone_app"]
    assert ids(result) == ["not_in_range"]
    steps = " ".join(result["findings"][0]["steps"])
    # The button "reset" only restarts the NotePin; say so rather than promise more.
    assert "12 seconds" in steps and "only restarts" in steps and "pairing stay" in steps
    assert "recording or updating its firmware" in steps


@pytest.mark.parametrize(
    ("led", "finding", "says"),
    [
        ("purple_flash", "low_battery", "low battery"),
        ("off", "no_light", "charger"),
        ("purple", "charging", "steady white light"),
        ("green", "charging", "steady white light"),
    ],
)
def test_the_light_decides_the_first_advice(led, finding, says):
    result = ts.diagnose(facts(found=False, notepins=[]), {"led": led})
    assert ids(result) == [finding, "not_in_range"]
    assert says in " ".join(result["findings"][0]["steps"])


def test_phone_app_open_is_the_first_suspect_when_nothing_is_found():
    result = ts.diagnose(facts(found=False, notepins=[]), {"led": "white", "phone_app": "yes"})
    assert ids(result) == ["phone_app", "not_in_range"]


def test_found_and_healthy_falls_back_to_general_advice():
    result = ts.diagnose(facts())
    assert asked(result) == ["phone_app", "paired_elsewhere"]
    assert ids(result) == ["nothing_obvious"]
    assert all(c["state"] in ("ok", "info") for c in result["checks"])


def test_signed_out_without_credentials_says_sign_in_or_import():
    result = ts.diagnose(
        facts(has_sn_signature=False, has_bind_token=False, cloud={"signed_in": False, "account": ""}, binding=None)
    )
    assert ids(result) == ["sign_in"]
    finding = result["findings"][0]
    assert finding["action"] == "sign_in"
    assert "Import pairing" in " ".join(finding["steps"])
    credentials = next(c for c in result["checks"] if c["id"] == "credentials")
    assert credentials["state"] == "bad" and "serial signature and bind token" in credentials["detail"]


@pytest.mark.parametrize(
    "over",
    [
        {"binding": {"state": "other", "msg": "device already bind other"}},
        {"binding": {"state": "unknown", "msg": ""}, "last_connect": failed("bind_refused"), "has_bind_token": False},
    ],
)
def test_notepin_on_another_account(over):
    result = ts.diagnose(facts(**over))
    assert ids(result) == ["other_account"]
    steps = " ".join(result["findings"][0]["steps"])
    assert "jane.doe@example.com" in steps  # which account they are signed in to
    assert "Sign in with Apple" in steps and "Hide My Email" in steps  # the two-accounts trap
    assert "Unbind" in steps
    assert "doesn't clear this" in steps  # the button reset


def test_user_says_another_account_and_plaud_cant_tell():
    result = ts.diagnose(facts(binding={"state": "unknown", "msg": ""}), {"paired_elsewhere": "other"})
    assert ids(result) == ["other_account"]
    # Plaud says it's on this account: the answer doesn't override that.
    assert ids(ts.diagnose(facts(), {"paired_elsewhere": "other"})) == ["nothing_obvious"]


def test_wrong_kind_of_sign_in_points_to_import_pairing():
    result = ts.diagnose(facts(has_sn_signature=False, last_connect=failed("wrong_token_type")))
    assert ids(result) == ["import_pairing"]
    assert result["findings"][0]["action"] == "import_pairing"


def test_signed_in_without_credentials_just_tries_again():
    result = ts.diagnose(facts(has_sn_signature=False, has_bind_token=False))
    assert ids(result) == ["fetch_credentials"]


def test_another_apps_key():
    result = ts.diagnose(facts(last_connect=failed("other_app_key")), {"phone_app": "yes"})
    assert ids(result) == ["other_app_key"]  # no second "close the phone app" finding
    assert "Unpair or remove" in " ".join(result["findings"][0]["steps"])


@pytest.mark.parametrize(
    ("status", "says"),
    [(1, "different Plaud account"), (2, "Stop the recording"), (255, "isn't ready to pair")],
)
def test_rejected_handshake_uses_the_status_message(status, says):
    result = ts.diagnose(
        facts(
            last_connect=failed("handshake_rejected"),
            last_handshake={"verdict": "rejected", "statuses": [status, status]},
        )
    )
    assert ids(result) == ["handshake_rejected"]
    assert says in result["findings"][0]["steps"][0]


def test_an_old_failure_is_ignored_once_a_connect_has_worked():
    last = {"connected": True, "code": None, "error": None, "at": "2026-11-19T10:00:00+00:00"}
    result = ts.diagnose(facts(last_connect=last, last_handshake={"verdict": "ok", "statuses": [0, 0]}))
    assert ids(result) == ["nothing_obvious"]


def test_no_reply_weak_signal_and_phone_app_stack_up():
    result = ts.diagnose(facts(rssi=-92, last_connect=failed("no_reply")), {"phone_app": "yes"})
    assert ids(result) == ["no_reply", "phone_app", "weak_signal"]
    assert "serial ending 5678" in " ".join(result["findings"][0]["steps"])
    assert next(c for c in result["checks"] if c["id"] == "notepin")["state"] == "warn"


def test_serial_that_did_not_come_through():
    pin = {"name": "PLAUD", "rssi": -70, "serial_last4": None}
    result = ts.diagnose(facts(notepins=[pin], serial_last4=None, has_sn_signature=False, has_bind_token=False))
    assert ids(result) == ["serial_unknown"]


def test_copy_is_plain_and_summary_leaves_out_the_account():
    answers = {"led": "purple_flash", "phone_app": "yes", "paired_elsewhere": "other"}
    for f in (
        facts(),
        facts(found=False, notepins=[]),
        facts(binding={"state": "other", "msg": ""}, last_connect=failed("other_app_key")),
        facts(bluetooth="off"),
    ):
        result = ts.diagnose(f, answers)
        text = json.dumps(result, ensure_ascii=False)
        assert "—" not in text
        assert "jane.doe@example.com" not in result["summary"]
        assert result["summary"].startswith("Connection troubleshooter")
        for finding in result["findings"]:
            assert f"- Suggested: {finding['title']}" in result["summary"]
    assert "- Answer (phone_app): yes" in ts.diagnose(facts(), answers)["summary"]
    # Only the listed options reach the summary, not whatever a caller sends.
    assert "Answer" not in ts.diagnose(facts(), {"phone_app": "<script>"})["summary"]


# -- the route: gathering facts ---------------------------------------------------


@pytest.fixture
def env(flask_client, monkeypatch):
    """A paired NotePin with credentials, signed in. `env["scan"]` is what the scan sees."""
    from app.utils.config import update_config

    update_config(
        lambda c: c.update(
            paired_devices={SN: {"name": "NotePin S", "last_address": ADDR}},
            device_bind_tokens={SN: TOKEN},
            device_sn_signatures={SN: SIGNATURE},
        )
    )
    state = {"scan": [{"name": "PLAUD NotePin S", "address": ADDR, "rssi": -58, "serial_number": SN}], "raise": None}

    async def fake_scan(attempts=2, scan_duration=4):
        if state["raise"]:
            raise state["raise"]
        return state["scan"]

    monkeypatch.setattr(dev, "scan_unless_connected", fake_scan)
    monkeypatch.setitem(dev._device_state, "connected", False)
    monkeypatch.setitem(dev._device_state, "last_connect", None)
    monkeypatch.setitem(dev._device_state, "last_handshake", None)
    monkeypatch.setitem(cloud_state._cloud_state, "logged_in", True)
    monkeypatch.setitem(cloud_state._cloud_state, "token", "eyJ-CLOUD-SESSION-TOKEN-SECRET")
    monkeypatch.setitem(cloud_state._cloud_state, "user", USER)
    state["binding_calls"] = []

    def fake_api(endpoint, method="GET", params=None, json_body=None):
        state["binding_calls"].append((endpoint, json_body))
        return {
            "status": 0,
            "data": {"is_bind": True, "token_list": ["OLD-TOKEN-SECRET"], "msg": "device already bind to self"},
        }

    monkeypatch.setattr(cloud_state, "cloud_api", fake_api)
    state["client"] = flask_client
    return state


def run(env, body=None):
    r = env["client"].post("/device/troubleshoot", json=body or {})
    assert r.status_code == 200
    return r.get_json()


def test_route_gathers_facts_without_secrets(env):
    data = run(env)
    f = data["facts"]
    assert f["bluetooth"] == "on" and f["found"] is True and f["connected"] is False
    assert f["serial_last4"] == "5678" and f["rssi"] == -58
    assert f["has_sn_signature"] is True and f["has_bind_token"] is True
    assert f["cloud"] == {"signed_in": True, "account": "jane.doe@example.com"}
    assert f["binding"] == {"state": "self", "msg": "device already bind to self"}
    assert env["binding_calls"] == [("/device-app/device/check-sn-info", {"sn": SN, "model": "882"})]
    assert ids(data) == ["nothing_obvious"]
    text = json.dumps(data)
    for secret in (SN, TOKEN, SIGNATURE, USER["id"], "OLD-TOKEN-SECRET", "eyJ-CLOUD", ADDR):
        assert secret not in text


def test_route_nothing_in_range_falls_back_to_the_paired_serial(env):
    env["scan"] = []
    data = run(env)
    assert data["facts"]["found"] is False and data["facts"]["serial_last4"] == "5678"
    assert asked(data) == ["led", "phone_app"]


def test_route_recognises_a_paired_notepin_by_its_address(env):
    env["scan"] = [{"name": "PLAUD", "address": ADDR, "rssi": -70}]  # no serial advertised
    assert run(env)["facts"]["serial_last4"] == "5678"


def test_route_signed_out_skips_the_plaud_check(env, monkeypatch):
    monkeypatch.setitem(cloud_state._cloud_state, "logged_in", False)
    data = run(env)
    assert data["facts"]["binding"] is None and data["facts"]["cloud"] == {"signed_in": False, "account": ""}
    assert env["binding_calls"] == []


def test_route_bluetooth_off_and_busy(env):
    class BleakBluetoothNotAvailableError(Exception):
        reason = type("Reason", (), {"name": "POWERED_OFF"})()

    env["raise"] = BleakBluetoothNotAvailableError("Bluetooth device is turned off")
    assert ids(run(env)) == ["bluetooth_off"]
    env["raise"] = TimeoutError("BLE busy")
    assert ids(run(env)) == ["bluetooth_busy"]
    env["raise"] = RuntimeError("boom")
    assert ids(run(env)) == ["bluetooth_unavailable"]


def test_route_connected_does_not_scan(env, monkeypatch):
    monkeypatch.setitem(dev._device_state, "connected", True)
    monkeypatch.setitem(dev._device_state, "serial_number", SN)
    env["raise"] = AssertionError("must not scan while connected")
    data = run(env)
    assert data["facts"]["connected"] is True and ids(data) == ["connected"]


def test_route_answers_reuse_the_facts_without_scanning_again(env):
    first = run(env)
    env["raise"] = AssertionError("must not scan again")
    second = run(env, {"facts": first["facts"], "answers": {"phone_app": "yes"}})
    assert second["facts"] == first["facts"]
    assert ids(second) == ["phone_app"]


def test_route_uses_the_last_connect_error(env):
    env["client"].application  # noqa: B018  (the app is up)
    dev.record_connect(dev._fail("other_app_key", "This NotePin is paired with another app."))
    data = run(env)
    assert ids(data) == ["other_app_key"]
    last = next(c for c in data["checks"] if c["id"] == "last_connect")
    assert last["state"] == "bad" and "paired with another app" in last["detail"]


# -- check-sn-info ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "state"),
    [
        ({"status": 0, "data": {"is_bind": True, "msg": "device already bind to self"}}, "self"),
        ({"status": 0, "is_bind": True, "msg": "device already bind to self"}, "self"),
        ({"status": 0, "data": {"is_bind": True, "msg": "device already bind other"}}, "other"),
        ({"status": -1, "msg": "device already bind other"}, "other"),
        ({"status": 0, "data": {"is_bind": False, "msg": ""}}, "none"),
        ({"status": 0, "data": {"is_bind": True}}, "unknown"),
        ({"status": -3900, "msg": "invalid auth header"}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_check_device_binding_reads_plauds_answer(monkeypatch, reply, state):
    monkeypatch.setattr(cloud_state, "cloud_api", lambda *a, **k: reply)
    assert cloud_state.check_device_binding(SN)["state"] == state


def test_check_device_binding_survives_a_failed_call(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("offline")

    monkeypatch.setattr(cloud_state, "cloud_api", boom)
    assert cloud_state.check_device_binding(SN) == {"state": "unknown", "msg": "offline"}


# -- Bluetooth off / not allowed, and the connect record ------------------------------


class _NotAvailable(Exception):
    def __init__(self, reason):
        super().__init__("Bluetooth is not available")
        self.reason = type("Reason", (), {"name": reason})()


_NotAvailable.__name__ = "BleakBluetoothNotAvailableError"


@pytest.mark.parametrize(
    ("reason", "state", "says"),
    [
        ("POWERED_OFF", "off", "Turn it on in System Settings › Bluetooth"),
        ("DENIED_BY_USER", "denied", "Privacy & Security › Bluetooth"),
        ("DENIED_BY_SYSTEM", "denied", "Privacy & Security › Bluetooth"),
        ("UNKNOWN", "unavailable", "off and on"),
    ],
)
def test_bluetooth_unavailable_is_explained(reason, state, says):
    assert dev.bluetooth_unavailable(ValueError("x")) is None
    got_state, message = dev.bluetooth_unavailable(_NotAvailable(reason))
    assert got_state == state and says in message


def test_scan_and_connect_routes_say_bluetooth_is_off(flask_client, monkeypatch):
    def off(coro, *a, **k):
        coro.close()
        raise _NotAvailable("POWERED_OFF")

    monkeypatch.setattr(dev, "run_async", off)
    monkeypatch.setitem(dev._device_state, "last_connect", None)
    for path, base in (("/device/scan-all", {"devices": []}), ("/device/scan", {"found": False})):
        r = flask_client.post(path)
        assert r.status_code == 200
        body = r.get_json()
        assert body["code"] == "bluetooth_off" and "turned off" in body["error"]
        assert {k: body[k] for k in base} == base
    r = flask_client.post("/device/connect", json={"address": ADDR})
    assert r.get_json()["connected"] is False and r.get_json()["code"] == "bluetooth_off"
    assert dev.get_last_connect()["code"] == "bluetooth_off"


def test_connect_route_records_how_it_ended(flask_client, monkeypatch):
    monkeypatch.setitem(dev._device_state, "last_connect", None)
    result = dev._fail("no_reply", "The NotePin did not respond.")

    def fake(coro, *a, **k):
        coro.close()
        return result

    monkeypatch.setattr(dev, "run_async", fake)
    assert flask_client.post("/device/connect", json={"address": ADDR}).get_json() == result
    last = dev.get_last_connect()
    assert (last["connected"], last["code"], last["error"]) == (False, "no_reply", "The NotePin did not respond.")
    # GET /diagnostics (the bug report) gets the code, not the message.
    assert flask_client.get("/diagnostics").get_json()["connection"]["last_connect"] == {
        "connected": False,
        "code": "no_reply",
        "at": last["at"],
    }

    def boom(coro, *a, **k):
        coro.close()
        raise RuntimeError(f"Device with address {ADDR} exploded")

    monkeypatch.setattr(dev, "run_async", boom)
    assert flask_client.post("/device/connect", json={"address": ADDR}).status_code == 500
    assert dev.get_last_connect()["error"] == "Unexpected error (RuntimeError)"
