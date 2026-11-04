import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import { stageRelease } from './stage-release.mjs';

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'htc-release-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const input = path.join(root, 'dist'), output = path.join(root, 'release');
  await fs.mkdir(input);
  const bytes = Buffer.from('fixture');
  const sha256 = createHash('sha256').update(bytes).digest('hex');
  const config = { bundle: 'tools-2026.10.0', upstream: { binary: { sha256 } }, tools: {
    ffmpeg: { platforms: { mac_arm64: { sourceBundle: 'complete', package: { upstream: 'binary' }, sources: ['fixture'] } } },
  }, sources: {
    fixture: { attach: true, sha256, filename: 'fixture.tar.gz' },
    complete: { attach: true, sha256, filename: 'complete.tar.gz', binaryArchives: [sha256], includesBuildInstructions: true },
  } };
  const manifest = { schema: 1, bundle: config.bundle, tools: { ffmpeg: { platforms: { mac_arm64: {
    version: '1', upstreamSha256: sha256, asset: 'ffmpeg.tar.gz', format: 'tar.gz', bin: 'ffmpeg/ffmpeg', sha256, size: bytes.length,
    sourceBundle: 'complete', sources: ['fixture', 'complete'], correspondingSource: { asset: 'source-complete.tar.gz', sha256, size: bytes.length },
  } } } } };
  for (const file of ['ffmpeg.tar.gz', 'source-complete.tar.gz', 'source-fixture.tar.gz']) await fs.writeFile(path.join(input, file), bytes);
  await fs.writeFile(path.join(input, 'SOURCES.md'), '# Source downloads\n');
  const { privateKey, publicKey } = generateKeyPairSync('ed25519');
  const resign = async () => {
    const data = Buffer.from(JSON.stringify(manifest));
    await fs.writeFile(path.join(input, 'manifest.json'), data);
    await fs.writeFile(path.join(input, 'manifest.json.sig'), sign(null, data, privateKey).toString('base64'));
  };
  await resign();
  const key = Buffer.from(publicKey.export({ format: 'jwk' }).x, 'base64url').toString('base64');
  return { input, output, config, manifest, resign, stage: () => stageRelease(input, output, config, key) };
}

test('release staging copies named assets and excludes all unrelated files', async t => {
  const f = await fixture(t);
  for (const file of ['internal-notes.md', 'build.log', '.DS_Store', 'private-key.pem']) await fs.writeFile(path.join(f.input, file), 'local-only fixture');
  const files = await f.stage();
  assert.deepEqual(files, ['SOURCES.md', 'ffmpeg.tar.gz', 'manifest.json', 'manifest.json.sig', 'source-complete.tar.gz', 'source-fixture.tar.gz']);
  assert.deepEqual((await fs.readdir(f.output)).sort(), files);
});

test('altered secondary project sources prevent staging', async t => {
  const f = await fixture(t);
  await fs.writeFile(path.join(f.input, 'source-fixture.tar.gz'), 'altered');
  await assert.rejects(f.stage(), /Source checksum mismatch/);
  await assert.rejects(fs.access(f.output), { code: 'ENOENT' });
});

test('missing source notices or invalid signatures prevent staging', async t => {
  const f = await fixture(t);
  await fs.rm(path.join(f.input, 'SOURCES.md'));
  await assert.rejects(f.stage(), { code: 'ENOENT' });
  await fs.writeFile(path.join(f.input, 'SOURCES.md'), '# Source downloads\n');
  await fs.appendFile(path.join(f.input, 'manifest.json'), '\n');
  await assert.rejects(f.stage(), /exited with/);
  await assert.rejects(fs.access(f.output), { code: 'ENOENT' });
});

test('an existing destination is preserved and never merged into a release', async t => {
  const f = await fixture(t);
  await fs.mkdir(f.output);
  await fs.writeFile(path.join(f.output, 'old-note.md'), 'keep');
  await assert.rejects(f.stage(), { code: 'EEXIST' });
  assert.deepEqual(await fs.readdir(f.output), ['old-note.md']);
});


