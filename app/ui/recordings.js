/* ============================================================
   Open Pin — Recordings List Module
   ============================================================ */

async function loadRecordings() {
  try {
    const [recs, trans] = await Promise.all([window.plaud.recordings(), window.plaud.transcripts()]);
    app.recordings = recs || [];
    app.transcripts = {};
    (trans || []).forEach((t) => {
      app.transcripts[t.session_id] = t;
    });
    renderRecordingsList();
  } catch (err) {
    console.error('Failed to load recordings:', err);
  }
}

function getFilteredRecordings() {
  let filtered = app.recordings;

  if (app.activeTagFilters.size > 0) {
    filtered = filtered.filter((rec) => {
      const tags = rec.tags || [];
      return [...app.activeTagFilters].some((f) => tags.includes(f));
    });
  }

  if (app.searchQuery) {
    const q = app.searchQuery.toLowerCase();
    filtered = filtered.filter((rec) => {
      const t = app.transcripts[rec.session_id];
      const title = (rec.title || '').toLowerCase();
      const date = formatDate(rec.date).toLowerCase();
      const text = t ? (t.text || '').toLowerCase() : '';
      return title.includes(q) || date.includes(q) || text.includes(q) || rec.session_id.toLowerCase().includes(q);
    });
  }

  return filtered;
}

// Session ids that select mode can act on: visible under the current
// search/tag filter, excluding incomplete transfers (which can't be selected).
function _selectableVisibleIds() {
  return getFilteredRecordings()
    .filter((rec) => !rec.partial)
    .map((rec) => rec.session_id);
}

// Drop selected ids that the current filter hides (or that no longer exist),
// so bulk actions only ever touch recordings the user can see.
function pruneSelectionToVisible() {
  if (app.selectedIds.size === 0) return;
  const visible = new Set(_selectableVisibleIds());
  for (const sid of [...app.selectedIds]) {
    if (!visible.has(sid)) app.selectedIds.delete(sid);
  }
}

// Select-all toggles only the recordings currently visible.
function toggleSelectAllVisible() {
  const visible = _selectableVisibleIds();
  const allSelected = visible.length > 0 && visible.every((sid) => app.selectedIds.has(sid));
  if (allSelected) {
    visible.forEach((sid) => app.selectedIds.delete(sid));
  } else {
    visible.forEach((sid) => app.selectedIds.add(sid));
  }
  renderRecordingsList();
}

