# Plaud V2 — Comprehensive Refactor & Code Quality Plan

## Current State Audit

### File Metrics
| File | Lines | Purpose |
|------|-------|---------|
| `app/api_server.py` | 2,983 | Monolithic Flask API — BLE, cloud, Notion, transcription, settings |
| `app/renderer.js` | 2,318 | All frontend logic in one file |
| `app/styles.css` | 1,371 | Design system + all component styles |
| `app/index.html` | 575 | All UI markup |
| `app/main.js` | 304 | Electron main process |
| `app/preload.js` | 95 | contextBridge API exposure |
| `src/transcribe.py` | 609 | Transcription + diarization cascade |

### Code Smell Metrics
- **42 Flask routes** in a single file
- **89 Python functions** in a single file
- **22 calls to `load_config()`** — config loaded fresh on every request
- **114 `toast()` calls** — most wrapped in identical try/catch patterns
- **49 try/catch blocks** in renderer.js — most with identical error handling
- **36 inline styles** in HTML that should be CSS classes
- **0 tests**, **0 linting**, **0 type checking**

### Route Distribution (natural module boundaries)
| Prefix | Routes | Target Module |
|--------|--------|---------------|
| `/device/*` | 11 | `routes/device.py` |
| `/cloud/*` | 10 | `routes/cloud.py` |
| `/notion/*` | 7 | `routes/notion.py` |
| `/recordings/*` | 5 | `routes/recordings.py` |
| `/transcripts/*` | 2 | `routes/recordings.py` |
| `/settings` | 2 | `routes/settings.py` |
| `/transcribe` | 1 | `routes/transcribe.py` |
| `/summarize` | 1 | `routes/transcribe.py` |
| `/validate-key` | 1 | `routes/settings.py` |
| `/health`, `/audio` | 2 | `routes/core.py` |

---

## Phase 1: Python Backend Modularisation

### Goal
Split `api_server.py` (2,983 lines) into focused modules. Target: no file over 400 lines.

### Step 1.1: Extract Shared Utilities

**Investigation:**
- Identify all shared patterns: config I/O, webhook firing, HTTP error handling, cloud API calls
- Map which functions are called from multiple routes

**Create:**
- `app/utils/config.py` — `load_config()`, `save_config()`, config path constants. Singleton pattern so config is loaded once and cached, invalidated on save.
- `app/utils/webhooks.py` — `fire_webhooks(event_type, payload)`
- `app/utils/cloud_client.py` — `PlaudCloudClient` class wrapping `_cloud_state`, `_cloud_api()`, token management, upload/download
- `app/utils/constants.py` — BLE UUIDs, API domains, Whisper models, default tags

**Validation:**
- All imports resolve correctly
- `python3 -c "from app.utils.config import load_config"` works
- Server starts without errors

### Step 1.2: Extract State Managers

**Investigation:**
- Map all module-level state variables (`_device_state`, `_cloud_state`, `_notion_state`, `_notion_session_id`)
- Identify thread safety requirements (BLE operations are async, webhooks are threaded)

**Create:**
- `app/state/device.py` — `DeviceManager` class encapsulating BLE connection, handshake, file operations, depair
- `app/state/cloud.py` — `CloudManager` class encapsulating Plaud cloud login, sync, upload
- `app/state/notion.py` — `NotionMCPClient` class encapsulating OAuth, session management, MCP calls

**Validation:**
- State isolation: each manager owns its state, no global dicts
- Thread safety: BLE operations still use the dedicated asyncio event loop
- All existing API behaviour preserved

### Step 1.3: Extract Route Modules

**Investigation:**
- Group routes by prefix (see table above)
- Identify shared dependencies per group

**Create using Flask Blueprints:**
- `app/routes/core.py` — `/health`, `/audio/<path>`
- `app/routes/device.py` — all `/device/*` routes
- `app/routes/cloud.py` — all `/cloud/*` routes
- `app/routes/notion.py` — all `/notion/*` routes
- `app/routes/recordings.py` — `/recordings/*`, `/transcripts/*`
- `app/routes/transcribe.py` — `/transcribe`, `/summarize`, `/validate-key`
- `app/routes/settings.py` — `/settings` GET/POST

