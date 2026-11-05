/* ============================================================
   Open Pin — Coaching progress view and "Your voice & coaching" settings
   ============================================================
   Globals: openProgressView() (header #progress-btn) and renderVoiceSettings()
   (Settings › Transcription & AI). Everything else is prefixed _coachProg / _voice,
   because every app/ui/*.js file shares one global scope. */

// Mirrors app/coaching/targets.py TARGETS (low/high in the backend's units, e.g.
// talk_share is a 0-1 fraction); tests/test_coaching_progress_ui.py keeps them in
// step. scale/dec/unit only shape how a number is written; targetDec, when set,
// writes the target band with other decimals ("at most 1 in 10 turns"). overall_score is the
// mean of the five AI scores and has no target; engagement_score is measured (laughs,
// follow-ups, points built on) and has none either. Rows show in this order, each only when
// the backend sends its series: the scores first, then the measures, then engagement's parts.
const _coachProgMetrics = {
  overall_score: { label: 'Overall score', low: null, high: null, scale: 1, dec: 0, unit: ' / 100', hint: 'Average of the five AI scores' },
  engagement_score: { label: 'Engagement score', low: null, high: null, scale: 1, dec: 0, unit: ' / 100', hint: 'Measured from laughs, follow-up questions and points built on' },
  pace_wpm: { label: 'Pace', low: 110, high: 160, scale: 1, dec: 0, unit: ' wpm', hint: 'Words per minute while you speak' },
  talk_share: { label: 'Talk share', low: 0.4, high: 0.6, scale: 100, dec: 0, unit: '%', hint: 'Your share of the talking' },
  longest_monologue_s: { label: 'Longest monologue', low: null, high: 150, scale: 1, dec: 0, unit: '', hint: 'Your longest turn without a break' },
  interruption_rate: { label: 'Likely interruptions', low: null, high: 0.1, scale: 10, dec: 1, targetDec: 0, unit: ' in 10 turns', hint: 'How often you likely cut someone off' },
  patience_s: { label: 'Patience', low: 0.5, high: 1.5, scale: 1, dec: 1, unit: ' s', hint: 'Usual pause before you reply' },
  questions_per_hour: { label: 'Questions', low: 10, high: 18, scale: 1, dec: 0, unit: ' an hour', hint: 'Questions you ask' },
  fillers_per_min: { label: 'Filler words', low: 0.6, high: 3, scale: 1, dec: 1, unit: ' a minute', hint: 'Um, uh, like, you know' },
  interactivity: { label: 'Interactivity', low: 5, high: null, scale: 1, dec: 1, unit: ' per 5 min', hint: 'How often the speaker changes' },
  laughs_per_hour: { label: 'Laughs after your lines', low: 6, high: null, scale: 1, dec: 1, targetDec: 0, unit: ' an hour', hint: 'Laughter straight after you speak' },
  follow_up_share: { label: 'Follow-up questions to you', low: 0.25, high: null, scale: 100, dec: 0, unit: '% of your turns', hint: 'Questions straight after your turns' },
  built_on_share: { label: 'Your points others built on', low: 0.5, high: null, scale: 100, dec: 0, unit: '%', hint: 'From the AI review' },
};
const COACH_PROG_WEEKS = 12;
const COACH_PROG_MIN = 3; // fewer coached conversations than this shows the empty state
let _coachProgSeq = 0;

// ---- Number formatting ----

function _coachProgNum(metric, v, dec) {
  const m = _coachProgMetrics[metric];
  if (!m) return String(Math.round(v * 100) / 100); // a metric the backend added later
  if (metric === 'longest_monologue_s') {
    const total = Math.round(v);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
  }
  const n = v * m.scale;
  const d = dec ?? m.dec;
  return d ? n.toFixed(d) : String(Math.round(n));
}

function _coachProgValue(metric, v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '–';
  const m = _coachProgMetrics[metric];
  return `${_coachProgNum(metric, v)}${m ? m.unit : ''}`;
}

function _coachProgTarget(metric) {
  const m = _coachProgMetrics[metric];
  if (!m) return '';
  const num = (v) => _coachProgNum(metric, v, m.targetDec);
  if (m.low !== null && m.high !== null) return `${num(m.low)}–${num(m.high)}${m.unit}`;
  if (m.low !== null) return `at least ${num(m.low)}${m.unit}`;
  if (m.high !== null) return `at most ${num(m.high)}${m.unit}`;
  return '';
}

