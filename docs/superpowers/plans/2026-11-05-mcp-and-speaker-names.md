# MCP server and speaker names: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Name speakers in Open Pin (in the app and through AI tools), and give Claude Desktop and Claude Code a local MCP server that reads the library and can name speakers and change tags.

**Architecture:**
- Speaker names are a per-recording `{label: name}` layer on the transcript JSON, applied whenever text is shown, exported, pushed to Notion or read by AI tools. Stored text never changes.
- A `/mcp/v1/*` backend blueprint, guarded by its own key, serves a stdio MCP connector (`app/mcp/server.py`) that Claude starts through a stable launcher script.
- Settings can write Claude's config files with backups.

**Tech stack:** Python 3.12 Flask backend, plain-JS frontend (global scope), Tauri v2 bundle resources, MCP JSON-RPC 2.0 over stdio (hand-written, no SDK).

**Spec:** `docs/superpowers/specs/2026-11-05-mcp-and-speaker-names-design.md`. Read it first: it is binding, and this plan argues from it.

## Global constraints

- Only while Open Pin is open. The connector says "Open Pin isn't running. Open it and try again." when the backend can't be reached.
- `mcp_enabled` defaults to False. With it off, `/mcp/v1/*` answers 403 `{"error": "AI tool access is turned off in Open Pin"}`.
- The MCP key is accepted only on `/mcp/v1/*`, and `/mcp/v1/*` accepts only the MCP key, never the app token. Header: `X-Open-Pin-MCP-Key`. Requests carrying an `Origin` header are refused on `/mcp/v1/*`.
- Nothing is exposed except the data listed in the spec. Never API keys, the voice profile, pairing data or audio bytes.
- No AI calls from Open Pin for any of this.
- Speaker name: trimmed, 1 to 80 characters, no control characters, no `:` or `*`.
- Tag: trimmed, 1 to 40 characters, no control characters. Matched case-insensitively against existing tags, where an existing tag's spelling wins. At most 20 per recording and 20 per call.
- `list_recordings`: limit default 25, max 100, opaque cursor, newest first. Plain case-insensitive substring search over title, tags, speaker names, transcript text and summary.
- Claude config writes: backup `<file>.open-pin-backup-<YYYYMMDD-HHMMSS>` (keep the last 3), atomic write, original file mode kept. Never overwrite invalid JSON. Keep every other key. Server name `open-pin`.
- Paths:
  - Launcher: `~/Library/Application Support/com.plaud.v2/mcp/open-pin-mcp`, mode 0755.
  - Connection file: `<DATA_DIR>/mcp-connection.json`, mode 0600, holding `{"port", "key", "pid"}`.
- British English, no em dashes in user-facing copy or docs. No `Co-Authored-By` trailers in commits.
- New backend files and packages are listed in `src-tauri/tauri.conf.json` `bundle.resources`; `test_bundle_ships_every_app_package` must pass.
- Tests: `~/.venvs/plaud-v2/bin/python -m pytest -q -p no:cacheprovider` (worktrees need `ln -s /Users/admin/projects/plaud-v2/keys keys`, removed before committing), and `~/.venvs/plaud-v2/bin/ruff check app/ src/ tests/` plus `ruff format --check` on touched files. Frontend: static and node-driven tests in the style of `tests/test_coaching_ui.py`.

## Review focus

1. **A label inside a longer one:** naming "Speaker 1" must not touch "Speaker 10" or "Speaker 12". Tests are in Task 1.
2. **Re-transcribing with different labelling** drops the names, so they never land on the wrong people. A re-transcribe with the same labelling keeps them. Tests are in Task 1.
3. **A stale window saving tags** after an AI tool added one must not remove the AI's tag. Test in Task 2.
4. **The app token sent to `/mcp/v1/*`,** or the MCP key sent to any other route, is refused, and so is a browser-origin request. Tests are in Task 3.
5. **A Claude config file that's invalid JSON,** missing, or has other servers: never corrupted, others kept, backup made. Tests are in Task 6.

---

### Task 1: Speaker names (backend)

**Files:**
- Create: `app/utils/speaker_names.py` (pure functions), `app/state/speakers.py` (storage and people)
- Modify: `app/routes/recordings.py` (`/transcripts` adds names; new routes; summary save converts names to labels; `/recordings` adds participants), `app/routes/transcribe.py` (`_merge_transcript` drops names on relabel), `app/routes/notion.py` (`_build_notion_content` applies names), the export paths in `app/routes/recordings.py`, `app/coaching/runner.py` (a user owner pick, mixed-up or exclude triggers a Notion full update when linked)
- Test: `tests/test_speaker_names.py`

