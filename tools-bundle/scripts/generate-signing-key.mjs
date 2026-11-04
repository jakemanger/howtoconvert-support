#!/usr/bin/env node
// Creates the Ed25519 key pair that signs manifest.json.
//
//   node tools-bundle/scripts/generate-signing-key.mjs <private-key-file>
//
// Store the private key as the repository secret TOOLS_MANIFEST_SIGNING_KEY
// (Settings → Secrets and variables → Actions) and never commit it. Store the
// public key as the repository variable TOOLS_MANIFEST_PUBLIC_KEY.
import { generateKeyPairSync } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

export function generateSigningKey(filename) {
  const destination = path.resolve(filename);
  if (fs.existsSync(destination) || fs.existsSync(`${destination}.pub`)) {
    throw new Error('Signing key already exists. Reuse and back it up; do not overwrite it.');
  }
  const { publicKey, privateKey } = generateKeyPairSync('ed25519');
  const rawPublicKey = Buffer.from(publicKey.export({ format: 'jwk' }).x, 'base64url');
  fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
  fs.writeFileSync(destination, privateKey.export({ format: 'pem', type: 'pkcs8' }), { mode: 0o600, flag: 'wx' });
  fs.writeFileSync(`${destination}.pub`, `${rawPublicKey.toString('base64')}\n`, { mode: 0o644, flag: 'wx' });
  return destination;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  if (!process.argv[2]) {
    console.error('Specify a private key path outside the repository');
    process.exitCode = 1;
  } else {
    try {
      const destination = generateSigningKey(process.argv[2]);
      console.log(`Private key saved with owner-only permissions: ${destination}`);
      console.log(`Public key: ${destination}.pub`);
      console.log('Back up the private key securely. Its contents are deliberately not printed.');
    } catch (error) { console.error(error.message); process.exitCode = 1; }
  }
}
