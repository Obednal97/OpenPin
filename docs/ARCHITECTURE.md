# Plaud V2 — Architecture

## Overview

Tauri 2 desktop app with a Python Flask backend. The Tauri Rust shell spawns Python as a child process on `localhost:5123`. The renderer is a vanilla-JS frontend served from a WebKit window; it communicates with the backend via HTTP and with the Rust shell via Tauri commands.

```
┌──────────────────────────────────────────────┐
│  Tauri Rust shell (src-tauri/src/lib.rs)     │
│  - Spawns Python subprocess (python.rs)      │
│  - Theme sync (light/dark/auto)              │
│  - OAuth URL scheme (plaud-v2://)            │
│  - Tray icon, login items, dock visibility   │
│  - Auto-updates via GitHub Releases API      │
│  - Bug reporter + diagnostics log            │
│  - Code signed + notarized (Developer ID)    │
├──────────────────────────────────────────────┤
│  Renderer (index.html + JS modules)          │
│  - ui/utils.js       → shared state, DOM     │
│  - ui/recordings.js  → list, search, delete  │
│  - ui/transcript.js  → view, player, crop,   │
│                         editable segments     │
│  - ui/settings.js    → all settings tabs     │
│  - ui/health-gate.js → backend-ready splash  │
│  - renderer.js       → orchestrator + init   │
├──────────────────────────────────────────────┤
│  tauri-bridge.js                             │
│  - window.plaud surface (Tauri invoke + HTTP)│
└───────────────┬──────────────────────────────┘
                │ HTTP (localhost:5123)
┌───────────────▼──────────────────────────────┐
│  Flask API (api_server.py)                   │
│                                              │
│  routes/          state/         providers/  │
│  ├─ core.py       ├─ device.py   ├─ summarize│
│  ├─ device.py     ├─ cloud.py    ├─ transcribe│
│  ├─ cloud.py      └─ notion.py   └─ validate │
│  ├─ notion.py                                │
│  ├─ recordings.py  utils/                    │
│  ├─ transcribe.py  ├─ config.py              │
│  └─ settings.py    ├─ constants.py           │
│                    ├─ audio.py               │
│                    ├─ webhooks.py             │
│                    ├─ system.py               │
│                    ├─ wifi_sync.py            │
│                    └─ wifi_sync_worker.py     │
└──────────────────────────────────────────────┘
```

## Packaging

