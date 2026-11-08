/* ============================================================
   Open Pin — Renderer Process (Orchestrator)
   Loads: ui/utils.js, ui/recordings.js, ui/transcript.js, ui/settings.js
   ============================================================ */

// ---- Theme Toggle ----
function showThemeIcon(preference) {
  const icons = { light: 'theme-icon-light', dark: 'theme-icon-dark', auto: 'theme-icon-auto' };
  Object.values(icons).forEach((id) => {
    const el = document.getElementById(id);
    el.classList.add('hidden');
    el.classList.remove('entering', 'exiting');
  });
  const active = document.getElementById(icons[preference]);
  if (active) {
    active.classList.remove('hidden');
    active.classList.add('entering');
  }
}

(async () => {
  try {
    const { preference } = await window.plaud.getTheme();
    showThemeIcon(preference);
    renderThemeSetting(preference);
  } catch {}
})();

$('#theme-toggle-btn').addEventListener('click', async () => {
  try {
    const { preference } = await window.plaud.cycleTheme();
    showThemeIcon(preference);
    renderThemeSetting(preference);
    const labels = { auto: 'Auto (system)', light: 'Light mode', dark: 'Dark mode' };
    toast(labels[preference], 'info');
  } catch {}
});

// ---- Bug Report Modal ----
// "Export bug report" saves a zip to ~/Downloads (Rust: export_bug_report);
// nothing is sent anywhere. The user attaches it to a GitHub issue.
// Source repo (also in app/utils/constants.py PROJECT_URL); GitHub redirects the old slug after a rename.
const BUG_REPORT_ISSUE_URL = 'https://github.com/Obednal97/OpenPin/issues/new?template=bug_report.yml';
let _bugReportSavedPath = '';

function _bugReportShowForm() {
  _bugReportSavedPath = '';
  $('#bug-report-form').classList.remove('hidden');
  $('#bug-report-form-footer').classList.remove('hidden');
  $('#bug-report-done').classList.add('hidden');
  $('#bug-report-done-footer').classList.add('hidden');
}

function _bugReportShowDone(fileName) {
  $('#bug-report-file-name').textContent = fileName;
  $('#bug-report-form').classList.add('hidden');
  $('#bug-report-form-footer').classList.add('hidden');
  $('#bug-report-done').classList.remove('hidden');
  $('#bug-report-done-footer').classList.remove('hidden');
  $('#bug-report-done-btn').focus();
}

function openBugReport() {
  const overlay = $('#bug-report-overlay');
  _bugReportShowForm();
  overlay.classList.remove('hidden');
  overlay.offsetHeight; // force reflow
  overlay.classList.add('visible');
  $('#bug-expected').focus();
}

function closeBugReport() {
  const overlay = $('#bug-report-overlay');
  overlay.classList.remove('visible');
  overlay.addEventListener('transitionend', function handler() {
    if (!overlay.classList.contains('visible')) {
      overlay.classList.add('hidden');
      // Clear form
      $('#bug-expected').value = '';
      $('#bug-actual').value = '';
      $('#bug-steps').value = '';
    }
    overlay.removeEventListener('transitionend', handler);
  });
}

$('#bug-report-btn').addEventListener('click', openBugReport);
$('#settings-export-bug-report-btn')?.addEventListener('click', openBugReport);
$('#bug-report-close').addEventListener('click', closeBugReport);
$('#bug-report-cancel').addEventListener('click', closeBugReport);
$('#bug-report-done-btn').addEventListener('click', closeBugReport);
$('#bug-report-overlay').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeBugReport();
});

$('#bug-report-submit').addEventListener('click', async () => {
  // All three answers are optional.
  const expected = $('#bug-expected').value.trim();
  const actual = $('#bug-actual').value.trim();
  const steps = $('#bug-steps').value.trim();

  const btn = $('#bug-report-submit');
  setLoading(btn, true);

  try {
    // Checks from the backend; still export if it isn't running.
    let checks;
    try {
      checks = await window.plaud.getDiagnostics();
    } catch {
      checks = { backend: 'not running' };
    }
    const result = await window.plaud.exportBugReport(expected || null, actual || null, steps || null, checks);
    _bugReportSavedPath = result.path;
    _bugReportShowDone(result.file_name);
  } catch (err) {
    toast(`Couldn't export the bug report: ${err?.message || err}`, 'error');
  } finally {
    setLoading(btn, false);
  }
});

$('#bug-report-reveal').addEventListener('click', async () => {
  if (!_bugReportSavedPath) return;
  try {
    await window.plaud.revealInFinder(_bugReportSavedPath);
  } catch (err) {
    toast(`Couldn't show it in Finder: ${err?.message || err}`, 'error');
  }
});

$('#bug-report-github').addEventListener('click', () => {
  window.plaud.openExternal(BUG_REPORT_ISSUE_URL);
});

// ---- Settings Open/Close ----
dom.settingsBtn.addEventListener('click', () => {
  openSettings();
  loadSettings();
});

dom.settingsClose.addEventListener('click', closeSettings);
dom.settingsOverlay.addEventListener('click', closeSettings);

// Clicking the device status chip opens Settings > Device tab
dom.statusEl.addEventListener('click', () => {
  openSettings();
  loadSettings();
  showSettingsTab('device');
});

// ---- Main Actions ----
dom.scanBtn.addEventListener('click', async () => {
  setLoading(dom.scanBtn, true);
  dom.statusEl.className = 'device-status scanning';
  dom.statusText.textContent = 'Scanning...';

  try {
    toast('Scanning for Plaud NotePin S...', 'info');
    const result = await window.plaud.scan();

    if (result.found) {
      toast(`Found ${prettyDeviceName(result.name)}`, 'success');
      dom.statusText.textContent = 'Connecting...';

      // Auto-bind to cloud if logged in (gets per-device BIND_TOKEN + SN_SIG)
      if (result.serial_number) {
        try {
          await window.plaud.cloudBindDevice(result.serial_number, result.name);
          await window.plaud.cloudSnSign(result.serial_number);
        } catch {}
      }
      const conn = await window.plaud.connect(result.address, result.serial_number);
      if (conn.connected) {
        toast(`Connected to ${prettyDeviceName(conn.device_name) || 'Plaud NotePin S'}`, 'success');
        await updateDeviceStatus();
      } else {
        toast(`Connection failed: ${conn.error || 'Unknown error'}`, 'error');
        dom.statusEl.className = 'device-status disconnected';
        dom.statusText.textContent = 'Disconnected';
      }
    } else {
      toast('Device not found. Make sure it is nearby and awake.', 'error');
      dom.statusEl.className = 'device-status disconnected';
      dom.statusText.textContent = 'Disconnected';
    }
  } catch (err) {
    toast(`Scan error: ${err.message}`, 'error');
    dom.statusEl.className = 'device-status disconnected';
    dom.statusText.textContent = 'Disconnected';
  } finally {
    setLoading(dom.scanBtn, false);
  }
});

