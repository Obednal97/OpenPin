<p align="center">
  <img src="app/assets/wordmark-black.png" alt="Plaud V2" height="40">
</p>

<p align="center">
A macOS companion app for the <b>Plaud NotePin S</b> voice recorder.<br>
Syncs recordings over Bluetooth or WiFi, decrypts them on your Mac, transcribes and summarises them.
</p>

Plaud V2 replaces the official Plaud app. Recordings are downloaded straight from the device and
decrypted locally. Nothing goes to Plaud's cloud. Transcription and summaries run on your Mac, or
with cloud AI providers if you add their API keys.

## Features

- **Bluetooth sync**: finds your NotePin S, pairs once, downloads new recordings. Incomplete
  downloads are kept and retried, never saved as broken audio.
- **WiFi sync**: for large files the Mac joins the device's own hotspot for a much faster
  transfer, then puts your normal WiFi back.
- **Local decryption**: audio is decrypted on the Mac (RSA file key + ChaCha20) and saved as Ogg/Opus.
- **Transcription**: Deepgram Nova-3, Groq Whisper, OpenAI, or local Whisper (`base` to `large`,
  including `turbo`). Custom vocabulary and your profile improve accuracy.
- **Speaker labels**: Deepgram labels speakers as it transcribes. With any other provider, a speaker
  model runs on your Mac afterwards (the `diarize` package, installed on first use, no account
  needed). If it can't run, the transcript is saved without labels and the app says why.
- **Summaries with titles**: Claude, OpenAI, OpenRouter, a local Ollama model, or DistilBART as a
  last resort. If one provider fails the next one is tried.
- **Editing**: crop audio with a waveform editor, edit transcript lines, edit summaries as Markdown,
  tag, rename, move recordings to another folder.
- **Notion**: pushes each recording's title, tags, summary and transcript to a Notion database.
  Edits you make in Notion outside the app's own section are left alone.
- **Google Drive**: uploads recordings to a folder you choose.
- **Webhooks**: calls your URLs when a recording is synced, transcribed or summarised.
- **Automation**: auto-sync, auto-transcribe, auto-summarise, open at login, menu-bar icon.
- **Updates**: the app checks for new versions, downloads them in the background and installs on
  restart. Every update is checked (signature, team ID, Gatekeeper, checksum) before it replaces the app.
- **Plaud Cloud sync** (optional): download recordings from, or upload them to, a Plaud account.

## Requirements

- A Mac with **Apple Silicon** (M1 or later). Intel Macs aren't supported.
- **macOS 12** or later.
- A Plaud NotePin S.
- A **Plaud account, once**, to pair the device. Pairing needs a device token and serial signature
  that only Plaud's API issues. After that the app works without it.
- An internet connection on first launch (see below).

You **don't** need to install Python. The app downloads and manages its own.

## Install