**Interfaces (produces):**
- `speaker_names.validate_name(name: str) -> str`: returns the trimmed name. Raises `ValueError` with a user-facing message.
- `speaker_names.apply_names(text: str, names: dict[str, str]) -> str`: whole-word label to name. Matches `Speaker N` only when not followed by a digit.
- `speaker_names.names_to_labels(text: str, names: dict[str, str]) -> str`: the reverse. Whole-word, longest names first.
- `speaker_names.effective_names(transcript: dict, sidecar: dict | None, profile_name: str | None) -> dict[str, str]`:
  - Starts from stored `speaker_names`.
  - Adds `profile_name` for each owner label only if `sidecar["owner"]["source"] == "user"`, its `labelling` equals `labelling_fingerprint(segments)`, `sidecar.get("mixed_up")` is not True, the recording isn't excluded, and the label has no stored name.
- `speakers.get_names(session_id) -> dict[str, str]`: the effective names, as above.
- `speakers.set_names(session_id: str, changes: dict[str, str | None]) -> dict[str, str]`:
  - Runs under `get_transcript_lock(session_id)`. Null removes a name.
  - Raises `KeyError` for no transcript, and `ValueError` for an unknown label or a bad name.
  - Stores `speaker_names` and `speaker_names_labelling` (= `labelling_fingerprint(segments)`).
  - Bumps the change counter (`app.utils.changes.bump()`; it's already in the base commit).
  - Enqueues `notion_queue.enqueue(sid, KIND_FULL)` when a page is linked.
  - Returns the stored map.
- `speakers.list_people() -> list[dict]`: `[{"name": str, "recordings": int}]` over stored names (not the profile default), sorted by count descending then name.
- Transcript JSON gains `speaker_names: {}`, `speaker_names_labelling: str` and `speaker_names_cleared: true` (set when a merge dropped names; the UI clears it via the route below).
- Routes, all app-token like the others:
  - `POST /transcripts/speaker-names` takes `{session_id, names: {label: name|null}}` and returns `{speaker_names}`. Errors: 404 for no transcript, 400 with a message otherwise.
  - `GET /people` returns `list_people()`.
  - `POST /transcripts/speaker-names/ack-cleared` takes `{session_id}`.
  - `GET /transcripts` items add `speaker_names` (effective), `stored_speaker_names` and `speaker_names_cleared`.
  - `/recordings` items add `participants: [names]`.

- [ ] **Step 1:** Write failing tests in `tests/test_speaker_names.py`:
  - `test_apply_names_whole_word`: `apply_names("Speaker 1 and Speaker 10 met", {"Speaker 1": "Sarah"}) == "Sarah and Speaker 10 met"`.
  - `test_names_to_labels_round_trip`.
  - `test_validate_name_rules`: rejects "", 81 characters, "a:b", "a*b" and "\x07". Trims "  Sarah ".
  - `test_set_names_unknown_label_rejected`.
  - `test_set_names_null_clears`.
  - `test_relabel_drops_names`: `_merge_transcript` with different speaker labelling drops the names and sets `speaker_names_cleared`. With the same labelling it keeps them.
  - `test_owner_default_only_after_user_pick`: no profile name with a `source: "voice"` owner. Profile name with `source: "user"` and a matching labelling. None when `mixed_up` is True. A typed name wins.
  - `test_summary_saved_with_names_is_stored_as_labels` (via `/transcripts/update`).
  - `test_notion_content_uses_names`.
  - `test_set_names_enqueues_full_notion_update_when_linked`.
  - `test_user_owner_pick_enqueues_notion_update`.
  - `test_list_people_counts`.
  - `test_routes_404_and_400`.
- [ ] **Step 2:** Run `pytest tests/test_speaker_names.py -q`. Expected: FAIL on import.
- [ ] **Step 3:** Implement the interfaces above. Profile name: `load_config().get("profile", {}).get("name")`; check the key used by `app/ui/settings.js` `PROFILE_FIELD_IDS` (`profile-name`) and the backend's profile shape. Exports in `app/routes/recordings.py` and Notion use `effective_names`.
- [ ] **Step 4:** Run the new tests and the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `Speaker names: storage, routes, summaries, Notion`.

### Task 2: Tags as single add and remove operations, with rules

**Files:**
- Create: `app/utils/tags.py`
- Modify: `app/routes/recordings.py` (new routes; `/recordings/tag` kept for compatibility but unused by the app), `app/ui/transcript.js` (tag picker), bulk tagging in `app/ui/recordings.js` or wherever `saveTags`/`tagRecording` is called, `app/tauri-bridge.js`
- Test: `tests/test_tags.py`

**Interfaces (produces):**
- `tags.normalise_tag(tag: str, existing: list[str]) -> str`: trims, validates (raises `ValueError`) and returns an existing tag's spelling on a case-insensitive match.
- `tags.add_tags(session_id: str, new: list[str]) -> list[str]`:
  - Updates the config's `recording_tags[sid]` and the transcript JSON `tags` (same places as `/recordings/tag`) under `update_config` plus the transcript lock.
  - Adds unknown tags to `custom_tags`, enforces the 20-tag limits, and is idempotent.
  - Bumps the change counter, enqueues the Notion property sync as `/recordings/tag` does, and returns the recording's tags.
- `tags.remove_tags(session_id: str, gone: list[str]) -> list[str]`: the same, removing. A tag the recording doesn't have is a no-op.
- `tags.all_tags() -> list[dict]`: `[{"tag", "recordings"}]` over `custom_tags` plus the tags in use.
- Routes: `POST /recordings/tags/add` and `POST /recordings/tags/remove`, both `{session_ids: [..], tags: [..]}`, returning `{tags: {sid: [...]}}`. `GET /tags`.
- Bridge: `addTags(sessionIds, tags)`, `removeTags(sessionIds, tags)`.

- [ ] **Step 1:** Failing tests:
  - `test_add_is_case_insensitive_and_keeps_existing_spelling`.
  - `test_limits` (21st tag rejected, 41-character tag rejected).
  - `test_new_tag_joins_custom_tags`.
  - `test_remove_missing_is_noop`.
  - `test_stale_window_cannot_wipe_ai_tag`: add "x" through `add_tags`, then the UI path removes only "y". "x" survives.
  - `test_ui_uses_add_remove_not_whole_list`: static check that `app/ui/*.js` no longer calls the bridge method that posts `/recordings/tag`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement, and switch every UI tag change (single and bulk) to `addTags`/`removeTags` with only the tag that changed.
- [ ] **Step 4:** Run the new tests and the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `Tags: add and remove single tags with limits`.

### Task 3: MCP access, key, guard and change counter (backend)

**Files:**
- Create: `app/mcp/__init__.py`, `app/mcp/access.py` (`app/utils/changes.py` is already in the base commit)
- Modify: `app/api_server.py` (request guard, startup and shutdown hooks), `app/routes/settings.py` (`mcp_enabled`), `src-tauri/tauri.conf.json` (`"../app/mcp/*": "app/mcp/"`)
- Test: `tests/test_mcp_access.py`

**Interfaces (produces):**
- Uses `changes.current()` from the base commit. Adds `GET /changes` (app token), which returns `{"counter": n}`.
- `access.MCP_KEY_HEADER = "X-Open-Pin-MCP-Key"`.
- `access.enable(port: int) -> None`: creates the key, writes the connection file (0600) and the launcher.
- `access.disable() -> None`: drops the key and deletes the connection file. The launcher stays.
- `access.current_key() -> str | None` and `access.connection_path() -> Path`.
- `access.launcher_path() -> Path`: `~/Library/Application Support/com.plaud.v2/mcp/open-pin-mcp`. Honour the dev build's folder in the same way `src-tauri/src/paths.rs` and `constants.py` split release and dev.
- `access.write_launcher() -> Path`: a `#!/bin/sh` script with `exec "<sys.executable>" "<bundle app dir>/mcp/server.py"`, exporting `PLAUD_DATA_DIR`. Rewritten each enable/startup.
- Guard in `api_server` `before_request`:
  - For a path under `/mcp/v1/`, refuse an `Origin` header (403), require the MCP key to match `current_key()` (401 otherwise), and when MCP is off answer 403 with the spec's message. Never fall through to app-token auth.
  - For every other path, keep today's checks. The MCP key header alone never authorises.
- `mcp_enabled` in GET/POST settings: on POST true, `access.enable(bound port)`; false, `access.disable()`. At startup, if enabled, `access.enable(port)`. On clean shutdown (atexit), `disable()` removes only the connection file and keeps the setting.

- [ ] **Step 1:** Failing tests:
  - `test_mcp_route_rejects_app_token`, `test_mcp_key_rejected_elsewhere`, `test_mcp_off_is_403_with_message`, `test_origin_refused_on_mcp`.
  - `test_connection_file_0600_and_removed_on_disable`.
  - `test_launcher_is_executable_and_points_at_bundle_server`.
  - `test_change_counter_route`.
  - Mount a throwaway `/mcp/v1/ping` route in the test to exercise the guard.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. The bound port is in `api_server` after `PLAUD_PORT_BOUND`; pass it to `access.enable`.
- [ ] **Step 4:** Run the full suite, including the bundle test. Expected: PASS.
- [ ] **Step 5:** Commit: `MCP access: key, guard, connection file, launcher, change counter`.

### Task 4: `/mcp/v1` data endpoints

Depends on Tasks 1, 2 and 3.

**Files:**
- Create: `app/routes/mcp_api.py` (blueprint registered in `api_server`), `app/mcp/library.py` (assembles read models)
- Test: `tests/test_mcp_api.py`

**Interfaces (produces):** JSON over HTTP. The connector maps these one to one onto tools.
- `GET /mcp/v1/recordings?query&tag&from&to&limit&cursor` returns `{"items": [ListItem], "next_cursor": str|null}`.
  - ListItem: `{id, title, recorded_at, duration_s, tags, participants, has_transcript, has_summary, coaching_status, notion_url}`.
- `GET /mcp/v1/recordings/<id>?include=transcript,summary,coaching` returns ListItem plus:
  - `file: {name, format, size_bytes, folder, location: "local"|"gdrive", audio_on_mac}`, where `folder` shows the home folder as `~`.
  - `speakers: {label: name}`, `you: [labels]` (only a confirmed user pick).
  - `transcript: [{start_s, end_s, speaker_label, speaker_name, text}]`.
  - `summary` with names applied.
  - `coaching`: `{status, metrics, review}`, or null for excluded recordings or with none.
- `GET /mcp/v1/people`, `GET /mcp/v1/tags`.
- `POST /mcp/v1/recordings/<id>/speaker-names` takes `{names}`. `POST /mcp/v1/recordings/<id>/tags` takes `{add: [], remove: []}`.
- Errors are JSON `{error}`: 404 "No recording with that id", 400 validation messages.
- `recorded_at` is ISO 8601 with the local offset. Title falls back as the app does (custom name, auto title, else date).

- [ ] **Step 1:** Failing tests: each endpoint's shape; search over each field; paging; excluded recordings have no coaching; that no response contains `api_key`, `voice_profile`, `token` or audio bytes (a scan of serialised outputs for key names from `config.json`); writes go through `speakers.set_names` and `tags.add_tags`/`remove_tags` and bump the counter.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement, reusing the `/recordings` and `/transcripts` logic through shared helpers (move code into `app/mcp/library.py` callers rather than duplicating listing logic: extract a function from `list_recordings` if needed).
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `MCP API: read and write endpoints`.

### Task 5: Stdio MCP connector

Depends on Task 3's paths and header. It can run in parallel with Task 4, against the endpoint contract above.

**Files:**
- Create: `app/mcp/server.py` (runnable with `python server.py`, standard library only, so it starts fast), `tests/test_mcp_server.py`

**Interfaces (produces):**
- JSON-RPC 2.0, newline-delimited on stdio. Handles `initialize` (answer `protocolVersion` with the client's version if supported, else `"2025-06-18"`; `capabilities: {tools: {}}`; `serverInfo {name: "open-pin", version}`; short `instructions`), `notifications/initialized`, `ping`, `tools/list` and `tools/call`.
- Unknown methods get -32601.
- Tools: `list_recordings`, `get_recording`, `list_people`, `list_tags`, `set_speaker_names`, `add_tags`, `remove_tags`, with JSON Schema `inputSchema` matching the spec's arguments.
- Results are `{content: [{type: "text", text: <json>}], isError: bool}`.
- It reads `PLAUD_DATA_DIR/mcp-connection.json` for every call. A missing file, refused connection or 401 gives `isError` "Open Pin isn't running. Open it and try again.". A 403 passes the backend's message through.
- Logs go to stderr only. Never print to stdout except protocol messages.

- [ ] **Step 1:** Failing tests: drive `server.py` as a subprocess with stdin/stdout. Cover the initialize handshake, `tools/list` names, a call against a stub HTTP server (`http.server` on a free port, with a connection file in tmp), not running, access off (403 message), bad arguments giving `isError`, and stdout containing only JSON-RPC.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with `urllib.request` and a 10 s timeout.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `MCP connector over stdio`.

### Task 6: One-click client setup and Settings UI

Depends on Task 3.

**Files:**
- Create: `app/mcp/clients.py`, `app/ui/mcp-settings.js`, `app/css/mcp-settings.css`
- Modify: `app/routes/settings.py` or a new `app/routes/mcp_setup.py` (app-token routes), `app/index.html` (section), `app/tauri-bridge.js`, `app/ui/settings.js` (render call)
- Test: `tests/test_mcp_clients.py`, `tests/test_mcp_settings_ui.py`

**Interfaces (produces):**
- `clients.claude_desktop_path() -> Path`: `~/Library/Application Support/Claude/claude_desktop_config.json`.
- `clients.claude_code_files() -> list[Path]`: `~/.claude.json` plus `~/.claude*/.claude.json` that exist.
- `clients.status() -> dict`: `{"desktop": {"path", "exists", "connected"}, "code": [{"path", "connected"}]}`. Paths are shown with `~`.
- `clients.connect(path: Path, kind: "desktop"|"code") -> None` and `clients.disconnect(path, kind) -> None`:
  - Backup, atomic write, keep the file mode, keep everything else, refuse invalid JSON (`ValueError` with a message).
  - Desktop entry: `{"command": launcher}`. Code entry: `{"type": "stdio", "command": launcher, "args": []}` under top-level `mcpServers`.
  - Desktop creates the file if missing (mode 0644). Code only edits existing files.
- Routes: `GET /mcp/setup/status`, `POST /mcp/setup/connect {kind, path}` and `POST /mcp/setup/disconnect {kind, path}`. The path must be one returned by `status()`, never arbitrary.
- UI section "AI tools (MCP)" in Settings › General, with the spec's switch copy, hint and buttons:
  - The Claude Code list has ticks.
  - After connecting Desktop, show "Quit and reopen Claude Desktop to start using Open Pin."
  - Turning the switch off doesn't edit Claude's files.
- The frontend polls `GET /changes` every 5 s while the window is focused. On a change, it reloads the recordings and transcripts, and re-renders the open recording unless an editor is open. Put the poll in `app/ui/mcp-settings.js` or `app/renderer.js`; names must be globally unique.

- [ ] **Step 1:** Failing tests:
  - `test_invalid_json_refused_untouched`, `test_other_servers_kept`, `test_backup_made_and_only_three_kept`, `test_mode_preserved`, `test_disconnect_removes_only_open_pin`.
  - `test_connect_rejects_unlisted_path`.
  - `test_code_files_discovered` (fake home via monkeypatched `Path.home`).
  - UI static and node tests: the section exists in General, the switch copy, the buttons disabled while off, the poll reloads on a counter change.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `MCP setup: one-click Claude Desktop and Claude Code`.

### Task 7: Speaker names in the app (frontend)

Depends on Tasks 1 and 2 (it edits `app/ui/transcript.js` after Task 2).

**Files:**
- Modify: `app/ui/transcript.js` (segments, summary view and editor, exports), `app/ui/coaching.js` (names in candidates and reactions), `app/ui/recordings.js` (participants in the row details, search matching names), `app/tauri-bridge.js` (`setSpeakerNames`, `people`, `ackSpeakerNamesCleared`), CSS next to the transcript styles
- Test: `tests/test_speaker_names_ui.py`

**Interfaces (consumes):** Task 1's routes and fields.

- Clicking a `.segment-speaker` opens an inline editor: a text input with a `<datalist>` from `/people`, plus Save, Clear and Cancel. Esc cancels and Enter saves.
- Segments show the name, with the label in the `title` attribute.
- The summary view shows `apply_names` done client-side with the same whole-word rule. Implement `applySpeakerNames(text, names)` in JS and test it against the Python cases. The editor shows names, and saving sends the text: the backend converts names to labels (Task 1).
- Exports (Markdown, TXT, HTML, JSON) use names.
- A once-only notice when `speaker_names_cleared` is set, which then calls ack.

- [ ] **Step 1:** Failing tests: the JS `applySpeakerNames` matches Python on the review-focus cases; the editor markup and handlers exist; exports use names; coaching candidates show names; search includes participants.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `Speaker names in the transcript, summary, exports and coaching`.

### Task 8: Docs

After everything else.

- [ ] Update `docs/ARCHITECTURE.md` (MCP server and speaker names section, the security guard), `SECURITY.md` (the MCP key, what's exposed, what isn't, off by default) and the README ("Use with Claude": turn on, click connect, restart Claude Desktop). Then commit: `Docs: MCP server and speaker names`.

## Base commit (the controller does this before wave 1)

`app/utils/changes.py`: `bump() -> int` and `current() -> int`, a thread-safe in-process counter starting at 0, with `tests/test_changes.py`. Every write path in Tasks 1, 2 and 4 calls `bump()`.

## Execution order

| Wave | Tasks (parallel within a wave) |
|---|---|
| 1 | Task 1, Task 2, Task 3 |
| 2 | Task 4, Task 5, Task 6, Task 7 |
| 3 | Task 8, then the final review, then release v2.8.0 |
