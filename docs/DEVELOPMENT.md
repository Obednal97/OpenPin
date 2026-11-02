# Development

Running Plaud V2 from source, testing, linting and the gotchas. Read
[ARCHITECTURE.md](ARCHITECTURE.md) first for how the pieces fit.

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
Copy them from an existing checkout; they aren't in the repo.

## Project layout

```
app/                    Python backend + frontend (one folder; the build splits them)
  api_server.py         Flask app and request security
  routes/ state/ providers/ utils/    backend modules
  requirements/         hash-pinned lockfiles (generated)
  index.html renderer.js tauri-bridge.js ui/ css/ assets/   frontend
src/                    shared Python: crypto, BLE scanner, local Whisper + diarization
src-tauri/              Rust shell (Tauri 2): src/, tauri.conf.json, Entitlements.plist, Info.plist
infra/update-worker/    Cloudflare Worker for updates and crash reports
scripts/                build helpers and legacy device scripts (see scripts/README.md)
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
PLAUD_DATA_DIR=~/plaud-dev-data npx --yes @tauri-apps/cli@2.12 dev
```

- **Always set `PLAUD_DATA_DIR`.** A Tauri build copies the backend into
  `src-tauri/target/<profile>/app/` and runs that copy. It has no `.git` next to it, so
  `app/utils/constants.py` treats it as packaged and would use your real
  `~/Library/Application Support/Plaud V2/` (recordings, config, tokens). `PLAUD_DATA_DIR` keeps
  dev data separate.
- There is no `beforeDevCommand` or dev server: the window loads `dist-tauri/`. Re-run
  `prepare-frontend.sh` (and reload with Cmd+R) after editing HTML, CSS or JS.
- The Rust side always uses the real `~/Library/Application Support/com.plaud.v2/` for the Python
  runtime and venv, and `~/Library/Logs/Plaud V2/` for logs, dev or not.
- Dev builds send crash reports too, tagged `[DEV]`, unless `crash_reports_enabled` is `false` in
  the real config (the Rust side reads only that file).
- `RUST_LOG=debug` (or e.g. `RUST_LOG=warn,plaud_v2::python=debug`) raises log levels; debug
  builds also log to stderr.

### Backend only

```bash
PLAUD_DATA_DIR=~/plaud-dev-data python app/api_server.py
curl -s http://127.0.0.1:5123/health
```

Run straight from the checkout, the default data folder is the repo root, so set
`PLAUD_DATA_DIR` here too. Without `PLAUD_API_TOKEN` the token check is off (it logs a warning);
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
  automatically; anything elsewhere needs adding to `bundle.resources` in `tauri.conf.json` and to
  `scripts/prepare-frontend.sh`.
- **Device protocol:** the BLE handshake uses the per-device **BIND_TOKEN**, not the static APP_KEY
  that appears in old notes and `src/client.py`.

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
