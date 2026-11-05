# Engagement score and laughter: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Detect laughter on the Mac and add a measured Engagement score (laughs after your lines, follow-up questions to you, points built on) to coaching reports and Progress.

**Architecture:** `app/coaching/laughter.py` turns audio into laughter events with the CED-Tiny ONNX model. `app/coaching/engagement.py` turns events, turns and the review into the engagement block. The runner stores it in the sidecar and copies its parts into `metrics.owner`, so Progress tracks them. The coaching tab shows it.

**Tech stack:** Python 3.12, onnxruntime and numpy (diarize extra, lazy import), soundfile, Flask, plain-JS UI.

**Spec:** `docs/superpowers/specs/2026-11-05-engagement-design.md`. It is binding; every exact value lives there.

## Global constraints

- Everything numeric comes from the spec, verbatim:
  - Model: revision, SHA-256 and size.
  - Windows: 2.0 s with a 0.5 s step, batches of 64.
  - Normalisation: -20 dBFS target, -60 dBFS skip.
  - Laughter classes: 16 to 21. Threshold 0.15.
  - Events: dropped over 8 s. A laugh counts within 2.0 s after your turn, or overlapping its last 1.0 s. Own laugh: loudness of at least your median minus 3 dB.
  - Follow-ups: a `?` within the first 30 words. Turns under 3 words don't count. Shares need at least 3 turns or reactions.
  - Targets: 6 per hour, 0.25, 0.5. Over 3 hours: skip laughter.
- No new dependencies. No AI calls added.
- Tests stub onnxruntime; a real-model test is skipped unless the model file exists.
- British English, no em dashes, no `Co-Authored-By` trailers. Test commands as in the MCP plan: `~/.venvs/plaud-v2/bin/python -m pytest -q -p no:cacheprovider` (keys link in the worktree, removed before committing) and `ruff check app/ src/ tests/` plus `ruff format --check` on touched files.

## Review focus

1. **Your own laugh** counted as a laugh you got: the loudness rule. A test with a loud event just after your turn must not count it. Task 2.
2. **Background laughter** (a long event) counted. Events over 8 s are dropped. Task 1.
3. **Missing parts treated as 0:** a recording with no local audio or AI review off must not get a low score just because a part is missing. Task 2.
4. **Laugh times cited as evidence** must survive grounding, and must not let the AI cite arbitrary times. Task 2.
5. **An hour of audio held in memory,** or a model download that isn't checked. Task 1.

---

### Task 1: Laughter detector

**Files:** create `app/coaching/laughter.py` and `tests/test_coaching_laughter.py`.

**Produces:**
- `MODEL_URL`, `MODEL_SHA256`, `MODEL_SIZE`, `THRESHOLD = 0.15`, `MAX_EVENT_S = 8.0`.
- `model_path() -> Path`.
- `runtime_available() -> bool`: onnxruntime and numpy import.
- `ensure_model() -> tuple[bool, str]`: downloads if missing, then checks size and SHA-256, writing atomically. Returns `(True, "")` or `(False, user-facing reason)`.
- `detect(audio_path: str, max_seconds: float = 3 * 3600) -> dict`. It never raises and returns:
  - `{"status": "done", "events": [{"start": float, "end": float, "score": float, "loudness_db": float}], "reason": ""}`, or
  - `{"status": "unavailable", "events": [], "reason": str}`.

  Reasons: "Laughter detection needs the speaker model (Settings › Transcription & AI)", "Couldn't download the laughter model", "No audio on this Mac", "Too long to check for laughter".

**Steps:**
- [ ] Write failing tests:
  - window maths: count and step, padding of the last window
  - normalisation: gain for a quiet window, near-silence left alone
  - class max and threshold
  - merging into events with the ±0.5 s edges
  - events over 8 s dropped
  - `loudness_db` is the raw value
  - chunked reading: never reads the whole file at once (patch `soundfile.read` to assert `frames` is bounded)
  - download: bad SHA → file removed, `(False, reason)`; good → atomic
  - `detect` with no runtime or no audio → unavailable
  - a real-model test, skipped unless `model_path()` exists, on a generated clip (synthetic noise must give no events)
