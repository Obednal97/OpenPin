# Conversation coaching: design

Date: 2026-11-04. Status: design, approved for planning once reviewed.

## Intent

Open Pin records the owner's conversations from the NotePin they wear. The owner wants honest,
specific feedback on **how they themselves came across** in conversations with other people, and
wants to see over weeks whether they are getting better. It's like Read.ai's Speaker Coach, Gong's
talk analytics or Zoom's speaker insights, but local-first, audio-only (there is no video), and
about one person: the wearer.

Two ways to use it, both wanted:

1. **One conversation, right after it:** "How did that call go? What did I do well, where did I
   rush, talk over them, ramble, lose them?"
2. **Progress over weeks:** "Am I actually interrupting less? Is my pace better? What should I
   work on this week?"

## Goals

- For every recording where the owner spoke with at least one other person, automatically
  produce a **coaching report**: scores, plain-spoken feedback, and timestamped moments.
- Measure, per speaker where it makes sense and always for the owner: **pace (words per minute),
  talk share, longest monologue, interruptions (made and received), response pause ("patience"),
  questions asked, filler words**.
- Use the user's configured AI provider to judge **clarity, tone/warmth, charisma, and how others
  reacted** to what the owner said, always citing the moments it is based on.
- A **progress view** across recordings: each measure over time against the owner's own baseline,
  most improved / most stuck habit, and a weekly "focus on this" suggestion.
- Know which speaker is the owner, reliably, after a **one-time voice setup**.

## Non-goals (for this version)

- Voice-quality analysis from the audio signal (pitch variation/monotone, energy, volume). It was
  deliberately deferred: it's the most work and the least reliable on a wearable mic. Leave room
  for it in the report structure.
- Live/real-time coaching during a conversation.
- Coaching for the other participants, or naming/identifying other people by voice.
- Video-based signals (there is no video).
- Re-processing old recordings automatically. Old recordings get coaching only when the user asks
  (they also need re-transcribing to get filler words).

## Anti-goals (would make this a failure even if it works)

- **Feedback that isn't grounded.** Every AI judgement must point to specific timestamped moments
  in the recording. A claim with no moment behind it shouldn't be shown.
- **Wrong person coached.** If the app isn't confident which speaker is the owner, it must say so
  and ask, not silently analyse the wrong voice.
- **Messier transcripts.** The transcript the user reads, the summary and the Notion push must stay
  as clean as today (no "um"/"uh").
- **Data leaving the Mac in new ways.** Voice data (embeddings/samples) stays on the Mac. Text goes
  only to the AI provider the user already chose for summaries, the same as summaries do today.

## What the user said (decisions)

- Both uses: per-conversation feedback and trends over weeks.
- Coaching runs **automatically** for every eligible recording.
- Owner identification: a **one-time voice setup** (pick yourself in a past recording, or read a
  short paragraph). After that the app matches the owner's voice on the Mac, says "this is you" on
  each report, and offers a one-click correction. Corrections improve future matching.
- Filler words: keep them **only for coaching**. Store a verbatim copy alongside; the transcript the
  user reads stays clean.
- Feedback style: **all three**. That means scores out of 100 per area, blunt plain-language
  feedback ("You rushed the pricing section and talked over them twice"), and specific clickable
  moments ("You interrupted 4 times, here's 12:40"). Blunt is fine.
- Recordings where the owner is the only speaker (voice notes) are skipped.
- **Speaker labels get turned on.** Speaker labelling (`diarize`) defaults to off in the code
  (`app/routes/settings.py`), though the owner's own install already has it on. Coaching needs it.
  Coaching (default on) switches speaker labels on for any install where they're off: new installs,
  and existing ones when they update to this version. Installs that already have them on see no
  change. From then on, transcripts and Notion pages show speaker names everywhere, as they already
  do for the owner; the user accepted this. If
  the user later turns speaker labels off, automatic coaching turns off with them (and the
  settings say why).

## How it fits the existing app (facts the design builds on)

These come from the code as of v2.6.1 (see `docs/ARCHITECTURE.md` for the wider picture):

- Transcripts are `DATA_DIR/transcripts/<session_id>.json` with `segments: [{start, end, text,
  speaker}]`, `speakers`, `diarization_status`, `provider`, `summary`, etc. Re-transcribing
  replaces only transcription keys and keeps everything else (`_merge_transcript`,
  `app/routes/transcribe.py`).
- **No word-level timestamps are kept today.** Deepgram (`nova-3`, `utterances=true`, `smart_format`)
  returns words with per-word timing and speaker, but `transcribe_deepgram` keeps only utterances,
  and the route then merges segments by speaker. Groq returns segment times only, and has no
  speakers until local diarization runs. OpenAI's times are *estimated* from character counts
  (`timestamps_estimated: true`). Local Whisper keeps segment times only.
- Speaker labels are `"Speaker N"`, renumbered per recording; they mean nothing across recordings.
  No speaker embeddings are kept by either local diarization tier (`diarize` package with WeSpeaker,
  or pyannote). There's no speaker naming anywhere yet.