function _coachProgOnTarget(metric, v) {
  const m = _coachProgMetrics[metric];
  if (m.low === null && m.high === null) return null;
  return (m.low === null || v >= m.low) && (m.high === null || v <= m.high);
}

function _coachProgDate(t) {
  return new Date(t * 1000).toLocaleDateString([], { day: 'numeric', month: 'short' });
}

// ---- Sparkline: hand-rolled SVG (target band shaded, baseline dashed) ----

function _coachProgSparkline(metric, points, baseline) {
  const W = 180;
  const H = 48;
  const PAD = 5;
  const m = _coachProgMetrics[metric];
  const vals = points.map((p) => p.value);
  const extent = [...vals];
  if (baseline !== null && baseline !== undefined) extent.push(baseline);
  if (m.low !== null) extent.push(m.low);
  if (m.high !== null) extent.push(m.high);
  let lo = Math.min(...extent);
  let hi = Math.max(...extent);
  if (hi - lo < 1e-9) {
    const d = Math.abs(hi) * 0.1 || 1;
    lo -= d;
    hi += d;
  }
  const span = hi - lo;
  lo -= span * 0.08;
  hi += span * 0.08;
  const y = (v) => PAD + (1 - (v - lo) / (hi - lo)) * (H - 2 * PAD);
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t;
  const x = (p, i) => {
    if (points.length === 1) return W / 2;
    const f = t1 > t0 ? (p.t - t0) / (t1 - t0) : i / (points.length - 1);
    return PAD + f * (W - 2 * PAD);
  };

  const parts = [];
  if (m.low !== null || m.high !== null) {
    const top = m.high !== null ? y(m.high) : 0;
    const bottom = m.low !== null ? y(m.low) : H;
    parts.push(`<rect class="coach-prog-band" x="0" y="${top.toFixed(1)}" width="${W}" height="${Math.max(0, bottom - top).toFixed(1)}" rx="3"></rect>`);
  }
  if (baseline !== null && baseline !== undefined) {
    const by = y(baseline).toFixed(1);
    parts.push(`<line class="coach-prog-baseline" x1="0" x2="${W}" y1="${by}" y2="${by}"></line>`);
  }
  const xy = points.map((p, i) => [x(p, i), y(p.value)]);
  if (xy.length > 1) {
    parts.push(`<polyline class="coach-prog-line" points="${xy.map(([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`).join(' ')}"></polyline>`);
  }
  const [lx, ly] = xy[xy.length - 1];
  parts.push(`<circle class="coach-prog-last" cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="3.5"></circle>`);
  // Hover: a bigger invisible target per conversation, with its date and value
  xy.forEach(([a, b], i) => {
    const tip = `${_coachProgDate(points[i].t)}: ${_coachProgValue(metric, points[i].value)}`;
    parts.push(`<circle class="coach-prog-hit" cx="${a.toFixed(1)}" cy="${b.toFixed(1)}" r="7"><title>${escapeHtml(tip)}</title></circle>`);
  });

  const target = _coachProgTarget(metric);
  const label = `${m.label} over ${points.length} conversation${points.length === 1 ? '' : 's'}, latest ${_coachProgValue(metric, vals[vals.length - 1])}${target ? `, target ${target}` : ''}`;
  return `<svg class="coach-prog-spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${escapeHtml(label)}">${parts.join('')}</svg>`;
}

// ---- Progress view ----

function _coachProgCard(title, body, kind) {
  return `<div class="coach-prog-card ${kind}"><div class="coach-prog-card-title">${escapeHtml(title)}</div><p class="coach-prog-card-body">${body}</p></div>`;
}

function _coachProgCards(data) {
  const name = (metric) => escapeHtml(_coachProgMetrics[metric] ? _coachProgMetrics[metric].label : metric);
  const focus = data.focus
    ? escapeHtml(data.focus.text)
    : 'Nothing stands out: the last two weeks were on target, or had no coached conversations.';
  const imp = data.most_improved;
  const improved = imp
    ? `<strong>${name(imp.metric)}</strong>: ${escapeHtml(_coachProgValue(imp.metric, imp.from))} → ${escapeHtml(_coachProgValue(imp.metric, imp.to))}`
    : 'Nothing yet. This compares the last four weeks with the four before.';
  const st = data.most_stuck;
  const stuck = st
    ? `<strong>${name(st.metric)}</strong>: ${escapeHtml(_coachProgValue(st.metric, st.value))}${_coachProgTarget(st.metric) ? `, target ${escapeHtml(_coachProgTarget(st.metric))}` : ''}`
    : 'Nothing. Everything was on target over the last four weeks.';
  return `
    ${_coachProgCard('Focus this week', focus, 'focus')}
    <div class="coach-prog-card-pair">
      ${_coachProgCard('Most improved', improved, 'improved')}
      ${_coachProgCard('Still stuck', stuck, 'stuck')}
    </div>`;
}