- [ ] Run them: they fail.
- [ ] Implement. The session runs on the CPU provider with `intra_op_num_threads = min(4, os.cpu_count())`. The input name comes from `session.get_inputs()[0].name`.
- [ ] Run the full suite: it passes. Commit "Coaching: on-device laughter detection".

### Task 2: Engagement block, runner, review, progress

**Files:** create `app/coaching/engagement.py`. Modify `app/coaching/targets.py`, `app/coaching/runner.py`, `app/coaching/review.py`, `app/coaching/progress.py`. Tests: `tests/test_coaching_engagement.py`, plus additions to the runner, review and progress tests.

**Consumes:** `laughter.detect(audio_path)` from Task 1, built in parallel. Import it as a module and stub it in tests.

**Produces:**
- `engagement.compute(segments, owner_labels, laughs: list[dict], laughter_status: str, laughter_reason: str, review: dict | None, duration: float, owner_loudness_db: float | None) -> dict`. It returns the sidecar block in the spec's exact shape: `score`, `laughs`, `laughs_count`, `laughs_per_hour`, `follow_ups`, `follow_up_share`, `built_on`, `built_on_share`, `laughter_status`, `laughter_reason`.
- `engagement.owner_loudness_db(audio_path, segments, owner_labels) -> float | None`: chunked reading, as in `voice.loudest_label`.
- `TARGETS` keys `laughs_per_hour` (low 6), `follow_up_share` (low 0.25) and `built_on_share` (low 0.5), with labels:
  - "Laughs after your lines (per hour)"
  - "Follow-up questions to you (share of your turns)"
  - "Your points others built on (share)"
- `sidecar["engagement"]`, and the three keys copied into `metrics["owner"]`.
- `progress` series `engagement_score`.

**Steps:**
- [ ] Write failing tests, one per Review focus item 1 to 4, plus:
  - the 2.0 s and 1.0 s window edges
  - one laugh per turn
  - backchannel turns excluded
  - a `?` after 30 words not counted
  - the score maps and means only the available parts
  - review off: `built_on` None and the score is still computed
  - the runner stores the block and survives `detect` raising or being unavailable
  - laughs in the review prompt
  - laugh times accepted by `ground` but other made-up times still dropped
  - Progress shows `engagement_score` and the three series
- [ ] Run them: they fail.
- [ ] Implement. Engagement is computed after the review, so `built_on` can use it. Laughter detection runs before the review, so the prompt can include the laughs. Store everything in one commit of fields.
- [ ] Run the full suite: it passes. Commit "Coaching: engagement score from laughs, follow-ups and points built on".

### Task 3: UI, AI tools, docs

**Files:** modify `app/ui/coaching.js` and `app/css/coaching.css` (the Engagement section), `app/ui/coaching-progress.js` (the score with the scores, the parts with the measures, if not automatic), `app/mcp/library.py` (`coaching.engagement`), `docs/ARCHITECTURE.md`, `SECURITY.md` and `README.md` (including the CED-Tiny credit). Tests: additions to `tests/test_coaching_ui.py`, `tests/test_coaching_progress_ui.py` and `tests/test_mcp_api.py`.

**Consumes:** the sidecar block shape from the spec, built in parallel by Task 2. Use fixtures.

**Steps:**
- [ ] Write failing tests:
  - the section renders all three rows with chips that seek
  - unavailable laughter shows the reason
  - built-on is hidden with the review off
  - the chuckle caveat appears once
  - names are applied to quoted lines
  - every field is escaped
  - Progress lists Engagement
  - MCP `get_recording` includes `coaching.engagement`
- [ ] Run them: they fail.
- [ ] Implement, following the existing coaching tab patterns.
- [ ] Run the full suite: it passes. Commit "Engagement in the coaching tab, Progress and AI tools; docs".

## Order

Tasks 1, 2 and 3 run in parallel against the contracts above. Then the final review, and release when the owner asks.
