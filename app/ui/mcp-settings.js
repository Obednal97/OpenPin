/* ============================================================
   Open Pin: Settings › General › AI tools (MCP), and the library change poll
   ============================================================ */

// The switch ("Let AI tools on this Mac use Open Pin") saves mcp_enabled; the
// backend makes or drops the MCP key (app/mcp/access.py). Turning it off never
// edits Claude's files: their entry stays and the connector says access is off.
// Connect / Disconnect edit Claude's settings files through /mcp/setup/*
// (app/mcp/clients.py), which backs each file up first.

// Claude Code files the user ticked (shown paths). null until the first status,
// so a single file found can start ticked.
let _mcpSetCodeTicks = null;
let _mcpSetStatus = null;
let _mcpSetStatusSeq = 0;
let _mcpSetBusy = false;

function _mcpSetEnabled() {
  return !!(app.settings && app.settings.mcp_enabled === true);
}

function _mcpSetBadge(entry) {
  if (entry.connected && entry.stale) return '<span class="ai-key-badge none">Needs updating</span>';
  if (entry.connected) return '<span class="ai-key-badge connected">Connected</span>';
  return '<span class="ai-key-badge none">Not connected</span>';
}

function _mcpSetDisconnectLink(kind, path) {
  return `<button class="link muted" data-mcp-disconnect="${kind}" data-mcp-path="${escapeHtml(path)}">Disconnect</button>`;
}

const _MCP_SET_STALE_HINT = 'It points at another copy of Open Pin. Connect again to use this one.';

/** Claude Desktop's row: its settings file, whether it's connected, and Disconnect. */
function _mcpSetDesktopHtml(desktop) {
  if (!desktop) return '';
  let hint = '';
  if (desktop.stale) hint = _MCP_SET_STALE_HINT;
  else if (!desktop.exists) hint = 'Claude Desktop&rsquo;s settings file wasn&rsquo;t found. Connecting creates it.';
  return (
    `<div class="mcp-client-row">` +
    `<span class="mcp-client-path" title="${escapeHtml(desktop.path)}">${escapeHtml(desktop.path)}</span>` +
    `<span class="mcp-client-state">${_mcpSetBadge(desktop)}` +
    `${desktop.connected ? _mcpSetDisconnectLink('desktop', desktop.path) : ''}</span>` +
    `</div>` +
    (hint ? `<p class="settings-hint">${hint}</p>` : '')
  );
}

/** Ticks kept from before, for files that are still listed and not yet connected. */
function _mcpSetSyncTicks(files) {
  const open = files.filter((f) => !f.error && (!f.connected || f.stale)).map((f) => f.path);
  if (_mcpSetCodeTicks === null) {
    _mcpSetCodeTicks = new Set(files.length === 1 ? open : []);
  } else {
    for (const p of [..._mcpSetCodeTicks]) if (!open.includes(p)) _mcpSetCodeTicks.delete(p);
  }
}

/** Claude Code's settings files: a tick box for each one not yet connected. */
function _mcpSetCodeHtml(files, enabled) {
  if (!files || files.length === 0) {
    return '<p class="settings-hint">No Claude Code settings were found in your home folder. Open Claude Code once, then come back.</p>';
  }
  return files
    .map((f) => {
      const path = escapeHtml(f.path);
      const canTick = !f.error && (!f.connected || f.stale);
      const tick = canTick
        ? `<input type="checkbox" class="mcp-code-tick" data-mcp-path="${path}"` +
          `${_mcpSetCodeTicks && _mcpSetCodeTicks.has(f.path) ? ' checked' : ''}${enabled ? '' : ' disabled'}>`
        : '';
      const state = f.error
        ? ''
        : `<span class="mcp-client-state">${_mcpSetBadge(f)}${f.connected ? _mcpSetDisconnectLink('code', f.path) : ''}</span>`;
      const note = f.error
        ? `<p class="ai-key-status error">${escapeHtml(f.error)}</p>`
        : f.stale
          ? `<p class="settings-hint">${_MCP_SET_STALE_HINT}</p>`
          : '';
      return (
        `<div class="mcp-client-row${tick ? ' mcp-tickable' : ''}"><label class="mcp-code-label">${tick}` +
        `<span class="mcp-client-path" title="${path}">${path}</span></label>${state}</div>${note}`
      );
    })
    .join('');
}

