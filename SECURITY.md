# Security

Open Pin (formerly Plaud V2) is a small open-source project maintained in spare time. There is no
bug bounty.

## Reporting a problem

Report security problems privately through GitHub's **Report a vulnerability** button on the
repository's Security tab. If that isn't available, open an issue that says you have a security
problem to report, without the details, and the maintainer will get in touch. Don't paste API
keys, tokens or recordings into any report.

## What the app protects against

The app runs on one user's Mac. The main threats are other websites open in a browser on the same
Mac, other machines on the network, and a tampered update.

| Threat | Defence |
|---|---|
| A web page calling the local backend (`127.0.0.1:5123+`) | A random 32-byte token per launch, required in the `X-Plaud-Token` header on every request except `/health` (`GET /audio` and `POST /device/disconnect` may use `?token=`). CORS headers only for the Tauri origins. Requests with a `Host` other than `127.0.0.1`/`localhost` are refused (DNS rebinding) |
| Path tricks in requests (`../`, absolute paths) | Session IDs are validated and paths must stay inside the recordings folders (`app/utils/paths.py`) |
| Losing recordings to a bug or a bad request | Deletes go to the macOS Trash; re-downloads back up the old files first; incomplete downloads are never saved as recordings |
| Other users on the Mac | Config files are written with mode `0600` |
| Credentials in the process list | The WiFi-sync worker gets its credentials on stdin, not in `argv` |
| A tampered or broken update | Downloaded through the Worker; size and SHA-256 checked; the install script refuses any bundle that isn't `com.plaud.v2` at the expected version, signed by the expected Apple team (the official releases' team unless the build set `APPLE_TEAM_ID`), and accepted by Gatekeeper |
| Personal data in bug reports | The app sends no crash or bug reports. **Export bug report** saves a zip to `~/Downloads` that the user reviews and attaches to an issue themselves. Every file in it is scrubbed first (home folder, user names, serials, BLE IDs, keys, tokens, e-mail addresses, Notion and Drive links); `GET /diagnostics` returns only booleans, counts, the API host and the last four characters of serials. Recordings, transcripts and summaries are never included |

Conversation coaching adds data that is more personal than a transcript, so its boundaries are
stated here:

- **Voice embeddings** (the numbers that describe your voice) are computed on the Mac with the
  WeSpeaker model and stored in `DATA_DIR/coaching/voice_profile.json` (mode `0600`). They are
  never sent anywhere. Removing your voice in Settings deletes the file.
- **Verbatim words** (the transcript with filler words and per-word timing, kept for coaching
  only) are stored in `DATA_DIR/coaching/<session_id>.json` and never leave the Mac. The
  transcript, summary and Notion page stay free of filler words.
- **The coaching AI call** sends the transcript text with timestamps and speaker labels, plus the
  measured metrics, to the same provider you chose for summaries (Anthropic, OpenAI, OpenRouter or
  local Ollama), using the same keys and order. No audio and no voice data is sent. As with
  summaries, Ollama `-cloud` models are never used, because they run on Ollama's servers. With
  Local as your summary provider, coaching text stays on the Mac.
- **Wrong person**: if the app can't tell which speaker is you, it asks and analyses nobody
  until you pick.

Not in scope: malware running as the same user. It can read the config (API keys and device
tokens are stored as plain JSON, not in the Keychain) and the backend's environment, which holds
the token.

During a WiFi sync the worker briefly runs a WebSocket server on all interfaces so the NotePin can
connect from its hotspot; the traffic is encrypted with the Bluetooth session keys.

## Secrets policy

- **No secrets in the repo or the app.** The only GitHub token is the update Worker's `GH_TOKEN`
  secret on Cloudflare (fine-grained: this repo only, Contents read, Issues read/write). Rotate it
  as described in [docs/RELEASING.md](docs/RELEASING.md#rotating-the-workers-github-token).
- **Signing and notarisation credentials** (Developer ID certificate, App Store Connect API key)
  live outside the repo, on the release machine and in a password manager.
- **User secrets** (AI keys, Notion and Google tokens, the Plaud token, device bind tokens) stay in
  the user's data folder.
- `.env`, `keys/`, config files and runtime data are gitignored. Don't commit device serials,
  bind tokens or serial signatures: scripts read them from the app's config.
- Git history has credentials that have since been revoked (old GitHub tokens) and one device's
  serial and bind token. Rewrite history before ever making the repository public.

## Accepted risks

### Embedded Google OAuth client

`app/utils/constants.py` holds the client ID and secret of a Google "desktop app" OAuth client,
used for Google Drive. Google treats desktop client secrets as not confidential: they can't be
hidden in a shipped binary and grant nothing without the user's consent. Anyone could copy them
and start a sign-in that presents itself as "Open Pin", which only matters to people who already
trust that name. If Google revokes the client, create a new one in Google Cloud Console and
update the constants.

### Bundled Plaud RSA keys

`keys/*.pem` (gitignored, bundled into the app) come from Plaud's Android app. Every Plaud client
ships the same keys; the device needs them for the Bluetooth key exchange and the per-file audio
encryption. They are private keys in format only.

### Hugging Face token

Local pyannote diarization reads a Hugging Face token from `.hf_token` in the user's data folder.
It is never bundled with the app; each user supplies their own.
