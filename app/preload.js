const { contextBridge, ipcRenderer } = require('electron');

// Get API URL dynamically from main process (port may vary if 5123 was in use)
let _apiUrl = null;
let _apiInfoReady = ipcRenderer.invoke('get-api-info').then((info) => {
  _apiUrl = info.url;
  return info;
});

// Endpoints whose errors should be auto-reported to GitHub
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
  // Wait for the dynamic port to be resolved before first API call
  if (!_apiUrl) await _apiInfoReady;
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body) {
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await _fetchWithTimeout(`${_apiUrl}${endpoint}`, opts, endpoint);
  } catch (err) {
    // Network error — reporter filters by backend-health state and AbortError
    if (REPORT_ENDPOINTS.has(endpoint)) {
      ipcRenderer.invoke('report-error', {
        message: `API fetch failed: ${endpoint} — ${err.message}`,
        stack: '',
        context: `api:${endpoint}`,
      }).catch(() => {});
    }
    throw err;
  }
  const data = await res.json();
  // Auto-report errors from critical endpoints (include Python traceback if available)
  // Skip transient BLE/network errors that are expected during background reconnect
  const errMsg = (data.raw_error || data.error || '').toLowerCase();
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
    ipcRenderer.invoke('report-error', {
      message: `API error: ${endpoint}${snTag} — ${reportMsg}`,
      stack: data.traceback || '',
      context: `api:${endpoint}`,
    }).catch(() => {});
  }
  return data;
}

contextBridge.exposeInMainWorld('plaud', {
  // Device
  scan: () => apiCall('POST', '/device/scan'),
  scanKnown: () => apiCall('POST', '/device/scan-known'),
  scanAll: () => apiCall('POST', '/device/scan-all'),
  connect: (address, serial_number) => apiCall('POST', '/device/connect', { address, serial_number }),
  disconnect: () => apiCall('POST', '/device/disconnect'),
  depair: () => apiCall('POST', '/device/depair'),
  status: () => apiCall('GET', '/device/status'),
  rssi: () => apiCall('GET', '/device/rssi'),
  deviceInfo: () => apiCall('GET', '/device/info'),
  setFindMy: (enabled) => apiCall('POST', '/device/find-my', { enabled }),
  files: () => apiCall('GET', '/device/files'),
  checkNew: () => apiCall('GET', '/device/check-new'),
  sync: () => apiCall('POST', '/device/sync'),
  abortSync: () => apiCall('POST', '/device/abort-sync'),
  wifiSync: () => apiCall('POST', '/device/wifi-sync'),
  wifiSyncStatus: () => apiCall('GET', '/device/wifi-sync-status'),

  // Transcription
  transcribe: (path, model, diarize, language) => apiCall('POST', '/transcribe', { path, model, diarize, language }),

  // Data
  transcripts: () => apiCall('GET', '/transcripts'),
  recordings: () => apiCall('GET', '/recordings'),
  deleteRecordings: (session_ids) => apiCall('POST', '/recordings/delete', { session_ids }),
  cloudTrash: (ids) => apiCall('POST', '/cloud/trash', { ids }),
  deleteTranscript: (session_id) => apiCall('POST', '/transcripts/delete', { session_id }),
  tagRecording: (session_id, tags) => apiCall('POST', '/recordings/tag', { session_id, tags }),
  renameRecording: (session_id, title) => apiCall('POST', '/recordings/rename', { session_id, title }),
  exportRecording: (session_id) => apiCall('POST', '/recordings/export', { session_id }),
  cropRecording: (session_id, start, end, mode) => apiCall('POST', '/recordings/crop', { session_id, start, end, mode }),
  updateTranscript: (session_id, updates) => apiCall('POST', '/transcripts/update', { session_id, ...updates }),
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

  // Audio URL builder (port resolved by the time UI calls this — after init())
  audioUrl: (filename) => `${_apiUrl || 'http://127.0.0.1:5123'}/audio/${encodeURIComponent(filename)}`,

  // Health check
  health: () => apiCall('GET', '/health'),
  getApiInfo: () => _apiInfoReady,
  onStartupError: (callback) => ipcRenderer.on('startup-error', (event, error) => callback(error)),
  onStartupStatus: (callback) => ipcRenderer.on('startup-status', (event, msg) => callback(msg)),

  // Notion
  notionStatus: () => apiCall('GET', '/notion/status'),
  notionConnect: () => apiCall('POST', '/notion/connect'),
  notionDisconnect: () => apiCall('POST', '/notion/disconnect'),
  notionDatabases: (q) => apiCall('GET', `/notion/databases${q ? '?q=' + encodeURIComponent(q) : ''}`),
  notionSetDatabase: (database_id, database_name) =>
    apiCall('POST', '/notion/set-database', { database_id, database_name }),
  notionSetDatabaseUrl: (url) => apiCall('POST', '/notion/set-database-url', { url }),
  notionPush: (session_id) => apiCall('POST', '/notion/push', { session_id }),
  notionPageUrl: (session_id) => apiCall('GET', `/notion/page-url/${session_id}`),
  notionQueueStatus: () => apiCall('GET', '/notion/queue-status'),
  notionQueueResume: () => apiCall('POST', '/notion/queue-resume'),

  // Theme
  cycleTheme: () => ipcRenderer.invoke('cycle-theme'),
  getTheme: () => ipcRenderer.invoke('get-theme'),

  // System settings
  getLoginItem: () => ipcRenderer.invoke('get-login-item'),
  setLoginItem: (enabled) => ipcRenderer.invoke('set-login-item', enabled),
  getDockVisible: () => ipcRenderer.invoke('get-dock-visible'),
  setDockVisible: (visible) => ipcRenderer.invoke('set-dock-visible', visible),

  // OAuth callback listener
  onOAuthCallback: (callback) => ipcRenderer.on('oauth-callback', (event, authCode) => callback(authCode)),

  // Open external URL
  openExternal: (url) => ipcRenderer.invoke('open-external', url),

  // System events
  onSystemWake: (callback) => ipcRenderer.on('system-wake', () => callback()),
  showNotification: (title, body, actionLabel) =>
    ipcRenderer.invoke('show-notification', { title, body, actionLabel }),
  onNotificationAction: (callback) =>
    ipcRenderer.on('notification-action', (event, action) => callback(action)),

  // Auto-updater
  installUpdate: () => ipcRenderer.invoke('install-update'),
  checkForUpdates: () => ipcRenderer.invoke('check-for-updates'),
  getAppVersion: () => ipcRenderer.invoke('get-app-version'),
  onUpdateStatus: (callback) => ipcRenderer.on('update-status', (event, data) => callback(data)),

  // Bug reporter
  reportError: (message, stack, context) => ipcRenderer.invoke('report-error', { message, stack, context }),
  reportBug: (expected, actual, steps) => ipcRenderer.invoke('report-bug', { expected, actual, steps }),
  setBackendHealth: (state) => ipcRenderer.invoke('set-backend-health', state),
  openDiagnosticsLog: () => ipcRenderer.invoke('open-diagnostics-log'),
});
