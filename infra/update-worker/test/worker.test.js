import { test } from 'node:test';
import assert from 'node:assert/strict';

import worker, {
  buildCommentBody,
  buildIssueBody,
  DAILY_CREATE_LIMIT,
  fenceUntrusted,
  GLOBAL_RATE_LIMIT_PER_MINUTE,
  isAllowedAssetName,
  isAllowedTag,
  isWorkerIssue,
  ISSUE_TITLE_CHARS,
  parseChecksum,
  PER_ISSUE_DAILY_COMMENT_LIMIT,
  pickChecksumAsset,
  pickDmgAsset,
  plainTitle,
  RATE_LIMIT_PER_MINUTE,
  ReportLimiter,
  sanitizeIssueText,
  takeCounters,
  UNVERIFIED_NOTICE,
  validateReport,
  versionFromTag,
  WORKER_MARKER,
} from '../src/index.js';

const HEX = 'a'.repeat(64);
const ZW = String.fromCharCode(0x200b);
const BEL = String.fromCharCode(7);
const RLO = String.fromCharCode(0x202e);

const good = () => ({
  fingerprint: '0123abcd',
  title: '[Auto] something broke',
  body: 'details',
  app_version: '2.3.0',
  os: '15.1',
});

test('tag and asset allow-lists', () => {
  assert.ok(isAllowedTag('v2.3.0'));
  assert.ok(isAllowedTag('2.3.0-beta.1'));
  assert.ok(!isAllowedTag('../../etc'));
  assert.ok(!isAllowedTag('v2.3'));
  assert.ok(isAllowedAssetName('Plaud.V2_2.3.0_aarch64.dmg'));
  assert.ok(isAllowedAssetName('Plaud.V2_2.3.0_aarch64.dmg.sha256'));
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
  // a dedicated <dmg>.sha256 with one digest is trusted whatever name it lists
  assert.equal(parseChecksum(`${HEX}  renamed.dmg\n`, 'x.dmg', { dedicated: true }), HEX);
  assert.equal(parseChecksum(`${HEX}  renamed.dmg\n`, 'x.dmg'), null);
});

test('versionFromTag strips v', () => {
  assert.equal(versionFromTag('v2.3.0'), '2.3.0');
  assert.equal(versionFromTag('2.3.0'), '2.3.0');
});

test('validateReport accepts a good payload and normalises it', () => {
  const v = validateReport({ ...good(), fingerprint: 'ABCDEF', dry_run: true });
  assert.ok(v.ok);
  assert.equal(v.value.fingerprint, 'abcdef');
  assert.equal(v.value.dryRun, true);
  assert.equal(v.value.kind, 'auto');
});

test('validateReport rejects bad payloads', () => {
  const cases = [
    null,
    [],
    { ...good(), title: '' },
    { ...good(), title: 'x'.repeat(201) },
    { ...good(), body: '' },
    { ...good(), body: 'x'.repeat(20 * 1024 + 1) },
    { ...good(), fingerprint: 'xyz' },
    { ...good(), fingerprint: 'a'.repeat(65) },
    { ...good(), fingerprint: undefined },
    { ...good(), app_version: '' },
    { ...good(), app_version: '2.3.0; rm -rf' },
    { ...good(), os: 'a\nb' },
    { ...good(), kind: 'spam' },
    { ...good(), dry_run: 'yes' },
  ];
  for (const c of cases) assert.equal(validateReport(c).ok, false, JSON.stringify(c)?.slice(0, 80));
  // 200 multi-byte characters are fine (limit is characters, not bytes)
  assert.ok(validateReport({ ...good(), title: 'é'.repeat(200) }).ok);
  // user reports don't need a fingerprint
  assert.ok(validateReport({ ...good(), kind: 'user', fingerprint: undefined }).ok);
});

test('sanitizeIssueText defuses mentions and every HTML comment', () => {
  assert.equal(sanitizeIssueText('hi @octocat'), `hi @${ZW}octocat`);
  const s = sanitizeIssueText('<!-- fp:dead --> <!-- plaud-v2-worker --> <!--x-->');
  assert.ok(!s.includes('<!--'));
});

