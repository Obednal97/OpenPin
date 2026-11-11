/* Settings › Transcription & AI › People: who Open Pin recognises by voice.
   Global: renderPeopleSettings() (called with renderVoiceSettings from renderSettings). */

const _pplPollMs = 2000;
const _pplMinNamed = 10; // fewer named speakers than this and the accuracy figure means little
let _pplData = null;
let _pplEditing = null; // { id, mode: 'rename' | 'merge' }
let _pplError = '';
let _pplTimer = null;
let _pplBound = false;

function _pplSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function _pplDate(iso) {
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
}

function _pplPlural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

function _pplVisible() {
  const el = $('#people-section');
  return !!el && el.offsetParent !== null;
}

function _pplCalibrationLine(cal) {
  if (!cal) return 'Open Pin will start naming speakers once it has learnt from your recordings.';
  if (cal.accuracy === null || cal.accuracy === undefined || !(cal.named >= _pplMinNamed)) return '';
  return `Tested on your recordings: ${Math.round(cal.accuracy * 100)}% of the speakers it names are right.`;
}

function _pplMeRow(me) {
  const name = me.name
    ? `<span class="ppl-name">${escapeHtml(me.name)}</span>`
    : '<a href="#" class="ppl-link" data-ppl="add-name">Add your name</a>';
  return `<div class="ppl-row ppl-me">
    <div class="ppl-main">${name}<span class="ppl-badge">You</span></div>
    <div class="ppl-meta">${me.samples} of 20 samples</div>
    <div class="ppl-actions"><a href="#" class="ppl-link" data-ppl="voice">Your voice &amp; coaching</a></div>
  </div>`;
}

function _pplPersonRow(p, people) {
  const id = escapeHtml(p.id);
  const last = p.last_heard ? ` · Last heard ${escapeHtml(_pplDate(p.last_heard))}` : '';
  const meta = `${_pplPlural(p.recordings || 0, 'recording', 'recordings')} · ${p.samples} of 20 samples · ${_pplSize(p.bytes)}${last}`;
  const editing = _pplEditing && _pplEditing.id === p.id ? _pplEditing.mode : '';
  let actions;
  if (editing === 'rename') {
    actions = `<input type="text" class="settings-input ppl-input" data-ppl-input aria-label="New name" value="${escapeHtml(_pplEditing.value !== undefined ? _pplEditing.value : p.name || '')}">
      <button class="action-btn primary settings-btn-compact" data-ppl="rename-save" data-id="${id}">Save</button>
      <button class="action-btn subtle settings-btn-compact" data-ppl="edit-cancel">Cancel</button>`;
  } else if (editing === 'merge') {
    const others = people.filter((o) => o.id !== p.id);
    const options = others.map((o) => `<option value="${escapeHtml(o.id)}"${_pplEditing.value === o.id ? ' selected' : ''}>${escapeHtml(o.name || 'Unnamed')}</option>`).join('');
    actions = `<select class="settings-select ppl-select" data-ppl-input aria-label="Merge into">${options}</select>
      <button class="action-btn primary settings-btn-compact" data-ppl="merge-go" data-id="${id}">Merge</button>
      <button class="action-btn subtle settings-btn-compact" data-ppl="edit-cancel">Cancel</button>`;
  } else {
    actions = `<button type="button" class="ppl-link" data-ppl="rename" data-id="${id}">Rename</button>
      <button type="button" class="ppl-link" data-ppl="merge" data-id="${id}"${people.length < 2 ? ' disabled' : ''}>Merge into…</button>
      <button type="button" class="ppl-link ppl-danger" data-ppl="delete" data-id="${id}">Delete</button>`;
  }
  const error = editing && _pplError ? `<p class="voice-error ppl-row-error" role="alert">${escapeHtml(_pplError)}</p>` : '';
  return `<div class="ppl-row${editing ? ' ppl-editing' : ''}">
    <div class="ppl-main"><span class="ppl-name">${escapeHtml(p.name || 'Unnamed')}</span></div>
    <div class="ppl-meta">${meta}</div>
    <div class="ppl-actions">${actions}</div>${error}
  </div>`;
}

