"""POST /device/troubleshoot: why the NotePin won't connect, and what to do about it.

Behind Settings › Device › Troubleshoot connection. `_gather()` collects what
the app can see for itself (Bluetooth, a scan, the pairing credentials on this
Mac, the Plaud sign-in, which account Plaud says the NotePin is on, how the
last connect ended). `diagnose()` turns those facts and the user's answers
(what the light is doing, whether Plaud's phone app is open) into checks, the
questions worth asking, and findings with the steps to fix each.

Like GET /diagnostics, nothing here carries a secret: the last four characters
of a serial number only, never tokens, signatures or user ids. The account
email is shown in the window but left out of `summary`, the text that goes
into an exported bug report.

Device behaviour that hasn't been seen on hardware (what the light means is
Plaud's own description) is worded so the advice is safe either way.
"""

from flask import Blueprint, jsonify, request

import app.state.cloud as cloud_state
import app.state.device as dev
from app.utils.ble_proto import handshake_rejected_error

bp = Blueprint("troubleshoot", __name__)

SCAN_S = 6
WEAK_RSSI = -85

_OPTIONS_YES_NO = [["yes", "Yes"], ["no", "No"], ["unsure", "Not sure"]]
QUESTIONS = {
    "led": {
        "text": "What is the light on the NotePin doing?",
        "options": [
            ["white", "Steady white"],
            ["purple_flash", "Flashing purple"],
            ["purple", "Steady purple"],
            ["green", "Green"],
            ["off", "No light"],
            ["unsure", "Something else"],
        ],
    },
    "phone_app": {"text": "Is Plaud's app open on a phone or tablet nearby?", "options": _OPTIONS_YES_NO},
    "paired_elsewhere": {
        "text": "Has this NotePin been set up in Plaud's app before?",
        "options": [
            ["same", "Yes, with the Plaud account I use here"],
            ["other", "Yes, with another account, or I'm not sure which"],
            ["no", "No"],
            ["unsure", "Not sure"],
        ],
    },
}

RESTART_STEP = (
    "Restart the NotePin: hold its button for about 12 seconds. This only restarts it; "
    "recordings and pairing stay as they are."
)


def _finding(fid, title, *steps, action=None):
    """One thing to fix. `action` names a button the window adds: "sign_in" or "import_pairing"."""
    return {"id": fid, "title": title, "steps": list(steps), "action": action}


_BLUETOOTH_FINDINGS = {
    "off": _finding(
        "bluetooth_off",
        "Bluetooth is turned off on this Mac",
        "Open System Settings › Bluetooth and turn Bluetooth on.",
        "Come back here and choose Try again.",
    ),
    "denied": _finding(
        "bluetooth_denied",
        "Open Pin isn't allowed to use Bluetooth",
        "Open System Settings › Privacy & Security › Bluetooth and switch on Open Pin.",
        "If macOS asks to quit and reopen Open Pin, let it. Then try again.",
    ),
    "busy": _finding(
        "bluetooth_busy",
        "Open Pin is busy talking to a NotePin",
        "A sync or another connection attempt is still running. Wait a minute, then choose Try again.",
    ),
}
_BLUETOOTH_UNAVAILABLE_FINDING = _finding(
    "bluetooth_unavailable",
    "Bluetooth isn't available right now",
    "Turn Bluetooth off and on again in System Settings › Bluetooth, then choose Try again.",
    "If that doesn't help, restart this Mac.",
)
_PHONE_APP_FINDING = _finding(
    "phone_app",
    "Plaud's phone app may be holding the connection",
    "A NotePin talks to one device at a time and stops showing up for others while it's connected.",
    "Close Plaud's app on the phone (swipe it away), or turn off the phone's Bluetooth for a moment.",
    "Then choose Try again.",
)


def _serial_of(device):
    return device.get("serial_number") or dev.paired_serial_for_address(device.get("address"))


def _scan(facts):
    """NotePins advertising now ([] with facts["bluetooth"] set if the scan couldn't run)."""
    try:
        return dev.run_async(dev.scan_unless_connected(1, SCAN_S), timeout=30, lock_timeout=10) or []
    except TimeoutError:  # a sync or a connect holds the Bluetooth lock
        facts["bluetooth"] = "busy"
    except Exception as e:
        problem = dev.bluetooth_unavailable(e)
        facts["bluetooth"] = problem[0] if problem else "unavailable"
    return []


