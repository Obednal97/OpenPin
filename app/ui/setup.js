/* ============================================================
   Plaud V2 — First-Run Setup / Onboarding
   Shows on first launch when no device has been paired.
   ============================================================ */

let _setupOverlay = null;

function _setupEl() {
  return _setupOverlay ? _setupOverlay.querySelector('.setup-body') : null;
}

function _renderSetup(html) {
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
      <img src="assets/icon-white.png" alt="" class="app-wordmark-dark" height="56">
      <img src="assets/icon-black.png" alt="" class="app-wordmark-light" height="56">
    </div>
    <h2>Welcome to Plaud V2</h2>
    <p>Your private, local voice recorder companion. Sync, transcribe, and organise recordings from your Plaud NotePin S — no cloud required.</p>
    <button class="action-btn primary" id="setup-get-started">Get Started</button>
    <button class="setup-skip" id="setup-skip-welcome">Skip for now</button>
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
    <h2>Sign into Plaud</h2>
    <p>Your Plaud account is needed to register your device. This is a one-time setup — after this, the app works fully offline.</p>
    <button class="action-btn primary" id="setup-cloud-login">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" style="margin-right:4px;">
        <path d="M15 3h6v6M14 10l6.1-6.1M9 21H3v-6M10 14l-6.1 6.1" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      Sign in with Browser
    </button>
    <div style="margin-top:var(--sp-4);padding:var(--sp-3);background:var(--bg-input);border-radius:var(--radius-sm);text-align:left;">
      <div style="font-size:var(--text-xs);font-weight:var(--weight-semibold);margin-bottom:var(--sp-1);">Having trouble with browser login?</div>
      <div style="font-size:var(--text-xs);color:var(--text-tertiary);line-height:1.5;">
        If the browser opens the official Plaud app instead:<br>
        1. Open <strong>web.plaud.ai</strong> in your browser and sign in<br>
        2. Open Developer Tools (Cmd+Option+I) → Application tab<br>
        3. In the left sidebar: Local Storage → plaud.ai<br>
        4. Find <strong>tokenstr</strong> and copy its value<br>
        5. Paste it below (works with or without the "Bearer" prefix)
      </div>
      <div class="token-input-row" style="margin-top:var(--sp-2);">
        <input type="password" id="setup-cloud-token" class="settings-input" placeholder="Paste bearer token...">
      </div>
      <button class="action-btn subtle" id="setup-cloud-token-btn" style="margin-top:var(--sp-1);width:100%;justify-content:center;font-size:var(--text-xs);">Login with Token</button>
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
          <h2>Waiting for Login</h2>
          <p>Sign into your Plaud account in the browser. You'll be redirected back automatically.</p>
          <div style="margin-top:var(--sp-4);padding:var(--sp-3);background:var(--bg-input);border-radius:var(--radius-sm);text-align:left;">
            <div style="font-size:var(--text-xs);font-weight:var(--weight-semibold);margin-bottom:var(--sp-1);">Redirected to the official Plaud app instead?</div>
            <div style="font-size:var(--text-xs);color:var(--text-tertiary);line-height:1.5;">
              1. Open <strong>web.plaud.ai</strong> in your browser and sign in<br>
              2. Open Developer Tools (Cmd+Option+I) → Application tab<br>
              3. In the left sidebar: Local Storage → plaud.ai<br>
              4. Find <strong>tokenstr</strong> and copy its value<br>
              5. Paste it below (works with or without the "Bearer" prefix)
            </div>
            <div class="token-input-row" style="margin-top:var(--sp-2);">
              <input type="password" id="setup-cloud-token-waiting" class="settings-input" placeholder="Paste bearer token...">
            </div>
            <button class="action-btn subtle" id="setup-cloud-token-btn-waiting" style="margin-top:var(--sp-1);width:100%;justify-content:center;font-size:var(--text-xs);">Login with Token</button>
          </div>
          <button class="setup-skip" id="setup-cancel-cloud" style="margin-top:var(--sp-2);">Cancel</button>
        `);

        $('#setup-cloud-token-btn-waiting').addEventListener('click', () => _handleTokenLogin($('#setup-cloud-token-waiting').value));
        $('#setup-cancel-cloud').addEventListener('click', _showCloudLogin);

        // Listen for OAuth callback
        const origCallback = window._setupOAuthHandler;
        window._setupOAuthHandler = async (authCode) => {
          if (!authCode) return;
          try {
            const res = await window.plaud.cloudExchangeToken(authCode, app.pendingDesktopUuid || '');
            if (res.logged_in) {
              app.cloudState = { logged_in: true, user: res.user };
              toast('Plaud account connected!', 'success');
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

async function _handleTokenLogin(token) {
  token = (token || '').trim();
  // Strip "Bearer " or "bearer " prefix if pasted from local storage
  if (token.toLowerCase().startsWith('bearer ')) {
    token = token.slice(7).trim();
  }
  if (!token) { toast('Please paste a token', 'error'); return; }
  try {
    const res = await window.plaud.cloudLoginToken(token, 'eu');
    if (res.logged_in) {
      app.cloudState = { logged_in: true, user: res.user };
      toast('Plaud account connected!', 'success');
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
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z" stroke="currentColor" stroke-width="1.8"/>
        <circle cx="8.5" cy="10.5" r="1.5" fill="currentColor"/>
        <circle cx="15.5" cy="10.5" r="1.5" fill="currentColor"/>
        <path d="M8.5 14.5c0 0 1.5 2 3.5 2s3.5-2 3.5-2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
    </div>
    <h2>Bluetooth Access</h2>
    <p>Plaud V2 uses Bluetooth to find and communicate with your NotePin S device. macOS will ask for your permission when we start scanning.</p>
    <button class="action-btn primary" id="setup-enable-bt">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" style="margin-right:4px;">
        <path d="M6.5 6.5l11 11M17.5 6.5L12 12l5.5-5.5zM12 1v22M6.5 17.5L12 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      Enable &amp; Scan for Device
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
    <h2>Scanning for Devices</h2>
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
    const name = escapeHtml(d.name || 'Plaud Device');
    const sn = d.serial_number ? `SN: ${escapeHtml(d.serial_number)}` : '';
    return `<div class="setup-device-row" data-addr="${escapeHtml(d.address)}" data-sn="${escapeHtml(d.serial_number || '')}" data-name="${name}">
      <div>
        <div class="setup-device-name">${name}</div>
        <div class="setup-device-meta">${sn}</div>
      </div>
      <div class="setup-device-rssi"><div class="dm-signal" title="${rssi} dBm">${bars}</div></div>
    </div>`;
  }).join('');

  _renderSetup(`
    <h2>Device${devices.length > 1 ? 's' : ''} Found</h2>
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
    <h2>No Devices Found</h2>
    <p>${errorMsg ? escapeHtml(errorMsg) : 'Could not find a Plaud device nearby.'}</p>
    <p class="setup-hint">
      Check that your Plaud NotePin S is:<br>
      &bull; Nearby (within a few feet)<br>
      &bull; Charged and powered on<br>
      &bull; Not connected to the Plaud phone app
    </p>
    <button class="action-btn primary" id="setup-retry">Try Again</button>
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
      _showSuccess(result.device_name || name);
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
    <h2>Connection Failed</h2>
    <p>${escapeHtml(error)}</p>
    <button class="action-btn primary" id="setup-retry-connect">Try Again</button>
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
    <h2>About You</h2>
    <p>Help the AI personalise transcriptions and summaries for you. You can always change this later in Settings.</p>
    <div class="setup-form" style="width:100%;">
      <div class="setup-field">
        <label class="settings-label">Your Name</label>
        <input type="text" id="setup-name" class="settings-input" style="width:100%;box-sizing:border-box;" placeholder="e.g. John, John Doe, Johnny">
      </div>
      <div class="setup-field">
        <label class="settings-label">Job Function</label>
        <input type="text" id="setup-job" class="settings-input" style="width:100%;box-sizing:border-box;" placeholder="e.g. Product Manager, Software Engineer">
      </div>
      <div class="setup-field">
        <label class="settings-label">Industry</label>
        <input type="text" id="setup-industry" class="settings-input" style="width:100%;box-sizing:border-box;" placeholder="e.g. Technology, Healthcare, Finance">
      </div>
      <div class="setup-field">
        <label class="settings-label">Seniority</label>
        <select id="setup-seniority" class="settings-select" style="width:100%;box-sizing:border-box;">
          <option value="">Not set</option>
          <option value="individual">Individual Contributor</option>
          <option value="lead">Team Lead</option>
          <option value="manager">Manager</option>
          <option value="director">Director</option>
          <option value="vp">VP</option>
          <option value="c-level">C-Level / Founder</option>
        </select>
      </div>
      <div class="setup-field">
        <label class="settings-label">Primary Use</label>
        <input type="text" id="setup-usecases" class="settings-input" style="width:100%;box-sizing:border-box;" placeholder="e.g. Meeting notes, interviews, brainstorming">
      </div>
    </div>
    <button class="action-btn primary" id="setup-profile-save">Get Started</button>
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
}

