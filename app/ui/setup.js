/* ============================================================
   Open Pin — First-Run Setup / Onboarding
   Shows on first launch when no device has been paired.
   ============================================================ */

let _setupOverlay = null;

function _setupEl() {
  return _setupOverlay ? _setupOverlay.querySelector('.setup-body') : null;
}

function _renderSetup(html) {
  // Each step replaces the whole body. A sign-in link belongs to the
  // "Waiting for Login" step only, which sets the handler again after rendering;
  // once the user moves on (Cancel, token login, any other step) a late link goes
  // to the Settings flow in renderer.js instead.
  window._setupOAuthHandler = null;
  const body = _setupEl();
  if (body) body.innerHTML = html;
}

function _showOverlay() {
  if (!_setupOverlay) return;
  _setupOverlay.classList.remove('hidden');
  _setupOverlay.offsetHeight; // force reflow
  _setupOverlay.classList.add('visible');
}

// ---- Step 1: Welcome ----

function showSetupWelcome() {
  // Create overlay + panel dynamically (one-shot flow)
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay hidden';
  overlay.id = 'setup-overlay';
  overlay.innerHTML = `
    <div class="modal-panel" style="width: 420px;">
      <div class="setup-body"></div>
    </div>
  `;
  document.body.appendChild(overlay);
  _setupOverlay = overlay;

  _renderSetup(`
    <div class="empty-icon">
      <img src="assets/icon-white.svg" alt="" class="app-wordmark-dark" height="56">
      <img src="assets/icon-black.svg" alt="" class="app-wordmark-light" height="56">
    </div>
    <h2>Welcome to Open Pin</h2>
    <p>Your private, local voice recorder companion. Sync, transcribe, and organise recordings from your Plaud NotePin S — no cloud required.</p>
    <button class="action-btn primary" id="setup-get-started">Get started</button>
    <button class="setup-skip" id="setup-skip-welcome">Skip for now</button>
    <p class="setup-disclaimer">Open Pin is an independent project. It isn't made by, affiliated with or endorsed by PLAUD. Plaud and NotePin are trademarks of their owners. Use it with your own NotePin and your own Plaud account, at your own risk.</p>
  `);

  _showOverlay();

  $('#setup-get-started').addEventListener('click', async () => {
    // Check if we're already logged in (token auto-detected from official Plaud app at startup)
    try {
      const status = await window.plaud.cloudStatus();
      if (status.logged_in) {
        app.cloudState = { logged_in: true, user: status.user };
        toast('Plaud account detected automatically!', 'success');
        _showPermissions();
        return;
      }
    } catch {}
    _showCloudLogin();
  });
  $('#setup-skip-welcome').addEventListener('click', dismissSetup);
}

// ---- Step 2: Plaud Cloud Login ----

