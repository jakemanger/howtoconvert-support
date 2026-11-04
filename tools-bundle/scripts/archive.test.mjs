import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { createTarGz, verifyArchiveNotice } from './lib.mjs';

const realTar = spawnSync('which', ['tar'], { encoding: 'utf8' }).stdout.trim();

test('reused archives must contain current notices, not earlier draft notes', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'htc-notice-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await fs.mkdir(path.join(root, 'tool'));
  await fs.writeFile(path.join(root, 'tool/HTC-NOTICE.txt'), 'Current release notices\n');
  const archive = path.join(root, 'tool.tar.gz');
  createTarGz(root, 'tool', archive);
  verifyArchiveNotice(archive, 'tool', 'Current release notices\n');
  assert.throws(() => verifyArchiveNotice(archive, 'tool', 'Updated release notices\n'), /Stale package notices/);
  await fs.rm(path.join(root, 'tool/HTC-NOTICE.txt'));
  createTarGz(root, 'tool', archive);
  assert.throws(() => verifyArchiveNotice(archive, 'tool', 'Current release notices\n'), /exited with/);
});

test('packages preserve an executable and its relative symlink', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'htc-archive-test-'));
  try {
    await fs.mkdir(path.join(root, 'tool/bin'), { recursive: true });
    await fs.writeFile(path.join(root, 'tool/bin/run'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
    await fs.symlink('run', path.join(root, 'tool/bin/alias'));
    const output = path.join(root, 'tool.tar.gz');
    createTarGz(root, 'tool', output);
    const unpacked = path.join(root, 'unpacked');
    await fs.mkdir(unpacked);
    const extracted = spawnSync(realTar, ['-xzf', output, '-C', unpacked]);
    assert.equal(extracted.status, 0);
    assert.equal(await fs.readlink(path.join(unpacked, 'tool/bin/alias')), 'run');
    assert.ok((await fs.stat(path.join(unpacked, 'tool/bin/run'))).mode & 0o111);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('a damaged generated archive is rejected without replacing the previous file', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'htc-archive-test-'));
  const originalPath = process.env.PATH;
  try {
    await fs.mkdir(path.join(root, 'tool'));
    await fs.writeFile(path.join(root, 'tool/input'), 'content');
    const output = path.join(root, 'tool.tar.gz');
    const previous = Buffer.from('previous reviewed artifact');
    await fs.writeFile(output, previous);
    const bin = path.join(root, 'bin');
    await fs.mkdir(bin);
    await fs.writeFile(path.join(bin, 'tar'), `#!${process.execPath}\nconst {spawnSync}=require('node:child_process');\nconst fs=require('node:fs');\nconst args=process.argv.slice(2);\nconst result=spawnSync(${JSON.stringify(realTar)},args,{stdio:'inherit'});\nconst index=args.indexOf('-czf');\nif(result.status===0&&index>=0)fs.writeFileSync(args[index+1],'damaged output');\nprocess.exit(result.status ?? 1);\n`, { mode: 0o755 });
    process.env.PATH = `${bin}${path.delimiter}${originalPath}`;
    assert.throws(() => createTarGz(root, 'tool', output), /exited with/);
    assert.deepEqual(await fs.readFile(output), previous);
    await assert.rejects(fs.stat(`${output}.partial`), { code: 'ENOENT' });
  } finally {
    process.env.PATH = originalPath;
    await fs.rm(root, { recursive: true, force: true });
  }
});
