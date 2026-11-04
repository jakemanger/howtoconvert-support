// Shared helpers for assembling the conversion tools bundle.
import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream, renameSync, rmSync } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
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
  const partial = `${outputFile}.partial`;
  const args = ['-czf', partial, '-C', stagingRoot, topLevel];
  const gnu = spawnSync('tar', ['--version'], { encoding: 'utf8' }).stdout?.includes('GNU');
  if (gnu) args.unshift('--owner=0', '--group=0', '--numeric-owner', '--sort=name');
  try {
    // macOS tar otherwise inserts AppleDouble ._ files outside the tool folder.
    run('tar', args, { env: { ...process.env, COPYFILE_DISABLE: '1' } });
    // Check the written archive, not just tar's successful creation exit code.
    run('tar', ['-tzf', partial], { stdio: 'ignore' });
    renameSync(partial, outputFile);
  } catch (error) {
    rmSync(partial, { force: true });
    throw error;
  }
}

export function verifyArchiveNotice(archive, tool, expected) {
  run('tar', ['-tzf', archive], { stdio: 'ignore' });
  const result = run('tar', ['-xOzf', archive, `${tool}/HTC-NOTICE.txt`], { encoding: 'utf8', stdio: 'pipe' });
  if (result.stdout !== expected) {
    throw new Error(`Stale package notices in ${path.basename(archive)}; rebuild without --reuse`);
  }
}

export function assetName(tool, version, platform, format) {
  const safeVersion = String(version).replace(/[^A-Za-z0-9._-]/g, '_');
  return `${tool}-${safeVersion}-${platform}.${format}`;
}

export function isPlainAssetName(value) {
  return typeof value === 'string' && value.length > 0 && !/[/\\\x00-\x1f\x7f]/.test(value)
    && value !== '.' && value !== '..';
}

export function sourceAssetName(id, source) {
  const filename = source.filename || decodeURIComponent(new URL(source.url).pathname.split('/').pop());
  if (!isPlainAssetName(id) || !isPlainAssetName(filename)) throw new Error('Source assets must be plain filenames');
  return `source-${filename.startsWith(id) ? filename : `${id}-${filename}`}`;
}

// Local collectors can supply first-release sources before their public URLs exist.
// Only the expected plain filename is read, and it must match the published pin.
export async function resolveSourceArchive(id, source, cacheDirectory, sourceDirectories = []) {
  const asset = sourceAssetName(id, source);
  const filename = source.filename || decodeURIComponent(new URL(source.url).pathname.split('/').pop());
  if (!/^[a-f0-9]{64}$/.test(source.sha256 || '')) throw new Error(`Invalid source checksum for ${id}`);
  for (const directory of sourceDirectories) {
    for (const name of new Set([asset, filename])) {
      const candidate = path.join(directory, name);
      let stat;
      try { stat = await fs.lstat(candidate); } catch (error) {
        if (error.code === 'ENOENT') continue;
        throw error;
      }
      if (!stat.isFile()) throw new Error(`Local source must be a regular file: ${name}`);
      if (await sha256File(candidate) !== source.sha256) throw new Error(`Checksum mismatch for local source ${name}`);
      return candidate;
    }
  }
  return fetchPinned(source, cacheDirectory);
}

// A corresponding-source set may be split into separately downloadable archives.
export function sourceBundleIds(entry) {
  const value = entry?.sourceBundle;
  return Array.isArray(value) ? value : value ? [value] : [];
}

export function sourceAttachments(entry) {
  const value = entry?.correspondingSource;
  return Array.isArray(value) ? value : value ? [value] : [];
}

export function hasReviewedSourceBundle(entry, config) {
  const ids = sourceBundleIds(entry);
  const binaryHash = config.upstream?.[entry?.package?.upstream]?.sha256;
  return /^[a-f0-9]{64}$/.test(binaryHash || '') && ids.length > 0 && new Set(ids).size === ids.length
    && ids.every(id => typeof id === 'string' && id.length > 0 && config.sources?.[id]?.attach === true
      && /^[a-f0-9]{64}$/.test(config.sources[id].sha256 || '')
      && Array.isArray(config.sources[id].binaryArchives)
      && config.sources[id].binaryArchives.includes(binaryHash))
    && ids.some(id => config.sources[id].includesBuildInstructions === true);
}


export function isVendorInstaller(format) {
  return ['windows-inno', 'windows-msi-admin'].includes(format);
}

export function validateVendorAsset(tool, platform, item) {
  const kind = platform === 'windows_x86_64' && (
    tool === 'magick' && item.format === 'windows-inno' && item.asset?.endsWith('.exe') && item.bin === 'magick/magick.exe'
    || tool === 'libreoffice' && item.format === 'windows-msi-admin' && item.asset?.endsWith('.msi') && item.bin === 'libreoffice/program/soffice.com');
  let url;
  try { url = new URL(item.downloadUrl); } catch { /* Rejected below. */ }
  if (!kind || typeof item.version !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(item.version) || !isPlainAssetName(item.asset) || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/.test(item.asset)
    || !url || url.protocol !== 'https:' || !url.hostname || url.username || url.password || url.hash
    || item.sha256 !== item.upstreamSha256 || !/^[a-f0-9]{64}$/.test(item.sha256 || '')) {
    throw new Error(`Invalid direct vendor installer: ${tool}/${platform}`);
  }
}

export function vendorDelivery(tool, platform, entry, upstream) {
  if (entry.package.delivery === undefined) return null;
  if (entry.package.delivery !== 'direct' || entry.package.type !== 'as-is') throw new Error('Unsupported package delivery');
  const asset = upstream.filename || decodeURIComponent(new URL(upstream.url).pathname.split('/').pop());
  const delivery = { asset, version: entry.version, format: entry.package.format, downloadUrl: upstream.url, bin: entry.bin, sha256: upstream.sha256, upstreamSha256: upstream.sha256 };
  validateVendorAsset(tool, platform, delivery);
  return delivery;
}

export async function verifyReusableAsset(target, tool, entry, upstream, expectedNotice) {
  if (entry.package.type === 'as-is') {
    if (await sha256File(target) !== upstream.sha256) throw new Error(`Checksum mismatch for reused ${path.basename(target)}`);
  } else {
    verifyArchiveNotice(target, tool, expectedNotice);
  }
}

// Link only immutable, verified release inputs when all directories share a filesystem.
// The caller still verifies the resulting manifest and every staged source file.
export async function copyReleaseFile(source, destination, link = false) {
  if (!(await fs.lstat(source)).isFile()) throw new Error(`Not a regular file: ${source}`);
  if (link) await fs.link(source, destination);
  else await fs.copyFile(source, destination);
}
