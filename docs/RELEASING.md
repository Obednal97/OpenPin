# Plaud V2 — Release & Auto-Update Guide

## How Auto-Updates Work

The app uses a custom updater that checks GitHub Releases directly via a read-only PAT.

### Update flow (from the user's perspective)

1. App launches, waits 5 seconds, then checks GitHub Releases API for a newer version
2. If found, downloads the ZIP silently in the background
3. A blue banner appears at the top: **"v1.2.0 ready to install" [Restart to Update] [x]**
4. User clicks "Restart to Update" — app quits, replaces itself, and reopens
5. App re-checks every 4 hours while running
6. Users can manually check via **Settings > General > About > Check for Updates**

### What gets published to GitHub Releases

| File | Purpose |
|------|---------|
| `Plaud-V2-{version}.dmg` | For first-time installs (drag to Applications) |
| `Plaud-V2-{version}-mac.zip` | For auto-updates (downloaded by the custom updater) |
| `latest-mac.yml` | Version metadata for update checks |
| `*.blockmap` | Delta update block maps |

electron-builder generates all of these automatically.

### How the install works (under the hood)

When the user clicks "Restart to Update":

1. A detached bash script is spawned
2. App quits
3. Script extracts the ZIP with `ditto -xk`
4. Validates extraction (checks `Contents/MacOS` exists)
5. Safe swap: renames old `.app` to `.app.old`, moves new one into place
6. If swap fails, restores the old app automatically
7. Strips macOS quarantine with `xattr -dr com.apple.quarantine`
8. Relaunches the app

Since the app is signed with a Developer ID certificate, macOS trusts it across updates and preserves Bluetooth/location permissions.

---

## Creating a Release

### Prerequisites

- `.env` at project root contains `GH_TOKEN`, `APPLE_TEAM_ID`, `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`
- FFmpeg binary in `bin/` (auto-downloaded by the prebuild script)
- Apple Developer ID Application certificate installed in Keychain

### Steps

```bash
# 1. Bump the version in app/package.json
#    Follow semver: major.minor.patch

# 2. Commit the version bump
git add app/package.json
git commit -m "Bump v{version}"
git push

# 3. Build, sign, notarize, and publish
cd app
pnpm run publish
```

The `publish` script:
1. Runs `scripts/download-ffmpeg.sh` (downloads FFmpeg if not present)
2. Loads env vars from `.env` (GH_TOKEN + Apple credentials)
3. Packages the Electron app with ASAR (Python files unpacked)
4. Code signs with Developer ID certificate + hardened runtime
5. Submits to Apple for notarization (typically 2-5 minutes)
6. Builds DMG + ZIP
7. Uploads to GitHub Releases

Every running instance of the app will pick up the update within 4 hours (or immediately if the user clicks "Check for Updates").

---

## Code Signing & Notarization

The app is signed with a Developer ID Application certificate and notarized with Apple.

### What this means for users

- **Double-click to install** — no Gatekeeper warnings, no right-click > Open needed
- **Permissions persist** — Bluetooth, location, and other permissions survive across updates because the signing identity is consistent
- **Trusted by macOS** — Apple has scanned and approved the binary

### Configuration (in `app/package.json`)

```json
"mac": {
  "identity": "<Developer Name> (<Team ID>)",
  "hardenedRuntime": true,
  "entitlements": "entitlements.mac.plist",
  "entitlementsInherit": "entitlements.mac.plist",
  "notarize": true
}
```

### Entitlements (`entitlements.mac.plist`)

| Entitlement | Reason |
|-------------|--------|
| `cs.allow-jit` | Electron requires JIT compilation |
| `cs.allow-unsigned-executable-memory` | Electron/Chromium requirement |
| `cs.allow-dyld-environment-variables` | Electron requirement |
| `device.bluetooth` | BLE communication with Plaud device |
| `network.client` | HTTP API calls, cloud sync |
| `files.user-selected.read-write` | Audio export to Downloads |

### Apple credentials (in `.env`)

```
APPLE_TEAM_ID=<your-team-id>
APPLE_ID=<your-apple-id-email>
APPLE_APP_SPECIFIC_PASSWORD=<app-specific-password>
```

Generate an app-specific password at [appleid.apple.com](https://appleid.apple.com) > Sign-In and Security > App-Specific Passwords.

### If notarization is slow or failing

- First submission to a new Apple Developer account can take hours
- Subsequent submissions typically take 2-5 minutes
- If Apple's timestamp servers are down, codesign will fail — retry later
- To publish without notarization temporarily: set `"notarize": false` in package.json, publish, then restore

---

## GitHub Tokens

The repo is **private**, so two fine-grained PATs are used:

### Token 1 — `plaud-v2-publish` (your machine only)

- **Scope:** `Obednal97/Plaud-V2` — Contents: Read and write
- **Used for:** creating releases and uploading build artifacts
- **Lives:** in `.env` at project root (gitignored). Never in code.

### Token 2 — `plaud-v2-updater-readonly` (ships with the app)

- **Scope:** `Obednal97/Plaud-V2` — Contents: Read-only, Issues: Read and write
- **Used for:** auto-update checks from private repo + auto-bug-reporting to GitHub Issues
- **Lives:** hardcoded in `main.js` (`UPDATE_TOKEN` constant)
- **Risk:** minimal — read-only for contents, and anyone with the app is a trusted friend

### If a token is compromised

1. Go to GitHub > Settings > Developer settings > Fine-grained tokens
2. Revoke the compromised token
3. Generate a new one with the same permissions
4. For the publish token: update `.env`
5. For the updater token: update `UPDATE_TOKEN` in `main.js` and publish a new release

---

## First-Time Install for Friends

1. Go to the [latest GitHub Release](https://github.com/Obednal97/Plaud-V2/releases/latest)
2. Download `Plaud-V2-{version}.dmg`
3. Open the DMG, drag Plaud V2 to Applications
4. Double-click to open — it just works (signed + notarized)
5. Grant Bluetooth permission when prompted (one time only)
6. All future updates happen automatically through the app

---

## Auto Bug Reporter

Unhandled errors in both the main process and renderer are automatically reported as GitHub Issues.

- Searches existing issues by error fingerprint to avoid duplicates
- Creates new issues with the `auto-bug` label
- Comments on existing open issues if the same error recurs
- Includes: app version, macOS version, architecture, Python environment, stack trace, error context
- Never crashes the app — all reporting is fire-and-forget

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Update check fails | Token expired or revoked | Regenerate `plaud-v2-updater-readonly` PAT, update in `main.js`, publish new release |
| "Restart to Update" doesn't work | App Management permission on macOS Ventura+ | User may need to approve in System Settings > Privacy > App Management |
| Notarization fails | Apple timestamp server down or credentials wrong | Retry later, or check APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD in `.env` |
| Codesign fails repeatedly | Apple timestamp server rate limiting | Wait a few minutes and retry |
| Python server won't start from Dock | PATH doesn't include Homebrew | Fixed — `main.js` adds common Python paths + bundled bin dir |
| `pnpm run publish` fails | Missing env vars | Check `.env` file at project root has GH_TOKEN + Apple credentials |
| FFmpeg not found in build | `bin/` is gitignored | Run `scripts/download-ffmpeg.sh` (or just `pnpm run build` which runs it automatically) |
