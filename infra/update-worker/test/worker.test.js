import { test } from 'node:test';
import assert from 'node:assert/strict';

import worker, {
  _resetLatestCache,
  isAllowedAssetName,
  isAllowedTag,
  isCanary,
  parseChecksum,
  pickChecksumAsset,
  pickDmgAsset,
  publicAssetUrl,
  REPO,
  versionFromTag,
} from '../src/index.js';

const HEX = 'a'.repeat(64);

// The repo is public, so the Worker needs no token; an optional read-only
// GH_TOKEN only raises the GitHub rate limit.
function makeEnv() {
  return {};
}

test('tag and asset allow-lists', () => {
  assert.ok(isAllowedTag('v2.3.0'));
  assert.ok(isAllowedTag('2.3.0-beta.1'));
  assert.ok(!isAllowedTag('../../etc'));
  assert.ok(!isAllowedTag('v2.3'));
  assert.ok(isAllowedAssetName('Plaud.V2_2.3.0_aarch64.dmg'));
  assert.ok(isAllowedAssetName('Plaud.V2_2.3.0_aarch64.dmg.sha256'));
  // Release assets after the rename to Open Pin
  assert.ok(isAllowedAssetName('Open.Pin_2.6.0_aarch64.dmg'));
  assert.ok(isAllowedAssetName('Open.Pin_2.6.0_aarch64.dmg.sha256'));
  assert.ok(!isAllowedAssetName('source.zip'));
  assert.ok(!isAllowedAssetName('../x.dmg'));
  assert.ok(!isAllowedAssetName('a/b.dmg'));
});

test('asset pickers', () => {
  const assets = [{ name: 'notes.txt' }, { name: 'App_1.0.0.dmg', size: 5 }, { name: 'App_1.0.0.dmg.sha256' }, { name: 'SHA256SUMS' }];
  assert.equal(pickDmgAsset(assets).name, 'App_1.0.0.dmg');
  assert.equal(pickChecksumAsset(assets, 'App_1.0.0.dmg').name, 'App_1.0.0.dmg.sha256');
  assert.equal(pickChecksumAsset([{ name: 'SHA256SUMS' }], 'App_1.0.0.dmg').name, 'SHA256SUMS');
  assert.equal(pickChecksumAsset([], 'x.dmg'), null);
  assert.equal(pickDmgAsset(null), null);
});

test('parseChecksum handles bare digests, shasum output and SHA256SUMS', () => {
  assert.equal(parseChecksum(`${HEX}\n`, 'x.dmg'), HEX);
  assert.equal(parseChecksum(`${HEX.toUpperCase()}  x.dmg\n`, 'x.dmg'), HEX);
  assert.equal(parseChecksum(`${'b'.repeat(64)}  other.dmg\n${HEX} *x.dmg\n`, 'x.dmg'), HEX);
  assert.equal(parseChecksum('not a checksum', 'x.dmg'), null);
  assert.equal(parseChecksum(`${'b'.repeat(64)}  other.dmg\n`, 'x.dmg'), null);
  // GitHub renames "Plaud V2_…" to "Plaud.V2_…" on upload
  assert.equal(parseChecksum(`${HEX}  Plaud V2_2.4.0_aarch64.dmg\n`, 'Plaud.V2_2.4.0_aarch64.dmg'), HEX);
  assert.equal(parseChecksum(`${HEX}  /path/to/Plaud V2_2.4.0_aarch64.dmg\n`, 'Plaud.V2_2.4.0_aarch64.dmg'), HEX);
  assert.equal(parseChecksum(`${HEX}  Open Pin_2.6.0_aarch64.dmg\n`, 'Open.Pin_2.6.0_aarch64.dmg'), HEX);
  // a dedicated <dmg>.sha256 with one digest is trusted whatever name it lists
  assert.equal(parseChecksum(`${HEX}  renamed.dmg\n`, 'x.dmg', { dedicated: true }), HEX);
  assert.equal(parseChecksum(`${HEX}  renamed.dmg\n`, 'x.dmg'), null);
});

test('versionFromTag strips v', () => {
  assert.equal(versionFromTag('v2.3.0'), '2.3.0');
  assert.equal(versionFromTag('2.3.0'), '2.3.0');
});


