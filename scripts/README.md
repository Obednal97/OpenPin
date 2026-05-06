# scripts/

Development and diagnostic scripts for the Plaud NotePin S device.

## Device credentials

All scripts that communicate with a Plaud device read `BIND_TOKEN` and
`SN_SIG` from the production app's config file at runtime:

```
~/Library/Application Support/Plaud V2/.plaud_v2_config.json
```

**To use these scripts you must have paired a device via the main Plaud V2
app first.** The app writes `device_bind_tokens` and `device_sn_signatures`
into that config during pairing. The scripts will exit with a clear error
message if the config is missing or the device SN has no entry.

## Do not commit hardcoded credentials

Never hardcode a `BIND_TOKEN` or `SN_SIG` value directly in a script. They
identify a specific physical device and a specific user account. Use the
config-read pattern already present in every script here:

```python
from pathlib import Path
import json, sys

CONFIG_PATH = Path.home() / "Library/Application Support/Plaud V2/.plaud_v2_config.json"
try:
    with open(CONFIG_PATH) as _f:
        _cfg = json.load(_f)
except FileNotFoundError:
    sys.exit(f"Config not found at {CONFIG_PATH}. Run the Plaud V2 app first to pair a device.")

DEVICE_SN = "<your-device-SN>"
BIND_TOKEN = _cfg.get("device_bind_tokens", {}).get(DEVICE_SN)
SN_SIG = _cfg.get("device_sn_signatures", {}).get(DEVICE_SN)
if not BIND_TOKEN or not SN_SIG:
    sys.exit(f"No credentials for SN {DEVICE_SN} in config — re-pair device via the Plaud V2 app first.")
```

This mirrors the pattern used by `test_wifi_connect_only.py` and
`test_wifi_full.py`, which were the canonical reference scripts.

## Notes on git history

Hardcoded credential values existed in earlier commits. Whether to scrub
them with `git filter-repo` is a separate decision tracked as F-bd-004.
The values in history are tied to one physical device and one account and
do not grant access to any cloud service independently.