function _pplBacklog(d) {
  const b = d.backlog || {};
  if (b.running) {
    let text = 'Testing on your recordings…';
    if (b.phase === 'learning') text = `Learning voices: ${b.done} of ${b.total} recordings`;
    else if (b.phase === 'naming') text = `Naming past recordings: ${b.done} of ${b.total}`;
    return `<div class="ppl-backlog"><span class="ppl-progress" role="status">${text}</span>
      <button class="action-btn subtle settings-btn-compact" data-ppl="backlog-cancel">Cancel</button></div>`;
  }
  return `<div class="ppl-backlog"><button class="action-btn subtle settings-btn-compact" data-ppl="backlog-start">Learn from past recordings</button></div>`;
}

function _pplRender() {
  const body = $('#people-body');
  const d = _pplData;
  if (!body || !d) return;
  const n = d.people.length;
  let html = `<p class="settings-desc">Open Pin recognises ${_pplPlural(n, 'person', 'people')} by voice. Voice data stays on this Mac (about ${_pplSize(d.total_bytes)}).</p>`;
  const cal = _pplCalibrationLine(d.calibration);
  if (cal) html += `<p class="settings-desc">${escapeHtml(cal)}</p>`;
  if (_pplError && !_pplEditing) html += `<p class="voice-error" role="alert">${escapeHtml(_pplError)}</p>`;
  if (!d.model) {
    const installing = _pplInstalling();
    html += `<div class="ppl-backlog"><span class="ppl-progress">Install the voice model to recognise people.</span>
      <button class="action-btn primary settings-btn-compact" data-ppl="install"${installing ? ' disabled' : ''}>${installing ? 'Installing…' : 'Install voice model'}</button></div>`;
  }
  html += `<div class="ppl-list">${_pplMeRow(d.me)}${d.people.map((p) => _pplPersonRow(p, d.people)).join('')}</div>`;
  if (d.model) html += _pplBacklog(d);
  body.innerHTML = html;
  const input = body.querySelector('[data-ppl-input]');
  if (input) {
    input.focus();
    if (input.select) input.select();
  }
}

// The voice section's install flow sets _voiceInstallAskedAt; poll briefly until the model arrives.
function _pplInstalling() {
  return !!_pplData && !_pplData.model && typeof _voiceInstallAskedAt === 'number'
    && Date.now() - _voiceInstallAskedAt < 15 * 60 * 1000 && _voiceInstallAskedAt > 0;
}

function _pplSchedule() {
  if (_pplTimer) return;
  const d = _pplData;
  if (!d || !(d.backlog && d.backlog.running) && !_pplInstalling()) return;
  _pplTimer = setTimeout(() => {
    _pplTimer = null;
    if (!_pplVisible()) return; // not shown: stop; opening Settings or the tab renders again
    renderPeopleSettings(true);
  }, _pplPollMs);
}

async function renderPeopleSettings(polling) {
  if (!$('#people-body')) return;
  if (!_pplBound) {
    _pplBound = true;
    $('#people-body').addEventListener('click', _pplClick);
    $('#people-body').addEventListener('keydown', _pplKey);
    document.querySelectorAll('.settings-tab').forEach((t) => t.addEventListener('click', () => {
      if (t.dataset.tab === 'ai') setTimeout(() => _pplVisible() && renderPeopleSettings(), 0);
    }));
  }
  let d;
  try {
    d = await window.plaud.peopleDirectory();
  } catch (err) {
    d = { error: (err && err.message) || String(err) };
  }
  if (!d || d.error) {
    if (!_pplData) {
      $('#people-body').innerHTML = `<p class="voice-error">Couldn’t load people: ${escapeHtml((d && d.error) || 'no reply')}</p>`;
    } else {
      _pplSchedule(); // keep the last data; a running backlog keeps being polled
    }
    return;
  }
  _pplData = d;
  if (!(polling && _pplEditing)) _pplRender(); // a poll never redraws over an open editor
  _pplSchedule();
}