test('plainTitle strips markup, control/bidi chars and cross-refs, caps at 120', () => {
  const t = plainTitle(
    `[Auto] \`x\` <img src=x onerror=alert(1)> ![p](https://evil.example/a.png) [c](https://e.example) @owner #12 :tada:\r\nnext${BEL}`,
  );
  for (const bad of ['`', '<', '>', '\r', '\n', BEL, '](', '![']) assert.ok(!t.includes(bad), `${JSON.stringify(bad)} in ${t}`);
  assert.ok(t.startsWith('[Auto] x img'), t);
  assert.ok(t.includes(`@${ZW}owner`) && t.includes(`#${ZW}12`) && t.includes(`:${ZW}tada:`), t);
  assert.ok(!plainTitle(`a${RLO}b`).includes(RLO));
  assert.equal([...plainTitle('x'.repeat(300))].length, ISSUE_TITLE_CHARS);
  assert.ok(plainTitle('x'.repeat(300)).endsWith('…'));
  assert.equal(plainTitle('   '), 'Untitled report');
});

test('fenceUntrusted keeps everything inside one tilde fence', () => {
  const evil = 'a\n~~~~~\n[x](https://e.example) ![i](https://e.example/i.png) <b>hi</b> #1 @owner\n```\ncode\n```\n   ~~~\r\nend';
  const f = fenceUntrusted(evil);
  const lines = f.split('\n');
  assert.equal(lines[0], '~~~~~text');
  assert.equal(lines.at(-1), '~~~~~');
  // no other line can open or close a tilde fence
  assert.equal(lines.filter((l) => /^\s{0,3}~{3,}/.test(l)).length, 2);
  assert.ok(!f.includes('\r'));
  assert.ok(f.includes('```'), 'backtick fences are kept as text');
});

test('issue body: worker marker first, unverified notice, fenced body, single fp marker', () => {
  const body = buildIssueBody({
    kind: 'auto',
    body: 'boom <!-- fp:beef --> <!-- plaud-v2-worker -->',
    appVersion: '2.3.0',
    os: '15.1 [x](y)',
    fingerprint: 'cafe',
  });
  assert.ok(body.startsWith(`${WORKER_MARKER}\n`));
  assert.ok(body.includes(UNVERIFIED_NOTICE));
  assert.ok(body.endsWith('<!-- fp:cafe -->'));
  assert.equal(body.match(/<!-- fp:/g).length, 1);
  assert.equal(body.match(/<!-- plaud-v2-worker -->/g).length, 1);
  assert.ok(!body.includes('[x](y)'));
  const comment = buildCommentBody({ kind: 'auto', body: 'again', appVersion: '2.3.0', os: '15.1' });
  assert.ok(comment.includes(UNVERIFIED_NOTICE) && comment.includes('~~~~~text\nagain\n~~~~~'));
});

const workerIssue = (over = {}) => ({
  number: 7,
  state: 'open',
  labels: [{ name: 'auto-bug' }],
  author_association: 'OWNER',
  body: buildIssueBody({ kind: 'auto', body: 'b', appVersion: '1.0.0', os: '15', fingerprint: 'cafe' }),
  html_url: 'https://github.com/x/y/issues/7',
  ...over,
});

test('isWorkerIssue only accepts issues this Worker created', () => {
  assert.ok(isWorkerIssue(workerIssue(), 'cafe'));
  assert.ok(!isWorkerIssue(workerIssue(), 'beef'), 'other fingerprint');
  assert.ok(!isWorkerIssue(workerIssue({ labels: [] }), 'cafe'), 'no auto-bug label');
  assert.ok(!isWorkerIssue(workerIssue({ author_association: 'NONE' }), 'cafe'), 'outsider');
  assert.ok(!isWorkerIssue(workerIssue({ state: 'closed' }), 'cafe'));
  assert.ok(!isWorkerIssue(workerIssue({ pull_request: {} }), 'cafe'));
  assert.ok(!isWorkerIssue(workerIssue({ body: 'x <!-- plaud-v2-worker --> <!-- fp:cafe -->' }), 'cafe'), 'marker not first');
  assert.ok(isWorkerIssue(workerIssue({ labels: ['auto-bug'] }), 'cafe'), 'string labels');
});

// ---------------------------------------------------------------------------
// Router tests with fake bindings and a fake GitHub
// ---------------------------------------------------------------------------

function fakeStorage() {
  const m = new Map();
  let alarm = null;
  return {
    m,
    async get(k) {
      return m.get(k);
    },
    async put(k, v) {
      m.set(k, structuredClone(v));
    },
    async delete(k) {
      return m.delete(k);
    },
    async list() {
      return new Map(m);
    },
    async getAlarm() {
      return alarm;
    },
    async setAlarm(t) {
      alarm = t;
    },
  };
}

