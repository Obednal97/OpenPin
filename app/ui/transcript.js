/* ============================================================
   Open Pin — Transcript View Module
   ============================================================ */

// Track document-level listener to prevent accumulation across openTranscript() calls
let _exportDropdownCleanup = null;

// OAuth poll handles — stored at module scope so repeated clicks don't accumulate intervals
let _notionPollHandleTx = null;
let _notionPollTimeoutHandleTx = null;
// Background diarization poll — orphan intervals could forcibly re-open the
// transcript view, so we track the handles and cancel on close + on re-entry.
let _diarPollHandleTx = null;
let _diarPollTimeoutHandleTx = null;
// The speaker name editor open in the transcript (one at a time), and the
// transcript object the open view renders from (refreshSpeakerNames updates it).
let _txSpeakerEditorEl = null;
let _txShownT = null;
// A background redraw of this recording that waits for the name editor to close
let _txPendingRedrawSid = null;

// The tag picker in the transcript header. It reads the recording's tags from
// app.recordings each time it draws or is clicked, never from when it opened: an
// AI tool can change them while the recording is open, the change poll then
// reloads app.recordings (with new objects), and the picker is drawn again only
// once nothing in it is being edited. `rec` stands in if the recording has left
// the list.
function _txMountTagPicker(sessionId, rec) {
  const tagPicker = $('#transcript-tags');
  const availableTags = app.settings.available_tags || [
    'Group Meeting',
    '1:1',
    'Memo',
    'Journal',
    'Interview',
    'Lecture',
  ];
  const liveRec = () => app.recordings.find((r) => r.session_id === sessionId) || rec;
  const currentTags = () => liveRec().tags || [];
  const setTags = (tags) => { liveRec().tags = tags; };
  const hasTag = (tags, tag) => tags.some((t) => t.toLowerCase() === tag.toLowerCase());
  // Collapsed: only this recording's tags and "+ Tag". Open: every tag, a
  // new-tag box and "Done".
  let tagPickerOpen = false;

  // Tags change one at a time on the server, never by saving the whole list,
  // so this window can't wipe a tag an AI tool added since it opened. The
  // server's reply is the recording's real tags (and new tags join the picker
  // there). On failure the click is undone and the reason shown.
  async function saveTagChange(tag, adding) {
    let res;
    try {
      res = adding
        ? await window.plaud.addTags([sessionId], [tag])
        : await window.plaud.removeTags([sessionId], [tag]);
    } catch (err) {
      res = { error: (err && err.message) || 'Could not save the tag' };
    }
    const saved = res && res.tags && res.tags[sessionId];
    if (saved) {
      setTags([...saved]);
      saved.forEach((t) => {
        if (!availableTags.some((a) => a.toLowerCase() === t.toLowerCase())) availableTags.push(t);
      });
      app.settings.available_tags = availableTags;
    } else {
      const tags = currentTags();
      if (adding) setTags(tags.filter((t) => t.toLowerCase() !== tag.toLowerCase()));
      else if (!hasTag(tags, tag)) setTags([...tags, tag]);
      toast(((res && (res.errors && res.errors[sessionId])) || (res && res.error)) || 'Could not save the tag', 'error');
    }
    if (app.currentView === 'transcript' && app.currentSessionId === sessionId) renderTagPicker();
  }

  function renderTagPicker() {
    const tags = currentTags();
    const shown = availableTags.filter((tag) => tagPickerOpen || tags.includes(tag));
    // Tags set elsewhere (e.g. spoken) that aren't in the list yet
    tags.forEach((tag) => { if (!shown.includes(tag)) shown.push(tag); });
    tagPicker.innerHTML =
      shown
        .map((tag) => {
          const active = tags.includes(tag);
          return `<button class="tag-option ${active ? 'active' : ''}" data-tag="${escapeHtml(tag)}" title="${active ? 'Remove tag' : 'Add tag'}">${escapeHtml(tag)}</button>`;
        })
        .join('')
      + (tagPickerOpen
        ? `<input type="text" class="tag-add-input" id="tag-add-input" placeholder="+ New tag"><button class="tag-option tag-picker-toggle" id="tag-picker-done">Done</button>`
        : `<button class="tag-option tag-picker-toggle" id="tag-picker-open">${tags.length ? '+ Tag' : '+ Add tag'}</button>`);

    const openBtn = document.getElementById('tag-picker-open');
    if (openBtn) {
      openBtn.addEventListener('click', () => {
        tagPickerOpen = true;
        renderTagPicker();
      });
    }
    const doneBtn = document.getElementById('tag-picker-done');
    if (doneBtn) {
      doneBtn.addEventListener('click', () => {
        tagPickerOpen = false;
        renderTagPicker();
      });
    }

    tagPicker.querySelectorAll('.tag-option[data-tag]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const tag = btn.dataset.tag;
        // Add or remove by the recording's tags now. If they already are what the
        // user asked for (an AI tool got there first, and the picker hadn't been
        // drawn again), send nothing: just show the real tags.
        const now = currentTags();
        const adding = !hasTag(now, tag);
        if (adding === btn.classList.contains('active')) {
          renderTagPicker();
          return;
        }
        setTags(adding ? [...now, tag] : now.filter((t) => t.toLowerCase() !== tag.toLowerCase()));
        // Render immediately: the server just saves locally and queues the
        // Notion sync, so the UI doesn't need to wait.
        renderTagPicker();
        saveTagChange(tag, adding);
      });
    });

    const addInput = document.getElementById('tag-add-input');
    if (!addInput) return;
    addInput.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        tagPickerOpen = false;
        renderTagPicker();
        return;
      }
      if (e.key !== 'Enter') return;
      const typed = addInput.value.trim();
      if (!typed) return;
      e.preventDefault();
      // An existing tag's spelling wins (the server applies the same rule and
      // adds a new tag to the picker list itself).
      const now = currentTags();
      const newTag = availableTags.concat(now).find((t) => t.toLowerCase() === typed.toLowerCase()) || typed;
      if (!hasTag(now, newTag)) {
        setTags([...now, newTag]);
        saveTagChange(newTag, true);
      }
      renderTagPicker();
    });
  }
  renderTagPicker();
}