- **Tauri bundle**: `cargo tauri build` produces a signed/notarized `.dmg`. Frontend assets are staged via `scripts/prepare-frontend.sh` (called by Tauri's `beforeBuildCommand`) into `dist-tauri/`, which is the `frontendDist` Tauri serves from the bundled webview.
- **Bundled resources** (`tauri.conf.json` `bundle.resources`): `src/` (transcription scripts), `keys/` (E2EE RSA keys), and the Python backend (`app/api_server.py`, `app/routes/`, `app/state/`, `app/providers/`, `app/utils/`).
- **FFmpeg**: Downloaded at first use by `app/utils/system.py` into `~/Library/Application Support/com.plaud.v2/bin/ffmpeg`. Not bundled with the app — keeps the DMG small and avoids licensing complexity.
- **Code signing**: Developer ID Application certificate, hardened runtime, entitlements at `src-tauri/Entitlements.plist` (Bluetooth, network client, user-selected file access).
- **Notarization**: Apple notarization via Tauri's bundler. Users can double-click to install without Gatekeeper warnings.

## Python Backend

### Route Modules (`app/routes/`)

| Module | Prefix | Routes | Purpose |
|--------|--------|--------|---------|
| `core.py` | `/` | 2 | Health check, audio file serving |
| `device.py` | `/device` | 17 | BLE scan, connect, disconnect, depair, sync, WiFi sync, record |
| `cloud.py` | `/cloud` | 12 | Plaud Cloud login, sync, upload, trash, device binding |
| `notion.py` | `/notion` | 8 | Notion OAuth, database selection, push |
| `recordings.py` | `/` | 9 | Recording list, tag, rename, export, crop, delete, transcript update |
| `transcribe.py` | `/` | 3 | Transcribe, summarise, validate API keys |
| `settings.py` | `/settings` | 2 | Get/save app settings |

### State Managers (`app/state/`)

Each manager owns module-level state and exposes functions to the routes.

- **`device.py`** — BLE connection, APP_KEY handshake (cmd_id=1), RSA + ChaCha20-Poly1305 encrypted command channel, BLE file sync, WiFi sync orchestration. Runs an asyncio event loop in a background thread for BLE operations.
- **`cloud.py`** — Plaud Cloud authentication (OAuth + token), recording download/upload, OGG-to-raw-Opus unwrapping for cloud upload, device binding and SN signature fetching.
- **`notion.py`** — OAuth 2.0 PKCE flow, MCP session management, Streamable HTTP transport for Notion API.

### AI Providers (`app/providers/`)

- **`summarize.py`** — Cascade: Anthropic Claude Sonnet 4.6 > OpenAI GPT-5.4-mini > local DistilBART (via model.generate()). Includes profile/tag context injection.
- **`transcribe.py`** — OpenAI Whisper API client with custom vocabulary support.
- **`validate.py`** — Lightweight API key validation for OpenAI and Anthropic.

### Utilities (`app/utils/`)

- **`config.py`** — JSON config load/save from `.plaud_v2_config.json`
- **`constants.py`** — All shared constants (BLE UUIDs, API domains, paths, Whisper models, CRC table)
- **`audio.py`** — OGG wrapping/unwrapping, BLE file decryption (RSA + ChaCha20 XOR)
- **`webhooks.py`** — Background webhook firing
- **`system.py`** — Dependency auto-install, RAM info
- **`wifi_sync.py`** — WiFi sync coordinator (hotspot enable, connect, handshake)
- **`wifi_sync_worker.py`** — WiFi sync subprocess (BLE disconnect, hotspot connect, file transfer, internet restore)

## Frontend

Multi-script architecture — modules loaded via `<script>` tags in order, sharing globals through the `app` and `dom` objects defined in `ui/utils.js`.

| File | Lines | Purpose |
|------|-------|---------|
| `ui/utils.js` | 319 | `$()`, `app` state, `dom` refs, formatters, toast, markdown renderer |
| `ui/recordings.js` | 487 | Recording list, filtering, select mode, bulk operations, delete dialog |
| `ui/transcript.js` | 1151 | Transcript view, audio player, audio crop editor, editable segments/summary, tag picker, transcribe/summarise actions |
| `ui/settings.js` | 1457 | All 6 settings tabs, device manager, cloud account, Notion, AI keys |
| `renderer.js` | 1002 | Theme toggle, main action buttons, search, init, background polling, WiFi sync UI |

## CSS Design System

Token-based design system with light/dark mode, split into 7 files:

| File | Purpose |
|------|---------|
| `css/tokens.css` | Design tokens (spacing, radii, typography, colours), dark/light themes, reset |
| `css/components.css` | Buttons, inputs, toggles, badges, toasts, checkboxes |
| `css/layouts.css` | Header, main content, action bar, settings panel |
| `css/recordings.css` | Search bar, select bar, recording cards |
| `css/transcript.css` | Transcript view, audio player, audio crop editor, editable summary, tags, markdown, segments |
| `css/settings.css` | Device manager, cloud account, Notion, AI keys, model selector, delete dialog |
| `css/utilities.css` | Utility classes, animations, responsive breakpoints |

## Config Files

| File | Purpose |
|------|---------|
| `.plaud_v2_config.json` | App settings, API keys, recording metadata, paired devices, webhooks |
| `.plaud_config.json` | Plaud Cloud bearer token |
| `.notion_mcp.json` | Notion OAuth tokens and database selection |
| `.hf_token` | HuggingFace token (bundled for diarization) |

## Key Flows

### BLE Connection & Sync

1. Scan for devices (bleak, 6s, match by manufacturer data)
2. Connect to device, wait for service discovery
3. RSA handshake: send SN signature (65040) > receive RSA blob (65042) > decrypt session keys
4. Establish encrypted ChaCha20-Poly1305 command channel
5. Send APP_KEY handshake (cmd_id=1) with static APP_KEY + audioChannel + serial number
6. Sync time (cmd_id=4) + GET_STATE (cmd_id=3) + GET_STORAGE (cmd_id=6)
7. List files (cmd 26), download each (cmd 28), decrypt (RSA + ChaCha20 XOR), wrap as OGG

### WiFi Sync

1. BLE disconnect (WiFi and BLE share the same radio)
2. Enable device hotspot via BLE command
3. Connect Mac to Plaud WiFi hotspot
4. TCP handshake with device (ChaCha20-Poly1305 encrypted)
5. Transfer files over WiFi (much faster than BLE)
6. Restore original WiFi connection (power-cycle + networksetup fallback)

### Transcription

1. If OpenAI key: send to Whisper API, save transcript immediately
2. If diarization enabled: spawn background thread for pyannote, merge results when done
3. If no OpenAI key: run local Whisper (bundled FFmpeg handles audio decoding), optionally with diarization inline
4. Save JSON transcript + TXT formatted version

### Summarisation Cascade

1. Build context: user profile + recording tags
2. Try Anthropic Claude Sonnet 4.6 (structured JSON prompt)
3. On failure: try OpenAI GPT-5.4-mini (JSON mode)
4. On failure: try local DistilBART (chunked, model.generate())
5. Auto-set recording title from summary if none exists

### Audio Crop

1. Frontend decodes audio via Web Audio API, draws waveform on canvas
2. User drags start/end handles to select region
3. Preview plays selection only
4. Save: backend uses FFmpeg `-ss`/`-to` with `-c copy` (lossless, fast)
5. Options: overwrite original (backs up as `_uncropped.ogg`) or save as new recording

## Auto-Updates

Custom updater in the Rust shell (`src-tauri/src/commands/mod.rs`) using GitHub Releases API with a read-only PAT (`update_token()` at `commands/mod.rs:201`).

- Checks for updates 5 s after launch, then every 4 hours (loop in `lib.rs`).
- Downloads the DMG silently, shows banner when ready.
- Install: detached script mounts the DMG, copies the new `.app` over the old one, relaunches.
- Manual check available in **Settings > General > About**.

## Auto Bug Reporter

Unhandled errors are automatically reported as GitHub Issues (tagged `auto-bug`). Deduplicates by error fingerprint — same error creates a comment on the existing issue instead of a new one. Catches Rust panics (`panic::set_hook`), Python uncaught exceptions (relayed via the `report_error` Tauri command from the renderer), and renderer errors (`window.onerror` / unhandled rejections).

Two filters keep noise low:

- `AbortError` (timeouts, navigation cancellations) → suppressed from GitHub, written to a local diagnostics log at `~/Library/Application Support/com.plaud.v2/diagnostics.log`.
- Backend-down state (the health-gate poller flips this) → suppressed from GitHub while the Python server is unreachable.

The diagnostics log rotates at 1 MB to `.log.1` (≈ 2 MB ceiling). Open it from the splash's "Backend not responding" screen via the **Open diagnostics log** button.

## Testing

112 tests via pytest, covering:
- Config load/save and edge cases
- OGG wrap/unwrap roundtrip
- BLE file decryption validation
- Transcript segment merging and splitting
- Recording date extraction (unix, cloud, ISO formats)
- JSON sanitisation
- API key validation (mocked HTTP)
- Flask endpoint integration tests
