# plaud-v2-updates (Cloudflare Worker)

Fronts the GitHub release API for the Open Pin app, so the app never calls GitHub directly.

Deployed at **https://openpin.bednal.com** (a custom domain, `routes` in `wrangler.toml`; the
hostname is hard-coded as `WORKER_BASE` in `src-tauri/src/commands/mod.rs`). Installs up to v2.5.x
call the same Worker at its `workers.dev` address (`workers_dev = true`), which keeps the
pre-rename Worker name; keep it until they have updated. `/v1/latest` is cached per host, because
its `download_url` must stay on the host the app called.

The repository is **public**, so the Worker needs no GitHub token. An optional read-only
`GH_TOKEN` (Contents: read) only raises the GitHub API rate limit; without it the Worker uses
unauthenticated GitHub requests, which the per-host cache keeps well under the limit.

**Test channel:** callers whose IP is in the secret `CANARY_IPS` (comma-separated;
`npx wrangler secret put CANARY_IPS`) get the newest release including pre-releases
(`X-Open-Pin-Channel: canary`); everyone else gets `releases/latest`.

The repository it reads releases from is `REPO` in `src/index.js`.

> **Retired (November 2026):** the `POST /v1/report` bug-report endpoint, its issue-writing
> `GH_TOKEN` and the `ReportLimiter` Durable Object are gone. The app exports bug reports to a
> local file (**Report a bug › Export bug report**) that the user attaches to a GitHub issue; the
> Worker no longer writes to GitHub.

## Endpoints

| Method + path | What it does |
|---|---|
| `GET /v1/latest` | Latest published release (GitHub `releases/latest`, so no drafts or pre-releases; canary callers get the newest including pre-releases): `{version, tag, name, notes, published_at, asset: {name, size, sha256 \| null}, download_url}`. `sha256` comes from a `<dmg name>.sha256` asset (or `SHA256SUMS`) on the release, else `null`. Cached per host 5 min in a per-isolate memory cache, with the Cache API as a best-effort second layer; the `X-Plaud-Cache` header says which (`miss`, `cache`, `memory`), and `X-Open-Pin-Channel` says `stable` or `canary`. |
| `GET /v1/download/<tag>/<asset>` | Looks the asset up on that release and answers **302** to GitHub's short-lived signed URL (no token in it), so the DMG never streams through the Worker. Only `.dmg` and `.sha256` assets; anything unknown is 404. |
| anything else | 404 |

## Caching

`/v1/latest` keeps the response in isolate memory for 5 minutes (the layer that always works),
keyed by host and channel, and also writes it to the Cache API as a best-effort second layer,
whose errors are ignored. A full miss costs 1–3 GitHub calls. The response carries
`Cache-Control: public, max-age=60`.

## Deploy

```bash
cd infra/update-worker
node --test test/                         # unit tests first (no network)
# Cloudflare credentials in the environment: CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID
npx --yes wrangler@4 deploy
```

## Optional read-only GitHub token

Only needed if unauthenticated reads ever hit GitHub's rate limit.

1. GitHub → Settings → Developer settings → Fine-grained tokens → generate a token for
   the app's repository (`REPO`) only, with **Contents: Read-only** (Metadata: Read-only is added
   automatically). No other permission — the Worker never writes.
2. `npx --yes wrangler@4 secret put GH_TOKEN` and paste it (stdin, not a CLI argument).
3. Remove it with `npx --yes wrangler@4 secret delete GH_TOKEN`.

## Smoke tests

```bash
W=https://openpin.bednal.com
curl -s $W/v1/latest | jq '{version, asset}'
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$W/v1/download/v2.6.0/Open.Pin_2.6.0_aarch64.dmg"   # 302 → githubusercontent
```

Logs: `npx --yes wrangler@4 tail` (observability is on in `wrangler.toml`).
