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
  const config = { bundle: 'tools-2026.10.0', sources: {
    fixture: { attach: true, sha256, filename: 'fixture.tar.gz' },
  } };
  const manifest = { schema: 1, bundle: config.bundle, tools: { ffmpeg: { platforms: { mac_arm64: {
    version: '1', asset: 'ffmpeg.tar.gz', format: 'tar.gz', bin: 'ffmpeg/ffmpeg', sha256, size: bytes.length,
    sources: ['fixture'], correspondingSource: { asset: 'source-complete.tar.gz', sha256, size: bytes.length },
  } } } } };
  for (const file of ['ffmpeg.tar.gz', 'source-complete.tar.gz', 'source-fixture.tar.gz']) await fs.writeFile(path.join(input, file), bytes);
  await fs.writeFile(path.join(input, 'SOURCES.md'), '# Source downloads\n');
  const { privateKey, publicKey } = generateKeyPairSync('ed25519');
  const data = Buffer.from(JSON.stringify(manifest));
  await fs.writeFile(path.join(input, 'manifest.json'), data);
  await fs.writeFile(path.join(input, 'manifest.json.sig'), sign(null, data, privateKey).toString('base64'));
  const key = Buffer.from(publicKey.export({ format: 'jwk' }).x, 'base64url').toString('base64');
  return { input, output, config, stage: () => stageRelease(input, output, config, key) };
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