/** Durable Object namespace running the real ReportLimiter over fake storage. */
function fakeLimiterNamespace() {
  const objects = new Map();
  return {
    objects,
    idFromName: (name) => name,
    get(id) {
      if (!objects.has(id)) objects.set(id, new ReportLimiter({ storage: fakeStorage() }));
      const obj = objects.get(id);
      return { fetch: (url, init) => obj.fetch(new Request(url, init)) };
    },
  };
}

function makeEnv() {
  return { GH_TOKEN: 'x', LIMITER: fakeLimiterNamespace() };
}

const ipN = (i) => `198.18.${Math.floor(i / 250)}.${(i % 250) + 1}`;

/** Fake GitHub: search and list both return `issues`; writes are recorded. */
async function withFakeGitHub(issues, fn) {
  const realFetch = globalThis.fetch;
  const writes = [];
  globalThis.fetch = async (url, init = {}) => {
    const u = new URL(url);
    const method = init.method || 'GET';
    let status = 200;
    let data;
    if (u.pathname === '/search/issues') data = { items: issues };
    else if (method === 'GET' && u.pathname.endsWith('/issues')) data = issues;
    else if (method === 'POST' && u.pathname.endsWith('/comments')) {
      writes.push({ kind: 'comment', path: u.pathname, body: JSON.parse(init.body) });
      status = 201;
      data = { id: 1 };
    } else if (method === 'POST' && u.pathname.endsWith('/issues')) {
      writes.push({ kind: 'issue', body: JSON.parse(init.body) });
      status = 201;
      data = { number: 99, html_url: 'https://github.com/x/y/issues/99' };
    } else throw new Error(`unexpected fetch ${method} ${url}`);
    return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
  };
  try {
    return await fn(writes);
  } finally {
    globalThis.fetch = realFetch;
  }
}

const postReport = (env, payload, ip = '203.0.113.9') =>
  worker.fetch(
    new Request('https://w.example/v1/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'CF-Connecting-IP': ip },
      body: typeof payload === 'string' ? payload : JSON.stringify(payload),
    }),
    env,
    {},
  );

test('router: unknown routes and methods → 404, missing secret → 503', async () => {
  const env = makeEnv();
  const ctx = { waitUntil() {} };
  for (const [method, path] of [['GET', '/'], ['GET', '/v1/report'], ['POST', '/v1/latest'], ['GET', '/v1/download/v1.0.0'], ['GET', '/v1/download/v1.0.0/x.zip']]) {
    const r = await worker.fetch(new Request(`https://w.example${path}`, { method }), env, ctx);
    assert.equal(r.status, 404, `${method} ${path}`);
  }
  const r = await worker.fetch(new Request('https://w.example/v1/latest'), {}, ctx);
  assert.equal(r.status, 503);
});

