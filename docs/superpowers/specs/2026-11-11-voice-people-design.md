# Recognising people by voice: design

Date: 11 November 2026. Status: agreed with the owner in conversation. Calls marked "my call" are the designer's and can be overruled.

## Intent

Today every transcript says "Speaker 1", "Speaker 2" until the owner names each speaker by hand, in every recording. Open Pin already learns the owner's voice for coaching, but deliberately never names a speaker from a voice match alone (see `2026-11-05-mcp-and-speaker-names-design.md`, "Your own name").

The owner wants Open Pin to recognise people:

- **Himself:** his own name on his speaker in every transcript, from his voice alone.
- **Everyone he talks to** (Harry, Nadia and so on): once he has named someone, Open Pin learns that voice and names them automatically in later recordings.
- **Everywhere names show:** the transcript, the summary, the coaching report, the MCP connector and Notion pages, with no "Speaker 1" left where a voice is known.

He also wants a **people directory** in Settings: who Open Pin knows by voice, how much is stored, and a way to delete a person's voice data. Names should be **consistent** (one spelling per person, ideally full names), so naming a speaker means picking from the people already known.

## Goals

1. **Your voice gets your name.** The name in Settings › Personalisation › Your name is tied to your voice profile. Any speaker matched to your voice shows that name in every transcript. A voice match is enough (the owner explicitly relaxed the old rule).
2. **Voices of other people are learnt from naming.** Naming a speaker (in the app or through the MCP connector) saves a voice sample for that person from that recording.
3. **Automatic naming.** After a recording's speakers are labelled, each unnamed speaker is compared against every known voice and named when the match is confident.
4. **Learn from the backlog straight away.** On first run, learn every voice from names already typed in past transcripts, then name the unnamed speakers in past transcripts.
5. **People directory** in Settings: list, rename, merge and delete people; see storage per person.
6. **Consistent names:** a picker of known people when naming a speaker, with a "Did you mean …?" check for near-duplicates.
7. **Notion follows:** any change of the names shown on a recording updates its Notion page, as typed names already do.

## Non-goals

- No cloud voice service. Voices, samples and matching stay on the Mac (WeSpeaker, already used for coaching).
- No raw audio kept for voices. A sample is a 256-number embedding (about 5.5 KB as JSON).
- No change to how speakers are split (diarisation). If the speaker model splits one person into two labels, both just get the same name.
- No new AI calls. Matching is local maths only.
- No voice data through the MCP connector (unchanged anti-goal: AI tools never see voice profiles).

## Anti-goals: what would make this a failure even if it works

- **Confident wrong names.** A wrong name pushed to Notion is worse than "Speaker 2". When in doubt, leave the label.
- **A name the owner typed being overwritten** by a voice match.
- **Errors that teach themselves.** Automatic matches never become new samples, so a wrong match can't reinforce itself. Only names a person set (typed in the app, or set through the connector) teach.
- **Duplicate people** ("Will", "will", "William") splitting one voice into several.
- **Coaching breaking or costing more.** Coaching's owner detection and AI review must behave as before, with no extra AI review runs caused by this feature.
- **The app slowing down.** Matching and backlog learning run in the background, one recording at a time.

## Decisions made with the owner

| Topic | Decision |
|---|---|
| Your name | Settings › Your name is linked to your voice. Voice match alone names you, in all transcripts |
| Other people | Learnt from names you give speakers; matched automatically in future recordings |
| Backlog | Learn from names already typed in past transcripts **straight away**, and name past transcripts |
| Samples | One sample per person per recording, so samples come from different conversations; capped at 20 per person |
| Variety | Keep a spread of conditions (rooms, noise, tone), weighted fairly: a 3-hour call counts once |
| Directory | Settings area listing people, with Delete that removes their voice data |
| Deleting a person | Stops future matching; names already in past transcripts stay |
| Typed names | Always win over voice matches |
| Naming a speaker | Pick from known people (normalised names, full names encouraged) |
| Notion | Pages show names, and update when a name changes in the transcript |

## Calls I made (the owner can overrule them)

