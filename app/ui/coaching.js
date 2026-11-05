/* ============================================================
   Open Pin — Coaching tab (recording view)
   ============================================================
   Shows the coaching sidecar for one recording (GET /coaching/<sid>):
   who the app thinks is you, scores, the summary and "next time", the
   measured metrics, moments and how the others responded. Everything the
   AI or the transcript wrote goes through escapeHtml. The *Html functions
   are pure (data in, markup out); renderCoachingTab wires them to the DOM.
   Globals are prefixed coach/_coach: this file shares one scope with the
   other app/ui/*.js scripts. */

const _COACH_POLL_MS = 5000;
// Safety net only: a run normally ends with a status other than "running"
const _COACH_POLL_MAX_MS = 30 * 60 * 1000;
const _COACH_RUN_GRACE_MS = 20 * 1000;
// "Your usual" is the progress baseline (median of the last 30 days), so five
// weeks of history is enough to compute it.
const _COACH_BASELINE_WEEKS = 5;
const _COACH_BASELINE_TTL_MS = 60 * 1000;
// Same tolerance the review uses to ground its timestamps (app/coaching/review.py)
const _COACH_TIME_TOLERANCE_S = 2;

const _COACH_AREAS = [
  ['clarity', 'Clarity'],
  ['warmth', 'Warmth'],
  ['charisma', 'Charisma'],
  ['listening', 'Listening'],
  ['reception', 'Reception'],
];

const _COACH_RESPONSES = {
  engaged: ['Engaged', 'good'],
  agreed: ['Agreed', 'good'],
  follow_up: ['Asked a follow-up', 'good'],
  pushed_back: ['Pushed back', 'warn'],
  deflected: ['Deflected', 'warn'],
  short: ['Short answer', 'warn'],
};

const _COACH_MOMENT_KINDS = {
  interruption: 'Likely interruption',
  monologue: 'Longest monologue',
  fast: 'Fast stretch',
  filler_burst: 'Run of filler words',
};

// Why the runner couldn't pick you (needs_owner status_reason, app/coaching/runner.py
// and voice.py): what happened, then how to fix it for next time (already escaped).
const _COACH_VOICE_SETTINGS_LINK =
  '<button type="button" class="coach-link" data-coach-action="voice-settings">Settings › Transcription &amp; AI › Your voice &amp; coaching</button>';
const _COACH_NEEDS_OWNER = {
  'no voice profile': [
    'You haven’t set up your voice yet, so the app can’t tell which speaker is you.',
    `Set it up in ${_COACH_VOICE_SETTINGS_LINK} so the app recognises you in later recordings.`,
  ],
  'voice model not installed': [
    'The voice model isn’t installed, so the app can’t tell which speaker is you.',
    `Install it in ${_COACH_VOICE_SETTINGS_LINK} so the app can learn your voice.`,
  ],
  'no confident match': ['The app couldn’t tell from your voice which speaker is you.', ''],
  'no audio file': ['There’s no audio for this recording, so there’s nothing to match your voice against.', ''],
};

// Mirrors REVIEW_OFF in app/coaching/runner.py: the run skipped the AI review by choice
const _COACH_REVIEW_OFF = 'AI review is off';

// Mirrors MIN_COACHING_SECONDS in app/coaching/targets.py (shorter recordings are skipped)
const _COACH_MIN_COACHING_SECONDS = 120;
// AI moment kinds that describe an interruption ("talked over", "cut in"...)
const _COACH_INTERRUPT_RE = /interrupt|\b(talk(ed|s|ing)?|spoke|speak(s|ing)?) over\b|\bcut(s|ting)? (in|off|across)\b|over-?talk/i;

let _coachPollHandle = null;
let _coachSeq = 0;
let _coachBaseline = null; // { at, value } — cached progress baseline

/* ---------- Formatting ---------- */

function _coachNum(v, decimals = 0) {
  return String(Number(Number(v).toFixed(decimals)));
}