async function splitFixture(t) {
  const f = await fixture(t);
  const item = f.manifest.tools.ffmpeg.platforms.mac_arm64;
  f.config.sources.dependency = { ...f.config.sources.complete, filename: 'dependency.tar.gz', includesBuildInstructions: false };
  f.config.tools.ffmpeg.platforms.mac_arm64.sourceBundle = ['complete', 'dependency'];
  item.sourceBundle = ['complete', 'dependency'];
  item.sources.push('dependency');
  item.correspondingSource = [item.correspondingSource, { ...item.correspondingSource, asset: 'source-dependency.tar.gz' }];
  await fs.copyFile(path.join(f.input, 'source-complete.tar.gz'), path.join(f.input, 'source-dependency.tar.gz'));
  await f.resign();
  return f;
}

test('all reviewed split-source parts are included in the release allowlist', async t => {
  const f = await splitFixture(t);
  const files = await f.stage();
  assert.ok(files.includes('source-complete.tar.gz'));
  assert.ok(files.includes('source-dependency.tar.gz'));
  assert.ok(files.includes('source-fixture.tar.gz'));
  assert.deepEqual((await fs.readdir(f.output)).sort(), files);
});

test('a signed manifest cannot omit a required source part or project source', async t => {
  const f = await splitFixture(t), item = f.manifest.tools.ffmpeg.platforms.mac_arm64;
  item.sourceBundle = 'complete';
  item.correspondingSource = item.correspondingSource[0];
  item.sources = ['complete', 'fixture'];
  await f.resign();
  await assert.rejects(f.stage(), /Corresponding-source set differs/);
  item.sourceBundle = ['complete', 'dependency'];
  item.correspondingSource = [item.correspondingSource, { ...item.correspondingSource, asset: 'source-dependency.tar.gz' }];
  item.sources = ['complete', 'dependency'];
  await f.resign();
  await assert.rejects(f.stage(), /Source list differs/);
  await assert.rejects(fs.access(f.output), { code: 'ENOENT' });
});

test('a validly signed substitute source archive cannot replace the reviewed source', async t => {
  const f = await splitFixture(t), item = f.manifest.tools.ffmpeg.platforms.mac_arm64;
  const other = Buffer.from('different source');
  await fs.writeFile(path.join(f.input, 'source-dependency.tar.gz'), other);
  item.correspondingSource[1].sha256 = createHash('sha256').update(other).digest('hex');
  item.correspondingSource[1].size = other.length;
  await f.resign();
  await assert.rejects(f.stage(), /Corresponding-source attachment differs/);
});

test('staging fails if any source part has not been reviewed for its binary', async t => {
  const f = await splitFixture(t);
  f.config.sources.dependency.binaryArchives = ['0'.repeat(64)];
  await assert.rejects(f.stage(), /has not been reviewed/);
});


test('staging binds binary input to the reviewed source mapping', async t => {
  const f = await fixture(t);
  f.manifest.tools.ffmpeg.platforms.mac_arm64.upstreamSha256 = 'a'.repeat(64);
  await f.resign();
  await assert.rejects(f.stage(), /Binary input differs/);
});


test('staging retains configured minimum macOS metadata', async t => {
  const f = await fixture(t);
  f.config.tools.ffmpeg.platforms.mac_arm64.minimumOS = '14.0';
  f.manifest.tools.ffmpeg.platforms.mac_arm64.minimumOS = '14.0';
  await f.resign();
  await f.stage();
  const staged = JSON.parse(await fs.readFile(path.join(f.output, 'manifest.json'), 'utf8'));
  assert.equal(staged.tools.ffmpeg.platforms.mac_arm64.minimumOS, '14.0');
});

test('a signed manifest cannot omit or lower the configured minimum OS', async t => {
  const f = await fixture(t);
  f.config.tools.ffmpeg.platforms.mac_arm64.minimumOS = '14.0';
  await assert.rejects(f.stage(), /Minimum OS differs/);
  f.manifest.tools.ffmpeg.platforms.mac_arm64.minimumOS = '13.0';
  await f.resign();
  await assert.rejects(f.stage(), /Minimum OS differs/);
});

test('malformed minimum OS values fail even with a valid signature', async t => {
  const f = await fixture(t);
  f.config.tools.ffmpeg.platforms.mac_arm64.minimumOS = 'newest';
  f.manifest.tools.ffmpeg.platforms.mac_arm64.minimumOS = 'newest';
  await f.resign();
  await assert.rejects(f.stage(), /exited with/);
});