**Refactor `api_server.py` to:**
```python
from flask import Flask
from app.routes import core, device, cloud, notion, recordings, transcribe, settings

app = Flask(__name__)
app.register_blueprint(core.bp)
app.register_blueprint(device.bp, url_prefix='/device')
# ... etc
```

**Validation:**
- `curl http://localhost:5123/health` returns OK
- Every existing endpoint responds identically
- Run a full manual test: scan device, connect, sync, transcribe, summarize, cloud sync, Notion push

### Step 1.4: Extract AI Provider Logic

**Investigation:**
- Map all summarization functions (`_summarize_anthropic`, `_summarize_openai`, `_summarize_local`)
- Map transcription paths (OpenAI API vs local Whisper)
- Identify the fallback cascade logic

**Create:**
- `app/providers/summarize.py` — `summarize(text, config)` with internal cascade
- `app/providers/transcribe.py` — `transcribe(path, config)` with internal cascade
- `app/providers/validate.py` — key validation logic

**Validation:**
- Summarize still cascades: Anthropic → OpenAI → Local
- Transcribe still uses OpenAI when available, falls back to local
- Background diarization still works
- Profile context and vocabulary still injected into prompts

---

## Phase 2: Frontend Modularisation

### Goal
Split `renderer.js` (2,318 lines) into focused modules. Target: no file over 400 lines.

### Step 2.1: Extract Shared UI Utilities

**Create `app/js/utils.js`:**
- `formatTime()`, `formatDate()`, `formatDuration()`
- `escapeHtml()`, `renderMarkdown()`
- `toast()`, `showProgress()`, `hideProgress()`, `setLoading()`
- `$(sel)`, `$$(sel)` — DOM helpers

**Create `app/js/api.js`:**
- Centralised API call wrapper (currently in preload.js)
- Error handling wrapper that auto-toasts on failure

### Step 2.2: Extract UI Modules

**Create:**
- `app/js/recordings.js` — recording list rendering, search, filtering, select mode, delete dialog
- `app/js/transcript.js` — transcript view, audio player, tag picker, rename, copy, summarize, Notion push
- `app/js/settings.js` — all settings tab rendering and event handlers
- `app/js/device.js` — device status polling, auto-connect, background sync
- `app/js/theme.js` — theme toggle, system theme detection

**Validation:**
- Use ES modules with `<script type="module">` in index.html
- All existing UI behaviour preserved
- Test: navigate all views, open/close settings tabs, play audio, search, select/delete

### Step 2.3: Extract Reusable Components

**Identify repeated UI patterns:**
- Connected/disconnected card (used for: device, Notion, Plaud Cloud, AI keys) — extract as `renderConnectionCard(config)`
- Toggle with label (used ~10 times) — already CSS, but JS handling is repeated
- Token input + save row (used for: cloud token, Notion URL, webhook URL, API keys)

**Create `app/js/components.js`:**
- `createDeleteDialog(options)` — the modal dialog
- `createConnectionCard(options)` — connected/disconnected state card
- `createTokenInput(options)` — input + validate + save pattern

### Step 2.4: Clean Up HTML

**Investigation:**
- Audit all 36 inline styles — convert to CSS classes
- Identify structurally identical settings sections

**Execute:**
- Replace all `style="..."` with semantic CSS classes
- Add CSS utility classes where needed (e.g., `.mt-3`, `.w-full`, `.flex-1`)

**Validation:**
- Visual regression check: screenshot before and after, diff
- No inline styles remaining in HTML

---

## Phase 3: CSS Cleanup

### Goal
Enforce design token usage, remove duplication, split into logical files.

### Step 3.1: Audit Token Compliance

**Investigation:**
- Find all hardcoded colors (not using `var(--*)`)
- Find all hardcoded sizes (not using `var(--sp-*)` or `var(--radius-*)`)
- Find all hardcoded font properties (not using `var(--font)` etc.)

**Execute:**
- Replace every hardcoded value with the appropriate token
- Add any missing tokens

### Step 3.2: Split CSS Files

**Create:**
- `app/css/tokens.css` — `:root` variables, dark/light mode
- `app/css/reset.css` — reset, base body styles, scrollbars
- `app/css/components.css` — buttons, inputs, toggles, badges, tags, checkboxes, toasts
- `app/css/layouts.css` — header, action bar, settings panel, settings tabs
- `app/css/recordings.css` — recording list, cards, search bar, select bar
- `app/css/transcript.css` — transcript view, audio player, segments, tabs
- `app/css/settings-modules.css` — device manager, AI keys, cloud account, Notion, webhooks, profile

