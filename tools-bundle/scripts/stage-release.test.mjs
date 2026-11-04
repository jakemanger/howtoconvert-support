import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import { stageRelease } from './stage-release.mjs';
import { vendorDelivery, verifyReusableAsset } from './lib.mjs';

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
  return { input, output, config, manifest, resign, key, stage: () => stageRelease(input, output, config, key) };
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


async function vendorFixture(t, tool = 'magick') {
  const f = await fixture(t), item = f.manifest.tools.ffmpeg.platforms.mac_arm64;
  const format = tool === 'magick' ? 'windows-inno' : 'windows-msi-admin';
  const asset = tool === 'magick' ? 'ImageMagick-original.exe' : 'LibreOffice-original.msi';
  const bin = tool === 'magick' ? 'magick/magick.exe' : 'libreoffice/program/soffice.com';
  const configured = f.config.tools.ffmpeg.platforms.mac_arm64;
  configured.package = { type: 'as-is', upstream: 'binary', format, delivery: 'direct' };
  configured.bin = bin;
  configured.version = item.version;
  f.config.upstream.binary.url = `https://vendor.example/${asset}`;
  f.config.upstream.binary.filename = asset;
  Object.assign(item, { asset, format, bin, downloadUrl: f.config.upstream.binary.url });
  f.config.tools = { [tool]: { platforms: { windows_x86_64: configured } } };
  f.manifest.tools = { [tool]: { platforms: { windows_x86_64: item } } };
  const vendorDirectory = path.join(path.dirname(f.input), 'vendor-originals');
  await fs.mkdir(vendorDirectory);
  await fs.copyFile(path.join(f.input, 'ffmpeg.tar.gz'), path.join(vendorDirectory, asset));
  await f.resign();
  return { ...f, item, configured, vendorDirectory, stage: () => stageRelease(f.input, f.output, f.config, f.key, vendorDirectory) };
}

for (const tool of ['magick', 'libreoffice']) {
  test(`${tool}: original vendor bytes are verified but never staged for redistribution`, async t => {
    const f = await vendorFixture(t, tool);
    const names = await f.stage();
    assert.deepEqual(names, ['SOURCES.md', 'manifest.json', 'manifest.json.sig', 'source-complete.tar.gz', 'source-fixture.tar.gz']);
    assert.equal(await fs.readFile(path.join(f.vendorDirectory, f.item.asset), 'utf8'), 'fixture');
    assert.equal(f.item.downloadUrl, f.config.upstream.binary.url);
    await assert.rejects(fs.access(path.join(f.output, f.item.asset)), { code: 'ENOENT' });
  });
}

test('direct vendor staging still requires original bytes and matching hashes', async t => {
  const f = await vendorFixture(t);
  await assert.rejects(stageRelease(f.input, f.output, f.config, f.key), /exited with/);
  await fs.writeFile(path.join(f.vendorDirectory, f.item.asset), 'changed');
  await assert.rejects(f.stage(), /exited with/);
  await assert.rejects(fs.access(f.output), { code: 'ENOENT' });
});

test('a signed replacement URL cannot change the reviewed direct delivery', async t => {
  const f = await vendorFixture(t);
  f.item.downloadUrl = 'https://another.example/original.exe';
  await f.resign();
  await assert.rejects(f.stage(), /Vendor delivery differs/);
});

test('direct delivery rejects HTTP, credentials, fragments and unsupported tool/platform pairs', async t => {
  const f = await vendorFixture(t), upstream = f.config.upstream.binary;
  for (const url of ['http://vendor.example/a.exe', 'https://user:secret@vendor.example/a.exe', 'https://vendor.example/a.exe#fragment']) {
    assert.throws(() => vendorDelivery('magick', 'windows_x86_64', f.configured, { ...upstream, url }), /Invalid direct vendor/);
  }
  assert.throws(() => vendorDelivery('ffmpeg', 'windows_x86_64', f.configured, upstream), /Invalid direct vendor/);
  assert.throws(() => vendorDelivery('magick', 'mac_arm64', f.configured, upstream), /Invalid direct vendor/);
  f.item.format = 'zip';
  await f.resign();
  await assert.rejects(f.stage(), /exited with/);
});

