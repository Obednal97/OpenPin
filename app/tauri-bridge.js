/**
 * Tauri IPC bridge
 *
 * Exposes the window.plaud.* API so the rest of the frontend
 * (renderer.js, ui/*.js) can call backend commands and receive events.
 *
 * HTTP API calls use fetch() to the Python backend.
 * IPC calls use Tauri's invoke() for Rust commands and listen() for events.
 */

/* global __TAURI__ */
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

// --- BLE disconnect on page reload (Cmd+R) to prevent stale connections ---
window.addEventListener('beforeunload', () => {
  if (!_apiUrl) return;
  const url = `${_apiUrl}/device/disconnect`;
  // sendBeacon with a text/plain body is a CORS "simple request": no preflight,
  // which WKWebView may drop while the page unloads. A beacon can't set the
  // X-Plaud-Token header, so the token goes in ?token= (the backend accepts the
  // query form only for GET /audio and POST /device/disconnect).
  const beaconUrl = _bridgeApiToken ? `${url}?token=${encodeURIComponent(_bridgeApiToken)}` : url;
  let sent = false;
  try {
    if (navigator.sendBeacon) sent = navigator.sendBeacon(beaconUrl, new Blob(['{}'], { type: 'text/plain' }));
  } catch {
    sent = false;
  }
  if (sent) return;
  // Fallback: keepalive fetch with the header (needs a preflight).
  fetch(url, {
    method: 'POST',
    headers: _bridgeHeaders({ 'Content-Type': 'application/json' }),
    body: '{}',
    keepalive: true,
  }).catch(() => {});
});

// --- Dynamic API URL + per-launch API token (resolved from Rust backend) ---
// The Python backend rejects any request without the token Rust generated at
// launch (header X-Plaud-Token; GET /audio also accepts ?token=). It is the
// same for the whole app session, including backend crash restarts.

const _BRIDGE_TOKEN_HEADER = 'X-Plaud-Token';
let _apiUrl = null;
let _bridgeApiToken = '';
// get_api_info's status: 'starting' | 'ready' | 'error' | 'stopped'. At page
// load the backend usually hasn't bound its real port yet, so the URL read
// then can be stale; until Rust says 'ready', apiCall re-reads it first.
let _bridgeApiStatus = null;
let _bridgeApiInfoInflight = null;

/** Re-read the backend URL/token/status from Rust (one IPC call in flight at a time). */
function _bridgeRefreshApiInfo() {
  if (!_bridgeApiInfoInflight) {
    _bridgeApiInfoInflight = invoke('get_api_info')
      .then((info) => {
        if (info && info.url) _apiUrl = info.url;
        if (info && info.token) _bridgeApiToken = info.token;
        _bridgeApiStatus = (info && info.status) || null;
        return info;
      })
      .finally(() => {
        _bridgeApiInfoInflight = null;
      });
  }
  return _bridgeApiInfoInflight;
}

let _apiInfoReady = _bridgeRefreshApiInfo().catch(() => null);

function _bridgeHeaders(extra) {
  const headers = { ...(extra || {}) };
  if (_bridgeApiToken) headers[_BRIDGE_TOKEN_HEADER] = _bridgeApiToken;
  return headers;
}

// --- HTTP proxy (same logic as preload.js apiCall) ---

const REPORT_ENDPOINTS = new Set([
  '/device/connect', '/device/scan', '/device/scan-known', '/device/wifi-sync',
  '/device/sync', '/device/depair',
  '/transcribe',
  '/cloud/sync', '/cloud/exchange-token',
  '/notion/push', '/notion/connect',
]);
// Endpoints that are legitimately long-running — never abort these on timeout
const NO_TIMEOUT_ENDPOINTS = new Set([
  '/device/connect', // BLE pairing — service discovery can take 30-45s on slow links
  '/device/sync', '/device/wifi-sync',
  '/transcribe', '/summarize', '/notion/push', // Notion creates of long transcripts can exceed 30s
  '/notion/duplicates', // reads every page of the database the first time
  '/cloud/sync',
  '/gdrive/sync', '/gdrive/upload',
  '/recordings/redownload', '/recordings/crop', '/recordings/export',
  '/coaching/voice/install', // downloads and installs the speaker model
  '/coaching/voice/setup', // a first setup can load the model and decode a whole recording
]);
const DEFAULT_TIMEOUT_MS = 30000;