test('latest + download pass a renamed DMG (Open.Pin_…) through unchanged', async () => {
  const realFetch = globalThis.fetch;
  const HEX = 'c'.repeat(64);
  const dmgName = 'Open.Pin_2.6.0_aarch64.dmg';
  const release = {
    tag_name: 'v2.6.0',
    name: 'v2.6.0',
    body: 'notes',
    draft: false,
    prerelease: false,
    assets: [
      { name: dmgName, size: 1234, url: 'https://api.github.com/repos/x/y/releases/assets/1' },
      { name: `${dmgName}.sha256`, size: 90, url: 'https://api.github.com/repos/x/y/releases/assets/2' },
    ],
  };
  globalThis.fetch = async (url) => {
    const u = new URL(url);
    if (u.pathname === `/repos/${REPO}/releases/latest` || u.pathname === `/repos/${REPO}/releases/tags/v2.6.0`) {
      return new Response(JSON.stringify(release), { headers: { 'Content-Type': 'application/json' } });
    }
    if (u.href === publicAssetUrl('v2.6.0', `${dmgName}.sha256`)) return new Response(`${HEX}  Open Pin_2.6.0_aarch64.dmg\n`);
    throw new Error(`unexpected fetch ${url}`);
  };
  try {
    const env = makeEnv();
    const ctx = { waitUntil() {} };
    const r = await worker.fetch(new Request('https://w.example/v1/latest'), env, ctx);
    assert.equal(r.status, 200);
    const data = await r.json();
    assert.equal(data.version, '2.6.0');
    assert.deepEqual(data.asset, { name: dmgName, size: 1234, sha256: HEX });
    assert.equal(data.download_url, `https://w.example/v1/download/v2.6.0/${dmgName}`);
    const d = await worker.fetch(new Request(data.download_url), env, ctx);
    assert.equal(d.status, 302);
    assert.equal(d.headers.get('Location'), `https://github.com/${REPO}/releases/download/v2.6.0/${dmgName}`);
  } finally {
    globalThis.fetch = realFetch;
  }
});


// ---- Canary channel and per-host cache ------------------------------------

function releaseFixture(tag, prerelease) {
  const dmg = `Open.Pin_${tag.slice(1)}_aarch64.dmg`;
  return {
    tag_name: tag,
    name: tag,
    body: 'notes',
    draft: false,
    prerelease,
    published_at: '2026-11-03T00:00:00Z',
    assets: [{ name: dmg, size: 10, url: `https://api.github.com/repos/x/y/releases/assets/${tag}` }],
  };
}

async function withReleases(fn) {
  const realFetch = globalThis.fetch;
  const stable = releaseFixture('v2.5.3', false);
  const pre = releaseFixture('v2.6.0', true);
  const calls = [];
  globalThis.fetch = async (url) => {
    const u = new URL(url);
    calls.push(u.pathname + u.search);
    if (u.pathname === `/repos/${REPO}/releases/latest`) return Response.json(stable);
    if (u.pathname === `/repos/${REPO}/releases`) return Response.json([pre, stable]);
    throw new Error(`unexpected fetch ${url}`);
  };
  _resetLatestCache();
  try {
    await fn(calls);
  } finally {
    globalThis.fetch = realFetch;
    _resetLatestCache();
  }
}

const latestFrom = (host, ip) =>
  new Request(`https://${host}/v1/latest`, { headers: ip ? { 'CF-Connecting-IP': ip } : {} });

test('isCanary matches only listed caller IPs', () => {
  const env = { CANARY_IPS: ' 203.0.113.7 , 198.51.100.2' };
  assert.equal(isCanary(latestFrom('w.example', '203.0.113.7'), env), true);
  assert.equal(isCanary(latestFrom('w.example', '198.51.100.2'), env), true);
  assert.equal(isCanary(latestFrom('w.example', '203.0.113.8'), env), false);
  assert.equal(isCanary(latestFrom('w.example'), env), false);
  assert.equal(isCanary(latestFrom('w.example', '203.0.113.7'), {}), false);
});

test('canary callers get the newest pre-release; everyone else the latest stable', async () => {
  await withReleases(async () => {
    const env = { ...makeEnv(), CANARY_IPS: '203.0.113.7' };
    const ctx = { waitUntil() {} };
    const canary = await worker.fetch(latestFrom('w.example', '203.0.113.7'), env, ctx);
    assert.equal(canary.headers.get('X-Open-Pin-Channel'), 'canary');
    assert.equal((await canary.json()).version, '2.6.0');
    const stable = await worker.fetch(latestFrom('w.example', '198.51.100.9'), env, ctx);
    assert.equal(stable.headers.get('X-Open-Pin-Channel'), 'stable');
    assert.equal((await stable.json()).version, '2.5.3');
    // And again from memory: still separate.
    assert.equal((await (await worker.fetch(latestFrom('w.example', '198.51.100.9'), env, ctx)).json()).version, '2.5.3');
  });
});

test("the cached answer never hands one host's download URL to another host", async () => {
  await withReleases(async (calls) => {
    const env = makeEnv();
    const ctx = { waitUntil() {} };
    const a = await (await worker.fetch(latestFrom('openpin.example'), env, ctx)).json();
    const b = await (await worker.fetch(latestFrom('old.workers.example'), env, ctx)).json();
    assert.ok(a.download_url.startsWith('https://openpin.example/v1/download/'));
    assert.ok(b.download_url.startsWith('https://old.workers.example/v1/download/'));
    const again = await (await worker.fetch(latestFrom('openpin.example'), env, ctx)).json();
    assert.ok(again.download_url.startsWith('https://openpin.example/'));
    assert.equal(calls.filter((c) => c.endsWith('/releases/latest')).length, 2); // one per host, then memory
  });
});


