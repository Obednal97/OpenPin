"""Config load/save with simple caching."""

import json

from app.utils.constants import CONFIG_PATH


def load_config():
    """Load the app config from disk."""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


def save_config(config):
    """Save the app config to disk."""
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
