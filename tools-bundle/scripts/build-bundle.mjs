#!/usr/bin/env node
// Builds the conversion tools bundle from pinned upstream releases: one
// archive per tool and platform, plus SOURCES.md, the tools' source archives
// and an (unsigned) manifest.json.
//
//   node tools-bundle/scripts/build-bundle.mjs --out dist [--only ffmpeg,pandoc]
//        [--platform mac_arm64] [--source-dir SOURCES] [--reuse] [--skip-sources] [--check] [--work .bundle-work]
//
// --reuse keeps assets already in --out (the Windows job builds the
// LibreOffice archive with msiexec and hands it to the Linux job).
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import {
  PLATFORMS, TOOL_IDS, assetName, commandExists, copyTree, createTarGz, exists,
  fetchPinned, findContaining, run, sha256File, sourceAssetName, verifyArchiveNotice,
  sourceBundleIds, sourceAttachments, hasReviewedSourceBundle, resolveSourceArchive,
} from './lib.mjs';

const bundleDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function parseArguments(argv) {
  const options = { out: 'dist', work: '.bundle-work', only: null, platforms: null, sourceDirectories: [], reuse: false, skipSources: false, check: false };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const next = () => argv[++i];
    if (arg === '--out') options.out = next();
    else if (arg === '--work') options.work = next();
    else if (arg === '--only') options.only = next().split(',');
    else if (arg === '--platform') options.platforms = next().split(',');
    else if (arg === '--source-dir') options.sourceDirectories.push(path.resolve(next()));
    else if (arg === '--reuse') options.reuse = true;
    else if (arg === '--skip-sources') options.skipSources = true;
    else if (arg === '--check') options.check = true;
    else throw new Error(`Unknown argument ${arg}`);
  }
  for (const tool of options.only || []) if (!TOOL_IDS.includes(tool)) throw new Error(`Unknown tool ${tool}`);
  for (const platform of options.platforms || []) if (!PLATFORMS.includes(platform)) throw new Error(`Unknown platform ${platform}`);
  if (options.check && options.skipSources) throw new Error('--check requires source verification; do not combine it with --skip-sources');
  return options;
}

function sevenZip() {
  for (const command of ['7z', '7zz']) if (commandExists(command)) return command;
  throw new Error('Install 7-Zip (7z or 7zz) to unpack this archive');
}

const unzipped = new Map();
async function unzipUpstream(id, archive, work) {
  if (unzipped.has(id)) return unzipped.get(id);
  const target = path.join(work, 'unzip', id);
  await fs.rm(target, { recursive: true, force: true });
  await fs.mkdir(target, { recursive: true });
  run('unzip', ['-q', archive, '-d', target]);
  unzipped.set(id, target);
  return target;
}

