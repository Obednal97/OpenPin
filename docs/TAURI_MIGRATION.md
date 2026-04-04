# Plaud V2 — Electron to Tauri Migration Plan

## Executive Summary

Migrate Plaud V2 from Electron 41 to Tauri v2, reducing the distributed app bundle from **~137 MB** to an estimated **~15-30 MB** (depending on FFmpeg strategy). The core win: Tauri uses the system's native WebView (WebKit on macOS) instead of bundling Chromium + Node.js (~283 MB in node_modules, ~120 MB compressed in the DMG).

### Current Size Breakdown

| Component | Size (uncompressed) | In DMG (~compressed) |
|-----------|---------------------|----------------------|
| Electron framework | 283 MB | ~120 MB |
| FFmpeg static binary | 77 MB | ~25 MB |
| Python source code | ~300 KB | ~100 KB |
| JS/CSS/HTML frontend | ~200 KB | ~80 KB |
| Keys, assets, icons | ~50 KB | ~50 KB |
| **Total DMG** | | **~137 MB** |

### Projected Tauri Size

| Component | Size (in DMG) |
|-----------|---------------|
| Tauri runtime (WebKit = system, Rust binary) | ~3-8 MB |
| FFmpeg static binary | ~25 MB (or 0 if we use ffmpeg-next crate) |
| Python sidecar (Phase 1) | ~0 MB (uses system Python) |
| JS/CSS/HTML frontend | ~80 KB |
| Keys, assets, icons | ~50 KB |
| **Phase 1 estimate** | **~28-33 MB** (with bundled FFmpeg) |
| **Phase 2 estimate (Rust backend)** | **~8-15 MB** (FFmpeg linked statically via Rust) |

---

## Why Tauri

| Aspect | Electron (current) | Tauri v2 |
|--------|--------------------|----|
| Bundle size | ~137 MB DMG | ~15-30 MB DMG |
| Memory usage | ~150-300 MB (Chromium) | ~30-80 MB (system WebView) |
| WebView | Bundled Chromium | System WebKit (macOS) |
| Backend language | Node.js (+ Python sidecar) | Rust (+ optional sidecar) |
| Code signing | Works, but large binary | Works, smaller binary = faster notarization |
| Security | Node.js process model | Rust + capability-based permissions |
| IPC | contextBridge + ipcMain | `#[tauri::command]` + events |
| Auto-updates | Custom (GitHub API + bash) | `tauri-plugin-updater` (built-in) |
| macOS support | Full | Full (WebKit is native) |

### Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| WebKit vs Chromium rendering differences | CSS/JS might behave slightly differently | Our CSS is already WebKit-compatible (tested in analysis). Vanilla JS, no framework quirks. |
| BLE from Rust is less mature than Python bleak | Could hit edge cases in btleplug | Phase 1 keeps Python bleak. Only move BLE to Rust in Phase 2 after thorough testing. |
| Python sidecar still requires Python on user's machine | Same first-run dependency as now | Phase 1 is identical UX. Phase 2 eliminates Python entirely. |
| Tauri v2 macOS-specific features (dock, tray) | API differences from Electron | Well-documented, plugins exist. See compatibility matrix below. |
| Custom auto-updater needs rewriting | Current bash-based updater is fragile | Tauri has a built-in updater plugin — this is actually an improvement. |
| Hardened runtime / notarization | Must still work | Tauri supports Apple code signing and notarization natively via `tauri-cli`. |

---

## Architecture Comparison

### Current: Electron + Python

```
┌──────────────────────────────────────┐
│  Electron Main Process (Node.js)     │   ← 283 MB
│  - Window management, tray, theme    │
│  - IPC bridge (contextBridge)        │
│  - Custom auto-updater               │
│  - Python process management         │
├──────────────────────────────────────┤
│  Chromium Renderer (bundled)         │   ← included in 283 MB
│  - index.html + renderer.js          │
│  - Vanilla JS, no framework          │
├──────────────────────────────────────┤
│  Python Flask Server (sidecar)       │   ← system Python
│  - 47 REST routes on localhost:5123  │
│  - BLE (bleak), crypto, WiFi, AI    │
│  - 6,470 lines of Python            │
└──────────────────────────────────────┘
```

### Target: Tauri + Python sidecar (Phase 1)

```
┌──────────────────────────────────────┐
│  Tauri Core (Rust binary)            │   ← ~3-8 MB
│  - Window management (WRY/WebKit)    │
│  - Tauri commands (IPC)              │
│  - Plugin: updater, tray, deeplink   │
│  - Python process management         │
├──────────────────────────────────────┤
│  System WebKit (macOS native)        │   ← 0 MB (already on system)
│  - index.html + renderer.js          │
│  - Vanilla JS, no framework          │
├──────────────────────────────────────┤
│  Python Flask Server (sidecar)       │   ← system Python (unchanged)
│  - 47 REST routes on localhost:5123  │
│  - BLE, crypto, WiFi, AI            │
│  - Identical Python code             │
└──────────────────────────────────────┘
```