function _mcpSetRenderClients() {
  const enabled = _mcpSetEnabled();
  const status = _mcpSetStatus;
  const desktopEl = document.getElementById('mcp-desktop-status');
  const codeEl = document.getElementById('mcp-code-list');
  const desktopBtn = document.getElementById('mcp-desktop-connect-btn');
  const codeBtn = document.getElementById('mcp-code-connect-btn');
  if (!status) {
    if (desktopBtn) desktopBtn.disabled = true;
    if (codeBtn) codeBtn.disabled = true;
    return;
  }
  const files = status.code || [];
  _mcpSetSyncTicks(files);
  if (desktopEl) desktopEl.innerHTML = _mcpSetDesktopHtml(status.desktop);
  if (codeEl) codeEl.innerHTML = _mcpSetCodeHtml(files, enabled);
  const desktopDone = !!(status.desktop && status.desktop.connected && !status.desktop.stale);
  if (desktopBtn) desktopBtn.disabled = !enabled || _mcpSetBusy || desktopDone;
  if (codeBtn) codeBtn.disabled = !enabled || _mcpSetBusy || _mcpSetCodeTicks.size === 0;
}

async function _mcpSetRefreshStatus() {
  const seq = ++_mcpSetStatusSeq;
  try {
    const status = await window.plaud.mcpSetupStatus();
    if (seq !== _mcpSetStatusSeq || !status || status.error) return;
    _mcpSetStatus = status;
  } catch (err) {
    console.warn('[mcp] setup status failed:', err);
  }
  _mcpSetRenderClients();
}

/** Called from renderSettings(): the switch, the error line, then the clients. */
async function renderMcpSettings() {
  const enabled = _mcpSetEnabled();
  const toggle = document.getElementById('mcp-enabled-toggle');
  if (toggle) toggle.checked = enabled;
  const error = document.getElementById('mcp-error');
  if (error) error.classList.toggle('hidden', !(enabled && app.settings.mcp_active === false));
  _mcpSetRenderClients();
  await _mcpSetRefreshStatus();
}

async function _mcpSetToggle(on) {
  try {
    const res = await window.plaud.saveSettings({ mcp_enabled: on });
    if (res && res.error) {
      toast(res.error, 'error');
    } else {
      app.settings.mcp_enabled = on;
      toast(on ? 'AI tools on this Mac can now use Open Pin' : 'AI tool access is off', 'success');
    }
  } catch (err) {
    toast(`Couldn't change AI tool access: ${err?.message || err}`, 'error');
  }
  // The backend says what actually happened (turning on can fail).
  try {
    const s = await window.plaud.getSettings();
    app.settings.mcp_enabled = s.mcp_enabled;
    app.settings.mcp_active = s.mcp_active;
  } catch {
    // Keep what we had; the next settings load corrects it.
  }
  await renderMcpSettings();
}

/** Connect or disconnect one file. Returns true when it worked. */
async function _mcpSetChange(action, kind, path) {
  const call = action === 'connect' ? window.plaud.mcpConnect : window.plaud.mcpDisconnect;
  try {
    const res = await call(kind, path);
    if (!res || res.error) {
      toast((res && res.error) || 'Something went wrong', 'error');
      return false;
    }
    _mcpSetStatus = res;
    return true;
  } catch (err) {
    toast(`Couldn't change ${path}: ${err?.message || err}`, 'error');
    return false;
  }
}

async function _mcpSetRun(work) {
  if (_mcpSetBusy) return;
  _mcpSetBusy = true;
  _mcpSetRenderClients();
  try {
    await work();
  } finally {
    _mcpSetBusy = false;
    _mcpSetRenderClients();
  }
}

