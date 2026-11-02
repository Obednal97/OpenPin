import { test } from 'node:test';
import assert from 'node:assert/strict';

import worker, {
  buildIssueBody,
  isAllowedAssetName,
  isAllowedTag,
  parseChecksum,
  pickChecksumAsset,
  pickDmgAsset,
  sanitizeIssueText,
  validateReport,
  versionFromTag,
} from '../src/index.js';

const HEX = 'a'.repeat(64);

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

test('issue text is sanitised and carries the marker', () => {
  assert.equal(sanitizeIssueText('hi @octocat'), 'hi @​octocat');
  assert.ok(!sanitizeIssueText('<!-- fp:dead -->').includes('<!-- fp:'));
  const body = buildIssueBody({ body: 'boom <!-- fp:beef -->', appVersion: '2.3.0', os: '15.1', fingerprint: 'cafe' });
  assert.ok(body.endsWith('<!-- fp:cafe -->'));
  assert.equal(body.match(/<!-- fp:/g).length, 1);
});

test('router: unknown routes and methods → 404, missing secret → 503', async () => {
  const env = { GH_TOKEN: 'x' };
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
    const env = { GH_TOKEN: 'x' };
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
