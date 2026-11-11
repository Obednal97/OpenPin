# Over-engineering review, 8 November 2026

Scope: everything tracked except `src-tauri/gen/`, `app/requirements/*.txt`, lockfiles, `dist-tauri/`, data folders and `docs/`. Review only: no code was changed. Line numbers are against `master` at 896e6e8 (v2.9.2).

Method: vulture over `app/`, `src/` and `scripts/`; a script that maps every Flask route to its `window.plaud` wrapper and every wrapper to its UI callers; a CSS class sweep against JS and HTML tokens (dynamic class names checked by hand); `python -X importtime` on `import app.api_server` in `~/.venvs/plaud-v2`; then reading the large files. Every "unused" claim below was checked with `git grep` across Python, JS, Rust, HTML and config, excluding `docs/`. "Tests only" means the only references outside the definition are in `tests/`.

## 1. Summary

Estimated removable: about 3,500 lines (roughly 6% of the ~60k tracked lines) with no change to features. About 1,650 of those are the legacy CLI scripts. The rest splits roughly as Python backend 1,200, frontend 300, Rust and Worker 400. On top of that there are about 2 MB of assets embedded in the binary for nothing, and 0.5 to 1 s off every backend start from one lazy import. Test files that only exercise removed code would go too and are not counted.

Top 10, ranked by (lines saved x confidence) / risk:

| # | Change | Lines | Confidence | Risk |
|---|---|---|---|---|
| 1 | Delete the four legacy scripts in `scripts/` and the CLI-only parts of `src/transcribe.py` (1.1) | ~1,650 | high | low |
| 2 | Delete dead routes, their `window.plaud` wrappers and the state functions behind them (2.1) | ~250 | high | low |
| 3 | Replace hand-rolled `sha256.rs` with the `sha2` crate (4.1) | ~170 | high | low-med |
| 4 | Remove leftovers of the retired error reporting in Rust, Worker and JS (4.2, 3.5) | ~150 | high | low |
| 5 | Delete small dead Python: `_opus_packet_samples`, `get_database_name`, unread session-key files, unused constants, stale 3.10 shims (2.2) | ~150 | high | low |
| 6 | One `atomic_write` / `transcripts.load/update` helper for the ~16 hand-rolled atomic writes and 4 transcript readers (2.3) | ~130 | high | low-med |
| 7 | Frontend dead weight: dead and duplicate CSS, Electron-era update branches, unused icons, the Python copy in `prepare-frontend.sh` (3.1-3.4) | ~120 + ~2 MB | high | low |
| 8 | Shared OAuth callback server and PKCE, one keep-awake helper (2.4) | ~95 | high | low |
| 9 | Make `summarize.py` call `llm_json.py` instead of re-implementing every provider (2.5) | ~110 | high | med |
| 10 | Lazy-import `anthropic` (2.6): no lines saved, but 0.5 to 1 s off every launch before `/health`. Ranked on speed, not lines | 0 | high | low |

Next in line: table-driven settings get/save (~45, low), a `body_session_id` helper (~50, low), the shared `/transcribe` tail (~60, med), and the BLE/Wi-Fi save path and RSA handshake dedup (~140, med but on the hardware path).

Two findings change accuracy rather than just size: the "Large recordings" notification fires a fake click 100 ms after showing (4.3), and an error toast still says "This error has been automatically reported" (3.5).

## 2. Python backend

### 2.1 Dead routes and the code behind them

The UI talks to Flask only through `window.plaud` in `app/tauri-bridge.js`. Routes with no wrapper, and wrappers with no caller in `app/ui/*.js`, `app/renderer.js` or `app/index.html`:

