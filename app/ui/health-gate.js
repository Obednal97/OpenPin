/* ============================================================
   Plaud V2 — Health Gate (startup state machine)
   ----------------------------------------------------------------
   Mounted before the main UI. Covers the window with a splash until
   the Python backend is ready, then reveals the app and starts the
   background /health poller.

   The Rust shell starts the backend on a background thread after the
   window is shown and reports progress through the
   `get_startup_status` command:
     { phase: 'starting' | 'ready' | 'error', message, elapsed_ms,
       attempt, error: { code, title, friendly, hint, detail } | null }
   A first launch can spend minutes installing Python and packages, so
   the gate waits for as long as Rust says it's still starting (up to a
   generous ceiling) and shows Rust's progress message. It shows the
   failure screen only on a real error, when Rust says ready but the
   page can't reach /health, or at the ceiling.

   API (window.plaudStartup):
     waitForBackend() -> Promise<boolean>  true once ready; false when
                                           the failure screen is shown
     onRetry(fn)                           Retry on the failure screen
                                           calls fn (renderer: init)

   Without get_startup_status (an older shell), it falls back to
   polling /health for up to a minute.
   ============================================================ */

(function () {
  const TICK_FAST_MS = 250;
  const TICK_SLOW_MS = 1000;
  const FAST_PHASE_MS = 10000; // poll quickly at first, then back off
  const SLOW_HINT_MS = 8000; // after this, explain that first launch is slow
  const STARTING_CEILING_MS = 10 * 60 * 1000; // Rust still "starting"
  const READY_GRACE_MS = 30000; // Rust "ready" but /health unreachable from the page
  const NO_STATUS_CEILING_MS = 60000; // no startup status available (older shell)
  const BG_POLL_INTERVAL_MS = 5000;

  let _bgPollerHandle = null;
  let _bgPollerStarted = false;
  let _gateRun = null; // in-flight / settled-true waitForBackend() promise
  let _retryHandler = null;
  let _lastStatus = null;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function invokeRust(cmd, args) {
    const core = window.__TAURI__ && window.__TAURI__.core;
    if (!core || typeof core.invoke !== 'function') return Promise.reject(new Error('no Tauri IPC'));
    return core.invoke(cmd, args);
  }

  /** Latest startup status from Rust, or null if the shell doesn't provide one. */
  async function readStartupStatus() {
    try {
      const st = await invokeRust('get_startup_status');
      if (st && typeof st.phase === 'string') return st;
    } catch {}
    return null;
  }

  async function pingHealthOnce() {
    try {
      const data = await window.plaud.health();
      return !!data && (data.status === 'ok' || data.ok === true);
    } catch {
      return false;
    }
  }

  // ---- Splash DOM ----

  const STYLE = `
    #health-gate-splash {
      position: fixed; inset: 0; z-index: 99999;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      gap: var(--sp-3, 12px); padding: var(--sp-8, 32px);
      background: var(--bg, #0d0d0d); color: var(--text, #f5f5f7);
      font-family: var(--font, -apple-system, system-ui, sans-serif);
      text-align: center; -webkit-user-select: none; user-select: none;
    }
    #health-gate-splash .hg-spinner {
      width: 32px; height: 32px; border-radius: 50%;
      border: 3px solid var(--border-strong, rgba(84,84,88,0.65));
      border-top-color: var(--accent, #0a84ff);
      animation: hg-spin 0.8s linear infinite; margin-bottom: var(--sp-2, 8px);
    }
    #health-gate-splash .hg-title { font-size: var(--text-lg, 16px); font-weight: var(--weight-semibold, 600); }
    #health-gate-splash .hg-msg { font-size: var(--text-sm, 13px); color: var(--text-secondary, #98989d); min-height: 1.4em; }
    #health-gate-splash .hg-hint { font-size: var(--text-xs, 11px); color: var(--text-tertiary, #636366); max-width: 360px; line-height: 1.5; }
    #health-gate-splash .hg-detail {
      font-family: var(--font-mono, ui-monospace, monospace); font-size: var(--text-xs, 11px);
      color: var(--text-tertiary, #636366); max-width: 520px; max-height: 160px; overflow: auto;
      text-align: left;
      white-space: pre-wrap; word-break: break-word; -webkit-user-select: text; user-select: text;
    }
    #health-gate-splash .hg-actions { display: flex; gap: var(--sp-2, 8px); margin-top: var(--sp-2, 8px); }
    #health-gate-splash [hidden] { display: none !important; }
    @keyframes hg-spin { to { transform: rotate(360deg); } }
  `;

  function ensureSplash() {
    let splash = document.getElementById('health-gate-splash');
    if (splash) return splash;
    if (!document.getElementById('health-gate-style')) {
      const style = document.createElement('style');
      style.id = 'health-gate-style';
      style.textContent = STYLE;
      document.head.appendChild(style);
    }
    splash = document.createElement('div');
    splash.id = 'health-gate-splash';
    // Let the window be dragged by its top edge while the splash covers the header.
    splash.setAttribute('data-tauri-drag-region', '');
    splash.innerHTML = `
      <div class="hg-spinner" id="hg-spinner"></div>
      <div class="hg-title" id="hg-title">Starting Plaud V2</div>
      <div class="hg-msg" id="hg-msg" role="status" aria-live="polite"></div>
      <div class="hg-hint" id="hg-hint" hidden></div>
      <div class="hg-detail" id="hg-detail" hidden></div>
      <div class="hg-actions" id="hg-actions" hidden>
        <button class="action-btn primary" id="hg-retry" type="button">Retry</button>
        <button class="action-btn subtle" id="hg-openlog" type="button">Open diagnostics log</button>
      </div>
    `;
    document.body.appendChild(splash);
    splash.querySelector('#hg-retry').addEventListener('click', retry);
    splash.querySelector('#hg-openlog').addEventListener('click', async () => {
      try {
        if (window.plaud && window.plaud.openDiagnosticsLog) await window.plaud.openDiagnosticsLog();
      } catch (err) {
        console.warn('[health-gate] open diagnostics log failed:', err);
      }
    });
    return splash;
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text || '';
    el.hidden = !text;
  }

  function showProgress(message, elapsedMs) {
    const splash = ensureSplash();
    splash.querySelector('#hg-spinner').hidden = false;
    splash.querySelector('#hg-actions').hidden = true;
    setText('hg-title', 'Starting Plaud V2');
    setText('hg-msg', message || 'Starting backend…');
    setText('hg-detail', '');
    setText(
      'hg-hint',
      elapsedMs >= SLOW_HINT_MS
        ? 'The first launch can take a few minutes while Plaud V2 installs what it needs. Keep this window open.'
        : '',
    );
  }

  function showFailure(err) {
    const splash = ensureSplash();
    splash.querySelector('#hg-spinner').hidden = true;
    splash.querySelector('#hg-actions').hidden = false;
    setText('hg-title', (err && err.title) || 'Backend not responding');
    setText('hg-msg', (err && err.friendly) || "Plaud V2's local server didn't start.");
    const hint = [err && err.hint, err && err.code ? `Error code: ${err.code}` : ''].filter(Boolean).join(' · ');
    setText('hg-hint', hint);
    setText('hg-detail', (err && err.detail) || '');
    const retryBtn = document.getElementById('hg-retry');
    if (retryBtn) retryBtn.focus();
  }

  function hideSplash() {
    const splash = document.getElementById('health-gate-splash');
    if (splash) splash.remove();
  }

  function pushHealth(state) {
    if (window.plaud && window.plaud.setBackendHealth) {
      window.plaud.setBackendHealth(state).catch(() => {});
    }
  }

  function startBackgroundPoller() {
    if (_bgPollerStarted) return;
    _bgPollerStarted = true;

    // Push initial 'up' state
    pushHealth('up');

    _bgPollerHandle = setInterval(async () => {
      const ok = await pingHealthOnce();
      pushHealth(ok ? 'up' : 'down');
    }, BG_POLL_INTERVAL_MS);
  }

  // ---- State machine ----

  /** One startup attempt. Resolves true when ready, false after showing the failure screen. */
  async function runGate() {
    const start = Date.now();
    let readySince = null;
    showProgress('Starting…', 0);

    for (;;) {
      const st = await readStartupStatus();
      _lastStatus = st;
      const elapsed = Date.now() - start;

      if (st && st.phase === 'error') {
        pushHealth('down');
        showFailure(st.error || {});
        return false;
      }

      // Only touch /health once Rust says the server is up (or when Rust
      // can't tell us): while it's still starting, the port may belong to
      // something else.
      if (!st || st.phase === 'ready') {
        if (await pingHealthOnce()) {
          hideSplash();
          startBackgroundPoller();
          return true;
        }
      }

      if (st && st.phase === 'ready') {
        if (readySince === null) readySince = Date.now();
        if (Date.now() - readySince > READY_GRACE_MS) {
          pushHealth('down');
          showFailure({
            code: 'WEBVIEW_UNREACHABLE',
            title: "Can't reach the backend",
            friendly: 'The local server is running, but this window can’t connect to it.',
            hint: 'Retry reloads the window.',
          });
          return false;
        }
      } else {
        readySince = null;
      }

      const ceiling = st ? STARTING_CEILING_MS : NO_STATUS_CEILING_MS;
      if (elapsed > ceiling) {
        pushHealth('down');
        showFailure({
          code: 'SERVER_TIMEOUT',
          title: 'Backend not responding',
          friendly: `Plaud V2's local server didn't start within ${Math.round(ceiling / 60000)} minute${ceiling >= 120000 ? 's' : ''}.`,
          hint: 'Retry, or open the diagnostics log if this keeps happening.',
          detail: st && st.message ? `Last step: ${st.message}` : '',
        });
        return false;
      }

      const message = st ? (st.phase === 'ready' ? 'Connecting…' : st.message) : 'Starting backend…';
      showProgress(message, elapsed);
      await sleep(elapsed < FAST_PHASE_MS ? TICK_FAST_MS : TICK_SLOW_MS);
    }
  }

  function waitForBackend() {
    if (!_gateRun) {
      _gateRun = runGate().then(
        (ok) => {
          if (!ok) _gateRun = null; // the next call starts a fresh attempt
          return ok;
        },
        (err) => {
          _gateRun = null;
          throw err;
        },
      );
    }
    return _gateRun;
  }

  async function retry() {
    const last = _lastStatus;
    showProgress('Retrying…', 0);
    // Server up but unreachable from the page: the webview's network stack
    // is wedged (seen after sleep). Reloading the page is the fix.
    if (last && last.phase === 'ready') {
      window.location.reload();
      return;
    }
    // Ask Rust to stop any half-started backend and start it again.
    if (last) {
      try {
        await invokeRust('retry_startup');
      } catch {}
    }
    if (typeof _retryHandler === 'function') {
      _retryHandler();
    } else {
      waitForBackend();
    }
  }

  window.plaudStartup = {
    waitForBackend,
    onRetry(fn) {
      _retryHandler = fn;
    },
    lastStatus: () => _lastStatus,
  };

  // tauri-bridge.js sets window.plaud synchronously during script eval,
  // before this IIFE runs. Start right away so the splash is up before
  // anything else renders; renderer.js init() awaits the same promise.
  waitForBackend();
})();
