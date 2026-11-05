# Releasing

How to build, sign, notarise and publish a release, and how the update channel works. Tested
with v2.4.0 on a headless Mac.

Names: the product is **Open Pin**; the bundle ID (`com.plaud.v2`), the Worker
(`plaud-v2-updates`), the data and log folders and the crate name keep the app's old name,
Plaud V2, on purpose (see [ARCHITECTURE.md](ARCHITECTURE.md#names)). Never change the bundle ID:
the installer refuses any other, and Bluetooth permission and the Keychain backup depend on it.

> **Note** — the repository is **public**, so the update Worker needs no GitHub token. The old
> `POST /v1/report` bug-report endpoint and its issue-writing token were retired in November 2026.
> The GitHub token embedded in **v2.3.0 and older** was revoked, so those builds can't update
> themselves: install the latest DMG by hand.

## Contents

- [How updates reach users](#how-updates-reach-users)
- [Prerequisites](#prerequisites)
- [Release recipe](#release-recipe)
- [Verify before publishing](#verify-before-publishing)
- [Publish](#publish)
- [The Worker's GitHub token (optional)](#the-workers-github-token-optional)
- [Bug reports](#bug-reports)
- [Troubleshooting](#troubleshooting)

## How updates reach users

The app never talks to GitHub and ships no token. The updater in `src-tauri/src/commands/mod.rs`
talks to the **`plaud-v2-updates` Cloudflare Worker** (`infra/update-worker/`) at
`https://openpin.bednal.com` (the `WORKER_BASE` constant), which holds a GitHub token as a secret.
The token can be rotated without a release. Installs up to v2.5.x call the same Worker at its old
`workers.dev` address; keep that address working until they have updated.

1. 5 seconds after launch, then every 4 hours, the app calls `GET /v1/latest`: the latest
   **published** release (no drafts or pre-releases), cached for 5 minutes by the Worker.
   **Test channel:** callers whose IP is in the Worker secret `CANARY_IPS` get the newest release
   including pre-releases. To try a build on your own Macs first, publish it as a GitHub
   pre-release, then promote it (`gh release edit <tag> --prerelease=false --latest`) for
   everyone. `--latest` matters: without it GitHub keeps the previous release as "Latest", and
   `/v1/latest` keeps serving that.
2. Versions compare as `major.minor.patch`; anything after `-` or `+` is ignored, so
   `2.4.0-beta` is never newer than `2.4.0`. Any non-2xx answer is shown as an update error, never
   as "up to date".
3. If it's newer, the app downloads `GET /v1/download/<tag>/<asset>` (a 302 to the asset's public
   `github.com/…/releases/download/` URL, which GitHub redirects to a short-lived signed URL; no
   GitHub API call, so no rate limit) to `~/Library/Application Support/com.plaud.v2/updates/<name>.part`, with a 15 s connect
   timeout and a 60 s idle timeout. It checks the **size** and, if the release has a
   `<dmg>.sha256` asset, the **SHA-256**, then renames the file into place.
4. A banner offers **Restart to Update**. A detached helper script (`INSTALL_SCRIPT`; log:
   `~/Library/Logs/Plaud V2/update.log`):
   1. waits up to 30 s for the app to exit;
   2. mounts the DMG on a private mount point;
   3. refuses the update unless the bundle is `com.plaud.v2`, has the expected version, passes
      `codesign --verify --deep --strict`, is signed by the expected team (`EXPECTED_TEAM_ID`: the
      build's `APPLE_TEAM_ID`, else the official releases' team), and passes Gatekeeper
      (`spctl -a -t exec`);
   4. picks the destination: over the running app, keeping the name and folder the user gave it,
      except that a bundle still called `Plaud V2.app` gets the DMG's `Open Pin.app` installed
      next to it (see [The rename to Open Pin](#the-rename-to-open-pin));
   5. moves whatever is at the destination aside, copies the new app with `ditto`, checks its
      version and signature again, deletes the aside copy (and, when renaming, the old
      `Plaud V2.app`), restoring everything on any failure;
   6. removes the quarantine flag and relaunches the new path.
5. If the app runs from a randomised `/AppTranslocation/` path (opened from Downloads or the DMG),
   the updater refuses and asks the user to move it to Applications.

Every build is signed with the same Developer ID, so macOS keeps Bluetooth and other permissions
across updates.

### The rename to Open Pin

Releases up to v2.5.3 shipped `Plaud V2.app`; later ones ship `Open Pin.app` (same bundle ID,
same team). What happens to an existing install:

1. **The update from a pre-rename version** is installed by that version's own installer, which
   copies the new app over `Plaud V2.app` and keeps that name. Nothing else changes: the data,
   logs, venv and Keychain items never depended on the app's name.
2. **On the first launch** from a bundle named exactly `Plaud V2.app`, the new version
   (`src-tauri/src/migrate.rs`) starts a helper and quits before anything else starts. The helper
   waits for it to exit, renames the bundle to `Open Pin.app` in the same folder with `mv` (a
   rename, nothing copied) and opens it. It reopens the old bundle instead if `Open Pin.app`
   already exists there, another copy is still running from the old bundle, or `mv` fails. It's
   tried once (marker `bundle-rename-tried` in `Application Support/com.plaud.v2/`); the steps
   are logged to `update.log`.
3. **The installer** does the same rename on any later update if the bundle is still called
   `Plaud V2.app`: it installs `Open Pin.app` next to it and deletes the old bundle only after the
   new one verified. An `Open Pin.app` already there is replaced only if it has our bundle ID.
4. **Open at login**: the autostart plugin names its LaunchAgent after the product, so it moves
   from `~/Library/LaunchAgents/Plaud V2.plist` to `Open Pin.plist`. At startup the app deletes
   the old file and registers the login item again under the new name, and re-registers it
   whenever the file points at an old path (the app was moved or renamed).
5. A bundle the user renamed to anything else, or moved, is updated in place and keeps its name.

Tests: `install_script_*` in `src-tauri/src/commands/mod.rs` and `rename_script_*` in
`migrate.rs` run the real scripts against fake bundles, with stub `hdiutil`, `codesign`, `spctl`
and `open`. Not covered by tests: Dock items. A Dock entry for `Plaud V2.app` may need pinning
again after the rename.

### What a release contains

| Asset (as GitHub names it) | Purpose |
|---|---|
| `Open.Pin_<version>_aarch64.dmg` | First installs and the update payload |
| `Open.Pin_<version>_aarch64.dmg.sha256` | `shasum -a 256` of the DMG; the updater checks the download against it |

GitHub turns spaces in uploaded file names into dots (`Open Pin_…` becomes `Open.Pin_…`), so the
recipe renames the DMG before uploading. Releases up to v2.5.3 used `Plaud.V2_…`. Nothing depends
on the name: the Worker returns whichever `.dmg` the latest release has, and the app accepts any
safe file name ending in `.dmg`. A release without a `.sha256` still updates, but only the size
is checked: always upload it.

## Prerequisites

- **Apple Silicon Mac** with Xcode command-line tools, Node (for `npx`) and Rust via rustup.
- A **Developer ID Application** certificate in an unlocked keychain. Its name goes in
  `APPLE_SIGNING_IDENTITY` and its team ID in `APPLE_TEAM_ID` (see the recipe).
- An **App Store Connect API key** (team key, `.p8` file) for notarisation. The release needs three
  values: key ID, issuer ID, and the path to the `.p8`. Notarisation uses only this key; there is
  no Apple ID password or app-specific password in the process any more.
- The RSA keys in `keys/` (gitignored; see [DEVELOPMENT.md](DEVELOPMENT.md#keys)). The build fails
  without them.
- `gh`, signed in with permission to create releases on the repository (`Obednal97/OpenPin`, renamed from
  `Obednal97/Plaud-V2` on 3 November 2026; GitHub redirects the old slug).
- `jq` (used in the checks below).

Keep all of these outside the repo. Nothing in the repo or the app holds a signing, notarisation
or GitHub credential.

### Signing settings

| Variable | Example | Used by |
|---|---|---|
| `APPLE_SIGNING_IDENTITY` | `Developer ID Application: Jane Doe (ABCDE12345)` | Tauri (overrides `bundle.macOS.signingIdentity` in `tauri.conf.json`) and the DMG `codesign` step |
| `APPLE_TEAM_ID` | `ABCDE12345` | Compiled into the app as the team its updater accepts (`EXPECTED_TEAM_ID`); also the check below |
| `APPLE_PROVIDER_SHORT_NAME` | `ABCDE12345` | Tauri (overrides `bundle.macOS.providerShortName`); only needed if the API key can see several teams |
| `APPLE_API_KEY`, `APPLE_API_ISSUER`, `APPLE_API_KEY_PATH` | key ID, issuer ID, path to the `.p8` | Notarisation |

`tauri.conf.json` still names the official releases' identity and team, so the owner's builds
work without these overrides; anyone else must set them. Without `APPLE_TEAM_ID` the app expects
updates signed by the official team, so a build signed by another team won't accept its own
updates. Your own build also needs its own update channel: `WORKER_BASE` in
`src-tauri/src/commands/mod.rs` points at the official Worker.

Keep machine-specific details (keychain unlock, where the API key lives) in your own notes, not
in the repo.

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

# 2. Signing and notarisation environment (see "Signing settings"; values from wherever you keep them)
export APPLE_SIGNING_IDENTITY="Developer ID Application: <name> (<team id>)"
export APPLE_TEAM_ID="<team id>"
export APPLE_API_KEY="<key id>" APPLE_API_ISSUER="<issuer id>" APPLE_API_KEY_PATH="/path/to/AuthKey_<key id>.p8"

# 3. Build: Tauri builds, signs and notarises the .app, then builds the DMG.
#    Use the @tauri-apps/cli minor that matches the tauri crate in Cargo.lock (2.12.x -> 2.12).
#    CI=true stops the DMG script from driving Finder, which fails on a headless Mac.
TAURI_MINOR=$(grep -A1 '^name = "tauri"$' src-tauri/Cargo.lock | sed -nE 's/^version = "([0-9]+\.[0-9]+)\..*/\1/p')
CI=true npx --yes "@tauri-apps/cli@$TAURI_MINOR" build

# 4. Sign, notarise and staple the DMG itself (Tauri only notarises the .app inside it).
DMG="src-tauri/target/release/bundle/dmg/Open Pin_${V}_aarch64.dmg"
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
APP="src-tauri/target/release/bundle/macos/Open Pin.app"
codesign --verify --deep --strict --verbose=2 "$APP"
codesign -dv "$APP" 2>&1 | grep -E "TeamIdentifier=$APPLE_TEAM_ID|flags=.*runtime"   # team + hardened runtime
plutil -extract CFBundleIdentifier raw "$APP/Contents/Info.plist"          # = com.plaud.v2
spctl -a -t exec -vv "$APP"                                          # accepted, Notarized Developer ID
xcrun stapler validate "$APP"
spctl -a -t open --context context:primary-signature -vv "$DMG"      # accepted, Notarized Developer ID
xcrun stapler validate "$DMG"
plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist"   # = $V
```

Then open the DMG, drag the app to Applications on a test Mac (or over an older install) and check
it starts, shows the new version in **Settings › General › About**, and connects to the device.
For the first release after the rename, also update a `Plaud V2.app` install through **Restart to
Update** and check that it ends up as `Open Pin.app` with its recordings, pairing and "Open at
login" intact, and that `update.log` shows the rename.

## Publish

Write the release notes to `release-notes.md` (user-facing: what changed and why it matters),
then:

```bash
OUT="$(mktemp -d)"
cp "$DMG" "$OUT/Open.Pin_${V}_aarch64.dmg"
( cd "$OUT" && shasum -a 256 "Open.Pin_${V}_aarch64.dmg" > "Open.Pin_${V}_aarch64.dmg.sha256" )

gh release create "v$V" \
  "$OUT/Open.Pin_${V}_aarch64.dmg" \
  "$OUT/Open.Pin_${V}_aarch64.dmg.sha256" \
  --title "v$V" --notes-file release-notes.md

# What installs will see (allow up to 5 minutes for the Worker's cache):
W=https://openpin.bednal.com
curl -s "$W/v1/latest" | jq '{version, asset}'                   # version = $V, sha256 not null
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$(curl -s "$W/v1/latest" | jq -r .download_url)"   # 302
```

Running copies pick the update up within 4 hours, or straight away with **Check for Updates**.
Check an older install updates and that `update.log` ends cleanly.

## The Worker's GitHub token (optional)

The repository is public, so the update Worker reads releases **without a token**. A read-only
`GH_TOKEN` is optional and only raises GitHub's API rate limit. Unauthenticated reads do get
`403` rate-limit errors now and then (Cloudflare's egress IPs are shared); the Worker then serves
the last good answer from KV, so apps see no error, but a new release only reaches them once a
GitHub call succeeds. A token removes that delay.

1. GitHub › Settings › Developer settings › Fine-grained tokens › **Generate new token**, for
   repository **Obednal97/OpenPin** only, with **Contents: Read-only** (Metadata: Read-only is
   added automatically). No other permission — the Worker never writes.
2. Set it (wrangler reads it from the prompt, so it stays out of shell history):

   ```bash
   cd infra/update-worker
   # Cloudflare credentials in the environment: CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID
   npx --yes wrangler@4 secret put GH_TOKEN
   ```

3. Check: `curl -s https://openpin.bednal.com/v1/latest | jq .version` (and the old `workers.dev` address, which installs up to v2.5.x still call)
   returns the latest version (wait out the 5-minute cache if it was failing before).
4. Revoke the old token on GitHub.

Deploying the Worker, its endpoints, limits and smoke tests:
[infra/update-worker/README.md](../infra/update-worker/README.md).

## Bug reports

The app files no GitHub issues: no crash reports, no in-app submission. Errors only go
to the app log and `~/Library/Application Support/com.plaud.v2/diagnostics.log` (rotates at 1 MB),
Rust panics included. (The Worker's old `/v1/report` endpoint was retired in November 2026.)

**Report a bug › Export bug report** (`export_bug_report` in `src-tauri/src/commands/mod.rs`)
saves `Open Pin bug report <date> <time>.zip` to `~/Downloads` (" 2", " 3"… if the name is taken).
People look through it and attach it to an issue made from `.github/ISSUE_TEMPLATE/bug_report.yml`.

- **`report.md`:** the user's three answers, system info (app and macOS version, architecture,
  RAM, Python and package versions, an anonymous device ID), the backend's checks from
  `GET /diagnostics` (signed in, user token present, API host, paired devices by the last four
  characters of their serial with token and signature present or not, the last NotePin handshake,
  providers and which keys are set, Notion and Drive connected, recording count, free disk space)
  and the last 50 diagnostics log entries, aborted requests left out.
- **`logs/`:** the last 3,000 lines of `app.log` (topped up from `app.log.1` after a rotation),
  500 of `diagnostics.log`, 500 of `update.log` and 1,000 of `wifi_sync.log`, each if it exists.
- **Scrubbed:** every file goes through `scrub_for_report`: the home folder becomes `~`, any
  `/Users/<name>` becomes `/Users/~`, device serials and BLE UUIDs become salted 8-character tags,
  and `report_scrub.rs` removes URL query strings, Notion and Google Drive paths, bearer tokens,
  `key=value` secrets, prefixed API keys, e-mail addresses and long hex or base64 tokens.
  Recording, transcript and summary text is never part of a report.
- If the backend isn't running, the report says so under Checks and is still exported.

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| "Update server returned HTTP 502" | The Worker can't reach GitHub: usually an expired or revoked `GH_TOKEN` | Rotate the token (above) |
| Update check fails for everyone | Worker down or not deployed | `cd infra/update-worker && npx --yes wrangler@4 deploy`; watch `npx --yes wrangler@4 tail` |
| "Downloaded update failed its checksum" | The `.sha256` doesn't match the DMG | Regenerate it from the uploaded DMG and `gh release upload v$V <file> --clobber` |
| Update downloads but doesn't install | A check or the copy failed | Read `~/Library/Logs/Plaud V2/update.log`; every step and the reason for an abort is there |
| "Open Pin is running from a temporary location…" | App Translocation | Move the app to Applications and open it from there |
| Still called `Plaud V2.app` after updating | The launch-time rename was skipped (see `update.log`: another copy running, `Open Pin.app` already there, or `mv` failed) | The next update renames it; or quit and rename it by hand |
| "Restart to Update" can't replace the app | App Management permission (macOS 13+) or Applications not writable | Allow it in System Settings › Privacy & Security › App Management |
| `glob pattern ../keys/* path not found` | `keys/` missing | Copy the RSA keys into `keys/` |
| Notarisation fails | Wrong or missing API key values, or the `.p8` isn't readable | Check the three `APPLE_API_*` variables; `xcrun notarytool log <submission-id> --key … --key-id … --issuer …` shows Apple's reason |
| `bundle_dmg.sh` fails | Finder scripting can't run headless | Build with `CI=true`, or re-run the script with `--skip-jenkins` (see the recipe) |
| codesign fails | Keychain locked, or Apple's timestamp server busy | Unlock the signing keychain; retry in a few minutes |
| Users stuck on the splash after an update | The venv couldn't be re-synced | They click **Retry**; if it keeps failing, delete `~/Library/Application Support/com.plaud.v2/venv` and reopen. `app.log` has the pip output |
