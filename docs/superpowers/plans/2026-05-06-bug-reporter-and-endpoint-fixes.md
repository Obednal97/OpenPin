# Bug Reporter Filtering, Health Gate, and Endpoint Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the auto-bug reporter from filing GitHub issues for legitimate fetch aborts and Python-startup races, while keeping every suppressed event in a local diagnostics log; gate the UI on backend health; remove the 30s timeout from `/device/connect` (BLE pairing legitimately runs longer).

**Architecture:** Three coordinated changes — a local `diagnostics.log` sink, two new reporter filters (`AbortError` and "backend down"), and a frontend health-gate splash that polls `/health` before mounting the main UI and continues to feed backend-health state to the reporter. Plus two surgical endpoint fixes.

**Tech Stack:** Electron + Tauri (parallel runtimes, both must be updated), vanilla JS frontend, Rust Tauri commands, Flask Python backend. Manual testing only — the reporter spans `window`, file system, and child processes; mocking would cost more than it catches.

**Spec:** `docs/superpowers/specs/2026-05-06-bug-reporter-and-endpoint-fixes-design.md`

---

## File Structure

| File | Disposition | Responsibility |
|---|---|---|
| `app/main.js` | Modify | Electron reporter — add diagnostics log, AbortError filter, backend-health state + IPC handler |
| `app/preload.js` | Modify | Add `/device/connect` to `NO_TIMEOUT_ENDPOINTS`; expose `setBackendHealth` IPC; remove 15s startup grace |
| `app/tauri-bridge.js` | Modify | Same as preload, Tauri parity (uses `invoke`) |
| `src-tauri/src/commands/mod.rs` | Modify | Tauri reporter — diagnostics log, AbortError filter, backend-health state, `set_backend_health` command |
| `src-tauri/src/lib.rs` | Modify | Register `set_backend_health` in `invoke_handler` |
| `app/ui/health-gate.js` | **New** | Splash UI, `/health` poll, retry screen, background poller, push to main |
| `app/index.html` | Modify | Insert splash markup, load `health-gate.js` first |
| `app/ui/transcript.js` | Modify | Wrap unwrapped event-handler awaits |

---

## Task ordering

Tasks are ordered so each leaves the app in a working, testable state:

1. Endpoint timeout (immediate noise reduction; independent of all other tasks)
2–5. Backend filter + state (Electron + Tauri parity, parallel work)
6. Health-gate splash + initial poll
7. Health-gate background poller
8. Remove the 15s startup grace (now safe — health-gate handles it)
9. Wrap transcript-view event-handler awaits
10. Manual verification matrix

---

## Pre-flight check

- [ ] **Step 1: Confirm working tree is clean**

Run: `git -C . status`
Expected: A few untracked `scripts/*.py` files from prior testing — fine to leave. No staged/modified files in `app/` or `src-tauri/` should exist before starting.

- [ ] **Step 2: Confirm spec is committed**

Run: `git -C . log --oneline -5`
Expected: Recent commit `Add design spec for bug reporter filtering and endpoint fixes`.

---

## Task 1: Move `/device/connect` to `NO_TIMEOUT_ENDPOINTS`

**Files:**
- Modify: `app/preload.js:19-25`
- Modify: `app/tauri-bridge.js:46-52`

This is independent of every other task. It's the simplest fix — closes #79/#81 root cause. Ship-able on its own.

- [ ] **Step 1: Update Electron preload**

In `app/preload.js`, change the `NO_TIMEOUT_ENDPOINTS` Set (currently lines 19-25) to add `/device/connect` at the top:

```javascript
// Endpoints that are legitimately long-running — never abort these on timeout
const NO_TIMEOUT_ENDPOINTS = new Set([
  '/device/connect', // BLE pairing — service discovery can take 30-45s on slow links
  '/device/sync', '/device/wifi-sync',
  '/transcribe', '/summarize',
  '/cloud/sync',
  '/gdrive/sync', '/gdrive/upload',
  '/recordings/redownload', '/recordings/crop', '/recordings/export',
]);
```

- [ ] **Step 2: Update Tauri bridge**

In `app/tauri-bridge.js`, apply the identical change to the `NO_TIMEOUT_ENDPOINTS` Set (currently lines 46-52):

```javascript
// Endpoints that are legitimately long-running — never abort these on timeout
const NO_TIMEOUT_ENDPOINTS = new Set([
  '/device/connect', // BLE pairing — service discovery can take 30-45s on slow links
  '/device/sync', '/device/wifi-sync',
  '/transcribe', '/summarize',
  '/cloud/sync',
  '/gdrive/sync', '/gdrive/upload',
  '/recordings/redownload', '/recordings/crop', '/recordings/export',
]);
```

- [ ] **Step 3: Verify both files have identical sets**

Run: `diff <(grep -A 9 'NO_TIMEOUT_ENDPOINTS = new Set' ./app/preload.js) <(grep -A 9 'NO_TIMEOUT_ENDPOINTS = new Set' ./app/tauri-bridge.js)`
Expected: No output (identical).

- [ ] **Step 4: Commit**

```bash
git -C . add app/preload.js app/tauri-bridge.js
git -C . commit -m "fix: remove 30s timeout from /device/connect (BLE pairing variance)"
```

---

## Task 2: Electron — diagnostics log helper

**Files:**
- Modify: `app/main.js` (add helper ~line 230, before `_reportedErrors`)

The diagnostics log is a JSON-line file under `<userData>/diagnostics.log`. Every suppressed report writes one line; rotation kicks in when the file exceeds 1 MB.

- [ ] **Step 1: Add the helper function**

Insert this block in `app/main.js` immediately above the `// ---- Auto Bug Reporter ----` comment at line 229 (so the helper is defined before `reportError` uses it):

