#!/usr/bin/env node
// Builds the conversion tools bundle from pinned upstream releases: one
// archive per tool and platform, plus SOURCES.md, the tools' source archives
// and an (unsigned) manifest.json.
//
//   node tools-bundle/scripts/build-bundle.mjs --out dist [--only ffmpeg,pandoc]
//        [--platform mac_arm64] [--reuse] [--skip-sources] [--work .bundle-work]
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
  fetchPinned, findContaining, run, sha256File, sourceAssetName,
} from './lib.mjs';

const bundleDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function parseArguments(argv) {
  const options = { out: 'dist', work: '.bundle-work', only: null, platforms: null, reuse: false, skipSources: false };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const next = () => argv[++i];
    if (arg === '--out') options.out = next();
    else if (arg === '--work') options.work = next();
    else if (arg === '--only') options.only = next().split(',');
    else if (arg === '--platform') options.platforms = next().split(',');
    else if (arg === '--reuse') options.reuse = true;
    else if (arg === '--skip-sources') options.skipSources = true;
    else throw new Error(`Unknown argument ${arg}`);
  }
  for (const tool of options.only || []) if (!TOOL_IDS.includes(tool)) throw new Error(`Unknown tool ${tool}`);
  for (const platform of options.platforms || []) if (!PLATFORMS.includes(platform)) throw new Error(`Unknown platform ${platform}`);
  return options;
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
      await fs.chmod(downloaded, 0o755);
      // --appimage-extract runs the AppImage runtime, which needs no FUSE.
      run(downloaded, ['--appimage-extract'], { cwd: scratch, stdio: 'ignore' });
      await copyTree(path.join(scratch, 'squashfs-root'), destination);
      const wrapper = path.join(destination, pkg.wrapper);
      await fs.mkdir(path.dirname(wrapper), { recursive: true });
      await fs.writeFile(wrapper, '#!/bin/sh\n# Runs the pre-extracted AppImage without FUSE.\nHERE="$(dirname "$(readlink -f "$0")")"\nexec "$HERE/../AppRun" "$@"\n');
      await fs.chmod(wrapper, 0o755);
      break;
    }
    case '7z-extract': {
      run('7z', ['x', '-y', `-o${scratch}`, downloaded], { stdio: 'ignore' });
      let root = scratch;
      if (pkg.dir) root = path.join(scratch, pkg.dir);
      else {
        const entries = (await fs.readdir(scratch, { withFileTypes: true })).filter(e => !e.name.startsWith('.'));
        if (entries.length === 1 && entries[0].isDirectory()) root = path.join(scratch, entries[0].name);
      }
      await copyTree(root, destination);
      break;
    }
    case 'tar-xz-dir': {
      run('tar', ['-xJf', downloaded, '-C', scratch]);
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
      for (const deb of debs.sort()) run('dpkg-deb', ['-x', deb, root]);
      const programDirectory = path.join(root, pkg.programDir);
      if (!await exists(path.join(programDirectory, 'program', 'soffice'))) {
        throw new Error(`${tool}/${platform}: program/soffice missing from ${pkg.programDir}`);
      }
      await copyTree(programDirectory, destination);
      break;
    }
    case 'msi-admin': {
      if (process.platform !== 'win32') throw new Error(`${tool}/${platform}: msiexec /a must run on Windows (use the workflow's Windows job)`);
      const target = path.join(scratch, 'admin');
      run('msiexec', ['/a', downloaded, '/qn', `TARGETDIR=${target}`]);
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
  const sources = (entry.sources || []).map(id => config.sources[id]).filter(Boolean);
  return [
    `${toolConfig.name} ${entry.version} for ${platform}`,
    '',
    `Origin: ${entry.origin}`,
    `License: ${toolConfig.license} (texts in HTC-LICENSES/)`,
    `Project: ${toolConfig.homepage}`,
    '',
    'Source code:',
    ...sources.map(s => `  - ${s.title}: ${s.url}${s.attach ? ` (also attached to the ${config.bundle} release as ${sourceAssetName(Object.keys(config.sources).find(k => config.sources[k] === s), s)})` : ''}${s.notes ? `\n    ${s.notes}` : ''}`),
    `  - Full record: https://github.com/${config.repository}/releases/download/${config.bundle}/SOURCES.md`,
    '',
    `Written offer: for at least three years from the date of the ${config.bundle} release,`,
    `Merlinsbeard Pty Ltd will provide the complete corresponding source code for this`,
    `binary, as required by its license, to anyone who asks at ${config.writtenOfferContact}.`,
    '',
  ].join('\n');
}

async function writeSourcesRecord({ config, out, manifestTools, attached }) {
  const lines = [
    `# ${config.bundle} sources and licenses`,
    '',
    'Command line conversion tools for macOS, Windows and Linux, from one place.',
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
    '## Written offer',
    '',
    `For at least three years from the publication of ${config.bundle}, Merlinsbeard Pty Ltd will give`,
    'anyone a copy of the complete corresponding source code for any GPL- or LGPL-licensed binary in this',
    `bundle, including the statically linked libraries, on request to ${config.writtenOfferContact}, for no`,
    'more than the cost of physically performing the distribution.',
    '',
    '## License texts',
    '',
    'Each archive contains the relevant texts in `HTC-LICENSES/`. The texts are also in',
    `https://github.com/${config.repository}/tree/main/tools-bundle/LICENSES.`,
    '',
  );
  await fs.writeFile(path.join(out, 'SOURCES.md'), lines.join('\n'));
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  const config = JSON.parse(await fs.readFile(path.join(bundleDirectory, 'bundle.config.json'), 'utf8'));
  if (config.schema !== 1) throw new Error(`Unsupported bundle config schema ${config.schema}`);
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
        await fs.rm(target, { force: true });
        createTarGz(staging, tool, target);
        await fs.rm(staging, { recursive: true, force: true });
      }
      builtAssets.set(key, name);
      const stat = await fs.stat(target);
      platforms[platform] = {
        version: entry.version,
        asset: name,
        sha256: await sha256File(target),
        size: stat.size,
        format,
        bin: entry.bin,
        origin: entry.origin,
        sources: entry.sources || [],
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
      const downloaded = await fetchPinned(source, path.join(work, 'sources'));
      const name = sourceAssetName(id, source);
      await fs.copyFile(downloaded, path.join(out, name));
      attached.set(id, name);
    }
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
  if (!commandExists('7z')) console.log('Note: 7z was not found; 7z-extract packages need p7zip-full.');
}

main().catch(error => {
  console.error(error.message || error);
  process.exit(1);
});
