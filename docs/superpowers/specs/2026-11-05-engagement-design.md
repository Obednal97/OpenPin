# Engagement score and laughter: design

Date: 5 November 2026. Status: agreed with the owner in conversation. The owner said to build on the synthetic calibration for now.

## Intent

The owner wants coaching to show how well they keep people engaged, especially over time:
- **when someone laughs after they say something, and how often**
- **when people ask follow-up questions**
- **when people build on what they said**

These make up a new, separate **Engagement score** (owner's choice), shown in each report and tracked in Progress. Laughter also informs the existing charisma score.

## Goals

1. **Laughter detection on the Mac**, with no cloud calls and no new Python dependency.
2. **Per report,** an Engagement section with:
   - laughs after your lines
   - follow-up questions to you
   - points of yours that others built on
   - the score

   Each item cites moments you can click to hear.
3. **Progress:** the Engagement score and its parts as weekly series, like the other measures.
4. **Charisma:** the AI review is told about the laughs, so its charisma score can use them.

## Non-goals

- Judging other people. As with all coaching, only the user is coached.
- Telling jokes apart from other causes of laughter. A laugh straight after your line counts.
- Catching every chuckle. The model misses short, soft chuckles, so counts run low, and the report says so once.

## Decisions

| Topic | Decision |
|---|---|
| Separate score | Engagement is its own 0-100 score, next to the AI scores, marked "measured" |
| Parts | Laughs after your lines; follow-up questions to you; points built on (AI review only) |
| Model | CED-Tiny ONNX, Apache-2.0. Revision `ace276d29dd0bb3f3517b0fa8cf300738c409019`, file `model.onnx`, SHA-256 `3890f227660be71886ff48633d11a731993e0206db8a486781412c28a0873435`, 6,681,767 bytes |
| Calibration | Synthetic tests only for now (owner's call) |

## How it works

### Laughter detection (`app/coaching/laughter.py`)

- **Download:**
  - Source: `https://huggingface.co/mispeech/ced-tiny/resolve/<revision>/model.onnx`.
  - Destination: Application Support `<support dir>/models/ced-tiny.onnx`. Use the same support folder logic as `app/mcp/access.py` (release `com.plaud.v2`, dev `com.plaud.v2.dev`).
  - Checks: size and SHA-256 must match, or the file is deleted. Written atomically.
  - It downloads on first need, but only when `onnxruntime` and `numpy` import (the voice model's diarize extra is installed). The user does nothing extra.
  - If it can't download or run, the laughter part is "not available" with a reason, and the rest of coaching carries on.
- **Audio:**
  - Convert with `src.transcribe._to_wav16k(audio)` to 16 kHz mono WAV. Read with `soundfile` in chunks, so an hour never sits in memory.
  - Windows of 2.0 s, a step of 0.5 s, batches of 64.
  - Per-window loudness normalisation: scale so the loudest 25 ms frame is at -20 dBFS RMS (gain = 0.1 / peak frame RMS). Leave near-silent windows (peak under -60 dBFS) as they are.
  - Also keep each window's raw peak-frame RMS in dBFS.
- **Scoring:**
  - A window's score is the maximum over AudioSet classes 16-21 (Laughter, Baby laughter, Giggle, Snicker, Belly laugh, Chuckle/chortle). The ONNX file already outputs probabilities.
  - A window counts as laughter when its score is at least **0.15**.
- **Events:**
  - Consecutive laughter windows merge into one event: start = first window's start + 0.5 s, end = last window's end - 0.5 s, plus the peak score and the raw loudness (the maximum raw peak-frame dBFS).
  - Events longer than **8 s** are dropped as steady background, such as TV or a crowd.
- **Speed and threads:** about 20 s per hour of audio with 4 threads. Use `min(4, cpu count)` threads.

### Engagement parts (`app/coaching/engagement.py`, pure functions)

Inputs: segments, owner labels, laughter events, review (may be None) and duration. All times are seconds.

- **Laughs after your lines:**
  - A laughter event counts when it starts within **2.0 s after the end of one of your turns** (`metrics.owner_turns`), or overlaps the last 1.0 s of it.
  - It isn't counted when it's probably **your own laugh**: its raw loudness is at least your median speech loudness minus 3 dB, since your clip-on mic hears you loudest.
  - Measure your median speech loudness from your labelled segments, as `voice.loudest_label` already does.
  - At most one counted laugh per turn.
  - Output:
    - `laughs`: list of `{t, line_start, line_text}`, where `t` is the laugh start and `line_start` your turn's start.
    - `laughs_count`
    - `laughs_per_hour`: per hour of conversation.
- **Follow-up questions to you:**
  - The turn straight after one of your turns, by another speaker, counts when it contains a `?` within its first 30 words.
  - Your turns of under 3 words (backchannels) don't count.
  - Output:
    - `follow_ups`: list of `{t, text}`
    - `follow_up_share`: follow-ups divided by your qualifying turns. None under 3 turns.
- **Built on** (AI review on only):
  - Of the review's `reactions` to your points, the share whose `response` is `engaged` or `follow_up`.
  - Output: `built_on_share`, with `built_on: {count, of}`. None when the review is off or has fewer than 3 reactions.
- **Score:**
  - Each available part maps to 0-100 against its target:
    - `laughs_per_hour`: at or above 6 → 100, linear down to 0 at 0.
    - `follow_up_share`: at or above 0.25 → 100, linear down to 0.
    - `built_on_share`: at or above 0.5 → 100, linear down to 0.
  - The score is the rounded mean of the available parts. None when no part is available.
  - A part is "not available" when laughter can't run (no model, no local audio) or there are too few turns or reactions. Missing parts never count as 0.
- **Targets:** add the three parts to `app/coaching/targets.py` `TARGETS`, with labels and `low` set to the values above (`high: None`), so Progress tracks them like other measures. Laughs only count when the laughter part ran.

### Runner and review

- `runner._analyse` runs laughter detection after the metrics, when the audio is on this Mac and the model is available or downloadable.
  - Timing budget: skip detection, with the reason "too long to check for laughter", for recordings over 3 hours.
- It stores `sidecar["engagement"] = {score, laughs, laughs_count, laughs_per_hour, follow_ups, follow_up_share, built_on, built_on_share, laughter_status: "done"|"unavailable", laughter_reason}`.
  - The built-on part is computed after the review.
  - Engagement fields are also copied into `metrics["owner"]` under the three target keys, so `progress.py` series pick them up with no special case.
- **The review's user prompt** gets one line per counted laugh: `[mm:ss] laugh after your line: "<first 80 chars>"`.
  - The system prompt says these laughs are measured, single-mic and may miss soft chuckles, and that charisma may consider them.
  - Laugh times count as valid evidence times in `review.ground` (pass them as allowed times alongside the line starts).
- **AI review off or Local without Ollama:** engagement still has laughs and follow-ups.
- **Excluded recordings:** nothing runs, as today.
- **Progress:**
  - `compute_progress` adds a series `engagement_score` (read from `sidecar["engagement"]["score"]`). It's shown in the score list like `overall_score`, but `overall_score` stays the mean of AI scores only.
  - The three parts are series through `TARGETS`.

### UI

- **Coaching tab:** a new "Engagement" section after the measured metrics.
  - The score, with "measured".
  - Three rows:
    - "Laughs after your lines: N (X per hour)", with time chips that seek.
    - "Follow-up questions to you: N of M of your turns", with chips.
    - "Your points others built on: N of M" (AI review only).
  - A muted line once: "Laughter detection can miss quiet chuckles."
  - When laughter is unavailable, that row says why.
- **Progress view:** Engagement appears with the scores, and its parts appear with the measures.
- **AI tools:** the MCP `get_recording` coaching block includes `engagement`. Check `app/mcp/library.py`'s coaching fields.
- Copy: British English, no em dashes. Names apply as elsewhere (`applySpeakerNames`) where speaker labels appear.

## Constraints

- No new dependencies: onnxruntime and numpy are already in the diarize extra, and the import is lazy.
- No AI calls added.
- Tests stub the ONNX session. One real-model test runs only when the model file is available locally (skipped otherwise).
- British English, no em dashes, no `Co-Authored-By` trailers.
- New files are covered by the existing `bundle.resources` globs (`app/coaching/*`). Check `test_bundle_ships_every_app_package`.
- `SECURITY.md` and `README.md`: one line each on the laughter model (local, from Hugging Face, checked). `ARCHITECTURE.md`: a coaching subsection on engagement.
- Credit: add CED-Tiny (Apache-2.0, mispeech) to the README's acknowledgements or third-party notices. Check where existing credits live.

## Left to the builder

Exact UI layout, following the coaching tab's patterns; how the threads are set; internal function names.
