/* ============================================================
   Plaud V2 — Renderer Process (Orchestrator)
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
  } catch {}
})();

$('#theme-toggle-btn').addEventListener('click', async () => {
  try {
    const { preference } = await window.plaud.cycleTheme();
    showThemeIcon(preference);
    const labels = { auto: 'Auto (system)', light: 'Light mode', dark: 'Dark mode' };
    toast(labels[preference], 'info');
  } catch {}
});

// ---- Bug Report Modal ----
function openBugReport() {
  const overlay = $('#bug-report-overlay');
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
$('#bug-report-close').addEventListener('click', closeBugReport);
$('#bug-report-cancel').addEventListener('click', closeBugReport);
$('#bug-report-overlay').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeBugReport();
});

$('#bug-report-submit').addEventListener('click', async () => {
  const expected = $('#bug-expected').value.trim();
  const actual = $('#bug-actual').value.trim();
  const steps = $('#bug-steps').value.trim();

  if (!expected && !actual) {
    toast('Please describe what you expected or what happened', 'error');
    return;
  }

  const btn = $('#bug-report-submit');
  setLoading(btn, true);

  try {
    const result = await window.plaud.reportBug(expected, actual, steps);
    if (result.success) {
      toast(`Bug report submitted (#${result.number})`, 'success');
      closeBugReport();
    } else {
      toast(`Failed to submit: ${result.error}`, 'error');
    }
  } catch (err) {
    toast(`Failed to submit: ${err.message}`, 'error');
  } finally {
    setLoading(btn, false);
  }
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
  // Switch to Device tab
  document.querySelectorAll('.settings-tab').forEach((t) => t.classList.remove('active'));
  document.querySelectorAll('.settings-tab-content').forEach((c) => c.classList.remove('active'));
  const deviceTab = document.querySelector('.settings-tab[data-tab="device"]');
  const deviceContent = document.querySelector('.settings-tab-content[data-tab="device"]');
  if (deviceTab) deviceTab.classList.add('active');
  if (deviceContent) deviceContent.classList.add('active');
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
      toast(`Found ${result.name}`, 'success');
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
        toast(`Connected to ${conn.device_name || 'Plaud NotePin S'}`, 'success');
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

dom.syncBtn.addEventListener('click', async () => {
  setLoading(dom.syncBtn, true);

  // Check file sizes first — offer WiFi for large transfers
  try {
    const check = await window.plaud.checkNew();
    if (check.new_files > 0 && check.total_size > WIFI_SIZE_THRESHOLD) {
      setLoading(dom.syncBtn, false);
      if (app.settings.auto_prefer_wifi) {
        executeWifiSync();
      } else {
        offerWifiSync(check.total_size, check.new_files, false);
      }
      return;
    }
  } catch {}

  showProgress('Syncing recordings...', 20);
  toast('Syncing recordings from device...', 'info');
  app.syncing = true;

  try {
    const result = await window.plaud.sync();
    if (result.error) {
      toast(`Sync failed: ${result.error}`, 'error');
    } else {
      const newFiles = (result.files || []).filter((f) => f.status === 'new');
      const total = (result.files || []).length;
      toast(`Sync complete: ${newFiles.length} new, ${total} total`, 'success');

      if (newFiles.length > 0) {
        showProgress('Transcribing new recordings...', 60);
        for (let i = 0; i < newFiles.length; i++) {
          const f = newFiles[i];
          if (f.ogg_path) {
            showProgress(`Transcribing ${i + 1}/${newFiles.length}...`, 60 + (40 * i) / newFiles.length);
            try {
              await window.plaud.transcribe(
                f.ogg_path,
                app.settings.model || 'small',
                app.settings.diarize || false,
                app.settings.language || 'auto',
              );
            } catch (err) {
              console.error(`Transcription failed for ${f.session_id}:`, err);
            }
          }
        }
        toast(`Transcribed ${newFiles.length} recording(s)`, 'success');

        // Auto-summarise if enabled
        if (app.settings.auto_summarise) {
          showProgress('Summarising...', 90);
          for (const f of newFiles) {
            if (f.ogg_path) {
              try {
                const sid = f.session_id || String(f.ogg_path).split('/').pop().replace('.ogg', '');
                await window.plaud.summarize(sid);
              } catch {}
            }
          }
          toast('Summarised new recordings', 'success');
        }
      }

      await loadRecordings();

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
    toast(`Sync error: ${err.message}`, 'error');
  } finally {
    app.syncing = false;
    setLoading(dom.syncBtn, false);
    hideProgress();
    // Immediately refresh status after sync ends — catches disconnections during transfer
    await updateDeviceStatus();
  }
});

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

$('#select-all-btn').addEventListener('click', () => {
  if (app.selectedIds.size === app.recordings.length) {
    app.selectedIds.clear();
  } else {
    app.recordings.forEach((r) => app.selectedIds.add(r.session_id));
  }
  renderRecordingsList();
});

$('#delete-selected-btn').addEventListener('click', deleteSelected);

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

// OAuth callback listener — routes to setup flow or settings depending on context
window.plaud.onOAuthCallback(async (authCode) => {
  if (!authCode) return;

  // If setup flow has a handler, route there
  if (window._setupOAuthHandler) {
    window._setupOAuthHandler(authCode);
    window._setupOAuthHandler = null;
    return;
  }

  // Normal settings flow
  toast('Received login callback, authenticating...', 'info');
  try {
    const res = await window.plaud.cloudExchangeToken(authCode, app.pendingDesktopUuid || '');
    if (res.logged_in) {
      app.cloudState = { logged_in: true, user: res.user };
      toast('Plaud account connected!', 'success');
      renderCloudAccount();
    } else {
      toast(res.error || 'Login failed', 'error');
    }
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
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
    actionBtn.textContent = 'Restart to Update';
  } else if (data.status === 'error') {
    console.error('[Updater]', data.message);
  } else if (data.status === 'up-to-date') {
    console.log('[Updater] App is up to date');
  }

  // Notify any waiting listeners (e.g. Check for Updates button)
  for (const fn of app.updateListeners.splice(0)) fn(data);
});

$('#update-action').addEventListener('click', () => {
  $('#update-message').textContent = 'Installing update, restarting...';
  $('#update-action').disabled = true;
  window.plaud.installUpdate();
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
  const msg = `${fileCount} recording(s) (${sizeStr}) — WiFi transfer is ~50x faster`;

  if (isBackground) {
    // App is in tray/background — show system notification
    window.plaud.showNotification(
      'Large recordings detected',
      `${sizeStr} to sync. Tap to use WiFi transfer (temporarily disconnects internet).`,
      'Use WiFi',
    );
    return;
  }

  // App is visible — show action toast
  toastAction(msg, 'Use WiFi', () => {
    startWifiSync();
  }, 15000);
}

async function startWifiSync() {
  // Confirm with user — WiFi interrupts internet
  const overlay = $('#wifi-confirm-overlay');
  if (!overlay) return;
  overlay.classList.remove('hidden');
  overlay.offsetHeight;
  overlay.classList.add('visible');
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
  if (m.includes('download') || m.includes('file')) return { icon: 'download', title: 'Downloading recordings...', detail: msg };
  if (m.includes('restor') || m.includes('wifi')) return { icon: 'wifi', title: 'Restoring internet...', detail: msg };
  return { icon: 'preparing', title: msg, detail: '' };
}

function _renderWifiProgress(icon, title, detail, pct, state = 'syncing') {
  const overlay = $('#wifi-confirm-overlay');
  if (!overlay) return;
  const panel = overlay.querySelector('.modal-panel');
  const iconClass = state === 'success' ? 'success' : state === 'error' ? 'error' : 'syncing';
  const barClass = state === 'success' ? 'success' : state === 'error' ? 'error' : '';

  panel.innerHTML = `
    <div class="wifi-sync-progress">
      <div class="wifi-sync-stage-icon ${iconClass}">${_wifiIcons[icon] || _wifiIcons.preparing}</div>
      <h3 class="wifi-sync-stage-title">${escapeHtml(title)}</h3>
      <p class="wifi-sync-stage-detail">${escapeHtml(detail)}</p>
      <div class="wifi-sync-bar"><div class="wifi-sync-bar-fill ${barClass}" style="width: ${Math.round(pct)}%"></div></div>
      <p class="wifi-sync-percent">${Math.round(pct)}%</p>
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

async function executeWifiSync() {
  // Keep the modal open — transform into progress view
  _renderWifiProgress('preparing', 'Preparing...', '', 2);

  app.syncing = true;

  // Poll for progress updates while sync runs
  let pollActive = true;
  const pollProgress = async () => {
    while (pollActive) {
      try {
        const s = await window.plaud.wifiSyncStatus();
        if (s && s.message) {
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

    if (result.error) {
      _renderWifiProgress('error', 'Transfer failed', result.error, 0, 'error');
      window.plaud.reportError(`WiFi sync failed: ${result.error}`, '', 'wifi-sync-error');
      const panel = $('#wifi-confirm-overlay .modal-panel');
      panel.querySelector('.wifi-sync-progress').insertAdjacentHTML('beforeend',
        '<button class="action-btn primary" id="wifi-done-btn" style="margin-top:var(--sp-4);">Close</button>');
      panel.querySelector('#wifi-done-btn').addEventListener('click', _closeWifiModal);
      return;
    }

    // Check if WiFi restore needs manual intervention
    if (result.wifi_restore && result.wifi_restore.need_manual) {
      const ssidName = result.wifi_restore.ssid || 'your WiFi';
      _renderWifiProgress('wifi', 'Reconnect to WiFi', `Please reconnect to "${ssidName}" — WiFi settings should be open.`, 95, 'error');
      window.plaud.reportError(`WiFi restore failed — manual reconnect needed for ${ssidName}`, '', 'wifi-restore-failed');
      window.plaud.showNotification(
        'WiFi transfer complete',
        `Please reconnect to "${ssidName}" in the WiFi settings panel.`,
      );
    }

    const newFiles = (result.files || []).filter((f) => f.status === 'new');

    if (newFiles.length > 0) {
      // Auto-transcribe if enabled
      if (app.settings.auto_transcribe !== false) {
        for (let i = 0; i < newFiles.length; i++) {
          _renderWifiProgress('preparing', 'Transcribing...', `Recording ${i + 1} of ${newFiles.length}`, 60 + (25 * i / newFiles.length));
          const f = newFiles[i];
          if (f.ogg_path) {
            try {
              await window.plaud.transcribe(
                f.ogg_path,
                app.settings.model || 'small',
                app.settings.diarize || false,
                app.settings.language || 'en',
              );
            } catch {}
          }
        }
        if (app.settings.auto_summarise) {
          for (let i = 0; i < newFiles.length; i++) {
            _renderWifiProgress('preparing', 'Summarising...', `Recording ${i + 1} of ${newFiles.length}`, 85 + (10 * i / newFiles.length));
            const f = newFiles[i];
            if (f.ogg_path) {
              try {
                const sid = f.session_id || f.ogg_path.split('/').pop().replace('.ogg', '');
                await window.plaud.summarize(sid);
              } catch {}
            }
          }
        }
      }
      await loadRecordings();

      // Show success
      _renderWifiProgress('check', 'Transfer complete!', `${newFiles.length} recording(s) synced`, 100, 'success');
    } else {
      _renderWifiProgress('check', 'Transfer complete', 'No new recordings found', 100, 'success');
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
    app.syncing = false;
    hideProgress();

    // Reconnect BLE (worker disconnected it to do its own BLE session)
    try {
      const scan = await window.plaud.scanKnown();
      if (scan.found) {
        const conn = await window.plaud.connect(scan.address, scan.serial_number);
        if (conn.connected) {
          toast(`Reconnected to ${scan.name}`, 'success');
        }
      }
    } catch {}
    await updateDeviceStatus();
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
$('#wifi-confirm-start').addEventListener('click', executeWifiSync);
$('#wifi-confirm-overlay').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) dismissWifiConfirm();
});

// Handle notification action from background
window.plaud.onNotificationAction((action) => {
  if (action === 'wifi-sync') {
    startWifiSync();
  }
});

// ---- System Wake Recovery ----
// BLE connections die during macOS sleep — detect wake and force reconnect cycle
window.plaud.onSystemWake(async () => {
  console.log('[Wake] System resumed — refreshing device status');
  // Give BLE stack a moment to settle after wake
  await new Promise((r) => setTimeout(r, 2000));
  await updateDeviceStatus();

  // If the device is now showing disconnected, try auto-reconnect
  const status = await window.plaud.status();
  if (!status.connected) {
    console.log('[Wake] Device disconnected after sleep — attempting reconnect');
    try {
      const scan = await window.plaud.scanKnown();
      if (scan.found) {
        const conn = await window.plaud.connect(scan.address, scan.serial_number);
        if (conn.connected) {
          toast(`Reconnected to ${scan.name}`, 'success');
          await updateDeviceStatus();

          // Auto-sync if enabled
          if (app.settings.auto_sync !== false) {
            const check = await window.plaud.checkNew();
            if (check.new_files > 0) {
              toast(`${check.new_files} new recording(s), syncing...`, 'info');
              try {
                await window.plaud.sync();
                await loadRecordings();
                toast('Sync complete', 'success');
              } catch {}
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

function showStartupError(error) {
  const h2 = dom.emptyState.querySelector('h2');
  const p = dom.emptyState.querySelector('p');

  // error is either a structured object { code, title, friendly, hint, detail } or a plain string
  const isStructured = error && typeof error === 'object' && error.code;
  const title = isStructured ? error.title : 'Server failed to start';
  const friendly = isStructured ? error.friendly : String(error);
  const hint = isStructured ? error.hint : '';
  const code = isStructured ? error.code : 'UNKNOWN';

  _startupErrorShown = true;
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

// Show startup progress messages (e.g. "Installing dependencies...")
window.plaud.onStartupStatus((msg) => {
  const p = dom.emptyState.querySelector('p');
  if (p && !_startupErrorShown) {
    p.textContent = msg;
  }
});
let _startupErrorShown = false;

// Init Electron-only features immediately (don't wait for Python API)
initSettingsEvents();

async function init() {
  // Check if main process already detected a startup error
  const apiInfo = await window.plaud.getApiInfo();
  if (apiInfo.error) {
    showStartupError(apiInfo.error);
    return;
  }

  // Show current startup status from main process
  if (apiInfo.status) {
    dom.emptyState.querySelector('p').textContent = apiInfo.status;
  }

  let apiReady = false;
  for (let i = 0; i < 30; i++) {
    try {
      const h = await window.plaud.health();
      if (h.status === 'ok') {
        apiReady = true;
        break;
      }
    } catch {}
    await new Promise((r) => setTimeout(r, 500));
  }

  if (!apiReady) {
    showStartupError({
      code: 'SERVER_TIMEOUT',
      title: 'Server not responding',
      friendly: 'The Python backend started but never became ready.',
      hint: 'Try restarting the app. Another instance may be running.',
    });
    return;
  }

  // Server is ready — reset empty state text to default
  dom.emptyState.querySelector('h2').textContent = 'No recordings yet';
  dom.emptyState.querySelector('p').textContent = 'Connect your Plaud NotePin S and sync recordings, or start a new recording from the device.';

  // Init sub-modules
  initAudioPlayer();

  await updateDeviceStatus();
  await loadCloudStatus();
  await loadNotionStatus();
  await loadSettings();
  await loadRecordings();

  // First-run setup — show onboarding if no paired devices and not dismissed
  const isFirstRun = !app.settings.has_paired_devices && !app.settings.setup_dismissed;
  if (isFirstRun) {
    showSetupWelcome();
    return; // completeSetup() will resume polling after onboarding finishes
  }

  // Immediately try to connect to a known device on startup
  try {
    const status = await window.plaud.status();
    if (!status.connected) {
      const scan = await window.plaud.scanKnown();
      if (scan.found) {
        const conn = await window.plaud.connect(scan.address, scan.serial_number);
        if (conn.connected) {
          toast(`Connected to ${scan.name}`, 'success');
          await updateDeviceStatus();
        }
      }
    }
  } catch {}

  _startBgPoll();
}

function _startBgPoll() {
  // Poll device status every 10 seconds
  app.pollInterval = setInterval(updateDeviceStatus, 10000);

  // Background device polling
  let _bgBusy = false;
  async function bgDevicePoll() {
    if (_bgBusy || app.syncing) return;
    _bgBusy = true;
    try {
      const status = await window.plaud.status();

      // Skip reconnect/sync if a sync is already running server-side
      if (status.syncing) {
        _bgBusy = false;
        return;
      }

      if (!status.connected) {
        const scan = await window.plaud.scanKnown();
        if (scan.found) {
          const conn = await window.plaud.connect(scan.address, scan.serial_number);
          if (conn.connected) {
            toast(`Auto-connected to ${scan.name}`, 'success');
            await updateDeviceStatus();
          }
        }
        // Don't spam toasts about device not found — user can see "Disconnected" in header
        _bgBusy = false;
        return;
      }

      // Auto-sync if enabled
      if (app.settings.auto_sync === false) {
        _bgBusy = false;
        return;
      }

      const check = await window.plaud.checkNew();
      if (check.new_files > 0) {
        // WiFi for large files — auto-execute or offer depending on setting
        if (check.total_size > WIFI_SIZE_THRESHOLD) {
          if (app.settings.auto_prefer_wifi) {
            executeWifiSync();
            _bgBusy = false;
            return;
          }
          const isBackground = !document.hasFocus();
          offerWifiSync(check.total_size, check.new_files, isBackground);
        }

        toast(`${check.new_files} new recording(s) found, syncing via Bluetooth...`, 'info');
        dom.progressBar.classList.remove('hidden');
        dom.progressBar.classList.add('indeterminate');
        app.syncing = true;
        try {
          const result = await window.plaud.sync();
          if (!result.error) {
            const newFiles = (result.files || []).filter((f) => f.status === 'new');
            if (newFiles.length > 0) {
              toast(`Synced ${newFiles.length} recording(s)`, 'success');

              // Auto-transcribe if enabled
              if (app.settings.auto_transcribe !== false) {
                for (const f of newFiles) {
                  if (f.ogg_path) {
                    try {
                      await window.plaud.transcribe(
                        f.ogg_path,
                        app.settings.model || 'small',
                        app.settings.diarize || false,
                        app.settings.language || 'en',
                      );
                    } catch {}
                  }
                }

                // Auto-summarise if enabled (requires auto-transcribe)
                if (app.settings.auto_summarise) {
                  for (const f of newFiles) {
                    if (f.ogg_path) {
                      try {
                        const sid = f.session_id || f.ogg_path.split('/').pop().replace('.ogg', '');
                        await window.plaud.summarize(sid);
                      } catch {}
                    }
                  }
                  toast('Auto-summarised new recordings', 'success');
                }
              }

              await loadRecordings();

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
          app.syncing = false;
          dom.progressBar.classList.remove('indeterminate');
          hideProgress();
          await updateDeviceStatus();
        }
      }
    } catch {}
    _bgBusy = false;
  }

  // First attempt after 5s, then every 45s (reduced from 30s to minimize BLE contention)
  setTimeout(bgDevicePoll, 5000);
  setInterval(bgDevicePoll, 45000);
}

init();
