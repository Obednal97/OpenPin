# Development

Running Open Pin from source, testing, linting and the gotchas. Read
[ARCHITECTURE.md](ARCHITECTURE.md) first for how the pieces fit. Folders, the bundle ID, the
crate and some config names still say Plaud V2, the app's old name; keep them (see
[ARCHITECTURE.md](ARCHITECTURE.md#names)).

## Prerequisites

- Apple Silicon Mac (the Python lockfiles only have `aarch64` wheels).
- Rust 1.90 or later (rustup), Node (for `npx`), [uv](https://docs.astral.sh/uv/)
  (`brew install uv`).
- The RSA keys in `keys/` (below).

## Keys

`keys/` is gitignored and must hold `app_private.pem`, `app_public.pem`, `e2ee_private.pem` and
`e2ee_public.pem`. They are the fixed RSA keys from Plaud's Android app (see
[reverse-engineering/](reverse-engineering/README.md)); every Plaud client ships the same ones.
The app needs them to talk to the device and decrypt audio, the Tauri build bundles them (it fails
with `glob pattern ../keys/* path not found` without them), and `tests/test_crypto.py` reads them.
They aren't in the repo: copy them from an existing checkout, or extract them from Plaud's
Android app as described in the reverse-engineering notes.

## Project layout

```
app/                    Python backend + frontend (one folder; the build splits them)
  api_server.py         Flask app and request security
  routes/ state/ providers/ utils/    backend modules
  requirements/         hash-pinned lockfiles (generated)
  index.html renderer.js tauri-bridge.js ui/ css/ assets/   frontend
src/                    shared Python: crypto, BLE scanner, local Whisper + diarization
src-tauri/              Rust shell (Tauri 2): src/, tauri.conf.json, Entitlements.plist, Info.plist
infra/update-worker/    Cloudflare Worker for updates (release lookup + download proxy)
scripts/                build helpers (see scripts/README.md)
tests/                  pytest suite
docs/                   these docs
keys/                   RSA keys (gitignored)
```

## Python environment

Use a CPython 3.12 venv built from the same lockfile as the app. Keep it outside the repo (`.venv`
isn't gitignored):

```bash
uv venv --python 3.12 ~/.venvs/plaud-v2
VIRTUAL_ENV=~/.venvs/plaud-v2 uv pip install -r app/requirements/core.txt pytest ruff
source ~/.venvs/plaud-v2/bin/activate
```

Add `-r app/requirements/whisper.txt` (and `diarize.txt`, `local-summary.txt`) only if you work
on local transcription, diarization or DistilBART; they pull in PyTorch.

## Running the app from source

```bash
bash scripts/prepare-frontend.sh                     # copies the frontend into dist-tauri/
export PATH="/opt/homebrew/opt/rustup/bin:$PATH"     # if rustup came from Homebrew
npx --yes @tauri-apps/cli@2.12 dev
```

- **Dev builds keep their own data.** A debug build (`tauri dev`, `cargo build`) passes
  `PLAUD_DATA_DIR=~/Library/Application Support/Plaud V2 (Dev)` to the backend, so it never reads
  or writes the installed app's recordings, config or tokens. Set `PLAUD_DATA_DIR` yourself to use
  another folder; it wins in debug and release builds. Don't point it into the repo or any synced folder:
  the folder holds API keys and device tokens, and synced folders would copy them.
- Debug builds also keep their own shell state in `~/Library/Application Support/com.plaud.v2.dev/`
  (venv, lockfiles, `server.pid`, `tauri-prefs.json`, `updates/`, `diagnostics.log`) and log to
  `~/Library/Logs/Plaud V2 (Dev)/`. They share only the downloaded Python runtime
  (`com.plaud.v2/python/`) and FFmpeg (`com.plaud.v2/bin/`) with the installed app. The first dev
  launch builds its own venv, so it takes a minute. All of this is in `src-tauri/src/paths.rs`.
- **Quit the installed app first.** Both builds have the bundle id `com.plaud.v2`, so they share
  the single-instance lock (`/tmp/com_plaud_v2_si.sock`): starting a dev build while the installed
  app runs only brings the installed app's window forward and exits. They also share the webview's
  storage (`~/Library/WebKit/com.plaud.v2`).
- Tauri copies the backend into `src-tauri/target/<profile>/app/` and runs that copy, not `app/`,
  so rebuild (or re-run `tauri dev`) after editing Python.
- There is no `beforeDevCommand` or dev server: the window loads `dist-tauri/`. Re-run
  `prepare-frontend.sh` (and reload with Cmd+R) after editing HTML, CSS or JS.
- The app sends no crash reports. **Export bug report** in a dev build marks the report
  "Build: dev" and reads the dev logs and data folder.
- `RUST_LOG=debug` (or e.g. `RUST_LOG=warn,plaud_v2::python=debug`) raises log levels; debug
  builds also log to stderr.

### Backend only

```bash
PLAUD_DATA_DIR="$HOME/Library/Application Support/Plaud V2 (Dev)" python app/api_server.py
curl -s http://127.0.0.1:5123/health
```

`app/utils/constants.py` picks the data folder as: `PLAUD_DATA_DIR` if set, otherwise the installed
app's `~/Library/Application Support/Plaud V2/`, even from a checkout. There is no "dev mode"
guess, so always set `PLAUD_DATA_DIR` when running the backend by hand. Without `PLAUD_API_TOKEN` the token check is off (it logs a warning);
the CORS and Host checks still apply, so only `curl`-style clients can use it, not a browser page.

## Tests

```bash
python -m pytest                                  # Python suite (~750 tests, seconds)
PLAUD_SLOW_TESTS=1 python -m pytest tests/test_python_runtime.py -k ForReal   # installs core.txt into a clean venv (network, minutes)

bash scripts/prepare-frontend.sh                  # cargo needs dist-tauri/ and keys/ to exist
(cd src-tauri && cargo test --lib)                # Rust unit tests

(cd infra/update-worker && node --test test/)     # Worker tests, no network
```

- `tests/conftest.py` points `PLAUD_DATA_DIR` at a temporary folder before anything imports the
  app, and refuses to run if that failed, so tests never touch real data.
- Ignored Rust tests, run by hand: `cargo test --lib -- --ignored live_worker` (hits the deployed
  Worker) and
  `PLAUD_RUNTIME_EXERCISE_HOME=/tmp/scratch cargo test --lib python::tests::exercise_runtime_setup -- --ignored --nocapture`
  (real Python download and venv build in a scratch HOME).
- There are no JavaScript tests and no CI. Run the suites above before merging.

## Lint and format

```bash
ruff check app/ src/ tests/
ruff format app/ src/ tests/
```

Settings are in `pyproject.toml` (Python 3.12 target, line length 120).

## Gotchas

- **One global scope for the frontend.** `app/tauri-bridge.js`, `app/renderer.js` and
  `app/ui/*.js` are plain `<script>` tags, so every top-level `const`, `let`, `function` and
  `class` shares one namespace. A name declared in two files is a `SyntaxError` that stops the
  whole UI from loading (v2.1.5 shipped one). Use names unique across files, or keep helpers
  inside a function.
- **Config writes:** never hold a config dict across a slow call and save it later; use
  `update_config()` in `app/utils/config.py`.
- **Session IDs from requests** go through `app/utils/paths.py` before touching the filesystem.
- **Model IDs** live only in `app/providers/_models.py`.
- **Python version:** the app accepts only CPython 3.12 on `aarch64` (`REQUIRED_PYTHON` and
  `SUPPORTED_ARCH` in `python.rs`, and the lockfiles). Change them together;
  `tests/test_tauri_parity.py` checks they agree.
- **New backend files** under `app/routes`, `state`, `providers` or `utils` are bundled
  automatically; anything elsewhere (including a new `src/` module, which are listed one by one)
  needs adding to `bundle.resources` in `tauri.conf.json` and to `scripts/prepare-frontend.sh`.
  `tests/test_tauri_parity.py` checks that every `src/` module the backend imports is bundled.
- **Device protocol:** the BLE handshake uses the per-device **BIND_TOKEN**, not the static APP_KEY
  that appears in old notes.
- **Pairing backup** (`app/utils/pairing_store.py`): the device credentials are also kept in the
  login Keychain, service "Plaud V2 pairing" for the installed app's data folder and "Plaud V2 (Dev)
  pairing" for any other (debug builds, `PLAUD_DATA_DIR`). `PLAUD_KEYCHAIN_BACKUP=0` turns it off;
  `tests/conftest.py` sets that, and `tests/test_pairing_backup.py` fakes `security`.

## Python dependencies

The app installs packages only from the lockfiles in `app/requirements/`. To add or upgrade one:

1. Edit the input in `scripts/python-deps/` (`core.in`, or the `whisper`, `diarize`,
   `local-summary` extras).
2. Run `scripts/lock-python-deps.sh` (needs uv). It resolves for CPython 3.12 on Apple Silicon,
   wheels only, with hashes, each extra constrained by the files before it.
3. Commit the `.in` and `.txt` changes together.

`core.txt` is compiled into the app binary. A release that changes it makes every install
rebuild its venv on the next launch (the venv stores the lockfile's hash). Extras are installed
the first time a feature needs them.

## Releasing

See [RELEASING.md](RELEASING.md).
