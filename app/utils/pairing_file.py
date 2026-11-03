"""Pairing export files: Settings › Device › Export / Import pairing.

An export holds the credentials that let anyone with the file connect to the
NotePin, so it's written with mode 0600, never into the app bundle, the repo
or the app's own data folder (a cleaner that wipes that would take the
backup too), and an import is validated strictly before anything is saved.

Format (version 1)::

    {"format": "plaud-v2-pairing", "version": 1, "exported_at": "<ISO 8601>",
     "devices": [{"serial", "name", "sn_signature", "bind_token"}]}
"""

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from app.utils import pairing_store
from app.utils.config import load_config, update_config
from app.utils.constants import DATA_DIR, PROJECT_ROOT

# The app's pre-rename name; a file-format id, kept so older exports import.
FORMAT = "plaud-v2-pairing"
VERSION = 1
MAX_FILE_BYTES = 64 * 1024
MAX_DEVICES = 32
_TOP_KEYS = {"format", "version", "exported_at", "devices"}
_DEVICE_KEYS = {"serial", "name", "sn_signature", "bind_token"}
_REQUIRED_DEVICE_KEYS = {"serial", "sn_signature", "bind_token"}


class PairingFileError(ValueError):
    """A user-facing reason the file or path was refused (never quotes a credential)."""


# ---------------------------------------------------------------------------
# Building and parsing
# ---------------------------------------------------------------------------


def exportable_devices(config: dict | None = None) -> list[dict]:
    """Paired devices that have both credentials in the config."""
    return pairing_store.paired_devices_with_credentials(load_config() if config is None else config)


def summary(devices) -> list[dict]:
    """What the UI may show about devices: name and serial tail, no credentials."""
    return [{"serial_tail": pairing_store.serial_tail(d["serial"]), "name": d.get("name")} for d in devices]


def build_export(devices, now: datetime | None = None) -> dict:
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": (now or datetime.now().astimezone()).isoformat(timespec="seconds"),
        "devices": [
            {
                "serial": d["serial"],
                "name": d.get("name") or pairing_store.DEFAULT_DEVICE_NAME,
                "sn_signature": d["sn_signature"],
                "bind_token": d["bind_token"],
            }
            for d in devices
        ],
    }


