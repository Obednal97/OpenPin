/**
 * plaud-v2-updates — Cloudflare Worker that fronts every GitHub call the
 * Open Pin desktop app makes, so no GitHub token ships inside the DMG.
 * (The Worker, its URL and the markers below keep the app's pre-rename name:
 * installed apps and existing issues depend on them.)
 *
 *   GET  /v1/latest                     latest published release (cached ~5 min)
 *   GET  /v1/download/<tag>/<asset>     302 to GitHub's signed, token-free asset URL
 *   POST /v1/report                     auto-bug / user bug report → GitHub Issues
 *
 * Bindings (wrangler.toml): LIMITER, the ReportLimiter Durable Object that
 * holds the report rate limits and the daily create / comment caps.
 * The only secret is GH_TOKEN (a fine-grained PAT on Obednal97/OpenPin with
 * Contents: read + Issues: read/write). Rotate it with
 *   npx wrangler secret put GH_TOKEN
 * — no app release needed.
 */

// The GitHub repo releases and issues live in. GitHub redirects API calls for
// a renamed repo, but update this when the repo is renamed (then redeploy).
export const REPO = 'Obednal97/OpenPin';
export const USER_AGENT = 'plaud-v2-updates-worker';
const GH_API = 'https://api.github.com';

export const LATEST_TTL_SECONDS = 300;
// POST /v1/report limits (enforced by the ReportLimiter Durable Object).
/** Requests per client IP per minute and per hour (every request counts, dry runs and invalid ones too). */
export const RATE_LIMIT_PER_MINUTE = 10;
export const RATE_LIMIT_PER_HOUR = 30;
/** Requests per minute from all clients together. */
export const GLOBAL_RATE_LIMIT_PER_MINUTE = 60;
/** New issues per UTC day, all installs together. */
export const DAILY_CREATE_LIMIT = 30;
/** Comments per UTC day, all installs together. */
export const DAILY_COMMENT_LIMIT = 200;
/** Comments per issue per UTC day. */
export const PER_ISSUE_DAILY_COMMENT_LIMIT = 5;
export const MAX_TITLE_CHARS = 200; // accepted from the client
export const ISSUE_TITLE_CHARS = 120; // what goes on GitHub
export const MAX_BODY_BYTES = 20 * 1024;
const MAX_REQUEST_BYTES = 32 * 1024;

/** First line of every issue this Worker creates; only such issues get comments. */
export const WORKER_MARKER = '<!-- plaud-v2-worker -->';
export const UNVERIFIED_NOTICE = 'Automated report submitted by an Open Pin install — unverified content';
/** GitHub author associations only the PAT's owner / collaborators can have. */
const TRUSTED_ASSOCIATIONS = new Set(['OWNER', 'MEMBER', 'COLLABORATOR']);

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
  // GitHub turns spaces in uploaded asset names into dots ("Open Pin_…" →
  // "Open.Pin_…"), so compare names with that normalisation too.
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

/**
 * No @mentions, and no HTML comments at all, so client text can never forge
 * our `<!-- plaud-v2-worker -->` / `<!-- fp:… -->` markers.
 */
export function sanitizeIssueText(text) {
  return String(text)
    .replace(/<!--/g, '<!\u200b--')
    .replace(/@(?=[A-Za-z0-9-])/g, '@\u200b');
}

const CONTROL_OR_BIDI_RE = /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u2069\ufeff]/g;

/** Truncate to `max` code points, ending with an ellipsis when cut. */
function truncateChars(s, max) {
  const chars = [...s];
  return chars.length <= max ? s : `${chars.slice(0, max - 1).join('').trimEnd()}…`;
}

/**
 * One line of client text made inert for GitHub: no control/bidi characters,
 * no markdown/HTML syntax, no @mentions, #123 cross-refs or :emoji: codes.
 */