function _mcpSetShowHint(id) {
  const hint = document.getElementById(id);
  if (hint) hint.classList.remove('hidden');
}

/** Binds the section's controls once, when this script loads (the markup is already parsed). */
function initMcpSettingsEvents() {
  const toggle = document.getElementById('mcp-enabled-toggle');
  if (toggle) toggle.addEventListener('change', (e) => _mcpSetToggle(e.target.checked));

  const desktopBtn = document.getElementById('mcp-desktop-connect-btn');
  if (desktopBtn) {
    desktopBtn.addEventListener('click', () =>
      _mcpSetRun(async () => {
        const desktop = _mcpSetStatus && _mcpSetStatus.desktop;
        if (!desktop) return;
        if (await _mcpSetChange('connect', 'desktop', desktop.path)) {
          _mcpSetShowHint('mcp-desktop-hint');
          toast('Connected to Claude Desktop', 'success');
        }
      }),
    );
  }

  const codeBtn = document.getElementById('mcp-code-connect-btn');
  if (codeBtn) {
    codeBtn.addEventListener('click', () =>
      _mcpSetRun(async () => {
        let done = 0;
        for (const path of [...(_mcpSetCodeTicks || [])]) {
          if (await _mcpSetChange('connect', 'code', path)) done += 1;
        }
        if (done > 0) {
          _mcpSetShowHint('mcp-code-hint');
          toast('Connected to Claude Code', 'success');
        }
      }),
    );
  }

  const codeList = document.getElementById('mcp-code-list');
  if (codeList) {
    codeList.addEventListener('change', (e) => {
      const box = e.target;
      if (!box || !box.dataset || !box.dataset.mcpPath || _mcpSetCodeTicks === null) return;
      if (box.checked) _mcpSetCodeTicks.add(box.dataset.mcpPath);
      else _mcpSetCodeTicks.delete(box.dataset.mcpPath);
      _mcpSetRenderClients();
    });
  }

  // Disconnect links (Desktop row and each Claude Code row)
  const section = document.getElementById('mcp-section');
  if (section) {
    section.addEventListener('click', (e) => {
      const link = e.target && e.target.closest ? e.target.closest('[data-mcp-disconnect]') : null;
      if (!link) return;
      const kind = link.dataset.mcpDisconnect;
      const path = link.dataset.mcpPath;
      _mcpSetRun(async () => {
        if (await _mcpSetChange('disconnect', kind, path)) {
          toast(kind === 'desktop' ? 'Disconnected from Claude Desktop' : 'Disconnected from Claude Code', 'success');
        }
      });
    });
  }
}

initMcpSettingsEvents();

// ---- Library change poll ----
// AI tools can name speakers and change tags while the window is open. The
// backend counts those changes (GET /changes); while the window is focused we
// read it every 5 s, and when it moves, reload the recordings and transcripts.
// The counter also moves for the window's own edits, so the open recording is
// drawn again only when its own data changed in the reload, and never while
// something in it is being edited (that waits for a later tick). Drawing it
// again reloads the audio, so the player's place and play state are put back.

const _MCP_CHANGES_POLL_MS = 5000;
let _mcpChangesTimer = null;
let _mcpChangesLast = null;
let _mcpChangesBusy = false;
let _mcpChangesPending = false;

function startMcpChangePoll() {
  if (_mcpChangesTimer) return;
  _mcpChangesTimer = setInterval(_mcpChangesTick, _MCP_CHANGES_POLL_MS);
}

/** What the open recording shows that an AI tool can change: transcript, summary, names and tags. */
function _mcpChangesSnapshot(sid) {
  const t = (app.transcripts && app.transcripts[sid]) || {};
  const rec = (app.recordings || []).find((r) => r.session_id === sid) || {};
  return JSON.stringify([t.segments || null, t.text || '', t.summary || '', t.speaker_names || null, rec.tags || []]);
}

