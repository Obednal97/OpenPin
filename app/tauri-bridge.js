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
  if (_apiUrl) {
    // keepalive fetch (not sendBeacon): beacons can't carry the X-Plaud-Token
    // header the backend requires, so they'd be rejected with 401.
    const url = `${_apiUrl}/device/disconnect`;
    fetch(url, {
      method: 'POST',
      headers: _bridgeHeaders({ 'Content-Type': 'application/json' }),
      body: '{}',
      keepalive: true,
    }).catch(() => {});
  }
});

// --- Dynamic API URL + per-launch API token (resolved from Rust backend) ---
// The Python backend rejects any request without the token Rust generated at
// launch (header X-Plaud-Token; GET /audio also accepts ?token=). It is the
// same for the whole app session, including backend crash restarts.

const _BRIDGE_TOKEN_HEADER = 'X-Plaud-Token';
let _apiUrl = null;
let _bridgeApiToken = '';
let _apiInfoReady = invoke('get_api_info').then((info) => {
  _apiUrl = info.url;
  _bridgeApiToken = info.token || '';
  return info;
});

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
  '/transcribe', '/summarize',
  '/cloud/sync',
  '/gdrive/sync', '/gdrive/upload',
  '/recordings/redownload', '/recordings/crop', '/recordings/export',
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

async function apiCall(method, endpoint, body = null) {
  if (!_apiUrl || !_bridgeApiToken) await _apiInfoReady;
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
      const info = await invoke('get_api_info');
      if (info.token) {
        _bridgeApiToken = info.token;
        opts.headers = _bridgeHeaders({ 'Content-Type': 'application/json' });
      }
      if (info.url && info.url !== _apiUrl) {
        _apiUrl = info.url;
        // Retry once with the new URL
        res = await _fetchWithTimeout(`${_apiUrl}${endpoint}`, opts, endpoint);
      } else {
        throw err; // Same URL, genuinely unreachable
      }
    } catch {
      if (REPORT_ENDPOINTS.has(endpoint)) {
        invoke('report_error', {
          message: `API fetch failed: ${endpoint} — ${err.message}`,
          stack: '',
          context: `api:${endpoint}`,
        }).catch(() => {});
      }
      throw err;
    }
  }
  const data = await res.json();
  const errMsg = (data.raw_error || data.error || '').toLowerCase();
  // Transient / environmental errors — not worth auto-reporting to GitHub.
  const TRANSIENT_MARKERS = [
    'not found',
    'not in range',
    'timeout',
    'timed out',
    'cancellederror',
    'disconnected',
    'went out of range',
  ];
  const isTransient = TRANSIENT_MARKERS.some((m) => errMsg.includes(m));
  if (REPORT_ENDPOINTS.has(endpoint) && (data.error || !res.ok) && !isTransient) {
    const snTag = data.device_sn ? ` [SN: ${data.device_sn}]` : '';
    const reportMsg = data.raw_error || data.error || `HTTP ${res.status}`;
    invoke('report_error', {
      message: `API error: ${endpoint}${snTag} — ${reportMsg}`,
      stack: data.traceback || '',
      context: `api:${endpoint}`,
    }).catch(() => {});
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
  sync: () => apiCall('POST', '/device/sync'),
  syncStatus: () => apiCall('GET', '/device/sync-status'),
  abortSync: () => apiCall('POST', '/device/abort-sync'),
  wifiSync: () => apiCall('POST', '/device/wifi-sync'),
  wifiSyncStatus: () => apiCall('GET', '/device/wifi-sync-status'),

  // Transcription
  transcribe: (path, model, diarize, language) =>
    apiCall('POST', '/transcribe', { path, model, diarize, language }),

  // Data
  transcripts: () => apiCall('GET', '/transcripts'),
  recordings: () => apiCall('GET', '/recordings'),
  deleteRecordings: (session_ids) => apiCall('POST', '/recordings/delete', { session_ids }),
  moveRecordings: (session_ids, destination) => apiCall('POST', '/recordings/move', { session_ids, destination }),
  redownloadRecordings: (session_ids) => apiCall('POST', '/recordings/redownload', { session_ids }),
  cloudTrash: (ids) => apiCall('POST', '/cloud/trash', { ids }),
  deleteTranscript: (session_id) => apiCall('POST', '/transcripts/delete', { session_id }),
  tagRecording: (session_id, tags) => apiCall('POST', '/recordings/tag', { session_id, tags }),
  renameRecording: (session_id, title) => apiCall('POST', '/recordings/rename', { session_id, title }),
  exportRecording: (session_id) => apiCall('POST', '/recordings/export', { session_id }),
  cropRecording: (session_id, start, end, mode) =>
    apiCall('POST', '/recordings/crop', { session_id, start, end, mode }),
  updateTranscript: (session_id, updates) =>
    apiCall('POST', '/transcripts/update', { session_id, ...updates }),
  summarize: (session_id) => apiCall('POST', '/summarize', { session_id }),

  // Settings
  getSettings: () => apiCall('GET', '/settings'),
  saveSettings: (settings) => apiCall('POST', '/settings', settings),
  validateKey: (provider, key) => apiCall('POST', '/validate-key', { provider, key }),

  // Cloud account
  cloudStatus: () => apiCall('GET', '/cloud/status'),
  cloudLoginUrl: (region) => apiCall('POST', '/cloud/login-url', { region }),
  cloudExchangeToken: (auth_code, desktop_uuid) =>
    apiCall('POST', '/cloud/exchange-token', { auth_code, desktop_uuid }),
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
  notionPush: (session_id) => apiCall('POST', '/notion/push', { session_id }),
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

  // Bug reporter — IPC to Rust
  reportError: (message, stack, context) => invoke('report_error', { message, stack, context }),
  reportBug: (expected, actual, steps) => invoke('report_bug', { expected, actual, steps }),
  setBackendHealth: (state) => invoke('set_backend_health', { state }),
  openDiagnosticsLog: () => invoke('open_diagnostics_log'),
};
