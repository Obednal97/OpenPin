/* ============================================================
   Plaud V2 — Shared UI Utilities
   ============================================================ */

// Global error reporter — catches unhandled errors and sends to GitHub Issues
function _errorContext(base) {
  try {
    const parts = [base];
    if (typeof app !== 'undefined') parts.push(`view:${app.currentView || 'unknown'}`);
    return parts.join(' | ');
  } catch { return base; }
}
window.onerror = (msg, src, line, col, err) => {
  const context = _errorContext(`renderer:${(src || '').split('/').pop()}:${line}`);
  window.plaud.reportError(String(msg), err?.stack || '', context).catch(() => {});
};
window.onunhandledrejection = (event) => {
  const err = event.reason;
  const msg = err instanceof Error ? err.message : String(err);
  window.plaud.reportError(msg, err?.stack || '', _errorContext('renderer-unhandled')).catch(() => {});
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// Shared app state — all modules read/write through this object
// currentView: 'recordings' | 'transcript'
const app = {
  currentView: 'recordings',
  recordings: [],
  transcripts: {},
  settings: {},
  isRecording: false,
  pollInterval: null,
  selectMode: false,
  selectedIds: new Set(),
  searchQuery: '',
  activeTagFilters: new Set(),
  syncing: false,
  processingIds: new Map(),
  cloudState: { logged_in: false, user: null },
  notionState: { connected: false, database_id: null, database_name: null },
  deviceStateCache: null,
  transcribeSpeed: null,
  activeTab: 'transcript',
  pendingDesktopUuid: null,
  currentSessionId: null,
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

function toastAction(message, actionLabel, onAction, duration = 10000) {
  const container = $('#toast-container');
  const el = document.createElement('div');
  el.className = 'toast toast-info toast-with-action';
  el.innerHTML = `
    <div class="toast-content">
      <span class="toast-message">${escapeHtml(message)}</span>
      <button class="toast-action-btn">${escapeHtml(actionLabel)}</button>
    </div>
  `;
  const btn = el.querySelector('.toast-action-btn');
  btn.addEventListener('click', () => {
    el.remove();
    onAction();
  });
  container.appendChild(el);
  const timer = setTimeout(() => el.remove(), duration);
  el._timer = timer;
  return el;
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

// Escapes for BOTH element content and quoted attribute values ("..." or '...').
// Callers interpolating into attributes must still quote the attribute.
const _ESCAPE_HTML_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
function escapeHtml(text) {
  if (text === null || text === undefined) return '';
  return String(text).replace(/[&<>"']/g, (c) => _ESCAPE_HTML_MAP[c]);
}

/**
 * Modal confirmation dialog. Resolves with the chosen action's `id`, or null
 * when cancelled (Cancel button, Escape, or clicking the backdrop).
 *
 *   showConfirmDialog({
 *     title: 'Delete 2 recordings?',
 *     message: 'Plain text, never HTML.',
 *     items: ['Standup', 'Budget'],          // optional bullet list (plain text)
 *     note: 'Optional warning line',          // optional, shown highlighted
 *     actions: [{ id: 'trash', label: 'Move to Trash', destructive: true }],
 *     cancelLabel: 'Cancel',
 *   })
 *
 * Cancel is the default (focused, prominent) button; destructive actions are
 * styled as secondary so Enter/Return never confirms a destructive action.
 */
function showConfirmDialog(opts) {
  const o = opts || {};
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'delete-dialog-overlay confirm-dialog-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');

    const box = document.createElement('div');
    box.className = 'delete-dialog confirm-dialog';

    const h = document.createElement('h3');
    h.textContent = o.title || 'Are you sure?';
    box.appendChild(h);

    if (o.message) {
      const p = document.createElement('p');
      p.className = 'settings-desc';
      p.textContent = o.message;
      box.appendChild(p);
    }
    if (Array.isArray(o.items) && o.items.length) {
      const ul = document.createElement('ul');
      ul.className = 'confirm-dialog-items';
      o.items.forEach((it) => {
        const li = document.createElement('li');
        li.textContent = it;
        ul.appendChild(li);
      });
      box.appendChild(ul);
    }
    if (o.note) {
      const n = document.createElement('p');
      n.className = 'confirm-dialog-note';
      n.textContent = o.note;
      box.appendChild(n);
    }

    const actionsEl = document.createElement('div');
    actionsEl.className = 'delete-dialog-actions';
    (o.actions || []).forEach((a) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = `action-btn subtle${a.destructive ? ' confirm-dialog-destructive' : ''}`;
      b.dataset.action = a.id;
      b.textContent = a.label;
      b.addEventListener('click', () => finish(a.id));
      actionsEl.appendChild(b);
    });
    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'action-btn primary confirm-dialog-cancel';
    cancelBtn.dataset.action = 'cancel';
    cancelBtn.textContent = o.cancelLabel || 'Cancel';
    cancelBtn.addEventListener('click', () => finish(null));
    actionsEl.appendChild(cancelBtn);
    box.appendChild(actionsEl);
    overlay.appendChild(box);

    let done = false;
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); finish(null); }
    };
    function finish(result) {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', onKey, true);
      overlay.classList.remove('visible');
      setTimeout(() => overlay.remove(), 300);
      resolve(result);
    }
    overlay.addEventListener('click', (e) => { if (e.target === overlay) finish(null); });
    document.addEventListener('keydown', onKey, true);

    document.body.appendChild(overlay);
    overlay.offsetHeight;
    overlay.classList.add('visible');
    try { cancelBtn.focus(); } catch {}
  });
}


