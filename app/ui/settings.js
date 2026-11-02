/* ============================================================
   Plaud V2 — Settings Module
   ============================================================ */

// OAuth poll handles — stored at module scope so repeated clicks don't accumulate intervals
let _notionPollHandle = null;
let _notionPollTimeoutHandle = null;
let _gdrivePollHandle = null;
let _gdrivePollTimeoutHandle = null;

async function loadSettings() {
  try {
    app.settings = await window.plaud.getSettings();
    renderSettings();
  } catch (err) {
    console.error('Failed to load settings:', err);
  }
}

// Provider metadata — capabilities and key info
const PROVIDERS = {
  deepgram: { name: 'Deepgram', prefix: '', url: 'https://console.deepgram.com/', placeholder: 'Paste API key', caps: ['transcription', 'diarization'] },
  groq: { name: 'Groq', prefix: 'gsk_', url: 'https://console.groq.com/keys', placeholder: 'Paste API key (gsk_...)', caps: ['transcription'] },
  openai: { name: 'OpenAI', prefix: 'sk-', url: 'https://platform.openai.com/api-keys', placeholder: 'Paste API key (sk-...)', caps: ['transcription', 'summarization'] },
  anthropic: { name: 'Anthropic', prefix: 'sk-ant-', url: 'https://console.anthropic.com/settings/keys', placeholder: 'Paste API key (sk-ant-...)', caps: ['summarization'] },
  openrouter: { name: 'OpenRouter', prefix: 'sk-or-', url: 'https://openrouter.ai/settings/keys', placeholder: 'Paste API key (sk-or-...)', caps: ['summarization'] },
};

function renderSettings() {
  // Language
  $('#language-select').value = app.settings.language || 'en';

  // English variant — show only when language is English or auto
  const engVariant = $('#english-variant-group');
  const lang = app.settings.language || 'en';
  if (engVariant) engVariant.classList.toggle('hidden', lang !== 'en' && lang !== 'auto');
  const engSelect = $('#english-variant-select');
  if (engSelect) engSelect.value = app.settings.english_variant || 'us';

  // Auto-pipeline toggles — must be set here (not just initSettingsEvents)
  // because settings load async after init
  const syncToggle = $('#auto-sync-toggle');
  const transcribeToggle = $('#auto-transcribe-toggle');
  const summariseToggle = $('#auto-summarise-toggle');
  if (syncToggle) syncToggle.checked = app.settings.auto_sync !== false;
  if (transcribeToggle) transcribeToggle.checked = app.settings.auto_transcribe !== false;
  if (summariseToggle) summariseToggle.checked = app.settings.auto_summarise || false;
  const wifiToggle = $('#auto-wifi-toggle');
  if (wifiToggle) wifiToggle.checked = app.settings.auto_prefer_wifi || false;
  updateAutoPipelineState();

  // Crash reports default to on; Rust reads the same config key before sending.
  const crashToggle = $('#crash-reports-toggle');
  if (crashToggle) crashToggle.checked = app.settings.crash_reports_enabled !== false;

  // Processing sections (replaces old model selector + AI key groups)
  renderProcessingSections();

  // Subsections
  renderProfile();
  renderWebhooks();
  renderDeviceManager();
  renderNotion();
  renderGoogleDrive();
  renderCloudAccount();
  renderStorageSettings();
}

// ---- Processing Sections ----

function renderProcessingSections() {
  const s = app.settings;

  // 1. Transcription provider dropdown
  const transProv = s.transcription_provider || 'deepgram';
  const transSelect = $('#transcription-provider');
  if (transSelect) transSelect.value = transProv;

  // Show/hide local whisper options
  const localOpts = $('#local-whisper-options');
  if (localOpts) localOpts.classList.toggle('hidden', transProv !== 'local');

  // Show recommended badge only for deepgram
  const transRec = $('#transcription-rec');
  if (transRec) transRec.classList.toggle('hidden', transProv !== 'deepgram');

  // Render model selector if local
  if (transProv === 'local') renderModelSelector();

  // Render inline key input for cloud provider
  renderInlineKey('transcription-key-area', transProv);

  // 2. Diarization
  const diarizeEnabled = s.diarize || false;
  const diarizeToggle = $('#diarize-toggle');
  if (diarizeToggle) diarizeToggle.checked = diarizeEnabled;
  const diarOpts = $('#diarization-options');

  // If transcription is set to Deepgram and the key is connected,
  // auto-select Deepgram for diarization and hide the provider choice
  const deepgramHandlesDiarization = transProv === 'deepgram' && s.deepgram_key_set;

  if (diarOpts) diarOpts.classList.toggle('hidden', !diarizeEnabled || deepgramHandlesDiarization);

  // Show a simple note when Deepgram handles it automatically
  const diarAutoNote = $('#diarization-auto-note');
  if (diarAutoNote) diarAutoNote.classList.toggle('hidden', !diarizeEnabled || !deepgramHandlesDiarization);

  if (deepgramHandlesDiarization) {
    // Silently set diarization provider to deepgram
    s.diarization_provider = 'deepgram';
    renderInlineKey('diarization-key-area', 'local'); // clear the key area
  } else if (diarizeEnabled) {
    const diarProv = s.diarization_provider || 'deepgram';
    const diarSelect = $('#diarization-provider');
    if (diarSelect) diarSelect.value = diarProv;

    const diarRec = $('#diarization-rec');
    if (diarRec) diarRec.classList.toggle('hidden', diarProv !== 'deepgram');

    const localNote = $('#diarization-local-note');
    if (localNote) localNote.classList.toggle('hidden', diarProv !== 'local');

    renderInlineKey('diarization-key-area', diarProv);
  }

  // 3. Summarization
  const sumProv = s.summarization_provider || 'anthropic';
  const sumSelect = $('#summarization-provider');
  if (sumSelect) sumSelect.value = sumProv;

  const sumRec = $('#summarization-rec');
  if (sumRec) sumRec.classList.toggle('hidden', sumProv !== 'anthropic');

  renderInlineKey('summarization-key-area', sumProv);

  // 4. API keys overview
  renderAPIKeysOverview();

  // Bind tooltips (info icons with data-tooltip attributes)
  initTooltips();
}

