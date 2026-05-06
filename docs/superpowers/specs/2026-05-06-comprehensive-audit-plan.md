# Comprehensive Audit — Fix Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Each finding is a discrete commit. P0/P1 findings require a verification step before marking complete.

**Goal:** Apply the prioritized fixes surfaced by the comprehensive audit (design spec: `2026-05-06-comprehensive-audit-design.md`).

**Architecture:** No system architecture changes — this plan is a sequence of surgical fixes grouped into 4 phases. Phase 5 (architectural smells, test gaps) is deferred to a separate spec because the total finding count exceeded 50 (escape hatch defined in design spec §5).

**Tech Stack:** Rust (Tauri shell), Python 3.10+ (Flask backend), vanilla JS/HTML/CSS (frontend), `cargo`/`pyproject.toml` (build).

---

## Front-Matter

### Total findings: 117 (105 unique after cross-surface dedupe)

| Surface | Raw count | Notes |
|---|---|---|
| Python backend | 33 | 6 P1, 21 P2, 6 P3 |
| Tauri Rust shell | 27 | 6 P1, 10 P2, 11 P3 |
| Frontend | 28 | 6 P1 (after dedupe), 13 P2, 5 P3 |
| Build / config / docs | 22 | 1 P0, 3 P1, 12 P2, 6 P3 |
| Scripts triage | 7 (covering 36 files) | 0 P1, 2 P2, 5 P3 |
| **Cross-cutting (merged)** | **8** | merged from above |

### Severity distribution (post-dedupe, deferring Phase 5)

| Sev | Count | Phases |
|---|---|---|
| P0 | 1 | Phase 2 (SECURITY) |
| P1 | 21 | Phases 1, 2 |
| P2 | ~50 | Phases 1, 2, 3, 4 |
| P3 | ~33 | Appendix |

### Per-phase effort estimate

| Phase | Title | Findings | Effort |
|---|---|---|---|
| 1 | Bugs & correctness | 22 | ~12h |
| 2 | Security & data integrity | 14 | ~8h |
| 3 | Dead code & inconsistencies | ~25 | ~10h |
| 4 | Doc/comment drift | ~12 | ~3h |
| 5 (separate spec) | Architectural smells & test gaps | ~18 | L (multi-day) |
| Appendix | P3 nits | ~33 | optional |

### Critical-path callouts

1. **Phase 2 must complete before next release.** F-bd-001 (live `.env` creds on disk) and F-x-001 (version drift causing wrong version in every bug report) have user-visible impact and bug-tracking implications.
2. **Phase 3's `gdriveConnect` fix (X-007)** is required before any user can connect Google Drive — the call site passes 0 args to a 2-arg function. This may have been broken for some time.
3. **Phase 5 deferred:** items moved to `2026-05-06-audit-phase-5-arch-and-tests.md` (to be authored). Includes splitting `api_server.py` (~2700 lines), splitting `settings.js` (1832 lines), Notion-queue test gap, BLE state-machine test gap, no Rust tests, no JS tests, no CI/CD pipeline.

### Deferred / out-of-scope (with reasoning)

- **All P3 findings** — listed in the Appendix; fix opportunistically when in the area, not as standalone work.
- **`docs/TAURI_MIGRATION.md` rewrite (F-bd-016, F-bd-017):** the doc is unchecked-future-tense throughout; rewriting it as a historical record is a 1-2h doc job moved to Phase 4.
- **Notion DOMPurify hardening (F-fe-007):** existing `escapeHtml` discipline is sound; defense-in-depth via DOMPurify is a Phase 5 architectural decision.
- **Splitting `api_server.py` and `settings.js`:** phase 5.

---

## Format reminder

Each finding below uses the per-finding format defined in the design spec §4:

```
### F-NNN — <summary>
- File: <path>:<line>
- Category / Severity: <X> / <Pn>
- Description: <what / why>
- Fix: <how>
- Effort: <S | M | L>
- Verification: <required for P0/P1>
```

Findings prefixed `F-x-` are cross-cutting (merged from multiple agents). All others use the surface prefix the audit assigned.

---

# Phase 1 — Bugs & Correctness

All `BUG` and `LATENT` P0/P1 findings. Goal: stop wrong behavior under realistic conditions.

---

### F-x-001 — Version drift across Cargo.toml, tauri.conf.json, /health endpoint, RELEASING.md
- **Files:** `src-tauri/Cargo.toml:3`, `src-tauri/Cargo.lock` (`plaud-v2` entry), `app/routes/core.py:32` (Python `/health`), `docs/RELEASING.md:50-55`, also affects auto-bug-report system info
- **Category / Severity:** BUG / P1 (cross-cutting — merges F-rs-004, F-bd-007, F-bd-008, F-py-006)
- **Description:** `Cargo.toml` declares `version = "2.0.0"` while `tauri.conf.json` declares `"2.1.4"`. The Rust binary reports 2.0.0 to every auto-filed bug report and every updater check. The Python `/health` endpoint hardcodes `"1.0.0"`. Latest git tag is `v2.1.3` — `v2.1.4` was never tagged. RELEASING.md only documents bumping `tauri.conf.json`.
- **Fix:** (1) Update `Cargo.toml` `version = "2.1.4"` and run `cargo build` to update `Cargo.lock`. (2) Replace hardcoded `"1.0.0"` in the Python `/health` route with `importlib.metadata.version("plaud-v2")` (requires F-x-003 first). (3) Add steps to `RELEASING.md`: bump `Cargo.toml` and `pyproject.toml` together with `tauri.conf.json`; tag with `git tag v<X.Y.Z>`. (4) Tag the missing v2.1.4.
- **Effort:** S
- **Verification:** Run app, trigger a bug report, confirm system info shows `"version": "2.1.4"` in both Rust and Python sections. Confirm `git tag` lists `v2.1.4`.

---

### F-py-001 — `assert` used for handshake validation in production
- **Files:** `app/state/device.py:412`, `app/utils/wifi_sync_worker.py:385`
- **Category / Severity:** BUG / P1
- **Description:** `assert ChaCha20Poly1305(key).decrypt(...) == b"PLAUD.AI"` raises `AssertionError` (or is stripped under `python -O`). Even at default optimization, the user sees a generic AssertionError instead of a clear handshake-failure message.
- **Fix:** Replace both `assert` calls with `if ... != b"PLAUD.AI": raise ValueError("BLE handshake AAD validation failed")`.
- **Effort:** S
- **Verification:** With a deliberately wrong key, run the handshake; confirm a `ValueError` is logged with a clear message and the connect attempt cleanly fails (no `AssertionError`).

---

### F-py-002 — `_syncing` flag is not thread-safe
- **File:** `app/state/device.py:53, 890-897, 906-927`
- **Category / Severity:** LATENT / P1
- **Description:** `_syncing` is a bare module-level bool mutated from Flask request threads and the BLE event-loop thread. The read-modify-write pattern in `do_wifi_sync_blocking()` can race with `sync_files()`, allowing two concurrent syncs that corrupt `file_data` buffers and produce truncated `.ogg` files.
- **Fix:** Gate `_syncing` reads/writes behind `_ble_lock` (already used for `run_async`). Or convert to `threading.Event` with `is_set()`/`set()`/`clear()`.
- **Effort:** S
- **Verification:** From a test or curl loop, fire two concurrent `/sync` requests within ~50ms; confirm one returns `{"already_syncing": true}` (or equivalent) and only one sync runs.