### Target: Tauri + Rust backend (Phase 2, future)

```
┌──────────────────────────────────────┐
│  Tauri Core (Rust binary)            │   ← ~8-15 MB (includes all backend)
│  - Window management (WRY/WebKit)    │
│  - All backend logic in Rust         │
│  - BLE (btleplug), crypto (RustCrypto)│
│  - WiFi (objc2), AI (reqwest HTTP)   │
│  - FFmpeg (ffmpeg-next or CLI)       │
├──────────────────────────────────────┤
│  System WebKit (macOS native)        │   ← 0 MB
│  - index.html + renderer.js          │
│  - Vanilla JS, no framework          │
│  - Calls Rust via invoke()           │
└──────────────────────────────────────┘
```

---

## Compatibility Matrix: Electron APIs to Tauri

### IPC / Bridge (18 channels)

| Electron API | Current Usage | Tauri v2 Equivalent | Effort |
|-------------|---------------|---------------------|--------|
| `contextBridge.exposeInMainWorld` | `window.plaud.*` namespace | `@tauri-apps/api/core` `invoke()` | Low — replace `window.plaud.foo()` with `invoke('foo')` |
| `ipcMain.handle` / `ipcRenderer.invoke` | 18 channels | `#[tauri::command]` functions | Low — 1:1 mapping |
| `webContents.send` | Events to renderer | `app.emit_to()` / `window.emit()` | Low — use `listen()` in frontend |

### Window & System

| Electron API | Current Usage | Tauri v2 Equivalent | Effort |
|-------------|---------------|---------------------|--------|
| `BrowserWindow` | Main window, `titleBarStyle: 'hiddenInset'` | `tauri.conf.json` window config + `decorations: false` or macOS `titleBarStyle` | Low |
| `trafficLightPosition` | `{ x: 16, y: 18 }` | Tauri macOS window config supports this | Low |
| `nativeTheme` | Dark/light/auto cycling | `tauri::window::Theme` + CSS `prefers-color-scheme` | Low |
| `app.dock.show/hide` | Dock visibility toggle | `cocoa` crate via Rust (`NSApplication.setActivationPolicy`) | Medium |
| `app.getLoginItemSettings/setLoginItemSettings` | Launch at login | `tauri-plugin-autostart` | Low (drop-in plugin) |
| `app.requestSingleInstanceLock` | Single instance | `tauri-plugin-single-instance` | Low (drop-in plugin) |
| `Tray` + `Menu` | System tray with Show/Sync/Quit | Built-in `tauri::tray::TrayIconBuilder` | Low |
| `shell.openExternal` | Open URLs in browser | `tauri-plugin-opener` | Low (drop-in plugin) |
| `powerMonitor.on('resume')` | Detect system wake | Custom Rust: `NSWorkspace.notificationCenter` via `objc2` | Medium |
| `Notification` | Native notifications | `tauri-plugin-notification` | Low (drop-in plugin) |

### Custom Features

| Electron Feature | Current Implementation | Tauri v2 Approach | Effort |
|-----------------|------------------------|-------------------|--------|
| Custom auto-updater | GitHub API + ZIP download + bash script | `tauri-plugin-updater` (supports GitHub releases natively) | Medium — simpler than current, but needs testing |
| Custom protocol `plaud-v2://` | `app.setAsDefaultProtocolClient` | `tauri-plugin-deep-link` | Low |
| Python sidecar management | `spawn()` in main.js, health check loop | `tauri::api::process::Command::new_sidecar()` or `tauri-plugin-shell` | Medium |
| Bug reporter (GitHub Issues) | HTTP from Node.js main process | `reqwest` from Rust command | Low |
| Close-to-hide behaviour | `win.on('close', e => { e.preventDefault(); win.hide() })` | `on_window_event(WindowEvent::CloseRequested)` | Low |

### Frontend (CSS)

| Current | Change Needed | Effort |
|---------|--------------|--------|
| `-webkit-app-region: drag` | Replace with `data-tauri-drag-region` attribute | Low — find & replace in HTML/CSS |
| `-webkit-backdrop-filter` | Works as-is (WebKit) | None |
| `-webkit-scrollbar` styles | Works as-is (WebKit) | None |
| `-electron-corner-smoothing` | Remove (Electron-only, decorative) | None — Tauri macOS windows have native rounded corners |
| CSP `connect-src 127.0.0.1:*` | Update to include `ipc://` and `tauri://` schemes | Low |
| Google Fonts (external CDN) | Must be allowed in Tauri CSP or bundled locally | Low |

---

## What Stays, What Changes, What Gets Dropped

### Stays Unchanged