```javascript
// ---- Diagnostics Log ----
// Local sink for events that are suppressed from GitHub.
// Path: <userData>/diagnostics.log, rotated at 1 MB to .log.1, max ~2 MB total.
const _DIAGNOSTICS_MAX_BYTES = 1024 * 1024;
function _diagnosticsLogPath() {
  return path.join(app.getPath('userData'), 'diagnostics.log');
}
function _appendDiagnostic({ context, message, stack, fingerprint }) {
  try {
    const file = _diagnosticsLogPath();
    // Rotate if size exceeds cap
    try {
      const st = fs.statSync(file);
      if (st.size >= _DIAGNOSTICS_MAX_BYTES) {
        fs.renameSync(file, file + '.1');
      }
    } catch (e) {
      if (e.code !== 'ENOENT') throw e;
    }
    const line = JSON.stringify({
      ts: new Date().toISOString(),
      version: app.getVersion(),
      context: context || 'unknown',
      message: String(message || ''),
      fingerprint: fingerprint || '',
      stackTruncated: String(stack || '').slice(0, 4096),
    }) + '\n';
    fs.appendFileSync(file, line);
  } catch (err) {
    // Never let diagnostics failures crash anything
    console.error('[Diagnostics] append failed:', err.message);
  }
}
```

- [ ] **Step 2: Verify the file still loads**

Run: `node -c ./app/main.js`
Expected: No output, exit 0 (syntax valid).

- [ ] **Step 3: Smoke-check by booting the dev app**

Run: `cd . && timeout 8 npm --prefix app start 2>&1 | head -20 || true`
Expected: Standard startup logs, no crash. Then quit (Cmd+Q if needed). The helper is defined but not yet called — no diagnostics file should be created.

Run: `ls -la "$HOME/Library/Application Support/Plaud V2/diagnostics.log" 2>&1`
Expected: `No such file or directory`.

- [ ] **Step 4: Commit**

```bash
git -C . add app/main.js
git -C . commit -m "feat: add diagnostics log helper to Electron main"
```

---

## Task 3: Electron — AbortError filter + write through

**Files:**
- Modify: `app/main.js` lines 318-322 (start of `reportError`)
- Modify: `app/main.js` line 431 area (after the GitHub flow)

The reporter currently dedupes on fingerprint and goes straight to GitHub. New flow:
1. Always call `_appendDiagnostic` first (every event captured locally).
2. If `_isAbortError(message)`, return — no GitHub.
3. Otherwise, dedupe + GitHub flow as before.

- [ ] **Step 1: Add the predicate and update reportError**

In `app/main.js`, replace the opening of `reportError` (current lines 318-322):

Current:
```javascript
async function reportError(error, context = 'main') {
  try {
    const fingerprint = errorFingerprint(error);
    if (_reportedErrors.has(fingerprint)) return;
    _reportedErrors.add(fingerprint);
```

Replace with:
```javascript
function _isAbortError(message) {
  return /aborted|AbortError|signal is aborted/i.test(String(message || ''));
}

async function reportError(error, context = 'main') {
  try {
    const fingerprint = errorFingerprint(error);
    const errorMsg = String(error.message || error);
    const stack = error.stack || '';

    // Always capture in local diagnostics, then decide whether to escalate to GitHub
    _appendDiagnostic({ context, message: errorMsg, stack, fingerprint });

    // FILTER: legitimate aborts (timeouts, navigation, manual cancellation) — log only
    if (_isAbortError(errorMsg)) {
      return;
    }

    // Existing dedupe (per session)
    if (_reportedErrors.has(fingerprint)) return;
    _reportedErrors.add(fingerprint);
```

Also delete the now-duplicated `const errorMsg = ...` and `const stack = ...` lines that appear a few lines below (currently lines 325-326). After your edit those declarations should only exist in the new opening block.

- [ ] **Step 2: Verify the file still parses**

Run: `node -c ./app/main.js`
Expected: No output, exit 0.

- [ ] **Step 3: Verify `errorMsg` is only declared once**

Run: `grep -c "const errorMsg = String(error.message" ./app/main.js`
Expected: `1`.

Run: `grep -c "const stack = error.stack" ./app/main.js`
Expected: `1`.

- [ ] **Step 4: Manually test by injecting an AbortError**

Add a temporary test: open `app/main.js` and add at the very bottom:
```javascript
setTimeout(() => {
  reportError(new Error('AbortError: The operation was aborted'), 'test:abort');
  reportError(new Error('Real bug: something blew up'), 'test:real');
}, 3000);
```

Run: `cd . && timeout 10 npm --prefix app start 2>&1 | head -40`
Expected: Two events fire at the 3s mark. Quit the app.

Run: `cat "$HOME/Library/Application Support/Plaud V2/diagnostics.log"`
Expected: Two JSON lines. The `test:abort` line is present but did NOT trigger a GitHub call. The `test:real` line is present and (in your console output above) you should have seen `[BugReporter] Created issue #N` or `Commented on existing issue`.

- [ ] **Step 5: Verify only the real error hit GitHub**

Run: `gh issue list --state open --limit 5 --json number,title --search "test:real"`
Expected: Either a new `[Auto] [DEV] Real bug: something blew up` issue OR a comment on an existing matching issue. Either way, no issue with `AbortError` in its title from this run.

If a test issue was created, close it: `gh issue close <number> --comment "test from plan task 3"`.

**Remove the temporary test block from `app/main.js` before committing.**

- [ ] **Step 6: Verify the test block is gone**

Run: `grep -c "test:abort" ./app/main.js`
Expected: `0`.

- [ ] **Step 7: Commit**

```bash
git -C . add app/main.js
git -C . commit -m "feat: filter AbortError from reporter, log all events to diagnostics"
```

---

## Task 4: Electron — backend-health state + IPC handler + filter

**Files:**
- Modify: `app/main.js` (state declaration near top + IPC handler near other handlers + filter inside reportError)

State: `_backendHealth` defaults to `'unknown'`. The renderer's health-gate poller pushes updates via the new `set-backend-health` IPC channel. The reporter filters when `_backendHealth === 'down'`.

- [ ] **Step 1: Add the state variable**