---

### F-py-012 — WiFi sync permanently removes user's preferred networks if BLE fails early
- **File:** `app/utils/wifi_sync_worker.py:636, 682-692`
- **Category / Severity:** LATENT / P1 (data loss)
- **Description:** `remove_preferred_networks()` is called at line 636 *before* the try block. `restore_preferred_networks()` is in the `finally`. If `ble_connect_and_setup()` fails earlier (line 448-449), the function returns before the try, so the finally never runs. The user's saved Wi-Fi networks are permanently erased and must be re-added manually.
- **Fix:** Move `saved_networks = remove_preferred_networks()` *inside* the try block, immediately before `connect_to_hotspot()`. Or wrap the entire BLE+WiFi flow in one try/finally.
- **Effort:** S
- **Verification:** Force BLE setup to fail (e.g., disable Bluetooth mid-test); confirm `networksetup -listpreferredwirelessnetworks en0` still shows the user's networks afterwards.

---

### F-py-013 — Unbound `serial_number` reachable in `connect()` error path
- **File:** `app/routes/device.py:58-72`
- **Category / Severity:** BUG / P1 (low probability, but real)
- **Description:** If an exception fires before `serial_number = data.get(...)` is reached (e.g., import-time failure in `dev.run_async`), the `except` block references an unbound `serial_number` and raises `NameError`, masking the original exception.
- **Fix:** Initialize `serial_number = None` and `address = None` at the top of the function, before the try block.
- **Effort:** S
- **Verification:** Read the diff; confirm both vars are declared before any `try:`.

---

### F-py-019 — `list_device_files()` packet dedup logic re-processes packets
- **File:** `app/state/device.py:716-744`
- **Category / Severity:** BUG / P1
- **Description:** The multi-packet wait loop uses `len(new_packets) > len(packets) - 1` for new-packet detection, but `packets` already includes `r` at index 0, and `packets = [r] + new_packets` re-inserts on every iteration. The inner loop re-processes old packets, generating duplicate file entries.
- **Fix:** Track previously-processed packet count separately from the `packets` list. Iterate only over `new_packets[prev_len:]`. Deduplicate by session_id before returning.
- **Effort:** M
- **Verification:** With a device having ≥3 recordings, run sync; assert `len(set(rec.session_id for rec in result)) == len(result)` (no duplicates) and the count matches the device's `n_files` count.

---

### F-py-021 — `save_config()` deadlocks if called from within `load_config()` (corrupt-config recovery path)
- **File:** `app/utils/config.py:26-27`
- **Category / Severity:** LATENT / P1
- **Description:** `_config_lock` is a non-reentrant `threading.Lock()`. The corrupt-config-with-backup recovery path inside `load_config()` calls `save_config(data)`, which tries to re-acquire the same lock → immediate deadlock. This fires exactly when recovery is most needed.
- **Fix:** Change `_config_lock = threading.Lock()` → `threading.RLock()`. Or extract `_save_locked()` and call that from inside `load_config()` without re-acquiring.
- **Effort:** S
- **Verification:** Corrupt the live config (`echo "garbage" > .plaud_v2_config.json`), ensure a backup exists, start the app; confirm it recovers without hanging.

---

### F-rs-001 — `get_api_info` returns `status: "ready"` while server is still starting
- **File:** `src-tauri/src/commands/mod.rs:22`
- **Category / Severity:** BUG / P1
- **Description:** `status` is `"ready"` whenever `startup_error` is None, including during the async startup window. Frontend resolves `_apiInfoReady` immediately and fires API requests at a port the Python server may not yet be listening on.
- **Fix:** Add a `"starting"` status: `if process.is_some() && !server_ready_flag { "starting" } else if startup_error.is_some() { "error" } else { "ready" }`. Add a `server_ready: AtomicBool` set after `wait_for_server` returns true.
- **Effort:** S
- **Verification:** Add a debug log on the first `get_api_info` call; restart app and confirm the first call returns `"starting"` until the Python /health responds.

---

