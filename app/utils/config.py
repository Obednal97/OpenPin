"""Config load/save with atomic writes and backup."""

import json
import os
import tempfile
import threading

from app.utils.constants import CONFIG_PATH

# Thread lock to prevent concurrent read-modify-write races
_config_lock = threading.RLock()


def load_config():
    """Load the app config from disk (thread-safe)."""
    with _config_lock:
        if CONFIG_PATH.exists():
            try:
                return json.loads(CONFIG_PATH.read_text())
            except (json.JSONDecodeError, ValueError):
                # Config is corrupted — try the backup
                backup = CONFIG_PATH.with_suffix(".json.bak")
                if backup.exists():
                    try:
                        data = json.loads(backup.read_text())
                        # Restore from backup
                        save_config(data)
                        return data
                    except Exception:
                        pass
            except Exception:
                pass
        return {}


def save_config(config):
    """Save the app config to disk atomically (thread-safe).

    Writes to a temp file first, then renames — prevents corruption
    if the process crashes or disk fills up mid-write.
    """
    with _config_lock:
        data = json.dumps(config, indent=2)

        # Keep a backup of the previous good config
        if CONFIG_PATH.exists():
            backup = CONFIG_PATH.with_suffix(".json.bak")
            try:
                os.replace(str(CONFIG_PATH), str(backup))
            except OSError:
                pass

        # Atomic write: write to temp file in the same directory, then rename
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(CONFIG_PATH.parent),
                prefix=".config_",
                suffix=".tmp",
            )
            with os.fdopen(fd, "w") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(CONFIG_PATH))
        except OSError:
            # Fallback to direct write if atomic fails (e.g., cross-device)
            CONFIG_PATH.write_text(data)