In `app/main.js`, find the line `let _startupStatus = 'Starting up...';` (around line 15). Add immediately after it:

```javascript
let _backendHealth = 'unknown'; // 'up' | 'down' | 'unknown' — pushed from renderer via set-backend-health IPC
```

- [ ] **Step 2: Add the IPC handler**

Find the block of `ipcMain.handle(...)` registrations (starts around line 1038 with `get-api-info`). Add this new handler at the end of that block (after the last `ipcMain.handle` and before the next non-handler code):

```javascript
ipcMain.handle('set-backend-health', (event, state) => {
  if (state === 'up' || state === 'down' || state === 'unknown') {
    _backendHealth = state;
  }
  return { ok: true, state: _backendHealth };
});
```

- [ ] **Step 3: Add the filter inside reportError**

Earlier in `reportError` (the function you edited in Task 3) you added the AbortError filter. Right after it, add the backend-down filter:

```javascript
    // FILTER: legitimate aborts (timeouts, navigation, manual cancellation) — log only
    if (_isAbortError(errorMsg)) {
      return;
    }

    // FILTER: backend is known to be down — these are environmental, log only
    if (_backendHealth === 'down') {
      return;
    }
```

- [ ] **Step 4: Verify the file parses**

Run: `node -c ./app/main.js`
Expected: No output, exit 0.

- [ ] **Step 5: Verify the IPC handler is registered**

Run: `grep -n "ipcMain.handle('set-backend-health'" ./app/main.js`
Expected: One line of output.

- [ ] **Step 6: Commit**

```bash
git -C . add app/main.js
git -C . commit -m "feat: add backend-health state + IPC handler, filter reports when down"
```

---

## Task 5: Tauri — diagnostics log + AbortError filter + backend-health state

**Files:**
- Modify: `src-tauri/src/commands/mod.rs` (helper functions + state + filter inside `report_error` at line 765)
- Modify: `src-tauri/src/lib.rs:305-321` (register new command)

Tauri parity: same behavior, Rust idioms.

- [ ] **Step 1: Add diagnostics log path helper and append function**

In `src-tauri/src/commands/mod.rs`, find the `// ---- Error/Bug Reporting ----` section (around line 723). Insert these helpers immediately before that comment:

```rust
// ---- Diagnostics Log ----

const DIAGNOSTICS_MAX_BYTES: u64 = 1024 * 1024;

fn diagnostics_log_path() -> std::path::PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| std::path::PathBuf::from("/tmp"))
        .join("Library/Application Support/com.plaud.v2/diagnostics.log")
}

fn append_diagnostic(context: &str, message: &str, stack: &str, fingerprint: &str) {
    let path = diagnostics_log_path();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    // Rotate if oversize
    if let Ok(meta) = std::fs::metadata(&path) {
        if meta.len() >= DIAGNOSTICS_MAX_BYTES {
            let mut rotated = path.clone();
            rotated.set_extension("log.1");
            let _ = std::fs::rename(&path, &rotated);
        }
    }
    let stack_trunc: String = stack.chars().take(4096).collect();
    let line = serde_json::json!({
        "ts": chrono::Utc::now().to_rfc3339(),
        "version": app_version(),
        "context": context,
        "message": message,
        "fingerprint": fingerprint,
        "stackTruncated": stack_trunc,
    }).to_string();
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        let _ = writeln!(f, "{}", line);
    }
}

fn is_abort_error(message: &str) -> bool {
    let lower = message.to_lowercase();
    lower.contains("aborted") || lower.contains("aborterror") || lower.contains("signal is aborted")
}
```

Note: this assumes `chrono` and `dirs` are already in `Cargo.toml`. Verify in step 2.

- [ ] **Step 2: Verify dependencies exist**

Run: `grep -E '^(chrono|dirs|serde_json)' ./src-tauri/Cargo.toml`
Expected: All three crates listed. If `chrono` is missing, add it:

```bash
cd ./src-tauri && cargo add chrono
```

If `dirs` is missing, add it:

```bash
cd ./src-tauri && cargo add dirs
```

- [ ] **Step 3: Add backend-health state**

In `src-tauri/src/commands/mod.rs`, find the `static REPORTED_ERRORS` declaration (around line 726). Add this block immediately after `fn reported_errors()` (which ends around line 731):

```rust
// ---- Backend Health State ----

static BACKEND_HEALTH: std::sync::OnceLock<std::sync::Mutex<String>> = std::sync::OnceLock::new();

fn backend_health() -> &'static std::sync::Mutex<String> {
    BACKEND_HEALTH.get_or_init(|| std::sync::Mutex::new("unknown".to_string()))
}

#[tauri::command]
pub fn set_backend_health(state: String) -> Result<String, String> {
    if state == "up" || state == "down" || state == "unknown" {
        let mut current = backend_health().lock().unwrap();
        *current = state.clone();
        Ok(state)
    } else {
        Err(format!("Invalid state: {}", state))
    }
}
```

- [ ] **Step 4: Update report_error to apply both filters**

In the same file, find `pub fn report_error` at line 765. Replace the function body up through the `set.insert(fingerprint.clone())` line (currently line 776) with:

```rust
#[tauri::command]
pub fn report_error(message: String, stack: Option<String>, context: Option<String>) {
    let ctx = context.unwrap_or_else(|| "unknown".to_string());
    log::error!("[BugReporter] {ctx}: {message}");

    let fingerprint = error_fingerprint(&message);
    let stack_str = stack.unwrap_or_default();

    // Always capture locally before deciding whether to escalate
    append_diagnostic(&ctx, &message, &stack_str, &fingerprint);

    // FILTER: legitimate aborts — log only
    if is_abort_error(&message) {
        return;
    }

    // FILTER: backend is known to be down — log only
    if backend_health().lock().unwrap().as_str() == "down" {
        return;
    }

    // Existing dedupe (per session)
    {
        let mut set = reported_errors().lock().unwrap();
        if set.contains(&fingerprint) {
            return;
        }
        set.insert(fingerprint.clone());
    }
```