**`styles.css` becomes:**
```css
@import 'css/tokens.css';
@import 'css/reset.css';
@import 'css/components.css';
@import 'css/layouts.css';
@import 'css/recordings.css';
@import 'css/transcript.css';
@import 'css/settings-modules.css';
```

**Validation:**
- Visual regression: app looks identical
- No broken selectors

---

## Phase 4: Testing

### Step 4.1: Python Test Infrastructure

**Setup:**
```bash
pip install pytest pytest-mock pytest-asyncio
```

**Create `tests/conftest.py`:**
- Flask test client fixture
- Mock config fixture (temp file)
- Mock BLE device fixture

### Step 4.2: Python Unit Tests

**`tests/test_config.py`:**
- `test_load_config_creates_default` — no config file → returns {}
- `test_save_and_load_roundtrip` — save dict, load it back, matches
- `test_deleted_ids_tracking` — delete recording, verify ID in deleted_ids
- `test_uploaded_session_ids_tracking` — upload, verify session ID tracked

**`tests/test_transcribe.py`:**
- `test_merge_same_speaker` — consecutive same-speaker segments merge
- `test_merge_speaker_change` — different speakers split
- `test_split_long_segment` — 2-minute block splits at ~30s sentence boundaries
- `test_split_preserves_sentences` — never splits mid-sentence
- `test_extract_recording_date_unix` — Unix timestamp session ID → correct datetime
- `test_extract_recording_date_cloud` — `cloud_2026-03-27_1234_...` → correct datetime
- `test_vocabulary_prompt` — custom vocab list → comma-separated string

**`tests/test_cloud.py`:**
- `test_download_skips_deleted` — deleted IDs not re-downloaded
- `test_download_skips_uploaded` — uploaded filenames not re-downloaded
- `test_upload_unwraps_ogg` — OGG file → raw Opus frames
- `test_upload_duration_in_ms` — duration converted to milliseconds
- `test_upload_scene_is_1` — scene defaults to 1

**`tests/test_summarize.py`:**
- `test_sanitize_for_json` — special chars escaped
- `test_profile_context_injection` — profile fields appear in prompt
- `test_tag_context_injection` — tags appear in prompt
- `test_cascade_fallback` — Anthropic fails → OpenAI tried → Local tried

### Step 4.3: Python Integration Tests

**`tests/test_api.py`:**
- `test_health_endpoint` — returns {"status": "ok"}
- `test_settings_roundtrip` — POST settings, GET them back
- `test_recordings_list` — returns list with expected fields
- `test_delete_recording` — delete, verify gone from list and in deleted_ids
- `test_validate_key_invalid` — bad key returns {"valid": false}

### Step 4.4: Frontend Tests

**Setup:**
```bash
npm install --save-dev vitest jsdom
```

**`tests/js/utils.test.js`:**
- `test_formatTime` — 0 → "00:00", 65 → "01:05", 3661 → "1:01:01"
- `test_formatDate` — today → "Today at HH:MM", yesterday → "Yesterday at..."
- `test_formatDuration` — 5 → "5s", 90 → "1m 30s", 3700 → "1h 1m"
- `test_escapeHtml` — `<script>` → `&lt;script&gt;`
- `test_renderMarkdown` — headings, bold, lists, code, blockquotes

### Step 4.5: E2E Tests (Future)

**Setup:**
```bash
npm install --save-dev playwright
```

**`tests/e2e/app.spec.js`:**
- App launches and shows recordings list or empty state
- Settings panel opens and closes
- Tab navigation works in settings
- Search filters recordings
- Theme toggle cycles light → dark → auto

---

## Phase 5: Linting & Formatting

### Step 5.1: Python Linting

**Setup:**
```bash
pip install ruff pyright
```

**Create `pyproject.toml`:**
```toml
[tool.ruff]
target-version = "py312"
line-length = 120
select = ["E", "F", "W", "I", "UP", "B", "SIM", "C4"]
ignore = ["E501"]  # line length handled separately

[tool.ruff.format]
quote-style = "double"

[tool.pyright]
typeCheckingMode = "basic"
pythonVersion = "3.12"
```

