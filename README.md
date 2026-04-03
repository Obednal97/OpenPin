<p align="center">
  <img src="app/assets/wordmark-black.png" alt="Plaud V2" height="40">
</p>

<p align="center">
An open-source desktop companion for the <b>Plaud NotePin S</b> voice recorder.<br>
Syncs recordings via BLE or WiFi, decrypts audio locally, transcribes with Whisper, and summarises with AI — no cloud required.
</p>

Built as a replacement for the official Plaud app. All processing happens on your Mac unless you explicitly connect cloud AI providers.

## Features

- **BLE sync** — Connect to your NotePin S, download recordings over Bluetooth
- **WiFi sync** — High-speed file transfer via the device's WiFi hotspot
- **Local decryption** — Audio is decrypted entirely on-device (RSA + ChaCha20)
- **Transcription** — Local Whisper or OpenAI Whisper API, with custom vocabulary
- **Speaker diarization** — 3-tier cascade: diarize (fast) -> pyannote-onnx -> pyannote standard
- **AI summaries** — Anthropic Claude / OpenAI GPT / local DistilBART fallback chain
- **Audio cropping** — Waveform editor with draggable handles, save as new or overwrite
- **Editable transcripts** — Edit segment text inline, delete rows, edit summaries as markdown
- **Plaud Cloud sync** — Two-way sync with your existing Plaud account
- **Notion integration** — Push transcripts and summaries to a Notion database via MCP
- **Webhooks** — Trigger external services on transcribe/summarise/sync events
- **Auto-updates** — App checks for new versions and updates itself
- **Apple-style UI** — Glass design, light/dark/auto theme, squircle corners
- **Signed & notarized** — Proper Apple Developer ID signing, no Gatekeeper warnings

---

## Install

### 1. Make sure you have Python 3

Open **Terminal** and run:

```bash
python3 --version
```

If you see `Python 3.x.x`, you're good. If not:

```bash
# Install via Homebrew (recommended)
brew install python3

# Or download from https://www.python.org/downloads/
```

> macOS 13+ usually has Python 3 via Xcode Command Line Tools. If you've ever used `git` in Terminal, you likely already have it.

### 2. Download the app

Go to the [latest release](https://github.com/Obednal97/Plaud-V2/releases/latest) and download **`Plaud-V2-x.x.x.dmg`**.

### 3. Install

1. Open the DMG
2. Drag **Plaud V2** to your **Applications** folder
3. Double-click to open — the app is signed and notarized, so it just works

### 4. First launch

The app will:
- Start a Python backend server automatically
- Auto-install Python dependencies (Flask, Whisper, etc.) — this takes ~30 seconds on first run
- Download Whisper speech models on first transcription (~74 MB for the `small` model)

That's it. Connect your Plaud NotePin S and hit **Scan**.

### Updates

The app checks for updates automatically. When a new version is available, a banner appears at the top — click **Restart to Update** and you're done. You can also check manually in **Settings > General > About > Check for Updates**.

---

## Optional Setup

### Cloud AI (better transcription + summaries)

Add API keys in **Settings > Processing > Cloud AI**:

| Provider | What it does | Get a key |
|----------|-------------|-----------|
| **OpenAI** | Faster transcription (Whisper API) + GPT summaries | [platform.openai.com](https://platform.openai.com/api-keys) |
| **Anthropic** | Higher-quality summaries (Claude) | [console.anthropic.com](https://console.anthropic.com/) |

Without API keys, everything runs locally using Whisper `small` and DistilBART. Works fine, just slower.

### Notion Integration

Push transcripts and summaries to a Notion database:

1. Go to **Settings > Notion**
2. Click **Connect to Notion** and authorise
3. Paste your database URL or search for it
4. Enable **Auto-push** to send summaries automatically

### Plaud Cloud

Sync recordings with your existing Plaud account:

1. Go to **Settings > Cloud Account**
2. Log in with your Plaud credentials
3. Choose sync direction (download / two-way)

---

## Architecture

```
app/
  api_server.py          # Flask API (app factory)
  main.js                # Electron main process + auto-updater
  renderer.js            # Frontend orchestrator
  index.html             # UI markup
  styles.css             # @import hub for CSS modules
  ui/                    # Frontend modules (utils, recordings, transcript, settings)
  routes/                # Flask blueprints (core, device, cloud, notion, recordings, transcribe, settings)
  state/                 # State managers (device, cloud, notion)
  providers/             # AI providers (summarize, transcribe, validate)
  utils/                 # Shared utilities (config, constants, audio, webhooks, system, wifi_sync)
  css/                   # Design system (tokens, components, layouts, recordings, transcript, settings, utilities)
bin/
  ffmpeg                 # Bundled static FFmpeg binary (downloaded by scripts/download-ffmpeg.sh)
src/
  transcribe.py          # Whisper transcription + diarization cascade
  crypto.py              # RSA key management
  scanner.py             # BLE manufacturer data parser
tests/                   # 112 tests (pytest)
docs/
  ARCHITECTURE.md        # Detailed architecture docs
  RELEASING.md           # Release workflow + auto-update system
  reverse-engineering/   # BLE protocol specs, capture analysis, API findings
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed architecture documentation.

## How It Works

1. **BLE connection** — Scans for nearby NotePin S devices, identifies by serial number (macOS randomises BLE addresses). Performs RSA handshake to establish encrypted ChaCha20-Poly1305 session, then sends APP_KEY handshake to complete pairing.

2. **File sync (BLE)** — Downloads encrypted `.asr` files over BLE (~33 KB/s). Each file contains an RSA-encrypted file key + ChaCha20-XOR'd Opus frames.

3. **File sync (WiFi)** — For faster transfers: disconnects BLE, enables the device's WiFi hotspot, connects the Mac, transfers files over TCP, then restores the original WiFi connection.

4. **Decryption** — RSA-decrypts the per-file key, generates ChaCha20 keystream, XORs each 80-byte Opus frame. Wraps raw frames in OGG container.

5. **Transcription** — Routes to OpenAI Whisper API (if key provided) or local Whisper. FFmpeg is bundled for audio decoding. Background diarization runs in a separate thread.

6. **Summarisation** — Cascade: Anthropic Claude > OpenAI GPT > local DistilBART. Injects user profile context and recording tags into prompts.

7. **Audio editing** — Crop recordings with a waveform editor. Backend uses FFmpeg stream-copy for lossless cropping. Edit transcript text inline or delete segments. Edit summaries as markdown.

## Development

```bash
# Run in dev mode
cd app && pnpm run dev

# Run Python tests
cd .. && python3 -m pytest tests/ -v

# Lint
cd app && pnpm run lint          # eslint + stylelint
cd .. && ruff check app/ src/ tests/   # Python

# Format
cd app && pnpm run format        # prettier + ruff

# Build DMG
cd app && pnpm run build

# Publish a new release (see docs/RELEASING.md)
cd app && pnpm run publish
```

## Reverse Engineering

The BLE protocol, audio format, and cloud API were reverse-engineered from the Android APK and iOS BLE packet captures. Full documentation in [docs/reverse-engineering/](docs/reverse-engineering/).

## License

MIT