The next line in the original is `let stack_str = stack.unwrap_or_default();` — DELETE it (we already declared `stack_str` above). The rest of the function (version/dev_tag/thread spawn) is unchanged.

- [ ] **Step 5: Register the new Tauri command**

In `src-tauri/src/lib.rs`, find the `invoke_handler` block at line 305. Add `commands::set_backend_health,` to the list — alphabetical-ish placement, e.g., right after `commands::report_error,`:

```rust
        .invoke_handler(tauri::generate_handler![
            commands::get_api_info,
            commands::get_theme,
            commands::cycle_theme,
            commands::get_app_version,
            commands::open_external,
            commands::report_error,
            commands::report_bug,
            commands::set_backend_health,
            commands::get_login_item,
            commands::set_login_item,
            commands::get_dock_visible,
            commands::set_dock_visible,
            commands::check_for_updates,
            commands::install_update,
            commands::show_notification,
            commands::select_directory,
        ])
```

- [ ] **Step 6: Verify the Rust crate builds**

Run: `cd ./src-tauri && cargo build 2>&1 | tail -30`
Expected: `Finished` (warnings OK, errors not). If errors mention missing `chrono` or `dirs`, redo Step 2 to add them.

- [ ] **Step 7: Verify the new command is registered**

Run: `grep -n "set_backend_health" ./src-tauri/src/lib.rs`
Expected: One line showing it inside `invoke_handler`.

Run: `grep -n "fn set_backend_health" ./src-tauri/src/commands/mod.rs`
Expected: One line.

- [ ] **Step 8: Commit**

```bash
git -C . add src-tauri/
git -C . commit -m "feat: Tauri parity — diagnostics log, AbortError filter, backend-health command"
```

---

## Task 6: Health-gate splash UI + initial poll

**Files:**
- Create: `app/ui/health-gate.js`
- Modify: `app/index.html` (add splash markup + load script first)

The splash covers the page on launch and polls `/health` every 250 ms for up to 30 s. On success it removes itself; on timeout it shows a "Backend not responding" screen with Retry and Open-diagnostics-log buttons.

- [ ] **Step 1: Create the health-gate module**

Create `app/ui/health-gate.js` with this content:

```javascript
/* ============================================================
   Plaud V2 — Health Gate
   ----------------------------------------------------------------
   Mounted before the main UI. Polls /health every 250 ms (cap 30 s)
   and only reveals the app once the backend responds. On timeout,
   shows a "Backend not responding" screen with Retry / Open log.

   Background poller is wired in Task 7.
   ============================================================ */

(function () {
  const POLL_INTERVAL_MS = 250;
  const POLL_TIMEOUT_MS = 30000;

  function getApiUrl() {
    // Both bridges expose audioUrl; cheaper: read _apiUrl via getApiInfo.
    if (typeof window.plaud === 'undefined') return null;
    return window.plaud.getApiInfo
      ? window.plaud.getApiInfo().then((info) => info && info.url).catch(() => null)
      : Promise.resolve(null);
  }

  async function pingHealthOnce() {
    try {
      const data = await window.plaud.health();
      return data && (data.status === 'ok' || data.ok === true);
    } catch {
      return false;
    }
  }

  function buildSplash() {
    const splash = document.createElement('div');
    splash.id = 'health-gate-splash';
    splash.setAttribute('style', [
      'position:fixed', 'inset:0', 'z-index:99999',
      'background:var(--bg-primary, #ffffff)', 'color:var(--text-primary, #111)',
      'display:flex', 'flex-direction:column', 'align-items:center', 'justify-content:center',
      'gap:16px', 'font-family:Inter, -apple-system, system-ui, sans-serif',
    ].join(';') + ';');
    splash.innerHTML = `
      <div style="width:48px;height:48px;border:3px solid currentColor;border-top-color:transparent;border-radius:50%;animation:hg-spin 0.8s linear infinite;"></div>
      <div id="health-gate-msg" style="font-size:14px;opacity:0.8;">Starting backend…</div>
      <style>@keyframes hg-spin { to { transform: rotate(360deg); } }</style>
    `;
    document.body.appendChild(splash);
    return splash;
  }

  function showFailureScreen(splash) {
    splash.innerHTML = `
      <div style="font-size:18px;font-weight:600;">Backend not responding</div>
      <div style="font-size:13px;opacity:0.7;max-width:340px;text-align:center;">
        Plaud V2's local server didn't start within 30 seconds.
        Try retrying. If this keeps happening, check the diagnostics log.
      </div>
      <div style="display:flex;gap:8px;margin-top:8px;">
        <button id="hg-retry" style="padding:8px 16px;border-radius:6px;border:1px solid currentColor;background:transparent;color:inherit;cursor:pointer;font:inherit;">Retry</button>
        <button id="hg-openlog" style="padding:8px 16px;border-radius:6px;border:1px solid currentColor;background:transparent;color:inherit;cursor:pointer;font:inherit;">Open diagnostics log</button>
      </div>
    `;
    document.getElementById('hg-retry').addEventListener('click', () => {
      splash.remove();
      mountGate();
    });
    document.getElementById('hg-openlog').addEventListener('click', () => {
      const logPath = '$HOME/Library/Application Support/Plaud V2/diagnostics.log';
      if (window.plaud && window.plaud.openExternal) {
        // openExternal expects a URL; use file:// path
        const home = (window.__TAURI__ ? '' : ''); // placeholder — handled below
        // Best-effort: open the directory; user can find diagnostics.log from there
        window.plaud.openExternal('file://' + (window.__USER_DATA_DIR__ || '/'));
      }
    });
  }

  async function mountGate() {
    const splash = buildSplash();
    const start = Date.now();

    while (Date.now() - start < POLL_TIMEOUT_MS) {
      if (await pingHealthOnce()) {
        splash.remove();
        return; // main UI already mounted in DOM; nothing more to do
      }
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }

    showFailureScreen(splash);
  }

  // Defer until window.plaud is available (preload/tauri-bridge sets it)
  if (window.plaud) {
    mountGate();
  } else {
    document.addEventListener('DOMContentLoaded', () => mountGate(), { once: true });
  }
})();
```

