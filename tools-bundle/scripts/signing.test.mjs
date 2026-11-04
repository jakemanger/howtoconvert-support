import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { generateSigningKey } from './generate-signing-key.mjs';

const scripts = fileURLToPath(new URL('./', import.meta.url));
function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'htc-tools-signing-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const key = path.join(dir, 'key.pem');
  const result = spawnSync(process.execPath, [path.join(scripts, 'generate-signing-key.mjs'), key], { encoding: 'utf8' });
  assert.equal(result.status, 0);
  const privateKey = fs.readFileSync(key, 'utf8');
  const publicKey = fs.readFileSync(`${key}.pub`, 'utf8').trim();
  const bytes = Buffer.from('a harmless signing test fixture');
  const asset = 'ffmpeg-test.tar.gz';
  fs.writeFileSync(path.join(dir, asset), bytes);
  const source = 'ffmpeg-source-test.tar.gz';
  fs.writeFileSync(path.join(dir, source), bytes);
  const manifest = { schema: 1, bundle: 'tools-2026.10.0', tools: { ffmpeg: { platforms: { mac_arm64: {
    version: '1.0', asset, sha256: createHash('sha256').update(bytes).digest('hex'), size: bytes.length, format: 'tar.gz', bin: 'ffmpeg/ffmpeg',
    correspondingSource:{asset:source,sha256:createHash('sha256').update(bytes).digest('hex'),size:bytes.length},
  } } } } };
  fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify(manifest));
  const run = (script, environment = {}) => spawnSync(process.execPath, [path.join(scripts, script), dir], {
    encoding: 'utf8', env: { ...process.env, TOOLS_MANIFEST_SIGNING_KEY: '', TOOLS_MANIFEST_PUBLIC_KEY: '', ...environment },
  });
  assert.equal(run('sign-manifest.mjs', { TOOLS_MANIFEST_SIGNING_KEY: privateKey }).status, 0);
  return { dir, key, privateKey, publicKey, asset, result, verify: (key = publicKey) => run('verify-bundle.mjs', { TOOLS_MANIFEST_PUBLIC_KEY: key }) };
}

test('key generation keeps private bytes out of output and refuses overwrite', t => {
  const f = fixture(t);
  assert.ok(!f.result.stdout.includes('BEGIN PRIVATE KEY'));
  assert.ok(!f.result.stdout.includes(f.privateKey));
  if (process.platform !== 'win32') assert.equal(fs.statSync(f.key).mode & 0o777, 0o600);
  assert.throws(() => generateSigningKey(f.key), /already exists/);
  assert.equal(fs.readFileSync(f.key, 'utf8'), f.privateKey);
});
test('valid signature and archive pass', t => assert.equal(fixture(t).verify().status, 0));
test('verification fails closed without a public key', t => assert.notEqual(fixture(t).verify('').status, 0));
test('another signing key is rejected', t => {
  const f = fixture(t), other = path.join(f.dir, 'other.pem');
  generateSigningKey(other);
  assert.notEqual(f.verify(fs.readFileSync(`${other}.pub`, 'utf8').trim()).status, 0);
});
test('altered manifest is rejected', t => {
  const f = fixture(t);
  fs.appendFileSync(path.join(f.dir, 'manifest.json'), '\n');
  assert.notEqual(f.verify().status, 0);
});
test('altered and missing archive are both rejected', t => {
  const f = fixture(t);
  fs.writeFileSync(path.join(f.dir, f.asset), 'replaced');
  assert.notEqual(f.verify().status, 0);
  fs.unlinkSync(path.join(f.dir, f.asset));
  assert.notEqual(f.verify().status, 0);
});

test('missing or altered corresponding source blocks distribution',t=>{
 const f=fixture(t);const source=path.join(f.dir,'ffmpeg-source-test.tar.gz');
 fs.writeFileSync(source,'changed source');assert.notEqual(f.verify().status,0);
 fs.unlinkSync(source);assert.notEqual(f.verify().status,0);
});
