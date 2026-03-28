/* ============================================================
   Plaud V2 — Transcript View Module
   ============================================================ */

function openTranscript(sessionId, autoPlay = false) {
  const rec = app.recordings.find((r) => r.session_id === sessionId);
  if (!rec) return;

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
      await window.plaud.renameRecording(sessionId, newTitle);
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
      btn.addEventListener('click', async () => {
        const tag = btn.dataset.tag;
        const idx = currentTags.indexOf(tag);
        if (idx >= 0) {
          currentTags.splice(idx, 1);
        } else {
          currentTags.push(tag);
        }
        rec.tags = currentTags;
        await window.plaud.tagRecording(sessionId, currentTags);
        renderTagPicker();
      });
    });

    const addInput = document.getElementById('tag-add-input');
    addInput.addEventListener('keydown', async (e) => {
      if (e.key === 'Enter') {
        const newTag = addInput.value.trim();
        if (!newTag) return;
        e.preventDefault();
        if (!availableTags.includes(newTag)) {
          availableTags.push(newTag);
          const customTags = availableTags.filter(
            (t) => !['Group Meeting', '1:1', 'Memo', 'Journal', 'Interview', 'Lecture'].includes(t),
          );
          await window.plaud.saveSettings({ custom_tags: customTags });
          app.settings.available_tags = availableTags;
        }
        if (!currentTags.includes(newTag)) {
          currentTags.push(newTag);
          rec.tags = currentTags;
          await window.plaud.tagRecording(sessionId, currentTags);
        }
        renderTagPicker();
      }
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

  // Audio
  const filename = `${sessionId}.ogg`;
  dom.audioEl.src = window.plaud.audioUrl(filename);
  dom.audioEl.load();
  if (autoPlay) {
    dom.audioEl.play().catch(() => {});
  }

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
    <button class="action-btn subtle" id="notion-push-btn" title="Push to Notion">
      <img src="notion-icon.svg" width="16" height="16" style="vertical-align:middle;">
      <span>Notion</span>
    </button>
    `
        : ''
    }
  `;

  // Re-transcribe
  $('#retranscribe-btn').addEventListener('click', async () => {
    if (t) await window.plaud.deleteTranscript(sessionId);
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
          showTranscriptTab('summary', sessionId, rec, t);
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

  // Notion push
  const notionBtn = $('#notion-push-btn');
  if (notionBtn) {
    notionBtn.addEventListener('click', async () => {
      const status = await window.plaud.notionStatus();

      if (!status.connected) {
        toast('Connecting to Notion...', 'info');
        try {
          const result = await window.plaud.notionConnect();
          if (result.url) {
            window.plaud.openExternal(result.url);
            toast('Sign in with Notion in your browser. Come back here after.', 'info');
            const poll = setInterval(async () => {
              const s = await window.plaud.notionStatus();
              if (s.connected) {
                clearInterval(poll);
                app.notionState = s;
                toast('Notion connected!', 'success');
                if (!s.database_id) {
                  toast('Now select a database in Settings to push to.', 'info');
                  openSettings();
                  loadSettings();
                }
              }
            }, 2000);
            setTimeout(() => clearInterval(poll), 120000);
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
          toast('Pushed to Notion!', 'success');
        }
      } catch (err) {
        toast(`Error: ${err.message}`, 'error');
      } finally {
        setLoading(notionBtn, false);
      }
    });
  }

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
    sumContainer.innerHTML = renderMarkdown(t.summary);
    tabsEl.classList.remove('hidden');
  } else {
    sumContainer.classList.add('hidden');
    segContainer.classList.remove('hidden');
    renderTranscriptSegments(segContainer, t);
  }
}

function renderTranscriptSegments(container, t) {
  if (t && t.segments && t.segments.length > 0) {
    container.innerHTML = t.segments
      .map(
        (seg, i) => `
      <div class="segment" data-index="${i}" data-start="${seg.start}">
        <span class="segment-time">${formatTime(seg.start)}</span>
        ${seg.speaker ? `<span class="segment-speaker">${escapeHtml(seg.speaker)}</span>` : ''}
        <span class="segment-text">${escapeHtml(seg.text)}</span>
      </div>
    `,
      )
      .join('');

    container.querySelectorAll('.segment').forEach((el) => {
      el.addEventListener('click', () => {
        const start = parseFloat(el.dataset.start);
        if (!isNaN(start)) {
          dom.audioEl.currentTime = start;
          dom.audioEl.play().catch(() => {});
        }
      });
    });
  } else if (t && t.text) {
    container.innerHTML = `<div class="segment"><span class="segment-text">${escapeHtml(t.text)}</span></div>`;
  } else {
    container.innerHTML = `
      <div style="text-align: center; padding: 40px; color: var(--text-tertiary);">
        <p>No transcript yet. Click <strong>Re-transcribe</strong> above to generate one.</p>
      </div>
    `;
  }
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
      openTranscript(sessionId);

      if (result.diarizing) {
        toast('Speaker detection running in background...', 'info');
        const pollDiar = setInterval(async () => {
          try {
            const transcriptData = await window.plaud.transcripts();
            const t = transcriptData.find((tr) => tr.session_id === sessionId);
            if (t && t.speakers && t.speakers.length > 0) {
              clearInterval(pollDiar);
              toast(`Speaker detection complete: ${t.speakers.length} speaker(s) found`, 'success');
              app.transcripts[sessionId] = t;
              if (app.currentView === 'transcript') openTranscript(sessionId);
            }
          } catch {}
        }, 5000);
        setTimeout(() => clearInterval(pollDiar), 300000);
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
  const titleEl = $('#transcript-title');
  if (titleEl && document.activeElement === titleEl) {
    titleEl.blur();
  }
  app.currentView = 'list';
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
  dom.audioEl.addEventListener('timeupdate', () => {
    if (dom.audioEl.duration) {
      const pct = (dom.audioEl.currentTime / dom.audioEl.duration) * 100;
      dom.playerFill.style.width = `${pct}%`;
      dom.playerCurrent.textContent = formatTime(dom.audioEl.currentTime);

      $$('.segment').forEach((el) => {
        const start = parseFloat(el.dataset.start);
        const nextEl = el.nextElementSibling;
        const nextStart = nextEl ? parseFloat(nextEl.dataset.start) : Infinity;
        if (dom.audioEl.currentTime >= start && dom.audioEl.currentTime < nextStart) {
          el.classList.add('active');
        } else {
          el.classList.remove('active');
        }
      });
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