function _showCloudLogin() {
  _renderSetup(`
    <div class="setup-perm-icon">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
        <path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </div>
    <h2>Sign in to Plaud</h2>
    <p>Your Plaud account is needed to register your device. This is a one-time setup — after this, the app works fully offline.</p>
    <button class="action-btn primary" id="setup-cloud-login">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" style="margin-right:4px;">
        <path d="M15 3h6v6M14 10l6.1-6.1M9 21H3v-6M10 14l-6.1 6.1" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      Sign in with your browser
    </button>
    <div style="margin-top:var(--sp-4);padding:var(--sp-3);background:var(--bg-input);border-radius:var(--radius-sm);text-align:left;">
      <div style="font-size:var(--text-xs);font-weight:var(--weight-semibold);margin-bottom:var(--sp-1);">Having trouble with browser login?</div>
      <div style="font-size:var(--text-xs);color:var(--text-tertiary);line-height:1.5;">
        If the browser opens the official Plaud app instead:<br>
        1. Open <strong>web.plaud.ai</strong> in your browser and sign in<br>
        2. Open Developer Tools (Cmd+Option+I) → Application tab<br>
        3. In the left sidebar: Local Storage → plaud.ai<br>
        4. Find <strong>tokenstr</strong> and copy its value<br>
        5. Paste it below (works with or without the "Bearer" prefix)<br>
        A <strong>plaud://login</strong> sign-in link from the browser works here too.
      </div>
      <div class="token-input-row" style="margin-top:var(--sp-2);">
        <input type="password" id="setup-cloud-token" class="settings-input" placeholder="Paste the sign-in link, code or token…">
      </div>
      <button class="action-btn subtle" id="setup-cloud-token-btn" style="margin-top:var(--sp-1);width:100%;justify-content:center;font-size:var(--text-xs);">Sign in with a token</button>
    </div>
    <button class="setup-skip" id="setup-skip-cloud">Skip for now</button>
  `);

  // Browser OAuth flow
  $('#setup-cloud-login').addEventListener('click', async () => {
    try {
      const result = await window.plaud.cloudLoginUrl('eu');
      if (result.url) {
        app.pendingDesktopUuid = result.desktop_uuid;
        window.plaud.openExternal(result.url);

        _renderSetup(`
          <div class="setup-spinner"></div>
          <h2>Waiting for you to sign in</h2>
          <p>Sign into your Plaud account in the browser and click <strong>Continue to desktop</strong>. Open Pin finishes signing in by itself.</p>
          <div style="margin-top:var(--sp-4);padding:var(--sp-3);background:var(--bg-input);border-radius:var(--radius-sm);text-align:left;">
            <div style="font-size:var(--text-xs);font-weight:var(--weight-semibold);margin-bottom:var(--sp-1);">Didn't come back to Open Pin?</div>
            <div style="font-size:var(--text-xs);color:var(--text-tertiary);line-height:1.5;">
              Paste the <strong>plaud://login</strong> link the browser tried to open, or a token:<br>
              1. Open <strong>web.plaud.ai</strong> in your browser and sign in<br>
              2. Open Developer Tools (Cmd+Option+I) → Application tab<br>
              3. In the left sidebar: Local Storage → plaud.ai<br>
              4. Find <strong>tokenstr</strong> and copy its value<br>
              5. Paste it below (works with or without the "Bearer" prefix)
            </div>
            <div class="token-input-row" style="margin-top:var(--sp-2);">
              <input type="password" id="setup-cloud-token-waiting" class="settings-input" placeholder="Paste the sign-in link, code or token…">
            </div>
            <button class="action-btn subtle" id="setup-cloud-token-btn-waiting" style="margin-top:var(--sp-1);width:100%;justify-content:center;font-size:var(--text-xs);">Sign in with a token</button>
          </div>
          <button class="setup-skip" id="setup-cancel-cloud" style="margin-top:var(--sp-2);">Cancel</button>
        `);

        $('#setup-cloud-token-btn-waiting').addEventListener('click', () => _handleTokenLogin($('#setup-cloud-token-waiting').value));
        $('#setup-cancel-cloud').addEventListener('click', _showCloudLogin);

        // Plaud sign-in link (routed here by renderer.js while this step is shown)
        window._setupOAuthHandler = async (authCode) => {
          if (!authCode) return;
          try {
            const res = await window.plaud.cloudExchangeToken(authCode, app.pendingDesktopUuid || '', 'link');
            if (res.logged_in) {
              app.cloudState = { logged_in: true, user: res.user };
              toast('Plaud account connected!', 'success');
              renderCloudAccount();
              _showPermissions();
            } else {
              toast(res.error || 'Login failed', 'error');
              _showCloudLogin();
            }
          } catch (err) {
            toast(`Error: ${err.message}`, 'error');
            _showCloudLogin();
          }
          app.pendingDesktopUuid = null;
        };
      }
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    }
  });

  // Direct token login
  $('#setup-cloud-token-btn').addEventListener('click', () => _handleTokenLogin($('#setup-cloud-token').value));
  $('#setup-skip-cloud').addEventListener('click', _showPermissions);
}

// Pasted sign-in link, auth code or bearer token (see parsePlaudSignInInput in utils.js)
async function _handleTokenLogin(input) {
  try {
    const res = await submitPlaudSignInInput(input);
    if (!res) return;
    if (res.logged_in) {
      app.cloudState = { logged_in: true, user: res.user };
      app.pendingDesktopUuid = null;
      toast('Plaud account connected!', 'success');
      renderCloudAccount();
      _showPermissions();
    } else {
      toast(res.error || 'Token invalid', 'error');
    }
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  }
}

// ---- Step 3: Permissions ----

function _showPermissions() {
  _renderSetup(`
    <div class="setup-perm-icon">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M7 7l10 10-5 5V2l5 5L7 17" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </div>
    <h2>Bluetooth access</h2>
    <p>Open Pin uses Bluetooth to find and communicate with your NotePin S device. macOS will ask for your permission when we start scanning.</p>
    <button class="action-btn primary" id="setup-enable-bt">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" style="margin-right:4px;" aria-hidden="true">
        <path d="M7 7l10 10-5 5V2l5 5L7 17" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      Allow and scan for your device
    </button>
    <button class="setup-skip" id="setup-skip-perm">Skip for now</button>
  `);

  $('#setup-enable-bt').addEventListener('click', _startScan);
  $('#setup-skip-perm').addEventListener('click', dismissSetup);
}

// ---- Step 3: Scanning ----

