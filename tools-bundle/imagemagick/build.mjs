#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const lock = JSON.parse(await fs.readFile(path.join(here, 'build.lock.json'), 'utf8'));
const args = process.argv.slice(2);
function option(name, fallback) {
  const index = args.indexOf(name);
  if (index < 0) return fallback;
  if (!args[index + 1] || args[index + 1].startsWith('--')) throw new Error(`Missing ${name} value`);
  return args[index + 1];
}
const arch = option('--arch', process.arch === 'arm64' ? 'arm64' : 'x86_64');
if (process.platform !== 'darwin' || !['arm64', 'x86_64'].includes(arch)) throw new Error('Requires macOS; --arch arm64 or x86_64');
const work = path.resolve(option('--work', '.imagemagick-work'));
const output = path.resolve(option('--out', 'dist/imagemagick'));
const xcpkgHome = path.join(work, 'xcpkg');
const env = { ...process.env, XCPKG_HOME: xcpkgHome, UPPM_HOME: path.join(work, 'uppm'), RUSTUP_TOOLCHAIN: lock.rustVersion };
function run(command, argv, capture = false) {
  const result = spawnSync(command, argv, { env, encoding: 'utf8', stdio: capture ? 'pipe' : 'inherit', maxBuffer: 32 * 1024 * 1024 });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${command} failed (${result.status}): ${result.stderr || ''}`);
  return result.stdout?.trim();
}
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
async function download(url, destination, expected) {
  try { if (sha(await fs.readFile(destination)) === expected) return; } catch {}
  const temporary = `${destination}.part`;
  await fs.mkdir(path.dirname(destination), { recursive: true });
  run('curl', ['--fail', '--location', '--retry', '3', url, '-o', temporary]);
  if (sha(await fs.readFile(temporary)) !== expected) throw new Error(`Checksum mismatch: ${url}`);
  await fs.rename(temporary, destination);
}
await fs.mkdir(work, { recursive: true });
await fs.mkdir(output, { recursive: true });
let xcpkg = option('--xcpkg', null);
if (xcpkg) xcpkg = path.resolve(xcpkg);
else {
  if (process.arch !== 'arm64') throw new Error('Pass --xcpkg with xcpkg 0.30.0 on an Intel build host');
  const archive = path.join(work, 'bootstrap.tar.xz');
  await download(lock.bootstrap.url, archive, lock.bootstrap.sha256);
  const bootstrap = path.join(work, 'bootstrap');
  await fs.mkdir(bootstrap, { recursive: true });
  run('tar', ['-xf', archive, '-C', bootstrap, '--strip-components=1']);
  xcpkg = path.join(bootstrap, 'bin/xcpkg');
}
if (run(xcpkg, ['--version'], true) !== '0.30.0') throw new Error('Requires xcpkg 0.30.0');
const repository = path.join(xcpkgHome, 'repos.d', 'official-core');
try { await fs.access(path.join(repository, '.git')); }
catch { run(xcpkg, ['formula-repo-add', 'official-core', lock.formulaRepository, '--pin']); }
run('git', ['-C', repository, 'fetch', '--depth=1', 'origin', lock.formulaCommit]);
run('git', ['-C', repository, 'checkout', '--detach', lock.formulaCommit]);
for (const name of ['imagemagick', 'libheif', 'libaom', 'librav1e', 'libfreetype', 'libx265']) {
  await fs.copyFile(path.join(here, `${name}.yml`), path.join(repository, `formula/${name}.yml`));
}
// Apply the build-system fixes to xcpkg's installed shell scripts. Existing
// patched installations are accepted only when the reverse check succeeds.
for (const patch of ['xcpkg-relocation.patch', 'xcpkg-macos-cross.patch']) {
  const patchArgs = ['-p1', '-d', xcpkgHome, '-i', path.join(here, patch)];
  const check = spawnSync('patch', ['--dry-run', '--batch', '--forward', ...patchArgs], { encoding: 'utf8' });
  if (check.status === 0) run('patch', ['--batch', '--forward', ...patchArgs]);
  else {
    const applied = spawnSync('patch', ['--dry-run', '--batch', '--reverse', ...patchArgs], { encoding: 'utf8' });
    if (applied.status !== 0) throw new Error(`Cannot apply ${patch} to this xcpkg installation`);
  }
}
const spec = `MacOSX-${lock.minimumMacOS}-${arch}/imagemagick`;
if (args.includes('--prepare-only')) {
  console.log(`Prepared ${spec}`);
  process.exit(0);
}
const rustc = path.join(process.env.CARGO_HOME || path.join(process.env.HOME, '.cargo'), 'bin/rustc');
if (!run(rustc, ['--version'], true).startsWith(`rustc ${lock.rustVersion} `)) {
  throw new Error(`Install Rust ${lock.rustVersion} with rustup before building`);
}
run(xcpkg, ['install', spec, '--profile=release', '-K']);
// Relocation changes Mach-O load commands. Refresh only local ad-hoc signatures;
// no Apple account, signing certificate or notarization service is used.
const installed = path.join(xcpkgHome, 'installed', `MacOSX-${lock.minimumMacOS}-${arch}`, 'imagemagick');
async function signMachO(directory) {
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) await signMachO(filename);
    else if (entry.isFile()) {
      const handle = await fs.open(filename, 'r');
      const magic = Buffer.alloc(4);
      try { await handle.read(magic, 0, 4, 0); } finally { await handle.close(); }
      if (['cffaedfe', 'cefaedfe', 'feedface', 'feedfacf', 'cafebabe', 'bebafeca'].includes(magic.toString('hex'))) {
        run('codesign', ['--force', '--sign', '-', '--timestamp=none', filename]);
      }
    }
  }
}
// A cached ImageMagick can outlive a rebuilt codec. Refresh its private copy
// so incremental packaging uses the same encoder as a clean build.
const codecDirectory = path.join(xcpkgHome, 'installed', `MacOSX-${lock.minimumMacOS}-${arch}`, 'libx265/lib');
for (const entry of await fs.readdir(codecDirectory, { withFileTypes: true })) {
  if (entry.isFile() && /^libx265\.[0-9]+\.dylib$/.test(entry.name)) {
    const destination = path.join(installed, '.xcpkg/dependencies/lib', entry.name);
    await fs.access(destination);
    await fs.copyFile(path.join(codecDirectory, entry.name), destination);
  }
}
const configuration = path.join(installed, 'etc/ImageMagick-7');
const delegatesFile = path.join(configuration, 'delegates.xml');
const delegatesXml = await fs.readFile(delegatesFile, 'utf8');
await fs.writeFile(delegatesFile, delegatesXml.replace(/<delegate\b[\s\S]*?\/>/g,
  element => /\b(?:gs|gswin32c|gswin64c)\b/.test(element) ? '' : element));
const typesFile = path.join(configuration, 'type.xml');
await fs.writeFile(typesFile, (await fs.readFile(typesFile, 'utf8'))
  .replace(/<include\b[^>]*file="type-ghostscript\.xml"[^>]*\/>/g, ''));
await fs.rm(path.join(configuration, 'type-ghostscript.xml'), { force: true });
const policyFile = path.join(configuration, 'policy.xml');
const policy = await fs.readFile(policyFile, 'utf8');
const pdfPolicy = '<policy domain="coder" rights="write" pattern="{PS,PS2,PS3,EPS,PDF,XPS}" />';
if (!policy.includes(pdfPolicy)) {
  if (!policy.includes('</policymap>')) throw new Error('Invalid ImageMagick policy');
  await fs.writeFile(policyFile, policy.replace('</policymap>', `  ${pdfPolicy}\n</policymap>`));
}
// Libtool decoder metadata must not prefer the original build directory.
for (const version of await fs.readdir(path.join(installed, 'lib'))) {
  if (!version.startsWith('ImageMagick-')) continue;
  const versionPath = path.join(installed, 'lib', version);
  for (const modules of await fs.readdir(versionPath)) {
    if (!modules.startsWith('modules-')) continue;
    for (const category of ['coders', 'filters']) {
      const directory = path.join(versionPath, modules, category);
      for (const name of await fs.readdir(directory)) {
        if (!name.endsWith('.la')) continue;
        const filename = path.join(directory, name);
        const contents = await fs.readFile(filename, 'utf8');
        await fs.writeFile(filename, contents.replace(/^(libdir|dependency_libs)='.*'$/gm, "$1=''") );
      }
    }
  }
}
await signMachO(installed);
const version = run(path.join(installed, 'bin/magick'), ['-version'], true);
const delegates = version.split('\n').find(line => line.startsWith('Delegates')) || '';
if (/\bgslib\b/i.test(delegates) || !/\braw\b/i.test(delegates)) {
  throw new Error('ImageMagick must include LibRaw and exclude Ghostscript');
}
const highDepthSample = path.join(work, `heic-16bit-${arch}.heic`);
try {
  run(path.join(installed, 'bin/magick'), ['-size', '32x32', 'gradient:red-blue', '-depth', '16', highDepthSample]);
  run(path.join(installed, 'bin/magick'), [highDepthSample, 'null:']);
} finally { await fs.rm(highDepthSample, { force: true }); }
run(xcpkg, ['bundle', spec, '.tar.xz', '-o', path.join(output, `imagemagick-${lock.version}-MacOSX-${lock.minimumMacOS}-${arch}.tar.xz`)]);