- "Transcription and speaker labels are final" is known at one point. It's the end of
  `_background_diarize` and the two branches that skip background diarization, which all fire
  `fire_webhooks("transcribe", …)`. Auto-processing is a frontend queue (`queueAutoProcess`,
  `app/ui/recordings.js`). There's no backend job queue; background work uses daemon threads with
  per-transcript locks (`app/utils/transcript_locks.py`).
- The summary AI call (`app/providers/summarize.py`) cascades Anthropic → OpenAI → Ollama →
  DistilBART with a hard-wired prompt and `SUMMARY_SCHEMA`. There's no general-purpose
  "call the user's LLM with this prompt and schema" function. `format_transcript(segments)` and
  map-reduce chunking (`_run_summary`) are reusable.
- The recording detail view has Transcript / Summary tabs (`app/index.html`,
  `app/ui/transcript.js`). Settings has five tabs. There's no chart library. Frontend files share one
  global scope (plain `<script>` tags), so module-level names must be unique across files.
- Config is `DATA_DIR/.plaud_v2_config.json` via `load_config` / `update_config`. The `/settings`
  POST whitelists keys. `profile.name` is the only owner identity today.

## The design

### 1. Data the analysis needs

- **Word-level timing with speakers (Deepgram).** Keep Deepgram's `words` (`word`,
  `punctuated_word`, `start`, `end`, `speaker`, `confidence`) instead of discarding them. Request
  `filler_words=true` so "um"/"uh" are present in the words. Then:
  - the **clean transcript** (segments, text) is built exactly as today, *excluding* filler tokens,
    so what the user reads is unchanged;
  - the **verbatim words** are stored for coaching only.
- **Other providers.** With no word timing, the metrics fall back to segment-level estimates (words
  per segment over segment duration), and filler words are unavailable unless the provider's text
  contains them. The report must say which measures are exact, estimated or unavailable for that
  recording. Measures built on *estimated* timestamps (OpenAI) shouldn't be shown for timing-based
  metrics (interruptions, patience). Groq/Whisper segment times are real, but coarse.
- **Where it's stored:** a sidecar `DATA_DIR/coaching/<session_id>.json` for the verbatim words,
  owner identification and the coaching report. The transcript JSON stays focused on the
  transcript. A re-transcribe or re-diarize marks the coaching stale, the way `summary_stale` works.

### 2. Knowing who the owner is