function _coachProgRow(metric, points, baseline) {
  const m = _coachProgMetrics[metric];
  const latest = points[points.length - 1].value;
  const on = _coachProgOnTarget(metric, latest);
  const target = _coachProgTarget(metric);
  const state = on === null ? '' : on ? '<span class="coach-prog-state on">On target</span>' : '<span class="coach-prog-state off">Off target</span>';
  const usual = baseline === null || baseline === undefined ? '' : `Your usual ${escapeHtml(_coachProgValue(metric, baseline))}`;
  const meta = [usual, target ? `target ${escapeHtml(target)}` : ''].filter(Boolean).join(' · ');
  return `
    <div class="coach-prog-row">
      <div class="coach-prog-row-text">
        <div class="coach-prog-row-label">${escapeHtml(m.label)} ${state}</div>
        <div class="coach-prog-row-value">${escapeHtml(_coachProgValue(metric, latest))}</div>
        <div class="coach-prog-row-meta">${meta || escapeHtml(m.hint)}</div>
      </div>
      ${_coachProgSparkline(metric, points, baseline)}
    </div>`;
}

function _coachProgRender(data) {
  const body = $('#progress-body');
  const count = Number(data.count) || 0;
  if (count < COACH_PROG_MIN) {
    body.innerHTML = `
      <div class="coach-prog-empty">
        <h3>Not enough to show yet</h3>
        <p>Progress appears once ${COACH_PROG_MIN} conversations have been coached. So far: ${count}.</p>
        <p class="settings-hint">Coaching runs after a recording with two or more speakers is transcribed with speaker labels. Set up your voice so Open Pin knows which speaker is you.</p>
        <button class="action-btn subtle" id="coach-prog-voice-btn">Your voice &amp; coaching settings</button>
      </div>`;
    $('#coach-prog-voice-btn').addEventListener('click', _coachProgOpenVoiceSettings);
    return;
  }
  const series = data.series || {};
  const baseline = data.baseline || {};
  const rows = Object.keys(_coachProgMetrics)
    .filter((metric) => (series[metric] || []).length > 0)
    .map((metric) => _coachProgRow(metric, series[metric], baseline[metric]))
    .join('');
  body.innerHTML = `
    <p class="coach-prog-count">From <strong>${count}</strong> coached conversation${count === 1 ? '' : 's'} in the last ${COACH_PROG_WEEKS} weeks. Recordings marked “speakers mixed up” are left out.</p>
    ${_coachProgCards(data)}
    <h3 class="coach-prog-heading">Each measure over time</h3>
    <p class="coach-prog-legend">
      <span class="coach-prog-key band"></span>Target
      <span class="coach-prog-key baseline"></span>Your usual (last 30 days)
      <span class="coach-prog-key line"></span>Each conversation
    </p>
    <div class="coach-prog-rows">${rows}</div>`;
}

async function openProgressView() {
  const panel = $('#progress-view');
  const overlay = $('#progress-overlay');
  panel.classList.remove('hidden');
  overlay.classList.remove('hidden');
  panel.offsetHeight;
  panel.classList.add('visible');
  overlay.classList.add('visible');
  await _coachProgLoad();
}

/** Load and draw the progress, and the "Coach them again" line. reload: keep what's
    shown until the new data is in (after a bulk re-run ends). */
