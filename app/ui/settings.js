/* ============================================================
   Open Pin — Settings Module
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
  $('#language-select').value = app.settings.language || 'auto';

  // English variant — show only when language is English or auto
  const engVariant = $('#english-variant-group');
  const lang = app.settings.language || 'auto';
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
  const autoDeleteToggle = $('#dm-auto-delete-toggle');
  if (autoDeleteToggle) autoDeleteToggle.checked = app.settings.auto_delete_after_sync || false;
  updateAutoPipelineState();

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
  renderAutomationState();
  renderVoiceSettings();
}

// ---- Settings tabs ----

/** Show one Settings tab: device | ai | automation | accounts | general. */
function showSettingsTab(name) {
  document.querySelectorAll('.settings-tab').forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('.settings-tab-content').forEach((c) => {
    c.classList.toggle('active', c.dataset.tab === name);
  });
  const body = $('.settings-body');
  if (body) body.scrollTop = 0;
}

// ---- Automation tab ----
// The Notion and Google Drive steps only work once those accounts are
// connected, and the Plaud cloud interval only once signed in with sync on:
// otherwise the row is dimmed with a hint pointing at Accounts & storage.

function _setAutomationRow(row, input, hintEl, enabled, hintText) {
  if (row) row.classList.toggle('disabled', !enabled);
  if (input) input.disabled = !enabled;
  if (hintEl) {
    if (hintText) hintEl.textContent = hintText;
    hintEl.classList.toggle('hidden', enabled);
  }
}

function renderAutomationState() {
  const notionToggle = $('#notion-auto-push-toggle');
  const notionOn = !!(app.notionState && (app.notionState.connected || app.notionState.needs_reconnect));
  _setAutomationRow(notionToggle && notionToggle.closest('label'), notionToggle, $('#automation-notion-hint'), notionOn);

  const gdriveOn = !!(app.gdriveState && app.gdriveState.connected);
  _setAutomationRow($('#gdrive-auto-sync-row'), $('#gdrive-auto-sync-toggle'), $('#automation-gdrive-hint'), gdriveOn);

  const loggedIn = !!(app.cloudState && app.cloudState.logged_in);
  const direction = (app.settings && app.settings.cloud_sync_direction) || 'download';
  const cloudOn = loggedIn && direction !== 'none';
  const hint = !loggedIn
    ? 'Sign in to your Plaud account in Accounts & storage to sync from Plaud\u2019s cloud.'
    : 'Sync with Plaud\u2019s cloud is off. Turn it on in Accounts & storage.';
  _setAutomationRow(null, $('#cloud-sync-interval'), $('#automation-cloud-hint'), cloudOn, hint);
}

/** Move the Notion auto-push toggle from the Notion block into Automation (once). */
function _mountNotionAutomationToggle() {
  const toggle = $('#notion-auto-push-toggle');
  const slot = $('#automation-notion-slot');
  if (!toggle || !slot) return;
  const row = toggle.closest('label');
  if (!row || slot.contains(row)) return;
  row.classList.remove('mt-3', 'dm-toggle-row');
  row.classList.add('auto-step');
  row.id = row.id || 'notion-auto-push-row';
  const label = row.querySelector('span:last-child');
  if (label) label.textContent = 'Push summaries to Notion';
  slot.replaceWith(row);
}

// ---- Theme (General › Appearance) ----

function renderThemeSetting(preference) {
  const pref = preference || window._themePreference || 'auto';
  document.querySelectorAll('#theme-setting [data-theme-pref]').forEach((btn) => {
    const on = btn.dataset.themePref === pref;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-checked', on ? 'true' : 'false');
  });
}

/** The Rust side only cycles auto → light → dark, so step until it matches. */
async function setThemePreference(target) {
  let pref = window._themePreference;
  for (let i = 0; i < 3 && pref !== target; i++) {
    ({ preference: pref } = await window.plaud.cycleTheme());
  }
  if (typeof showThemeIcon === 'function') showThemeIcon(pref);
  renderThemeSetting(pref);
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

  // Render model selector if local
  if (transProv === 'local') renderModelSelector();

  // Render inline key input for cloud provider
  renderInlineKey('transcription-key-area', transProv);

  // 2. Speaker identification
  renderDiarizationSection(transProv);

  // 3. Summarization
  const sumProv = s.summarization_provider || 'anthropic';
  const sumSelect = $('#summarization-provider');
  if (sumSelect) sumSelect.value = sumProv;

  renderInlineKey('summarization-key-area', sumProv);

  // 4. Keys saved for providers that aren't selected
  renderAPIKeysOverview();

  // Bind tooltips (info icons with data-tooltip attributes)
  initTooltips();
}

// Speaker identification follows the transcription provider: Deepgram labels
// speakers itself; any other provider (or a fallback after Deepgram fails)
// runs the local model after transcription. There's no separate provider choice.
function renderDiarizationSection(transProv) {
  const s = app.settings;
  const enabled = s.diarize || false;
  const toggle = $('#diarize-toggle');
  if (toggle) toggle.checked = enabled;

  const deepgramLabels = transProv === 'deepgram' && s.deepgram_key_set;
  const note = $('#diarization-mode-note');
  if (note) {
    note.textContent = deepgramLabels
      ? 'Deepgram labels speakers as it transcribes.'
      : 'Runs on this Mac after transcription, with a built-in speaker model (no account needed). '
        + 'The first run installs it, which can take a few minutes. If it can’t run, the transcript is saved without speaker labels.';
    note.classList.toggle('hidden', !enabled);
  }

  // The token only matters when the local model labels speakers, i.e. with any
  // provider but Deepgram. A saved token stays removable under "Other saved keys".
  const area = $('#hf-token-area');
  if (area) area.classList.toggle('hidden', !enabled || transProv === 'deepgram');
  const entry = $('#hf-token-entry');
  const saved = $('#hf-token-saved');
  if (entry) entry.classList.toggle('hidden', !!s.hf_token_set);
  if (saved) saved.classList.toggle('hidden', !s.hf_token_set);
}