async function completeSetup() {
  _closeSetup();
  // Resume normal init — update status, start polling
  await updateDeviceStatus();
  await loadRecordings();
  app.pollInterval = setInterval(updateDeviceStatus, 10000);
  _startBgPoll();

  // Auto-sync after first pairing — check for recordings on device
  try {
    const check = await window.plaud.checkNew();
    if (check.new_files > 0) {
      toast(`${check.new_files} recording(s) found, syncing...`, 'info');
      showProgress('Syncing recordings...', 20);
      app.syncing = true;
      try {
        const result = await window.plaud.sync();
        if (!result.error) {
          const newFiles = (result.files || []).filter((f) => f.status === 'new');
          if (newFiles.length > 0) {
            toast(`Synced ${newFiles.length} recording(s)`, 'success');
            // Auto-transcribe
            for (let i = 0; i < newFiles.length; i++) {
              const f = newFiles[i];
              if (f.ogg_path) {
                showProgress(`Transcribing ${i + 1}/${newFiles.length}...`, 60 + (30 * i) / newFiles.length);
                try {
                  await window.plaud.transcribe(
                    f.ogg_path,
                    app.settings.model || 'small',
                    app.settings.diarize || false,
                    app.settings.language || 'auto',
                  );
                } catch {}
              }
            }
            toast(`Transcribed ${newFiles.length} recording(s)`, 'success');
          }
          await loadRecordings();
        }
      } finally {
        app.syncing = false;
        hideProgress();
        await updateDeviceStatus();
      }
    }
  } catch {}
}

function _closeSetup() {
  if (!_setupOverlay) return;
  _setupOverlay.classList.remove('visible');
  _setupOverlay.addEventListener('transitionend', function handler() {
    _setupOverlay.remove();
    _setupOverlay = null;
  }, { once: true });
}