- [ ] **Step 2: Mount the script in index.html**

Open `app/index.html`. Find the existing script tags near the bottom (around line 884):

```html
  <script src="tauri-bridge.js"></script>
  <script src="ui/utils.js"></script>
  <script src="ui/recordings.js"></script>
  <script src="ui/transcript.js"></script>
  <script src="ui/settings.js"></script>
  <script src="ui/setup.js"></script>
  <script src="renderer.js"></script>
```

Insert `<script src="ui/health-gate.js"></script>` immediately after `tauri-bridge.js` and before `ui/utils.js`:

```html
  <script src="tauri-bridge.js"></script>
  <script src="ui/health-gate.js"></script>
  <script src="ui/utils.js"></script>
  <script src="ui/recordings.js"></script>
  <script src="ui/transcript.js"></script>
  <script src="ui/settings.js"></script>
  <script src="ui/setup.js"></script>
  <script src="renderer.js"></script>
```

The Electron preload doesn't load via `<script>` (it's wired up by Electron's `webPreferences.preload`), so this script-tag mount only affects the Tauri build. For Electron, the same `health-gate.js` file gets loaded by the renderer because the preload exposes `window.plaud` synchronously and the script tag pattern works the same way in both. Confirm by checking that no other module needs to load before `health-gate.js`.

- [ ] **Step 3: Verify file syntax**

Run: `node -c ./app/ui/health-gate.js`
Expected: No output, exit 0.

- [ ] **Step 4: Manual test — Tauri build**

Run: `cd . && cargo tauri dev 2>&1 | tee /tmp/plaud-tauri-test.log | head -40`
Expected: App launches, you briefly see the spinner splash, then the main UI appears as soon as `/health` returns OK. The splash typically vanishes within ~500 ms on a warm boot.

Quit the app once you've confirmed the splash flickers in and out.

- [ ] **Step 5: Manual test — Electron build**

Run: `cd . && timeout 12 npm --prefix app start 2>&1 | head -40 || true`
Expected: Same behavior — splash visible momentarily, then main UI.

- [ ] **Step 6: Manual test — backend-not-responding screen**

Temporarily break health by editing `app/routes/core.py:30-32` to return a 503:

```python
@bp.route("/health")
def health():
    from flask import abort
    abort(503)
```

Run the app (Tauri or Electron). Expected: After 30 s the splash transitions to "Backend not responding" with Retry / Open diagnostics log buttons. Click Retry — it should re-attempt and stay on failure since /health is broken.

**REVERT the `core.py` change before proceeding.** Run: `git -C . checkout -- app/routes/core.py`

- [ ] **Step 7: Commit**

```bash
git -C . add app/ui/health-gate.js app/index.html
git -C . commit -m "feat: add health-gate splash before mounting main UI"
```

---

## Task 7: Health-gate background poller

**Files:**
- Modify: `app/ui/health-gate.js` (add a background poller after splash dismissal)
- Modify: `app/preload.js` (expose `setBackendHealth` to renderer)
- Modify: `app/tauri-bridge.js` (expose `setBackendHealth` via Tauri command)

After the splash dismisses, a slower 5 s poller continues running. Each tick pushes the current state to the main process, which the reporter reads to gate filter (b).

- [ ] **Step 1: Expose `setBackendHealth` in Electron preload**

In `app/preload.js`, find the `contextBridge.exposeInMainWorld('plaud', {...})` block (line 91). The reporter section near the bottom (around lines 195-198) currently has:

```javascript
  // Bug reporter
  reportError: (message, stack, context) => ipcRenderer.invoke('report-error', { message, stack, context }),
  reportBug: (expected, actual, steps) => ipcRenderer.invoke('report-bug', { expected, actual, steps }),
});
```

Add a new line in that section:

```javascript
  // Bug reporter
  reportError: (message, stack, context) => ipcRenderer.invoke('report-error', { message, stack, context }),
  reportBug: (expected, actual, steps) => ipcRenderer.invoke('report-bug', { expected, actual, steps }),
  setBackendHealth: (state) => ipcRenderer.invoke('set-backend-health', state),
});
```

- [ ] **Step 2: Expose `setBackendHealth` in Tauri bridge**

In `app/tauri-bridge.js`, find the equivalent reporter section near the bottom of the `window.plaud = {...}` block. Add:

```javascript
  setBackendHealth: (state) => invoke('set_backend_health', { state }),
```

Place it near `reportError` for consistency.

- [ ] **Step 3: Add background poller to health-gate.js**

Open `app/ui/health-gate.js`. Replace the `mountGate` function with this version that starts the background poller after success:

```javascript
  const BG_POLL_INTERVAL_MS = 5000;
  let _bgPollerStarted = false;

  function startBackgroundPoller() {
    if (_bgPollerStarted) return;
    _bgPollerStarted = true;

    // Push initial 'up' state
    pushHealth('up');

    setInterval(async () => {
      const ok = await pingHealthOnce();
      pushHealth(ok ? 'up' : 'down');
    }, BG_POLL_INTERVAL_MS);
  }

  function pushHealth(state) {
    if (window.plaud && window.plaud.setBackendHealth) {
      window.plaud.setBackendHealth(state).catch(() => {});
    }
  }

  async function mountGate() {
    const splash = buildSplash();
    const start = Date.now();

    while (Date.now() - start < POLL_TIMEOUT_MS) {
      if (await pingHealthOnce()) {
        splash.remove();
        startBackgroundPoller();
        return;
      }
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }

    pushHealth('down');
    showFailureScreen(splash);
  }
```

(The `BG_POLL_INTERVAL_MS` and `_bgPollerStarted` variables go inside the IIFE, alongside `POLL_INTERVAL_MS`.)

