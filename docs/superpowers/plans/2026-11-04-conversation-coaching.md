# Conversation Coaching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every recording where the NotePin's owner talked with someone else gets an automatic coaching report (scores, blunt feedback, timestamped moments, words per minute per speaker, how the other person reacted). A Progress view shows the owner's trends over weeks.

**Architecture:** A new backend package `app/coaching/` holds pure metric computation, owner voice matching (local WeSpeaker embeddings), an LLM judgement step and a background runner. It writes one sidecar JSON per recording in `DATA_DIR/coaching/`. Deepgram transcription keeps verbatim word timings (with "um"/"uh") for coaching only; the transcript the user reads stays clean. A new Flask blueprint serves reports and progress. The frontend adds a Coaching tab, a Progress view and a "Your voice" settings section.

**Tech Stack:** Python 3.12 / Flask (backend, `app/`), plain-JS frontend with global-scope `<script>` files (`app/ui/*.js`), Tauri v2 shell (unchanged), pytest, ruff. Optional extra `diarize` (Silero VAD + `wespeakerruntime` ONNX, already pinned in `app/requirements/diarize.txt`).

**Spec:** `docs/superpowers/specs/2026-11-04-conversation-coaching-design.md`. Read it first: it holds the intent, anti-goals and the user's decisions. This plan doesn't repeat them.

---

## Orientation for a fresh agent

Start on the branch holding the spec and this plan: `git switch docs/conversation-coaching-design`, then `git switch -c feat/conversation-coaching`. Repo conventions:

