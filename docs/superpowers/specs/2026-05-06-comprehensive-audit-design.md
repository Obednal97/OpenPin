# Comprehensive Codebase Audit — Design Spec

**Date:** 2026-05-06
**Status:** Approved, ready for plan generation
**Output:** A phased fix plan (`2026-05-06-comprehensive-audit-plan.md`) that an executing-plans session can run end-to-end.

---

## 1. Goal & Scope

### Goal
Produce a phased, prioritized fix plan covering bugs, security issues, dead code, inconsistencies, doc drift, architectural smells, and test gaps across the entire Plaud V2 codebase. The plan is the deliverable; fixes happen in a separate executing-plans session.

### In scope (all five surfaces)

| Surface | Path | What lives there |
|---|---|---|
| Tauri Rust shell | `src-tauri/` | IPC commands, health-gate, diagnostics, recent Electron-removal work |
| Frontend | `app/` | Renderer, UI modules, bug-reporter, settings, transcript view |
| Python backend | `src/` + root `.py` files | Flask API server, BLE protocol, audio decryption, transcription, cloud sync, Notion MCP |
| Scripts | `scripts/` | All committed scripts plus the 19 untracked experimental ones (`depair_v*.py`, `wifi_restore_*.py`, `test_hotspot_*.py`, etc.) |
| Build / config / docs | Root configs, `docs/` | `Cargo.toml`, `pyproject.toml`, `tauri.conf.json`, `package.json`, `README.md`, `RELEASING.md`, `docs/ARCHITECTURE.md`, `docs/TAURI_MIGRATION.md` |

### Off-limits (deferred unless something egregious surfaces)
- The reverse-engineered protocol itself (BLE UUIDs, ChaCha20 frame format, RSA key handling) — assumed correct; only flag if code diverges from documented behavior.
- Vendored crypto code and third-party libraries.
- Runtime data directories: `recordings/`, `sessions/`, `transcripts/`, `keys/`.

### Recent commits — fair game
The last ~10 commits (health-gate, bug-reporter, Electron removal, v2.1.4 bump) are explicitly in scope. Fresh review may catch issues that slipped through.

### Cross-cutting checks (run as part of synthesis, not delegated)
- IPC contract consistency between Rust commands in `src-tauri/` and `invoke()` calls in `app/`.
- Config-key drift across `.plaud_config.json`, `.plaud_v2_config.json`, `.plaud_v2_config.json.bak`, and the readers/writers in Rust + Python + JS.
- Doc-vs-code drift in `README.md`, `docs/ARCHITECTURE.md`, `docs/TAURI_MIGRATION.md`.
- Electron leftovers — recent commits removed shells; grep for stragglers.
- Token/secret hygiene — `.env`, `.hf_token`, `.notion_mcp.json`, hardcoded tokens in source.

---

## 2. Audit Methodology (Hybrid)

### Phase A — Mechanical sweep (run directly, fast)
- Grep markers: `TODO|FIXME|XXX|HACK|@ts-ignore|# type: ignore|\.unwrap\(\)|panic!`
- Grep Electron leftovers: `electron|ipcRenderer|contextBridge|nodeIntegration`
- Grep hardcoded URLs, tokens, magic strings.
- Run `ruff check` against Python (config in `pyproject.toml`).
- Run `cargo clippy` against `src-tauri/`.
- Inspect `app/` for ESLint/Prettier config; absence = `BUILD` finding.
- Enumerate untracked files via `git ls-files --others --exclude-standard` for triage.
- Diff `.plaud_config.json` ↔ `.plaud_v2_config.json` ↔ `.plaud_v2_config.json.bak` to surface key drift.

### Phase B — Parallel agent dispatch (single message, four agents in parallel)