async function _coachProgLoad(reload = false) {
  const body = $('#progress-body');
  const seq = ++_coachProgSeq;
  _coachProgRerunWasRunning = false;
  _coachProgRerunRefresh();
  if (!reload) body.innerHTML = '<p class="coach-prog-loading">Loading your progress…</p>';
  let data;
  try {
    data = await window.plaud.coachingProgress(COACH_PROG_WEEKS);
  } catch (err) {
    data = { error: (err && err.message) || String(err) };
  }
  if (seq !== _coachProgSeq) return;
  if (data && !data.error) {
    try {
      _coachProgRender(data);
      return;
    } catch (err) {
      console.error('[coaching] progress render failed:', err);
      data = { error: 'something in the reply couldn’t be shown' };
    }
  }
  body.innerHTML = `
    <div class="coach-prog-empty">
      <p>Couldn’t load your progress: ${escapeHtml((data && data.error) || 'no reply')}</p>
      <button class="action-btn subtle" id="coach-prog-retry">Try again</button>
    </div>`;
  $('#coach-prog-retry').addEventListener('click', openProgressView);
}

// ---- "Coach them again": older reports saved before Engagement existed ----
// The backend coaches them one at a time (app/coaching/runner.py start_rerun_all) and keeps
// going when the view closes; while the view is open, its status is polled every 3 s.

const COACH_PROG_RERUN_POLL_MS = 3000;
let _coachProgRerunTimer = null;
let _coachProgRerunStatus = null; // the last status drawn
let _coachProgRerunWasRunning = false; // so the end of a run reloads the progress once
let _coachProgRerunStopping = false;

function _coachProgIsOpen() {
  return $('#progress-view').classList.contains('visible');
}

/** "Include an AI review" (Settings › Your voice & coaching) as it is now. */
function _coachProgReviewOn() {
  return !(app.settings && app.settings.coaching_ai_review === false);
}

function _coachProgRerunHtml(st) {
  if (st.running) {
    const stop = _coachProgRerunStopping
      ? '<span class="coach-prog-rerun-note">Stopping after this conversation…</span>'
      : '<button type="button" class="coach-link" id="coach-prog-rerun-stop">Stop</button>';
    return `<span class="coach-prog-rerun-text">Coaching again: ${Number(st.done) || 0} of ${Number(st.total) || 0}</span>${stop}`;
  }
  const n = Number(st.pending_count) || 0;
  if (n <= 0) return '';
  const text = n === 1 ? '1 older report doesn’t have Engagement yet.' : `${n} older reports don’t have Engagement yet.`;
  return `<span class="coach-prog-rerun-text">${text}</span>
    <button type="button" class="action-btn subtle settings-btn-compact" id="coach-prog-rerun-btn">Coach them again</button>`;
}

function _coachProgRerunDraw(st) {
  const bar = $('#progress-rerun');
  if (!bar) return;
  const html = st ? _coachProgRerunHtml(st) : '';
  bar.innerHTML = html;
  bar.classList.toggle('hidden', !html);
  const start = $('#coach-prog-rerun-btn');
  if (start) start.addEventListener('click', _coachProgRerunStart);
  const stop = $('#coach-prog-rerun-stop');
  if (stop) stop.addEventListener('click', _coachProgRerunStop);
}

function _coachProgRerunStopPoll() {
  if (_coachProgRerunTimer) clearTimeout(_coachProgRerunTimer);
  _coachProgRerunTimer = null;
}

async function _coachProgRerunRefresh() {
  _coachProgRerunStopPoll();
  const seq = _coachProgSeq;
  let st;
  try {
    st = await window.plaud.coachingRerunAllStatus();
  } catch (err) {
    st = { error: (err && err.message) || String(err) };
  }
  if (seq !== _coachProgSeq || !_coachProgIsOpen()) return;
  if (!st || st.error) {
    _coachProgRerunDraw(null); // nothing to offer if the backend can't say
    return;
  }
  _coachProgRerunStatus = st;
  if (!st.running) _coachProgRerunStopping = false;
  if (!st.running && _coachProgRerunWasRunning) {
    _coachProgLoad(true); // the re-coached reports change the progress
    return;
  }
  _coachProgRerunWasRunning = !!st.running;
  _coachProgRerunDraw(st);
  if (st.running) {
    _coachProgRerunStopPoll();
    _coachProgRerunTimer = setTimeout(_coachProgRerunRefresh, COACH_PROG_RERUN_POLL_MS);
  }
}