| Route (handler) | Wrapper | State code only it uses | Lines | Notes |
|---|---|---|---|---|
| `/cloud/auto-detect` (`routes/cloud.py:35`) | none | `cloud.auto_detect` (`state/cloud.py:636`) | ~12 | no references at all |
| `/device/record` (`routes/device.py:359`) | none | `start_recording`, `stop_recording` (`state/device.py:1508,1514`) | ~32 | no references at all |
| `/device/find-my` (`routes/device.py:233`) | `setFindMy` | `set_find_my` (`state/device.py:1473`) | ~22 | `test_tauri_parity.py` lists it |
| `/cloud/recordings` (`routes/cloud.py:86`) | `cloudRecordings` | `list_cloud_recordings` (`state/cloud.py:1181`, 31 lines) | ~45 | parity test only |
| `/cloud/devices` (`routes/cloud.py:128`) | `cloudDevices` | `list_cloud_devices` (`state/cloud.py:1214`) | ~16 | parity test only |
| `/recordings/tag` (`routes/recordings.py:412`) | `tagRecording` | none | ~42 | docstring says "kept for compatibility only"; window and backend ship together, so nothing old can call it; `test_tags.py:279` already asserts no UI file uses it |
| `/tags` (`routes/recordings.py:406`) | `listTags` | none (`tags_store.all_tags` is used by MCP) | ~6 | MCP has its own `/mcp/v1/tags` |
| `/transcripts/delete` (`routes/recordings.py:1111`) | `deleteTranscript` | none (helpers shared with `/recordings/delete`) | ~36 | tests only |
| `/gdrive/upload`, `/gdrive/sync-status`, `/gdrive/upload-progress` (`routes/gdrive.py:71,105,110`) | `gdriveUpload`, `gdriveSyncStatus`, `gdriveUploadProgress` | `gdrive.get_upload_progress` (`state/gdrive.py:74`) | ~40 | `get_sync_status` stays (used by `/gdrive/status`) |

Fix: delete each route, wrapper and state function, and drop them from the route lists in `tests/test_tauri_parity.py` and the tests that call them. About 250 lines across Python and JS. Risk low. Verified: route-to-wrapper and wrapper-to-caller scripts, then `git grep` for each URL, wrapper name and state function (no dynamic `plaud[...]` access exists). The MCP server only calls `/mcp/v1/*`.

### 2.2 Small dead code and stale shims (~150 lines, low risk)

- `app/utils/audio.py:19` `_opus_packet_samples`: 70 lines, no callers anywhere, tests included.
- `app/state/notion.py:688` `get_database_name`: no callers.
- `app/utils/constants.py:55` `BLE_BATTERY_UUID`: no callers. `APP_NAME` (`:12`) is tests only, but it documents the rename, so keep it.
- `app/state/device.py:1055-1077` writes `DATA_DIR/sessions/session_<ts>.json` (session key, nonce, AAD) on every connect. Nothing reads these files; the only other use is the depair clean-up at `:1290`. `:1311-1316` deletes a `session_keys.json` only the old scripts wrote. Dropping the write also stops BLE session keys sitting on disk. ~25 lines. Verified: `git grep SESSIONS_DIR` and `session_` across app, src, scripts and Rust.
- `app/coaching/runner.py:625` `set_owner`: tests only; routes use `start_set_owner`. Move `test_coaching_runner.py:319` onto `start_set_owner`. ~18 lines.
- `app/mcp/access.py:246` `remove_connection_file`: tests only. ~8 lines.
- `except (asyncio.TimeoutError, TimeoutError)  # noqa: UP041` at `device.py:1207,1280,2590` and `wifi_sync_worker.py:1224,1470`, the "3.10 (allowed by pyproject)" note in `wait_event` (`device.py:1217`) and `notion_queue.py:95 # noqa: UP017`: pyproject pins 3.12, where these are the same thing.
- `app/state/device.py:1367-1470` `get_device_info`: returns about a dozen fields the Settings screen never reads (serial_number_full, firmware_version_raw, privacy_enabled, key_state, find_my_*, storage_field3_raw, accelerometer and others). The accelerometer read is an extra BLE round trip (up to 3 s) every time the Device panel loads. Drop the unread fields, the seven-line "let's check" comment at `:1373` and the repeated length check at `:1405`. ~30 lines, and the Device panel opens faster. Risk low. Verified per field with grep across `app/`, Rust and tests.
- `app/utils/wifi_sync_worker.py:1333-1334` redefines BLE UUIDs already in `constants`; `:1888` adds a third `sys.path` insert for a root the parent already passes.
- `CLOUD_RECORDINGS_DIR` is created (`state/cloud.py:865`) but nothing writes there.
- `cloud._decode_body` keeps an ImportError fallback for `cryptography`, a hard dependency.
- `sync_files` has a `progress_callback` parameter no caller passes; `device._update_config` is a one-line wrapper.

### 2.3 One atomic-write and transcript helper (~130 lines, low-med)