// While a sync runs the Sync button just reads "Syncing…" (clicking it says
// "Already syncing"). During a Bluetooth sync a separate, secondary
// "Switch to WiFi" button appears; it always goes through the WiFi
// confirmation modal (WiFi transfer disconnects the Mac from the internet).
function setSyncButtonMode(mode) {
  const btn = dom.syncBtn;
  const switchBtn = $('#switch-wifi-btn');
  const syncing = mode === 'syncing';
  if (btn) {
    const label = btn.querySelector('span');
    if (label) label.textContent = syncing ? 'Syncing…' : 'Sync recordings';
    btn.classList.toggle('is-syncing', syncing);
    btn.title = syncing ? 'A sync is in progress' : '';
  }
  if (switchBtn) {
    const showSwitch = syncing && app.syncKind === 'ble' && btn && !btn.classList.contains('hidden');
    switchBtn.classList.toggle('hidden', !showSwitch);
  }
}

// Sync ownership. Every sync flow (button, tray, background poll, wake, setup,
// WiFi) claims a generation; only the current owner may clear app.syncing and
// reset the sync UI when it finishes. This stops an aborted Bluetooth sync from
// clearing the "syncing" state of the WiFi transfer that replaced it.
function _claimSync(kind) {
  app.syncGen = (app.syncGen || 0) + 1;
  app.syncing = true;
  app.syncKind = kind;
  setSyncButtonMode('syncing');
  return app.syncGen;
}

function _releaseSync(gen) {
  if (app.syncGen !== gen) return false;
  app.syncing = false;
  app.syncKind = null;
  setSyncButtonMode('sync');
  return true;
}

// Unified sync entry point — header Sync button, tray "Sync Device" and
// Settings > Device sync. WiFi size check, BLE transfer, transcription,
// auto-summarise, cloud sync. Never starts a WiFi transfer without the
// confirmation modal (auto-confirmed only when "Prefer WiFi" is on).
async function performSync(triggerBtn) {
  if (app.syncing) {
    toast('Already syncing', 'info');
    return;
  }

  const btn = triggerBtn || dom.syncBtn;
  setLoading(btn, true);

  // A sync this window didn't start may already be running server-side
  // (background poll, setup, a reload mid-sync). Don't start a second one.
  try {
    const s = await window.plaud.syncStatus();
    if (s && s.active) {
      setLoading(btn, false);
      toast('Already syncing', 'info');
      return;
    }
  } catch {}

  // Check file sizes first — offer WiFi for large transfers
  try {
    const check = await window.plaud.checkNew();
    if (isDeviceBusyResponse(check)) {
      setLoading(btn, false);
      toast('Already syncing', 'info');
      return;
    }
    if (check.new_files > 0 && check.total_size > WIFI_SIZE_THRESHOLD) {
      setLoading(btn, false);
      if (app.settings.auto_prefer_wifi) {
        startWifiSync({ auto: true });
      } else {
        offerWifiSync(check.total_size, check.new_files, false);
      }
      return;
    }
  } catch {}

  if (app.syncing) {
    // Something else claimed the device while we were checking
    setLoading(btn, false);
    toast('Already syncing', 'info');
    return;
  }

  const gen = _claimSync('ble');
  showProgress('Checking the device for new recordings…', 0);
  toast('Syncing recordings from device...', 'info');
  setLoading(btn, false);

  // Poll BLE sync progress so the UI updates per-file instead of sitting frozen
  // on one blocking call. setTimeout is OS-throttled while the window is in the
  // background, but the transfer keeps running (App Nap is suppressed backend-side);
  // the bar catches up on refocus. The bar is the download itself: the backend
  // counts only the recordings this Mac is missing ("Syncing recording 1 of 2")
  // and moves the percentage with their bytes. Transcription runs afterwards in
  // the background, with its own badges.
  let _syncPollActive = true;
  (async () => {
    while (_syncPollActive) {
      try {
        const s = await window.plaud.syncStatus();
        if (_syncPollActive && app.syncGen === gen && s && s.active && s.message) {
          showProgress(s.message, s.percent || 0);
        }
      } catch {}
      await new Promise((r) => setTimeout(r, 1000));
    }
  })();

  try {
    let result = await window.plaud.sync();
    // A transient post-sleep connection flap can fail the first attempt with
    // "Not connected" even though the device is in range. Verify the link
    // (which resets a stale-dead connection), reconnect if needed, and retry
    // once before surfacing an error to the user.
    if (result.error && !isDeviceBusyResponse(result) && /not connected/i.test(result.error)) {
      try {
        const live = await window.plaud.verifyConnection();
        if (!live.connected) {
          const scan = await window.plaud.scanKnown();
          if (scan.found) await window.plaud.connect(scan.address, scan.serial_number);
        }
        result = await window.plaud.sync();
      } catch {}
    }
    _syncPollActive = false;
    const superseded = app.syncGen !== gen; // user switched to WiFi mid-transfer
    if (isDeviceBusyResponse(result)) {
      toast('Already syncing', 'info');
    } else if (result.error) {
      if (!superseded) toast(`Sync failed: ${result.error}`, 'error');
    } else {
      const newFiles = (result.files || []).filter((f) => f.status === 'new');
      const total = (result.files || []).length;
      toast(`Sync complete: ${newFiles.length} new, ${total} total`, 'success');
      toastPartialSync(result);

      await loadRecordings();
      await loadSettings();
      // Transcribed in the background: the sync is over once the files are here.
      // (A manual sync always transcribed, whatever "Auto-transcribe" says.)
      queueAutoProcess(newFiles, { always: true });

      const syncDir = app.settings.cloud_sync_direction || 'download';
      if (syncDir === 'twoway' && app.cloudState.logged_in) {
        toast('Uploading to Plaud Cloud...', 'info');
        try {
          const cloudResult = await window.plaud.cloudSync('twoway');
          const ul = cloudResult.upload || {};
          const uploaded = (ul.uploaded || []).filter((u) => u.status === 'uploaded');
          if (uploaded.length > 0) {
            toast(`Uploaded ${uploaded.length} recording(s) to cloud`, 'success');
          }
        } catch (err) {
          toast(`Cloud upload error: ${err.message}`, 'error');
        }
      }
    }
  } catch (err) {
    if (app.syncGen === gen) toast(`Sync error: ${err.message}`, 'error');
  } finally {
    _syncPollActive = false;
    setLoading(btn, false);
    setLoading(dom.syncBtn, false);
    if (_releaseSync(gen)) {
      hideProgress();
      await updateDeviceStatus();
    }
  }
}