function renderModelSelector() {
  const modelSelector = $('#model-selector');
  if (!modelSelector) return;
  const models = app.settings.models || {};
  modelSelector.innerHTML = Object.entries(models)
    .map(([name, info]) => {
      const selected = name === (app.settings.model || 'small');
      const disabled = !info.fits;
      return `
      <div class="model-option ${selected ? 'selected' : ''} ${disabled ? 'disabled' : ''}"
           data-model="${escapeHtml(name)}" ${disabled ? 'title="Not enough RAM"' : ''}>
        <div>
          <div class="model-name">${escapeHtml(name)}</div>
          <div class="model-detail">${escapeHtml(info.speed)} \u00B7 ${escapeHtml(info.accuracy)}</div>
        </div>
        <div class="model-size">${escapeHtml(info.size)} \u00B7 ~${escapeHtml(info.ram_gb)} GB RAM</div>
      </div>
    `;
    })
    .join('');

  modelSelector.querySelectorAll('.model-option:not(.disabled)').forEach((opt) => {
    opt.addEventListener('click', async () => {
      const model = opt.dataset.model;
      app.settings.model = model;
      await window.plaud.saveSettings({ model });
      renderModelSelector();
      toast(`Model set to ${model}`, 'success');
    });
  });

  const ramInfo = $('#ram-info');
  if (ramInfo) {
    ramInfo.textContent = app.settings.ram_total_gb
      ? `System: ${app.settings.ram_total_gb} GB total, ${app.settings.ram_used_gb} GB used, ${app.settings.ram_headroom_gb} GB available`
      : '';
  }
}

function renderInlineKey(containerId, provider) {
  const container = $(`#${containerId}`);
  if (!container) return;

  // Local providers don't need a key
  if (provider === 'local') {
    container.innerHTML = '';
    return;
  }

  const meta = PROVIDERS[provider];
  if (!meta) { container.innerHTML = ''; return; }

  const isSet = app.settings[`${provider}_key_set`];

  if (isSet) {
    container.innerHTML = `
      <div class="ai-provider-card">
        <div class="ai-provider-card-left">
          <div class="ai-provider-name">${meta.name}</div>
          <div class="ai-provider-detail">API key connected</div>
        </div>
        <div class="ai-provider-card-right">
          <span class="ai-key-badge connected">Connected</span>
          <button class="ai-provider-remove proc-key-remove" data-provider="${provider}" title="Remove key">&times;</button>
        </div>
      </div>`;
    container.querySelector('.proc-key-remove').addEventListener('click', async () => {
      await window.plaud.saveSettings({ [`remove_${provider}_key`]: true });
      app.settings[`${provider}_key_set`] = false;
      toast(`${meta.name} key removed`, 'info');
      renderProcessingSections();
    });
  } else {
    container.innerHTML = `
      <div class="proc-key-inline">
        <a href="#" class="ai-key-get-link proc-key-link" data-url="${meta.url}">
          Get key from ${meta.name}
          <svg width="10" height="10" viewBox="0 0 12 12" fill="none" class="valign-mid"><path d="M4 1h7v7M11 1L5 7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </a>
        <div class="token-input-row">
          <input type="password" class="settings-input proc-key-input" data-provider="${provider}" placeholder="${meta.placeholder}">
        </div>
        <p class="ai-key-status hidden proc-key-status"></p>
      </div>`;
    const link = container.querySelector('.proc-key-link');
    link.addEventListener('click', (e) => { e.preventDefault(); window.plaud.openExternal(meta.url); });

    const input = container.querySelector('.proc-key-input');
    input.addEventListener('paste', () => {
      setTimeout(() => {
        const val = input.value.trim();
        if (val && val.startsWith(meta.prefix)) handleInlineKeyPaste(container, provider, val);
      }, 50);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const val = input.value.trim();
        if (val) handleInlineKeyPaste(container, provider, val);
      }
    });
  }
}

