#!/usr/bin/env node
// Creates the Ed25519 key pair that signs manifest.json.
//
//   node tools-bundle/scripts/generate-signing-key.mjs
//
// Store the private key as the repository secret TOOLS_MANIFEST_SIGNING_KEY
// (Settings → Secrets and variables → Actions) and never commit it. Store the
// public key as the repository variable TOOLS_MANIFEST_PUBLIC_KEY.
import { generateKeyPairSync } from 'node:crypto';

const { publicKey, privateKey } = generateKeyPairSync('ed25519');
const rawPublicKey = publicKey.export({ format: 'der', type: 'spki' }).subarray(-32);
console.log('TOOLS_MANIFEST_SIGNING_KEY (secret, keep private):\n');
console.log(privateKey.export({ format: 'pem', type: 'pkcs8' }).toString());
console.log('TOOLS_MANIFEST_PUBLIC_KEY (public):\n');
console.log(rawPublicKey.toString('base64'));
