# Open Pin documentation

Start with the [project README](../README.md) for what the app does, how to install it and where
the logs are. Open Pin was called Plaud V2 until the rename after v2.5.3; some internal names still
use that (see [ARCHITECTURE.md](ARCHITECTURE.md#names)). Then, in reading order:

| Doc | Read it to |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Understand how the app works: legacy names, modules, startup, security model, data locations, device sync, AI pipeline, conversation coaching and Engagement, the MCP server and speaker names, Notion, updates |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Run it from source, run the tests and linters, avoid the known gotchas, change Python dependencies |
| [RELEASING.md](RELEASING.md) | Build, sign, notarise and publish a release; rotate the update server's token; what an exported bug report contains |
| [../SECURITY.md](../SECURITY.md) | Report a problem; threat model; secrets policy; accepted risks |
| [../infra/update-worker/README.md](../infra/update-worker/README.md) | The `plaud-v2-updates` Cloudflare Worker: endpoints, limits, deploy, smoke tests |
| [../scripts/README.md](../scripts/README.md) | Build scripts and the legacy device scripts |
| [superpowers/specs/](superpowers/specs/) and [superpowers/plans/](superpowers/plans/) | The design and build plan behind each recent feature: conversation coaching, the MCP server and speaker names, Engagement and laughter. The code is the final word where they differ |

## Reverse engineering

How the NotePin S protocol was worked out, and the protocol itself:

| Doc | Contents |
|---|---|
| [reverse-engineering/README.md](reverse-engineering/README.md) | Index, methodology and open questions |
| [reverse-engineering/PROTOCOL.md](reverse-engineering/PROTOCOL.md) | Transport, handshake and crypto, file transfer, WiFi, cloud endpoints, audio format |
| [reverse-engineering/COMMANDS.md](reverse-engineering/COMMANDS.md) | Per-command reference |
| [reverse-engineering/CAPTURES.md](reverse-engineering/CAPTURES.md) | BLE capture analysis |

## History

Major milestones. Release notes are on each GitHub release; the full story is in git history.

| When | Version | What happened |
|---|---|---|
| March 2026 | v1.0–v1.9 | First version, an Electron app: BLE sync, local decryption, Whisper, summaries, auto-updates and the bug reporter (v1.1), WiFi sync (v1.3–v1.4) |
| April 2026 | v1.10.2 | Signed with Developer ID and notarised |
| April 2026 | [v2.0.0](https://github.com/Obednal97/OpenPin/releases/tag/v2.0.0) | Moved from Electron to Tauri 2 with the Python backend kept as a child process. The DMG shrank from about 137 MB; users had to install 2.0 by hand |
| May 2026 | v2.1.4–v2.1.10 | Whole-codebase audit and fixes; the last Electron code removed; startup health gate and local diagnostics log |
| June 2026 | v2.2.0–v2.2.3 | BLE sync progress and abort, tray sync, battery from cmd 9, recovery after sleep |
| November 2026 | [v2.3.0](https://github.com/Obednal97/OpenPin/releases/tag/v2.3.0), [PR #95](https://github.com/Obednal97/OpenPin/pull/95) | Security and data-loss fixes: per-launch API token, CORS and Host checks, session-ID validation, deletes to the Trash, BLE download integrity |
| November 2026 | [v2.4.0](https://github.com/Obednal97/OpenPin/releases/tag/v2.4.0), [PR #96](https://github.com/Obednal97/OpenPin/pull/96) | Update channel through a Cloudflare Worker (no token in the app), managed Python runtime and hash-pinned dependencies, window-first startup, safer WiFi restore, current AI models, Notion reliability |
| November 2026 | [v2.5.0](https://github.com/Obednal97/OpenPin/releases/tag/v2.5.0)–[v2.5.3](https://github.com/Obednal97/OpenPin/releases/tag/v2.5.3) | First real syncs from a NotePin S; the device's legacy bind token as a fallback; transcription moved to a background queue; bug reports exported locally instead of filed; Notion pages from older versions linked, not duplicated; faster connects |
| November 2026 | [v2.6.0](https://github.com/Obednal97/OpenPin/releases/tag/v2.6.0), [PR #111](https://github.com/Obednal97/OpenPin/pull/111) | Renamed from Plaud V2 to **Open Pin**; updates from openpin.bednal.com with a test channel; v2.6.1 recovers a NotePin left connected after sleep |
| November 2026 | [v2.7.0](https://github.com/Obednal97/OpenPin/releases/tag/v2.7.0), [PR #116](https://github.com/Obednal97/OpenPin/pull/116) | **Conversation coaching**: measured metrics, an AI review with cited moments, your voice learned on the Mac, and a Progress view. v2.7.1 made sync progress count only new recordings; v2.7.2 added coaching to Automation, a measured-only mode and a per-recording opt-out |
| November 2026 | [v2.8.0](https://github.com/Obednal97/OpenPin/releases/tag/v2.8.0), [PR #120](https://github.com/Obednal97/OpenPin/pull/120) | **Speaker names** in the app, and a local **MCP server** so Claude Desktop and Claude Code can read the library, name speakers and tag recordings, with one-click setup |
| November 2026 | [v2.9.0](https://github.com/Obednal97/OpenPin/releases/tag/v2.9.0), [PR #121](https://github.com/Obednal97/OpenPin/pull/121) | **Engagement** score with laughter detected on the Mac. v2.9.1 added Coach again and coaching older reports again; v2.9.2 made the Claude Code setup rows easy to tick |

Planning documents for the Tauri migration and the May 2026 audit were deleted in November 2026
because they no longer matched the code (`docs/superpowers` now holds only the designs for features
built since). To read the old ones:

```bash
git log --diff-filter=D --oneline -- docs/TAURI_MIGRATION.md docs/superpowers
git show <commit>^:docs/TAURI_MIGRATION.md
```
