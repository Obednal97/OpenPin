# Security Notes

This document records security-sensitive design decisions for Plaud V2.

## Embedded Google OAuth client secret

`app/utils/constants.py` contains a `GDRIVE_CLIENT_SECRET` value
constant. This is the OAuth 2.0 client secret for a desktop OAuth
application. Per Google's published OAuth guidelines, desktop client
secrets are not strictly confidential — they cannot be hidden from
end users running the binary, and the secret on its own does not grant
access to user data.

**Accepted risk:** A user reading this repository (or extracting the
constant from a built binary) could initiate OAuth flows that present
themselves as Plaud V2. The user-consent screen would still display
"Plaud V2" as the requesting app, so this enables phishing only against
users who already trust the Plaud V2 name.

**Mitigation:** Per-user OAuth credentials (each install registers its
own OAuth client with Google) would eliminate this surface but are
impractical for the shareable-build use case.

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
