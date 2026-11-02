/* ============================================================
   Plaud V2 — Transcript View Module
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

function openTranscript(sessionId, autoPlay = false) {
  const rec = app.recordings.find((r) => r.session_id === sessionId);
  if (!rec) return;

  app.currentSessionId = sessionId;
  const t = app.transcripts[sessionId];
  app.currentView = 'transcript';

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
  titleEl.setAttribute('data-placeholder', rec.title ? 'Add a title...' : formatDate(rec.date));

  titleEl.onblur = async () => {
    const newTitle = titleEl.textContent.trim();
    if (newTitle !== (rec.title || '')) {
      try {
        await window.plaud.renameRecording(sessionId, newTitle);
      } catch (err) {
        if (err.name !== 'AbortError') console.warn('[transcript] rename failed:', err);
        return;
      }
      rec.title = newTitle;
      if (!newTitle) {
        titleEl.setAttribute('data-placeholder', formatDate(rec.date));
      }
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

  // Tag picker
  const tagPicker = $('#transcript-tags');
  const availableTags = app.settings.available_tags || [
    'Group Meeting',
    '1:1',
    'Memo',
    'Journal',
    'Interview',
    'Lecture',
  ];
  const currentTags = rec.tags || [];

  function renderTagPicker() {
    tagPicker.innerHTML =
      availableTags
        .map((tag) => {
          const active = currentTags.includes(tag);
          return `<button class="tag-option ${active ? 'active' : ''}" data-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>`;
        })
        .join('') + `<input type="text" class="tag-add-input" id="tag-add-input" placeholder="+ New tag">`;

    tagPicker.querySelectorAll('.tag-option').forEach((btn) => {
      btn.addEventListener('click', () => {
        const tag = btn.dataset.tag;
        const idx = currentTags.indexOf(tag);
        if (idx >= 0) {
          currentTags.splice(idx, 1);
        } else {
          currentTags.push(tag);
        }
        rec.tags = currentTags;
        // Render immediately — the server-side tag endpoint just saves locally
        // and enqueues the Notion sync, so the UI doesn't need to wait.
        renderTagPicker();
        window.plaud.tagRecording(sessionId, currentTags).catch(() => {});
      });
    });

    const addInput = document.getElementById('tag-add-input');
    addInput.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      const newTag = addInput.value.trim();
      if (!newTag) return;
      e.preventDefault();
      const addedToAvailable = !availableTags.includes(newTag);
      if (addedToAvailable) {
        availableTags.push(newTag);
        const customTags = availableTags.filter(
          (t) => !['Group Meeting', '1:1', 'Memo', 'Journal', 'Interview', 'Lecture'].includes(t),
        );
        app.settings.available_tags = availableTags;
        window.plaud.saveSettings({ custom_tags: customTags }).catch(() => {});
      }
      if (!currentTags.includes(newTag)) {
        currentTags.push(newTag);
        rec.tags = currentTags;
        window.plaud.tagRecording(sessionId, currentTags).catch(() => {});
      }
      renderTagPicker();
    });
  }
  renderTagPicker();

  // Meta
  const metaParts = [formatDuration(rec.duration)];
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

  const filename = `${sessionId}.ogg`;
  dom.audioEl.src = window.plaud.audioUrl(filename);
  dom.audioEl.load();
  if (autoPlay) {
    dom.audioEl.play().catch(() => {});
  }

  // Pre-warm the m4a cache for neighbouring recordings so flipping through
  // the list doesn't re-block on ffmpeg. Cached hits are a no-op server-side.
  try {
    const idx = app.recordings.findIndex((r) => r.session_id === sessionId);
    if (idx >= 0) {
      [app.recordings[idx - 1], app.recordings[idx + 1]]
        .filter(Boolean)
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
  actionsBar.innerHTML = `
    <button class="action-btn accent" id="retranscribe-btn" title="Re-run transcription from scratch">
      <svg width="16" height="16" viewBox="0 0 18 18" fill="none">
        <path d="M1.5 9a7.5 7.5 0 0113.1-5M16.5 9a7.5 7.5 0 01-13.1 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
        <path d="M14.6 1v3h-3M3.4 17v-3h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span>Re-transcribe</span>
    </button>
    ${
      t && t.text
        ? `
    <button class="action-btn subtle" id="summarize-btn" title="Generate AI summary">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <path d="M2 4h12M2 8h8M2 12h10" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
      </svg>
      <span>${hasSummary ? 'Re-summarize' : 'Summarize'}</span>
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
        <button class="export-option" data-format="txt">Plain Text (.txt)</button>
        <button class="export-option" data-format="pdf">PDF (.pdf)</button>
        <button class="export-option" data-format="json">JSON (.json)</button>
      </div>
    </div>
    <button class="action-btn subtle" id="notion-push-btn" title="Push to Notion">
      <img src="notion-icon.svg" width="16" height="16" style="vertical-align:middle;">
      <span>Notion</span>
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

  // Re-transcribe
  $('#retranscribe-btn').addEventListener('click', async () => {
    if (t) {
      try {
        await window.plaud.deleteTranscript(sessionId);
      } catch (err) {
        if (err.name !== 'AbortError') console.warn('[transcript] deleteTranscript failed:', err);
      }
    }
    await transcribeRecording(rec.ogg_path, sessionId);
  });

  // Copy
  const copyBtn = $('#copy-transcript-btn');
  if (copyBtn && t) {
    copyBtn.addEventListener('click', () => {
      let text = '';
      if (app.activeTab === 'summary' && t.summary) {
        text = t.summary;
      } else if (t.segments && t.segments.length > 0) {
        text = t.segments
          .map((seg) => {
            const time = formatTime(seg.start);
            const speaker = seg.speaker ? `[${seg.speaker}] ` : '';
            return `[${time}] ${speaker}${seg.text}`;
          })
          .join('\n');
      } else {
        text = t.text || '';
      }
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

        // Build content based on format
        let content, filename, mimeType;

        if (format === 'json') {
          content = JSON.stringify(isSum ? { title: t.auto_title, summary: t.summary, segments: t.segments } : { text: t.text, segments: t.segments, duration: t.duration, language: t.language, speakers: t.speakers }, null, 2);
          filename = `${safeName}.json`;
          mimeType = 'application/json';
        } else if (format === 'md') {
          if (isSum) {
            content = `# ${t.auto_title || customName}\n\n${t.summary}`;
          } else if (t.segments && t.segments.length > 0) {
            const lines = t.segments.map(s => {
              const time = formatTime(s.start);
              const speaker = s.speaker ? `**${s.speaker}**: ` : '';
              return `[${time}] ${speaker}${s.text}`;
            });
            content = `# ${customName}\n\n${lines.join('\n\n')}`;
          } else {
            content = `# ${customName}\n\n${t.text || ''}`;
          }
          filename = `${safeName}.md`;
          mimeType = 'text/markdown';
        } else if (format === 'txt') {
          if (isSum) {
            content = `${t.auto_title || customName}\n${'='.repeat(40)}\n\n${t.summary}`;
          } else if (t.segments && t.segments.length > 0) {
            content = t.segments.map(s => {
              const time = formatTime(s.start);
              const speaker = s.speaker ? `[${s.speaker}] ` : '';
              return `[${time}] ${speaker}${s.text}`;
            }).join('\n');
          } else {
            content = t.text || '';
          }
          filename = `${safeName}.txt`;
          mimeType = 'text/plain';
        } else if (format === 'pdf') {
          // Generate PDF via a simple HTML→print approach
          let html;
          if (isSum) {
            html = `<h1>${escapeHtml(t.auto_title || customName)}</h1>${renderMarkdown(t.summary)}`;
          } else if (t.segments && t.segments.length > 0) {
            html = `<h1>${escapeHtml(customName)}</h1>` + t.segments.map(s => {
              const time = formatTime(s.start);
              const speaker = s.speaker ? `<strong>${escapeHtml(s.speaker)}:</strong> ` : '';
              return `<p><small>[${time}]</small> ${speaker}${escapeHtml(s.text)}</p>`;
            }).join('');
          } else {
            html = `<h1>${escapeHtml(customName)}</h1><p>${escapeHtml(t.text || '')}</p>`;
          }

          const printWin = window.open('', '_blank', 'width=800,height=600');
          if (printWin) {
            printWin.document.write(`<!DOCTYPE html><html><head><title>${escapeHtml(safeName)}</title>
              <style>body{font-family:-apple-system,system-ui,sans-serif;max-width:700px;margin:40px auto;padding:0 20px;line-height:1.6;color:#222;}
              h1{font-size:20px;margin-bottom:24px;}h2{font-size:16px;margin-top:24px;}h3{font-size:14px;}
              ul{padding-left:20px;}li{margin:4px 0;}small{color:#888;}p{margin:8px 0;}</style>
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
      toast('Generating summary...', 'info');
      dom.progressBar.classList.remove('hidden');
      dom.progressBar.classList.add('indeterminate');
      try {
        const result = await window.plaud.summarize(sessionId);
        if (result.error) {
          toast(`Summary failed: ${result.error}`, 'error');
        } else {
          toast('Summary generated', 'success');
          if (result.notion_url) {
            toast('Pushed to Notion', 'success');
          } else if (result.notion_error) {
            toast(`Notion: ${result.notion_error}`, 'error');
          }
          if (t) {
            t.summary = result.summary;
            t.auto_title = result.title;
          }
          if (result.title && !rec.title) {
            rec.title = result.title;
            titleEl.textContent = result.title;
            titleEl.setAttribute('data-placeholder', 'Add a title...');
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
  if (notionBtn) {
    // Check if this recording already has a Notion page
    (async () => {
      try {
        const pageInfo = await window.plaud.notionPageUrl(sessionId);
        if (pageInfo.has_page) {
          notionBtn.innerHTML = `<img src="notion-icon.svg" width="16" height="16" style="vertical-align:middle;"><span>Open in Notion</span>`;
          notionBtn.dataset.notionUrl = pageInfo.url;
        }
      } catch {}
    })();

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
        const result = await window.plaud.notionPush(sessionId);
        if (result.error) {
          toast(`Notion push failed: ${result.error}`, 'error');
        } else {
          const verb = result.updated ? 'Updated in' : 'Pushed to';
          toast(`${verb} Notion!`, 'success');
          if (result.url) {
            notionBtn.innerHTML = `<img src="notion-icon.svg" width="16" height="16" style="vertical-align:middle;"><span>Open in Notion</span>`;
            notionBtn.dataset.notionUrl = result.url;
          }
        }
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

  // Tabs
  const tabsEl = $('#transcript-tabs');
  if (t && t.summary) {
    tabsEl.classList.remove('hidden');
    showTranscriptTab('summary', sessionId, rec, t);
  } else if (t && t.text) {
    tabsEl.classList.add('hidden');
    showTranscriptTab('transcript', sessionId, rec, t);
  } else {
    tabsEl.classList.add('hidden');
    showTranscriptTab('transcript', sessionId, rec, t);
  }

  tabsEl.querySelectorAll('.transcript-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      showTranscriptTab(tab.dataset.tab, sessionId, rec, t);
    });
  });
}

function showTranscriptTab(tabName, sessionId, rec, t) {
  const segContainer = $('#transcript-segments');
  const sumContainer = $('#transcript-summary');
  const tabsEl = $('#transcript-tabs');

  app.activeTab = tabName;

  tabsEl.querySelectorAll('.transcript-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.tab === tabName);
  });

  if (tabName === 'summary' && t && t.summary) {
    segContainer.classList.add('hidden');
    sumContainer.classList.remove('hidden');
    renderEditableSummary(sumContainer, t, sessionId, rec);
    tabsEl.classList.remove('hidden');
  } else {
    sumContainer.classList.add('hidden');
    segContainer.classList.remove('hidden');
    renderTranscriptSegments(segContainer, t, sessionId);
  }
}

function renderTranscriptSegments(container, t, sessionId) {
  if (t && t.segments && t.segments.length > 0) {
    container.innerHTML = t.segments
      .map(
        (seg, i) => `
      <div class="segment" data-index="${i}" data-start="${escapeHtml(seg.start)}">
        <span class="segment-time">${formatTime(seg.start)}</span>
        ${seg.speaker ? `<span class="segment-speaker">${escapeHtml(seg.speaker)}</span>` : ''}
        <span class="segment-text" contenteditable="true" data-index="${i}">${escapeHtml(seg.text)}</span>
        <button class="segment-delete" data-index="${i}" title="Remove segment">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
    `,
      )
      .join('');

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
    container.innerHTML = `
      <div style="text-align: center; padding: 40px; color: var(--text-tertiary);">
        <p>No transcript yet. Click <strong>Re-transcribe</strong> above to generate one.</p>
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

function renderEditableSummary(container, t, sessionId, rec) {
  let editing = false;
  const render = () => {
    container.innerHTML = renderMarkdown(t.summary);
    // Add edit hint
    const editHint = document.createElement('button');
    editHint.className = 'summary-edit-btn';
    editHint.textContent = 'Edit';
    editHint.addEventListener('click', () => startEditing());
    container.prepend(editHint);
  };

  const startEditing = () => {
    editing = true;
    const textarea = document.createElement('textarea');
    textarea.className = 'summary-editor';
    textarea.value = t.summary;
    textarea.rows = Math.max(10, t.summary.split('\n').length + 2);

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
    container.append(textarea, actions);
    textarea.focus();

    saveBtn.addEventListener('click', async () => {
      const newSummary = textarea.value.trim();
      if (newSummary !== t.summary) {
        if (newSummary) {
          t.summary = newSummary;
        } else {
          delete t.summary;
          if (app.transcripts[sessionId]) {
            delete app.transcripts[sessionId].summary;
          }
        }
        try {
          await window.plaud.updateTranscript(sessionId, { summary: newSummary || null });
          if (newSummary && app.transcripts[sessionId]) {
            app.transcripts[sessionId].summary = newSummary;
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

      if (result.diarizing) {
        toast('Speaker detection running in background...', 'info');
        // Cancel any prior diarization poll before starting a new one — otherwise
        // closing+re-transcribing leaves an orphan that can force-navigate the
        // user back to this session whenever the speakers field appears.
        if (_diarPollHandleTx) clearInterval(_diarPollHandleTx);
        if (_diarPollTimeoutHandleTx) clearTimeout(_diarPollTimeoutHandleTx);
        _diarPollHandleTx = setInterval(async () => {
          try {
            const transcriptData = await window.plaud.transcripts();
            const t = transcriptData.find((tr) => tr.session_id === sessionId);
            if (t && t.speakers && t.speakers.length > 0) {
              clearInterval(_diarPollHandleTx);
              _diarPollHandleTx = null;
              if (_diarPollTimeoutHandleTx) {
                clearTimeout(_diarPollTimeoutHandleTx);
                _diarPollTimeoutHandleTx = null;
              }
              toast(`Speaker detection complete: ${t.speakers.length} speaker(s) found`, 'success');
              app.transcripts[sessionId] = t;
              if (app.currentView === 'transcript' && app.currentSessionId === sessionId) {
                openTranscript(sessionId);
              }
            }
          } catch {}
        }, 5000);
        _diarPollTimeoutHandleTx = setTimeout(() => {
          if (_diarPollHandleTx) {
            clearInterval(_diarPollHandleTx);
            _diarPollHandleTx = null;
          }
          _diarPollTimeoutHandleTx = null;
        }, 300000);
      }
    }
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  } finally {
    app.processingIds.delete(sessionId);
    dom.progressFill.style.transition = '';
    hideProgress();
    renderRecordingsList();
  }
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
        // Reload audio for the active recording
        const targetId = result.session_id || sessionId;
        audio.src = window.plaud.audioUrl(`${targetId}.ogg`) + '?t=' + Date.now();
        audio.load();
        if (result.session_id && result.session_id !== sessionId) {
          openTranscript(result.session_id);
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