function openTranscript(sessionId, autoPlay = false) {
  const rec = app.recordings.find((r) => r.session_id === sessionId);
  if (!rec) return;

  const reopening = app.currentView === 'transcript' && app.currentSessionId === sessionId;
  app.currentSessionId = sessionId;
  const t = app.transcripts[sessionId];
  app.currentView = 'transcript';
  _txShownT = t || null;
  _txPendingRedrawSid = null; // this is the redraw
  _txRemoveSpeakerEditor();
  if (t && t.speaker_names_cleared) _txNoticeNamesCleared(sessionId, t);

  $('#main-content').scrollTop = 0;

  if (app.selectMode) exitSelectMode();
  dom.recordingsList.classList.add('hidden');
  dom.emptyState.classList.add('hidden');
  $('#select-bar').classList.add('hidden');
  dom.transcriptView.classList.remove('hidden');

  // Editable title
  const titleEl = $('#transcript-title');
  titleEl.textContent = rec.title || '';
  titleEl.dataset.sid = sessionId;
  // The date is on the line below, so an untitled recording doesn't show it twice
  titleEl.setAttribute('data-placeholder', 'Add a title…');

  titleEl.onblur = async () => {
    // Only ever rename the recording this title element is showing.
    if (titleEl.dataset.sid !== sessionId || app.currentSessionId !== sessionId) return;
    const newTitle = titleEl.textContent.trim();
    if (newTitle !== (rec.title || '')) {
      try {
        await window.plaud.renameRecording(sessionId, newTitle);
      } catch (err) {
        if (err.name !== 'AbortError') console.warn('[transcript] rename failed:', err);
        return;
      }
      rec.title = newTitle;
      loadRecordings();
    }
  };

  titleEl.onkeydown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      titleEl.blur();
    }
  };

  $('#transcript-date').textContent = formatDate(rec.date);

  _txMountTagPicker(sessionId, rec);

  // Meta
  const metaParts = rec.duration ? [formatDuration(rec.duration)] : [];
  if (t && t.language) metaParts.push(t.language.toUpperCase());
  if (t && t.speakers && t.speakers.length > 0) {
    metaParts.push(`${t.speakers.length} speaker${t.speakers.length > 1 ? 's' : ''}`);
  }
  $('#transcript-meta').textContent = metaParts.join(' \u00B7 ');

  // Audio — reset player UI immediately so we don't show the previous
  // recording's progress while the new m4a transcodes on the server.
  dom.audioEl.pause();
  dom.playerFill.style.width = '0%';
  dom.playerCurrent.textContent = formatTime(0);
  dom.playerDuration.textContent = formatTime(rec.duration || 0);
  dom.playIcon.classList.remove('hidden');
  dom.pauseIcon.classList.add('hidden');

  // No local .ogg (waiting for re-download, or an incomplete transfer): the
  // audio would only fail with a media error, so disable play/crop/download
  // and explain why in the notice above the actions. Cloud-only recordings
  // still stream from Google Drive.
  const noLocalAudio = _txHasNoAudio(rec);
  _txSetAudioControlsEnabled(!noLocalAudio);
  if (noLocalAudio) {
    dom.audioEl.removeAttribute('src');
    dom.audioEl.load();
  } else {
    dom.audioEl.src = window.plaud.audioUrl(`${sessionId}.ogg`);
    dom.audioEl.load();
    if (autoPlay) {
      dom.audioEl.play().catch(() => {});
    }
  }

  // Pre-warm the m4a cache for neighbouring recordings so flipping through
  // the list doesn't re-block on ffmpeg. Cached hits are a no-op server-side.
  try {
    const idx = app.recordings.findIndex((r) => r.session_id === sessionId);
    if (idx >= 0) {
      [app.recordings[idx - 1], app.recordings[idx + 1]]
        .filter((n) => n && !_txHasNoAudio(n))
        .forEach((n) => {
          fetch(window.plaud.audioUrl(`${n.session_id}.ogg`))
            .then((r) => r.blob())
            .catch(() => {});
        });
    }
  } catch {}

  // Actions bar
  const actionsBar = $('#transcript-actions');
  const hasSummary = t && t.summary;
  const hasTranscriptText = !!(t && (t.text || (t.segments && t.segments.length > 0)));
  const isTranscribing = app.processingIds.get(sessionId) === 'transcribing';
  const awaitingRedownload = !!rec.redownload_pending;
  const transcribeLabel = hasTranscriptText ? 'Re-transcribe' : 'Transcribe';
  const noAudioNotice = awaitingRedownload
    ? 'Waiting for re-download — connect your device and sync.'
    : 'Incomplete transfer — connect your device and sync again to finish it.';
  actionsBar.innerHTML = `
    ${noLocalAudio ? `
    <div class="redownload-notice" id="redownload-notice">
      <span>${noAudioNotice}</span>
      ${awaitingRedownload ? '<button class="action-btn subtle" id="restore-redownload-btn">Restore</button>' : ''}
    </div>` : ''}
    <button class="action-btn subtle" id="retranscribe-btn" title="${!rec.ogg_path ? 'No local audio to transcribe' : hasTranscriptText ? 'Replace the transcript with a new transcription' : 'Transcribe this recording'}" ${isTranscribing || awaitingRedownload || !rec.ogg_path ? 'disabled' : ''}>
      <svg width="16" height="16" viewBox="0 0 18 18" fill="none">
        <path d="M1.5 9a7.5 7.5 0 0113.1-5M16.5 9a7.5 7.5 0 01-13.1 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
        <path d="M14.6 1v3h-3M3.4 17v-3h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span>${isTranscribing ? 'Transcribing…' : transcribeLabel}</span>
    </button>
    ${
      t && t.text
        ? `
    <button class="action-btn subtle" id="summarize-btn" title="Write an AI summary">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <path d="M2 4h12M2 8h8M2 12h10" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
      </svg>
      <span>${hasSummary ? 'Re-summarise' : 'Summarise'}</span>
    </button>
    <div class="actions-divider"></div>
    <button class="action-btn subtle" id="copy-transcript-btn" title="Copy to clipboard">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <rect x="5" y="5" width="9" height="9" rx="1.5" stroke="currentColor" stroke-width="1.3"/>
        <path d="M3 11V3a1.5 1.5 0 011.5-1.5H11" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
      </svg>
      <span>Copy</span>
    </button>
    <div class="export-dropdown-wrap" style="position:relative;display:inline-block;">
      <button class="action-btn subtle" id="export-transcript-btn" title="Export">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <path d="M8 2v8m0 0L5 7m3 3l3-3M3 12v1.5a1 1 0 001 1h8a1 1 0 001-1V12" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span>Export</span>
      </button>
      <div id="export-dropdown" class="export-dropdown hidden">
        <button class="export-option" data-format="md">Markdown (.md)</button>
        <button class="export-option" data-format="txt">Plain text (.txt)</button>
        <button class="export-option" data-format="pdf">PDF (.pdf)</button>
        <button class="export-option" data-format="json">JSON (.json)</button>
      </div>
    </div>
    <button class="action-btn subtle" id="notion-push-btn" title="Push to Notion">
      <img src="notion-icon.svg" width="16" height="16" style="vertical-align:middle;">
      <span>Notion</span>
    </button>
    <button class="action-btn subtle hidden" id="notion-update-btn" title="Update title, tags and the Open Pin section of the Notion page. Your own notes in Notion are kept.">
      <span>Update in Notion</span>
    </button>
    `
        : ''
    }
    <div class="actions-divider"></div>
    <button class="action-btn subtle" id="delete-recording-btn" title="Delete recording" style="color:var(--destructive);">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <path d="M2 4h12M5.3 4V2.7a1.3 1.3 0 011.3-1.3h2.8a1.3 1.3 0 011.3 1.3V4M6.7 7.3v4M9.3 7.3v4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M3.3 4h9.4l-.7 9.3a1.3 1.3 0 01-1.3 1.2H5.3a1.3 1.3 0 01-1.3-1.2L3.3 4z" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span>Delete</span>
    </button>
  `;

  // (Re-)transcribe. The backend merges a re-transcription into the existing
  // transcript file and keeps the summary, so nothing is deleted up front.
  const restoreBtn = $('#restore-redownload-btn');
  if (restoreBtn) {
    restoreBtn.addEventListener('click', async () => {
      restoreBtn.disabled = true;
      const ok = await restoreRedownload(sessionId);
      if (!ok && restoreBtn.isConnected) restoreBtn.disabled = false;
    });
  }

  const retxBtn = $('#retranscribe-btn');
  retxBtn.addEventListener('click', async () => {
    if (!rec.ogg_path) {
      toast('No local audio yet — sync to re-download it first', 'info');
      return;
    }
    if (app.processingIds.get(sessionId) === 'transcribing') {
      toast('Already transcribing this recording', 'info');
      return;
    }
    if (hasTranscriptText) {
      const choice = await showConfirmDialog({
        title: 'Replace the transcript?',
        message: 'Your edits to segments will be lost; the summary is kept.',
        actions: [{ id: 'retranscribe', label: 'Re-transcribe', destructive: true }],
      });
      if (choice !== 'retranscribe') return;
      // Another click may have started a job while the dialog was open
      if (app.processingIds.get(sessionId) === 'transcribing') return;
    }
    retxBtn.disabled = true;
    const label = retxBtn.querySelector('span');
    if (label) label.textContent = 'Transcribing…';
    try {
      await transcribeRecording(rec.ogg_path, sessionId);
    } finally {
      // openTranscript() may have re-rendered the bar meanwhile (on success,
      // or by navigating away and back), detaching retxBtn: update whichever
      // button is on screen now.
      _txRefreshRetranscribeBtn(sessionId);
    }
  });

  // Copy
  const copyBtn = $('#copy-transcript-btn');
  if (copyBtn && t) {
    copyBtn.addEventListener('click', () => {
      const text = _txCopyText(t, !!(app.activeTab === 'summary' && t.summary));
      const label = app.activeTab === 'summary' ? 'Summary' : 'Transcript';
      navigator.clipboard
        .writeText(text)
        .then(() => {
          toast(`${label} copied to clipboard`, 'success');
          copyBtn.querySelector('span').textContent = 'Copied!';
          setTimeout(() => {
            copyBtn.querySelector('span').textContent = 'Copy';
          }, 2000);
        })
        .catch(() => toast('Failed to copy', 'error'));
    });
  }

  // Export dropdown
  const exportBtn = $('#export-transcript-btn');
  const exportDropdown = $('#export-dropdown');
  if (exportBtn && exportDropdown && t) {
    exportBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      exportDropdown.classList.toggle('hidden');
    });
    // Close dropdown on click outside — remove previous listener to prevent accumulation
    if (_exportDropdownCleanup) {
      document.removeEventListener('click', _exportDropdownCleanup);
    }
    _exportDropdownCleanup = () => { if (exportDropdown) exportDropdown.classList.add('hidden'); };
    document.addEventListener('click', _exportDropdownCleanup);

    exportDropdown.querySelectorAll('.export-option').forEach((opt) => {
      opt.addEventListener('click', async (e) => {
        e.stopPropagation();
        const format = opt.dataset.format;
        exportDropdown.classList.add('hidden');

        const isSum = app.activeTab === 'summary' && t.summary;
        const label = isSum ? 'Summary' : 'Transcript';
        const customName = app.settings.recording_names?.[sessionId] || t.auto_title || sessionId;
        const safeName = customName.replace(/[/\\:*?"<>|]/g, '-').slice(0, 80);

        // Build content based on format (speaker names in place of labels)
        const built = _txExportContent(format, t, customName, safeName, isSum);
        const { content, filename, mimeType } = built;

        if (format === 'pdf') {
          const { html } = built;
          const printWin = window.open('', '_blank', 'width=800,height=600');
          if (printWin) {
            printWin.document.write(`<!DOCTYPE html><html><head><title>${escapeHtml(safeName)}</title>
              <style>body{font-family:-apple-system,system-ui,sans-serif;max-width:700px;margin:40px auto;padding:0 20px;line-height:1.6;color:#222;}
              h1{font-size:20px;margin-bottom:24px;}h2{font-size:16px;margin-top:24px;}h3{font-size:14px;}
              ul,ol{padding-left:20px;}li{margin:4px 0;}li.md-task{list-style:none;margin-left:-16px;}small{color:#888;}p{margin:8px 0;}
              table{border-collapse:collapse;margin:12px 0;}th,td{border:1px solid #ccc;padding:4px 8px;text-align:left;}</style>
              </head><body>${html}</body></html>`);
            printWin.document.close();
            printWin.focus();
            setTimeout(() => { printWin.print(); printWin.close(); }, 500);
            toast(`Opening PDF print dialog...`, 'info');
          } else {
            // Fallback for WebViews that block window.open — download as HTML
            const blob = new Blob([`<!DOCTYPE html><html><head><title>${escapeHtml(safeName)}</title></head><body>${html}</body></html>`], { type: 'text/html' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${safeName}.html`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            toast(`Exported as HTML (open in browser and print to PDF)`, 'info');
          }
          return;
        }

        // Download file — try anchor-click, fallback to data URI for Tauri WebView
        try {
          const blob = new Blob([content], { type: mimeType });
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = filename;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(url);
        } catch {
          // Fallback: open as data URI (prompts save dialog)
          const encoded = btoa(unescape(encodeURIComponent(content)));
          const a = document.createElement('a');
          a.href = `data:${mimeType};base64,${encoded}`;
          a.download = filename;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
        }
        toast(`Exported ${label} as ${filename}`, 'success');
      });
    });
  }

  // Summarize
  const sumBtn = $('#summarize-btn');
  if (sumBtn) {
    sumBtn.addEventListener('click', async () => {
      setLoading(sumBtn, true);
      app.processingIds.set(sessionId, 'summarizing');
      renderRecordingsList();
      toast('Writing the summary…', 'info');
      dom.progressBar.classList.remove('hidden');
      dom.progressBar.classList.add('indeterminate');
      try {
        const result = await window.plaud.summarize(sessionId);
        if (result.error) {
          toast(`Summary failed: ${result.error}`, 'error');
        } else {
          toast('Summary generated', 'success');
          if (result.notion_queued) {
            // Auto-push runs in the background queue (retried on failure).
            toast('Sending to Notion in the background', 'info');
          } else if (result.notion_error) {
            toast(`Notion: ${result.notion_error}`, 'error');
          }
          if (t) {
            t.summary = result.summary;
            t.auto_title = result.title;
            t.summary_stale = false;
          }
          if (result.title && !rec.title) {
            rec.title = result.title;
            // The user may have opened another recording while this ran —
            // never write this title into (and later rename) that one.
            if (app.currentView === 'transcript' && app.currentSessionId === sessionId && titleEl.dataset.sid === sessionId) {
              titleEl.textContent = result.title;
              titleEl.setAttribute('data-placeholder', 'Add a title…');
            }
          }
          await loadRecordings();
          // Only switch tab if user is still viewing this transcript
          if (app.currentView === 'transcript' && app.currentSessionId === sessionId) {
            showTranscriptTab('summary', sessionId, rec, t);
          }
        }
      } catch (err) {
        toast(`Error: ${err.message}`, 'error');
      } finally {
        app.processingIds.delete(sessionId);
        dom.progressBar.classList.remove('indeterminate');
        hideProgress();
        setLoading(sumBtn, false);
        renderRecordingsList();
      }
    });
  }

  // Notion push / open
  const notionBtn = $('#notion-push-btn');
  const notionUpdateBtn = $('#notion-update-btn');
  // Once a page exists: the main button opens it, and "Update in Notion"
  // re-sends title, tags and the app's own section (user notes are kept).
  const showNotionPageLinked = (url) => {
    notionBtn.innerHTML = `<img src="notion-icon.svg" width="16" height="16" style="vertical-align:middle;"><span>Open in Notion</span>`;
    notionBtn.dataset.notionUrl = url;
    if (notionUpdateBtn) notionUpdateBtn.classList.remove('hidden');
  };
  // The push found a page an older version of the app made that may hold
  // this recording: let the owner use it or create a new page.
  const hideNotionReview = () => {
    const box = $('#notion-review');
    if (box) box.remove();
  };
  // `message` is shown when there are no candidate pages (Notion couldn't be
  // checked for older pages, so nothing was created).
  const showNotionReview = (candidates, message) => {
    hideNotionReview();
    const pages = (candidates || []).filter((c) => c && c.url);
    // Only while this recording is still the one shown.
    if ((!pages.length && !message) || app.currentSessionId !== sessionId || (notionBtn && !notionBtn.isConnected)) return;
    const box = document.createElement('div');
    box.id = 'notion-review';
    box.className = 'redownload-notice';
    box.style.flexWrap = 'wrap';
    const text = document.createElement('span');
    const pageLink = (c) => {
      const a = document.createElement('a');
      a.href = '#';
      a.textContent = c.title || 'the Notion page';
      a.style.color = 'inherit';
      a.style.textDecoration = 'underline';
      a.addEventListener('click', (e) => {
        e.preventDefault();
        window.plaud.openExternal(c.url);
      });
      return a;
    };
    const choose = async (btn, opts) => {
      box.querySelectorAll('button').forEach((b) => (b.disabled = true));
      setLoading(btn, true);
      try {
        reportNotionPush(await window.plaud.notionPush(sessionId, opts));
      } catch (err) {
        toast(`Error: ${err.message}`, 'error');
      } finally {
        if (btn.isConnected) {
          setLoading(btn, false);
          box.querySelectorAll('button').forEach((b) => (b.disabled = false));
        }
      }
    };
    const button = (label, opts) => {
      const b = document.createElement('button');
      b.className = 'action-btn subtle';
      b.textContent = label;
      b.addEventListener('click', () => choose(b, opts));
      return b;
    };
    if (!pages.length) {
      text.textContent = `${message} Try again later, or create a new page if it isn't in Notion yet.`;
      box.append(text);
    } else if (pages.length === 1) {
      text.append('Looks like this recording is already in Notion: ', pageLink(pages[0]), '.');
      box.append(text, button('Use that page', { link_url: pages[0].url }));
    } else {
      text.textContent = 'Looks like this recording is already in Notion, on one of these pages:';
      box.append(text);
      pages.forEach((c) => {
        const row = document.createElement('span');
        row.style.flexBasis = '100%';
        row.append(pageLink(c), ' ', button('Use this page', { link_url: c.url }));
        box.append(row);
      });
    }
    box.append(button('Create a new page', { force_new: true }));
    actionsBar.insertAdjacentElement('afterend', box);
  };
  const reportNotionPush = (result) => {
    if (result.error) {
      toast(`Notion push failed: ${result.error}`, 'error');
      return;
    }
    if (result.needs_review) {
      showNotionReview(result.candidates, result.message);
      return;
    }
    if (result.queued) {
      toast(result.message || 'Sending to Notion in the background', 'info');
      return;
    }
    hideNotionReview();
    toast(result.updated ? 'Updated in Notion' : 'Pushed to Notion!', 'success');
    if (result.content_note) toast(result.content_note, 'info');
    if (result.url) showNotionPageLinked(result.url);
  };
  hideNotionReview();
  if (notionBtn) {
    // Check if this recording already has a Notion page, or is waiting for
    // the owner to choose one (found by a background push).
    (async () => {
      try {
        const pageInfo = await window.plaud.notionPageUrl(sessionId);
        if (pageInfo.has_page) showNotionPageLinked(pageInfo.url);
        else if (pageInfo.needs_review) showNotionReview(pageInfo.candidates, pageInfo.message);
      } catch {}
    })();

    if (notionUpdateBtn) {
      notionUpdateBtn.addEventListener('click', async () => {
        setLoading(notionUpdateBtn, true);
        try {
          reportNotionPush(await window.plaud.notionPush(sessionId));
        } catch (err) {
          toast(`Error: ${err.message}`, 'error');
        } finally {
          setLoading(notionUpdateBtn, false);
        }
      });
    }

    notionBtn.addEventListener('click', async () => {
      // If page exists, open it
      if (notionBtn.dataset.notionUrl) {
        window.plaud.openExternal(notionBtn.dataset.notionUrl);
        return;
      }

      const status = await window.plaud.notionStatus();

      if (!status.connected) {
        toast('Connecting to Notion...', 'info');
        try {
          const result = await window.plaud.notionConnect();
          if (result.url) {
            window.plaud.openExternal(result.url);
            toast('Sign in with Notion in your browser. Come back here after.', 'info');
            if (_notionPollHandleTx) clearInterval(_notionPollHandleTx);
            if (_notionPollTimeoutHandleTx) clearTimeout(_notionPollTimeoutHandleTx);
            _notionPollHandleTx = setInterval(async () => {
              const s = await window.plaud.notionStatus();
              if (s.connected) {
                clearInterval(_notionPollHandleTx);
                _notionPollHandleTx = null;
                clearTimeout(_notionPollTimeoutHandleTx);
                _notionPollTimeoutHandleTx = null;
                app.notionState = s;
                toast('Notion connected!', 'success');
                if (!s.database_id) {
                  toast('Now select a database in Settings to push to.', 'info');
                  openSettings();
                  loadSettings();
                }
              }
            }, 2000);
            _notionPollTimeoutHandleTx = setTimeout(() => {
              if (_notionPollHandleTx) {
                clearInterval(_notionPollHandleTx);
                _notionPollHandleTx = null;
              }
              _notionPollTimeoutHandleTx = null;
            }, 120000);
          }
        } catch (err) {
          toast(`Error: ${err.message}`, 'error');
        }
        return;
      }

      if (!status.database_id) {
        toast('Select a Notion database first', 'info');
        openSettings();
        loadSettings();
        return;
      }

      setLoading(notionBtn, true);
      try {
        reportNotionPush(await window.plaud.notionPush(sessionId));
      } catch (err) {
        toast(`Error: ${err.message}`, 'error');
      } finally {
        setLoading(notionBtn, false);
      }
    });
  }

  // Crop audio
  closeCropEditor();
  initCropEditor(sessionId);

  // Download audio
  const dlBtn = $('#download-audio-btn');
  dlBtn.onclick = async () => {
    toast('Exporting audio...', 'info');
    try {
      const result = await window.plaud.exportRecording(sessionId);
      if (result.error) {
        toast(`Export failed: ${result.error}`, 'error');
      } else {
        toast(`Saved to ${result.path}${result.note ? ' (' + result.note + ')' : ''}`, 'success');
      }
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    }
  };

  // Delete recording — same confirmation as every other delete path
  const deleteBtn = $('#delete-recording-btn');
  if (deleteBtn) {
    deleteBtn.addEventListener('click', () => {
      showDeleteDialog([sessionId], 1, () => {
        if (app.currentSessionId === sessionId) closeTranscript();
      });
    });
  }

  // Tabs: shown whenever there's a transcript (Coaching needs one).
  // Re-opening the recording that's already on screen (a background refresh)
  // keeps the Coaching tab.
  const tabsEl = $('#transcript-tabs');
  tabsEl.classList.toggle('hidden', !hasTranscriptText);
  if (reopening && app.activeTab === 'coaching' && hasTranscriptText) {
    showTranscriptTab('coaching', sessionId, rec, t);
  } else if (hasSummary) {
    showTranscriptTab('summary', sessionId, rec, t);
  } else {
    showTranscriptTab('transcript', sessionId, rec, t);
  }

  tabsEl.querySelectorAll('.transcript-tab').forEach((tab) => {
    tab.onclick = () => showTranscriptTab(tab.dataset.tab, sessionId, rec, t);
  });
}

function showTranscriptTab(tabName, sessionId, rec, t) {
  const segContainer = $('#transcript-segments');
  const sumContainer = $('#transcript-summary');
  const coachContainer = $('#transcript-coaching');
  const tabsEl = $('#transcript-tabs');
  const hasText = !!(t && (t.text || (t.segments && t.segments.length > 0)));

  // A tab with nothing to show falls back to the transcript
  let shown = 'transcript';
  if (tabName === 'summary' && t && t.summary) shown = 'summary';
  else if (tabName === 'coaching' && hasText) shown = 'coaching';
  app.activeTab = shown;

  tabsEl.querySelectorAll('.transcript-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.tab === shown);
    // The Summary tab only when there's a summary
    if (tab.dataset.tab === 'summary') tab.classList.toggle('hidden', !(t && t.summary));
  });

  segContainer.classList.toggle('hidden', shown !== 'transcript');
  sumContainer.classList.toggle('hidden', shown !== 'summary');
  coachContainer.classList.toggle('hidden', shown !== 'coaching');
  if (shown === 'summary') {
    renderEditableSummary(sumContainer, t, sessionId, rec);
    tabsEl.classList.remove('hidden');
  } else if (shown === 'coaching') {
    renderCoachingTab(coachContainer, sessionId);
  } else {
    renderTranscriptSegments(segContainer, t, sessionId);
  }
}

function renderTranscriptSegments(container, t, sessionId) {
  if (t && t.segments && t.segments.length > 0) {
    // Recordings over an hour show every time as H:MM:SS, so the column lines up
    const lastStart = t.segments[t.segments.length - 1].start || 0;
    const withHours = (t.duration || 0) >= 3600 || lastStart >= 3600;
    const names = _txNames(t);
    container.innerHTML = t.segments
      .map(
        (seg, i) => `
      <div class="segment" data-index="${i}" data-start="${escapeHtml(seg.start)}">
        <span class="segment-time">${formatTime(seg.start, withHours)}</span>
        ${seg.speaker ? _txSpeakerButtonHtml(seg.speaker, names, _txSources(t)) : ''}
        <span class="segment-text" contenteditable="true" data-index="${i}">${escapeHtml(seg.text)}</span>
        <button class="segment-delete" data-index="${i}" title="Remove segment">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
    `,
      )
      .join('');

    // Click a speaker to name them (one handler for the list, replaced on each render)
    container.onclick = (e) => {
      const btn = e.target && e.target.closest ? e.target.closest('.segment-speaker') : null;
      if (!btn) return;
      e.stopPropagation();
      _txOpenSpeakerEditor(container, sessionId, t, btn.dataset.label, btn.closest('.segment'));
    };

    // Click timestamp to seek audio
    container.querySelectorAll('.segment-time').forEach((el) => {
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        const start = parseFloat(el.parentElement.dataset.start);
        if (!isNaN(start)) {
          dom.audioEl.currentTime = start;
          dom.audioEl.play().catch(() => {});
        }
      });
    });

    // Edit segment text — save on blur
    container.querySelectorAll('.segment-text[contenteditable]').forEach((el) => {
      el.addEventListener('blur', () => saveSegmentEdit(sessionId, t));
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          el.blur();
        }
      });
    });

    // Delete segment
    container.querySelectorAll('.segment-delete').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const idx = parseInt(btn.dataset.index);
        t.segments.splice(idx, 1);
        saveSegments(sessionId, t);
        renderTranscriptSegments(container, t, sessionId);
      });
    });
  } else if (t && t.text) {
    container.innerHTML = `<div class="segment"><span class="segment-text" contenteditable="true" data-plain="1">${escapeHtml(t.text)}</span></div>`;
    container.querySelector('.segment-text').addEventListener('blur', async (e) => {
      const newText = e.target.textContent.trim();
      if (newText !== t.text) {
        t.text = newText;
        try {
          await window.plaud.updateTranscript(sessionId, { segments: [{ text: newText, start: 0 }] });
        } catch (err) {
          if (err.name !== 'AbortError') console.warn('[transcript] updateTranscript failed:', err);
        }
      }
    });
  } else {
    // Only point at the Transcribe button when it can be used
    const rec = app.recordings.find((r) => r.session_id === sessionId);
    let hint = 'Click <strong>Transcribe</strong> above to make one.';
    if (app.processingIds.get(sessionId) === 'transcribing') hint = 'Transcribing now…';
    else if (app.processingIds.get(sessionId) === 'queued') hint = 'Waiting to be transcribed.';
    else if (rec && rec.redownload_pending) hint = 'Sync your device to download the audio again, then transcribe it.';
    else if (rec && !rec.ogg_path) hint = 'The audio isn’t on this Mac yet. Sync to download it, then transcribe it.';
    container.innerHTML = `
      <div class="transcript-empty">
        <p>No transcript yet. ${hint}</p>
      </div>
    `;
  }
}

