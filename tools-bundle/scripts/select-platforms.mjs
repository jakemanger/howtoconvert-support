#!/usr/bin/env node
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { PLATFORMS } from './lib.mjs';

export function selectPlatforms(value) {
  if (typeof value !== 'string' || !value.trim()) throw new Error('Select at least one platform');
  const selected = value.trim() === 'all' ? [...PLATFORMS] : value.split(',').map(item => item.trim());
  for (const platform of selected) {
    if (!PLATFORMS.includes(platform)) throw new Error(`Unknown platform: ${platform}; choose ${PLATFORMS.join(', ')} or all`);
  }
  if (new Set(selected).size !== selected.length) throw new Error('Duplicate platform selection');
  return { platforms: selected.join(','), windows: selected.includes('windows_x86_64') };
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    const selection = selectPlatforms(process.env.INPUT_PLATFORMS);
    if (!process.env.GITHUB_OUTPUT) throw new Error('GITHUB_OUTPUT is required');
    await fs.appendFile(process.env.GITHUB_OUTPUT, `platforms=${selection.platforms}\nwindows=${selection.windows}\n`);
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
