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
const work = path.resolve(option('--work', '.ffmpeg-work'));
const output = path.resolve(option('--out', 'dist/ffmpeg'));
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
run(xcpkg, ['formula-repo-conf', 'official-core', '--pin']);
const currentCommit = run('git', ['-C', repository, 'rev-parse', 'HEAD'], true);
if (currentCommit !== lock.formulaCommit) {
  run('git', ['-C', repository, 'fetch', '--depth=1', 'origin', lock.formulaCommit]);
  run('git', ['-C', repository, 'checkout', '--detach', lock.formulaCommit]);
}
for (const name of ['ffmpeg', 'libx264', 'libvpx']) {
  await fs.copyFile(path.join(here, `${name}.yml`), path.join(repository, `formula/${name}.yml`));
}
// Share the CPU dispatch fix with the image tool build.
for (const name of ['libaom', 'librav1e', 'libfreetype', 'libx265']) {
  await fs.copyFile(path.join(here, `../imagemagick/${name}.yml`), path.join(repository, `formula/${name}.yml`));
}
// Apply the build-system fixes to xcpkg's installed shell scripts. Existing
// patched installations are accepted only when the reverse check succeeds.
for (const patch of ['xcpkg-relocation.patch', 'xcpkg-macos-cross.patch']) {
  const patchArgs = ['-p1', '-d', xcpkgHome, '-i', path.join(here, '../imagemagick', patch)];
  const check = spawnSync('patch', ['--dry-run', '--batch', '--forward', ...patchArgs], { encoding: 'utf8' });
  if (check.status === 0) run('patch', ['--batch', '--forward', ...patchArgs]);
  else {
    const applied = spawnSync('patch', ['--dry-run', '--batch', '--reverse', ...patchArgs], { encoding: 'utf8' });
    if (applied.status !== 0) throw new Error(`Cannot apply ${patch} to this xcpkg installation`);
  }
}
const spec = `MacOSX-${lock.minimumMacOS}-${arch}/ffmpeg`;
if (args.includes('--prepare-only')) {
  console.log(`Prepared ${spec}`);
  process.exit(0);
}
const rustc = path.join(process.env.CARGO_HOME || path.join(process.env.HOME, '.cargo'), 'bin/rustc');
const rustVersion = run(rustc, ['--version', '--verbose'], true);
if (!rustVersion.startsWith(`rustc ${lock.rustVersion} `) || !rustVersion.includes(`commit-hash: ${lock.rustCommit}`)) {
  throw new Error(`Install Rust ${lock.rustVersion} with rustup before building`);
}
if (args.includes('--dependencies-only')) {
  const formula = JSON.parse(run(xcpkg, ['formula-parse', path.join(here, 'ffmpeg.yml'), '--json'], true));
  const prefix = spec.slice(0, spec.lastIndexOf('/') + 1);
  const dependencies = formula['dep-pkg'].split(/\s+/).filter(Boolean).map(name => prefix + name);
  run(xcpkg, ['install', ...dependencies, '--profile=release', '-K']);
  console.log('Prepared FFmpeg dependencies');
  process.exit(0);
}
run(xcpkg, ['install', spec, '--profile=release', '-K']);
// Relocation changes Mach-O load commands. Refresh only local ad-hoc signatures;
// no Apple account, signing certificate or notarization service is used.
const installed = path.join(xcpkgHome, 'installed', `MacOSX-${lock.minimumMacOS}-${arch}`, 'ffmpeg');
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
await signMachO(installed);
const ffmpeg = path.join(installed, 'bin/ffmpeg');
const version = run(ffmpeg, ['-hide_banner', '-version'], true);
if (!version.startsWith(`ffmpeg version ${lock.version}`)) throw new Error('Unexpected FFmpeg version');
run(path.join(installed, 'bin/ffprobe'), ['-version'], true);
const encoders = run(ffmpeg, ['-hide_banner', '-encoders'], true);
for (const codec of ['libx264', 'libx264rgb', 'libx265', 'libvpx', 'libvpx-vp9', 'libaom-av1', 'libsvtav1', 'libopenh264', 'libopenjpeg', 'libtheora', 'libmp3lame', 'libopus', 'libvorbis', 'libwebp', 'libwebp_anim']) {
  if (!encoders.split('\n').some(line => line.trim().split(/\s+/)[1] === codec)) throw new Error(`Missing encoder: ${codec}`);
}
const filters = run(ffmpeg, ['-hide_banner', '-filters'], true);
if (!filters.split('\n').some(line => line.trim().split(/\s+/)[1] === 'subtitles')) throw new Error('Missing subtitle renderer');
run(xcpkg, ['bundle', spec, '.tar.xz', '-o', path.join(output, `ffmpeg-${lock.version}-MacOSX-${lock.minimumMacOS}-${arch}.tar.xz`)]);