- **Run tests:** `python3 -m pytest -q` (Homebrew Python has the deps; the app's own venv does not have pytest). Lint: `ruff check app tests && ruff format --check app tests`. About 5 tests in `test_validate.py`, `test_summarize.py` and `test_security.py` fail before you start (environment SDK and werkzeug versions). Leave them alone, and don't count them as yours.
- **Test isolation:** `tests/conftest.py` sets `PLAUD_DATA_DIR` to a temp folder before any `app.*` import. Its autouse fixtures stub real Bluetooth (`_system_connected_notepins`) and AI title generation. Add an autouse stub for anything new that would touch the network, the microphone, model downloads or real embeddings.
- **Frontend gotcha:** all `app/ui/*.js` files share one global scope (plain `<script>` tags, `app/index.html:992-999`). Module-level names must be unique across files. A duplicate `let`/`const` breaks the whole renderer. Prefix new globals with `coach`/`_coach`.
- **Frontend API:** the renderer calls the backend through `window.plaud.*` in `app/tauri-bridge.js` (`apiCall(method, path, body)`). Add new methods there. Long calls go in `NO_TIMEOUT_ENDPOINTS`. `tests/test_tauri_parity.py` checks that the bridge's surface matches the Flask routes.
- **Commits** end with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  ```

### Code facts this plan relies on (checked against v2.6.1)

| What | Where |
|---|---|
| Transcript JSON `DATA_DIR/transcripts/<sid>.json`; `_TRANSCRIPTION_KEYS`, `_merge_transcript()` (takes the per-session lock itself; sets `summary_stale` when the text changes) | `app/routes/transcribe.py:97-142` |
| Deepgram call: `transcribe_deepgram(audio_path, api_key, language=None, diarize=False, keywords=None)` → `{text, segments, duration, language, speakers}`. It builds segments from `results.utterances` and discards `alt.words`. Speakers are labelled `"Speaker {n+1}"` | `app/providers/transcribe.py:223-288` |
| Cloud result → segments merged by speaker with `_merge_speaker_segments`, then `_merge_transcript` | `app/routes/transcribe.py:427-470` |
| The three places where the transcript and speaker labels are final, each calling `fire_webhooks("transcribe", …)`: end of `_background_diarize` (`:223-234`), cloud no-bg-diarize branch (`:494-508`), local no-bg-diarize branch (`:592-604`). `_background_diarize` drops its result if `td["text"] != text` | `app/routes/transcribe.py` |
| Background jobs are `threading.Thread(daemon=True)`. Writes take `get_transcript_lock(session_id)` | `app/utils/transcript_locks.py` |
| Summary cascade `summarize(full_text, config, tag_context="", segments=None)`: providers Anthropic SDK (`_anthropic_client`, streamed, `output_config.format=json_schema`), OpenAI / OpenRouter chat completions (`response_format` json_schema), Ollama (`format=<schema>`, `ollama_models()`, `pick_ollama_model()`), DistilBART. Helpers `_describe_error`, `_PROVIDER_GROUP`, `_PREFERENCE_GROUP`, `format_transcript(segments, full_text)`. Profile context is built in the `/summarize` route (`app/routes/transcribe.py:658-690`) | `app/providers/summarize.py` |
| Settings: GET defaults `diarize: False`, POST whitelist `_apply(config)` | `app/routes/settings.py:45-75, 110-160` |
| Config: `load_config()`, `update_config(fn)` (locked, atomic) | `app/utils/config.py` |
| Blueprints registered | `app/api_server.py:237-245` |
| Session-ID validation: `validate_session_id(sid) -> str` (raises on bad IDs) | `app/utils/paths.py:56` |
| Local diarization: `_load_diarize_lib()` installs the extra via `ensure_installed("diarize", extra="diarize")`; `_to_wav16k(audio_path) -> str \| None` | `src/transcribe.py:331-404` |
| Embeddings: `wespeakerruntime.Speaker(lang="en").extract_embedding(wav_path) -> np.ndarray` (256-dim); `diarize.embeddings.extract_embeddings(audio, speech_segments)` | installed `diarize` extra |
| Recording view tabs: `#transcript-tabs` (Transcript, Summary), `showTranscriptTab(tabName, sessionId, rec, t)`; seek with `dom.audioEl.currentTime = start` | `app/index.html:290-297`, `app/ui/transcript.js:760-835` |
| Header icon buttons (where a Progress button goes): `#settings-btn` | `app/index.html:58` |

## Global Constraints

- Voice embeddings and verbatim words never leave the Mac. The only text sent out is to the user's existing summary AI provider (spec §7).
- The transcript the user reads (`segments`, `text`, `.txt`), the summary and the Notion push stay free of filler tokens.
- Every AI judgement and moment cites a timestamp that exists in the recording; anything else is dropped (spec "Grounding rule").
- Coaching never analyses a guessed owner: with no confident match, status is `needs_owner` (spec §2).
- Interruptions are always labelled "likely" (single-channel audio; spec §3).
- Recording-level skips: fewer than 2 non-owner-merged speakers, no speaker labels, or duration < `MIN_COACHING_SECONDS = 120`.
- Targets (exact, from spec §3): pace 110–160 WPM; talk share 40–60%; longest monologue ≤ 150 s; likely interruptions < 0.10 per owner turn; patience 0.5–1.5 s; questions 10–18 per hour; fillers 0.6–3.0 per minute; interactivity ≥ 5 switches per 5 min.
- `coaching_auto` defaults to `True`. When coaching ships, an install with `diarize` false gets it switched on once. Turning `diarize` off also turns `coaching_auto` off.
- British English in user-facing copy (the owner's preference); match the app's existing tone.

## Review Focus

1. **Owner split across labels so only the owner spoke.** All labels are ticked as "you", leaving no other speaker. Expect `status: "skipped"` with reason "only you spoke", not a report with a 100% talk share. (Task 7)
2. **Re-transcribed or edited while coaching runs.** Expect the stale result to be dropped, as `_background_diarize` does, and never written over a newer transcript. (Task 7)
3. **OpenAI estimated timestamps (`timestamps_estimated: true`).** Expect pace and interruptions to be shown as "unavailable", not computed from fake times. (Task 3)
4. **The AI cites a time not in the recording, or returns broken JSON.** Expect the item to be dropped; with broken JSON, `failed` with the provider errors, not a crash. (Task 6)
5. **No voice profile and the `diarize` extra not installed.** Expect `needs_owner` with the speakers' first lines, and no install triggered from the runner. (Tasks 4, 7)

---

### Task 1: Coaching sidecar store

**Files:**
- Create: `app/coaching/__init__.py` (empty), `app/coaching/store.py`
- Modify: `app/utils/constants.py` (add `COACHING_DIR = DATA_DIR / "coaching"` next to `TRANSCRIPTS_DIR`)
- Test: `tests/test_coaching_store.py`

**Interfaces:**
- Produces:
  - `COACHING_SCHEMA_VERSION = 1`
  - `coaching_path(session_id: str) -> Path`
  - `load(session_id: str) -> dict` (returns `{}` if missing or unreadable)
  - `update(session_id: str, mutate: Callable[[dict], None]) -> dict` (atomic write under `get_transcript_lock(session_id)`)
  - `mark_stale(session_id: str) -> None` (sets `report_stale: True` if a report exists)
  - Sidecar keys:
    - `version`, `words` (list of verbatim word dicts), `owner` (`{"labels": [str], "source": "voice"|"user"|"loudness", "confidence": float}`)
    - `status` (`running|done|needs_owner|skipped|failed`), `status_reason`
    - `metrics`, `review`, `report_stale`, `mixed_up: bool`, `transcript_text_hash`, `created_at`

- [ ] **Step 1: Write failing tests** `test_load_missing_returns_empty`, `test_update_roundtrip_atomic` (after `update(sid, lambda d: d.update(status="done"))`, `load(sid)["status"] == "done"` and `["version"] == 1`), `test_mark_stale_only_when_report_exists`, and `test_corrupt_file_loads_empty`.
- [ ] **Step 2:** Run `python3 -m pytest tests/test_coaching_store.py -v`. Expect FAIL (module missing).
- [ ] **Step 3:** Implement `store.py`. Write with a temp file plus `os.replace`, the same pattern as `_write_text_atomic` in `app/routes/transcribe.py:110`. Create `COACHING_DIR` on first write.
- [ ] **Step 4:** Re-run. Expect PASS.
- [ ] **Step 5:** Wire staleness. `_merge_transcript` (`app/routes/transcribe.py`) calls `store.mark_stale(session_id)` when the text changes. So does `POST /transcripts/update` (`app/routes/recordings.py:888`). Add `test_merge_transcript_marks_coaching_stale` to `tests/test_coaching_store.py`, using the `route_env` fixture pattern from `tests/test_transcribe_providers.py:146`.
- [ ] **Step 6:** Commit with the message `Coaching: sidecar store in DATA_DIR/coaching`.

### Task 2: Keep Deepgram's verbatim words; transcript stays clean

**Files:**
- Modify: `app/providers/transcribe.py` (`transcribe_deepgram`), `app/routes/transcribe.py` (cloud branch, `:427-470`)
- Test: `tests/test_transcribe_providers.py` (add tests)

**Interfaces:**
- Consumes: `store.update` (Task 1).
- Produces:
  - `FILLER_TOKENS = frozenset({"um", "uh", "uhm", "umm", "er", "erm", "hmm", "mm", "mhm"})` in `app/providers/transcribe.py`
  - The `transcribe_deepgram` result gains `"words": [{"word", "start", "end", "speaker": "Speaker N"|None, "confidence"}]`. Words keep fillers; `word` is Deepgram's `punctuated_word` or else `word`.

- [ ] **Step 1: Write failing tests**, using a fake Deepgram JSON fixture that has `filler_words` output:
  - `test_deepgram_requests_filler_words`: the query string contains `filler_words=true`.
  - `test_deepgram_clean_text_has_no_fillers`: given utterance words `["So", "um", "pricing", "uh", "works."]`, the segment `text == "So pricing works."` and the top-level `text` contains no `um`/`uh` token.
  - `test_deepgram_returns_verbatim_words`: `[w["word"] for w in result["words"]]` includes `"um"`, and speakers are `"Speaker 1"` style when `diarize=True`.
  - `test_cloud_route_saves_words_to_coaching_sidecar_not_transcript`: after `/transcribe`, `store.load(sid)["words"]` is non-empty and the transcript JSON has no `"words"` key.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3: Implement.**
  - Add `"filler_words": "true"` to `params`.
  - Build each utterance's clean text from `utt["words"]` (`punctuated_word`), skipping tokens whose lowercase form with punctuation stripped is in `FILLER_TOKENS`. Fall back to `utt["transcript"]` when `words` is absent.
  - Rebuild `full_text` the same way from `alt["words"]`.
  - Return `words` from `alt["words"]`.
  - In the route, pop `result["words"]` before building `t_data`, then call `store.update(session_id, lambda d: d.update(words=..., transcript_text_hash=<sha1 of text>))`.
- [ ] **Step 4:** Run the whole of `tests/test_transcribe_providers.py`. Expect PASS, with no regressions.
- [ ] **Step 5:** Commit with the message `Deepgram: keep verbatim words for coaching; strip fillers from transcript`.

### Task 3: Metrics (pure computation)

**Files:**
- Create: `app/coaching/targets.py`, `app/coaching/metrics.py`
- Test: `tests/test_coaching_metrics.py`

**Interfaces:**
- Consumes: segment dicts `{start, end, text, speaker}`, word dicts from Task 2.
- Produces:
  - `targets.TARGETS: dict[str, dict]`, keyed by `pace_wpm, talk_share, longest_monologue_s, interruption_rate, patience_s, questions_per_hour, fillers_per_min, interactivity`. Each entry is `{"low": float|None, "high": float|None, "label": str}`, using the Global Constraints values.
  - `targets.MIN_COACHING_SECONDS = 120`
  - `targets.INTERRUPT_MAX_GAP_S = 0.2`
  - `targets.BACKCHANNELS = frozenset({"yeah", "yes", "yep", "mm", "mhm", "right", "ok", "okay", "sure", "uh-huh", "true", "exactly"})`
  - `targets.FILLER_PHRASES = ("um", "uh", "er", "erm", "like", "you know", "i mean", "sort of", "kind of", "basically", "actually")`. Count "like" only when it isn't followed by a noun phrase, approximated as "like" + comma, or "like" between two fillers. Keep this simple, and document it in the docstring as approximate.
  - `metrics.compute_metrics(segments: list[dict], words: list[dict] | None, owner_labels: set[str], duration: float, timestamps_estimated: bool) -> dict` returns:
    ```python
    {
      "per_speaker": {label: {"wpm": float|None, "talk_s": float, "talk_share": float, "turns": int}},
      "owner": {"pace_wpm", "talk_share", "longest_monologue_s", "interruptions_made", "interruption_rate",
                "interrupted_by_others", "patience_s", "questions", "questions_per_hour",
                "fillers", "fillers_per_min", "interactivity"},   # each value float|int|None
      "availability": {metric: "exact"|"estimated"|"unavailable"},
      "moments": [{"t": float, "kind": "interruption"|"monologue"|"fast"|"filler_burst",
                   "speaker": str, "quote": str, "likely": bool}],
    }
    ```
  - `metrics.owner_turns(segments, owner_labels) -> list[dict]`. Consecutive same-side segments are merged into turns; Task 6 reuses this.

- [ ] **Step 1: Write failing tests** with hand-built segments and words:
  - `test_wpm_excludes_silence`: 150 owner words across 60 s of segments separated by gaps, so `pace_wpm == 150.0`.
  - `test_talk_share_merges_owner_labels`: owner labels `{"Speaker 1", "Speaker 3"}` count as one person.
  - `test_longest_monologue_merges_consecutive_owner_segments`.
  - `test_likely_interruption_needs_short_gap_and_cut_off_sentence`: the other speaker's turn ends `"...and the agent and"` (no `.?!`), and the owner starts 0.1 s later, so 1 interruption with `likely=True`. A 0.1 s gap after `"...works."` gives 0. A 0.5 s gap after a cut-off sentence also gives 0.
  - `test_backchannel_is_not_an_interruption`: the owner's turn is just `"Yeah."`.
  - `test_patience_is_median_gap_before_owner_turns`.
  - `test_questions_count_owner_question_marks`.
  - `test_fillers_from_words_only`: with `words=None`, `fillers_per_min is None` and availability is `"unavailable"`.
  - `test_estimated_timestamps_mark_timing_metrics_unavailable` (Review Focus 3): `timestamps_estimated=True` sets `pace_wpm`, `interruption_rate` and `patience_s` to `None`, each marked `"unavailable"`.
  - `test_wpm_per_speaker_reported_for_everyone`.
- [ ] **Step 2:** Run the tests. Expect FAIL.
- [ ] **Step 3: Implement.**
  - Use word timings for speaking time when words exist; otherwise use segment durations, and mark the result `"estimated"`.
  - Interruption detection: owner turn start − previous other-speaker turn end ≤ `INTERRUPT_MAX_GAP_S`, the other turn's text not ending in `.?!`, and the owner turn not a backchannel. "Interrupted by others" is the same rule mirrored.
- [ ] **Step 4:** Run the tests. Expect PASS.
- [ ] **Step 5:** Commit with the message `Coaching: speaking metrics and targets`.

### Task 4: Owner voice profile and matching

**Files:**
- Create: `app/coaching/voice.py`
- Test: `tests/test_coaching_voice.py`; `tests/conftest.py` (autouse stub)

**Interfaces:**
- Consumes: `src/transcribe._to_wav16k`, `src/transcribe._load_diarize_lib` (for the install), `wespeakerruntime`.
- Produces:
  - `PROFILE_PATH = COACHING_DIR / "voice_profile.json"`, stored as `{"version": 1, "exemplars": [[float]*256], "updated_at"}`. File mode is 0600. At most `MAX_EXEMPLARS = 20`; the oldest is dropped first.
  - `MATCH_THRESHOLD = 0.55` (cosine) and `MARGIN = 0.10` (the best match must beat the second-best label by this much).
  - `model_available() -> bool` (import check only, never installs)
  - `install_model() -> tuple[bool, str]` (blocking; calls `_load_diarize_lib()`; used only by the voice-setup route)
  - `speaker_embeddings(audio_path: str, segments: list[dict], max_seconds_per_speaker: float = 60.0) -> dict[str, np.ndarray]`: one mean, L2-normalised embedding per speaker label, from that speaker's longest segments up to the cap
  - `match_owner(audio_path, segments) -> dict`, returning `{"labels": [str], "confidence": float, "source": "voice"}`, or `{"labels": [], "confidence": 0.0, "source": "none", "reason": str}`. `reason` is one of "no voice profile", "voice model not installed" or "no confident match". Every label whose similarity is ≥ threshold *and* within `MARGIN` of the best counts as the owner (this handles the split owner).
  - `loudest_label(audio_path, segments) -> str | None`: the RMS-loudest speaker, offered only as a suggestion
  - `add_exemplars(audio_path, segments, labels: list[str]) -> int`: the voice-setup path and the correction path
  - `delete_profile() -> None`

- [ ] **Step 1: Write failing tests.** Inject a fake embedder with `monkeypatch.setattr(voice, "_embed_wav", fake)` that returns fixed vectors per label.
  - `test_match_owner_needs_profile` gives `reason == "no voice profile"`.
  - `test_match_owner_model_missing_never_installs` (Review Focus 5): with `model_available` patched to `False`, `install_model` must not be called and `reason == "voice model not installed"`.
  - `test_match_owner_picks_closest_above_threshold`.
  - `test_split_owner_returns_both_labels`.
  - `test_ambiguous_match_returns_none`: two labels both near the profile and within `MARGIN` of each other, but below the threshold, gives no match.
  - `test_add_exemplars_caps_at_20`.
  - `test_profile_file_is_0600`.
  - `test_delete_profile_removes_file`.
- [ ] **Step 2:** Run the tests. Expect FAIL.
- [ ] **Step 3: Implement.**
  - `_embed_wav(path) -> np.ndarray` wraps a lazily built, module-cached `wespeakerruntime.Speaker(lang="en")`.
  - Cut segments to 16 kHz WAV clips with `_to_wav16k` and `soundfile`. Skip clips shorter than 1.0 s.
  - Wrap everything in try/except so it returns the "none" result, never raises into the runner.
- [ ] **Step 4:** Add a conftest autouse fixture that patches `app.coaching.voice._embed_wav` to raise `AssertionError("no real embeddings in tests")`, so a test can't load the model by accident. The voice tests override it.
- [ ] **Step 5:** Run `python3 -m pytest tests/test_coaching_voice.py -v`. Expect PASS.
- [ ] **Step 6:** Commit with the message `Coaching: owner voice profile and matching (local WeSpeaker)`.

### Task 5: Generic structured LLM call

**Files:**
- Create: `app/providers/llm_json.py`
- Test: `tests/test_llm_json.py`

**Interfaces:**
- Consumes: from `app/providers/summarize.py`, `_anthropic_client`, `_describe_error`, `_PROVIDER_GROUP`, `_PREFERENCE_GROUP`, `ollama_models`, `pick_ollama_model`, `SummaryError`, and the model constants in `app/providers/_models.py`.
- Produces: `call_llm_json(system: str, user: str, schema: dict, config: dict, schema_name: str = "result") -> tuple[dict | None, str, list[str]]`, returning `(parsed, provider_name, errors)`. The cascade and preference order match `summarize()`, *minus DistilBART*. `parsed` is `None` when every provider fails. Results cut off at the token limit or refused raise inside a provider, and the cascade moves on.

- [ ] **Step 1: Write failing tests**, mocking HTTP the same way as `tests/test_summarize.py`'s `_http_response`:
  - `test_prefers_configured_provider`.
  - `test_falls_back_on_error_and_collects_errors`.
  - `test_never_uses_distilbart`: with provider `local` and Ollama down, the result is `(None, "none", [...])`.
  - `test_schema_passed_to_each_provider`: Anthropic `output_config.format.schema`, OpenAI `response_format.json_schema.schema` and Ollama `format`.
  - `test_invalid_json_is_an_error_not_a_crash`.
- [ ] **Step 2:** Run the tests. Expect FAIL.
- [ ] **Step 3: Implement.** The four provider callables take `(system, user, schema)` and mirror the existing summarize ones, with the schema parameterised. JSON parsing tolerates code fences, as `_parse_summary` does.
- [ ] **Step 4:** Run the tests. Expect PASS. Run `tests/test_summarize.py` too; nothing should change there.
- [ ] **Step 5:** Commit with the message `Providers: generic structured-output LLM call`.

### Task 6: AI coaching review with grounding

**Files:**
- Create: `app/coaching/review.py`
- Test: `tests/test_coaching_review.py`

**Interfaces:**
- Consumes: `call_llm_json` (Task 5); `compute_metrics` output and `owner_turns` (Task 3); `format_transcript` (`app/providers/summarize.py`).
- Produces:
  - `REVIEW_SCHEMA`: an object with:
    - `scores` (object with `clarity`, `warmth`, `charisma`, `listening`, `reception`; each `{score: int 0-100, reason: str}`)
    - `summary: str`
    - `next_time: [str]`, 2–3 items
    - `reactions: [{t: number, owner_point: str, response: "engaged"|"agreed"|"pushed_back"|"deflected"|"short"|"follow_up", evidence: str}]`
    - `moments: [{t: number, kind: str, note: str}]`
    - `interruptions_confirmed: [number]`, holding the `t` of each likely interruption the model agrees with
  - `review_conversation(segments, owner_labels, metrics, config, owner_name: str = "") -> dict` returns `{"review": dict | None, "provider": str, "errors": [str]}`
  - `ground(review: dict, segments: list[dict], tolerance_s: float = 2.0) -> dict` drops every reaction or moment whose `t` isn't within `tolerance_s` of a segment start, and clamps scores to 0–100.

- [ ] **Step 1: Write failing tests:**
  - `test_ground_drops_invented_timestamps` (Review Focus 4).
  - `test_ground_clamps_scores`.
  - `test_prompt_marks_owner_and_includes_metrics`: capture the `user` argument; it contains `"(you)"` on owner lines and the pace figure.
  - `test_failed_llm_returns_none_with_errors`.
  - `test_owner_name_from_profile_used`.
- [ ] **Step 2:** Run the tests. Expect FAIL.
- [ ] **Step 3: Implement.**
  - The transcript goes in as `[mm:ss] Speaker N (you): text` lines.
  - The system prompt asks for blunt, specific, kind-but-honest coaching; British English if `config["english_variant"] == "gb"`; and requires every claim to cite a `t` taken from the lines given.
  - Long transcripts: send the first 400 owner-adjacent turns, and say so in the prompt. No map-reduce in this version.
- [ ] **Step 4:** Run the tests. Expect PASS.
- [ ] **Step 5:** Commit with the message `Coaching: AI review with timestamp grounding`.

### Task 7: Runner and pipeline hook

**Files:**
- Create: `app/coaching/runner.py`
- Modify: `app/routes/transcribe.py` (call the hook at the three `fire_webhooks("transcribe", …)` points)
- Test: `tests/test_coaching_runner.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4 and 6; `load_config`.
- Produces:
  - `run_coaching(session_id: str, audio_path: str | None, force: bool = False) -> dict` (synchronous; returns the sidecar)
  - `start_coaching_async(session_id: str, audio_path: str | None) -> None` (daemon thread; no-op unless `config.get("coaching_auto", True)` or `force`)
  - `set_owner(session_id: str, labels: list[str], learn: bool = True) -> dict` (saves `source="user"`, adds exemplars if `learn` and the model is available, re-runs coaching)
  - Status rules:
    - `skipped`, with these reasons: `"no speaker labels"`, `"only you spoke"`, `"too short"`
    - `needs_owner`: no confident match and no user choice. The sidecar stores `candidates: [{label, first_line, loud: bool}]`.
    - `failed`: the review failed. The metrics are still saved, and `status_reason` lists the provider errors.
    - `done`

- [ ] **Step 1: Write failing tests**, with voice, review and threads stubbed (copy `_SyncThread` from `tests/test_speaker_id.py:396`):
  - `test_skips_without_speaker_labels`.
  - `test_skips_too_short`.
  - `test_only_owner_spoke_is_skipped` (Review Focus 1): owner labels cover every speaker.
  - `test_needs_owner_lists_candidates_with_first_lines`.
  - `test_done_writes_metrics_and_review`.
  - `test_review_failure_keeps_metrics_status_failed`.
  - `test_result_dropped_if_transcript_changed_meanwhile` (Review Focus 2): change the transcript text between start and write; the sidecar must not take the stale report.
  - `test_user_owner_choice_persists_and_reruns`.
  - `test_auto_off_does_nothing`.
  - `test_hook_called_after_background_diarize_and_both_no_bg_branches`: patch `start_coaching_async` and drive the three branches through the existing `route_env` helpers.
- [ ] **Step 2:** Run the tests. Expect FAIL.
- [ ] **Step 3: Implement.**
  - Owner precedence: a saved `owner.source == "user"`, then `match_owner`, then `needs_owner`.
  - Before writing, compare `transcript_text_hash` with the current transcript text, and drop the result if they differ.
  - Hook: add `_after_transcript_final(session_id, audio_path)` in `app/routes/transcribe.py`. It calls `start_coaching_async`, inside try/except so a coaching failure can never break transcription. Call it next to each of the three `fire_webhooks("transcribe", …)` calls.
- [ ] **Step 4:** Run `python3 -m pytest tests/test_coaching_runner.py tests/test_transcribe_providers.py tests/test_speaker_id.py -q`. Expect PASS.
- [ ] **Step 5:** Commit with the message `Coaching: background runner after transcription and speaker labels`.

### Task 8: Progress aggregation

**Files:**
- Create: `app/coaching/progress.py`
- Test: `tests/test_coaching_progress.py`

**Interfaces:**
- Consumes: the sidecars (Task 1); recording dates from the transcript JSON or the recordings list.
- Produces: `compute_progress(sidecars: list[tuple[str, float, dict]], now: float, weeks: int = 12) -> dict`. Each tuple is `(session_id, recorded_at_epoch, sidecar)`. The result is:
  ```python
  {"series": {metric: [{"session_id", "t", "value"}]},
   "baseline": {metric: float|None},          # median of the last 30 days
   "weekly": [{"week_start", metric...}],      # medians per ISO week
   "most_improved": {"metric", "from", "to"} | None,
   "most_stuck": {"metric", "value", "target"} | None,
   "focus": {"metric", "text"} | None,
   "count": int}
  ```
  Only `status == "done"` and `mixed_up is False` count. "Improved" means moved toward the target band, comparing the last 4 weeks' median with the 4 weeks before. Focus is the metric furthest outside its band in the last 2 weeks, with a templated sentence from `targets.TARGETS[metric]["label"]`. No AI call here.

- [ ] **Step 1: Write failing tests:**
  - `test_excludes_mixed_up_and_not_done`.
  - `test_baseline_is_30_day_median`.
  - `test_most_improved_moves_toward_band`.
  - `test_focus_is_furthest_outside_band`.
  - `test_empty_is_safe`.
- [ ] **Step 2:** Run the tests. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expect PASS.
- [ ] **Step 5:** Commit with the message `Coaching: progress over weeks`.

### Task 9: API routes, settings and the speaker-labels migration

**Files:**
- Create: `app/routes/coaching.py` (blueprint `coaching`)
- Modify: `app/api_server.py` (register the blueprint), `app/routes/settings.py` (GET `coaching_auto` default `True`; POST whitelist; `diarize: false` also sets `coaching_auto: false`), `app/utils/config.py` or the startup path in `app/api_server.py` (a one-time migration)
- Test: `tests/test_coaching_routes.py`

**Interfaces:**
- Consumes: the runner (Task 7), voice (Task 4), progress (Task 8), store (Task 1).
- Produces these routes. All are JSON, behind the existing token check.
  - `GET /coaching/<sid>` returns the sidecar minus `words`, plus a `targets` copy.
  - `POST /coaching/<sid>/run` returns `{"started": true}` and starts a forced run in the background.
  - `POST /coaching/<sid>/owner {"labels": [str], "learn": bool}` returns the sidecar.
  - `POST /coaching/<sid>/mixed-up {"mixed_up": bool}` returns the sidecar.
  - `GET /coaching/progress?weeks=12` returns the `compute_progress` result.
  - `GET /coaching/voice` returns `{"profile": bool, "exemplars": int, "model": bool, "installing": bool, "install_error": str|None}`.
  - `POST /coaching/voice/install` returns `{"started": true}`. It runs `install_model()` in a background thread. Poll `GET /coaching/voice` for progress.
  - `POST /coaching/voice/setup {"session_id": str, "labels": [str]}` returns `{"exemplars": int}`, or 409 `{"error": "voice model not installed"}`.
  - `DELETE /coaching/voice` returns `{"deleted": true}`.
  - Migration: on startup, if the config lacks `coaching_migrated`, then set `diarize = True` if it's falsy, set `coaching_auto` to `True` if it's unset, and set `coaching_migrated = True`. Use `update_config`.

- [ ] **Step 1: Write failing tests** using `flask_client`:
  - one test per route for its status code and shape;
  - `test_words_never_returned`;
  - `test_voice_setup_409_without_model`;
  - `test_migration_turns_on_speaker_labels_once` (run it twice; a user who later turns `diarize` off stays off);
  - `test_diarize_off_turns_coaching_off`;
  - `test_bad_session_id_404` (validate with `app.utils.paths.validate_session_id`, which raises on a bad or path-traversal ID; answer 404).
- [ ] **Step 2:** Run the tests. Expect FAIL.
- [ ] **Step 3:** Implement. Add the bridge methods to `app/tauri-bridge.js` `window.plaud`: `coaching(sid)`, `coachingRun(sid)`, `coachingSetOwner(sid, labels, learn)`, `coachingMixedUp(sid, flag)`, `coachingProgress(weeks)`, `voiceStatus()`, `voiceInstall()`, `voiceSetup(sid, labels)` and `voiceDelete()`. Put `/coaching/voice/install` in `NO_TIMEOUT_ENDPOINTS`.
- [ ] **Step 4:** Run `python3 -m pytest tests/test_coaching_routes.py tests/test_tauri_parity.py -q`. Expect PASS.
- [ ] **Step 5:** Commit with the message `Coaching: API, settings, speaker-labels migration`.

### Task 10: Coaching tab in the recording view

**Files:**
- Create: `app/ui/coaching.js`, `app/css/coaching.css`
- Modify: `app/index.html` (third tab button `data-tab="coaching"`, the `#transcript-coaching` container, the script tag after `ui/transcript.js`, and the CSS link), `app/ui/transcript.js` (`showTranscriptTab` handles `coaching`; the tab row shows when a transcript exists, not only when there's a summary)
- Test: `tests/test_settings_ui.py`-style static checks in a new `tests/test_coaching_ui.py` (parse the JS and HTML as text, the same way existing UI tests do)

**Interfaces:**
- Consumes: the bridge methods (Task 9); `dom.audioEl`.
- Produces: `renderCoachingTab(container, sessionId)` (global) and `coachSeek(t)`.

Render order, as in spec §6:
1. "This is you: Speaker 2 · change". "Change" opens a picker with checkboxes for every speaker, showing their first line, plus a "Speakers mixed up" toggle.
2. The overall score, then area scores. Each area shows its value, the target band and "your usual", read from `/coaching/progress` baseline.
3. Summary and "Next time".
4. A metrics table, per speaker where it applies (WPM per speaker), with "estimated" or "unavailable" badges from `availability`.
5. Moments, sorted by time. Clicking one calls `coachSeek(t)`. Likely interruptions are labelled "Likely interruption".
6. "How they responded".

Other states:
- `running`: a spinner. Poll every 5 s, like `diarization_status` (`transcript.js:1088-1112`).
- `needs_owner`: the picker inline.
- `skipped`: the reason, plus a "Coach anyway" button.
- `failed`: the errors, the metrics, and a "Try again" button.
- Stale: "Transcript changed: Update coaching".

- [ ] **Step 1: Write failing static tests:**
  - `index.html` has a tab with `data-tab="coaching"` and `#transcript-coaching`;
  - `coaching.js` defines `renderCoachingTab`;
  - no top-level `let`/`const`/`function` name in `coaching.js` collides with another `app/ui/*.js` or `renderer.js` (parse the names with a regex);
  - the script tag order puts `coaching.js` after `transcript.js` and before `renderer.js`.
- [ ] **Step 2:** Run the tests. Expect FAIL.
- [ ] **Step 3:** Implement. Use the existing CSS tokens from `app/css/tokens.css`. Escape all transcript text with the existing escape helper in `app/ui/utils.js`.
- [ ] **Step 4:** Run the tests. Expect PASS. Then check the tab in the running app with the `run` skill or `npm`/Tauri dev (see `docs/DEVELOPMENT.md`), using a real Deepgram-transcribed recording that has two speakers.
- [ ] **Step 5:** Commit with the message `Coaching tab in the recording view`.

### Task 11: Progress view and "Your voice" settings

**Files:**
- Create: `app/ui/coaching-progress.js`
- Modify: `app/index.html` (header button `#progress-btn` before `#settings-btn`; a `#progress-view` panel; script tag), `app/ui/settings.js` and `app/index.html` (a "Your voice & coaching" section in the **Transcription & AI** tab, `data-tab="ai"`), `app/css/coaching.css`
- Test: `tests/test_coaching_ui.py` (extend)

**Interfaces:**
- Consumes: `coachingProgress`, `voiceStatus`, `voiceInstall`, `voiceSetup` and `voiceDelete` (Task 9).
- Produces: `openProgressView()` and `renderVoiceSettings()` (globals).

**Progress view:**
- One small SVG sparkline per metric, hand-rolled from `<polyline>` with no library. It has the target band shaded and the baseline as a dashed line.
- Cards for "Most improved", "Still stuck" and "Focus this week". There's a count of coached conversations.
- An empty state for fewer than 3 coached conversations.

**Settings:**
- The "Coach my conversations automatically" toggle (`coaching_auto`). If speaker labels are off, the toggle is disabled with "Needs speaker labels" next to it.
- Voice status.
- "Set up your voice": install the voice model with progress, from polling `GET /coaching/voice`. Then pick a recording and tick the speakers who are you; that recording can be one where you read a paragraph into the NotePin.
- "Redo" and "Remove my voice".

- [ ] **Step 1: Write failing static tests:**
  - `#progress-btn` and `#progress-view` exist;
  - the settings markup has `coaching_auto` and `voice-setup` elements;
  - the global-name collision check covers `coaching-progress.js`.
- [ ] **Step 2:** Run the tests. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expect PASS. Then check by hand in the running app: set up your voice from a recording, coach a recording, and open Progress.
- [ ] **Step 5:** Commit with the message `Coaching progress view and voice settings`.

### Task 12: Docs and the full suite

**Files:**
- Modify:
  - `docs/ARCHITECTURE.md`: a coaching section covering data flow, the sidecar, where the hook sits, and that voice data stays local.
  - `README.md`: a one-paragraph feature mention.
  - `SECURITY.md`: voice embeddings are local; the coaching text goes to the same AI provider as summaries.

- [ ] **Step 1:** Write the docs.
- [ ] **Step 2:** Run `python3 -m pytest -q && ruff check app tests && ruff format --check app tests`. Expect only the known failures from Orientation.
- [ ] **Step 3:** Commit with the message `Docs: conversation coaching`, then open a PR to `master`. The PR body summarises the feature, lists Review Focus items 1–5 and how each is tested, and notes that the release bump is a separate step.

---

## Self-review notes (for the executor)

- Spec coverage:

  | Spec | Task |
  |---|---|
  | §1 data | 2 |
  | §2 owner | 4, 7, 9, 11 |
  | §3 metrics | 3 |
  | §4 AI | 5, 6 |
  | §5 when it runs | 7, 9 |
  | §6 UI | 10, 11 |
  | §7 privacy and settings | 4, 9, 11, 12 |
  | Speaker-labels decision | 9 |
  | Anti-goals | grounding (6), wrong person (4, 7), clean transcripts (2), data locality (4, 12) |

- Deferred by the spec, and not to be built: prosody (pitch, energy), live coaching, Mac-microphone voice capture, and filtering progress by person or type.
