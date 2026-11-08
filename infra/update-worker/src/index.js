/**
 * plaud-v2-updates — Cloudflare Worker that fronts the GitHub release API for
 * the Open Pin desktop app, so the app never calls GitHub directly.
 * (The Worker keeps its pre-rename name: installed apps call this URL.)
 *
 *   GET  /v1/latest                     latest published release (cached ~5 min; when
 *                                       GitHub refuses, the last good answer from KV)
 *   GET  /v1/download/<tag>/<asset>     302 to GitHub's signed, token-free asset URL
 *
 * The repo is public, so these reads need no token. An optional read-only
 * GH_TOKEN (Contents: read) only raises the GitHub API rate limit:
 *   npx wrangler secret put GH_TOKEN      # optional
 * The POST /v1/report bug-report endpoint and its issue-writing token were
 * retired in November 2026; the app exports bug reports locally instead.
 */

// The GitHub repo releases and issues live in. GitHub redirects API calls for
// a renamed repo, but update this when the repo is renamed (then redeploy).
export const REPO = 'Obednal97/OpenPin';
export const USER_AGENT = 'plaud-v2-updates-worker';
const GH_API = 'https://api.github.com';

export const LATEST_TTL_SECONDS = 300;

const TAG_RE = /^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]{1,32})?$/;
const ASSET_RE = /^[A-Za-z0-9][A-Za-z0-9._ -]{0,150}\.(?:dmg|sha256)$/;

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

function ghHeaders(env) {
  const headers = {
    Accept: 'application/vnd.github+json',
    'User-Agent': USER_AGENT,
    'X-GitHub-Api-Version': '2022-11-28',
  };
  // The repo is public, so reads work unauthenticated. A GH_TOKEN (read-only)
  // is optional and only raises the GitHub API rate limit.
  if (env.GH_TOKEN) headers.Authorization = `Bearer ${env.GH_TOKEN}`;
  return headers;
}

async function ghJson(env, path) {
  const resp = await fetch(`${GH_API}${path}`, { headers: ghHeaders(env) });
  let data = null;
  try {
    data = await resp.json();
  } catch {
    data = null;
  }
  return { status: resp.status, ok: resp.ok, data };
}

/**
 * The public download URL of a release asset. The repo is public, so
 * github.com serves it (302 to a signed githubusercontent.com URL) with no
 * GitHub API call: no rate limit. Unauthenticated API calls share a 60/hour
 * limit per egress IP, and Cloudflare's egress IPs are shared, so resolving
 * assets through the API failed downloads with 502 (v2.6.1, 2026-11-04).
 */
export function publicAssetUrl(tag, name) {
  return `https://github.com/${REPO}/releases/download/${encodeURIComponent(tag)}/${encodeURIComponent(name)}`;
}