function _pplFail(res) {
  _pplError = res && res.error ? res.error : '';
  return !!_pplError;
}

async function _pplCall(fn, keepEditor) {
  let res;
  try {
    res = await fn();
  } catch (err) {
    res = { error: (err && err.message) || String(err) };
  }
  if (!(_pplFail(res) && keepEditor && _pplEditing)) _pplEditing = null;
  await renderPeopleSettings();
  return res;
}

function _pplById(id) {
  return _pplData && (_pplData.people.find((p) => p.id === id) || null);
}

async function _pplConfirmDelete(id) {
  const p = _pplById(id);
  if (!p) return;
  const name = p.name || 'Unnamed';
  const choice = await showConfirmDialog({
    title: `Delete ${name}?`,
    message: `Delete ${name}’s voice? Open Pin will stop recognising them. Names in past transcripts stay.`,
    actions: [{ id: 'delete', label: 'Delete', destructive: true }],
    cancelLabel: 'Cancel',
  });
  if (choice === 'delete') await _pplCall(() => window.plaud.deletePerson(id));
}

async function _pplConfirmMerge(id, into) {
  const src = _pplById(id);
  const dst = _pplById(into);
  if (!src || !dst) return;
  const source = src.name || 'Unnamed';
  const target = dst.name || 'Unnamed';
  const choice = await showConfirmDialog({
    title: `Merge ${source} into ${target}?`,
    message: `Merge ${source} into ${target}? Their voice samples are combined and ${source} is renamed to ${target} in every transcript.`,
    actions: [{ id: 'merge', label: 'Merge' }],
    cancelLabel: 'Cancel',
  });
  if (choice === 'merge') await _pplCall(() => window.plaud.mergePerson(id, into), true);
}

function _pplFocusSoon(selector) {
  const el = $(selector);
  if (!el) return;
  el.scrollIntoView({ block: 'center' });
  if (el.focus) el.focus();
}

function _pplAddName() {
  const input = $('#profile-name');
  if (!input) return;
  const tab = input.closest('.settings-tab-content');
  if (tab && !tab.classList.contains('active') && tab.dataset.tab) showSettingsTab(tab.dataset.tab);
  _pplFocusSoon('#profile-name');
}

function _pplClick(e) {
  const el = e.target.closest('[data-ppl]');
  if (!el) return;
  e.preventDefault();
  const id = el.dataset.id;
  const root = $('#people-body');
  const input = root.querySelector('[data-ppl-input]');
  switch (el.dataset.ppl) {
    case 'add-name': return _pplAddName();
    case 'voice': return _pplFocusSoon('#voice-coaching-section');
    case 'rename': _pplEditing = { id, mode: 'rename' }; _pplError = ''; return _pplRender();
    case 'merge': _pplEditing = { id, mode: 'merge' }; _pplError = ''; return _pplRender();
    case 'edit-cancel': _pplEditing = null; return _pplRender();
    case 'rename-save':
      _pplEditing.value = input.value;
      return _pplCall(() => window.plaud.renamePerson(id, input.value.trim()), true);
    case 'merge-go':
      if (_pplEditing) _pplEditing.value = input && input.value;
      return input && input.value ? _pplConfirmMerge(id, input.value) : undefined;
    case 'delete': _pplError = ''; return _pplConfirmDelete(id);
    case 'backlog-start': return _pplCall(() => window.plaud.startPeopleBacklog());
    case 'backlog-cancel': return _pplCall(() => window.plaud.cancelPeopleBacklog());
    case 'install': return _voiceStartInstall(el).then(() => renderPeopleSettings());
  }
}

function _pplKey(e) {
  if (!e.target.matches || !e.target.matches('[data-ppl-input]')) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    e.stopPropagation();
    _pplEditing = null;
    _pplRender();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const save = $('#people-body [data-ppl="rename-save"]');
    if (save) save.click();
  }
}