async function _fetchWithTimeout(url, opts, endpoint) {
  if (NO_TIMEOUT_ENDPOINTS.has(endpoint)) return fetch(url, opts);
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
  try {
    return await fetch(url, { ...opts, signal: controller.signal });
  } finally {
    clearTimeout(timeoutId);
  }
}

// --- Error text from API errors (local diagnostics log) ---
// /transcribe, /summarize and /notion/* errors can quote transcript or Notion
// page content (raw_error, traceback), so for those only the endpoint, HTTP
// status and a short first line are logged. Rust scrubs exported bug reports
// again (src-tauri/src/commands/report_scrub.rs); this is a simplified mirror.

const _BRIDGE_REPORT_SHORT_CHARS = 120;

function _bridgeIsPrivateEndpoint(endpoint) {
  const path = String(endpoint).split('?')[0];
  return path === '/transcribe' || path === '/summarize' || path.startsWith('/notion/');
}

/** Remove URLs' queries and document paths, credentials, keys, e-mails, long tokens. */
function _bridgeScrubReportText(text) {
  return String(text || '')
    .replace(/\bhttps?:\/\/[^\s"'<>`)\]}|\\]+/gi, (url) => {
      const m = url.match(/^(https?:\/\/)(?:[^/?#@]*@)?([^/?#]*)([^?#]*)/i);
      if (!m) return '[url]';
      const host = m[2].toLowerCase().split(':')[0];
      if (/(^|\.)(notion\.(so|site|com)|drive\.google\.com|docs\.google\.com|googleusercontent\.com|googleapis\.com)$/.test(host)) {
        return `${m[1]}${m[2]}/[redacted]`;
      }
      return `${m[1]}${m[2]}${m[3]}${url.length > m[0].length ? '?[redacted]' : ''}`;
    })
    .replace(/\b(bearer|basic)([ \t]+)[A-Za-z0-9._~+/=-]{8,}/gi, '$1$2[redacted]')
    .replace(
      /\b(api[_-]?key|x-api-key|access_token|refresh_token|id_token|client_secret|x-plaud-token|authorization|password|passwd|secret|token)(["']?\s*[:=]\s*["']?)[^\s"',;&)}\]]+/gi,
      '$1$2[redacted]',
    )
    .replace(/(^|[^A-Za-z0-9_-])(github_pat_|sk-proj-|sk-ant-|secret_|xox[bp]-|ntn_|gh[pousr]_|sk-|hf_|pk-)[A-Za-z0-9_-]{16,}/g, '$1$2[redacted]')
    .replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}/g, '[email]')
    .replace(/\b[0-9a-fA-F]{32,}\b/g, '[redacted]')
    .replace(/[A-Za-z0-9+/_-]{32,}={0,2}/g, (t) =>
      /[A-Z]/.test(t) && /[a-z]/.test(t) && /[0-9]/.test(t) && (!t.includes('/') || t.split('/').some((s) => s.length >= 24))
        ? '[redacted]'
        : t,
    );
}

/** First line of an error, scrubbed and cut to 120 characters. */
function _bridgeShortError(text) {
  const first = String(text || '').split(/\r?\n/).find((l) => l.trim()) || '';
  const line = _bridgeScrubReportText(first.trim());
  return line.length > _BRIDGE_REPORT_SHORT_CHARS ? `${line.slice(0, _BRIDGE_REPORT_SHORT_CHARS - 1)}…` : line;
}

