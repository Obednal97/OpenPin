/* ============================================================
   Open Pin — Troubleshoot connection (Settings › Device)
   The backend (app/routes/troubleshoot.py) runs the checks and writes
   every word of advice; this file only shows them, collects the answers
   and retries the connection. Everything lives inside one function so it
   adds a single name to the shared global scope.
   ============================================================ */

/**
 * Open the troubleshooter.
 *   onConnected(deviceName)  called after a successful "Try again" (default: refresh Settings › Device)
 *   onSignIn()               where "Sign in to Plaud" goes (default: Settings › Accounts & storage)
 */
function openConnectionTroubleshooter({ onConnected, onSignIn } = {}) {
  if (document.getElementById('ct-overlay')) return;
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay hidden';
  overlay.id = 'ct-overlay';
  overlay.innerHTML = `
    <div class="modal-panel ct-panel" role="dialog" aria-labelledby="ct-title">
      <div class="modal-header">
        <h2 id="ct-title">Troubleshoot connection</h2>
        <button class="icon-btn" id="ct-close" title="Close">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M1 1l12 12M13 1L1 13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        </button>
      </div>
      <div class="modal-body" id="ct-body"></div>
      <div class="modal-footer">
        <button class="action-btn subtle" id="ct-report">Export bug report…</button>
        <span class="ct-spacer"></span>
        <button class="action-btn subtle" id="ct-recheck">Check again</button>
        <button class="action-btn primary" id="ct-retry">Try again</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.classList.remove('hidden');
  overlay.offsetHeight; // force reflow
  overlay.classList.add('visible');

  const body = overlay.querySelector('#ct-body');
  const buttons = ['#ct-report', '#ct-recheck', '#ct-retry'].map((id) => overlay.querySelector(id));
  const answers = {};
  let last = null; // the backend's last reply: { facts, checks, questions, findings, summary }
  let closed = false;

  const close = () => {
    closed = true;
    overlay.remove();
  };
  const setBusy = (text) => {
    buttons.forEach((b) => (b.disabled = !!text));
    if (text) body.innerHTML = `<div class="dm-scanning">${escapeHtml(text)}</div>`;
  };

  function render() {
    const checks = last.checks
      .map(
        (c) => `<div class="ct-check">
          <span class="ct-dot ct-${escapeHtml(c.state)}" aria-hidden="true"></span>
          <div><div class="ct-check-label">${escapeHtml(c.label)}</div>
          <div class="ct-check-detail">${escapeHtml(c.detail)}</div></div>
        </div>`
      )
      .join('');
    const questions = last.questions
      .map(
        (q) => `<div class="ct-question">
          <div class="ct-question-text">${escapeHtml(q.text)}</div>
          <div class="ct-options">${q.options
            .map(
              ([value, label]) =>
                `<button class="ct-option ${answers[q.id] === value ? 'active' : ''}" data-q="${escapeHtml(q.id)}" data-a="${escapeHtml(value)}" aria-pressed="${answers[q.id] === value}">${escapeHtml(label)}</button>`
            )
            .join('')}</div>
        </div>`
      )
      .join('');
    const actionLabels = { sign_in: 'Sign in to Plaud', import_pairing: 'Import pairing…' };
    const findings = last.findings
      .map(
        (f) => `<div class="ct-finding">
          <div class="ct-finding-title">${escapeHtml(f.title)}</div>
          <ol>${f.steps.map((s) => `<li>${escapeHtml(s)}</li>`).join('')}</ol>
          ${actionLabels[f.action] ? `<button class="action-btn subtle ct-action" data-action="${escapeHtml(f.action)}">${actionLabels[f.action]}</button>` : ''}
        </div>`
      )
      .join('');
    const scrollTop = body.scrollTop;
    body.innerHTML = `
      ${questions ? `<h4 class="ct-heading">A couple of questions</h4>${questions}` : ''}
      <h4 class="ct-heading">What to do</h4>${findings}
      <h4 class="ct-heading">What Open Pin checked</h4>${checks}`;
    body.scrollTop = scrollTop;
    overlay.querySelector('#ct-retry').classList.toggle('hidden', !!last.facts.connected);
  }

  // No arguments: run the checks again (a scan, several seconds). With the
  // earlier facts: only re-read them with the answers, which is instant.
  async function check(facts) {
    if (!facts) setBusy('Checking Bluetooth, your NotePin and your Plaud account');
    try {
      const reply = await window.plaud.troubleshootConnection(facts ? { facts, answers } : { answers });
      if (closed) return;
      if (!reply || !reply.findings) throw new Error((reply && reply.error) || 'no answer from the app');
      last = reply;
      setBusy('');
      render();
    } catch (err) {
      if (closed) return;
      setBusy('');
      body.innerHTML = `<p class="settings-desc">Couldn't run the checks: ${escapeHtml(err.message || String(err))}. Choose Check again, or export a bug report.</p>`;
    }
  }

  async function retry() {
    setBusy('Looking for your NotePin and connecting');
    let conn = null;
    try {
      const scan = await window.plaud.scan();
      if (scan.found) {
        if (scan.serial_number) {
          // Same as the Scan buttons: fetch this account's pairing credentials first.
          try {
            await window.plaud.cloudBindDevice(scan.serial_number, scan.name);
            await window.plaud.cloudSnSign(scan.serial_number);
          } catch {}
        }
        conn = await window.plaud.connect(scan.address, scan.serial_number);
      }
    } catch {}
    if (closed) return;
    if (!conn || !conn.connected) return check(); // shows why it failed this time
    const name = prettyDeviceName(conn.device_name) || 'Plaud NotePin S';
    toast(`Connected to ${name}`, 'success');
    close();
    if (onConnected) onConnected(name);
    else {
      await updateDeviceStatus();
      renderDeviceManager();
    }
  }

  overlay.querySelector('#ct-close').addEventListener('click', close);
  overlay.querySelector('#ct-recheck').addEventListener('click', () => check());
  overlay.querySelector('#ct-retry').addEventListener('click', retry);
  overlay.querySelector('#ct-report').addEventListener('click', () => {
    const summary = last ? last.summary : '';
    close();
    openBugReport();
    if (summary) $('#bug-actual').value = summary;
  });
  body.addEventListener('click', async (e) => {
    const option = e.target.closest('.ct-option');
    if (option && last) {
      answers[option.dataset.q] = option.dataset.a;
      return check(last.facts);
    }
    const action = e.target.closest('.ct-action');
    if (!action) return;
    if (action.dataset.action === 'import_pairing') {
      await importPairingFile();
      if (!closed) check();
    } else {
      close();
      if (onSignIn) onSignIn();
      else showSettingsTab('accounts');
    }
  });

  check();
}