dom.syncBtn.addEventListener('click', () => performSync(dom.syncBtn));

// Explicit, secondary "Switch to WiFi" (only visible during a Bluetooth sync).
// Opens the WiFi confirmation; the BLE transfer is only aborted on Start.
$('#switch-wifi-btn').addEventListener('click', () => startWifiSync());

// Tray "Sync Device" menu item routes here so it behaves identically to the
// in-app Sync button (progress UI, transcription, reload) instead of poking the
// backend directly. Rust shows the window before emitting this event. During a
// sync it just toasts "Already syncing".
window.plaud.onTraySync(() => performSync(dom.syncBtn));

dom.disconnectBtn.addEventListener('click', async () => {
  try {
    await window.plaud.disconnect();
    toast('Disconnected', 'info');
  } catch {}
  await updateDeviceStatus();
});

// Back button
$('#transcript-back').addEventListener('click', closeTranscript);

// Select mode
$('#select-toggle').addEventListener('click', () => {
  app.selectMode ? exitSelectMode() : enterSelectMode();
});

// Select-all acts only on the recordings visible under the current search/tag filter
$('#select-all-btn').addEventListener('click', toggleSelectAllVisible);

$('#delete-selected-btn').addEventListener('click', deleteSelected);
$('#move-selected-btn').addEventListener('click', moveSelected);
$('#transcribe-selected-btn').addEventListener('click', transcribeSelected);

// Search
$('#search-input').addEventListener('input', (e) => {
  app.searchQuery = e.target.value;
  const clearBtn = $('#search-clear');
  if (app.searchQuery) {
    clearBtn.classList.remove('hidden');
  } else {
    clearBtn.classList.add('hidden');
  }
  renderRecordingsList();
});

$('#search-clear').addEventListener('click', () => {
  $('#search-input').value = '';
  app.searchQuery = '';
  $('#search-clear').classList.add('hidden');
  renderRecordingsList();
});

// Plaud sign-in link (plaud://login?auth_code=…, delivered by tauri-bridge.js once
// the backend is ready) — routes to the setup flow or to Settings › Plaud Cloud
window.plaud.onOAuthCallback(async (authCode) => {
  if (!authCode) return;

  // If setup flow has a handler, route there
  if (window._setupOAuthHandler) {
    const handler = window._setupOAuthHandler;
    window._setupOAuthHandler = null;
    handler(authCode);
    return;
  }

  // Normal settings flow
  toast('Received Plaud sign-in link, signing in...', 'info');
  try {
    const res = await window.plaud.cloudExchangeToken(authCode, app.pendingDesktopUuid || '', 'link');
    finishCloudSignIn(res);
  } catch (err) {
    app.cloudLoginPending = false;
    toast(`Error: ${err.message}`, 'error');
    renderCloudAccount();
  }
  app.pendingDesktopUuid = null;
});

// ---- Auto-Update UI ----
// Global update status listeners — settings.js can push handlers here
app.updateListeners = [];

window.plaud.onUpdateStatus((data) => {
  const banner = $('#update-banner');
  const message = $('#update-message');
  const actionBtn = $('#update-action');
  if (data.status !== 'error' && data.status !== 'up-to-date') banner.classList.remove('update-banner-error');

  if (data.status === 'available') {
    banner.classList.remove('hidden');
    message.textContent = `Downloading v${data.version}...`;
    actionBtn.style.display = 'none';
  } else if (data.status === 'downloading') {
    banner.classList.remove('hidden');
    message.textContent = `Downloading update... ${data.percent}%`;
    actionBtn.style.display = 'none';
  } else if (data.status === 'ready') {
    banner.classList.remove('hidden');
    message.textContent = `v${data.version} ready to install`;
    actionBtn.style.display = '';
    actionBtn.textContent = 'Restart to update';
  } else if (data.status === 'major-update-downloading') {
    // Major version DMG downloading
    banner.classList.remove('hidden');
    const pct = data.percent ? ` ${data.percent}%` : '...';
    message.textContent = `Downloading v${data.version} (major upgrade)${pct}`;
    actionBtn.style.display = 'none';
  } else if (data.status === 'major-update') {
    // Major version DMG ready — auto-opened by main process
    banner.classList.remove('hidden');
    message.textContent = `v${data.version} ready — drag to Applications to install`;
    actionBtn.style.display = '';
    actionBtn.textContent = 'Open DMG Again';
    actionBtn.disabled = false;
    actionBtn.onclick = (e) => {
      e.preventDefault();
      if (data.dmgPath) {
        window.plaud.openExternal(`file://${data.dmgPath}`);
      }
    };
  } else if (data.status === 'error') {
    console.error('[Updater]', data.message);
    // If an update was in progress (banner showing), say what went wrong
    // instead of leaving it on "Downloading…" / "Installing…".
    if (!banner.classList.contains('hidden')) {
      banner.classList.add('update-banner-error');
      message.textContent = `Update failed: ${data.message || 'unknown error'}`;
      actionBtn.style.display = 'none';
      actionBtn.disabled = false;
    }
  } else if (data.status === 'up-to-date') {
    console.log('[Updater] App is up to date');
  }

  // Notify any waiting listeners (e.g. Check for Updates button)
  for (const fn of app.updateListeners.splice(0)) fn(data);
});