The "write to a temp file, `os.replace`" pattern is written by hand about 16 times: `state/device.py:1056,1800,1906`, `utils/wifi_sync_worker.py:248,1208`, `state/cloud.py:451`, `state/speakers.py:42`, `utils/tags.py:81`, `utils/notion_queue.py:160`, `utils/notion_legacy.py:379`, `utils/pairing_file.py:217`, `coaching/store.py:78`, `coaching/voice.py:230`, `mcp/access.py:129`, `routes/notion.py:109`, `routes/transcribe.py:136`. Some use a fixed `.tmp` name, which two writers can collide on.

There are also four "read a transcript under its lock" helpers: `recordings.load_transcript:940`, `runner._read_transcript:96` and `speakers._read:32` are identical, and `notion._read_transcript_json:63` is close. Inline read-modify-write appears again in `recordings.py:440,483,1076` and `transcribe.py:321,327,778`. Tag, rename and `/transcripts/update` currently write non-atomically, unlike every other writer.

Fix: `app/utils/fileio.py` with `atomic_write(path, data, *, mode=None, fsync=False)` on `mkstemp`, plus `transcripts.load(sid)` and `transcripts.update(sid, mutate)`. Leave `config._write_live` alone, because it keeps the `.bak`.

Risk: low-med. `get_transcript_lock` is not re-entrant, and `tags._write_transcript_tags` runs with the lock already held, so `update()` must not be called from inside it. Verified: `git grep "os.replace\|mkstemp"` (19 hits, each read) and `git grep get_transcript_lock`.

### 2.4 Duplicate helpers across state modules (~95 lines, low)