function saveSegmentEdit(sessionId, t) {
  const els = $$('.segment-text[contenteditable]');
  let changed = false;
  els.forEach((el) => {
    const idx = parseInt(el.dataset.index);
    if (isNaN(idx) || !t.segments[idx]) return;
    const newText = el.textContent.trim();
    if (newText !== t.segments[idx].text) {
      t.segments[idx].text = newText;
      changed = true;
    }
  });
  if (changed) saveSegments(sessionId, t);
}

async function saveSegments(sessionId, t) {
  t.text = t.segments.map((s) => s.text).join(' ');
  t.text_preview = t.text.length > 150 ? t.text.slice(0, 150) + '...' : t.text;
  try {
    await window.plaud.updateTranscript(sessionId, { segments: t.segments });
  } catch (err) {
    if (err.name !== 'AbortError') console.warn('[transcript] saveSegments failed:', err);
    return;
  }
  // Update in-memory cache
  if (app.transcripts[sessionId]) {
    app.transcripts[sessionId].segments = t.segments;
    app.transcripts[sessionId].text = t.text;
    app.transcripts[sessionId].text_preview = t.text_preview;
  }
}

/* ---------- Speaker names ----------
   A transcript carries speaker_names (the names to show: the ones typed, plus your
   profile name on your own speaker once you picked it in Coaching) and
   stored_speaker_names (only the typed ones). Segments and summaries keep their
   labels; names are applied when shown, copied or exported (applySpeakerNames). */