function renderMarkdown(md) {
  // Process line by line for proper list nesting
  const lines = md.split('\n');
  const output = [];
  let inList = false;
  let listDepth = 0;

  function closeList() {
    while (listDepth > 0) { output.push('</ul>'); listDepth--; }
    inList = false;
  }

  for (const raw of lines) {
    let line = escapeHtml(raw);

    // Headings
    if (/^### (.+)$/.test(line)) { closeList(); output.push(line.replace(/^### (.+)$/, '<h3>$1</h3>')); continue; }
    if (/^## (.+)$/.test(line)) { closeList(); output.push(line.replace(/^## (.+)$/, '<h2>$1</h2>')); continue; }
    if (/^# (.+)$/.test(line)) { closeList(); output.push(line.replace(/^# (.+)$/, '<h1>$1</h1>')); continue; }

    // Horizontal rule
    if (/^---$/.test(line)) { closeList(); output.push('<hr>'); continue; }

    // Blockquote
    if (/^&gt; (.+)$/.test(line)) { closeList(); output.push(line.replace(/^&gt; (.+)$/, '<blockquote>$1</blockquote>')); continue; }

    // List items (with nesting support)
    const listMatch = line.match(/^(\s*)-\s+(.+)$/);
    if (listMatch) {
      const indent = Math.floor(listMatch[1].length / 2) + 1; // 0 spaces = depth 1, 2 spaces = depth 2
      let content = listMatch[2];

      // Checkbox: - [ ] or - [x]
      content = content.replace(/^\[ \]\s*/, '<input type="checkbox" disabled> ');
      content = content.replace(/^\[x\]\s*/i, '<input type="checkbox" checked disabled> ');

      if (!inList) { inList = true; listDepth = 0; }
      while (listDepth < indent) { output.push('<ul>'); listDepth++; }
      while (listDepth > indent) { output.push('</ul>'); listDepth--; }
      // Inline formatting
      content = content.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
      content = content.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
      content = content.replace(/\*(.+?)\*/g, '<em>$1</em>');
      content = content.replace(/`([^`]+)`/g, '<code>$1</code>');
      output.push(`<li>${content}</li>`);
      continue;
    }

    // Bullet with * syntax
    const starMatch = line.match(/^(\s*)\*\s+(.+)$/);
    if (starMatch && !line.match(/^\*[^*]+\*$/)) { // Don't match *italic*
      const indent = Math.floor(starMatch[1].length / 2) + 1;
      let content = starMatch[2];
      content = content.replace(/^\[ \]\s*/, '<input type="checkbox" disabled> ');
      content = content.replace(/^\[x\]\s*/i, '<input type="checkbox" checked disabled> ');
      if (!inList) { inList = true; listDepth = 0; }
      while (listDepth < indent) { output.push('<ul>'); listDepth++; }
      while (listDepth > indent) { output.push('</ul>'); listDepth--; }
      content = content.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
      content = content.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
      content = content.replace(/`([^`]+)`/g, '<code>$1</code>');
      output.push(`<li>${content}</li>`);
      continue;
    }

    // Not a list item — close any open list
    closeList();

    // Empty line
    if (!line.trim()) continue;

    // Inline formatting for paragraphs
    line = line.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
    line = line.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    line = line.replace(/\*(.+?)\*/g, '<em>$1</em>');
    line = line.replace(/`([^`]+)`/g, '<code>$1</code>');

    output.push(`<p>${line}</p>`);
  }

  closeList();
  return output.join('\n');
}

function openSettings() {
  dom.settingsPanel.classList.remove('hidden');
  dom.settingsOverlay.classList.remove('hidden');
  dom.settingsPanel.offsetHeight;
  dom.settingsPanel.classList.add('visible');
  dom.settingsOverlay.classList.add('visible');
}

// ---- Tooltip ----

const _tooltipEl = document.createElement('div');
_tooltipEl.className = 'app-tooltip';
_tooltipEl.setAttribute('role', 'tooltip');
document.body.appendChild(_tooltipEl);

function showTooltip(anchor, text) {
  _tooltipEl.textContent = text;
  _tooltipEl.classList.add('visible');
  // Position below the anchor, right-aligned
  const rect = anchor.getBoundingClientRect();
  _tooltipEl.style.top = `${rect.bottom + 6}px`;
  _tooltipEl.style.right = `${window.innerWidth - rect.right}px`;
  _tooltipEl.style.left = '';
}

function hideTooltip() {
  _tooltipEl.classList.remove('visible');
}

/**
 * Create an info icon that shows a tooltip on hover.
 * Returns an HTMLElement. Usage: container.appendChild(infoTip('Helpful text'));
 */
function infoTip(text) {
  const icon = document.createElement('span');
  icon.className = 'proc-info-icon';
  icon.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.5"/><text x="8" y="12" text-anchor="middle" fill="currentColor" font-size="11" font-weight="600">i</text></svg>';
  icon.addEventListener('mouseenter', () => showTooltip(icon, text));
  icon.addEventListener('mouseleave', hideTooltip);
  return icon;
}

/**
 * Initialize all tooltip triggers on the page (elements with data-tooltip attribute).
 * Call once after DOM is ready, or after rendering new tooltip triggers.
 */
function initTooltips(root = document) {
  root.querySelectorAll('[data-tooltip]').forEach(el => {
    if (el._tooltipBound) return;
    el._tooltipBound = true;
    el.addEventListener('mouseenter', () => showTooltip(el, el.dataset.tooltip));
    el.addEventListener('mouseleave', hideTooltip);
  });
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
