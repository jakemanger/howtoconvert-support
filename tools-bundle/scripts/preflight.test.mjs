import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

test('preflight validates source mappings without downloads or build directories', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'htc-preflight-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const scripts = path.join(root, 'scripts');
  await fs.mkdir(scripts);
  for (const file of ['build-bundle.mjs', 'lib.mjs']) await fs.copyFile(new URL(file, import.meta.url), path.join(scripts, file));
  const config = { schema: 1, upstream: { binary: { sha256: 'a'.repeat(64) } }, tools: {
    ffmpeg: { platforms: { mac_arm64: { sourceBundle: 'source', package: { upstream: 'binary' } } } },
  }, sources: { source: { attach: true, sha256: 'b'.repeat(64), includesBuildInstructions: true, binaryArchives: ['a'.repeat(64)] } } };
  const write = () => fs.writeFile(path.join(root, 'bundle.config.json'), JSON.stringify(config));
  const run = (...args) => spawnSync(process.execPath, [path.join(scripts, 'build-bundle.mjs'), '--check', ...args], { cwd: root, encoding: 'utf8' });
  await write();
  assert.equal(run().status, 0);
  assert.notEqual(run('--skip-sources').status, 0);
  delete config.tools.ffmpeg.platforms.mac_arm64.sourceBundle;
  await write();
  const missing = run();
  assert.notEqual(missing.status, 0);
  assert.match(missing.stderr, /Complete corresponding source has not been reviewed/);
  assert.deepEqual((await fs.readdir(root)).sort(), ['bundle.config.json', 'scripts']);
});