function _txNames(t) {
  return (t && t.speaker_names && typeof t.speaker_names === 'object') ? t.speaker_names : {};
}

// Labels named by voice: { label: { source: 'voice', score } }
function _txSources(t) {
  return (t && t.speaker_name_sources && typeof t.speaker_name_sources === 'object') ? t.speaker_name_sources : {};
}

function _txStoredNames(t) {
  return (t && t.stored_speaker_names && typeof t.stored_speaker_names === 'object') ? t.stored_speaker_names : {};
}

// The names held by exactly one speaker
function _txUniqueNames(names) {
  const count = new Map();
  for (const n of Object.values(names)) count.set(n, (count.get(n) || 0) + 1);
  return Object.fromEntries(Object.entries(names).filter(([, n]) => count.get(n) === 1));
}

// Speaker labels in order of first appearance
function _txSpeakerLabels(t) {
  const seen = new Set();
  for (const s of (t && t.segments) || []) if (s && s.speaker) seen.add(s.speaker);
  return [...seen];
}

function _txSpeakerButtonHtml(label, names, sources) {
  const name = names[label];
  const byVoice = !!(name && sources && sources[label]);
  const title = byVoice ? 'Matched by voice' : name ? `${name} (${label}). Click to rename` : `${label}. Click to name this speaker`;
  return `<button type="button" class="segment-speaker${name ? ' named' : ''}" data-label="${escapeHtml(label)}" title="${escapeHtml(title)}">${escapeHtml(name || label)}</button>`;
}

// One transcript line for Copy and the text exports: "[00:04] [Sarah] Hello"
function _txSegmentLines(t, speakerFmt) {
  const names = _txNames(t);
  return (t.segments || []).map((s) => {
    const speaker = s.speaker ? speakerFmt(speakerName(s.speaker, names)) : '';
    return `[${formatTime(s.start)}] ${speaker}${s.text}`;
  });
}

/** What Copy puts on the clipboard: the summary, or the transcript, with names. */
function _txCopyText(t, summary) {
  if (summary) return applySpeakerNames(t.summary || '', _txNames(t));
  if (t.segments && t.segments.length > 0) return _txSegmentLines(t, (n) => `[${n}] `).join('\n');
  return t.text || '';
}

/**
 * An export's content with names in place of labels: { content, filename, mimeType },
 * or { html } for PDF (printed from a window). JSON keeps each segment's label and
 * adds speaker_name, plus the speaker_names map, so the file stays machine-readable.
 */
function _txExportContent(format, t, customName, safeName, isSum) {
  const names = _txNames(t);
  const summary = applySpeakerNames(t.summary || '', names);
  const hasSegments = !!(t.segments && t.segments.length > 0);
  if (format === 'json') {
    const segments = (t.segments || []).map((s) => (s && s.speaker && names[s.speaker] ? { ...s, speaker_name: names[s.speaker] } : s));
    const body = isSum
      ? { title: t.auto_title, summary, segments, speaker_names: names }
      : { text: t.text, segments, duration: t.duration, language: t.language, speakers: t.speakers, speaker_names: names };
    return { content: JSON.stringify(body, null, 2), filename: `${safeName}.json`, mimeType: 'application/json' };
  }
  if (format === 'md') {
    let content;
    if (isSum) content = `# ${t.auto_title || customName}\n\n${summary}`;
    else if (hasSegments) content = `# ${customName}\n\n${_txSegmentLines(t, (n) => `**${n}**: `).join('\n\n')}`;
    else content = `# ${customName}\n\n${t.text || ''}`;
    return { content, filename: `${safeName}.md`, mimeType: 'text/markdown' };
  }
  if (format === 'txt') {
    let content;
    if (isSum) content = `${t.auto_title || customName}\n${'='.repeat(40)}\n\n${summary}`;
    else if (hasSegments) content = _txSegmentLines(t, (n) => `[${n}] `).join('\n');
    else content = t.text || '';
    return { content, filename: `${safeName}.txt`, mimeType: 'text/plain' };
  }
  // PDF: HTML for the print window
  let html;
  if (isSum) {
    html = `<h1>${escapeHtml(t.auto_title || customName)}</h1>${renderMarkdown(summary)}`;
  } else if (hasSegments) {
    html = `<h1>${escapeHtml(customName)}</h1>` + t.segments.map((s) => {
      const speaker = s.speaker ? `<strong>${escapeHtml(speakerName(s.speaker, names))}:</strong> ` : '';
      return `<p><small>[${formatTime(s.start)}]</small> ${speaker}${escapeHtml(s.text)}</p>`;
    }).join('');
  } else {
    html = `<h1>${escapeHtml(customName)}</h1><p>${escapeHtml(t.text || '')}</p>`;
  }
  return { html };
}

