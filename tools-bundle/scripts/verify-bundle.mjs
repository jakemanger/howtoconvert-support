#!/usr/bin/env node
// Checks the manifest signature, shape, and the SHA-256 and size of every
// declared binary and corresponding-source archive.
//
//   node tools-bundle/scripts/verify-bundle.mjs dist <base64 public key>
import { createPublicKey, verify } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { PLATFORMS, TOOL_IDS, isPlainAssetName, sha256File, sourceAttachments, sourceBundleIds, isVendorInstaller, validateVendorAsset } from './lib.mjs';

const [directory = 'dist', publicKeyBase64 = process.env.TOOLS_MANIFEST_PUBLIC_KEY, ...options] = process.argv.slice(2);
let vendorDirectory;
if (options.length) {
  if (options.length !== 2 || options[0] !== '--vendor-dir' || !options[1]) throw new Error('Use --vendor-dir <original installer cache>');
  vendorDirectory = path.resolve(options[1]);
}
const problems = [];
const manifestBytes = await fs.readFile(path.join(directory, 'manifest.json'));
const manifest = JSON.parse(manifestBytes);

if (publicKeyBase64) {
  const raw = Buffer.from(publicKeyBase64, 'base64');
  if (raw.length !== 32) throw new Error('The Ed25519 public key must contain exactly 32 bytes');
  const spkiPrefix = Buffer.from('302a300506032b6570032100', 'hex');
  const publicKey = createPublicKey({ key: Buffer.concat([spkiPrefix, raw]), format: 'der', type: 'spki' });
  const signature = Buffer.from((await fs.readFile(path.join(directory, 'manifest.json.sig'), 'utf8')).trim(), 'base64');
  if (signature.length !== 64) throw new Error('The Ed25519 signature must contain exactly 64 bytes');
  if (!verify(null, manifestBytes, publicKey, signature)) problems.push('manifest.json.sig does not match the public key');
} else {
  problems.push('TOOLS_MANIFEST_PUBLIC_KEY is required; signature verification cannot be skipped');
}

if (manifest.schema !== 1) problems.push(`unsupported schema ${manifest.schema}`);
if (!/^tools-\d{4}\.\d{2}\.\d+$/.test(manifest.bundle)) problems.push(`bundle id ${manifest.bundle} is not tools-YYYY.MM.N`);
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
async function checkFile(item, label, baseDirectory = directory) {
  if (!isRecord(item) || !isPlainAssetName(item.asset) || !/^[a-f0-9]{64}$/.test(item.sha256 || '')
    || !Number.isSafeInteger(item.size) || item.size <= 0) {
    problems.push(`invalid ${label}`);
    return;
  }
  const file = path.join(baseDirectory, item.asset);
  const stat = await fs.lstat(file).catch(error => {
    if (error.code !== 'ENOENT') throw error;
    return null;
  });
  if (!stat) problems.push(`missing ${label}: ${item.asset}`);
  else if (!stat.isFile()) problems.push(`not a regular file: ${item.asset}`);
  else if (stat.size !== item.size || await sha256File(file) !== item.sha256) problems.push(`${item.asset}: ${label} checksum mismatch`);
}
if (!isRecord(manifest.tools) || !Object.keys(manifest.tools).length) problems.push('tools must be a nonempty object');
for (const [tool, entry] of Object.entries(isRecord(manifest.tools) ? manifest.tools : {})) {
  if (!TOOL_IDS.includes(tool)) problems.push(`unknown tool ${tool}`);
  if (!isRecord(entry?.platforms) || !Object.keys(entry.platforms).length) {
    problems.push(`${tool}: platforms must be a nonempty object`);
    continue;
  }
  for (const [platform, item] of Object.entries(entry.platforms)) {
    const label = `${tool}/${platform}`;
    if (!PLATFORMS.includes(platform)) problems.push(`${tool}: unknown platform ${platform}`);
    if (!isRecord(item)) { problems.push(`${label}: invalid platform entry`); continue; }
    if (item.minimumOS !== undefined && (!platform.startsWith('mac_') || typeof item.minimumOS !== 'string'
      || !/^[1-9]\d*\.\d+(?:\.\d+)?$/.test(item.minimumOS))) problems.push(`${label}: invalid minimum macOS version`);
    if (!/^[a-f0-9]{64}$/.test(item.upstreamSha256 || '')) problems.push(`${label}: missing upstream checksum`);
    if (typeof item.version !== 'string' || !item.version) problems.push(`${label}: missing version`);
    if (!['tar.gz', 'zip', 'dmg'].includes(item.format) && !isVendorInstaller(item.format)) problems.push(`${label}: unsupported archive format`);
    if (typeof item.bin !== 'string' || !item.bin || item.bin.includes('..') || /[\\:]/.test(item.bin) || path.isAbsolute(item.bin)) {
      problems.push(`${label}: unsafe bin path`);
    }
    const ids = sourceBundleIds(item);
    const parts = sourceAttachments(item);
    if (!ids.length || ids.some(id => typeof id !== 'string' || !id) || new Set(ids).size !== ids.length) {
      problems.push(`${label}: invalid corresponding-source set`);
    }
    if (!parts.length || parts.length !== ids.length || new Set(parts.map(part => part?.asset)).size !== parts.length) {
      problems.push(`${label}: incomplete or duplicate corresponding-source attachments`);
    }
    for (const source of parts) await checkFile(source, 'corresponding source');
    if (isVendorInstaller(item.format)) {
      try { validateVendorAsset(tool, platform, item); } catch (error) { problems.push(error.message); }
      if (!vendorDirectory) problems.push(`${label}: --vendor-dir is required to verify original installer bytes`);
      else await checkFile(item, 'vendor installer', vendorDirectory);
    } else {
      if (item.downloadUrl !== undefined) problems.push(`${label}: direct download requires a vendor installer format`);
      await checkFile(item, 'archive');
    }
  }
}

if (problems.length) {
  console.error(`Bundle verification failed:\n- ${problems.join('\n- ')}`);
  process.exit(1);
}
console.log(`Verified ${manifest.bundle}: ${Object.keys(manifest.tools).length} tools.`);
