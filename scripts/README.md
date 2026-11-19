# scripts/

## Build and maintenance (used by the build, keep working)

| Script | What it does |
|---|---|
| `prepare-frontend.sh` | Copies the frontend (and backend files) into `dist-tauri/`. Tauri runs it as `beforeBuildCommand`; run it by hand before `tauri dev` or `cargo test`. |
| `lock-python-deps.sh` | Regenerates the hash-pinned lockfiles in `app/requirements/` from `python-deps/*.in` (needs uv). See [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md#python-dependencies). |
| `publish-update.sh <version>` | After `gh release create`: writes the release into the update Worker's KV so installs are offered it even while GitHub refuses the Worker. See [docs/RELEASING.md](../docs/RELEASING.md#publish). |
| `python-deps/*.in` | The unpinned inputs: `core` plus the `whisper`, `diarize` and `local-summary` extras. |

## Legacy device scripts

Deleted in November 2026 (in git history): the standalone reverse-engineering scripts, including `sync_and_transcribe.py`, `sync_device.py`, `session_manager.py` and `download_all.py`. The app does all of this.
