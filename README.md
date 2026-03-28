# Plaud V2

An open-source desktop companion for the **Plaud NotePin S** voice recorder. Syncs recordings via BLE, decrypts audio locally, transcribes with Whisper, and summarises with AI — no cloud required.

Built as a replacement for the official Plaud app. All processing happens on your Mac unless you explicitly connect cloud AI providers.

## Features

- **BLE sync** — Connect to your NotePin S, download recordings over Bluetooth
- **Local decryption** — Audio is decrypted entirely on-device (RSA + ChaCha20)
- **Transcription** — Local Whisper or OpenAI Whisper API, with custom vocabulary
- **Speaker diarization** — 3-tier cascade: diarize (fast) -> pyannote-onnx -> pyannote standard
- **AI summaries** — Anthropic Claude / OpenAI GPT / local DistilBART fallback chain
- **Plaud Cloud sync** — Two-way sync with your existing Plaud account
- **Notion integration** — Push transcripts and summaries to a Notion database via MCP
- **Webhooks** — Trigger external services on transcribe/summarise/sync events
- **Apple-style UI** — Glass design, light/dark/auto theme, squircle corners

## Architecture

```
app/
  api_server.py          # Flask API (51 lines — app factory)
  main.js                # Electron main process
  renderer.js            # Frontend orchestrator
  index.html             # UI markup
  styles.css             # @import hub for CSS modules
  ui/                    # Frontend modules (utils, recordings, transcript, settings)
  routes/                # Flask blueprints (core, device, cloud, notion, recordings, transcribe, settings)
  state/                 # State managers (device, cloud, notion)
  providers/             # AI providers (summarize, transcribe, validate)
  utils/                 # Shared utilities (config, constants, audio, webhooks, system)
  css/                   # Design system (tokens, components, layouts, recordings, transcript, settings, utilities)
src/
  transcribe.py          # Whisper transcription + diarization cascade
  crypto.py              # RSA key management
  scanner.py             # BLE manufacturer data parser
tests/                   # 58 tests (pytest)
docs/
  reverse-engineering/   # BLE protocol specs, capture analysis, API findings
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed architecture documentation.

## Quick Start

### Prerequisites

- macOS (BLE + CoreWLAN)
- Python 3.12+
- Node.js 18+
- [ffmpeg](https://formulae.brew.sh/formula/ffmpeg) (optional, for MP3 export)

### Install & Run

```bash
git clone https://github.com/Obednal97/Plaud-V2.git
cd Plaud-V2/app
npm install
npm start
```

The Electron app launches and starts the Python API server automatically. On first run, it will install Python dependencies (Flask, Whisper, etc.).

### API Keys (Optional)

For cloud AI features, add API keys in **Settings > Processing > Cloud AI**:

- **OpenAI** — Whisper API for faster transcription + GPT for summaries
- **Anthropic** — Claude for higher-quality summaries

Without API keys, everything runs locally using Whisper `small` and DistilBART.

## Development

```bash
# Run the app
cd app && npm start

# Run Python tests
cd .. && python3 -m pytest tests/ -v

# Lint
cd app && npm run lint          # eslint + stylelint
cd .. && ruff check app/ src/ tests/   # Python

# Format
cd app && npm run format        # prettier + ruff
```

## How It Works

1. **BLE connection** — Scans for nearby NotePin S devices, identifies by serial number (macOS randomises BLE addresses). Performs RSA handshake to establish encrypted ChaCha20-Poly1305 session.

2. **File sync** — Downloads encrypted `.asr` files over BLE (~33 KB/s). Each file contains an RSA-encrypted file key + ChaCha20-XOR'd Opus frames.

3. **Decryption** — RSA-decrypts the per-file key, generates ChaCha20 keystream, XORs each 80-byte Opus frame. Wraps raw frames in OGG container.

4. **Transcription** — Routes to OpenAI Whisper API (if key provided) or local Whisper. Background diarization runs in a separate thread.

5. **Summarisation** — Cascade: Anthropic Claude > OpenAI GPT > local DistilBART. Injects user profile context and recording tags into prompts.

## Reverse Engineering

The BLE protocol, audio format, and cloud API were reverse-engineered from the Android APK and iOS BLE packet captures. Full documentation in [docs/reverse-engineering/](docs/reverse-engineering/).

## License

MIT
