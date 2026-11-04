import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { createHash } from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';
import { cacheBase, collectCacheInputs, stageInputCache, importInputCache } from './input-cache.mjs';
import { sha256File } from './lib.mjs';

const hash = value => createHash('sha256').update(value).digest('hex');
const base = 'https://downloads.example.test/tool-inputs/';
async function fixture(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'bundle-input-cache-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const local = path.join(directory, 'local'); await fs.mkdir(local);
  const binary = Buffer.from('pinned binary'), source = Buffer.from('matching source and build instructions');
  await fs.writeFile(path.join(local, 'binary.tar.gz'), binary);
  await fs.writeFile(path.join(local, 'source.tar.gz'), source);
  await fs.writeFile(path.join(local, 'private-notes.txt'), 'must remain local');
  const config = { schema: 1, repository: 'example/support', bundle: 'tools-test', upstream: {
    binary: { url: 'https://github.com/example/support/releases/download/tools-test/binary.tar.gz', sha256: hash(binary) },
  }, tools: { ffmpeg: { name: 'FFmpeg', license: 'test fixture', homepage: 'https://example.test', licenseFiles: [],
    verifyArgs: ['-version'], platforms: { mac_arm64: { version: '1', minimumOS: '14.0',
      package: { type: 'tar-gz-dir', upstream: 'binary', dir: 'build' }, bin: 'ffmpeg/ffmpeg',
      sourceBundle: ['source'], sources: [], origin: 'Test fixture' } } } }, sources: {
    source: { title: 'Build source', url: 'https://github.com/example/support/releases/download/tools-test/source-source.tar.gz',
      filename: 'source.tar.gz', sha256: hash(source), attach: true, includesBuildInstructions: true, binaryArchives: [hash(binary)] },
  } };
  return { directory, local, config, binary, source, output: path.join(directory, 'cache') };
}

function runNode(args, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', data => { stdout += data; });
    child.stderr.on('data', data => { stderr += data; });
    child.once('error', reject);
    child.once('exit', code => resolve({ code, stdout, stderr }));
  });
}

test('cache URLs reject credentials, unsafe protocols, queries and fragments', () => {
  assert.equal(cacheBase('https://example.test/cache'), 'https://example.test/cache/');
  assert.equal(cacheBase('http://127.0.0.1:1234/cache'), 'http://127.0.0.1:1234/cache/');
  for (const url of ['http://example.test/cache', 'file:///tmp/cache', 'https://user:password@example.test/',
    'https://example.test/?token=secret', 'https://example.test/#fragment']) assert.throws(() => cacheBase(url), /HTTPS base URL/);
});

test('staging allowlists reviewed inputs and writes an immutable configuration without editing the original', async t => {
  const f = await fixture(t), before = JSON.stringify(f.config);
  const result = await stageInputCache(f.config, base, [f.local], f.output);
  assert.equal(result.length, 2);
  assert.deepEqual((await fs.readdir(path.join(f.output, 'objects'))).sort(), [hash(f.binary), hash(f.source)].sort());
  assert.equal(JSON.stringify(f.config), before);
  const overlay = JSON.parse(await fs.readFile(path.join(f.output, 'bundle.config.json')));
  assert.equal(overlay.upstream.binary.url, base + hash(f.binary));
  assert.equal(overlay.upstream.binary.filename, 'binary.tar.gz');
  assert.equal(overlay.sources.source.url, base + hash(f.source));
  assert.deepEqual(overlay.sources.source.binaryArchives, [hash(f.binary)]);
  assert.equal(collectCacheInputs(overlay, base).length, 2);
  assert.equal((await fs.readFile(path.join(f.output, 'inputs.json'), 'utf8')).includes(f.local), false);
  await assert.rejects(stageInputCache(f.config, base, [f.local], f.output), /already exists/);
});

test('incomplete sources, changed inputs and input symlinks fail before an output is created', async t => {
  const f = await fixture(t);
  delete f.config.sources.source.includesBuildInstructions;
  await assert.rejects(stageInputCache(f.config, base, [f.local], f.output), /source has not been reviewed/);
  f.config.sources.source.includesBuildInstructions = true;
  await fs.writeFile(path.join(f.local, 'source.tar.gz'), 'wrong source');
  await assert.rejects(stageInputCache(f.config, base, [f.local], f.output), /Checksum mismatch/);
  await fs.rm(path.join(f.local, 'source.tar.gz'));
  await fs.symlink('private-notes.txt', path.join(f.local, 'source.tar.gz'));
  await assert.rejects(stageInputCache(f.config, base, [f.local], f.output), /regular file/);
  await assert.rejects(fs.stat(f.output), { code: 'ENOENT' });
});

