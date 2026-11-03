<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="app/assets/wordmark-white.png">
    <img src="app/assets/wordmark-black.png" alt="Open Pin" height="40">
  </picture>
</p>

<p align="center">
A macOS app for the <b>Plaud NotePin S</b> voice recorder.<br>
It syncs recordings over Bluetooth or Wi-Fi, decrypts them on your Mac, and transcribes and summarises them.
</p>

Open Pin is an alternative to the official Plaud app. It downloads recordings straight from the
device and decrypts them on your Mac, so recordings don't have to go through Plaud's cloud.
Transcription and summaries run on your Mac, or with the AI providers whose API keys you add.

> **Open Pin is an independent project. It isn't made by, affiliated with or endorsed by PLAUD.
> Plaud and NotePin are trademarks of their owners. Use it with your own NotePin and your own
> Plaud account, at your own risk.**

## Supported devices

| Device | Status |
|---|---|
| Plaud NotePin S | Tested |
| Other Plaud recorders (NotePin, Note, Note Pro…) | Not tested. They may share the protocol, but nobody has checked |

Open Pin needs a Mac with **Apple Silicon** (M1 or later) and **macOS 12** or later. Intel Macs
aren't supported.

## Features

- **Bluetooth sync**: finds your NotePin, pairs once and downloads new recordings. Incomplete
  downloads are kept and retried, never saved as broken audio.
- **Wi-Fi sync**: for large files the Mac joins the device's own hotspot for a much faster
  transfer, then reconnects to your usual Wi-Fi.
- **Local decryption**: audio is decrypted on the Mac and saved as Ogg/Opus.
- **Transcription**: Deepgram, Groq Whisper, OpenAI, or Whisper running on your Mac. Custom
  vocabulary and speaker labels.
- **Summaries with titles**: Claude, OpenAI, OpenRouter, a local Ollama model, or a small local
  model as a last resort.
- **Editing**: crop audio, fix transcript lines, edit summaries as Markdown, tag, rename and file
  recordings into folders.
- **Integrations**: Notion (a page per recording, your own notes on the page are kept), Google
  Drive uploads, webhooks.
- **Automation**: auto-sync, auto-transcribe, auto-summarise, open at login, menu-bar icon.
- **Updates**: checks for new versions and installs them on restart. Every update is checked
  (code signature, team ID, Gatekeeper, checksum) before it replaces the app.
- **Plaud Cloud sync** (optional): download recordings from, or upload them to, your Plaud account.

## Install