async function handleInlineKeyPaste(container, provider, key) {
  const statusEl = container.querySelector('.proc-key-status');
  const input = container.querySelector('.proc-key-input');

  statusEl.textContent = 'Validating key...';
  statusEl.className = 'ai-key-status validating proc-key-status';
  statusEl.classList.remove('hidden');

  try {
    const result = await window.plaud.validateKey(provider, key);
    if (result.valid) {
      statusEl.textContent = 'Key valid — saving...';
      statusEl.className = 'ai-key-status success proc-key-status';
      await window.plaud.saveSettings({ [`${provider}_api_key`]: key });
      app.settings[`${provider}_key_set`] = true;
      input.value = '';
      toast(`${PROVIDERS[provider]?.name || provider} key connected`, 'success');
      renderProcessingSections();
    } else {
      statusEl.textContent = result.error || 'Invalid key';
      statusEl.className = 'ai-key-status error proc-key-status';
    }
  } catch (err) {
    statusEl.textContent = `Validation failed: ${err.message}`;
    statusEl.className = 'ai-key-status error proc-key-status';
  }
}

function renderAPIKeysOverview() {
  const container = $('#api-keys-overview');
  if (!container) return;

  const section = container.closest('.settings-section');
  const connected = [];
  for (const [id, meta] of Object.entries(PROVIDERS)) {
    if (app.settings[`${id}_key_set`]) {
      connected.push({ id, ...meta });
    }
  }

  // Hide entire section if no keys are set
  if (connected.length === 0) {
    if (section) section.classList.add('hidden');
    return;
  }
  if (section) section.classList.remove('hidden');

  container.innerHTML = `<div class="api-keys-list">
    ${connected.map(p => `
      <div class="api-key-overview-item connected">
        <span class="api-key-overview-name">${p.name}</span>
        <div class="api-key-overview-right">
          <span class="ai-key-badge connected">Connected</span>
          <button class="ai-provider-remove proc-overview-remove" data-provider="${p.id}" title="Remove">&times;</button>
        </div>
      </div>
    `).join('')}
  </div>`;

  container.querySelectorAll('.proc-overview-remove').forEach(btn => {
    btn.addEventListener('click', async () => {
      const provider = btn.dataset.provider;
      await window.plaud.saveSettings({ [`remove_${provider}_key`]: true });
      app.settings[`${provider}_key_set`] = false;
      toast(`${PROVIDERS[provider]?.name || provider} key removed`, 'info');
      renderProcessingSections();
    });
  });
}

// ---- Profile & Vocabulary ----