- **Voice setup (once):** Settings gets a "Your voice" section with two options:
  - pick yourself in a past recording (choose a recording, then the speaker that is you);
  - read a short paragraph aloud (recorded on the Mac's microphone, or on the NotePin and synced).
  The app computes a **voice embedding** for the owner locally and stores it on the Mac only.
- **Per recording:** after speaker labels are final, compute an embedding for each speaker's audio
  (locally, using the WeSpeaker model that the `diarize` extra already installs). Compare each with
  the owner's voice, and pick the best match above a confidence threshold.
  - Matched confidently: mark that speaker as the owner.
  - Not confident, or no voice set up: the report shows "Which one is you?" with the speakers'
    first lines; one click picks. Nothing is analysed against a guessed owner.
  - A user correction is saved for that recording and added to the owner's voice profile
    (e.g. a running average of embeddings or a small set of exemplars; builder's call).
- **The owner can be more than one label.** A wearable mic often splits the owner into two labels
  or merges two people. The owner is a *set* of speaker labels per recording; the picker allows
  several. A report can be flagged "speakers mixed up". Flagged recordings keep their report but
  are left out of the baseline and the progress view. *(Builder-check answer; my call.)*
- **Wearable hint:** the owner is nearest the mic, so loudness can be a tiebreaker or a fallback
  suggestion. It never decides on its own.
- This needs the `diarize` extra (WeSpeaker). Deepgram users, who are the default, have never
  installed it, because Deepgram supplies the speaker labels. The extra is installed during
  **voice setup**, with progress and a clear error, never silently in the background. Without it,
  every report asks "Which one is you?". *(My call.)*
- **Voice setup in this version does not record from the Mac's microphone.** That would need a new
  macOS permission, entitlement and capture code. "Read a paragraph" means reading it into the
  NotePin and picking that recording after sync. *(My call.)*

### 3. Measured metrics (pure computation)

Computed for every speaker where meaningful, headlined for the owner. Each has a **target range**
from published benchmarks (the research covered Zoom, Microsoft, Gong, tl;dv, Read.ai) and a
comparison with the owner's own rolling baseline.

| Metric | Definition | Target (start here; tunable) |
|---|---|---|
| Pace (WPM) | Words per minute of the speaker's *speaking* time (silences excluded). Also flag fast stretches. | 110–160 |
| Talk share | Owner's speaking time ÷ all speaking time | ~40–60% (context-dependent) |
| Longest monologue | Longest continuous owner turn | ≤ 2:30 |
| Likely interruptions made | Single-channel audio means word timings never overlap, so true overlap can't be measured. Instead, the owner's turn starts within a very short gap (e.g. ≤ 0.2 s) of the other speaker's turn, *and* that turn ends without sentence-final punctuation (cut off). The AI step then confirms or rejects each candidate. Always labelled "likely", each linked to its moment. Short backchannels ("yeah", "mm", "right") don't count. Rate per owner turn. *(Builder-check answer; my call.)* | < 1 in 10 turns |
| Interrupted by others | The same, the other way round. Reported, not scored against the owner. | — |
| Patience | Median gap between another speaker finishing and the owner starting | 0.5–1.5 s |
| Questions asked | Owner question count per hour (open vs closed classified by the AI step) | 10–18 / hour |
| Filler words | Owner "um/uh/er/like/you know…" per minute (verbatim words) | 0.6–3 / min (zero also scores slightly lower) |
| Interactivity | Speaker switches per 5 min, excluding backchannels | ≥ 5 |

Every flagged instance (each interruption, the longest monologue, fast stretches) becomes a
**moment**: start time, speaker, short quote.

### 4. AI judgements

One AI call per recording (chunked for long recordings, like summaries), using the user's
configured summary provider and its fallback order. Input:
- the clean transcript with timestamps and speakers, owner marked;
- the computed metrics and flagged moments.

Structured output (JSON schema), roughly:
- **scores** 0–100 with a one-line reason each: clarity, warmth/tone, charisma, listening, and
  "how they responded";
- **summary**: 2–4 blunt sentences about how the owner came across;
- **do differently next time**: 2–3 concrete actions;
- **reactions**: for the owner's key points, what the other person did next (engaged, agreed,
  pushed back, deflected, short/flat answer, asked a follow-up), each with its timestamp;
- **notable moments**: strong question, a point that landed, rambling, hedging, etc., each with
  a timestamp.

**Grounding rule:** every judgement and moment must cite timestamps that exist in the transcript.
Drop any that don't (validate after parsing). The prompt includes `profile.name` as the owner's
name, like summaries do.

Add a general-purpose "call the configured LLM with this prompt and schema" function alongside
`summarize()`, rather than copying the provider cascade. Summaries can move to it later; that's
not required now.

**Local-only users** (provider `local`, i.e. Ollama/DistilBART): use Ollama with the schema if
there is one. DistilBART can't do this, so the report shows measured metrics only, with a note.

### 5. When it runs

- Automatically, after transcription **and** speaker labelling are final (the
  `fire_webhooks("transcribe")` points), for recordings with ≥ 2 speakers where the owner is
  identified. Run it in the background like diarization, with its own status (`running`, `done`,
  `needs_owner`, `skipped`, `failed`) that the frontend can poll.
- Skipped: one speaker only; no speaker labels at all (e.g. diarization off or failed); the
  recording is shorter than a minimum (e.g. 2 minutes; builder's call).
- A setting to turn automatic coaching off (default on). There's a "Coach this recording" action
  for old or skipped recordings. For old recordings, it offers re-transcribing to get filler words
  and word timing.

### 6. What the user sees

- **Recording view:** a third tab, **Coaching**, next to Transcript and Summary:
  1. who the app thinks is you ("This is you: Speaker 2 · change");
  2. an overall score and one score per area, each against its target range and "your usual";
  3. the blunt summary and "do differently next time";
  4. the measured metrics, per speaker where shown (WPM per speaker included);
  5. **Moments:** a timestamped list; clicking one jumps the transcript/audio to that point;
  6. a "how they responded" list for your key points.
  Measures that are estimated or unavailable for this recording are labelled as such.
- **Progress view** (new screen, reachable from the main navigation): each measure over time
  (simple line/sparkline charts; there's no chart library today, so the builder picks something
  light or hand-rolls SVG), your baseline, the most improved and most stuck habit, and a
  **"focus on this week"** suggestion. The suggestion can be computed or AI-written; builder's call.
- Filtering progress by who you talked to or by conversation type was suggested (my guess). The
  user didn't confirm it. Nice to have, not required.

### 7. Privacy and settings

- Voice embeddings and the verbatim words stay in `DATA_DIR` on the Mac. They're never sent
  anywhere. The coaching AI call sends the same kind of text a summary already sends.
- Settings: "Your voice" (set up / redo / remove), "Coach my conversations automatically" (on),
  and any new keys added to the `/settings` whitelist.
- Removing the voice profile deletes the embedding(s).

## Left to the builder

- How embeddings are extracted from the `diarize`/WeSpeaker install, the similarity measure and
  the confidence threshold, and how corrections update the profile.
- The exact filler-word list and the backchannel list; the interruption overlap threshold.
- Overall-score weighting.
- Chart rendering approach.
- Sidecar schema details and versioning.
- Prompt wording, chunking and token limits.
- Exactly how a "Coach this recording" re-transcribe is offered.
- Test strategy (follow `tests/` conventions; no real Bluetooth, network or AI calls in tests).

## Open risks

- Speaker-label errors corrupt every metric. Show confidence, and make "this isn't me / these
  speakers are mixed up" easy to flag.
- Overlap detection depends on Deepgram word timing. Other providers give weaker or no
  interruption data.
- Benchmarks come from sales and meeting tools. They may not suit every conversation type. Keep the
  targets easy to tune.