// Re-transcribing found the speakers again, so the backend dropped the names: say so once.
function _txNoticeNamesCleared(sessionId, t) {
  if (!t || !t.speaker_names_cleared) return;
  t.speaker_names_cleared = false;
  if (app.transcripts[sessionId]) app.transcripts[sessionId].speaker_names_cleared = false;
  toast('Speaker names were cleared because the speakers were detected again', 'info');
  Promise.resolve()
    .then(() => window.plaud.ackSpeakerNamesCleared(sessionId))
    .catch(() => {});
}

// Put new names on every cached copy of a recording's transcript, and its list row.
function _txSetNames(sessionId, effective, stored, extra, sources) {
  const targets = new Set([app.transcripts[sessionId], extra, _txShownT && _txShownT.session_id === sessionId ? _txShownT : null]);
  for (const obj of targets) {
    if (!obj) continue;
    obj.speaker_names = { ...effective };
    obj.stored_speaker_names = { ...stored };
    if (sources) obj.speaker_name_sources = { ...sources };
  }
  const rec = app.recordings.find((r) => r.session_id === sessionId);
  if (rec) rec.participants = [...new Set(Object.values(effective))];
}

/**
 * Re-read a recording's speaker names from the backend into the cached transcript
 * (in place, so the open view sees them). Used when only the backend knows the
 * result: your own name after a pick in Coaching, or under a name just cleared.
 * Resolves true if the names were updated.
 */
async function refreshSpeakerNames(sessionId, t) {
  let list;
  try {
    list = await window.plaud.transcripts();
  } catch {
    return false;
  }
  const fresh = (list || []).find((x) => x && x.session_id === sessionId);
  if (!fresh) return false;
  _txSetNames(sessionId, fresh.speaker_names || {}, fresh.stored_speaker_names || {}, t, fresh.speaker_name_sources || {});
  return true;
}

/** True while the speaker name editor is open (a background reload shouldn't redraw then). */
function speakerNameEditorOpen() {
  return !!(_txSpeakerEditorEl && _txSpeakerEditorEl.isConnected);
}

// Take the editor off the page, keeping any held redraw (switching to another speaker)
function _txRemoveSpeakerEditor() {
  if (_txSpeakerEditorEl) _txSpeakerEditorEl.remove();
  _txSpeakerEditorEl = null;
}

// The editor is done with (Save, Clear, Cancel, Esc): remove it and run any held redraw
function _txCloseSpeakerEditor() {
  _txRemoveSpeakerEditor();
  const pending = _txPendingRedrawSid;
  _txPendingRedrawSid = null;
  if (pending) redrawOpenTranscript(pending);
}

/**
 * Redraw the recording on screen after something changed it in the background
 * (a re-download, a restore, auto-processing, speaker detection). While the
 * speaker name editor is open the redraw waits, and runs once it closes, so it
 * never throws away a name being typed.
 */
function redrawOpenTranscript(sessionId) {
  if (app.currentView !== 'transcript' || app.currentSessionId !== sessionId) return;
  if (speakerNameEditorOpen()) {
    _txPendingRedrawSid = sessionId;
    return;
  }
  openTranscript(sessionId);
}

// Why your own name is on a speaker, and how to change it
const _TX_DEFAULT_NAME_HELP =
  'This is your name because Open Pin recognised your voice, or you picked this speaker in Coaching. '
  + 'Type another name to change it.';

const _TX_PICK_MAX_ROWS = 8;
const _TX_PICK_VOICE_ICON =
  '<svg class="speaker-name-voice-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2" stroke-linecap="round" aria-label="Known by voice"><rect x="9" y="2" width="6" height="12" rx="3"/>'
  + '<path d="M5 11a7 7 0 0 0 14 0M12 18v4"/></svg>';

function _txPickLevenshtein(a, b) {
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
    prev = cur;
  }
  return prev[b.length];
}

// The known person whose name matches `typed` ignoring case, or undefined
function _txPickExact(known, typed) {
  const low = typed.trim().toLowerCase();
  return known.find((p) => p.name.toLowerCase() === low);
}

/**
 * The known name to offer instead of `typed`, or null. Offered when typed is a single
 * word that starts exactly one longer known name ("will" for "Will Moss"), or is
 * within Levenshtein distance 2 of a known name. The closest wins; ties go to the most used.
 */
function _txPickDidYouMean(typed, known) {
  const low = typed.trim().toLowerCase();
  if (!low || _txPickExact(known, low)) return null;
  const first = /\s/.test(low) ? [] : known.filter((p) => /\s/.test(p.name.trim()) && p.name.trim().split(/\s+/)[0].toLowerCase() === low);
  const scored = first.length === 1
    ? [{ p: first[0], d: 0 }]
    : known.map((p) => ({ p, d: _txPickLevenshtein(low, p.name.toLowerCase()) })).filter((x) => x.d <= 2);
  scored.sort((x, y) => x.d - y.d || (y.p.recordings || 0) - (x.p.recordings || 0));
  return scored.length ? scored[0].p.name : null;
}

// Names that match what is typed (as listed: people known by voice first), at most 8
function _txPickMatches(known, typed) {
  const low = typed.trim().toLowerCase();
  return known.filter((p) => p.name.toLowerCase().includes(low)).slice(0, _TX_PICK_MAX_ROWS);
}

// The suggestion rows: the matching names, then "Add new person" unless typed is a known name
function _txPickRowsHtml(known, typed, active) {
  const rows = _txPickMatches(known, typed).map((p) => ({ attr: `data-pick="${escapeHtml(p.name)}"`, cls: '',
    body: `<span>${escapeHtml(p.name)}</span>${p.has_voice ? _TX_PICK_VOICE_ICON : ''}` }));
  const name = typed.trim();
  if (name && !_txPickExact(known, name)) {
    rows.push({ attr: 'data-pick-new="1"', cls: ' speaker-name-option-new', body: `Add new person: “${escapeHtml(name)}”` });
  }
  return rows
    .map((r, i) => `<div class="speaker-name-option${r.cls}" role="option" id="speaker-name-opt-${i}" ${r.attr}`
      + `${i === active ? ' aria-selected="true"' : ''}>${r.body}</div>`)
    .join('');
}

function _txSpeakerEditorHtml(label, current, isDefault, hasStored, byVoice) {
  const hint = isDefault ? _TX_DEFAULT_NAME_HELP : 'Pick a name from the list or type a new one, with the full name if you can.';
  const clearTitle = hasStored ? 'Remove this name' : 'Only a name you typed can be cleared';
  const voice = byVoice
    ? '<p class="speaker-name-voice">Matched by voice. <button type="button" class="action-btn subtle" data-speaker-action="reject">Not them</button></p>'
    : '';
  return `
    <label class="speaker-name-label" for="speaker-name-input">Name for <strong>${escapeHtml(label)}</strong></label>
    <div class="speaker-name-field">
      <input type="text" id="speaker-name-input" class="speaker-name-input" role="combobox" aria-autocomplete="list"
        aria-expanded="true" aria-controls="speaker-name-list" maxlength="80"
        autocomplete="off" spellcheck="false" placeholder="Full name, such as Harry Fisher" value="${escapeHtml(current)}">
      <div id="speaker-name-list" class="speaker-name-list" role="listbox" aria-label="People"></div>
    </div>
    <div class="speaker-name-actions">
      <button type="button" class="action-btn subtle" data-speaker-action="cancel">Cancel</button>
      <button type="button" class="action-btn subtle" data-speaker-action="clear" title="${clearTitle}"${hasStored ? '' : ' disabled'}>Clear</button>
      <button type="button" class="action-btn accent" data-speaker-action="save">Save</button>
    </div>
    ${voice}
    <p class="speaker-name-dym" hidden></p>
    <p class="speaker-name-hint">${escapeHtml(hint)}</p>
    <p class="speaker-name-error" role="alert" hidden></p>`;
}

/**
 * The change to send for `typed` on `label`: { label: name }, { label: null } to clear
 * a typed name, or null when there's nothing to save (unchanged, your default name
 * kept as it is, or empty with no typed name).
 */
function _txSpeakerNameChange(t, label, typed) {
  const name = String(typed || '').trim();
  const stored = _txStoredNames(t)[label];
  if (!name) return stored ? { [label]: null } : null;
  if (name === stored) return null;
  if (!stored && name === _txNames(t)[label]) return null;
  return { [label]: name };
}

