#!/usr/bin/env node
// Verify a bundle and copy only its declared release files into a new directory.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { run, sha256File, sourceAssetName, sourceAttachments, sourceBundleIds, hasReviewedSourceBundle, isPlainAssetName, vendorDelivery, copyReleaseFile } from './lib.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));

export async function stageRelease(directory, destination, config, publicKey, vendorDirectory, { link = false } = {}) {
  const input = path.resolve(directory);
  const output = path.resolve(destination);
  run(process.execPath, [path.join(here, 'verify-bundle.mjs'), input, publicKey || '', ...(vendorDirectory ? ['--vendor-dir', path.resolve(vendorDirectory)] : [])], { stdio: 'pipe' });
  const manifest = JSON.parse(await fs.readFile(path.join(input, 'manifest.json'), 'utf8'));
  if (manifest.bundle !== config.bundle) throw new Error('Manifest and configuration bundle IDs differ');
  const files = new Set(['manifest.json', 'manifest.json.sig', 'SOURCES.md']);
  const sources = new Set();
  for (const [toolId, tool] of Object.entries(manifest.tools)) {
    for (const [platform, item] of Object.entries(tool.platforms)) {
      const configured = config.tools?.[toolId]?.platforms?.[platform];
      if (!configured || !hasReviewedSourceBundle(configured, config)) {
        throw new Error(`Complete corresponding source has not been reviewed: ${toolId}/${platform}`);
      }
      if (item.upstreamSha256 !== config.upstream[configured.package.upstream].sha256) {
        throw new Error(`Binary input differs from reviewed source mapping: ${toolId}/${platform}`);
      }
      if (item.minimumOS !== configured.minimumOS) {
        throw new Error(`Minimum OS differs from configuration: ${toolId}/${platform}`);
      }
      const requiredIds = sourceBundleIds(configured);
      const actualIds = sourceBundleIds(item);
      if (requiredIds.length !== actualIds.length || requiredIds.some(id => !actualIds.includes(id))) {
        throw new Error(`Corresponding-source set differs from configuration: ${toolId}/${platform}`);
      }
      const parts = sourceAttachments(item);
      for (const id of requiredIds) {
        const expected = config.sources[id];
        const name = sourceAssetName(id, expected);
        const part = parts.find(source => source.asset === name);
        if (!part || part.sha256 !== expected.sha256) {
          throw new Error(`Corresponding-source attachment differs from configuration: ${id}`);
        }
        files.add(name);
      }
      const expectedSources = [...new Set([...(configured.sources || []), ...requiredIds])];
      if (!Array.isArray(item.sources) || item.sources.length !== expectedSources.length
        || new Set(item.sources).size !== item.sources.length || expectedSources.some(id => !item.sources.includes(id))) {
        throw new Error(`Source list differs from configuration: ${toolId}/${platform}`);
      }
      const direct = vendorDelivery(toolId, platform, configured, config.upstream[configured.package.upstream]);
      if (direct) {
        if (item.downloadUrl !== direct.downloadUrl || item.asset !== direct.asset || item.format !== direct.format
          || item.bin !== direct.bin || item.sha256 !== direct.sha256 || item.version !== direct.version) throw new Error(`Vendor delivery differs from configuration: ${toolId}/${platform}`);
      } else {
        if (item.downloadUrl !== undefined) throw new Error(`Unexpected direct vendor URL: ${toolId}/${platform}`);
        files.add(item.asset);
      }
      for (const id of expectedSources) sources.add(id);
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
    if (!isPlainAssetName(name)) {
      throw new Error('Release assets must be plain filenames');
    }
    if (!(await fs.lstat(path.join(input, name))).isFile()) throw new Error(`Not a regular file: ${name}`);
  }
  // Refuse an existing destination so stale files can never join the upload.
  await fs.mkdir(output);
  try {
    for (const name of names) await copyReleaseFile(path.join(input, name), path.join(output, name), link);
  } catch (error) {
    await fs.rm(output, { recursive: true, force: true });
    throw error;
  }
  return names;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    const config = JSON.parse(await fs.readFile(path.join(here, '../bundle.config.json'), 'utf8'));
    const options = process.argv.slice(4);
    let vendorDirectory, link = false;
    for (let i = 0; i < options.length; i++) {
      if (options[i] === '--link') link = true;
      else if (options[i] === '--vendor-dir' && options[i + 1] && !options[i + 1].startsWith('--')) vendorDirectory = options[++i];
      else throw new Error('Use --vendor-dir <original installer cache> and/or --link');
    }
    const names = await stageRelease(process.argv[2] || 'dist', process.argv[3] || 'release', config, process.env.TOOLS_MANIFEST_PUBLIC_KEY, vendorDirectory, { link });
    console.log(`Staged ${names.length} release files.`);
  } catch (error) {
    console.error(error.stderr?.toString().trim() || error.message);
    process.exitCode = 1;
  }
}
