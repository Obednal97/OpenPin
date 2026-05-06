# Remove the Electron Build

**Date:** 2026-05-06
**Status:** Approved (design phase)

## Background

Plaud V2 was originally an Electron app and was later ported to Tauri (`docs/TAURI_MIGRATION.md`). The two builds have coexisted in `master` since the migration, with Tauri being the active runtime — every recent release tag (v2.0.x – v2.1.3) ships the Tauri DMG. Electron remains in the tree as dead weight: dependencies, build scripts, signing config, the main-process JS, and a handful of Tauri-side migration shims that import data left over by the Electron build.

The user's directive: stop maintaining the Electron build. Friends who still need to upgrade from Electron can do so via a previous release that retains the migration code; future releases no longer need that path.

## Goals

1. Remove every file in the repo whose only purpose is the Electron build.
2. Strip the Rust migration shims that import or clean up Electron-era state.
3. Update narrative docs and inline comments so the codebase reflects "Tauri only".
4. Verify the Tauri build still works after each phase (`cargo check`, `prepare-frontend.sh`, optional `cargo tauri dev`).

## Non-goals

- No frontend feature changes.
- No Tauri build-config changes beyond what's required by the deletions.
- No edits to historical documentation (`docs/TAURI_MIGRATION.md`, reverse-engineering notes, prior release notes).
- No edits to the recently-shipped bug-reporter / health-gate code.
- No changes to the Python backend beyond a single docstring.

## Architecture (current → target)

The Tauri build pipeline is already fully independent:

```
tauri.conf.json
  └─ beforeBuildCommand: scripts/prepare-frontend.sh
       └─ copies app/{index.html,renderer.js,tauri-bridge.js,styles.css,ui/*,css/*,assets/*,api_server.py,routes/,state/,providers/,utils/,icons,trays} → dist-tauri/
  └─ frontendDist: ../dist-tauri
src-tauri/
  └─ Rust shell, ffmpeg downloaded at runtime by app/utils/system.py:ensure_ffmpeg()
```

Nothing in that chain references `app/main.js`, `app/preload.js`, `app/package.json`, `app/entitlements.mac.plist`, `app/eslint.config.mjs`, `app/.stylelintrc.json`, `app/dist/`, `bin/ffmpeg`, or `scripts/download-ffmpeg.sh`. They're orphaned the moment Electron is dropped.

After removal, the only "frontend tooling" remaining is the Tauri build itself (`cargo tauri dev` / `cargo tauri build`).

## Components

### Phase 1 — Delete the Electron app shell + build assets (one commit)

| Path | Reason |
|---|---|
| `app/main.js` | Electron main process |
| `app/preload.js` | Electron context bridge |
| `app/package.json` | npm scripts (`start`, `build`, `publish`, lint) + electron-builder config |
| `app/package-lock.json` | npm lockfile |
| `app/node_modules/` | npm install output |
| `app/entitlements.mac.plist` | electron-builder signing entitlements (Tauri uses `src-tauri/Entitlements.plist`) |
| `app/eslint.config.mjs` | eslint config tied to `npm run lint` |
| `app/.stylelintrc.json` | stylelint config tied to `npm run lint` |
| `app/dist/` | Electron build output |
| `bin/ffmpeg` | Static FFmpeg used only by electron-builder's `extraResources` |
| `bin/` (parent dir) | Empty after `bin/ffmpeg` removal |
| `scripts/download-ffmpeg.sh` | Prebuild step for the deleted electron-builder pipeline |

Tauri's runtime ffmpeg path (`<app-support>/com.plaud.v2/bin/ffmpeg`, downloaded at first use) is unaffected by deleting `bin/`.

### Phase 2 — Strip Tauri-side migration shims (one commit)

| File | Lines (approx) | What goes |
|---|---|---|
| `src-tauri/src/lib.rs` | ~59-67 | Block in `load_prefs()` that imports `~/Library/Application Support/Plaud V2/electron-prefs.json` when the Tauri prefs file is empty |
| `src-tauri/src/lib.rs` | ~123-128 | Block in app setup that wipes `~/Library/Application Support/Plaud V2/updates` on launch |
| `src-tauri/src/python.rs` | ~73-90 | `electron_python_dir()` fn + the fallback in `find_python()` that pushes Electron's bundled-Python path into the candidate list |