- OAuth callback servers: `state/gdrive.py:246-344` and `state/notion.py:598-659` differ only in port, path, service name and exchange call, and each has its own `_generate_pkce`. Fix: `oauth_page.serve_callback(port, path, service, state_fn, exchange_fn)` and `pkce_pair()`. ~65 lines. Tests patch only `notion._start_callback_server`.
- Keep-awake: `state/device.py:469 _keep_awake` and `utils/wifi_sync_worker.py:1775 _KeepAwake` are the same (the worker's docstring says so). Fix: one ~10-line context manager in `app/utils`. ~30 lines.

### 2.5 `llm_json.py` repeats `summarize.py` (~110 lines, med)

`app/coaching/llm_json.py:57-185` re-implements the Anthropic stream call, OpenAI, OpenRouter, Ollama, `_parse_chat_completion` and the code-fence parsing from `app/providers/summarize.py`. `llm_json.py:187-245` repeats the provider-list builder and sort from `summarize.py:583-635`.

Fix: summarize calls the `llm_json` provider functions with its schema, and picks out the summary and title. One `_cloud_attempts(config, preferred, factories)` builds the list for both.

Two behaviours must stay: in `summarize()`, a "local" preference falls through to cloud, and `_parse_summary` returns plain markdown for a non-JSON answer. Also dead: the non-tuple `else` branch at `summarize.py:641-644` (every provider lambda returns a tuple), and the "safety net" re-parse at `routes/transcribe.py:756-765` (every LLM path already goes through `_parse_summary`). Verified by reading both files side by side.

### 2.6 Lazy-import `anthropic` (startup, low risk)

`app/providers/summarize.py:22-25` and `app/providers/validate.py:6` import the SDK at module load. The chain `api_server -> routes.coaching -> runner -> review -> llm_json -> summarize` pulls it in on every launch.

Measured with `-X importtime`: anthropic is 0.66 to 0.74 s of 0.81 to 0.89 s total backend import on a warm run, and about 1.05 s of 1.37 s cold. `python.rs:174` already avoids anthropic in its own check for this reason.

Fix: import it inside `_anthropic_client` and `validate_key`. In the error classifiers (`summarize.py:535,560`), use `sys.modules.get("anthropic")`: an SDK exception can't exist if the SDK was never imported. Adjust `tests/test_summarize.py:154` and `tests/test_validate.py:53-75`, which patch the module attribute. The exact saving on the owner's MacBook is unverified.

### 2.7 Route-level repetition (low to med)

- `_error_json` is defined four times, identically: `routes/cloud.py:11`, `device.py:16`, `notion.py:23`, `transcribe.py:184`. Its docstring "auto-reported to GitHub" is stale. The `traceback` it adds is discarded by the bridge for `/notion/*`, `/transcribe` and `/summarize`, and no UI file reads it. Fix: one shared helper. ~20 lines, low.
- `routes/settings.py:59-100` and `:136-205`: about 30 `config.get(k, default)` lines and 25 `if k in data: config[k] = data[k]` lines. Fix: one `{key: default}` table for both directions, keeping the special cases (`model` to `whisper_model`, coaching/diarise order, `storage_mode` validation, key removal). ~45 lines, low.
- "Get `session_id`, 400 if empty, validate" appears 11 times, and the `session_ids` list form 4 times (`recordings.py`, `transcribe.py:687`, `notion.py:2036`, `gdrive.py:73`). Fix: `body_session_id(data) -> (sid, error_response)`. ~50 lines, low.
- `routes/transcribe.py:505-575` (cloud) and `:618-683` (local) share the same tail: diarisation plan, merge, coaching words, spoken tags, background work, response, auto-title. Fix: one `_finish(...)`. ~60 lines, med.
- `routes/recordings.py:197-322 collect_recordings` builds the same entry dict four times; `set(config.get("uploaded_session_ids"))` is rebuilt per file at `:220`. ~30 lines, low.
- `routes/core.py:131-137` sniffs the Accept header, but the only caller (`tauri-bridge.js:466`) always sends `?format=m4a`. The cloud-only branch at `:110-123` repeats `_get_or_transcode_m4a`. ~14 lines, low, confidence med-high.
- Small aliases: `runner._speaker_labels:119` duplicates `utils/speaker_names.speaker_labels:72`; `coaching/store.py:56-63` has `_lock` and `session_lock` as the same thing twice; `notion._extract_page_id:683` is an alias of `_extract_notion_id`; the provider-label dict in `transcribe._try_provider` is built twice (`:458,466`). ~25 lines, low.

### 2.8 Device, cloud and Drive internals

- BLE and Wi-Fi save paths: `state/device.py:2335-2430` and `utils/wifi_sync_worker.py:1253-1330` follow the same rules: a partial only grows, the encrypted copy is written first, a size mismatch is kept only if it decrypts. ~70 lines. Risk med (data integrity, though both are well tested).
- RSA/SN_SIG handshake: `device.py:900-985` and `wifi_sync_worker.py:1360-1420` share the notify handler, chunked writes, ACK wait and `PLAUD.AI` check. The BIND_TOKEN check is duplicated at `device.py:1112-1129` and `wifi_sync_worker.py:1478-1490`, and depair (`device.py:1260`) repeats the chunked write a third time. Fix: `ble_proto.rsa_session(...)` and `ble_proto.token32(...)`. ~70 lines. Risk med (hardware path, covered by fakes in `test_ble_handshake` and `test_wifi_worker_protocol`).
- Credential auto-fetch: `connect_device` (`device.py:921-940,1097-1108`) and `_do_wifi_sync_locked` (`:2160-2190`) each run "bind / fetch SN signature if missing". Fix: `cloud.ensure_device_credentials(sn)`. ~30 lines, med.
- `state/cloud.py`: the `/file/simple/web` listing and its fallbacks appear three times (`:850,1078,1183`, one of them in the dead `list_cloud_recordings`). `upload_to_cloud` (`:982`) writes Opus bytes to a temp file and reads them back in 5 MB chunks, where slicing the bytes in memory would do. `extract_recording_date` runs twice per file (`:1105,1133`). ~40 lines, low-med.
- `state/gdrive.py:427 _simple_upload` beside `_resumable_upload` (`:461`): the resumable path handles any size. `load`/`save` (`:81-122`) repeat one 8-key list. ~45 lines, med for the upload change.
- The "every non-auxiliary .ogg" loop appears at `gdrive.py:562,600,674`, `cloud.py:1100` and `routes/diagnostics.py:91`. ~20 lines, low.
- `state/device.py:707-771 scan_device` builds the same result dict three times. ~20 lines, low.
- `utils/audio.py:165-178 decrypt_ble_file` XORs byte by byte. The continuous path is one `Cipher(ChaCha20).decryptor().update(...)` call, measured on 10 minutes of audio at 0.06 s down to 0.001 s with identical output. ~6 lines, low.
- `utils/pairing_store.py:518-565` hand-rolls a single-worker queue that `ThreadPoolExecutor(max_workers=1)` provides. ~15 lines, low-med (tests use `flush`).

### 2.9 Runtime install shims (~65 lines, med)

`app/api_server.py:21-24` calls `ensure_installed("flask"/"requests")`, which Rust's `core_imports_ok` has already checked before spawning the backend. In `app/utils/system.py:59-121`, the non-venv path (`--user`, `--break-system-packages`, `is_externally_managed`) is never reached by the app, which always runs in its managed venv. Risk med: a developer running on a system Python loses it, and 6 tests in `test_python_runtime.py` cover it.

### 2.10 Unverified, needs hardware logs

- `state/device.py:224-280`: `FILTER_DATA_SESSIONS` and `ENFORCE_DATA_OFFSETS`. Neither is set in production.
- `state/device.py:1590-1665`: `CRC16_ENFORCED_VARIANT=None`, with five CRC variants computed on every download for logging only.
- `FAIL_ON_HANDSHAKE_REJECTED=False` at `device.py:1146` and `wifi_sync_worker.py:1498`. Never set in production.
- Together about 120 lines. If the MacBook's `app.log` shows which CRC variant matches, and that the switches never fire, they can be settled. There were no logs on the review machine to check, so the risk is high until then.
- `state/device.py:1861-1915 scrub_wifi_logs` is a one-time clean-up for logs from before v2.4.1. ~40 lines. Risk med: someone upgrading straight from before v2.4.1 would keep unredacted logs.

## 3. Frontend

### 3.1 `scripts/prepare-frontend.sh:33-44` copies the backend into the webview bundle

Lines 39-43 copy `api_server.py`, `routes/`, `state/`, `providers/` and `utils/` (with their `__pycache__` folders, ~1.8 MB here) into `dist-tauri/`, the `frontendDist` Tauri embeds in the binary. The backend runs from `bundle.resources` (`tauri.conf.json:46`, `python.rs:1450 find_server_script`). The copy is also incomplete (no `coaching/` or `mcp/`), which shows nothing uses it. As a side effect, the webview serves Python source at `tauri://localhost/routes/...`.

Lines 35-37 copy icons the webview never references. Fix: delete both blocks. ~10 lines and 1 to 2 MB off the binary. Risk low. Verified: read the script, `tauri.conf.json` and `python.rs:1450-1470`; `dist-tauri` is referenced only by the build config, `.gitignore` and README.

### 3.2 Unused image files (~250 KB, low)

- `app/icon_16.png`, `icon_32.png`, `icon_64.png`, `icon_128.png`, `icon_256.png` and `icon_1024.png`: unreferenced.
- `app/icon.png`: byte-identical to `icon_512.png`.
- `app/trayTemplate.png` and `trayTemplate@2x.png`: the tray uses `src-tauri/icons/tray.png` via `include_bytes!`, `lib.rs:222`.
- `app/assets/icon-black.png` and `icon-white.png`: the HTML uses the `.svg` versions.
- `app/assets/wordmark-header-black.png` and `wordmark-header-white.png`: unreferenced.
- `src-tauri/icons/icon.icns`: byte-identical to `app/icon.icns`, and only the `app/` copy is referenced. Unverified whether Tauri picks up `src-tauri/icons/` by convention, so check that before deleting it.
- `make_mark.py` and `make_wordmark.py` generate some of these. Keep the generators if wanted, but stop committing and copying the outputs.
- Verified: `git grep` for each name, and `cmp` for the duplicates.

### 3.3 Dead and duplicate CSS (~85 lines, low)

Dead classes, found by a class sweep and each checked for dynamic construction:
- `layouts.css`: `.app-title` (`:25`).
- `components.css`: `.badge-transcribing` (`:186`), `.wifi-sync-stages` (`:548-558`).
- `settings.css`: `.webhook-status` (`:63-72`), `.proc-recommended` (`:176-188`), `.proc-toggle-row` (`:190-200`), `.ai-key-row`, `.ai-key-name` and `.ai-key-detail` (`:288-295`), `.dm-toggles` (`:426`).
- `utilities.css`: `.text-center` and `.mb-3` (`:7,19`).

Duplicates:
- `settings.css:525-534` and `:615-624`: the same indeterminate progress rule and `@keyframes indeterminate-sweep` appear twice.
- `@keyframes slideDown` is in both `layouts.css:133` and `transcript.css:347` with different bodies. The later `transcript.css` copy already wins, so deleting the `layouts.css` one keeps today's rendering. It also means the update banner's intended -100% slide has never run.
- `setupSpin` and `wifiSpin` (`components.css:390,510`) copy `spin` (`utilities.css:29`).

### 3.4 Electron-era and impossible branches (~35 lines, low)

- `app/renderer.js:476-495`: the `major-update-downloading` and `major-update` branches (DMG reopen, "auto-opened by main process"). Rust only emits available, downloading, ready, up-to-date and error (`commands/mod.rs:485-559,807`). Verified: no `major-update` in Rust or tests.
- `recordings.js:680` and `settings.js:2353` fall back to `prompt()` when `selectDirectory` is missing, but the bridge always defines it. `recordings.js:551`'s "Restore is not available in this version" can't trigger either.
- `health-gate.js:31` has a fallback for an "older shell", which can't happen because the page and the shell ship in one binary. Keep the same no-status path at `:266,290`, which also covers a failed IPC call.
- `tauri-bridge.js:79,331` mention `preload.js`, which no longer exists.

### 3.5 Error-reporting leftovers in JS

- `app/renderer.js:994` tells the user "This error has been automatically reported." Nothing is sent: `report_error` only writes the local log (`commands/mod.rs:1316`). Change the text. This is an accuracy fix.
- `tauri-bridge.js:114-157` `_bridgeScrubReportText` is a "simplified mirror" of `report_scrub.rs`. It scrubs the first line of an error before it reaches the local diagnostics log, and Rust scrubs again on export. Fix: keep the "no raw_error or traceback for private endpoints" rule and drop the regex copy. ~30 lines. Risk med: this is a privacy choice, because the local log on the user's Mac would then hold unscrubbed first lines. Owner's call.
- `REPORT_ENDPOINTS` now only decides what goes to the local log. The name is a leftover; renaming it is optional.

### 3.6 Repeated UI code (~110 lines, low-med)

- Four copies of "poll until done, with a timeout": `settings.js:771-793` (Drive), `settings.js:1765-1785` (Notion), `transcript.js:672-698` (Notion again) and `transcript.js:1437-1467` (diarisation). Between them they hold 8 module-level handles. Fix: one `pollUntil(key, check, {everyMs, timeoutMs, onDone, onTimeout})` in `utils.js`, with handles kept in one `Map`, so no new globals. Keep the diarisation poll's error swallowing. ~45 lines.
- `settings.js:1562-1640`: about 10 toggles hand-written as "set, save, toast". Fix: a table and one `_bindSettingToggle`. Keep `auto-coach` and diarise by hand. ~30 lines, confidence med.
- `_shortenPath` (`settings.js:2176`) and `_shortenDirPath` (`recordings.js:581`) do the same job; keep the recordings version, which guards bare `/Users/`, in `utils.js`.
- `formatBytes` (`renderer.js:538`) and `formatStorageBytes` (`settings.js:1200`) differ only in precision.
- `_coachOpenVoiceSettings` (`coaching.js:968`) and `_coachProgOpenVoiceSettings` (`coaching-progress.js:392`) differ by one call.
- `cycleTheme` and `getTheme` in the bridge repeat the same four lines.
- `infoTip` (`utils.js:596`) has no callers, and `pathEl` (`settings.js:2348`) is assigned but never used.
- About 35 lines in total. Any helper moved into `utils.js` must get a name no other `app/ui/*.js` file declares at module scope.
- `setup.js:175 _handleTokenLogin` largely repeats `finishCloudSignIn` (`settings.js:989`), but differs in the sign-in sequence bump. Merging is med risk.

## 4. Rust shell

### 4.1 Hand-rolled SHA-256 (`src-tauri/src/commands/sha256.rs`, 175 lines, low-med)

Its header says it exists because `sha2` "is only a transitive dependency here", so it couldn't be imported without editing Cargo.toml. Cargo.lock already has sha2 0.10.9 (via `wry` and `tauri-codegen`). Adding `sha2 = "0.10"` and replacing the 5 call sites removes hand-rolled crypto from the update-verification path. Unverified whether wry's copy is compiled for macOS (cargo wasn't on the review machine's PATH); if it isn't, sha2 is still a tiny crate.

### 4.2 Leftovers from the retired `/v1/report` (~125 lines, low)

- `commands/mod.rs:1323` `report_error_blocking`: its `_timeout` parameter is unused and it always returns true ("the signature is kept"). `lifecycle.rs:384 PANIC_REPORT_GRACE` and the branch at `:406-411` now do exactly what `report_error` does. Fix: call `report_error`. ~20 lines.
- `commands/mod.rs:1272 error_fingerprint` and the `"fingerprint"` field in `append_diagnostic`: this deduplicated the Worker's GitHub issue markers. Nothing reads it now. ~30 lines.
- `commands/mod.rs:1120 server_snapshot(wait)`: the try-lock path and `include_python_env=false` (`:1140`) served the panic-time report. Production always passes `true` (`:1394`); `false` is used only by the test at `:2178`. ~25 lines.
- Startup-status round trip: `python.rs:888` emits `startup-status`, and `lifecycle.rs:150 track_startup_messages` listens for Rust's own event only to call `set_startup_message`. No JS subscribes (`onStartupStatus` has no callers; `health-gate.js` polls `get_startup_status`). Fix: call `set_startup_message` directly, and delete the listener, the bridge method and its `_eventCallbacks` slot. ~15 lines.
- `infra/update-worker/src/index.js:83-86`: an orphaned comment about @mentions and fingerprint markers. In `:112-115`, `ghJson`'s `init` and body handling only served the issue POST, and `ghHeaders`' `accept` parameter is never passed. ~8 lines.
- `power.rs:142-150`: the WillSleep observer emits `system-will-sleep`, which nothing listens to (the source says "Nothing listens yet"). ~15 lines.
- `report_scrub.rs` itself is live (`export_bug_report` -> `scrub_for_report`). Keep it.

### 4.3 Fake notification action (`commands/mod.rs:1705-1738`, ~25 lines, low)

When `action_label` is set, Rust emits `notification-action` 100 ms after showing the notification, whether or not anyone clicks. So `renderer.js:855` starts the Wi-Fi flow automatically. The "Click to use Wi-Fi transfer" text (`renderer.js:552`) is misleading, and clicking does nothing extra.

Fix: drop `action_label`, the thread and the emit, plus the `onNotificationAction` bridge method and listener. Call `startWifiSync()` right after `showNotification` at `renderer.js:550`, which keeps today's behaviour exactly. Separately, consider changing the wording. Verified by reading both sides.

### 4.4 Capabilities and plugins (~35 lines, low-med)

- `src-tauri/capabilities/default.json` grants 36 permissions. The webview uses only `invoke`, `event.listen`, window `startDragging` and `toggleMaximize`, and `plugin:dialog|open` and `|save`. Tray, menu, notification and autostart are driven from Rust, which capabilities don't gate. Fix: keep `core:default`, `core:window:allow-start-dragging`, `core:window:allow-toggle-maximize` and `dialog:default`. This also cuts attack surface. Unverified at runtime: smoke-test drag, maximise, the dialogs and events afterwards.
- `tauri-plugin-opener` (`Cargo.toml:15`, `lib.rs:99`) is registered but unused: `open_external` uses the `open` crate (`mod.rs:230`), and nothing calls `plugin:opener`. Drop the crate, the init and `opener:default`.

### 4.5 Smaller Rust items (~90 lines, low)

- `cfg(not(target_os = "macos"))` stubs (`mod.rs:103,178`, `power.rs:91`) and about 20 `cfg(target_os = "macos")` gates: the objc2 crates are unconditional dependencies, so a non-mac build fails anyway. ~25 lines.
- `python.rs:84 generate_api_token` has a RandomState fallback for a missing `/dev/urandom`. Return an error instead. ~17 lines.
- `commands/mod.rs:60 resolve_theme` repeats `lib.rs:45 is_system_dark_mode` (the same `defaults read`). ~8 lines.
- `python.rs:763 health_ok` and `:847 send_ble_disconnect` are two hand-rolled HTTP/1.0 clients. Fix: one `local_request(method, path, timeout)`. The doc line at `:845` ("fire-and-forget") contradicts the code. ~15 lines.
- `append_diagnostic` (`mod.rs:1191`) re-implements the size rotation in `logging.rs RotatingFile`. ~10 lines.
- Unverified, med risk: `power.rs:96-133` has a wall-clock fallback wake detector (~70 lines) behind the primary NSWorkspaceDidWake source. Check the owner's `app.log` for "wall-clock gap" wakes before cutting it.

## 5. Worker

See 4.2 for its report leftovers. Nothing else is worth trimming. The validation it shares with the app is defence across a trust boundary, and the wrangler migrations and the `workers.dev` route are needed for history and old installs.

## 6. scripts/ and src/

### 6.1 Legacy CLI scripts (~1,650 lines, low)

`scripts/sync_and_transcribe.py` (519), `sync_device.py` (420), `session_manager.py` (384) and `download_all.py` (~160). `scripts/README.md` already says "Nothing in the app, build or tests uses these" and calls them reference code. Fix: delete them (git history keeps them) and cut that README section to one line.

That leaves CLI-only code in `src/transcribe.py`: `setup_diarization` (`:212`, used only by `sync_and_transcribe.py`), `transcribe_directory` (`:699`, used only by `__main__`) and the `__main__` block (`:743`). About 165 lines. Verified: `git grep` finds no references outside `scripts/` and the README, and no tests.

The rest of `src/` is live: `crypto`, `scanner` and `transcribe` are imported by `app/` and bundled by `tauri.conf.json:46-50`.

## 7. Dependencies and startup

- `anthropic` eager import: see 2.6. It is the single biggest startup cost in the backend.
- `tauri-plugin-opener` is unused (4.4). `sha2` can replace `sha256.rs` (4.1).
- Every package in `scripts/python-deps/*.in` is imported somewhere. `percent-encoding`, `signal-hook`, `tokio`, `block2` and `libc` are all used.
- The Device panel's accelerometer read costs a BLE round trip on every open (2.2).
- Google Fonts Inter loads over the network on every launch. Bundling the woff2 keeps the look and works offline. Optional, saves no lines.
- Test coupling to watch: `tests/test_startup.py:136` asserts `count("is_abort_error(") >= 2` and `tests/test_tauri_parity.py:314-317` asserts on the text of `REPORT_ENDPOINTS`. Source-text tests like these break on harmless refactors and pin leftover names.

## 8. Looked at, left alone

- `notion_legacy.py` and the pre-v2.4.0 page adoption in `routes/notion.py` (~900 lines with tests): a live feature with UI (`transcript.js:551-639`). Retiring it is a product decision, not trimming.
- Notion add-only schema and tag-option logic (`routes/notion.py:725-1003`): it stops the app wiping the user's select options.
- `notion_queue.py`: a real state machine (generations, review list, pause and probe). Only the entry construction could share a helper (~8 lines).
- `app/mcp/server.py`: stdlib-only on purpose (fast start, never imports `app.*`), so its own schema checker, bounded HTTP and repeated `DATA_DIR` constants are justified.
- `mcp/clients.py:241-297`: compare-and-swap, backups and fsync are right for editing the user's Claude config files. `mcp/access.py`: the off-marker, lock and SIGTERM clean-up are each documented.
- `api_server.py` security layers (Host check, token, CORS, MCP guard).
- `config.py` fsync, `.bak` and re-entrant lock: its header documents the crash-safety rules.
- `pairing_store` core: the stdin/hex calls to `security` keep secrets out of `ps`.
- `ble_proto.bind_handshake` retries (16-byte and legacy tokens): they cover real device generations.
- `wifi_sync_worker` restore record, `WifiRun` and `HotspotHold`: they protect the user's Wi-Fi settings.
- `cloud.legacy_cloud_session_id`: still matches files already on disk.
- `apiCall` retry-once, `TRANSIENT_MARKERS` and the `sendBeacon` disconnect: each fixes a documented failure.
- The `typeof fn === 'function'` guards across UI files: node UI tests load single files with a fake DOM.
- Hand-rolled `renderMarkdown` (`utils.js:403-560`) and the SVG sparkline: small and escape-safe; a library would weigh more.
- All 22 `#[tauri::command]`s are invoked from JS.
- `report_scrub.rs`: live, and hand-rolled because there is no regex crate in the build.
- The custom updater and its installer: it checks signature, team ID, bundle ID and translocation. Moving to tauri-plugin-updater would be a redesign.
- `migrate.rs`: the Open Pin rename is recent. Keep it until old installs have updated.
- Managed Python with a Homebrew fallback, `find_free_port` plus Python's port walk (TOCTOU), and the per-launch `core_imports_ok` check (~0.12 s, repairs damaged venvs).
- The custom Rust logger is needed before the Tauri builder runs. Its RUST_LOG parser (~50 lines) is dev-only and optional to cut.
- Coaching metrics, engagement, progress and review: compact domain logic.
- `migrate_speaker_labels` and the inline-`words` fallback in the coaching store: tiny shims that can go once a minimum upgrade version is set.