function _coachIsNum(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function _coachMinSec(s) {
  const total = Math.round(s);
  const m = Math.floor(total / 60);
  const sec = total % 60;
  if (m === 0) return `${sec}s`;
  return sec ? `${m}m ${sec}s` : `${m}m`;
}

// One row per owner measure. fmt formats the value and the target band the same way.
const _COACH_MEASURES = [
  { key: 'pace_wpm', name: 'Pace', fmt: (v) => `${_coachNum(v)} wpm` },
  { key: 'talk_share', name: 'Talk share', fmt: (v) => `${_coachNum(v * 100)}%` },
  { key: 'longest_monologue_s', name: 'Longest monologue', fmt: _coachMinSec },
  {
    key: 'interruption_rate',
    count: 'interruptions_made',
    name: 'Likely interruptions by you',
    fmt: (v) => `${_coachNum(v * 100)}% of your turns`,
    value: (rate, made) => {
      if (made === 0 || rate === 0) return 'None';
      if (_coachIsNum(made) && rate > 0) return `${made} in ${Math.round(made / rate)} turns`;
      return `${_coachNum(rate * 100)}% of your turns`;
    },
    band: (low, high) => `at most 1 in ${Math.round(1 / high)} turns`,
  },
  {
    key: 'interrupted_by_others',
    name: 'Likely interruptions by others',
    fmt: (v) => (v === 0 ? 'None' : _coachNum(v)),
    unscored: true,
  },
  { key: 'patience_s', name: 'Pause before you reply', fmt: (v) => `${_coachNum(v, 1)} s` },
  {
    key: 'questions_per_hour',
    count: 'questions',
    name: 'Questions you asked',
    fmt: (v) => `${_coachNum(v)} per hour`,
  },
  {
    key: 'fillers_per_min',
    count: 'fillers',
    name: 'Filler words',
    fmt: (v) => `${_coachNum(v, 1)} per minute`,
  },
  {
    key: 'interactivity',
    name: 'Back-and-forth',
    fmt: (v) => `${_coachNum(v, 1)} switches per 5 min`,
  },
];

function _coachBandText(measure, target) {
  if (!target || measure.unscored) return '';
  const { low, high } = target;
  if (measure.band && _coachIsNum(high)) return measure.band(low, high);
  if (_coachIsNum(low) && _coachIsNum(high)) {
    // "110–160 wpm": the unit once, after the range
    const lo = measure.fmt(low);
    const hi = measure.fmt(high);
    const unit = hi.replace(/^[\d.]+/, '');
    return lo.endsWith(unit) ? `${lo.slice(0, lo.length - unit.length)}–${hi}` : `${lo}–${hi}`;
  }
  if (_coachIsNum(low)) return `at least ${measure.fmt(low)}`;
  if (_coachIsNum(high)) return `at most ${measure.fmt(high)}`;
  return '';
}

/** 'in', 'high' or 'low' against the target band; '' when there's no band. */
function _coachBandState(value, target) {
  if (!target || !_coachIsNum(value)) return '';
  if (_coachIsNum(target.low) && value < target.low) return 'low';
  if (_coachIsNum(target.high) && value > target.high) return 'high';
  return 'in';
}

/* ---------- Speaker names ----------
   The open recording's speaker names ({label: name}, the transcript's speaker_names,
   your own name included once you picked yourself). _coachReportHtml sets them for
   the report it builds; the *Html helpers below read them while it runs. */
let _coachNames = {};

/** A speaker as HTML: the name with the label beside it, or the label alone. */
function _coachWho(label) {
  const name = _coachNames[label];
  if (!name || name === label) return escapeHtml(label);
  return `${escapeHtml(name)} <span class="coach-label">${escapeHtml(label)}</span>`;
}

/** Text the AI or the measures wrote, as HTML, with names in place of labels. */
function _coachText(text) {
  return escapeHtml(applySpeakerNames(String(text), _coachNames));
}

/* ---------- Data helpers ---------- */

/** Mean of the area scores present, rounded (as progress.py's overall_score); null without scores.
    The review drops a score none of whose evidence matches the transcript. */
function _coachOverall(review) {
  const scores = (review && review.scores) || {};
  const vals = _COACH_AREAS.map(([k]) => scores[k] && scores[k].score).filter(_coachIsNum);
  if (!vals.length) return null;
  return Math.round(vals.reduce((a, b) => a + b, 0) / vals.length);
}

/** Measured and AI moments in one list, sorted by time. */
function _coachMoments(sidecar) {
  const metrics = (sidecar && sidecar.metrics) || {};
  const review = (sidecar && sidecar.review) || {};
  const confirmed = (review.interruptions_confirmed || []).filter(_coachIsNum);
  const out = [];
  for (const m of metrics.moments || []) {
    if (!m || !_coachIsNum(m.t)) continue;
    out.push({
      t: m.t,
      kind: m.kind,
      label: _COACH_MOMENT_KINDS[m.kind] || String(m.kind || 'Moment'),
      speaker: m.speaker || '',
      text: m.quote || '',
      agreed: m.kind === 'interruption' && confirmed.some((c) => Math.abs(c - m.t) <= _COACH_TIME_TOLERANCE_S),
    });
  }
  for (const m of review.moments || []) {
    if (!m || !_coachIsNum(m.t)) continue;
    const kind = String(m.kind || 'Moment').replace(/_/g, ' ');
    // Single-channel audio: an interruption is only ever "likely"
    const interruption = _COACH_INTERRUPT_RE.test(kind);
    out.push({
      t: m.t,
      kind: interruption ? 'interruption' : kind,
      label: interruption ? 'Likely interruption' : kind.charAt(0).toUpperCase() + kind.slice(1),
      speaker: '',
      text: m.note || '',
      agreed: false,
    });
  }
  return out.sort((a, b) => a.t - b.t);
}

/** Every speaker for the "which one is you" picker, with their first line. */
function _coachSpeakers(sidecar, segments) {
  const candidates = (sidecar && sidecar.candidates) || [];
  const byLabel = new Map();
  for (const c of candidates) {
    if (c && c.label) byLabel.set(c.label, { label: c.label, firstLine: c.first_line || '', loud: !!c.loud });
  }
  for (const s of segments || []) {
    if (s && s.speaker && !byLabel.has(s.speaker)) {
      byLabel.set(s.speaker, { label: s.speaker, firstLine: s.text || '', loud: false });
    }
  }
  const per = (sidecar && sidecar.metrics && sidecar.metrics.per_speaker) || {};
  for (const label of Object.keys(per)) {
    if (label !== 'Unknown' && !byLabel.has(label)) byLabel.set(label, { label, firstLine: '', loud: false });
  }
  return [...byLabel.values()];
}

function _coachErrors(reason) {
  if (Array.isArray(reason)) return reason.map(String).filter(Boolean);
  if (!reason) return [];
  return String(reason)
    .split(/;\s+|\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/* ---------- Markup ---------- */

function _coachSeekAttr(t) {
  return `data-coach-seek="${escapeHtml(String(t))}"`;
}

function _coachTime(t) {
  return `<span class="coach-time">${escapeHtml(formatTime(t))}</span>`;
}

function _coachBadge(availability) {
  if (availability !== 'estimated' && availability !== 'unavailable') return '';
  return `<span class="coach-badge coach-badge-${availability}">${availability}</span>`;
}

// html must already be escaped
function _coachNotice(html, actionLabel, action = 'run', tone = '') {
  return `
    <div class="coach-notice${tone ? ` coach-notice-${tone}` : ''}">
      <span>${html}</span>
      ${actionLabel ? `<button type="button" class="coach-link" data-coach-action="${action}">${escapeHtml(actionLabel)}</button>` : ''}
    </div>`;
}

function _coachOwnerLine(sidecar) {
  const owner = sidecar.owner || {};
  const labels = (owner.labels || []).filter(Boolean);
  let how = '';
  if (owner.source === 'voice') {
    const title = _coachIsNum(owner.confidence) ? ` title="Voice match ${escapeHtml(_coachNum(owner.confidence, 2))}"` : '';
    how = `<span class="coach-owner-how"${title}>matched by your voice</span>`;
  } else if (owner.source === 'user') {
    how = '<span class="coach-owner-how">you chose this</span>';
  }
  return `
    <div class="coach-owner">
      <span>This is you: <strong>${labels.length ? labels.map(_coachWho).join(' + ') : 'not set'}</strong></span>
      ${how ? `<span class="coach-sep" aria-hidden="true">·</span>${how}` : ''}
      <span class="coach-sep" aria-hidden="true">·</span>
      <button type="button" class="coach-link" data-coach-action="change-owner" aria-expanded="false">Change</button>
    </div>`;
}

function _coachPickerHtml(sidecar, segments, inline) {
  const chosen = new Set((sidecar.owner && sidecar.owner.labels) || []);
  const rows = _coachSpeakers(sidecar, segments)
    .map(
      (s) => `
      <label class="coach-picker-row">
        <input type="checkbox" name="coach-owner" value="${escapeHtml(s.label)}"${chosen.has(s.label) ? ' checked' : ''}>
        <span class="coach-picker-label" title="${escapeHtml(_coachNames[s.label] ? `${_coachNames[s.label]} (${s.label})` : s.label)}">${_coachWho(s.label)}</span>
        <span class="coach-picker-line">${s.firstLine ? `“${escapeHtml(s.firstLine)}”` : '<em>No lines</em>'}</span>
        ${s.loud ? '<span class="coach-badge coach-badge-hint" title="Loudest voice, so probably nearest the mic">loudest</span>' : ''}
      </label>`,
    )
    .join('');
  return `
    <div class="coach-picker${inline ? '' : ' hidden'}" data-coach-picker>
      <p class="coach-muted">Tick every label that’s you: a wearable mic can split one voice into two.</p>
      <div class="coach-picker-list">${rows}</div>
      <label class="coach-picker-mixed">
        <input type="checkbox" name="coach-mixed"${sidecar.mixed_up ? ' checked' : ''}>
        <span><strong>Speakers mixed up</strong><span class="coach-muted"> Keeps this report but leaves it out of your progress.</span></span>
      </label>
      <div class="coach-picker-actions">
        ${inline ? '' : '<button type="button" class="action-btn subtle" data-coach-action="cancel-owner">Cancel</button>'}
        <button type="button" class="action-btn accent" data-coach-action="save-owner"${chosen.size ? '' : ' disabled'}>${inline ? 'This is me' : 'Save'}</button>
      </div>
    </div>`;
}

// The transcript times a score is based on, as chips that play from there
function _coachEvidenceHtml(evidence) {
  const times = [...new Set((Array.isArray(evidence) ? evidence : []).filter((t) => _coachIsNum(t) && t >= 0))];
  if (!times.length) return '';
  const chips = times
    .sort((a, b) => a - b)
    .map((t) => {
      const label = escapeHtml(formatTime(t));
      return `<button type="button" class="coach-chip" ${_coachSeekAttr(t)} title="Play from ${label}">${label}</button>`;
    })
    .join('');
  return `<span class="coach-area-evidence" role="group" aria-label="Based on these moments">${chips}</span>`;
}

function _coachScoresHtml(review, baseline) {
  const overall = _coachOverall(review);
  if (overall === null) return '';
  const usual = baseline && _coachIsNum(baseline.overall_score) ? Math.round(baseline.overall_score) : null;
  const areas = _COACH_AREAS.map(([key, name]) => {
    const s = (review.scores || {})[key];
    if (!s || !_coachIsNum(s.score)) return '';
    const score = Math.max(0, Math.min(100, Math.round(s.score)));
    return `
      <div class="coach-area">
        <span class="coach-area-name">${name}</span>
        <span class="coach-bar" role="img" aria-label="${score} out of 100"><span class="coach-bar-fill" style="width:${score}%"></span></span>
        <span class="coach-area-score">${score}</span>
        ${s.reason ? `<span class="coach-area-reason">${_coachText(s.reason)}</span>` : ''}
        ${_coachEvidenceHtml(s.evidence)}
      </div>`;
  }).join('');
  return `
    <section class="coach-card coach-scores">
      <div class="coach-overall">
        <span class="coach-overall-label">Overall</span>
        <span class="coach-overall-score">${overall}</span>
        <span class="coach-muted">out of 100</span>
        ${usual !== null ? `<span class="coach-usual">Your usual ${usual}</span>` : ''}
      </div>
      <div class="coach-areas">${areas}</div>
    </section>`;
}

function _coachSummaryHtml(review) {
  const next = (review.next_time || []).filter((s) => typeof s === 'string' && s.trim());
  if (!review.summary && !next.length) return '';
  return `
    <section class="coach-card">
      ${review.summary ? `<p class="coach-summary">${_coachText(review.summary)}</p>` : ''}
      ${next.length ? `<h3 class="coach-h">Next time</h3><ol class="coach-next">${next.map((s) => `<li>${_coachText(s)}</li>`).join('')}</ol>` : ''}
    </section>`;
}

function _coachMeasuresHtml(sidecar, baseline) {
  const metrics = sidecar.metrics || {};
  const owner = metrics.owner || {};
  const av = metrics.availability || {};
  const targets = sidecar.targets || {};
  const hasUsual = !!baseline && _COACH_MEASURES.some((m) => _coachIsNum(baseline[m.key]));
  const rows = _COACH_MEASURES.map((m) => {
    const v = owner[m.key];
    const availability = av[m.key];
    let value = '—';
    if (_coachIsNum(v)) value = m.value ? m.value(v, owner[m.count]) : m.fmt(v);
    if (_coachIsNum(v) && m.count && !m.value && _coachIsNum(owner[m.count])) value += ` <span class="coach-muted">(${owner[m.count]} in all)</span>`;
    const state = m.unscored ? '' : _coachBandState(v, targets[m.key]);
    const stateTitle = { in: 'Within target', high: 'Above target', low: 'Below target' }[state] || '';
    const dot = state ? `<span class="coach-dot coach-dot-${state === 'in' ? 'in' : 'out'}" title="${stateTitle}" aria-label="${stateTitle}"></span>` : '';
    const usual = hasUsual ? `<td class="coach-usual-cell">${baseline && _coachIsNum(baseline[m.key]) ? escapeHtml(m.fmt(baseline[m.key])) : '—'}</td>` : '';
    return `
      <tr>
        <th scope="row">${m.name}</th>
        <td class="coach-value">${dot}${value} ${_coachBadge(availability)}</td>
        <td class="coach-target">${escapeHtml(_coachBandText(m, targets[m.key])) || '—'}</td>
        ${usual}
      </tr>`;
  }).join('');

  const ownerLabels = new Set((sidecar.owner && sidecar.owner.labels) || []);
  const per = Object.entries(metrics.per_speaker || {}).sort(
    ([a, x], [b, y]) => ownerLabels.has(b) - ownerLabels.has(a) || (y.talk_share || 0) - (x.talk_share || 0),
  );
  const speakerRows = per.map(([label, s]) => `
      <tr>
        <th scope="row">${_coachWho(label)}${ownerLabels.has(label) ? ' <span class="coach-you">you</span>' : ''}</th>
        <td>${_coachIsNum(s.wpm) ? `${_coachNum(s.wpm)} wpm` : '—'}</td>
        <td>${_coachIsNum(s.talk_share) ? `${_coachNum(s.talk_share * 100)}%` : '—'}</td>
        <td>${_coachIsNum(s.turns) ? s.turns : '—'}</td>
      </tr>`).join('');

  const approx = Object.values(av).some((a) => a === 'estimated' || a === 'unavailable');
  return `
    <section class="coach-card">
      <h3 class="coach-h">Measures</h3>
      <div class="coach-table-wrap">
        <table class="coach-table">
          <thead><tr><th>Measure</th><th>You</th><th>Target</th>${hasUsual ? '<th>Your usual</th>' : ''}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      ${per.length ? `
      <h3 class="coach-h">By speaker</h3>
      <div class="coach-table-wrap">
        <table class="coach-table">
          <thead><tr><th>Speaker</th><th>Pace ${_coachBadge(av.pace_wpm)}</th><th>Talk share ${_coachBadge(av.talk_share)}</th><th>Turns</th></tr></thead>
          <tbody>${speakerRows}</tbody>
        </table>
      </div>` : ''}
      ${approx ? '<p class="coach-muted coach-footnote">Estimated and unavailable measures need word timing, which only a Deepgram transcript has. Re-transcribing with Deepgram measures them exactly.</p>' : ''}
    </section>`;
}

function _coachMomentsHtml(sidecar) {
  const moments = _coachMoments(sidecar);
  const items = moments.map((m) => `
      <li>
        <button type="button" class="coach-row" ${_coachSeekAttr(m.t)}>
          ${_coachTime(m.t)}
          <span class="coach-row-body">
            <span class="coach-row-head">
              <span class="coach-tag${m.kind === 'interruption' ? ' coach-tag-warn' : ''}">${escapeHtml(m.label)}</span>
              ${m.speaker ? `<span class="coach-row-speaker">${escapeHtml(speakerName(m.speaker, _coachNames))}</span>` : ''}
              ${m.agreed ? '<span class="coach-muted">review agrees</span>' : ''}
            </span>
            ${m.text ? `<span class="coach-row-text">${_coachText(m.text)}</span>` : ''}
          </span>
        </button>
      </li>`).join('');
  return `
    <section class="coach-card">
      <h3 class="coach-h">Moments</h3>
      ${moments.length ? `<ul class="coach-list">${items}</ul>` : '<p class="coach-muted">No moments flagged.</p>'}
    </section>`;
}

function _coachReactionsHtml(review) {
  const reactions = (review.reactions || []).filter((r) => r && _coachIsNum(r.t)).sort((a, b) => a.t - b.t);
  const items = reactions.map((r) => {
    const [label, tone] = _COACH_RESPONSES[r.response] || [String(r.response || ''), ''];
    return `
      <li>
        <button type="button" class="coach-row" ${_coachSeekAttr(r.t)}>
          ${_coachTime(r.t)}
          <span class="coach-row-body">
            <span class="coach-row-head">
              ${r.owner_point ? `<span class="coach-row-point">${_coachText(r.owner_point)}</span>` : ''}
              ${label ? `<span class="coach-tag${tone ? ` coach-tag-${tone}` : ''}">${escapeHtml(label)}</span>` : ''}
            </span>
            ${r.evidence ? `<span class="coach-row-text">${_coachText(r.evidence)}</span>` : ''}
          </span>
        </button>
      </li>`;
  }).join('');
  return `
    <section class="coach-card">
      <h3 class="coach-h">How they responded</h3>
      ${reactions.length ? `<ul class="coach-list">${items}</ul>` : '<p class="coach-muted">Nothing to show for this conversation.</p>'}
    </section>`;
}

function _coachSpinnerHtml(text) {
  return `
    <div class="coach-state" role="status">
      <span class="coach-spinner" aria-hidden="true"></span>
      <p>${text}</p>
    </div>`;
}

function _coachMinLengthText() {
  const s = _COACH_MIN_COACHING_SECONDS;
  if (s % 60) return `${s} seconds`;
  return s === 60 ? '1 minute' : `${s / 60} minutes`;
}

// force (Coach anyway) only bypasses the minimum length: the runner never coaches a
// recording without speaker labels or where only you spoke, so those offer a fix instead.
function _coachSkippedHtml(sc, segments) {
  const reason = String(sc.status_reason || '');
  if (reason === 'too short') {
    return `
      <div class="coach-state">
        <p>This recording is too short to coach automatically (under ${escapeHtml(_coachMinLengthText())}).</p>
        <button type="button" class="action-btn subtle" data-coach-action="run">Coach anyway</button>
      </div>`;
  }
  if (reason === 'no speaker labels') {
    return `
      <div class="coach-state">
        <p>This recording has no speaker labels, so there’s no telling who said what.</p>
        <p class="coach-muted">Turn on speaker labels in Settings › Transcription &amp; AI, then re-transcribe this recording. Coaching runs again once the labels are in.</p>
      </div>`;
  }
  if (reason === 'only you spoke') {
    if (_coachSpeakers(sc, segments).length < 2) {
      return `
        <div class="coach-state">
          <p>Only one voice was found in this recording, so there’s no conversation to coach.</p>
        </div>`;
    }
    // Every speaker is marked as you: offer the picker in case that's wrong
    return `
      ${_coachOwnerLine(sc)}
      ${_coachPickerHtml(sc, segments, false)}
      <div class="coach-state">
        <p>Every speaker in this recording is marked as you, so there’s no one else to compare with.</p>
        <p class="coach-muted">If that’s wrong, use Change above to pick only the labels that are you.</p>
      </div>`;
  }
  return `
    <div class="coach-state">
      <p>Not coached: ${escapeHtml(reason || 'no reason given')}.</p>
    </div>`;
}

/** "Include an AI review" (Settings › Your voice & coaching) as it is now. */
function _coachReviewSettingOn() {
  return !(typeof app !== 'undefined' && app.settings && app.settings.coaching_ai_review === false);
}

// A report made with "Include an AI review" off: say why there are no scores, and how to
// get them, without error styling.
function _coachReviewOffHtml() {
  if (!_coachReviewSettingOn()) {
    return _coachNotice(`AI review is off. Turn it on in ${_COACH_VOICE_SETTINGS_LINK}.`, '', '', 'info');
  }
  return _coachNotice('This report was made with the AI review off.', 'Add an AI review', 'run', 'info');
}

function _coachExcludedHtml() {
  return `
    <div class="coach-state">
      <p>This recording isn’t coached.</p>
      <button type="button" class="action-btn subtle" data-coach-action="include">Coach this recording</button>
    </div>`;
}

/** The whole tab for one sidecar. segments are the transcript's, for the speaker picker;
    names its speaker names ({label: name}), shown wherever a speaker is.
    Every state but an excluded one (or an error) ends with "Don't coach this recording". */
function _coachReportHtml(sidecar, baseline, segments, names = {}) {
  _coachNames = names && typeof names === 'object' ? names : {};
  const sc = sidecar || {};
  if (sc.error) return _coachReportBodyHtml(sc, baseline, segments);
  if (sc.excluded || sc.status === 'excluded') return _coachExcludedHtml();
  return `${_coachReportBodyHtml(sc, baseline, segments)}
    <div class="coach-exclude-row">
      <button type="button" class="coach-link coach-exclude" data-coach-action="exclude">Don’t coach this recording</button>
    </div>`;
}

function _coachReportBodyHtml(sc, baseline, segments) {
  if (sc.error) {
    return `
      <div class="coach-state">
        <p>Couldn’t load coaching: ${escapeHtml(sc.error)}</p>
        <button type="button" class="action-btn subtle" data-coach-action="reload">Try again</button>
      </div>`;
  }
  const status = sc.status;
  if (status === 'running') return _coachSpinnerHtml('Coaching this conversation…');
  if (status === 'needs_owner') {
    const [why, fix] = _COACH_NEEDS_OWNER[sc.status_reason] || ['The app couldn’t tell which speaker is you.', ''];
    return `
      <section class="coach-card">
        <h3 class="coach-h">Which one is you?</h3>
        <p class="coach-muted">${why} Nothing is coached until you choose.</p>
        ${fix ? `<p class="coach-muted coach-needs-fix">${fix}</p>` : ''}
        ${_coachPickerHtml(sc, segments, true)}
      </section>`;
  }
  if (status === 'skipped') return _coachSkippedHtml(sc, segments);
  if (status !== 'done' && status !== 'failed') {
    return `
      <div class="coach-state">
        <p>This recording hasn’t been coached yet.</p>
        <button type="button" class="action-btn subtle" data-coach-action="run">Coach this recording</button>
      </div>`;
  }

  // A failed run that still holds a review never got as far as replacing the last report
  // (it crashed, or the app quit mid-run): that review, and the metrics with it, are the
  // previous run's, maybe for another owner. Only a run's own results are shown.
  const previous = status === 'failed' && !!(sc.review && typeof sc.review === 'object');
  const review = !previous && sc.review && typeof sc.review === 'object' ? sc.review : null;
  const parts = [];
  if (sc.report_stale && !previous) parts.push(_coachNotice('Transcript changed:', 'Update coaching'));
  if ((sc.owner && (sc.owner.labels || []).length) || status === 'done') {
    parts.push(_coachOwnerLine(sc), _coachPickerHtml(sc, segments, false));
  }
  if (sc.mixed_up) {
    parts.push(_coachNotice('Marked as speakers mixed up, so this report is left out of your progress.', '', '', 'info'));
  }
  // A run whose AI review failed still measured the metrics; one that failed or was
  // interrupted before measuring anything has none of its own
  const hasMetrics = !previous && !!(sc.metrics && sc.metrics.owner);
  if (status === 'failed') {
    const errors = _coachErrors(sc.status_reason);
    parts.push(`
      <section class="coach-card coach-failed">
        <h3 class="coach-h">${hasMetrics ? 'The AI review didn’t finish' : 'Coaching didn’t finish'}</h3>
        ${errors.length ? `<ul class="coach-errors">${errors.map((e) => `<li>${escapeHtml(e)}</li>`).join('')}</ul>` : ''}
        ${hasMetrics ? '<p class="coach-muted">The measured metrics below are still right.</p>' : ''}
        ${previous ? '<p class="coach-muted">The previous report is hidden, as it may no longer match.</p>' : ''}
        <button type="button" class="action-btn subtle" data-coach-action="run">Try again</button>
      </section>`);
  } else if (!review && sc.status_reason === _COACH_REVIEW_OFF) {
    parts.push(_coachReviewOffHtml());
  } else if (!review) {
    parts.push(_coachNotice(escapeHtml(sc.status_reason || 'This report has measured metrics only.'), '', '', 'info'));
  }
  if (review) parts.push(_coachScoresHtml(review, baseline), _coachSummaryHtml(review));
  if (hasMetrics || status === 'done') parts.push(_coachMeasuresHtml(sc, baseline), _coachMomentsHtml(sc));
  if (review) parts.push(_coachReactionsHtml(review));
  return parts.join('');
}

/* ---------- DOM wiring ---------- */

function _coachStillShowing(container, sessionId) {
  return (
    container.isConnected &&
    !container.classList.contains('hidden') &&
    app.currentView === 'transcript' &&
    app.currentSessionId === sessionId &&
    app.activeTab === 'coaching'
  );
}

function _coachStopPoll() {
  if (_coachPollHandle) clearInterval(_coachPollHandle);
  _coachPollHandle = null;
}

/** What identifies one run's outcome: every finished run stamps a new created_at. */
function _coachRunMark(sc) {
  const s = sc || {};
  return JSON.stringify([s.status || '', s.created_at || '', s.status_reason || '']);
}

// Poll like the background diarization poll in transcript.js: every 5 s while the
// sidecar says "running", then redraw. Stops as soon as the tab is no longer on screen.
// before: _coachRunMark of the sidecar when a run was asked for (Coach anyway, Try
// again), whose response doesn't say "running". Until the poll has seen "running", a
// sidecar with that same mark is the old outcome, not this run's, so it doesn't end
// the poll during the first seconds.
function _coachStartPoll(container, sessionId, before = null) {
  _coachStopPoll();
  const started = Date.now();
  let seenRunning = false;
  const handle = setInterval(async () => {
    if (!_coachStillShowing(container, sessionId) || Date.now() - started > _COACH_POLL_MAX_MS) {
      if (_coachPollHandle === handle) _coachStopPoll();
      return;
    }
    let sc;
    try {
      sc = await window.plaud.coaching(sessionId);
    } catch {
      return;
    }
    if (_coachPollHandle !== handle || !_coachStillShowing(container, sessionId)) return;
    if (!sc || sc.error) return;
    if (sc.status === 'running') {
      seenRunning = true;
      return;
    }
    const waiting = before !== null && !seenRunning && Date.now() - started < _COACH_RUN_GRACE_MS;
    if (waiting && _coachRunMark(sc) === before) return;
    _coachStopPoll();
    _coachBaseline = null; // a finished run can move "your usual"
    renderCoachingTab(container, sessionId);
  }, _COACH_POLL_MS);
  _coachPollHandle = handle;
}

async function _coachLoadBaseline() {
  if (_coachBaseline && Date.now() - _coachBaseline.at < _COACH_BASELINE_TTL_MS) return _coachBaseline.value;
  let value = null;
  try {
    const p = await window.plaud.coachingProgress(_COACH_BASELINE_WEEKS);
    if (p && !p.error && p.baseline) value = p.baseline;
  } catch {}
  _coachBaseline = { at: Date.now(), value };
  return value;
}

function _coachShowRunning(container) {
  _coachSeq++; // drop any render still in flight
  _coachStopPoll();
  _coachBaseline = null;
  container.innerHTML = _coachReportHtml({ status: 'running' }, null, []);
}

async function _coachRun(container, sessionId, sidecar, btn) {
  setLoading(btn, true);
  let res;
  try {
    res = await window.plaud.coachingRun(sessionId);
  } catch (err) {
    res = { error: (err && err.message) || 'no response' };
  }
  if (!_coachStillShowing(container, sessionId)) return;
  if (!res || res.error) {
    setLoading(btn, false);
    toast(`Couldn’t start coaching: ${(res && res.error) || 'no response'}`, 'error');
    return;
  }
  _coachShowRunning(container);
  _coachStartPoll(container, sessionId, _coachRunMark(sidecar));
}

// Who's you decides whether your profile name is on a speaker (only once you picked
// yourself, and not when mixed up or excluded): re-read the names after such a change.
// refreshSpeakerNames lives in transcript.js. Never throws.
async function _coachRefreshNames(sessionId) {
  try {
    if (typeof refreshSpeakerNames === 'function') await refreshSpeakerNames(sessionId);
  } catch {}
}

async function _coachSaveOwner(container, sessionId, sidecar, btn) {
  const labels = [...container.querySelectorAll('input[name="coach-owner"]:checked')].map((i) => i.value);
  const mixedBox = container.querySelector('input[name="coach-mixed"]');
  const mixed = !!(mixedBox && mixedBox.checked);
  if (!labels.length) {
    toast('Tick at least one speaker', 'error');
    return;
  }
  const before = ((sidecar.owner && sidecar.owner.labels) || []).slice().sort().join('\n');
  const ownerChanged = sidecar.status === 'needs_owner' || labels.slice().sort().join('\n') !== before;
  setLoading(btn, true);
  let latest = sidecar;
  try {
    if (mixed !== !!sidecar.mixed_up) {
      // Saved at once; the response is the updated sidecar
      const res = await window.plaud.coachingMixedUp(sessionId, mixed);
      if (!res || res.error) throw new Error((res && res.error) || 'no response');
      latest = res;
      _coachBaseline = null; // mixed-up recordings are left out of "your usual"
    }
    if (ownerChanged) {
      // A mixed-up recording isn't used to learn your voice
      const res = await window.plaud.coachingSetOwner(sessionId, labels, !mixed);
      if (!res || res.error) throw new Error((res && res.error) || 'no response');
      const { started, ...saved } = res;
      if (started) {
        _coachRefreshNames(sessionId); // drawn by the poll once the run ends
        // Coaching runs again in the background. The response is the sidecar already
        // marked "running": draw it, and _coachDraw polls until the run ends.
        if (!_coachStillShowing(container, sessionId)) return;
        _coachSeq++; // drop any render still in flight
        _coachBaseline = null;
        if (saved.status === 'running') {
          _coachDraw(container, sessionId, saved, null);
        } else {
          // Not marked "running" (yet, or the run already ended): wait for an outcome
          // other than the one before the save, like Coach anyway
          _coachShowRunning(container);
          _coachStartPoll(container, sessionId, _coachRunMark(latest));
        }
        return;
      }
      latest = saved;
    }
  } catch (err) {
    toast(`Couldn’t save: ${(err && err.message) || 'no response'}`, 'error');
    if (_coachStillShowing(container, sessionId)) renderCoachingTab(container, sessionId);
    return;
  }
  const seq = ++_coachSeq;
  if (latest !== sidecar) await _coachRefreshNames(sessionId);
  const baseline = await _coachLoadBaseline();
  if (seq === _coachSeq && _coachStillShowing(container, sessionId)) _coachDraw(container, sessionId, latest, baseline);
}

// "Don't coach this recording": confirm, then the backend deletes the report and keeps
// any run from saving one. The response is the excluded sidecar, drawn as it is.
async function _coachExclude(container, sessionId, btn) {
  const choice = await showConfirmDialog({
    title: 'Don’t coach this recording?',
    message:
      'Open Pin deletes this recording’s coaching report and leaves it out of your progress. It won’t be coached again, '
      + 'and won’t be used to learn your voice, unless you choose Coach this recording.',
    actions: [{ id: 'exclude', label: 'Don’t coach it', destructive: true }],
    cancelLabel: 'Cancel',
  });
  if (choice !== 'exclude') return;
  setLoading(btn, true);
  let res;
  try {
    res = await window.plaud.coachingExclude(sessionId, true);
  } catch (err) {
    res = { error: (err && err.message) || 'no response' };
  }
  if (!res || res.error) {
    setLoading(btn, false);
    toast(`Couldn’t turn coaching off for this recording: ${(res && res.error) || 'no response'}`, 'error');
    return;
  }
  _coachBaseline = null; // the report no longer counts towards "your usual"
  _coachRefreshNames(sessionId);
  toast('This recording won’t be coached', 'success');
  if (!_coachStillShowing(container, sessionId)) return;
  _coachSeq++; // drop any render still in flight
  _coachDraw(container, sessionId, res, null);
}

// "Coach this recording" on an excluded recording: clear the exclusion, then run (force)
async function _coachInclude(container, sessionId, btn) {
  setLoading(btn, true);
  let res;
  try {
    res = await window.plaud.coachingExclude(sessionId, false);
  } catch (err) {
    res = { error: (err && err.message) || 'no response' };
  }
  if (!_coachStillShowing(container, sessionId)) return;
  if (!res || res.error) {
    setLoading(btn, false);
    toast(`Couldn’t start coaching: ${(res && res.error) || 'no response'}`, 'error');
    return;
  }
  _coachRefreshNames(sessionId);
  await _coachRun(container, sessionId, res, btn);
}

// The same place the Progress view's "set up your voice" link opens
function _coachOpenVoiceSettings() {
  openSettings();
  loadSettings();
  showSettingsTab('ai');
  const section = document.querySelector('#voice-coaching-section');
  if (section) section.scrollIntoView({ block: 'start' });
}

function _coachTogglePicker(container, open) {
  const picker = container.querySelector('[data-coach-picker]');
  if (picker) picker.classList.toggle('hidden', !open);
  const change = container.querySelector('[data-coach-action="change-owner"]');
  if (change) change.setAttribute('aria-expanded', String(open));
}

/** Render the Coaching tab for a recording into container (#transcript-coaching). */
async function renderCoachingTab(container, sessionId) {
  const seq = ++_coachSeq;
  _coachStopPoll();
  if (container.dataset.sid !== sessionId) {
    container.dataset.sid = sessionId;
    container.innerHTML = _coachSpinnerHtml('Loading coaching…');
  }
  let sidecar;
  let baseline = null;
  try {
    [sidecar, baseline] = await Promise.all([window.plaud.coaching(sessionId), _coachLoadBaseline()]);
  } catch (err) {
    sidecar = { error: (err && err.message) || 'no response' };
  }
  if (seq !== _coachSeq || !_coachStillShowing(container, sessionId)) return;
  _coachDraw(container, sessionId, sidecar || {}, baseline);
}

/** Draw one sidecar into the tab and wire its buttons; polls while it's running. */
function _coachDraw(container, sessionId, sidecar, baseline) {
  _coachStopPoll();
  const t = app.transcripts[sessionId];
  container.innerHTML = _coachReportHtml(sidecar, baseline, (t && t.segments) || [], (t && t.speaker_names) || {});

  container.onclick = (e) => {
    const seek = e.target.closest('[data-coach-seek]');
    if (seek) {
      coachSeek(parseFloat(seek.dataset.coachSeek));
      return;
    }
    const btn = e.target.closest('[data-coach-action]');
    if (!btn || btn.disabled) return;
    const action = btn.dataset.coachAction;
    if (action === 'change-owner') _coachTogglePicker(container, btn.getAttribute('aria-expanded') !== 'true');
    else if (action === 'cancel-owner') _coachTogglePicker(container, false);
    else if (action === 'run') _coachRun(container, sessionId, sidecar, btn);
    else if (action === 'save-owner') _coachSaveOwner(container, sessionId, sidecar, btn);
    else if (action === 'reload') renderCoachingTab(container, sessionId);
    else if (action === 'voice-settings') _coachOpenVoiceSettings();
    else if (action === 'exclude') _coachExclude(container, sessionId, btn);
    else if (action === 'include') _coachInclude(container, sessionId, btn);
  };
  container.onchange = () => {
    const save = container.querySelector('[data-coach-action="save-owner"]');
    if (save) save.disabled = !container.querySelector('input[name="coach-owner"]:checked');
  };

  if (sidecar.status === 'running') _coachStartPoll(container, sessionId);
}

/** Jump to t seconds: play the audio from there and show that line in the transcript. */
function coachSeek(t) {
  const time = Number(t);
  if (!Number.isFinite(time) || time < 0) return;
  const sid = app.currentSessionId;
  const rec = app.recordings.find((r) => r.session_id === sid);
  if (!sid || !rec) return;
  const tr = app.transcripts[sid];
  showTranscriptTab('transcript', sid, rec, tr);

  // The line containing t, else the one starting nearest it (review times can
  // sit up to _COACH_TIME_TOLERANCE_S before the line they cite).
  const segs = (tr && tr.segments) || [];
  let idx = segs.findIndex((s) => s.start <= time && time < s.end);
  if (idx < 0) {
    let best = Infinity;
    segs.forEach((s, i) => {
      const d = Math.abs(s.start - time);
      if (d < best) {
        best = d;
        idx = i;
      }
    });
  }
  const row = idx >= 0 ? document.querySelector(`#transcript-segments .segment[data-index="${idx}"]`) : null;
  if (row) {
    row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    row.classList.add('coach-seek-flash');
    setTimeout(() => row.classList.remove('coach-seek-flash'), 1600);
  }

  if (dom.audioEl.getAttribute('src')) {
    dom.audioEl.currentTime = time;
    dom.audioEl.play().catch(() => {});
  }
}
