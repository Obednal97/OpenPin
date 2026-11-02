# Plaud V2 — Release & Auto-Update Guide

## How Auto-Updates Work

The Rust shell (`src-tauri/src/commands/mod.rs`, "Auto-Updater") never talks to GitHub. It talks to
the **`plaud-v2-updates` Cloudflare Worker** (`infra/update-worker/`,
`https://plaud-v2-updates.leave-hacker-feedback.workers.dev`, the `WORKER_BASE` constant). The
Worker holds the GitHub token as a secret, so **the app ships no token** and the token can be
rotated without a release.

### Update flow (from the user's perspective)

1. App launches, waits 5 seconds, then asks the Worker (`GET /v1/latest`) for the latest published release.
2. If it's newer, the DMG is downloaded silently in the background.
3. A blue banner appears at the top: **"v1.2.0 ready to install" [Restart to Update] [x]**.
4. User clicks "Restart to Update". The app quits, the new `.app` is verified and copied into place, and the app reopens.
5. App re-checks every 4 hours while running.
6. Users can check manually via **Settings › General › About › Check for Updates**.

### What the updater checks

- **Version:** `major.minor.patch` compared numerically. Anything after `-` or `+` is ignored, so
  `2.4.0-beta` is never treated as newer than `2.4.0`. Drafts and pre-releases are never offered
  (GitHub's `releases/latest`).
- **Server errors** (any non-2xx from the Worker) show as an update error, never as "up to date".
- **Download:** `GET /v1/download/<tag>/<asset>` answers 302 to GitHub's short-lived signed URL.
  The DMG is written to `~/Library/Application Support/com.plaud.v2/updates/<name>.part` (15 s
  connect timeout, 60 s idle timeout, no overall limit), checked against the asset's **size** and,
  if the release has a `<dmg>.sha256` asset, its **SHA-256**, then renamed into place.
- **Install** (detached `bash` helper, log in `~/Library/Logs/Plaud V2/update.log`):
  1. Waits for the old app's PID to exit (up to 30 s).
  2. Mounts the DMG on a private temp mount point (`hdiutil attach -mountpoint`).
  3. Refuses the update unless the bundle has `CFBundleIdentifier` `com.plaud.v2`, the expected
     `CFBundleShortVersionString`, passes `codesign --verify --deep --strict`, is signed by team
     **4SN93PWF7Z**, and passes Gatekeeper (`spctl -a -t exec`).
  4. Moves the old app aside (aborts if that fails), copies the new one with `ditto`, re-verifies,
     then deletes the old copy. On any failure it restores the old app.
  5. Strips quarantine and relaunches. On abort it relaunches the old app.
- **App Translocation:** if the app is running from a randomised `/AppTranslocation/` path (opened
  straight from Downloads or the DMG), the updater refuses and asks the user to move Plaud V2 to
  Applications.

Because every build is signed with the same Developer ID, macOS keeps Bluetooth and other
permissions across updates.

### What gets published to GitHub Releases

| File (as GitHub names it) | Purpose |
|------|---------|
| `Plaud.V2_{version}_aarch64.dmg` | First-time installs (drag to Applications) and the auto-update payload |
| `Plaud.V2_{version}_aarch64.dmg.sha256` | `shasum -a 256` of the DMG. The Worker serves it as `asset.sha256` and the updater verifies the download against it |

GitHub replaces spaces in uploaded file names with dots (`Plaud V2_…` becomes `Plaud.V2_…`). The
Worker copes with either name inside the `.sha256` file. Releases without a `.sha256` still
update, but only the size is checked.

---

## Creating a Release

### Prerequisites (this Mac, "ai-mini")

- Rust via rustup: `export PATH="/opt/homebrew/opt/rustup/bin:$PATH"`.
- Signing identity **Developer ID Application: Oliver Bednal (4SN93PWF7Z)** in
  `~/Library/Keychains/signing.keychain-db`. Unlock it first:
  `security unlock-keychain -p "$(cat ~/.secrets/signing-keychain.password)" ~/Library/Keychains/signing.keychain-db`
- Notarisation uses the App Store Connect **API key** (team 4SN93PWF7Z), not an Apple ID password.
  IDs are in `~/.appstoreconnect/iqralock.env` (`ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_PATH`). The
  `.p8` lives only in 1Password and is mounted on a RAM disk: run `apple-keys-mount` if the key
  is missing.
- `gh` signed in as Obednal97 (for `gh release create`).

### Headless release recipe

```bash
cd ~/projects/plaud-v2
export PATH="/opt/homebrew/opt/rustup/bin:$PATH"
V=2.4.0   # the new version

# 1. Bump the version in ALL THREE manifests (they must match):
#    src-tauri/tauri.conf.json  "version": "$V"
#    src-tauri/Cargo.toml       version = "$V"   (then `cd src-tauri && cargo check` to refresh Cargo.lock)
#    pyproject.toml             version = "$V"
git add src-tauri/tauri.conf.json src-tauri/Cargo.toml src-tauri/Cargo.lock pyproject.toml
git commit -m "Bump v$V"
git tag "v$V" && git push && git push --tags

# 2. Signing + notarisation environment
security unlock-keychain -p "$(cat ~/.secrets/signing-keychain.password)" ~/Library/Keychains/signing.keychain-db
apple-keys-mount
set -a; . ~/.appstoreconnect/iqralock.env; set +a
export APPLE_SIGNING_IDENTITY="Developer ID Application: Oliver Bednal (4SN93PWF7Z)"
export APPLE_API_KEY="$ASC_KEY_ID" APPLE_API_ISSUER="$ASC_ISSUER_ID" APPLE_API_KEY_PATH="$ASC_KEY_PATH"

# 3. Build, sign and notarise the .app, then build the DMG.
#    Use the @tauri-apps/cli version that matches the `tauri` crate in src-tauri/Cargo.lock
#    (`grep -A1 '^name = "tauri"$' src-tauri/Cargo.lock`), e.g. 2.10.x for tauri 2.10.3.
npx --yes @tauri-apps/cli@2.10 build
```

**If `bundle_dmg.sh` fails** (on a headless Mac the Finder AppleScript that lays out the DMG
window can't run), the `.app` is already built, signed and notarised in
`src-tauri/target/release/bundle/macos/`. Re-run the bundler script with `--skip-jenkins`, which
skips the Finder step:

```bash
cd src-tauri/target/release/bundle/dmg
# Add --skip-jenkins in front of the exact arguments the failed build passed
# (`npx --yes @tauri-apps/cli@2.10 build --verbose` prints the bundle_dmg.sh command line):
./bundle_dmg.sh --skip-jenkins <same arguments as the failed run>
cd -
```

Then sign, notarise and staple **the DMG** itself:

```bash
DMG="src-tauri/target/release/bundle/dmg/Plaud V2_${V}_aarch64.dmg"
codesign --force --timestamp --sign "$APPLE_SIGNING_IDENTITY" "$DMG"
xcrun notarytool submit "$DMG" --key "$APPLE_API_KEY_PATH" --key-id "$APPLE_API_KEY" \
  --issuer "$APPLE_API_ISSUER" --wait
xcrun stapler staple "$DMG"
spctl -a -t open --context context:primary-signature -vv "$DMG"   # expect: accepted, Notarized Developer ID
```

### Publish

```bash
# Name the files the way GitHub will, then write the checksum next to the DMG.
OUT=/tmp/plaud-release-$V && mkdir -p "$OUT"
cp "$DMG" "$OUT/Plaud.V2_${V}_aarch64.dmg"
( cd "$OUT" && shasum -a 256 "Plaud.V2_${V}_aarch64.dmg" > "Plaud.V2_${V}_aarch64.dmg.sha256" )

gh release create "v$V" \
  "$OUT/Plaud.V2_${V}_aarch64.dmg" \
  "$OUT/Plaud.V2_${V}_aarch64.dmg.sha256" \
  --title "v$V" --notes-file release-notes.md

# Check what the app will see (the Worker caches /v1/latest for up to 5 minutes):
curl -s https://plaud-v2-updates.leave-hacker-feedback.workers.dev/v1/latest | jq '{version, asset}'
```

Running copies pick the update up within 4 hours (or straight away via **Check for Updates**).

---

## Code Signing & Notarization

The app is signed with a Developer ID Application certificate and notarised by Apple.

### What this means for users

- **Double-click to install.** No Gatekeeper warnings, no right-click › Open.
- **Permissions persist.** Bluetooth and other permissions survive updates because the signing identity stays the same.
- **Trusted by macOS.** Apple has scanned and approved the binary.

### Configuration

- `src-tauri/tauri.conf.json` → `bundle.macOS` (signing identity, provider short name)
- `src-tauri/Entitlements.plist` (entitlements)
- `src-tauri/Info.plist` (bundle metadata)

### Entitlements (`src-tauri/Entitlements.plist`)

| Entitlement | Reason |
|-------------|--------|
| `device.bluetooth` | BLE communication with Plaud device |
| `network.client` | HTTP API calls, cloud sync |
| `files.user-selected.read-write` | Audio export to Downloads |

(Tauri does not need JIT or unsigned-memory entitlements.)

### If notarisation is slow or failing

- Subsequent submissions typically take 2–5 minutes.
- `xcrun notarytool log <submission-id> --key … --key-id … --issuer …` shows why a submission was rejected.
- If Apple's timestamp servers are down, codesign fails: retry later.

---

## GitHub Tokens

The repo is **private**. No token ships in the app.

### Publish token (this Mac only)

- `gh` CLI, signed in as Obednal97. Used for `gh release create`.

### `GH_TOKEN` Worker secret (server-side only)

- **What:** fine-grained PAT scoped to **Obednal97/Plaud-V2** only: **Contents: Read-only**,
  **Issues: Read and write** (Metadata: Read-only is automatic).
- **Used by:** the `plaud-v2-updates` Worker for `/v1/latest`, `/v1/download` and `/v1/report`.
- **Lives:** only as a Cloudflare Worker secret. Never in the repo or the app.

### Rotating the token (no app release needed)

1. Create a new fine-grained PAT with the scopes above.
2. `cd infra/update-worker && source ~/.secrets/cloudflare.env && npx --yes wrangler@3 secret put GH_TOKEN`
   (paste the token at the prompt).
3. `curl -s https://plaud-v2-updates.leave-hacker-feedback.workers.dev/v1/latest` must still return the latest release.
4. Revoke the old token on GitHub (Settings › Developer settings › Fine-grained tokens).

### Retiring the token that shipped in v2.3.0 and earlier

Builds up to and including v2.3.0 embed a PAT (XOR-obfuscated in the binary) and call GitHub
directly. Revoking it stops those builds from updating, so:

1. Ship a release with the Worker-based updater and wait until every install has updated to it.
2. Then rotate as above (new PAT into the Worker, revoke the old one). Any install still on
   v2.3.0 or older after that has to be updated by hand from the latest release DMG.

---

## First-Time Install for Friends

1. Go to the [latest GitHub Release](https://github.com/Obednal97/Plaud-V2/releases/latest).
2. Download `Plaud.V2_{version}_aarch64.dmg`.
3. Open the DMG, drag Plaud V2 to **Applications** (running it straight from the DMG or Downloads blocks self-updates).
4. Double-click to open. It's signed and notarised, so it just opens.
5. Grant Bluetooth permission when prompted (one time only).
6. All future updates happen automatically through the app.

---

## Auto Bug Reporter

Unhandled errors are reported to GitHub Issues through the Worker (`POST /v1/report`).

- **Opt-out:** Settings › General › System › **Send anonymous crash reports** (on by default,
  stored as `crash_reports_enabled` in the backend config). When off, nothing is sent, Rust
  panics included; errors still go to the local diagnostics log.
- **Dedupe:** the fingerprint is a SHA-256 (first 16 hex chars) of the normalised message (long
  hex runs → `HEX`, digits → `N`). The Worker comments on the open `auto-bug` issue carrying
  `<!-- fp:<fingerprint> -->`, or creates one. The app also sends each fingerprint once per session.
- **What's sent:** error message (≤ 2,000 chars), context, the last 15 traceback lines, app and
  macOS version, architecture, RAM, Python and package versions, and an anonymous device ID.
- **Scrubbed before sending:** the home folder becomes `~`, any `/Users/<name>` becomes `/Users/~`,
  Plaud serial numbers (`[SN: …]` and bare 16-hex-digit serials) and BLE peripheral UUIDs are
  replaced by 8-hex-char tags salted with this Mac's hardware UUID (which is never sent).
  Recording, transcript and summary text are never part of a report.
- **Filtered locally:** `AbortError`s and anything fired while the backend health is `down` go to
  the local log only.
- **Rate limit:** the Worker accepts 20 reports per IP per hour.
- **User reports** (the in-app "Report a bug" form) go the same way with `kind: "user"` and label
  `user-report`. They are sent even if crash reports are off, because the user asked.
- Local diagnostics log: `~/Library/Application Support/com.plaud.v2/diagnostics.log` (rotates at 1 MB to `.log.1`).
- Never crashes the app: all reporting is fire-and-forget.

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| "Update server returned HTTP 502" | Worker can't reach GitHub, usually an expired or revoked `GH_TOKEN` | Rotate the Worker secret (see above). No app release needed |
| Update check fails for everyone | Worker down or renamed | `cd infra/update-worker && npx --yes wrangler@3 deploy`; check `npx --yes wrangler@3 tail` |
| "Downloaded update failed its checksum" | `.sha256` asset doesn't match the DMG | Regenerate the `.sha256` from the uploaded DMG and re-upload it (`gh release upload v$V … --clobber`) |
| Update downloads but doesn't install | Verification or copy failed | Read `~/Library/Logs/Plaud V2/update.log`: every step and the reason for any abort is there |
| "Plaud V2 is running from a temporary location…" | App Translocation (run from Downloads/DMG) | Move Plaud V2 to Applications and open it from there |
| "Restart to Update" can't replace the app | App Management permission (macOS Ventura+) or Applications not writable | Approve in System Settings › Privacy & Security › App Management; check the update log |
| Notarisation fails | API key not mounted or wrong IDs | `apple-keys-mount`; check `~/.appstoreconnect/iqralock.env` |
| `bundle_dmg.sh` fails | Finder AppleScript can't run headless | Re-run it with `--skip-jenkins`, then sign/notarise/staple the DMG (see recipe) |
| Codesign fails repeatedly | Apple timestamp server rate limiting, or keychain locked | Unlock `signing.keychain-db`; wait a few minutes and retry |
| Python server won't start from Dock | PATH doesn't include Homebrew | Fixed: the Rust shell adds common Python paths to PATH before spawning |
| FFmpeg not found at runtime | First-launch download failed | App auto-retries on next launch; users can `brew install ffmpeg` as a manual fallback (`utils/system.py` checks `shutil.which("ffmpeg")` first) |