$('#update-action').addEventListener('click', async () => {
  $('#update-message').textContent = 'Installing update, restarting...';
  $('#update-action').disabled = true;
  try {
    const res = await window.plaud.installUpdate();
    // On success the app quits; we only get here if the install was refused
    // (e.g. running from a mounted DMG / App Translocation).
    if (res && res.installing === false) {
      $('#update-message').textContent = res.error || 'The update could not be installed.';
      $('#update-action').disabled = false;
    }
  } catch (err) {
    $('#update-banner').classList.add('update-banner-error');
    $('#update-message').textContent = `Update failed: ${err && err.message ? err.message : err}`;
    $('#update-action').disabled = false;
  }
});

$('#update-dismiss').addEventListener('click', () => {
  $('#update-banner').classList.add('hidden');
});

// ---- WiFi Sync (user-initiated) ----
const WIFI_SIZE_THRESHOLD = 500000; // 500KB — offer WiFi above this

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function offerWifiSync(totalSize, fileCount, isBackground) {
  const sizeStr = formatBytes(totalSize);
  const msg = `${fileCount} recording(s) (${sizeStr}). Wi-Fi transfer is about 50 times faster.`;

  if (isBackground) {
    // App is in tray/background — show system notification
    window.plaud.showNotification(
      'Large recordings detected',
      `${sizeStr} to sync. Confirm the Wi-Fi transfer in Open Pin (it temporarily disconnects the internet).`,
    );
    startWifiSync(); // shows the Wi-Fi confirmation in the window
    return;
  }

  // App is visible — show action toast
  toastAction(msg, 'Use Wi-Fi', () => {
    startWifiSync();
  }, 15000);
}

function _showWifiOverlay() {
  const overlay = $('#wifi-confirm-overlay');
  if (!overlay) return null;
  overlay.classList.remove('hidden');
  overlay.offsetHeight;
  overlay.classList.add('visible');
  return overlay;
}

// Progress renders into its own container so the confirmation view (header,
// explanation, Cancel/Start) survives and can be shown again next time.
function _wifiProgressHost() {
  const panel = $('#wifi-confirm-overlay .modal-panel');
  if (!panel) return null;
  let host = panel.querySelector('.wifi-progress-host');
  if (!host) {
    host = document.createElement('div');
    host.className = 'wifi-progress-host hidden';
    panel.appendChild(host);
  }
  return host;
}

function _setWifiConfirmPartsVisible(visible) {
  const panel = $('#wifi-confirm-overlay .modal-panel');
  if (!panel) return;
  [...panel.children]
    .filter((el) => el.matches('.modal-header, .modal-body, .modal-footer'))
    .forEach((el) => el.classList.toggle('hidden', !visible));
}

function _resetWifiConfirmView() {
  const host = _wifiProgressHost();
  if (host) {
    host.innerHTML = '';
    host.classList.add('hidden');
  }
  _setWifiConfirmPartsVisible(true);
}

// Every way into a WiFi transfer goes through here. Shows the confirmation
// (WiFi transfer disconnects the Mac from the internet); with { auto: true }
// — only used when the user turned on "Prefer WiFi" — it confirms itself but
// still shows the progress view.
function startWifiSync(opts = {}) {
  if (app.syncing && app.syncKind === 'wifi') {
    _showWifiOverlay();
    toast('Already syncing over Wi-Fi', 'info');
    return;
  }
  if (!$('#wifi-confirm-overlay')) return;
  _resetWifiConfirmView();
  _showWifiOverlay();
  if (opts.auto && app.settings.auto_prefer_wifi) {
    _beginWifiTransfer();
  }
}

// Start (or auto-start) a confirmed WiFi transfer. If a Bluetooth sync is
// running, it's aborted first; the WiFi flow claims the sync up front so the
// aborted BLE flow can't clear app.syncing underneath it.
async function _beginWifiTransfer() {
  if (app.syncing && app.syncKind === 'wifi') return;
  const wasBle = app.syncing && app.syncKind === 'ble';
  const gen = _claimSync('wifi');
  if (wasBle) {
    _renderWifiProgress('preparing', 'Stopping Bluetooth transfer...', '', 1);
    try {
      await window.plaud.abortSync();
    } catch {}
    await new Promise((r) => setTimeout(r, 500));
    if (app.syncGen !== gen) return;
  }
  return executeWifiSync(gen);
}

