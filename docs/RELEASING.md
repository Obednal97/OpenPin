# Plaud V2 — Release & Auto-Update Guide

## How Auto-Updates Work

The app uses a custom updater that checks GitHub Releases directly (no code signing required). Squirrel.Mac / electron-updater were dropped because they require Apple code signatures.

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

electron-builder generates both automatically.

### How the install works (under the hood)

When the user clicks "Restart to Update":

1. A detached bash script is spawned
2. App quits
3. Script extracts the ZIP with `ditto -xk`
4. Validates extraction (checks `Contents/MacOS` exists)
5. Safe swap: renames old `.app` to `.app.old`, moves new one into place
6. If swap fails, restores the old app automatically
7. Strips macOS quarantine with `xattr -dr com.apple.quarantine`
8. Ad-hoc signs with `codesign --sign -` (preserves Bluetooth/mic permissions)
9. Relaunches the app

---

## Creating a Release

### Prerequisites

- The publish token is stored in `.env` at the project root (gitignored)

### Steps

```bash
# 1. Bump the version in app/package.json
#    Follow semver: major.minor.patch
#    e.g. 1.0.0 → 1.1.0 (new feature) or 1.0.1 (bug fix)

# 2. Build and publish
cd app
GH_TOKEN=$(grep GH_TOKEN ../.env | cut -d= -f2) npx electron-builder --mac --publish always

# 3. The release is created as a draft — publish it:
gh release edit v{version} --repo Obednal97/Plaud-V2 --draft=false --title "v{version}" --notes "Release notes here"
```

Every running instance of the app will pick up the update within 4 hours (or immediately if the user clicks "Check for Updates").

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
4. First launch: right-click the app > Open (bypasses Gatekeeper since it's unsigned)
5. All future updates happen automatically through the app

---

## Auto Bug Reporter

Unhandled errors in both the main process and renderer are automatically reported as GitHub Issues.

- Searches existing issues by error fingerprint to avoid duplicates
- Creates new issues with the `auto-bug` label
- Comments on existing open issues if the same error recurs
- Includes: app version, macOS version, architecture, stack trace, error context
- Never crashes the app — all reporting is fire-and-forget

---

## Architecture

```
main.js
├── Custom updater (no code signing required)
│   ├── checkForUpdates() — GitHub Releases API, called 5s after launch + every 4h
│   ├── downloadFile() — streams ZIP to ~/Library/Application Support/updates/
│   ├── installUpdate() — detached bash: ditto, safe-swap, xattr, codesign, relaunch
│   └── IPC events → renderer: update-status { available | downloading | ready | error }
├── Auto bug reporter
│   ├── reportError() — searches/creates GitHub Issues with auto-bug label
│   ├── process.on('uncaughtException') — main process errors
│   └── IPC 'report-error' — renderer errors forwarded from window.onerror
├── IPC handlers:
│   ├── 'install-update' → installUpdate()
│   ├── 'check-for-updates' → checkForUpdates()
│   ├── 'get-app-version' → app.getVersion()
│   └── 'report-error' → reportError()

preload.js
├── plaud.installUpdate()
├── plaud.checkForUpdates()
├── plaud.getAppVersion()
├── plaud.onUpdateStatus(callback)
└── plaud.reportError(message, stack, context)

ui/utils.js
├── window.onerror → plaud.reportError()
└── window.onunhandledrejection → plaud.reportError()

renderer.js
├── Listens for update-status events via app.updateListeners
├── Shows/hides #update-banner with download progress
└── "Restart to Update" button → plaud.installUpdate()
```

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Update check fails | Token expired or revoked | Regenerate `plaud-v2-updater-readonly` PAT, update in `main.js`, publish new release |
| "Restart to Update" doesn't work | App Management permission on macOS Ventura+ | User may need to approve in System Settings > Privacy > App Management |
| DMG blocked by Gatekeeper | App is unsigned | Right-click > Open on first launch |
| Python server won't start from Dock | PATH doesn't include Homebrew | Fixed in v1.0.5+ — `main.js` adds common Python paths |
| Bluetooth permissions lost after update | TCC invalidation | Fixed in v1.0.9+ — ad-hoc `codesign --sign -` after replacement |
| `npm run publish` fails | Missing GH_TOKEN | Check `.env` file at project root |

## Code Signing (future)

If you ever want to remove the Gatekeeper friction:

1. Get an Apple Developer account ($99/year)
2. Add signing config to `package.json`:
   ```json
   "mac": {
     "identity": "Developer ID Application: Your Name (TEAM_ID)",
     "hardenedRuntime": true,
     "notarize": true
   }
   ```
3. electron-builder handles signing + notarization automatically during build
4. Users can double-click to install without the right-click workaround
5. Could switch back to electron-updater (Squirrel.Mac works with signed apps)