async function _coachProgRerunStart(e) {
  const btn = e && e.currentTarget;
  const n = Number((_coachProgRerunStatus || {}).pending_count) || 0;
  if (n <= 0) return;
  let message = `Open Pin will coach ${n === 1 ? '1 conversation' : `${n} conversations`} again, one at a time.`;
  if (_coachProgReviewOn()) message += ` With the AI review on, that’s ${n === 1 ? '1 AI call' : `${n} AI calls`} to your AI provider.`;
  const choice = await showConfirmDialog({
    title: 'Coach older reports again?',
    message,
    actions: [{ id: 'rerun', label: 'Coach them again' }],
    cancelLabel: 'Cancel',
  });
  if (choice !== 'rerun') return;
  if (btn) setLoading(btn, true);
  let res;
  try {
    res = await window.plaud.coachingRerunAll();
  } catch (err) {
    res = { error: (err && err.message) || String(err) };
  }
  // Already going (409) is fine: the status below shows it
  if (!res || (res.error && !/already/i.test(res.error))) {
    toast(`Couldn’t start coaching again: ${(res && res.error) || 'no reply'}`, 'error');
  }
  _coachProgRerunStopping = false;
  await _coachProgRerunRefresh();
}

async function _coachProgRerunStop() {
  _coachProgRerunStopping = true;
  if (_coachProgRerunStatus) _coachProgRerunDraw(_coachProgRerunStatus);
  let res;
  try {
    res = await window.plaud.coachingRerunAllCancel();
  } catch (err) {
    res = { error: (err && err.message) || String(err) };
  }
  if (!res || res.error) {
    _coachProgRerunStopping = false;
    toast(`Couldn’t stop: ${(res && res.error) || 'no reply'}`, 'error');
  }
  await _coachProgRerunRefresh();
}

function _coachProgClose() {
  _coachProgRerunStopPoll(); // a bulk re-run keeps going; reopening shows it
  const panel = $('#progress-view');
  const overlay = $('#progress-overlay');
  panel.classList.remove('visible');
  overlay.classList.remove('visible');
  panel.addEventListener('transitionend', function handler() {
    if (!panel.classList.contains('visible')) {
      panel.classList.add('hidden');
      overlay.classList.add('hidden');
    }
    panel.removeEventListener('transitionend', handler);
  });
}

function _coachProgOpenVoiceSettings() {
  _coachProgClose();
  openSettings();
  loadSettings();
  showSettingsTab('ai');
  const section = $('#voice-coaching-section');
  if (section) section.scrollIntoView({ block: 'start' });
}

$('#progress-btn').addEventListener('click', openProgressView);
$('#progress-close').addEventListener('click', _coachProgClose);
$('#progress-overlay').addEventListener('click', _coachProgClose);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && $('#progress-view').classList.contains('visible')) _coachProgClose();
});

// ---- Settings › Your voice & coaching ----

let _voicePollTimer = null;
let _voiceSetupMode = null; // null | 'setup' | 'redo'
let _voiceInstallAskedAt = 0; // covers the moment between "install" and the first "installing" status
let _voiceSetupSid = '';
const _voiceTicked = new Set();
const VOICE_POLL_MS = 2000;
const VOICE_READ_ALOUD =
  'I am reading this so Open Pin can learn my voice. Yesterday I walked to the station, bought a coffee and caught the early train. '
  + 'The carriage was quiet, so I read for a while and watched the fields go by. When I arrived, I rang a friend and we agreed to meet for lunch on Thursday, '
  + 'somewhere near the river, if the weather holds.';

function _voiceRenderAutoToggle() {
  const toggle = $('#coaching-auto-toggle');
  if (!toggle) return;
  const labelsOn = !!(app.settings && app.settings.diarize);
  toggle.disabled = !labelsOn;
  toggle.checked = labelsOn && app.settings.coaching_auto !== false;
  $('#coaching-auto-row').classList.toggle('voice-auto-off', !labelsOn);
  $('#coaching-auto-needs').classList.toggle('hidden', labelsOn);
  const review = $('#coaching-ai-review-toggle');
  if (review) review.checked = !app.settings || app.settings.coaching_ai_review !== false;
}

function _voiceInstalling(status) {
  if (status.installing) return true;
  // The install thread may not have reported yet: count it as installing for a few seconds.
  return !status.model && !status.install_error && Date.now() - _voiceInstallAskedAt < 6000;
}