- **Matched names look like typed ones,** with a small hint on hover: "Matched by voice". In the name editor a matched name says so and offers "Not them".
- **Keep the most varied samples:** when a person has 21 samples, drop the one most similar to the rest (highest mean cosine to the others), not the oldest. This applies to your own voice too.
- **Minimum speech to learn:** a recording only gives a sample for a person who speaks for at least **15 seconds** in total (segments of 1 s or more). Each sample still averages at most 60 s of their speech, as today.
- **Names set through the MCP connector teach voices,** like names typed in the app. The owner names speakers through Claude, so leaving those out would leave the feature with little to learn from. A wrong one is fixed by renaming, which moves the sample.
- **Ambiguity rule:** a speaker is named only if the best person scores at least the threshold **and** beats the second-best person by at least 0.05. Two labels may get the same person (one voice split in two), but only if both pass.
- **"Not them":** rejecting a matched name records that this label in this recording isn't that person, so re-matching never puts it back. The speaker then shows its label.
- **Renaming a person in the directory** renames them everywhere, typed names included (that's what normalisation is for). Notion pages update.
- **Merge:** the directory can merge two people (keep one name; samples combine and are trimmed back to 20; typed names in transcripts are rewritten). The backlog will surface duplicates such as "Will" and "William Moss", so this is needed from day one.
- **Deleting a person** turns their matched names into typed names in past transcripts (so those stay, as agreed), marked as frozen so they never teach, and deletes their samples. It doesn't touch Notion pages, since the names don't change. Typing that name on a speaker later starts a new person from scratch.
- **Deleting a recording** keeps the samples learnt from it: forgetting a recording shouldn't forget a voice. The directory's count shows recordings that still exist.
- **Typing your own name** (the profile name, any letter case) on a speaker teaches your voice profile, as the coaching pick does. It doesn't change coaching's owner pick or re-run coaching.
- **No profile name set:** your speaker isn't named, and the People section asks you to add your name, with a link to the field.
- **Where it lives:** a new "People" section in Settings › Transcription & AI, straight after "Your voice & coaching". The "Your name" field gets a hint: "Used for your voice in transcripts. See People."

## How it fits together

### The voice store

One file, `COACHING_DIR / "voices.json"` (mode 0600, atomic writes like `voice_profile.json` today), replaces `voice_profile.json`:

```json
{
  "version": 2,
  "people": {
    "me":       {"name": null, "is_me": true,  "samples": [...], "rejected": []},
    "p_9f2c1a": {"name": "Harry Fisher", "is_me": false, "samples": [...]}
  },
  "updated_at": "..."
}
```

- A sample is `{"vec": [256 floats], "session_id": "...", "label": "Speaker 2", "seconds": 48.2, "added_at": "..."}`. `session_id` and `label` let a correction move or remove exactly the sample a rename created.
- The owner is the `me` entry. Its name is never stored: it is always read from `config["profile"]["name"]`, so changing your name in Settings changes it everywhere.
- **Migration:** on first load, `voice_profile.json` exemplars become `me` samples with no `session_id`. A copy of the old file is kept as `voice_profile.json.v1-backup`.
- **Older versions keep working (my call, after the builder check):** the canary channel makes going back a version realistic, so every save also writes `me`'s vectors to `voice_profile.json` in the v1 format. An older version then still finds your voice.
- **Coaching:** `voice.match_owner` reads the `me` samples. The voice-setup and "forget my voice" routes act on `me`. Coaching's behaviour is otherwise unchanged.
- People ids are random and stable. Names are unique case-insensitively (the store enforces it).

### Per-transcript names

The transcript JSON keeps `speaker_names` (typed, as today) and gains:

```json
"speaker_voice_matches": {
  "labelling": "<labelling_fingerprint>",
  "matches":  {"Speaker 1": {"person": "me", "score": 0.81}, "Speaker 3": {"person": "p_9f2c1a", "score": 0.66}},
  "rejected": {"Speaker 2": ["p_9f2c1a"]}
}
```

- **Effective names** (`effective_names`), in order: a typed name, then a voice match (the person's current name; your profile name for `me`), then the label. Matches only count while `labelling` equals the transcript's current `labelling_fingerprint`. Re-transcribing re-matches.
- **Your own speaker:** your own coaching pick (source `user`) for the current labelling names your speaker and wins over a people match for those labels. Otherwise `me` is matched like everyone else, with the people threshold and ambiguity rule (coaching's own voice match doesn't name anyone, so the two thresholds stay independent). The old "user pick only" restriction is removed.
- **Speakers mixed up:** a recording marked mixed up gets no voice matches and teaches nobody, since its speaker split is known to be wrong. Typed names still show.
- **Don't coach this recording:** voice matches apply as normal, you included (naming isn't coaching).
- The API gives the UI the effective names plus, per label, whether the name came from a voice match (and the score) so the hover hint can show.
- Summaries stay in label form (unchanged), so matched names apply wherever names apply today: transcript, summary, coaching report, exports, MCP and Notion.

### Matching (`app/coaching/voice.py` or a new `app/people/` package)

- Runs after the speakers are final, at the same hook that starts coaching (`app/routes/transcribe.py`, next to `start_coaching_async`), in a background worker shared with backlog jobs (one recording at a time).
- Only when the voice model is installed and the recording's audio is on the Mac. No model means no matching and no error, and the People section offers to install it.
- For each label: one embedding (`speaker_embeddings`, as today). Score per person = best cosine over their samples. Apply the threshold and the ambiguity rule above, and skip any person in `rejected` for that label.
- **Embedding cache (my call, after the builder check):** each recording's per-label embeddings are saved once in `COACHING_DIR/embeddings/<session_id>.json` (mode 0600, with the labelling fingerprint, and recomputed when it changes). Coaching's owner match, learning, the backlog and re-matching all read it, so audio is converted and embedded once per recording. Deleting a recording deletes its cache file. These are voice data and stay on the Mac like `voices.json`; SECURITY.md says so.
- **Learning someone new names them in the past too:** when a person gains samples, every recording without a typed name for that speaker is re-matched from the cache (maths only, no audio work), in the background worker. Changes queue Notion updates as usual. So naming Harry once fills him in everywhere the voice is confident.
- A change in the matched names counts as a name change: it bumps the library change counter and queues a full Notion update when the page exists (`speakers._names_changed`).

**Threshold, calibrated on the owner's own library (my call, after the builder check):** the existing `MATCH_THRESHOLD = 0.55` was tuned only for "is this the owner", so people matching has its own constant and is calibrated inside the app, not by a script:

- After backlog phase 1, the app runs a leave-one-recording-out test over the typed names. For each named speaker it hides that recording's sample, matches the speaker, and checks the result. It does this for thresholds 0.45 to 0.80 in steps of 0.025.
- It picks the lowest threshold where wrong names are under 2% of named speakers, and saves it (`people_match_threshold` in config). With fewer than 20 named speaker instances to test on, it uses a conservative 0.70 and calibrates again as names are added (whenever the count doubles).
- The People section shows the result in one line ("Tested on your recordings: 96% of speakers it names are right").
- Phase 2 and live matching only start once a threshold exists, so nothing reaches Notion before calibration.
- `scripts/voice_eval.py` wraps the same function and prints the full table, for development.
- Coaching keeps its own `MATCH_THRESHOLD` and margin, unchanged.

### Learning (when a name is set)

`speakers.set_names` (app and MCP alike) queues learning after it saves:

- **Name set or changed to a person:** find the person by name, ignoring case and spaces, or create them. Remove any earlier sample from this `session_id` and `label` (from whoever had it), then add the new sample if the label has at least 15 s of speech.
- **Name cleared:** remove the sample this `session_id` and `label` gave.
- **Typing over a matched name** is a correction: the same as setting a name, plus the old person goes into `rejected` for that label.
- Your profile name typed on a speaker teaches `me`.
- An excluded recording ("Don't coach this recording") still teaches people's voices, since exclusion is about coaching. It never teaches `me`, as today.
- Trimming to 20 samples uses the "most varied" rule.
- Names that came from a deleted person (see the directory) never teach. The transcript marks them in `speaker_names_frozen: ["Speaker 3"]`, and typing a new name over one removes the mark.

### Backlog

- Runs once, automatically, after the update (a flag in config marks it done), and again from the People section ("Learn from past recordings").
- **Phase 1, learn:** every transcript with typed names and audio on the Mac gives samples by the rules above. Typed names are grouped case-insensitively. Near-duplicates (such as "Will" and "William Moss") are left as separate people for the owner to merge, never merged automatically.
- **Phase 2, name:** every transcript is matched, newest first. Changes queue Notion updates like any name change, through the existing queue (which already retries and paces).
- One recording at a time on a background thread, with progress in the People section ("Learning voices: 120 of 340 recordings"), cancellable, and it resumes after an app restart.
- Skips recordings without audio or speaker labels, recordings marked mixed up, and frozen names.

### The people directory (Settings › Transcription & AI › People)

- **Top line:** "Open Pin recognises N people by voice. Voice data stays on this Mac (about X KB)."
- **You** first: your profile name (or "Add your name" linking to the field), samples n/20, and a link to "Your voice & coaching".
- **Then each person:** name, recordings they're named in, samples n/20, size, and when last heard. Actions: **Rename**, **Merge into…**, **Delete** (confirm: "Delete Harry Fisher's voice? Open Pin will stop recognising them. Names in past transcripts stay.").
- **Learn from past recordings** button with progress, and the backlog progress while it runs.
- When the voice model isn't installed: one line and the existing Install button (shared with "Your voice & coaching").

### Naming a speaker (transcript view)

- The existing inline editor's `<datalist>` becomes a picker: known people (directory first, then names typed in transcripts but with no voice yet), most used first, filtered as you type, with "Add new person: …".
- **Before saving a new name:** an exact match ignoring case uses the existing spelling silently. A close one (a first name matching a known full name, or within two letters' difference) asks "Did you mean William Moss?" with both choices.
- The placeholder suggests a full name ("Full name, e.g. Harry Fisher").
- A matched name shows "Matched by voice" with Not them and Change.
- The MCP `set_speaker_names` tool description tells AI tools to check `list_people` for existing spellings (it already does) and that names teach voices.

## Constraints

- **Stack:** Python 3.12 Flask backend, plain-JS frontend with globally unique top-level names across `app/ui/*.js` (a duplicate broke v2.1.5), Tauri v2 shell. No new Python dependency.
- **Bundle:** a new backend package must be listed in `src-tauri/tauri.conf.json` `bundle.resources` (`test_bundle_ships_every_app_package` guards it).
- **Locks:** transcript writes under `get_transcript_lock`. The voice store gets its own lock. Never hold the transcript lock while computing embeddings.
- **Tests:** `~/.venvs/plaud-v2/bin/python -m pytest` and `ruff`. Real-model tests use `~/.venvs/plaud-v2-diarize`, otherwise fake embeddings. Cover: migration, effective-name order, threshold and ambiguity, rejection, correction moving samples, the 15 s minimum, variety trimming, backlog resume, delete freezing names, merge, rename, Notion queueing, and MCP never exposing vectors.
- **Copy:** British English, no em dashes, plain words. No `Co-Authored-By` trailers in commits (owner's rule).
- **Docs:** `docs/ARCHITECTURE.md` (people and voices), `SECURITY.md` (what the voice store holds, that it stays local), README (a line under speaker names). The 5 Nov spec's "Your own name" rule is superseded by this one: note it there.

## Left to the builder

- Whether the code lives in `app/coaching/voice.py` or a new `app/people/` package.
- The UI layout of the People section and the picker, following existing settings and editor patterns.
- How the background worker is structured, as long as there's one at a time, it's cancellable and it resumes.
- How the work is split into tasks and releases.

## Testing note

The build Mac (the Mac Mini) has no Open Pin library. Test with fake embeddings and synthetic transcripts, plus a few real-model tests with generated audio in the diarize venv. The owner checks real accuracy with `scripts/voice_eval.py` on the MacBook before the threshold is final.