| Agent | Subagent type | Surface | Brief |
|---|---|---|---|
| 1 | `feature-dev:code-reviewer` | Python backend (`src/`, root `.py` files, `api_server.py` if present) | Bugs, dead code, error-handling gaps, monolithic-file problems, inconsistencies, state-management hazards in BLE/cloud/Notion classes |
| 2 | `feature-dev:code-reviewer` | Tauri Rust shell (`src-tauri/`) | IPC command bugs, panic/unwrap risks, error-propagation gaps, health-gate race conditions, leftover Electron-era shims |
| 3 | `feature-dev:code-explorer` | Scripts triage (`scripts/`) | Per script: classify as (a) production utility, (b) one-off experiment safe to delete, (c) test candidate worth promoting. Flag any scripts referenced by other code |
| 4 | `feature-dev:code-reviewer` | Build / config / docs | Version drift, dependency staleness, doc-vs-code mismatches, missing CI, missing license, secret leakage in committed files |

`Explore` is deliberately NOT used: its tool description warns against using it for code review, design-doc auditing, or cross-file consistency checks — exactly this audit's purpose.

Each agent returns findings as: file path, line, category, severity, description, recommended fix.

### Phase C — Personal audit (after agents return)
- **Frontend (`app/`):** Read renderer, UI modules, bug-reporter, settings, transcript view. Look for runtime bugs, async-handling gaps, IPC misuse, accessibility issues, state-management hazards, leftover Electron globals.
- **IPC contract consistency:** Diff Rust command names/signatures (from agent 2) vs `invoke()` calls in `app/`.
- **Config-key drift:** Compare what each runtime reads/writes.
- **Recent-commit regression risk:** Read the last ~10 commits' diffs; look for things merged too fast.

### Phase D — Synthesis
Dedupe, prioritize, group findings into phases per the rules in §3. Cross-surface findings become top-level entries.

### Stop conditions / escape hatches
- Agent surfaces a cross-surface issue → I escalate to my own queue, don't ask the agent to chase.
- A finding is actually a redesign (e.g., "split `api_server.py` into modules") → its own design doc, not crammed into the plan.
- Total findings >~50 → Phase 5 splits into a separate spec.
- Memory contradicts current code → update or delete the stale memory rather than acting on it (the 39-day-old `project_refactor_plan.md` is already a candidate — was written when the app was Electron + monolithic JS).

---

## 3. Finding Taxonomy

Each finding gets one **category** and one **severity**.

### Categories

| Tag | Meaning |
|---|---|
| `BUG` | Code that doesn't work as intended — runtime errors, wrong outputs, broken happy paths |
| `LATENT` | Works today, breaks under specific conditions — race, edge-case input, missing error path, panic risk |
| `SECURITY` | Token leakage, unsafe deserialization, injection, missing auth, insecure defaults |
| `INCONSISTENCY` | Same thing done two ways, naming drift, conflicting conventions, contract mismatch (Rust ↔ frontend) |
| `DEAD` | Unused code, unreferenced files, leftover Electron shims, throwaway scripts |
| `DOC_DRIFT` | Comment/docstring/README/design-doc contradicts current code |
| `TODO` | Explicit `TODO`/`FIXME`/`XXX`/`HACK` markers |
| `ARCH` | God object, tight coupling, monolith file, layering violation |
| `TEST_GAP` | Critical path without test coverage |
| `STYLE` | Lint/format violations, missing types, low-effort hygiene |
| `BUILD` | Dependency staleness, version drift, missing CI/lint config, missing release-process step |

### Severities

| Level | Meaning |
|---|---|
| `P0` | Ship-stopper — corrupts data, crashes the app, leaks secrets, already broken in production |
| `P1` | Real bug or hazard, fix soon — wrong behavior under realistic conditions, security concern, broken UX |
| `P2` | Worth fixing — cleanup wins, real inconsistencies, doc drift on important docs |
| `P3` | Nits — style, naming nits, drift on internal docs, low-effort hygiene |

### Phasing rule (drives the fix-plan structure)

