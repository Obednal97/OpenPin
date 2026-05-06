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
      // Best-effort: open the userData parent dir; user can find diagnostics.log from there.
      // A proper getDiagnosticsLogPath IPC/command is a follow-up.
      if (window.plaud && window.plaud.openExternal) {
        window.plaud.openExternal('file:///');
      }
    });
  }

  async function mountGate() {
    const splash = buildSplash();
    const start = Date.now();

    while (Date.now() - start < POLL_TIMEOUT_MS) {
      if (await pingHealthOnce()) {
        splash.remove();
        return;
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