// WiFi sync stage icons (SVG paths)
const _wifiIcons = {
  preparing: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M12 2v4m0 12v4m-7.07-3.93l2.83-2.83m8.48-8.48l2.83-2.83M2 12h4m12 0h4m-3.93 7.07l-2.83-2.83M6.34 6.34L3.51 3.51" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  wifi: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M5 12.55a11 11 0 0114 0M8.53 16.11a6 6 0 016.95 0M12 20h.01" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  lock: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><rect x="5" y="11" width="14" height="10" rx="2" stroke="currentColor" stroke-width="2"/><path d="M8 11V7a4 4 0 118 0v4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  folder: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  download: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  check: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M6 12l4 4 8-8" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  error: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M12 9v4m0 4h.01M4.93 19h14.14c1.34 0 2.17-1.46 1.5-2.62L13.5 4.38c-.67-1.16-2.33-1.16-3 0L3.43 16.38c-.67 1.16.16 2.62 1.5 2.62z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

function _wifiSyncStageFromMessage(msg, pct) {
  if (!msg) return { icon: 'preparing', title: 'Preparing...', detail: '' };
  const m = msg.toLowerCase();
  if (pct <= 5) return { icon: 'preparing', title: 'Preparing device...', detail: msg };
  if (m.includes('hotspot') && m.includes('open')) return { icon: 'wifi', title: 'Enabling device hotspot...', detail: '' };
  if (m.includes('connecting to') || m.includes('hotspot')) return { icon: 'wifi', title: 'Connecting to device hotspot...', detail: 'Internet will be temporarily disconnected' };
  if (m.includes('handshake') || m.includes('secure')) return { icon: 'lock', title: 'Establishing secure connection...', detail: '' };
  if (m.includes('file list') || m.includes('discover')) return { icon: 'folder', title: 'Discovering recordings...', detail: '' };
  if (m.startsWith('syncing recording') || m.startsWith('synced ') || m.includes('to sync') || m.includes('download') || m.includes('file')) {
    return { icon: 'download', title: 'Downloading recordings...', detail: msg };
  }
  if (m.includes('restor') || m.includes('wifi')) return { icon: 'wifi', title: 'Restoring internet...', detail: msg };
  return { icon: 'preparing', title: msg, detail: '' };
}

function _renderWifiProgress(icon, title, detail, pct, state = 'syncing') {
  const host = _wifiProgressHost();
  if (!host) return;
  _setWifiConfirmPartsVisible(false);
  host.classList.remove('hidden');
  const iconClass = ['success', 'error', 'warning'].includes(state) ? state : 'syncing';
  const barClass = state === 'success' ? 'success' : '';
  // A failed or "action needed" state has no meaningful progress: no bar, no "0%".
  const showBar = state === 'syncing' || state === 'success';

  host.innerHTML = `
    <div class="wifi-sync-progress wifi-sync-${iconClass}">
      <div class="wifi-sync-stage-icon ${iconClass}">${_wifiIcons[icon] || _wifiIcons.preparing}</div>
      <h3 class="wifi-sync-stage-title">${escapeHtml(title)}</h3>
      ${detail ? `<p class="wifi-sync-stage-detail">${escapeHtml(detail)}</p>` : ''}
      ${showBar ? `<div class="wifi-sync-bar"><div class="wifi-sync-bar-fill ${barClass}" style="width: ${Math.round(pct)}%"></div></div>
      <p class="wifi-sync-percent">${Math.round(pct)}%</p>` : ''}
    </div>
  `;
}

function _closeWifiModal() {
  const overlay = $('#wifi-confirm-overlay');
  if (!overlay) return;
  overlay.classList.remove('visible');
  overlay.addEventListener('transitionend', function h() {
    overlay.classList.add('hidden');
    overlay.removeEventListener('transitionend', h);
  });
}

// Only call via startWifiSync()/_beginWifiTransfer() so the user has confirmed.
async function executeWifiSync(claimedGen) {
  const gen = claimedGen && claimedGen === app.syncGen ? claimedGen : _claimSync('wifi');
  // Keep the modal open — transform into progress view
  _showWifiOverlay();
  _renderWifiProgress('preparing', 'Preparing...', '', 2);

  // Poll for progress updates while sync runs
  let pollActive = true;
  const pollProgress = async () => {
    const pollStartedAt = Date.now();
    // The backend reports its own time budget (`deadline`, UNIX seconds) and
    // stops + restores the worker itself; only give up well after that. Without
    // a deadline, fall back to a generous fixed ceiling (the parent can take
    // ~16 min before it returns, even for small transfers).
    const POLL_FALLBACK_MAX_MS = 20 * 60 * 1000;
    let wifiDeadlineS = 0;
    while (pollActive) {
      const timedOut = wifiDeadlineS
        ? Date.now() / 1000 > wifiDeadlineS + 240
        : Date.now() - pollStartedAt > POLL_FALLBACK_MAX_MS;
      if (timedOut) {
        pollActive = false;
        _renderWifiProgress('error', 'Sync timed out', 'The Wi-Fi sync took longer than expected. Your Wi-Fi settings are restored automatically.', 0, 'error');
        const panel = $('#wifi-confirm-overlay .modal-panel');
        panel.querySelector('.wifi-sync-progress').insertAdjacentHTML('beforeend',
          '<button class="action-btn primary" id="wifi-done-btn" style="margin-top:var(--sp-4);">Close</button>');
        panel.querySelector('#wifi-done-btn').addEventListener('click', _closeWifiModal);
        break;
      }
      try {
        const s = await window.plaud.wifiSyncStatus();
        if (s && typeof s.deadline === 'number' && s.deadline > 0) wifiDeadlineS = s.deadline;
        // Re-check pollActive after the async status call: the main sync may have
        // already finished (setting pollActive=false) and rendered a terminal
        // success/error state with an appended Close/Done button. Calling
        // _renderWifiProgress now would replace the entire innerHTML and destroy
        // that button.
        if (pollActive && s && s.message) {
          const stage = _wifiSyncStageFromMessage(s.message, s.percent || 5);
          _renderWifiProgress(stage.icon, stage.title, stage.detail, s.percent || 5);
        }
      } catch {}
      await new Promise((r) => setTimeout(r, 1000));
    }
  };
  pollProgress();

  try {
    const result = await window.plaud.wifiSync();
    pollActive = false;

    if (isDeviceBusyResponse(result)) {
      _renderWifiProgress('preparing', 'Already syncing', 'A sync with the device is already running. Try again when it finishes.', 0, 'warning');
      const panel = $('#wifi-confirm-overlay .modal-panel');
      panel.querySelector('.wifi-sync-progress').insertAdjacentHTML('beforeend',
        '<button class="action-btn primary" id="wifi-done-btn" style="margin-top:var(--sp-4);">Close</button>');
      panel.querySelector('#wifi-done-btn').addEventListener('click', _closeWifiModal);
      return;
    }

    if (result.error) {
      _renderWifiProgress('error', 'Transfer failed', result.error, 0, 'error');
      window.plaud.reportError(`WiFi sync failed: ${result.error}`, '', 'wifi-sync-error');
      const panel = $('#wifi-confirm-overlay .modal-panel');
      panel.querySelector('.wifi-sync-progress').insertAdjacentHTML('beforeend',
        '<button class="action-btn primary" id="wifi-done-btn" style="margin-top:var(--sp-4);">Close</button>');
      panel.querySelector('#wifi-done-btn').addEventListener('click', _closeWifiModal);
      return;
    }

    if (result.wifi_restore_notice) _maybeShowWifiRestoreNotice(result.wifi_restore_notice);

    const newFiles = (result.files || []).filter((f) => f.status === 'new');
    toastPartialSync(result);

    if (newFiles.length > 0) {
      await loadRecordings();
      await loadSettings();
      // Transcribed in the background, after the transfer (see queueAutoProcess).
      queueAutoProcess(newFiles);
    }
    const syncedText = newFiles.length > 0
      ? `${newFiles.length} recording${newFiles.length === 1 ? '' : 's'} synced.`
      : 'No new recordings.';

    // Wi-Fi couldn't be put back by itself. Not a bug: a known macOS limitation
    // (networksetup returns -3900 after Bluetooth sharing toggles the
    // interface), so the user reconnects by hand. This is the final state: it
    // says what synced too, and isn't replaced by "Transfer complete".
    if (result.wifi_restore && result.wifi_restore.need_manual) {
      const ssidName = result.wifi_restore.ssid ? `"${result.wifi_restore.ssid}"` : 'your Wi-Fi network';
      _renderWifiProgress(
        'wifi',
        'Transfer complete: reconnect to Wi-Fi',
        `${syncedText} Reconnect to ${ssidName} in the Wi-Fi settings, which should be open.`,
        100,
        'warning',
      );
      window.plaud.showNotification(
        'Wi-Fi transfer complete',
        `${syncedText} Reconnect to ${ssidName} in the Wi-Fi settings.`,
      );
    } else {
      _renderWifiProgress('check', 'Transfer complete', syncedText, 100, 'success');
    }

    // Add done button
    const panel = $('#wifi-confirm-overlay .modal-panel');
    panel.querySelector('.wifi-sync-progress').insertAdjacentHTML('beforeend',
      '<button class="action-btn primary" id="wifi-done-btn" style="margin-top:var(--sp-4);">Done</button>');
    panel.querySelector('#wifi-done-btn').addEventListener('click', _closeWifiModal);

  } catch (err) {
    pollActive = false;
    _renderWifiProgress('error', 'Transfer failed', err.message, 0, 'error');
    window.plaud.reportError(`WiFi sync exception: ${err.message}`, err.stack || '', 'wifi-sync-exception');
    const panel = $('#wifi-confirm-overlay .modal-panel');
    panel.querySelector('.wifi-sync-progress').insertAdjacentHTML('beforeend',
      '<button class="action-btn primary" id="wifi-done-btn" style="margin-top:var(--sp-4);">Close</button>');
    panel.querySelector('#wifi-done-btn').addEventListener('click', _closeWifiModal);
  } finally {
    pollActive = false;
    hideProgress();

    // Reconnect BLE (worker disconnected it to do its own BLE session).
    // Still "syncing" until this finishes so the background poll stays out.
    try {
      const scan = await window.plaud.scanKnown();
      if (scan.found) {
        const conn = await window.plaud.connect(scan.address, scan.serial_number);
        if (conn.connected) {
          toast(`Reconnected to ${prettyDeviceName(scan.name)}`, 'success');
        }
      }
    } catch {}
    if (_releaseSync(gen)) await updateDeviceStatus();
  }
}

function dismissWifiConfirm() {
  const overlay = $('#wifi-confirm-overlay');
  overlay.classList.remove('visible');
  overlay.addEventListener('transitionend', function h() {
    overlay.classList.add('hidden');
    overlay.removeEventListener('transitionend', h);
  });
}

// WiFi confirmation modal event listeners
$('#wifi-confirm-close').addEventListener('click', dismissWifiConfirm);
$('#wifi-confirm-cancel').addEventListener('click', dismissWifiConfirm);
$('#wifi-confirm-start').addEventListener('click', () => _beginWifiTransfer());
$('#wifi-confirm-overlay').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) dismissWifiConfirm();
});

