# Device Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move everything that knows about the Plaud NotePin behind a small device contract (`app/devices/`), with the generic sync code living once in the app, and `app/state/device.py` gone, with no change in NotePin behaviour.

**Architecture:** `app/devices/base.py` documents the contract as a `Protocol`. `app/devices/notepin/` is a package whose `__init__` exposes exactly the contract functions (plus declared extras) from its modules. `app/devices/__init__.active()` returns that package. The shared sync loop moves to `app/state/sync.py` and calls the adapter for listing, downloading one recording and deleting. Routes call `devices.active()` for device operations and `sync` for sync state. This is a move: function bodies stay as they are unless a task says otherwise.

**Tech Stack:** Python 3.12, Flask, bleak, asyncio; Tauri 2 (Rust) bundles the Python as resources; pytest + ruff.

**Spec:** `docs/superpowers/specs/2026-11-08-device-adapters-design.md`

**Base:** branch `feat/device-adapters`, stacked on `fix/wifi-skip-rules` (#127, on #126, on #125). Rebase onto master once those merge. Line numbers below are for that base.

## Global Constraints

- No change in NotePin behaviour: pairing, Bluetooth sync, Wi-Fi fast sync, find-my, battery, delete-after-sync, Settings › Device, auto-sync timing.
- No data migration: config keys (`paired_devices`, `deleted_ids`, `device_bind_tokens`, `device_sn_signatures`, ...), Keychain backup, `<session_id>.ogg` files and transcripts are untouched.
- One device paired at a time; no device picker, no plugin loading, no new settings.
- The HTTP API (`/device/*`) keeps every route, request and response shape, except that `/device/status` gains `extras` (Task 7).
- Lean: no wrappers that only forward a call, no new abstraction beyond those named here. Net line count may grow only by the contract and adapter glue (target under +150 lines across the whole plan, excluding tests and docs).
- Every new folder under `app/` with `.py` files gets a `bundle.resources` entry in `src-tauri/tauri.conf.json`.
- `app/ui/*.js` and `app/renderer.js` share one global scope: any new top-level name must not exist in another of those files.
- Tests: `python -m pytest -q` and `ruff check .` in the dev environment (see `docs/DEVELOPMENT.md`), green after every task. A worktree needs the `keys` folder linked in from the main checkout (`ln -s <main checkout>/keys keys`, never committed). Rust: `bash scripts/prepare-frontend.sh; (cd src-tauri && cargo test)`.
- Commits: one per task, British English, no em dashes, no `Co-Authored-By` trailer.

## Review Focus

1. Import order: `app.state.cloud` imports `pairing_store` (moving into the notepin package, whose `__init__` imports `ble`, which uses cloud). A fresh interpreter must import either side first without an `ImportError`. Test in Task 2 and Task 6.
2. The Wi-Fi worker runs as a script from its file path in a separate Python process. From its new, deeper path it must still find the repo root. Test in Task 2.
3. An abort mid-file during a Bluetooth sync must still stop the transfer at once, save the partial and stop the loop, after the per-file transfer moves into the adapter. Test in Task 3.
4. A disconnect mid-sync must stop the loop and skip auto-delete for anything not deleted yet, as today. Test in Task 3.
5. The built app must contain the new subpackages (`app/devices/`, `app/devices/notepin/`); the existing bundle test only checks top-level `app/` folders. Test in Task 1, build check in Task 8.

---

### Task 1: Shared event loop, devices package, nested-bundle test

**Files:**
- Create: `app/devices/__init__.py` (docstring only for now), `app/devices/loop.py`
- Modify: `app/state/device.py` (remove `_ble_lock`, `_loop`, `_loop_thread`, `_get_loop`, `run_async`, `_run_on_loop`: lines 67, 77-78, 464-512), `src-tauri/tauri.conf.json` (`bundle.resources`), `tests/test_tauri_parity.py:967`

**Interfaces:**
- Produces: `app.devices.loop.run_async(coro, timeout=120, lock_timeout=None)`, `app.devices.loop._ble_lock` (same objects and behaviour as today). `app/state/device.py` imports both by name, so `dev.run_async` and `dev._ble_lock` still resolve for callers and tests.

- [ ] **Step 1: Extend the bundle test.** In `test_bundle_ships_every_app_package`, also require an entry `app/<relative path>/` for every nested folder under `app/` that holds `.py` files (skip `__pycache__`). Run it: passes today (no nested packages yet).
- [ ] **Step 2: Move the loop code** into `app/devices/loop.py` unchanged; in `device.py`, `from app.devices.loop import _ble_lock, run_async`. Keep `_get_loop` callers inside device.py working (import it too if used).
- [ ] **Step 3: Add `"../app/devices/*": "app/devices/"`** to `bundle.resources`.
- [ ] **Step 4: Run** the full suite and ruff. Expected: all pass (`tests/test_ble_run_async.py` patches `routes.dev.run_async`, still valid).
- [ ] **Step 5: Commit** "Devices: move the shared Bluetooth event loop into app/devices".

### Task 2: Move the NotePin helper modules into `app/devices/notepin/`

**Files:**
- Move (`git mv`): `app/utils/ble_proto.py` → `app/devices/notepin/ble_proto.py`; `app/utils/wifi_sync_worker.py` → `app/devices/notepin/wifi_worker.py`; `app/utils/pairing_store.py` → `app/devices/notepin/pairing_store.py`; `app/utils/pairing_file.py` → `app/devices/notepin/pairing_file.py`
- Create: `app/devices/notepin/__init__.py` (docstring only in this task)
- Modify: every importer (see list below), `src-tauri/tauri.conf.json`, `scripts/python-deps/core.in:12` (comment path), `tests/test_startup.py` (string patch `"app.utils.wifi_sync_worker.get_wifi_interface"`), `app/state/device.py:2082` (`find_spec("app.utils.wifi_sync_worker")`)
- Test: `tests/test_device_imports.py` (new)

Importers to update: `app/state/device.py`, `app/api_server.py:242,392`, `app/state/cloud.py:17`, `app/routes/device.py:9`, and every test importing these modules. `src/crypto.py` and `src/scanner.py` stay in `src/` (spec: move only where it doesn't churn; they are bundled separately).

- [ ] **Step 1: Write `tests/test_device_imports.py`:**
  - `test_cloud_then_notepin_import` and `test_notepin_then_cloud_import`: in a fresh `subprocess.run([sys.executable, "-c", "import app.state.cloud, app.devices.notepin"])` (and the reverse order), with `cwd` = repo root; assert return code 0 and no `ImportError` in stderr.
  - `test_wifi_worker_runs_as_script`: run `[sys.executable, "app/devices/notepin/wifi_worker.py"]` with empty stdin and `PLAUD_DATA_DIR` set to a tmp dir; assert `"ModuleNotFoundError"` and `"ImportError"` are not in stderr (the worker may exit non-zero on empty input; that's fine).
- [ ] **Step 2: Run them**: the worker test fails (file not there yet).
- [ ] **Step 3: Move the files and update imports.** In `wifi_worker.py`, fix `_PROJECT_ROOT` (line ~62) for the extra folder depth (`parents[3]` instead of the current count); check `app/state/device.py`'s worker `sys.path`/`cwd` arguments still point at the repo root. Add `"../app/devices/notepin/*": "app/devices/notepin/"` to `bundle.resources`.
- [ ] **Step 4: Run** the full suite and ruff: all pass.
- [ ] **Step 5: Commit** "Devices: move the NotePin protocol, Wi-Fi worker and pairing modules into app/devices/notepin".

### Task 3: Shared sync loop in `app/state/sync.py`; NotePin downloads one recording

**Files:**
- Create: `app/state/sync.py`
- Modify: `app/state/device.py` (remove the moved names; add `download_recording`), `app/routes/device.py` (sync names now from `sync`), tests that patch moved names: `tests/test_ble_sync_integrity.py`, `tests/test_sync_progress.py`, `tests/test_wifi_sync_parent.py`
- Test: `tests/test_ble_sync_integrity.py` (two new tests)

**Moves to `sync.py` unchanged** (names kept): `_syncing`, `_sync_abort`, `_NO_FILE_PROGRESS`, `_ble_sync_status`, `RETRY_CAP`, `_download_failures(_lock)`, `_finalize_redownload_backup`, `SYNC_BUSY_ERROR`, `claim_sync`, `release_sync`, `sync_files`, `run_sync_blocking`, `record_download_failure`, `clear_download_failures`, `download_failures`, `is_retry_capped`, `track_download_result`, `gdrive_cloud_synced_ids`, `find_local_recording`, `_wifi_skip_ids`, `is_already_synced`, `is_syncing`, `abort_sync`, `get_ble_sync_status`, and `_sync_files_inner` minus the per-file transfer.

**Interfaces:**
- Consumes: `run_async`, `_ble_lock` from `app.devices.loop` (Task 1).
- Produces:
  - `app.state.device.download_recording(f: dict, rec_dir: Path, report: Callable[[int], None], should_stop: Callable[[], bool]) -> tuple[dict | None, bool]`: everything `_sync_files_inner` does today for one file from "Download via BLE" to the CRC diagnostic (device.py ~2411-2535): cmd 28, receive loop (where it read `_sync_abort` it now calls `should_stop()`), stop-sync, `evaluate_download`, `_save_partial` / `_save_size_mismatch` / `_save_complete`, CRC diagnostic. `report(bytes_received)` replaces the `_report(i + 1, received)` calls (the loop binds the index). Returns `(entry, verified)`, where `verified` is today's "eligible for auto-delete" (`verdict == DOWNLOAD_OK` and status `"new"`), or `(None, False)` when the link state is gone (today's "device state reset mid-sync" break). The RSA app key is loaded on first use and cached; a missing key raises `RuntimeError(f"Missing {APP_KEY_PATH}")`.
  - `sync._sync_files_inner(background=False)` keeps its result shape. It gets the device with a function-level `from app.state import device as dev` (Task 5 switches this to `devices.active()`), calls `dev.list_device_files()`, then per file `dev.download_recording(...)`, `track_download_result(entry)`; breaks when `entry is None`, when a partial came back and `should_stop()` (sets `aborted`), or when `not dev.is_connected()`; keeps the auto-upload and auto-delete (`dev.delete_device_file`) exactly as now.
  - `app.routes.device`: `run_sync_blocking`, `abort_sync`, `is_syncing`, `get_ble_sync_status`, `RETRY_CAP`, `SYNC_BUSY_ERROR`, `gdrive_cloud_synced_ids`, `is_already_synced`, `is_retry_capped`, `download_failures` are called as `sync.<name>`.
  - Wi-Fi code still in device.py calls the slot/skip/tracking helpers as `sync.<name>`.

- [ ] **Step 1: Write the two Review Focus tests** in `tests/test_ble_sync_integrity.py`, using its `FakeDevice` / `run_sync` helpers:
  - `test_abort_mid_file_stops_the_loop`: two files on the device; abort during the first file's transfer; assert the first entry is `status == "partial"` with reason `"aborted"`, the second file was never requested, `result["aborted"] is True`, and nothing was deleted from the device.
  - `test_disconnect_mid_sync_stops_and_skips_auto_delete`: `auto_delete_after_sync` on; first file completes, the link drops during the second; assert the loop stopped, the first file is in `delete_failed` (not deleted), and no exception escapes.
- [ ] **Step 2: Run them on the current code**: they should pass (they pin today's behaviour). If one fails, stop: that's current behaviour to keep, so fix the test, not the code.
- [ ] **Step 3: Do the move.** In tests, repoint patches of moved names from `dev` to `sync` (`import app.state.sync as sync`), e.g. `monkeypatch.setattr(sync, "_sync_abort", ...)`, `sync._sync_files_inner`, `sync._download_failures`, `sync._ble_sync_status`, `sync._finalize_redownload_backup`. Direct `dev.list_device_files = ...` assignments keep working (sync looks it up on the module at call time).
- [ ] **Step 4: Run** the full suite and ruff: all pass, including the two new tests.
- [ ] **Step 5: Commit** "Sync: one shared sync loop; the NotePin downloads one recording".

### Task 4: Wi-Fi supervision into `app/devices/notepin/wifi.py`

**Files:**
- Create: `app/devices/notepin/wifi.py`
- Modify: `app/state/device.py`, `app/api_server.py:273-277`, `src-tauri/src/commands/mod.rs:1276` (comment) and `:2450` (`include_str!("../../../app/devices/notepin/wifi.py")`), `tests/test_wifi_sync_parent.py`

**Moves unchanged:** `_wifi_sync_status` (and its module-level `.update`), `WIFI_BUDGET_MARGIN_S`, `WIFI_TERM_GRACE_S`, `WIFI_KILL_WAIT_S`, `WIFI_LOG_NAME`, `WIFI_LOG_MAX_BYTES`, `WIFI_LOG_SCRUB_MARKER`, `_REDACTED`, `_SECRET_PATTERNS`, `redact_secrets`, `_configured_secrets`, `scrub_wifi_logs`, `append_rotating_log`, `_WifiDeadline`, `_supervise_wifi_worker`, `_apply_wifi_progress`, `_recover_wifi_after_worker`, `_wifi_recovery_idle`, `WIFI_RECOVERY_WAIT_S`, `run_wifi_recovery_in_background`, `get_wifi_restore_notice`, `acknowledge_wifi_restore_notice`, `do_wifi_sync_blocking`, `_do_wifi_sync_locked`, `get_wifi_sync_status`. Check whether `redact_secrets` / `append_rotating_log` have callers outside the Wi-Fi code (`git grep`); if they do, leave them where those callers can reach them and note it.

**Interfaces:**
- Consumes: `sync.claim_sync`, `sync.release_sync`, `sync._wifi_skip_ids`, `sync.track_download_result`, `sync._finalize_redownload_backup` (Task 3); the device's `_device_state` / serial and credential helpers from `app.state.device` via a module import (`from app.state import device as ble_state` or similar), never `from ... import name`, so test patches on device.py still apply.
- Produces: the same public Wi-Fi names, now on `app.devices.notepin.wifi`. `app/state/device.py` imports `do_wifi_sync_blocking`, `get_wifi_sync_status`, `acknowledge_wifi_restore_notice`, `run_wifi_recovery_in_background` by name for the routes until Task 5 removes device.py.

- [ ] **Step 1: Move the code**, repoint `tests/test_wifi_sync_parent.py` patches to the new module (`import app.devices.notepin.wifi as wifi`), update the Rust `include_str!` path and comment, and the `api_server` docstring/import.
- [ ] **Step 2: Run** pytest, ruff and `cargo test` (the `wifi_log_name_matches_the_backend` test must pass): all pass.
- [ ] **Step 3: Commit** "Devices: move Wi-Fi sync supervision into the NotePin package".

### Task 5: The contract, the NotePin adapter, `app/state/device.py` removed

**Files:**
- Create: `app/devices/base.py`, `app/devices/notepin/ble.py` (the rest of `app/state/device.py`), `tests/test_device_contract.py`
- Modify: `app/devices/__init__.py`, `app/devices/notepin/__init__.py`, `app/state/sync.py`, `app/routes/device.py`, `app/routes/diagnostics.py`, `app/devices/notepin/wifi.py` (its device-state import), docstrings naming `app/state/device.py` (`app/devices/notepin/ble_proto.py:1`, `src/scanner.py:1`, `src/crypto.py:6`), `docs/ARCHITECTURE.md:93,228`, `docs/reverse-engineering/README.md:19`, `docs/reverse-engineering/PROTOCOL.md:31`, `tests/test_cloud_device.py:26-51` (source-text tests read `app/devices/notepin/ble.py`), and every test importing `app.state.device`
- Delete: `app/state/device.py`

**Interfaces:**
- `app/devices/base.py`: `class Recorder(Protocol)` listing, with one-line docstrings, the required members below, plus `EXTRA_NAMES: dict[str, tuple[str, ...]]` mapping each extra to the functions it requires.
  - Required: `EXTRAS: frozenset[str]`; `get_state() -> dict` (keys `connected`, `device_name`, `device_address`, `serial_number`, `battery`); `is_connected() -> bool`; `async scan_device() -> dict`; `async scan_devices_with_retry() -> list[dict]`; `async scan_known() -> dict`; `async connect(address: str | None, serial_number: str | None) -> dict`; `async disconnect_device() -> dict`; `async depair_device() -> dict`; `async verify_connection() -> bool`; `force_reset() -> None`; `async poll_status() -> dict` (keys `is_recording`, `battery`, `charging`, `rssi`); `async list_device_files() -> list[dict]` (keys `session_id`, `size`, `duration_est`); `async download_recording(...)` (Task 3 signature); `async delete_device_file(session_id) -> dict` (key `deleted`); `diagnostics() -> dict`.
  - Extras: `"rssi": ("read_rssi",)`, `"info": ("get_device_info",)`, `"find_my": ("set_find_my",)`, `"wifi_sync": ("do_wifi_sync_blocking", "get_wifi_sync_status", "acknowledge_wifi_restore_notice")`.
- `app.devices.notepin`: `__init__` imports exactly those names from `ble` / `wifi` (no other code), and `EXTRAS = frozenset({"rssi", "info", "find_my", "wifi_sync"})`.
- `app.devices.active() -> ModuleType`: returns `app.devices.notepin` (docstring: one adapter today; choose by paired device type when a second exists).
- New NotePin functions in `ble.py`, made of code moved out of the routes:
  - `connect(address, serial_number)`: the body of today's `/device/connect` up to the response: `paired_serial_for_address`, `set_serial_number`, `connect_device`, `save_known_device`, and `set_connected(False)` on error (re-raise; the route keeps its error translation).
  - `scan_known()`: the body of today's `/device/scan-known`, returning the same dicts.
  - `poll_status()`: today's `_poll_device` from `/device/status`, including the "all commands failed → mark disconnected" handling; returns the dict above.
  - `diagnostics()`: `{"port_version": _device_state.get("port_version"), "last_handshake": get_last_handshake()}` (what `routes/diagnostics.py:65-67` reads now).
- `routes/device.py`: `dev = devices.active()` at module level; device calls go to `dev.*`, sync calls to `sync.*` (Task 3), and `run_async` is imported from `app.devices.loop` (`tests/test_ble_run_async.py` then patches `app.routes.device.run_async`).
- `sync._sync_files_inner` gets the device via `devices.active()`.

- [ ] **Step 1: Write `tests/test_device_contract.py`:**
  - `test_notepin_meets_the_contract`: every required member of `Recorder` (from its annotations and functions) exists on `app.devices.notepin`; coroutine functions in the Protocol are coroutine functions on the adapter.
  - `test_declared_extras_are_present`: for each name in `notepin.EXTRAS`, every function in `EXTRA_NAMES[name]` exists; every key of `EXTRAS` is in `EXTRA_NAMES`.
  - `test_active_is_notepin`: `devices.active() is app.devices.notepin`.
  - `test_nothing_imports_app_state_device`: `git grep -n "app.state.device\|app/state/device.py\|state import device"` over `app/`, `src/`, `src-tauri/src/`, `tests/` returns nothing.
- [ ] **Step 2: Run**: fails (no contract yet).
- [ ] **Step 3: Move and wire.** Tests: switch `import app.state.device as dev` to `from app.devices.notepin import ble as dev` so most patches keep working unchanged; repoint the few that target Wi-Fi or sync names.
- [ ] **Step 4: Run** the full suite, ruff and `cargo test`: all pass. `/device/*` responses are unchanged (the existing route tests cover them).
- [ ] **Step 5: Commit** "Devices: the NotePin adapter behind a small contract; app/state/device.py is gone".

### Task 6: NotePin Plaud-cloud credentials into `app/devices/notepin/plaud_cloud.py`

**Files:**
- Create: `app/devices/notepin/plaud_cloud.py`
- Modify: `app/state/cloud.py` (remove the moved functions), `app/routes/cloud.py:110-141`, `app/devices/notepin/ble.py`, `app/devices/notepin/wifi.py`, `app/routes/diagnostics.py:50-51` if it reads the token config through these helpers, tests: `test_ble_handshake.py`, `test_ble_scan_serial.py`, `test_wifi_sync_parent.py`, `test_cloud_device.py:94`, `test_cloud_signin.py`, `test_pairing_backup.py`

**Moves unchanged:** `_sn_sign_headers`, `_resolve_bind_token`, `bind_device`, `get_device_bind_token`, `fetch_sn_signature`, `get_device_sn_signature`. `extract_recording_date` is generic (`app/utils/recording_utils.py`) and stays. Plaud sign-in, cloud recordings and the rest of `app/state/cloud.py` stay.

**Interfaces:**
- `plaud_cloud` uses the account/session helpers it needs from `app.state.cloud` through a module import (`from app.state import cloud`), and `ble` / `wifi` call `plaud_cloud.<name>(...)` through a module import, so string patches like `patch("app.devices.notepin.plaud_cloud.get_device_sn_signature")` work.
- `routes/cloud.py` keeps the `/bind-device` and SN-signature routes (same URLs), calling `plaud_cloud`.

- [ ] **Step 1: Move**, repoint the string patches (`"app.state.cloud.<name>"` → `"app.devices.notepin.plaud_cloud.<name>"`) and direct calls in tests.
- [ ] **Step 2: Run** the full suite (including `tests/test_device_imports.py` from Task 2) and ruff: all pass.
- [ ] **Step 3: Commit** "Devices: NotePin pairing calls to Plaud's cloud live with the NotePin".

### Task 7: The app shows only the extras a device declares

**Files:**
- Modify: `app/routes/device.py` (`/device/status`), `app/renderer.js` (status handling, `startWifiSync`), `app/ui/settings.js` (`renderDeviceInfoPanel`, the Device panel loader at ~1186, RSSI display)
- Test: `tests/test_find_my.py` or a new route test, plus the existing UI source tests

**Interfaces:**
- `/device/status` adds `"extras": sorted(dev.EXTRAS)` to its JSON (always, connected or not).
- Frontend: one global `deviceHas(name)` in `app/renderer.js` reading the last status's `extras` (treat a missing list as "all", so an old backend never hides anything). Guards: `startWifiSync` returns early unless `deviceHas('wifi_sync')`; the Settings › Device info panel isn't requested unless `deviceHas('info')`; the Find My toggle renders only if `deviceHas('find_my')`; the signal-strength display hides unless `deviceHas('rssi')`.

- [ ] **Step 1: Write tests:** `test_status_lists_extras` (route returns `extras == ["find_my", "info", "rssi", "wifi_sync"]`); a UI source test that `deviceHas(` exists once at top level in `renderer.js` and that `startWifiSync` checks it.
- [ ] **Step 2: Run**: fail.
- [ ] **Step 3: Implement** the route field and the four guards. Run the global-name collision check from the project memory note (no duplicate top-level names across `app/ui/*.js`, `app/renderer.js`, `app/tauri-bridge.js`).
- [ ] **Step 4: Run** the full suite: all pass.
- [ ] **Step 5: Commit** "Device extras: the app shows only what the paired device supports".

### Task 8: Paper check, docs, final verification

**Files:**
- Modify: `docs/superpowers/specs/2026-11-08-device-adapters-design.md` (append "Paper check: a USB recorder"), `docs/ARCHITECTURE.md` (short "Adding a device" section: the contract, the folder, the bundle entry, `active()`)

- [ ] **Step 1: Write the paper check** (not code): for a recorder that mounts as a drive, one line per `Recorder` member saying what it would do (e.g. scan = look for the mounted volume, `download_recording` = copy and convert to Ogg Opus, `connect` = no-op), its estimated lines, and every change outside its own folder it would need. At least cover: recording ids (string ids are allowed by `validate_session_id`, but `recording_utils.extract_recording_date` and other code derive the date from a numeric NotePin id), converting other formats to Ogg Opus (which tool, already available on macOS or not), and the setup/pairing screens. If the estimate is far above ~150 lines or needs app changes, say so plainly: that is a finding, not a failure to hide.
- [ ] **Step 2: Measure** `git diff --shortstat <base>` for `app/` and `src/` (code only) and record it in the PR body; compare with the "under +150 lines" constraint.
- [ ] **Step 3: Verify the bundle**: `bash scripts/prepare-frontend.sh && CI=true npx --yes @tauri-apps/cli@2.12 build --bundles app` (signing set up as in `docs/RELEASING.md`); check `src-tauri/target/release/bundle/macos/Open Pin.app/Contents/Resources/app/devices/notepin/ble.py` and `.../app/devices/loop.py` exist. Then start the backend from a temp data dir (`PLAUD_DATA_DIR=<tmp> PLAUD_API_TOKEN=t PLAUD_API_PORT=5791 python app/api_server.py`) and check `/health`, `/device/status` (has `extras`), `/recordings`, `/diagnostics` return 200.
- [ ] **Step 4: Run** the full suite, ruff, `cargo test`, Worker tests (`cd infra/update-worker && npm test`): all pass.
- [ ] **Step 5: Commit** "Devices: paper check for a USB recorder, and how to add a device".

Hardware check for the owner before a stable release (not an agent step): pair, Bluetooth sync, Wi-Fi sync, Find My, delete-after-sync, Settings › Device panel.