- [ ] **Step 4: Verify all files parse**

Run:
```bash
node -c ./app/ui/health-gate.js && \
node -c ./app/preload.js && \
node -c ./app/tauri-bridge.js
```
Expected: No output, exit 0.

- [ ] **Step 5: Verify Rust still builds**

Run: `cd ./src-tauri && cargo check 2>&1 | tail -10`
Expected: `Finished` with no errors.

- [ ] **Step 6: Manual test — happy path**

Run: `cd . && timeout 15 npm --prefix app start 2>&1 | head -30 || true`

Expected console output should include log lines suggesting the IPC handler ran (Electron will not log it by default — that's fine). Quit the app.

In a separate terminal:
```bash
cat "$HOME/Library/Application Support/Plaud V2/diagnostics.log" 2>&1 || echo "(none yet — that's fine if no errors fired)"
```

- [ ] **Step 7: Manual test — Python crashes mid-session**

Start the app (`npm --prefix app start`). Once the UI is up, open Activity Monitor (or a separate terminal) and force-kill the Python process:

```bash
pkill -f "api_server.py" || pkill -f "python.*plaud"
```

Within 5 seconds, the `setBackendHealth('down')` IPC call should fire. To confirm: trigger a known-failing call from the UI (e.g., click Scan). The resulting fetch error should be **suppressed** from GitHub but still appear in the diagnostics log.

Run: `tail -5 "$HOME/Library/Application Support/Plaud V2/diagnostics.log"`
Expected: A line with `"context":"api:/device/scan"` or similar, and no new GitHub issue from this run.

Run: `gh issue list --state open --limit 3`
Expected: No new issues created in the last few minutes.

- [ ] **Step 8: Commit**

```bash
git -C . add app/ui/health-gate.js app/preload.js app/tauri-bridge.js
git -C . commit -m "feat: health-gate background poller pushes state to main process"
```

---

## Task 8: Remove the 15s startup grace

**Files:**
- Modify: `app/preload.js` lines 27-28 (constant) and 56 (usage)
- Modify: `app/tauri-bridge.js` lines 54 (constant) and 91 (usage)

The grace was a workaround for the same Python-startup race the health-gate now handles. Removing it tightens the window during which legitimate failures could slip through.

- [ ] **Step 1: Update Electron preload**

In `app/preload.js`:

Delete lines 27-28:
```javascript
// Suppress "Failed to fetch" reports during the first 15s (server may still be starting)
const _appStartTime = Date.now();
```

Find the catch block at line 54-63:
```javascript
  } catch (err) {
    // Network error (server down, etc.) — suppress during startup grace period
    if (REPORT_ENDPOINTS.has(endpoint) && Date.now() - _appStartTime > 15000) {
      ipcRenderer.invoke('report-error', {
        message: `API fetch failed: ${endpoint} — ${err.message}`,
        stack: '',
        context: `api:${endpoint}`,
      }).catch(() => {});
    }
    throw err;
  }
```

Replace with:
```javascript
  } catch (err) {
    // Network error — reporter filters by backend-health state and AbortError
    if (REPORT_ENDPOINTS.has(endpoint)) {
      ipcRenderer.invoke('report-error', {
        message: `API fetch failed: ${endpoint} — ${err.message}`,
        stack: '',
        context: `api:${endpoint}`,
      }).catch(() => {});
    }
    throw err;
  }
```

- [ ] **Step 2: Update Tauri bridge**

In `app/tauri-bridge.js`:

Delete line 54:
```javascript
const _appStartTime = Date.now();
```

Find the catch block (around lines 89-99):
```javascript
    } catch {
      if (REPORT_ENDPOINTS.has(endpoint) && Date.now() - _appStartTime > 15000) {
        invoke('report_error', {
          message: `API fetch failed: ${endpoint} — ${err.message}`,
          stack: '',
          context: `api:${endpoint}`,
        }).catch(() => {});
      }
      throw err;
    }
```

Replace with:
```javascript
    } catch {
      if (REPORT_ENDPOINTS.has(endpoint)) {
        invoke('report_error', {
          message: `API fetch failed: ${endpoint} — ${err.message}`,
          stack: '',
          context: `api:${endpoint}`,
        }).catch(() => {});
      }
      throw err;
    }
```

- [ ] **Step 3: Verify both files no longer reference `_appStartTime`**

Run: `grep -n "_appStartTime" ./app/preload.js ./app/tauri-bridge.js`
Expected: No output (no matches in either file).

- [ ] **Step 4: Verify both files parse**

Run:
```bash
node -c ./app/preload.js && \
node -c ./app/tauri-bridge.js
```
Expected: No output, exit 0.

- [ ] **Step 5: Smoke-test the full flow**

Run: `cd . && timeout 12 npm --prefix app start 2>&1 | head -30 || true`
Expected: No "Failed to fetch" auto-issues created during cold start. Splash is briefly visible. Quit.

Run: `gh issue list --state open --limit 3`
Expected: No new auto-bug issues from this run.

- [ ] **Step 6: Commit**

```bash
git -C . add app/preload.js app/tauri-bridge.js
git -C . commit -m "refactor: remove 15s startup grace (health-gate now owns startup readiness)"
```

---

## Task 9: Wrap unwrapped event-handler awaits in transcript.js

**Files:**
- Modify: `app/ui/transcript.js` lines 32, 213, 675, 705, 751

The spec named line 837 but that one is **already** wrapped (verified at investigation time). The actual unwrapped event-handler awaits live at the lines above. Wrap each in `try { ... } catch (err) { if (err.name !== 'AbortError') console.warn('[transcript]', err); }`.

- [ ] **Step 1: Wrap the rename-title blur handler (line ~30-34)**

Find this block:
```javascript
  titleEl.onblur = async () => {
    const newTitle = titleEl.textContent.trim();
    if (newTitle && newTitle !== rec.title) {
      rec.title = newTitle;
      await window.plaud.renameRecording(sessionId, newTitle);
```

Replace the `await` line and wrap:
```javascript
  titleEl.onblur = async () => {
    const newTitle = titleEl.textContent.trim();
    if (newTitle && newTitle !== rec.title) {
      rec.title = newTitle;
      try {
        await window.plaud.renameRecording(sessionId, newTitle);
      } catch (err) {
        if (err.name !== 'AbortError') console.warn('[transcript] rename failed:', err);
      }
```

The matching closing brace already exists — just make sure the new try-block is closed before the existing `}`.

- [ ] **Step 2: Wrap the delete-transcript call (line ~213)**

Find:
```javascript
    if (t) await window.plaud.deleteTranscript(sessionId);
```

Replace with:
```javascript
    if (t) {
      try {
        await window.plaud.deleteTranscript(sessionId);
      } catch (err) {
        if (err.name !== 'AbortError') console.warn('[transcript] deleteTranscript failed:', err);
      }
    }
```

- [ ] **Step 3: Wrap the single-segment edit blur handler (line ~675)**

Find:
```javascript
    container.querySelector('.segment-text').addEventListener('blur', async (e) => {
      const newText = e.target.textContent.trim();
      if (newText !== t.text) {
        t.text = newText;
        await window.plaud.updateTranscript(sessionId, { segments: [{ text: newText, start: 0 }] });
      }
    });
```

Replace the await line:
```javascript
    container.querySelector('.segment-text').addEventListener('blur', async (e) => {
      const newText = e.target.textContent.trim();
      if (newText !== t.text) {
        t.text = newText;
        try {
          await window.plaud.updateTranscript(sessionId, { segments: [{ text: newText, start: 0 }] });
        } catch (err) {
          if (err.name !== 'AbortError') console.warn('[transcript] updateTranscript failed:', err);
        }
      }
    });
```

- [ ] **Step 4: Wrap saveSegments (line ~702-705)**

Find the `saveSegments` function body:
```javascript
async function saveSegments(sessionId, t) {
  t.text = t.segments.map((s) => s.text).join(' ');
  t.text_preview = t.text.length > 150 ? t.text.slice(0, 150) + '...' : t.text;
  await window.plaud.updateTranscript(sessionId, { segments: t.segments });
```

Wrap the await:
```javascript
async function saveSegments(sessionId, t) {
  t.text = t.segments.map((s) => s.text).join(' ');
  t.text_preview = t.text.length > 150 ? t.text.slice(0, 150) + '...' : t.text;
  try {
    await window.plaud.updateTranscript(sessionId, { segments: t.segments });
  } catch (err) {
    if (err.name !== 'AbortError') console.warn('[transcript] saveSegments failed:', err);
    return;
  }
```

The `return` on error prevents the cache-update lines below from running with stale state. The existing code below (`if (app.transcripts[sessionId]) { ... }`) stays unchanged.

- [ ] **Step 5: Wrap the save-summary click handler (line ~751)**

Find:
```javascript
    saveBtn.addEventListener('click', async () => {
      const newSummary = textarea.value.trim();
      if (newSummary !== t.summary) {
        t.summary = newSummary;
        await window.plaud.updateTranscript(sessionId, { summary: newSummary });
        if (app.transcripts[sessionId]) {
          app.transcripts[sessionId].summary = newSummary;
        }
      }
```

Replace:
```javascript
    saveBtn.addEventListener('click', async () => {
      const newSummary = textarea.value.trim();
      if (newSummary !== t.summary) {
        t.summary = newSummary;
        try {
          await window.plaud.updateTranscript(sessionId, { summary: newSummary });
          if (app.transcripts[sessionId]) {
            app.transcripts[sessionId].summary = newSummary;
          }
        } catch (err) {
          if (err.name !== 'AbortError') console.warn('[transcript] save summary failed:', err);
        }
      }
```

- [ ] **Step 6: Verify the file parses**

Run: `node -c ./app/ui/transcript.js`
Expected: No output, exit 0.

- [ ] **Step 7: Verify the wrapping count**

Run: `grep -c "if (err.name !== 'AbortError') console.warn" ./app/ui/transcript.js`
Expected: `5`.

- [ ] **Step 8: Commit**

```bash
git -C . add app/ui/transcript.js
git -C . commit -m "fix: wrap unwrapped event-handler awaits in transcript view"
```

---

## Task 10: Manual verification matrix

Run the full spec test matrix to confirm everything works end-to-end.

- [ ] **Step 1: Cold start, slow Python**

In a fresh shell, edit `app/api_server.py` to inject a 5-second sleep at the top of the startup path (look for the main entry — typically `if __name__ == "__main__"` or similar block). Add:
```python
import time
time.sleep(5)
```
right before the Flask `app.run(...)` call.

Run: `cd . && timeout 20 npm --prefix app start 2>&1 | head -30 || true`
Expected: Splash visible for ~5 seconds, then main UI mounts. No GitHub issues filed.

Verify:
```bash
gh issue list --state open --limit 5 --search 'created:>=now-2m'
```
Expected: No new issues from this run.

**Revert `app/api_server.py`:** `git -C . checkout -- app/api_server.py`

- [ ] **Step 2: Python never starts**

Edit `app/api_server.py` and add `import sys; sys.exit(1)` at the top so Python exits immediately.

Run: `cd . && timeout 40 npm --prefix app start 2>&1 | head -40 || true`
Expected: Splash visible 30 seconds, then "Backend not responding" screen with Retry / Open diagnostics log buttons.

Click Retry — should re-attempt and stay on failure screen.

```bash
ls -la "$HOME/Library/Application Support/Plaud V2/diagnostics.log"
```
Expected: File exists (probably has entries from health-gate's initial poll attempts pushed via the IPC layer; OK if empty if no errors actually fired in the renderer).

```bash
gh issue list --state open --limit 5 --search 'created:>=now-2m'
```
Expected: No new GitHub issues.

**Revert `app/api_server.py`:** `git -C . checkout -- app/api_server.py`

- [ ] **Step 3: AbortError on navigation**

Start the app normally (`npm --prefix app start`). Wait for the UI to mount.

In the app, click Scan to start `/device/scan`. Before it completes (within ~1s), navigate to a different view (Settings) or close the app window.

```bash
tail -5 "$HOME/Library/Application Support/Plaud V2/diagnostics.log"
```
Expected: One JSON line with an abort-style message.

```bash
gh issue list --state open --limit 5 --search 'created:>=now-2m'
```
Expected: No new GitHub issues.

- [ ] **Step 4: Real backend error**

Edit `app/routes/transcribe.py` (or whichever file has the `@bp.route("/transcribe")` handler) to raise an exception on entry. Add at the top of the handler:
```python
raise RuntimeError("planned test failure for verification")
```

Trigger transcription from the UI (open any recording, click Re-transcribe).

```bash
gh issue list --state open --limit 5 --search 'created:>=now-2m label:auto-bug'
```
Expected: One new issue (or comment on existing) titled with "[Auto] [DEV]" and the test failure message.

```bash
tail -5 "$HOME/Library/Application Support/Plaud V2/diagnostics.log"
```
Expected: A JSON line with the same error message.

**Revert the transcribe.py change:** `git -C . checkout -- app/routes/transcribe.py`

If a test issue was created, close it: `gh issue close <number> --comment "test from plan task 10 step 4"`.

- [ ] **Step 5: Mid-session Python crash**

Start the app normally. Once UI is up:

```bash
pkill -f "api_server.py"
```

Wait 5–10 seconds (background poller flips state to 'down'). Click Scan in the UI — toast appears, fetch fails.

```bash
tail -3 "$HOME/Library/Application Support/Plaud V2/diagnostics.log"
```
Expected: New entries for the failed scan call.

```bash
gh issue list --state open --limit 5 --search 'created:>=now-2m label:auto-bug'
```
Expected: No new issues (suppressed by backend-down filter).

Restart the app (Cmd+R or quit + relaunch) to recover.

- [ ] **Step 6: Log rotation**

```bash
LOG="$HOME/Library/Application Support/Plaud V2/diagnostics.log"
# Pre-write a 1.1 MB file
yes 'rotation-padding' | head -c 1200000 > "$LOG"
ls -la "$LOG"
```

Now trigger one suppressed event (start the app, do a Cmd+R while a scan is in flight, or use the same trick from Step 3).

```bash
ls -la "$LOG" "$LOG.1"
```
Expected: `$LOG.1` now exists (~1.1 MB), `$LOG` is small (just the new entry).

- [ ] **Step 7: Final clean state check**

Run: `git -C . status`
Expected: Clean working tree (no leftover test edits).

Run: `git -C . log --oneline | head -10`
Expected: Recent commits from Tasks 1–9 visible.

- [ ] **Step 8: (Optional) Tag a release if shipping**

Out of scope for this plan, but if you're ready to release the fixes: bump `package.json` version, update changelog, tag, and push. The four open auto-bug issues (#78–#81) can be closed with a pointer to the release tag once verified in the field.

---

## Followups (out of scope for this plan)

- Comment-and-close #78, #79, #80, #81 with a pointer to the release tag once shipped.
- Watch local diagnostics on the maintainer's device for `/device/scan-known` patterns; revisit if a real signal emerges.
- The renderer's `init()` health-poll loop (`renderer.js:783-803`) is now redundant with the health-gate. Removing it is a small cleanup that doesn't affect correctness — leave it as defense-in-depth or pull it out in a follow-up commit.
- The `_pythonEnvCache` and "Open diagnostics log" button-handler path in `health-gate.js` use a placeholder `__USER_DATA_DIR__` lookup. If the button proves useful, wire a proper `getDiagnosticsLogPath` IPC/command — currently the click does a best-effort `openExternal('file:///')`.

---

## Self-Review

**Spec coverage check:**

| Spec section | Implemented in |
|---|---|
| Local diagnostics log under `<userData>/diagnostics.log` | Task 2 (Electron), Task 5 (Tauri) |
| 1 MB rotation, one backup | Task 2 + Task 5 |
| `_isAbortError` predicate | Task 3 + Task 5 |
| `_isBackendDown` / backend-health filter | Task 4 + Task 5 |
| Reporter routes through filters; suppressed events still logged | Task 3 + Task 5 |
| Health-gate splash polling `/health` every 250 ms (cap 30 s) | Task 6 |
| "Backend not responding" screen with Retry + Open log | Task 6 |
| 5 s background poller pushes health to main | Task 7 |
| `/device/connect` → `NO_TIMEOUT_ENDPOINTS` | Task 1 |
| Wrap transcript-view fetches | Task 9 (note: spec said line 837; that line was already wrapped — Task 9 wraps the actual unwrapped lines 32, 213, 675, 705, 751) |
| Remove 15s startup grace | Task 8 |
| `/health` endpoint exists | Already present at `app/routes/core.py:30` — no task needed |
| Manual verification matrix (6 cases) | Task 10 |

All spec requirements have an owning task. The transcript-view divergence from the spec's line number is documented in Task 9 (line 837 was already correctly wrapped at investigation time).

**Placeholder scan:** Searched for "TBD", "TODO", "implement later", "fill in details" — none present in plan content. The single `__USER_DATA_DIR__` placeholder is in the new health-gate.js code but is documented as a follow-up in the Followups section.

**Type/name consistency:**
- `_appendDiagnostic({context, message, stack, fingerprint})` matches across Electron and Tauri usages.
- `setBackendHealth(state)` / `set_backend_health(state)` — IPC and Tauri command names consistent.
- `_isAbortError(message)` / `is_abort_error(message)` — naming convention consistent within each runtime.
- `BACKEND_HEALTH` static / `_backendHealth` variable — values are always one of `'up' | 'down' | 'unknown'`.

No type or name mismatches found.