After this commit, the Tauri runtime no longer reads or writes anything under `Plaud V2/` (Electron's userData dir).

### Phase 3 — Cosmetic comment + narrative-doc updates (one commit)

| File | Change |
|---|---|
| `app/api_server.py:2,5` | Update the module docstring: drop "Electron app" and "Spawned by Electron main.js as a child process". The backend is now spawned by the Tauri Rust shell. |
| `app/renderer.js:767` | Update the comment "Init Electron-only features immediately" — it predates the migration and now refers to native-app features generally. |
| `README.md` | Update build/run instructions: `cargo tauri dev` for development, `cargo tauri build` for release. Strip Electron / `npm` references from setup. |
| `docs/ARCHITECTURE.md` | Replace any "Electron + Tauri" section with Tauri-only. |
| `docs/RELEASING.md` | Replace electron-builder release flow with Tauri release flow. |

Comments in `app/tauri-bridge.js` (lines 2, 8, 126, 248) that reference Electron's preload as historical context — leave as-is. They explain *why* the file exists and don't depend on deleted files.

Comments in `src-tauri/src/commands/mod.rs` and `src-tauri/src/python.rs` that say things like "matches Electron's algorithm" — leave as-is. They're informational, not load-bearing.

`docs/TAURI_MIGRATION.md`, `docs/reverse-engineering/*.md`, and the `docs/superpowers/specs/*` and `docs/superpowers/plans/*` files we wrote earlier in this session — all kept as historical record.

## Data flow / runtime impact

After Phase 2, on a fresh Tauri launch:

- `load_prefs()` returns `{}` if no Tauri prefs file exists (instead of trying Electron's location).
- `find_python()` searches: bundled Tauri location → `PYTHON_CANDIDATES` (homebrew + system python). No Electron fallback.
- No directory-cleanup pass on launch.

A user who already migrated from Electron to Tauri (every release since v2.0) is unaffected — their Tauri prefs are populated and their Tauri Python is bundled.

A user still on Electron who installs the future "post-this-cleanup" Tauri DMG would get fresh prefs and a fresh Python install. They'd lose their custom settings and re-pair their device. This is the explicitly-accepted trade-off.

## Testing strategy

Manual / inspection-based — there is no automated test for the Tauri build pipeline.

After each phase, run:

| Check | Phase relevance |
|---|---|
| `git status` clean (only the intended deletions/edits staged) | All |
| `node -c` on each remaining JS file | Phase 1 (no syntax breakage from any cascading reference) |
| `cargo check` in `src-tauri/` | Phases 1, 2 |
| `bash scripts/prepare-frontend.sh` succeeds and `dist-tauri/` rebuilds correctly | Phase 1 |
| `cargo tauri dev` launches and the app reaches the main UI | After Phase 2 (migration shims removed) |
| `git grep -i electron` matches only intentionally-kept references | After Phase 3 |

The "intentionally-kept" set after Phase 3:

- `docs/TAURI_MIGRATION.md`, `docs/reverse-engineering/*.md` (historical)
- `docs/superpowers/specs/2026-05-06-bug-reporter-and-endpoint-fixes-design.md` and the corresponding plan (immutable history)
- `docs/superpowers/specs/2026-05-06-remove-electron-design.md` (this file) and its plan
- Comment lines in `app/tauri-bridge.js` (preload-replacement context)
- Comment lines in `src-tauri/src/commands/mod.rs` and `src-tauri/src/python.rs` (algorithm-parity comments)

## Edge cases & risks

- **Hidden cross-reference to a deleted file.** Electron-specific files imported elsewhere would surface as a `node -c` syntax error or a `cargo check` failure. The verification list above catches both.
- **`prepare-frontend.sh` referencing a deleted path.** The script copies from `app/` selectively; none of the deleted files are in its source list, so it's already independent.
- **`tauri.conf.json` `bundle.icon` references `../app/icon_512.png` and `../app/icon.icns`.** Both files stay (they're shared assets). Verified.
- **`tauri.conf.json` `resources` mappings reference `../src/*`, `../keys/*`, and `../app/{api_server.py,routes/*,state/*,providers/*,utils/*}`.** None of these are deleted. Verified.
- **A friend on a stale Electron release upgrading directly to a post-Phase-2 build will lose preferences and re-bundle Python.** Accepted per user direction; their upgrade path is via a prior release that still has the shims.
- **`bin/` removal could break a developer's local script that hardcodes `bin/ffmpeg`.** Spot-check: no Tauri code references `bin/ffmpeg`; the Python backend uses `<app-support>/com.plaud.v2/bin/ffmpeg`. Safe.

## Out of scope

- Removing references to Electron in spec/plan documents written earlier in this session (immutable history).
- Editing the recently-shipped bug-reporter / health-gate code.
- Any change to how Tauri downloads/manages ffmpeg (separate concern).
- Cleaning the long list of pre-existing untracked `scripts/depair_*.py` and `scripts/wifi_restore_*.py` files (unrelated).

## Followups

- After this work ships in a release, a single Linear/issue note can record that the Electron migration is fully retired and that the upgrade path for stragglers is "install a v2.1.3 or earlier release first, then upgrade".