async function apiCall(method, endpoint, body = null) {
  // Until Rust reports 'ready', the cached URL may point at a port the
  // backend hasn't bound (or has since moved off): re-read it first.
  if (!_apiUrl || !_bridgeApiToken || _bridgeApiStatus !== 'ready') {
    try {
      await _bridgeRefreshApiInfo();
    } catch {
      await _apiInfoReady;
    }
  }
  const opts = {
    method,
    headers: _bridgeHeaders({ 'Content-Type': 'application/json' }),
  };
  if (body) {
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await _fetchWithTimeout(`${_apiUrl}${endpoint}`, opts, endpoint);
  } catch (err) {
    // Connection failed — re-resolve the API URL in case the port changed (server restart)
    try {
      const previousUrl = _apiUrl;
      const info = await _bridgeRefreshApiInfo();
      if (info && info.token) {
        opts.headers = _bridgeHeaders({ 'Content-Type': 'application/json' });
      }
      if (info && info.url && info.url !== previousUrl) {
        // Retry once with the new URL
        res = await _fetchWithTimeout(`${_apiUrl}${endpoint}`, opts, endpoint);
      } else {
        throw err; // Same URL, genuinely unreachable
      }
    } catch {
      // An abort is this page giving up on its own request (the timeout above,
      // or a reconnect replacing it), not a backend failure.
      const aborted = (err && err.name === 'AbortError') || /\babort/i.test(String(err && err.message));
      if (REPORT_ENDPOINTS.has(endpoint) && !aborted) {
        invoke('report_error', {
          message: `API fetch failed: ${endpoint} — ${_bridgeShortError(err && err.message)}`,
          stack: '',
          context: `api:${endpoint}`,
        }).catch(() => {});
      }
      throw err;
    }
  }
  const data = await res.json();
  const errMsg = (data.raw_error || data.error || '').toLowerCase();
  // Transient / environmental errors — not worth logging as errors.
  const TRANSIENT_MARKERS = [
    'not found',
    'not in range',
    'timeout',
    'timed out',
    'cancellederror',
    'disconnected',
    'went out of range',
    'already in progress',
    'powered off',
    'powered_off', // bleak's BleakBluetoothNotAvailableReason.POWERED_OFF
    'turned off', // "Bluetooth device is turned off"
    'not connected', // the renderer re-checks the link and retries once
    'invalid or has expired', // expired Plaud sign-in code: the user signs in again
    'ignored a plaud sign-in link', // a plaud:// link nobody asked for
  ];
  // 409 / busy = "a sync is already running" — expected, never a bug.
  const isTransient = res.status === 409 || data.busy === true || TRANSIENT_MARKERS.some((m) => errMsg.includes(m));
  if (REPORT_ENDPOINTS.has(endpoint) && (data.error || !res.ok) && !isTransient) {
    const snTag = data.device_sn ? ` [SN: ${data.device_sn}]` : '';
    let message;
    let stack;
    if (_bridgeIsPrivateEndpoint(endpoint)) {
      // Never raw_error / traceback here: they can quote transcript or page
      // text. Send the exception class if raw_error starts with one, else
      // the first line of the error.
      const cls = (String(data.raw_error || '').match(/^\s*([A-Za-z_][\w.]*(?:Error|Exception|Timeout))\b/) || [])[1];
      const short = cls ? _bridgeShortError(cls) : _bridgeShortError(data.error || data.raw_error);
      message = `API error: ${endpoint} — HTTP ${res.status}${short ? `: ${short}` : ''}`;
      stack = '';
    } else {
      message = `API error: ${endpoint}${snTag} — ${data.raw_error || data.error || `HTTP ${res.status}`}`;
      stack = data.traceback || '';
    }
    invoke('report_error', { message, stack, context: `api:${endpoint}` }).catch(() => {});
  }
  return data;
}

// --- Event listeners (server-push events via Tauri's listen()) ---

const _eventCallbacks = {
  'startup-error': [],
  'startup-status': [],
  'oauth-callback': [],
  'system-wake': [],
  'notification-action': [],
  'update-status': [],
  'tray-sync': [],
};

// Set up Tauri event listeners
for (const eventName of Object.keys(_eventCallbacks)) {
  listen(eventName, (event) => {
    for (const cb of _eventCallbacks[eventName]) {
      cb(event.payload);
    }
  });
}

// macOS appearance flips while the app is running — Rust emits this from the
// .GlobalPreferences.plist watcher. Re-resolve so `auto` follows the system.
listen('system-theme-changed', () => {
  if (window.plaud && window.plaud.getTheme) {
    window.plaud.getTheme().catch(() => {});
  }
});

