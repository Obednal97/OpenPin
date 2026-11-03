# scripts/

## Build and maintenance (used by the build, keep working)

| Script | What it does |
|---|---|
| `prepare-frontend.sh` | Copies the frontend (and backend files) into `dist-tauri/`. Tauri runs it as `beforeBuildCommand`; run it by hand before `tauri dev` or `cargo test`. |
| `lock-python-deps.sh` | Regenerates the hash-pinned lockfiles in `app/requirements/` from `python-deps/*.in` (needs uv). See [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md#python-dependencies). |
| `python-deps/*.in` | The unpinned inputs: `core` plus the `whisper`, `diarize` and `local-summary` extras. |

## Legacy device scripts

Standalone scripts from the reverse-engineering days. The app does all of this better
(`app/state/device.py`, `app/utils/wifi_sync_worker.py`, `app/utils/audio.py`): it checks
downloads are complete, keeps partial files, and never deletes from the device unless a download
was exact. Nothing in the app, build or tests uses these. They use the current BIND_TOKEN
handshake but haven't been run against a device since the app took over, so treat them as
reference code.

| Script | What it does | Caveats |
|---|---|---|
| `sync_and_transcribe.py` | Command-line sync: BLE connect, download new recordings, decrypt, transcribe with local Whisper | No completeness checks. Writes `recordings/`, `transcripts/` and `sessions/` in the current folder |
| `sync_device.py` | Connect, **record** for N seconds (argument, default 5), download over BLE, stay paired | Saves the downloads undecrypted (`recordings/ble_<id>.opus`) and a log in `sessions/`, in the current folder |
| `session_manager.py` | Connect, log every key and response to `sessions/`, then **depair** the device | Depairing means pairing again in the app afterwards. Writes session keys to disk |
| `download_all.py` | Download every recording from Plaud Cloud and wrap it as Ogg | Reads the Plaud token from `.plaud_config.json` in the current folder (Open Pin keeps it in `~/Library/Application Support/Plaud V2/`, a folder that keeps the app's old name); EU API only. The app's Plaud Cloud sync replaces it |

### Running them

Run from the repo root with the app's Python environment (they import `src.crypto`) and the RSA
keys in `keys/`:

```bash
PYTHONPATH=. python scripts/sync_and_transcribe.py --no-transcribe
```

The BLE scripts need a device paired in the app first: they read its `BIND_TOKEN` and serial
signature from `~/Library/Application Support/Plaud V2/.plaud_v2_config.json`
(`device_bind_tokens`, `device_sn_signatures`). They use the device in `$PLAUD_SN`, or else the
first one in that file:

```bash
PLAUD_SN=88xxxxxxxxxxxxxx PYTHONPATH=. python scripts/sync_device.py 10
```

Quit the app first: the device accepts one Bluetooth connection at a time.

Never hard-code a serial, bind token or serial signature in a script. They identify one physical
device and one Plaud account.

Deleted in November 2026 (in git history): `plaud.py`, `connect_device.py`, `pair_device.py`,
`handshake_test.py`, `decrypt_audio.py`, `wifi_sync.py`, `wifi_transfer.py`,
`wifi_handshake_test.py`. They used the old APP_KEY handshake or none, the wrong decryption
scheme, or a hard-coded `en0` WiFi interface.