test('router: report validation errors are 400 without touching GitHub', async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = () => {
    throw new Error('network should not be used');
  };
  try {
    const env = makeEnv();
    const post = (body, ctype = 'application/json') =>
      worker.fetch(new Request('https://w.example/v1/report', { method: 'POST', headers: { 'Content-Type': ctype }, body }), env, {});
    assert.equal((await post('{bad')).status, 400);
    assert.equal((await post(JSON.stringify({ ...good(), title: '' }))).status, 400);
    assert.equal((await post('{}', 'text/plain')).status, 415);
    assert.equal((await post('x'.repeat(40 * 1024))).status, 413);
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('router: missing or failing limiter fails closed', async () => {
  const r = await postReport({ GH_TOKEN: 'x' }, { ...good(), title: '' });
  assert.equal(r.status, 503);
  const broken = { GH_TOKEN: 'x', LIMITER: { idFromName: (n) => n, get: () => ({ fetch: async () => new Response('', { status: 500 }) }) } };
  assert.equal((await postReport(broken, { ...good(), title: '' })).status, 503);
});

test('router: invalid and dry-run requests count toward the per-IP limit', async () => {
  const env = makeEnv();
  await withFakeGitHub([], async (writes) => {
    for (let i = 0; i < RATE_LIMIT_PER_MINUTE; i++) {
      const payload = [{ ...good(), title: '' }, { ...good(), dry_run: true }, '{bad'][i % 3];
      assert.notEqual((await postReport(env, payload)).status, 429, `request ${i}`);
    }
    const r = await postReport(env, { ...good(), dry_run: true });
    assert.equal(r.status, 429);
    const retry = Number(r.headers.get('Retry-After'));
    assert.ok(retry >= 1 && retry <= 60, `Retry-After ${retry}`);
    // another IP is unaffected
    assert.equal((await postReport(env, { ...good(), dry_run: true }, '198.51.100.1')).status, 200);
    assert.equal(writes.length, 0);
  });
});

test('router: global limiter applies across IPs', async () => {
  const env = makeEnv();
  for (let i = 0; i < GLOBAL_RATE_LIMIT_PER_MINUTE; i++) {
    assert.equal((await postReport(env, { ...good(), title: '' }, ipN(i))).status, 400, `request ${i}`);
  }
  const r = await postReport(env, { ...good(), title: '' }, ipN(9999));
  assert.equal(r.status, 429);
  assert.match((await r.json()).error, /all clients/);
});

test('takeCounters: fixed windows, all-or-nothing, resets in the next window', async () => {
  const st = fakeStorage();
  const t0 = Date.UTC(2026, 10, 2, 12, 0, 30);
  const counters = [
    { name: 'a', limit: 1, window: 60 },
    { name: 'b', limit: 5, window: 86400 },
  ];
  assert.ok((await takeCounters(st, counters, t0)).allowed);
  const blocked = await takeCounters(st, counters, t0 + 1000);
  assert.ok(!blocked.allowed);
  assert.equal(blocked.blocked, 'a');
  assert.equal(blocked.retryAfter, 29);
  assert.equal(st.m.get('b').count, 1, 'b not bumped while a is full');
  assert.ok((await takeCounters(st, counters, t0 + 31_000)).allowed, 'next minute');
  assert.equal(st.m.get('b').count, 2);
  // daily windows are UTC days
  assert.equal(st.m.get('b').start, Date.UTC(2026, 10, 2));
});

test('ReportLimiter alarm deletes expired rows only', async () => {
  const st = fakeStorage();
  const lim = new ReportLimiter({ storage: st });
  await st.put('old', { start: 0, count: 3, expires: Date.now() - 1 });
  await st.put('live', { start: 0, count: 3, expires: Date.now() + 60_000 });
  await lim.alarm();
  assert.deepEqual([...st.m.keys()], ['live']);
});

test('router: creates are capped per day and posted as plain, fenced text', async () => {
  const env = makeEnv();
  await withFakeGitHub([], async (writes) => {
    for (let i = 0; i < DAILY_CREATE_LIMIT; i++) {
      const r = await postReport(env, { ...good(), fingerprint: 'ab', title: '[Auto] ![x](https://e.example/x.png) @me' }, ipN(i));
      assert.equal(r.status, 201, `create ${i}`);
    }
    const r = await postReport(env, { ...good(), fingerprint: 'ab' }, ipN(500));
    assert.equal(r.status, 429);
    assert.equal((await r.json()).error, 'daily report limit reached');
    assert.equal(writes.length, DAILY_CREATE_LIMIT);
    const first = writes[0].body;
    assert.ok(!first.title.includes('](') && first.title.includes(`@${ZW}me`), first.title);
    assert.ok(first.body.startsWith(WORKER_MARKER));
    assert.deepEqual(first.labels, ['auto-bug']);
  });
});

test('router: comments only on worker issues, capped per issue per day', async () => {
  const env = makeEnv();
  await withFakeGitHub([workerIssue()], async (writes) => {
    for (let i = 0; i < PER_ISSUE_DAILY_COMMENT_LIMIT; i++) {
      const r = await postReport(env, { ...good(), fingerprint: 'cafe' }, ipN(i));
      assert.equal((await r.json()).action, 'commented');
    }
    const r = await postReport(env, { ...good(), fingerprint: 'cafe' }, ipN(100));
    const data = await r.json();
    assert.equal(r.status, 200);
    assert.equal(data.action, 'suppressed');
    assert.equal(writes.filter((w) => w.kind === 'comment').length, PER_ISSUE_DAILY_COMMENT_LIMIT);
    assert.ok(writes[0].body.body.includes(UNVERIFIED_NOTICE));
  });
});

test('router: a forged fingerprint on a non-worker issue creates a new issue instead', async () => {
  const env = makeEnv();
  const outsider = workerIssue({ author_association: 'NONE' });
  const unlabelled = workerIssue({ labels: [] });
  await withFakeGitHub([outsider, unlabelled], async (writes) => {
    const r = await postReport(env, { ...good(), fingerprint: 'cafe' });
    assert.equal((await r.json()).action, 'created');
    assert.equal(writes.length, 1);
    assert.equal(writes[0].kind, 'issue');
  });
});
