#!/usr/bin/env node
// Collect the external archives pinned by a LibreOffice core source release.
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { fetchPinned, isPlainAssetName, run, sha256File } from './lib.mjs';

export function parseDownloadList(text) {
  const variables = new Map([...text.matchAll(/^([A-Z0-9_]+)\s*:?=\s*(.*?)\s*$/gm)].map(match => [match[1], match[2]]));
  function expand(value, seen = new Set()) {
    return value.replace(/\$\(([A-Z0-9_]+)\)/g, (_, name) => {
      if (seen.has(name) || !variables.has(name)) throw new Error(`Unresolved source variable: ${name}`);
      return expand(variables.get(name), new Set([...seen, name]));
    });
  }
  const entries = [];
  for (const [name, value] of variables) {
    if (!name.endsWith('_TARBALL')) continue;
    const id = name.slice(0, -8), filename = expand(value), sha256 = variables.get(`${id}_SHA256SUM`);
    if (!isPlainAssetName(filename) || /[$\s]/.test(filename) || !/^[a-f0-9]{64}$/.test(sha256 || '')) {
      throw new Error(`Unpinned or unsafe source archive: ${id}`);
    }
    entries.push({ id, filename, sha256, url: `https://dev-www.libreoffice.org/src/${encodeURIComponent(filename)}` });
  }
  if (!entries.length) throw new Error('The source release contains no pinned external archives');
  return entries;
}

export async function collectLibreOfficeSources(archive, source, cache, index) {
  if (await sha256File(archive) !== source.sha256) throw new Error('Core source checksum mismatch');
  const filename = source.filename || path.basename(new URL(source.url).pathname);
  if (!/^libreoffice-\d[\d.]*\.tar\.xz$/.test(filename)) throw new Error('Expected a LibreOffice core source archive');
  const member = `${filename.slice(0, -7)}/download.lst`;
  const text = run('tar', ['-xOf', archive, member], { encoding: 'utf8', stdio: 'pipe' }).stdout;
  const entries = parseDownloadList(text);
  let next = 0, finished = 0;
  const errors = [];
  await fs.mkdir(cache, { recursive: true });
  await Promise.all(Array.from({ length: 4 }, async () => {
    while (next < entries.length) {
      const entry = entries[next++];
      try {
        const file = await fetchPinned(entry, cache);
        entry.size = (await fs.stat(file)).size;
        finished++;
        if (finished % 10 === 0 || finished === entries.length) console.log(`Verified ${finished}/${entries.length} external archives.`);
      } catch (error) { errors.push(`${entry.id}: ${error.message}`); }
    }
  }));
  if (errors.length) throw new Error(`Source collection incomplete:\n${errors.join('\n')}`);
  const record = { schema: 1, core: { filename, url: source.url, sha256: source.sha256 }, archives: entries };
  await fs.writeFile(index, JSON.stringify(record, null, 2) + '\n');
  return record;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    const [archive, sourceId, cache, index] = process.argv.slice(2);
    if (!archive || !sourceId || !cache || !index) throw new Error('Usage: collect-libreoffice-sources.mjs CORE_ARCHIVE SOURCE_ID CACHE_DIRECTORY OUTPUT_JSON');
    const config = JSON.parse(await fs.readFile(new URL('../bundle.config.json', import.meta.url), 'utf8'));
    const source = config.sources[sourceId];
    if (!source) throw new Error(`Unknown source: ${sourceId}`);
    await collectLibreOfficeSources(archive, source, cache, index);
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