async function _startScan() {
  _renderSetup(`
    <div class="setup-spinner"></div>
    <h2>Looking for devices</h2>
    <p>Looking for nearby Plaud devices...</p>
    <p class="setup-hint">Make sure your Plaud is nearby, charged, and not connected to the phone app.</p>
  `);

  try {
    const result = await window.plaud.scanAll();
    const devices = result.devices || [];
    if (devices.length > 0) {
      _showDevices(devices);
    } else {
      _showNoDevices();
    }
  } catch (err) {
    _showNoDevices(err.message);
  }
}

// ---- Step 4a: Devices Found ----

function _showDevices(devices) {
  const rows = devices.map((d) => {
    const rssi = d.rssi || -80;
    const level = rssi > -50 ? 4 : rssi > -65 ? 3 : rssi > -80 ? 2 : 1;
    const heights = [4, 7, 10, 13];
    const bars = heights.map((h, i) =>
      `<div class="dm-signal-bar ${i < level ? 'active' : ''}" style="height:${h}px;"></div>`
    ).join('');
    const name = escapeHtml(prettyDeviceName(d.name) || 'Plaud device');
    const sn = d.serial_number ? `SN: ${escapeHtml(d.serial_number)}` : '';
    return `<div class="setup-device-row" data-addr="${escapeHtml(d.address)}" data-sn="${escapeHtml(d.serial_number || '')}" data-name="${name}">
      <div>
        <div class="setup-device-name">${name}</div>
        <div class="setup-device-meta">${sn}</div>
      </div>
      <div class="setup-device-rssi"><div class="dm-signal" title="${escapeHtml(rssi)} dBm">${bars}</div></div>
    </div>`;
  }).join('');

  _renderSetup(`
    <h2>Device${devices.length > 1 ? 's' : ''} found</h2>
    <p>Tap a device to connect.</p>
    <div class="setup-device-list">${rows}</div>
    <button class="setup-skip" id="setup-skip-devices">Skip for now</button>
  `);

  _setupEl().querySelectorAll('.setup-device-row').forEach((row) => {
    row.addEventListener('click', () => {
      const addr = row.dataset.addr;
      const sn = row.dataset.sn;
      const name = row.dataset.name;
      _connectDevice(addr, sn, name);
    });
  });

  $('#setup-skip-devices').addEventListener('click', dismissSetup);
}

// ---- Step 4b: No Devices Found ----

function _showNoDevices(errorMsg) {
  _renderSetup(`
    <div class="setup-perm-icon" style="background: var(--bg-hover); color: var(--text-tertiary);">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.8"/>
        <path d="M8 8l8 8M16 8l-8 8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
      </svg>
    </div>
    <h2>No devices found</h2>
    <p>${errorMsg ? escapeHtml(errorMsg) : 'Could not find a Plaud device nearby.'}</p>
    <p class="setup-hint">
      Check that your Plaud NotePin S is:<br>
      &bull; Nearby (within a few feet)<br>
      &bull; Charged and powered on<br>
      &bull; Not connected to the Plaud phone app
    </p>
    <button class="action-btn primary" id="setup-retry">Try again</button>
    <button class="setup-skip" id="setup-skip-notfound">Skip for now</button>
  `);

  $('#setup-retry').addEventListener('click', _startScan);
  $('#setup-skip-notfound').addEventListener('click', dismissSetup);
}

// ---- Step 5: Connecting ----

async function _connectDevice(address, serialNumber, name) {
  _renderSetup(`
    <div class="setup-spinner"></div>
    <h2>Connecting</h2>
    <p>Pairing with ${escapeHtml(name)}...</p>
  `);

  try {
    const result = await window.plaud.connect(address, serialNumber);
    if (result.connected) {
      _showSuccess(prettyDeviceName(result.device_name) || name);
    } else {
      _showConnectError(result.error || 'Connection failed', address, serialNumber, name);
    }
  } catch (err) {
    _showConnectError(err.message, address, serialNumber, name);
  }
}

function _showConnectError(error, address, serialNumber, name) {
  _renderSetup(`
    <div class="setup-perm-icon" style="background: var(--bg-hover); color: var(--text-tertiary);">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.8"/>
        <path d="M12 8v5M12 16h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
      </svg>
    </div>
    <h2>Connection failed</h2>
    <p>${escapeHtml(error)}</p>
    <button class="action-btn primary" id="setup-retry-connect">Try again</button>
    <button class="setup-skip" id="setup-skip-error">Skip for now</button>
  `);

  $('#setup-retry-connect').addEventListener('click', () => _connectDevice(address, serialNumber, name));
  $('#setup-skip-error').addEventListener('click', dismissSetup);
}

// ---- Step 6: Success ----

function _showSuccess(deviceName) {
  _renderSetup(`
    <div class="setup-check">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
        <path d="M6 12l4 4 8-8" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </div>
    <h2>Connected!</h2>
    <p>
      <strong>${escapeHtml(deviceName)}</strong><br>
      Recordings will sync automatically when your device is nearby.
    </p>
    <button class="action-btn primary" id="setup-next">Continue</button>
  `);

  $('#setup-next').addEventListener('click', _showProfileSetup);
}

