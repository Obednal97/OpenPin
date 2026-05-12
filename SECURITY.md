# Security Notes

This document records security-sensitive design decisions for Plaud V2.

## Embedded Google OAuth client secret

The Google Drive OAuth client ID + secret are loaded at runtime from
`keys/gdrive_oauth.json` (gitignored) and bundled into the DMG via the
`tauri.conf.json` `keys/*` resource. They are NOT in tracked source —
this keeps the secret out of GitHub's secret-scanner crawls of public
repos, which would otherwise trigger Google to auto-revoke the credential.

Per Google's published OAuth guidelines, desktop client secrets are
not strictly confidential — they cannot be hidden from end users running
the binary, and the secret on its own does not grant access to user data.

**Accepted risk:** Anyone with the DMG can extract the credentials and
initiate OAuth flows that present themselves as Plaud V2. The user-consent
screen still displays "Plaud V2" as the requesting app, so this enables
phishing only against users who already trust the Plaud V2 name.

**Mitigation:** Per-user OAuth credentials (each install registers its
own OAuth client with Google) would eliminate this surface but are
impractical for the shareable-build use case.

## Bundled RSA keys for Plaud device E2EE

`keys/app_private.pem` and `keys/e2ee_private.pem` are reverse-engineered
from the official Plaud Android APK. They are the same constants that
ship in every official Plaud client — required for the BLE RSA key
exchange + per-file ChaCha20 decryption of audio recorded by any
Plaud NotePin S device. They are "private keys" in PEM format only;
functionally they are public constants for the Plaud device ecosystem.

The files are bundled into the DMG (via `tauri.conf.json` `keys/*`)
but gitignored, mirroring the pattern used for the GDrive OAuth secret.

## Embedded HuggingFace token

`.hf_token` (gitignored, runtime-loaded) holds the HuggingFace API
token used for diarization model downloads. See `docs/RELEASING.md`
for an open question about whether this token ships in the DMG bundle.
If it ships, every user of the binary uses the developer's HF API
quota — possibly violating HF terms of service.

## Local API surface

The Flask API server binds to `127.0.0.1` (localhost). No authentication
is performed on local endpoints. This is acceptable for the single-user
desktop use case but means any process running on the user's machine
could call the API while the app is running.

## Update channel

Auto-updates are fetched from the configured GitHub repository. A
compromised release repo would deliver malicious updates to all users.
The DMG signing identity (Apple Developer Team) provides one layer
of protection — Gatekeeper will refuse unsigned modifications.