def _gather():
    facts = {"bluetooth": "on", "notepins": [], "rssi": None}
    paired = dev.get_paired_serials()
    devices = [] if dev.is_connected() else _scan(facts)
    facts["connected"] = bool(dev.is_connected())
    for d in devices:
        sn = _serial_of(d)
        facts["notepins"].append(
            {
                "name": d.get("name") or "Plaud device",
                "rssi": d.get("rssi"),
                "serial_last4": sn[-4:] if sn else None,
            }
        )
    # With a NotePin paired, one advertising another serial is someone else's.
    ours = [d for d in devices if not paired or not _serial_of(d) or _serial_of(d) in paired]
    facts["found"] = bool(ours)
    facts["others_only"] = bool(devices) and not ours

    # The NotePin in question: a paired one in range, else the strongest with a
    # serial, else the only one this Mac has paired.
    sn = dev.get_state().get("serial_number") if facts["connected"] else None
    if not sn:
        with_serial = [d for d in ours if _serial_of(d)]
        target = next((d for d in with_serial if _serial_of(d) in paired), with_serial[0] if with_serial else None)
        if target:
            sn, facts["rssi"] = _serial_of(target), target.get("rssi")
        elif len(paired) == 1:
            sn = next(iter(paired))
    facts["serial_last4"] = sn[-4:] if sn else None
    facts["has_sn_signature"] = bool(sn and cloud_state.get_device_sn_signature(sn))
    facts["has_bind_token"] = bool(sn and cloud_state.get_device_bind_token(sn))

    signed_in = bool(cloud_state.is_logged_in())
    user = cloud_state.get_state().get("user") or {}
    facts["cloud"] = {
        "signed_in": signed_in,
        "account": (user.get("email") or user.get("nickname") or "") if signed_in else "",
    }
    facts["binding"] = cloud_state.check_device_binding(sn) if signed_in and sn else None
    facts["last_connect"] = dev.get_last_connect()
    facts["last_handshake"] = dev.get_last_handshake()
    return facts


def _signal(rssi):
    if rssi is None:
        return ""
    return f", signal {'strong' if rssi > -65 else 'fine' if rssi >= WEAK_RSSI else 'weak'} ({rssi} dBm)"


def _checks(facts):
    """What was found, one line each: {id, label, state: ok | warn | bad | info, detail}."""
    cloud = facts.get("cloud") or {}
    last = facts.get("last_connect") or {}
    binding = facts.get("binding") or {}
    tail = facts.get("serial_last4")
    checks = []

    def add(cid, label, state, detail):
        checks.append({"id": cid, "label": label, "state": state, "detail": detail})

    bluetooth = facts.get("bluetooth")
    add(
        "bluetooth",
        "Bluetooth on this Mac",
        "ok" if bluetooth == "on" else "warn" if bluetooth == "busy" else "bad",
        {
            "on": "On, and Open Pin may use it",
            "off": "Turned off",
            "denied": "Open Pin isn't allowed to use it",
            "busy": "Busy with a sync or another connection attempt",
        }.get(bluetooth, "Not available"),
    )
    if facts.get("connected"):
        add("notepin", "NotePin", "ok", "Connected")
    elif bluetooth == "on":
        pins = facts.get("notepins") or []
        if facts.get("others_only"):
            other = ", ".join(sorted({p["serial_last4"] for p in pins if p.get("serial_last4")}))
            add(
                "notepin", "NotePin in range", "bad", f"Only someone else's (serial ending {other}); yours wasn't found"
            )
        elif pins:
            seen = "; ".join(f"{p.get('name') or 'Plaud device'}{_signal(p.get('rssi'))}" for p in pins[:3])
            weak = facts.get("rssi") is not None and facts["rssi"] < WEAK_RSSI
            add("notepin", "NotePin in range", "warn" if weak else "ok", f"Found: {seen}")
        else:
            add("notepin", "NotePin in range", "bad", f"None found in a {SCAN_S} second scan")
    if tail:
        missing = [
            name
            for name, have in (("serial signature", "has_sn_signature"), ("bind token", "has_bind_token"))
            if not facts.get(have)
        ]
        add("serial", "Serial number", "info", f"Ending {tail}")
        add(
            "credentials",
            "Pairing credentials on this Mac",
            "bad" if missing else "ok",
            f"Missing: {' and '.join(missing)}" if missing else "Both saved",
        )
    elif facts.get("found"):
        add("serial", "Serial number", "warn", "The NotePin didn't send it during the scan")
    account = f" as {cloud['account']}" if cloud.get("account") else ""
    add("cloud", "Plaud sign-in", "ok" if cloud.get("signed_in") else "info", f"Signed in{account}")
    if not cloud.get("signed_in"):
        checks[-1]["detail"] = "Not signed in"
    if binding:
        state = binding.get("state")
        add(
            "binding",
            "Plaud account the NotePin is on",
            {"self": "ok", "other": "bad"}.get(state, "info"),
            {
                "self": "The one you're signed in to",
                "other": "A different Plaud account",
                "none": "Not registered to any account yet",
            }.get(state, f"Couldn't tell ({binding.get('msg') or 'no answer'})"),
        )
    if last:
        add(
            "last_connect",
            "Last connection attempt",
            "ok" if last.get("connected") else "bad",
            "Connected" if last.get("connected") else (last.get("error") or "Failed"),
        )
    return checks