async function stageContents({ tool, platform, entry, config, work }) {
  const pkg = entry.package;
  const upstream = config.upstream[pkg.upstream];
  if (!upstream) throw new Error(`${tool}/${platform}: unknown upstream ${pkg.upstream}`);
  const downloaded = await fetchPinned(upstream, path.join(work, 'downloads'));
  const staging = path.join(work, 'staging', `${tool}-${platform}`);
  await fs.rm(staging, { recursive: true, force: true });
  const destination = path.join(staging, tool);
  const scratch = await fs.mkdtemp(path.join(work, `${tool}-${platform}-`));

  switch (pkg.type) {
    case 'zip-subdir': {
      const root = path.join(await unzipUpstream(pkg.upstream, downloaded, work), upstream.root);
      for (const sub of pkg.paths) {
        // `paths` name tool folders inside the platform root; their contents
        // become the archive's tool folder.
        if (!await exists(path.join(root, sub))) throw new Error(`${tool}/${platform}: ${sub} missing from ${pkg.upstream}`);
        await copyTree(path.join(root, sub), destination);
      }
      for (const [from, to] of Object.entries(pkg.extra || {})) {
        await copyTree(path.join(root, from), path.join(staging, to));
      }
      break;
    }
    case 'appimage-extract': {
      let extracted;
      if (process.platform === 'linux' && process.arch === 'x64') {
        await fs.chmod(downloaded, 0o755);
        // Only execute the runtime on a compatible build host.
        run(downloaded, ['--appimage-extract'], { cwd: scratch, stdio: 'ignore' });
        extracted = path.join(scratch, 'squashfs-root');
      } else {
        // 7-Zip reads the embedded SquashFS without running a foreign binary.
        run(sevenZip(), ['x', '-y', `-o${scratch}`, downloaded], { stdio: 'ignore' });
        extracted = await findContaining(scratch, 'AppRun');
        if (!extracted) throw new Error(`${tool}/${platform}: AppRun missing from extracted AppImage`);
      }
      await copyTree(extracted, destination);
      const wrapper = path.join(destination, pkg.wrapper);
      await fs.mkdir(path.dirname(wrapper), { recursive: true });
      await fs.writeFile(wrapper, '#!/bin/sh\n# Runs the pre-extracted AppImage without FUSE.\nHERE="$(dirname "$(readlink -f "$0")")"\nexec "$HERE/../AppRun" "$@"\n');
      await fs.chmod(wrapper, 0o755);
      break;
    }
    case '7z-extract': {
      run(sevenZip(), ['x', '-y', `-o${scratch}`, downloaded], { stdio: 'ignore' });
      let root = scratch;
      if (pkg.dir) root = path.join(scratch, pkg.dir);
      else {
        const entries = (await fs.readdir(scratch, { withFileTypes: true })).filter(e => !e.name.startsWith('.'));
        if (entries.length === 1 && entries[0].isDirectory()) root = path.join(scratch, entries[0].name);
      }
      await copyTree(root, destination);
      break;
    }
    case 'tar-gz-dir':
    case 'tar-xz-dir': {
      run('tar', [pkg.type === 'tar-gz-dir' ? '-xzf' : '-xJf', downloaded, '-C', scratch]);
      if (!await exists(path.join(scratch, pkg.dir))) throw new Error(`${tool}/${platform}: ${pkg.dir}/ missing from ${upstream.url}`);
      await copyTree(path.join(scratch, pkg.dir), destination);
      break;
    }
    case 'deb-extract': {
      run('tar', ['-xzf', downloaded, '-C', scratch]);
      const debs = [];
      const queue = [scratch];
      while (queue.length) {
        const directory = queue.shift();
        for (const item of await fs.readdir(directory, { withFileTypes: true })) {
          const full = path.join(directory, item.name);
          if (item.isDirectory()) queue.push(full);
          else if (item.name.endsWith('.deb')) debs.push(full);
        }
      }
      if (!debs.length) throw new Error(`${tool}/${platform}: no .deb files in ${upstream.url}`);
      const root = path.join(scratch, 'root');
      await fs.mkdir(root, { recursive: true });
      for (const [index, deb] of debs.sort().entries()) {
        if (commandExists('dpkg-deb')) run('dpkg-deb', ['-x', deb, root]);
        else {
          // BSD tar on macOS supports both the outer ar and inner data archive.
          const ar = path.join(scratch, `deb-${index}`);
          await fs.mkdir(ar);
          run('tar', ['-xf', deb, '-C', ar]);
          const data = (await fs.readdir(ar)).filter(name => /^data\.tar(?:\.|$)/.test(name));
          if (data.length !== 1) throw new Error(`${tool}/${platform}: expected one data archive in ${deb}`);
          run('tar', ['-xf', path.join(ar, data[0]), '-C', root]);
          await fs.rm(ar, { recursive: true });
        }
      }
      const programDirectory = path.join(root, pkg.programDir);
      if (!await exists(path.join(programDirectory, 'program', 'soffice'))) {
        throw new Error(`${tool}/${platform}: program/soffice missing from ${pkg.programDir}`);
      }
      await copyTree(programDirectory, destination);
      break;
    }
    case 'msi-admin': {
      const target = path.join(scratch, 'admin');
      if (process.platform === 'win32') run('msiexec', ['/a', downloaded, '/qn', `TARGETDIR=${target}`]);
      else {
        if (!commandExists('msiextract')) throw new Error(`${tool}/${platform}: install msitools or use the Windows build job`);
        await fs.mkdir(target, { recursive: true });
        run('msiextract', ['--directory', target, downloaded], { stdio: 'ignore' });
      }
      const programRoot = await findContaining(target, path.join('program', 'soffice.com'));
      if (!programRoot) throw new Error(`${tool}/${platform}: program\\soffice.com not found after administrative install`);
      await copyTree(programRoot, destination);
      break;
    }
    default:
      throw new Error(`${tool}/${platform}: unknown package type ${pkg.type}`);
  }
  await fs.rm(scratch, { recursive: true, force: true });
  if (!await exists(path.join(staging, entry.bin))) throw new Error(`${tool}/${platform}: ${entry.bin} missing after packaging`);
  return staging;
}