export function plainInline(text, max) {
  const t = String(text)
    .replace(CONTROL_OR_BIDI_RE, ' ')
    // Code spans, HTML and escapes go; "[Auto]" stays readable, but no
    // `](` / `![` pairs survive to form a link or image.
    .replace(/[`<>\\]/g, '')
    .replace(/!\[/g, '[')
    .replace(/\]\s*\(/g, '] (')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/@(?=[A-Za-z0-9-])/g, '@\u200b')
    .replace(/#(?=\d)/g, '#\u200b')
    .replace(/:(?=[a-z0-9_+-]+:)/gi, ':\u200b');
  return truncateChars(t, max);
}

/** Issue title: plain text, at most ISSUE_TITLE_CHARS characters. */
export function plainTitle(text) {
  return plainInline(text, ISSUE_TITLE_CHARS) || 'Untitled report';
}

/**
 * Wrap client text in a tilde code fence so GitHub renders none of it (no
 * links, images, HTML, mentions or cross-refs). Runs of 3+ tildes are broken
 * up so nothing inside can close the fence; backtick fences inside a tilde
 * fence are plain text in CommonMark, so the app's own ``` blocks survive.
 */
export function fenceUntrusted(text) {
  const t = sanitizeIssueText(
    String(text)
      .replace(/\r\n?/g, '\n')
      .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, ''),
  ).replace(/~{3,}/g, (m) => m.split('').join('\u200b'));
  return `~~~~~text\n${t.trimEnd()}\n~~~~~`;
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

function reportMeta(report) {
  const os = plainInline(report.os, 64) || 'unknown';
  const kind = report.kind === 'user' ? 'user report' : 'auto report';
  return `Open Pin ${plainInline(report.appVersion, 32)} · macOS ${os} · ${kind}`;
}

export function buildIssueBody(report) {
  const parts = [
    WORKER_MARKER,
    `> **${UNVERIFIED_NOTICE}.** Everything in the block below came from the app and is shown as plain text.`,
    '',
    `*${reportMeta(report)}*`,
    '',
    fenceUntrusted(report.body),
  ];
  if (report.fingerprint) parts.push('', fingerprintMarker(report.fingerprint));
  return parts.join('\n');
}

export function buildCommentBody(report) {
  return [
    '### Another occurrence',
    `> **${UNVERIFIED_NOTICE}.**`,
    '',
    `*${reportMeta(report)}*`,
    '',
    fenceUntrusted(report.body),
  ].join('\n');
}

/**
 * True only for an issue this Worker created for `fingerprint`: open, labelled
 * auto-bug, opened by the token's owner or a collaborator, body starting with
 * WORKER_MARKER and carrying the fingerprint marker. Client text can contain
 * neither marker (sanitizeIssueText breaks every `<!--`).
 */
export function isWorkerIssue(issue, fingerprint) {
  if (!issue || typeof issue !== 'object' || issue.pull_request) return false;
  if (issue.state !== 'open') return false;
  const labels = Array.isArray(issue.labels) ? issue.labels.map((l) => (typeof l === 'string' ? l : l?.name)) : [];
  if (!labels.includes('auto-bug')) return false;
  if (!TRUSTED_ASSOCIATIONS.has(issue.author_association)) return false;
  if (typeof issue.body !== 'string' || !issue.body.startsWith(WORKER_MARKER)) return false;
  return issue.body.includes(fingerprintMarker(fingerprint));
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

// Per-isolate memory cache: the layer that always works. The Cache API below
// is only a best-effort second layer: it is per data centre, and Cloudflare
// documents it as functional on custom domains, not on workers.dev (it did
// store entries there when tested on 2026-11-02, but nothing relies on that).
// Its errors are swallowed. A full miss costs 1-3 GitHub calls against the
// PAT's 5,000/hour quota.
// Keyed by `${origin}|${channel}`: the body holds a download_url on the host
// the request came to, and installed apps only accept their own Worker host
// (old installs: *.workers.dev; newer: the custom domain). One shared entry
// would hand one host's URL to the other's apps.
const memLatest = new Map(); // key -> { body: string, expires: number }

// Pre-releases are offered only to a few test machines, matched by the
// caller's IP (CANARY_IPS: comma-separated, a Worker secret). Installed apps
// send nothing that identifies them, so the address is the only handle.
// Everyone else gets releases/latest, which never includes a pre-release.
export function isCanary(request, env) {
  const list = String((env && env.CANARY_IPS) || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  if (list.length === 0) return false;
  return list.includes(request.headers.get('CF-Connecting-IP') || '');
}

export function _resetLatestCache() {
  memLatest.clear();
}

async function handleLatest(request, env, ctx) {
  const now = Date.now();
  const origin = new URL(request.url).origin;
  const channel = isCanary(request, env) ? 'canary' : 'stable';
  const memKey = `${origin}|${channel}`;
  const mem = memLatest.get(memKey);
  if (mem && mem.expires > now) {
    return new Response(mem.body, { headers: latestHeaders('memory', channel) });
  }

  const cacheKey = new Request(new URL(`/__cache/v1/latest/${channel}`, request.url).toString(), { method: 'GET' });
  const cache = typeof caches !== 'undefined' ? caches.default : null;
  if (cache) {
    const hit = await cache.match(cacheKey).catch(() => undefined);
    if (hit) {
      const body = await hit.text();
      memLatest.set(memKey, { body, expires: now + 60 * 1000 });
      return new Response(body, { headers: latestHeaders('cache', channel) });
    }
  }

  let release;
  if (channel === 'canary') {
    // Newest first (by creation); the first published one with a DMG,
    // pre-release or not.
    const list = await ghJson(env, `/repos/${REPO}/releases?per_page=10`);
    if (!list.ok || !Array.isArray(list.data)) return json({ ok: false, error: `GitHub returned ${list.status}` }, 502);
    release = list.data.find((r) => r && !r.draft && pickDmgAsset(r.assets || []));
    if (!release) return json({ ok: false, error: 'no published release' }, 404);
  } else {
    const rel = await ghJson(env, `/repos/${REPO}/releases/latest`);
    if (rel.status === 404) return json({ ok: false, error: 'no published release' }, 404);
    if (!rel.ok || !rel.data) return json({ ok: false, error: `GitHub returned ${rel.status}` }, 502);
    release = rel.data;
    // releases/latest already excludes drafts and prereleases; belt and braces.
    if (release.draft || release.prerelease) return json({ ok: false, error: 'no published release' }, 404);
  }

  const dmg = pickDmgAsset(release.assets);
  if (!dmg) return json({ ok: false, error: 'latest release has no .dmg asset' }, 502);
  const sumAsset = pickChecksumAsset(release.assets, dmg.name);
  const sha256 = sumAsset ? await readChecksum(env, sumAsset, dmg.name) : null;

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
  memLatest.set(memKey, { body, expires: now + LATEST_TTL_SECONDS * 1000 });
  if (cache) {
    const toCache = new Response(body, {
      headers: { 'Content-Type': 'application/json', 'Cache-Control': `max-age=${LATEST_TTL_SECONDS}` },
    });
    ctx.waitUntil(cache.put(cacheKey, toCache).catch(() => {}));
  }
  return new Response(body, { headers: latestHeaders('miss', channel) });
}

function latestHeaders(source, channel = 'stable') {
  return {
    'X-Open-Pin-Channel': channel,
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

// ---- Limits (Durable Object `ReportLimiter`, binding LIMITER) ----
//
// Why a Durable Object: caches.default is per data centre, not atomic, and
// Cloudflare documents it as functional on custom domains only; the Workers
// Rate Limiting binding is approximate per location, and on this account it
// returned success for 45 same-key calls in a row against a 10/min limit
// (tested 2026-11-02). A Durable Object is single-threaded with durable
// storage, so its fixed-window counters are exact.
//
// Objects: one per hashed client IP ("ip:<hash>") and one "global".

/**
 * Fixed-window counters in a Durable Object's storage. All-or-nothing: if any
 * counter is at its limit nothing is incremented. Window starts are multiples
 * of the window length since the epoch, so 86400 s windows are UTC days.
 * @param {{get: Function, put: Function}} storage
 * @param {Array<{name: string, limit: number, window: number}>} counters
 * @returns {Promise<{allowed: boolean, retryAfter: number, blocked: string | null}>}
 */
export async function takeCounters(storage, counters, now = Date.now()) {
  const rows = await Promise.all(counters.map((c) => storage.get(c.name)));
  const state = counters.map((c, i) => {
    const start = Math.floor(now / (c.window * 1000)) * c.window * 1000;
    const row = rows[i];
    const count = row && row.start === start ? row.count : 0;
    return { ...c, start, count };
  });
  const full = state.find((s) => s.count >= s.limit);
  if (full) {
    return { allowed: false, retryAfter: Math.max(1, Math.ceil((full.start + full.window * 1000 - now) / 1000)), blocked: full.name };
  }
  await Promise.all(
    state.map((s) => storage.put(s.name, { start: s.start, count: s.count + 1, expires: s.start + s.window * 1000 })),
  );
  return { allowed: true, retryAfter: 0, blocked: null };
}

/** Durable Object: POST {counters: [...]} → takeCounters result. */
export class ReportLimiter {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    const { counters } = await request.json();
    const storage = this.state.storage;
    const result = await takeCounters(storage, counters);
    // Tidy expired rows (per-IP objects would otherwise keep one forever).
    if ((await storage.getAlarm()) === null) await storage.setAlarm(Date.now() + 3600 * 1000);
    return new Response(JSON.stringify(result), { headers: { 'Content-Type': 'application/json' } });
  }

  async alarm() {
    const storage = this.state.storage;
    const now = Date.now();
    const rows = await storage.list();
    let left = 0;
    for (const [key, row] of rows) {
      if (!row || typeof row.expires !== 'number' || row.expires <= now) await storage.delete(key);
      else left++;
    }
    if (left) await storage.setAlarm(now + 3600 * 1000);
  }
}

/** Ask the `objectName` limiter to take `counters`; throws if unavailable. */
async function takeLimit(env, objectName, counters) {
  const stub = env.LIMITER.get(env.LIMITER.idFromName(objectName));
  const resp = await stub.fetch('https://limiter/take', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ counters }),
  });
  if (!resp.ok) throw new Error(`limiter returned ${resp.status}`);
  return resp.json();
}

/**
 * Per-request limits, checked before anything else so invalid and dry-run
 * requests count too: per client IP (hashed), then all clients together
 * (protects the PAT's search quota). Fails closed. Returns null when allowed,
 * else the error Response.
 */
async function checkRateLimits(request, env) {
  if (!env.LIMITER) return json({ ok: false, error: 'rate limiter not configured' }, 503);
  const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
  const ipKey = `ip:${(await sha256Hex(`plaud-v2:${ip}`)).slice(0, 32)}`;
  try {
    const perIp = await takeLimit(env, ipKey, [
      { name: 'report-min', limit: RATE_LIMIT_PER_MINUTE, window: 60 },
      { name: 'report-hour', limit: RATE_LIMIT_PER_HOUR, window: 3600 },
    ]);
    if (!perIp.allowed) {
      return json({ ok: false, error: 'rate limited' }, 429, {
        'Retry-After': String(perIp.retryAfter),
        'X-RateLimit-Limit': `${RATE_LIMIT_PER_MINUTE}/min, ${RATE_LIMIT_PER_HOUR}/hour`,
      });
    }
    const global = await takeLimit(env, 'global', [{ name: 'report-min', limit: GLOBAL_RATE_LIMIT_PER_MINUTE, window: 60 }]);
    if (!global.allowed) {
      return json({ ok: false, error: 'rate limited (all clients)' }, 429, { 'Retry-After': String(global.retryAfter) });
    }
  } catch {
    return json({ ok: false, error: 'rate limiter unavailable' }, 503);
  }
  return null;
}

/**
 * Daily caps on GitHub writes, in the "global" limiter object. Returns
 * {allowed, retryAfter}; throws if the limiter is unavailable.
 */
function reserveDaily(env, counters) {
  return takeLimit(env, 'global', counters.map(([name, limit]) => ({ name, limit, window: 86400 })));
}

/** Find the open issue this Worker created for `fingerprint` (see isWorkerIssue). */
async function findIssueByMarker(env, fingerprint) {
  // 1) Search API (body text is indexed, incl. the HTML comment's text).
  const q = `repo:${REPO} is:issue is:open label:auto-bug in:body "${fingerprint}"`;
  const search = await ghJson(env, `/search/issues?q=${encodeURIComponent(q)}&per_page=10`);
  if (search.ok && Array.isArray(search.data?.items)) {
    const hit = search.data.items.find((i) => isWorkerIssue(i, fingerprint));
    if (hit) return hit;
  }
  // 2) The search index lags issue creation by up to a minute or two; scan the
  //    most recent open auto-bug issues directly so bursts still dedupe.
  const list = await ghJson(env, `/repos/${REPO}/issues?state=open&labels=auto-bug&per_page=100&sort=created&direction=desc`);
  if (list.ok && Array.isArray(list.data)) {
    const hit = list.data.find((i) => isWorkerIssue(i, fingerprint));
    if (hit) return hit;
  }
  if (!search.ok && !list.ok) throw new Error(`GitHub search failed (${search.status}/${list.status})`);
  return null;
}

async function handleReport(request, env) {
  const limited = await checkRateLimits(request, env);
  if (limited) return limited;

  const ctype = request.headers.get('Content-Type') || '';
  if (!ctype.toLowerCase().includes('application/json')) {
    return json({ ok: false, error: 'Content-Type must be application/json' }, 415);
  }
  const declared = parseInt(request.headers.get('Content-Length') || '0', 10);
  if (declared > MAX_REQUEST_BYTES) return json({ ok: false, error: 'request too large' }, 413);
  const raw = await request.text();
  if (new TextEncoder().encode(raw).length > MAX_REQUEST_BYTES) {
    return json({ ok: false, error: 'request too large' }, 413);
  }
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    return json({ ok: false, error: 'invalid JSON' }, 400);
  }
  const v = validateReport(payload);
  if (!v.ok) return json({ ok: false, error: v.error }, 400);
  const report = v.value;

  try {
    if (report.kind === 'auto') {
      const existing = await findIssueByMarker(env, report.fingerprint);
      if (existing) {
        if (report.dryRun) {
          return json({ ok: true, dry_run: true, action: 'would_comment', issue_number: existing.number });
        }
        const quota = await reserveDaily(env, [
          [`comments:${existing.number}`, PER_ISSUE_DAILY_COMMENT_LIMIT],
          ['comments', DAILY_COMMENT_LIMIT],
        ]);
        if (!quota.allowed) {
          // This issue already has plenty of occurrences today; nothing for the app to retry.
          return json({ ok: true, action: 'suppressed', issue_number: existing.number, url: existing.html_url });
        }
        const c = await ghJson(env, `/repos/${REPO}/issues/${existing.number}/comments`, {
          method: 'POST',
          body: JSON.stringify({ body: buildCommentBody(report) }),
        });
        if (!c.ok) return json({ ok: false, error: `GitHub returned ${c.status}` }, 502);
        return json({ ok: true, action: 'commented', issue_number: existing.number, url: existing.html_url });
      }
    }
    if (report.dryRun) {
      return json({ ok: true, dry_run: true, action: 'would_create', issue_number: null });
    }
    const quota = await reserveDaily(env, [['creates', DAILY_CREATE_LIMIT]]);
    if (!quota.allowed) {
      return json({ ok: false, error: 'daily report limit reached' }, 429, { 'Retry-After': String(quota.retryAfter) });
    }
    const created = await ghJson(env, `/repos/${REPO}/issues`, {
      method: 'POST',
      body: JSON.stringify({
        title: plainTitle(report.title),
        body: buildIssueBody(report),
        labels: [report.kind === 'user' ? 'user-report' : 'auto-bug'],
      }),
    });
    if (created.status !== 201 || !created.data) {
      return json({ ok: false, error: `GitHub returned ${created.status}` }, 502);
    }
    return json({ ok: true, action: 'created', issue_number: created.data.number, url: created.data.html_url }, 201);
  } catch (e) {
    return json({ ok: false, error: String(e && e.message ? e.message : e) }, 502);
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