def _other_account(cloud):
    signed_in = (
        f"You're signed in to Plaud as {cloud['account']}."
        if cloud.get("account")
        else "Check which Plaud account you're signed in to under Settings › Plaud Cloud."
        if cloud.get("signed_in")
        else "Sign in under Settings › Plaud Cloud with the account the NotePin is on."
    )
    return _finding(
        "other_account",
        "This NotePin is registered to a different Plaud account",
        signed_in,
        "You may have two Plaud accounts without knowing: Sign in with Apple (with Hide My Email) makes a "
        "separate account from signing in with your email address or Google. Sign out here and sign in the "
        "way you did when you first set the NotePin up.",
        "If it's on someone else's account, they remove it in Plaud's app (the device › Unbind). Then "
        "connect again here.",
        "Restarting or resetting the NotePin with its button doesn't clear this. Only the account it's on can.",
        action=None if cloud.get("signed_in") else "sign_in",
    )


def _not_found(answers):
    out = []
    if answers.get("phone_app") == "yes":
        out.append(_PHONE_APP_FINDING)
    led = answers.get("led")
    if led == "purple_flash":
        out.append(
            _finding(
                "low_battery",
                "The NotePin's battery is low",
                "Plaud says a flashing purple light means low battery. Put it on its charger: steady purple "
                "is charging, green is full.",
                "Once it has some charge and shows a steady white light, it's ready. Choose Try again.",
            )
        )
    elif led == "off":
        out.append(
            _finding(
                "no_light",
                "The NotePin looks switched off or out of battery",
                "Put it on its charger for a few minutes, then press its button.",
                "A steady white light means it's ready to connect. Choose Try again.",
            )
        )
    elif led in ("purple", "green"):
        out.append(
            _finding(
                "charging",
                "The NotePin is on charge",
                "Steady purple is charging and green is full. If this Mac still can't see it, take it off "
                "the charger, wait for a steady white light (ready to connect) and choose Try again.",
            )
        )
    out.append(
        _finding(
            "not_in_range",
            "This Mac can't see the NotePin",
            "Keep the NotePin within a metre or two of this Mac.",
            "Make sure no phone nearby has Plaud's app connected to it.",
            "If it's recording or updating its firmware, wait until it has finished.",
            "Turn this Mac's Bluetooth off and on again (System Settings › Bluetooth).",
            RESTART_STEP,
        )
    )
    return out


