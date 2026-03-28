/* ============================================================
   Plaud V2 — Settings Module
   ============================================================ */

async function loadSettings() {
  try {
    app.settings = await window.plaud.getSettings();
    renderSettings();
  } catch (err) {
    console.error('Failed to load settings:', err);
  }
}

function renderSettings() {
  // Model selector
  const modelSelector = $('#model-selector');
  const models = app.settings.models || {};
  modelSelector.innerHTML = Object.entries(models)
    .map(([name, info]) => {
      const selected = name === (app.settings.model || 'small');
      const disabled = !info.fits;
      return `
      <div class="model-option ${selected ? 'selected' : ''} ${disabled ? 'disabled' : ''}"
           data-model="${name}" ${disabled ? 'title="Not enough RAM"' : ''}>
        <div>
          <div class="model-name">${name}</div>
          <div class="model-detail">${info.speed} \u00B7 ${info.accuracy}</div>
        </div>
        <div class="model-size">${info.size} \u00B7 ~${info.ram_gb} GB RAM</div>
      </div>
    `;
    })
    .join('');

  modelSelector.querySelectorAll('.model-option:not(.disabled)').forEach((opt) => {
    opt.addEventListener('click', async () => {
      const model = opt.dataset.model;
      app.settings.model = model;
      await window.plaud.saveSettings({ model });
      renderSettings();
      toast(`Model set to ${model}`, 'success');
    });
  });

  // RAM info
  $('#ram-info').textContent = app.settings.ram_total_gb
    ? `System: ${app.settings.ram_total_gb} GB total, ${app.settings.ram_used_gb} GB used, ${app.settings.ram_headroom_gb} GB available`
    : '';

  // Language
  $('#language-select').value = app.settings.language || 'en';

  // Diarize toggle
  $('#diarize-toggle').checked = app.settings.diarize || false;

  // Subsections
  renderProfile();
  renderWebhooks();
  renderAIKeys();
  renderDeviceManager();
  renderNotion();
  renderCloudAccount();
}

// ---- Profile & Vocabulary ----

function renderProfile() {
  const p = app.settings.profile || {};
  $('#profile-job').value = p.job || '';
  $('#profile-industry').value = p.industry || '';
  $('#profile-seniority').value = p.seniority || '';
  $('#profile-usecases').value = p.usecases || '';
  $('#profile-instructions').value = p.instructions || '';
  renderVocabulary();
}

function renderVocabulary() {
  const vocab = app.settings.custom_vocabulary || [];
  const list = $('#vocab-list');
  list.innerHTML = vocab
    .map(
      (word, i) =>
        `<span class="vocab-item">${escapeHtml(word)}<button class="vocab-remove" data-index="${i}">&times;</button></span>`,
    )
    .join('');
  list.querySelectorAll('.vocab-remove').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const idx = parseInt(btn.dataset.index);
      const vocab = app.settings.custom_vocabulary || [];
      vocab.splice(idx, 1);
      app.settings.custom_vocabulary = vocab;
      await window.plaud.saveSettings({ custom_vocabulary: vocab });
      renderVocabulary();
    });
  });
}

// ---- Webhooks ----

function renderWebhooks() {
  const webhooks = app.settings.webhooks || [];
  const list = $('#webhook-list');
  list.innerHTML = webhooks
    .map((wh, i) => {
      const url = wh.url || '';
      const name = wh.name || '';
      const triggers = Object.entries(wh.triggers || {})
        .filter(([, v]) => v)
        .map(([k]) => k)
        .join(', ');
      return `<div class="webhook-item">
      <div class="webhook-info">
        ${name ? `<span class="webhook-name">${escapeHtml(name)}</span>` : ''}
        <span class="webhook-url" title="${escapeHtml(url)}">${escapeHtml(url)}</span>
      </div>
      <span class="webhook-status active">${escapeHtml(triggers || 'none')}</span>
      <button class="icon-btn text-destructive" data-index="${i}" title="Remove" style="flex-shrink:0;">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
          <path d="M10 4L4 10M4 4l6 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
        </svg>
      </button>
    </div>`;
    })
    .join('');
  list.querySelectorAll('.icon-btn[data-index]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const idx = parseInt(btn.dataset.index);
      const webhooks = app.settings.webhooks || [];
      webhooks.splice(idx, 1);
      app.settings.webhooks = webhooks;
      await window.plaud.saveSettings({ webhooks });
      renderWebhooks();
      toast('Webhook removed', 'success');
    });
  });
}

// ---- Cloud AI Keys ----

function renderAIKeys() {
  for (const provider of ['openai', 'anthropic', 'openrouter']) {
    const isSet = app.settings[`${provider}_key_set`];
    const badge = $(`#${provider}-key-badge`);
    const form = $(`#${provider}-key-form`);
    const connected = $(`#${provider}-key-connected`);

    if (isSet) {
      badge.textContent = 'Connected';
      badge.className = 'ai-key-badge connected';
      form.classList.add('hidden');
      connected.classList.remove('hidden');
    } else {
      badge.textContent = 'Not set';
      badge.className = 'ai-key-badge none';
      form.classList.remove('hidden');
      connected.classList.add('hidden');
    }
  }
}

