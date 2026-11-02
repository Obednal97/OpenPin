"""Back up NotePin pairing credentials in the macOS login Keychain.

A NotePin S only accepts a connection from an app that sends two per-device
credentials, and only Plaud's cloud issues them: the SN signature (config
``device_sn_signatures[sn]``) and the BIND_TOKEN (``device_bind_tokens[sn]``).
They live in the app config, so a Mac cleaner that deletes the data folder
used to cost the pairing, and getting it back needed a Plaud sign-in. This
module keeps a copy in the login Keychain, which cleaners leave alone:

- one generic-password item per device: service :func:`service_name`,
  account = serial, password = JSON ``{serial, name, sn_signature,
  bind_token, saved_at}``;
- one index item (account ``index``, a JSON list of serials), because the
  ``security`` tool can't list a service's items without dumping the whole
  keychain. Serials are uppercase hex, so they can never be ``index``.

The secret never appears in a process's arguments (anyone can read those
with ``ps``). Writes run ``security -i``, which reads its command from stdin,
and pass the password as ``-X <hex>`` (hex also means no quoting of JSON).
Reads use ``find-generic-password -w``, whose argv holds only the service and
account; the password comes back on stdout.

Everything fails soft: a locked, missing or refusing Keychain logs a warning
(never a credential) and the caller carries on with the config alone. Tests
and anyone else can switch it off with ``PLAUD_KEYCHAIN_BACKUP=0``.

Debug builds and any non-default data folder use a separate service
("Plaud V2 (Dev) pairing"), so a dev build never overwrites or restores the
installed app's pairing.
"""

import base64
import binascii
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from app.utils.config import load_config, update_config
from app.utils.constants import DATA_DIR, PACKAGED_DATA_DIR

SECURITY = "/usr/bin/security"
RELEASE_SERVICE = "Plaud V2 pairing"
DEV_SERVICE = "Plaud V2 (Dev) pairing"
INDEX_ACCOUNT = "index"
ENABLE_ENV = "PLAUD_KEYCHAIN_BACKUP"
DEFAULT_DEVICE_NAME = "Plaud NotePin S"
# A Keychain call that shows a dialog nobody answers mustn't hang a request.
TIMEOUT_S = 15

# errSecItemNotFound: `security` exits with 44 when there's no such item.
_RC_NOT_FOUND = 44

# Tests may point this at a throwaway keychain file. None = the default
# (login) keychain. A path that doesn't exist is refused: `security` would
# otherwise quietly fall back to the login keychain.
_KEYCHAIN: str | None = None

# Serialises index read-modify-writes and the background writer.
_lock = threading.RLock()

# ---------------------------------------------------------------------------
# Validation (shared with the export/import file format)
# ---------------------------------------------------------------------------

# The scanner reports serials as the advertised bytes in uppercase hex
# (src/scanner.py parse_manufacturer_data); a NotePin S's is 16 characters.
_SERIAL_RE = re.compile(r"^(?:[0-9A-F]{2}){4,32}$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,64}$")
MAX_NAME_CHARS = 64
_MAX_SIGNATURE_BYTES = 2048


def is_valid_serial(value) -> bool:
    """Uppercase hex, whole bytes, 8 to 64 characters."""
    return isinstance(value, str) and bool(_SERIAL_RE.match(value))


def is_valid_signature(value) -> bool:
    """Standard base64 (with padding) of 16 to 2048 bytes. A NotePin's is 256."""
    if not isinstance(value, str) or len(value) % 4 or not _BASE64_RE.match(value):
        return False
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return 16 <= len(raw) <= _MAX_SIGNATURE_BYTES


def is_valid_token(value) -> bool:
    """Printable ASCII without spaces, 1 to 64 characters."""
    return isinstance(value, str) and bool(_TOKEN_RE.match(value))


def is_valid_name(value) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_NAME_CHARS
        and value.strip() == value
        and all(ch.isprintable() for ch in value)
    )


def serial_tail(serial) -> str:
    """The last four characters of a serial, the only part that goes in logs."""
    return str(serial or "")[-4:] or "????"


# ---------------------------------------------------------------------------
# The `security` tool
# ---------------------------------------------------------------------------