function renderRecordingsList() {
  const filtered = getFilteredRecordings();
  pruneSelectionToVisible();
  if (app.recordings.length === 0) {
    dom.emptyState.classList.remove('hidden');
    // Context-aware empty state message
    const h2 = dom.emptyState.querySelector('h2');
    const p = dom.emptyState.querySelector('p');
    if (app.deviceStateCache && app.deviceStateCache.connected) {
      h2.textContent = 'No recordings yet';
      p.textContent = 'Your device is connected. Sync recordings or start a new recording from the device.';
    } else {
      h2.textContent = 'No recordings yet';
      p.textContent = 'Connect your Plaud NotePin S to sync recordings.';
    }
    dom.recordingsList.innerHTML = '';
    updateSelectBar();
    return;
  }
  dom.emptyState.classList.add('hidden');

  if (filtered.length === 0 && app.searchQuery) {
    dom.recordingsList.innerHTML = `<div style="text-align:center;padding:40px;color:var(--text-muted);">
      No recordings match "${escapeHtml(app.searchQuery)}"
    </div>`;
    updateSelectBar();
    return;
  }

  dom.recordingsList.innerHTML = filtered
    .map((rec) => {
      const t = app.transcripts[rec.session_id];
      const hasTranscript = rec.has_transcript && t;
      const date = formatDate(rec.date);
      // Cloud-only recordings have no duration until they're downloaded
      const dur = rec.duration ? formatDuration(rec.duration) : '';
      const preview = hasTranscript ? t.text_preview : '';
      const speakerCount =
        hasTranscript && t.speakers && t.speakers.length > 0
          ? `${t.speakers.length} speaker${t.speakers.length > 1 ? 's' : ''}`
          : '';

      let badgeClass = 'badge-new';
      let badgeText = 'New';
      const processing = app.processingIds.get(rec.session_id);
      if (rec.partial) {
        badgeClass = 'badge-partial';
        badgeText = 'Incomplete';
      } else if (rec.redownload_pending) {
        badgeClass = 'badge-partial';
        badgeText = 'Waiting for re-download';
      } else if (processing) {
        badgeClass = 'badge-processing';
        badgeText = { transcribing: 'Transcribing', summarizing: 'Summarising', queued: 'Queued' }[processing] || 'Processing';
      } else if (hasTranscript) {
        badgeClass = 'badge-transcribed';
        badgeText = 'Transcribed';
      } else if (rec.cloud_only) {
        badgeClass = 'badge-cloud';
        badgeText = 'Cloud';
      }

      const isSelected = app.selectedIds.has(rec.session_id);

      return `
      <div class="recording-card ${app.selectMode ? 'select-mode' : ''} ${isSelected ? 'selected' : ''} ${rec.partial ? 'recording-partial' : ''}" data-sid="${escapeHtml(rec.session_id)}">
        ${
          rec.partial
            ? `
          <div class="recording-partial-icon" title="${rec.redownload_pending ? 'Re-download interrupted — sync again, or right-click to restore the previous copy' : 'Incomplete transfer — sync again to complete'}">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
              <path d="M8 1a7 7 0 100 14A7 7 0 008 1zm0 10.5a.75.75 0 110-1.5.75.75 0 010 1.5zM8.75 4v4.5h-1.5V4h1.5z"/>
            </svg>
          </div>
        `
            : app.selectMode
              ? `
          <label class="select-checkbox" data-sid="${escapeHtml(rec.session_id)}">
            <input type="checkbox" ${isSelected ? 'checked' : ''}>
            <span class="checkbox-mark"></span>
          </label>
        `
              : `
          <button class="recording-play-btn" data-sid="${escapeHtml(rec.session_id)}" title="Play">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
              <path d="M4 2l10 6-10 6z"/>
            </svg>
          </button>
        `
        }
        <div class="recording-info">
          ${rec.title ? `<div class="recording-title">${rec.cloud_synced ? '<svg class="cloud-icon" width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4.5 13h7a3.5 3.5 0 00.5-6.97A5 5 0 002.1 8.26 3 3 0 004.5 13z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg> ' : ''}${escapeHtml(rec.title)}</div>` : ''}
          <div class="recording-date">${!rec.title && rec.cloud_synced ? '<svg class="cloud-icon" width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4.5 13h7a3.5 3.5 0 00.5-6.97A5 5 0 002.1 8.26 3 3 0 004.5 13z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg> ' : ''}${escapeHtml(date)}</div>
          <div class="recording-duration">${rec.partial ? (rec.redownload_pending ? 'Sync again to finish, or right-click to restore the previous copy' : 'Sync again to complete transfer') : [dur, speakerCount].filter(Boolean).join(' \u00B7 ')}</div>
          ${preview ? `<div class="recording-preview">${escapeHtml(preview)}</div>` : ''}
        </div>
        <div class="recording-right">
          ${_recordingTagChips(rec.tags)}
          <span class="recording-badge ${badgeClass}">${badgeText}</span>
        </div>
      </div>
    `;
    })
    .join('');

  // Bind click handlers
  dom.recordingsList.querySelectorAll('.recording-card').forEach((card) => {
    // Shift-click extends the selection: stop the browser selecting the text
    // between the two clicks instead.
    card.addEventListener('mousedown', (e) => {
      if (app.selectMode && e.shiftKey) e.preventDefault();
    });
    card.addEventListener('click', (e) => {
      if (card.classList.contains('recording-partial')) return;
      if (e.target.closest('.recording-play-btn')) return;
      const sid = card.dataset.sid;
      if (e.target.closest('.select-checkbox') || app.selectMode) {
        if (e.shiftKey && selectRangeTo(sid)) return;
        toggleSelection(sid);
        return;
      }
      openTranscript(sid);
    });

    // Right-click context menu
    card.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      e.stopPropagation();
      _showContextMenu(e.clientX, e.clientY, card.dataset.sid);
    });
  });

  dom.recordingsList.querySelectorAll('.recording-play-btn').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openTranscript(btn.dataset.sid, true);
    });
  });

  dom.recordingsList.querySelectorAll('.select-checkbox input').forEach((cb) => {
    cb.addEventListener('click', (e) => e.stopPropagation());
    cb.addEventListener('change', (e) => e.stopPropagation());
  });

  updateSelectBar();
}