function renderProfile() {
  const p = app.settings.profile || {};
  $('#profile-name').value = p.name || '';
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

// renderAIKeys and handleKeyPaste are now part of renderProcessingSections/renderInlineKey above

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

// ---- Google Drive ----

async function loadGdriveStatus() {
  try {
    app.gdriveState = await window.plaud.gdriveStatus();
  } catch {
    app.gdriveState = { connected: false };
  }
}

function renderGoogleDrive() {
  const disconnected = $('#gdrive-disconnected');
  const connected = $('#gdrive-connected');
  if (!disconnected) return;

  if (app.gdriveState && app.gdriveState.connected) {
    disconnected.classList.add('hidden');
    connected.classList.remove('hidden');
    $('#gdrive-folder-name').textContent = app.gdriveState.folder_name || 'No folder selected';
    $('#gdrive-email').textContent = app.gdriveState.user_email || 'Google Drive';

    const syncInfo = $('#gdrive-sync-info');
    if (syncInfo && app.gdriveState.total !== undefined) {
      const synced = app.gdriveState.synced || 0;
      const total = app.gdriveState.total || 0;
      syncInfo.textContent = `${synced} of ${total} recordings synced`;
    }
  } else {
    disconnected.classList.remove('hidden');
    connected.classList.add('hidden');
  }

  const autoSync = $('#gdrive-auto-sync-toggle');
  if (autoSync) autoSync.checked = (app.settings && app.settings.gdrive_auto_sync) || false;
}

function initGdriveEvents() {
  // Connect
  const connectBtn = $('#gdrive-connect-btn');
  if (connectBtn) {
    connectBtn.addEventListener('click', async () => {
      setLoading(connectBtn, true);
      try {
        const result = await window.plaud.gdriveConnect();
        if (result.error) {
          toast(result.error, 'error');
          setLoading(connectBtn, false);
          return;
        }
        if (result.url) {
          window.plaud.openExternal(result.url);
          toast('Sign in with Google in your browser...', 'info');

          if (_gdrivePollHandle) clearInterval(_gdrivePollHandle);
          if (_gdrivePollTimeoutHandle) clearTimeout(_gdrivePollTimeoutHandle);
          _gdrivePollHandle = setInterval(async () => {
            const status = await window.plaud.gdriveStatus();
            if (status.connected) {
              clearInterval(_gdrivePollHandle);
              _gdrivePollHandle = null;
              clearTimeout(_gdrivePollTimeoutHandle);
              _gdrivePollTimeoutHandle = null;
              app.gdriveState = status;
              renderGoogleDrive();
              toast('Google Drive connected!', 'success');
              setLoading(connectBtn, false);
            }
          }, 2000);

          _gdrivePollTimeoutHandle = setTimeout(() => {
            if (_gdrivePollHandle) {
              clearInterval(_gdrivePollHandle);
              _gdrivePollHandle = null;
            }
            _gdrivePollTimeoutHandle = null;
            setLoading(connectBtn, false);
          }, 120000);
        }
      } catch (err) {
        toast(`Error: ${err.message}`, 'error');
        setLoading(connectBtn, false);
      }
    });
  }

  // Disconnect
  const disconnectBtn = $('#gdrive-disconnect-btn');
  if (disconnectBtn) {
    disconnectBtn.addEventListener('click', async () => {
      try {
        await window.plaud.gdriveDisconnect();
        app.gdriveState = { connected: false };
        renderGoogleDrive();
        toast('Google Drive disconnected');
      } catch (err) {
        toast(`Error: ${err.message}`, 'error');
      }
    });
  }

  // Folder search
  let _folderSearchTimeout;
  const folderSearch = $('#gdrive-folder-search');

  function _searchGdriveFolders() {
    clearTimeout(_folderSearchTimeout);
    _folderSearchTimeout = setTimeout(async () => {
      const q = (folderSearch ? folderSearch.value.trim() : '');
      const list = $('#gdrive-folder-list');
      if (!list) return;
      try {
        list.innerHTML = '<p class="settings-hint">Searching...</p>';
        const result = await window.plaud.gdriveFolders(q);
        if (result.error) {
          list.innerHTML = `<p class="settings-hint" style="color:var(--destructive);">${escapeHtml(result.error)}</p>`;
          return;
        }
        const folders = result.folders || [];
        if (folders.length === 0) {
          list.innerHTML = '<p class="settings-hint">No folders found</p>';
          return;
        }
        list.innerHTML = folders.map(f =>
          `<button class="action-btn subtle w-full justify-start mt-1 gdrive-folder-option" data-id="${escapeHtml(f.id)}" data-name="${escapeHtml(f.name)}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0;opacity:0.5;"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
            ${escapeHtml(f.name)}
          </button>`
        ).join('');
        list.querySelectorAll('.gdrive-folder-option').forEach(btn => {
          btn.addEventListener('click', async () => {
            await window.plaud.gdriveSetFolder(btn.dataset.id, btn.dataset.name);
            app.gdriveState = await window.plaud.gdriveStatus();
            renderGoogleDrive();
            toast(`Folder set: ${btn.dataset.name}`, 'success');
            list.innerHTML = '';
            if (folderSearch) folderSearch.value = '';
          });
        });
      } catch (err) {
        list.innerHTML = `<p class="settings-hint" style="color:var(--destructive);">${escapeHtml(err.message)}</p>`;
      }
    }, 300);
  }

  if (folderSearch) {
    folderSearch.addEventListener('input', _searchGdriveFolders);
    folderSearch.addEventListener('focus', _searchGdriveFolders);
  }

  // Create folder
  const createBtn = $('#gdrive-folder-create-btn');
  if (createBtn) {
    createBtn.addEventListener('click', async () => {
      const nameInput = $('#gdrive-new-folder-name');
      const name = (nameInput ? nameInput.value : '').trim();
      if (!name) return;
      setLoading(createBtn, true);
      try {
        await window.plaud.gdriveCreateFolder(name);
        app.gdriveState = await window.plaud.gdriveStatus();
        renderGoogleDrive();
        toast(`Folder created: ${name}`, 'success');
        if (nameInput) nameInput.value = '';
      } catch (err) {
        toast(`Error: ${err.message}`, 'error');
      } finally {
        setLoading(createBtn, false);
      }
    });
  }

  // Sync all
  const syncBtn = $('#gdrive-sync-btn');
  if (syncBtn) {
    syncBtn.addEventListener('click', async () => {
      setLoading(syncBtn, true);
      try {
        const result = await window.plaud.gdriveSync();
        if (result.error) {
          toast(result.error, 'error');
        } else {
          const n = result.uploaded || 0;
          toast(`Synced ${n} recording${n !== 1 ? 's' : ''} to Google Drive`, 'success');
        }
        app.gdriveState = await window.plaud.gdriveStatus();
        renderGoogleDrive();
      } catch (err) {
        toast(`Sync failed: ${err.message}`, 'error');
      } finally {
        setLoading(syncBtn, false);
      }
    });
  }

  // Auto-sync toggle
  const autoToggle = $('#gdrive-auto-sync-toggle');
  if (autoToggle) {
    autoToggle.addEventListener('change', async () => {
      await window.plaud.saveSettings({ gdrive_auto_sync: autoToggle.checked });
    });
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
  const noneRadio = $('#sync-none');
  const dlRadio = $('#sync-download');
  const twRadio = $('#sync-twoway');
  if (noneRadio && dlRadio && twRadio) {
    noneRadio.checked = savedDirection === 'none';
    dlRadio.checked = savedDirection === 'download';
    twRadio.checked = savedDirection === 'twoway';
  }
  // Hide sync button and interval when "none" selected
  const syncActions = $('#cloud-sync-actions');
  const syncIntervalSection = $('#cloud-sync-interval');
  if (syncActions) syncActions.classList.toggle('hidden', savedDirection === 'none');
  if (syncIntervalSection) syncIntervalSection.closest('.settings-section').classList.toggle('hidden', savedDirection === 'none');

  // Sync interval
  const savedInterval = app.settings.cloud_sync_interval || 'manual';
  if (syncIntervalSection) syncIntervalSection.value = savedInterval;

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
        ` <span class="dm-rssi-value">${escapeHtml(app.deviceStateCache.rssi)} dBm</span>`;
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

// Storage-scale byte formatter (KB → MB → GB). Renamed from formatBytes to
// avoid colliding with renderer.js's file-size formatBytes — both declared as
// plain `function` in scripts that share global scope, and the later-loaded
// one silently wins. See feedback_frontend_global_scope.md.
function formatStorageBytes(bytes) {
  if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(1)} GB`;
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(0)} MB`;
  return `${(bytes / 1024).toFixed(0)} KB`;
}

function renderDeviceInfoPanel(panel, info) {
  const items = [];

  // Storage
  if (info.storage_total_bytes) {
    const used = formatStorageBytes(info.storage_used_bytes || 0);
    const total = formatStorageBytes(info.storage_total_bytes);
    const pct = info.storage_used_pct || 0;
    const remaining = info.storage_remaining_hours || 0;
    items.push(`<div class="dm-info-item full-width">
      <div class="dm-info-label">Storage</div>
      <div class="dm-info-value">${used} / ${total} <span class="dm-info-secondary">(${escapeHtml(remaining)}h remaining)</span></div>
      <div class="dm-info-bar"><div class="dm-info-bar-fill" style="width:${Number(pct) || 0}%"></div></div>
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
      <div class="dm-info-value">${escapeHtml(info.battery_level)}%${charging}</div>
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
      <div class="dm-info-value">${escapeHtml(info.active_session_id)}</div>
    </div>`);
  }

  // Accelerometer
  if (info.accelerometer) {
    const a = info.accelerometer;
    items.push(`<div class="dm-info-item full-width">
      <div class="dm-info-label">Orientation</div>
      <div class="dm-info-value">Pitch ${escapeHtml(a.pitch)}\u00B0 \u00B7 Roll ${escapeHtml(a.roll)}\u00B0 \u00B7 Yaw ${escapeHtml(a.yaw)}\u00B0</div>
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
  return `<div class="dm-signal" title="Signal: ${escapeHtml(rssi)} dBm (${rssiToLabel(rssi)})">
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
  // Skip polling during active sync — BLE commands would interfere with file transfer
  if (app.syncing) return;
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

function updateAutoPipelineState() {
  const syncToggle = $('#auto-sync-toggle');
  const transcribeToggle = $('#auto-transcribe-toggle');
  const summariseToggle = $('#auto-summarise-toggle');
  if (!syncToggle) return; // DOM not ready yet

  const syncOn = syncToggle.checked;
  const transcribeOn = transcribeToggle.checked;

  const transcribeRow = $('#auto-transcribe-row');
  if (!syncOn) {
    transcribeRow.classList.add('disabled');
    transcribeToggle.disabled = true;
  } else {
    transcribeRow.classList.remove('disabled');
    transcribeToggle.disabled = false;
  }

  const summariseRow = $('#auto-summarise-row');
  if (!syncOn || !transcribeOn) {
    summariseRow.classList.add('disabled');
    summariseToggle.disabled = true;
  } else {
    summariseRow.classList.remove('disabled');
    summariseToggle.disabled = false;
  }

  const wifiToggle = $('#auto-wifi-toggle');
  const wifiRow = $('#auto-wifi-row');
  if (wifiToggle && wifiRow) {
    if (!syncOn) {
      wifiRow.classList.add('disabled');
      wifiToggle.disabled = true;
    } else {
      wifiRow.classList.remove('disabled');
      wifiToggle.disabled = false;
    }
  }
}

function initSettingsEvents() {
  initStorageEvents();
  initGdriveEvents();

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

  $('#crash-reports-toggle')?.addEventListener('change', async (e) => {
    const enabled = e.target.checked;
    try {
      await window.plaud.saveSettings({ crash_reports_enabled: enabled });
      app.settings.crash_reports_enabled = enabled;
      toast(enabled ? 'Crash reports on' : 'Crash reports off', 'success');
    } catch (err) {
      e.target.checked = !enabled;
      toast(`Couldn't save: ${err.message || err}`, 'error');
    }
  });

  $('#language-select').addEventListener('change', async (e) => {
    app.settings.language = e.target.value;
    await window.plaud.saveSettings({ language: e.target.value });
    // Show/hide English variant selector
    const engVariant = $('#english-variant-group');
    if (engVariant) engVariant.classList.toggle('hidden', e.target.value !== 'en' && e.target.value !== 'auto');
    toast('Language saved', 'success');
  });

  $('#english-variant-select').addEventListener('change', async (e) => {
    app.settings.english_variant = e.target.value;
    await window.plaud.saveSettings({ english_variant: e.target.value });
    toast(`English variant set to ${e.target.value === 'gb' ? 'British' : 'American'}`, 'success');
  });

  $('#diarize-toggle').addEventListener('change', async (e) => {
    app.settings.diarize = e.target.checked;
    await window.plaud.saveSettings({ diarize: e.target.checked });
    toast(e.target.checked ? 'Diarization enabled' : 'Diarization disabled', 'success');
    renderProcessingSections();
  });

  // Automation pipeline toggles with cascade disable
  // Initialize toggle states (also set in renderSettings when settings load)
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

  $('#auto-wifi-toggle').addEventListener('change', async (e) => {
    app.settings.auto_prefer_wifi = e.target.checked;
    await window.plaud.saveSettings({ auto_prefer_wifi: e.target.checked });
    toast(
      e.target.checked
        ? 'WiFi preferred — large transfers will use WiFi automatically'
        : 'WiFi preference disabled',
      'success',
    );
  });

  // Profile save
  $('#profile-save-btn').addEventListener('click', async () => {
    const profile = {
      name: $('#profile-name').value.trim(),
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

  // Processing section — provider dropdown change handlers
  $('#transcription-provider').addEventListener('change', async (e) => {
    app.settings.transcription_provider = e.target.value;
    await window.plaud.saveSettings({ transcription_provider: e.target.value });
    renderProcessingSections();
  });

  $('#diarization-provider').addEventListener('change', async (e) => {
    app.settings.diarization_provider = e.target.value;
    await window.plaud.saveSettings({ diarization_provider: e.target.value });
    renderProcessingSections();
  });

  $('#summarization-provider').addEventListener('change', async (e) => {
    app.settings.summarization_provider = e.target.value;
    await window.plaud.saveSettings({ summarization_provider: e.target.value });
    renderProcessingSections();
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
        if (_notionPollHandle) clearInterval(_notionPollHandle);
        if (_notionPollTimeoutHandle) clearTimeout(_notionPollTimeoutHandle);
        _notionPollHandle = setInterval(async () => {
          const status = await window.plaud.notionStatus();
          if (status.connected) {
            clearInterval(_notionPollHandle);
            _notionPollHandle = null;
            clearTimeout(_notionPollTimeoutHandle);
            _notionPollTimeoutHandle = null;
            app.notionState = status;
            renderNotion();
            toast('Notion connected!', 'success');
            setLoading(btn, false);
          }
        }, 2000);
        _notionPollTimeoutHandle = setTimeout(() => {
          if (_notionPollHandle) {
            clearInterval(_notionPollHandle);
            _notionPollHandle = null;
          }
          _notionPollTimeoutHandle = null;
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
              db.type && db.type !== 'raw' ? `<span style="opacity:0.5;margin-left:4px;">${escapeHtml(db.type)}</span>` : '';
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
          let token = $('#cloud-token-input-waiting').value.trim();
          if (token.toLowerCase().startsWith('bearer ')) token = token.slice(7).trim();
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
    let token = $('#cloud-token-input').value.trim();
    if (token.toLowerCase().startsWith('bearer ')) token = token.slice(7).trim();
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
  for (const radio of ['sync-none', 'sync-download', 'sync-twoway']) {
    $(`#${radio}`).addEventListener('change', async (e) => {
      const dir = e.target.value;
      app.settings.cloud_sync_direction = dir;
      await window.plaud.saveSettings({ cloud_sync_direction: dir });
      // Show/hide sync button and interval
      const syncActions = $('#cloud-sync-actions');
      const syncIntervalEl = $('#cloud-sync-interval');
      if (syncActions) syncActions.classList.toggle('hidden', dir === 'none');
      if (syncIntervalEl) syncIntervalEl.closest('.settings-section').classList.toggle('hidden', dir === 'none');
      // Restart polling timers to apply new sync direction
      if (app.restartPollingTimers) app.restartPollingTimers();
    });
  }

  // Sync interval
  $('#cloud-sync-interval').addEventListener('change', async (e) => {
    app.settings.cloud_sync_interval = e.target.value;
    await window.plaud.saveSettings({ cloud_sync_interval: e.target.value });
    toast(`Sync interval set to ${e.target.options[e.target.selectedIndex].text}`, 'success');
    // Restart polling timers to apply new interval
    if (app.restartPollingTimers) app.restartPollingTimers();
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
            <div class="dm-device-row" data-address="${escapeHtml(d.address)}" data-name="${escapeHtml(d.name || 'Plaud')}" data-sn="${escapeHtml(d.serial_number || '')}">
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
            results.innerHTML = `<div class="dm-scanning">Connecting to ${escapeHtml(name)}</div>`;
            try {
              // Auto-bind device to cloud (gets per-device BIND_TOKEN + SN_SIG)
              if (sn) {
                try {
                  await window.plaud.cloudBindDevice(sn, name);
                  await window.plaud.cloudSnSign(sn);
                } catch {}
              }
              const conn = await window.plaud.connect(address, sn);
              if (conn.connected) {
                toast(`Connected to ${name}`, 'success');
                await updateDeviceStatus();
                renderDeviceManager();
                results.classList.add('hidden');
              } else {
                toast(`Connection failed: ${conn.error || 'Unknown error'}`, 'error');
                results.innerHTML = `<p class="settings-desc" style="text-align:center;color:var(--primary);">
                  Connection failed. ${escapeHtml(conn.error || 'Try again.')}
                </p>`;
              }
            } catch (err) {
              toast(`Error: ${err.message}`, 'error');
            }
          });
        });
      }
    } catch (err) {
      results.innerHTML = `<p class="settings-desc" style="color:var(--primary);">Scan error: ${escapeHtml(err.message)}</p>`;
    } finally {
      setLoading(btn, false);
    }
  });

  // Device manager force sync
  $('#dm-sync-btn').addEventListener('click', async () => {
    // Delegate to shared sync handler — keeps behavior consistent with the header sync button
    await performSync($('#dm-sync-btn'));
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

// ---- Storage Settings ----

function _shortenPath(p) {
  const home = '/Users/' + p.split('/Users/')[1]?.split('/')[0];
  if (home && p.startsWith(home)) return '~' + p.slice(home.length);
  return p;
}

function renderStorageSettings() {
  const s = app.settings;
  const labelEl = $('#save-dir-label');
  const fieldEl = $('#save-dir-path');
  const resetBtn = $('#reset-save-dir-btn');
  if (!labelEl) return;

  const currentDir = s.save_directory || s.default_save_directory || '';
  const isDefault = currentDir === s.default_save_directory;

  labelEl.textContent = _shortenPath(currentDir);
  if (fieldEl) fieldEl.title = currentDir;

  if (resetBtn) resetBtn.classList.toggle('hidden', isDefault);

  // Historical directories
  const histContainer = $('#historical-dirs');
  const histList = $('#historical-dirs-list');
  const historical = (s.historical_directories || []).filter(d => d !== currentDir);
  if (histContainer && histList) {
    if (historical.length > 0) {
      histContainer.classList.remove('hidden');
      histList.innerHTML = historical.map(d => `<li>${escapeHtml(d)}</li>`).join('');
    } else {
      histContainer.classList.add('hidden');
    }
  }

  // Storage mode
  const mode = s.storage_mode || 'local';
  const gdConnected = app.gdriveState && app.gdriveState.connected && app.gdriveState.folder_id;
  const selector = $('#storage-mode-selector');
  if (selector) {
    selector.querySelectorAll('.storage-mode-btn').forEach(btn => {
      const m = btn.dataset.mode;
      btn.classList.toggle('active', m === mode);
      // Disable cloud/both if Drive not connected
      if (m === 'cloud' || m === 'both') {
        btn.disabled = !gdConnected;
      }
    });
  }
  const hint = $('#storage-mode-hint');
  if (hint) {
    const hints = {
      local: 'Recordings saved locally only.',
      cloud: 'Recordings uploaded to Google Drive. Local files removed after upload to save space.',
      both: 'Recordings saved locally and backed up to Google Drive.',
    };
    let text = hints[mode] || '';
    if ((mode === 'cloud' || mode === 'both') && !gdConnected) {
      text = 'Connect Google Drive in Integrations to enable cloud storage.';
    }
    hint.textContent = text;
  }
}

function _showMigrationDialog(newDir, oldDir, isReset) {
  const shortNew = _shortenPath(newDir);
  const overlay = document.createElement('div');
  overlay.className = 'delete-dialog-overlay';
  overlay.innerHTML = `
    <div class="delete-dialog" style="max-width:440px;">
      <h3>${isReset ? 'Reset save location' : 'Change save location'}</h3>
      <p class="settings-desc">New recordings will save to <strong>${escapeHtml(shortNew)}</strong></p>
      <p class="settings-desc" style="margin-top:var(--sp-2);">What would you like to do with your existing recordings?</p>
      <div class="delete-dialog-actions">
        <button class="action-btn primary" id="migrate-move-all">
          Move all recordings to new location
        </button>
        <button class="action-btn subtle" id="migrate-leave">
          Leave them where they are
        </button>
      </div>
      <button class="action-btn subtle" id="migrate-cancel" style="width:100%;margin-top:var(--sp-2);justify-content:center;">
        Cancel
      </button>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.offsetHeight;
  overlay.classList.add('visible');

  const cleanup = () => {
    overlay.classList.remove('visible');
    setTimeout(() => overlay.remove(), 300);
  };

  const changeDirOnly = async () => {
    try {
      await window.plaud.saveSettings({ save_directory: isReset ? '' : newDir });
      toast(isReset ? 'Reset to default directory' : 'Save directory updated');
      await loadSettings();
      await loadRecordings();
    } catch (err) {
      toast(`Failed: ${err.message}`, 'error');
    }
  };

  document.getElementById('migrate-cancel').addEventListener('click', cleanup);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(); });

  document.getElementById('migrate-leave').addEventListener('click', async () => {
    cleanup();
    await changeDirOnly();
  });

  document.getElementById('migrate-move-all').addEventListener('click', async () => {
    cleanup();
    // Change the directory first so new saves go to new location
    await changeDirOnly();

    // Now move all recordings from old dir to new dir
    const allIds = (app.recordings || [])
      .filter(r => r.ogg_path && r.ogg_path.startsWith(oldDir))
      .map(r => r.session_id);

    if (allIds.length === 0) {
      toast('No recordings to move');
      return;
    }

    try {
      toast(`Moving ${allIds.length} recording${allIds.length > 1 ? 's' : ''}...`, 'info');
      const result = await window.plaud.moveRecordings(allIds, newDir);
      const moved = result.count || 0;
      const errors = (result.errors || []).length;
      if (moved > 0) toast(`Moved ${moved} recording${moved > 1 ? 's' : ''}`, 'success');
      if (errors > 0) toast(`${errors} could not be moved`, 'error');
      await loadSettings();
      await loadRecordings();
    } catch (err) {
      toast(`Move failed: ${err.message}`, 'error');
    }
  });
}

function initStorageEvents() {
  // Storage mode selector
  const selector = $('#storage-mode-selector');
  if (selector) {
    selector.querySelectorAll('.storage-mode-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (btn.disabled) return;
        const mode = btn.dataset.mode;
        const gdConnected = app.gdriveState && app.gdriveState.connected && app.gdriveState.folder_id;
        if ((mode === 'cloud' || mode === 'both') && !gdConnected) {
          toast('Connect Google Drive first in Settings > Integrations', 'error');
          return;
        }
        try {
          await window.plaud.saveSettings({ storage_mode: mode });
          app.settings.storage_mode = mode;
          renderStorageSettings();
          toast(`Storage mode: ${mode}`, 'success');
        } catch (err) {
          toast(`Failed: ${err.message}`, 'error');
        }
      });
    });
  }

  const changeBtn = $('#change-save-dir-btn');
  const resetBtn = $('#reset-save-dir-btn');
  const pathEl = $('#save-dir-path');

  if (changeBtn) {
    changeBtn.addEventListener('click', async () => {
      let dir;
      if (window.plaud.selectDirectory) {
        dir = await window.plaud.selectDirectory();
      } else {
        dir = prompt('Enter the full path for new recording saves:');
      }
      if (!dir) return;

      const oldDir = (app.settings.save_directory || app.settings.default_save_directory || '');
      if (dir === oldDir) return;

      _showMigrationDialog(dir, oldDir);
    });
  }

  if (resetBtn) {
    resetBtn.addEventListener('click', async () => {
      const oldDir = (app.settings.save_directory || app.settings.default_save_directory || '');
      const defaultDir = app.settings.default_save_directory || '';
      if (oldDir === defaultDir) return;

      _showMigrationDialog(defaultDir, oldDir, true);
    });
  }

  const fieldEl = $('#save-dir-path');
  if (fieldEl) {
    fieldEl.addEventListener('click', () => {
      const dir = fieldEl.title;
      if (dir && window.plaud.openExternal) {
        window.plaud.openExternal(`file://${dir}`);
      }
    });
  }
}