async function handleKeyPaste(provider, key) {
  const statusEl = $(`#${provider}-key-status`);
  const input = $(`#${provider}-key-input`);

  statusEl.textContent = 'Validating key...';
  statusEl.className = 'ai-key-status validating';
  statusEl.classList.remove('hidden');

  try {
    const result = await window.plaud.validateKey(provider, key);
    if (result.valid) {
      statusEl.textContent = 'Key valid — saving...';
      statusEl.className = 'ai-key-status success';
      await window.plaud.saveSettings({ [`${provider}_api_key`]: key });
      app.settings[`${provider}_key_set`] = true;
      input.value = '';
      statusEl.classList.add('hidden');
      const names = { openai: 'OpenAI', anthropic: 'Anthropic', openrouter: 'OpenRouter' };
      toast(`${names[provider] || provider} key connected`, 'success');
      renderAIKeys();
    } else {
      statusEl.textContent = result.error || 'Invalid key';
      statusEl.className = 'ai-key-status error';
    }
  } catch (err) {
    statusEl.textContent = `Validation failed: ${err.message}`;
    statusEl.className = 'ai-key-status error';
  }
}

// ---- Notion ----

async function loadNotionStatus() {
  try {
    app.notionState = await window.plaud.notionStatus();
  } catch {
    app.notionState = { connected: false };
  }
}

function renderNotion() {
  const disconnected = $('#notion-disconnected');
  const connected = $('#notion-connected');
  if (app.notionState.connected) {
    disconnected.classList.add('hidden');
    connected.classList.remove('hidden');
    $('#notion-db-name').textContent = app.notionState.database_name || 'No database selected';
  } else {
    disconnected.classList.remove('hidden');
    connected.classList.add('hidden');
  }
  // Set auto-push toggle state
  const autoPushToggle = $('#notion-auto-push-toggle');
  if (autoPushToggle) {
    autoPushToggle.checked = app.settings.notion_auto_push || false;
  }
}

// ---- Cloud Account ----

async function loadCloudStatus() {
  try {
    app.cloudState = await window.plaud.cloudStatus();
  } catch {
    app.cloudState = { logged_in: false, user: null };
  }
}

function renderCloudAccount() {
  const loggedOut = $('#cloud-logged-out');
  const loggedIn = $('#cloud-logged-in');

  const savedDirection = app.settings.cloud_sync_direction || 'download';
  const dlRadio = $('#sync-download');
  const twRadio = $('#sync-twoway');
  if (dlRadio && twRadio) {
    dlRadio.checked = savedDirection === 'download';
    twRadio.checked = savedDirection === 'twoway';
  }

  if (app.cloudState.logged_in) {
    loggedOut.classList.add('hidden');
    loggedIn.classList.remove('hidden');
    const userName = $('#cloud-user-name');
    if (app.cloudState.user) {
      const raw = app.cloudState.user;
      const u = raw.data_user || raw;
      userName.textContent = u.nickname || u.email || u.phone || 'Plaud User';
    } else {
      userName.textContent = 'Plaud User';
    }
  } else {
    loggedOut.classList.remove('hidden');
    loggedIn.classList.add('hidden');
  }
}

// ---- Device Manager ----

function renderDeviceManager() {
  const dmDisconnected = $('#dm-disconnected');
  const dmConnected = $('#dm-connected');

  if (app.deviceStateCache && app.deviceStateCache.connected) {
    dmDisconnected.classList.add('hidden');
    dmConnected.classList.remove('hidden');
    $('#dm-device-name').textContent = app.deviceStateCache.device_name || 'Plaud NotePin S';
    const sn = app.deviceStateCache.serial_number;
    $('#dm-device-meta').textContent = sn ? `SN: ${sn}` : '';
    const bat = app.deviceStateCache.battery;
    $('#dm-device-battery').textContent = bat != null ? `${bat}%` : '';
    // Show RSSI if available
    const dmRssi = $('#dm-device-rssi');
    if (dmRssi && app.deviceStateCache.rssi != null) {
      dmRssi.innerHTML =
        renderSignalBars(app.deviceStateCache.rssi) +
        ` <span class="dm-rssi-value">${app.deviceStateCache.rssi} dBm</span>`;
    }
    // Fetch full device info (async, populates panel when ready)
    fetchDeviceInfo();
  } else {
    dmDisconnected.classList.remove('hidden');
    dmConnected.classList.add('hidden');
  }
}

let _deviceInfoFetching = false;
async function fetchDeviceInfo() {
  const panel = $('#dm-info-panel');
  if (!panel || _deviceInfoFetching) return;
  _deviceInfoFetching = true;
  try {
    const info = await window.plaud.deviceInfo();
    if (info.error) {
      _deviceInfoFetching = false;
      return;
    }
    renderDeviceInfoPanel(panel, info);
  } catch {}
  _deviceInfoFetching = false;
}

function formatBytes(bytes) {
  if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(1)} GB`;
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(0)} MB`;
  return `${(bytes / 1024).toFixed(0)} KB`;
}