// --- Plaud sign-in links (plaud://login?auth_code=…; src-tauri/src/deep_link.rs) ---
// Rust keeps the code until the page takes it and emits 'plaud-sign-in-link'
// (no payload). The link may have launched the app, so check once after every
// script has loaded too. Wait for the backend (the exchange needs it), then take
// the code (handed out once) and give it to the 'oauth-callback' listeners.
let _bridgeSignInDraining = false;
let _bridgeSignInRecheck = false;

async function _bridgeDrainSignInLink() {
  if (_bridgeSignInDraining) {
    _bridgeSignInRecheck = true;
    return;
  }
  _bridgeSignInDraining = true;
  try {
    do {
      _bridgeSignInRecheck = false;
      while (await invoke('has_pending_sign_in_code')) {
        let info = null;
        try {
          info = await _bridgeRefreshApiInfo();
        } catch {
          info = null;
        }
        if (info && info.status === 'ready') {
          const code = await invoke('take_pending_sign_in_code');
          if (code) for (const cb of _eventCallbacks['oauth-callback']) cb(code);
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    } while (_bridgeSignInRecheck);
  } catch (err) {
    console.error('[sign-in link]', err && err.message ? err.message : err);
  } finally {
    _bridgeSignInDraining = false;
  }
}

listen('plaud-sign-in-link', () => _bridgeDrainSignInLink());
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => _bridgeDrainSignInLink(), { once: true });
} else {
  setTimeout(_bridgeDrainSignInLink, 0);
}

function addListener(eventName, callback) {
  if (_eventCallbacks[eventName]) {
    _eventCallbacks[eventName].push(callback);
  }
}

// --- Expose window.plaud (identical API to preload.js) ---

