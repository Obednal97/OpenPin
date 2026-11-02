#!/usr/bin/env python3
"""WiFi sync worker — runs as standalone subprocess.

Called by the app with session keys + per-device credentials as JSON on stdin.
Outputs JSON result to stdout. Progress updates to stderr as JSON lines.

The args were previously passed via argv but BIND_TOKEN + SN_SIG embedded in
argv are visible to every process on the machine through `ps aux`; stdin keeps
them out of the process table.

Keeping the Mac's WiFi safe:
- The saved ("preferred") networks are left alone by default: the worker joins
  the hotspot directly and HotspotHold rejoins if macOS auto-joins away. Only
  if auto-join keeps winning are they paused, and only when every one can be
  put back exactly (WifiRun.isolate).
- Before any network change a restore record is fsynced to
  <state_dir>/wifi_restore.json; it's deleted only after a verified restore.
  recover_from_record() replays it (startup, or after the parent kills an
  over-time worker). If the record can't be fsynced, the saved networks are
  never paused (HotspotHold just keeps rejoining).
- isolate() and restore_networks() are serialised by WifiRun's lock, and once
  a restore has begun no isolation can start; cleanup also waits for an
  in-flight isolate() before restoring (cancelling its asyncio task doesn't
  stop the thread).
- A restore that keeps failing (MAX_RESTORE_ATTEMPTS, or every missing network
  failing with a "not found / invalid" error, e.g. one the user has since
  deleted) is given up: the record moves to wifi_restore.stuck.json for
  diagnostics and restore_notice() tells the UI once which networks are gone.
  Otherwise every sync would refuse and every launch would retry forever.
- Passwords: restore re-adds each network by SSID + security type only and
  passes NO password. That relies on macOS keeping the keychain items ("AirPort
  network password", keyed by SSID) when -removeallpreferredwirelessnetworks
  removes the profiles. That's expected (networksetup only edits the preferred
  list) but NOT yet verified on real hardware for every macOS version: before
  relying on isolation, check on a real Mac that a paused network rejoins
  without a password prompt after the sync.
- <state_dir>/wifi_sync.lock (flock) allows one worker at a time.
- SIGTERM cancels the transfer; the restore always runs (run_guarded).
- Progress lines carry budget_s / remaining_s (compute_budget) so the parent
  and the UI know how long to wait.
"""

import asyncio
import fcntl
import json
import logging
import math
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# The parent starts this file as a script with the project root on PYTHONPATH;
# make sure app.* imports even without it.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path:
    sys.path.append(_PROJECT_ROOT)

from app.utils.ble_proto import (  # noqa: E402 — needs the path set above
    HANDSHAKE_NO_REPLY,
    HANDSHAKE_NO_REPLY_ERROR,
    HANDSHAKE_REJECTED,
    HANDSHAKE_REJECTED_ERROR,
    SN_SIG_REPLY_WAIT_S,
    SN_SIG_SEND_REPEATS,
    WIFI_CLOSE_REPLY_IDS,
    WIFI_CLOSE_REQUEST,
    WIFI_OPEN_REQUEST,
    bind_handshake,
    cmd_id_of,
    describe_status_reply,
    reply_matches,
)

WS_PORT_CANDIDATES = [8081, 8082, 8083]
BIND_TOKEN = None  # Set from args — per-device token from cloud API


_last_pct = 0


def status(msg, pct=None, **extra):
    """Send progress update to parent via stderr (pct None = keep the last one).

    `extra` carries machine-readable fields, e.g. budget_s / remaining_s.

    Never raises: if the parent app died (closed pipe), a BrokenPipeError here
    would abort the worker's cleanup before the WiFi restore runs and leave the
    Mac on the Plaud hotspot.
    """
    global _last_pct
    if pct is None:
        pct = _last_pct
    _last_pct = pct
    try:
        print(json.dumps({"status": msg, "percent": pct, **extra}), file=sys.stderr, flush=True)
    except (BrokenPipeError, OSError, ValueError):
        pass


def build_wifi_packet(cmd_id, json_data, protocol_version=2):
    json_bytes = json.dumps(json_data, separators=(",", ":")).encode("ascii")
    total_len = 8 + len(json_bytes)
    header = bytearray(8)
    header[0] = total_len & 0xFF
    header[1] = (total_len >> 8) & 0xFF
    header[2] = (total_len >> 16) & 0xFF
    header[3] = protocol_version
    struct.pack_into("<H", header, 4, cmd_id)
    struct.pack_into("<H", header, 6, len(json_bytes))
    return bytes(header) + json_bytes


def parse_wifi_packet(data):
    if len(data) < 8:
        return None, None
    cmd_id = struct.unpack_from("<H", data, 4)[0]
    json_len = struct.unpack_from("<H", data, 6)[0]
    json_bytes = data[8 : 8 + json_len]
    try:
        json_data = json.loads(json_bytes)
    except Exception:
        json_data = {}
    return cmd_id, json_data


DEFAULT_WIFI_INTERFACE = "en0"
_wifi_iface_cache = None


def parse_wifi_device(hardware_ports_output):
    """Find the Wi-Fi device (e.g. en1) in `networksetup -listallhardwareports` output.

    Blocks look like:
        Hardware Port: Wi-Fi
        Device: en1
        Ethernet Address: ...
    Older macOS names the port "AirPort". Returns None if not found.
    """
    port = None
    for line in hardware_ports_output.splitlines():
        line = line.strip()
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port is not None:
            device = line.split(":", 1)[1].strip()
            if port.lower() in ("wi-fi", "wifi", "airport") and device:
                return device
            port = None
    return None


def _corewlan_interface_name():
    try:
        from CoreWLAN import CWWiFiClient

        iface = CWWiFiClient.sharedWiFiClient().interface()
        name = iface.interfaceName() if iface else None
        return str(name) if name else None
    except Exception:
        return None


def _networksetup_wifi_device():
    """Read-only lookup: lists hardware ports, changes nothing."""
    try:
        r = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=5)
        return parse_wifi_device(r.stdout or "")
    except Exception:
        return None


def get_wifi_interface():
    """The Mac's Wi-Fi interface name, detected once per worker run.

    en0 on MacBooks, but en1 on Mac mini / Mac Studio / iMac (en0 is Ethernet).
    CoreWLAN first, then `networksetup -listallhardwareports`, then en0.
    """
    global _wifi_iface_cache
    if _wifi_iface_cache is None:
        _wifi_iface_cache = _corewlan_interface_name() or _networksetup_wifi_device() or DEFAULT_WIFI_INTERFACE
    return _wifi_iface_cache


def get_wifi_ip():
    try:
        r = subprocess.run(["ipconfig", "getifaddr", get_wifi_interface()], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or ""
    except Exception:
        return ""


def _get_current_ssid():
    """Get the SSID we're actually connected to (empty without Location permission)."""
    try:
        from CoreWLAN import CWWiFiClient

        iface = CWWiFiClient.sharedWiFiClient().interface()
        if iface and iface.ssid():
            return str(iface.ssid())
    except Exception:
        pass
    return ""


# ── Durable state: one-worker lock + WiFi restore record ─────────────────────
#
# Everything needed to give the Mac its WiFi back is written to a JSON record in
# the app's data dir, and fsynced, BEFORE each change to the network setup. It
# is deleted only after a verified restore. If the worker is killed, the parent
# (after its timeout) or the next app launch restores from it
# (recover_from_record), and a new worker refuses to start until it has.

RESTORE_RECORD_NAME = "wifi_restore.json"
STUCK_RECORD_NAME = "wifi_restore.stuck.json"
LOCK_NAME = "wifi_sync.lock"
RECORD_VERSION = 1
# Failed restores (in the run, at the next worker start, after a kill, at app
# launch) before the leftover networks are given up on.
MAX_RESTORE_ATTEMPTS = 3


def _fsync_dir(path):
    """fsync a directory (makes a rename/unlink durable). False if it failed."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _write_json_durably(path, data, strict=False):
    """Atomically replace `path` with JSON and fsync it. Raises OSError if the
    file can't be written or fsynced (with strict, also if the directory can't)."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(json.dumps(data, indent=2).encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    if not _fsync_dir(path.parent) and strict:
        raise OSError(f"could not fsync {path.parent}")


def write_restore_record(state_dir, record, strict=False):
    """Atomically replace the restore record and fsync it (file + directory).

    strict=True (used before pausing the saved networks) also raises when the
    directory fsync fails, so the caller can refuse the change.
    """
    _write_json_durably(Path(state_dir) / RESTORE_RECORD_NAME, record, strict)