/** Something in the open recording is being edited, so drawing it again would lose work. */
function _mcpChangesEditing() {
  // The speaker-name editor (ui/transcript.js)
  if (typeof speakerNameEditorOpen === 'function' && speakerNameEditorOpen()) return true;
  const view = document.getElementById('transcript-view');
  if (!view) return false;
  // Summary editor, the tag picker's new-tag field, or any text field in the
  // transcript panel.
  const field = '#transcript-content input:not([type="checkbox"]):not([type="radio"]), #transcript-content textarea';
  if (view.querySelector(`.summary-editor, #tag-add-input, ${field}`)) return true;
  // Tick boxes (coaching's "Which one is you?") only while changed and not yet saved.
  const boxes = view.querySelectorAll('#transcript-content input[type="checkbox"], #transcript-content input[type="radio"]');
  if ([...(boxes || [])].some((b) => b.checked !== b.defaultChecked)) return true;
  const active = document.activeElement;
  if (active && view.contains(active) && (active.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName || ''))) {
    return true;
  }
  // The crop editor (ui/transcript.js)
  if (typeof cropState !== 'undefined' && cropState) return true;
  return false;
}

/** Put the player back where it was after the redraw reloaded its source. */
function _mcpChangesRestoreAudio(audio, time, wasPlaying) {
  if (!audio || !audio.getAttribute || !audio.getAttribute('src')) return; // no audio to put back
  const restore = () => {
    try {
      if (time > 0) audio.currentTime = time;
    } catch {}
    if (wasPlaying) audio.play().catch(() => {});
  };
  // Seeking needs the new source's metadata.
  if (audio.readyState >= 1) restore();
  else audio.addEventListener('loadedmetadata', restore, { once: true });
}

function _mcpChangesRedraw() {
  if (app.currentView !== 'transcript' || !app.currentSessionId) {
    _mcpChangesPending = false;
    return;
  }
  if (_mcpChangesEditing()) return; // try again next tick
  _mcpChangesPending = false;
  const sid = app.currentSessionId;
  const tab = app.activeTab;
  const main = document.getElementById('main-content');
  const scroll = main ? main.scrollTop : 0;
  const audio = typeof dom !== 'undefined' && dom ? dom.audioEl : null;
  const time = audio ? audio.currentTime || 0 : 0;
  const wasPlaying = !!(audio && !audio.paused);
  if (typeof redrawOpenTranscript === 'function') redrawOpenTranscript(sid);
  else openTranscript(sid);
  if (app.currentSessionId !== sid) return;
  // openTranscript() picks the Summary tab; stay where the user was.
  if (tab && tab !== app.activeTab && typeof showTranscriptTab === 'function') {
    const rec = app.recordings.find((r) => r.session_id === sid);
    if (rec) showTranscriptTab(tab, sid, rec, app.transcripts[sid]);
  }
  if (main) main.scrollTop = scroll;
  _mcpChangesRestoreAudio(audio, time, wasPlaying);
}

async function _mcpChangesTick() {
  if (_mcpChangesBusy || document.hidden || !document.hasFocus()) return;
  _mcpChangesBusy = true;
  try {
    const res = await window.plaud.libraryChanges();
    const counter = res && typeof res.counter === 'number' ? res.counter : null;
    if (counter === null) return;
    if (_mcpChangesLast === null) {
      _mcpChangesLast = counter; // first read: nothing to catch up on
    } else if (counter !== _mcpChangesLast) {
      // Any difference, including a restarted backend counting from 0 again.
      _mcpChangesLast = counter;
      const sid = app.currentView === 'transcript' ? app.currentSessionId : null;
      const before = sid ? _mcpChangesSnapshot(sid) : null;
      await loadRecordings();
      if (sid && app.currentSessionId === sid && _mcpChangesSnapshot(sid) !== before) _mcpChangesPending = true;
    }
    if (_mcpChangesPending) _mcpChangesRedraw();
  } catch (err) {
    console.warn('[changes] poll failed:', err);
  } finally {
    _mcpChangesBusy = false;
  }
}
