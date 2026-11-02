/**
 * plaud-v2-updates — Cloudflare Worker that fronts every GitHub call the
 * Plaud V2 desktop app makes, so no GitHub token ships inside the DMG.
 *
 *   GET  /v1/latest                     latest published release (cached ~5 min)
 *   GET  /v1/download/<tag>/<asset>     302 to GitHub's signed, token-free asset URL
 *   POST /v1/report                     auto-bug / user bug report → GitHub Issues
 *
 * The only secret is GH_TOKEN (a fine-grained PAT on Obednal97/Plaud-V2 with
 * Contents: read + Issues: read/write). Rotate it with
 *   npx wrangler secret put GH_TOKEN
 * — no app release needed.
 */

export const REPO = 'Obednal97/Plaud-V2';
export const USER_AGENT = 'plaud-v2-updates-worker';
const GH_API = 'https://api.github.com';

export const LATEST_TTL_SECONDS = 300;
export const RATE_LIMIT_PER_HOUR = 20;
export const MAX_TITLE_CHARS = 200;
export const MAX_BODY_BYTES = 20 * 1024;
const MAX_REQUEST_BYTES = 32 * 1024;

const TAG_RE = /^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]{1,32})?$/;
const ASSET_RE = /^[A-Za-z0-9][A-Za-z0-9._ -]{0,150}\.(?:dmg|sha256)$/;
const FINGERPRINT_RE = /^[0-9a-f]{1,64}$/;
const APP_VERSION_RE = /^[0-9A-Za-z.+-]{1,32}$/;

// ---------------------------------------------------------------------------
// Pure helpers (exported for tests)
// ---------------------------------------------------------------------------

export function isAllowedTag(tag) {
  return typeof tag === 'string' && TAG_RE.test(tag);
}

export function isAllowedAssetName(name) {
  return typeof name === 'string' && ASSET_RE.test(name) && !name.includes('..');
}

/** Pick the installer DMG from a GitHub release's asset list. */
export function pickDmgAsset(assets) {
  if (!Array.isArray(assets)) return null;
  return assets.find((a) => typeof a?.name === 'string' && a.name.endsWith('.dmg')) || null;
}

/** Find the checksum asset for `dmgName`: `<dmg>.sha256` first, then SHA256SUMS. */
export function pickChecksumAsset(assets, dmgName) {
  if (!Array.isArray(assets) || !dmgName) return null;
  return (
    assets.find((a) => a?.name === `${dmgName}.sha256`) ||
    assets.find((a) => a?.name === 'SHA256SUMS' || a?.name === 'SHA256SUMS.txt') ||
    null
  );
}

/**
 * Extract the SHA-256 for `fileName` from a checksum file. Accepts a bare hex
 * digest, `shasum -a 256` output (`<hex>  <name>` / `<hex> *<name>`), or a
 * multi-line SHA256SUMS file. Returns lowercase hex or null.
 */