**Process:**
1. Run `ruff check app/ src/ --fix` — auto-fix safe issues
2. Run `ruff format app/ src/` — format all Python
3. Run `pyright app/ src/` — check types, fix critical issues
4. Manual review of remaining warnings

### Step 5.2: JavaScript Linting

**Setup:**
```bash
npm install --save-dev eslint prettier eslint-config-prettier
```

**Create `.eslintrc.json`:**
```json
{
  "env": {"browser": true, "es2022": true, "node": true},
  "extends": ["eslint:recommended", "prettier"],
  "parserOptions": {"ecmaVersion": 2022, "sourceType": "module"},
  "rules": {
    "no-unused-vars": "warn",
    "no-console": "off",
    "prefer-const": "error",
    "eqeqeq": "error"
  }
}
```

**Create `.prettierrc`:**
```json
{
  "singleQuote": true,
  "trailingComma": "all",
  "printWidth": 100,
  "semi": true
}
```

**Process:**
1. Run `npx prettier --write app/*.js app/*.html app/*.css`
2. Run `npx eslint app/*.js --fix`
3. Manual review of remaining warnings

### Step 5.3: CSS Linting

**Setup:**
```bash
npm install --save-dev stylelint stylelint-config-standard
```

**Create `.stylelintrc.json`:**
```json
{
  "extends": "stylelint-config-standard",
  "rules": {
    "custom-property-pattern": null,
    "selector-class-pattern": null,
    "declaration-block-no-redundant-longhand-properties": null
  }
}
```

---

## Phase 6: Pre-commit & CI

### Step 6.1: Pre-commit Hooks

**Create `.pre-commit-config.yaml`:**
```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.8.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-prettier
    rev: v4.0.0
    hooks:
      - id: prettier
        types_or: [javascript, css, html]
  - repo: https://github.com/pre-commit/mirrors-eslint
    rev: v9.0.0
    hooks:
      - id: eslint
        files: \.js$
```

### Step 6.2: Package.json Scripts

```json
{
  "scripts": {
    "start": "electron .",
    "lint": "eslint app/*.js && stylelint app/*.css",
    "lint:py": "ruff check app/ src/",
    "format": "prettier --write app/*.js app/*.html app/*.css && ruff format app/ src/",
    "test": "vitest run",
    "test:py": "pytest tests/ -v",
    "test:all": "npm run test && npm run test:py"
  }
}
```

---

## Execution Order & Dependencies

```
Phase 1.1 (Extract Utils)
    ↓
Phase 1.2 (Extract State Managers)
    ↓
Phase 1.3 (Extract Route Modules)    →  Phase 4.2 (Python Unit Tests)
    ↓                                         ↓
Phase 1.4 (Extract AI Providers)      Phase 4.3 (Python Integration Tests)
    ↓
Phase 5.1 (Python Linting)
    ↓
Phase 2.1 (Extract JS Utils)
    ↓
Phase 2.2 (Extract JS Modules)       →  Phase 4.4 (JS Unit Tests)
    ↓
Phase 2.3 (Extract JS Components)
    ↓
Phase 2.4 (Clean HTML)
    ↓
Phase 3 (CSS Cleanup)
    ↓
Phase 5.2 (JS Linting)
Phase 5.3 (CSS Linting)
    ↓
Phase 6 (Pre-commit & CI)
    ↓
Phase 4.5 (E2E Tests — Future)
```

## Per-Phase Process

For each phase:

1. **Investigation** — Read all affected files, map dependencies, identify patterns
2. **Plan** — Define exact file splits, function moves, import changes
3. **Execute** — Make the changes, one module at a time
4. **Validate** — Run the app, test affected functionality manually
5. **Test** — Write/run automated tests for the changed code
6. **Confirm** — Full app restart, verify no regressions

## Success Criteria

- [ ] No file over 400 lines
- [ ] Zero hardcoded colors/sizes in CSS (all tokens)
- [ ] Zero inline styles in HTML
- [ ] All Python tests pass (`pytest tests/ -v`)
- [ ] All JS tests pass (`npm test`)
- [ ] Ruff reports zero errors (`ruff check app/ src/`)
- [ ] ESLint reports zero errors (`npx eslint app/*.js`)
- [ ] Prettier reports no changes needed
- [ ] App starts and all features work end-to-end