function _txOpenSpeakerEditor(container, sessionId, t, label, row) {
  // Not _txCloseSpeakerEditor: a held redraw would re-render the rows and detach `row`
  _txRemoveSpeakerEditor();
  if (!t || !label || !row) {
    _txCloseSpeakerEditor(); // no new editor after all: let a held redraw run
    return;
  }
  const current = _txNames(t)[label] || '';
  const hasStored = !!_txStoredNames(t)[label];
  const byVoice = !!current && !hasStored && !!_txSources(t)[label];
  const editor = document.createElement('div');
  editor.className = 'speaker-name-editor';
  editor.setAttribute('role', 'group');
  editor.setAttribute('aria-label', `Name ${label}`);
  editor.innerHTML = _txSpeakerEditorHtml(label, current, !!current && !hasStored, hasStored, byVoice);
  row.insertAdjacentElement('afterend', editor);
  _txSpeakerEditorEl = editor;

  const input = editor.querySelector('.speaker-name-input');
  const listEl = editor.querySelector('.speaker-name-list');
  const dymEl = editor.querySelector('.speaker-name-dym');
  const errorEl = editor.querySelector('.speaker-name-error');
  input.value = current;
  input.focus();
  input.select();

  // The picker: everyone known (people with a voice first), filtered by what you type.
  // Until you type, the list is unfiltered, so your current name doesn't hide the rest.
  let known = [];
  let query = '';
  let active = -1;
  let listOpen = !byVoice; // a matched name's editor starts with the list closed, so "Not them" isn't covered
  const showList = () => {
    const html = listOpen ? _txPickRowsHtml(known, query, active) : '';
    listEl.innerHTML = html;
    listEl.hidden = !html;
    input.setAttribute('aria-expanded', String(!!html));
    if (active >= 0) input.setAttribute('aria-activedescendant', `speaker-name-opt-${active}`);
    else input.removeAttribute('aria-activedescendant');
  };
  const rowCount = () => _txPickMatches(known, query).length + (query.trim() && !_txPickExact(known, query) ? 1 : 0);
  Promise.resolve()
    .then(() => window.plaud.people())
    .then((people) => {
      if (!Array.isArray(people)) return;
      known = people.filter((p) => p && typeof p.name === 'string' && p.name.trim());
      showList();
    })
    .catch(() => {});

  let busy = false;
  const fail = (message) => {
    busy = false;
    errorEl.textContent = message;
    errorEl.hidden = false;
    editor.querySelectorAll('button[data-speaker-action]').forEach((b) => {
      b.disabled = b.dataset.speakerAction === 'clear' && !_txStoredNames(t)[label];
    });
    input.focus();
  };
  const reject = async () => {
    busy = true;
    editor.querySelectorAll('button').forEach((b) => (b.disabled = true));
    let res;
    try {
      res = await window.plaud.rejectVoiceMatch(sessionId, label);
    } catch (err) {
      res = { error: `Couldn’t do that: ${(err && err.message) || 'no response'}` };
    }
    if (!res || res.error || !res.speaker_names || typeof res.speaker_names !== 'object') {
      fail((res && res.error) || 'Couldn’t do that');
      return;
    }
    _txSetNames(sessionId, res.speaker_names, _txStoredNames(t), t, res.speaker_name_sources || {});
    _txCloseSpeakerEditor();
    _txRedrawSegments(container, t, sessionId);
  };

  // action: save, clear, cancel, reject, use-known (the offered name), keep-typed (no question asked)
  const act = async (action, name) => {
    if (busy) return;
    if (action === 'cancel') {
      _txCloseSpeakerEditor();
      return;
    }
    if (action === 'reject') {
      await reject();
      return;
    }
    if (action === 'use-known') input.value = name;
    let change;
    if (action === 'clear') change = _txStoredNames(t)[label] ? { [label]: null } : null;
    else change = _txSpeakerNameChange(t, label, input.value);
    if (!change && action === 'save' && !String(input.value || '').trim() && _txNames(t)[label] && !_txStoredNames(t)[label]) {
      // Emptied your own name: it can't be cleared here, so say how to change it
      errorEl.textContent = _TX_DEFAULT_NAME_HELP;
      errorEl.hidden = false;
      input.focus();
      return;
    }
    if (change && change[label] && action !== 'keep-typed') {
      // An exact match ignoring case keeps the known spelling; a near miss gets a question
      const exact = _txPickExact(known, change[label]);
      const maybe = exact ? null : _txPickDidYouMean(change[label], known);
      if (exact) change = _txSpeakerNameChange(t, label, exact.name);
      else if (maybe) {
        dymEl.innerHTML = `Did you mean ${escapeHtml(maybe)}? `
          + `<button type="button" class="action-btn subtle" data-speaker-action="use-known" data-speaker-name="${escapeHtml(maybe)}">${escapeHtml(maybe)}</button> `
          + `<button type="button" class="action-btn subtle" data-speaker-action="keep-typed">Keep “${escapeHtml(change[label])}”</button>`;
        dymEl.hidden = false;
        listEl.hidden = true;
        return;
      }
    }
    if (!change) {
      _txCloseSpeakerEditor();
      return;
    }
    busy = true;
    editor.querySelectorAll('button').forEach((b) => (b.disabled = true));
    await _txSaveSpeakerName(container, sessionId, t, change, fail);
  };

  editor.addEventListener('click', (e) => {
    e.stopPropagation(); // not a click on the transcript behind it
    const find = (sel) => (e.target && e.target.closest ? e.target.closest(sel) : null);
    const btn = find('[data-speaker-action]');
    const pick = find('[data-pick]');
    if (btn && !btn.disabled) act(btn.dataset.speakerAction, btn.dataset.speakerName);
    else if (pick) {
      input.value = pick.dataset.pick;
      act('save');
    } else if (find('[data-pick-new]')) act('keep-typed');
  });
  input.addEventListener('input', () => {
    query = input.value;
    listOpen = true;
    active = -1;
    dymEl.hidden = true;
    showList();
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const matches = _txPickMatches(known, query);
      if (active >= 0 && active < matches.length) {
        input.value = matches[active].name;
        act('save');
      } else act(active >= 0 ? 'keep-typed' : 'save');
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      listOpen = true;
      const n = rowCount();
      if (!n) return;
      if (!rowCount()) return;
      active = e.key === 'ArrowDown' ? (active + 1) % n : (active - 1 + n) % n;
      showList();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      act('cancel');
    }
  });
}

// Redraw the segments after a name change, unless the view has moved on or an editor is open
function _txRedrawSegments(container, t, sessionId) {
  if (app.currentView === 'transcript' && app.currentSessionId === sessionId && app.activeTab === 'transcript' && !speakerNameEditorOpen()) {
    renderTranscriptSegments(container, t, sessionId);
  }
}

// Send a name change; on success update every copy of the names and redraw the
// transcript, otherwise hand the message to onError (the editor stays open).
async function _txSaveSpeakerName(container, sessionId, t, change, onError) {
  let res;
  try {
    res = await window.plaud.setSpeakerNames(sessionId, change);
  } catch (err) {
    res = { error: `Couldn’t save the name: ${(err && err.message) || 'no response'}` };
  }
  if (!res || res.error || !res.speaker_names || typeof res.speaker_names !== 'object') {
    onError((res && res.error) || 'Couldn’t save the name');
    return;
  }
  // The names shown: the stored ones, else your own name where it was shown by default
  const stored = res.speaker_names;
  const before = _txNames(t);
  const beforeStored = _txStoredNames(t);
  const effective = {};
  for (const label of _txSpeakerLabels(t)) {
    if (stored[label]) effective[label] = stored[label];
    else if (before[label] && !beforeStored[label]) effective[label] = before[label];
  }
  // A name you typed is no longer a voice match
  const sources = { ..._txSources(t) };
  for (const [label, name] of Object.entries(change)) if (name) delete sources[label];
  _txSetNames(sessionId, effective, stored, t, sources);
  _txCloseSpeakerEditor();
  _txRedrawSegments(container, t, sessionId);
  // A cleared name may have hidden your own name, which only the backend can tell
  if (Object.values(change).includes(null) && (await refreshSpeakerNames(sessionId, t))) _txRedrawSegments(container, t, sessionId);
}

function renderEditableSummary(container, t, sessionId, rec) {
  let editing = false;
  // Stored in label form; shown, and edited, with the speakers' names
  const render = () => {
    container.innerHTML = renderMarkdown(applySpeakerNames(t.summary, _txNames(t)));
    // Add edit hint
    const editHint = document.createElement('button');
    editHint.className = 'summary-edit-btn';
    editHint.textContent = 'Edit';
    editHint.addEventListener('click', () => startEditing());
    container.prepend(editHint);
    if (t.summary_stale) {
      // Transcript changed (e.g. re-transcribed) after this summary was made
      const stale = document.createElement('div');
      stale.className = 'summary-stale-hint';
      const msg = document.createElement('span');
      msg.textContent = 'Summary may be out of date — ';
      const link = document.createElement('button');
      link.type = 'button';
      link.className = 'summary-stale-action';
      link.textContent = 'Re-summarise';
      link.addEventListener('click', () => {
        const sb = document.getElementById('summarize-btn');
        if (sb && !sb.disabled && app.currentSessionId === sessionId) sb.click();
      });
      stale.append(msg, link);
      container.prepend(stale);
    }
  };

  const startEditing = () => {
    editing = true;
    // Only names one speaker holds: a name two speakers share (one voice split in two)
    // can't be turned back into a label on save, so those speakers stay as labels here.
    const names = _txUniqueNames(_txNames(t));
    const shown = applySpeakerNames(t.summary, names);
    const textarea = document.createElement('textarea');
    textarea.className = 'summary-editor';
    textarea.value = shown;
    textarea.rows = Math.max(10, shown.split('\n').length + 2);
    let hint = null;
    if (Object.keys(names).length) {
      hint = document.createElement('p');
      hint.className = 'summary-editor-hint';
      hint.textContent = 'Speaker names are saved as their speaker labels, so renaming a speaker later updates this summary too.';
    }

    const actions = document.createElement('div');
    actions.className = 'summary-editor-actions';
    const saveBtn = document.createElement('button');
    saveBtn.className = 'action-btn accent';
    saveBtn.textContent = 'Save';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'action-btn subtle';
    cancelBtn.textContent = 'Cancel';

    actions.append(cancelBtn, saveBtn);
    container.innerHTML = '';
    container.append(...[textarea, hint, actions].filter(Boolean));
    textarea.focus();

    saveBtn.addEventListener('click', async () => {
      const newSummary = textarea.value.trim();
      if (newSummary !== shown) {
        // The backend turns the names back into labels when it saves; do the same here
        const stored = speakerNamesToLabels(newSummary, names);
        if (newSummary) {
          t.summary = stored;
        } else {
          delete t.summary;
          if (app.transcripts[sessionId]) {
            delete app.transcripts[sessionId].summary;
          }
        }
        try {
          // With the names this editor showed: an AI tool may rename, clear or share one
          // while it is open, and the backend must reverse what the user saw.
          await window.plaud.updateTranscript(sessionId, { summary: newSummary || null, summary_names: names });
          if (newSummary && app.transcripts[sessionId]) {
            app.transcripts[sessionId].summary = stored;
          }
        } catch (err) {
          if (err.name !== 'AbortError') console.warn('[transcript] save summary failed:', err);
        }
      }
      editing = false;
      if (!t.summary) {
        showTranscriptTab('transcript', sessionId, rec, t);
      } else {
        render();
      }
    });

    cancelBtn.addEventListener('click', () => {
      editing = false;
      render();
    });

    textarea.addEventListener('keydown', (e) => {
      if (e.key === 's' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        saveBtn.click();
      }
      if (e.key === 'Escape') {
        cancelBtn.click();
      }
    });
  };

  render();
}