// A card shows at most two tags (each cut short with an ellipsis if long) and
// a "+N" chip for the rest, so a recording with many tags keeps its title.
const _CARD_TAG_LIMIT = 2;

function _recordingTagChips(tags) {
  if (!tags || !tags.length) return '';
  const shown = tags.slice(0, _CARD_TAG_LIMIT);
  const rest = tags.slice(_CARD_TAG_LIMIT);
  const chips = shown.map((t) => `<span class="tag" title="${escapeHtml(t)}">${escapeHtml(t)}</span>`);
  if (rest.length) {
    chips.push(`<span class="tag tag-more" title="${escapeHtml(tags.join(', '))}">+${rest.length}</span>`);
  }
  return `<div class="tag-list">${chips.join('')}</div>`;
}

// The recording a Shift-click range starts from: the last one clicked without
// Shift (as in Finder).
let _selectAnchorSid = null;

function toggleSelection(sid) {
  if (app.selectedIds.has(sid)) {
    app.selectedIds.delete(sid);
  } else {
    app.selectedIds.add(sid);
  }
  _selectAnchorSid = sid;
  renderRecordingsList();
}

/**
 * Shift-click: select every visible, selectable recording from the anchor to
 * `sid`, both included, in list order. Returns false (so the caller toggles
 * instead) when there's no anchor in the current list.
 */
function selectRangeTo(sid) {
  const ids = _selectableVisibleIds();
  const from = ids.indexOf(_selectAnchorSid);
  const to = ids.indexOf(sid);
  if (from === -1 || to === -1) return false;
  const [lo, hi] = from < to ? [from, to] : [to, from];
  for (const id of ids.slice(lo, hi + 1)) app.selectedIds.add(id);
  window.getSelection()?.removeAllRanges();
  renderRecordingsList();
  return true;
}

function enterSelectMode() {
  app.selectMode = true;
  app.selectedIds.clear();
  _selectAnchorSid = null;
  renderRecordingsList();
}

function exitSelectMode() {
  app.selectMode = false;
  app.selectedIds.clear();
  _selectAnchorSid = null;
  renderRecordingsList();
}

function updateSelectBar() {
  const selectBar = $('#select-bar');
  const selectActions = $('#select-actions');

  if (app.recordings.length === 0 || app.currentView === 'transcript') {
    selectBar.classList.add('hidden');
    return;
  }

  selectBar.classList.remove('hidden');

  if (app.selectMode && app.selectedIds.size > 0) {
    selectActions.classList.remove('hidden');
    $('#select-count').textContent = `${app.selectedIds.size}`;
  } else {
    selectActions.classList.add('hidden');
  }

  const usedTags = new Set();
  app.recordings.forEach((r) => (r.tags || []).forEach((t) => usedTags.add(t)));
  const allTags = new Set([...usedTags, ...(app.settings.available_tags || [])]);

  const tagFilters = $('#tag-filters');
  const tagsToShow = app.selectMode && app.selectedIds.size > 0 ? allTags : usedTags;

  if (tagsToShow.size > 0) {
    // In select mode with a selection, the chips tag the selected recordings
    // instead of filtering the list: the label says which.
    const applying = app.selectMode && app.selectedIds.size > 0;
    const label = applying ? 'Tag selected:' : 'Filter:';
    tagFilters.innerHTML = `<span class="tag-filters-label">${label}</span>` + [...tagsToShow]
      .sort()
      .map(
        (tag) =>
          `<button class="tag-filter ${app.activeTagFilters.has(tag) ? 'active' : ''}" data-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>`,
      )
      .join('');
    tagFilters.classList.toggle('applying', applying);

    tagFilters.querySelectorAll('.tag-filter').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const tag = btn.dataset.tag;

        if (app.selectMode && app.selectedIds.size > 0) {
          // Only tag recordings that are both selected and visible right now.
          const visibleIds = new Set(_selectableVisibleIds());
          const ids = [...app.selectedIds].filter((sid) => visibleIds.has(sid));
          if (ids.length === 0) return;
          const allHaveTag = ids.every((sid) => {
            const rec = app.recordings.find((r) => r.session_id === sid);
            return rec && (rec.tags || []).includes(tag);
          });

          const failures = [];
          for (const sid of ids) {
            const rec = app.recordings.find((r) => r.session_id === sid);
            if (!rec) continue;
            const tags = rec.tags || [];
            if (allHaveTag) {
              rec.tags = tags.filter((t) => t !== tag);
            } else {
              if (!tags.includes(tag)) {
                tags.push(tag);
                rec.tags = tags;
              }
            }
            try {
              await window.plaud.tagRecording(sid, rec.tags);
            } catch (err) {
              failures.push({ sid, err });
            }
          }
          if (failures.length === 0) {
            toast(
              allHaveTag
                ? `Removed "${tag}" from ${ids.length} recording(s)`
                : `Tagged ${ids.length} recording(s) as "${tag}"`,
              'success',
            );
          } else if (failures.length === ids.length) {
            toast(
              `Failed to tag any recordings (${failures[0].err.message || 'unknown'})`,
              'error',
            );
          } else {
            toast(
              `Tagged ${ids.length - failures.length}/${ids.length}; ${failures.length} failed`,
              'warning',
            );
          }
          renderRecordingsList();
          return;
        }

        if (app.activeTagFilters.has(tag)) {
          app.activeTagFilters.delete(tag);
        } else {
          app.activeTagFilters.add(tag);
        }
        renderRecordingsList();
      });
    });
  } else {
    tagFilters.innerHTML = '';
  }
}