def available() -> bool:
    """True if the backup is switched on and macOS's `security` tool exists."""
    if os.environ.get(ENABLE_ENV, "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return sys.platform == "darwin" and os.access(SECURITY, os.X_OK)


def service_name(data_dir: Path | None = None) -> str:
    """Keychain service for this data folder: the release name only for the
    installed app's folder, the dev name for any other (debug builds,
    PLAUD_DATA_DIR overrides)."""
    data_dir = DATA_DIR if data_dir is None else data_dir
    try:
        is_release = Path(data_dir).resolve() == PACKAGED_DATA_DIR.resolve()
    except OSError:
        is_release = False
    return RELEASE_SERVICE if is_release else DEV_SERVICE


def _quote(arg: str) -> str:
    """Quote one argument for a `security -i` command line."""
    if "\n" in arg or "\r" in arg or "\x00" in arg:
        raise ValueError("argument contains a line break")
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _keychain_args() -> list[str] | None:
    """Trailing keychain argument: [] for the default keychain, or None if a
    configured keychain file is missing (refuse rather than fall back)."""
    if _KEYCHAIN is None:
        return []
    if not os.path.isfile(_KEYCHAIN):
        return None
    return [_KEYCHAIN]


_warned: set = set()
# `security` doesn't echo its input, but just in case: hide any long run with a
# digit in it (hex or base64 credentials), keeping API names such as
# SecKeychainItemCreateFromContent readable.
_REDACT_RE = re.compile(r"[A-Za-z0-9+/=_-]{16,}")


def _redact(text: str) -> str:
    return _REDACT_RE.sub(lambda m: "[redacted]" if any(c.isdigit() for c in m.group()) else m.group(), text)


def _warn(op: str, rc, stderr: str = ""):
    """Log a Keychain failure once per (operation, exit code), then quietly."""
    first = (stderr or "").strip().splitlines()[:1]
    detail = _redact(first[0])[:200] if first else ""
    msg = f"[Pairing] Keychain {op} failed (exit {rc}){': ' + detail if detail else ''}; carrying on without it"
    key = (op, rc)
    if key in _warned:
        logging.debug(msg)
    else:
        _warned.add(key)
        logging.warning(msg)


def _run(argv: list[str], stdin: str | None = None):
    """Run `security`; return (returncode, stdout, stderr) or None if it couldn't run."""
    try:
        r = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        _warn(argv[1] if len(argv) > 1 else "call", type(e).__name__)
        return None
    return r.returncode, r.stdout or "", r.stderr or ""


def _read_status(account: str) -> tuple[str, str | None]:
    """("ok", password), ("missing", None) or ("error", None)."""
    kc = _keychain_args()
    if kc is None:
        _warn("read", "no-keychain")
        return "error", None
    res = _run([SECURITY, "find-generic-password", "-s", service_name(), "-a", account, "-w", *kc])
    if res is None:
        return "error", None
    rc, out, err = res
    if rc == 0:
        return "ok", out[:-1] if out.endswith("\n") else out
    if rc == _RC_NOT_FOUND:
        return "missing", None
    _warn("read", rc, err)
    return "error", None


def _read(account: str) -> str | None:
    """The password of (service, account), or None (missing or failed)."""
    return _read_status(account)[1]


def _write(account: str, secret: str, label: str) -> bool:
    """Create or update (service, account). The secret goes over stdin only."""
    kc = _keychain_args()
    if kc is None:
        _warn("write", "no-keychain")
        return False
    words = [
        "add-generic-password",
        "-U",
        "-s",
        _quote(service_name()),
        "-a",
        _quote(account),
        "-l",
        _quote(label),
        "-X",
        secret.encode("utf-8").hex(),
        *(_quote(k) for k in kc),
    ]
    res = _run([SECURITY, "-i"], stdin=" ".join(words) + "\n")
    if res is None:
        return False
    rc, _out, err = res
    # `security -i` exits non-zero when the command fails; it also reports
    # "<command>: returned <status>" on stderr.
    if rc != 0 or "returned -" in err:
        _warn("write", rc, err)
        return False
    return True


def _delete(account: str) -> bool:
    kc = _keychain_args()
    if kc is None:
        _warn("delete", "no-keychain")
        return False
    res = _run([SECURITY, "delete-generic-password", "-s", service_name(), "-a", account, *kc])
    if res is None:
        return False
    rc, _out, err = res
    if rc in (0, _RC_NOT_FOUND):
        return True
    _warn("delete", rc, err)
    return False


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def _decode_item(serial: str, text: str | None) -> dict | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if (
        not isinstance(data, dict)
        or data.get("serial") != serial
        or not is_valid_signature(data.get("sn_signature"))
        or not is_valid_token(data.get("bind_token"))
    ):
        logging.warning(
            "[Pairing] Keychain backup for serial ending %s is unreadable; ignoring it", serial_tail(serial)
        )
        return None
    name = data.get("name")
    return {
        "serial": serial,
        "name": name if is_valid_name(name) else None,
        "sn_signature": data["sn_signature"],
        "bind_token": data["bind_token"],
    }


def _parse_index(text: str | None) -> list[str]:
    try:
        data = json.loads(text) if text else []
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return sorted({s for s in data if is_valid_serial(s)})


def list_serials() -> list[str]:
    """Serials in the Keychain index (empty if unavailable)."""
    if not available():
        return []
    return _parse_index(_read(INDEX_ACCOUNT))


def _update_index(change) -> bool:
    """Apply ``change(set_of_serials)`` to the index. Skipped when the index
    can't be read (rewriting it then would drop the other serials)."""
    status, text = _read_status(INDEX_ACCOUNT)
    if status == "error":
        return False
    serials = set(_parse_index(text))
    before = set(serials)
    change(serials)
    if serials == before:
        return True
    return _write(INDEX_ACCOUNT, json.dumps(sorted(serials)), "Plaud V2 paired devices")


def load(serial: str) -> dict | None:
    """The backed-up credentials for ``serial``, or None."""
    if not available() or not is_valid_serial(serial):
        return None
    return _decode_item(serial, _read(serial))


def save(serial: str, sn_signature: str, bind_token: str, name: str | None = None) -> bool:
    """Back up one device's credentials (and add it to the index)."""
    if not available():
        return False
    if not (is_valid_serial(serial) and is_valid_signature(sn_signature) and is_valid_token(bind_token)):
        logging.warning("[Pairing] Not backing up serial ending %s: credentials look malformed", serial_tail(serial))
        return False
    payload = json.dumps(
        {
            "serial": serial,
            "name": name if is_valid_name(name) else DEFAULT_DEVICE_NAME,
            "sn_signature": sn_signature,
            "bind_token": bind_token,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    with _lock:
        if not _write(serial, payload, f"Plaud V2 pairing ({serial_tail(serial)})"):
            return False
        _update_index(lambda serials: serials.add(serial))
    return True


def delete(serial: str) -> bool:
    """Remove a device's backup (and its index entry). True if it's gone."""
    if not available() or not is_valid_serial(serial):
        return False
    with _lock:
        ok = _delete(serial)
        _update_index(lambda serials: serials.discard(serial))
    return ok


# ---------------------------------------------------------------------------
# Config <-> Keychain
# ---------------------------------------------------------------------------


def config_credentials(config: dict, serial: str) -> tuple[str | None, str | None]:
    """(sn_signature, bind_token) for ``serial`` in ``config``."""
    sigs = config.get("device_sn_signatures") or {}
    tokens = config.get("device_bind_tokens") or {}
    sig = sigs.get(serial) if isinstance(sigs, dict) else None
    token = tokens.get(serial) if isinstance(tokens, dict) else None
    return sig, token


def paired_devices_with_credentials(config: dict) -> list[dict]:
    """Paired devices whose config holds both valid credentials, by serial."""
    paired = config.get("paired_devices") or {}
    if not isinstance(paired, dict):
        return []
    out = []
    for serial in sorted(paired):
        if not is_valid_serial(serial):
            continue
        sig, token = config_credentials(config, serial)
        if not (is_valid_signature(sig) and is_valid_token(token)):
            continue
        info = paired.get(serial) or {}
        name = info.get("name") if isinstance(info, dict) else None
        out.append(
            {
                "serial": serial,
                "name": name if is_valid_name(name) else DEFAULT_DEVICE_NAME,
                "sn_signature": sig,
                "bind_token": token,
            }
        )
    return out


def backup_serial(serial: str, name: str | None = None) -> bool:
    """Copy ``serial``'s credentials from the config to the Keychain.

    Does nothing unless both are in the config, and skips the write when the
    Keychain already holds the same values. Returns True if the Keychain now
    matches the config.
    """
    if not available() or not is_valid_serial(serial):
        return False
    config = load_config()
    sig, token = config_credentials(config, serial)
    if not (sig and token):
        return False
    if not is_valid_name(name):
        info = (config.get("paired_devices") or {}).get(serial)
        name = info.get("name") if isinstance(info, dict) else None
    with _lock:
        existing = load(serial)
        if existing and existing["sn_signature"] == sig and existing["bind_token"] == token:
            if not is_valid_name(name) or existing.get("name") == name:
                return True
        if not is_valid_name(name) and existing:
            name = existing.get("name")
        return save(serial, sig, token, name)


def restore_serial(serial: str) -> dict | None:
    """Fill ``serial``'s missing credentials in the config from the Keychain.

    Also adds a ``paired_devices`` entry (without ``last_address``) if there
    isn't one. Returns ``{"sn_signature", "bind_token"}`` as now in the config,
    or None when there's nothing to restore.
    """
    if not available() or not is_valid_serial(serial):
        return None
    item = load(serial)
    if not item:
        return None
    filled = []

    def _apply(cfg):
        sigs = cfg.get("device_sn_signatures")
        if not isinstance(sigs, dict):
            sigs = cfg["device_sn_signatures"] = {}
        tokens = cfg.get("device_bind_tokens")
        if not isinstance(tokens, dict):
            tokens = cfg["device_bind_tokens"] = {}
        if not sigs.get(serial):
            sigs[serial] = item["sn_signature"]
            filled.append("sig")
        if not tokens.get(serial):
            tokens[serial] = item["bind_token"]
            filled.append("token")
        paired = cfg.get("paired_devices")
        if not isinstance(paired, dict):
            paired = cfg["paired_devices"] = {}
        if serial not in paired:
            paired[serial] = {"name": item.get("name") or DEFAULT_DEVICE_NAME}
            filled.append("paired")

    try:
        cfg = update_config(_apply)
    except Exception as e:
        logging.warning("[Pairing] Could not write the restored pairing to the config: %s", type(e).__name__)
        return {"sn_signature": item["sn_signature"], "bind_token": item["bind_token"]}
    if filled:
        logging.info("[Pairing] Restored pairing for serial ending %s from the Keychain", serial_tail(serial))
    sig, token = config_credentials(cfg, serial)
    return {"sn_signature": sig, "bind_token": token}


def forget(serials) -> None:
    """Delete the Keychain backups of ``serials`` (after "Erase & Disconnect")."""
    for serial in sorted({s for s in serials if is_valid_serial(s)}):
        if delete(serial):
            logging.info("[Pairing] Removed the Keychain backup for serial ending %s", serial_tail(serial))


def sync_at_startup() -> dict:
    """Back up every paired device's credentials, and restore into the config
    any backed-up device the config has lost. Returns counts."""
    result = {"backed_up": 0, "restored": 0}
    if not available():
        return result
    try:
        config = load_config()
        for device in paired_devices_with_credentials(config):
            if backup_serial(device["serial"], device["name"]):
                result["backed_up"] += 1
        for serial in list_serials():
            sig, token = config_credentials(load_config(), serial)
            if sig and token:
                continue
            if restore_serial(serial):
                result["restored"] += 1
    except Exception as e:  # never let a backup problem stop the app
        logging.warning("[Pairing] Keychain sync at startup failed: %s", type(e).__name__)
    if result["backed_up"] or result["restored"]:
        logging.info(
            "[Pairing] Keychain backup: %d device(s) up to date, %d restored", result["backed_up"], result["restored"]
        )
    return result


# ---------------------------------------------------------------------------
# Background writer: Keychain calls can block (an unanswered dialog), so the
# write sites in the BLE/cloud paths queue their work instead of waiting.
# ---------------------------------------------------------------------------

_jobs: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()  # not _lock: the worker holds that during slow calls


def _work():
    while True:
        fn, args, kwargs = _jobs.get()
        try:
            fn(*args, **kwargs)
        except Exception as e:
            logging.warning("[Pairing] Keychain job failed: %s", type(e).__name__)
        finally:
            _jobs.task_done()


def _submit(fn, *args, **kwargs) -> bool:
    global _worker
    if not available():
        return False
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="pairing-keychain", daemon=True)
            _worker.start()
    _jobs.put((fn, args, kwargs))
    return True


def backup_serial_async(serial: str, name: str | None = None) -> bool:
    return _submit(backup_serial, serial, name)


def forget_async(serials) -> bool:
    return _submit(forget, list(serials))


def start_startup_sync() -> bool:
    return _submit(sync_at_startup)


def flush(timeout: float | None = None) -> bool:
    """Wait for queued Keychain jobs (tests). True if the queue drained."""
    if timeout is None:
        _jobs.join()
        return True
    done = threading.Event()

    def _wait():
        _jobs.join()
        done.set()

    threading.Thread(target=_wait, daemon=True).start()
    return done.wait(timeout)
