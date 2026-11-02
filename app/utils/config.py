"""Config load/save with crash-safe atomic writes, a backup, and locked updates.

Rules for callers:

- Never hold a config dict across a network/AI/BLE call and then save it —
  anything written meanwhile (deleted_ids, tags, tokens, API keys…) is lost.
- For every read-modify-write use :func:`update_config`, which loads a fresh
  copy, applies the mutation and saves, all under ``_config_lock``.
"""

import json
import logging
import os
import shutil
import tempfile
import threading

from app.utils.constants import CONFIG_PATH

# Re-entrant so update_config's mutator may call load_config/save_config.
_config_lock = threading.RLock()

_FILE_MODE = 0o600


def _backup_path():
    return CONFIG_PATH.with_suffix(".json.bak")


def _read_json(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("config root is not an object")
    return data


def load_config():
    """Load the app config from disk (thread-safe).

    Falls back to the ``.bak`` copy when the live file is missing or corrupt
    (e.g. a crash mid-save), and restores the live file from it.
    """
    with _config_lock:
        live = CONFIG_PATH
        if live.exists():
            try:
                return _read_json(live)
            except Exception as e:
                logging.warning("[Config] Live config unreadable (%s); trying backup", e)
        backup = _backup_path()
        if backup.exists():
            try:
                data = _read_json(backup)
            except Exception as e:
                logging.warning("[Config] Backup config unreadable too: %s", e)
                return {}
            logging.warning("[Config] Restored config from %s", backup.name)
            try:
                _write_live(data, make_backup=False)
            except OSError as e:
                logging.warning("[Config] Could not restore live config: %s", e)
            return data
        return {}


def _write_live(config, *, make_backup=True):
    """Write ``config`` to CONFIG_PATH: temp → fsync → backup → os.replace."""
    data = json.dumps(config, indent=2)
    live = CONFIG_PATH
    live.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(live.parent), prefix=".config_", suffix=".tmp")
    try:
        try:
            os.fchmod(fd, _FILE_MODE)
        except OSError:
            pass
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # Keep the previous good config as .bak. Copy (not rename) so the live
        # file exists at every instant — a crash here leaves both intact.
        if make_backup and live.exists():
            backup = _backup_path()
            try:
                # Only back up a file that actually parses, so a corrupt live
                # file never overwrites a good backup.
                _read_json(live)
                shutil.copy2(str(live), str(backup))
                os.chmod(str(backup), _FILE_MODE)
            except Exception as e:
                logging.warning("[Config] Skipped backup: %s", e)
        os.replace(tmp_path, str(live))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_config(config):
    """Save the app config to disk atomically (thread-safe, mode 0600).

    Prefer :func:`update_config` for read-modify-write sequences.
    """
    with _config_lock:
        _write_live(config)


def update_config(fn):
    """Atomically read-modify-write the config.

    Under ``_config_lock``: loads a fresh copy, calls ``fn(cfg)`` which may
    mutate ``cfg`` in place (return ``None``) or return a new dict, saves the
    result and returns it. ``fn`` must be quick and must not do network I/O.
    """
    with _config_lock:
        cfg = load_config()
        result = fn(cfg)
        if result is None:
            result = cfg
        save_config(result)
        return result