test('download redirects to the public URL without calling the GitHub API', async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    throw new Error(`no fetch expected, got ${url}`);
  };
  try {
    const ctx = { waitUntil() {} };
    const d = await worker.fetch(new Request('https://w.example/v1/download/v2.6.1/Open.Pin_2.6.1_aarch64.dmg'), makeEnv(), ctx);
    assert.equal(d.status, 302);
    assert.equal(d.headers.get('Location'), `https://github.com/${REPO}/releases/download/v2.6.1/Open.Pin_2.6.1_aarch64.dmg`);
    assert.equal(d.headers.get('Cache-Control'), 'no-store');
    const bad = await worker.fetch(new Request('https://w.example/v1/download/..%2Fx/Open.Pin_2.6.1_aarch64.dmg'), makeEnv(), ctx);
    assert.equal(bad.status, 404);
    const badName = await worker.fetch(new Request('https://w.example/v1/download/v2.6.1/source.zip'), makeEnv(), ctx);
    assert.equal(badName.status, 404);
  } finally {
    globalThis.fetch = realFetch;
  }
});


// ---- Last good answer (KV) when GitHub refuses -------------------------------

function fakeKV() {
  const store = new Map();
  return {
    store,
    puts: 0,
    async get(k) {
      return store.has(k) ? store.get(k) : null;
    },
    async put(k, v) {
      this.puts += 1;
      store.set(k, v);
    },
  };
}

// Runs fn with GitHub answering `mode()` ('ok' | 403 | 'throw' | 404) and a
// ctx whose waitUntil promises can be awaited.
async function withGitHub(fn) {
  const realFetch = globalThis.fetch;
  const stable = releaseFixture('v2.7.0', false);
  let mode = 'ok';
  globalThis.fetch = async (url) => {
    const u = new URL(url);
    if (u.pathname === `/repos/${REPO}/releases/latest`) {
      if (mode === 'throw') throw new Error('network down');
      if (mode === 403) return Response.json({ message: 'API rate limit exceeded' }, { status: 403 });
      if (mode === 404) return Response.json({ message: 'Not Found' }, { status: 404 });
      return Response.json(stable);
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  _resetLatestCache();
  const pending = [];
  const ctx = { waitUntil: (p) => pending.push(p) };
  const settle = () => Promise.all(pending.splice(0));
  try {
    await fn({ setMode: (m) => (mode = m), ctx, settle });
  } finally {
    globalThis.fetch = realFetch;
    _resetLatestCache();
  }
}

test('a GitHub refusal serves the last good answer, with the asking host’s URL', async () => {
  await withGitHub(async ({ setMode, ctx, settle }) => {
    const env = { LAST_GOOD: fakeKV() };
    const ok = await worker.fetch(latestFrom('openpin.example'), env, ctx);
    assert.equal(ok.status, 200);
    await settle();
    assert.equal(env.LAST_GOOD.puts, 1);
    const saved = JSON.parse(env.LAST_GOOD.store.get('latest|stable'));
    assert.equal(saved.download_url, undefined); // host-independent

    for (const mode of [403, 'throw']) {
      _resetLatestCache();
      setMode(mode);
      const r = await worker.fetch(latestFrom('old.workers.example'), env, ctx);
      assert.equal(r.status, 200, `mode ${mode}`);
      assert.equal(r.headers.get('X-Plaud-Cache'), 'stale');
      const data = await r.json();
      assert.equal(data.version, '2.7.0');
      assert.equal(data.asset.name, 'Open.Pin_2.7.0_aarch64.dmg');
      assert.equal(data.download_url, 'https://old.workers.example/v1/download/v2.7.0/Open.Pin_2.7.0_aarch64.dmg');
    }
  });
});

test('a refusal with nothing saved, or no binding, is still a 502', async () => {
  await withGitHub(async ({ setMode, ctx }) => {
    setMode(403);
    const empty = await worker.fetch(latestFrom('openpin.example'), { LAST_GOOD: fakeKV() }, ctx);
    assert.equal(empty.status, 502);
    assert.equal((await empty.json()).error, 'GitHub returned 403');
    _resetLatestCache();
    const unbound = await worker.fetch(latestFrom('openpin.example'), makeEnv(), ctx);
    assert.equal(unbound.status, 502);
  });
});

test('"no published release" is a real answer, not replaced by the saved one', async () => {
  await withGitHub(async ({ setMode, ctx, settle }) => {
    const env = { LAST_GOOD: fakeKV() };
    await worker.fetch(latestFrom('openpin.example'), env, ctx);
    await settle();
    _resetLatestCache();
    setMode(404);
    const r = await worker.fetch(latestFrom('openpin.example'), env, ctx);
    assert.equal(r.status, 404);
  });
});

test('the saved answer is written only when it changes', async () => {
  await withGitHub(async ({ ctx, settle }) => {
    const env = { LAST_GOOD: fakeKV() };
    for (let i = 0; i < 3; i += 1) {
      _resetLatestCache();
      assert.equal((await worker.fetch(latestFrom('openpin.example'), env, ctx)).status, 200);
      await settle();
    }
    assert.equal(env.LAST_GOOD.puts, 1);
  });
});
