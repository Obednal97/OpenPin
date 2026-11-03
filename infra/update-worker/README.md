# plaud-v2-updates (Cloudflare Worker)

Fronts every GitHub call the Open Pin app makes, so the DMG ships **no GitHub token**.
The token lives only as a Worker secret and can be rotated without an app release.

Deployed at **https://plaud-v2-updates.leave-hacker-feedback.workers.dev**
(the hostname is hard-coded as `WORKER_BASE` in `src-tauri/src/commands/mod.rs`). The Worker
keeps the app's pre-rename name (Plaud V2): every installed copy calls this URL, so renaming the
Worker would need a new route and an app release, and old installs would still call the old one.
The repository it reads releases from is `REPO` in `src/index.js`.

## Endpoints

| Method + path | What it does |
|---|---|
| `GET /v1/latest` | Latest published release (GitHub `releases/latest`, so no drafts or pre-releases): `{version, tag, name, notes, published_at, asset: {name, size, sha256 \| null}, download_url}`. `sha256` comes from a `<dmg name>.sha256` asset (or `SHA256SUMS`) on the release, else `null`. Cached 5 min in a per-isolate memory cache, with the Cache API as a best-effort second layer (see "Caching"); the `X-Plaud-Cache` header says which (`miss`, `cache`, `memory`). |
| `GET /v1/download/<tag>/<asset>` | Looks the asset up on that release and answers **302** to GitHub's short-lived signed URL (no token in it), so the DMG never streams through the Worker. Only `.dmg` and `.sha256` assets; anything unknown is 404. |
| `POST /v1/report` | Bug report → GitHub Issues. Body: `{kind?: "auto"\|"user", fingerprint, title, body, app_version, os, dry_run?}`. Limits: title ≤ 200 chars, body ≤ 20 KB, fingerprint 1–64 hex chars (required for `auto`), whole request ≤ 32 KB. `auto` reports look for an open issue **this Worker created** for the fingerprint (search API, then the 100 newest open `auto-bug` issues, because search lags) and comment on it, else create one with label `auto-bug`. `user` reports always create an issue labelled `user-report`. `dry_run: true` validates and searches but writes nothing. Returns `{ok, action, issue_number, url}` (`action`: `created`, `commented`, `suppressed`, `would_create`, `would_comment`). |
| anything else | 404 |

### Issue content (everything posted runs under the owner's GitHub identity)

- The body starts with `<!-- plaud-v2-worker -->`, then the notice "Automated report submitted by an
  Open Pin install — unverified content", the app version / macOS line (plain text), and the
  submitted body inside a `~~~~~text` code fence, so GitHub renders none of it: no links, images,
  HTML, `@mentions` or `#123` cross-refs. Runs of 3+ tildes are broken up so nothing can close the
  fence; every `<!--` is broken too, so client text can't forge either marker. `auto` issues end
  with `<!-- fp:<fingerprint> -->`.
- Titles are plain: control/bidi characters, backticks, `<`, `>` and `\` removed, `](` / `![`
  pairs broken, `@`, `#<digit>` and `:emoji:` defused, cut to 120 characters.
- A report only comments on an issue that is open, labelled `auto-bug`, opened by the token's
  owner or a collaborator (`author_association`), whose body **starts with** the Worker marker and
  carries the fingerprint marker. Anything else (e.g. an issue someone else wrote to look like
  ours) is ignored and a new issue is created instead. Issues created before this marker existed
  no longer collect comments; the next occurrence opens a fresh one.

### Limits (Durable Object `ReportLimiter`)

| What | Limit | Over it |
|---|---|---|
| Requests per client IP (SHA-256 of `CF-Connecting-IP`), **every** request incl. invalid and `dry_run` | 10 / minute and 30 / hour | `429`, `Retry-After` = seconds left in the window |
| Requests from all clients | 60 / minute | `429` |
| New issues, all clients | 30 / UTC day | `429 daily report limit reached` |
| Comments per issue | 5 / UTC day | `200 {action: "suppressed"}` (nothing to retry) |
| Comments, all issues | 200 / UTC day | same |

Counters are fixed windows in a SQLite-backed Durable Object (works on the Free plan): one object
per hashed IP, one `global`. An alarm deletes expired rows. If the limiter is missing or errors,
`/v1/report` fails closed (`503`).

Why not the alternatives (checked 2026-11-02):
- **Cache API** (`caches.default`, the old limiter): per data centre and not atomic, and
  Cloudflare documents it as functional only on custom domains (this Worker is on workers.dev).
- **Workers Rate Limiting binding** (`[[ratelimits]]`): approximate and per location by design;
  deployed here with 10/min, it returned `success: true` for 45 same-key calls in a row from one
  colo (LHR), so it never limited anything.
- **KV**: eventually consistent, no atomic increment.

### Caching

`/v1/latest` keeps the response in isolate memory for 5 minutes (the layer that always works) and
also writes it to the Cache API as a best-effort second layer, which may be a no-op on workers.dev;
its errors are ignored. A full miss costs 1–3 GitHub calls against the token's 5,000/hour quota.
The response carries `Cache-Control: public, max-age=60`.

## Deploy

```bash
cd infra/update-worker
node --test test/                         # unit tests first (no network)
# Cloudflare credentials in the environment: CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID
npx --yes wrangler@4 deploy
```

## Rotate the GitHub token (no app release needed)

Also in [docs/RELEASING.md](../../docs/RELEASING.md#rotating-the-workers-github-token), which notes the
current status of the token.

1. GitHub → Settings → Developer settings → Fine-grained tokens → generate a token for
   the app's repository (`REPO`) only, with **Contents: Read-only** and **Issues: Read and write**
   (Metadata: Read-only is added automatically).
2. `npx --yes wrangler@4 secret put GH_TOKEN` and paste it (stdin, not a CLI argument).
3. Check: `curl -s https://plaud-v2-updates.leave-hacker-feedback.workers.dev/v1/latest` returns the latest version.
4. Revoke the old token on GitHub.

## Smoke tests

```bash
W=https://plaud-v2-updates.leave-hacker-feedback.workers.dev
curl -s $W/v1/latest | jq '{version, asset}'
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$W/v1/download/v2.3.0/Plaud.V2_2.3.0_aarch64.dmg"   # 302 → githubusercontent
curl -s -X POST -H 'Content-Type: application/json' -d '{"fingerprint":"00","title":"t","body":"b","app_version":"0.0.0","os":"test","dry_run":true}' $W/v1/report
# Rate limit: the 11th request in a minute from one IP is 429 (invalid ones count too; writes nothing)
for i in $(seq 1 12); do curl -s -o /dev/null -w '%{http_code} ' -X POST -H 'Content-Type: application/json' -d '{"title":""}' $W/v1/report; done; echo
```

Logs: `npx --yes wrangler@4 tail` (observability is on in `wrangler.toml`).