1. Download the DMG (`Open.Pin_<version>_aarch64.dmg`) from the
   [latest release](https://github.com/Obednal97/OpenPin/releases/latest).
2. Open it and drag **Open Pin** to **Applications**. Run it from Applications: run from the DMG
   or Downloads, it can't update itself.
3. Open it. It's signed and notarised, so macOS opens it without warnings. Allow Bluetooth when
   asked.

On first launch the app downloads a pinned Python 3.12 build, sets up its own environment and
starts its local backend. This takes a minute or two the first time; you don't need to install
Python yourself. Some features download more the first time you use them (FFmpeg, local Whisper
and its model, the speaker-labelling package).

**Pairing** needs a Plaud account once: the NotePin only connects with a device token and serial
signature that Plaud's server issues. The setup screen signs you in, then scans for the device.
Keep the NotePin nearby and switched on, and disconnect it from the Plaud phone app first. After
pairing, Open Pin doesn't need your Plaud account.

**Updating from Plaud V2:** Open Pin used to be called Plaud V2. Updating keeps your recordings,
settings, pairing, Bluetooth permission and "Open at login". On its first launch after the update
the app renames itself from `Plaud V2.app` to `Open Pin.app` in the same folder (if it can't, the
next update does it). If you renamed the app yourself, it keeps your name.

## Build from source

You need an Apple Silicon Mac, Rust 1.90 or later, Node, [uv](https://docs.astral.sh/uv/), and
the four RSA key files from Plaud's Android app in `keys/`. They aren't in this repository; see
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#keys).

```bash
bash scripts/prepare-frontend.sh
npx --yes @tauri-apps/cli@2.12 dev        # run a debug build (its own data folder)
```

A debug build keeps its data apart from an installed copy. To build a signed DMG of your own, see
[docs/RELEASING.md](docs/RELEASING.md): the signing identity and team ID come from environment
variables.

## Privacy

**Stays on your Mac:** recordings, transcripts, summaries, settings and API keys, in
`~/Library/Application Support/Plaud V2/` (the folder keeps the app's old name so updates don't
move your data). The config file is readable only by you. Deleted recordings go to the Trash.

**Pairing backup:** the two keys your NotePin needs (its serial signature and bind token) are also
kept in your login Keychain, so the pairing survives a cleaner deleting the folder above.
**Erase & Disconnect** removes them.

**Leaves your Mac only if you set it up:**

- Audio goes to the transcription provider you choose (Deepgram, Groq or OpenAI).
- Transcript text goes to the summary provider you choose (Anthropic, OpenAI or OpenRouter).
- Notion, Google Drive and your webhooks get the recordings or text you send them.
- Plaud's API is used to pair a device, and for Plaud Cloud sync if you turn it on.

**Always:**

- Update checks go to the update server (a Cloudflare Worker that fronts GitHub Releases). It sees
  your IP address and app version.
- First-launch and on-demand downloads come from GitHub (Python), PyPI (packages), evermeet.cx
  (FFmpeg) and Hugging Face / OpenAI (models). The first time local speaker labelling runs, the
  `diarize` library downloads its voice model from WeSpeaker's own server. No audio is sent.

Open Pin has no analytics and sends no crash reports.

## Reporting bugs

Click the bug icon in the toolbar (or **Settings › General › About › Report a bug…**) and choose
**Export bug report**. It saves a zip to your Downloads folder with what you wrote, app and Mac
details, a few checks (signed in or not, which keys are set, the last NotePin handshake) and
recent logs. Your home folder, user name, device serial numbers, Bluetooth IDs, API keys, tokens,
e-mail addresses and Notion and Google Drive links are removed, and recordings, transcripts and
summaries are never included. Look through the zip, then attach it to a
[new issue](https://github.com/Obednal97/OpenPin/issues/new?template=bug_report.yml).

Logs, if you'd rather look yourself:

| Log | What's in it |
|---|---|
| `~/Library/Logs/Plaud V2/app.log` | The app and its backend (startup, Bluetooth, sync, errors) |
| `~/Library/Logs/Plaud V2/update.log` | Every step of the last update install |
| `~/Library/Application Support/com.plaud.v2/diagnostics.log` | Errors the app caught |

Common problems:

- **Stuck on the startup screen.** Click **Retry**. If it keeps failing, quit, delete
  `~/Library/Application Support/com.plaud.v2/venv` and open the app again.
- **Device not found.** Check the NotePin is on, nearby and not connected to the Plaud phone app,
  and that Bluetooth is allowed in **System Settings › Privacy & Security › Bluetooth**.
- **"Open Pin is running from a temporary location".** Move the app to Applications and open it
  from there.
- **Update won't install.** Allow Open Pin in **System Settings › Privacy & Security › App
  Management**, then check `update.log`.

## Contributing

Issues and pull requests are welcome. For anything bigger than a fix, open an issue first so we
can agree on the approach. Before sending a pull request, run the checks in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) (pytest, ruff, `cargo test` and the Worker's tests).
Never commit device serials, tokens, API keys or the RSA keys.

Some internal names still say Plaud V2: the bundle ID `com.plaud.v2`, the data and log folders,
the crate and package name `plaud-v2`, config keys and the Keychain service. They're kept on
purpose so existing installs keep their data; see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#names).

Documentation: [docs/README.md](docs/README.md) (architecture, development, releasing,
reverse-engineering notes).

## Licence

MIT. See [LICENSE](LICENSE).