### F-rs-002 — `MainThreadMarker::new_unchecked()` called from non-main IPC threads (UB)
- **File:** `src-tauri/src/commands/mod.rs:142, 159`
- **Category / Severity:** LATENT / P1
- **Description:** `get_dock_visible` and `set_dock_visible` use `MainThreadMarker::new_unchecked()` and call AppKit (`NSApplication::sharedApplication`, `setActivationPolicy`) from Tauri's thread pool. AppKit calls from non-main threads are UB. Can crash or corrupt AppKit state.
- **Fix:** Dispatch the AppKit work via `tauri::async_runtime::spawn` with a `dispatch_async_main` bridge, or use `MainThreadMarker::new()` (returns `None` off-main) and return an error in that case so the caller can re-dispatch.
- **Effort:** M
- **Verification:** Toggle dock visibility 10× rapidly via the settings UI; confirm no crash and the dock state matches expected. (Best-effort — UB doesn't always manifest.)

---

### F-rs-003 — Shell injection surface in `install_update`
- **File:** `src-tauri/src/commands/mod.rs:440-492`
- **Category / Severity:** SECURITY / P1
- **Description:** `install_update` constructs a bash script with `format!()` interpolating `zip_path` (derived from a GitHub release asset name) and `app_path`. A maliciously crafted release-asset name with shell metacharacters could execute arbitrary commands. Mitigated by the trust boundary (only the configured update repo is queried), but the repo being compromised would mean RCE on every user.
- **Fix:** Pass values to the script as positional arguments via `cmd.arg(zip_path).arg(app_path)`, with the script reading `$1` and `$2`. Quote internally as `"$1"` / `"$2"`. Alternatively, validate the asset name matches `^[A-Za-z0-9._-]+$` and reject otherwise.
- **Effort:** S
- **Verification:** Read the diff; confirm no `format!()` interpolation of `zip_path` / `app_path` into the script body, and the script uses positional args inside double quotes.

---

### F-rs-005 — `find_free_port` silently returns a busy port
- **File:** `src-tauri/src/python.rs:47-54`
- **Category / Severity:** LATENT / P1
- **Description:** If all 100 ports in 5123–5222 are in use, falls back to returning `start` (5123) — guaranteed busy. Python crashes on bind, crash-monitor retries 3× with the same port, all fail, user sees a generic "SERVER_CRASH" with no port-conflict hint.
- **Fix:** Change return type to `Result<u16, String>` and return `Err("NO_FREE_PORT".into())` if exhausted. Surface the error via `startup_error` with a hint about port conflict.
- **Effort:** S
- **Verification:** Manually bind all 100 ports (`for p in {5123..5222}; do nc -l $p & done`), start the app, confirm the failure screen says "No free port".

---

### F-rs-006 — Diagnostics log writes can interleave on concurrent error reports
- **File:** `src-tauri/src/commands/mod.rs:756`
- **Category / Severity:** LATENT / P1
- **Description:** `append_diagnostic` opens with `OpenOptions::append(true)`. POSIX `O_APPEND` is atomic only for writes ≤ pipe-buffer (typically 512 bytes on macOS). Larger JSON lines from concurrent `report_error` calls can interleave bytes, producing malformed lines.
- **Fix:** Wrap the write in the existing `REPORTED_ERRORS` mutex, or add a dedicated `static DIAG_LOG_LOCK: Mutex<()>` around file writes. Format the entire line into a `String` first, then write it once under the lock.
- **Effort:** S
- **Verification:** Trigger 5 concurrent `report_error` IPC calls with long context strings (~1KB each); confirm every line in `diagnostics.log` is a valid JSON object.

---

### F-fe-001 — `app.currentSessionId` is read but never written — guards always false
- **Files:** `app/ui/transcript.js:422, 853`, `app/ui/utils.js:27-46`
- **Category / Severity:** BUG / P1
- **Description:** `app.currentSessionId === sessionId` checks at two guards always evaluate against `undefined`. Effect: post-transcribe auto-navigate is broken; tab-switch guard after summarize is a no-op.
- **Fix:** Set `app.currentSessionId = sessionId` at top of `openTranscript`. Clear with `app.currentSessionId = null` in `closeTranscript`.
- **Effort:** S
- **Verification:** Re-transcribe a recording, confirm UI auto-navigates to the new transcript.

---

### F-x-007 — `gdriveConnect` IPC contract mismatch (call site passes 0 args, bridge declares 2)
- **Files:** `app/tauri-bridge.js:238`, `app/ui/settings.js:454`
- **Category / Severity:** BUG / P1 (merges F-fe-002 + F-fe-028)
- **Description:** Bridge: `gdriveConnect: (client_id, client_secret) => apiCall('POST', '/gdrive/connect', { client_id, client_secret })`. Settings call site: `window.plaud.gdriveConnect()`. POST body is `{client_id: undefined, client_secret: undefined}`. Likely silently broken since the OAuth refactor.
- **Fix:** Read the Python `/gdrive/connect` route to determine actual contract. If credentials are server-side (likely), remove the parameters from the bridge declaration and the body. If frontend-supplied, add UI fields.
- **Effort:** S
- **Verification:** With a clean state, click Connect in Google Drive settings; confirm OAuth flow opens browser tab and the Settings UI updates after auth.

---

### F-fe-003 — Unescaped `err.message` injected via `innerHTML` (device-scan errors)
- **File:** `app/ui/settings.js:1561`
- **Category / Severity:** SECURITY / P1
- **Description:** `results.innerHTML = \`<p ...>Scan error: ${err.message}</p>\`` — `err.message` is not passed through `escapeHtml()`. A device name or backend response with HTML/JS markup would render as live HTML. Adjacent sites (513, 538, 1239, 1282) all use `escapeHtml`.
- **Fix:** `${escapeHtml(err.message)}`.
- **Effort:** S
- **Verification:** With a Plaud device renamed to `<img src=x onerror=alert(1)>`, trigger a scan error; confirm the alert does not fire.

---

### F-fe-004 — Unescaped directory paths injected via `innerHTML` (historical-dirs list)
- **File:** `app/ui/settings.js:1652`
- **Category / Severity:** SECURITY / P1
- **Description:** `histList.innerHTML = historical.map(d => \`<li>${d}</li>\`).join('')` — backend-supplied path strings rendered as raw HTML.
- **Fix:** `escapeHtml(d)` in the map.
- **Effort:** S
- **Verification:** Diff review.

---

### F-fe-025 — Unescaped device name in connecting indicator
- **File:** `app/ui/settings.js:1533`
- **Category / Severity:** SECURITY / P1
- **Description:** `results.innerHTML = \`<div class="dm-scanning">Connecting to ${name}</div>\`` — `name` is read from `row.dataset.name`, where the browser auto-decodes HTML entities, so the value at this site is raw and unescaped.
- **Fix:** `${escapeHtml(name)}`.
- **Effort:** S
- **Verification:** Device renamed with HTML markup; confirm no execution.

---

### F-x-008 — `app.currentView` value mismatch: `'list'` vs `'recordings'`
- **Files:** `app/ui/utils.js:28`, `app/ui/transcript.js:853, 889`
- **Category / Severity:** BUG / P1 (merges F-fe-008 + F-fe-021)
- **Description:** `currentView` is initialized to `'list'` and reset to `'list'` in `closeTranscript`, but the `transcribeRecording` guard checks `=== 'recordings'` — never true. Auto-open of finished transcript is dead unless the user is already on the transcript view.
- **Fix:** Pick one canonical value. Recommend `'recordings'` (more descriptive). Update `utils.js:28`, `closeTranscript`, all guards. Document the enum in the `app` object header.
- **Effort:** S
- **Verification:** Start a transcribe job, navigate to recordings list, wait for completion; confirm UI auto-navigates to the new transcript.

---

### F-rs-004 — (subsumed by F-x-001)

(Removed — see F-x-001.)

---

# Phase 2 — Security & Data Integrity

`SECURITY` findings + `BUG`/`LATENT` involving recordings/keys/config persistence.

---

### F-bd-001 — Live `.env` file with real GitHub PAT and Apple App-Specific Password on disk

> **⚠️ DEFERRED BY USER (2026-05-06):** Skipped from this execution run. The `.env` is gitignored and not committed; user accepts the on-disk credentials risk for now. Re-evaluate before sharing the repo broadly or moving to a shared dev machine. Related cleanup items (`.env.example` in F-bd-011, comment update in F-x-005) remain in scope.

- **File:** `.env`
- **Category / Severity:** SECURITY / P0
- **Description:** `.env` (gitignored, never committed per audit) contains a live GitHub personal access token, a live Apple app-specific password, and an App Store Connect API key. Live credentials on disk; if the machine is shared or backed up to an unencrypted location, they're leaked. Comment also references the deleted Electron `npm run publish` flow.
- **Fix:** (1) Verify never committed: `git log --all -S 'github_pat_11A525FRI' && git log --all -S 'zrsc-jwno-lpmg-yhxo'` should both be empty. (2) Rotate both credentials regardless. (3) Update the comment to reference `cargo tauri build`. (4) Add `.env.example` with placeholder values (covered in F-bd-011). (5) Document in `RELEASING.md` that these tokens live in `.env` only, never in code.
- **Effort:** S
- **Verification:** `git log --all -S '<token-prefix>'` returns no commits. New tokens issued. `.env.example` exists.

---

### F-bd-002 — `.notion_mcp.json` contains live OAuth tokens on disk
- **File:** `.notion_mcp.json`
- **Category / Severity:** SECURITY / P1
- **Description:** Live Notion `access_token` and `refresh_token` in plaintext. Gitignored.
- **Fix:** (1) Verify never committed: `git log --all -- .notion_mcp.json` empty. (2) Add a comment in `RELEASING.md` clarifying this file is auto-generated at runtime, never to be tracked.
- **Effort:** S
- **Verification:** `git log --all -- .notion_mcp.json` empty.

---

### F-bd-003 — `.hf_token` contains live HuggingFace token; may ship in DMG
- **File:** `.hf_token`
- **Category / Severity:** SECURITY / P1
- **Description:** Live HuggingFace token on disk. Gitignored. ARCHITECTURE.md describes it as "bundled for diarization" — if shipped in the DMG via the `keys/` resources, every user gets the developer's token, possibly violating HF ToS.
- **Fix:** (1) Verify never committed: `git log --all -- .hf_token` empty. (2) Clarify whether `.hf_token` ships in `keys/` to the DMG. If yes — replace with per-user prompt or document as accepted risk for shareable build. If no — document.
- **Effort:** S (verify) or M (per-user token UI if shipping)
- **Verification:** `git log --all -- .hf_token` empty. Clear answer on bundle inclusion documented in ARCHITECTURE.md.

---

### F-bd-004 — `session_keys.json` was tracked, now staged-deleted but not committed
- **Files:** `session_keys.json` (in git), `scripts/rsa_session.json` (untracked, related)
- **Category / Severity:** SECURITY / P1
- **Description:** Git status shows `session_keys.json` as ` D` — staged deletion uncommitted. May have been committed with content earlier. `scripts/rsa_session.json` (BLE capture artifact) is not gitignored.
- **Fix:** (1) Verify history: `git log --all -- session_keys.json` to see if it was ever committed with content. If yes, decide if `git filter-repo` is needed. (2) Commit the staged deletion. (3) Add `scripts/rsa_session.json` and `scripts/*.json` to `.gitignore`. (4) Delete `scripts/rsa_session.json` from working tree (it's an output of `handshake_test.py`, not source).
- **Effort:** S
- **Verification:** `git status` shows clean; `cat .gitignore | grep rsa_session` shows the entry.

---

### F-py-004 — Google OAuth client secret committed in source
- **File:** `app/utils/constants.py:110`
- **Category / Severity:** SECURITY / P2
- **Description:** `GDRIVE_CLIENT_SECRET` is hardcoded. The "desktop app — not confidential per Google docs" comment is technically right but enables phishing OAuth flows. For a shareable build, document the threat model or move to a build-time env var.
- **Fix:** Move to env var read at build time via Tauri's env-var bridge. Or add a `SECURITY.md` documenting accepted risk.
- **Effort:** S
- **Verification:** `grep -rn "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" app/` returns no source matches (only env reads).

---

### F-py-020 — Transcript update endpoint accepts unvalidated segment structures
- **File:** `app/routes/recordings.py:615-640`
- **Category / Severity:** SECURITY / P2
- **Description:** `/transcripts/update` writes user-supplied JSON directly to the transcript file without validating segment shape (start/end/text/speaker types). Localhost-only, but the WebView is a Chromium process; XSS in transcript content could exploit this to corrupt files.
- **Fix:** Validate at write: `segments` must be a list, each item must have numeric `start`/`end` and string `text`/`speaker`. Add a Pydantic model or hand-rolled check. Reject with 400 on invalid.
- **Effort:** S
- **Verification:** POST a malformed body (`{"segments": [{"text": "x"}]}`); confirm 400 response.

---

### F-py-022 — `list_folders()` builds Drive API query with unsanitized user input
- **File:** `app/state/gdrive.py:342-350`
- **Category / Severity:** SECURITY / P2
- **Description:** `q += f" and name contains '{query}'"` — single quote in `query` (e.g., `O'Brien`) breaks Drive API query syntax. Mild injection surface.
- **Fix:** Escape single quotes: `query.replace("'", "\\'")` before interpolation.
- **Effort:** S
- **Verification:** Search for `O'Brien` in Drive folder picker; confirm 200 response and reasonable results.

---

### F-py-003 — Session JSON write is non-atomic, no error handling
- **File:** `app/state/device.py:437-442`
- **Category / Severity:** LATENT / P2
- **Description:** `json.dump(..., open(...), ...)` — no context manager, no error handling, not atomic. FD leak. Mid-write crash corrupts the file.
- **Fix:** Use the same atomic write pattern as `save_config()` (write to `.tmp` then `os.rename`).
- **Effort:** S
- **Verification:** Diff review.

---

### F-py-009 — Background diarization races transcript file with no lock
- **File:** `app/routes/transcribe.py:242-302`
- **Category / Severity:** LATENT / P2
- **Description:** `_bg_diarize()` reads/writes `TRANSCRIPTS_DIR / f"{session_id}.json"` while the main thread or `/transcripts/update` may also write. No locking; one write silently overwrites the other.
- **Fix:** Per-session `threading.Lock` keyed in a module-level dict; acquire around all transcript reads/writes. Or stage to `.tmp` and atomic-rename.
- **Effort:** M
- **Verification:** Stress test: re-transcribe + concurrent /transcripts/update; assert all updates are present in the final file.

---

### F-py-010 — Notion `_tags_schema_cache` global mutated without lock
- **File:** `app/routes/notion.py:364, 425-471`
- **Category / Severity:** LATENT / P2
- **Description:** Concurrent push (manual + queue worker) can both fetch the schema, both attempt ALTER on the Tags column; one Notion error is swallowed.
- **Fix:** Wrap check-then-set with `threading.Lock`. Or move schema management into the queue worker so it's single-threaded.
- **Effort:** S
- **Verification:** Diff review. Optional: stress-test concurrent push/queue.

---

### F-py-011 — `_cloud_state["api_domain"]` mutated without lock + non-atomic config write
- **File:** `app/state/cloud.py:174-205`
- **Category / Severity:** LATENT / P2
- **Description:** Region-mismatch retry mutates shared dict and writes config atomically — wrong, neither is locked. Concurrent requests can race.
- **Fix:** Wrap `_cloud_state` mutations with a `threading.Lock`. Use `save_cloud_token()` (which goes through the atomic config write).
- **Effort:** S
- **Verification:** Diff review.

---

### F-py-015 — BIND_TOKEN truncated to 32 chars without validation
- **File:** `app/state/device.py:505`, `app/utils/wifi_sync_worker.py:422`
- **Category / Severity:** LATENT / P2
- **Description:** `(device_token or "")[:32].encode("ascii").ljust(32, b"\x00")` silently truncates. JWT tokens are >32 chars; truncation produces a wrong token and a confusing handshake failure.
- **Fix:** Validate `device_token` is a non-empty string of expected length; log a warning if truncation would occur.
- **Effort:** S
- **Verification:** Diff review; assert log entry on a too-short token.

---

### F-rs-009 — `open_external` accepts any URL scheme
- **File:** `src-tauri/src/commands/mod.rs:184-186`
- **Category / Severity:** SECURITY / P2
- **Description:** No scheme allowlist. A compromised IPC payload could open `file:///etc/passwd`, `ssh://...`, or custom URI handlers.
- **Fix:** Allowlist `https://`, `http://`, `mailto:`. Reject everything else with an error.
- **Effort:** S
- **Verification:** Manually invoke with `file:///etc/passwd`; confirm error returned, file not opened.

---

### F-x-002 — CSP allows `'unsafe-eval'` and `'unsafe-inline'` on `script-src`
- **File:** `src-tauri/tauri.conf.json:28`
- **Category / Severity:** SECURITY / P2 (merges F-rs-012 + F-bd-006)
- **Description:** `'unsafe-eval'` enables `eval()`/`new Function()`, which defeats CSP XSS mitigation. Combined with `'unsafe-inline'`, any DOM-injected string can execute as code. App is vanilla JS — no eval needed.
- **Fix:** Remove `'unsafe-eval'` from `script-src` and `default-src`. Test the app fully — toggle theme, view transcripts, view summaries (markdown render path), trigger error report, run sync. If all works, ship without it.
- **Effort:** M (if removing; S if adding nonces also)
- **Verification:** Open DevTools console after each tested flow; no CSP violation reports.

---

### F-sc-007 — Hardcoded BIND_TOKEN and SN_SIG in 10+ committed scripts
- **Files:** `scripts/pair_device.py:21`, `scripts/connect_device.py:18`, `scripts/session_manager.py:15`, `scripts/sync_device.py:15`, `scripts/wifi_transfer.py:22`, `scripts/wifi_sync.py`, `scripts/sync_and_transcribe.py:26`, `scripts/handshake_test.py:8`, `scripts/wifi_handshake_test.py:32`, `scripts/open_hotspot_and_test.py:16` (last one already in delete list F-sc-003)
- **Category / Severity:** SECURITY / P2
- **Description:** A specific user's JWT BIND_TOKEN and SN_SIG (for a real device) are hardcoded across many committed scripts. They identify a specific physical device; checked into history.
- **Fix:** Replace each `BIND_TOKEN = "..."` and `SN_SIG = "..."` with a config read (same pattern `test_wifi_connect_only.py` and `test_wifi_full.py` already use): `cfg = json.load(open(os.path.expanduser("~/Library/Application Support/com.plaud.v2/.plaud_v2_config.json"))); BIND_TOKEN = cfg["device_bind_tokens"][SN]; SN_SIG = cfg["device_sn_sigs"][SN]`. Add a `scripts/README.md` documenting the convention. Consider `git filter-repo` to scrub from history if these tokens are still valid (check `exp` claim).
- **Effort:** M
- **Verification:** `grep -rn 'BIND_TOKEN\s*=\s*"ey' scripts/` returns no results. Each script still works after the change.

---

# Phase 3 — Dead Code & Inconsistencies

`DEAD`, `INCONSISTENCY` at P1/P2, `LATENT` at P2 not already handled, IPC contract fixes.

---

### F-x-005 — Electron straggler comments and CSS property
- **Files:** `src-tauri/src/commands/mod.rs:280, 292, 565, 577, 816, 892`, `app/index.html:6, 864`, `app/tauri-bridge.js:2, 8, 126, 248`, `app/css/tokens.css:138`, `.env:2`, `app/main.js` (deleted), `tests/test_startup.py:26`
- **Category / Severity:** DEAD / P2 (merges F-rs-007, F-rs-025, F-fe-016/17/18/19, F-bd-018, also includes the `-electron-corner-smoothing` CSS property)
- **Description:** Migration-era comments and one non-standard CSS property left from the Electron era. The DMG-only updater has dead `.zip` fallback logic. Consolidated cleanup.
- **Fix:** Per file:
  - `commands/mod.rs:280, 292`: Remove `.or_else` ZIP fallback in `check_for_updates`; rewrite comments without "Electron" references at the four flagged sites.
  - `app/index.html:6`: `<!-- CSP managed by Tauri (tauri.conf.json) -->`.
  - `app/index.html:864`: Trim the migration note; describe what the code does.
  - `app/tauri-bridge.js:2, 8, 126, 248`: Rewrite each comment in present tense ("Tauri IPC bridge" not "drop-in replacement for Electron's preload").
  - `app/css/tokens.css:138`: Delete `-electron-corner-smoothing: system-ui;`.
  - `.env:2`: `# Used by: source .env && cargo tauri build`.
  - `tests/test_startup.py:26`: Update docstring to "CSP is in tauri.conf.json".
- **Effort:** S
- **Verification:** `git -C . grep -i electron -- ':!docs/superpowers/' ':!docs/TAURI_MIGRATION.md' ':!docs/reverse-engineering/'` returns empty.

---

### F-rs-008 — `get_theme` mutates dock icon (read-side-effect)
- **File:** `src-tauri/src/commands/mod.rs:90-98`
- **Category / Severity:** BUG / P2
- **Description:** Getter calls `update_dock_icon(&resolved)`. Should not mutate from a query.
- **Fix:** Remove `update_dock_icon` from `get_theme`. It already runs in `cycle_theme` (the only mutation path) and the theme-watcher thread.
- **Effort:** S

---

### F-rs-010 — `select_directory` blocks the async runtime
- **File:** `src-tauri/src/commands/mod.rs:1045-1049`
- **Category / Severity:** LATENT / P2
- **Description:** `async fn` calls `blocking_pick_folder()`, blocking a tokio worker thread for the dialog duration.
- **Fix:** Use the callback-based `pick_folder(...)` with a `tokio::sync::oneshot` to bridge back to async, or wrap in `tokio::task::spawn_blocking`.
- **Effort:** S

---

### F-rs-011 — `show_notification` action API used incorrectly; hardcoded `wifi-sync` event
- **File:** `src-tauri/src/commands/mod.rs:1018, 1027-1035`
- **Category / Severity:** BUG / P2
- **Description:** `.action_type_id("default").action_type_id(label)` overwrites; the intent is unclear from the chained call. The action callback emits a hardcoded `"wifi-sync"` event regardless of `action_label`.
- **Fix:** Read `tauri-plugin-notification` docs; set the action correctly. Replace hardcoded `"wifi-sync"` emit with the actual `action_label` parameter.
- **Effort:** S

---

### F-rs-013 — `cwd` calculation: nested `unwrap()` on `parent()`
- **Files:** `src-tauri/src/python.rs:476-480, 667-671`
- **Category / Severity:** LATENT / P2
- **Description:** `.unwrap_or(server_script.parent().unwrap())` panics if `server_script` has no parent. Duplicated copy-paste.
- **Fix:** Extract a `script_cwd(script: &Path) -> PathBuf` helper that returns `script.parent().unwrap_or(Path::new(".")).to_path_buf()`. Use it in both sites.
- **Effort:** S

---

### F-rs-014 — Crash-monitor lock held during 5s sleep window delays shutdown
- **File:** `src-tauri/src/python.rs:566`
- **Category / Severity:** LATENT / P2
- **Description:** Lock is acquired at top of each iteration, holds across a 5s sleep. `stop_python_server` (on quit) waits up to 5s before it can acquire.
- **Fix:** Restructure: lock → check → clone needed data → drop lock → sleep.
- **Effort:** M

---

### F-rs-015 — `device_id()` `child.stdin.take().unwrap()` panic risk
- **File:** `src-tauri/src/commands/mod.rs:585`
- **Category / Severity:** LATENT / P2
- **Description:** Inside `and_then`, `unwrap()` on stdin should be `?`-style fallback.
- **Fix:** Replace `.unwrap()` with `if let Some(stdin) = child.stdin.take() { ... } else { return Err(...) }`.
- **Effort:** S

---

### F-py-005 — `_extract_recording_date` duplicated verbatim
- **Files:** `app/routes/recordings.py:102-127`, `app/state/cloud.py:283-308`
- **Category / Severity:** INCONSISTENCY / P2
- **Description:** Identical 25-line function in two modules.
- **Fix:** Move to `app/utils/recording_utils.py`; import in both callers. Update the 2000000000 upper-bound comment to flag the 2033 expiration.
- **Effort:** S

---

### F-py-007 — ASAR-unpacked import shim references Electron packaging
- **File:** `app/api_server.py:14-30`
- **Category / Severity:** DEAD / P2
- **Description:** Branch handles Electron's "ASAR-unpacked" layout. Tauri uses sidecar; this branch is dead.
- **Fix:** Remove the ASAR branch and the `(PROJECT_ROOT / "app").exists()` check. Replace with a comment explaining the Tauri sidecar launch path.
- **Effort:** S

---

### F-py-008 — OAuth callback servers handle one request then die
- **Files:** `app/state/notion.py:371-377`, `app/state/gdrive.py:314-319`
- **Category / Severity:** LATENT / P2
- **Description:** `server.handle_request()` (one-shot, 120s timeout). On race or browser retry, callback is silently dropped; main process has no signal of success.
- **Fix:** Replace with a result `queue.Queue` or `threading.Event`. Loop `handle_request()` until success-or-timeout. Expose a polling endpoint the frontend checks.
- **Effort:** M

---

### F-py-014 — `ensure_installed()` swallows pip failures for required deps
- **File:** `app/utils/system.py:11-29`
- **Category / Severity:** LATENT / P2
- **Description:** `flask` and `requests` are installed via this "optional" path; if pip fails, the next `from flask import Flask` raises a confusing ImportError instead of "install failed".
- **Fix:** Add a `required: bool = False` parameter to `ensure_installed()`. Mark flask and requests as required; raise `RuntimeError` with installation instructions if both pip attempts fail.
- **Effort:** S

---

### F-py-017 — `load_cloud_token()` runs at module import (network/IO at import)
- **File:** `app/state/cloud.py:750-751`
- **Category / Severity:** LATENT / P2
- **Description:** Import-time network call + LevelDB iteration. Risk of slow startup / hang on no-network.
- **Fix:** Defer the validation network call to a background thread; trust saved tokens at import (already documented behavior) and validate async.
- **Effort:** S

---

### F-py-023 — WiFi WS server hardcoded port 8081 with no fallback
- **Files:** `app/utils/wifi_sync_worker.py:21`, `app/utils/wifi_sync.py:29`
- **Category / Severity:** LATENT / P2
- **Description:** Bind on 0.0.0.0:8081; if taken, OSError kills sync.
- **Fix:** Try 8081, then 8082, 8083 on bind failure. Surface a clear error if all three fail.
- **Effort:** S

---

### F-py-024 — `recover_metadata_from_transcripts()` runs at module import
- **File:** `app/routes/recordings.py:98-99`
- **Category / Severity:** ARCH / P2 (small enough to keep in this plan)
- **Description:** Synchronous filesystem scan + repeated `load_config()/save_config()` per import.
- **Fix:** Move to an explicit startup function called once from `api_server.py` after blueprint registration.
- **Effort:** S

---

### F-fe-005 — Health-gate poller never has a stored handle (cleanup is impossible)
- **File:** `app/ui/health-gate.js:77-88`
- **Category / Severity:** LATENT / P2
- **Description:** `setInterval` return value is discarded. `_bgPollerStarted` guard prevents double-start, but cleanup is impossible.
- **Fix:** Store handle: `_bgPollerHandle = setInterval(...)`. Expose `stopBackgroundPoller()`. Document explicitly that it runs for the app lifetime.
- **Effort:** S

---

### F-fe-006 — `_renderWifiProgress` race with `insertAdjacentHTML('beforeend', ...)`
- **File:** `app/renderer.js:504-512, 555-557, 617-619, 626-628`
- **Category / Severity:** BUG / P2
- **Description:** `pollActive` flag race window can call `_renderWifiProgress` after the success/done buttons are added, destroying them.
- **Fix:** Inside `pollProgress`, gate `_renderWifiProgress` calls on `if (state !== 'success' && state !== 'error' && pollActive)`.
- **Effort:** S

---

### F-fe-009 — Tag bulk-apply loop has no per-item error handling
- **File:** `app/ui/recordings.js:249-261`
- **Category / Severity:** LATENT / P2
- **Description:** Mid-loop throw aborts; partial state with no user feedback.
- **Fix:** Wrap each `await window.plaud.tagRecording(...)` in try/catch, accumulate failures, show a toast summarizing partial success.
- **Effort:** S

---

### F-fe-010 — Notion connect poller leaks intervals on rapid clicks
- **Files:** `app/ui/transcript.js:468-481`, `app/ui/settings.js:1184`
- **Category / Severity:** LATENT / P2
- **Description:** Each click creates a new interval; old ones run until their 120s timeout fires.
- **Fix:** Store handle on a module-level variable (or `notionBtn.dataset.pollHandle`). `clearInterval` any existing handle before creating new.
- **Effort:** S

---

### F-fe-011 — Crop playhead positioned by global percentage instead of regional
- **File:** `app/ui/transcript.js:1088-1089`
- **Category / Severity:** BUG / P2
- **Description:** `regionPct` and `containerRect` computed but unused; `pct` (global) used instead. Playhead visually wrong unless crop starts at 0%.
- **Fix:** `playhead.style.left = \`${regionPct * 100}%\``. Delete the unused `containerRect`.
- **Effort:** S

---

### F-fe-012 — `_exportDropdownCleanup` document listener leaks on `closeTranscript`
- **File:** `app/ui/transcript.js:269-273`
- **Category / Severity:** LATENT / P2
- **Description:** Listener references the (now-removed) `exportDropdown` element; null-ref throws on subsequent document clicks.
- **Fix:** In `closeTranscript`: `if (_exportDropdownCleanup) { document.removeEventListener('click', _exportDropdownCleanup); _exportDropdownCleanup = null; }`.
- **Effort:** S

---

### F-fe-013 — WiFi sync poller has no max-duration safety
- **File:** `app/renderer.js:532-544`
- **Category / Severity:** LATENT / P2
- **Description:** `pollActive` only cleared in finally; if `wifiSync` hangs, poller runs forever.
- **Fix:** Add a `pollStartedAt = Date.now()`; in the loop, check `Date.now() - pollStartedAt > 5*60*1000` and break with an error.
- **Effort:** S

---

### F-fe-014 — Google Drive OAuth poll leaks on Settings close
- **File:** `app/ui/settings.js:464-475`
- **Category / Severity:** LATENT / P2 (same pattern as F-fe-010)
- **Description:** Interval not cleaned up when settings close.
- **Fix:** Same as F-fe-010 — store at module scope, clear on `closeSettings`.
- **Effort:** S

---

### F-fe-015 — Empty summary save creates desync between `t.summary === ''` and falsy-check
- **File:** `app/ui/transcript.js:737, 769`
- **Category / Severity:** LATENT / P2
- **Description:** Saving an empty summary leaves `t.summary = ""`. `showTranscriptTab` falsy-checks; tab logic desyncs.
- **Fix:** In save handler: `if (!newSummary) { delete t.summary; showTranscriptTab('transcript', ...); } else { t.summary = newSummary }`.
- **Effort:** S

---

### F-py-016 — `__import__("os")` used inline despite `os` being imported
- **File:** `app/state/device.py:1021`
- **Category / Severity:** STYLE / P2 (small enough to fix)
- **Description:** `env={**__import__("os").environ, ...}` — `os` is already imported. Use it directly.
- **Fix:** `env={**os.environ, "PYTHONPATH": ...}`.
- **Effort:** S

---

### F-rs-018 — `python_env_info()` `OnceLock` populated too early (before deps installed)
- **File:** `src-tauri/src/commands/mod.rs:628-686`
- **Category / Severity:** LATENT / P2
- **Description:** Subsequent bug reports show stale package list.
- **Fix:** Drop the `OnceLock`; recompute fresh each time. The cost is one `pip list` per bug report — fine.
- **Effort:** S

---

### F-rs-020 — Inconsistent home-dir resolution (`std::env::var("HOME")` vs `dirs::home_dir()`)
- **File:** `src-tauri/src/lib.rs:46-51`, `src-tauri/src/commands/mod.rs:727-731`, also `src-tauri/src/python.rs` (multiple sites)
- **Category / Severity:** INCONSISTENCY / P2
- **Description:** Some sites fall back to `""` (current dir) on missing `$HOME`; others fall back to `/tmp` via `dirs`. Inconsistent.
- **Fix:** Add a `home_dir()` helper in `lib.rs` that uses `dirs::home_dir().unwrap_or_else(|| PathBuf::from("/tmp"))`. Replace all `std::env::var("HOME")` with calls to it.
- **Effort:** S

---

### F-rs-022 — `kill_orphan_python_servers` may kill unrelated Python servers
- **File:** `src-tauri/src/python.rs:370-396`
- **Category / Severity:** LATENT / P2
- **Description:** Scans port range 5123–5223; kills any Python listening. Could kill Jupyter or another local Python tool.
- **Fix:** Write child PID to `~/Library/Application Support/com.plaud.v2/server.pid` at startup. On startup, read this file and kill only that PID (verify it's still a Python process before SIGKILL).
- **Effort:** M

---

# Phase 4 — Doc / Comment Drift

`DOC_DRIFT` items + `TODO` resolution.

---

### F-bd-016 — `docs/TAURI_MIGRATION.md` is unchecked future-tense throughout despite migration being complete
- **File:** `docs/TAURI_MIGRATION.md` (entire file)
- **Category / Severity:** DOC_DRIFT / P2
- **Description:** All checkboxes unchecked, "Getting Started" still says "To begin Phase 0", rollback plan references non-existent Electron code.
- **Fix:** Either (a) convert to a historical migration record (retitle to `MIGRATION_RECORD.md`, check all completed items, remove future-tense framing), or (b) delete since `ARCHITECTURE.md` reflects the current state. Recommend (a) for archival value.
- **Effort:** S (option b) or M (option a, ~1h rewrite)

---

### F-bd-017 — `docs/TAURI_MIGRATION.md` rollback plan references deleted Electron code
- **File:** `docs/TAURI_MIGRATION.md:693-699`
- **Category / Severity:** DOC_DRIFT / P2
- **Description:** "Switch back to master branch", "Run `pnpm run build` as before" — neither exists post-Electron-removal.
- **Fix:** Subsumed by F-bd-016. If keeping the doc, the rollback section becomes "Rolled back to v1.10.4 tag if needed" — minimal historical reference.
- **Effort:** included in F-bd-016

---

### F-py-018 — Model names reference unverified IDs (`gpt-5.4-mini`, mismatched Claude model)
- **Files:** `app/providers/summarize.py:57, 99`, `app/providers/validate.py:29`
- **Category / Severity:** DOC_DRIFT / P2
- **Description:** `gpt-5.4-mini` is not a known OpenAI model. `claude-sonnet-4-6` (summarize) vs `claude-haiku-4-5` (validate) — should share a constants file.
- **Fix:** Verify model IDs are valid (check OpenAI's models list, Anthropic's). Extract model name strings to `app/providers/_models.py` shared constants. As of audit date the latest Claude is `claude-opus-4-7` (per env), `claude-sonnet-4-6`, `claude-haiku-4-5`. Verify `gpt-5.4-mini` — likely should be `gpt-4o-mini` or similar.
- **Effort:** S
- **Verification:** Trigger a summarize on a real transcript; confirm no API error.

---

### F-rs-017 — `wait_for_server` timeout mismatch (60 vs 30 attempts)
- **File:** `src-tauri/src/python.rs:542, 710`
- **Category / Severity:** DOC_DRIFT / P3 (promoted to Phase 4 for inclusion)
- **Description:** Initial: 30s. Restart: 15s. No documented reason for asymmetry.
- **Fix:** Define `const SERVER_READY_MAX_ATTEMPTS: u32 = 60` and use in both sites.
- **Effort:** S

---

### F-bd-022 — `ARCHITECTURE.md` aligned but `TAURI_MIGRATION.md` directory tree is stale
- **File:** Cross-reference; subsumed by F-bd-016.
- **Category / Severity:** DOC_DRIFT / P3
- **Fix:** included in F-bd-016 cleanup.
- **Effort:** included.

---

### F-x-003 — `pyproject.toml` has no `[project]` table (blocks F-x-001 version source-of-truth)
- **File:** `pyproject.toml`
- **Category / Severity:** BUILD / P2 (merges F-py-031 + F-bd-019)
- **Description:** Only `[tool.ruff]` sections. No `name`/`version`/`requires-python`. Means `importlib.metadata.version("plaud-v2")` raises `PackageNotFoundError`, blocking the canonical version-from-package fix in F-x-001. Also blocks `pip install -e .`.
- **Fix:** Add a minimal `[project]` table:
  ```toml
  [project]
  name = "plaud-v2"
  version = "2.1.4"
  requires-python = ">=3.10"
  ```
  Run `pip install -e .` from repo root to validate.
- **Effort:** S
- **Verification:** `python -c 'import importlib.metadata; print(importlib.metadata.version("plaud-v2"))'` prints `2.1.4`. Required precondition for F-x-001's `/health` fix.

---

### F-x-006 — Repository has no `LICENSE` file
- **File:** repo root
- **Category / Severity:** SECURITY / P2 (was F-bd-010)
- **Description:** README says "MIT" and `Cargo.toml` declares `license = "MIT"`, but no `LICENSE` file exists. For a project intended to be shareable, MIT is not legally effective without the actual text.
- **Fix:** Add `LICENSE` file at repo root with the standard MIT license text, year `2026`, copyright holder `Ollie Bednal`.
- **Effort:** S
- **Verification:** `ls LICENSE && head -1 LICENSE` shows the MIT header.

---

# Appendix — Deferred (P3 + Out-of-Scope)

These items are tracked but not scheduled for the executing-plans run. Pick them up opportunistically.

### Deferred to Phase 5 separate spec (architectural / test gaps)

To be authored as `2026-05-06-audit-phase-5-arch-and-tests.md`. Includes:

- **F-py-029** — No tests for BLE / sync / WiFi transfer (TEST_GAP, P2 → escalate)
- **F-py-030** — No tests for Notion queue (TEST_GAP, P2)
- **F-py-026** — `BleEncryption` unused — refactor or delete decision
- **F-py-028** — `wifi_sync.py` vs `wifi_sync_worker.py` duplication
- **F-fe-026** — Zero frontend tests (TEST_GAP, P2)
- **F-fe-027** — `settings.js` is 1832 lines (ARCH, P2)
- **F-rs-024** — No Rust tests (TEST_GAP, P3)
- **F-rs-023** — `report_bug` is blocking (ARCH, P3)
- **F-rs-027** — `tauri-plugin-decorum` uses old `objc` crate (BUILD, P3)
- **F-rs-016** — Hardcoded Python standalone build tag (BUILD, P2)
- **F-bd-009** — No CI/CD (ARCH, P2)
- **F-bd-020** — No pre-commit (ARCH, P3)
- **F-fe-007** — Markdown render relies on escape-then-regex (defense-in-depth)
- **F-fe-023** — `bgDevicePoll` `_bgBusy` reset race (LATENT, P2)
- **F-fe-024** — `_startupErrorShown` TDZ risk (LATENT, P2)
- **F-fe-022** — Health-gate `DOMContentLoaded` else-branch is dead (DOC_DRIFT, P3)
- **Splitting `api_server.py`** (referenced by 39-day-old refactor plan)

### Cross-cutting consolidation (already in main plan)

- **F-x-003** — Add `[project]` table to `pyproject.toml` (was F-py-031 + F-bd-019). **Promote into Phase 4** — required by F-x-001 to read version from `importlib.metadata`. Effort: S.
- **F-x-004** — Add ESLint config (was F-fe-020 + F-bd-012). **Promote into Phase 5 separate spec** since it's a new toolchain.
- **F-x-006** — Add LICENSE file (was F-bd-010). **Promote into Phase 4.** Effort: S.

### Pure P3 nits

| ID | File | Description |
|---|---|---|
| F-py-025 | `src/constants.py:12` | Remove unused `APP_KEY` constant |
| F-py-027 | `src/summarize.py` (whole file) | Delete unused module |
| F-py-032 | `app/state/device.py` various lines | Convert `print()` to `logging.debug()` |
| F-py-033 | `src/transcribe.py:356` | Delete dead `(seg.start + seg.end) / 2` expression |
| F-rs-019 | `tauri.conf.json:28` `connect-src https://*` | Narrow CSP connect-src to specific domains |
| F-rs-021 | `commands/mod.rs:612-616` `is_dev_mode` | Replace path-string check with `cfg!(debug_assertions)` |
| F-rs-026 | `Cargo.toml:28` `tokio` features | Remove redundant `tokio` dep or align with Tauri |
| F-bd-005 | `tauri.conf.json:51-52` | Move signing identity to env var |
| F-bd-013 | `src-tauri/` | Add `rustfmt.toml` |
| F-bd-014 | `pyproject.toml` | Add `[tool.pyright]` |
| F-bd-015 | repo root | Add `.editorconfig` |
| F-bd-021 | repo root | Add `rust-toolchain.toml` |
| F-fe-016/017/018/019 | (various) | All consolidated into F-x-005 in Phase 3 |

### Scripts triage (Phase 3 batch — included for completeness)

| ID | Action | Files |
|---|---|---|
| F-sc-001 | DELETE | `scripts/depair_v2.py` through `depair_v9.py`, `depair_now.py`, `depair_final.py` (10 files) |
| F-sc-002 | DELETE | `scripts/wifi_restore_*.py` series (8 files) |
| F-sc-003 | DELETE | `scripts/open_hotspot_and_test.py`, `scripts/test_hotspot_connect.py` |
| F-sc-004 | PROMOTE | `scripts/test_wifi_connect_only.py` → `tests/test_wifi_sync_worker.py` (with `pytest.mark.hardware`) |
| F-sc-005 | PROMOTE | `scripts/test_wifi_full.py` → `tests/test_wifi_full_integration.py` (with `pytest.mark.hardware`) |
| F-sc-006 | DELETE + GITIGNORE | `scripts/rsa_session.json`; add to `.gitignore` |

These are bulk operations. Run as a single Phase 3 commit batch: ~20 file deletions, 2 moves, 1 gitignore update.

---

## Cross-cutting findings I deliberately ruled out

- **Notion queue race conditions beyond what's listed** — needs deeper investigation; deferred to Phase 5.
- **Markdown rendering DOMPurify upgrade (F-fe-007)** — current escape-then-regex pattern is sound; defense-in-depth is a Phase 5 architectural decision.
- **Renaming `currentView` enum values across the codebase** — F-x-008 fixes the bug; full rename is a separate refactor.
- **The 39-day-old `project_refactor_plan.md` memory entry** — partially superseded; the modular extraction goal is partially achieved (renderer.js extracted, settings.js still monolithic). This audit's Phase 5 spec replaces it.

---

## Acceptance Criteria for This Plan

- [x] Every spec section has corresponding tasks (or explicit deferral with reasoning)
- [x] No placeholders, TBDs, or "implement later"
- [x] Each finding cites file:line, has a recommended fix, has effort estimate
- [x] Cross-surface duplicates are merged (`F-x-NNN` IDs)
- [x] Phase 5 split into separate spec per design escape hatch (>50 findings)
- [x] Critical-path callouts in front-matter
- [x] Scripts triage included as a Phase 3 batch operation

---

## Self-Review Notes

After writing, I checked:

1. **Spec coverage:** Every methodology check from §2 is addressed (mechanical sweep ran, 4 parallel agents dispatched, frontend personally audited via dispatched agent due to context, IPC consistency verified, config drift cross-checked, Electron leftovers grep'd).
2. **No placeholders:** Every fix line is concrete. No "TBD".
3. **Cross-cutting consistency:** F-x-NNN merges done. Phase 5 deferral applied per escape hatch.
4. **Effort sanity:** Phase 1 is ~12h of S-effort items, achievable in a single executing-plans session.
5. **Critical path:** Phase 2 must complete before next release for credential rotation reasons.
