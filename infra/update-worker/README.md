# plaud-v2-updates (Cloudflare Worker)

Fronts every GitHub call the Plaud V2 app makes, so the DMG ships **no GitHub token**.
The token lives only as a Worker secret and can be rotated without an app release.

Deployed at **https://plaud-v2-updates.leave-hacker-feedback.workers.dev**
(the hostname is hard-coded as `WORKER_BASE` in `src-tauri/src/commands/mod.rs`).

## Endpoints

| Method + path | What it does |
|---|---|
| `GET /v1/latest` | Latest published release (GitHub `releases/latest`, so no drafts or pre-releases): `{version, tag, name, notes, published_at, asset: {name, size, sha256 \| null}, download_url}`. `sha256` comes from a `<dmg name>.sha256` asset (or `SHA256SUMS`) on the release, else `null`. Cached 5 min in the Cache API plus a per-isolate memory cache; the `X-Plaud-Cache` header says which (`miss`, `cache`, `memory`). |
| `GET /v1/download/<tag>/<asset>` | Looks the asset up on that release and answers **302** to GitHub's short-lived signed URL (no token in it), so the DMG never streams through the Worker. Only `.dmg` and `.sha256` assets; anything unknown is 404. |
| `POST /v1/report` | Bug report → GitHub Issues. Body: `{kind?: "auto"\|"user", fingerprint, title, body, app_version, os, dry_run?}`. Limits: title ≤ 200 chars, body ≤ 20 KB, fingerprint 1–64 hex chars (required for `auto`), whole request ≤ 32 KB. `auto` reports look for an open `auto-bug` issue containing `<!-- fp:<fingerprint> -->` (search API, then the 100 newest open `auto-bug` issues, because search lags) and comment on it, else create one with label `auto-bug` and the marker. `user` reports always create an issue labelled `user-report`. `dry_run: true` validates and searches but writes nothing. Returns `{ok, action, issue_number, url}` (`action`: `created`, `commented`, `would_create`, `would_comment`). |
| anything else | 404 |

Reports are sanitised server-side too: `@mentions` are defused and any client-supplied `<!-- fp:` marker is neutralised.

### Rate limiting (why no KV)

`POST /v1/report` allows **20 requests per client IP per hour** (fixed window), counted in the
Cache API under a SHA-256 of the IP. No KV namespace is needed. Caveats: the Cache API is per
Cloudflare data centre and increments aren't atomic, so it's a best-effort brake against a
runaway client, not an exact quota. That's enough for a handful of trusted installs (the app also
dedupes per session). Over the limit → `429` with `Retry-After: 3600`. Responses carry
`X-RateLimit-Remaining`.

## Deploy

```bash
cd infra/update-worker
source ~/.secrets/cloudflare.env          # CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID
npx --yes wrangler@3 deploy
node --test test/                         # unit tests (no network)
```

## Rotate the GitHub token (no app release needed)

1. GitHub → Settings → Developer settings → Fine-grained tokens → generate a token for
   **Obednal97/Plaud-V2** only, with **Contents: Read-only** and **Issues: Read and write**
   (Metadata: Read-only is added automatically).
2. `npx --yes wrangler@3 secret put GH_TOKEN` and paste it (stdin, not a CLI argument).
3. Check: `curl -s https://plaud-v2-updates.leave-hacker-feedback.workers.dev/v1/latest` returns the latest version.
4. Revoke the old token on GitHub.

## Smoke tests

```bash
W=https://plaud-v2-updates.leave-hacker-feedback.workers.dev
curl -s $W/v1/latest | jq '{version, asset}'
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$W/v1/download/v2.3.0/Plaud.V2_2.3.0_aarch64.dmg"   # 302 → githubusercontent
curl -s -X POST -H 'Content-Type: application/json' -d '{"fingerprint":"00","title":"t","body":"b","app_version":"0.0.0","os":"test","dry_run":true}' $W/v1/report
```

Logs: `npx --yes wrangler@3 tail` (observability is on in `wrangler.toml`).