window.plaud = {
  // Device
  scan: () => apiCall('POST', '/device/scan'),
  scanKnown: () => apiCall('POST', '/device/scan-known'),
  scanAll: () => apiCall('POST', '/device/scan-all'),
  connect: (address, serial_number) => apiCall('POST', '/device/connect', { address, serial_number }),
  disconnect: () => apiCall('POST', '/device/disconnect'),
  verifyConnection: () => apiCall('POST', '/device/verify'),
  depair: () => apiCall('POST', '/device/depair'),
  status: () => apiCall('GET', '/device/status'),
  rssi: () => apiCall('GET', '/device/rssi'),
  deviceInfo: () => apiCall('GET', '/device/info'),
  setFindMy: (enabled) => apiCall('POST', '/device/find-my', { enabled }),
  files: () => apiCall('GET', '/device/files'),
  checkNew: () => apiCall('GET', '/device/check-new'),
  // Pairing backup (Settings › Device). Responses never contain credentials.
  pairingStatus: () => apiCall('GET', '/device/pairing'),
  exportPairing: (path) => apiCall('POST', '/device/pairing/export', { path }),
  importPairing: (path) => apiCall('POST', '/device/pairing/import', { path }),
  // Native save/open panels from the dialog plugin (capability dialog:default
  // allows save and open). Resolve to an absolute path, or null if cancelled.
  choosePairingExportPath: (defaultPath) =>
    invoke('plugin:dialog|save', {
      options: {
        title: 'Export pairing',
        defaultPath,
        filters: [{ name: 'Pairing export', extensions: ['json'] }],
        canCreateDirectories: true,
      },
    }),
  choosePairingImportFile: () =>
    invoke('plugin:dialog|open', {
      options: {
        title: 'Import pairing',
        multiple: false,
        directory: false,
        filters: [{ name: 'Pairing export', extensions: ['json'] }],
      },
    }),
  // opts e.g. { background: true } for automatic syncs; no args = no body, as before.
  sync: (opts) => apiCall('POST', '/device/sync', opts || null),
  syncStatus: () => apiCall('GET', '/device/sync-status'),
  abortSync: () => apiCall('POST', '/device/abort-sync'),
  wifiSync: () => apiCall('POST', '/device/wifi-sync'),
  wifiSyncStatus: () => apiCall('GET', '/device/wifi-sync-status'),
  ackWifiRestoreNotice: () => apiCall('POST', '/device/wifi-restore-notice/ack'),

  // Transcription
  transcribe: (path, model, diarize, language) =>
    apiCall('POST', '/transcribe', { path, model, diarize, language }),

  // Data
  transcripts: () => apiCall('GET', '/transcripts'),
  recordings: () => apiCall('GET', '/recordings'),
  deleteRecordings: (session_ids) => apiCall('POST', '/recordings/delete', { session_ids }),
  moveRecordings: (session_ids, destination) => apiCall('POST', '/recordings/move', { session_ids, destination }),
  redownloadRecordings: (session_ids) => apiCall('POST', '/recordings/redownload', { session_ids }),
  restoreRedownload: (sessionIds) => apiCall('POST', '/recordings/restore-redownload', { session_ids: sessionIds }),
  cloudTrash: (ids) => apiCall('POST', '/cloud/trash', { ids }),
  deleteTranscript: (session_id) => apiCall('POST', '/transcripts/delete', { session_id }),
  tagRecording: (session_id, tags) => apiCall('POST', '/recordings/tag', { session_id, tags }),
  // Add or remove single tags: { tags: { sid: [...] }, errors?: { sid: msg } }.
  addTags: (session_ids, tags) => apiCall('POST', '/recordings/tags/add', { session_ids, tags }),
  removeTags: (session_ids, tags) => apiCall('POST', '/recordings/tags/remove', { session_ids, tags }),
  listTags: () => apiCall('GET', '/tags'),
  renameRecording: (session_id, title) => apiCall('POST', '/recordings/rename', { session_id, title }),
  exportRecording: (session_id) => apiCall('POST', '/recordings/export', { session_id }),
  cropRecording: (session_id, start, end, mode) =>
    apiCall('POST', '/recordings/crop', { session_id, start, end, mode }),
  updateTranscript: (session_id, updates) =>
    apiCall('POST', '/transcripts/update', { session_id, ...updates }),
  summarize: (session_id) => apiCall('POST', '/summarize', { session_id }),
  // Speaker names. names maps labels to a name, or null to clear it. Returns
  // { speaker_names } (the names as stored) or { error } with a message to show.
  setSpeakerNames: (session_id, names) =>
    apiCall('POST', '/transcripts/speaker-names', { session_id, names }),
  ackSpeakerNamesCleared: (session_id) =>
    apiCall('POST', '/transcripts/speaker-names/ack-cleared', { session_id }),
  // Name suggestions, [{ name, recordings, person_id, has_voice }]: people known by
  // voice first, then other typed names, each most used first
  people: () => apiCall('GET', '/people'),
  // "Not them" on a voice-matched speaker: { speaker_names, speaker_name_sources }
  rejectVoiceMatch: (session_id, label) =>
    apiCall('POST', '/transcripts/speaker-names/reject', { session_id, label }),

  // People known by voice (Settings › People). Rename, merge and delete return the
  // directory afterwards, or { error } with a message to show.
  peopleDirectory: () => apiCall('GET', '/people/directory'),
  renamePerson: (id, name) => apiCall('POST', `/people/${encodeURIComponent(id)}/rename`, { name }),
  mergePerson: (id, into) => apiCall('POST', `/people/${encodeURIComponent(id)}/merge`, { into }),
  deletePerson: (id) => apiCall('DELETE', `/people/${encodeURIComponent(id)}`),
  startPeopleBacklog: () => apiCall('POST', '/people/backlog'),
  cancelPeopleBacklog: () => apiCall('DELETE', '/people/backlog'),

  // Conversation coaching
  coaching: (session_id) => apiCall('GET', `/coaching/${encodeURIComponent(session_id)}`),
  coachingRun: (session_id) => apiCall('POST', `/coaching/${encodeURIComponent(session_id)}/run`),
  coachingSetOwner: (session_id, labels, learn) =>
    apiCall('POST', `/coaching/${encodeURIComponent(session_id)}/owner`, { labels, learn }),
  coachingMixedUp: (session_id, mixed_up) =>
    apiCall('POST', `/coaching/${encodeURIComponent(session_id)}/mixed-up`, { mixed_up }),
  coachingExclude: (session_id, exclude) =>
    apiCall('POST', `/coaching/${encodeURIComponent(session_id)}/exclude`, { exclude: !!exclude }),
  coachingProgress: (weeks) => apiCall('GET', `/coaching/progress?weeks=${encodeURIComponent(weeks ?? 12)}`),
  // "Coach them again" in Progress: older reports without Engagement, coached one at a time
  coachingRerunAllStatus: () => apiCall('GET', '/coaching/rerun-all'),
  coachingRerunAll: () => apiCall('POST', '/coaching/rerun-all'),
  coachingRerunAllCancel: () => apiCall('POST', '/coaching/rerun-all/cancel'),
  voiceStatus: () => apiCall('GET', '/coaching/voice'),
  voiceInstall: () => apiCall('POST', '/coaching/voice/install'),
  voiceSetup: (session_id, labels, replace) =>
    apiCall('POST', '/coaching/voice/setup', { session_id, labels, replace: !!replace }),
  voiceDelete: () => apiCall('DELETE', '/coaching/voice'),

  // Settings
  getSettings: () => apiCall('GET', '/settings'),
  saveSettings: (settings) => apiCall('POST', '/settings', settings),
  validateKey: (provider, key) => apiCall('POST', '/validate-key', { provider, key }),

  // AI tools (MCP): the library change counter, and one-click setup for
  // Claude Desktop / Claude Code (path: one that mcpSetupStatus listed).
  libraryChanges: () => apiCall('GET', '/changes'),
  mcpSetupStatus: () => apiCall('GET', '/mcp/setup/status'),
  mcpConnect: (kind, path) => apiCall('POST', '/mcp/setup/connect', { kind, path }),
  mcpDisconnect: (kind, path) => apiCall('POST', '/mcp/setup/disconnect', { kind, path }),

  // Cloud account
  cloudStatus: () => apiCall('GET', '/cloud/status'),
  cloudLoginUrl: (region) => apiCall('POST', '/cloud/login-url', { region }),
  // source 'link': the code came from a plaud:// link, so the backend only uses it
  // while a sign-in started here is pending (any web page can open such a link)
  cloudExchangeToken: (auth_code, desktop_uuid, source) =>
    apiCall('POST', '/cloud/exchange-token', { auth_code, desktop_uuid, source: source || 'paste' }),
  cloudLoginToken: (token, region) => apiCall('POST', '/cloud/login-token', { token, region }),
  cloudLogout: () => apiCall('POST', '/cloud/logout'),
  cloudRecordings: () => apiCall('GET', '/cloud/recordings'),
  cloudSync: (direction) => apiCall('POST', '/cloud/sync', { direction }),
  cloudDevices: () => apiCall('GET', '/cloud/devices'),
  cloudBindDevice: (sn, name, model) => apiCall('POST', '/cloud/bind-device', { sn, name, model }),
  cloudSnSign: (sn) => apiCall('POST', '/cloud/sn-sign', { sn }),

  // Audio URL builder — request M4A format since WebKit can't play OGG.
  // <audio src> can't send headers, so the API token rides in the query.
  // The URL always has a query string already: append extra params with '&'.
  audioUrl: (filename) =>
    `${_apiUrl || 'http://127.0.0.1:5123'}/audio/${encodeURIComponent(filename)}` +
    `?format=m4a&token=${encodeURIComponent(_bridgeApiToken)}`,

  // Health check
  health: () => apiCall('GET', '/health'),
  // Always returns current state — the cached _apiInfoReady is for startup
  // URL resolution; callers checking server status need a fresh value.
  getApiInfo: () => invoke('get_api_info'),
  onStartupError: (callback) => addListener('startup-error', callback),
  onStartupStatus: (callback) => addListener('startup-status', callback),

  // Notion
  notionStatus: () => apiCall('GET', '/notion/status'),
  notionConnect: () => apiCall('POST', '/notion/connect'),
  notionDisconnect: () => apiCall('POST', '/notion/disconnect'),
  notionDatabases: (q) =>
    apiCall('GET', `/notion/databases${q ? '?q=' + encodeURIComponent(q) : ''}`),
  notionSetDatabase: (database_id, database_name) =>
    apiCall('POST', '/notion/set-database', { database_id, database_name }),
  notionSetDatabaseUrl: (url) => apiCall('POST', '/notion/set-database-url', { url }),
  // opts: { link_url } to use an existing page, { force_new: true } to create one anyway
  notionPush: (session_id, opts) => apiCall('POST', '/notion/push', { session_id, ...(opts || {}) }),
  notionDuplicates: () => apiCall('GET', '/notion/duplicates'),
  notionPageUrl: (session_id) => apiCall('GET', `/notion/page-url/${session_id}`),
  notionQueueStatus: () => apiCall('GET', '/notion/queue-status'),
  notionQueueResume: () => apiCall('POST', '/notion/queue-resume'),

  // Google Drive
  gdriveStatus: () => apiCall('GET', '/gdrive/status'),
  gdriveConnect: () => apiCall('POST', '/gdrive/connect', {}),
  gdriveDisconnect: () => apiCall('POST', '/gdrive/disconnect'),
  gdriveFolders: (q) => apiCall('GET', `/gdrive/folders${q ? '?q=' + encodeURIComponent(q) : ''}`),
  gdriveCreateFolder: (name) => apiCall('POST', '/gdrive/create-folder', { name }),
  gdriveSetFolder: (folder_id, folder_name) => apiCall('POST', '/gdrive/set-folder', { folder_id, folder_name }),
  gdriveUpload: (session_id) => apiCall('POST', '/gdrive/upload', { session_id }),
  gdriveSync: () => apiCall('POST', '/gdrive/sync'),
  gdriveSyncStatus: () => apiCall('GET', '/gdrive/sync-status'),
  gdriveUploadProgress: () => apiCall('GET', '/gdrive/upload-progress'),

  // Theme — invoke Rust command, then apply the resolved theme to the DOM
  cycleTheme: async () => {
    const result = await invoke('cycle_theme');
    document.documentElement.setAttribute('data-theme', result.resolved);
    window._currentTheme = result.resolved;
    window._themePreference = result.preference;
    return result;
  },
  getTheme: async () => {
    const result = await invoke('get_theme');
    document.documentElement.setAttribute('data-theme', result.resolved);
    window._currentTheme = result.resolved;
    window._themePreference = result.preference;
    return result;
  },

  // System settings — IPC to Rust
  getLoginItem: () => invoke('get_login_item'),
  setLoginItem: (enabled) => invoke('set_login_item', { enabled }),
  getDockVisible: () => invoke('get_dock_visible'),
  setDockVisible: (visible) => invoke('set_dock_visible', { visible }),

  // OAuth callback listener
  onOAuthCallback: (callback) => addListener('oauth-callback', callback),

  // Open external URL — IPC to Rust (allowlisted to https/http/mailto)
  openExternal: (url) =>
    invoke('open_external', { url }).catch((err) => {
      console.error('[openExternal] Blocked:', err);
    }),

  // System events
  onSystemWake: (callback) => addListener('system-wake', callback),
  // Tray "Sync Device" menu item — routed through the normal in-app sync flow
  onTraySync: (callback) => addListener('tray-sync', callback),
  showNotification: (title, body, actionLabel) =>
    invoke('show_notification', { title, body, actionLabel }),
  onNotificationAction: (callback) => addListener('notification-action', callback),

  // Directory picker
  selectDirectory: () => invoke('select_directory'),

  // Auto-updater — IPC to Rust
  installUpdate: () => invoke('install_update'),
  checkForUpdates: () => invoke('check_for_updates'),
  getAppVersion: () => invoke('get_app_version'),
  onUpdateStatus: (callback) => addListener('update-status', callback),

  // Errors — IPC to Rust, which only writes the app log and the local
  // diagnostics log. Nothing is sent anywhere.
  reportError: (message, stack, context) => invoke('report_error', { message, stack, context }),
  // Report a bug: Rust saves a zip to ~/Downloads (scrubbed of personal
  // details) for the user to attach to a GitHub issue themselves.
  exportBugReport: (expected, actual, steps, checks) =>
    invoke('export_bug_report', { expected, actual, steps, checks }),
  // Only files inside ~/Downloads (Rust refuses anything else).
  revealInFinder: (path) => invoke('reveal_in_finder', { path }),
  // Checks for the bug report: booleans and counts, never secrets.
  getDiagnostics: () => apiCall('GET', '/diagnostics'),
  setBackendHealth: (state) => invoke('set_backend_health', { state }),
  openDiagnosticsLog: () => invoke('open_diagnostics_log'),
};