export function parseChecksum(text, fileName, { dedicated = false } = {}) {
  if (typeof text !== 'string') return null;
  const entries = [];
  for (const raw of text.split(/\r?\n/)) {
    const m = raw.trim().match(/^([0-9a-fA-F]{64})(?:\s+\*?(.+))?$/);
    if (m) entries.push({ digest: m[1].toLowerCase(), name: m[2] ? m[2].trim() : null });
  }
  // GitHub turns spaces in uploaded asset names into dots ("Plaud V2_…" →
  // "Plaud.V2_…"), so compare names with that normalisation too.
  const norm = (n) => n.replace(/^.*\//, '').replace(/ /g, '.');
  const named = entries.find((e) => e.name && fileName && norm(e.name) === norm(fileName));
  if (named) return named.digest;
  // A dedicated `<dmg>.sha256` file with a single digest is unambiguous.
  if (dedicated && entries.length === 1) return entries[0].digest;
  const bare = entries.filter((e) => !e.name);
  return bare.length === 1 ? bare[0].digest : null;
}

export function versionFromTag(tag) {
  return String(tag || '').replace(/^v/, '');
}

/** Stop issue text from pinging GitHub users or spoofing our dedupe marker. */
export function sanitizeIssueText(text) {
  return String(text)
    .replace(/<!--\s*fp:/gi, '<!-- (fp):')
    .replace(/@(?=[A-Za-z0-9-])/g, '@​');
}

export function fingerprintMarker(fp) {
  return `<!-- fp:${fp} -->`;
}

/**
 * Validate a POST /v1/report payload. Returns { ok: true, value } or
 * { ok: false, error }.
 */
export function validateReport(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return { ok: false, error: 'payload must be a JSON object' };
  }
  const kind = payload.kind === undefined ? 'auto' : payload.kind;
  if (kind !== 'auto' && kind !== 'user') return { ok: false, error: 'kind must be "auto" or "user"' };

  const { title, body } = payload;
  if (typeof title !== 'string' || !title.trim()) return { ok: false, error: 'title is required' };
  if ([...title].length > MAX_TITLE_CHARS) return { ok: false, error: `title exceeds ${MAX_TITLE_CHARS} characters` };
  if (typeof body !== 'string' || !body.trim()) return { ok: false, error: 'body is required' };
  if (new TextEncoder().encode(body).length > MAX_BODY_BYTES) {
    return { ok: false, error: `body exceeds ${MAX_BODY_BYTES} bytes` };
  }

  let fingerprint = payload.fingerprint;
  if (fingerprint === undefined || fingerprint === null || fingerprint === '') {
    if (kind === 'auto') return { ok: false, error: 'fingerprint is required' };
    fingerprint = null;
  } else if (typeof fingerprint !== 'string' || !FINGERPRINT_RE.test(fingerprint.toLowerCase())) {
    return { ok: false, error: 'fingerprint must be 1-64 hex characters' };
  } else {
    fingerprint = fingerprint.toLowerCase();
  }

  const appVersion = payload.app_version;
  if (typeof appVersion !== 'string' || !APP_VERSION_RE.test(appVersion)) {
    return { ok: false, error: 'app_version is required (1-32 chars of [0-9A-Za-z.+-])' };
  }
  const os = payload.os;
  if (typeof os !== 'string' || os.length > 64 || /[\r\n]/.test(os)) {
    return { ok: false, error: 'os is required (≤ 64 chars, single line)' };
  }
  if (payload.dry_run !== undefined && typeof payload.dry_run !== 'boolean') {
    return { ok: false, error: 'dry_run must be a boolean' };
  }

  return {
    ok: true,
    value: {
      kind,
      title: title.trim(),
      body,
      fingerprint,
      appVersion,
      os: os.trim(),
      dryRun: payload.dry_run === true,
    },
  };
}

export function buildIssueBody(report) {
  const parts = [sanitizeIssueText(report.body).trimEnd(), '', '---'];
  parts.push(`*Plaud V2 ${report.appVersion} · macOS ${sanitizeIssueText(report.os) || 'unknown'} · via update worker*`);
  if (report.fingerprint) parts.push('', fingerprintMarker(report.fingerprint));
  return parts.join('\n');
}

export function buildCommentBody(report) {
  return [
    `### Another occurrence (${report.appVersion}, macOS ${sanitizeIssueText(report.os) || 'unknown'})`,
    '',
    sanitizeIssueText(report.body).trimEnd(),
  ].join('\n');
}

// ---------------------------------------------------------------------------
// HTTP plumbing
// ---------------------------------------------------------------------------

function json(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...extraHeaders },
  });
}

const notFound = () => json({ ok: false, error: 'not found' }, 404);

function ghHeaders(env, accept = 'application/vnd.github+json') {
  return {
    Authorization: `Bearer ${env.GH_TOKEN}`,
    Accept: accept,
    'User-Agent': USER_AGENT,
    'X-GitHub-Api-Version': '2022-11-28',
  };
}