test('direct delivery preserves complete corresponding-source gates', async t => {
  const f = await vendorFixture(t);
  f.config.sources.complete.binaryArchives = ['a'.repeat(64)];
  await assert.rejects(f.stage(), /has not been reviewed/);
});

test('as-is runtime reuse verifies exact published bytes without requiring rewritten notices', async t => {
  const f = await fixture(t), file = path.join(f.input, 'ffmpeg.tar.gz');
  const entry = { package: { type: 'as-is', format: 'tar.gz' } };
  await verifyReusableAsset(file, 'ffmpeg', entry, f.config.upstream.binary, 'not used for unmodified archives');
  await fs.appendFile(file, 'altered');
  await assert.rejects(verifyReusableAsset(file, 'ffmpeg', entry, f.config.upstream.binary, ''), /Checksum mismatch/);
});

test('linked release staging uses the verified files without changing their bytes', async t => {
  const f = await fixture(t);
  const before = await fs.readFile(path.join(f.input, 'ffmpeg.tar.gz'));
  const names = await stageRelease(f.input, f.output, f.config, f.key, undefined, { link: true });
  for (const name of names) {
    const source = await fs.stat(path.join(f.input, name)), staged = await fs.stat(path.join(f.output, name));
    assert.equal(staged.dev, source.dev);
    assert.equal(staged.ino, source.ino);
  }
  assert.deepEqual(await fs.readFile(path.join(f.input, 'ffmpeg.tar.gz')), before);
});

test('failed release links clean only the new destination and never fall back to copies', async t => {
  const f = await fixture(t), originalLink = fs.link;
  let links = 0;
  const link = t.mock.method(fs, 'link', async (...args) => {
    if (++links === 2) throw Object.assign(new Error('different filesystem'), { code: 'EXDEV' });
    return originalLink(...args);
  });
  const copy = t.mock.method(fs, 'copyFile', async () => { throw new Error('copy fallback is not allowed'); });
  await assert.rejects(stageRelease(f.input, f.output, f.config, f.key, undefined, { link: true }), { code: 'EXDEV' });
  assert.equal(copy.mock.callCount(), 0);
  await assert.rejects(fs.access(f.output), { code: 'ENOENT' });
  assert.equal(await fs.readFile(path.join(f.input, 'ffmpeg.tar.gz'), 'utf8'), 'fixture');
  link.mock.restore();
  copy.mock.restore();
});

test('linked mode retains checksum rejection and refuses an existing release directory', async t => {
  const f = await fixture(t);
  await fs.writeFile(path.join(f.input, 'ffmpeg.tar.gz'), 'mutated');
  await assert.rejects(stageRelease(f.input, f.output, f.config, f.key, undefined, { link: true }), /exited with/);
  await assert.rejects(fs.access(f.output), { code: 'ENOENT' });
  await fs.writeFile(path.join(f.input, 'ffmpeg.tar.gz'), 'fixture');
  await fs.mkdir(f.output);
  await fs.writeFile(path.join(f.output, 'keep.txt'), 'preserve');
  await assert.rejects(stageRelease(f.input, f.output, f.config, f.key, undefined, { link: true }), { code: 'EEXIST' });
  assert.deepEqual(await fs.readdir(f.output), ['keep.txt']);
});


test('direct installer versions must be safe and match the reviewed configuration', async t => {
  const f = await vendorFixture(t), original = f.item.version;
  f.item.version = '2.0';
  await f.resign();
  await assert.rejects(f.stage(), /Vendor delivery differs from configuration/);
  for (const version of ['', '../1', '-1', '1 beta', 'é1', 'a'.repeat(65)]) {
    assert.throws(() => vendorDelivery('magick', 'windows_x86_64', { ...f.configured, version }, f.config.upstream.binary), /Invalid direct vendor/);
    f.item.version = version;
    await f.resign();
    await assert.rejects(f.stage(), /exited with/);
  }
  for (const version of ['7.1.1-47', '26.2.6.3', 'A_1', 'a'.repeat(64)]) {
    assert.equal(vendorDelivery('magick', 'windows_x86_64', { ...f.configured, version }, f.config.upstream.binary).version, version);
  }
  f.item.version = original;
  await f.resign();
  await f.stage();
});