function renderDeviceInfoPanel(panel, info) {
  const items = [];

  // Storage
  if (info.storage_total_bytes) {
    const used = formatBytes(info.storage_used_bytes || 0);
    const total = formatBytes(info.storage_total_bytes);
    const pct = info.storage_used_pct || 0;
    const remaining = info.storage_remaining_hours || 0;
    items.push(`<div class="dm-info-item full-width">
      <div class="dm-info-label">Storage</div>
      <div class="dm-info-value">${used} / ${total} <span class="dm-info-secondary">(${remaining}h remaining)</span></div>
      <div class="dm-info-bar"><div class="dm-info-bar-fill" style="width:${pct}%"></div></div>
    </div>`);
  }

  // Firmware
  if (info.firmware_version) {
    items.push(`<div class="dm-info-item">
      <div class="dm-info-label">Firmware</div>
      <div class="dm-info-value">${escapeHtml(info.firmware_version)}</div>
    </div>`);
  }

  // Battery + charging
  if (info.battery_level != null) {
    const charging = info.charging ? ' (Charging)' : '';
    items.push(`<div class="dm-info-item">
      <div class="dm-info-label">Battery</div>
      <div class="dm-info-value">${info.battery_level}%${charging}</div>
    </div>`);
  }

  // Recording status
  if (info.scene_name != null) {
    const dot = info.is_recording ? 'on' : 'off';
    items.push(`<div class="dm-info-item">
      <div class="dm-info-label">Status</div>
      <div class="dm-info-value"><span class="dm-info-dot ${dot}"></span>${escapeHtml(info.scene_name)}</div>
    </div>`);
  }

  // USB
  if (info.usb_connected != null) {
    const dot = info.usb_connected ? 'on' : 'off';
    const label = info.usb_connected ? 'Connected' : 'Disconnected';
    items.push(`<div class="dm-info-item">
      <div class="dm-info-label">USB</div>
      <div class="dm-info-value"><span class="dm-info-dot ${dot}"></span>${label}</div>
    </div>`);
  }

  // Active session
  if (info.active_session_id && info.active_session_id > 0) {
    items.push(`<div class="dm-info-item">
      <div class="dm-info-label">Active Session</div>
      <div class="dm-info-value">${info.active_session_id}</div>
    </div>`);
  }

  // Accelerometer
  if (info.accelerometer) {
    const a = info.accelerometer;
    items.push(`<div class="dm-info-item full-width">
      <div class="dm-info-label">Orientation</div>
      <div class="dm-info-value">Pitch ${a.pitch}\u00B0 \u00B7 Roll ${a.roll}\u00B0 \u00B7 Yaw ${a.yaw}\u00B0</div>
    </div>`);
  }

  // Build the panel: grid for info cards, then standalone toggle rows below
  const autoDelete = app.settings.auto_delete_after_sync || false;
  const toggles = `<div class="dm-toggles">
    <label class="toggle-label dm-toggle-row">
      <input type="checkbox" id="dm-auto-delete-toggle" class="toggle-input" ${autoDelete ? 'checked' : ''}>
      <span class="toggle-switch"></span>
      <span>Delete recordings from device after sync</span>
    </label>
  </div>`;

  panel.innerHTML = `<div class="dm-info-grid">${items.join('')}</div>${toggles}`;

  // Wire up auto-delete toggle
  const autoDeleteToggle = document.getElementById('dm-auto-delete-toggle');
  if (autoDeleteToggle) {
    autoDeleteToggle.addEventListener('change', async (e) => {
      const enabled = e.target.checked;
      await window.plaud.saveSettings({ auto_delete_after_sync: enabled });
      app.settings.auto_delete_after_sync = enabled;
      toast(
        enabled ? 'Files will be deleted from device after sync' : 'Files will be kept on device after sync',
        'success',
      );
    });
  }
}

function rssiToSignalLevel(rssi) {
  if (rssi >= -50) return 4;
  if (rssi >= -65) return 3;
  if (rssi >= -80) return 2;
  if (rssi >= -90) return 1;
  return 0;
}

function rssiToLabel(rssi) {
  if (rssi >= -50) return 'Excellent';
  if (rssi >= -65) return 'Good';
  if (rssi >= -80) return 'Fair';
  return 'Weak';
}

function renderSignalBars(rssi) {
  const level = rssiToSignalLevel(rssi);
  const heights = [4, 7, 10, 13];
  return `<div class="dm-signal" title="Signal: ${rssi} dBm (${rssiToLabel(rssi)})">
    ${heights
      .map((h, i) => `<div class="dm-signal-bar ${i < level ? 'active' : ''}" style="height:${h}px;"></div>`)
      .join('')}
  </div>`;
}

// ---- Device Status ----

function renderHeaderSignal(rssi) {
  const el = $('#header-signal');
  if (rssi == null) {
    el.classList.add('hidden');
    return;
  }
  el.classList.remove('hidden');
  const level = rssiToSignalLevel(rssi);
  const heights = [3, 5, 8, 11];
  el.innerHTML = heights
    .map((h, i) => `<div class="header-signal-bar ${i < level ? 'active' : ''}" style="height:${h}px;"></div>`)
    .join('');
  el.title = `Signal: ${rssi} dBm (${rssiToLabel(rssi)})`;
}