function _voiceRenderStatus(status) {
  const el = $('#voice-status');
  let html;
  if (status.profile) {
    const n = Number(status.exemplars) || 0;
    html = `<p class="voice-status-line"><span class="ai-key-badge connected">Set up</span>Open Pin knows your voice from ${n} sample${n === 1 ? '' : 's'}. It learns more each time you say which speaker is you.</p>`;
  } else {
    html = '<p class="voice-status-line"><span class="ai-key-badge none">Not set up</span>Until it is, each coached conversation asks which speaker is you.</p>';
  }
  if (!_voiceSetupMode) {
    html += status.profile
      ? `<div class="voice-actions">
           <button class="action-btn subtle settings-btn-compact" id="voice-redo-btn">Redo</button>
           <button class="action-btn subtle settings-btn-compact voice-remove" id="voice-remove-btn">Remove my voice</button>
         </div>`
      : '<div class="voice-actions"><button class="action-btn primary settings-btn-compact" id="voice-setup-btn">Set up your voice</button></div>';
  }
  el.innerHTML = html;
  const start = (mode) => {
    _voiceSetupMode = mode;
    _voiceSetupSid = '';
    _voiceTicked.clear();
    renderVoiceSettings();
  };
  if ($('#voice-setup-btn')) $('#voice-setup-btn').addEventListener('click', () => start('setup'));
  if ($('#voice-redo-btn')) $('#voice-redo-btn').addEventListener('click', () => start('redo'));
  if ($('#voice-remove-btn')) $('#voice-remove-btn').addEventListener('click', _voiceRemove);
}

function _voiceStep(n, title) {
  return `<div class="voice-step-title"><span class="voice-step-num">Step ${n} of 2</span>${escapeHtml(title)}</div>`;
}

function _voiceRenderInstallStep(setupEl, status) {
  setupEl.dataset.step = 'install';
  if (_voiceInstalling(status)) {
    setupEl.innerHTML = `
      ${_voiceStep(1, 'Install the voice model')}
      <p class="settings-desc">Installing… this can take a few minutes. You can keep using Open Pin, and close Settings if you like.</p>
      <div class="voice-progress" role="progressbar" aria-label="Installing the voice model"><div class="voice-progress-fill"></div></div>`;
    return;
  }
  const error = status.install_error
    ? `<p class="voice-error" role="alert">${escapeHtml(status.install_error)}</p>`
    : '';
  setupEl.innerHTML = `
    ${_voiceStep(1, 'Install the voice model')}
    <p class="settings-desc">Open Pin recognises your voice with a speaker model that runs on this Mac. It’s a one-off download and can take a few minutes.</p>
    ${error}
    <div class="voice-actions">
      <button class="action-btn primary settings-btn-compact" id="voice-install-btn">${status.install_error ? 'Try again' : 'Install voice model'}</button>
      <button class="action-btn subtle settings-btn-compact" id="voice-cancel-btn">Cancel</button>
    </div>`;
  $('#voice-install-btn').addEventListener('click', async (e) => {
    setLoading(e.currentTarget, true);
    _voiceInstallAskedAt = Date.now();
    let res;
    try {
      res = await window.plaud.voiceInstall();
    } catch (err) {
      res = { error: (err && err.message) || String(err) };
    }
    if (res && res.error) {
      _voiceInstallAskedAt = 0;
      toast(`Couldn’t start the install: ${res.error}`, 'error');
    }
    renderVoiceSettings();
  });
  $('#voice-cancel-btn').addEventListener('click', _voiceCancel);
}

/** Recordings that have a transcript with speaker labels, newest first. */
function _voiceCandidates() {
  return (app.recordings || [])
    .filter((rec) => {
      const t = app.transcripts[rec.session_id];
      return t && (t.segments || []).some((s) => s.speaker);
    })
    .sort((a, b) => new Date(b.date || 0) - new Date(a.date || 0));
}

/** Speakers in order of first appearance, each with their first line. */
function _voiceSpeakers(sid) {
  const t = app.transcripts[sid];
  const out = [];
  const seen = new Set();
  for (const seg of (t && t.segments) || []) {
    const text = (seg.text || '').trim();
    if (!seg.speaker || seen.has(seg.speaker) || !text) continue;
    seen.add(seg.speaker);
    out.push({ label: seg.speaker, start: seg.start || 0, text: text.length > 140 ? `${text.slice(0, 140)}…` : text });
  }
  return out;
}

function _voiceRecordingName(rec) {
  const t = app.transcripts[rec.session_id] || {};
  const title = rec.title || t.auto_title || 'Untitled recording';
  return `${title} · ${formatDate(rec.date)}`;
}