// ---- System Wake Recovery ----
// BLE connections die during macOS sleep — detect wake and force reconnect cycle
// On macOS the WKWebView's NSURLSession can also be stranded by sleep, making
// every fetch() throw "Load failed" even though the Python sidecar is fine.
// `probeWebviewFetch` detects that and the handler reloads to reset the stack.
async function probeWebviewFetch() {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const h = await window.plaud.health();
      if (h && (h.status === 'ok' || h.ok === true)) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 1500));
  }
  return false;
}

window.plaud.onSystemWake(async () => {
  console.log('[Wake] System resumed — refreshing device status');
  // System appearance may have flipped while sleeping — re-resolve before
  // anything paints, so the user doesn't see a flash of the wrong theme.
  try { await window.plaud.getTheme(); } catch {}
  // Unstick any polling guards that may have been locked true by a fetch
  // that was in flight when the system slept and never resolved.
  if (typeof app.resetPollingState === 'function') app.resetPollingState();
  if (typeof app.restartPollingTimers === 'function') app.restartPollingTimers();
  // Give BLE stack a moment to settle after wake
  await new Promise((r) => setTimeout(r, 2000));

  // WKWebView post-sleep network breakage: sidecar is healthy but every fetch
  // throws "Load failed". Only fix is to reload the renderer.
  if (!app.syncing && !(await probeWebviewFetch())) {
    try {
      const info = await window.plaud.getApiInfo();
      if (info && info.status === 'ready') {
        console.log('[Wake] WebView fetch broken, sidecar healthy — reloading renderer');
        window.location.reload();
        return;
      }
    } catch {}
  }

  await updateDeviceStatus();

  // Don't trust the cached `connected` flag after sleep: the BLE link routinely
  // drops during sleep WITHOUT bleak firing its disconnect callback, so /status
  // keeps reporting connected:true and serving a frozen pre-sleep snapshot
  // (e.g. battery/charging stuck). Actively probe the link — verifyConnection
  // sends a real command and resets state if it's dead — then decide.
  let live;
  try {
    live = await window.plaud.verifyConnection();
  } catch {
    live = { connected: false };
  }
  if (!live.connected) {
    await updateDeviceStatus();
    console.log('[Wake] Device link dead after sleep — attempting reconnect');
    try {
      const scan = await window.plaud.scanKnown();
      if (scan.found) {
        const conn = await window.plaud.connect(scan.address, scan.serial_number);
        if (conn.connected) {
          toast(`Reconnected to ${prettyDeviceName(scan.name)}`, 'success');
          await updateDeviceStatus();

          // Auto-sync if enabled (and nothing else is syncing)
          if (app.settings.auto_sync !== false && !app.syncing) {
            const check = await window.plaud.checkNew();
            if (!isDeviceBusyResponse(check) && check.new_files > 0 && !app.syncing) {
              toast(`${check.new_files} new recording(s), syncing...`, 'info');
              const gen = _claimSync('ble');
              try {
                const r = await window.plaud.sync({ background: true });
                if (!isDeviceBusyResponse(r) && !r.error) {
                  await loadRecordings();
                  toast('Sync complete', 'success');
                  toastPartialSync(r);
                  queueAutoProcess((r.files || []).filter((f) => f.status === 'new'));
                }
              } catch {} finally {
                if (_releaseSync(gen)) await updateDeviceStatus();
              }
            }
          }
        }
      }
    } catch (err) {
      console.log('[Wake] Reconnect failed:', err.message);
    }
  } else {
    console.log('[Wake] Device still connected');
  }
});

// ---- Init ----
// Startup is one state machine in ui/health-gate.js: its splash shows the
// Rust shell's startup progress until the backend is ready, or a failure
// screen whose Retry re-runs init(). showStartupError only covers errors that
// arrive after startup (e.g. the backend crashing for good later on).