| Component | Reason |
|-----------|--------|
| All Python backend code (6,470 lines) | Runs as sidecar process, identical to current |
| Frontend HTML/CSS/JS (4,904 lines) | Vanilla JS works in any WebView |
| Flask REST API (47 routes) | Frontend still calls via `fetch()` |
| BLE logic (bleak) | Python sidecar, no change |
| Crypto (RSA + ChaCha20) | Python sidecar, no change |
| WiFi sync (CoreWLAN) | Python sidecar, no change |
| AI providers (Anthropic/OpenAI/local) | Python sidecar, no change |
| Transcription (Whisper) | Python sidecar, no change |
| Audio decryption | Python sidecar, no change |
| Config files (JSON) | Same paths, same format |
| FFmpeg binary | Same static binary in resources |
| RSA keys | Same key files in resources |

### Changes (must rewrite)

| Component | Lines | What Changes |
|-----------|-------|-------------|
| `main.js` (Electron main process) | 1,167 | Rewritten as Rust `src-tauri/src/main.rs` + command modules |
| `preload.js` (contextBridge) | 167 | Replaced by Tauri `invoke()` — frontend calls Rust directly |
| `renderer.js` IPC calls | ~50 lines | `window.plaud.foo()` → `invoke('foo', { args })` |
| `ui/*.js` IPC calls | ~20 lines | Same `window.plaud.foo()` → `invoke('foo')` pattern |
| `package.json` build config | N/A | Replaced by `tauri.conf.json` + `Cargo.toml` |
| Entitlements plist | 6 entries | Equivalent in Tauri's `info.plist` config |
| Auto-updater | ~170 lines JS | Replaced by `tauri-plugin-updater` |
| Code signing config | electron-builder | `tauri-cli` handles signing + notarization |

### Gets Dropped

| Component | Reason |
|-----------|--------|
| Electron framework (283 MB) | Replaced by Tauri (~5 MB) |
| electron-builder | Replaced by `tauri-cli` |
| `node_modules/` (609 MB) | Only dev deps remain (eslint, prettier, stylelint) |
| `preload.js` | Tauri doesn't need a preload bridge |
| Chromium | System WebKit used instead |

---

## Execution Plan

### Phase 0: Preparation (before writing any Tauri code)

**Goal:** Set up the Tauri project structure alongside the existing Electron app, verify toolchain works.

#### Tasks

- [ ] **0.1** Install Rust toolchain (`rustup`) and verify `cargo` works
- [ ] **0.2** Install Tauri CLI: `cargo install tauri-cli` (or `pnpm add -D @tauri-apps/cli`)
- [ ] **0.3** Run `cargo tauri init` in the project root to scaffold `src-tauri/` directory
- [ ] **0.4** Configure `tauri.conf.json`:
  - `devUrl`: point to a local dev server or `index.html`
  - `frontendDist`: `../app` (our existing frontend)
  - Window: `titleBarStyle: "hiddenInset"`, `width: 1200`, `height: 800`, `minWidth: 800`, `minHeight: 600`
  - `identifier`: `com.plaud.v2`
  - `bundle.macOS.signingIdentity`: `"Oliver Bednal (4SN93PWF7Z)"`
  - `bundle.macOS.entitlements`: (BLE, network, files)
- [ ] **0.5** Verify `cargo tauri dev` opens a window with the existing `index.html`
- [ ] **0.6** Verify `cargo tauri build` produces a `.dmg` and check its size
- [ ] **0.7** Set up `.cargo/config.toml` for macOS arm64 target
- [ ] **0.8** Add `src-tauri/` to the project structure, update `.gitignore` for Rust build artifacts (`target/`)

**Exit criteria:** `cargo tauri dev` opens a window showing `index.html`. No backend functionality yet.

**Estimated effort:** 1-2 hours

---

### Phase 1: Core Shell (Tauri window + Python sidecar)

**Goal:** Replace Electron's main process with Tauri while keeping the Python backend identical. The app should be fully functional at the end of this phase.

#### 1A: Python Sidecar Management

The most critical piece — Tauri must spawn and manage the Python Flask server exactly as Electron does today.

- [ ] **1A.1** Create `src-tauri/src/python.rs` module:
  - Find Python 3 binary (check system paths: `/usr/bin/python3`, `/usr/local/bin/python3`, Homebrew paths)
  - Find a free port (start at 5123, increment if busy)
  - Spawn `python3 api_server.py --port {port}` as a child process
  - Capture stdout/stderr and log to Tauri's log system
  - Health check loop: poll `http://127.0.0.1:{port}/health` until 200
  - On app quit: send SIGTERM, wait 2s, then SIGKILL
  - Emit `startup-status` events to frontend during startup
  - Emit `startup-error` if Python fails to start

- [ ] **1A.2** Expose the API port to the frontend:
  - Tauri command `get_api_info` returns `{ port, url }` so frontend knows where to `fetch()`
  - Alternative: set `window.__PLAUD_API_URL` via Tauri's initialization script