def read_restore_record(state_dir):
    """The leftover restore record, or None. A corrupt one is set aside, not trusted."""
    path = Path(state_dir) / RESTORE_RECORD_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logging.error(f"[WiFi] Unreadable restore record {path}: {e}; keeping it as {path.name}.corrupt")
        try:
            os.replace(path, path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
        return None
    return data if isinstance(data, dict) else None


def clear_restore_record(state_dir):
    path = Path(state_dir) / RESTORE_RECORD_NAME
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_dir(path.parent)


# -- giving up on a restore that can't succeed ------------------------------

# networksetup / CoreWLAN errors meaning "this network can't be added back"
# (e.g. the user deleted it, or its parameters aren't accepted). Retrying those
# would only fail again.
_PERMANENT_ADD_ERRORS = ("not found", "could not find", "does not exist", "invalid", "not valid", "unknown network")


def is_permanent_add_error(err):
    err = (err or "").lower()
    return any(marker in err for marker in _PERMANENT_ADD_ERRORS)


def _stuck_notice_message(networks):
    return f"Some saved WiFi networks couldn't be restored: {', '.join(networks) if networks else 'unknown'}"


def read_stuck_record(state_dir):
    try:
        data = json.loads((Path(state_dir) / STUCK_RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def note_failed_restore(state_dir, record, check, keep_record=False):
    """Count one failed restore of `record`'s saved networks.

    Gives up after MAX_RESTORE_ATTEMPTS, or straight away when every missing
    network failed with a permanent error: a copy goes to
    wifi_restore.stuck.json (for diagnostics and the UI's one-time notice) and
    the active record is cleared, so syncs aren't refused and launches don't
    retry forever. With keep_record (e.g. still on the hotspot) the record
    stays, minus the saved networks, for the hotspot part of the recovery.

    Returns "gave_up", or "incomplete" (record rewritten with the attempt count).
    """
    missing = list(check.get("missing") or [])
    errors = dict(check.get("errors") or {})
    attempts = int(record.get("restore_attempts") or 0) + 1
    record = {**record, "restore_attempts": attempts, "last_missing": missing, "last_errors": errors}
    permanent = bool(missing) and all(is_permanent_add_error(errors.get(s)) for s in missing)
    if attempts < MAX_RESTORE_ATTEMPTS and not permanent:
        write_restore_record(state_dir, record)
        logging.warning(f"[WiFi] Restore attempt {attempts}/{MAX_RESTORE_ATTEMPTS} failed; missing {missing}")
        return "incomplete"

    previous = read_stuck_record(state_dir) or {}
    networks = list(missing)
    if not previous.get("acknowledged"):
        networks += [s for s in previous.get("missing") or [] if s not in networks]
    stuck = {
        **record,
        "gave_up_at": time.time(),
        "reason": "permanent errors" if permanent else f"{attempts} failed attempts",
        "missing": networks,
        "acknowledged": False,
    }
    try:
        _write_json_durably(Path(state_dir) / STUCK_RECORD_NAME, stuck)
    except OSError as e:
        logging.error(f"[WiFi] Could not write {STUCK_RECORD_NAME}: {e}")
    if keep_record:
        write_restore_record(state_dir, {**record, "isolated": False, "saved_networks": None})
    else:
        clear_restore_record(state_dir)
    logging.error(
        f"[WiFi] Gave up restoring saved networks {missing} after {attempts} attempt(s) "
        f"({stuck['reason']}; errors: {errors}); kept as {STUCK_RECORD_NAME}"
    )
    status(_stuck_notice_message(missing), None)
    return "gave_up"


def restore_notice(state_dir):
    """The one-time "couldn't restore" notice for the UI, or None (none, or acknowledged).

    {"message": "Some saved WiFi networks couldn't be restored: A, B",
     "networks": ["A", "B"], "since": <unix time>}
    """
    stuck = read_stuck_record(state_dir) if state_dir else None
    if not stuck or stuck.get("acknowledged"):
        return None
    networks = list(stuck.get("missing") or [])
    return {"message": _stuck_notice_message(networks), "networks": networks, "since": stuck.get("gave_up_at")}


def acknowledge_restore_notice(state_dir):
    """The UI has shown the notice: stop returning it (the file stays for diagnostics)."""
    stuck = read_stuck_record(state_dir) if state_dir else None
    if not stuck or stuck.get("acknowledged"):
        return False
    _write_json_durably(Path(state_dir) / STUCK_RECORD_NAME, {**stuck, "acknowledged": True})
    return True


class WorkerLock:
    """Exclusive flock on <state_dir>/wifi_sync.lock, holding the owner's PID.

    The kernel drops a flock when its process dies (even on SIGKILL), so a
    crashed worker can never wedge it. The file is unlinked on release; acquire()
    checks it locked the file that's still at the path, so a process that opened
    the old file just before the unlink can't think it holds the lock too.
    """

    def __init__(self, state_dir):
        self.path = Path(state_dir) / LOCK_NAME
        self._fd = None

    def acquire(self):
        """Take the lock without blocking. False if another process holds it."""
        while True:
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
            try:
                same = os.stat(self.path).st_ino == os.fstat(fd).st_ino
            except FileNotFoundError:
                same = False
            if not same:
                os.close(fd)
                continue
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            self._fd = fd
            return True

    def release(self):
        if self._fd is None:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def holder_pid(self):
        try:
            return int(self.path.read_text().strip() or 0) or None
        except (OSError, ValueError):
            return None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


# ── The Mac's saved ("preferred") WiFi networks ──────────────────────────────

# networksetup security names (`networksetup -help`) for CoreWLAN CWSecurity
# values (CoreWLANTypes.h). Enterprise (802.1X credentials), WPA3-only, OWE and
# unknown networks are missing on purpose: networksetup can't re-add them as
# they were, so a list containing one is never removed.
CW_SECURITY_TO_NETWORKSETUP = {
    0: "OPEN",  # kCWSecurityNone
    1: "WEP",  # kCWSecurityWEP
    2: "WPA",  # kCWSecurityWPAPersonal
    3: "WPA2",  # kCWSecurityWPAPersonalMixed (WPA/WPA2)
    4: "WPA2",  # kCWSecurityWPA2Personal
    5: "WPA2",  # kCWSecurityPersonal
    13: "WPA2",  # kCWSecurityWPA3Transition (the AP also accepts WPA2)
}


def _networksetup(*args, timeout=10):
    """Run networksetup; the CompletedProcess, or None if it couldn't run at all."""
    try:
        return subprocess.run(["networksetup", *args], capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        logging.warning(f"[WiFi] networksetup {args[0] if args else ''} failed to run: {e}")
        return None


def parse_preferred_networks(output):
    """SSIDs from `networksetup -listpreferredwirelessnetworks` in auto-join order.

    None when the output isn't a list (an error message) — never [] for an
    error, so a failed read can't be mistaken for "no saved networks".
    """
    lines = output.splitlines()
    if not lines:
        return None
    if not lines[0].startswith("Preferred networks on"):
        return [] if "no preferred" in lines[0].lower() else None
    # Entries are tab-indented; keep any other leading/trailing spaces, which
    # are part of the SSID.
    return [line[1:] if line.startswith("\t") else line.strip() for line in lines[1:] if line.strip()]


def list_preferred_networks():
    r = _networksetup("-listpreferredwirelessnetworks", get_wifi_interface())
    if r is None or r.returncode != 0:
        return None
    return parse_preferred_networks(r.stdout or "")


def _corewlan_profiles():
    """[(ssid or None, CWSecurity int)] in auto-join order, or None if unavailable.

    CWConfiguration.networkProfiles is "in the order in which the profiles
    participate in the auto-join process" (Apple docs). The SSID is withheld
    (None) when the app lacks Location Services permission.
    """
    try:
        from CoreWLAN import CWWiFiClient

        iface = CWWiFiClient.sharedWiFiClient().interface()
        cfg = iface.configuration() if iface else None
        profiles = cfg.networkProfiles() if cfg else None
        if profiles is None:
            return None
        out = []
        for i in range(profiles.count()):
            p = profiles.objectAtIndex_(i)
            ssid = p.ssid()
            out.append((str(ssid) if ssid else None, int(p.security())))
        return out
    except Exception:
        return None


def snapshot_preferred_networks():
    """The saved networks in auto-join order with their security types, or None.

    Names and order come from networksetup (it isn't redacted without Location
    permission); security from CoreWLAN, matched by SSID, or by position when
    CoreWLAN hides SSIDs but lists the same number of profiles. "security" is
    None when it couldn't be determined or networksetup can't express it.
    """
    names = list_preferred_networks()
    if names is None:
        return None
    profiles = _corewlan_profiles() or []
    by_ssid = {ssid: sec for ssid, sec in profiles if ssid}
    same_shape = len(profiles) == len(names)
    snapshot = []
    for i, name in enumerate(names):
        cw = by_ssid.get(name)
        if cw is None and same_shape and profiles[i][0] is None:
            cw = profiles[i][1]
        snapshot.append({"ssid": name, "cw_security": cw, "security": CW_SECURITY_TO_NETWORKSETUP.get(cw)})
    return snapshot


def can_isolate(snapshot):
    """Only remove saved networks we can put back exactly as they were."""
    return bool(snapshot) and all(n.get("security") for n in snapshot)


def check_preferred_restored(expected, current):
    """Compare a snapshot with the current list: which SSIDs are missing, and
    whether the present ones are still in their original relative order."""
    want = [n["ssid"] for n in expected]
    missing = [s for s in want if s not in (current or [])]
    present = [s for s in (current or []) if s in want]
    order_ok = present == [s for s in want if s in present]
    return {"verified": current is not None and not missing, "missing": missing, "order_ok": order_ok}


def remove_all_preferred_networks():
    r = _networksetup("-removeallpreferredwirelessnetworks", get_wifi_interface())
    ok = r is not None and r.returncode == 0 and list_preferred_networks() == []
    status("Paused saved WiFi networks for the transfer" if ok else "Could not pause saved WiFi networks", None)
    return ok


def _restore_via_corewlan(networks):
    """Write the profiles back with CWMutableConfiguration + commitConfiguration.

    Apple: the commit "requires the caller have root privileges or obtain
    administrator privileges with the authorization parameter" — the app runs
    as the user, so this usually fails and networksetup is used instead.
    """
    try:
        from CoreWLAN import CWMutableConfiguration, CWMutableNetworkProfile, CWWiFiClient
        from Foundation import NSData, NSOrderedSet

        iface = CWWiFiClient.sharedWiFiClient().interface()
        if not iface or any(n.get("cw_security") is None for n in networks):
            return False
        cfg = CWMutableConfiguration.alloc().initWithConfiguration_(iface.configuration())
        wanted = {n["ssid"] for n in networks}
        profiles = []
        for n in networks:
            raw = n["ssid"].encode("utf-8")
            p = CWMutableNetworkProfile.alloc().init()
            p.setSsidData_(NSData.dataWithBytes_length_(raw, len(raw)))
            p.setSecurity_(n["cw_security"])
            profiles.append(p)
        for p in cfg.networkProfiles() or []:
            if p.ssid() and str(p.ssid()) not in wanted:
                profiles.append(p)
        cfg.setNetworkProfiles_(NSOrderedSet.orderedSetWithArray_(profiles))
        ok, _err = iface.commitConfiguration_authorization_error_(cfg, None, None)
        return bool(ok)
    except Exception:
        return False


def restore_preferred_networks(networks):
    """Put saved networks back in their ORIGINAL auto-join order and verify.

    CoreWLAN first, then networksetup, adding each missing network at its
    original index (ascending, so the order is rebuilt front to back) with its
    recorded security type. No passwords are passed: macOS keeps them in the
    keychain under the SSID, and removing a preferred network is expected to
    leave that keychain item alone (unverified on real hardware: see the module
    docstring). Returns check_preferred_restored()'s dict plus "errors"
    ({ssid: error} for re-adds that failed).
    """
    if not networks:
        return {"verified": True, "missing": [], "order_ok": True, "errors": {}}
    if _restore_via_corewlan(networks):
        result = check_preferred_restored(networks, list_preferred_networks())
        if result["verified"]:
            status(f"Restored {len(networks)} saved WiFi networks", None)
            return {**result, "errors": {}}
    current = list_preferred_networks() or []
    iface = get_wifi_interface()
    errors = {}
    for index, net in enumerate(networks):
        if net["ssid"] in current:
            continue
        security = net.get("security") or "WPA2"
        r = _networksetup("-addpreferredwirelessnetworkatindex", iface, net["ssid"], str(index), security)
        if r is None or r.returncode != 0:
            err = ((r.stderr or r.stdout or "").strip() if r else "did not run")[:200]
            errors[net["ssid"]] = err
            logging.error(f"[WiFi] Could not re-add saved network #{index}: {err}")
    result = {**check_preferred_restored(networks, list_preferred_networks()), "errors": errors}
    if result["verified"]:
        status(f"Restored {len(networks)} saved WiFi networks", None)
        if not result["order_ok"]:
            logging.warning("[WiFi] Saved networks restored, but not all in their original order")
    else:
        status(f"Could not restore {len(result['missing'])} saved WiFi network(s)", None)
    return result


def forget_network(ssid):
    """Remove one network (the device hotspot) from the saved list."""
    r = _networksetup("-removepreferredwirelessnetwork", get_wifi_interface(), ssid)
    return r is not None and r.returncode == 0


class WifiRun:
    """What one WiFi sync changed on the Mac, mirrored to the restore record.

    Default ("hold") mode changes nothing but joining the hotspot: the saved
    networks stay, and HotspotHold rejoins if macOS auto-joins away. Only when
    auto-join keeps pulling the Mac off the hotspot does isolate() pause the
    saved networks, after first recording them durably.

    isolate() runs on a worker thread (HotspotHold), so every method that
    changes the networks or the record holds self._lock: a restore can't run
    between isolate()'s record write and its removal (it would see every
    network still present, report success, and finish() would clear the record
    just before the removal). Once restore_networks() has started, isolate()
    refuses.
    """

    def __init__(self, state_dir, hotspot_ssid, original_ip="", original_ssid=""):
        self.state_dir = Path(state_dir) if state_dir else None
        self.hotspot_ssid = hotspot_ssid
        self.original_ip = original_ip
        self.original_ssid = original_ssid
        self.hotspot_ip = None
        self.plaud_was_preferred = False
        self.saved_networks = None
        self.isolated = False
        self.touched = False
        self.restoring = False  # set by restore_networks(); no isolation after it
        self.restore_attempts = 0
        self.last_check = None  # restore_preferred_networks()'s result
        self.gave_up = False  # the saved networks couldn't be restored (see note_failed_restore)
        self.started = time.time()
        self._lock = threading.Lock()

    def record(self):
        return {
            "version": RECORD_VERSION,
            "pid": os.getpid(),
            "started": self.started,
            "interface": get_wifi_interface(),
            "hotspot_ssid": self.hotspot_ssid,
            "hotspot_ip": self.hotspot_ip,
            "original_ip": self.original_ip,
            "original_ssid": self.original_ssid,
            "plaud_was_preferred": self.plaud_was_preferred,
            "isolated": self.isolated,
            "saved_networks": self.saved_networks,
            "restore_attempts": self.restore_attempts,
        }

    def _save(self, strict=False):
        """Write the record. Caller holds self._lock."""
        if self.state_dir is not None:
            write_restore_record(self.state_dir, self.record(), strict=strict)

    def begin(self):
        """Call right before the first network change (joining the hotspot)."""
        with self._lock:
            names = list_preferred_networks()
            self.plaud_was_preferred = bool(names) and self.hotspot_ssid in names
            self.touched = True
            self._save()

    def set_hotspot_ip(self, ip):
        with self._lock:
            self.hotspot_ip = ip
            self._save()

    def isolate(self):
        """Pause all saved networks so auto-join can't leave the hotspot. Last resort.

        Refused (False, nothing removed) when the networks couldn't be put back
        exactly, when there's nowhere durable to record them, when the record
        can't be fsynced, or once a restore has started.
        """
        with self._lock:
            if self.isolated:
                return True
            if self.restoring:
                return False
            if self.state_dir is None:
                return False  # no restore record possible: never remove anything
            snapshot = snapshot_preferred_networks()
            if snapshot is not None:
                # The hotspot itself (when it was saved before) is restored like any other.
                snapshot = [n for n in snapshot if n["ssid"] != self.hotspot_ssid or self.plaud_was_preferred]
            if not can_isolate(snapshot):
                status("Keeping saved WiFi networks (they couldn't be restored exactly)", None)
                return False
            self.saved_networks = snapshot
            self.isolated = True
            try:
                self._save(strict=True)  # durable BEFORE the removal
            except OSError as e:
                self.saved_networks = None
                self.isolated = False
                logging.error(f"[WiFi] Could not save the restore record ({e}); not pausing saved networks")
                status("Keeping saved WiFi networks (couldn't save a restore record)", None)
                return False
            return remove_all_preferred_networks()

    def _recorded_networks(self):
        """The saved networks as the durable record has them (the source of truth
        once isolation started), falling back to memory. Caller holds the lock."""
        rec = read_restore_record(self.state_dir) if self.state_dir is not None else None
        if rec and rec.get("isolated") and rec.get("saved_networks"):
            return rec["saved_networks"]
        return self.saved_networks

    def restore_networks(self):
        """Saved networks back (if paused) and the hotspot forgotten. True if verified.

        Waits for an isolate() in progress (the lock) and stops any later one.
        After isolation, the networks come from the restore record and are
        verified against the list as it is now, after the removal finished.
        """
        with self._lock:
            self.restoring = True
            ok = True
            self.last_check = None
            if self.isolated:
                self.last_check = restore_preferred_networks(self._recorded_networks())
                ok = self.last_check["verified"]
            if not self.plaud_was_preferred:
                forget_network(self.hotspot_ssid)
            return ok

    def finish(self, networks_ok):
        """Delete the record once the saved networks are verified and we're off the hotspot.

        A failed network restore counts as an attempt (note_failed_restore);
        after MAX_RESTORE_ATTEMPTS, or permanent errors, it's given up.
        """
        with self._lock:
            if self.state_dir is None or not self.touched:
                return True
            on_hotspot = bool(self.hotspot_ip) and get_wifi_ip() == self.hotspot_ip
            if networks_ok and not on_hotspot:
                clear_restore_record(self.state_dir)
                return True
            if not networks_ok and self.isolated:
                check = self.last_check or {"missing": [n["ssid"] for n in self.saved_networks or []]}
                outcome = note_failed_restore(self.state_dir, self.record(), check, keep_record=on_hotspot)
                self.restore_attempts += 1
                if outcome == "gave_up":
                    self.gave_up = True
                    self.isolated = False
                    self.saved_networks = None
                    if not on_hotspot:
                        return False
            status("WiFi settings not fully restored — the app will retry when it next starts", None)
            return False


# ── Joining and holding the device hotspot ───────────────────────────────────


def hotspot_ip_ok(ip, original_ip):
    """The Plaud hotspot hands out 192.168.84.x; anything else (e.g. an iPhone
    hotspot's 172.20.10.x, or the network we were on) isn't it."""
    return bool(ip) and ip != original_ip and ip.startswith("192.168.")


def _corewlan_join(ssid, password):
    """Directed scan for the hotspot + associateToNetwork:password:error:.

    Leaves the saved networks alone. Without Location permission scan results
    have no SSID, so nothing matches and networksetup is used instead (we never
    associate to a network we can't identify).
    """
    try:
        from CoreWLAN import CWWiFiClient

        iface = CWWiFiClient.sharedWiFiClient().interface()
        if not iface:
            return False
        networks, _err = iface.scanForNetworksWithName_error_(ssid, None)
        best = None
        for net in networks or []:
            if net.ssid() and str(net.ssid()) == ssid and (best is None or net.rssiValue() > best.rssiValue()):
                best = net
        if best is None:
            return False
        ok, _err = iface.associateToNetwork_password_error_(best, password, None)
        return bool(ok)
    except Exception:
        return False


def _join_once(ssid, password):
    """One join attempt (blocking). Returns (ok, detail)."""
    if _corewlan_join(ssid, password):
        return True, "CoreWLAN"
    try:
        r = subprocess.run(
            ["networksetup", "-setairportnetwork", get_wifi_interface(), ssid, password],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return False, "networksetup timed out"
    except Exception as e:
        return False, str(e)
    # networksetup prints errors on stdout with exit code 0 on some macOS versions.
    out = (r.stderr or "").strip() or (r.stdout or "").strip()
    if r.returncode == 0 and not out:
        return True, "networksetup"
    return False, out or f"networksetup exit {r.returncode}"


_pending_join = None  # in-flight blocking join; cleanup waits for it before restoring


async def connect_to_hotspot(ssid, password, original_ip, timeout=45):
    """Join the device hotspot and wait for an IP on it. Returns the IP or None.

    Doesn't disassociate first: joining switches networks directly, and
    disassociating puts macOS in a "user disconnected" state that later stops
    it auto-joining the user's network again.
    """
    global _pending_join
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        status(f"WiFi join attempt {attempt} for {ssid}...", 15)
        _pending_join = asyncio.ensure_future(asyncio.to_thread(_join_once, ssid, password))
        # shield: a SIGTERM cancels us, not the thread; cleanup waits for it.
        ok, detail = await asyncio.shield(_pending_join)
        _pending_join = None
        if ok:
            for _ in range(15):
                await asyncio.sleep(1)
                ip = get_wifi_ip()
                if hotspot_ip_ok(ip, original_ip):
                    status(f"Connected to {ssid} ({ip})", 20)
                    return ip
                if ip and ip != original_ip:
                    status(f"Got IP {ip} but not on hotspot subnet, waiting...", 15)
            status("No hotspot IP after 15s, retrying...", 15)
        else:
            status(f"Join failed: {detail}", 15)
            await asyncio.sleep(2)
    return None


async def wait_for_pending_join(timeout=30):
    """Let a join that was running when we were cancelled finish before restoring."""
    global _pending_join
    fut, _pending_join = _pending_join, None
    if fut is not None and not fut.done():
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout)
        except BaseException:
            pass


class HotspotHold:
    """Keeps the Mac on the device hotspot for the whole transfer.

    Polls the WiFi IP; if macOS auto-joins a saved network (the IP leaves the
    hotspot for two polls in a row) it rejoins. If that happens again, auto-join
    is fighting us and the saved networks are paused (WifiRun.isolate) — only
    if they can be restored exactly. Gives up after max_rejoins.
    """

    def __init__(self, run, ssid, password, poll_s=3.0, max_rejoins=3, rejoin_timeout=30):
        self.run = run
        self.ssid = ssid
        self.password = password
        self.poll_s = poll_s
        self.max_rejoins = max_rejoins
        self.rejoin_timeout = rejoin_timeout
        self.drifts = 0
        self.rejoining = False
        self.failed = None  # reason string once it gives up
        self._isolation = None  # in-flight run.isolate() (a thread; cancelling us doesn't stop it)

    async def wait_isolation(self):
        """Wait for an isolate() that's still running on its thread (cleanup calls
        this before restoring). Each networksetup call in it has a timeout."""
        fut = self._isolation
        if fut is not None and not fut.done():
            try:
                await asyncio.shield(fut)
            except BaseException:
                pass

    async def watch(self):
        misses = 0
        while self.failed is None:
            await asyncio.sleep(self.poll_s)
            ip = await asyncio.to_thread(get_wifi_ip)
            if ip and ip == self.run.hotspot_ip:
                misses = 0
                continue
            misses += 1
            if misses < 2:
                continue
            misses = 0
            self.drifts += 1
            if self.drifts > self.max_rejoins:
                self.failed = "The Mac kept leaving the device hotspot"
                status(self.failed, None)
                return
            self.rejoining = True
            try:
                status(f"Mac left the device hotspot (WiFi now {ip or 'disconnected'}), rejoining...", None)
                if self.drifts >= 2:
                    # Refused (False) if it isn't safe; we then just rejoin again.
                    self._isolation = asyncio.ensure_future(asyncio.to_thread(self.run.isolate))
                    await asyncio.shield(self._isolation)
                new_ip = await connect_to_hotspot(self.ssid, self.password, self.run.original_ip, self.rejoin_timeout)
                if not new_ip:
                    self.failed = "Could not rejoin the device hotspot"
                    status(self.failed, None)
                    return
                self.run.set_hotspot_ip(new_ip)
            finally:
                self.rejoining = False


async def _on_target_network(original_ssid, hotspot_ip, timeout_s=12):
    """Poll for up to timeout_s seconds waiting to be back on original_ssid
    with a non-hotspot IP. Returns (ip, ssid) on success, or (None, None).

    Uses SSID-match rather than IP-equality because DHCP usually assigns a
    fresh IP on reconnect, so comparing IPs produces false negatives.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        ip = get_wifi_ip()
        current_ssid = _get_current_ssid()
        if ip and ip != hotspot_ip:
            # If we know the target SSID, require it to match. Otherwise any
            # non-hotspot IP is good enough.
            if not original_ssid or current_ssid == original_ssid:
                return ip, current_ssid
        await asyncio.sleep(1.5)
    return None, None


async def restore_internet(ssid, hotspot_ip, original_ip="", original_ssid=""):
    """Get the Mac back online after the Plaud hotspot.

    The caller has already restored the saved networks, forgotten the hotspot
    and closed it over BLE. Strategy (least disruptive first):
    0. Wait for macOS to auto-join a saved network on its own
    1. networksetup rejoin of the original SSID (keychain password)
    2-3. Power-cycle WiFi + rejoin / CoreWLAN scan + associate
    4. If all fail, open WiFi settings for manual reconnection
    """
    try:
        from CoreWLAN import CWWiFiClient

        iface = CWWiFiClient.sharedWiFiClient().interface()
    except Exception:
        iface = None

    # Step 0: the hotspot is gone and the saved networks are intact, so
    # auto-join usually reconnects within a few seconds.
    status("Reconnecting to your WiFi...", 91)
    ip, current_ssid = await _on_target_network("", hotspot_ip, timeout_s=15)
    if ip:
        status(f"Restored to {current_ssid or 'network'} ({ip})", 98)
        return {"restored": True, "ssid": current_ssid, "ip": ip}

    # Step 1: Try networksetup first (uses saved keychain password, no power-cycle needed).
    # macOS can take 8–12s to settle after setairportnetwork, so poll rather than
    # sleeping a fixed 5s — the previous fixed sleep was short-circuiting into
    # an unnecessary power-cycle even when the reconnect had actually succeeded.
    if original_ssid:
        status(f"Reconnecting to {original_ssid}...", 92)
        subprocess.run(
            ["networksetup", "-setairportnetwork", get_wifi_interface(), original_ssid],
            capture_output=True,
            timeout=15,
        )
        ip, current_ssid = await _on_target_network(original_ssid, hotspot_ip, timeout_s=12)
        if ip:
            status(f"Restored to {current_ssid or original_ssid} ({ip})", 98)
            return {"restored": True, "ssid": current_ssid or original_ssid, "ip": ip}

    # Step 2-3: Power-cycle WiFi radio + try multiple rejoin methods
    for cycle in range(2):
        off_time = 10 if cycle == 0 else 5
        settle_time = 5 if cycle == 0 else 8
        status(f"Power-cycling WiFi (attempt {cycle + 1}/2)...", 93 + cycle * 2)
        subprocess.run(
            ["networksetup", "-setairportpower", get_wifi_interface(), "off"], capture_output=True, timeout=5
        )
        await asyncio.sleep(off_time)
        subprocess.run(["networksetup", "-setairportpower", get_wifi_interface(), "on"], capture_output=True, timeout=5)
        await asyncio.sleep(settle_time)

        # Try networksetup rejoin (uses keychain)
        if original_ssid:
            status(f"Rejoining {original_ssid}...", 94 + cycle * 2)
            subprocess.run(
                ["networksetup", "-setairportnetwork", get_wifi_interface(), original_ssid],
                capture_output=True,
                timeout=15,
            )
            ip, current_ssid = await _on_target_network(original_ssid, hotspot_ip, timeout_s=10)
            if ip:
                status(f"Restored to {current_ssid or original_ssid} ({ip})", 98)
                return {"restored": True, "ssid": current_ssid or original_ssid, "ip": ip}

        # Try CoreWLAN scan + join as fallback
        if original_ssid and iface:
            status(f"Scanning for {original_ssid}...", 95 + cycle)
            try:
                networks, _ = iface.scanForNetworksWithName_error_(original_ssid, None)
                if networks:
                    best = None
                    enumerator = networks.objectEnumerator()
                    while True:
                        obj = enumerator.nextObject()
                        if obj is None:
                            break
                        if obj.ssid() and str(obj.ssid()) == original_ssid:
                            if best is None or obj.rssiValue() > best.rssiValue():
                                best = obj
                    if best:
                        success, error = iface.associateToNetwork_password_error_(best, None, None)
                        if success:
                            ip, current_ssid = await _on_target_network(original_ssid, hotspot_ip, timeout_s=8)
                            if ip:
                                status(f"Restored to {current_ssid or original_ssid} ({ip})", 98)
                                return {"restored": True, "ssid": current_ssid or original_ssid, "ip": ip}
            except Exception:
                pass

        # Check if macOS auto-joined on its own — accept ANY non-hotspot network
        # since the user is back online. DHCP often hands out a different IP
        # than before, so ip == original_ip was producing false negatives.
        ip = get_wifi_ip()
        if ip and ip != hotspot_ip:
            current = _get_current_ssid()
            status(f"Restored to {current or 'network'} ({ip})", 98)
            return {"restored": True, "ssid": current, "ip": ip}

    # Final check before giving up — we may have reconnected during the last
    # iteration's settle but missed the window. Don't open WiFi settings if
    # we're already back online.
    ip = get_wifi_ip()
    if ip and ip != hotspot_ip:
        current = _get_current_ssid()
        status(f"Restored to {current or 'network'} ({ip})", 98)
        return {"restored": True, "ssid": current, "ip": ip}

    # All automatic methods failed — open WiFi settings for user
    status("Opening WiFi settings...", 96)
    subprocess.run(["open", "x-apple.systempreferences:com.apple.wifi-settings"], capture_output=True)
    msg = f"Please reconnect to {original_ssid}" if original_ssid else "Please reconnect to your WiFi"
    status(msg, 97)
    return {"restored": False, "need_manual": True, "ssid": original_ssid, "message": msg}


# ── Recovery from a leftover record (killed worker, crash, startup) ──────────


def _power_cycle_wifi(off_s=3):
    iface = get_wifi_interface()
    _networksetup("-setairportpower", iface, "off", timeout=5)
    time.sleep(off_s)
    _networksetup("-setairportpower", iface, "on", timeout=5)


def _stuck_on_hotspot(record):
    ip = get_wifi_ip()
    if ip and record.get("hotspot_ip") and ip == record["hotspot_ip"]:
        return True
    r = _networksetup("-getairportnetwork", get_wifi_interface(), timeout=5)
    out = (r.stdout or "") if r else ""
    current = out.strip().split(": ", 1)[-1] if ": " in out else ""
    hotspot = record.get("hotspot_ssid") or ""
    return bool(current) and (current == hotspot or current.startswith(("Plaud", "PLAUD")))


def recover_locked(state_dir, record):
    """Undo what a dead worker left behind, from its record. Caller holds the lock.

    Restores the saved networks in their original order, forgets the hotspot,
    power-cycles WiFi if the Mac is still on the hotspot (macOS then auto-joins
    a saved network), and deletes the record only once the networks verify.

    A failed restore is counted in the record; after MAX_RESTORE_ATTEMPTS (or
    when only networks that fail permanently are missing) it's given up
    ("gave_up": the record is cleared and wifi_restore.stuck.json kept), so a
    network the user deleted can't block WiFi sync forever.
    """
    networks = record.get("saved_networks") if record.get("isolated") else None
    check = restore_preferred_networks(networks) if networks else {"verified": True, "missing": [], "errors": {}}
    if record.get("hotspot_ssid") and not record.get("plaud_was_preferred"):
        forget_network(record["hotspot_ssid"])
    if _stuck_on_hotspot(record):
        logging.warning("[WiFi Recovery] Still on the device hotspot; power-cycling WiFi")
        _power_cycle_wifi()
    if check["verified"]:
        clear_restore_record(state_dir)
        return {"status": "restored", "networks": len(networks or [])}
    outcome = note_failed_restore(state_dir, record, check)
    return {
        "status": outcome,
        "missing": check["missing"],
        "attempts": int(record.get("restore_attempts") or 0) + 1,
    }


def recover_from_record(state_dir):
    """Restore the Mac's WiFi from a leftover record (startup, or after the parent
    killed an over-time worker). Never runs while a live worker holds the lock.

    Returns {"status": "none" | "busy" | "restored" | "incomplete" | "gave_up", ...}.
    """
    if read_restore_record(state_dir) is None:
        return {"status": "none"}
    lock = WorkerLock(state_dir)
    if not lock.acquire():
        return {"status": "busy", "pid": lock.holder_pid()}
    try:
        record = read_restore_record(state_dir)  # re-read under the lock
        if record is None:
            return {"status": "none"}
        result = recover_locked(state_dir, record)
        logging.warning(f"[WiFi Recovery] Restore record from PID {record.get('pid')}: {result}")
        return result
    finally:
        lock.release()


# ── Time budget ──────────────────────────────────────────────────────────────

# Worst cases: BLE scan (up to 10 x 5 s) + handshake ~80 s, hotspot join 45 s,
# WS handshake 30 s, file list 15 s.
SETUP_BUDGET_S = 240
# Rejoining the hotspot if macOS auto-joins away (HotspotHold, up to 3 x ~45 s).
HOLD_BUDGET_S = 150
# Closing the hotspot + restore_internet's worst case (auto-join wait, rejoin,
# two power-cycles, opening Settings) + re-adding saved networks.
RESTORE_BUDGET_S = 180
MIN_TRANSFER_RATE_BPS = 50_000  # conservative WiFi throughput (B/s)
MIN_DOWNLOAD_BUDGET_S = 60
RECONNECT_GRACE_S = 20  # how long to wait for the device to reconnect its WebSocket


def compute_budget(total_bytes):
    """Seconds the worker may take: download_s for the transfer itself, total_s
    for a whole run (the parent's timeout and the UI's wait derive from it)."""
    download_s = max(math.ceil(max(total_bytes, 0) / MIN_TRANSFER_RATE_BPS), MIN_DOWNLOAD_BUDGET_S)
    return {
        "download_s": download_s,
        "total_s": SETUP_BUDGET_S + HOLD_BUDGET_S + download_s + RESTORE_BUDGET_S,
    }


def _atomic_write(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


async def wait_event(event, timeout):
    """Wait for an asyncio.Event; True if it was set, False on timeout.

    Catches asyncio.TimeoutError explicitly: on Python 3.10 (allowed by
    pyproject) it is NOT the builtin TimeoutError, so `except TimeoutError`
    would let the timeout escape and skip the clean error result.
    """
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return True
    except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041 — distinct classes on Python 3.10
        return False


def next_wifi_download(file_queue, recordings_dir, dismissed, new_files):
    """Pop `file_queue` until an entry needs downloading; return it, or None when empty.

    Dismissed sessions and sessions with an existing {sid}.ogg are recorded in
    `new_files` as "exists". An existing .ogg means present WHATEVER its size:
    the worker never deletes or replaces user recordings (a cropped recording
    is legitimately much smaller than the device's copy — an earlier "< 50 % of
    the device size → unlink and re-download" heuristic destroyed crops).
    """
    while file_queue:
        f = file_queue.pop(0)
        sid = f["session_id"]
        ogg_path = recordings_dir / f"{sid}.ogg"
        if str(sid) in dismissed:
            new_files.append({"session_id": sid, "ogg_path": None, "duration": 0, "status": "exists"})
            continue
        if ogg_path.exists():
            new_files.append({"session_id": sid, "ogg_path": str(ogg_path), "duration": 0, "status": "exists"})
            continue
        # Remove legacy decrypted .ogg.partial files from older versions (never an .ogg).
        (recordings_dir / f"{sid}.ogg.partial").unlink(missing_ok=True)
        return f
    return None


def _save_wifi_partial(recordings_dir, sid, raw, expected_size, reason):
    partial_path = recordings_dir / f"{sid}_partial.bin"
    status(f"Partial download for {sid}: {reason} — will retry next sync", 40)
    if raw:
        try:
            if not partial_path.exists() or partial_path.stat().st_size <= len(raw):
                _atomic_write(partial_path, raw)
        except OSError:
            pass
    return {
        "session_id": sid,
        "ogg_path": None,
        "duration": 0,
        "status": "partial",
        "received": len(raw),
        "expected": expected_size,
        "reason": reason,
    }


def save_wifi_download(recordings_dir, sid, raw, expected_size, app_rsa_key):
    """Persist one WiFi-downloaded file (the device sent last=1) and return its result entry.

    - Size == expected: the encrypted original is written first (dropped only
      for short files, matching BLE), then the .ogg atomically → status "new";
      decrypt failure → raw kept as {sid}_encrypted.bin, status "decrypt_failed".
    - Size != expected (same fail-safe rule as BLE; the size semantics are
      unconfirmed on real hardware): if it decrypts to valid Opus it's accepted
      as "new" with size_mismatch {received, expected} and its encrypted
      original kept; otherwise raw is kept as {sid}_partial.bin, status
      "partial" (never an .ogg, so the next sync retries it). WiFi sync never
      deletes files from the device.
    """
    from app.utils.audio import BleDecryptError, decrypt_ble_file, wrap_opus_ogg

    recordings_dir.mkdir(exist_ok=True)
    raw_size = len(raw)
    partial_path = recordings_dir / f"{sid}_partial.bin"
    size_mismatch = raw_size != expected_size
    frames = None
    if size_mismatch:
        reason = f"size mismatch ({raw_size} of {expected_size} bytes)"
        if not raw:
            return _save_wifi_partial(recordings_dir, sid, raw, expected_size, reason)
        try:
            frames = decrypt_ble_file(raw, app_rsa_key)
        except BleDecryptError as e:
            return _save_wifi_partial(recordings_dir, sid, raw, expected_size, f"{reason}; decrypt failed: {e}")
        logging.warning(
            f"[WiFi] size mismatch accepted for session {sid}: received {raw_size} bytes, file list said "
            f"{expected_size} (last=1, decrypts to valid Opus) — kept"
        )
        status(f"Size mismatch accepted for {sid}: {raw_size}/{expected_size} bytes (decrypts OK)", 40)

    enc_path = recordings_dir / f"{sid}_encrypted.bin"
    try:
        _atomic_write(enc_path, raw)
    except OSError as e:
        return {"session_id": sid, "ogg_path": None, "duration": 0, "status": "write_failed", "error": str(e)}
    partial_path.unlink(missing_ok=True)
    if frames is None:
        try:
            frames = decrypt_ble_file(raw, app_rsa_key)
        except BleDecryptError as e:
            return {"session_id": sid, "ogg_path": None, "duration": 0, "status": "decrypt_failed", "error": str(e)}
    final_path = recordings_dir / f"{sid}.ogg"
    try:
        _atomic_write(final_path, wrap_opus_ogg(frames))
    except OSError as e:
        return {"session_id": sid, "ogg_path": None, "duration": 0, "status": "write_failed", "error": str(e)}
    if raw_size <= 500_000 and not size_mismatch:
        enc_path.unlink(missing_ok=True)
    duration = len(frames) // 80 * 0.02
    entry = {"session_id": sid, "ogg_path": str(final_path), "duration": round(duration, 1), "status": "new"}
    if size_mismatch:
        entry["size_mismatch"] = {"received": raw_size, "expected": expected_size}
    return entry


SN_SIG = None  # Set from args — per-device signature from cloud API
HANDSHAKE_REPLY_TIMEOUT_S = 5  # cmd 1 / cmd 4 / cmd 3 replies during the BLE setup
W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"


async def ble_connect_and_setup():
    """Full BLE connect + handshake + setup, returns (client, key, nonce, aad, cipher, enc_func)."""
    import base64

    from bleak import BleakClient, BleakScanner

    from src.crypto import get_public_key_full_pem, load_rsa_private_key, rsa_decrypt

    status("BLE scanning...", 2)
    target = None
    for attempt in range(10):
        found = await BleakScanner.discover(timeout=5, return_adv=True)
        for a, (d, adv) in found.items():
            if 0x5D in adv.manufacturer_data:
                target = d
        if target:
            break
        status(f"BLE scan {attempt + 1}...", 2)
    if not target:
        return None

    cl = BleakClient(target, timeout=30.0)
    await cl.connect()
    status("BLE connected, handshaking...", 3)

    # RSA handshake
    ack = asyncio.Event()
    rsa = {}
    done = asyncio.Event()

    def h(s, d):
        d = bytes(d)
        cid = struct.unpack_from("<H", d, 0)[0] if len(d) >= 2 else 0
        if cid == 65041:
            ack.set()
        elif cid == 65042 and len(d) >= 4:
            idx = d[3]
            if idx not in rsa:
                rsa[idx] = d[4:]
            if len(rsa) >= d[2]:
                done.set()

    try:
        await cl.start_notify(N, h)
        status("BLE notifications started", 3)
    except Exception as e:
        status(f"BLE start_notify failed: {e}", 3)
        return None
    sig = base64.b64decode(SN_SIG)
    tc = (len(sig) + 99) // 100
    status(f"Sending SN_SIG ({len(sig)} bytes, {tc} chunks)...", 3)
    for _ in range(SN_SIG_SEND_REPEATS):
        for i in range(tc):
            await cl.write_gatt_char(
                W, struct.pack("<H", 65040) + bytes([tc, i]) + sig[i * 100 : (i + 1) * 100], response=False
            )
            await asyncio.sleep(0.02)
    for _ in range(int(SN_SIG_REPLY_WAIT_S * 10)):
        await asyncio.sleep(0.1)
        if ack.is_set() or done.is_set():
            break
    status(f"Handshake: ack={ack.is_set()}, done={done.is_set()}", 3)
    if ack.is_set() and not done.is_set():
        pem = get_public_key_full_pem().encode()
        pc = (len(pem) + 99) // 100
        status(f"Sending public key ({len(pem)} bytes, {pc} chunks)...", 3)
        for i in range(pc):
            await cl.write_gatt_char(
                W, struct.pack("<H", 65042) + bytes([pc, i]) + pem[i * 100 : (i + 1) * 100], response=True
            )
            await asyncio.sleep(0.02)
        await asyncio.wait_for(done.wait(), timeout=15)
    elif not done.is_set():
        status("Handshake failed — device did not respond to SN_SIG", 0)
        try:
            await cl.stop_notify(N)
        except Exception:
            pass
        return None
    blob = bytearray()
    for i in sorted(rsa.keys()):
        blob.extend(rsa[i])
    dec = rsa_decrypt(bytes(blob), load_rsa_private_key())
    key, nonce, aad, ct = dec[:32], dec[32:44], dec[44:56], dec[56:]
    cipher = ChaCha20Poly1305(key)
    if cipher.decrypt(nonce, ct, aad) != b"PLAUD.AI":
        raise ValueError("BLE handshake identity check failed: decrypted token mismatch")
    try:
        await cl.stop_notify(N)
    except Exception:
        pass

    # Setup encrypted command channel
    seq = [0]

    def enc(pt):
        seq[0] += 1
        return cipher.encrypt(nonce, struct.pack("<I", seq[0]) + pt, aad)

    cmd_evt = asyncio.Event()
    cmd_resp = [None]
    expected = [None]  # cmd_ids the command in flight accepts as its reply

    def handler(s, d):
        try:
            dd = cipher.decrypt(nonce, bytes(d), aad)[4:]
        except Exception:
            return
        if len(dd) < 1 or dd[0] != 1:
            return
        # Only the reply to the command in flight wakes cmd(): the device also
        # sends unsolicited type-1 packets (record events, late replies), the
        # class of bug device.py fixed in v2.2.0.
        if expected[0] is not None and reply_matches(dd, expected[0]):
            cmd_resp[0] = dd
            cmd_evt.set()
        else:
            logging.debug(f"[BLE] ignored cmd {cmd_id_of(dd)} packet (waiting for {expected[0]})")

    await cl.start_notify(N, handler)

    async def cmd(name, pt, timeout=5, expect=None):
        """Send one command; return its matching reply, or None after `timeout`.

        expect: cmd_ids accepted as the reply (default: the cmd_id sent).
        """
        expected[0] = tuple(expect) if expect is not None else (cmd_id_of(pt),)
        cmd_evt.clear()
        cmd_resp[0] = None
        try:
            await cl.write_gatt_char(W, enc(pt), response=False)
            try:
                await asyncio.wait_for(cmd_evt.wait(), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041 — distinct classes on Python 3.10
                pass
            return cmd_resp[0]
        finally:
            expected[0] = None

    cmd.handler = handler  # _close_hotspot() re-arms notifications with it

    # BLE setup commands
    try:
        if not BIND_TOKEN:
            raise ValueError("BIND_TOKEN missing — cannot proceed with BLE handshake")
        if len(BIND_TOKEN) > 32:
            logging.warning(
                "BIND_TOKEN length %d exceeds 32-byte handshake field; truncating "
                "(this likely indicates a configuration error — the device-bound token should be ≤32 ASCII chars).",
                len(BIND_TOKEN),
            )
        try:
            token32 = BIND_TOKEN[:32].encode("ascii").ljust(32, b"\x00")
        except UnicodeEncodeError as e:
            raise ValueError(f"BIND_TOKEN contains non-ASCII characters: {e}") from e
        # cmd 1 twice, then cmd 4 with the local time zone — the same code as device.py.
        verdict, _info = await bind_handshake(
            lambda pt: cmd("handshake", pt, timeout=HANDSHAKE_REPLY_TIMEOUT_S), token32, log=lambda m: status(m, 3)
        )
        if verdict == HANDSHAKE_REJECTED:
            raise ValueError(HANDSHAKE_REJECTED_ERROR)
        if verdict == HANDSHAKE_NO_REPLY:
            raise ValueError(HANDSHAKE_NO_REPLY_ERROR)
    except ValueError:
        try:
            await asyncio.wait_for(cl.disconnect(), 10)
        except BaseException:
            pass
        raise
    status("BLE ready", 4)

    return cl, key, nonce, aad, cipher, cmd


class WifiSession:
    """The device↔Mac WebSocket protocol for one transfer; survives reconnects.

    - Only one connection at a time, and only from the hotspot (admit()).
    - Each connection has its own message counter (the old shared ws_seq was
      reset by ANY new connection, breaking the live one).
    - The file list is merged by session id, so a reconnect that re-sends it
      doesn't duplicate files, and files already handled aren't fetched again.
    """

    def __init__(self, cipher, nonce, aad, bind_token, recordings_dir, dismissed, app_rsa_key, original_ip=""):
        self.cipher = cipher
        self.nonce = nonce
        self.aad = aad
        self.bind_token = bind_token
        self.recordings_dir = Path(recordings_dir)
        self.dismissed = dismissed
        self.app_rsa_key = app_rsa_key
        self.original_ip = original_ip
        self.handshake_done = asyncio.Event()
        self.file_list_received = asyncio.Event()
        self.all_done = asyncio.Event()
        self.wifi_files = []
        self.new_files = []
        self.active = None
        self.lost_at = None

    # -- bookkeeping --------------------------------------------------------

    def merge_file_list(self, entries):
        known = {f["session_id"] for f in self.wifi_files}
        for e in entries:
            if e["session_id"] not in known:
                known.add(e["session_id"])
                self.wifi_files.append(e)

    def pending_files(self):
        handled = {e["session_id"] for e in self.new_files}
        return [f for f in self.wifi_files if f["session_id"] not in handled]

    def bytes_to_download(self):
        return sum(
            f["size"]
            for f in self.pending_files()
            if str(f["session_id"]) not in self.dismissed
            and not (self.recordings_dir / f"{f['session_id']}.ogg").exists()
        )

    def lost_for(self, now=None):
        """Seconds since the device's connection dropped (0 while connected)."""
        if self.active is not None or self.lost_at is None:
            return 0.0
        return (time.monotonic() if now is None else now) - self.lost_at

    def admit(self, local_ip, remote_ip, wifi_ip):
        """Accept a connection only over the hotspot, and only one at a time.

        The server listens before the Mac joins the hotspot (the device connects
        as soon as it has a client, and an early server was what made transfers
        reliable), so the check is per connection: it must arrive on the WiFi
        interface's current address, that address must be a hotspot address, and
        the peer must be on the same /24. Other hosts on the user's LAN (or via
        Ethernet) are refused.
        """
        if self.active is not None:
            return False, "another device connection is active"
        if not hotspot_ip_ok(wifi_ip, self.original_ip):
            return False, "not on the device hotspot"
        if local_ip != wifi_ip:
            return False, "not via the WiFi interface"
        if not remote_ip or remote_ip.rsplit(".", 1)[0] != wifi_ip.rsplit(".", 1)[0]:
            return False, "peer is not on the hotspot subnet"
        return True, ""

    # -- protocol -----------------------------------------------------------

    async def handler(self, websocket):
        local_ip = (getattr(websocket, "local_address", None) or ("",))[0]
        remote_ip = (getattr(websocket, "remote_address", None) or ("",))[0]
        ok, why = self.admit(local_ip, remote_ip, await asyncio.to_thread(get_wifi_ip))
        if not ok:
            status(f"Refused WebSocket connection from {remote_ip}: {why}", None)
            try:
                await websocket.close(1008, why)
            except Exception:
                pass
            return
        self.active = websocket
        self.lost_at = None
        seq = 0
        cipher, nonce, aad = self.cipher, self.nonce, self.aad

        def encrypt(payload):
            nonlocal seq
            seq += 1
            return cipher.encrypt(nonce, struct.pack("<I", seq) + payload, aad)

        download_buf = bytearray()
        current = {"sid": 0, "size": 0}
        queue = []

        async def download_next():
            download_buf.clear()
            f = next_wifi_download(queue, self.recordings_dir, self.dismissed, self.new_files)
            if f is not None:
                current["sid"], current["size"] = f["session_id"], f["size"]
                req = build_wifi_packet(12, {"session": f["session_id"], "scene": 0, "start": 0, "end": f["size"]})
                await websocket.send(encrypt(req))
                status(f"Downloading session {f['session_id']}...", 40)
                return True
            self.all_done.set()
            return False

        try:
            async for message in websocket:
                if not isinstance(message, bytes):
                    continue
                try:
                    decrypted = cipher.decrypt(nonce, message, aad)[4:]
                except Exception:
                    continue

                cmd_id, json_data = parse_wifi_packet(decrypted)
                info = json_data if isinstance(json_data, dict) else {}

                if cmd_id == 2:
                    response = build_wifi_packet(1, {"token": self.bind_token, "stamp": int(time.time())})
                    await websocket.send(encrypt(response))

                elif cmd_id == 1:
                    self.handshake_done.set()
                    await asyncio.sleep(0.3)
                    fl_req = build_wifi_packet(11, {"uid": int(time.time()), "start": 0, "single": 0})
                    await websocket.send(encrypt(fl_req))

                elif cmd_id == 11:
                    if info.get("status", -1) == 0 and info.get("count", 0) > 0:
                        json_len_val = struct.unpack_from("<H", decrypted, 6)[0]
                        bin_data = decrypted[8 + json_len_val :]
                        entries = []
                        for i in range(0, len(bin_data) - 9, 10):
                            sid = struct.unpack_from("<I", bin_data, i)[0]
                            fsize = struct.unpack_from("<I", bin_data, i + 4)[0]
                            codec = struct.unpack_from("<H", bin_data, i + 8)[0]
                            entries.append({"session_id": sid, "size": fsize, "codec": codec})
                        self.merge_file_list(entries)
                    queue[:] = self.pending_files()
                    self.file_list_received.set()
                    status(f"{len(self.wifi_files)} file(s) found", 35)
                    await download_next()

                elif cmd_id == 13:
                    json_len_c = struct.unpack_from("<H", decrypted, 6)[0]
                    chunk = decrypted[8 + json_len_c :]
                    if chunk:
                        download_buf.extend(chunk)
                    if info.get("last", 0):
                        # Same fail-safe rule as BLE: exact size → saved; a size
                        # mismatch is kept only if it decrypts, else partial.
                        entry = save_wifi_download(
                            self.recordings_dir, current["sid"], bytes(download_buf), current["size"], self.app_rsa_key
                        )
                        self.new_files.append(entry)
                        done = sum(1 for f in self.new_files if f["status"] == "new")
                        total = len(self.wifi_files)
                        status(f"Downloaded {done}/{total}", 35 + int(55 * done / max(total, 1)))
                        await download_next()

                elif cmd_id == 3:
                    await websocket.send(encrypt(build_wifi_packet(16, {})))

        except Exception:
            pass
        finally:
            self.active = None
            if not self.all_done.is_set():
                self.lost_at = time.monotonic()
                status("Device connection dropped, waiting for it to reconnect...", None)


async def run_guarded(body, cleanup):
    """Run body(); SIGTERM cancels it, and cleanup() ALWAYS runs afterwards.

    cleanup runs exactly once and a second SIGTERM can't interrupt it (the
    WiFi restore must finish). Returns body's result, or None if terminated.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    state = {"cleaning": False, "terminated": False}

    def on_term():
        state["terminated"] = True
        if not state["cleaning"]:
            status("Stop requested, restoring WiFi before exiting...", None)
            task.cancel()

    installed = False
    try:
        loop.add_signal_handler(signal.SIGTERM, on_term)
        installed = True
    except (NotImplementedError, RuntimeError, ValueError):
        pass
    try:
        try:
            return await body()
        finally:
            state["cleaning"] = True
            await cleanup()
    except asyncio.CancelledError:
        if not state["terminated"]:
            raise
        return None
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)


class _KeepAwake:
    """NSProcessInfo activity (as app/state/device.py's _keep_awake): no App Nap
    and no idle system sleep while the Mac is on the hotspot. No-op without pyobjc."""

    _OPTIONS = 0x00FFFFFF  # NSActivityUserInitiated

    def __init__(self, reason):
        self._reason = reason
        self._token = None

    def __enter__(self):
        try:
            from Foundation import NSProcessInfo

            self._token = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(self._OPTIONS, self._reason)
        except Exception:
            self._token = None
        return self

    def __exit__(self, *exc):
        if self._token is not None:
            try:
                from Foundation import NSProcessInfo

                NSProcessInfo.processInfo().endActivity_(self._token)
            except Exception:
                pass
            self._token = None
        return False


def _emit_result(result):
    """Final JSON result on stdout; never raises (the parent may be gone)."""
    try:
        print(json.dumps(result), flush=True)
    except (BrokenPipeError, OSError, ValueError):
        pass


CLOSE_HOTSPOT_REPLY_S = 3  # wait this long for the close reply
CLOSE_HOTSPOT_TIMEOUT_S = 6  # whole close step, so cleanup isn't held up


async def _close_hotspot(cl, ble_cmd, timeout=CLOSE_HOTSPOT_TIMEOUT_S):
    """Tell the device to close its hotspot over BLE (bounded; failures ignored).

    Notifications were stopped after the hotspot opened, so they're re-armed
    with the command channel's own handler: the reply (cmd 10, or cmd 13
    CloseWiFi) is logged, which is how hardware testing will settle whether
    [1,10,0,0] closes the hotspot (README open question 8).
    """

    async def go():
        await cl.start_notify(N, getattr(ble_cmd, "handler", None) or (lambda s, d: None))
        try:
            reply = await ble_cmd(
                "WiFi_Close", WIFI_CLOSE_REQUEST, timeout=CLOSE_HOTSPOT_REPLY_S, expect=WIFI_CLOSE_REPLY_IDS
            )
            status(f"Hotspot close reply: {describe_status_reply(reply)}", None)
        finally:
            await cl.stop_notify(N)

    try:
        await asyncio.wait_for(go(), timeout)
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        status("Could not close the device hotspot over Bluetooth", None)


async def _start_ws_server(session):
    import websockets.asyncio.server

    last_err = None
    for port in WS_PORT_CANDIDATES:
        try:
            # Listens on all interfaces BEFORE the hotspot is joined (the device
            # connects straight away); session.admit() refuses anything that
            # doesn't arrive over the hotspot.
            return await websockets.asyncio.server.serve(session.handler, "0.0.0.0", port)
        except OSError as e:
            last_err = e
            status(f"Port {port} in use, trying next...", 5)
    raise OSError(f"WiFi WebSocket server: all ports {WS_PORT_CANDIDATES} are in use") from last_err


async def transfer(args):
    """BLE handshake → device hotspot → WebSocket download → WiFi restored."""
    recordings_dir = Path(args["recordings_dir"])
    app_key_path = Path(args["app_key_path"])
    dismissed = set(args.get("dismissed", []))
    sn = args["sn"]
    started = time.monotonic()

    def report_budget(total_bytes, msg, pct):
        b = compute_budget(total_bytes)
        remaining = b["total_s"] - SETUP_BUDGET_S + max(0, SETUP_BUDGET_S - (time.monotonic() - started))
        status(msg, pct, budget_s=b["total_s"], remaining_s=math.ceil(remaining))

    report_budget(0, "Connecting to the device over Bluetooth...", 2)

    # Do our own BLE connection (device invalidates sessions when BLE drops)
    try:
        ble = await ble_connect_and_setup()
    except ValueError as e:
        return {"error": f"BLE handshake failed: {e}"}
    if not ble:
        return {"error": "BLE connection failed"}
    cl, key, nonce, aad, cipher, ble_cmd = ble

    app_rsa_key = load_pem_private_key(app_key_path.read_bytes(), password=None)
    # The app's own modules (decrypt, ogg wrapping) are imported lazily from the
    # project root.
    sys.path.insert(0, str(app_key_path.parent.parent))
    status(f"WiFi interface: {get_wifi_interface()}", 3)

    wifi_ssid = f"Plaud{sn[-4:]}"
    wifi_pass = sn[-8:]
    original_ip = get_wifi_ip()
    # Needs Location permission; empty otherwise (restore then accepts any network).
    original_ssid = _get_current_ssid()
    if original_ssid:
        status(f"Current network: {original_ssid}", 3)

    run = WifiRun(args.get("state_dir"), wifi_ssid, original_ip, original_ssid)
    session = WifiSession(cipher, nonce, aad, BIND_TOKEN, recordings_dir, dismissed, app_rsa_key, original_ip)
    hold = HotspotHold(run, wifi_ssid, wifi_pass)
    ctx = {"server": None, "hold_task": None, "hotspot_opened": False, "wifi_restore": None}

    async def body():
        status("Starting WebSocket server...", 5)
        ctx["server"] = await _start_ws_server(session)

        status("Opening device hotspot...", 10)
        ctx["hotspot_opened"] = True
        opened = await ble_cmd("WiFi_Open", WIFI_OPEN_REQUEST)
        status(f"Hotspot open reply: {describe_status_reply(opened)}", None)

        # Stop BLE notifications — they conflict with WiFi
        try:
            await cl.stop_notify(N)
        except Exception:
            pass

        status(f"Connecting to {wifi_ssid}...", 15)
        run.begin()  # restore record on disk BEFORE the first network change
        hotspot_ip = await connect_to_hotspot(wifi_ssid, wifi_pass, original_ip, timeout=45)
        if not hotspot_ip:
            return {"error": "Could not connect to hotspot", "files": session.new_files}
        run.set_hotspot_ip(hotspot_ip)
        ctx["hold_task"] = asyncio.create_task(hold.watch())

        status("Waiting for handshake...", 25)
        if not await wait_event(session.handshake_done, 30):
            return {"error": hold.failed or "Handshake timed out", "files": session.new_files}

        status("Getting file list...", 30)
        if not await wait_event(session.file_list_received, 15):
            return {"error": hold.failed or "File list timed out", "files": session.new_files}

        total_bytes = session.bytes_to_download()
        dl_budget = compute_budget(total_bytes)["download_s"]
        report_budget(total_bytes, f"{len(session.wifi_files)} file(s) found", 35)
        deadline = time.monotonic() + dl_budget
        error = None
        while not await wait_event(session.all_done, 0.5):
            if hold.failed:
                error = hold.failed
                break
            if not hold.rejoining and session.lost_for() > RECONNECT_GRACE_S:
                error = "The device's WiFi connection dropped"
                break
            if time.monotonic() > deadline:
                done_so_far = sum(1 for f in session.new_files if f["status"] == "new")
                status(f"Download timed out: got {done_so_far}/{len(session.wifi_files)} files", 88)
                break

        done_count = sum(1 for f in session.new_files if f["status"] == "new")
        status(f"Download complete: {done_count} file(s)", 90)
        result = {"files": session.new_files, "method": "wifi"}
        if error and not done_count:
            result["error"] = error
        return result

    async def cleanup():
        if ctx["hold_task"] is not None:
            ctx["hold_task"].cancel()
            try:
                await ctx["hold_task"]
            except BaseException:
                pass
        # Cancelling the watcher doesn't stop an isolate() on its thread: let it
        # finish (restore_networks also waits on WifiRun's lock) so the restore
        # below works from the record it wrote.
        await hold.wait_isolation()
        await wait_for_pending_join()
        if ctx["server"] is not None:
            await asyncio.sleep(1)  # let the last frames flush
            ctx["server"].close()
        networks_ok = True
        if run.touched:
            # Saved networks back (if paused) and the hotspot forgotten BEFORE
            # leaving it, so macOS rejoins the user's network, not the hotspot.
            networks_ok = await asyncio.to_thread(run.restore_networks)
        if ctx["hotspot_opened"]:
            # Closing the hotspot first lets macOS auto-join a saved network.
            await _close_hotspot(cl, ble_cmd)
        if run.touched:
            ctx["wifi_restore"] = await restore_internet(wifi_ssid, run.hotspot_ip or "", original_ip, original_ssid)
            await asyncio.to_thread(run.finish, networks_ok)
        try:
            await asyncio.wait_for(cl.disconnect(), 10)
        except BaseException:
            pass

    result = await run_guarded(body, cleanup)
    if result is None:
        result = {"error": "WiFi transfer stopped before it finished", "terminated": True, "files": session.new_files}
    if ctx["wifi_restore"]:
        result["wifi_restore"] = ctx["wifi_restore"]
    if run.gave_up:
        result["wifi_restore_notice"] = restore_notice(run.state_dir)
    return result


async def main():
    global BIND_TOKEN, SN_SIG
    # Read args from stdin (parent writes JSON + closes pipe). Keeps the
    # per-device BIND_TOKEN and SN_SIG out of argv → out of `ps aux` output.
    args = json.loads(sys.stdin.read())
    BIND_TOKEN = args.get("bind_token")
    SN_SIG = args.get("sn_sig")
    if not BIND_TOKEN:
        _emit_result({"error": "No BIND_TOKEN — user must log into Plaud cloud first"})
        return
    if not SN_SIG:
        _emit_result({"error": "No SN_SIG — user must log into Plaud cloud first"})
        return

    # One worker at a time, machine-wide (the parent's lock is per app process).
    state_dir = args.get("state_dir")
    lock = WorkerLock(state_dir) if state_dir else None
    if lock is not None and not lock.acquire():
        _emit_result({"error": "Another WiFi transfer is already running", "busy": True})
        return
    try:
        if state_dir:
            leftover = read_restore_record(state_dir)
            if leftover is not None:
                status("Restoring WiFi settings from an interrupted transfer...", 2)
                outcome = await asyncio.to_thread(recover_locked, state_dir, leftover)
                # "gave_up": the record is cleared and the UI gets restore_notice();
                # the sync goes ahead.
                if outcome["status"] not in ("restored", "gave_up"):
                    _emit_result(
                        {
                            "error": "WiFi settings from an interrupted transfer couldn't be fully restored "
                            f"(missing: {', '.join(outcome.get('missing', []))}). Check your WiFi settings.",
                            "wifi_restore_pending": True,
                        }
                    )
                    return
        with _KeepAwake("Plaud WiFi sync"):
            try:
                result = await transfer(args)
            except Exception as e:  # the restore already ran (run_guarded's cleanup)
                logging.exception("[WiFi] transfer failed")
                result = {"error": f"WiFi transfer failed: {e}"}
        _emit_result(result)
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    asyncio.run(main())