function showStartupError(error) {
  const h2 = dom.emptyState.querySelector('h2');
  const p = dom.emptyState.querySelector('p');

  // error is either a structured object { code, title, friendly, hint, detail } or a plain string
  const isStructured = error && typeof error === 'object' && error.code;
  const title = isStructured ? error.title : 'Server failed to start';
  const friendly = isStructured ? error.friendly : String(error);
  const hint = isStructured ? error.hint : '';
  const code = isStructured ? error.code : 'UNKNOWN';

  h2.textContent = title;
  p.innerHTML = '';

  const msg = document.createElement('div');
  msg.textContent = friendly;
  msg.style.marginBottom = '8px';
  p.appendChild(msg);

  if (hint) {
    const hintEl = document.createElement('div');
    hintEl.textContent = hint;
    hintEl.style.cssText = 'font-size:12px;padding:8px 12px;border-radius:6px;background:var(--bg-secondary);text-align:left;';
    p.appendChild(hintEl);
  }

  const footer = document.createElement('div');
  footer.style.cssText = 'margin-top:12px;font-size:11px;opacity:0.5;';
  footer.textContent = `This error has been automatically reported. Error code: ${code}`;
  p.appendChild(footer);
}

// Listen for late-arriving startup errors (e.g. Python crashes after window opens)
window.plaud.onStartupError((error) => {
  showStartupError(error);
});

// Init host-app features immediately (don't wait for Python API)
initSettingsEvents();

let _rendererInitPassedGate = false;

async function init() {
  // Wait for the health gate: it resolves true once the backend answers
  // /health, or false after showing its failure screen (whose Retry calls
  // init() again).
  const ready = await window.plaudStartup.waitForBackend();
  if (!ready || _rendererInitPassedGate) return;
  _rendererInitPassedGate = true;

  // Server is ready — reset empty state text to default
  dom.emptyState.querySelector('h2').textContent = 'No recordings yet';
  dom.emptyState.querySelector('p').textContent = 'Connect your Plaud NotePin S and sync recordings, or start a new recording from the device.';

  // Init sub-modules
  initAudioPlayer();

  await updateDeviceStatus();
  await loadCloudStatus();
  await loadNotionStatus();
  await loadGdriveStatus();
  await loadSettings();
  await loadRecordings();
  // Picks up speaker names and tags that AI tools change (ui/mcp-settings.js)
  startMcpChangePoll();

  // First-run setup — show onboarding if no paired devices and not dismissed
  const isFirstRun = !app.settings.has_paired_devices && !app.settings.setup_dismissed;
  if (isFirstRun) {
    showSetupWelcome();
    return; // completeSetup() will resume polling after onboarding finishes
  }

  // Try to reconnect to known device on startup
  // Only scan if we have paired devices (avoids unnecessary BLE permission prompts)
  if (app.settings.has_paired_devices) {
    try {
      const status = await window.plaud.status();
      if (!status.connected) {
        const scan = await window.plaud.scanKnown();
        if (scan.found) {
          const conn = await window.plaud.connect(scan.address, scan.serial_number);
          if (conn.connected) {
            toast(`Connected to ${prettyDeviceName(scan.name)}`, 'success');
            await updateDeviceStatus();
          }
        }
      }
    } catch {}
  }

  _maybeShowWifiRestoreNotice();
  _startBgPoll();
}

// Shown once if a crashed/aborted WiFi sync couldn't put back every saved
// WiFi network (the backend gave up after repeated failed restores).
async function _maybeShowWifiRestoreNotice(notice) {
  try {
    if (!notice) {
      const s = await window.plaud.wifiSyncStatus();
      notice = s && s.restore_notice;
    }
    if (!notice || !notice.message) return;
    toastAction(notice.message, 'OK', () => {}, 30000);
    await window.plaud.ackWifiRestoreNotice();
  } catch (err) {
    console.warn('[wifi] restore notice check failed:', err);
  }
}

