# Recognising People by Voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Name speakers automatically from their voices (the owner and everyone he has named), with a People directory in Settings, a name picker, and names flowing to Notion.

**Architecture:** A new `app/people/` package holds the voice store (`voices.json`, owner plus people), a per-recording embedding cache, pure matching and calibration, how matches are applied to transcripts, and one background worker for learning, matching and the backlog. Coaching's `app/coaching/voice.py` keeps its API but reads and writes the owner (`me`) through the new store. Transcripts gain `speaker_voice_matches` and `speaker_names_frozen`. `speaker_names.effective_names` resolves typed name, then your coaching pick, then voice match.

**Tech Stack:** Python 3.12 + Flask, plain-JS frontend (`app/ui/*.js`, global scope), Tauri v2, WeSpeaker via the optional `diarize` extra, pytest + ruff.

**Spec:** `docs/superpowers/specs/2026-11-11-voice-people-design.md`

## Global Constraints

- No new Python dependency. numpy/soundfile/wespeakerruntime stay lazily imported (`import app.people.*` must work without them).
- Voice data never leaves the Mac and never goes through the MCP connector: no vectors in any `/people*` or MCP response.
- Every top-level name in `app/ui/*.js` must be unique across all those files (prefix new ones `_ppl` in `people.js`, `_txPick` in `transcript.js`).
- Transcript writes under `get_transcript_lock(session_id)`. The voice store has its own `threading.Lock`. Never hold either lock while computing embeddings.
- Store files: mode 0600, atomic write (temp file in the same dir, then `os.replace`).
- Storage location (deviation from the spec, which said `COACHING_DIR`): `PEOPLE_DIR = DATA_DIR / "people"`, holding `voices.json`, `backlog.json` and `embeddings/<session_id>.json`. `COACHING_DIR` globs `*.json` as sidecars (`store.is_sidecar_stem`), so a new file there would be read as a recording. The v1 mirror stays at `COACHING_DIR / "voice_profile.json"`.
- Constants (in `app/people/matching.py` unless stated): `SAMPLE_CAP = 20`, `MIN_LEARN_SECONDS = 15.0`, `AMBIGUITY_MARGIN = 0.05`, `DEFAULT_THRESHOLD = 0.70`, `CALIBRATION_MIN_INSTANCES = 20`, `MAX_WRONG_RATE = 0.02`, thresholds `0.45..0.80` step `0.025`. Coaching's `MATCH_THRESHOLD`/`MARGIN` in `voice.py` stay unchanged.
- Config keys: `people_match_threshold` (float or absent), `people_calibration` (`{"instances": int, "accuracy": float, "threshold": float, "at": iso}`).
- Copy: British English, no em dashes, plain words. Commits: no `Co-Authored-By` trailer (owner's rule overrides the harness default).
- Tests: `~/.venvs/plaud-v2/bin/python -m pytest -q` and `~/.venvs/plaud-v2/bin/ruff check app tests scripts`. Baseline failures already on master are listed in memory `reference_test_venvs`: compare against master before blaming this branch.
- The `app/people/` package must be added to `src-tauri/tauri.conf.json` `bundle.resources` (`"../app/people/*": "app/people/"`); `test_bundle_ships_every_app_package` guards it.

## Review Focus

1. **Owner with no profile name:** `me` matches but there's no name, so the label must stay "Speaker N" (not "None", not empty) everywhere, including Notion. Test: Task 4 `test_me_match_without_profile_name_shows_label`.
2. **Re-transcription mid-flight:** the worker computed matches for an old labelling, and the transcript was re-diarised before the write. The write must be dropped, not applied to new labels. Test: Task 5 `test_apply_matches_drops_stale_labelling`.
3. **Two typed spellings of one person in the backlog** ("will", "Will"): one person, not two. Different names ("Will", "William Moss") stay two, so they can be merged. Test: Task 6 `test_backlog_groups_names_case_insensitively`.
4. **Voice model not installed or audio missing:** learning, matching and the backlog skip silently, `set_names` still saves, and the People section says why. Test: Task 5 `test_set_names_without_model_still_saves`.
5. **Deleting a person who is the only match on a recording already in Notion:** the name stays (frozen) and no Notion update is queued. Test: Task 7 `test_delete_person_freezes_names_without_notion_update`.

---

### Task 1: Voice store with the owner inside it

**Files:**
- Create: `app/people/__init__.py`, `app/people/store.py`
- Modify: `app/utils/constants.py` (add `PEOPLE_DIR = DATA_DIR / "people"`), `app/coaching/voice.py` (`_load_exemplars`, `_save_exemplars`, `delete_profile`, `PROFILE_PATH` use), `src-tauri/tauri.conf.json` (bundle `app/people/*`)
- Test: `tests/test_people_store.py`, keep `tests/test_coaching_voice.py` passing (its fixture patches `voice.PROFILE_PATH`; also patch `people.store.VOICES_PATH`)

**Interfaces:**
- Produces (`app/people/store.py`):
  - `VOICES_PATH: Path` (`PEOPLE_DIR / "voices.json"`), `ME = "me"`
  - Sample dict: `{"vec": list[float], "session_id": str | None, "label": str | None, "seconds": float, "added_at": str}`
  - `load() -> dict`: `{"version": 2, "people": {id: {"name": str | None, "is_me": bool, "samples": [...]}} }`. Migrates from `voice.PROFILE_PATH` (v1) on first load: copies it to `voice_profile.json.v1-backup`, imports exemplars as `me` samples (`session_id` None, `seconds` 0).
  - `mutate(fn: Callable[[dict], None]) -> dict`: under the store lock: load, `fn(data)`, trim, save, mirror `me` vectors to `voice.PROFILE_PATH` in v1 format (`{"version": 1, "exemplars": [...], "updated_at": ...}`).
  - `find_person(data, name: str) -> str | None`: case-insensitive, whitespace-collapsed match; `ME` is never returned by name here.
  - `create_person(data, name: str) -> str`: id `"p_" + secrets.token_hex(4)`. Raises ValueError if the name is taken (case-insensitive).
  - `add_sample(data, person_id, sample) -> None`: first removes any sample with the same `(session_id, label)` from **every** person, then appends.
  - `remove_sample(data, session_id, label) -> str | None`: returns the person it was removed from.
  - `trim_varied(samples: list[dict], cap: int = 20) -> list[dict]`: while over cap, drop the sample with the highest mean cosine to the others (ties: oldest `added_at`).
  - `person_summary(data) -> list[dict]`: `[{"id", "name", "is_me", "samples": int, "bytes": int, "last_heard": str | None}]`, with no vectors. `bytes` = `len(json.dumps(samples))`.

- [ ] **Step 1: Write failing tests** in `tests/test_people_store.py`:
  - `test_migrates_v1_profile_into_me`: write v1 `voice_profile.json` with 3 exemplars, `load()` gives `people["me"]["samples"]` of length 3 with `session_id is None`, and `voice_profile.json.v1-backup` exists.
  - `test_mutate_mirrors_me_to_v1_file`: after adding a `me` sample, `voice._load_exemplars()` returns the vectors, and the v1 file's `version == 1`.
  - `test_add_sample_moves_between_people`: a sample for `(s1, "Speaker 2")` added to Harry, then to Nadia: Harry has 0, Nadia 1.
  - `test_trim_keeps_varied`: 20 copies of A plus one B, trimmed to 20: B is kept and one A is dropped.
  - `test_find_person_ignores_case_and_spaces`: `"  harry   fisher"` finds `"Harry Fisher"`.
  - `test_create_person_rejects_duplicate_name`.
  - `test_files_are_0600`.
  - `test_summary_has_no_vectors`: `"vec"` not in `json.dumps(person_summary(...))`.
- [ ] **Step 2:** Run `~/.venvs/plaud-v2/bin/python -m pytest tests/test_people_store.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `app/people/store.py` as above. Point `voice._load_exemplars` at `[s["vec"] for s in store.load()["people"]["me"]["samples"]]`. Point `_save_exemplars(exemplars)` at a `mutate` that replaces `me` samples, keeping the existing samples whose `vec` equals one passed in, and wraps new ones as samples with `session_id` None. Point `delete_profile` at clearing `me` samples and unlinking the v1 file. `add_exemplars` and `replace_profile` keep their signatures and gain an optional `session_id: str | None = None` that is recorded on the new samples. Add the bundle entry.
- [ ] **Step 4:** Run the new tests plus `tests/test_coaching_voice.py tests/test_coaching_routes.py`, then the full suite. Expected: PASS (no new failures against master).
- [ ] **Step 5:** Commit `"People: voice store with the owner inside it"`.

### Task 2: Per-recording embedding cache

**Files:**
- Create: `app/people/embeddings.py`
- Modify: `app/coaching/voice.py` (`match_owner` takes `session_id: str | None = None` and uses the cache when given), `app/coaching/runner.py:173` (pass `session_id`), `app/routes/recordings.py` (`_trash_coaching_files` callers: also delete the cache file)
- Test: `tests/test_people_embeddings.py`

**Interfaces:**
- Consumes: `voice.speaker_embeddings(audio, segments)`, `runner.labelling_fingerprint(segments)`, `runner._resolve_audio(sid, None)`.
- Produces (`app/people/embeddings.py`):
  - `speech_seconds(segments) -> dict[str, float]`: per label, the sum of segment lengths for segments of 1.0 s or more.
  - `get(session_id: str, segments: list[dict], audio_path: str | None = None) -> dict[str, list[float]] | None`: cached `{label: vec}` for the current labelling. Otherwise computes it (needs the model and audio; returns None without them, never raises), saves `{"labelling", "embeddings", "seconds"}` 0600 and returns it.
  - `cached(session_id: str, segments) -> dict[str, list[float]] | None`: cache only; never touches audio.
  - `delete(session_id: str) -> None`.

- [ ] **Step 1: Failing tests:** `test_get_computes_once_then_reads_cache` (fake `speaker_embeddings` counts calls: 1 after two `get`s); `test_relabelled_transcript_recomputes`; `test_no_model_returns_none`; `test_speech_seconds_ignores_short_segments`; `test_delete_removes_file`; `test_match_owner_uses_cache_with_session_id`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Embedding runs outside any lock, and the save is atomic.
- [ ] **Step 4:** Run the new tests + `tests/test_coaching_runner.py tests/test_recordings.py`. Expected: PASS.
- [ ] **Step 5:** Commit `"People: cache each recording's speaker embeddings once"`.

### Task 3: Matching and calibration (pure)

**Files:**
- Create: `app/people/matching.py`
- Test: `tests/test_people_matching.py`

**Interfaces:**
- Produces:
  - `match(embeddings: dict[str, list[float]], people: dict, threshold: float, rejected: dict[str, list[str]] | None = None, exclude_session: str | None = None) -> dict[str, dict]`: returns `{label: {"person": id, "score": float}}`. Score per person = max cosine over their samples (ignoring samples whose `session_id == exclude_session`; people left with no samples are skipped). A label is named only if best ≥ `threshold` and best − second-best person ≥ `AMBIGUITY_MARGIN`, and the best person is not in `rejected[label]`. If the best is rejected, the label stays unnamed (it does not fall through to the second).
  - `calibrate(instances: list[tuple[str, str, list[float], str]], people: dict) -> dict`: instances are `(session_id, label, vec, true_person_id)`. Returns `{"threshold": float, "accuracy": float | None, "instances": int}`. Leave-one-recording-out via `exclude_session`. Picks the lowest threshold in the grid where `wrong / named < MAX_WRONG_RATE` with `named > 0`. If none qualifies: 0.80. With fewer than `CALIBRATION_MIN_INSTANCES`: `DEFAULT_THRESHOLD` and accuracy None.
  - `current_threshold() -> float | None`: `load_config().get("people_match_threshold")`.

- [ ] **Step 1: Failing tests:** `test_match_names_confident_speaker`; `test_match_below_threshold_unnamed`; `test_match_ambiguous_between_two_people_unnamed` (scores 0.80 vs 0.77); `test_two_labels_can_match_same_person`; `test_rejected_person_not_applied_and_no_fallthrough`; `test_calibrate_few_instances_uses_default`; `test_calibrate_picks_lowest_safe_threshold` (synthetic people where impostors score 0.6 and genuine 0.75 gives 0.625); `test_calibrate_single_recording_person_acts_as_stranger` (their only sample is excluded, so they're unnamed or wrong, and that counts against the threshold).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with plain-Python cosine (reuse `voice._cosine`).
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `"People: matching with an ambiguity rule, and calibration on the library"`.

### Task 4: Effective names from voice matches

**Files:**
- Modify: `app/utils/speaker_names.py` (`effective_names`, new `voice_matched`), `app/state/speakers.py` (`names_for`, `owner_changed`, `participants` cache key), `app/routes/recordings.py:972` (new `speaker_name_sources` field)
- Test: `tests/test_speaker_names.py` (update the tests that pin "never from a voice match alone"), new cases in `tests/test_people_names.py`

**Interfaces:**
- Transcript fields (written by Task 5): `speaker_voice_matches = {"labelling": str, "matches": {label: {"person": id, "score": float}}, "rejected": {label: [id]}}`, `speaker_names_frozen = [label]`.
- Produces:
  - `effective_names(transcript, sidecar, profile_name, people_names: dict[str, str | None] | None = None) -> dict[str, str]`. Order per label: typed name; else your coaching pick (`source == "user"`, as today) → profile name; else a voice match valid for the current labelling, with the recording not marked `mixed_up` → `people_names[person]` (for `me`, the profile name). A match whose person has no name, or a deleted person, gives nothing.
  - `voice_matched(transcript, sidecar, profile_name, people_names) -> dict[str, float]`: labels whose effective name came from a voice match → score.
  - `speakers.people_names() -> dict[str, str | None]` (from `people.store.load()`, `me` → profile name). `names_for` passes it. The participants cache key gains the `voices.json` mtime.
  - Transcript API adds `"speaker_name_sources": {label: {"source": "voice", "score": float}}` for voice-matched labels only.

- [ ] **Step 1: Failing tests:** `test_typed_name_beats_voice_match`; `test_voice_match_names_owner_without_coaching_pick`; `test_me_match_without_profile_name_shows_label`; `test_stale_labelling_match_ignored`; `test_mixed_up_recording_ignores_voice_matches`; `test_deleted_person_match_ignored`; `test_api_reports_voice_sources`. Change the existing "voice match alone never names" test to expect the new rule.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. `names_for` must not take the voice-store lock while the caller holds the transcript lock: `load()` reads an atomically written file without locking.
- [ ] **Step 4:** Run `tests/test_speaker_names.py tests/test_people_names.py tests/test_mcp_api.py tests/test_notion.py`. Expected: PASS.
- [ ] **Step 5:** Commit `"Speaker names: voice matches name speakers, typed names still win"`.

### Task 5: Worker, learning on rename, and matching new recordings

**Files:**
- Create: `app/people/apply.py`, `app/people/worker.py`
- Modify: `app/state/speakers.py` (`set_names` queues learning; typing over a match adds a rejection; clearing a frozen label's mark), `app/routes/transcribe.py:_after_transcript_final` (queue match)
- Test: `tests/test_people_worker.py`

**Interfaces:**
- Produces:
  - `apply.apply_matches(session_id, labelling: str, matches: dict) -> bool`: under the transcript lock, writes `speaker_voice_matches.matches`. Drops the write if the transcript's current fingerprint ≠ `labelling`. Keeps `rejected`. Returns whether the effective names changed, and if so calls `speakers._names_changed(session_id)` after releasing the lock.
  - `apply.add_rejection(session_id, label, person_id) -> None`.
  - `apply.match_recording(session_id, audio_path=None) -> bool`: skips when the threshold is None (not calibrated yet), the recording is mixed up, or there are no labels or embeddings. Uses `embeddings.get` and `matching.match` with that recording's `rejected`.
  - `worker.enqueue(kind: str, **kw) -> None`, kinds: `"match"(session_id, audio_path)`, `"learn"(session_id, label, name)`, `"unlearn"(session_id, label)`, `"rematch_person"(person_id)`, `"backlog"()`. One daemon thread, FIFO, de-duplicates identical pending jobs, never raises out of a job. `worker.wait_idle(timeout)` for tests.
  - learn: needs `embeddings.get` and `speech_seconds[label] ≥ MIN_LEARN_SECONDS`; `me` if the name equals the profile name (case-insensitive) and the recording isn't excluded from coaching; else `find_person`/`create_person`. `add_sample`, then enqueue `rematch_person`. Never learns from a frozen label or a mixed-up recording.
  - rematch_person: every transcript with an embedding cache (`embeddings.cached`, no audio work) is matched again via `match_recording`.

- [ ] **Step 1: Failing tests** (fake embeddings, a temporary data dir, a synchronous worker via `wait_idle`): `test_rename_learns_sample`; `test_rename_under_15s_learns_nothing`; `test_clear_name_unlearns`; `test_typing_over_match_moves_sample_and_rejects`; `test_profile_name_teaches_me`; `test_excluded_recording_never_teaches_me`; `test_mixed_up_recording_teaches_nobody`; `test_new_recording_matched_after_transcribe`; `test_learning_rematches_past_recordings_from_cache`; `test_apply_matches_drops_stale_labelling`; `test_set_names_without_model_still_saves`; `test_no_threshold_no_matching`; `test_match_change_queues_notion_full_update` (Notion linked → `notion_queue.enqueue(sid, KIND_FULL)`).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. `set_names` computes which labels changed versus the stored map before writing, then enqueues `learn`/`unlearn` after the lock is released.
- [ ] **Step 4:** Run the new tests + `tests/test_speaker_names.py tests/test_transcribe.py tests/test_mcp_api.py`. Expected: PASS.
- [ ] **Step 5:** Commit `"People: learn voices from names, match new recordings"`.

### Task 6: Backlog and calibration

**Files:**
- Create: `app/people/backlog.py`
- Modify: `app/people/worker.py` (`"backlog"` job), `app/api_server.py` (start the backlog once at startup when `backlog.json` says it isn't done)
- Test: `tests/test_people_backlog.py`

**Interfaces:**
- Produces:
  - `backlog.state() -> dict`: `{"phase": "idle"|"learning"|"calibrating"|"naming"|"done"|"cancelled", "done": int, "total": int}` (persisted in `PEOPLE_DIR/backlog.json` with the processed session ids per phase, so it resumes after a restart).
  - `backlog.run(cancel: threading.Event) -> None`. Phase learning: for every transcript (newest first) with typed, non-frozen names and not mixed up, `embeddings.get` and learn each named label (≥15 s; profile name → `me`). Coaching user picks also count as `me` instances. Phase calibrating: build instances from the same labels and call `matching.calibrate`, then save `people_match_threshold` and `people_calibration`. Phase naming: `match_recording` for every transcript, newest first.
  - `backlog.start() / cancel()`.
  - `backlog.maybe_recalibrate() -> None`: called after each `learn`. If the instance count is ≥ 2× `people_calibration.instances`, recalibrate (cached embeddings only).

- [ ] **Step 1: Failing tests:** `test_backlog_learns_then_calibrates_then_names`; `test_backlog_groups_names_case_insensitively`; `test_backlog_skips_frozen_and_mixed_up`; `test_backlog_resumes_after_restart` (stop after 2 of 4, new run processes only 2); `test_backlog_cancel_stops_after_current`; `test_nothing_named_before_calibration`; `test_recalibrates_when_instances_double`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS. Then the full suite.
- [ ] **Step 5:** Commit `"People: learn from past recordings, calibrate, then name them"`.

### Task 7: Directory routes: list, rename, merge, delete, reject

**Files:**
- Create: `app/routes/people.py` (register in `app/api_server.py`), `app/people/directory.py`
- Modify: `app/tauri-bridge.js` (bridge methods), `app/mcp/server.py` (the `set_speaker_names` description adds: "Names you set teach Open Pin that person's voice, so later recordings are named automatically.")
- Test: `tests/test_people_routes.py`

**Interfaces:**
- Produces (JSON; no vectors anywhere):
  - `GET /people/directory` → `{"me": {...summary, "name": profile or null}, "people": [summary + "recordings": int], "total_bytes": int, "model": bool, "backlog": backlog.state(), "calibration": config.people_calibration or null}`. `recordings` counts existing transcripts where that person is the effective name of some label.
  - `POST /people/<id>/rename {name}` → validates with `sn.validate_name`; 409 if another person has it. Rewrites typed names equal to the old name (case-insensitive) in every transcript, and calls `_names_changed` for each transcript whose effective names changed.
  - `POST /people/<id>/merge {into}` → samples moved and trimmed, matches repointed, typed names rewritten to the target's name; the source person is deleted; Notion queued where names changed.
  - `DELETE /people/<id>` → for each transcript with a match to it: write its name as a typed name, add the label to `speaker_names_frozen`, remove the match (no `_names_changed`: the shown name is the same; still `changes.bump()`). Then delete the person. `me` can't be deleted here (400; use `DELETE /coaching/voice`).
  - `POST /people/backlog` (start) and `DELETE /people/backlog` (cancel).
  - `POST /transcripts/speaker-names/reject {session_id, label}` → `apply.add_rejection` for the matched person; returns the transcript's new `speaker_names` and `speaker_name_sources`.
  - Bridge: `peopleDirectory()`, `renamePerson(id, name)`, `mergePerson(id, into)`, `deletePerson(id)`, `startPeopleBacklog()`, `cancelPeopleBacklog()`, `rejectVoiceMatch(session_id, label)`.
  - `GET /people` (existing suggestions) gains `"person_id"` and `"has_voice"` per item, with directory people listed first.

- [ ] **Step 1: Failing tests:** `test_directory_lists_me_and_people_without_vectors`; `test_rename_rewrites_typed_names_and_queues_notion`; `test_rename_to_existing_name_409`; `test_merge_combines_and_trims_to_20`; `test_delete_person_freezes_names_without_notion_update`; `test_delete_me_refused`; `test_reject_unnames_and_survives_rematch`; `test_mcp_people_has_no_vectors`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the new tests + `tests/test_mcp_server.py tests/test_tauri_parity.py`. Expected: PASS.
- [ ] **Step 5:** Commit `"People: directory routes (rename, merge, delete, reject)"`.

### Task 8: People section in Settings

**Files:**
- Create: `app/ui/people.js` (top-level names prefixed `_ppl`), `app/css/people.css`
- Modify: `app/index.html` (section `#people-section` after `#voice-coaching-section`; script and style tags; hint under `#profile-name`: "Used for your voice in transcripts. See People."), `app/ui/settings.js` (call `renderPeopleSettings()` where `renderVoiceSettings` is called)
- Test: `tests/test_people_ui.py` (static checks in the style of `tests/test_speaker_names_ui.py`: the section exists, the names are globally unique, the copy has no em dashes)

**Interfaces:**
- Consumes: the Task 7 bridge methods.
- Produces: `renderPeopleSettings()` (global).

Content (exact copy):
- Top line: `Open Pin recognises {n} people by voice. Voice data stays on this Mac (about {size}).`
- Calibration line, when present: `Tested on your recordings: {pct}% of the speakers it names are right.` Before calibration: `Open Pin will start naming speakers once it has learnt from your recordings.`
- You row: profile name, or a link `Add your name` that focuses `#profile-name`; `{n} of 20 samples`; link `Your voice & coaching`.
- Person rows: name, `{r} recordings`, `{n} of 20 samples`, size, `Last heard {date}`; buttons Rename, Merge into…, Delete. Delete confirmation: `Delete {name}'s voice? Open Pin will stop recognising them. Names in past transcripts stay.`
- Backlog: `Learn from past recordings` button, or progress `Learning voices: {done} of {total} recordings` / `Naming past recordings: {done} of {total}`, with Cancel. Polls `peopleDirectory()` every 2 s while it runs.
- Model missing: `Install the voice model to recognise people.` plus the existing install button behaviour (reuse the voice section's handler).

- [ ] **Step 1:** Failing static tests. **Step 2:** Run (FAIL). **Step 3:** Implement following the `#voice-coaching-section` and settings-section patterns and tokens in `app/css/tokens.css`. **Step 4:** Run the UI tests + `tests/test_settings_ui.py`, then launch the app with the `run` skill and screenshot the section (light and dark). **Step 5:** Commit `"Settings: People section"`.

### Task 9: Name picker, "Did you mean", and the voice-match hint

**Files:**
- Modify: `app/ui/transcript.js` (`_txSpeakerEditorHtml` and its handlers around lines 1096-1250; the speaker label rendering that shows names), `app/css/transcript.css`
- Test: `tests/test_speaker_names_ui.py` (extend)

**Interfaces:**
- Consumes: `window.plaud.people()` (now with `person_id`, `has_voice`), `rejectVoiceMatch`, the transcript's `speaker_name_sources`.
- Behaviour:
  - Replace the `<datalist>` with a filtered list under the input: people with a voice first (marked with a small voice icon), then other names, most used first; last row `Add new person: “{typed}”`. Arrow keys, Enter and Escape work.
  - Placeholder: `Full name, such as Harry Fisher`.
  - On save of a name not in the list: an exact match ignoring case silently uses the existing spelling. Otherwise, if a known name starts with the typed text as a whole first word, or is within Levenshtein distance 2 (case-insensitive), show inline: `Did you mean {known}?` with buttons `{known}` and `Keep “{typed}”`.
  - A voice-matched label shows its name with `title="Matched by voice"`. Its editor shows `Matched by voice.` with buttons `Not them` (calls `rejectVoiceMatch`) and the usual name field to change it.
  - Update `_TX_DEFAULT_NAME_HELP` for the new rule: `This is your name because Open Pin recognised your voice, or you picked this speaker in Coaching. Type another name to change it.`

- [ ] **Step 1:** Failing static tests (helper names exist and are unique; copy strings present; no em dashes); add a JS unit check only if the repo already has a JS test runner (it doesn't: keep the static tests). **Step 2:** Run (FAIL). **Step 3:** Implement. **Step 4:** Run the tests, then launch the app with the `run` skill against a fixture library: rename a speaker, see the picker and "Did you mean", and see a matched name's hint and Not them. **Step 5:** Commit `"Transcript: people picker, Did you mean, and voice-match hint"`.

### Task 10: Docs, eval script and version

**Files:**
- Create: `scripts/voice_eval.py` (loads the library from the app's data dir, builds instances like `backlog`, prints the threshold table: threshold, named, right, wrong, wrong %)
- Modify: `docs/ARCHITECTURE.md` (a "People and voices" section: store, cache, matching, worker, effective-name order), `SECURITY.md` (what `voices.json` and `embeddings/` hold, 0600, local only, never via MCP), `README.md` (a line under speaker names), `docs/superpowers/specs/2026-11-05-mcp-and-speaker-names-design.md` ("Your own name" superseded by 2026-11-11), version bump per the release recipe (minor: 2.10.0) in the files the recipe lists
- Test: `tests/test_people_eval_script.py` (runs `main()` against a fixture library with fake embeddings; prints a table)

- [ ] **Step 1:** Failing test. **Step 2:** Run (FAIL). **Step 3:** Implement + docs. **Step 4:** Full suite + ruff. Expected: no new failures against master. **Step 5:** Commit `"People: docs, eval script, v2.10.0"`.
