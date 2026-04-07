/* ============================================================
   Plaud V2 — Recordings List Module
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

function renderRecordingsList() {
  const filtered = getFilteredRecordings();
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
      const dur = formatDuration(rec.duration);
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
      } else if (processing) {
        badgeClass = 'badge-processing';
        badgeText = processing === 'transcribing' ? 'Transcribing' : 'Summarizing';
      } else if (hasTranscript) {
        badgeClass = 'badge-transcribed';
        badgeText = 'Transcribed';
      }

      const isSelected = app.selectedIds.has(rec.session_id);

      return `
      <div class="recording-card ${app.selectMode ? 'select-mode' : ''} ${isSelected ? 'selected' : ''} ${rec.partial ? 'recording-partial' : ''}" data-sid="${rec.session_id}">
        ${
          rec.partial
            ? `
          <div class="recording-partial-icon" title="Incomplete transfer — sync again to complete">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
              <path d="M8 1a7 7 0 100 14A7 7 0 008 1zm0 10.5a.75.75 0 110-1.5.75.75 0 010 1.5zM8.75 4v4.5h-1.5V4h1.5z"/>
            </svg>
          </div>
        `
            : app.selectMode
              ? `
          <label class="select-checkbox" data-sid="${rec.session_id}">
            <input type="checkbox" ${isSelected ? 'checked' : ''}>
            <span class="checkbox-mark"></span>
          </label>
        `
              : `
          <button class="recording-play-btn" data-sid="${rec.session_id}" title="Play">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
              <path d="M4 2l10 6-10 6z"/>
            </svg>
          </button>
        `
        }
        <div class="recording-info">
          ${rec.title ? `<div class="recording-title">${rec.cloud_synced ? '<svg class="cloud-icon" width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4.5 13h7a3.5 3.5 0 00.5-6.97A5 5 0 002.1 8.26 3 3 0 004.5 13z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg> ' : ''}${escapeHtml(rec.title)}</div>` : ''}
          <div class="recording-date">${!rec.title && rec.cloud_synced ? '<svg class="cloud-icon" width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4.5 13h7a3.5 3.5 0 00.5-6.97A5 5 0 002.1 8.26 3 3 0 004.5 13z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg> ' : ''}${date}</div>
          <div class="recording-duration">${rec.partial ? 'Sync again to complete transfer' : dur}${speakerCount ? ' \u00B7 ' + speakerCount : ''}</div>
          ${preview ? `<div class="recording-preview">${escapeHtml(preview)}</div>` : ''}
        </div>
        <div class="recording-right">
          ${rec.tags && rec.tags.length ? `<div class="tag-list">${rec.tags.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join('')}</div>` : ''}
          <span class="recording-badge ${badgeClass}">${badgeText}</span>
        </div>
      </div>
    `;
    })
    .join('');

  // Bind click handlers
  dom.recordingsList.querySelectorAll('.recording-card').forEach((card) => {
    card.addEventListener('click', (e) => {
      if (card.classList.contains('recording-partial')) return;
      if (e.target.closest('.recording-play-btn')) return;
      if (e.target.closest('.select-checkbox')) {
        toggleSelection(card.dataset.sid);
        return;
      }
      const sid = card.dataset.sid;
      if (app.selectMode) {
        toggleSelection(sid);
      } else {
        openTranscript(sid);
      }
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

function toggleSelection(sid) {
  if (app.selectedIds.has(sid)) {
    app.selectedIds.delete(sid);
  } else {
    app.selectedIds.add(sid);
  }
  renderRecordingsList();
}

function enterSelectMode() {
  app.selectMode = true;
  app.selectedIds.clear();
  renderRecordingsList();
}

function exitSelectMode() {
  app.selectMode = false;
  app.selectedIds.clear();
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
    tagFilters.innerHTML = [...tagsToShow]
      .sort()
      .map(
        (tag) =>
          `<button class="tag-filter ${app.activeTagFilters.has(tag) ? 'active' : ''}" data-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>`,
      )
      .join('');

    tagFilters.querySelectorAll('.tag-filter').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const tag = btn.dataset.tag;

        if (app.selectMode && app.selectedIds.size > 0) {
          const ids = [...app.selectedIds];
          const allHaveTag = ids.every((sid) => {
            const rec = app.recordings.find((r) => r.session_id === sid);
            return rec && (rec.tags || []).includes(tag);
          });

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
            await window.plaud.tagRecording(sid, rec.tags);
          }
          toast(
            allHaveTag
              ? `Removed "${tag}" from ${ids.length} recording(s)`
              : `Tagged ${ids.length} recording(s) as "${tag}"`,
            'success',
          );
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
  if (app.selectedIds.size === 0) return;
  const ids = Array.from(app.selectedIds);
  const count = ids.length;

  const hasCloud = ids.some((sid) => {
    const rec = app.recordings.find((r) => r.session_id === sid);
    return rec && rec.cloud_synced;
  });

  if (hasCloud) {
    showDeleteDialog(ids, count);
  } else {
    try {
      await window.plaud.deleteRecordings(ids);
      toast(`Deleted ${count} recording(s)`, 'success');
      exitSelectMode();
      await loadRecordings();
    } catch (err) {
      toast(`Delete failed: ${err.message}`, 'error');
    }
  }
}

function showDeleteDialog(ids, count, onDeleted) {
  const overlay = document.createElement('div');
  overlay.className = 'delete-dialog-overlay';
  overlay.innerHTML = `
    <div class="delete-dialog">
      <h3>Delete ${count} recording${count > 1 ? 's' : ''}?</h3>
      <p class="settings-desc">Some recordings are synced to Plaud Cloud.</p>
      <div class="delete-dialog-actions">
        <button class="action-btn subtle" id="delete-local-btn">
          Delete locally only
        </button>
        <button class="action-btn delete" id="delete-both-btn">
          Delete from cloud &amp; local
        </button>
      </div>
      <button class="action-btn subtle" id="delete-cancel-btn" style="width:100%;margin-top:8px;justify-content:center;">
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

  document.getElementById('delete-cancel-btn').addEventListener('click', cleanup);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) cleanup();
  });

  document.getElementById('delete-local-btn').addEventListener('click', async () => {
    cleanup();
    try {
      await window.plaud.deleteRecordings(ids);
      toast(`Deleted ${count} recording(s) locally`, 'success');
      if (onDeleted) onDeleted();
      exitSelectMode();
      await loadRecordings();
    } catch (err) {
      toast(`Delete failed: ${err.message}`, 'error');
    }
  });

  document.getElementById('delete-both-btn').addEventListener('click', async () => {
    cleanup();
    try {
      const cloudIds = ids
        .map((sid) => {
          const rec = app.recordings.find((r) => r.session_id === sid);
          return rec && rec.cloud_id;
        })
        .filter(Boolean);

      await window.plaud.deleteRecordings(ids);

      if (cloudIds.length > 0 && app.cloudState.logged_in) {
        try {
          await window.plaud.cloudTrash(cloudIds);
          toast(`Deleted ${count} recording(s) from local and cloud`, 'success');
        } catch (err) {
          toast(`Deleted locally but cloud trash failed: ${err.message}`, 'error');
        }
      } else if (app.cloudState.logged_in) {
        toast(`Deleted locally — cloud file ID not available, remove from cloud manually`, 'info');
      } else {
        toast(`Deleted ${count} recording(s) locally`, 'success');
      }

      if (onDeleted) onDeleted();
      exitSelectMode();
      await loadRecordings();
    } catch (err) {
      toast(`Delete failed: ${err.message}`, 'error');
    }
  });
}


// ---- Move Recordings ----

function _shortenDirPath(p) {
  const home = '/Users/' + (p.split('/Users/')[1] || '').split('/')[0];
  if (home && home !== '/Users/' && p.startsWith(home)) return '~' + p.slice(home.length);
  return p;
}

function showMoveDialog(ids, onMoved) {
  const count = ids.length;
  const s = app.settings || {};
  const activeDir = s.save_directory || s.default_save_directory || '';
  const defaultDir = s.default_save_directory || '';
  const historical = (s.historical_directories || []);

  // Build list of unique destination options (exclude the source dir of the first selected recording)
  const sourceRec = app.recordings.find(r => r.session_id === ids[0]);
  const sourceDir = sourceRec && sourceRec.ogg_path ? sourceRec.ogg_path.replace(/\/[^/]+$/, '') : '';

  const destinations = [];
  const seen = new Set();
  for (const d of [activeDir, defaultDir, ...historical]) {
    if (!d || seen.has(d) || d === sourceDir) continue;
    seen.add(d);
    destinations.push(d);
  }

  const overlay = document.createElement('div');
  overlay.className = 'delete-dialog-overlay';
  overlay.innerHTML = `
    <div class="delete-dialog" style="max-width:420px;">
      <h3>Move ${count} recording${count > 1 ? 's' : ''}</h3>
      <p class="settings-desc">Choose a destination folder.</p>
      <div class="move-destinations">
        ${destinations.map(d => `
          <button class="action-btn subtle move-dest-btn" data-dir="${escapeHtml(d)}" style="width:100%;justify-content:flex-start;text-align:left;">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;opacity:0.5;"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
            <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(_shortenDirPath(d))}</span>
            ${d === activeDir ? '<span style="margin-left:auto;font-size:var(--text-xs);color:var(--text-tertiary);">active</span>' : ''}
          </button>
        `).join('')}
        <button class="action-btn subtle" id="move-browse-btn" style="width:100%;justify-content:center;">
          Browse...
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

  overlay.querySelectorAll('.move-dest-btn').forEach(btn => {
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
    items.push({ label: 'Copy Transcript', icon: 'copy', action: () => {
      const text = (t.segments || []).map(s => {
        const speaker = s.speaker ? `[${s.speaker}] ` : '';
        return `${speaker}${s.text}`;
      }).join('\n') || t.text;
      navigator.clipboard.writeText(text).then(() => toast('Transcript copied', 'success')).catch(() => toast('Copy failed', 'error'));
    }});
  }

  if (hasSummary) {
    items.push({ label: 'Copy Summary', icon: 'copy', action: () => {
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

  items.push({ label: 'Move to...', icon: 'move', action: () => {
    showMoveDialog([sessionId], () => { loadRecordings(); });
  }});
  items.push({ label: 'Re-download from device', icon: 'refresh', action: async () => {
    try {
      await window.plaud.redownloadRecordings([sessionId]);
      toast('Recording cleared — connect device and sync to re-download', 'info');
      await loadRecordings();
    } catch (err) {
      toast(`Failed: ${err.message}`, 'error');
    }
  }});
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
