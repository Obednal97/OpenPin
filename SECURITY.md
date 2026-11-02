# Security

Plaud V2 is a personal project in a private repository, used by its owner and a few friends.

## Reporting a problem

Tell the repository owner (Obednal97 on GitHub) directly, or open an issue in this repository
(it's private). Don't paste API keys, tokens or recordings into the report.

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
| A tampered or broken update | Downloaded through the Worker; size and SHA-256 checked; the install script refuses any bundle that isn't `com.plaud.v2` at the expected version, signed by team 4SN93PWF7Z, and accepted by Gatekeeper |
| Personal data in crash reports | Reports are scrubbed before sending (home folder, serials, BLE IDs, keys, tokens, e-mail addresses, Notion and Drive links) and can be turned off. Recordings, transcripts and summaries are never sent |
| Abuse of the report endpoint | The Worker rate-limits per IP and globally, caps issues and comments per day, and fences submitted text so GitHub renders none of it |

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
and start a sign-in that presents itself as "Plaud V2", which only matters to people who already
trust that name. If Google revokes the client, create a new one in Google Cloud Console and
update the constants.

### Bundled Plaud RSA keys

`keys/*.pem` (gitignored, bundled into the app) come from Plaud's Android app. Every Plaud client
ships the same keys; the device needs them for the Bluetooth key exchange and the per-file audio
encryption. They are private keys in format only.

### Hugging Face token

Local pyannote diarization reads a Hugging Face token from `.hf_token` in the user's data folder.
It is never bundled with the app; each user supplies their own.