test('only the selected reviewed platforms enter a cache; other publishers stay upstream', async t => {
  const f = await fixture(t);
  f.config.tools.ffmpeg.platforms.windows_x86_64 = { package: { upstream: 'unreviewed' } };
  assert.throws(() => collectCacheInputs(f.config, base), /windows_x86_64/);
  assert.equal(collectCacheInputs(f.config, base, { platforms: ['mac_arm64'] }).length, 2);
  f.config.upstream.binary.url = 'https://publisher.example.test/binary.tar.gz';
  assert.deepEqual(collectCacheInputs(f.config, base, { platforms: ['mac_arm64'] }).map(i => i.kind), ['source']);
  f.config.sources.source.filename = '../private-notes.txt';
  assert.throws(() => collectCacheInputs(f.config, base, { platforms: ['mac_arm64'] }), /plain/);
});

test('a selected-platform release builds only reviewed cache inputs while release URLs are unpublished', async t => {
  const f = await fixture(t);
  f.config.tools.ffmpeg.platforms.linux_arm64 = { package: { upstream: 'unreviewed' } };
  const selection = { platforms: ['mac_arm64'] };
  const tree = path.join(f.directory, 'raw'); await fs.mkdir(path.join(tree, 'build'), { recursive: true });
  await fs.writeFile(path.join(tree, 'build/ffmpeg'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  const packed = spawnSync('tar', ['-czf', path.join(f.local, 'binary.tar.gz'), '-C', tree, 'build']);
  assert.equal(packed.status, 0);
  f.config.upstream.binary.sha256 = await sha256File(path.join(f.local, 'binary.tar.gz'));
  f.config.sources.source.binaryArchives = [f.config.upstream.binary.sha256];
  let serveWrongSource = false;
  const hits = [];
  const server = http.createServer(async (request, response) => {
    const name = request.url.split('/').pop(); hits.push(name);
    try {
      const data = serveWrongSource && name === hash(f.source) ? Buffer.from('corrupted source') : await fs.readFile(path.join(f.output, 'objects', name));
      response.writeHead(200).end(data);
    } catch { response.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const url = `http://127.0.0.1:${server.address().port}/inputs/`;
  await stageInputCache(f.config, url, [f.local], f.output, selection);
  const work = path.join(f.directory, 'work');
  await importInputCache(f.config, url, work, selection, () => {});
  assert.equal(hits.length, 2);
  const tools = path.join(f.directory, 'tools-bundle'); await fs.mkdir(path.join(tools, 'scripts'), { recursive: true });
  for (const filename of ['build-bundle.mjs', 'lib.mjs']) await fs.copyFile(new URL(filename, import.meta.url), path.join(tools, 'scripts', filename));
  await fs.writeFile(path.join(tools, 'bundle.config.json'), JSON.stringify(f.config));
  const result = await runNode([path.join(tools, 'scripts/build-bundle.mjs'), '--out', 'dist', '--work', work, '--platform', 'mac_arm64'], f.directory);
  assert.equal(result.code, 0, result.stderr);
  const manifest = JSON.parse(await fs.readFile(path.join(f.directory, 'dist/manifest.json')));
  assert.deepEqual(Object.keys(manifest.tools.ffmpeg.platforms), ['mac_arm64']);
  assert.equal(manifest.tools.ffmpeg.platforms.mac_arm64.upstreamSha256, f.config.upstream.binary.sha256);
  assert.equal(await sha256File(path.join(f.directory, 'dist/source-source.tar.gz')), hash(f.source));
  assert.equal(hits.length, 2, 'assembly should reuse checked inputs, not re-download');
  // A clean clone with the reviewed overlay can also build directly, without
  // relying on a workflow-only cache or the not-yet-created release.
  await fs.copyFile(path.join(f.output, 'bundle.config.json'), path.join(tools, 'bundle.config.json'));
  const direct = await runNode([path.join(tools, 'scripts/build-bundle.mjs'), '--out', 'direct-dist', '--work', path.join(f.directory, 'direct-work'), '--platform', 'mac_arm64'], f.directory);
  assert.equal(direct.code, 0, direct.stderr);
  assert.equal(hits.length, 4);
  assert.equal(await sha256File(path.join(f.directory, 'direct-dist/source-source.tar.gz')), hash(f.source));
  serveWrongSource = true;
  await assert.rejects(importInputCache(f.config, url, path.join(f.directory, 'bad-work'), selection, () => {}), /Checksum mismatch/);
  await assert.rejects(fs.stat(path.join(f.directory, 'bad-work/sources/source.tar.gz')), { code: 'ENOENT' });
});