function _voiceRenderSpeakers() {
  const list = $('#voice-speakers');
  const save = $('#voice-save-btn');
  if (!_voiceSetupSid) {
    list.innerHTML = '';
    save.disabled = true;
    return;
  }
  const speakers = _voiceSpeakers(_voiceSetupSid);
  list.innerHTML = `
    <p class="settings-label">Tick every speaker that is you (a wearable sometimes splits one voice in two):</p>
    ${speakers
      .map(
        (s) => `
      <label class="voice-speaker">
        <input type="checkbox" value="${escapeHtml(s.label)}"${_voiceTicked.has(s.label) ? ' checked' : ''}>
        <span class="voice-speaker-text">
          <span class="voice-speaker-name">${escapeHtml(s.label)} <span class="voice-speaker-time">${escapeHtml(formatTime(s.start))}</span></span>
          <span class="voice-speaker-line">“${escapeHtml(s.text)}”</span>
        </span>
      </label>`,
      )
      .join('')}`;
  list.querySelectorAll('input[type="checkbox"]').forEach((box) => {
    box.addEventListener('change', () => {
      if (box.checked) _voiceTicked.add(box.value);
      else _voiceTicked.delete(box.value);
      save.disabled = _voiceTicked.size === 0;
    });
  });
  save.disabled = _voiceTicked.size === 0;
}

async function _voiceRenderPickStep(setupEl) {
  setupEl.dataset.step = 'pick';
  if (!app.recordings || app.recordings.length === 0) await loadRecordings();
  const recs = _voiceCandidates();
  const options = recs
    .map((rec) => `<option value="${escapeHtml(rec.session_id)}"${rec.session_id === _voiceSetupSid ? ' selected' : ''}>${escapeHtml(_voiceRecordingName(rec))}</option>`)
    .join('');
  const picker = recs.length
    ? `<select id="voice-recording-select" class="settings-select" aria-label="Recording">
         <option value="">Choose a recording…</option>${options}
       </select>`
    : '<p class="settings-hint">No recordings with speaker labels yet. Record a conversation, or read the paragraph below into your NotePin, then sync it and transcribe it with speaker labels on.</p>';
  setupEl.innerHTML = `
    ${_voiceStep(2, 'Show Open Pin your voice')}
    <p class="settings-desc">Pick a recording where you speak, then tick the speakers that are you.</p>
    ${picker}
    <div id="voice-speakers" class="voice-speakers"></div>
    <details class="voice-read-aloud">
      <summary>No good recording? Read a paragraph into your NotePin</summary>
      <p class="settings-hint">Read this aloud at your normal pace (about 30 seconds). Sync and transcribe it, then pick it above.</p>
      <blockquote>${escapeHtml(VOICE_READ_ALOUD)}</blockquote>
    </details>
    <p id="voice-setup-error" class="voice-error hidden" role="alert"></p>
    <div class="voice-actions">
      <button class="action-btn primary settings-btn-compact" id="voice-save-btn" disabled>${_voiceSetupMode === 'redo' ? 'Replace my voice' : 'Use these speakers'}</button>
      <button class="action-btn subtle settings-btn-compact" id="voice-cancel-btn">Cancel</button>
    </div>`;
  const select = $('#voice-recording-select');
  if (select) {
    select.addEventListener('change', () => {
      _voiceSetupSid = select.value;
      _voiceTicked.clear();
      _voiceRenderSpeakers();
    });
  }
  $('#voice-save-btn').addEventListener('click', _voiceSave);
  $('#voice-cancel-btn').addEventListener('click', _voiceCancel);
  _voiceRenderSpeakers();
}

function _voiceShowError(text) {
  const el = $('#voice-setup-error');
  if (!el) return toast(text, 'error');
  el.textContent = text;
  el.classList.remove('hidden');
}