async function updateDeviceStatus() {
  try {
    const status = await window.plaud.status();
    app.deviceStateCache = status;
    if (status.connected) {
      dom.statusEl.className = 'device-status connected';
      dom.statusText.textContent = status.device_name || 'Connected';
      const battStr = status.battery != null ? `${status.battery}%` : '';
      const chargingStr = status.charging ? ' \u26A1' : '';
      dom.batteryText.textContent = battStr + chargingStr;
      dom.scanBtn.classList.add('hidden');
      dom.syncBtn.classList.remove('hidden');
      dom.disconnectBtn.classList.remove('hidden');
      renderHeaderSignal(status.rssi);
    } else {
      dom.statusEl.className = 'device-status disconnected';
      dom.statusText.textContent = 'Disconnected';
      dom.batteryText.textContent = '';
      dom.scanBtn.classList.remove('hidden');
      dom.syncBtn.classList.add('hidden');
      dom.disconnectBtn.classList.add('hidden');
      renderHeaderSignal(null);
    }
    renderDeviceManager();
  } catch {
    dom.statusEl.className = 'device-status disconnected';
    dom.statusText.textContent = 'API unavailable';
    renderHeaderSignal(null);
  }
}

// RSSI is read inline during the 10s status poll — no separate poller needed

// ---- Settings Event Wiring ----

