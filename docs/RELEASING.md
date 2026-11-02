# Releasing

How to build, sign, notarise and publish a release, and how the update channel works. Tested
with v2.4.0 on a headless Mac mini.

> **Operational status (2 November 2026)** — delete this box once it's resolved.
>
> - The fine-grained token the Worker used (`GH_TOKEN`) was **revoked on 2026-11-02**. Until a new
>   one is set, `/v1/latest`, `/v1/download` and `/v1/report` fail once the Worker's 5-minute
>   cache expires, so no install can update or send reports. Create a new token and set it as in
>   [Rotating the Worker's GitHub token](#rotating-the-workers-github-token).
> - The GitHub token embedded in **v2.3.0 and older** has been revoked too. Those builds can't
>   update themselves any more: install the latest DMG by hand.

## Contents

- [How updates reach users](#how-updates-reach-users)
- [Prerequisites](#prerequisites)
- [Release recipe](#release-recipe)
- [Verify before publishing](#verify-before-publishing)
- [Publish](#publish)
- [Rotating the Worker's GitHub token](#rotating-the-workers-github-token)
- [Crash reports](#crash-reports)
- [Troubleshooting](#troubleshooting)

## How updates reach users

The app never talks to GitHub and ships no token. The updater in `src-tauri/src/commands/mod.rs`
talks to the **`plaud-v2-updates` Cloudflare Worker** (`infra/update-worker/`,
`https://plaud-v2-updates.leave-hacker-feedback.workers.dev`, the `WORKER_BASE` constant), which
holds a GitHub token as a secret. The token can be rotated without a release.

1. 5 seconds after launch, then every 4 hours, the app calls `GET /v1/latest`: the latest
   **published** release (no drafts or pre-releases), cached for 5 minutes by the Worker.
2. Versions compare as `major.minor.patch`; anything after `-` or `+` is ignored, so
   `2.4.0-beta` is never newer than `2.4.0`. Any non-2xx answer is shown as an update error, never
   as "up to date".
3. If it's newer, the app downloads `GET /v1/download/<tag>/<asset>` (a 302 to a short-lived GitHub
   URL) to `~/Library/Application Support/com.plaud.v2/updates/<name>.part`, with a 15 s connect
   timeout and a 60 s idle timeout. It checks the **size** and, if the release has a
   `<dmg>.sha256` asset, the **SHA-256**, then renames the file into place.
4. A banner offers **Restart to Update**. A detached helper script (log:
   `~/Library/Logs/Plaud V2/update.log`):
   1. waits up to 30 s for the app to exit;
   2. mounts the DMG on a private mount point;
   3. refuses the update unless the bundle is `com.plaud.v2`, has the expected version, passes
      `codesign --verify --deep --strict`, is signed by team **4SN93PWF7Z**, and passes Gatekeeper
      (`spctl -a -t exec`);
   4. moves the old app aside, copies the new one with `ditto`, checks it again and deletes the
      old copy, restoring the old app on any failure;
   5. removes the quarantine flag and relaunches.
5. If the app runs from a randomised `/AppTranslocation/` path (opened from Downloads or the DMG),
   the updater refuses and asks the user to move it to Applications.

Every build is signed with the same Developer ID, so macOS keeps Bluetooth and other permissions
across updates.

### What a release contains

| Asset (as GitHub names it) | Purpose |
|---|---|
| `Plaud.V2_<version>_aarch64.dmg` | First installs and the update payload |
| `Plaud.V2_<version>_aarch64.dmg.sha256` | `shasum -a 256` of the DMG; the updater checks the download against it |

GitHub turns spaces in uploaded file names into dots (`Plaud V2_…` becomes `Plaud.V2_…`), so the
recipe renames the DMG before uploading. A release without a `.sha256` still updates, but only the
size is checked: always upload it.

## Prerequisites

- **Apple Silicon Mac** with Xcode command-line tools, Node (for `npx`) and Rust via rustup.
- **Developer ID Application** certificate for team 4SN93PWF7Z
  (`Developer ID Application: Oliver Bednal (4SN93PWF7Z)`) in an unlocked keychain.
- An **App Store Connect API key** (team key, `.p8` file) for notarisation. The release needs three
  values: key ID, issuer ID, and the path to the `.p8`. Notarisation uses only this key; there is
  no Apple ID password or app-specific password in the process any more.
- The RSA keys in `keys/` (gitignored; see [DEVELOPMENT.md](DEVELOPMENT.md#keys)). The build fails
  without them.
- `gh`, signed in with permission to create releases on `Obednal97/Plaud-V2`.
- `jq` (used in the checks below).

Keep all of these outside the repo. Nothing in the repo or the app holds a signing, notarisation
or GitHub credential.

**On the owner's Mac mini** (headless): Rust is at `/opt/homebrew/opt/rustup/bin`; the
certificate is in a dedicated signing keychain that has to be unlocked first; the API key's IDs
and `.p8` path are in `~/.appstoreconnect/iqralock.env` (`ASC_KEY_ID`, `ASC_ISSUER_ID`,
`ASC_KEY_PATH`) and the `.p8` is mounted from 1Password by `apple-keys-mount`. See that Mac's
own notes for the unlock command.

## Release recipe

```bash
cd plaud-v2                   # repo root, on master, clean, tests passing
V=2.4.1                       # the new version

# 1. Bump the version in all three manifests (they must match):
#      src-tauri/tauri.conf.json   "version": "$V"
#      src-tauri/Cargo.toml        version = "$V"   (then: cd src-tauri && cargo check && cd ..)
#      pyproject.toml              version = "$V"
git add src-tauri/tauri.conf.json src-tauri/Cargo.toml src-tauri/Cargo.lock pyproject.toml
git commit -m "Bump v$V"
git tag "v$V"
git push origin master "v$V"

# 2. Signing and notarisation environment (values from wherever you keep them)
export APPLE_SIGNING_IDENTITY="Developer ID Application: Oliver Bednal (4SN93PWF7Z)"
export APPLE_API_KEY="<key id>" APPLE_API_ISSUER="<issuer id>" APPLE_API_KEY_PATH="/path/to/AuthKey_<key id>.p8"
#    On the owner's Mac mini, after unlocking the signing keychain and running apple-keys-mount:
#    set -a; . ~/.appstoreconnect/iqralock.env; set +a
#    export APPLE_API_KEY="$ASC_KEY_ID" APPLE_API_ISSUER="$ASC_ISSUER_ID" APPLE_API_KEY_PATH="$ASC_KEY_PATH"

# 3. Build: Tauri builds, signs and notarises the .app, then builds the DMG.
#    Use the @tauri-apps/cli minor that matches the tauri crate in Cargo.lock (2.12.x -> 2.12).
#    CI=true stops the DMG script from driving Finder, which fails on a headless Mac.
TAURI_MINOR=$(grep -A1 '^name = "tauri"$' src-tauri/Cargo.lock | sed -nE 's/^version = "([0-9]+\.[0-9]+)\..*/\1/p')
CI=true npx --yes "@tauri-apps/cli@$TAURI_MINOR" build

# 4. Sign, notarise and staple the DMG itself (Tauri only notarises the .app inside it).
DMG="src-tauri/target/release/bundle/dmg/Plaud V2_${V}_aarch64.dmg"
codesign --force --timestamp --sign "$APPLE_SIGNING_IDENTITY" "$DMG"
xcrun notarytool submit "$DMG" --key "$APPLE_API_KEY_PATH" --key-id "$APPLE_API_KEY" \
  --issuer "$APPLE_API_ISSUER" --wait
xcrun stapler staple "$DMG"
```

If `bundle_dmg.sh` still fails, the `.app` is already built, signed and notarised in
`src-tauri/target/release/bundle/macos/`. Run the build with `--verbose` to see the
`bundle_dmg.sh` command line, then re-run that script from `src-tauri/target/release/bundle/dmg/`
with `--skip-jenkins` added in front of the same arguments.

## Verify before publishing

```bash
APP="src-tauri/target/release/bundle/macos/Plaud V2.app"
codesign --verify --deep --strict --verbose=2 "$APP"
codesign -dv "$APP" 2>&1 | grep -E 'TeamIdentifier=4SN93PWF7Z|flags=.*runtime'   # team + hardened runtime
spctl -a -t exec -vv "$APP"                                          # accepted, Notarized Developer ID
xcrun stapler validate "$APP"
spctl -a -t open --context context:primary-signature -vv "$DMG"      # accepted, Notarized Developer ID
xcrun stapler validate "$DMG"
plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist"   # = $V
```

Then open the DMG, drag the app to Applications on a test Mac (or over an older install) and check
it starts, shows the new version in **Settings › General › About**, and connects to the device.

## Publish

Write the release notes to `release-notes.md` (user-facing: what changed and why it matters),
then:

```bash
OUT="$(mktemp -d)"
cp "$DMG" "$OUT/Plaud.V2_${V}_aarch64.dmg"
( cd "$OUT" && shasum -a 256 "Plaud.V2_${V}_aarch64.dmg" > "Plaud.V2_${V}_aarch64.dmg.sha256" )

gh release create "v$V" \
  "$OUT/Plaud.V2_${V}_aarch64.dmg" \
  "$OUT/Plaud.V2_${V}_aarch64.dmg.sha256" \
  --title "v$V" --notes-file release-notes.md

# What installs will see (allow up to 5 minutes for the Worker's cache):
W=https://plaud-v2-updates.leave-hacker-feedback.workers.dev
curl -s "$W/v1/latest" | jq '{version, asset}'                   # version = $V, sha256 not null
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$(curl -s "$W/v1/latest" | jq -r .download_url)"   # 302
```

Running copies pick the update up within 4 hours, or straight away with **Check for Updates**.
Check an older install updates and that `update.log` ends cleanly.

## Rotating the Worker's GitHub token

The only GitHub token in the system is the Worker secret `GH_TOKEN`. Rotate it any time; no app
release is needed.

1. GitHub › Settings › Developer settings › Fine-grained tokens › **Generate new token**, for
   repository **Obednal97/Plaud-V2** only, with **Contents: Read-only** and **Issues: Read and
   write** (Metadata: Read-only is added automatically).
2. Set it (wrangler reads it from the prompt, so it stays out of shell history):

   ```bash
   cd infra/update-worker
   # Cloudflare credentials in the environment: CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID
   npx --yes wrangler@4 secret put GH_TOKEN
   ```

3. Check: `curl -s https://plaud-v2-updates.leave-hacker-feedback.workers.dev/v1/latest | jq .version`
   returns the latest version (wait out the 5-minute cache if it was failing before).
4. Revoke the old token on GitHub.

Deploying the Worker, its endpoints, limits and smoke tests:
[infra/update-worker/README.md](../infra/update-worker/README.md).

## Crash reports

Unhandled errors are reported to GitHub Issues through the Worker (`POST /v1/report`).

- **Opt-out:** Settings › General › System › **Send anonymous crash reports** (on by default,
  `crash_reports_enabled` in the config). Off means nothing is sent, Rust panics included; errors
  still go to the local diagnostics log.
- **What's sent:** the error message (up to 1,000 characters), context, the last 15 traceback
  lines, app and macOS version, architecture, RAM, Python and package versions, and an anonymous
  device ID (8 hex characters of a hash of the Mac's hardware UUID).
- **Scrubbed first:** the home folder becomes `~`, any `/Users/<name>` becomes `/Users/~`, device
  serials and BLE UUIDs become salted 8-character tags, and `report_scrub.rs` removes URL query
  strings, Notion and Google Drive paths, bearer tokens, `key=value` secrets, prefixed API keys,
  e-mail addresses and long hex or base64 tokens. Recording, transcript and summary text is never
  part of a report.
- **Dedupe:** the fingerprint is a SHA-256 (first 16 hex characters) of the normalised message.
  The Worker comments on the open `auto-bug` issue it created for that fingerprint, or opens one.
  The app sends each fingerprint once per session.
- **Not sent:** `AbortError`s, and anything while the backend is down. Both go to
  `~/Library/Application Support/com.plaud.v2/diagnostics.log` (rotates at 1 MB).
- **User reports** (the **Report a bug** button) are sent with `kind: "user"` and label
  `user-report`, even with crash reports off.
- **Limits** are enforced by the Worker (per IP and global, per minute, hour and day); see its
  README.
- Reporting is fire-and-forget and never crashes the app.

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| "Update server returned HTTP 502" | The Worker can't reach GitHub: usually an expired or revoked `GH_TOKEN` | Rotate the token (above) |
| Update check fails for everyone | Worker down or not deployed | `cd infra/update-worker && npx --yes wrangler@4 deploy`; watch `npx --yes wrangler@4 tail` |
| "Downloaded update failed its checksum" | The `.sha256` doesn't match the DMG | Regenerate it from the uploaded DMG and `gh release upload v$V <file> --clobber` |
| Update downloads but doesn't install | A check or the copy failed | Read `~/Library/Logs/Plaud V2/update.log`; every step and the reason for an abort is there |
| "Plaud V2 is running from a temporary location…" | App Translocation | Move the app to Applications and open it from there |
| "Restart to Update" can't replace the app | App Management permission (macOS 13+) or Applications not writable | Allow it in System Settings › Privacy & Security › App Management |
| `glob pattern ../keys/* path not found` | `keys/` missing | Copy the RSA keys into `keys/` |
| Notarisation fails | Wrong or missing API key values, or the `.p8` isn't readable | Check the three `APPLE_API_*` variables; `xcrun notarytool log <submission-id> --key … --key-id … --issuer …` shows Apple's reason |
| `bundle_dmg.sh` fails | Finder scripting can't run headless | Build with `CI=true`, or re-run the script with `--skip-jenkins` (see the recipe) |
| codesign fails | Keychain locked, or Apple's timestamp server busy | Unlock the signing keychain; retry in a few minutes |
| Users stuck on the splash after an update | The venv couldn't be re-synced | They click **Retry**; if it keeps failing, delete `~/Library/Application Support/com.plaud.v2/venv` and reopen. `app.log` has the pip output |