async function _voiceSave(e) {
  const btn = e.currentTarget;
  const labels = [..._voiceTicked];
  if (!_voiceSetupSid || labels.length === 0) return;
  setLoading(btn, true);
  $('#voice-setup-error').classList.add('hidden');
  // Redo replaces the profile in one step on the backend: if it fails, the old voice stays.
  const replace = _voiceSetupMode === 'redo';
  const kept = replace ? ' Your old voice is kept.' : '';
  try {
    const res = await window.plaud.voiceSetup(_voiceSetupSid, labels, replace);
    if (res && res.error) {
      if (/not installed/i.test(res.error)) {
        toast('The voice model isn’t installed yet. Install it first.', 'error');
        renderVoiceSettings();
        return;
      }
      throw new Error(res.error);
    }
    if (!res || !res.exemplars) {
      _voiceShowError(
        `Couldn’t hear enough clear speech from those speakers. Pick a recording where you talk for longer.${kept}`,
      );
      renderVoiceSettings();
      return;
    }
    _voiceSetupMode = null;
    toast(replace ? 'Your voice has been replaced' : 'Your voice is set up', 'success');
    renderVoiceSettings();
  } catch (err) {
    _voiceShowError(`Couldn’t set up your voice: ${(err && err.message) || err}${kept}`);
    renderVoiceSettings();
  } finally {
    setLoading(btn, false);
  }
}

function _voiceCancel() {
  _voiceSetupMode = null;
  renderVoiceSettings();
}

async function _voiceRemove() {
  const choice = await showConfirmDialog({
    title: 'Remove your voice?',
    message: 'Open Pin forgets your voice. Coached conversations keep their reports. New ones will ask which speaker is you until you set your voice up again.',
    actions: [{ id: 'remove', label: 'Remove my voice', destructive: true }],
    cancelLabel: 'Cancel',
  });
  if (choice !== 'remove') return;
  let res;
  try {
    res = await window.plaud.voiceDelete();
  } catch (err) {
    res = { error: (err && err.message) || String(err) };
  }
  if (res && res.error) toast(`Couldn’t remove your voice: ${res.error}`, 'error');
  else toast('Your voice has been removed', 'success');
  renderVoiceSettings();
}

function _voiceSchedulePoll() {
  if (_voicePollTimer) return;
  _voicePollTimer = setTimeout(() => {
    _voicePollTimer = null;
    renderVoiceSettings();
  }, VOICE_POLL_MS);
}

// One render at a time: a call that arrives mid-render (a poll and a click together)
// runs once more after the current render instead of in parallel with it.
let _voiceRendering = null;
let _voiceRenderAgain = false;

async function renderVoiceSettings() {
  if (_voiceRendering) {
    _voiceRenderAgain = true;
    return _voiceRendering;
  }
  _voiceRendering = (async () => {
    try {
      do {
        _voiceRenderAgain = false;
        await _voiceRenderOnce();
      } while (_voiceRenderAgain);
    } finally {
      _voiceRendering = null;
    }
  })();
  return _voiceRendering;
}

async function _voiceRenderOnce() {
  _voiceRenderAutoToggle();
  const statusEl = $('#voice-status');
  const setupEl = $('#voice-setup');
  if (!statusEl || !setupEl) return;
  let status;
  try {
    status = await window.plaud.voiceStatus();
  } catch (err) {
    status = { error: (err && err.message) || String(err) };
  }
  if (!status || status.error) {
    statusEl.innerHTML = `<p class="voice-error">Couldn’t check your voice set-up: ${escapeHtml((status && status.error) || 'no reply')}</p>`;
    setupEl.classList.add('hidden');
    return;
  }
  if (_voiceInstalling(status)) _voiceSchedulePoll();
  _voiceRenderStatus(status);

  setupEl.classList.toggle('hidden', !_voiceSetupMode);
  if (!_voiceSetupMode) {
    setupEl.dataset.step = '';
    setupEl.innerHTML = '';
    return;
  }
  if (!status.model) _voiceRenderInstallStep(setupEl, status);
  else if (setupEl.dataset.step !== 'pick') await _voiceRenderPickStep(setupEl);
}

// The same coaching_auto setting as Automation › "Coach my conversations" (_renderAutoCoachRow
// in settings.js): redraw that switch too.
$('#coaching-auto-toggle').addEventListener('change', async (e) => {
  app.settings.coaching_auto = e.target.checked;
  updateAutoPipelineState();
  await window.plaud.saveSettings({ coaching_auto: e.target.checked });
  toast(e.target.checked ? 'New conversations will be coached' : 'Automatic coaching off', 'success');
});

// A privacy choice, so it applies to "Coach this recording" too (app/coaching/runner.py)
$('#coaching-ai-review-toggle').addEventListener('change', async (e) => {
  app.settings.coaching_ai_review = e.target.checked;
  await window.plaud.saveSettings({ coaching_ai_review: e.target.checked });
  toast(
    e.target.checked ? 'Coaching will include an AI review' : 'AI review off: coaching stays on your Mac',
    'success',
  );
});