The GitHub repository is **private**, so the
[Releases page](https://github.com/Obednal97/Plaud-V2/releases/latest) only works for people with
access to the repo. Everyone else can download the DMG through the update server:

```bash
# Prints the download link of the latest release
curl -s https://plaud-v2-updates.leave-hacker-feedback.workers.dev/v1/latest | jq -r .download_url
```

The link has the form
`https://plaud-v2-updates.leave-hacker-feedback.workers.dev/v1/download/<tag>/<asset>`, for example
`.../v1/download/v2.4.0/Plaud.V2_2.4.0_aarch64.dmg`. There is no `/latest` download shortcut.

Then:

1. Open the DMG and drag **Plaud V2** to **Applications**. Run it from Applications: run from the
   DMG or Downloads, it can't update itself.
2. Open it. It is signed and notarised, so macOS opens it without warnings.
3. Allow Bluetooth when asked.

### First launch

The window opens straight away and shows progress while the app sets itself up:

1. Downloads a pinned CPython 3.12 build and checks its SHA-256. If that fails, it uses a Homebrew
   or python.org Python 3.12 if you have one.
2. Creates its own virtual environment and installs the core packages from a hash-pinned lockfile.
3. Starts the local backend.

This takes a minute or two the first time and a few seconds after that. If it fails, the splash
screen shows what went wrong, a **Retry** button and a link to the diagnostics log.

Some features download more on first use: FFmpeg (for audio editing and local transcription),
local Whisper and its model, the diarization package, and the DistilBART summariser.

### Pairing

The setup screen asks you to sign in to your Plaud account, then scans for the device. Keep the
NotePin nearby, charged and switched on, and disconnect it from the Plaud phone app first. Pick it
from the list and the app pairs with it.

### Updates

The app checks for updates 5 seconds after launch and every 4 hours. When one is ready, a banner
offers **Restart to Update**. You can also check in **Settings › General › About**.

## Optional setup

### AI providers

Add API keys in **Settings › Processing**. Without keys everything runs locally (slower).

| Provider | Used for | Get a key |
|---|---|---|
| Deepgram | Transcription with speaker labels (default) | [console.deepgram.com](https://console.deepgram.com/) |
| Groq | Transcription (Whisper large-v3) | [console.groq.com](https://console.groq.com/keys) |
| OpenAI | Transcription and summaries | [platform.openai.com](https://platform.openai.com/api-keys) |
| Anthropic | Summaries (Claude) | [console.anthropic.com](https://console.anthropic.com/) |
| OpenRouter | Summaries, if you have no Anthropic or OpenAI key | [openrouter.ai](https://openrouter.ai/keys) |

For local summaries with a better model than DistilBART, install [Ollama](https://ollama.com) and
pull a model (for example `ollama pull qwen3`). The app uses it when it's running; it never pulls
models itself.

### Notion

**Settings › Integrations › Notion**: click **Connect Notion**, approve in the browser, then pick a
database (or paste its URL). Turn on **Auto-push** to send every recording after it's summarised.
The app adds a `Session ID` column (and a `Tags` column if you use tags) and writes its content
between marked headings, so your own notes on the page are kept.

### Google Drive

**Settings › Integrations › Google Drive**: connect, then choose or create a folder.

### Webhooks

**Settings › Integrations › Webhooks**: add a URL and choose the events (recording synced,
transcribed, summary generated). The app POSTs JSON: `{event, timestamp, data}`.

### Plaud Cloud (optional)

**Settings › Plaud Cloud**: you're already signed in if you paired a device. Choose **No sync**,
**Download only** or **Two-way**. You don't need Plaud Cloud for anything after pairing.

## Privacy

**Stays on your Mac:** recordings, transcripts, summaries, settings and API keys (the config file
is readable only by you), in `~/Library/Application Support/Plaud V2/`. Deleted recordings go to
the macOS Trash.

**Leaves your Mac only if you set it up:**

- Audio goes to the transcription provider you choose (Deepgram, Groq or OpenAI).
- Transcript text goes to the summary provider you choose (Anthropic, OpenAI or OpenRouter).
- Notion, Google Drive and webhooks get what's described above.
- Plaud's API is used to pair a device, and for Plaud Cloud sync if you turn it on.

**Always:**

- Update checks go to the update server (`plaud-v2-updates`, a Cloudflare Worker). It sees your
  IP address and app version.
- First-launch and on-demand downloads come from GitHub (Python), PyPI (packages), evermeet.cx
  (FFmpeg) and Hugging Face / OpenAI (models).

**Crash reports** are on by default and can be turned off in **Settings › General › System ›
Send anonymous crash reports**. Before a report is sent, the app removes your user name and home
folder, device serial numbers, Bluetooth IDs, API keys and tokens, e-mail addresses, and Notion and
Google Drive links. Recording audio, transcripts and summaries are never included. Reports become
issues in the private GitHub repo. **Report a bug** (the bug icon in the toolbar) sends a report
you write yourself, even with crash reports off.

## Troubleshooting

| Log | What's in it |
|---|---|
| `~/Library/Logs/Plaud V2/app.log` | The app and its Python backend (startup, BLE, sync, errors). Rotates at 1 MB to `app.log.1` |
| `~/Library/Logs/Plaud V2/update.log` | Every step of the last update install, and why it stopped if it did |
| `~/Library/Application Support/com.plaud.v2/diagnostics.log` | Errors the app caught, including ones it didn't report. Open it from the splash screen's **Open diagnostics log** |
| `~/Library/Application Support/Plaud V2/wifi_sync.log` | WiFi syncs (appended, not rotated) |

Common problems:

- **Stuck on the startup screen.** Click **Retry**. If it keeps failing, quit, delete
  `~/Library/Application Support/com.plaud.v2/venv` and open the app again: it rebuilds it.
- **Device not found.** Check the NotePin is on, nearby and not connected to the Plaud phone app,
  and that Bluetooth is allowed in **System Settings › Privacy & Security › Bluetooth**.
- **WiFi didn't come back after a WiFi sync.** The app restores your networks itself, also on the
  next launch if it was interrupted. If a network can't be restored, the app tells you which one;
  rejoin it from the WiFi menu.
- **"Plaud V2 is running from a temporary location".** Move the app to Applications and open it from there.
- **Update won't install.** Allow Plaud V2 in **System Settings › Privacy & Security › App
  Management**, then check `update.log`.

## Development

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) to run from source, test and lint, and
[docs/README.md](docs/README.md) for the rest of the documentation (architecture, releasing,
reverse-engineering notes).

## Licence

MIT. See [LICENSE](LICENSE).