// ---- Step 7: Profile Setup ----

function _showProfileSetup() {
  _renderSetup(`
    <h2>About you</h2>
    <p>Help the AI personalise transcriptions and summaries for you. You can always change this later in Settings.</p>
    <div class="setup-form" style="width:100%;">
      <div class="setup-field">
        <label class="settings-label">Your name</label>
        <input type="text" id="setup-name" class="settings-input" style="width:100%;box-sizing:border-box;" placeholder="e.g. John, John Doe, Johnny">
      </div>
      <div class="setup-field">
        <label class="settings-label">Job</label>
        <input type="text" id="setup-job" class="settings-input" style="width:100%;box-sizing:border-box;" placeholder="e.g. Product manager, software engineer">
      </div>
      <div class="setup-field">
        <label class="settings-label">Industry</label>
        <input type="text" id="setup-industry" class="settings-input" style="width:100%;box-sizing:border-box;" placeholder="e.g. Technology, healthcare, finance">
      </div>
      <div class="setup-field">
        <label class="settings-label">Seniority</label>
        <select id="setup-seniority" class="settings-select" style="width:100%;box-sizing:border-box;">
          <option value="">Not set</option>
          <option value="individual">Individual contributor</option>
          <option value="lead">Team lead</option>
          <option value="manager">Manager</option>
          <option value="director">Director</option>
          <option value="vp">VP</option>
          <option value="c-level">C-level or founder</option>
        </select>
      </div>
      <div class="setup-field">
        <label class="settings-label">What you record</label>
        <input type="text" id="setup-usecases" class="settings-input" style="width:100%;box-sizing:border-box;" placeholder="e.g. Meeting notes, interviews, brainstorming">
      </div>
    </div>
    <button class="action-btn primary" id="setup-profile-save">Get started</button>
    <button class="action-btn ghost" id="setup-profile-skip" style="margin-top:8px;">Skip for now</button>
  `);

  $('#setup-profile-save').addEventListener('click', async () => {
    const profile = {
      name: ($('#setup-name').value || '').trim(),
      job: ($('#setup-job').value || '').trim(),
      industry: ($('#setup-industry').value || '').trim(),
      seniority: ($('#setup-seniority').value || ''),
      usecases: ($('#setup-usecases').value || '').trim(),
    };
    // Only save if they filled in at least one field
    if (Object.values(profile).some(v => v)) {
      try {
        await window.plaud.saveSettings({ profile });
        if (typeof app !== 'undefined') app.settings.profile = profile;
        if (profile.name) await window.plaud.foldNamesake();
      } catch {}
    }
    completeSetup();
  });

  $('#setup-profile-skip').addEventListener('click', completeSetup);
}

// ---- Dismiss & Complete ----

async function dismissSetup() {
  // Save that user dismissed — don't show again
  try {
    await window.plaud.saveSettings({ setup_dismissed: true });
  } catch {}
  _closeSetup();
  // init() returned early to show setup, so background polling (reconnect,
  // auto-sync) only starts here — same as completeSetup().
  await updateDeviceStatus();
  _startBgPoll();
}

async function completeSetup() {
  _closeSetup();
  // Resume normal init — update status, start adaptive polling
  await updateDeviceStatus();
  await loadRecordings();
  _startBgPoll();

  // Auto-sync after first pairing — check for recordings on device
  try {
    if (app.syncing) return;
    const check = await window.plaud.checkNew();
    if (isDeviceBusyResponse(check) || app.syncing) return; // already syncing
    if (check.new_files > 0) {
      toast(`${check.new_files} recording(s) found, syncing...`, 'info');
      showProgress('Syncing recordings...', 20);
      const syncGen = _claimSync('ble');
      try {
        const result = await window.plaud.sync({ background: true });
        if (!result.error && !isDeviceBusyResponse(result)) {
          toastPartialSync(result);
          const newFiles = (result.files || []).filter((f) => f.status === 'new');
          if (newFiles.length > 0) {
            toast(`Synced ${newFiles.length} recording(s)`, 'success');
          }
          await loadRecordings();
          // First sync after pairing always transcribed: do it in the background.
          queueAutoProcess(newFiles, { always: true });
        }
      } finally {
        if (_releaseSync(syncGen)) {
          hideProgress();
          await updateDeviceStatus();
        }
      }
    }
  } catch {}
}

function _closeSetup() {
  window._setupOAuthHandler = null;
  if (!_setupOverlay) return;
  _setupOverlay.classList.remove('visible');
  _setupOverlay.addEventListener('transitionend', function handler() {
    _setupOverlay.remove();
    _setupOverlay = null;
  }, { once: true });
}
