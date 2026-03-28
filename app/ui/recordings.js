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
      if (processing) {
        badgeClass = 'badge-processing';
        badgeText = processing === 'transcribing' ? 'Transcribing' : 'Summarizing';
      } else if (hasTranscript) {
        badgeClass = 'badge-transcribed';
        badgeText = 'Transcribed';
      }

      const isSelected = app.selectedIds.has(rec.session_id);

      return `
      <div class="recording-card ${app.selectMode ? 'select-mode' : ''} ${isSelected ? 'selected' : ''}" data-sid="${rec.session_id}">
        ${
          app.selectMode
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
          <div class="recording-duration">${dur}${speakerCount ? ' \u00B7 ' + speakerCount : ''}</div>
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
