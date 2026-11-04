#!/usr/bin/env node
// Stage or fetch checksum-addressed inputs for a bundle's first release.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { fetchPinned, hasReviewedSourceBundle, isPlainAssetName, PLATFORMS, TOOL_IDS,
  sha256File, sourceAssetName, sourceBundleIds } from './lib.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const checksum = /^[a-f0-9]{64}$/;
export function cacheBase(value) {
  const url = new URL(value);
  const local = url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if ((url.protocol !== 'https:' && !local) || url.username || url.password || url.search || url.hash) {
    throw new Error('Input cache requires an HTTPS base URL without credentials, query or fragment');
  }
  url.pathname = url.pathname.replace(/\/+$/, '') + '/';
  return url.href;
}

export function collectCacheInputs(config, base, { only = null, platforms = null } = {}) {
  base = cacheBase(base);
  if (config.schema !== 1 || !config.repository || !config.bundle) throw new Error('Invalid bundle configuration');
  for (const tool of only || []) if (!TOOL_IDS.includes(tool)) throw new Error(`Unknown tool: ${tool}`);
  for (const platform of platforms || []) if (!PLATFORMS.includes(platform)) throw new Error(`Unknown platform: ${platform}`);
  const release = `https://github.com/${config.repository}/releases/download/${config.bundle}/`;
  const inputs = new Map();
  function add(kind, id, item) {
    if (!item || !checksum.test(item.sha256 || '')) throw new Error(`Invalid pinned input: ${kind}/${id}`);
    const url = new URL(item.url);
    // Other publishers' inputs continue to download from their pinned URLs.
    const generated = url.href.startsWith(release) || url.href === base + item.sha256;
    if (!generated) return;
    if (url.username || url.password || url.search || url.hash) throw new Error(`Invalid generated input URL: ${id}`);
    const filename = item.filename || decodeURIComponent(url.pathname.split('/').pop());
    if (!isPlainAssetName(filename)) throw new Error(`Input filename must be plain: ${id}`);
    const names = [item.sha256, filename];
    if (kind === 'source') names.push(sourceAssetName(id, item));
    inputs.set(`${kind}/${id}`, { kind, id, sha256: item.sha256, filename, names: [...new Set(names)] });
  }
  let selected = 0;
  for (const [tool, value] of Object.entries(config.tools)) {
    if (only && !only.includes(tool)) continue;
    for (const [platform, entry] of Object.entries(value.platforms)) {
      if (platforms && !platforms.includes(platform)) continue;
      selected += 1;
      if (!hasReviewedSourceBundle(entry, config)) throw new Error(`Complete corresponding source has not been reviewed: ${tool}/${platform}`);
      add('upstream', entry.package.upstream, config.upstream[entry.package.upstream]);
      if (entry.sources !== undefined && !Array.isArray(entry.sources)) throw new Error(`Invalid source list: ${tool}/${platform}`);
      for (const id of new Set([...(entry.sources || []), ...sourceBundleIds(entry)])) {
        const source = config.sources[id];
        if (!source) throw new Error(`Unknown source: ${id}`);
        if (source.attach) add('source', id, source);
      }
    }
  }
  if (!selected) throw new Error('No matching tool/platform entries');
  const destinations = new Map();
  for (const input of inputs.values()) {
    const key = `${input.kind}/${input.filename}`;
    if (destinations.has(key) && destinations.get(key) !== input.sha256) throw new Error(`Conflicting input filenames: ${key}`);
    destinations.set(key, input.sha256);
  }
  return [...inputs.values()];
}

async function localInput(input, directories) {
  for (const directory of directories) {
    for (const name of input.names) {
      const filename = path.join(directory, name);
      let stat;
      try { stat = await fs.lstat(filename); } catch (error) {
        if (error.code === 'ENOENT') continue;
        throw error;
      }
      if (!stat.isFile()) throw new Error(`Input must be a regular file: ${name}`);
      if (await sha256File(filename) !== input.sha256) throw new Error(`Checksum mismatch for input: ${name}`);
      return { filename, size: stat.size };
    }
  }
  throw new Error(`Missing local input: ${input.kind}/${input.id} (${input.filename})`);
}

