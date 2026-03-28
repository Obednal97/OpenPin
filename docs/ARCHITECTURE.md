# Plaud V2 — Architecture

## Overview

Electron 41 desktop app with a Python Flask backend. The Electron main process spawns Python as a child process on `localhost:5123`. The renderer communicates with the backend via HTTP.

```
┌──────────────────────────────────────────────┐
│  Electron (main.js)                          │
│  - Spawns Python subprocess                  │
│  - Theme sync (light/dark/auto)              │
│  - OAuth URL scheme (plaud-v2://)            │
│  - Tray icon, login items, dock visibility   │
│  - Auto-updates via electron-updater         │
│  - Clean BLE disconnect on quit/reload       │
├──────────────────────────────────────────────┤
│  Renderer (index.html + JS modules)          │
│  - ui/utils.js    → shared state, DOM, toast │
│  - ui/recordings.js → list, search, delete   │
│  - ui/transcript.js → view, player, actions  │
│  - ui/settings.js   → all settings tabs      │
│  - renderer.js       → orchestrator + init   │
├──────────────────────────────────────────────┤
│  preload.js (contextBridge)                  │
│  - ~40 API methods exposed as window.plaud   │
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
│                    └─ system.py               │
└──────────────────────────────────────────────┘
```

## Python Backend

### Route Modules (`app/routes/`)

| Module | Prefix | Routes | Purpose |
|--------|--------|--------|---------|
| `core.py` | `/` | 2 | Health check, audio file serving |
| `device.py` | `/device` | 11 | BLE scan, connect, disconnect, depair, sync, record |
| `cloud.py` | `/cloud` | 8 | Plaud Cloud login, sync, upload, trash |
| `notion.py` | `/notion` | 7 | Notion OAuth, database selection, push |
| `recordings.py` | `/` | 7 | Recording list, tag, rename, export, delete, transcripts |
| `transcribe.py` | `/` | 3 | Transcribe, summarise, validate API keys |
| `settings.py` | `/settings` | 2 | Get/save app settings |

### State Managers (`app/state/`)

Each manager owns module-level state and exposes functions to the routes.

- **`device.py`** — BLE connection, handshake (RSA + ChaCha20), encrypted command channel, file sync. Runs an asyncio event loop in a background thread for BLE operations.
- **`cloud.py`** — Plaud Cloud authentication (OAuth + token), recording download/upload, OGG-to-raw-Opus unwrapping for cloud upload.
- **`notion.py`** — OAuth 2.0 PKCE flow, MCP session management, Streamable HTTP transport for Notion API.

### AI Providers (`app/providers/`)

- **`summarize.py`** — Cascade: Anthropic Claude Sonnet 4.6 > OpenAI GPT-5.4-mini > local DistilBART. Includes profile/tag context injection.
- **`transcribe.py`** — OpenAI Whisper API client with custom vocabulary support.
- **`validate.py`** — Lightweight API key validation for OpenAI and Anthropic.

### Utilities (`app/utils/`)

- **`config.py`** — JSON config load/save from `.plaud_v2_config.json`
- **`constants.py`** — All shared constants (BLE UUIDs, API domains, paths, Whisper models, CRC table)
- **`audio.py`** — OGG wrapping/unwrapping, BLE file decryption (RSA + ChaCha20)
- **`webhooks.py`** — Background webhook firing
- **`system.py`** — Dependency auto-install, RAM info

## Frontend

Multi-script architecture — modules loaded via `<script>` tags in order, sharing globals through the `app` and `dom` objects defined in `ui/utils.js`.

| File | Lines | Purpose |
|------|-------|---------|
| `ui/utils.js` | 166 | `$()`, `app` state, `dom` refs, formatters, toast, markdown renderer |
| `ui/recordings.js` | 332 | Recording list, filtering, select mode, bulk operations, delete dialog |
| `ui/transcript.js` | 505 | Transcript view, audio player, tag picker, transcribe/summarise actions |
| `ui/settings.js` | 884 | All 6 settings tabs, device manager, cloud account, Notion, AI keys |
| `renderer.js` | 315 | Theme toggle, main action buttons, search, init, background polling |

## CSS Design System

Token-based design system with light/dark mode, split into 7 files:

| File | Purpose |
|------|---------|
| `css/tokens.css` | Design tokens (spacing, radii, typography, colours), dark/light themes, reset |
| `css/components.css` | Buttons, inputs, toggles, badges, toasts, checkboxes |
| `css/layouts.css` | Header, main content, action bar, settings panel |
| `css/recordings.css` | Search bar, select bar, recording cards |
| `css/transcript.css` | Transcript view, audio player, tags, markdown, segments |
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
5. Send bind token + sync time + GET_STATE + GET_STORAGE
6. List files (cmd 26), download each (cmd 28), decrypt (RSA + ChaCha20 XOR), wrap as OGG

### Transcription

1. If OpenAI key: send to Whisper API, save transcript immediately
2. If diarization enabled: spawn background thread for pyannote, merge results when done
3. If no OpenAI key: run local Whisper, optionally with diarization inline
4. Save JSON transcript + TXT formatted version

### Summarisation Cascade

1. Build context: user profile + recording tags
2. Try Anthropic Claude Sonnet 4.6 (structured JSON prompt)
3. On failure: try OpenAI GPT-5.4-mini (JSON mode)
4. On failure: try local DistilBART (chunked)
5. Auto-set recording title from summary if none exists

## Auto-Updates

Custom updater using GitHub Releases API (no code signing required). See [RELEASING.md](RELEASING.md) for full workflow.

- Checks for updates 5s after launch, then every 4 hours
- Downloads ZIP silently, shows banner when ready
- Install: detached bash script extracts with `ditto`, safe-swaps the `.app` bundle, ad-hoc signs, relaunches
- Manual check available in Settings > General > About

## Auto Bug Reporter

Unhandled errors are automatically reported as GitHub Issues (tagged `auto-bug`). Deduplicates by error fingerprint — same error creates a comment on the existing issue instead of a new one. Catches both main process (`uncaughtException`) and renderer (`window.onerror`) errors.

## Testing

58 tests via pytest, covering:
- Config load/save and edge cases
- OGG wrap/unwrap roundtrip
- BLE file decryption validation
- Transcript segment merging and splitting
- Recording date extraction (unix, cloud, ISO formats)
- JSON sanitisation
- API key validation (mocked HTTP)
- Flask endpoint integration tests (20 endpoints)
