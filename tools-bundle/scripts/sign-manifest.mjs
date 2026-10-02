#!/usr/bin/env node
// Signs manifest.json with Ed25519 and writes manifest.json.sig (base64 of the
// raw 64-byte signature over the file's exact bytes).
//
//   TOOLS_MANIFEST_SIGNING_KEY="$(cat key.pem)" node tools-bundle/scripts/sign-manifest.mjs dist
import { createPrivateKey, sign } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const directory = process.argv[2] || 'dist';
const key = process.env.TOOLS_MANIFEST_SIGNING_KEY;
if (!key) {
  console.error('TOOLS_MANIFEST_SIGNING_KEY is not set. Generate one with generate-signing-key.mjs.');
  process.exit(1);
}
const privateKey = createPrivateKey(key.includes('BEGIN') ? key : { key: Buffer.from(key, 'base64'), format: 'der', type: 'pkcs8' });
const manifest = await fs.readFile(path.join(directory, 'manifest.json'));
const signature = sign(null, manifest, privateKey);
await fs.writeFile(path.join(directory, 'manifest.json.sig'), `${signature.toString('base64')}\n`);
console.log(`Signed ${path.join(directory, 'manifest.json')}`);
