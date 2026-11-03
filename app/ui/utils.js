/* ============================================================
   Open Pin — Shared UI Utilities
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
  syncKind: null, // 'ble' | 'wifi' while app.syncing
  syncGen: 0, // bumped by each sync flow; see _claimSync() in renderer.js
  processingIds: new Map(),
  cloudState: { logged_in: false, user: null },
  notionState: { connected: false, database_id: null, database_name: null },
  deviceStateCache: null,
  transcribeSpeed: null,
  activeTab: 'transcript',
  pendingDesktopUuid: null,
  // Settings › Plaud Cloud: true while waiting for the browser sign-in (shows #cloud-login-waiting)
  cloudLoginPending: false,
  // Bumped by each successful sign-in in Settings/renderer, so a slower loadCloudStatus() can't undo it
  cloudSignInSeq: 0,
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

/**
 * "MM:SS", or "H:MM:SS" from an hour on. Pass withHours to keep a column of
 * times the same width (e.g. every segment of a recording longer than an hour).
 */
function formatTime(seconds, withHours = false) {
  const total = !seconds || isNaN(seconds) || seconds < 0 ? 0 : Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mmss = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return h > 0 || withHours ? `${h}:${mmss}` : mmss;
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

// Toasts: errors stay until closed; the rest leave after a few seconds (longer
// messages stay longer). Every toast has a close button.
const _TOAST_MAX = 5;

function _toastDismiss(el) {
  if (!el || el._leaving) return;
  el._leaving = true;
  clearTimeout(el._timer);
  el.classList.add('toast-leaving');
  setTimeout(() => el.remove(), 300);
}

function _toastMount(el, durationMs) {
  const container = $('#toast-container');
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'toast-close';
  close.title = 'Dismiss';
  close.setAttribute('aria-label', 'Dismiss');
  close.textContent = '\u00D7';
  close.addEventListener('click', () => _toastDismiss(el));
  el.appendChild(close);
  container.appendChild(el);
  // Keep the stack short: drop the oldest ones first.
  const live = [...container.children].filter((t) => !t._leaving);
  live.slice(0, Math.max(0, live.length - _TOAST_MAX)).forEach(_toastDismiss);
  if (durationMs > 0) el._timer = setTimeout(() => _toastDismiss(el), durationMs);
  return el;
}

function toast(message, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  const text = document.createElement('span');
  text.className = 'toast-message';
  text.textContent = message;
  el.appendChild(text);
  const len = String(message || '').length;
  const duration = type === 'error' ? 0 : Math.min(8000, 3000 + len * 40);
  return _toastMount(el, duration);
}

function toastAction(message, actionLabel, onAction, duration = 10000) {
  const el = document.createElement('div');
  el.className = 'toast toast-info toast-with-action';
  el.setAttribute('role', 'status');
  el.innerHTML = `
    <div class="toast-content">
      <span class="toast-message">${escapeHtml(message)}</span>
      <button class="toast-action-btn">${escapeHtml(actionLabel)}</button>
    </div>
  `;
  const btn = el.querySelector('.toast-action-btn');
  btn.addEventListener('click', () => {
    _toastDismiss(el);
    onAction();
  });
  return _toastMount(el, duration);
}

/** "PLAUD_NOTEPIN_S" (the Bluetooth name) → "Plaud NotePin S". Other names are kept. */
function prettyDeviceName(name) {
  const raw = String(name || '').trim();
  if (!/^plaud[_\s-]/i.test(raw)) return raw;
  const words = { PLAUD: 'Plaud', NOTEPIN: 'NotePin', NOTE: 'Note', PIN: 'Pin', PRO: 'Pro' };
  return raw
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((w) => words[w.toUpperCase()] || (w.length <= 2 ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1).toLowerCase()))
    .join(' ');
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

// ---- Plaud sign-in paste boxes ----
// They take the plaud://login?auth_code=… link the browser opens after
// "Continue to desktop", the bare auth code from it (contains "::", which may
// be URL-encoded as %3A%3A), or a bearer token (a JWT, "eyJ…", from
// web.plaud.ai's local storage; a "Bearer " prefix is fine).

/** { kind: 'code', code } | { kind: 'token', token } | { kind: 'invalid' } | { kind: 'empty' } */
function parsePlaudSignInInput(raw) {
  let text = String(raw || '').trim();
  text = text.replace(/^bearer(\s+|$)/i, '').trim();
  if (!text) return { kind: 'empty' };
  if (text.startsWith('eyJ')) return { kind: 'token', token: text };
  const decode = (s) => {
    try {
      return decodeURIComponent(s).trim();
    } catch {
      return '';
    }
  };
  if (/^plaud:/i.test(text)) {
    const m = text.match(/[?&]auth_code=([^&#]*)/);
    const code = m ? decode(m[1]) : '';
    return code ? { kind: 'code', code } : { kind: 'invalid' };
  }
  if (text.includes('::') || /%3A%3A/i.test(text)) {
    const code = decode(text);
    return code ? { kind: 'code', code } : { kind: 'invalid' };
  }
  return { kind: 'token', token: text };
}

/**
 * Sign in with what was pasted: an auth code is exchanged (with the desktop_uuid
 * from the last browser sign-in; the backend falls back to its own), a token is
 * checked. Returns the backend's reply, or null after a toast when nothing usable
 * was pasted. Throws on network errors.
 */
async function submitPlaudSignInInput(raw) {
  const parsed = parsePlaudSignInInput(raw);
  if (parsed.kind === 'code') return window.plaud.cloudExchangeToken(parsed.code, app.pendingDesktopUuid || '');
  if (parsed.kind === 'token') return window.plaud.cloudLoginToken(parsed.token, 'eu');
  toast(parsed.kind === 'invalid' ? 'That link has no sign-in code' : 'Paste the sign-in link, code or token', 'error');
  return null;
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

// Backend answers 409 {"busy": true} (or {connected:true, busy:true} from
// /device/verify) while a device sync holds the BLE link. That's not an error:
// callers should treat it as "already syncing" and stay quiet.
function isDeviceBusyResponse(res) {
  return !!(res && typeof res === 'object' && res.busy === true);
}

// Sync results may list files that only partly transferred. They're kept and
// resumed on the next sync, so tell the user without alarming them.
function toastPartialSync(result) {
  if (!result || typeof result !== 'object') return 0;
  let n = 0;
  if (Array.isArray(result.partial)) n = result.partial.length;
  else if (Array.isArray(result.files)) n = result.files.filter((f) => f && (f.status === 'partial' || f.partial === true)).length;
  if (n > 0) {
    toast(
      n === 1
        ? "1 recording didn't finish transferring — it will retry next sync"
        : `${n} recordings didn't finish transferring — they will retry next sync`,
      'info',
    );
  }
  return n;
}

// ---- Summary markdown ----
// A small renderer for the summaries the AI writes: headings, paragraphs,
// bullet / numbered / to-do lists (nested by indentation), GFM tables, quotes,
// rules and inline bold / italic / code. Every line is HTML-escaped first; the
// renderer only ever adds its own fixed tags, so model output can't inject HTML.

/** Inline formatting on text that is already escaped. */
function _mdInline(escaped) {
  const codes = [];
  let s = escaped.replace(/`([^`]+)`/g, (_, c) => {
    codes.push(c);
    return `\u0000${codes.length - 1}\u0000`;
  });
  s = s.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/__(.+?)__/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g, '$1<em>$2</em>');
  s = s.replace(/~~(.+?)~~/g, '<del>$1</del>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
}

/** Cells of a table row ("| a | b |" or "a | b"); "\|" stays a literal pipe. */
function _mdTableCells(line) {
  let t = line.trim();
  if (t.startsWith('|')) t = t.slice(1);
  if (t.endsWith('|') && !t.endsWith('\\|')) t = t.slice(0, -1);
  const cells = [];
  let cur = '';
  for (let k = 0; k < t.length; k++) {
    if (t[k] === '\\' && t[k + 1] === '|') {
      cur += '|';
      k++;
    } else if (t[k] === '|') {
      cells.push(cur.trim());
      cur = '';
    } else {
      cur += t[k];
    }
  }
  cells.push(cur.trim());
  return cells;
}

function _mdIsTableSeparator(line) {
  if (!line || !line.includes('-')) return false;
  const cells = _mdTableCells(line);
  return cells.length > 0 && cells.every((c) => /^:?-{1,}:?$/.test(c));
}

function renderMarkdown(md) {
  const lines = String(md || '').replace(/\r\n?/g, '\n').split('\n');
  const output = [];
  // Open lists, innermost last: { tag: 'ul' | 'ol', indent, liOpen }
  const lists = [];

  const closeItem = (list) => {
    if (list.liOpen) {
      output.push('</li>');
      list.liOpen = false;
    }
  };
  const closeLists = (depth = 0) => {
    while (lists.length > depth) {
      const list = lists.pop();
      closeItem(list);
      output.push(`</${list.tag}>`);
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];

    // GFM table: a header row followed by a |---|:---:| separator row
    if (raw.includes('|') && _mdIsTableSeparator(lines[i + 1])) {
      closeLists();
      const head = _mdTableCells(raw);
      const align = _mdTableCells(lines[i + 1]).map((c) => {
        if (c.startsWith(':') && c.endsWith(':')) return 'center';
        if (c.endsWith(':')) return 'right';
        return '';
      });
      const cell = (tag, text, col) => {
        const a = align[col] ? ` style="text-align:${align[col]}"` : '';
        return `<${tag}${a}>${_mdInline(escapeHtml(text))}</${tag}>`;
      };
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
        const cells = _mdTableCells(lines[i]);
        rows.push(`<tr>${head.map((_, c) => cell('td', cells[c] || '', c)).join('')}</tr>`);
        i++;
      }
      i--;
      output.push(
        `<div class="md-table-wrap"><table><thead><tr>${head.map((h, c) => cell('th', h, c)).join('')}</tr></thead>`
          + `<tbody>${rows.join('')}</tbody></table></div>`,
      );
      continue;
    }

    const line = escapeHtml(raw.replace(/\t/g, '    '));

    // List item: "-", "*" or "+" bullets, "1." / "1)" numbers, "- [ ]" to-dos
    const item = line.match(/^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/);
    if (item) {
      const indent = item[1].length;
      const tag = /\d/.test(item[2]) ? 'ol' : 'ul';
      while (lists.length && lists[lists.length - 1].indent > indent) closeLists(lists.length - 1);
      let top = lists[lists.length - 1];
      if (top && top.indent === indent && top.tag !== tag) {
        closeLists(lists.length - 1);
        top = lists[lists.length - 1];
      }
      if (!top || indent > top.indent) {
        const start = tag === 'ol' ? parseInt(item[2], 10) : 1;
        output.push(tag === 'ol' && start !== 1 ? `<ol start="${start}">` : `<${tag}>`);
        lists.push({ tag, indent, liOpen: false });
      } else {
        closeItem(top);
      }
      let content = item[3];
      let cls = '';
      const task = content.match(/^\[( |x|X)\]\s*(.*)$/);
      if (task) {
        cls = ' class="md-task"';
        content = `<input type="checkbox" disabled${task[1] === ' ' ? '' : ' checked'}> ${_mdInline(task[2])}`;
      } else {
        content = _mdInline(content);
      }
      output.push(`<li${cls}>${content}`);
      lists[lists.length - 1].liOpen = true;
      continue;
    }

    // Blank lines don't end a list ("1. a", "", "2. b" stays one list)
    if (!line.trim()) continue;

    // Indented text under a list item continues that item
    if (lists.length && /^\s+\S/.test(line)) {
      output.push(`<br>${_mdInline(line.trim())}`);
      continue;
    }

    closeLists();

    const heading = line.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const level = Math.min(heading[1].length, 3);
      output.push(`<h${level}>${_mdInline(heading[2])}</h${level}>`);
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      output.push('<hr>');
      continue;
    }
    const quote = line.match(/^&gt;\s?(.*)$/);
    if (quote) {
      output.push(`<blockquote>${_mdInline(quote[1])}</blockquote>`);
      continue;
    }
    output.push(`<p>${_mdInline(line)}</p>`);
  }

  closeLists();
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
