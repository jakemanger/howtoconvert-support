import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { selectPlatforms } from './select-platforms.mjs';
import { PLATFORMS } from './lib.mjs';

test('selection accepts known platforms and identifies when Windows packaging is needed', () => {
  assert.deepEqual(selectPlatforms('mac_arm64, windows_x86_64'), { platforms: 'mac_arm64,windows_x86_64', windows: true });
  assert.deepEqual(selectPlatforms('mac_arm64'), { platforms: 'mac_arm64', windows: false });
  assert.deepEqual(selectPlatforms('all'), { platforms: PLATFORMS.join(','), windows: true });
  for (const platform of PLATFORMS) assert.equal(selectPlatforms(platform).platforms, platform);
});

test('empty, unknown, duplicate and injected selections are rejected', () => {
  for (const input of [undefined, '', ' ', 'all,mac_arm64', 'windows', 'mac_arm64,',
    'mac_arm64,mac_arm64', 'mac_arm64;echo bad', 'mac_arm64\nwindows=true']) {
    assert.throws(() => selectPlatforms(input));
  }
});

test('workflow output is validated before anything is written', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'htc-platform-output-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const output = path.join(root, 'output');
  const run = input => spawnSync(process.execPath, [new URL('select-platforms.mjs', import.meta.url).pathname], {
    env: { ...process.env, INPUT_PLATFORMS: input, GITHUB_OUTPUT: output }, encoding: 'utf8',
  });
  assert.notEqual(run('mac_arm64\nwindows=true').status, 0);
  await assert.rejects(fs.stat(output), { code: 'ENOENT' });
  assert.equal(run('mac_arm64').status, 0);
  assert.equal(await fs.readFile(output, 'utf8'), 'platforms=mac_arm64\nwindows=false\n');
});

test('selected-platform preflight rejects any selected tool with incomplete sources', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'htc-selected-preflight-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const scripts = path.join(root, 'scripts'); await fs.mkdir(scripts);
  for (const file of ['build-bundle.mjs', 'lib.mjs']) await fs.copyFile(new URL(file, import.meta.url), path.join(scripts, file));
  const reviewed = { package: { upstream: 'binary' }, sourceBundle: 'source' };
  const config = { schema: 1, upstream: { binary: { sha256: 'a'.repeat(64) } }, tools: {
    ffmpeg: { platforms: { mac_arm64: reviewed, windows_x86_64: reviewed, linux_arm64: { package: { upstream: 'binary' } } } },
    magick: { platforms: { mac_arm64: reviewed, windows_x86_64: reviewed } },
  }, sources: { source: { attach: true, sha256: 'b'.repeat(64), includesBuildInstructions: true, binaryArchives: ['a'.repeat(64)] } } };
  const write = () => fs.writeFile(path.join(root, 'bundle.config.json'), JSON.stringify(config));
  const run = selection => spawnSync(process.execPath, [path.join(scripts, 'build-bundle.mjs'), '--check', '--platform', selection], { cwd: root, encoding: 'utf8' });
  await write();
  assert.equal(run('mac_arm64,windows_x86_64').status, 0);
  assert.match(run(PLATFORMS.join(',')).stderr, /ffmpeg\/linux_arm64/);
  config.tools.magick.platforms.windows_x86_64 = { package: { upstream: 'binary' } };
  await write();
  const incomplete = run('mac_arm64,windows_x86_64');
  assert.notEqual(incomplete.status, 0);
  assert.match(incomplete.stderr, /magick\/windows_x86_64/);
  assert.equal(run('mac_arm64').status, 0);
  assert.deepEqual((await fs.readdir(root)).sort(), ['bundle.config.json', 'scripts']);
});