async function deleteSelected() {
  pruneSelectionToVisible();
  if (app.selectedIds.size === 0) return;
  const ids = Array.from(app.selectedIds);
  return showDeleteDialog(ids, ids.length);
}

function _recordingLabel(sid) {
  const rec = app.recordings.find((r) => r.session_id === sid);
  if (!rec) return String(sid);
  return rec.title || formatDate(rec.date) || String(sid);
}

/**
 * Confirm-then-delete for one or many recordings. Always asks first, naming the
 * count and up to 3 titles. When any of them is synced to Plaud Cloud and the
 * user is signed in, also offers "Move to Trash and delete from Plaud Cloud".
 * Cancel is the default button. Resolves true if something was deleted.
 * (`count` is kept for call-site compatibility; ids.length is authoritative.)
 */
async function showDeleteDialog(ids, count, onDeleted) {
  ids = (ids || []).filter(Boolean);
  if (ids.length === 0) return false;
  count = ids.length;
  const plural = count === 1 ? 'recording' : 'recordings';

  const cloudRecs = ids
    .map((sid) => app.recordings.find((r) => r.session_id === sid))
    .filter((r) => r && r.cloud_synced);
  const offerCloud = cloudRecs.length > 0 && !!(app.cloudState && app.cloudState.logged_in);

  const names = ids.slice(0, 3).map(_recordingLabel);
  if (count > 3) names.push(`…and ${count - 3} more`);

  const actions = [{ id: 'local', label: 'Move to Trash', destructive: true }];
  if (offerCloud) {
    actions.push({ id: 'both', label: 'Move to Trash and delete from Plaud Cloud', destructive: true });
  }
  let message = `${count === 1 ? 'This recording' : `These ${count} recordings`} (audio, transcript and summary) will be moved to the Trash.`;
  if (offerCloud) {
    message += cloudRecs.length === count
      ? ` ${count === 1 ? 'It is' : 'They are'} also synced to Plaud Cloud.`
      : ' Some are also synced to Plaud Cloud.';
  } else if (cloudRecs.length > 0) {
    message += ' Copies in Plaud Cloud are not affected.';
  }

  const choice = await showConfirmDialog({
    title: `Delete ${count} ${plural}?`,
    message,
    items: names,
    actions,
  });
  if (!choice) return false;

  try {
    const cloudIds = choice === 'both'
      ? ids
          .map((sid) => {
            const rec = app.recordings.find((r) => r.session_id === sid);
            return rec && rec.cloud_id;
          })
          .filter(Boolean)
      : [];

    const res = await window.plaud.deleteRecordings(ids);
    if (res && res.error) {
      toast(`Delete failed: ${res.error}`, 'error');
      await loadRecordings();
      return false;
    }
    const n = res && typeof res.count === 'number' ? res.count : count;
    const nPlural = n === 1 ? 'recording' : 'recordings';
    const localMsg = res && res.trashed ? `Moved ${n} ${nPlural} to Trash` : `Deleted ${n} ${nPlural}`;
    const errs = res && Array.isArray(res.errors) ? res.errors : [];

    if (errs.length > 0) {
      const first = _describeApiError(errs[0]);
      const more = errs.length > 1 ? ` (+${errs.length - 1} more)` : '';
      toast(
        n > 0
          ? `${localMsg}; ${errs.length} couldn't be removed: ${first}${more}`
          : `Delete failed: ${first}${more}`,
        'error',
      );
      // Don't touch Plaud Cloud when the local delete didn't fully succeed
      if (choice === 'both' && cloudIds.length > 0) toast('Plaud Cloud copies were left untouched', 'info');
    } else if (choice === 'both') {
      if (cloudIds.length > 0) {
        try {
          const cr = await window.plaud.cloudTrash(cloudIds);
          if (cr && cr.error) toast(`${localMsg}, but cloud delete failed: ${cr.error}`, 'error');
          else toast(`${localMsg} and deleted from Plaud Cloud`, 'success');
        } catch (err) {
          toast(`${localMsg}, but cloud delete failed: ${err.message}`, 'error');
        }
      } else {
        toast(`${localMsg}. Cloud file ID not available: remove it from Plaud Cloud manually`, 'info');
      }
    } else {
      toast(localMsg, 'success');
    }

    if (onDeleted) onDeleted();
    exitSelectMode();
    await loadRecordings();
    return true;
  } catch (err) {
    toast(`Delete failed: ${err.message}`, 'error');
    return false;
  }
}