- [ ] **1A.3** Handle Python dependency installation:
  - Port `ensurePythonDeps()` from `main.js` to Rust
  - Run `pip install --user flask requests bleak cryptography` before starting the server
  - Emit progress events to frontend

- [ ] **1A.4** Handle edge case: no Python installed
  - Port the "download python-build-standalone" logic from `main.js`
  - Download standalone Python to app data directory
  - Use it as the sidecar binary

**Considerations:**
- The current Electron app has ~350 lines of Python management code in `main.js` (lines 434-789). This needs careful porting.
- Tauri's `Command::new_sidecar()` is designed for this but expects the sidecar in the bundle. Since we use system Python, we'll use `std::process::Command` directly.
- Must handle the case where Python is in a non-standard location (e.g., pyenv, conda).

**Exit criteria:** `cargo tauri dev` starts the Python server, frontend can `fetch()` to it, BLE scan works.

**Estimated effort:** 4-6 hours

---

#### 1B: IPC Bridge (replace preload.js)

Replace the Electron contextBridge with Tauri's `invoke()` system.

- [ ] **1B.1** Create a frontend wrapper `app/tauri-bridge.js`:
  ```js
  // Drop-in replacement for preload.js's window.plaud
  // Most methods just call fetch() to the Python backend (unchanged)
  // IPC methods call Tauri invoke() instead
  const { invoke } = window.__TAURI__.core;
  const { listen } = window.__TAURI__.event;

  window.plaud = {
    // HTTP proxy calls — identical to current, just need the port
    scan: () => apiCall('/device/scan', 'POST'),
    // ... all 40+ HTTP methods stay as fetch()

    // IPC calls — now go to Rust instead of Electron
    cycleTheme: () => invoke('cycle_theme'),
    getTheme: () => invoke('get_theme'),
    getLoginItem: () => invoke('get_login_item'),
    setLoginItem: (enabled) => invoke('set_login_item', { enabled }),
    // ... all 18 IPC methods
  };
  ```