def _found(facts, answers):
    cloud = facts.get("cloud") or {}
    last = facts.get("last_connect") or {}
    code = None if last.get("connected") else last.get("code")
    binding = (facts.get("binding") or {}).get("state")
    missing = bool(facts.get("serial_last4")) and not (facts.get("has_sn_signature") and facts.get("has_bind_token"))
    out = []

    if (
        binding == "other"
        or code == "bind_refused"
        or (answers.get("paired_elsewhere") == "other" and binding != "self")
    ):
        out.append(_other_account(cloud))
    elif missing and not cloud.get("signed_in"):
        out.append(
            _finding(
                "sign_in",
                "This Mac doesn't have the NotePin's pairing credentials",
                "They come from Plaud once per NotePin. Sign in under Settings › Plaud Cloud with the account "
                "the NotePin is on, then choose Try again. You can sign out afterwards.",
                "Or import a pairing file exported from a Mac where this NotePin already works "
                "(Settings › Device › Advanced › Import pairing).",
                action="sign_in",
            )
        )
    elif code == "wrong_token_type":
        out.append(
            _finding(
                "import_pairing",
                "Plaud won't hand out the pairing to this sign-in",
                "Sign out under Settings › Plaud Cloud, sign in again with the browser button and choose Try again.",
                "If it still fails, import a pairing file exported from a Mac where this NotePin already works.",
                action="import_pairing",
            )
        )
    elif missing:
        out.append(
            _finding(
                "fetch_credentials",
                "The pairing credentials aren't on this Mac yet",
                "Choose Try again: Open Pin fetches them from Plaud as it connects.",
                "If that fails, sign out and in again under Settings › Plaud Cloud, with the account the NotePin is on.",
            )
        )

    if code == "other_app_key":
        out.append(
            _finding(
                "other_app_key",
                "The NotePin is paired with another app",
                "It answered with a key meant for another app, such as Plaud's phone app.",
                "Unpair or remove the NotePin in that app, close the app, then choose Try again.",
            )
        )
    elif code == "handshake_rejected":
        statuses = (facts.get("last_handshake") or {}).get("statuses") or []
        out.append(
            _finding("handshake_rejected", "The NotePin refused the pairing", handshake_rejected_error(statuses))
        )
    elif code == "no_reply":
        out.append(
            _finding(
                "no_reply",
                "The NotePin didn't answer the pairing request",
                "Move it closer and make sure Plaud's phone app isn't connected to it, then choose Try again.",
                "If you imported a pairing file, check it was exported for this NotePin "
                f"(serial ending {facts.get('serial_last4') or 'unknown'}).",
            )
        )
    if answers.get("phone_app") == "yes" and code != "other_app_key":
        out.append(_PHONE_APP_FINDING)
    if facts.get("rssi") is not None and facts["rssi"] < WEAK_RSSI:
        out.append(
            _finding(
                "weak_signal",
                "The Bluetooth signal is weak",
                "Move the NotePin next to this Mac, away from other wireless devices, then choose Try again.",
            )
        )
    if not facts.get("serial_last4"):
        out.append(
            _finding(
                "serial_unknown",
                "The NotePin's serial number didn't come through",
                "Open Pin needs it to pair. Move the NotePin closer and choose Try again.",
            )
        )
    return out


def diagnose(facts, answers=None):
    """Checks, the questions worth asking, findings and a bug-report summary for `facts`."""
    answers = answers if isinstance(answers, dict) else {}
    bluetooth = facts.get("bluetooth")
    ask, findings = [], []
    if facts.get("connected"):
        findings.append(
            _finding(
                "connected",
                "Your NotePin is connected",
                "Nothing to fix here. If something else is wrong, export a bug report below.",
            )
        )
    elif bluetooth != "on":
        findings.append(_BLUETOOTH_FINDINGS.get(bluetooth, _BLUETOOTH_UNAVAILABLE_FINDING))
    elif not facts.get("found"):
        ask = ["led", "phone_app"]
        findings = _not_found(answers)
    else:
        ask = ["phone_app", "paired_elsewhere"]
        findings = _found(facts, answers) or [
            _finding(
                "nothing_obvious",
                "Nothing obvious is wrong",
                "Choose Try again.",
                "If the NotePin is recording or updating its firmware, wait until it has finished.",
                RESTART_STEP,
                "Still stuck? Export a bug report below and send it to us.",
            )
        ]

    checks = _checks(facts)
    summary = ["Connection troubleshooter"]
    for c in checks:
        detail = (
            ("Signed in" if (facts.get("cloud") or {}).get("signed_in") else "Not signed in")
            if c["id"] == "cloud"
            else c["detail"]
        )
        summary.append(f"- {c['label']}: {detail}")
    summary += [f"- Answer ({q}): {a}" for q in ask for a, _label in QUESTIONS[q]["options"] if answers.get(q) == a]
    summary += [f"- Suggested: {f['title']}" for f in findings]
    return {
        "checks": checks,
        "questions": [{"id": q, **QUESTIONS[q]} for q in ask],
        "findings": findings,
        "summary": "\n".join(summary),
    }


@bp.route("/device/troubleshoot", methods=["POST"])
def troubleshoot():
    """Body {} runs the checks (a scan, so several seconds). Body {facts, answers}
    re-reads earlier facts with the user's answers, without scanning again."""
    body = request.get_json(silent=True) or {}
    facts = body["facts"] if isinstance(body.get("facts"), dict) else _gather()
    return jsonify({"facts": facts, **diagnose(facts, body.get("answers"))})