// Errors in API `errors` arrays may be strings or {session_id, error} objects.
function _describeApiError(e) {
  if (!e) return 'unknown error';
  if (typeof e === 'string') return e;
  const what = e.error || e.message || 'error';
  return e.session_id ? `${_recordingLabel(e.session_id)}: ${what}` : String(what);
}

// Re-download sets the local audio aside (backend keeps a backup) so the next
// sync fetches it again from the device. Confirm first.
async function confirmRedownload(sessionId) {
  const autoDelete = !!(app.settings && app.settings.auto_delete_after_sync);
  const choice = await showConfirmDialog({
    title: 'Re-download from device?',
    message: `The local copy of "${_recordingLabel(sessionId)}" will be set aside and replaced with the copy on your Plaud device the next time you sync. You can restore the old copy until then.`,
    note: autoDelete
      ? '"Delete recordings from device after sync" is on, so the device may no longer have this recording.'
      : '',
    actions: [{ id: 'redownload', label: 'Replace from device', destructive: true }],
  });
  if (choice !== 'redownload') return false;
  try {
    const res = await window.plaud.redownloadRecordings([sessionId]);
    if (res && res.error) {
      toast(`Failed: ${res.error}`, 'error');
      return false;
    }
    const errs = res && Array.isArray(res.errors) ? res.errors : [];
    if (errs.length > 0) toast(`Re-download: ${_describeApiError(errs[0])}`, 'error');
    else toast('Old copy set aside — connect your device and sync to re-download', 'info');
    await loadRecordings();
    _refreshOpenTranscript(sessionId);
    return true;
  } catch (err) {
    toast(`Failed: ${err.message}`, 'error');
    return false;
  }
}

// Put back the copy that "Re-download from device" set aside.
async function restoreRedownload(sessionId) {
  if (!window.plaud || typeof window.plaud.restoreRedownload !== 'function') {
    toast('Restore is not available in this version', 'error');
    return false;
  }
  try {
    const res = await window.plaud.restoreRedownload([sessionId]);
    if (res && res.error) {
      toast(`Restore failed: ${res.error}`, 'error');
      return false;
    }
    const errs = res && Array.isArray(res.errors) ? res.errors : [];
    if (errs.length > 0) toast(`Restore failed: ${_describeApiError(errs[0])}`, 'error');
    else toast('Previous copy restored', 'success');
    await loadRecordings();
    _refreshOpenTranscript(sessionId);
    return errs.length === 0;
  } catch (err) {
    toast(`Restore failed: ${err.message}`, 'error');
    return false;
  }
}

