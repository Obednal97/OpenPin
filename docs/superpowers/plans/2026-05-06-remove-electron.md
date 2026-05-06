# Remove Electron Build — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete every file whose only purpose is the Electron build, strip the Rust migration shims that import Electron-era state, and update narrative docs so the codebase reflects "Tauri only".

**Architecture:** Tauri's pipeline (`tauri.conf.json` → `scripts/prepare-frontend.sh` → `dist-tauri/`) is already independent of any Electron file. The work is therefore deletion plus a small Rust trim and a docs refresh — no architectural change.

**Tech Stack:** Tauri 2 (Rust), Flask Python backend, vanilla JS frontend.

**Spec:** `docs/superpowers/specs/2026-05-06-remove-electron-design.md`

---

## File Structure (target state)

After all tasks complete:

| Path | Disposition |
|---|---|
| `app/main.js`, `app/preload.js` | Deleted |
| `app/package.json`, `app/package-lock.json`, `app/node_modules/` | Deleted |
| `app/entitlements.mac.plist`, `app/eslint.config.mjs`, `app/.stylelintrc.json`, `app/dist/` | Deleted |
| `bin/`, `scripts/download-ffmpeg.sh` | Deleted |
| `src-tauri/src/lib.rs` | Migration block in `load_prefs()` and updater-cache cleanup block removed |
| `src-tauri/src/python.rs` | `electron_python_dir()` fn + the fallback in `find_python()` removed |
| `app/api_server.py` | Module docstring updated (lines 2-6) |
| `app/renderer.js` | Comment at line 767 updated |
| `README.md` | Architecture diagram + Development section rewritten |
| `docs/ARCHITECTURE.md` | Overview, Packaging, Auto-Updates, Auto Bug Reporter sections rewritten |
| `docs/RELEASING.md` | Wholly rewritten for Tauri release flow |
| Everything else under `app/` (index.html, ui/*, css/*, assets/*, api_server.py, routes/, state/, providers/, utils/) | Untouched (still consumed by Tauri) |
| `src-tauri/` | Otherwise untouched |
| `docs/TAURI_MIGRATION.md`, `docs/reverse-engineering/*.md` | Untouched (historical record) |

---

## Pre-flight check

- [ ] **Step 1: Confirm working tree is clean**

Run: `git -C . status`

Expected: only the untracked `scripts/depair_*.py`, `scripts/wifi_restore_*.py`, `.plaud_v2_config.json.bak`, `.DS_Store` modifications, and `session_keys.json` deletion — these are pre-existing and unrelated to this work. No staged or modified files in `app/`, `src-tauri/`, or `docs/` should exist.

- [ ] **Step 2: Confirm spec is committed**

Run: `git -C . log --oneline -3`

Expected: recent commit `e369835 Add design spec for removing the Electron build` is present.

---

## Task 1: Phase 1 — Delete Electron app shell + build assets

**Files (all deleted):**
- `app/main.js`
- `app/preload.js`
- `app/package.json`
- `app/package-lock.json`
- `app/node_modules/`
- `app/entitlements.mac.plist`
- `app/eslint.config.mjs`
- `app/.stylelintrc.json`
- `app/dist/`
- `bin/ffmpeg` (the only file in `bin/`)
- `bin/` (now empty, remove)
- `scripts/download-ffmpeg.sh`

- [ ] **Step 1: Verify each target exists**

Run:
```
ls ./app/main.js \
   ./app/preload.js \
   ./app/package.json \
   ./app/package-lock.json \
   ./app/entitlements.mac.plist \
   ./app/eslint.config.mjs \
   ./app/.stylelintrc.json \
   ./scripts/download-ffmpeg.sh \
   2>&1
ls -d ./app/node_modules \
      ./app/dist \
      ./bin \
      2>&1
ls ./bin/ 2>&1
```

Expected: every path exists; `bin/` contains only `ffmpeg` (no other files to worry about).

If `bin/` contains anything other than `ffmpeg`, STOP and re-evaluate — the plan assumed `ffmpeg` was the only file there. If something else is there, escalate before deleting.

- [ ] **Step 2: Run a baseline check that Tauri builds before deletion**

Run: `cd ./src-tauri && cargo check 2>&1 | tail -5`

Expected: `Finished` with no errors.

This establishes a baseline so any post-deletion failure clearly comes from the deletion.

- [ ] **Step 3: Delete the Electron app files**

Run:
```
rm -f ./app/main.js \
      ./app/preload.js \
      ./app/package.json \
      ./app/package-lock.json \
      ./app/entitlements.mac.plist \
      ./app/eslint.config.mjs \
      ./app/.stylelintrc.json
rm -rf ./app/node_modules \
       ./app/dist
rm -rf ./bin
rm -f ./scripts/download-ffmpeg.sh
```

- [ ] **Step 4: Verify the deletions**

Run:
```
ls ./app/main.js 2>&1 || echo "(deleted - expected)"
ls -d ./app/node_modules 2>&1 || echo "(deleted - expected)"
ls -d ./bin 2>&1 || echo "(deleted - expected)"
ls ./scripts/download-ffmpeg.sh 2>&1 || echo "(deleted - expected)"
```

Expected: each command emits "(deleted - expected)".

- [ ] **Step 5: Verify Tauri-required files are still present**

Run:
```
ls ./app/index.html \
   ./app/renderer.js \
   ./app/tauri-bridge.js \
   ./app/styles.css \
   ./app/api_server.py \
   ./app/icon.icns \
   ./app/icon_512.png
ls -d ./app/ui \
      ./app/css \
      ./app/assets \
      ./app/routes \
      ./app/state \
      ./app/providers \
      ./app/utils
ls -d ./src-tauri \
      ./src \
      ./keys
```

Expected: all paths exist.

- [ ] **Step 6: Verify all remaining JS files still parse**

Run:
```
for f in ./app/renderer.js \
         ./app/tauri-bridge.js \
         ./app/ui/*.js; do
  node -c "$f" || echo "FAIL: $f"
done
```

Expected: no output (all files parse).

- [ ] **Step 7: Verify the Tauri frontend prep script still works**

Run: `cd . && bash scripts/prepare-frontend.sh`

Expected: `Frontend prepared in ./dist-tauri`. No errors. The script does not reference any deleted file.

- [ ] **Step 8: Verify Tauri still compiles**

Run: `cd ./src-tauri && cargo check 2>&1 | tail -10`

Expected: `Finished` with no errors. (The migration shims are still present at this point — they will be removed in Task 2.)

- [ ] **Step 9: Commit**

```
git -C . add -A app/main.js app/preload.js app/package.json app/package-lock.json app/node_modules app/entitlements.mac.plist app/eslint.config.mjs app/.stylelintrc.json app/dist bin scripts/download-ffmpeg.sh
git -C . commit -m "chore: remove Electron app shell and build assets"
```

Note: `git add -A <path>` stages deletions. If git complains that some paths are already untracked or out of scope, simplify with:
```
cd . && git add -A
git commit -m "chore: remove Electron app shell and build assets"
```
…but **first verify** with `git status` that no unrelated files are staged. The pre-existing untracked `scripts/depair_*.py` etc. should NOT be in the commit.

After the commit, run: `git -C . show --stat HEAD | head -20`

Expected: only the deleted Electron files appear (with their sizes shown as removals).

---

## Task 2: Phase 2 — Strip Tauri-side migration shims

**Files:**
- Modify: `./src-tauri/src/lib.rs` (delete two blocks)
- Modify: `./src-tauri/src/python.rs` (delete one fn + clean up `find_python`)

### Step 1: Strip the prefs migration block in `lib.rs`

- [ ] **Step 1: Read the current `load_prefs` function**

Run: `sed -n '53,72p' ./src-tauri/src/lib.rs`

Expected output (current state):
```rust
fn load_prefs() -> serde_json::Value {
    let prefs = std::fs::read_to_string(prefs_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(serde_json::json!({}));

    // One-time migration: if tauri-prefs is empty, import from Electron prefs
    if prefs.as_object().map(|o| o.is_empty()).unwrap_or(true) {
        let electron_prefs_path = std::path::PathBuf::from(std::env::var("HOME").unwrap_or_default())
            .join("Library/Application Support/Plaud V2/electron-prefs.json");
        if let Ok(data) = std::fs::read_to_string(&electron_prefs_path) {
            if let Ok(electron_prefs) = serde_json::from_str::<serde_json::Value>(&data) {
                log::info!("[Migration] Importing preferences from Electron build");
                save_prefs(&electron_prefs);
                return electron_prefs;
            }
        }
    }
    prefs
}
```

- [ ] **Step 2: Replace `load_prefs` with the stripped version**

Use Edit to replace the multi-line block above with this (the migration `if`-block is gone; the function returns `prefs` directly):

```rust
fn load_prefs() -> serde_json::Value {
    std::fs::read_to_string(prefs_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(serde_json::json!({}))
}
```

(This collapses the no-longer-needed `let prefs = ...; ...; prefs` ceremony into a direct return now that the migration branch is gone.)

- [ ] **Step 3: Verify the migration block is gone**

Run:
```
grep -n "Migration\|electron_prefs\|electron-prefs" ./src-tauri/src/lib.rs
```

Expected: only the `[Migration] Cleaning up Electron update cache` line in the next block (line ~123-128) — that one is removed in Step 4 below.

### Step 4: Strip the updater-cache cleanup in `lib.rs`

- [ ] **Step 4: Read the current cleanup block**

Run: `sed -n '120,132p' ./src-tauri/src/lib.rs`

Expected current state (line numbers approximate after Step 2's collapse):
```rust
            // Cache app version for bug reports (report_error doesn't have AppHandle)
            commands::set_app_version(app.package_info().version.to_string());

            // One-time cleanup: remove Electron update cache if upgrading
            let electron_updates = std::path::PathBuf::from(std::env::var("HOME").unwrap_or_default())
                .join("Library/Application Support/Plaud V2/updates");
            if electron_updates.exists() {
                log::info!("[Migration] Cleaning up Electron update cache");
                let _ = std::fs::remove_dir_all(&electron_updates);
            }
```

- [ ] **Step 5: Delete the cleanup block**

Use Edit to replace:
```rust
            // Cache app version for bug reports (report_error doesn't have AppHandle)
            commands::set_app_version(app.package_info().version.to_string());

            // One-time cleanup: remove Electron update cache if upgrading
            let electron_updates = std::path::PathBuf::from(std::env::var("HOME").unwrap_or_default())
                .join("Library/Application Support/Plaud V2/updates");
            if electron_updates.exists() {
                log::info!("[Migration] Cleaning up Electron update cache");
                let _ = std::fs::remove_dir_all(&electron_updates);
            }
```

With:
```rust
            // Cache app version for bug reports (report_error doesn't have AppHandle)
            commands::set_app_version(app.package_info().version.to_string());
```

- [ ] **Step 6: Verify all migration references in lib.rs are gone**

Run:
```
grep -n -i "electron\|migration" ./src-tauri/src/lib.rs
```

Expected: no output.

### Step 7: Strip the Electron Python fallback in `python.rs`

- [ ] **Step 7: Read the current `electron_python_dir` and `find_python`**

Run: `sed -n '67,95p' ./src-tauri/src/python.rs`

Expected current state:
```rust
fn bundled_python_dir() -> PathBuf {
    home_dir()
        .unwrap_or_default()
        .join("Library/Application Support/com.plaud.v2/python")
}

/// Electron's bundled Python location (for users upgrading from Electron)
fn electron_python_dir() -> PathBuf {
    home_dir()
        .unwrap_or_default()
        .join("Library/Application Support/Plaud V2/python")
}

/// Find a working Python 3 binary, returns (path, version_string)
fn find_python(full_path: &str) -> Option<(String, String)> {
    // Check both Tauri and Electron bundled Python locations (upgrade compatibility)
    let tauri_bundled = bundled_python_dir().join("bin/python3");
    let electron_bundled = electron_python_dir().join("bin/python3");
    let mut candidates: Vec<String> = vec![];
    if tauri_bundled.exists() {
        candidates.push(tauri_bundled.to_string_lossy().to_string());
    }
    if electron_bundled.exists() {
        candidates.push(electron_bundled.to_string_lossy().to_string());
    }
    candidates.extend(PYTHON_CANDIDATES.iter().map(|s| s.to_string()));
```

- [ ] **Step 8: Delete `electron_python_dir` and the fallback in `find_python`**

Use Edit to replace the block above with:

```rust
fn bundled_python_dir() -> PathBuf {
    home_dir()
        .unwrap_or_default()
        .join("Library/Application Support/com.plaud.v2/python")
}

/// Find a working Python 3 binary, returns (path, version_string)
fn find_python(full_path: &str) -> Option<(String, String)> {
    let tauri_bundled = bundled_python_dir().join("bin/python3");
    let mut candidates: Vec<String> = vec![];
    if tauri_bundled.exists() {
        candidates.push(tauri_bundled.to_string_lossy().to_string());
    }
    candidates.extend(PYTHON_CANDIDATES.iter().map(|s| s.to_string()));
```

(The `electron_python_dir` fn is gone; the comment "Check both Tauri and Electron…" is gone; the `electron_bundled` variable and its `if exists()` push are gone.)

- [ ] **Step 9: Verify `python.rs` has no remaining Electron references**

Run:
```
grep -n -i "electron" ./src-tauri/src/python.rs
```

Expected: no output.

### Step 10: Build verification

- [ ] **Step 10: Verify the Rust crate still compiles**

Run: `cd ./src-tauri && cargo check 2>&1 | tail -10`

Expected: `Finished` with no errors and no new warnings about unused imports / unused functions.

If `cargo check` warns about a now-unused function or variable, the cleanup wasn't complete — re-read the relevant region and remove the unused item.

- [ ] **Step 11: Optional smoke test**

If you have time, run `cd . && cargo tauri dev` for ~15 seconds and confirm the app launches and reaches the main UI. Then quit it.

If `cargo tauri dev` is too slow (it can take a minute or more for cold builds), you can skip this — the migration shims being absent is verified by Step 9, and behavioral verification happens after Phase 3.

### Step 12: Commit

- [ ] **Step 12: Commit**

```
git -C . add src-tauri/src/lib.rs src-tauri/src/python.rs
git -C . commit -m "refactor: remove Electron-era migration shims from Tauri runtime"
```

Verify:
```
git -C . show --stat HEAD
```

Expected: only `src-tauri/src/lib.rs` and `src-tauri/src/python.rs` appear.

---

## Task 3: Phase 3 — Cosmetic comment + narrative-doc updates

**Files:**
- Modify: `./app/api_server.py` (lines 2-6)
- Modify: `./app/renderer.js` (line 767)
- Modify: `./README.md` (Architecture section + Development section)
- Modify: `./docs/ARCHITECTURE.md` (Overview, Packaging, Auto-Updates, Auto Bug Reporter sections)
- Modify: `./docs/RELEASING.md` (whole-document rewrite)

### Step 1: Update `api_server.py` docstring

- [ ] **Step 1: Replace the module docstring**

In `./app/api_server.py`, the current first 6 lines are:

```python
#!/usr/bin/env python3
"""Plaud V2 — Flask API server for the Electron app.

Wraps the existing BLE/crypto/transcription code as HTTP endpoints.
Spawned by Electron main.js as a child process on localhost:5123.
"""
```

Replace those lines with:

```python
#!/usr/bin/env python3
"""Plaud V2 — Flask API server.

Wraps the existing BLE/crypto/transcription code as HTTP endpoints.
Spawned by the Tauri Rust shell as a child process on localhost:5123.
"""
```

### Step 2: Update `renderer.js` comment

- [ ] **Step 2: Replace the line-767 comment**

In `./app/renderer.js`, the current line is:

```javascript
// Init Electron-only features immediately (don't wait for Python API)
```

Replace it with:

```javascript
// Init host-app features immediately (don't wait for Python API)
```

### Step 3: Rewrite `README.md` Architecture and Development sections

- [ ] **Step 3: Replace the Architecture section**

In `./README.md`, find the `## Architecture` heading (around line 109). The current section runs from line 109 down to and including line 137 (just before `See [docs/ARCHITECTURE.md]…`). The current content includes:

```
## Architecture

```
app/
  api_server.py          # Flask API (app factory)
  main.js                # Electron main process + auto-updater
  renderer.js            # Frontend orchestrator
  index.html             # UI markup
  styles.css             # @import hub for CSS modules
  ui/                    # Frontend modules (utils, recordings, transcript, settings)
  routes/                # Flask blueprints (core, device, cloud, notion, recordings, transcribe, settings)
  state/                 # State managers (device, cloud, notion)
  providers/             # AI providers (summarize, transcribe, validate)
  utils/                 # Shared utilities (config, constants, audio, webhooks, system, wifi_sync)
  css/                   # Design system (tokens, components, layouts, recordings, transcript, settings, utilities)
bin/
  ffmpeg                 # Bundled static FFmpeg binary (downloaded by scripts/download-ffmpeg.sh)
src/
  transcribe.py          # Whisper transcription + diarization cascade
  crypto.py              # RSA key management
  scanner.py             # BLE manufacturer data parser
tests/                   # 112 tests (pytest)
docs/
  ARCHITECTURE.md        # Detailed architecture docs
  RELEASING.md           # Release workflow + auto-update system
  reverse-engineering/   # BLE protocol specs, capture analysis, API findings
```
```

Use Edit to replace that whole section with:

```
## Architecture

```
app/
  api_server.py          # Flask API (app factory)
  index.html             # UI markup
  renderer.js            # Frontend orchestrator
  tauri-bridge.js        # window.plaud bridge (Tauri invoke)
  styles.css             # @import hub for CSS modules
  ui/                    # Frontend modules (utils, recordings, transcript, settings, health-gate)
  css/                   # Design system (tokens, components, layouts, recordings, transcript, settings, utilities)
  routes/                # Flask blueprints (core, device, cloud, notion, recordings, transcribe, settings)
  state/                 # State managers (device, cloud, notion)
  providers/             # AI providers (summarize, transcribe, validate)
  utils/                 # Shared utilities (config, constants, audio, webhooks, system, wifi_sync)
src-tauri/
  src/                   # Rust shell (window, IPC commands, Python supervisor, updater, bug reporter)
  tauri.conf.json        # Bundle + window config
  Entitlements.plist     # macOS code-signing entitlements
src/
  transcribe.py          # Whisper transcription + diarization cascade
  crypto.py              # RSA key management
  scanner.py             # BLE manufacturer data parser
tests/                   # pytest suite
docs/
  ARCHITECTURE.md        # Detailed architecture docs
  RELEASING.md           # Release workflow + auto-update system
  TAURI_MIGRATION.md     # Migration record (Electron → Tauri)
  reverse-engineering/   # BLE protocol specs, capture analysis, API findings
```
```

- [ ] **Step 4: Replace the Development section**

In the same file, find the `## Development` heading (around line 155). The current section runs through line 176:

```
## Development

```bash
# Run in dev mode
cd app && pnpm run dev

# Run Python tests
cd .. && python3 -m pytest tests/ -v

# Lint
cd app && pnpm run lint          # eslint + stylelint
cd .. && ruff check app/ src/ tests/   # Python

# Format
cd app && pnpm run format        # prettier + ruff

# Build DMG
cd app && pnpm run build

# Publish a new release (see docs/RELEASING.md)
cd app && pnpm run publish
```
```

Use Edit to replace it with:

```
## Development

```bash
# Run in dev mode (launches Tauri shell + Python backend)
cargo tauri dev

# Run Python tests
python3 -m pytest tests/ -v

# Lint Python
ruff check app/ src/ tests/

# Format Python
ruff format app/ src/ tests/

# Build a signed/notarized DMG (see docs/RELEASING.md for the full release flow)
cargo tauri build
```
```

### Step 5: Rewrite `docs/ARCHITECTURE.md` Overview and Packaging sections

- [ ] **Step 5: Replace the Overview section**

In `./docs/ARCHITECTURE.md`, the current Overview section (lines 1-47) is:

```markdown
# Plaud V2 — Architecture

## Overview

Electron 41 desktop app with a Python Flask backend. The Electron main process spawns Python as a child process on `localhost:5123`. The renderer communicates with the backend via HTTP.

```
┌──────────────────────────────────────────────┐
│  Electron (main.js)                          │
│  - Spawns Python subprocess                  │
│  - Theme sync (light/dark/auto)              │
│  - OAuth URL scheme (plaud-v2://)            │
│  - Tray icon, login items, dock visibility   │
│  - Auto-updates via GitHub Releases API      │
│  - Clean BLE disconnect on quit/reload       │
│  - Code signed + notarized (Developer ID)    │
├──────────────────────────────────────────────┤
│  Renderer (index.html + JS modules)          │
│  - ui/utils.js    → shared state, DOM, toast │
│  - ui/recordings.js → list, search, delete   │
│  - ui/transcript.js → view, player, crop,    │
│                        editable segments      │
│  - ui/settings.js   → all settings tabs      │
│  - renderer.js       → orchestrator + init   │
├──────────────────────────────────────────────┤
│  preload.js (contextBridge)                  │
│  - ~67 API methods exposed as window.plaud   │
└───────────────┬──────────────────────────────┘
                │ HTTP (localhost:5123)
┌───────────────▼──────────────────────────────┐
│  Flask API (api_server.py)                   │
│                                              │
│  routes/          state/         providers/  │
│  ├─ core.py       ├─ device.py   ├─ summarize│
│  ├─ device.py     ├─ cloud.py    ├─ transcribe│
│  ├─ cloud.py      └─ notion.py   └─ validate │
│  ├─ notion.py                                │
│  ├─ recordings.py  utils/                    │
│  ├─ transcribe.py  ├─ config.py              │
│  └─ settings.py    ├─ constants.py           │
│                    ├─ audio.py               │
│                    ├─ webhooks.py             │
│                    ├─ system.py               │
│                    ├─ wifi_sync.py            │
│                    └─ wifi_sync_worker.py     │
└──────────────────────────────────────────────┘
```
```

Replace it with:

```markdown
# Plaud V2 — Architecture

## Overview

Tauri 2 desktop app with a Python Flask backend. The Tauri Rust shell spawns Python as a child process on `localhost:5123`. The renderer is a vanilla-JS frontend served from a WebKit window; it communicates with the backend via HTTP and with the Rust shell via Tauri commands.

```
┌──────────────────────────────────────────────┐
│  Tauri Rust shell (src-tauri/src/lib.rs)     │
│  - Spawns Python subprocess (python.rs)      │
│  - Theme sync (light/dark/auto)              │
│  - OAuth URL scheme (plaud-v2://)            │
│  - Tray icon, login items, dock visibility   │
│  - Auto-updates via GitHub Releases API      │
│  - Bug reporter + diagnostics log            │
│  - Code signed + notarized (Developer ID)    │
├──────────────────────────────────────────────┤
│  Renderer (index.html + JS modules)          │
│  - ui/utils.js       → shared state, DOM     │
│  - ui/recordings.js  → list, search, delete  │
│  - ui/transcript.js  → view, player, crop,   │
│                         editable segments     │
│  - ui/settings.js    → all settings tabs     │
│  - ui/health-gate.js → backend-ready splash  │
│  - renderer.js       → orchestrator + init   │
├──────────────────────────────────────────────┤
│  tauri-bridge.js                             │
│  - window.plaud surface (Tauri invoke + HTTP)│
└───────────────┬──────────────────────────────┘
                │ HTTP (localhost:5123)
┌───────────────▼──────────────────────────────┐
│  Flask API (api_server.py)                   │
│                                              │
│  routes/          state/         providers/  │
│  ├─ core.py       ├─ device.py   ├─ summarize│
│  ├─ device.py     ├─ cloud.py    ├─ transcribe│
│  ├─ cloud.py      └─ notion.py   └─ validate │
│  ├─ notion.py                                │
│  ├─ recordings.py  utils/                    │
│  ├─ transcribe.py  ├─ config.py              │
│  └─ settings.py    ├─ constants.py           │
│                    ├─ audio.py               │
│                    ├─ webhooks.py             │
│                    ├─ system.py               │
│                    ├─ wifi_sync.py            │
│                    └─ wifi_sync_worker.py     │
└──────────────────────────────────────────────┘
```
```

- [ ] **Step 6: Replace the Packaging section**

In the same file, the current Packaging section (lines 49-55) is:

```markdown
## Packaging

- **ASAR**: Enabled. JS/CSS/HTML/node_modules packed into `app.asar`. Python files selectively unpacked via `asarUnpack` so they remain on the real filesystem for the Python subprocess.
- **extraResources**: `src/` (transcription scripts), `keys/` (E2EE RSA keys), `bin/` (bundled FFmpeg).
- **FFmpeg**: Static macOS binary bundled in `bin/`, downloaded by `scripts/download-ffmpeg.sh` before build. Added to PATH so both our code and Whisper find it.
- **Code signing**: Developer ID Application certificate, hardened runtime, entitlements for BLE/network/JIT.
- **Notarization**: Apple notarization via electron-builder. Users can double-click to install without Gatekeeper warnings.
```

Replace it with:

```markdown
## Packaging

- **Tauri bundle**: `cargo tauri build` produces a signed/notarized `.dmg`. Frontend assets are staged via `scripts/prepare-frontend.sh` (called by Tauri's `beforeBuildCommand`) into `dist-tauri/`, which is the `frontendDist` Tauri serves from the bundled webview.
- **Bundled resources** (`tauri.conf.json` `bundle.resources`): `src/` (transcription scripts), `keys/` (E2EE RSA keys), and the Python backend (`app/api_server.py`, `app/routes/`, `app/state/`, `app/providers/`, `app/utils/`).
- **FFmpeg**: Downloaded at first use by `app/utils/system.py` into `~/Library/Application Support/com.plaud.v2/bin/ffmpeg`. Not bundled with the app — keeps the DMG small and avoids licensing complexity.
- **Code signing**: Developer ID Application certificate, hardened runtime, entitlements at `src-tauri/Entitlements.plist` (Bluetooth, network client, user-selected file access).
- **Notarization**: Apple notarization via Tauri's bundler. Users can double-click to install without Gatekeeper warnings.
```

- [ ] **Step 7: Replace the Auto-Updates section**

In the same file, the current Auto-Updates section (around lines 174-181) is:

```markdown
## Auto-Updates

Custom updater using GitHub Releases API with a read-only PAT.

- Checks for updates 5s after launch, then every 4 hours
- Downloads ZIP silently, shows banner when ready
- Install: detached bash script extracts with `ditto`, safe-swaps the `.app` bundle, relaunches
- Manual check available in Settings > General > About
```

Replace it with:

```markdown
## Auto-Updates

Custom updater in the Rust shell (`src-tauri/src/commands/mod.rs`) using GitHub Releases API with a read-only PAT (`update_token()` at `commands/mod.rs:201`).

- Checks for updates 5 s after launch, then every 4 hours (loop in `lib.rs`).
- Downloads the DMG silently, shows banner when ready.
- Install: detached script mounts the DMG, copies the new `.app` over the old one, relaunches.
- Manual check available in **Settings > General > About**.
```

- [ ] **Step 8: Replace the Auto Bug Reporter section**

In the same file, the current Auto Bug Reporter section (around lines 183-185) is:

```markdown
## Auto Bug Reporter

Unhandled errors are automatically reported as GitHub Issues (tagged `auto-bug`). Deduplicates by error fingerprint — same error creates a comment on the existing issue instead of a new one. Catches both main process (`uncaughtException`) and renderer (`window.onerror`) errors.
```

Replace it with:

```markdown
## Auto Bug Reporter

Unhandled errors are automatically reported as GitHub Issues (tagged `auto-bug`). Deduplicates by error fingerprint — same error creates a comment on the existing issue instead of a new one. Catches Rust panics (`panic::set_hook`), Python uncaught exceptions (relayed via the `report_error` Tauri command from the renderer), and renderer errors (`window.onerror` / unhandled rejections).

Two filters keep noise low:

- `AbortError` (timeouts, navigation cancellations) → suppressed from GitHub, written to a local diagnostics log at `~/Library/Application Support/com.plaud.v2/diagnostics.log`.
- Backend-down state (the health-gate poller flips this) → suppressed from GitHub while the Python server is unreachable.

The diagnostics log rotates at 1 MB to `.log.1` (≈ 2 MB ceiling). Open it from the splash's "Backend not responding" screen via the **Open diagnostics log** button.
```

### Step 9: Rewrite `docs/RELEASING.md` end to end

- [ ] **Step 9: Replace `docs/RELEASING.md` with the Tauri release flow**

Replace the **entire contents** of `./docs/RELEASING.md` with:

````markdown
# Plaud V2 — Release & Auto-Update Guide

## How Auto-Updates Work

The app uses a custom updater in the Rust shell (`src-tauri/src/commands/mod.rs`) that checks GitHub Releases directly via a read-only PAT.

### Update flow (from the user's perspective)

1. App launches, waits 5 seconds, then checks the GitHub Releases API for a newer version.
2. If found, the DMG is downloaded silently in the background.
3. A blue banner appears at the top: **"v1.2.0 ready to install" [Restart to Update] [x]**.
4. User clicks "Restart to Update" — app quits, the new `.app` is copied into place, and the app reopens.
5. App re-checks every 4 hours while running.
6. Users can manually check via **Settings > General > About > Check for Updates**.

### What gets published to GitHub Releases

| File | Purpose |
|------|---------|
| `Plaud-V2_{version}_aarch64.dmg` | First-time installs (drag to Applications) and auto-update payload |

The Tauri bundler produces the DMG; the updater downloads and mounts it directly. There is no separate ZIP / blockmap.

### How the install works (under the hood)

When the user clicks "Restart to Update":

1. A detached helper is spawned by the Rust shell.
2. App quits.
3. Helper mounts the downloaded DMG, copies `Plaud V2.app` over the existing `/Applications/Plaud V2.app` (preserving signing), unmounts the DMG.
4. Strips macOS quarantine with `xattr -dr com.apple.quarantine`.
5. Relaunches the app.

Because the app is signed with a Developer ID certificate, macOS preserves Bluetooth/location permissions across updates.

---

## Creating a Release

### Prerequisites

- `.env` at project root contains `GH_TOKEN`, `APPLE_TEAM_ID`, `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`.
- Apple Developer ID Application certificate installed in Keychain.
- Rust toolchain installed (`rustup`).
- Tauri CLI installed (`cargo install tauri-cli`).

### Steps

```bash
# 1. Bump the version in src-tauri/tauri.conf.json (the "version" field).
#    Follow semver: major.minor.patch.

# 2. Commit the version bump
git add src-tauri/tauri.conf.json
git commit -m "Bump v{version}"
git push

# 3. Build, sign, notarize
cargo tauri build

# The signed/notarized DMG ends up at:
#   src-tauri/target/release/bundle/dmg/Plaud V2_{version}_aarch64.dmg

# 4. Upload to GitHub Releases
gh release create v{version} \
  "src-tauri/target/release/bundle/dmg/Plaud V2_{version}_aarch64.dmg" \
  --title "v{version}" \
  --notes "Release notes here"
```

The build:

1. Runs `scripts/prepare-frontend.sh` (Tauri `beforeBuildCommand`) — stages frontend assets into `dist-tauri/`.
2. Compiles the Rust shell.
3. Bundles the resources from `tauri.conf.json` (`src/`, `keys/`, `app/api_server.py`, `app/routes/`, etc.).
4. Code-signs with the Developer ID certificate + hardened runtime.
5. Submits to Apple for notarization (typically 2–5 minutes).
6. Builds the DMG.

Every running instance picks up the update within 4 hours (or immediately if the user clicks **Check for Updates**).

---

## Code Signing & Notarization

The app is signed with a Developer ID Application certificate and notarized with Apple.

### What this means for users

- **Double-click to install** — no Gatekeeper warnings, no right-click > Open needed.
- **Permissions persist** — Bluetooth, location, and other permissions survive updates because the signing identity is consistent.
- **Trusted by macOS** — Apple has scanned and approved the binary.

### Configuration

Code-signing identity, notarization team ID, and entitlements are configured in:

- `src-tauri/tauri.conf.json` → `bundle.macOS` (signing identity, notarization Apple ID)
- `src-tauri/Entitlements.plist` (entitlements)
- `src-tauri/Info.plist` (bundle metadata)

### Entitlements (`src-tauri/Entitlements.plist`)

| Entitlement | Reason |
|-------------|--------|
| `device.bluetooth` | BLE communication with Plaud device |
| `network.client` | HTTP API calls, cloud sync |
| `files.user-selected.read-write` | Audio export to Downloads |

(Tauri does not need the JIT/unsigned-memory entitlements that Electron required.)

### Apple credentials (in `.env`)

```
APPLE_TEAM_ID=<your-team-id>
APPLE_ID=<your-apple-id-email>
APPLE_APP_SPECIFIC_PASSWORD=<app-specific-password>
```

Generate an app-specific password at [appleid.apple.com](https://appleid.apple.com) > Sign-In and Security > App-Specific Passwords.

### If notarization is slow or failing

- First submission to a new Apple Developer account can take hours.
- Subsequent submissions typically take 2–5 minutes.
- If Apple's timestamp servers are down, codesign will fail — retry later.

---

## GitHub Tokens

The repo is **private**, so two fine-grained PATs are used:

### Token 1 — `plaud-v2-publish` (your machine only)

- **Scope:** `Obednal97/Plaud-V2` — Contents: Read and write
- **Used for:** creating releases and uploading build artifacts (`gh release create`)
- **Lives:** in `.env` at project root (gitignored). Never in code.

### Token 2 — `plaud-v2-updater-readonly` (ships with the app)

- **Scope:** `Obednal97/Plaud-V2` — Contents: Read-only, Issues: Read and write
- **Used for:** auto-update checks from the private repo + auto-bug-reporting to GitHub Issues
- **Lives:** in the Rust source at `src-tauri/src/commands/mod.rs:201` (`update_token()` function), obfuscated at compile time
- **Risk:** minimal — read-only for contents, and anyone with the app is a trusted friend

### If a token is compromised

1. Go to GitHub > Settings > Developer settings > Fine-grained tokens.
2. Revoke the compromised token.
3. Generate a new one with the same permissions.
4. For the publish token: update `.env`.
5. For the updater token: update `update_token()` in `src-tauri/src/commands/mod.rs` and publish a new release.

---

## First-Time Install for Friends

1. Go to the [latest GitHub Release](https://github.com/Obednal97/Plaud-V2/releases/latest).
2. Download `Plaud V2_{version}_aarch64.dmg`.
3. Open the DMG, drag Plaud V2 to Applications.
4. Double-click to open — it just works (signed + notarized).
5. Grant Bluetooth permission when prompted (one time only).
6. All future updates happen automatically through the app.

---

## Auto Bug Reporter

Unhandled errors are automatically reported as GitHub Issues.

- Searches existing issues by error fingerprint to avoid duplicates.
- Creates new issues with the `auto-bug` label.
- Comments on existing open issues if the same error recurs.
- Includes: app version, macOS version, architecture, Python environment, stack trace, error context.
- Filters out `AbortError` (timeouts / navigation) and any error fired while the backend health is `down` — these go to the local diagnostics log only.
- Local diagnostics log: `~/Library/Application Support/com.plaud.v2/diagnostics.log` (rotates at 1 MB to `.log.1`).
- Never crashes the app — all reporting is fire-and-forget.

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Update check fails | Token expired or revoked | Regenerate `plaud-v2-updater-readonly` PAT, update in `src-tauri/src/commands/mod.rs`, publish new release |
| "Restart to Update" doesn't work | App Management permission on macOS Ventura+ | User may need to approve in System Settings > Privacy > App Management |
| Notarization fails | Apple timestamp server down or credentials wrong | Retry later, or check `APPLE_ID` / `APPLE_APP_SPECIFIC_PASSWORD` in `.env` |
| Codesign fails repeatedly | Apple timestamp server rate limiting | Wait a few minutes and retry |
| Python server won't start from Dock | PATH doesn't include Homebrew | Fixed — the Rust shell adds common Python paths to PATH before spawning |
| `cargo tauri build` fails | Missing env vars or Rust toolchain | Check `.env` has GH_TOKEN + Apple credentials, and `rustup show` lists a stable toolchain |
| FFmpeg not found at runtime | First-launch download failed | App auto-retries on next launch; users can `brew install ffmpeg` as a manual fallback (`utils/system.py` checks `shutil.which("ffmpeg")` first) |
````

### Step 10: Verify everything still parses / builds / matches

- [ ] **Step 10: Verify Python parses**

Run: `python3 -m py_compile ./app/api_server.py`

Expected: no output, exit 0.

- [ ] **Step 11: Verify renderer.js parses**

Run: `node -c ./app/renderer.js`

Expected: no output, exit 0.

- [ ] **Step 12: Verify Rust still compiles**

Run: `cd ./src-tauri && cargo check 2>&1 | tail -5`

Expected: `Finished` with no errors.

- [ ] **Step 13: Verify the diagnostics path string in ARCHITECTURE.md matches the code**

The ARCHITECTURE rewrite refers to `~/Library/Application Support/com.plaud.v2/diagnostics.log`. Cross-check against the Rust source:

Run: `grep -n "diagnostics.log" ./src-tauri/src/commands/mod.rs`

Expected: a line including `"Library/Application Support/com.plaud.v2/diagnostics.log"` — confirms the doc and code agree.

- [ ] **Step 14: Verify intentionally-kept Electron references are present, others gone**

Run:
```
git -C . grep -i electron -- ':!docs/superpowers/' ':!docs/TAURI_MIGRATION.md' ':!docs/reverse-engineering/'
```

Expected: matches ONLY in:
- `app/tauri-bridge.js` (lines 2, 8, 126, 248 — historical-context comments about preload)
- `src-tauri/src/commands/mod.rs` (algorithm-parity comments around lines 280, 292, 565, 577, 816, 892)

Run separately:
```
git -C . grep -i electron -- 'docs/' ':!docs/TAURI_MIGRATION.md' ':!docs/reverse-engineering/' ':!docs/superpowers/'
```

Expected: NO output. (No Electron references in README.md, ARCHITECTURE.md, or RELEASING.md.)

If either grep returns unexpected results, re-read the offending file and clean up before committing.

### Step 15: Commit

- [ ] **Step 15: Commit Phase 3**

```
git -C . add app/api_server.py app/renderer.js README.md docs/ARCHITECTURE.md docs/RELEASING.md
git -C . commit -m "docs: drop Electron references from inline comments and narrative docs"
```

Verify:
```
git -C . show --stat HEAD
```

Expected: only the five files listed above.

---

## Final verification

- [ ] **Step 1: Inspect the three commits as a whole**

Run:
```
git -C . log --oneline e369835..HEAD
git -C . diff --stat e369835..HEAD | tail -20
```

Expected: three new commits, each on master:
1. `chore: remove Electron app shell and build assets` (~12 deletions)
2. `refactor: remove Electron-era migration shims from Tauri runtime` (2 files modified)
3. `docs: drop Electron references from inline comments and narrative docs` (5 files modified)

- [ ] **Step 2: Confirm Tauri build is fully self-contained**

Run:
```
cd . && bash scripts/prepare-frontend.sh && cd src-tauri && cargo check 2>&1 | tail -5
```

Expected: prepare-frontend.sh completes; `cargo check` finishes with `Finished` and no errors.

- [ ] **Step 3: Confirm working tree is clean**

Run: `git -C . status`

Expected: same pre-existing untracked files as at the start (`scripts/depair_*.py`, etc.). No new dirty files in `app/`, `src-tauri/`, or `docs/`.

- [ ] **Step 4: Optional end-to-end smoke**

Run: `cd . && cargo tauri dev`

Expected: app launches, splash visible briefly, main UI mounts. Quit with Cmd+Q.

If skipped, note in the report so the maintainer knows to do it before tagging a release.

---

## Self-Review

After writing this plan, comparing against the spec:

**1. Spec coverage:**

| Spec section | Implementing task |
|---|---|
| Phase 1 — Delete Electron app shell + build assets | Task 1 (Steps 1–9) |
| Phase 2 — Strip Tauri-side migration shims | Task 2 (Steps 1–12) |
| Phase 3 — Cosmetic comment + narrative-doc updates | Task 3 (Steps 1–15) |
| Verification per phase | Built into each task; Final verification step covers cross-cutting |
| Risks (hidden cross-references, prepare-frontend.sh, tauri.conf.json paths) | Task 1 Steps 5–7 verify all preserved paths; Task 2 Step 10 catches stale unused-fn warnings |
| Out of scope (don't touch session bug-reporter code, ffmpeg handling, etc.) | None of the tasks touches those areas |
| Followups (post-release Linear note) | Out of scope for the plan; logged in spec |

All spec sections have an owning task.

**2. Placeholder scan:**

No "TBD" / "TODO" / "implement later" content. Each rewrite section provides exact target text. The two grep verifications in Step 14 use fully-qualified paths.

**3. Type / name consistency:**

- Diagnostics path: spec, plan, ARCHITECTURE.md rewrite, and Rust code all agree on `~/Library/Application Support/com.plaud.v2/diagnostics.log`.
- Updater token location: plan and RELEASING.md rewrite both point at `src-tauri/src/commands/mod.rs:201` (`update_token()`).
- File paths in deletions match exactly the file structure shown at the top.
- Commit messages: one per phase, exactly as quoted.

No inconsistencies found.
