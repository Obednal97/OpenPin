# Open Pin MCP server and speaker names: design

Date: 5 November 2026. Status: agreed with the owner in conversation; a builder review's questions are answered below.

## Intent

The owner wants Claude (Claude Desktop and Claude Code on the same Mac) to use Open Pin's library as material for other AI work: find a recording, read what was said and decided, see the coaching, and tidy up the library (name the speakers, tag recordings). Open Pin becomes a local data source for AI tools, without becoming an AI tool itself.

Speaker naming doesn't exist yet. Transcripts only say "Speaker 1", "Speaker 2". The owner wants to name speakers **both in the app and through the MCP server**, and to see those names everywhere.

## Goals

1. **A local MCP server** ("connector") that Claude Desktop and Claude Code can use while Open Pin is open.
   - **Read:** recording file info, recording date and time, length, title, tags, Notion link, transcript, summary, participants (named speakers) and coaching.
   - **Write:** speaker names, and adding or removing tags.
2. **Speaker names in the app:** name a speaker in a recording's transcript, with names suggested from people named before. The names show in the transcript, the summary, the coaching report, Notion and the MCP server.
3. **One-click connect** for Claude Desktop and Claude Code from Open Pin's settings, backing up their settings files first.

## Non-goals

- No access while Open Pin is closed. The connector then answers with a clear message (owner's choice).
- No other writes: no deleting, re-transcribing, re-summarising, moving, cropping, settings, keys or device control.
- No new AI calls from Open Pin because of this feature. Anything that needs judgement (such as working out who Speaker 2 is) is done by the AI tool calling the connector, on its own account.
- No network access: nothing listens on anything but 127.0.0.1, and nothing is reachable from other machines.
- No other MCP clients built or tested now. Others may work, since it's standard MCP over stdio, but setup is only automated for the two named.
- No recognising people by voice, other than the existing "which speaker is you" from coaching.

## Anti-goals: what would make this a failure even if it works

- AI tools reaching more than the listed data, such as API keys, the voice profile, pairing data or raw audio bytes. Any of these leaking through the connector is a failure.
- An AI edit and a user edit in the app clashing or losing data.
- Editing Claude's settings files in a way that loses the user's other MCP servers or settings.
- Being on without the user knowing.
- Summaries changed in a way that can't be undone (renaming must be reversible).

## Decisions made with the owner

| Topic | Decision |
|---|---|
| When it works | Only while Open Pin is open |
| Clients | Claude Desktop and Claude Code |
| Read | file info, times, Notion link, title, tags, transcript, summary, participant names, coaching |
| Write | speaker names (set, change, clear), tags (add, remove) |
| Speaker naming | In the app and through the connector, shown everywhere |
| Existing summaries | Names swapped in **without any AI call from Open Pin** |
| Notion | Already-pushed pages updated with the names, again with no AI call |
| Setup | One click per client, with a backup of the settings file |
| Your own name | Filled in on your speaker **only once you've confirmed** which speaker is you (your own pick), never from a voice match alone. *Superseded by [2026-11-11-voice-people-design.md](2026-11-11-voice-people-design.md): a voice match can now name you and others.* |
| New tags | AI tools **may create new tags**, and new tags join the app's tag picker |

## Calls I made (the owner can overrule them)

- **Off by default.** A switch, "Let AI tools on this Mac use Open Pin", sits in a new Settings section. Its description says that turning it on lets AI tools read your transcripts, summaries and coaching.
- **Removing tags is allowed too**, not just adding.
- **Search** is part of the listing tool, because finding the right recording is the first thing an AI tool needs.
- **Excluded recordings** ("Don't coach this recording") show their transcript but never coaching.
- **Name suggestions** when naming a speaker come from names used in other recordings.
- **Your name on your speaker** (owner's rule: only once confirmed):
  - Your speaker shows your profile name (`config["profile"]`) only when you picked that speaker yourself, which is the coaching owner pick with source `user` ("Which one is you?" or "Speakers mixed up" then a pick).
  - A voice match alone never names a speaker. It still drives coaching as before.
  - The default never overrides a name you typed. Confirming who you are counts as a name change, so an already-pushed Notion page updates.
  - Undoing the pick (marking speakers mixed up, or excluding the recording from coaching) removes the default name.
- **No new Python dependency.** The connector speaks MCP's stdio JSON-RPC itself (initialize, tools/list, tools/call, ping), so the hash-locked `app/requirements/core.txt` doesn't grow by pydantic, anyio and the rest of the official SDK. Its surface is a handful of tools, so this is small.

## How it fits together

```
Claude Desktop / Claude Code
   │ spawns (stdio)
   ▼
open-pin-mcp launcher  ── stable path: ~/Library/Application Support/com.plaud.v2/mcp/open-pin-mcp
   │ execs the app's managed venv Python with the connector script from the CURRENT app bundle
   ▼
connector (app/mcp/server.py): MCP over stdio, no state of its own
   │ reads the connection file (port + MCP key); none, or a refused connection → "Open Pin isn't running"
   │ HTTP to 127.0.0.1:<port>/mcp/v1/*, header X-Open-Pin-MCP-Key
   ▼
Open Pin backend (Flask): /mcp/v1/* blueprint → same store functions the app uses (same locks)
```

### Why through the backend

Every write goes through the running app's own code paths and locks (the transcript lock, the config update function), so an AI edit and a user edit can't interleave badly. The open window also learns about changes, which file writes from outside the app couldn't do safely.

### The MCP key: a separate, narrow credential

- The connector never gets the app's per-launch API token, which opens the whole backend.
- While MCP access is on and the app is running, the backend creates a random **MCP key**. It writes `{port, key, pid}` to `mcp-connection.json` in the data folder, mode 0600, rewritten each launch.
- The file is deleted when the switch is turned off, and on clean quit. A stale file left by a crash is harmless: the connector gets a refused connection or a rejected key and says the app isn't running.
- The backend accepts the MCP key **only** on `/mcp/v1/*`, and those routes accept nothing else (not the app token either). The existing CORS and Host checks still apply. Requests from browser origins are refused on `/mcp/v1/*`.
- Turning the switch off revokes the key at once, and the routes then answer 403 with "AI tool access is turned off in Open Pin".
- A local process could already read the data folder directly. The key is there so that being able to reach the backend's port isn't enough to use the full API, and so that turning the switch off really stops access.

### Launcher and bundle moves

- Claude's settings point at a fixed launcher path in Application Support, never into the app bundle. The bundle can move or be replaced by an update.
- The app rewrites the launcher at every launch while MCP access is on. It's a small shell script that runs the managed venv's Python with the connector script inside the current bundle (`Contents/Resources/app/mcp/server.py`, which must be listed in `tauri.conf.json` `bundle.resources`). The launcher sets the environment the connector needs, such as the data folder path for the connection file.
- If the app has been deleted, the launcher fails with a clear message on stderr, and Claude shows the server as failed.

## The connector's tools

Names and shapes can be tuned by the builder, but the set and the limits below are decided. All output is JSON text content. Times are ISO 8601 with offset, in the Mac's time zone. Durations are in seconds.

**Read**

1. `list_recordings(query?, tag?, from?, to?, limit=25, cursor?)`
   - Searches title, tags, speaker names, transcript and summary text, case-insensitive, with plain substring matching. Newest first.
   - Each item has: `id`, `title`, `recorded_at`, `duration_s`, `tags`, `participants` (named speakers, plus you if known), `has_transcript`, `has_summary`, `coaching_status`, `notion_url`.
   - Paged with an opaque cursor, at most 100 per page.
2. `get_recording(id, include=["transcript","summary","coaching"])`
   - **Everything from the list item, plus file info:** the file name, the format, the size in bytes, the folder it's stored in as a display path (with the home folder shown as `~`), where it is (`local` or Google Drive), and whether the audio is on this Mac.
   - **Speakers:** the `{label: name}` map for each speaker, and which label is you.
   - **Transcript:** segments as `{start_s, end_s, speaker_label, speaker_name, text}`.
   - **Summary:** the text with names swapped in.
   - **Coaching:** status, metrics and review. Left out for excluded recordings.
   - Very long transcripts are returned whole. MCP clients can handle large text, but the builder may add `max_segments` paging if a client struggles.
3. `list_people()`: every speaker name used across the library, with how many recordings each appears in. AI tools use it to keep spellings consistent.
4. `list_tags()`: every tag, with counts.

**Write**

5. `set_speaker_names(id, names)`
   - `names` maps labels to a name or null, e.g. `{"Speaker 2": "Sarah Jones", "Speaker 3": null}`.
   - A label not in the transcript is an error. Null clears a name.
   - Names are trimmed. Each is 1 to 80 characters, with no control characters and no `:` or `*`, because those characters break transcript and Markdown formatting.
   - Returns the updated speaker map.
6. `add_tags(id, tags)` and `remove_tags(id, tags)`:
   - `add_tags` may create new tags (owner's choice). A new tag is added to the app's tag list (`custom_tags`) so it appears in the tag picker. This is the one settings write the connector makes, through the same backend function the app uses when you create a tag.
   - The app has no tag rules today, so these apply to the app and the connector alike: trimmed, 1 to 40 characters, no control characters, matched case-insensitively against existing tags (an existing tag's spelling wins), at most 20 tags per recording, and at most 20 tags per call.

Every write is applied by the same backend function the app's own UI uses, then the app's change signal fires (see below). Bad input gets an MCP tool error with a plain message, never a crash. An unknown id gets "No recording with that id".

## Speaker names

### Storage

- A per-recording map `speaker_names: {"Speaker 2": "Sarah Jones"}` kept in the recording's transcript record, under the same transcript lock as other transcript edits.
- The labels stay as they are in the segments. Names are a layer on top, so clearing a name brings the label back.
- **Re-transcribing** can renumber speakers, so names would then point at the wrong people. When a transcript is replaced with different speaker labelling, the names are dropped and the UI says so once ("Speaker names were cleared because the speakers were detected again"). Coaching already uses `labelling_fingerprint` for the same problem. Reuse it.

### In the app

- **In the transcript view,** each speaker label can be clicked. A small inline editor offers a name field with suggestions from names used elsewhere (`list_people` data), plus Save, Clear and Cancel.
- **Every line** shows the name instead of the label. Hovering or a muted suffix still shows the label, so "Speaker 2" stays findable.
- **The recording list** shows participants where it already shows details. The builder chooses the exact spot, following the existing row layout.
- **The coaching report** uses names where it refers to speakers ("Which one is you?" candidates, reactions such as "what the other person did next", and the speaker share).
- **Search** in the app matches speaker names too.

### Summaries, without AI calls

- Summaries keep the text as written, in label form. Names are applied **when the summary is shown, exported, pushed to Notion or read through the connector**, by replacing whole-word occurrences of each named label ("Speaker 2", not "Speaker 21"), so renaming is always reversible.
- **New summaries:** the summariser is sent the transcript with **labels**, not names, so that every summary stays in label form and renaming keeps working.
  - The cost: the AI can't use a name it would otherwise see.
  - The upside: one rule for every summary.
  - The builder must not change this without asking.
- A summary can contain the user's own edits. The swap only touches exact label matches.
- **Editing a summary in the app** (my call): the editor shows the names. On save, whole-word occurrences of the names the editor showed (sent with the summary as `summary_names`) are turned back into their labels, so stored summaries stay in label form and renaming keeps working. A name the user types that matches a current name is stored as that label, which displays the same.

### Notion

- When names change on a recording that's already linked to a Notion page, Open Pin re-sends its own section of that page with the names applied (`notion-update-page` replacing the app-owned section, as updates already do). It goes through the existing background Notion queue, so a failure retries like other updates.
- There's no AI call. Pages that were never pushed stay unpushed.
- The page title isn't changed.

### The open window

The app has to show connector edits without a manual refresh.

- The backend keeps a change counter. The frontend's existing polling can read it, or it can be a new light poll while the window is focused.
- When the counter changes, the recording list reloads, and so does the open transcript, as long as the user isn't editing it at that moment.
- **Conflict rule: the last write wins, per item.** Writes change one speaker's name or one tag. Today the app's own tag saving (`/recordings/tag`, bulk tagging too) sends the window's whole cached tag list, so a stale window would wipe a tag an AI tool just added. Change the app's tag saving to add and remove individual tags, so whole-list overwrites can't happen.

## Settings: "AI tools (MCP)"

This is a new section in Settings › General, or Accounts & storage if that fits better.

- **The switch:** "Let AI tools on this Mac use Open Pin", off by default. The hint says: "Claude and other AI tools on this Mac can read your recordings' details, transcripts, summaries and coaching, and can name speakers and change tags. Only while Open Pin is open. Your API keys, voice and audio files are never shared."
- **Connect to Claude Desktop** and **Connect to Claude Code** buttons, enabled while the switch is on. Each shows whether it's connected, and a Disconnect link.
- **Claude Desktop:** the button adds `mcpServers["open-pin"]` with `{"command": "<launcher path>"}` to `~/Library/Application Support/Claude/claude_desktop_config.json`.
  - It keeps every other key, and creates the file if it's missing.
  - First it copies the existing file to `claude_desktop_config.json.open-pin-backup-<YYYYMMDD-HHMMSS>`, keeping the last 3 backups.
  - It writes atomically (temporary file, then rename) and keeps the file's permissions.
  - If the file isn't valid JSON, it refuses and says so, never overwriting it.
  - Afterwards it tells the user to quit and reopen Claude Desktop.
- **Claude Code** (my call, after the reviewer found the owner's setup isn't the default):
  - A desktop app can't see shell aliases or `CLAUDE_CONFIG_DIR`, so the app looks for every Claude Code settings file in the home folder: `~/.claude.json` and `~/.claude*/.claude.json`.
  - It lists them with their folder, and the user ticks which to connect. A single file found is pre-ticked.
  - Each ticked file gets `mcpServers["open-pin"] = {"type": "stdio", "command": "<launcher path>", "args": []}` at its top level (user scope), with the same backup, atomic-write and keep-everything rules as Claude Desktop.
  - It never runs the `claude` CLI.
  - "Connected" is read from the files, so it's always true to what's on disk.
- **Disconnect** removes only the `open-pin` entry, backing up first, the same way.
- **Turning the switch off** revokes the key. It doesn't edit Claude's files: the entry stays, and the connector says access is turned off.

## Constraints

- **Stack:** Python 3.12 Flask backend, plain-JS frontend with globally unique top-level names across `app/ui/*.js`, and a Tauri v2 shell.
- **Tests:** `~/.venvs/plaud-v2/bin/python -m pytest` and `ruff`. Every backend change gets tests, and the connector gets protocol tests: initialize, tools/list, each tool, the app not running, and access turned off.
- **Bundle:** new backend packages and files must be listed in `src-tauri/tauri.conf.json` `bundle.resources`. `test_bundle_ships_every_app_package` guards packages.
- **Copy:** British English, no em dashes in user-facing copy or docs, and no `Co-Authored-By` trailers in commits.
- **Docs:** `docs/ARCHITECTURE.md` (a new section on the MCP server and speaker names), `SECURITY.md` (the new key, what's exposed, what isn't) and the README (a short "Use with Claude" section with the one-click steps).

## Left to the builder

- Tool names and argument names within the limits above, the MCP protocol version string (use a current one that both Claude Desktop and Claude Code accept), and server instructions text.
- The UI layout details of the name editor and the settings section, following existing patterns.
- How the change counter is polled.
- How the work is split into tasks and releases. Speaker names could ship before the connector.

## Testing note

The build Mac (the Mac Mini) has no Claude Desktop and no installed Open Pin. Test the settings-file editing against temporary home folders, and test the connector end to end against a backend started from the source tree. The owner checks the one-click setup with both clients on the MacBook.

## Unverified, check during build

- The user-scope `mcpServers` schema in current Claude Code `.claude.json` files.
- The exact location and schema of Claude Desktop's config on current versions.
- That both clients start a stdio server from a shell-script launcher with no arguments, and show its stderr message when it fails.