function _setHfTokenStatus(text, kind) {
  const el = $('#hf-token-status');
  if (!el) return;
  el.textContent = text || '';
  el.className = `ai-key-status${kind ? ` ${kind}` : ''}${text ? '' : ' hidden'}`;
}

let _hfTokenSaving = false; // paste + Enter must not submit twice

async function saveHfTokenFromInput() {
  const input = $('#hf-token-input');
  if (!input || _hfTokenSaving) return;
  const token = input.value.trim();
  if (!token) return;
  _hfTokenSaving = true;
  _setHfTokenStatus('Checking the token with Hugging Face…', 'validating');
  try {
    const result = await window.plaud.saveSettings({ hf_token: token });
    if (result && result.error) {
      _setHfTokenStatus(result.error, 'error');
      return;
    }
    input.value = '';
    app.settings.hf_token_set = true;
    _setHfTokenStatus('', '');
    toast('Hugging Face token saved', 'success');
    renderDiarizationSection(app.settings.transcription_provider || 'deepgram');
    renderAPIKeysOverview();
  } catch (err) {
    _setHfTokenStatus(`Couldn't save the token: ${err.message}`, 'error');
  } finally {
    _hfTokenSaving = false;
  }
}

function renderModelSelector() {
  const modelSelector = $('#model-selector');
  if (!modelSelector) return;
  const models = app.settings.models || {};
  // Smallest download first (the API's JSON keys arrive alphabetically)
  modelSelector.innerHTML = Object.entries(models)
    .sort((a, b) => (a[1].download_mb || 0) - (b[1].download_mb || 0))
    .map(([name, info]) => {
      const selected = name === (app.settings.model || 'small');
      const disabled = !info.fits;
      const why = `Needs about ${info.ram_gb} GB of free memory${app.settings.ram_headroom_gb ? `; this Mac has ${app.settings.ram_headroom_gb} GB free` : ''}`;
      return `
      <div class="model-option ${selected ? 'selected' : ''} ${disabled ? 'disabled' : ''}"
           data-model="${escapeHtml(name)}" ${disabled ? `title="${escapeHtml(why)}" aria-disabled="true"` : ''}>
        <div>
          <div class="model-name">${escapeHtml(name)}</div>
          <div class="model-detail">${escapeHtml(info.speed)} \u00B7 ${escapeHtml(info.accuracy)}</div>
          ${disabled ? `<div class="model-note">${escapeHtml(why)}</div>` : ''}
        </div>
        <div class="model-size">${escapeHtml(info.size)}<br>~${escapeHtml(info.ram_gb)} GB RAM</div>
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
      ? `This Mac: ${app.settings.ram_total_gb} GB memory, ${app.settings.ram_used_gb} GB in use, ${app.settings.ram_headroom_gb} GB free`
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

  statusEl.textContent = 'Checking the key…';
  statusEl.className = 'ai-key-status validating proc-key-status';
  statusEl.classList.remove('hidden');

  try {
    const result = await window.plaud.validateKey(provider, key);
    if (result.valid) {
      statusEl.textContent = 'Key works. Saving…';
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

/**
 * "Other saved keys": keys for providers that aren't selected for transcription
 * or summaries (the selected ones show in their own section), plus a saved
 * Hugging Face token while its own field is hidden. Each can be removed here.
 */
function renderAPIKeysOverview() {
  const container = $('#api-keys-overview');
  if (!container) return;

  const section = $('#other-keys-section') || container.closest('.settings-section');
  const s = app.settings;
  const inUse = new Set([s.transcription_provider || 'deepgram', s.summarization_provider || 'anthropic']);
  const others = [];
  for (const [id, meta] of Object.entries(PROVIDERS)) {
    if (s[`${id}_key_set`] && !inUse.has(id)) others.push({ id, name: meta.name, remove: `remove_${id}_key` });
  }
  const hfArea = $('#hf-token-area');
  if (s.hf_token_set && (!hfArea || hfArea.classList.contains('hidden'))) {
    others.push({ id: 'hf', name: 'Hugging Face token', remove: 'remove_hf_token' });
  }

  if (others.length === 0) {
    container.innerHTML = '';
    if (section) section.classList.add('hidden');
    return;
  }
  if (section) section.classList.remove('hidden');

  container.innerHTML = `<div class="api-keys-list">
    ${others.map(p => `
      <div class="api-key-overview-item connected">
        <span class="api-key-overview-name">${escapeHtml(p.name)}</span>
        <div class="api-key-overview-right">
          <span class="ai-key-badge connected">Saved</span>
          <button class="ai-provider-remove proc-overview-remove" data-provider="${p.id}" data-remove="${p.remove}" title="Remove ${escapeHtml(p.name)}" aria-label="Remove ${escapeHtml(p.name)}">&times;</button>
        </div>
      </div>
    `).join('')}
  </div>`;

  container.querySelectorAll('.proc-overview-remove').forEach(btn => {
    btn.addEventListener('click', async () => {
      const provider = btn.dataset.provider;
      await window.plaud.saveSettings({ [btn.dataset.remove]: true });
      if (provider === 'hf') {
        app.settings.hf_token_set = false;
        toast('Hugging Face token removed', 'info');
      } else {
        app.settings[`${provider}_key_set`] = false;
        toast(`${PROVIDERS[provider]?.name || provider} key removed`, 'info');
      }
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

// Readable names for webhook triggers (config keys stay transcribe/summarize/sync)
const WEBHOOK_TRIGGER_LABELS = { transcribe: 'Transcribed', summarize: 'Summarised', sync: 'Synced' };

function renderWebhooks() {
  const webhooks = app.settings.webhooks || [];
  const list = $('#webhook-list');
  list.innerHTML = webhooks
    .map((wh, i) => {
      const url = wh.url || '';
      const name = wh.name || '';
      // Pipeline order: synced, transcribed, summarised
      const order = ['sync', 'transcribe', 'summarize'];
      const triggers = Object.entries(wh.triggers || {})
        .filter(([, v]) => v)
        .map(([k]) => k)
        .sort((x, y) => (order.indexOf(x) + 1 || 99) - (order.indexOf(y) + 1 || 99))
        .map((k) => `<span class="webhook-trigger">${escapeHtml(WEBHOOK_TRIGGER_LABELS[k] || k)}</span>`)
        .join('');
      return `<div class="webhook-item">
      <div class="webhook-info">
        ${name ? `<span class="webhook-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>` : ''}
        <span class="webhook-url" title="${escapeHtml(url)}">${escapeHtml(url)}</span>
        <div class="webhook-triggers" aria-label="Sends when">${triggers || '<span class="webhook-trigger none">Never: no triggers</span>'}</div>
      </div>
      <button class="icon-btn text-destructive" data-index="${i}" title="Remove webhook" aria-label="Remove webhook ${escapeHtml(name || url)}">
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

let _notionQueueFetchedAt = 0;

async function loadNotionStatus() {
  try {
    app.notionState = await window.plaud.notionStatus();
  } catch {
    app.notionState = { connected: false };
  }
  try {
    app.notionQueue = await window.plaud.notionQueueStatus();
  } catch {
    app.notionQueue = null;
  }
}

function renderNotion() {
  const state = app.notionState || {};
  const queueSnap = app.notionQueue || {};
  const disconnected = $('#notion-disconnected');
  const connected = $('#notion-connected');
  // A login Notion no longer accepts: keep showing the database, offer Reconnect.
  const reconnect = !!state.needs_reconnect;
  if (state.connected || reconnect) {
    disconnected.classList.add('hidden');
    connected.classList.remove('hidden');
    $('#notion-db-name').textContent = state.database_name || 'No database selected';
  } else {
    disconnected.classList.remove('hidden');
    connected.classList.add('hidden');
  }
  renderNotionBadge(state, reconnect);
  renderNotionQueueNotice(queueSnap, reconnect);
  // Settings is opened long after startup: refresh the queue snapshot so a
  // push that failed meanwhile shows up (throttled; re-renders only the notice).
  if (state.connected && Date.now() - _notionQueueFetchedAt > 10000) {
    _notionQueueFetchedAt = Date.now();
    window.plaud
      .notionQueueStatus()
      .then((snap) => {
        app.notionQueue = snap;
        renderNotionQueueNotice(snap, !!(app.notionState || {}).needs_reconnect);
      })
      .catch(() => {});
  }
  // Set auto-push toggle state
  const autoPushToggle = $('#notion-auto-push-toggle');
  if (autoPushToggle) {
    autoPushToggle.checked = app.settings.notion_auto_push || false;
  }
  renderAutomationState();
}

function renderNotionBadge(state, reconnect) {
  const badge = $('#notion-status-badge');
  if (!badge) return;
  badge.onclick = null;
  badge.removeAttribute('role');
  badge.removeAttribute('tabindex');
  badge.style.background = '';
  badge.style.cursor = '';
  if (reconnect) {
    badge.className = 'ai-key-badge';
    badge.textContent = 'Reconnect';
    badge.title = 'Notion no longer accepts this login. Click to sign in again.';
    badge.style.background = 'var(--orange)';
    badge.style.cursor = 'pointer';
    badge.setAttribute('role', 'button');
    badge.setAttribute('tabindex', '0');
    badge.onclick = () => $('#notion-connect-btn').click();
  } else if (!state.database_id) {
    badge.className = 'ai-key-badge none';
    badge.textContent = 'Choose a database';
    badge.title = 'Search for a database below or paste its URL';
  } else {
    badge.className = 'ai-key-badge connected';
    badge.textContent = 'Connected';
    badge.title = `Connected · ${state.database_name || 'database'}`;
  }
}

// "N Notion pushes failed — Retry" under the database card.
function renderNotionQueueNotice(queueSnap, reconnect) {
  let notice = $('#notion-queue-notice');
  const failed = queueSnap.failed_count || 0;
  const paused = !!queueSnap.paused && !reconnect;
  const review = queueSnap.needs_review_count || 0;
  if (!failed && !paused && !review) {
    if (notice) notice.remove();
    return;
  }
  if (!notice) {
    notice = document.createElement('div');
    notice.id = 'notion-queue-notice';
    notice.className = 'settings-desc mt-2';
    notice.style.color = 'var(--orange)';
    const card = $('#notion-db-card');
    if (!card) return;
    card.insertAdjacentElement('afterend', notice);
  }
  notice.textContent = '';
  notice.title = '';
  if (review) {
    // Pushes that found a page an older version of the app made: the owner
    // chooses in the transcript view ("Use that page" / "Create a new page").
    const names = (queueSnap.needs_review || []).map((r) => _recordingLabel(r.session_id));
    const reviewText = document.createElement('div');
    reviewText.textContent =
      `${review === 1 ? 'This recording' : `These ${review} recordings`} may already be in Notion: ` +
      `${names.join(', ')}. Open ${review === 1 ? 'it' : 'each one'} to choose which page to use.`;
    notice.append(reviewText);
    if (!failed && !paused) return;
  }
  const text = document.createElement('span');
  if (failed) {
    text.textContent = `${failed} Notion push${failed === 1 ? '' : 'es'} failed — `;
    const last = (queueSnap.failed || [])[failed - 1];
    if (last && last.error) notice.title = last.error;
  } else {
    text.textContent = 'Notion pushes are paused — ';
    notice.title = queueSnap.pause_reason || '';
  }
  const retry = document.createElement('button');
  retry.className = 'btn-small';
  retry.textContent = 'Retry';
  retry.addEventListener('click', async () => {
    retry.disabled = true;
    try {
      app.notionQueue = await window.plaud.notionQueueResume();
      renderNotion();
      toast('Retrying Notion pushes', 'info');
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
      retry.disabled = false;
    }
  });
  notice.append(text, retry);
}

// "Check for duplicates": pairs of pages holding the same recording, shown
// as links so the owner can delete one in Notion (the app never deletes).
function renderNotionDuplicates(result) {
  const box = $('#notion-duplicates-result');
  if (!box) return;
  box.textContent = '';
  box.classList.remove('hidden');
  const pairs = (result && result.pairs) || [];
  if (!pairs.length) {
    box.textContent = `No duplicates found (${result.checked || 0} pages checked).`;
    return;
  }
  const intro = document.createElement('p');
  intro.className = 'mb-2';
  intro.textContent =
    `${pairs.length} recording${pairs.length === 1 ? ' is' : 's are'} in Notion twice. ` +
    "Delete the page you don't want in Notion. If you delete the newer one, the app uses the older page " +
    'the next time it sends that recording.';
  box.append(intro);
  const link = (label, page) => {
    const a = document.createElement('a');
    a.href = '#';
    a.className = 'ai-key-get-link';
    a.textContent = label;
    a.title = page.title || '';
    a.addEventListener('click', (e) => {
      e.preventDefault();
      window.plaud.openExternal(page.url);
    });
    return a;
  };
  pairs.forEach((pair) => {
    const row = document.createElement('div');
    row.className = 'mb-2';
    const name = document.createElement('div');
    name.textContent = pair.newer.title || pair.older.title || pair.session_id;
    row.append(name, link('Older page', pair.older), ' · ', link('Newer page', pair.newer));
    box.append(row);
  });
  if (result.complete === false) {
    const more = document.createElement('p');
    more.className = 'mb-0';
    more.textContent = 'The database is very large, so only its first pages were checked.';
    box.append(more);
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
      syncInfo.textContent = `${synced} of ${total} recordings uploaded`;
    }
  } else {
    disconnected.classList.remove('hidden');
    connected.classList.add('hidden');
  }

  const autoSync = $('#gdrive-auto-sync-toggle');
  if (autoSync) autoSync.checked = (app.settings && app.settings.gdrive_auto_sync) || false;
  // Storage mode (same section) needs a Drive folder; the Automation step needs Drive
  if (app.settings) renderStorageSettings();
  renderAutomationState();
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
          toast('Sign in with Google in your browser…', 'info');

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
        list.innerHTML = '<p class="settings-hint">Searching…</p>';
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
          toast(`Uploaded ${n} recording${n !== 1 ? 's' : ''} to Google Drive`, 'success');
        }
        app.gdriveState = await window.plaud.gdriveStatus();
        renderGoogleDrive();
      } catch (err) {
        toast(`Upload failed: ${err.message}`, 'error');
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
  // A sign-in link that launched the app can finish while this request is in
  // flight; don't let its older answer undo that sign-in.
  const seq = app.cloudSignInSeq;
  let state;
  try {
    state = await window.plaud.cloudStatus();
  } catch {
    state = { logged_in: false, user: null };
  }
  if (seq === app.cloudSignInSeq) app.cloudState = state;
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
  // Hide the sync button when "none" selected (the interval, under
  // Automation, is dimmed by renderAutomationState)
  const syncActions = $('#cloud-sync-actions');
  if (syncActions) syncActions.classList.toggle('hidden', savedDirection === 'none');

  // Sync interval
  const savedInterval = app.settings.cloud_sync_interval || 'manual';
  const syncIntervalSelect = $('#cloud-sync-interval');
  if (syncIntervalSelect) syncIntervalSelect.value = savedInterval;

  // Three states: logged in, waiting for the browser sign-in, logged out.
  // Only classes change here: the markup (and its listeners) is static in index.html.
  const waiting = $('#cloud-login-waiting');
  if (app.cloudState.logged_in) app.cloudLoginPending = false;
  const showWaiting = !app.cloudState.logged_in && app.cloudLoginPending;
  if (waiting) waiting.classList.toggle('hidden', !showWaiting);

  if (app.cloudState.logged_in) {
    loggedOut.classList.add('hidden');
    loggedIn.classList.remove('hidden');
    const userName = $('#cloud-user-name');
    if (app.cloudState.user) {
      const raw = app.cloudState.user;
      const u = raw.data_user || raw;
      userName.textContent = u.nickname || u.email || u.phone || 'Plaud user';
    } else {
      userName.textContent = 'Plaud user';
    }
  } else {
    loggedOut.classList.toggle('hidden', showWaiting);
    loggedIn.classList.add('hidden');
  }
  renderAutomationState();
}

/**
 * Settings › Plaud Cloud: apply the reply of a sign-in attempt (pasted token or
 * code, or a plaud:// sign-in link). Success or failure, the waiting state ends.
 * Returns true when signed in.
 */
function finishCloudSignIn(res) {
  app.cloudLoginPending = false;
  const ok = !!(res && res.logged_in);
  if (ok) {
    app.cloudState = { logged_in: true, user: res.user };
    app.cloudSignInSeq += 1;
    app.pendingDesktopUuid = null;
    toast('Plaud account connected!', 'success');
  } else {
    toast((res && res.error) || 'Sign-in failed', 'error');
  }
  renderCloudAccount();
  return ok;
}

/** Settings › Plaud Cloud: sign in with a paste box's contents (link, code or token). */
async function _cloudSignInFromInput(input, btn) {
  setLoading(btn, true);
  try {
    const res = await submitPlaudSignInInput(input.value);
    if (!res) return;
    input.value = '';
    finishCloudSignIn(res);
  } catch (err) {
    app.cloudLoginPending = false;
    toast(`Error: ${err.message}`, 'error');
    renderCloudAccount();
  } finally {
    setLoading(btn, false);
  }
}

// ---- Device Manager ----

function renderDeviceManager() {
  const dmDisconnected = $('#dm-disconnected');
  const dmConnected = $('#dm-connected');

  const connectedNow = !!(app.deviceStateCache && app.deviceStateCache.connected);
  // Advanced: device details and "Erase and disconnect" need a connection
  const eraseSection = $('#dm-erase-section');
  if (eraseSection) eraseSection.classList.toggle('hidden', !connectedNow);
  const infoEmpty = $('#dm-info-empty');
  if (infoEmpty) infoEmpty.classList.toggle('hidden', connectedNow);
  if (!connectedNow) {
    const infoPanel = $('#dm-info-panel');
    if (infoPanel) infoPanel.innerHTML = '';
  }

  if (connectedNow) {
    dmDisconnected.classList.add('hidden');
    dmConnected.classList.remove('hidden');
    $('#dm-device-name').textContent = prettyDeviceName(app.deviceStateCache.device_name) || 'Plaud NotePin S';
    const sn = app.deviceStateCache.serial_number;
    $('#dm-device-meta').textContent = sn ? `Serial number ${sn}` : '';
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
  // Connecting adds a paired device; Erase & Disconnect removes them all.
  const pairingConnected = !!(app.deviceStateCache && app.deviceStateCache.connected);
  if (pairingConnected !== _pairingBackupLastConnected) {
    _pairingBackupLastConnected = pairingConnected;
    refreshPairingBackupSection();
  }
}

// ---- Pairing backup (export / import) ----

let _pairingBackupLastConnected = null;
let _pairingBackupBusy = false;

function _pairingSerialTails(devices) {
  return (devices || []).map((d) => `ending ${d.serial_tail}`).join(', ');
}

async function refreshPairingBackupSection() {
  const exportBtn = $('#pairing-export-btn');
  const hint = $('#pairing-backup-hint');
  if (!exportBtn || !hint) return;
  try {
    const status = await window.plaud.pairingStatus();
    const devices = (status && status.devices) || [];
    if (!_pairingBackupBusy) exportBtn.disabled = !(status && status.exportable);
    if (devices.length) {
      const names = devices.map((d) => `${d.name || 'NotePin'} (serial ${_pairingSerialTails([d])})`).join(', ');
      hint.textContent = devices.every((d) => d.in_keychain)
        ? `Paired: ${names}. Backed up in your login Keychain.`
        : `Paired: ${names}. Not backed up in your Keychain yet, so consider exporting the pairing.`;
    } else {
      hint.textContent = 'Connect your NotePin to export its pairing.';
    }
  } catch {
    if (!_pairingBackupBusy) exportBtn.disabled = true;
  }
}

/** The dialog plugin returns a path string (or a list for multi-select). */
function _pairingDialogPath(picked) {
  const p = Array.isArray(picked) ? picked[0] : picked;
  if (typeof p !== 'string' || !p) return null;
  return p.startsWith('file://') ? decodeURIComponent(p.slice('file://'.length)) : p;
}

async function exportPairingFile() {
  const choice = await showConfirmDialog({
    title: 'Export pairing?',
    message:
      "The file holds your NotePin's pairing keys. Anyone who has it can connect to your NotePin and download its recordings.",
    note: 'Store it somewhere safe, such as a password manager, and delete other copies.',
    actions: [{ id: 'export', label: 'Choose where to save…' }],
  });
  if (choice !== 'export') return;
  let path;
  try {
    path = _pairingDialogPath(await window.plaud.choosePairingExportPath('Open Pin pairing.json'));
  } catch (err) {
    toast(`Couldn't open the save panel: ${err && err.message ? err.message : err}`, 'error');
    return;
  }
  if (!path) return;
  const btn = $('#pairing-export-btn');
  _pairingBackupBusy = true;
  setLoading(btn, true);
  try {
    const result = await window.plaud.exportPairing(path);
    if (result && result.error) {
      toast(`Export failed: ${result.error}`, 'error');
    } else {
      toast(`Exported the pairing for serial ${_pairingSerialTails(result.devices)}. Keep the file somewhere safe.`, 'success');
    }
  } catch (err) {
    toast(`Export failed: ${err.message}`, 'error');
  } finally {
    _pairingBackupBusy = false;
    setLoading(btn, false);
    refreshPairingBackupSection();
  }
}

async function importPairingFile() {
  let path;
  try {
    path = _pairingDialogPath(await window.plaud.choosePairingImportFile());
  } catch (err) {
    toast(`Couldn't open the file panel: ${err && err.message ? err.message : err}`, 'error');
    return;
  }
  if (!path) return;
  const btn = $('#pairing-import-btn');
  setLoading(btn, true);
  try {
    const result = await window.plaud.importPairing(path);
    if (result && result.error) {
      toast(`Import failed: ${result.error}`, 'error');
      return;
    }
    const imported = (result && result.imported) || [];
    const keychainNote =
      result.keychain_available && result.keychain_saved < imported.length
        ? " Couldn't save it to the Keychain, so keep the file."
        : '';
    toast(`Imported the pairing for serial ${_pairingSerialTails(imported)}.${keychainNote}`, 'success');
  } catch (err) {
    toast(`Import failed: ${err.message}`, 'error');
  } finally {
    setLoading(btn, false);
    refreshPairingBackupSection();
  }
}

function initPairingBackupEvents() {
  const exportBtn = $('#pairing-export-btn');
  const importBtn = $('#pairing-import-btn');
  if (!exportBtn || !importBtn) return;
  exportBtn.addEventListener('click', exportPairingFile);
  importBtn.addEventListener('click', importPairingFile);
  refreshPairingBackupSection();
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
    const charging = info.charging ? ' (charging)' : '';
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

  // Recording now: the session id is the recording's start time (UNIX seconds)
  if (info.is_recording && info.active_session_id > 0) {
    const started = new Date(Number(info.active_session_id) * 1000);
    const when = isNaN(started) ? '' : started.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    items.push(`<div class="dm-info-item">
      <div class="dm-info-label">Recording since</div>
      <div class="dm-info-value">${escapeHtml(when || 'now')}</div>
    </div>`);
  }

  panel.innerHTML = `<div class="dm-info-grid">${items.join('')}</div>`;
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
  return `<div class="dm-signal" title="Signal: ${escapeHtml(rssi)} dBm (${rssiToLabel(rssi)})" role="img" aria-label="Signal ${rssiToLabel(rssi).toLowerCase()}">
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
      dom.statusText.textContent = prettyDeviceName(status.device_name) || 'Connected';
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

  _renderAutoCoachRow(syncOn && transcribeOn);

  // "Prefer Wi-Fi" (Device › Transfer) isn't part of this chain: manual syncs
  // use it too (performSync), so it stays available with auto-sync off.
}

// "Coach my conversations" is the coaching_auto setting, shared with the switch in
// Transcription & AI › Your voice & coaching (_voiceRenderAutoToggle): each change
// handler redraws the other switch. Coaching needs speaker labels (the backend turns
// coaching_auto off with diarize) and a transcript (the steps above).
function _renderAutoCoachRow(stepsBeforeOn) {
  const toggle = $('#auto-coach-toggle');
  const row = $('#auto-coach-row');
  if (!toggle || !row) return;
  const labelsOn = !!(app.settings && app.settings.diarize);
  toggle.checked = labelsOn && app.settings.coaching_auto !== false;
  _setAutomationRow(row, toggle, $('#automation-coach-hint'), stepsBeforeOn);
  if (!labelsOn) toggle.disabled = true;
  row.classList.toggle('voice-auto-off', !labelsOn);
  $('#auto-coach-needs').classList.toggle('hidden', labelsOn);
}

// Personalisation saves as you type (debounced) and on blur/change.
const PROFILE_SAVE_DELAY_MS = 700;
const PROFILE_FIELD_IDS = ['profile-name', 'profile-job', 'profile-industry', 'profile-seniority', 'profile-usecases', 'profile-instructions'];
let _profileSaveTimer = null;
let _profileStatusTimer = null;

function _readProfileFields() {
  return {
    name: $('#profile-name').value.trim(),
    job: $('#profile-job').value.trim(),
    industry: $('#profile-industry').value.trim(),
    seniority: $('#profile-seniority').value,
    usecases: $('#profile-usecases').value.trim(),
    instructions: $('#profile-instructions').value.trim(),
  };
}

function _setProfileSaveStatus(text, isError = false) {
  const el = $('#profile-save-status');
  if (!el) return;
  clearTimeout(_profileStatusTimer);
  el.textContent = text;
  el.classList.toggle('error', isError);
  if (text && !isError) _profileStatusTimer = setTimeout(() => { el.textContent = ''; }, 2000);
}

async function saveProfileNow() {
  clearTimeout(_profileSaveTimer);
  _profileSaveTimer = null;
  const profile = _readProfileFields();
  const before = app.settings.profile || {};
  const same = Object.keys(profile).every((k) => (before[k] || '') === profile[k]);
  if (same) return;
  try {
    await window.plaud.saveSettings({ profile });
    app.settings.profile = profile;
    _setProfileSaveStatus('Saved');
  } catch (err) {
    _setProfileSaveStatus(`Not saved: ${err.message}`, true);
  }
}

function scheduleProfileSave() {
  clearTimeout(_profileSaveTimer);
  _profileSaveTimer = setTimeout(saveProfileNow, PROFILE_SAVE_DELAY_MS);
}

function initSettingsEvents() {
  initStorageEvents();
  initGdriveEvents();
  _mountNotionAutomationToggle();

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

  // Tab switching (arrow keys move between tabs too)
  const settingsTabs = [...document.querySelectorAll('.settings-tab')];
  settingsTabs.forEach((tab, i) => {
    tab.addEventListener('click', () => showSettingsTab(tab.dataset.tab));
    tab.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      e.preventDefault();
      const next = settingsTabs[(i + (e.key === 'ArrowRight' ? 1 : settingsTabs.length - 1)) % settingsTabs.length];
      next.focus();
      showSettingsTab(next.dataset.tab);
    });
  });

  // General › Appearance
  document.querySelectorAll('#theme-setting [data-theme-pref]').forEach((btn) => {
    btn.addEventListener('click', () => setThemePreference(btn.dataset.themePref).catch(() => {}));
  });
  renderThemeSetting();

  // General › About
  const diagBtn = $('#open-diagnostics-log-btn');
  if (diagBtn) {
    diagBtn.addEventListener('click', async () => {
      try {
        await window.plaud.openDiagnosticsLog();
      } catch (err) {
        toast(`Couldn't open the diagnostics log: ${err && err.message ? err.message : err}`, 'error');
      }
    });
  }

  // Device › Transfer
  $('#dm-auto-delete-toggle').addEventListener('change', async (e) => {
    const enabled = e.target.checked;
    await window.plaud.saveSettings({ auto_delete_after_sync: enabled });
    app.settings.auto_delete_after_sync = enabled;
    toast(
      enabled ? 'Recordings will be deleted from the device after sync' : 'Recordings will stay on the device after sync',
      'success',
    );
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
    statusEl.textContent = 'Checking…';
    try {
      const resultPromise = new Promise((resolve) => {
        app.updateListeners.push(resolve);
        setTimeout(() => resolve({ status: 'timeout' }), 10000);
      });
      window.plaud.checkForUpdates();
      const result = await resultPromise;
      if (result.status === 'up-to-date') {
        statusEl.textContent = 'You\'re up to date';
      } else if (result.status === 'available' || result.status === 'downloading' || result.status === 'ready') {
        statusEl.textContent = '';
      } else if (result.status === 'error') {
        statusEl.textContent = `Update error: ${result.message || 'Unknown'}`;
      } else if (result.status === 'timeout') {
        statusEl.textContent = 'No response. Checking may not work in development builds.';
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
    toast(e.target.checked ? 'Open Pin will open at login' : 'Open Pin won\'t open at login', 'success');
  });

  $('#show-dock-toggle').addEventListener('change', async (e) => {
    await window.plaud.setDockVisible(e.target.checked);
    toast(e.target.checked ? 'Dock icon visible' : 'Dock icon hidden', 'success');
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
    toast(e.target.checked ? 'Speaker identification on' : 'Speaker identification off', 'success');
    renderProcessingSections();
    // The backend turns automatic coaching off with speaker labels (it needs them)
    if (!e.target.checked) app.settings.coaching_auto = false;
    renderVoiceSettings();
    updateAutoPipelineState();
  });

  // Automation pipeline toggles with cascade disable
  // Initialize toggle states (also set in renderSettings when settings load)
  updateAutoPipelineState();

  $('#auto-sync-toggle').addEventListener('change', async (e) => {
    app.settings.auto_sync = e.target.checked;
    await window.plaud.saveSettings({ auto_sync: e.target.checked });
    updateAutoPipelineState();
    toast(e.target.checked ? 'New recordings will sync by themselves' : 'Automatic sync off', 'success');
  });

  $('#auto-transcribe-toggle').addEventListener('change', async (e) => {
    app.settings.auto_transcribe = e.target.checked;
    await window.plaud.saveSettings({ auto_transcribe: e.target.checked });
    updateAutoPipelineState();
    toast(e.target.checked ? 'New recordings will be transcribed' : 'Automatic transcription off', 'success');
  });

  $('#auto-coach-toggle').addEventListener('change', async (e) => {
    app.settings.coaching_auto = e.target.checked;
    _voiceRenderAutoToggle();
    await window.plaud.saveSettings({ coaching_auto: e.target.checked });
    toast(e.target.checked ? 'New conversations will be coached' : 'Automatic coaching off', 'success');
  });

  $('#auto-summarise-toggle').addEventListener('change', async (e) => {
    app.settings.auto_summarise = e.target.checked;
    await window.plaud.saveSettings({ auto_summarise: e.target.checked });
    toast(e.target.checked ? 'New transcripts will be summarised' : 'Automatic summaries off', 'success');
  });

  $('#auto-wifi-toggle').addEventListener('change', async (e) => {
    app.settings.auto_prefer_wifi = e.target.checked;
    await window.plaud.saveSettings({ auto_prefer_wifi: e.target.checked });
    toast(
      e.target.checked
        ? 'Large transfers will use Wi-Fi without asking'
        : 'Open Pin will ask before using Wi-Fi',
      'success',
    );
  });

  // Personalisation: autosave (no Save button)
  PROFILE_FIELD_IDS.forEach((id) => {
    const el = $(`#${id}`);
    if (!el) return;
    el.addEventListener('input', scheduleProfileSave);
    el.addEventListener('change', saveProfileNow);
    el.addEventListener('blur', saveProfileNow);
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
      toast('Enter the webhook\'s URL', 'error');
      return;
    }
    try {
      new URL(url);
    } catch {
      toast('That isn\'t a valid URL', 'error');
      return;
    }

    const triggers = {
      transcribe: $('#webhook-on-transcribe').checked,
      summarize: $('#webhook-on-summarize').checked,
      sync: $('#webhook-on-sync').checked,
    };

    toast('Testing the webhook…', 'info');
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          event: 'test',
          timestamp: new Date().toISOString(),
          data: { message: 'Open Pin webhook test' },
        }),
      });
      if (!resp.ok) {
        toast(`The webhook answered ${resp.status}. Added it anyway.`, 'info');
      } else {
        toast('Webhook tested and added', 'success');
      }
    } catch {
      toast('Couldn\'t reach that URL. Added it anyway: check the URL.', 'info');
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

  // Hugging Face token (optional pyannote fallback). The backend validates it
  // and keeps it in its own file; settings only ever report hf_token_set.
  $('#hf-token-save-btn').addEventListener('click', () => saveHfTokenFromInput());
  $('#hf-token-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') saveHfTokenFromInput();
  });
  $('#hf-token-input').addEventListener('paste', () => setTimeout(() => saveHfTokenFromInput(), 50));
  $('#hf-token-remove-btn').addEventListener('click', async () => {
    try {
      await window.plaud.saveSettings({ remove_hf_token: true });
      app.settings.hf_token_set = false;
      toast('Hugging Face token removed', 'info');
      renderDiarizationSection(app.settings.transcription_provider || 'deepgram');
    } catch (err) {
      toast(`Couldn't remove the token: ${err.message}`, 'error');
    }
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

  $('#notion-duplicates-btn').addEventListener('click', async () => {
    const btn = $('#notion-duplicates-btn');
    setLoading(btn, true);
    try {
      const result = await window.plaud.notionDuplicates();
      if (result.error) {
        toast(`Couldn't check for duplicates: ${result.error}`, 'error');
      } else {
        renderNotionDuplicates(result);
      }
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    } finally {
      setLoading(btn, false);
    }
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

  // Cloud login (OAuth browser flow). The waiting panel (#cloud-login-waiting) is
  // static markup in index.html; renderCloudAccount() shows it while
  // app.cloudLoginPending. Never replace #cloud-logged-out's markup: its buttons'
  // listeners are bound once, here.
  $('#cloud-login-browser-btn').addEventListener('click', async () => {
    try {
      const result = await window.plaud.cloudLoginUrl('eu');
      if (result.url) {
        app.pendingDesktopUuid = result.desktop_uuid;
        window.plaud.openExternal(result.url);
        app.cloudLoginPending = true;
        renderCloudAccount();
      }
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    }
  });

  $('#cloud-token-login-btn-waiting').addEventListener('click', () =>
    _cloudSignInFromInput($('#cloud-token-input-waiting'), $('#cloud-token-login-btn-waiting')),
  );
  $('#cloud-cancel-login').addEventListener('click', () => {
    app.pendingDesktopUuid = null;
    app.cloudLoginPending = false;
    $('#cloud-token-input-waiting').value = '';
    renderCloudAccount();
  });

  // Direct sign-in: pasted link, code or token
  $('#cloud-token-login-btn').addEventListener('click', () =>
    _cloudSignInFromInput($('#cloud-token-input'), $('#cloud-token-login-btn')),
  );

  // Sync direction
  for (const radio of ['sync-none', 'sync-download', 'sync-twoway']) {
    $(`#${radio}`).addEventListener('change', async (e) => {
      const dir = e.target.value;
      app.settings.cloud_sync_direction = dir;
      await window.plaud.saveSettings({ cloud_sync_direction: dir });
      // Show/hide the sync button; dim the interval (Automation) when off
      const syncActions = $('#cloud-sync-actions');
      if (syncActions) syncActions.classList.toggle('hidden', dir === 'none');
      renderAutomationState();
      // Restart polling timers to apply new sync direction
      if (app.restartPollingTimers) app.restartPollingTimers();
    });
  }

  // Sync interval
  $('#cloud-sync-interval').addEventListener('change', async (e) => {
    app.settings.cloud_sync_interval = e.target.value;
    await window.plaud.saveSettings({ cloud_sync_interval: e.target.value });
    toast(`Plaud cloud sync: ${e.target.options[e.target.selectedIndex].text.toLowerCase()}`, 'success');
    // Restart polling timers to apply new interval
    if (app.restartPollingTimers) app.restartPollingTimers();
  });

  // Cloud sync button
  $('#cloud-sync-btn').addEventListener('click', async () => {
    const btn = $('#cloud-sync-btn');
    setLoading(btn, true);
    const statusEl = $('#cloud-sync-status');
    const direction = $('#sync-twoway').checked ? 'twoway' : 'download';
    statusEl.textContent = direction === 'twoway' ? 'Syncing both ways…' : 'Downloading from Plaud\u2019s cloud…';

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

        const summary = parts.length > 0 ? parts.join(', ') : 'Everything is up to date';
        statusEl.textContent = summary;
        toast(`Cloud sync: ${summary}`, 'success');

        await loadRecordings();
        // Downloads from Plaud Cloud were always transcribed: in the background now.
        if (queueAutoProcess(newDown, { always: true, language: app.settings.language || 'en' }) > 0) {
          statusEl.textContent = `${summary}. Transcribing in the background.`;
        }
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
      app.cloudLoginPending = false;
      app.pendingDesktopUuid = null;
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
    results.innerHTML = '<div class="dm-scanning">Looking for nearby devices</div>';

    try {
      const data = await window.plaud.scanAll();
      const devices = data.devices || [];

      if (devices.length === 0) {
        results.innerHTML = `<p class="settings-desc" style="text-align:center;padding:12px;">
          No devices found. Make sure your NotePin is nearby and not connected to the Plaud phone app.
        </p>`;
      } else {
        results.innerHTML = devices
          .map((d) => {
            const sn = d.serial_number ? `Serial ${d.serial_number}` : '';
            const model = d.model || '';
            const detail = [model, sn, `${d.rssi} dBm`].filter(Boolean).join(' · ');
            return `
            <div class="dm-device-row" data-address="${escapeHtml(d.address)}" data-name="${escapeHtml(prettyDeviceName(d.name) || 'Plaud')}" data-sn="${escapeHtml(d.serial_number || '')}" role="button" tabindex="0">
              <div class="dm-device-row-icon">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><rect x="5" y="1" width="6" height="14" rx="2"/></svg>
              </div>
              <div class="dm-device-row-info">
                <div class="dm-device-row-name">${escapeHtml(prettyDeviceName(d.name) || 'Plaud device')}</div>
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
      btn.textContent = 'Click again to erase';
      btn.style.background = 'var(--destructive-fill-hover)';
      btn.dataset.confirmed = 'yes';
      setTimeout(() => {
        if (btn.dataset.confirmed === 'yes') {
          btn.innerHTML = 'Erase &amp; disconnect';
          btn.style.background = '';
          btn.dataset.confirmed = '';
        }
      }, 3000);
      return;
    }
    btn.dataset.confirmed = '';
    btn.innerHTML = 'Erase &amp; disconnect';
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

  initPairingBackupEvents();
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
      local: 'Recordings are kept on this Mac only.',
      cloud: 'Recordings are uploaded to Google Drive, then removed from this Mac to save space.',
      both: 'Recordings are kept on this Mac and backed up to Google Drive.',
    };
    let text = hints[mode] || '';
    if (!gdConnected) {
      text = mode === 'local'
        ? `${text} Connect Google Drive and choose a folder to keep them there too.`
        : 'Connect Google Drive and choose a folder above to keep recordings there.';
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
      <h3>${isReset ? 'Reset the recordings folder' : 'Change the recordings folder'}</h3>
      <p class="settings-desc">New recordings will save to <strong>${escapeHtml(shortNew)}</strong></p>
      <p class="settings-desc" style="margin-top:var(--sp-2);">What should happen to your existing recordings?</p>
      <div class="delete-dialog-actions">
        <button class="action-btn primary" id="migrate-move-all">
          Move them all to the new folder
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
      toast(isReset ? 'Recordings folder reset to the default' : 'Recordings folder changed');
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
      toast(`Moving ${allIds.length} recording${allIds.length > 1 ? 's' : ''}…`, 'info');
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
          toast('Connect Google Drive and choose a folder first', 'error');
          return;
        }
        try {
          await window.plaud.saveSettings({ storage_mode: mode });
          app.settings.storage_mode = mode;
          renderStorageSettings();
          toast(`Recordings kept: ${{ local: 'on this Mac', cloud: 'in Google Drive', both: 'on this Mac and in Google Drive' }[mode] || mode}`, 'success');
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
