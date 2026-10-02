#!/usr/bin/env node
// Checks a built bundle exactly as the app will: the manifest signature, the
// manifest shape, and the SHA-256 and size of every archive that is present.
//
//   node tools-bundle/scripts/verify-bundle.mjs dist <base64 public key>
import { createPublicKey, verify } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { PLATFORMS, TOOL_IDS, exists, sha256File } from './lib.mjs';

const [directory = 'dist', publicKeyBase64 = process.env.TOOLS_MANIFEST_PUBLIC_KEY] = process.argv.slice(2);
const problems = [];
const manifestBytes = await fs.readFile(path.join(directory, 'manifest.json'));
const manifest = JSON.parse(manifestBytes);

if (publicKeyBase64) {
  const raw = Buffer.from(publicKeyBase64, 'base64');
  const spkiPrefix = Buffer.from('302a300506032b6570032100', 'hex');
  const publicKey = createPublicKey({ key: Buffer.concat([spkiPrefix, raw]), format: 'der', type: 'spki' });
  const signature = Buffer.from((await fs.readFile(path.join(directory, 'manifest.json.sig'), 'utf8')).trim(), 'base64');
  if (!verify(null, manifestBytes, publicKey, signature)) problems.push('manifest.json.sig does not match the public key');
} else {
  console.log('No public key given; skipping signature check.');
}

if (manifest.schema !== 1) problems.push(`unsupported schema ${manifest.schema}`);
if (!/^tools-\d{4}\.\d{2}\.\d+$/.test(manifest.bundle)) problems.push(`bundle id ${manifest.bundle} is not tools-YYYY.MM.N`);
for (const [tool, entry] of Object.entries(manifest.tools || {})) {
  if (!TOOL_IDS.includes(tool)) problems.push(`unknown tool ${tool}`);
  for (const [platform, item] of Object.entries(entry.platforms || {})) {
    if (!PLATFORMS.includes(platform)) problems.push(`${tool}: unknown platform ${platform}`);
    for (const field of ['version', 'asset', 'sha256', 'size', 'format', 'bin']) {
      if (item[field] === undefined || item[field] === '') problems.push(`${tool}/${platform}: missing ${field}`);
    }
    if (item.bin.includes('..') || path.isAbsolute(item.bin)) problems.push(`${tool}/${platform}: unsafe bin path ${item.bin}`);
    const file = path.join(directory, item.asset);
    if (!await exists(file)) continue;
    const stat = await fs.stat(file);
    if (stat.size !== item.size) problems.push(`${item.asset}: size ${stat.size} != ${item.size}`);
    if (await sha256File(file) !== item.sha256) problems.push(`${item.asset}: sha256 mismatch`);
  }
}

if (problems.length) {
  console.error(`Bundle verification failed:\n- ${problems.join('\n- ')}`);
  process.exit(1);
}
console.log(`Verified ${manifest.bundle}: ${Object.keys(manifest.tools).length} tools.`);