function initSettingsEvents() {
  // Settings tabs — translate vertical scroll to horizontal (mouse wheel)
  // but let native horizontal scroll through (trackpad two-finger swipe)
  document.querySelector('.settings-tabs').addEventListener(
    'wheel',
    (e) => {
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        // Trackpad horizontal swipe — let it scroll natively
        return;
      }
      // Mouse wheel vertical — convert to horizontal
      e.preventDefault();
      e.currentTarget.scrollLeft += e.deltaY;
    },
    { passive: false },
  );

  // Tab switching
  document.querySelectorAll('.settings-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.settings-tab').forEach((t) => t.classList.remove('active'));
      document.querySelectorAll('.settings-tab-content').forEach((c) => c.classList.remove('active'));
      tab.classList.add('active');
      const content = document.querySelector(`.settings-tab-content[data-tab="${tab.dataset.tab}"]`);
      if (content) content.classList.add('active');
    });
  });

  // App version + check for updates
  (async () => {
    try {
      const version = await window.plaud.getAppVersion();
      const el = $('#app-version');
      if (el) el.textContent = `Version ${version}`;
    } catch (e) {
      console.error('[Settings] Failed to get app version:', e);
    }
  })();

  $('#check-updates-btn').addEventListener('click', async () => {
    const statusEl = $('#update-check-status');
    statusEl.textContent = 'Checking...';
    try {
      const resultPromise = new Promise((resolve) => {
        app.updateListeners.push(resolve);
        setTimeout(() => resolve({ status: 'timeout' }), 10000);
      });
      window.plaud.checkForUpdates();
      const result = await resultPromise;
      if (result.status === 'up-to-date') {
        statusEl.textContent = 'You\'re up to date!';
      } else if (result.status === 'available' || result.status === 'downloading' || result.status === 'ready') {
        statusEl.textContent = '';
      } else if (result.status === 'error') {
        statusEl.textContent = `Update error: ${result.message || 'Unknown'}`;
      } else if (result.status === 'timeout') {
        statusEl.textContent = 'No response — check may not work in dev mode';
      }
      setTimeout(() => { statusEl.textContent = ''; }, 5000);
    } catch (e) {
      statusEl.textContent = `Error: ${e.message || 'Could not check'}`;
      setTimeout(() => { statusEl.textContent = ''; }, 5000);
    }
  });

  // System settings
  (async () => {
    try {
      const login = await window.plaud.getLoginItem();
      $('#launch-login-toggle').checked = login.enabled;
    } catch {}
    try {
      const dock = await window.plaud.getDockVisible();
      $('#show-dock-toggle').checked = dock.visible;
    } catch {}
  })();

  $('#launch-login-toggle').addEventListener('change', async (e) => {
    await window.plaud.setLoginItem(e.target.checked);
    toast(e.target.checked ? 'Will open at login' : 'Disabled open at login', 'success');
  });

  $('#show-dock-toggle').addEventListener('change', async (e) => {
    await window.plaud.setDockVisible(e.target.checked);
    toast(e.target.checked ? 'Dock icon visible' : 'Dock icon hidden', 'success');
  });

  $('#language-select').addEventListener('change', async (e) => {
    app.settings.language = e.target.value;
    await window.plaud.saveSettings({ language: e.target.value });
    toast('Language saved', 'success');
  });

  $('#diarize-toggle').addEventListener('change', async (e) => {
    app.settings.diarize = e.target.checked;
    await window.plaud.saveSettings({ diarize: e.target.checked });
    toast(e.target.checked ? 'Diarization enabled' : 'Diarization disabled', 'success');
  });

  // Automation pipeline toggles with cascade disable
  function updateAutoPipelineState() {
    const syncOn = $('#auto-sync-toggle').checked;
    const transcribeOn = $('#auto-transcribe-toggle').checked;

    // Transcribe depends on sync
    const transcribeRow = $('#auto-transcribe-row');
    const transcribeToggle = $('#auto-transcribe-toggle');
    if (!syncOn) {
      transcribeRow.classList.add('disabled');
      transcribeToggle.disabled = true;
    } else {
      transcribeRow.classList.remove('disabled');
      transcribeToggle.disabled = false;
    }

    // Summarise depends on sync + transcribe
    const summariseRow = $('#auto-summarise-row');
    const summariseToggle = $('#auto-summarise-toggle');
    if (!syncOn || !transcribeOn) {
      summariseRow.classList.add('disabled');
      summariseToggle.disabled = true;
    } else {
      summariseRow.classList.remove('disabled');
      summariseToggle.disabled = false;
    }
  }

  // Initialize toggle states
  $('#auto-sync-toggle').checked = app.settings.auto_sync !== false;
  $('#auto-transcribe-toggle').checked = app.settings.auto_transcribe !== false;
  $('#auto-summarise-toggle').checked = app.settings.auto_summarise || false;
  updateAutoPipelineState();

  $('#auto-sync-toggle').addEventListener('change', async (e) => {
    app.settings.auto_sync = e.target.checked;
    await window.plaud.saveSettings({ auto_sync: e.target.checked });
    updateAutoPipelineState();
    toast(e.target.checked ? 'Auto-sync enabled' : 'Auto-sync disabled', 'success');
  });

  $('#auto-transcribe-toggle').addEventListener('change', async (e) => {
    app.settings.auto_transcribe = e.target.checked;
    await window.plaud.saveSettings({ auto_transcribe: e.target.checked });
    updateAutoPipelineState();
    toast(e.target.checked ? 'Auto-transcribe enabled' : 'Auto-transcribe disabled', 'success');
  });

  $('#auto-summarise-toggle').addEventListener('change', async (e) => {
    app.settings.auto_summarise = e.target.checked;
    await window.plaud.saveSettings({ auto_summarise: e.target.checked });
    toast(e.target.checked ? 'Auto-summarise enabled' : 'Auto-summarise disabled', 'success');
  });

  // Profile save
  $('#profile-save-btn').addEventListener('click', async () => {
    const profile = {
      job: $('#profile-job').value.trim(),
      industry: $('#profile-industry').value.trim(),
      seniority: $('#profile-seniority').value,
      usecases: $('#profile-usecases').value.trim(),
      instructions: $('#profile-instructions').value.trim(),
    };
    await window.plaud.saveSettings({ profile });
    app.settings.profile = profile;
    toast('Profile saved', 'success');
  });

  // Vocabulary
  $('#vocab-add-btn').addEventListener('click', async () => {
    const input = $('#vocab-input');
    const word = input.value.trim();
    if (!word) return;
    const vocab = app.settings.custom_vocabulary || [];
    if (!vocab.includes(word)) {
      vocab.push(word);
      app.settings.custom_vocabulary = vocab;
      await window.plaud.saveSettings({ custom_vocabulary: vocab });
    }
    input.value = '';
    renderVocabulary();
  });

  $('#vocab-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      $('#vocab-add-btn').click();
    }
  });

  // Webhooks
  $('#webhook-add-btn').addEventListener('click', async () => {
    const input = $('#webhook-url-input');
    const url = input.value.trim();
    if (!url) {
      toast('Enter a webhook URL', 'error');
      return;
    }
    try {
      new URL(url);
    } catch {
      toast('Invalid URL', 'error');
      return;
    }

    const triggers = {
      transcribe: $('#webhook-on-transcribe').checked,
      summarize: $('#webhook-on-summarize').checked,
      sync: $('#webhook-on-sync').checked,
    };

    toast('Testing webhook...', 'info');
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          event: 'test',
          timestamp: new Date().toISOString(),
          data: { message: 'Plaud V2 webhook test' },
        }),
      });
      if (!resp.ok) {
        toast(`Webhook returned ${resp.status} — added anyway`, 'info');
      } else {
        toast('Webhook validated and added', 'success');
      }
    } catch {
      toast('Could not reach URL — added anyway (check URL)', 'info');
    }

    const nameInput = $('#webhook-name-input');
    const name = nameInput ? nameInput.value.trim() : '';
    const webhooks = app.settings.webhooks || [];
    webhooks.push({ url, name, triggers });
    app.settings.webhooks = webhooks;
    await window.plaud.saveSettings({ webhooks });
    input.value = '';
    if (nameInput) nameInput.value = '';
    renderWebhooks();
  });

  // AI key auto-paste + validation
  const keyPrefixes = { openai: 'sk-', anthropic: 'sk-ant-', openrouter: 'sk-or-' };
  for (const provider of ['openai', 'anthropic', 'openrouter']) {
    const input = $(`#${provider}-key-input`);
    const prefix = keyPrefixes[provider];

    input.addEventListener('paste', (e) => {
      setTimeout(() => {
        const val = input.value.trim();
        if (val && val.startsWith(prefix)) {
          handleKeyPaste(provider, val);
        }
      }, 50);
    });

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const val = input.value.trim();
        if (val) handleKeyPaste(provider, val);
      }
    });
  }

  // Remove key buttons
  $('#openai-key-remove').addEventListener('click', async () => {
    await window.plaud.saveSettings({ remove_openai_key: true });
    app.settings.openai_key_set = false;
    toast('OpenAI key removed', 'info');
    renderAIKeys();
  });

  $('#anthropic-key-remove').addEventListener('click', async () => {
    await window.plaud.saveSettings({ remove_anthropic_key: true });
    app.settings.anthropic_key_set = false;
    toast('Anthropic key removed', 'info');
    renderAIKeys();
  });

  $('#openrouter-key-remove').addEventListener('click', async () => {
    await window.plaud.saveSettings({ remove_openrouter_key: true });
    app.settings.openrouter_key_set = false;
    toast('OpenRouter key removed', 'info');
    renderAIKeys();
  });

  // External links
  $('#openai-key-link').addEventListener('click', (e) => {
    e.preventDefault();
    window.plaud.openExternal('https://platform.openai.com/api-keys');
  });

  $('#anthropic-key-link').addEventListener('click', (e) => {
    e.preventDefault();
    window.plaud.openExternal('https://console.anthropic.com/settings/keys');
  });

  $('#openrouter-key-link').addEventListener('click', (e) => {
    e.preventDefault();
    window.plaud.openExternal('https://openrouter.ai/settings/keys');
  });

  // Notion connect/disconnect
  $('#notion-connect-btn').addEventListener('click', async () => {
    const btn = $('#notion-connect-btn');
    setLoading(btn, true);
    try {
      const result = await window.plaud.notionConnect();
      if (result.error) {
        toast(`Notion error: ${result.error}`, 'error');
      } else if (result.url) {
        window.plaud.openExternal(result.url);
        toast('Sign in with Notion in your browser...', 'info');
        const pollInterval = setInterval(async () => {
          const status = await window.plaud.notionStatus();
          if (status.connected) {
            clearInterval(pollInterval);
            app.notionState = status;
            renderNotion();
            toast('Notion connected!', 'success');
            setLoading(btn, false);
          }
        }, 2000);
        setTimeout(() => {
          clearInterval(pollInterval);
          setLoading(btn, false);
        }, 120000);
      }
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
      setLoading(btn, false);
    }
  });

  $('#notion-disconnect-btn').addEventListener('click', async () => {
    await window.plaud.notionDisconnect();
    app.notionState = { connected: false };
    renderNotion();
    toast('Notion disconnected', 'info');
  });

  // Notion auto-push toggle
  $('#notion-auto-push-toggle').addEventListener('change', async (e) => {
    const enabled = e.target.checked;
    await window.plaud.saveSettings({ notion_auto_push: enabled });
    app.settings.notion_auto_push = enabled;
    toast(enabled ? 'Auto-push to Notion enabled' : 'Auto-push to Notion disabled', 'success');
  });

  // Notion database search (debounced)
  let _notionSearchTimeout = null;
  $('#notion-db-search').addEventListener('input', (e) => {
    clearTimeout(_notionSearchTimeout);
    const query = e.target.value.trim();
    const list = $('#notion-db-list');

    if (!query) {
      list.classList.add('hidden');
      return;
    }

    list.classList.remove('hidden');
    list.innerHTML = '<div class="dm-scanning">Searching</div>';

    _notionSearchTimeout = setTimeout(async () => {
      try {
        const result = await window.plaud.notionDatabases(query);
        if (result.error) {
          list.innerHTML = `<p class="settings-desc" style="color:var(--destructive);">${escapeHtml(result.error)}</p>`;
          return;
        }
        const dbs = result.databases || [];
        if (dbs.length === 0) {
          list.innerHTML =
            '<p class="settings-desc" style="text-align:center;padding:12px;">No results. Try a different search or paste a database URL below.</p>';
          return;
        }

        list.innerHTML = dbs
          .map((db, i) => {
            const title = String(db.title || `Result ${i + 1}`).substring(0, 80);
            const type =
              db.type && db.type !== 'raw' ? `<span style="opacity:0.5;margin-left:4px;">${db.type}</span>` : '';
            return `<div class="dm-device-row" data-index="${i}">
            <div class="dm-device-row-info">
              <div class="dm-device-row-name">${escapeHtml(title)}${type}</div>
            </div>
          </div>`;
          })
          .join('');

        list.querySelectorAll('.dm-device-row').forEach((row) => {
          row.addEventListener('click', async () => {
            const idx = parseInt(row.dataset.index);
            const db = dbs[idx];
            const dbId = String(db.id || '');
            const dbName = String(db.title || `Database ${idx + 1}`).substring(0, 60);
            if (!dbId) {
              toast('Could not determine database ID — try pasting the URL instead', 'error');
              return;
            }
            await window.plaud.notionSetDatabase(dbId, dbName);
            app.notionState.database_id = dbId;
            app.notionState.database_name = dbName;
            renderNotion();
            list.classList.add('hidden');
            $('#notion-db-search').value = '';
            toast(`Database selected: ${dbName}`, 'success');
          });
        });
      } catch (err) {
        list.innerHTML = `<p class="settings-desc" style="color:var(--destructive);">Error: ${escapeHtml(err.message)}</p>`;
      }
    }, 500);
  });

  $('#notion-db-url-save').addEventListener('click', async () => {
    const url = $('#notion-db-url-input').value.trim();
    if (!url) {
      toast('Please paste a Notion database URL', 'error');
      return;
    }
    const btn = $('#notion-db-url-save');
    setLoading(btn, true);
    try {
      const result = await window.plaud.notionSetDatabaseUrl(url);
      if (result.error) {
        toast(`Failed: ${result.error}`, 'error');
      } else {
        app.notionState.database_id = result.database_id;
        app.notionState.database_name = result.database_name;
        $('#notion-db-url-input').value = '';
        renderNotion();
        toast(`Database set: ${result.database_name}`, 'success');
      }
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    } finally {
      setLoading(btn, false);
    }
  });

  // Cloud login (OAuth browser flow)
  $('#cloud-login-browser-btn').addEventListener('click', async () => {
    try {
      const result = await window.plaud.cloudLoginUrl('eu');
      if (result.url) {
        app.pendingDesktopUuid = result.desktop_uuid;
        window.plaud.openExternal(result.url);

        const loggedOut = $('#cloud-logged-out');
        loggedOut.innerHTML = `
          <div style="text-align:center;padding:16px 0;">
            <div class="settings-desc" style="margin-bottom:12px;">Waiting for login in browser...</div>
            <div style="color:var(--text-muted);font-size:12px;margin-bottom:16px;">
              Sign into your Plaud account in the browser. You'll be redirected back automatically.
            </div>
            <div class="cloud-divider" style="margin:12px 0;"><span>or paste your bearer token</span></div>
            <div class="token-input-row" style="margin-top:12px;">
              <input type="password" id="cloud-token-input-waiting" class="settings-input" placeholder="Paste bearer token...">
              <button id="cloud-token-login-btn-waiting" class="btn-small">Login</button>
            </div>
            <button id="cloud-cancel-login" class="action-btn subtle" style="margin-top:12px;width:100%;justify-content:center;font-size:12px;">
              Cancel
            </button>
          </div>
        `;
        $('#cloud-token-login-btn-waiting').addEventListener('click', async () => {
          const token = $('#cloud-token-input-waiting').value.trim();
          if (!token) {
            toast('Please paste a token', 'error');
            return;
          }
          try {
            const res = await window.plaud.cloudLoginToken(token, 'eu');
            if (res.logged_in) {
              app.cloudState = { logged_in: true, user: res.user };
              toast('Plaud account connected!', 'success');
              renderCloudAccount();
            } else {
              toast(res.error || 'Token invalid', 'error');
            }
          } catch (err) {
            toast(`Error: ${err.message}`, 'error');
          }
        });
        $('#cloud-cancel-login').addEventListener('click', () => {
          app.pendingDesktopUuid = null;
          renderCloudAccount();
        });
      }
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    }
  });

  // Direct token login
  $('#cloud-token-login-btn').addEventListener('click', async () => {
    const token = $('#cloud-token-input').value.trim();
    if (!token) {
      toast('Please paste a token', 'error');
      return;
    }
    try {
      const res = await window.plaud.cloudLoginToken(token, 'eu');
      if (res.logged_in) {
        app.cloudState = { logged_in: true, user: res.user };
        toast('Plaud account connected!', 'success');
        renderCloudAccount();
      } else {
        toast(res.error || 'Token invalid', 'error');
      }
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    }
  });

  // Sync direction
  $('#sync-download').addEventListener('change', async () => {
    await window.plaud.saveSettings({ cloud_sync_direction: 'download' });
  });
  $('#sync-twoway').addEventListener('change', async () => {
    await window.plaud.saveSettings({ cloud_sync_direction: 'twoway' });
  });

  // Cloud sync button
  $('#cloud-sync-btn').addEventListener('click', async () => {
    const btn = $('#cloud-sync-btn');
    setLoading(btn, true);
    const statusEl = $('#cloud-sync-status');
    const direction = $('#sync-twoway').checked ? 'twoway' : 'download';
    statusEl.textContent = direction === 'twoway' ? 'Syncing (download + upload)...' : 'Downloading from cloud...';

    try {
      const result = await window.plaud.cloudSync(direction);
      if (result.error) {
        toast(`Cloud sync failed: ${result.error}`, 'error');
        statusEl.textContent = `Error: ${result.error}`;
      } else {
        const parts = [];
        const dl = result.download || {};
        const dlFiles = dl.files || [];
        const newDown = dlFiles.filter((f) => f.status === 'new');
        if (newDown.length > 0) parts.push(`${newDown.length} downloaded`);

        const ul = result.upload || {};
        const uploaded = (ul.uploaded || []).filter((u) => u.status === 'uploaded');
        const uploadErrors = (ul.uploaded || []).filter((u) => u.status === 'error');
        if (uploaded.length > 0) parts.push(`${uploaded.length} uploaded`);
        if (uploadErrors.length > 0) parts.push(`${uploadErrors.length} upload failed`);

        const summary = parts.length > 0 ? parts.join(', ') : 'Everything up to date';
        statusEl.textContent = summary;
        toast(`Cloud sync: ${summary}`, 'success');

        if (newDown.length > 0) {
          statusEl.textContent = `Transcribing ${newDown.length} new recording(s)...`;
          for (let i = 0; i < newDown.length; i++) {
            const f = newDown[i];
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
          statusEl.textContent = `${summary} — transcription complete`;
        }

        await loadRecordings();
      }
    } catch (err) {
      toast(`Cloud sync error: ${err.message}`, 'error');
      statusEl.textContent = `Error: ${err.message}`;
    } finally {
      setLoading(btn, false);
    }
  });

  // Cloud logout
  $('#cloud-logout-btn').addEventListener('click', async () => {
    try {
      await window.plaud.cloudLogout();
      app.cloudState = { logged_in: false, user: null };
      toast('Plaud account disconnected', 'info');
      renderCloudAccount();
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    }
  });

  // Plaud web link
  $('#plaud-web-link').addEventListener('click', (e) => {
    e.preventDefault();
    window.plaud.openExternal('https://web.plaud.ai');
  });

  // Device manager scan
  $('#dm-scan-btn').addEventListener('click', async () => {
    const btn = $('#dm-scan-btn');
    const results = $('#dm-scan-results');
    setLoading(btn, true);
    results.classList.remove('hidden');
    results.innerHTML = '<div class="dm-scanning">Scanning for nearby devices</div>';

    try {
      const data = await window.plaud.scanAll();
      const devices = data.devices || [];

      if (devices.length === 0) {
        results.innerHTML = `<p class="settings-desc" style="text-align:center;padding:12px;">
          No devices found. Make sure your Plaud is nearby and not connected to the phone app.
        </p>`;
      } else {
        results.innerHTML = devices
          .map((d) => {
            const sn = d.serial_number ? `SN: ${d.serial_number}` : '';
            const model = d.model || '';
            const detail = [model, sn, `${d.rssi} dBm`].filter(Boolean).join(' · ');
            return `
            <div class="dm-device-row" data-address="${d.address}" data-name="${escapeHtml(d.name || 'Plaud')}" data-sn="${escapeHtml(d.serial_number || '')}">
              <div class="dm-device-row-icon">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><rect x="5" y="1" width="6" height="14" rx="2"/></svg>
              </div>
              <div class="dm-device-row-info">
                <div class="dm-device-row-name">${escapeHtml(d.name || 'Plaud Device')}</div>
                <div class="dm-device-row-detail">${escapeHtml(detail)}</div>
              </div>
              ${renderSignalBars(d.rssi)}
            </div>
          `;
          })
          .join('');

        results.querySelectorAll('.dm-device-row').forEach((row) => {
          row.addEventListener('click', async () => {
            const address = row.dataset.address;
            const name = row.dataset.name;
            const sn = row.dataset.sn;
            results.innerHTML = `<div class="dm-scanning">Connecting to ${name}</div>`;
            try {
              const conn = await window.plaud.connect(address, sn);
              if (conn.connected) {
                toast(`Connected to ${name}`, 'success');
                await updateDeviceStatus();
                renderDeviceManager();
                results.classList.add('hidden');
              } else {
                toast(`Connection failed: ${conn.error || 'Unknown error'}`, 'error');
                results.innerHTML = `<p class="settings-desc" style="text-align:center;color:var(--primary);">
                  Connection failed. ${conn.error || 'Try again.'}
                </p>`;
              }
            } catch (err) {
              toast(`Error: ${err.message}`, 'error');
            }
          });
        });
      }
    } catch (err) {
      results.innerHTML = `<p class="settings-desc" style="color:var(--primary);">Scan error: ${err.message}</p>`;
    } finally {
      setLoading(btn, false);
    }
  });

  // Device manager force sync
  $('#dm-sync-btn').addEventListener('click', async () => {
    const btn = $('#dm-sync-btn');
    setLoading(btn, true);
    dom.progressBar.classList.remove('hidden');
    dom.progressBar.classList.add('indeterminate');
    toast('Syncing from device...', 'info');
    try {
      const result = await window.plaud.sync();
      if (result.error) {
        toast(`Sync failed: ${result.error}`, 'error');
      } else {
        const newFiles = (result.files || []).filter((f) => f.status === 'new');
        toast(`Sync complete: ${newFiles.length} new recording(s)`, 'success');
        if (newFiles.length > 0) {
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
          await loadRecordings();
        }
      }
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    } finally {
      dom.progressBar.classList.remove('indeterminate');
      hideProgress();
      setLoading(btn, false);
    }
  });

  // Device manager disconnect/depair
  $('#dm-disconnect-btn').addEventListener('click', async () => {
    try {
      await window.plaud.disconnect();
      toast('Disconnected', 'info');
      await updateDeviceStatus();
      renderDeviceManager();
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    }
  });

  $('#dm-depair-btn').addEventListener('click', async () => {
    const btn = $('#dm-depair-btn');
    if (btn.dataset.confirmed !== 'yes') {
      btn.textContent = 'Confirm Erase?';
      btn.style.background = '#e74c3c';
      btn.dataset.confirmed = 'yes';
      setTimeout(() => {
        if (btn.dataset.confirmed === 'yes') {
          btn.innerHTML = 'Erase &amp; Disconnect';
          btn.style.background = '';
          btn.dataset.confirmed = '';
        }
      }, 3000);
      return;
    }
    btn.dataset.confirmed = '';
    btn.innerHTML = 'Erase &amp; Disconnect';
    btn.style.background = '';

    setLoading(btn, true);
    try {
      const result = await window.plaud.depair();
      if (result.depaired) {
        toast('Device unpaired. It can now pair with another phone or computer.', 'success');
      } else {
        toast(result.error || 'Depair may have failed', 'error');
      }
      await updateDeviceStatus();
      renderDeviceManager();
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    } finally {
      setLoading(btn, false);
    }
  });
}
