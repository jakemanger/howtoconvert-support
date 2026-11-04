import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { resolveSourceArchive } from './lib.mjs';

async function fixture(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'bundle-source-import-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const data = 'matching source';
  const source = { filename: 'project.tar.gz', sha256: createHash('sha256').update(data).digest('hex') };
  return { directory, data, source };
}

test('local original filename is hash verified without a remote URL', async t => {
  const { directory, data, source } = await fixture(t);
  const file = path.join(directory, source.filename);
  await fs.writeFile(file, data);
  assert.equal(await resolveSourceArchive('project', source, '/unused', [directory]), file);
});

test('local release asset name is accepted and unrelated files are ignored', async t => {
  const { directory, data, source } = await fixture(t);
  const file = path.join(directory, 'source-project.tar.gz');
  await fs.writeFile(file, data);
  await fs.writeFile(path.join(directory, 'private-notes.txt'), 'not a source');
  assert.equal(await resolveSourceArchive('project', source, '/unused', [path.join(directory, 'missing'), directory]), file);
});

test('a present wrong source fails instead of falling back to a URL or later directory', async t => {
  const { directory, source } = await fixture(t);
  await fs.writeFile(path.join(directory, source.filename), 'different content');
  await assert.rejects(resolveSourceArchive('project', source, '/unused', [directory]), /Checksum mismatch/);
});

test('symlinks and directories cannot be used as local sources', async t => {
  const { directory, data, source } = await fixture(t);
  const file = path.join(directory, source.filename);
  await fs.writeFile(path.join(directory, 'target'), data);
  await fs.symlink('target', file);
  await assert.rejects(resolveSourceArchive('project', source, '/unused', [directory]), /regular file/);
  await fs.unlink(file);
  await fs.mkdir(file);
  await assert.rejects(resolveSourceArchive('project', source, '/unused', [directory]), /regular file/);
});

test('invalid source filenames and checksums fail before opening local files', async t => {
  const { directory, source } = await fixture(t);
  await assert.rejects(resolveSourceArchive('project', { ...source, filename: '../private' }, '/unused', [directory]), /plain filenames/);
  await assert.rejects(resolveSourceArchive('project', { ...source, sha256: '' }, '/unused', [directory]), /Invalid source checksum/);
});