export async function stageInputCache(config, base, directories, output, selection = {}) {
  base = cacheBase(base);
  const inputs = collectCacheInputs(config, base, selection);
  if (!inputs.length) throw new Error('No generated inputs need a cache');
  try { await fs.lstat(output); throw new Error('Input-cache output already exists'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const resolved = new Map();
  for (const input of inputs) {
    if (!resolved.has(input.sha256)) resolved.set(input.sha256, await localInput(input, directories));
  }
  const overlay = structuredClone(config);
  for (const input of inputs) {
    const target = input.kind === 'upstream' ? overlay.upstream[input.id] : overlay.sources[input.id];
    target.url = base + input.sha256;
    target.filename = input.filename;
  }
  await fs.mkdir(path.dirname(path.resolve(output)), { recursive: true });
  await fs.mkdir(output);
  try {
    const objects = path.join(output, 'objects');
    await fs.mkdir(objects);
    const inventory = [];
    for (const [sha256, local] of resolved) {
      await fs.copyFile(local.filename, path.join(objects, sha256));
      if (await sha256File(path.join(objects, sha256)) !== sha256) throw new Error(`Copied input changed: ${sha256}`);
      inventory.push({ sha256, size: local.size, inputs: inputs.filter(item => item.sha256 === sha256)
        .map(({ kind, id, filename }) => ({ kind, id, filename })) });
    }
    await fs.writeFile(path.join(output, 'inputs.json'), JSON.stringify({ schema: 1, objects: inventory }, null, 2) + '\n');
    await fs.writeFile(path.join(output, 'bundle.config.json'), JSON.stringify(overlay, null, 2) + '\n');
    return inventory;
  } catch (error) {
    await fs.rm(output, { recursive: true, force: true });
    throw error;
  }
}

export async function importInputCache(config, base, work, selection = {}, log = console.log) {
  base = cacheBase(base);
  const inputs = collectCacheInputs(config, base, selection);
  for (const input of inputs) {
    const directory = path.join(work, input.kind === 'upstream' ? 'downloads' : 'sources');
    await fetchPinned({ url: base + input.sha256, sha256: input.sha256, filename: input.filename }, directory, log);
  }
  return inputs;
}

async function main(argv) {
  const mode = argv.shift();
  if (!['stage', 'fetch'].includes(mode)) throw new Error('Usage: input-cache.mjs stage|fetch --url URL [--input-dir DIR --out DIR] [--work DIR] [--config FILE] [--only TOOL] [--platform PLATFORM]');
  const options = { directories: [], work: '.bundle-work', config: path.join(here, '../bundle.config.json') };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i], value = argv[++i];
    if (!value || value.startsWith('--')) throw new Error(`Missing value for ${flag}`);
    if (flag === '--url') options.url = value;
    else if (flag === '--input-dir') options.directories.push(path.resolve(value));
    else if (flag === '--out') options.out = path.resolve(value);
    else if (flag === '--work') options.work = path.resolve(value);
    else if (flag === '--config') options.config = path.resolve(value);
    else if (flag === '--only') options.only = value.split(',');
    else if (flag === '--platform') options.platforms = value.split(',');
    else throw new Error(`Unknown option: ${flag}`);
  }
  if (!options.url) throw new Error('--url is required');
  const config = JSON.parse(await fs.readFile(options.config, 'utf8'));
  if (mode === 'stage') {
    if (!options.out || !options.directories.length) throw new Error('stage requires --out and --input-dir');
    const objects = await stageInputCache(config, options.url, options.directories, options.out, options);
    console.log(`Staged ${objects.length} verified input objects and a configuration for review.`);
  } else {
    const inputs = await importInputCache(config, options.url, options.work, options);
    console.log(`Prepared ${inputs.length} pinned inputs.`);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main(process.argv.slice(2)).catch(error => { console.error(error.message); process.exitCode = 1; });
}
