#!/usr/bin/env node
// Verify a bundle and copy only its declared release files into a new directory.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { run, sha256File, sourceAssetName } from './lib.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));

export async function stageRelease(directory, destination, config, publicKey) {
  const input = path.resolve(directory);
  const output = path.resolve(destination);
  run(process.execPath, [path.join(here, 'verify-bundle.mjs'), input, publicKey || ''], { stdio: 'pipe' });
  const manifest = JSON.parse(await fs.readFile(path.join(input, 'manifest.json'), 'utf8'));
  if (manifest.bundle !== config.bundle) throw new Error('Manifest and configuration bundle IDs differ');
  const files = new Set(['manifest.json', 'manifest.json.sig', 'SOURCES.md']);
  const sources = new Set();
  for (const tool of Object.values(manifest.tools)) {
    for (const item of Object.values(tool.platforms)) {
      files.add(item.asset);
      files.add(item.correspondingSource.asset);
      for (const id of item.sources || []) sources.add(id);
    }
  }
  for (const id of sources) {
    const source = config.sources[id];
    if (!source) throw new Error(`Unknown source: ${id}`);
    if (!source.attach) continue;
    const name = sourceAssetName(id, source);
    if (await sha256File(path.join(input, name)) !== source.sha256) {
      throw new Error(`Source checksum mismatch: ${name}`);
    }
    files.add(name);
  }
  const names = [...files].sort();
  for (const name of names) {
    if (typeof name !== 'string' || !name || /[/\\]/.test(name) || name === '.' || name === '..') {
      throw new Error('Release assets must be plain filenames');
    }
    if (!(await fs.lstat(path.join(input, name))).isFile()) throw new Error(`Not a regular file: ${name}`);
  }
  // Refuse an existing destination so stale files can never join the upload.
  await fs.mkdir(output);
  try {
    for (const name of names) await fs.copyFile(path.join(input, name), path.join(output, name));
  } catch (error) {
    await fs.rm(output, { recursive: true, force: true });
    throw error;
  }
  return names;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    const config = JSON.parse(await fs.readFile(path.join(here, '../bundle.config.json'), 'utf8'));
    const names = await stageRelease(process.argv[2] || 'dist', process.argv[3] || 'release', config, process.env.TOOLS_MANIFEST_PUBLIC_KEY);
    console.log(`Staged ${names.length} release files.`);
  } catch (error) {
    console.error(error.stderr?.toString().trim() || error.message);
    process.exitCode = 1;
  }
}
