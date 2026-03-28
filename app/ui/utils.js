/* ============================================================
   Plaud V2 — Shared UI Utilities
   ============================================================ */

// Global error reporter — catches unhandled errors and sends to GitHub Issues
window.onerror = (msg, src, line, col, err) => {
  const context = `renderer:${(src || '').split('/').pop()}:${line}`;
  window.plaud.reportError(String(msg), err?.stack || '', context).catch(() => {});
};
window.onunhandledrejection = (event) => {
  const err = event.reason;
  const msg = err instanceof Error ? err.message : String(err);
  window.plaud.reportError(msg, err?.stack || '', 'renderer-unhandled').catch(() => {});
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// Shared app state — all modules read/write through this object
const app = {
  currentView: 'list',
  recordings: [],
  transcripts: {},
  settings: {},
  isRecording: false,
  pollInterval: null,
  selectMode: false,
  selectedIds: new Set(),
  searchQuery: '',
  activeTagFilters: new Set(),
  processingIds: new Map(),
  cloudState: { logged_in: false, user: null },
  notionState: { connected: false, database_id: null, database_name: null },
  deviceStateCache: null,
  transcribeSpeed: null,
  activeTab: 'transcript',
  pendingDesktopUuid: null,
};

// DOM refs — cached once
const dom = {
  statusEl: $('#device-status'),
  statusText: $('#status-text'),
  batteryText: $('#battery-text'),
  emptyState: $('#empty-state'),
  recordingsList: $('#recordings-list'),
  transcriptView: $('#transcript-view'),
  scanBtn: $('#scan-btn'),
  syncBtn: $('#sync-btn'),
  disconnectBtn: $('#disconnect-btn'),
  progressBar: $('#progress-bar'),
  progressFill: $('#progress-fill'),
  progressText: $('#progress-text'),
  settingsBtn: $('#settings-btn'),
  settingsPanel: $('#settings-panel'),
  settingsOverlay: $('#settings-overlay'),
  settingsClose: $('#settings-close'),
  audioEl: $('#audio-element'),
  playBtn: $('#play-btn'),
  playIcon: $('#play-icon'),
  pauseIcon: $('#pause-icon'),
  playerProgress: $('#player-progress'),
  playerFill: $('#player-fill'),
  playerCurrent: $('#player-current'),
  playerDuration: $('#player-duration'),
};

function formatTime(seconds) {
  if (!seconds || isNaN(seconds)) return '00:00';
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function formatDate(isoString) {
  try {
    const d = new Date(isoString);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const recDate = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const diffDays = Math.floor((today - recDate) / 86400000);
    const timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (diffDays === 0) return `Today at ${timeStr}`;
    if (diffDays === 1) return `Yesterday at ${timeStr}`;
    if (diffDays < 7) return `${d.toLocaleDateString([], { weekday: 'long' })} at ${timeStr}`;
    return d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' }) + ` at ${timeStr}`;
  } catch {
    return isoString;
  }
}

function formatDuration(seconds) {
  if (!seconds) return '0s';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${rm}m`;
}

function toast(message, type = 'info') {
  const container = $('#toast-container');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

function showProgress(text, pct) {
  dom.progressBar.classList.remove('hidden');
  dom.progressFill.style.width = `${pct}%`;
  dom.progressText.textContent = text;
}

function hideProgress() {
  dom.progressBar.classList.add('hidden');
  dom.progressFill.style.width = '0%';
  dom.progressText.textContent = '';
}

function setLoading(btn, loading) {
  if (loading) {
    btn.classList.add('loading');
    btn.style.position = 'relative';
    btn.disabled = true;
  } else {
    btn.classList.remove('loading');
    btn.disabled = false;
  }
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function renderMarkdown(md) {
  let html = escapeHtml(md);
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/^---$/gm, '<hr>');
  html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
  html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
  html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/^(?!<[hublio]|<\/|<hr)(.+)$/gm, '<p>$1</p>');
  html = html.replace(/<p>\s*<\/p>/g, '');
  return html;
}

function openSettings() {
  dom.settingsPanel.classList.remove('hidden');
  dom.settingsOverlay.classList.remove('hidden');
  dom.settingsPanel.offsetHeight;
  dom.settingsPanel.classList.add('visible');
  dom.settingsOverlay.classList.add('visible');
}

function closeSettings() {
  dom.settingsPanel.classList.remove('visible');
  dom.settingsOverlay.classList.remove('visible');
  dom.settingsPanel.addEventListener('transitionend', function handler() {
    if (!dom.settingsPanel.classList.contains('visible')) {
      dom.settingsPanel.classList.add('hidden');
      dom.settingsOverlay.classList.add('hidden');
    }
    dom.settingsPanel.removeEventListener('transitionend', handler);
  });
}