async function ghJson(env, path, init = {}) {
  const resp = await fetch(`${GH_API}${path}`, {
    ...init,
    headers: { ...ghHeaders(env), ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...(init.headers || {}) },
  });
  let data = null;
  try {
    data = await resp.json();
  } catch {
    data = null;
  }
  return { status: resp.status, ok: resp.ok, data };
}

/**
 * Resolve a release asset's API URL to GitHub's short-lived, signed download
 * URL (no token in it). Returns the Location string or null.
 */
async function signedAssetLocation(env, assetApiUrl) {
  const resp = await fetch(assetApiUrl, {
    headers: ghHeaders(env, 'application/octet-stream'),
    redirect: 'manual',
  });
  const loc = resp.headers.get('Location');
  if (resp.status >= 300 && resp.status < 400 && loc && loc.startsWith('https://')) return loc;
  return null;
}

async function readChecksum(env, asset, dmgName) {
  try {
    const loc = await signedAssetLocation(env, asset.url);
    if (!loc) return null;
    const resp = await fetch(loc, { headers: { 'User-Agent': USER_AGENT } });
    if (!resp.ok) return null;
    const text = (await resp.text()).slice(0, 64 * 1024);
    return parseChecksum(text, dmgName, { dedicated: asset.name === `${dmgName}.sha256` });
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// GET /v1/latest
// ---------------------------------------------------------------------------

// Per-isolate memory cache. The Cache API below is per-data-centre and shared
// by all isolates there; this is a cheap first layer in front of it.
let memLatest = null; // { body: string, expires: number }

async function handleLatest(request, env, ctx) {
  const now = Date.now();
  if (memLatest && memLatest.expires > now) {
    return new Response(memLatest.body, { headers: latestHeaders('memory') });
  }

  const cacheKey = new Request(new URL('/__cache/v1/latest', request.url).toString(), { method: 'GET' });
  const cache = typeof caches !== 'undefined' ? caches.default : null;
  if (cache) {
    const hit = await cache.match(cacheKey);
    if (hit) {
      const body = await hit.text();
      memLatest = { body, expires: now + 60 * 1000 };
      return new Response(body, { headers: latestHeaders('cache') });
    }
  }

  const rel = await ghJson(env, `/repos/${REPO}/releases/latest`);
  if (rel.status === 404) return json({ ok: false, error: 'no published release' }, 404);
  if (!rel.ok || !rel.data) return json({ ok: false, error: `GitHub returned ${rel.status}` }, 502);
  const release = rel.data;
  // releases/latest already excludes drafts and prereleases; belt and braces.
  if (release.draft || release.prerelease) return json({ ok: false, error: 'no published release' }, 404);

  const dmg = pickDmgAsset(release.assets);
  if (!dmg) return json({ ok: false, error: 'latest release has no .dmg asset' }, 502);
  const sumAsset = pickChecksumAsset(release.assets, dmg.name);
  const sha256 = sumAsset ? await readChecksum(env, sumAsset, dmg.name) : null;

  const origin = new URL(request.url).origin;
  const data = {
    version: versionFromTag(release.tag_name),
    tag: release.tag_name,
    name: release.name || release.tag_name,
    notes: typeof release.body === 'string' ? release.body.slice(0, 20000) : '',
    published_at: release.published_at,
    asset: { name: dmg.name, size: dmg.size, sha256 },
    download_url: `${origin}/v1/download/${encodeURIComponent(release.tag_name)}/${encodeURIComponent(dmg.name)}`,
  };
  const body = JSON.stringify(data);
  memLatest = { body, expires: now + LATEST_TTL_SECONDS * 1000 };
  if (cache) {
    const toCache = new Response(body, {
      headers: { 'Content-Type': 'application/json', 'Cache-Control': `max-age=${LATEST_TTL_SECONDS}` },
    });
    ctx.waitUntil(cache.put(cacheKey, toCache).catch(() => {}));
  }
  return new Response(body, { headers: latestHeaders('miss') });
}

function latestHeaders(source) {
  return {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': `public, max-age=60`,
    'X-Plaud-Cache': source,
  };
}

// ---------------------------------------------------------------------------
// GET /v1/download/<tag>/<asset>
// ---------------------------------------------------------------------------

async function handleDownload(tag, assetName, env) {
  if (!isAllowedTag(tag) || !isAllowedAssetName(assetName)) return notFound();
  const rel = await ghJson(env, `/repos/${REPO}/releases/tags/${encodeURIComponent(tag)}`);
  if (rel.status === 404) return notFound();
  if (!rel.ok || !rel.data) return json({ ok: false, error: `GitHub returned ${rel.status}` }, 502);
  if (rel.data.draft) return notFound();
  const asset = (rel.data.assets || []).find((a) => a.name === assetName);
  if (!asset) return notFound();
  const loc = await signedAssetLocation(env, asset.url);
  if (!loc) return json({ ok: false, error: 'GitHub did not return a download location' }, 502);
  return new Response(null, { status: 302, headers: { Location: loc, 'Cache-Control': 'no-store' } });
}

// ---------------------------------------------------------------------------
// POST /v1/report
// ---------------------------------------------------------------------------

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * Fixed-window per-IP counter stored in the Cache API (no KV namespace
 * needed). The cache is per data centre and increments are not atomic, so this
 * is a best-effort abuse brake, not an exact quota — good enough for a handful
 * of trusted installs. The IP is hashed before it becomes a cache key.
 * Returns { allowed, remaining }.
 */
async function rateLimit(request, limit) {
  const cache = typeof caches !== 'undefined' ? caches.default : null;
  if (!cache) return { allowed: true, remaining: limit };
  const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
  const hour = Math.floor(Date.now() / 3600_000);
  const ipHash = (await sha256Hex(`plaud-v2:${ip}`)).slice(0, 32);
  const key = new Request(new URL(`/__ratelimit/report/${ipHash}/${hour}`, request.url).toString(), { method: 'GET' });
  const hit = await cache.match(key);
  const count = hit ? parseInt(await hit.text(), 10) || 0 : 0;
  if (count >= limit) return { allowed: false, remaining: 0 };
  const secondsLeft = 3600 - Math.floor((Date.now() / 1000) % 3600);
  await cache.put(
    key,
    new Response(String(count + 1), { headers: { 'Cache-Control': `max-age=${Math.max(secondsLeft, 60)}` } }),
  );
  return { allowed: true, remaining: limit - count - 1 };
}

/** Find an open auto-bug issue whose body carries `marker`. */
async function findIssueByMarker(env, fingerprint) {
  const marker = fingerprintMarker(fingerprint);
  // 1) Search API (body text is indexed, incl. the HTML comment's text).
  const q = `repo:${REPO} is:issue is:open label:auto-bug in:body "${fingerprint}"`;
  const search = await ghJson(env, `/search/issues?q=${encodeURIComponent(q)}&per_page=10`);
  if (search.ok && Array.isArray(search.data?.items)) {
    const hit = search.data.items.find((i) => i.state === 'open' && typeof i.body === 'string' && i.body.includes(marker));
    if (hit) return hit;
  }
  // 2) The search index lags issue creation by up to a minute or two; scan the
  //    most recent open auto-bug issues directly so bursts still dedupe.
  const list = await ghJson(env, `/repos/${REPO}/issues?state=open&labels=auto-bug&per_page=100&sort=created&direction=desc`);
  if (list.ok && Array.isArray(list.data)) {
    const hit = list.data.find((i) => !i.pull_request && typeof i.body === 'string' && i.body.includes(marker));
    if (hit) return hit;
  }
  if (!search.ok && !list.ok) throw new Error(`GitHub search failed (${search.status}/${list.status})`);
  return null;
}

async function handleReport(request, env) {
  const rl = await rateLimit(request, RATE_LIMIT_PER_HOUR);
  const rlHeaders = { 'X-RateLimit-Limit': String(RATE_LIMIT_PER_HOUR), 'X-RateLimit-Remaining': String(rl.remaining) };
  if (!rl.allowed) return json({ ok: false, error: 'rate limited' }, 429, { ...rlHeaders, 'Retry-After': '3600' });

  const ctype = request.headers.get('Content-Type') || '';
  if (!ctype.toLowerCase().includes('application/json')) {
    return json({ ok: false, error: 'Content-Type must be application/json' }, 415, rlHeaders);
  }
  const declared = parseInt(request.headers.get('Content-Length') || '0', 10);
  if (declared > MAX_REQUEST_BYTES) return json({ ok: false, error: 'request too large' }, 413, rlHeaders);
  const raw = await request.text();
  if (new TextEncoder().encode(raw).length > MAX_REQUEST_BYTES) {
    return json({ ok: false, error: 'request too large' }, 413, rlHeaders);
  }
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    return json({ ok: false, error: 'invalid JSON' }, 400, rlHeaders);
  }
  const v = validateReport(payload);
  if (!v.ok) return json({ ok: false, error: v.error }, 400, rlHeaders);
  const report = v.value;

  try {
    if (report.kind === 'auto') {
      const existing = await findIssueByMarker(env, report.fingerprint);
      if (existing) {
        if (report.dryRun) {
          return json({ ok: true, dry_run: true, action: 'would_comment', issue_number: existing.number }, 200, rlHeaders);
        }
        const c = await ghJson(env, `/repos/${REPO}/issues/${existing.number}/comments`, {
          method: 'POST',
          body: JSON.stringify({ body: buildCommentBody(report) }),
        });
        if (!c.ok) return json({ ok: false, error: `GitHub returned ${c.status}` }, 502, rlHeaders);
        return json({ ok: true, action: 'commented', issue_number: existing.number, url: existing.html_url }, 200, rlHeaders);
      }
    }
    if (report.dryRun) {
      return json({ ok: true, dry_run: true, action: 'would_create', issue_number: null }, 200, rlHeaders);
    }
    const created = await ghJson(env, `/repos/${REPO}/issues`, {
      method: 'POST',
      body: JSON.stringify({
        title: sanitizeIssueText(report.title),
        body: buildIssueBody(report),
        labels: [report.kind === 'user' ? 'user-report' : 'auto-bug'],
      }),
    });
    if (created.status !== 201 || !created.data) {
      return json({ ok: false, error: `GitHub returned ${created.status}` }, 502, rlHeaders);
    }
    return json(
      { ok: true, action: 'created', issue_number: created.data.number, url: created.data.html_url },
      201,
      rlHeaders,
    );
  } catch (e) {
    return json({ ok: false, error: String(e && e.message ? e.message : e) }, 502, rlHeaders);
  }
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

export default {
  async fetch(request, env, ctx) {
    if (!env.GH_TOKEN) return json({ ok: false, error: 'worker not configured' }, 503);
    const url = new URL(request.url);
    const path = url.pathname;
    try {
      if (path === '/v1/latest' && request.method === 'GET') return await handleLatest(request, env, ctx);
      if (path.startsWith('/v1/download/') && request.method === 'GET') {
        const rest = path.slice('/v1/download/'.length).split('/');
        if (rest.length !== 2) return notFound();
        let tag;
        let name;
        try {
          tag = decodeURIComponent(rest[0]);
          name = decodeURIComponent(rest[1]);
        } catch {
          return notFound();
        }
        return await handleDownload(tag, name, env);
      }
      if (path === '/v1/report' && request.method === 'POST') return await handleReport(request, env);
    } catch (e) {
      return json({ ok: false, error: 'internal error' }, 500);
    }
    return notFound();
  },
};