def parse_export(data) -> list[dict]:
    """Validate a parsed export; return its devices or raise PairingFileError."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise PairingFileError("This isn't an Open Pin pairing export.")
    version = data.get("version")
    if type(version) is not int:
        raise PairingFileError("The pairing file has no valid version number.")
    if version != VERSION:
        raise PairingFileError(
            f"This pairing file is version {version}; this app reads version {VERSION}. Update Open Pin."
        )
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise PairingFileError(f"The pairing file has unexpected fields: {', '.join(sorted(unknown))[:200]}.")
    exported_at = data.get("exported_at")
    if exported_at is not None and not (isinstance(exported_at, str) and len(exported_at) <= 64):
        raise PairingFileError("The pairing file's exported_at isn't a date.")
    devices = data.get("devices")
    if not isinstance(devices, list) or not devices:
        raise PairingFileError("The pairing file lists no devices.")
    if len(devices) > MAX_DEVICES:
        raise PairingFileError(f"The pairing file lists more than {MAX_DEVICES} devices.")

    out, seen = [], set()
    for i, d in enumerate(devices, 1):
        where = f"Device {i} in the pairing file"
        if not isinstance(d, dict):
            raise PairingFileError(f"{where} isn't an object.")
        unknown = set(d) - _DEVICE_KEYS
        if unknown:
            raise PairingFileError(f"{where} has unexpected fields: {', '.join(sorted(unknown))[:200]}.")
        missing = _REQUIRED_DEVICE_KEYS - set(d)
        if missing:
            raise PairingFileError(f"{where} is missing: {', '.join(sorted(missing))}.")
        serial = d["serial"]
        if not pairing_store.is_valid_serial(serial):
            raise PairingFileError(f"{where} has an invalid serial number (expected uppercase hex).")
        if serial in seen:
            raise PairingFileError(f"The pairing file lists the serial ending {serial[-4:]} twice.")
        seen.add(serial)
        if not pairing_store.is_valid_signature(d["sn_signature"]):
            raise PairingFileError(f"{where} has an invalid SN signature (expected base64).")
        if not pairing_store.is_valid_token(d["bind_token"]):
            raise PairingFileError(f"{where} has an invalid bind token (expected up to 64 ASCII characters).")
        name = d.get("name")
        if name is not None and not pairing_store.is_valid_name(name):
            raise PairingFileError(f"{where} has an invalid name (1 to {pairing_store.MAX_NAME_CHARS} characters).")
        out.append(
            {
                "serial": serial,
                "name": name or pairing_store.DEFAULT_DEVICE_NAME,
                "sn_signature": d["sn_signature"],
                "bind_token": d["bind_token"],
            }
        )
    return out


def parse_export_bytes(raw: bytes) -> list[dict]:
    if len(raw) > MAX_FILE_BYTES:
        raise PairingFileError("That file is too big to be a pairing export.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PairingFileError("That file isn't an Open Pin pairing export (not JSON).") from None
    return parse_export(data)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    try:
        root = root.resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def _forbidden_reason(path: Path) -> str | None:
    if any(part.lower().endswith(".app") for part in path.parts):
        return "Choose a location outside an app bundle."
    if _inside(path, PROJECT_ROOT):
        return "Choose a location outside Open Pin's own files."
    if _inside(path, DATA_DIR):
        return (
            "Choose a location outside Open Pin's data folder: a cleaner that wipes that folder would delete "
            "the export too."
        )
    return None


def _absolute(raw) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise PairingFileError("No file was chosen.")
    if "\x00" in raw:
        raise PairingFileError("That path isn't valid.")
    path = Path(raw)
    if not path.is_absolute():
        raise PairingFileError("The path must be absolute.")
    return path


def resolve_export_path(raw) -> Path:
    """Validate where an export may be written; return the target path."""
    path = _absolute(raw)
    if path.suffix.lower() != ".json":
        raise PairingFileError("Save the export as a .json file.")
    try:
        parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise PairingFileError("That folder doesn't exist.") from None
    if not parent.is_dir():
        raise PairingFileError("That folder doesn't exist.")
    target = parent / path.name
    if target.is_symlink():
        raise PairingFileError("Won't write through a symbolic link; choose another name.")
    if target.exists() and not target.is_file():
        raise PairingFileError("Something that isn't a file already has that name.")
    reason = _forbidden_reason(target)
    if reason:
        raise PairingFileError(reason)
    return target


def resolve_import_path(raw) -> Path:
    """Validate a pairing file to import; return its resolved path."""
    path = _absolute(raw)
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise PairingFileError("That file doesn't exist.") from None
    if not target.is_file():
        raise PairingFileError("That isn't a file.")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise PairingFileError("That file is too big to be a pairing export.")
    return target


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------


def write_export(target: Path, data: dict) -> None:
    """Write ``data`` to ``target`` atomically with mode 0600."""
    text = json.dumps(data, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".plaud-pairing-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def export_to(raw_path) -> dict:
    """Export every paired device to ``raw_path``. Returns a summary."""
    target = resolve_export_path(raw_path)
    devices = exportable_devices()
    if not devices:
        raise PairingFileError("There's no paired device to export yet. Connect your NotePin first.")
    write_export(target, build_export(devices))
    logging.info("[Pairing] Exported the pairing for %d device(s)", len(devices))
    return {"exported": len(devices), "devices": summary(devices), "path": str(target)}


def merge(devices) -> None:
    """Save imported credentials into the config (overwriting that serial's
    old ones) and add missing paired_devices entries."""

    def _apply(cfg):
        sigs = cfg.get("device_sn_signatures")
        if not isinstance(sigs, dict):
            sigs = cfg["device_sn_signatures"] = {}
        tokens = cfg.get("device_bind_tokens")
        if not isinstance(tokens, dict):
            tokens = cfg["device_bind_tokens"] = {}
        paired = cfg.get("paired_devices")
        if not isinstance(paired, dict):
            paired = cfg["paired_devices"] = {}
        for d in devices:
            sigs[d["serial"]] = d["sn_signature"]
            tokens[d["serial"]] = d["bind_token"]
            if d["serial"] not in paired:
                paired[d["serial"]] = {"name": d["name"]}

    update_config(_apply)


def import_from(raw_path) -> dict:
    """Import a pairing file: validate, merge into the config, back up to the Keychain."""
    target = resolve_import_path(raw_path)
    try:
        raw = target.read_bytes()
    except OSError:
        raise PairingFileError("Couldn't read that file.") from None
    devices = parse_export_bytes(raw)
    merge(devices)
    keychain_saved = 0
    for d in devices:
        if pairing_store.save(d["serial"], d["sn_signature"], d["bind_token"], d["name"]):
            keychain_saved += 1
    logging.info(
        "[Pairing] Imported the pairing for serial(s) ending %s",
        ", ".join(pairing_store.serial_tail(d["serial"]) for d in devices),
    )
    return {
        "imported": summary(devices),
        "keychain_saved": keychain_saved,
        "keychain_available": pairing_store.available(),
    }