function _refreshOpenTranscript(sessionId) {
  if (app.currentView === 'transcript' && app.currentSessionId === sessionId) openTranscript(sessionId);
}


// ---- Move Recordings ----

function _shortenDirPath(p) {
  const home = '/Users/' + (p.split('/Users/')[1] || '').split('/')[0];
  if (home && home !== '/Users/' && p.startsWith(home)) return '~' + p.slice(home.length);
  return p;
}

/** "/a/very/long/path" → "/a/ver…ng/path": keeps both ends, which tell folders apart. */
function _moveMiddleTruncate(text, max = 44) {
  if (text.length <= max) return text;
  const keep = max - 1;
  const head = Math.ceil(keep * 0.4);
  return `${text.slice(0, head)}\u2026${text.slice(text.length - (keep - head))}`;
}

function showMoveDialog(ids, onMoved) {
  const count = ids.length;
  const s = app.settings || {};
  const activeDir = s.save_directory || s.default_save_directory || '';
  const defaultDir = s.default_save_directory || '';
  const historical = (s.historical_directories || []);

  // The folder the (first) selected recording is in now: listed, marked, not clickable
  const sourceRec = app.recordings.find(r => r.session_id === ids[0]);
  const sourceDir = sourceRec && sourceRec.ogg_path ? sourceRec.ogg_path.replace(/\/[^/]+$/, '') : '';

  const destinations = [];
  const seen = new Set();
  for (const d of [sourceDir, activeDir, defaultDir, ...historical]) {
    if (!d || seen.has(d)) continue;
    seen.add(d);
    destinations.push(d);
  }
  const destButton = (d) => {
    const short = _shortenDirPath(d);
    const cut = short.lastIndexOf('/');
    const name = cut >= 0 && cut < short.length - 1 ? short.slice(cut + 1) : short;
    const parent = cut > 0 ? short.slice(0, cut) : '';
    const isCurrent = d === sourceDir;
    const marks = [isCurrent ? 'Current location' : '', d === activeDir ? 'Save folder' : ''].filter(Boolean);
    return `
          <button class="action-btn subtle move-dest-btn${isCurrent ? ' is-current' : ''}" data-dir="${escapeHtml(d)}" title="${escapeHtml(d)}" ${isCurrent ? 'disabled aria-disabled="true"' : ''}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="move-dest-icon"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
            <span class="move-dest-text">
              <span class="move-dest-name">${escapeHtml(name)}</span>
              ${parent ? `<span class="move-dest-path">${escapeHtml(_moveMiddleTruncate(parent))}</span>` : ''}
            </span>
            ${marks.length ? `<span class="move-dest-mark">${escapeHtml(marks.join(' · '))}</span>` : ''}
          </button>`;
  };

  const overlay = document.createElement('div');
  overlay.className = 'delete-dialog-overlay';
  overlay.innerHTML = `
    <div class="delete-dialog" style="max-width:420px;">
      <h3>Move ${count} recording${count > 1 ? 's' : ''}</h3>
      <p class="settings-desc">Choose a destination folder.</p>
      <div class="move-destinations">
        ${destinations.map(destButton).join('')}
        <button class="action-btn subtle" id="move-browse-btn" style="width:100%;justify-content:center;">
          Choose another folder…
        </button>
      </div>
      <button class="action-btn subtle" id="move-cancel-btn" style="width:100%;margin-top:var(--sp-2);justify-content:center;">
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

  const doMove = async (dest) => {
    cleanup();
    try {
      const result = await window.plaud.moveRecordings(ids, dest);
      const moved = result.count || 0;
      const errors = (result.errors || []).length;
      if (moved > 0) toast(`Moved ${moved} recording${moved > 1 ? 's' : ''}`, 'success');
      if (errors > 0) toast(`${errors} could not be moved`, 'error');
      if (onMoved) onMoved();
      exitSelectMode();
      await loadSettings();
      await loadRecordings();
    } catch (err) {
      toast(`Move failed: ${err.message}`, 'error');
    }
  };

  overlay.querySelectorAll('.move-dest-btn:not(.is-current)').forEach(btn => {
    btn.addEventListener('click', () => doMove(btn.dataset.dir));
  });

  document.getElementById('move-browse-btn').addEventListener('click', async () => {
    let dir;
    if (window.plaud.selectDirectory) {
      dir = await window.plaud.selectDirectory();
    } else {
      dir = prompt('Enter the full destination path:');
    }
    if (dir) doMove(dir);
    else cleanup();
  });

  document.getElementById('move-cancel-btn').addEventListener('click', cleanup);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(); });
}

/** Queue the selected recordings that have no transcript yet (select bar). */
function transcribeSelected() {
  if (app.selectedIds.size === 0) return;
  const todo = app.recordings.filter(
    (rec) => app.selectedIds.has(rec.session_id) && rec.ogg_path && !rec.partial && !app.transcripts[rec.session_id],
  );
  if (todo.length === 0) {
    toast('The selected recordings are already transcribed', 'info');
    return;
  }
  queueAutoProcess(todo, { always: true });
}

async function moveSelected() {
  if (app.selectedIds.size === 0) return;
  showMoveDialog(Array.from(app.selectedIds));
}


// ---- Context Menu ----

let _ctxMenu = null;

function _removeContextMenu() {
  if (_ctxMenu) { _ctxMenu.remove(); _ctxMenu = null; }
}

function _showContextMenu(x, y, sessionId) {
  _removeContextMenu();

  const t = app.transcripts[sessionId];
  const hasTranscript = t && t.text;
  const hasSummary = t && t.summary;

  const items = [];

  if (hasTranscript) {
    items.push({ label: 'Copy transcript', icon: 'copy', action: () => {
      const text = (t.segments || []).map(s => {
        const speaker = s.speaker ? `[${s.speaker}] ` : '';
        return `${speaker}${s.text}`;
      }).join('\n') || t.text;
      navigator.clipboard.writeText(text).then(() => toast('Transcript copied', 'success')).catch(() => toast('Copy failed', 'error'));
    }});
  }

  if (hasSummary) {
    items.push({ label: 'Copy summary', icon: 'copy', action: () => {
      navigator.clipboard.writeText(t.summary).then(() => toast('Summary copied', 'success')).catch(() => toast('Copy failed', 'error'));
    }});
  }

  // Notion — check if page exists
  if (app.notionState && app.notionState.connected) {
    items.push({ label: 'Push to Notion', icon: 'notion', action: async () => {
      try {
        const pageCheck = await window.plaud.notionPageUrl(sessionId);
        if (pageCheck.has_page && pageCheck.url) {
          window.plaud.openExternal(pageCheck.url);
        } else if (hasTranscript) {
          toast('Pushing to Notion...', 'info');
          const result = await window.plaud.notionPush(sessionId);
          if (result.pushed) {
            toast('Pushed to Notion', 'success');
            if (result.url) window.plaud.openExternal(result.url);
          } else if (result.needs_review) {
            toast('This recording may already be in Notion. Open it to choose which page to use.', 'info');
          } else {
            toast(result.error || 'Push failed', 'error');
          }
        }
      } catch (err) {
        toast(`Notion error: ${err.message}`, 'error');
      }
    }});
  }

  if (items.length > 0) {
    items.push({ divider: true });
  }

  items.push({ label: 'Move to…', icon: 'move', action: () => {
    showMoveDialog([sessionId], () => { loadRecordings(); });
  }});
  const ctxRec = app.recordings.find((r) => r.session_id === sessionId);
  // Any card whose old copy is still set aside — including an interrupted
  // re-download that now shows as "Incomplete" — offers the restore.
  if (ctxRec && ctxRec.redownload_pending) {
    items.push({ label: 'Restore previous copy', icon: 'refresh', action: () => {
      restoreRedownload(sessionId);
    }});
  } else {
    items.push({ label: 'Re-download from device', icon: 'refresh', action: () => {
      confirmRedownload(sessionId);
    }});
  }
  items.push({ label: 'Delete', icon: 'delete', destructive: true, action: () => {
    showDeleteDialog([sessionId], 1, () => { loadRecordings(); });
  }});

  // Build menu DOM
  const menu = document.createElement('div');
  menu.className = 'ctx-menu';
  menu.innerHTML = items.map(item => {
    if (item.divider) return '<div class="ctx-menu-divider"></div>';
    const cls = item.destructive ? ' ctx-menu-item-destructive' : '';
    return `<button class="ctx-menu-item${cls}">${escapeHtml(item.label)}</button>`;
  }).join('');

  // Position — keep within viewport
  document.body.appendChild(menu);
  const rect = menu.getBoundingClientRect();
  const maxX = window.innerWidth - rect.width - 8;
  const maxY = window.innerHeight - rect.height - 8;
  menu.style.left = `${Math.min(x, maxX)}px`;
  menu.style.top = `${Math.min(y, maxY)}px`;
  _ctxMenu = menu;

  // Bind actions
  let actionIdx = 0;
  menu.querySelectorAll('.ctx-menu-item').forEach(btn => {
    // Find the matching non-divider item
    while (items[actionIdx] && items[actionIdx].divider) actionIdx++;
    if (items[actionIdx]) {
      const action = items[actionIdx].action;
      btn.addEventListener('click', () => { _removeContextMenu(); action(); });
      actionIdx++;
    }
  });

  // Close on click outside
  setTimeout(() => {
    document.addEventListener('click', _removeContextMenu, { once: true });
  }, 10);
}

// ---- Background processing of new recordings ----
// A sync ends once the files are on disk: the list refreshes straight away and
// new recordings are transcribed (and summarised, if that's on) here, one at a
// time, with a "Transcribing" / "Summarizing" badge on each. Before v2.5.1 every
// sync waited for all of this, so 47 recordings on local Whisper kept the sync
// window open, the list empty and "Syncing" showing for hours.
const _autoProcessQueue = [];
let _autoProcessRunning = false;

function _autoProcessSessionId(f) {
  return String(f.session_id || String(f.ogg_path).split('/').pop().replace('.ogg', ''));
}

/**
 * Queue newly synced recordings ({session_id, ogg_path}) for transcription.
 * Honours "Auto-transcribe" unless opts.always; opts.language overrides the
 * language setting. Recordings already queued are skipped.
 */
function queueAutoProcess(files, opts = {}) {
  if (!opts.always && app.settings.auto_transcribe === false) return 0;
  let added = 0;
  for (const f of files || []) {
    if (!f || !f.ogg_path) continue;
    const sid = _autoProcessSessionId(f);
    if (_autoProcessQueue.some((q) => q.sid === sid)) continue;
    _autoProcessQueue.push({ sid, oggPath: f.ogg_path, language: opts.language });
    app.processingIds.set(sid, 'queued');
    added++;
  }
  if (added > 0) {
    toast(`Transcribing ${added} new recording(s) in the background`, 'info');
    renderRecordingsList();
    _runAutoProcessQueue();
  }
  return added;
}

async function _runAutoProcessQueue() {
  if (_autoProcessRunning) return;
  _autoProcessRunning = true;
  let done = 0;
  let failed = 0;
  try {
    while (_autoProcessQueue.length > 0) {
      const item = _autoProcessQueue[0];
      try {
        // Already transcribed meanwhile (e.g. by hand): only summarise.
        if (!app.transcripts[item.sid]) {
          app.processingIds.set(item.sid, 'transcribing');
          renderRecordingsList();
          const result = await window.plaud.transcribe(
            item.oggPath,
            app.settings.model || 'small',
            app.settings.diarize || false,
            item.language || app.settings.language || 'auto',
          );
          if (result && result.error) throw new Error(result.error);
        }
        if (app.settings.auto_summarise) {
          app.processingIds.set(item.sid, 'summarizing');
          renderRecordingsList();
          await window.plaud.summarize(item.sid);
        }
        done++;
      } catch (err) {
        failed++;
        console.error(`Background processing failed for ${item.sid}:`, err);
      } finally {
        app.processingIds.delete(item.sid);
        _autoProcessQueue.shift();
      }
      await loadRecordings();
      if (app.currentView === 'transcript' && app.currentSessionId === item.sid) openTranscript(item.sid);
    }
  } finally {
    _autoProcessRunning = false;
  }
  if (done > 0) toast(`Finished processing ${done} new recording(s)`, 'success');
  if (failed > 0) toast(`${failed} recording(s) couldn't be transcribed; open one to try again`, 'error');
}