function noticeText({ config, tool, toolConfig, platform, entry }) {
  return [
    `${toolConfig.name} ${entry.version} for ${platform}`,
    `Origin: ${entry.origin}`,
    `Input SHA-256: ${config.upstream[entry.package.upstream].sha256}`,
    ...(entry.minimumOS ? [`Minimum macOS: ${entry.minimumOS}`] : []),
    `License: ${toolConfig.license} (texts in HTC-LICENSES/)`,
    `Project: ${toolConfig.homepage}`,
    '',
    entry.package.type === 'appimage-extract'
      ? 'Packaging: extracted AppImage with a launcher for use without FUSE.'
      : 'Packaging: upstream binaries unchanged.',
    '',
    ...(hasReviewedSourceBundle(entry, config) ? [
      'Matching source and build instructions (free download):',
      `https://github.com/${config.repository}/releases/download/${config.bundle}/SOURCES.md`,
    ] : [
      'Local draft: matching source and build instructions are required before distribution.',
    ]),
    '',
  ].join('\n');
}

async function writeSourcesRecord({ config, out, manifestTools, attached }) {
  const lines = [
    `# ${config.bundle} sources and licenses`,
    '',
    'Source downloads and notices for the packages in this release.',
    '',
    '## Programs',
    '',
    '| Tool | Platform | Version | License | Origin | Archive | SHA-256 |',
    '|---|---|---|---|---|---|---|',
  ];
  for (const [tool, toolEntry] of Object.entries(manifestTools)) {
    for (const [platform, entry] of Object.entries(toolEntry.platforms)) {
      lines.push(`| ${toolEntry.name} | ${platform} | ${entry.version} | ${toolEntry.license} | ${entry.origin} | ${entry.asset} | \`${entry.sha256}\` |`);
    }
  }
  lines.push('', '## Source code', '');
  const used = new Set(Object.values(manifestTools).flatMap(t => Object.values(t.platforms).flatMap(p => p.sources || [])));
  for (const id of [...used].sort()) {
    const source = config.sources[id];
    const attachment = attached.get(id);
    lines.push(`- **${source.title}**: ${source.url}${attachment ? ` · attached as \`${attachment}\` (SHA-256 \`${source.sha256}\`)` : ''}`);
    if (source.notes) lines.push(`  - ${source.notes}`);
  }
  lines.push(
    '',
    '## Corresponding source',
    '',
    ...(Object.values(manifestTools).every(value => Object.values(value.platforms).every(item => item.correspondingSource)) ? [
      'Download these matching source bundles, dependencies and build instructions',
      'from this release without charge.',
    ] : [
      'Local draft: matching source bundles are missing. Do not distribute.',
    ]),
    '',
    ...Object.entries(manifestTools).flatMap(([tool, value]) => Object.entries(value.platforms).map(([platform, item]) =>
      `- ${tool}/${platform}: ${sourceAttachments(item).map(source => source.asset).join(', ') || 'Missing'}`)),
    '',
    '## License texts',
    '',
    'Repacked archives include `HTC-LICENSES/` and retain upstream notices.',
    'Unmodified LibreOffice disk images retain their own license files. See also:',
    `https://github.com/${config.repository}/tree/tools-bundle/tools-bundle/LICENSES.`,
    '',
  );
  await fs.writeFile(path.join(out, 'SOURCES.md'), lines.join('\n'));
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  const config = JSON.parse(await fs.readFile(path.join(bundleDirectory, 'bundle.config.json'), 'utf8'));
  if (config.schema !== 1) throw new Error(`Unsupported bundle config schema ${config.schema}`);
  if (!options.skipSources) {
    const missing = [];
    for (const [tool, value] of Object.entries(config.tools)) {
      if (options.only && !options.only.includes(tool)) continue;
      for (const [platform, entry] of Object.entries(value.platforms)) {
        if (options.platforms && !options.platforms.includes(platform)) continue;
        if (!hasReviewedSourceBundle(entry, config)) missing.push(`${tool}/${platform}`);
      }
    }
    if (missing.length) throw new Error(`Complete corresponding source has not been reviewed for: ${missing.join(', ')}. A main-project source tarball or an upstream homepage is not sufficient.`);
  }
  if (options.check) {
    console.log('Source mappings are configured. Build and staging will verify the downloads.');
    return;
  }
  const out = path.resolve(options.out);
  const work = path.resolve(options.work);
  await fs.mkdir(out, { recursive: true });
  await fs.mkdir(work, { recursive: true });

  const manifestTools = {};
  const builtAssets = new Map();
  for (const [tool, toolConfig] of Object.entries(config.tools)) {
    if (options.only && !options.only.includes(tool)) continue;
    const platforms = {};
    for (const [platform, entry] of Object.entries(toolConfig.platforms)) {
      if (options.platforms && !options.platforms.includes(platform)) continue;
      const format = entry.package.type === 'as-is' ? entry.package.format : 'tar.gz';
      const name = assetName(tool, entry.version, platform, format);
      const target = path.join(out, name);
      // Identical inputs (e.g. the universal macOS TinyTeX) share one asset.
      const key = `${tool}:${entry.package.upstream}:${JSON.stringify(entry.package)}:${entry.version}`;
      const sharedName = builtAssets.get(key);
      if (sharedName) {
        platforms[platform] = { ...platforms[Object.keys(platforms).find(p => platforms[p].asset === sharedName)], asset: sharedName };
        console.log(`${tool}/${platform}: shares ${sharedName}`);
        continue;
      }
      if (options.reuse && await exists(target)) {
        if (format === 'tar.gz') {
          verifyArchiveNotice(target, tool, noticeText({ config, tool, toolConfig, platform, entry }));
        } else if (await sha256File(target) !== config.upstream[entry.package.upstream].sha256) {
          throw new Error(`Checksum mismatch for reused ${name}`);
        }
        console.log(`${tool}/${platform}: reusing ${name}`);
      } else if (entry.package.type === 'as-is') {
        const downloaded = await fetchPinned(config.upstream[entry.package.upstream], path.join(work, 'downloads'));
        await fs.copyFile(downloaded, target);
      } else {
        const staging = await stageContents({ tool, platform, entry, config, work });
        const licenseDirectory = path.join(staging, tool, 'HTC-LICENSES');
        await fs.mkdir(licenseDirectory, { recursive: true });
        for (const file of toolConfig.licenseFiles) {
          await fs.copyFile(path.join(bundleDirectory, 'LICENSES', file), path.join(licenseDirectory, file));
        }
        await fs.writeFile(path.join(staging, tool, 'HTC-NOTICE.txt'), noticeText({ config, tool, toolConfig, platform, entry }));
        createTarGz(staging, tool, target);
        await fs.rm(staging, { recursive: true, force: true });
      }
      builtAssets.set(key, name);
      const stat = await fs.stat(target);
      platforms[platform] = {
        version: entry.version,
        upstreamSha256: config.upstream[entry.package.upstream].sha256,
        asset: name,
        sha256: await sha256File(target),
        size: stat.size,
        format,
        bin: entry.bin,
        origin: entry.origin,
        sources: [...new Set([...(entry.sources || []), ...sourceBundleIds(entry)])],
        sourceBundle: entry.sourceBundle,
        ...(entry.minimumOS ? { minimumOS: entry.minimumOS } : {}),
        ...(entry.installTo ? { installTo: entry.installTo } : {}),
        ...(entry.package.app ? { app: entry.package.app } : {}),
      };
      console.log(`${tool}/${platform}: ${name} (${(stat.size / 1048576).toFixed(1)} MB)`);
    }
    if (Object.keys(platforms).length) {
      manifestTools[tool] = {
        name: toolConfig.name,
        license: toolConfig.license,
        homepage: toolConfig.homepage,
        verifyArgs: toolConfig.verifyArgs,
        notes: toolConfig.notes || '',
        severity: toolConfig.severity || 'none',
        platforms,
      };
    }
  }

  const attached = new Map();
  if (!options.skipSources) {
    const used = new Set(Object.values(manifestTools).flatMap(t => Object.values(t.platforms).flatMap(p => p.sources)));
    for (const id of used) {
      const source = config.sources[id];
      if (!source) throw new Error(`Unknown source ${id}`);
      if (!source.attach) continue;
      const downloaded = await resolveSourceArchive(id, source, path.join(work, 'sources'), options.sourceDirectories);
      const name = sourceAssetName(id, source);
      await fs.copyFile(downloaded, path.join(out, name));
      attached.set(id, name);
    }
  }
  for (const value of Object.values(manifestTools)) for (const item of Object.values(value.platforms)) {
    const parts = [];
    for (const id of sourceBundleIds(item)) {
      const name = attached.get(id);
      if (name) parts.push({ asset: name, sha256: config.sources[id].sha256, size: (await fs.stat(path.join(out, name))).size });
    }
    if (parts.length) item.correspondingSource = Array.isArray(item.sourceBundle) ? parts : parts[0];
  }
  await writeSourcesRecord({ config, out, manifestTools, attached });

  const manifest = {
    schema: 1,
    bundle: config.bundle,
    createdAt: new Date().toISOString(),
    repository: config.repository,
    releaseUrl: `https://github.com/${config.repository}/releases/tag/${config.bundle}`,
    sourcesUrl: `https://github.com/${config.repository}/releases/download/${config.bundle}/SOURCES.md`,
    tools: manifestTools,
  };
  await fs.writeFile(path.join(out, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
  console.log(`Wrote ${path.join(out, 'manifest.json')} for ${Object.keys(manifestTools).length} tools on ${os.platform()}`);
  if (!commandExists('7z') && !commandExists('7zz')) console.log('Note: 7-Zip was not found; 7z-extract packages need 7z or 7zz.');
}

main().catch(error => {
  console.error(error.message || error);
  process.exit(1);
});
