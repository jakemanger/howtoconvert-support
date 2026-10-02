// Shared helpers for assembling the conversion tools bundle.
import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';
import { spawnSync } from 'node:child_process';

export const PLATFORMS = ['mac_arm64', 'mac_x86_64', 'linux_x86_64', 'linux_arm64', 'windows_x86_64'];
export const TOOL_IDS = ['ffmpeg', 'pandoc', 'magick', 'libreoffice', 'tex'];

export async function sha256File(file) {
  const hash = createHash('sha256');
  await pipeline(createReadStream(file), hash);
  return hash.digest('hex');
}

export async function exists(file) {
  try { await fs.access(file); return true; } catch { return false; }
}

// Downloads once into the cache and refuses any file whose checksum differs
// from the pinned value, so a moved or tampered upstream fails the build.
export async function fetchPinned({ url, sha256, filename }, cacheDirectory, log = console.log) {
  if (!sha256) throw new Error(`No pinned sha256 for ${url}`);
  await fs.mkdir(cacheDirectory, { recursive: true });
  const target = path.join(cacheDirectory, filename || decodeURIComponent(new URL(url).pathname.split('/').pop()));
  if (await exists(target) && await sha256File(target) === sha256) return target;
  log(`Downloading ${url}`);
  const response = await fetch(url, { redirect: 'follow' });
  if (!response.ok) throw new Error(`Download failed (${response.status}) for ${url}`);
  const partial = `${target}.partial`;
  await pipeline(Readable.fromWeb(response.body), createWriteStream(partial));
  const actual = await sha256File(partial);
  if (actual !== sha256) {
    await fs.rm(partial, { force: true });
    throw new Error(`Checksum mismatch for ${url}\n  expected ${sha256}\n  actual   ${actual}`);
  }
  await fs.rename(partial, target);
  return target;
}

export function run(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: 'inherit', ...options });
  if (result.error) throw new Error(`${command} could not start: ${result.error.message}`);
  if (result.status !== 0) throw new Error(`${command} ${args.join(' ')} exited with ${result.status}`);
  return result;
}

export function commandExists(command) {
  const probe = process.platform === 'win32' ? 'where' : 'which';
  return spawnSync(probe, [command], { stdio: 'ignore' }).status === 0;
}

// Copies a directory tree, keeping symbolic links as links (TeX Live and
// ImageMagick builds rely on them) and dropping Finder metadata.
export async function copyTree(source, destination) {
  const stat = await fs.lstat(source);
  const name = path.basename(source);
  if (name === '.DS_Store' || name === '__MACOSX' || name.startsWith('._')) return;
  if (stat.isSymbolicLink()) {
    await fs.mkdir(path.dirname(destination), { recursive: true });
    await fs.symlink(await fs.readlink(source), destination);
  } else if (stat.isDirectory()) {
    await fs.mkdir(destination, { recursive: true });
    for (const entry of await fs.readdir(source)) {
      await copyTree(path.join(source, entry), path.join(destination, entry));
    }
  } else {
    await fs.mkdir(path.dirname(destination), { recursive: true });
    await fs.copyFile(source, destination);
    await fs.chmod(destination, stat.mode & 0o7777);
  }
}

// Finds the single directory that contains `relative` (e.g. program/soffice.com).
export async function findContaining(root, relative) {
  const queue = [root];
  while (queue.length) {
    const directory = queue.shift();
    if (await exists(path.join(directory, relative))) return directory;
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      if (entry.isDirectory()) queue.push(path.join(directory, entry.name));
    }
  }
  return null;
}

// Every archive has exactly one top-level folder named after the tool, so the
// whole tool can be extracted and swapped in as one folder.
export function createTarGz(stagingRoot, topLevel, outputFile) {
  const args = ['-czf', outputFile, '-C', stagingRoot, topLevel];
  const gnu = spawnSync('tar', ['--version'], { encoding: 'utf8' }).stdout?.includes('GNU');
  if (gnu) args.unshift('--owner=0', '--group=0', '--numeric-owner', '--sort=name');
  run('tar', args);
}

export function assetName(tool, version, platform, format) {
  const safeVersion = String(version).replace(/[^A-Za-z0-9._-]/g, '_');
  return `${tool}-${safeVersion}-${platform}.${format}`;
}

export function sourceAssetName(id, source) {
  const filename = source.filename || decodeURIComponent(new URL(source.url).pathname.split('/').pop());
  return `source-${filename.startsWith(id) ? filename : `${id}-${filename}`}`;
}