- [ ] **1B.2** Strategy decision: **keep `window.plaud` interface identical**
  - The frontend code (renderer.js, ui/*.js) calls `window.plaud.scan()`, `window.plaud.getTheme()`, etc.
  - By making `tauri-bridge.js` expose the exact same `window.plaud` namespace, we don't need to change any frontend code
  - This is a **drop-in replacement** for `preload.js`

- [ ] **1B.3** Create Rust commands for all 18 IPC channels:
  - `src-tauri/src/commands/theme.rs` — cycle_theme, get_theme
  - `src-tauri/src/commands/system.rs` — get_login_item, set_login_item, get_dock_visible, set_dock_visible, get_app_version
  - `src-tauri/src/commands/updater.rs` — check_for_updates, install_update
  - `src-tauri/src/commands/reporting.rs` — report_error, report_bug
  - `src-tauri/src/commands/shell.rs` — open_external

- [ ] **1B.4** Set up Tauri event listeners for push events:
  - `startup-status`, `startup-error` — emitted during Python boot
  - `update-status` — emitted during update checks
  - `system-wake` — emitted on macOS resume
  - `oauth-callback` — emitted when deep link received
  - `notification-action` — emitted when notification clicked

- [ ] **1B.5** Update `index.html`:
  - Remove `<script src="preload.js">` (doesn't exist in Tauri context)
  - Add `<script src="tauri-bridge.js">` before `renderer.js`
  - Update CSP to allow `ipc://localhost`, `tauri://localhost`, and `asset://localhost`

**Considerations:**
- The frontend never imports Electron directly — it only uses `window.plaud.*`. This means we can swap the implementation behind `window.plaud` without touching any frontend code.
- Tauri v2's `invoke()` is async and returns JSON, matching our current pattern.
- Events use `listen('event-name', callback)` which maps cleanly to our current `window.plaud.onUpdateStatus(cb)` pattern.

**Exit criteria:** All `window.plaud.*` methods work. Frontend code unchanged.

**Estimated effort:** 3-4 hours

---

#### 1C: Tauri Plugins (replace Electron features)

- [ ] **1C.1** System tray:
  ```rust
  // Built-in Tauri v2 tray
  TrayIconBuilder::new()
    .tooltip("Plaud V2")
    .icon(app.default_window_icon().unwrap().clone())
    .menu(&menu)  // Show, Sync Device, Quit
    .on_menu_event(|app, event| { ... })
    .on_tray_icon_event(|tray, event| { ... })
    .build(app)?;
  ```

- [ ] **1C.2** Auto-updater — `tauri-plugin-updater`:
  - Configure to check GitHub Releases API (same repo, same PAT)
  - Tauri's updater supports private repos with auth headers
  - Replaces 170 lines of custom JS updater code
  - Must publish Tauri-format update bundles (`.tar.gz` + `latest.json`)

- [ ] **1C.3** Deep linking — `tauri-plugin-deep-link`:
  - Register `plaud-v2://` scheme
  - On callback, parse auth code and emit `oauth-callback` event to frontend

- [ ] **1C.4** Auto-start — `tauri-plugin-autostart`:
  - Replace `app.getLoginItemSettings()` / `app.setLoginItemSettings()`

- [ ] **1C.5** Single instance — `tauri-plugin-single-instance`:
  - Replace `app.requestSingleInstanceLock()`

- [ ] **1C.6** Notifications — `tauri-plugin-notification`:
  - Replace `new Notification({ title, body })`
  - Handle notification click actions

- [ ] **1C.7** Opener — `tauri-plugin-opener`:
  - Replace `shell.openExternal(url)`

- [ ] **1C.8** Theme detection:
  - Use `tauri::window::Theme` API or let CSS `prefers-color-scheme` handle it
  - The current theme cycling (light → dark → auto) needs a Tauri command that reads/sets the window theme
  - Store user preference in config, apply on startup

- [ ] **1C.9** Dock visibility (macOS-specific):
  - Use `cocoa` crate to call `NSApplication.setActivationPolicy(.regular)` (show dock) or `.accessory` (hide dock)
  - This is ~10 lines of Rust unsafe code with `objc2`

- [ ] **1C.10** System wake detection:
  - Subscribe to `NSWorkspaceDidWakeNotification` via `objc2`
  - Emit `system-wake` event to frontend
  - ~20 lines of Rust

**Considerations:**
- Tauri's updater uses a different format than electron-builder. The release pipeline will need updating (Phase 1D).
- The current custom updater downloads a ZIP, extracts, and swaps the `.app` bundle. Tauri's updater does this natively and more safely.
- Some plugins (deep-link, notification) require Info.plist entries which Tauri handles via `tauri.conf.json`.

**Exit criteria:** Tray works, theme cycling works, deep links work, notifications work, auto-start works.

**Estimated effort:** 4-6 hours

---

#### 1D: Build & Release Pipeline

- [ ] **1D.1** Configure `tauri.conf.json` for macOS builds:
  ```json
  {
    "bundle": {
      "active": true,
      "targets": ["dmg", "updater"],
      "macOS": {
        "signingIdentity": "Oliver Bednal (4SN93PWF7Z)",
        "entitlements": "Entitlements.plist",
        "providerShortName": "4SN93PWF7Z"
      },
      "resources": [
        "../src/*",
        "../keys/*",
        "../bin/ffmpeg"
      ]
    }
  }
  ```

- [ ] **1D.2** Create `src-tauri/Entitlements.plist`:
  - `com.apple.security.device.bluetooth` — BLE
  - `com.apple.security.network.client` — HTTP
  - `com.apple.security.files.user-selected.read-write` — file access
  - May not need JIT/unsigned-memory entitlements (those were Chromium requirements)

- [ ] **1D.3** Update `package.json` scripts:
  ```json
  {
    "scripts": {
      "tauri:dev": "cargo tauri dev",
      "tauri:build": "cargo tauri build",
      "tauri:publish": "export $(cat ../.env | grep -v '^#' | xargs) && cargo tauri build --bundles dmg,updater"
    }
  }
  ```

- [ ] **1D.4** Configure auto-updater endpoints:
  - Tauri updater checks a JSON endpoint for new versions
  - Option A: Use GitHub Releases directly (Tauri supports this)
  - Option B: Host a `latest.json` file in the release assets
  - The updater PAT (`UPDATE_TOKEN`) moves to Rust config

- [ ] **1D.5** Test the full build pipeline:
  - `cargo tauri build` produces a signed `.dmg`
  - Verify notarization works
  - Verify DMG size (target: <35 MB)
  - Verify the app launches from DMG on a clean system

- [ ] **1D.6** Test auto-update flow:
  - Publish a test release
  - Verify running app detects and installs update
  - Verify the update preserves BLE/location permissions

- [ ] **1D.7** Update `docs/RELEASING.md` for Tauri workflow

**Considerations:**
- Tauri and Electron will have **different release assets**. During the transition, we need to publish both formats or cut over cleanly.
- Tauri's updater format is different from electron-builder's `latest-mac.yml`. Existing Electron installs won't auto-update to Tauri — users will need to manually install the Tauri version once.
- Code signing is handled by `tauri-cli` using the same Apple Developer ID certificate.

**Exit criteria:** `cargo tauri build` produces a signed, notarized DMG under 35 MB. Auto-updates work.

**Estimated effort:** 3-4 hours

---

#### 1E: CSS & Frontend Adjustments

- [ ] **1E.1** Replace `-webkit-app-region: drag` with `data-tauri-drag-region`:
  - In `index.html`: add `data-tauri-drag-region` to the header/titlebar element
  - In CSS: keep the `-webkit-app-region` for reference but it's not needed in Tauri
  - Tauri uses the HTML attribute for drag regions, not CSS

- [ ] **1E.2** Remove `-electron-corner-smoothing: system-ui`:
  - This is Electron-only. Tauri macOS windows get native rounded corners automatically.

- [ ] **1E.3** Update CSP in `index.html`:
  ```html
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'self' 'unsafe-inline' tauri: asset: ipc:;
             connect-src 'self' 127.0.0.1:* https://*;
             img-src 'self' data: https://*;
             font-src 'self' https://fonts.gstatic.com;">
  ```

- [ ] **1E.4** Test all CSS in WebKit:
  - `backdrop-filter` — works natively in WebKit
  - Custom scrollbar styles — works in WebKit
  - `color-scheme: dark light` — works in WebKit
  - Squircle corners — use `border-radius` (already in place)

- [ ] **1E.5** Test Web Audio API (used for waveform display in crop editor):
  - WebKit's Web Audio API is fully compatible
  - Verify `AudioContext.decodeAudioData()` works with OGG files

**Considerations:**
- The frontend is vanilla HTML/CSS/JS with zero frameworks. WebKit compatibility is excellent for this kind of code.
- The only Electron-specific CSS properties are `-webkit-app-region` and `-electron-corner-smoothing`. Everything else is standard CSS.

**Exit criteria:** All UI renders correctly in Tauri's WebKit webview. Crop editor works.

**Estimated effort:** 1-2 hours

---

#### 1F: Testing & Validation

- [ ] **1F.1** BLE scan + connect + sync flow
- [ ] **1F.2** WiFi sync (hotspot enable → transfer → internet restore)
- [ ] **1F.3** Transcription (local Whisper + OpenAI API)
- [ ] **1F.4** Summarisation (Claude → GPT → DistilBART cascade)
- [ ] **1F.5** Audio crop (waveform render, drag handles, save)
- [ ] **1F.6** Plaud Cloud sync (login, download, upload)
- [ ] **1F.7** Notion integration (OAuth, push)
- [ ] **1F.8** Settings (all 6 tabs, save/load)
- [ ] **1F.9** Theme cycling (light → dark → auto)
- [ ] **1F.10** Tray icon (show, sync, quit)
- [ ] **1F.11** Auto-updater (check, download, install, restart)
- [ ] **1F.12** Deep link (plaud-v2:// OAuth callback)
- [ ] **1F.13** Launch at login
- [ ] **1F.14** Dock visibility toggle
- [ ] **1F.15** Notifications (sync complete, update available)
- [ ] **1F.16** Error reporting (auto-bug to GitHub Issues)
- [ ] **1F.17** First-run experience (onboarding, permissions, device pairing)
- [ ] **1F.18** Run existing pytest suite (112 tests) — should all pass since Python backend is unchanged

**Exit criteria:** All features work identically to the Electron version. DMG size is under 35 MB.

**Estimated effort:** 2-3 hours

---

### Phase 1 Summary

| Sub-phase | Effort | Dependencies |
|-----------|--------|--------------|
| 1A: Python sidecar | 4-6h | Phase 0 |
| 1B: IPC bridge | 3-4h | Phase 0 |
| 1C: Tauri plugins | 4-6h | 1A, 1B |
| 1D: Build pipeline | 3-4h | 1C |
| 1E: CSS/frontend | 1-2h | 1B |
| 1F: Testing | 2-3h | All above |
| **Total Phase 1** | **~17-25 hours** | |

**Phase 1 delivers:** A fully functional Tauri app at ~28-33 MB, identical features to Electron, same Python backend. The Electron code remains in the repo for reference/rollback.

---

### Phase 2: Port Python Backend to Rust (Future, Optional)

**Goal:** Eliminate the Python dependency entirely. All backend logic runs as Rust code inside the Tauri binary. App shrinks to ~8-15 MB. Users no longer need Python installed.

This is a much larger effort and should only be undertaken after Phase 1 is stable and shipped.

#### 2A: Rust BLE (replace bleak)

- [ ] **2A.1** Add `btleplug` crate for cross-platform BLE
- [ ] **2A.2** Port BLE scanner (manufacturer data parsing, serial number matching)
- [ ] **2A.3** Port BLE connect + service discovery
- [ ] **2A.4** Port RSA handshake (send SN signature, receive RSA blob)
- [ ] **2A.5** Port ChaCha20-Poly1305 encrypted command channel
- [ ] **2A.6** Port APP_KEY handshake (cmd_id=1)
- [ ] **2A.7** Port file listing (cmd 26) and download (cmd 28)
- [ ] **2A.8** Port WiFi sync BLE commands (hotspot enable/disable)

**Risk:** `btleplug` on macOS uses CoreBluetooth, which is well-supported. However, our BLE protocol has specific timing requirements and the bleak implementation has been battle-tested. Must test thoroughly.

**Effort:** 15-20 hours (BLE is the most complex part of the backend)

#### 2B: Rust Crypto (replace cryptography)

- [ ] **2B.1** Add `rsa` + `chacha20` + `chacha20poly1305` crates
- [ ] **2B.2** Port RSA key loading and PKCS1v15 decryption
- [ ] **2B.3** Port ChaCha20-Poly1305 session encryption/decryption
- [ ] **2B.4** Port per-file ChaCha20 XOR audio decryption
- [ ] **2B.5** Port OGG/Opus container wrapping (struct packing)
- [ ] **2B.6** Verify decrypted audio matches Python output (bit-exact comparison)

**Effort:** 6-8 hours

#### 2C: Rust HTTP & AI Providers (replace requests)

- [ ] **2C.1** Add `reqwest` crate for HTTP
- [ ] **2C.2** Port Plaud Cloud API client (OAuth, token refresh, recording list, download, upload)
- [ ] **2C.3** Port OpenAI Whisper API client (multipart upload, streaming response)
- [ ] **2C.4** Port Anthropic Claude API client (JSON messages, streaming)
- [ ] **2C.5** Port OpenAI GPT API client (JSON mode)
- [ ] **2C.6** Port Notion MCP client (OAuth PKCE, Streamable HTTP transport)
- [ ] **2C.7** Port webhook firing

**Effort:** 8-12 hours

#### 2D: Rust WiFi Management (replace CoreWLAN)

- [ ] **2D.1** Use `objc2` crate to call CoreWLAN APIs directly
- [ ] **2D.2** Port WiFi scan + associate for hotspot connection
- [ ] **2D.3** Port internet restoration (preferred networks, power cycle)
- [ ] **2D.4** Port `networksetup` CLI calls via `std::process::Command`

**Effort:** 4-6 hours

#### 2E: FFmpeg Integration

- [ ] **2E.1** Option A: Keep bundled FFmpeg binary, call from Rust via `std::process::Command` (simplest, same as now)
- [ ] **2E.2** Option B: Use `ffmpeg-next` Rust crate (statically links FFmpeg, no separate binary needed, saves ~25 MB in bundle)
- [ ] **2E.3** Port audio export (OGG → MP3) and crop (seek + copy) operations

**Effort:** 2-4 hours (Option A) or 6-8 hours (Option B)

#### 2F: Local AI (replace transformers)

- [ ] **2F.1** Decision: keep local summarization or drop it?
  - Current: DistilBART via `transformers` Python library (~500 MB of dependencies when installed)
  - Option A: Drop local summarization — require cloud AI keys for summaries
  - Option B: Use `candle` (Rust ML framework by Hugging Face) for local DistilBART
  - Option C: Use `whisper-rs` (Rust Whisper bindings) for local transcription + drop DistilBART
- [ ] **2F.2** If keeping: port DistilBART inference to `candle`
- [ ] **2F.3** If keeping Whisper: port local Whisper to `whisper-rs`

**Effort:** Variable — 0 hours (drop) to 20+ hours (full port)

#### 2G: Remove Python

- [ ] **2G.1** Remove Python sidecar management code from Rust
- [ ] **2G.2** Remove `api_server.py` and all Flask routes
- [ ] **2G.3** Remove `ensurePythonDeps` and Python path discovery
- [ ] **2G.4** Update frontend to call Tauri `invoke()` instead of `fetch()` for all 47 routes
- [ ] **2G.5** Remove Python source from bundle resources
- [ ] **2G.6** Update README: "No Python required"

**Effort:** 4-6 hours

### Phase 2 Summary

| Sub-phase | Effort | Can be done incrementally |
|-----------|--------|--------------------------|
| 2A: Rust BLE | 15-20h | Yes — port one command at a time |
| 2B: Rust Crypto | 6-8h | Yes — can coexist with Python |
| 2C: Rust HTTP/AI | 8-12h | Yes — port one provider at a time |
| 2D: Rust WiFi | 4-6h | Yes |
| 2E: FFmpeg | 2-8h | Yes |
| 2F: Local AI | 0-20h | Yes — can drop if not needed |
| 2G: Remove Python | 4-6h | Only after all above complete |
| **Total Phase 2** | **~40-80 hours** | |

---

## Migration Risks & Mitigations

### Critical Risks

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| **Tauri WebKit doesn't render our UI correctly** | Low | High | Our CSS is already WebKit-compatible. Test early in Phase 0. |
| **BLE permissions don't persist across Tauri updates** | Medium | High | Tauri uses the same code signing identity mechanism. Test in Phase 1D. |
| **Python sidecar management is unreliable** | Low | High | Pattern is well-established (VS Code, other apps do this). Test thoroughly in Phase 1A. |
| **Auto-updater breaks for existing Electron users** | Certain | Medium | Electron users must manually install the Tauri version. Communicate this clearly. Cannot auto-update across frameworks. |
| **Notarization fails with Tauri** | Low | Medium | Tauri has built-in notarization support. Same certificate. Test in Phase 1D. |

### Medium Risks

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| **Web Audio API differences in WebKit** | Low | Medium | Test crop editor early. WebKit's Web Audio API is mature. |
| **Phase 2 BLE port introduces regressions** | Medium | Medium | Keep Python fallback until Rust BLE is fully tested. Run both in parallel during development. |
| **`btleplug` doesn't support all our BLE operations** | Low | Medium | btleplug supports CoreBluetooth well. Our protocol is standard GATT read/write/notify. |
| **Tauri plugin versions are incompatible** | Low | Medium | Pin plugin versions. Use Tauri v2 stable. |

### Low Risks

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| **Users confused by framework change** | Low | Low | App looks and works identically. Only the install is different. |
| **Rust compile times slow development** | Medium | Low | Use `cargo tauri dev` with hot-reload for frontend changes. Rust changes require recompile (~30s incremental). |

---

## Rollback Plan

### Phase 1 Rollback

The Electron code is preserved on the `master` branch. If Tauri doesn't work out:

1. Switch back to `master` branch
2. Run `pnpm run build` as before
3. Publish with electron-builder as before
4. No data migration needed — config files are identical

### Phase 2 Rollback

If partial Rust porting causes issues:

1. Revert to Phase 1 (Tauri + Python sidecar) — all Python code is still present
2. Re-enable Python sidecar management
3. No data migration needed

---

## Release Strategy

### Transition Plan

1. **Ship Phase 1 as a new release** (e.g., v2.0.0) to signify the framework change
2. **Existing Electron users** cannot auto-update — they must manually download the new DMG
3. **Communicate via GitHub Release notes** that v2.0.0 is a rebuild with identical features but smaller size
4. **Keep the last Electron release** (v1.x.x) available for download as a fallback
5. **Phase 2 releases** (v2.1.0+) can auto-update from v2.0.0 since they're all Tauri

### Version Strategy

| Version | Framework | Python | Notes |
|---------|-----------|--------|-------|
| v1.10.4 (current) | Electron | Yes (sidecar) | Last Electron release |
| v2.0.0 | Tauri | Yes (sidecar) | Phase 1 — same features, smaller bundle |
| v2.x.x | Tauri | Yes (sidecar) | Bug fixes, new features |
| v3.0.0 | Tauri | No (all Rust) | Phase 2 — no Python required |

---

## Directory Structure (Phase 1)

```
plaud-v2/
├── app/                          # Frontend (unchanged)
│   ├── index.html
│   ├── renderer.js
│   ├── tauri-bridge.js           # NEW — replaces preload.js
│   ├── ui/
│   │   ├── utils.js
│   │   ├── recordings.js
│   │   ├── transcript.js
│   │   ├── settings.js
│   │   └── setup.js
│   ├── css/
│   ├── api_server.py             # Python backend (unchanged)
│   ├── routes/
│   ├── state/
│   ├── providers/
│   ├── utils/
│   └── assets/
├── src/                          # Python transcription scripts (unchanged)
├── src-tauri/                    # NEW — Tauri backend
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   ├── Entitlements.plist
│   ├── icons/
│   ├── src/
│   │   ├── main.rs               # Tauri app entry point
│   │   ├── python.rs             # Python sidecar management
│   │   ├── commands/
│   │   │   ├── mod.rs
│   │   │   ├── theme.rs
│   │   │   ├── system.rs
│   │   │   ├── updater.rs
│   │   │   ├── reporting.rs
│   │   │   └── shell.rs
│   │   └── lib.rs
│   └── build.rs
├── keys/                         # RSA keys (unchanged)
├── bin/                          # FFmpeg (unchanged)
├── tests/                        # Python tests (unchanged)
├── scripts/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── RELEASING.md
│   └── TAURI_MIGRATION.md        # This document
└── .gitignore                    # Add: /src-tauri/target/
```

---

## Decision Log

| Decision | Choice | Reasoning |
|----------|--------|-----------|
| Keep Python backend in Phase 1 | Yes | Minimises risk, maximises reuse. 6,470 lines of tested Python code. |
| Use `window.plaud` compatibility layer | Yes | Zero changes to 4,904 lines of frontend code. |
| Tauri v2 (not v1) | v2 | Stable, better plugin ecosystem, better macOS support. |
| Version bump to 2.0.0 | Yes | Framework change warrants major version bump. Communicates the switch. |
| Keep FFmpeg as bundled binary (Phase 1) | Yes | Simplest. Saves the ffmpeg-next crate complexity for Phase 2. |
| Manual install for Electron→Tauri transition | Yes | No safe way to auto-update across frameworks. Clear communication needed. |

---

## Getting Started

To begin Phase 0:

```bash
# 1. Install Rust (if not already installed)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# 2. Install Tauri CLI
cargo install tauri-cli

# 3. Initialize Tauri in the project
cd .
cargo tauri init

# 4. Configure and test
cargo tauri dev
```
