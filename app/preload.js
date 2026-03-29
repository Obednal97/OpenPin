const { contextBridge, ipcRenderer } = require('electron');

// Listen for OAuth callbacks from the main process

const API_URL = 'http://127.0.0.1:5123';

async function apiCall(method, endpoint, body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body) {
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`${API_URL}${endpoint}`, opts);
  return res.json();
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

  // Audio URL builder
  audioUrl: (filename) => `${API_URL}/audio/${encodeURIComponent(filename)}`,

  // Health check
  health: () => apiCall('GET', '/health'),

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

  // Auto-updater
  installUpdate: () => ipcRenderer.invoke('install-update'),
  checkForUpdates: () => ipcRenderer.invoke('check-for-updates'),
  getAppVersion: () => ipcRenderer.invoke('get-app-version'),
  onUpdateStatus: (callback) => ipcRenderer.on('update-status', (event, data) => callback(data)),

  // Bug reporter
  reportError: (message, stack, context) => ipcRenderer.invoke('report-error', { message, stack, context }),
  reportBug: (expected, actual, steps) => ipcRenderer.invoke('report-bug', { expected, actual, steps }),
});