async function readChecksum(tag, asset, dmgName) {
  try {
    const resp = await fetch(publicAssetUrl(tag, asset.name), { headers: { 'User-Agent': USER_AGENT } });
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
// Its errors are swallowed. A full miss costs one GitHub API call (the
// checksum comes from the public download URL).
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

// The last good answer per channel, in KV (binding LAST_GOOD, optional), so a
// GitHub refusal never reaches the app. Unauthenticated API calls share a
// 60/hour limit per egress IP and Cloudflare's egress IPs are shared, so
// GitHub intermittently answers 403 (seen 2026-11-05: 2 of 8 checks failed).
// KV is global, unlike the Cache API, so a data centre that has never reached
// GitHub still has an answer. Stored without download_url, which is rebuilt
// for the host asked.
const LAST_GOOD_KEY = (channel) => `latest|${channel}`;
const STALE_MEMORY_MS = 60 * 1000;

function withDownloadUrl(data, origin) {
  return {
    ...data,
    download_url: `${origin}/v1/download/${encodeURIComponent(data.tag)}/${encodeURIComponent(data.asset.name)}`,
  };
}

async function readLastGood(env, channel) {
  if (!env || !env.LAST_GOOD) return null;
  try {
    const raw = await env.LAST_GOOD.get(LAST_GOOD_KEY(channel));
    const data = raw ? JSON.parse(raw) : null;
    return data && data.tag && data.asset && data.asset.name ? data : null;
  } catch {
    return null;
  }
}

async function saveLastGood(env, channel, data) {
  if (!env || !env.LAST_GOOD) return;
  try {
    const value = JSON.stringify(data);
    // Write only on change: KV writes are limited and this runs on every miss.
    if ((await env.LAST_GOOD.get(LAST_GOOD_KEY(channel))) === value) return;
    await env.LAST_GOOD.put(LAST_GOOD_KEY(channel), value);
  } catch {
    // best effort
  }
}

/**
 * Ask GitHub for the release a channel should get. Returns { data } (without
 * download_url), or { status, error, fallback } where fallback says whether
 * the last good answer may be served instead (GitHub refused or failed),
 * as opposed to a definite answer such as "no published release".
 */
async function fetchLatest(env, channel) {
  let release;
  try {
    if (channel === 'canary') {
      // Newest first (by creation); the first published one with a DMG,
      // pre-release or not.
      const list = await ghJson(env, `/repos/${REPO}/releases?per_page=10`);
      if (!list.ok || !Array.isArray(list.data)) return { status: 502, error: `GitHub returned ${list.status}`, fallback: true };
      release = list.data.find((r) => r && !r.draft && pickDmgAsset(r.assets || []));
      if (!release) return { status: 404, error: 'no published release', fallback: false };
    } else {
      const rel = await ghJson(env, `/repos/${REPO}/releases/latest`);
      if (rel.status === 404) return { status: 404, error: 'no published release', fallback: false };
      if (!rel.ok || !rel.data) return { status: 502, error: `GitHub returned ${rel.status}`, fallback: true };
      release = rel.data;
      // releases/latest already excludes drafts and prereleases; belt and braces.
      if (release.draft || release.prerelease) return { status: 404, error: 'no published release', fallback: false };
    }
  } catch {
    return { status: 502, error: 'GitHub unreachable', fallback: true };
  }

  const dmg = pickDmgAsset(release.assets);
  if (!dmg) return { status: 502, error: 'latest release has no .dmg asset', fallback: true };
  const sumAsset = pickChecksumAsset(release.assets, dmg.name);
  const sha256 = sumAsset ? await readChecksum(release.tag_name, sumAsset, dmg.name) : null;
  return {
    data: {
      version: versionFromTag(release.tag_name),
      tag: release.tag_name,
      name: release.name || release.tag_name,
      notes: typeof release.body === 'string' ? release.body.slice(0, 20000) : '',
      published_at: release.published_at,
      asset: { name: dmg.name, size: dmg.size, sha256 },
    },
  };
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

  const result = await fetchLatest(env, channel);
  if (!result.data) {
    const stale = result.fallback ? await readLastGood(env, channel) : null;
    if (!stale) return json({ ok: false, error: result.error }, result.status);
    // Held briefly in memory so a refusing GitHub isn't asked on every call.
    const body = JSON.stringify(withDownloadUrl(stale, origin));
    memLatest.set(memKey, { body, expires: now + STALE_MEMORY_MS });
    return new Response(body, { headers: latestHeaders('stale', channel) });
  }

  const body = JSON.stringify(withDownloadUrl(result.data, origin));
  memLatest.set(memKey, { body, expires: now + LATEST_TTL_SECONDS * 1000 });
  ctx.waitUntil(saveLastGood(env, channel, result.data));
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

// Redirects to the public download URL: no GitHub API call (see
// publicAssetUrl). Draft and unknown assets 404 at GitHub, as before here.
function handleDownload(tag, assetName) {
  if (!isAllowedTag(tag) || !isAllowedAssetName(assetName)) return notFound();
  return new Response(null, { status: 302, headers: { Location: publicAssetUrl(tag, assetName), 'Cache-Control': 'no-store' } });
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

export default {
  async fetch(request, env, ctx) {
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
        return handleDownload(tag, name);
      }
    } catch (e) {
      return json({ ok: false, error: 'internal error' }, 500);
    }
    return notFound();
  },
};
