import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { copyReleaseFile } from './lib.mjs';
import { stageRelease } from './stage-release.mjs';

for (const link of [false, true]) {
  test(`assembly ${link ? 'links' : 'copies'} checked as-is runtimes and source archives`, async t => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'htc-link-assembly-'));
    t.after(() => fs.rm(root, { recursive: true, force: true }));
    const scripts = path.join(root, 'scripts'), work = path.join(root, 'work'), out = path.join(root, 'dist');
    await fs.mkdir(scripts);
    for (const file of ['build-bundle.mjs', 'lib.mjs']) await fs.copyFile(new URL(file, import.meta.url), path.join(scripts, file));
    const sha = bytes => createHash('sha256').update(bytes).digest('hex');
    const binary = Buffer.from('pinned original runtime'), source = Buffer.from('pinned complete source');
    const config = { schema: 1, bundle: 'tools-2026.11.0', repository: 'example/tools',
      upstream: { binary: { url: 'https://example.invalid/original.tar.gz', filename: 'original.tar.gz', sha256: sha(binary) } },
      sources: { 'tool-1~2': { title: 'Complete source', attach: true, url: 'https://example.invalid/tool-1~2.tar.gz', filename: 'tool-1~2.tar.gz', sha256: sha(source), binaryArchives: [sha(binary)], includesBuildInstructions: true } },
      tools: { ffmpeg: { name: 'FFmpeg', license: 'GPL', homepage: 'https://example.invalid', verifyArgs: ['-version'], platforms: {
        mac_arm64: { version: '1', bin: 'ffmpeg/ffmpeg', package: { type: 'as-is', format: 'tar.gz', upstream: 'binary' }, sourceBundle: ['tool-1~2'] },
      } } } };
    await fs.writeFile(path.join(root, 'bundle.config.json'), JSON.stringify(config));
    await fs.mkdir(path.join(work, 'downloads'), { recursive: true });
    await fs.mkdir(path.join(work, 'sources'));
    const original = path.join(work, 'downloads/original.tar.gz'), sourceOriginal = path.join(work, 'sources/tool-1~2.tar.gz');
    await fs.writeFile(original, binary); await fs.writeFile(sourceOriginal, source);
    const run = spawnSync(process.execPath, [path.join(scripts, 'build-bundle.mjs'), '--out', out, '--work', work, ...(link ? ['--link'] : [])], { cwd: root, encoding: 'utf8' });
    assert.equal(run.status, 0, run.stderr);
    for (const [input, output, bytes] of [[original, 'ffmpeg-1-mac_arm64.tar.gz', binary], [sourceOriginal, 'source-tool-1.2.tar.gz', source]]) {
      const a = await fs.stat(input), b = await fs.stat(path.join(out, output));
      assert.equal(a.dev === b.dev && a.ino === b.ino, link);
      assert.deepEqual(await fs.readFile(input), bytes);
      assert.deepEqual(await fs.readFile(path.join(out, output)), bytes);
    }
    const manifest = JSON.parse(await fs.readFile(path.join(out, 'manifest.json')));
    assert.equal(manifest.tools.ffmpeg.platforms.mac_arm64.sha256, sha(binary));
    const entry = manifest.tools.ffmpeg.platforms.mac_arm64;
    assert.deepEqual(entry.sourceBundle, ['tool-1~2']);
    assert.deepEqual(entry.correspondingSource, [{ asset: 'source-tool-1.2.tar.gz', sha256: sha(source), size: source.length }]);
    const docs = await fs.readFile(path.join(out, 'SOURCES.md'), 'utf8');
    assert.ok(docs.includes('https://example.invalid/tool-1~2.tar.gz · attached as `source-tool-1.2.tar.gz`'));
    const { privateKey, publicKey } = generateKeyPairSync('ed25519');
    const signed = sign(null, await fs.readFile(path.join(out, 'manifest.json')), privateKey);
    await fs.writeFile(path.join(out, 'manifest.json.sig'), signed.toString('base64'));
    const key = Buffer.from(publicKey.export({ format: 'jwk' }).x, 'base64url').toString('base64');
    const files = await stageRelease(out, path.join(root, 'release'), config, key);
    assert.ok(files.includes('source-tool-1.2.tar.gz'));
    assert.ok(files.every(name => !name.includes('~')));
  });
}

test('archive linking refuses symlinks and never overwrites an existing destination', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'htc-link-safety-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const input = path.join(root, 'input'), output = path.join(root, 'output'), symlink = path.join(root, 'symlink');
  await fs.writeFile(input, 'original'); await fs.writeFile(output, 'keep'); await fs.symlink(input, symlink);
  await assert.rejects(copyReleaseFile(symlink, path.join(root, 'new'), true), /Not a regular file/);
  await assert.rejects(copyReleaseFile(input, output, true), { code: 'EEXIST' });
  assert.equal(await fs.readFile(input, 'utf8'), 'original');
  assert.equal(await fs.readFile(output, 'utf8'), 'keep');
});