async function transcribeRecording(oggPath, sessionId) {
  app.processingIds.set(sessionId, 'transcribing');
  renderRecordingsList();

  const rec = app.recordings.find((r) => r.session_id === sessionId);
  const audioDuration = rec ? rec.duration : 0;

  let estimateMsg = 'Transcribing...';
  if (app.transcribeSpeed && audioDuration > 0) {
    const estSeconds = Math.ceil(audioDuration / app.transcribeSpeed);
    estimateMsg =
      estSeconds < 60 ? `Transcribing... ~${estSeconds}s` : `Transcribing... ~${Math.ceil(estSeconds / 60)}m`;
  }
  toast(estimateMsg, 'info');

  const speed = app.transcribeSpeed || 15;
  const estSeconds = audioDuration > 0 ? audioDuration / speed : 30;

  dom.progressBar.classList.remove('hidden');
  dom.progressFill.style.transition = 'none';
  dom.progressFill.style.width = '0%';
  dom.progressFill.offsetHeight;
  dom.progressFill.style.transition = `width ${estSeconds}s cubic-bezier(0.1, 0.4, 0.2, 0.9)`;
  dom.progressFill.style.width = '90%';

  const startTime = Date.now();
  try {
    const result = await window.plaud.transcribe(
      oggPath,
      app.settings.model || 'small',
      app.settings.diarize || false,
      app.settings.language || 'auto',
    );

    // The job is over: clear the flag before anything re-renders, or
    // openTranscript() below would draw a disabled "Transcribing…" button.
    app.processingIds.delete(sessionId);

    const elapsed = (Date.now() - startTime) / 1000;
    if (audioDuration > 0 && elapsed > 0) {
      app.transcribeSpeed = audioDuration / elapsed;
    }

    if (result.error) {
      toast(`Transcription failed: ${result.error}`, 'error');
    } else {
      dom.progressFill.style.transition = 'width 0.3s ease';
      dom.progressFill.style.width = '100%';
      await new Promise((r) => setTimeout(r, 400));
      if (result.warning) toast(result.warning, 'info');
      toast('Transcription complete', 'success');
      await loadRecordings();
      await loadSettings();
      // Only auto-navigate if user is on the recordings list or already viewing this transcript
      if (app.currentView === 'recordings' || (app.currentView === 'transcript' && app.currentSessionId === sessionId)) {
        openTranscript(sessionId);
      }

      // Speaker labels couldn't run (e.g. the model failed to install): say why.
      if (result.diarization_message) toast(result.diarization_message, 'info');

      if (result.diarizing) {
        toast('Speaker detection running in background...', 'info');
        // Cancel any prior diarization poll before starting a new one — otherwise
        // closing+re-transcribing leaves an orphan that can force-navigate the
        // user back to this session whenever the speakers field appears.
        if (_diarPollHandleTx) clearInterval(_diarPollHandleTx);
        if (_diarPollTimeoutHandleTx) clearTimeout(_diarPollTimeoutHandleTx);
        const stopDiarPoll = () => {
          if (_diarPollHandleTx) clearInterval(_diarPollHandleTx);
          if (_diarPollTimeoutHandleTx) clearTimeout(_diarPollTimeoutHandleTx);
          _diarPollHandleTx = null;
          _diarPollTimeoutHandleTx = null;
        };
        _diarPollHandleTx = setInterval(async () => {
          try {
            const transcriptData = await window.plaud.transcripts();
            const t = transcriptData.find((tr) => tr.session_id === sessionId);
            if (!t) return;
            // The backend sets diarization_status to done / none / failed when
            // the background job ends ("running" until then).
            const status = t.diarization_status || '';
            const finished = (status && status !== 'running') || (t.speakers && t.speakers.length > 0);
            if (!finished) return;
            stopDiarPoll();
            app.transcripts[sessionId] = t;
            if (t.speakers && t.speakers.length > 0) {
              toast(`Speaker detection complete: ${t.speakers.length} speaker(s) found`, 'success');
              redrawOpenTranscript(sessionId);
            } else {
              toast(t.diarization_message || 'No speakers detected', 'info');
            }
          } catch {}
        }, 5000);
        // Safety net only (the status above normally ends the poll): long
        // recordings plus a first-run model install can take a while on CPU.
        _diarPollTimeoutHandleTx = setTimeout(stopDiarPoll, 30 * 60 * 1000);
      }
    }
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  } finally {
    app.processingIds.delete(sessionId);
    dom.progressFill.style.transition = '';
    hideProgress();
    renderRecordingsList();
    _txRefreshRetranscribeBtn(sessionId);
  }
}

// Bring the open transcript's (Re-)transcribe button in line with the current
// state. No-op unless that recording's transcript is on screen.
function _txRefreshRetranscribeBtn(sessionId) {
  if (app.currentView !== 'transcript' || app.currentSessionId !== sessionId) return;
  const btn = $('#retranscribe-btn');
  if (!btn) return;
  const rec = app.recordings.find((r) => r.session_id === sessionId);
  const t = app.transcripts[sessionId];
  const hasText = !!(t && (t.text || (t.segments && t.segments.length > 0)));
  const busy = app.processingIds.get(sessionId) === 'transcribing';
  btn.disabled = busy || !rec || !rec.ogg_path || !!rec.redownload_pending;
  const label = btn.querySelector('span');
  if (label) label.textContent = busy ? 'Transcribing…' : hasText ? 'Re-transcribe' : 'Transcribe';
}

// True when a recording has no audio the player can load: no local .ogg
// (re-download pending, incomplete transfer) and not streamable from Drive.
function _txHasNoAudio(rec) {
  if (!rec) return true;
  if (rec.partial || rec.redownload_pending) return true;
  return !rec.ogg_path && !rec.cloud_only;
}

const _TX_AUDIO_CONTROL_TITLES = {
  'play-btn': 'Play',
  'crop-audio-btn': 'Crop audio',
  'download-audio-btn': 'Download audio',
};

function _txSetAudioControlsEnabled(enabled) {
  for (const [id, title] of Object.entries(_TX_AUDIO_CONTROL_TITLES)) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.disabled = !enabled;
    el.title = enabled ? title : 'No local audio — sync to download it first';
  }
  const player = document.getElementById('audio-player');
  if (player) player.classList.toggle('audio-unavailable', !enabled);
}

function closeTranscript() {
  if (_exportDropdownCleanup) {
    document.removeEventListener('click', _exportDropdownCleanup);
    _exportDropdownCleanup = null;
  }
  if (_notionPollHandleTx) {
    clearInterval(_notionPollHandleTx);
    _notionPollHandleTx = null;
  }
  if (_notionPollTimeoutHandleTx) {
    clearTimeout(_notionPollTimeoutHandleTx);
    _notionPollTimeoutHandleTx = null;
  }
  if (_diarPollHandleTx) {
    clearInterval(_diarPollHandleTx);
    _diarPollHandleTx = null;
  }
  if (_diarPollTimeoutHandleTx) {
    clearTimeout(_diarPollTimeoutHandleTx);
    _diarPollTimeoutHandleTx = null;
  }
  const titleEl = $('#transcript-title');
  if (titleEl && document.activeElement === titleEl) {
    titleEl.blur();
  }
  app.currentView = 'recordings';
  app.currentSessionId = null;
  dom.transcriptView.classList.add('hidden');
  dom.recordingsList.classList.remove('hidden');
  dom.audioEl.pause();
  dom.audioEl.src = '';
  if (app.recordings.length === 0) {
    dom.emptyState.classList.remove('hidden');
  }
  renderRecordingsList();
}

function initAudioPlayer() {
  // Track the previously active segment to avoid O(n) scans on every timeupdate
  let _lastActiveSegment = null;

  dom.audioEl.addEventListener('timeupdate', () => {
    if (dom.audioEl.duration) {
      const pct = (dom.audioEl.currentTime / dom.audioEl.duration) * 100;
      dom.playerFill.style.width = `${pct}%`;
      dom.playerCurrent.textContent = formatTime(dom.audioEl.currentTime);

      // Efficient active segment tracking — check current/next instead of scanning all
      const t = dom.audioEl.currentTime;
      const segments = $$('.segment');
      if (segments.length === 0) return;

      // If current segment is still active, skip the search.
      // isConnected guards against a stale reference after the transcript
      // was re-rendered (e.g. the user switched to a different recording).
      if (_lastActiveSegment && _lastActiveSegment.isConnected) {
        const start = parseFloat(_lastActiveSegment.dataset.start);
        const nextEl = _lastActiveSegment.nextElementSibling;
        const nextStart = nextEl ? parseFloat(nextEl.dataset.start) : Infinity;
        if (t >= start && t < nextStart) return; // Still in the same segment
        _lastActiveSegment.classList.remove('active');
      } else {
        _lastActiveSegment = null;
      }

      // Binary search for the active segment
      let lo = 0, hi = segments.length - 1, found = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const midStart = parseFloat(segments[mid].dataset.start);
        if (midStart <= t) {
          found = mid;
          lo = mid + 1;
        } else {
          hi = mid - 1;
        }
      }

      if (found >= 0) {
        segments[found].classList.add('active');
        _lastActiveSegment = segments[found];
      } else {
        _lastActiveSegment = null;
      }
    }
  });

  dom.audioEl.addEventListener('loadedmetadata', () => {
    dom.playerDuration.textContent = formatTime(dom.audioEl.duration);
  });

  dom.audioEl.addEventListener('play', () => {
    dom.playIcon.classList.add('hidden');
    dom.pauseIcon.classList.remove('hidden');
  });

  dom.audioEl.addEventListener('pause', () => {
    dom.playIcon.classList.remove('hidden');
    dom.pauseIcon.classList.add('hidden');
  });

  dom.audioEl.addEventListener('ended', () => {
    dom.playIcon.classList.remove('hidden');
    dom.pauseIcon.classList.add('hidden');
    dom.playerFill.style.width = '0%';
  });

  dom.playBtn.addEventListener('click', () => {
    if (dom.audioEl.paused) {
      dom.audioEl.play().catch(() => {});
    } else {
      dom.audioEl.pause();
    }
  });

  dom.playerProgress.addEventListener('click', (e) => {
    if (dom.audioEl.duration) {
      const rect = dom.playerProgress.getBoundingClientRect();
      const pct = (e.clientX - rect.left) / rect.width;
      dom.audioEl.currentTime = pct * dom.audioEl.duration;
    }
  });
}

/* ============================================================
   Audio Crop Editor
   ============================================================ */

let cropState = null;