| Phase | Includes |
|---|---|
| **Phase 1 — Bugs & correctness** | All `BUG` + all `LATENT` at P0/P1 |
| **Phase 2 — Security & data integrity** | All `SECURITY`, plus any `BUG`/`LATENT` involving recordings/keys/config persistence |
| **Phase 3 — Dead code & inconsistencies** | `DEAD` (incl. scripts/ triage), `INCONSISTENCY` at P1/P2, `LATENT` at P2, IPC contract fixes |
| **Phase 4 — Doc/comment drift** | `DOC_DRIFT` + `TODO` resolution |
| **Phase 5 — Architectural smells & test gaps** | `ARCH` + `TEST_GAP` — likely spins off into separate specs if a fix > ~1 day |
| **Appendix — Deferred** | All `P3` items (incl. `LATENT` P3) + anything deliberately ruled out, with reasoning |

`STYLE` and `BUILD` fold into whichever phase is most natural (typically 3 or 4).

---

## 4. Deliverable Structure

### Two artifacts
1. **This design spec** — `docs/superpowers/specs/2026-05-06-comprehensive-audit-design.md` (committed when finalized).
2. **The fix plan** — `docs/superpowers/specs/2026-05-06-comprehensive-audit-plan.md` (produced by writing-plans skill, after this design is approved).

### Per-finding format inside the plan

> ⚠️ The block below is a **format template** — `F-042` and the file path are illustrative, not real findings.

```markdown
### F-NNN — <one-line summary>
- **File:** `<path>:<line>`
- **Category:** <BUG | LATENT | SECURITY | INCONSISTENCY | DEAD | DOC_DRIFT | TODO | ARCH | TEST_GAP | STYLE | BUILD>
- **Severity:** <P0 | P1 | P2 | P3>
- **Phase:** <1 | 2 | 3 | 4 | 5 | Appendix>
- **Description:** <what's wrong, why it matters, how it manifests>
- **Recommended fix:** <concrete change, options if more than one>
- **Effort:** <S | M | L>
- **Verification:** <required for P0/P1, optional otherwise>
```

- `F-NNN` is a stable ID for cross-referencing across phases.
- `Effort` is one of `S` (≤1h), `M` (≤4h), `L` (>4h or needs investigation).
- `Verification` is required for P0/P1, optional for P2/P3.

### Plan front-matter (top of the plan doc)
- Total findings count, broken down by category × severity.
- Per-phase total effort estimate.
- Critical-path callouts (e.g., "Phase 2 must complete before next release").
- "Deferred to separate spec" list — any architectural finding too big for the plan.

### Granularity (decided)
**One commit per finding.** Trivially-related findings may be grouped; defaulting to one-finding-per-commit keeps the executing-plans session traceable and reversible.

---

## 5. Non-Goals & Escape Hatches

### Explicit non-goals (the audit will NOT do these)
1. **No new features.** Missing functionality becomes a `TODO` finding for later, not a slipped-in feature.
2. **No protocol re-derivation.** Trust documented BLE/audio work; only flag if code diverges from docs.
3. **No "while I'm here" refactoring.** Findings describe what to change and why; fixes happen in executing-plans.
4. **No reformatting the entire codebase.** Missing lint/format config is one `BUILD` finding, not 800 style findings.
5. **No fixing during the audit.** Even obvious one-line bugs — write them up, don't fix.
6. **No churning settled APIs.** Awkward but widely-used names get noted, not renamed, unless actively misleading.

### Escape hatches
- Finding count >~50 → Phase 5 splits to a separate spec.
- Investigation deeper than ~1 hour → write as `needs-investigation` finding with current state, don't block.
- Cross-cutting issue spanning all surfaces → top-level entry, not nested.
- Low-confidence agent finding → I review and promote, demote to P3, or drop with reasoning recorded.
- Stale memory contradicting code → update or delete the memory.

### Stop condition for the audit
All four agents return + personal passes complete + cross-cutting checks done + synthesis complete = ready to invoke writing-plans.

### Stop condition for this brainstorming session
This spec written, committed, user-approved. Then writing-plans is invoked.

---

## Acceptance Criteria for This Design (all met)
- [x] Scope locked (§1)
- [x] Methodology locked (§2)
- [x] Taxonomy locked (§3)
- [x] Deliverable format locked (§4)
- [x] Non-goals & escape hatches locked (§5)