let _bgPollStarted = false;
function _startBgPoll() {
  if (_bgPollStarted) return;
  _bgPollStarted = true;
  // ---- Adaptive polling ----
  // Adjusts frequency based on app focus + device connection state
  // to minimize battery drain on the Plaud device.
  //
  // | State                        | Status poll | Reconnect/sync |
  // |------------------------------|-------------|----------------|
  // | Focused + connected          | 10s         | 45s            |
  // | Focused + disconnected       | off         | 30s            |
  // | Background + connected       | 60s         | 5 min          |
  // | Background + disconnected    | off         | 5 min          |

  let _appFocused = document.hasFocus();
  let _deviceConnected = false;
  let _statusTimer = null;
  let _bgTimer = null;
  let _cloudSyncTimer = null;

  function _getIntervals() {
    if (_appFocused && _deviceConnected) return { status: 10000, bg: 45000 };
    if (_appFocused && !_deviceConnected) return { status: 0, bg: 30000 };
    if (!_appFocused && _deviceConnected) return { status: 0, bg: 300000 };
    return { status: 0, bg: 300000 }; // background + disconnected
  }

  function _restartTimers() {
    const intervals = _getIntervals();
    if (_statusTimer) clearInterval(_statusTimer);
    if (_bgTimer) clearInterval(_bgTimer);
    if (_cloudSyncTimer) clearInterval(_cloudSyncTimer);
    _statusTimer = intervals.status > 0 ? setInterval(updateDeviceStatus, intervals.status) : null;
    _bgTimer = setInterval(bgDevicePoll, intervals.bg);
    // Cloud sync timer — respects the user's interval setting
    _cloudSyncTimer = null;
    const syncInterval = app.settings?.cloud_sync_interval;
    const syncDirection = app.settings?.cloud_sync_direction;
    if (syncInterval && syncInterval !== 'manual' && syncDirection && syncDirection !== 'none') {
      const ms = parseInt(syncInterval, 10) * 1000;
      if (ms > 0) {
        _cloudSyncTimer = setInterval(cloudSyncPoll, ms);
      }
    }
  }

  async function cloudSyncPoll() {
    if (app.syncing) return;
    try {
      const status = await window.plaud.cloudStatus();
      if (!status.logged_in) return;
      const direction = app.settings?.cloud_sync_direction || 'download';
      if (direction === 'none') return;
      const result = await window.plaud.cloudSync(direction);
      if (result.error) return;
      const downloaded = result.download?.files?.filter(f => f.status === 'new') || [];
      if (downloaded.length > 0) {
        toast(`Cloud sync: ${downloaded.length} new recording(s)`, 'info');
        await loadRecordings();
      }
    } catch {}
  }

  // Track focus changes
  document.addEventListener('visibilitychange', () => {
    const wasFocused = _appFocused;
    _appFocused = !document.hidden;
    if (_appFocused !== wasFocused) {
      _restartTimers();
      // When app becomes active again, immediately check status + sync.
      // Reset the busy guard first in case a pre-background fetch got stranded.
      if (_appFocused) {
        _bgBusy = false;
        updateDeviceStatus();
        bgDevicePoll();
      }
    }
  });
  window.addEventListener('focus', () => {
    if (!_appFocused) {
      _appFocused = true;
      _bgBusy = false;
      _restartTimers();
      updateDeviceStatus();
      bgDevicePoll();
    }
  });
  window.addEventListener('blur', () => {
    _appFocused = false;
    _restartTimers();
  });

  // Background device polling
  let _bgBusy = false;
  // Monotonically-increasing generation counter. Each bgDevicePoll() invocation
  // captures its own generation at entry. resetPollingState() bumps the counter,
  // so any in-flight poll whose generation is now stale detects the mismatch
  // after each await and exits — preventing two concurrent polls from racing.
  let _bgGen = 0;
  // Expose a way to unstick the guard — e.g. after system wake if a fetch
  // got stranded pre-sleep and never resolved.
  app.resetPollingState = () => {
    _bgBusy = false;
    _bgGen++;          // invalidate any in-flight poll; new invocation gets fresh gen
    app.syncGen = (app.syncGen || 0) + 1; // in-flight sync flows no longer own the UI
    app.syncing = false;
    app.syncKind = null;
    setSyncButtonMode('sync');
  };
  async function bgDevicePoll() {
    if (_bgBusy || app.syncing) return;
    _bgBusy = true;
    const myGen = _bgGen; // capture generation; stale if resetPollingState fires mid-flight
    try {
      const status = await window.plaud.status();
      // If resetPollingState() fired during the await above, _bgGen will have
      // advanced and a fresh poll may already be running. Exit so we don't
      // race with — or clobber the guard of — that newer invocation.
      if (_bgGen !== myGen) return;
      const wasConnected = _deviceConnected;
      _deviceConnected = status.connected;

      // Connection state changed — adjust polling frequency
      if (_deviceConnected !== wasConnected) {
        _restartTimers();
        // Just reconnected — trigger immediate sync check
        if (_deviceConnected && !wasConnected) {
          toast(`Device reconnected`, 'success');
          await updateDeviceStatus();
        }
      }

      // Skip reconnect/sync if a sync is already running server-side
      if (status.syncing) return;

      if (!status.connected) {
        const scan = await window.plaud.scanKnown();
        if (_bgGen !== myGen) return; // stale — resetPollingState fired during scan
        if (scan.found) {
          const conn = await window.plaud.connect(scan.address, scan.serial_number);
          if (conn.connected) {
            _deviceConnected = true;
            _restartTimers();
            toast(`Connected to ${prettyDeviceName(scan.name)}`, 'success');
            await updateDeviceStatus();
          }
        }
        return;
      }

      // Auto-sync if enabled
      if (app.settings.auto_sync === false) return;

      const check = await window.plaud.checkNew();
      if (_bgGen !== myGen) return; // stale — resetPollingState fired during checkNew
      if (isDeviceBusyResponse(check) || app.syncing) return; // a sync is already running
      if (check.new_files > 0) {
        // WiFi for large files — auto-start (with progress UI) only if the user
        // turned on "Prefer WiFi"; otherwise offer it.
        if (check.total_size > WIFI_SIZE_THRESHOLD) {
          if (app.settings.auto_prefer_wifi) {
            startWifiSync({ auto: true });
            return;
          }
          const isBackground = !document.hasFocus();
          offerWifiSync(check.total_size, check.new_files, isBackground);
          // Intentional fall-through: start BLE sync immediately as the
          // default + show the WiFi toast as an optional upgrade. If the user
          // clicks "Use WiFi" and confirms, _beginWifiTransfer() aborts the
          // BLE sync first. Don't add a `return` here — it makes BLE never
          // run when files > WIFI_SIZE_THRESHOLD.
        }

        toast(`${check.new_files} new recording(s) found, syncing via Bluetooth...`, 'info');
        dom.progressBar.classList.remove('hidden');
        dom.progressBar.classList.add('indeterminate');
        const syncGen = _claimSync('ble');
        try {
          const result = await window.plaud.sync({ background: true });
          if (!result.error && !isDeviceBusyResponse(result)) {
            toastPartialSync(result);
            const newFiles = (result.files || []).filter((f) => f.status === 'new');
            if (newFiles.length > 0) {
              toast(`Synced ${newFiles.length} recording(s)`, 'success');

              await loadRecordings();
              await loadSettings();
              // Auto-transcribe (and summarise) in the background.
              queueAutoProcess(newFiles);

              const bgSyncDir = app.settings.cloud_sync_direction || 'download';
              if (bgSyncDir === 'twoway' && app.cloudState.logged_in) {
                try {
                  const cr = await window.plaud.cloudSync('twoway');
                  const ul = cr.upload || {};
                  const upped = (ul.uploaded || []).filter((u) => u.status === 'uploaded');
                  if (upped.length > 0) toast(`Uploaded ${upped.length} to cloud`, 'success');
                } catch {}
              }
            }
          }
        } finally {
          dom.progressBar.classList.remove('indeterminate');
          if (_releaseSync(syncGen)) {
            hideProgress();
            await updateDeviceStatus();
          }
        }
      }
    } catch {}
    finally {
      // Only clear the guard if we're still the current generation. If
      // resetPollingState() bumped _bgGen while we were mid-flight, a newer
      // invocation already owns _bgBusy and must clear it itself.
      if (_bgGen === myGen) _bgBusy = false;
    }
  }

  // Start with initial timers
  _restartTimers();
  // First attempt after 5s
  setTimeout(bgDevicePoll, 5000);

  // Allow settings to trigger timer restart when sync interval/direction changes
  app.restartPollingTimers = _restartTimers;
}

window.plaudStartup.onRetry(init);
init();