// Append a cache-busting param to a URL that may already have a query string.
function withCacheBust(url, stamp = Date.now()) {
  const u = String(url);
  return `${u}${u.includes('?') ? '&' : '?'}t=${stamp}`;
}

function initCropEditor(sessionId) {
  const cropBtn = $('#crop-audio-btn');
  const editor = $('#crop-editor');

  cropBtn.onclick = () => {
    if (!editor.classList.contains('hidden')) {
      closeCropEditor();
      return;
    }
    openCropEditor(sessionId);
  };
}

async function openCropEditor(sessionId) {
  const editor = $('#crop-editor');
  const canvas = $('#crop-waveform');
  const audio = dom.audioEl;

  if (!audio.duration) {
    toast('Audio not loaded yet', 'error');
    return;
  }

  editor.classList.remove('hidden');
  $('#crop-audio-btn').classList.add('active');

  const duration = audio.duration;

  cropState = {
    sessionId,
    duration,
    startPct: 0,
    endPct: 1,
    dragging: null,
  };

  // Draw waveform
  await drawWaveform(canvas, audio.src);

  // Set initial region
  updateCropRegion();

  // Handle dragging
  const container = canvas.parentElement;

  const onPointerDown = (e) => {
    const leftHandle = $('#crop-handle-left');
    const rightHandle = $('#crop-handle-right');
    if (leftHandle.contains(e.target)) {
      cropState.dragging = 'left';
    } else if (rightHandle.contains(e.target)) {
      cropState.dragging = 'right';
    } else {
      return;
    }
    e.preventDefault();
    document.addEventListener('pointermove', onPointerMove);
    document.addEventListener('pointerup', onPointerUp);
  };

  const onPointerMove = (e) => {
    if (!cropState || !cropState.dragging) return;
    const rect = container.getBoundingClientRect();
    let pct = (e.clientX - rect.left) / rect.width;
    pct = Math.max(0, Math.min(1, pct));

    if (cropState.dragging === 'left') {
      cropState.startPct = Math.min(pct, cropState.endPct - 0.01);
    } else {
      cropState.endPct = Math.max(pct, cropState.startPct + 0.01);
    }
    updateCropRegion();
  };

  const onPointerUp = () => {
    if (cropState) cropState.dragging = null;
    document.removeEventListener('pointermove', onPointerMove);
    document.removeEventListener('pointerup', onPointerUp);
  };

  container.addEventListener('pointerdown', onPointerDown);
  cropState._cleanup = () => {
    container.removeEventListener('pointerdown', onPointerDown);
    document.removeEventListener('pointermove', onPointerMove);
    document.removeEventListener('pointerup', onPointerUp);
  };

  // Playhead tracking during preview
  const onTimeUpdate = () => {
    if (!cropState) return;
    const playhead = $('#crop-playhead');
    const pct = audio.currentTime / cropState.duration;
    if (pct >= cropState.startPct && pct <= cropState.endPct) {
      playhead.style.display = 'block';
      const regionPct = (pct - cropState.startPct) / (cropState.endPct - cropState.startPct);
      playhead.style.left = `${regionPct * 100}%`;
    } else {
      playhead.style.display = 'none';
    }
  };
  audio.addEventListener('timeupdate', onTimeUpdate);
  cropState._cleanupPlayhead = () => audio.removeEventListener('timeupdate', onTimeUpdate);

  // Button handlers
  $('#crop-preview-btn').onclick = () => {
    if (!cropState) return;
    audio.currentTime = cropState.startPct * cropState.duration;
    audio.play().catch(() => {});
    // Stop at end of crop region
    const checkEnd = () => {
      if (!cropState) { audio.removeEventListener('timeupdate', checkEnd); return; }
      if (audio.currentTime >= cropState.endPct * cropState.duration) {
        audio.pause();
        audio.removeEventListener('timeupdate', checkEnd);
      }
    };
    audio.addEventListener('timeupdate', checkEnd);
  };

  $('#crop-cancel-btn').onclick = closeCropEditor;

  // Save crop dropdown
  const saveBtn = $('#crop-save-btn');
  const saveMenu = $('#crop-save-menu');

  saveBtn.onclick = () => {
    saveMenu.classList.toggle('hidden');
  };

  // Close menu when clicking outside
  const closeSaveMenu = (e) => {
    if (!saveMenu.contains(e.target) && e.target !== saveBtn) {
      saveMenu.classList.add('hidden');
    }
  };
  document.addEventListener('click', closeSaveMenu);
  cropState._cleanupMenu = () => document.removeEventListener('click', closeSaveMenu);

  const executeCrop = async (mode) => {
    if (!cropState) return;
    const startTime = cropState.startPct * cropState.duration;
    const endTime = cropState.endPct * cropState.duration;

    if (endTime - startTime < 1) {
      toast('Selection too short (min 1 second)', 'error');
      return;
    }

    saveMenu.classList.add('hidden');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';

    try {
      const result = await window.plaud.cropRecording(cropState.sessionId, startTime, endTime, mode);
      if (result.error) {
        toast(`Crop failed: ${result.error}`, 'error');
      } else {
        toast(mode === 'new' ? 'Saved as new recording' : 'Audio cropped successfully', 'success');
        closeCropEditor();
        await loadRecordings();
        const targetId = result.session_id || sessionId;
        if (result.session_id && result.session_id !== sessionId) {
          openTranscript(result.session_id);
        } else if (app.currentView === 'transcript' && app.currentSessionId === sessionId) {
          // Overwrite removed the transcript — re-render so the view matches
          openTranscript(sessionId);
        }
        // Reload audio, bypassing WebKit's cache. audioUrl() already carries
        // query params (?format=m4a, &token=…), so append rather than add '?'.
        if (app.currentSessionId === targetId) {
          audio.src = withCacheBust(window.plaud.audioUrl(`${targetId}.ogg`));
          audio.load();
        }
      }
    } catch (err) {
      toast(`Error: ${err.message}`, 'error');
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save crop';
    }
  };

  $('#crop-save-overwrite').onclick = () => executeCrop('overwrite');
  $('#crop-save-new').onclick = () => executeCrop('new');
}

function closeCropEditor() {
  const editor = $('#crop-editor');
  editor.classList.add('hidden');
  $('#crop-audio-btn').classList.remove('active');
  if (cropState) {
    if (cropState._cleanup) cropState._cleanup();
    if (cropState._cleanupPlayhead) cropState._cleanupPlayhead();
    if (cropState._cleanupMenu) cropState._cleanupMenu();
    cropState = null;
  }
  $('#crop-save-menu').classList.add('hidden');
  $('#crop-playhead').style.display = 'none';
}

function updateCropRegion() {
  if (!cropState) return;
  const region = $('#crop-region');
  const overlayLeft = $('#crop-overlay-left');
  const overlayRight = $('#crop-overlay-right');

  region.style.left = `${cropState.startPct * 100}%`;
  region.style.width = `${(cropState.endPct - cropState.startPct) * 100}%`;
  overlayLeft.style.width = `${cropState.startPct * 100}%`;
  overlayRight.style.width = `${(1 - cropState.endPct) * 100}%`;

  $('#crop-start-time').textContent = formatTime(cropState.startPct * cropState.duration);
  $('#crop-end-time').textContent = formatTime(cropState.endPct * cropState.duration);
}

async function drawWaveform(canvas, audioSrc) {
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, rect.width, rect.height);

  let audioContext = null;
  try {
    audioContext = new (window.AudioContext || window.webkitAudioContext)();
    const response = await fetch(audioSrc);
    const arrayBuffer = await response.arrayBuffer();
    const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);

    const channelData = audioBuffer.getChannelData(0);
    const barCount = Math.floor(rect.width / 3);
    const samplesPerBar = Math.floor(channelData.length / barCount);
    const barWidth = 2;
    const gap = 1;
    const midY = rect.height / 2;
    const maxAmp = rect.height / 2 - 4;

    // Compute peaks
    const peaks = [];
    for (let i = 0; i < barCount; i++) {
      let peak = 0;
      const start = i * samplesPerBar;
      for (let j = start; j < start + samplesPerBar && j < channelData.length; j++) {
        const abs = Math.abs(channelData[j]);
        if (abs > peak) peak = abs;
      }
      peaks.push(peak);
    }

    // Normalize
    const maxPeak = Math.max(...peaks, 0.01);

    // Get accent color from CSS
    const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();

    for (let i = 0; i < barCount; i++) {
      const amp = (peaks[i] / maxPeak) * maxAmp;
      const h = Math.max(2, amp);
      ctx.fillStyle = accent;
      ctx.globalAlpha = 0.7;
      ctx.beginPath();
      // roundRect polyfill for Safari <16 (macOS Monterey)
      const bx = i * (barWidth + gap), by = midY - h, bw = barWidth, bh = h * 2, br = 1;
      if (ctx.roundRect) {
        ctx.roundRect(bx, by, bw, bh, br);
      } else {
        ctx.moveTo(bx + br, by);
        ctx.lineTo(bx + bw - br, by);
        ctx.quadraticCurveTo(bx + bw, by, bx + bw, by + br);
        ctx.lineTo(bx + bw, by + bh - br);
        ctx.quadraticCurveTo(bx + bw, by + bh, bx + bw - br, by + bh);
        ctx.lineTo(bx + br, by + bh);
        ctx.quadraticCurveTo(bx, by + bh, bx, by + bh - br);
        ctx.lineTo(bx, by + br);
        ctx.quadraticCurveTo(bx, by, bx + br, by);
      }
      ctx.fill();
    }

    ctx.globalAlpha = 1;
  } catch {
    // Fallback: draw placeholder bars
    const barCount = Math.floor(rect.width / 3);
    const midY = rect.height / 2;
    const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
    ctx.fillStyle = accent;
    ctx.globalAlpha = 0.3;
    for (let i = 0; i < barCount; i++) {
      const h = 4 + Math.random() * 20;
      ctx.fillRect(i * 3, midY - h, 2, h * 2);
    }
    ctx.globalAlpha = 1;
  } finally {
    // WKWebView caps simultaneous AudioContexts at 6; closing in finally
    // prevents the editor from breaking after repeated crop-open failures.
    if (audioContext) {
      try { audioContext.close(); } catch {}
    }
  }
}
