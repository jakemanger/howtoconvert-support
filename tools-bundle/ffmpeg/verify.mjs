#!/usr/bin/env node
// Encode, inspect and decode the codecs selected by the desktop conversion UI.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';

const [directory] = process.argv.slice(2);
if (!directory) throw new Error('Usage: verify.mjs FFMPEG_BIN_DIRECTORY');
const extension = process.platform === 'win32' ? '.exe' : '';
const ffmpeg = path.resolve(directory, `ffmpeg${extension}`);
const ffprobe = path.resolve(directory, `ffprobe${extension}`);
const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'conversion-codecs-'));
function run(command, args) {
  const result = spawnSync(command, args, { encoding: 'utf8', maxBuffer: 2 * 1024 * 1024, timeout: 120000 });
  if (result.error || result.status !== 0) throw new Error(result.error?.message || result.stderr || `Exit ${result.status}`);
  return result.stdout;
}
const video = [
  ['libx264', 'h264', 'mp4', []], ['libx264rgb', 'h264', 'mkv', ['-pix_fmt', 'rgb24']],
  ['libx265', 'hevc', 'mp4', ['-x265-params', 'pools=1:frame-threads=1']],
  ['libvpx', 'vp8', 'webm', ['-deadline', 'realtime']],
  ['libvpx-vp9', 'vp9', 'webm', ['-deadline', 'realtime']],
  ['libaom-av1', 'av1', 'mkv', ['-cpu-used', '8']],
  ['libsvtav1', 'av1', 'mkv', ['-preset', '12']],
  ['libopenh264', 'h264', 'mp4', []], ['libopenjpeg', 'jpeg2000', 'mkv', []],
  ['libtheora', 'theora', 'ogv', []], ['libwebp', 'webp', 'webp', ['-frames:v', '1']],
  ['libwebp_anim', 'webp', 'webp', ['-frames:v', '1']], ['gif', 'gif', 'gif', []],
];
const audio = [['libmp3lame', 'mp3', 'mp3'], ['libopus', 'opus', 'opus'], ['libvorbis', 'vorbis', 'ogg'], ['aac', 'aac', 'm4a'], ['flac', 'flac', 'flac']];
async function inspect(file, expected, pixelFormat) {
  const probe = JSON.parse(run(ffprobe, ['-v', 'error', '-show_streams', '-of', 'json', file]));
  if (!probe.streams.some(stream => stream.codec_name === expected)) throw new Error(`${file}: expected ${expected}`);
  if (pixelFormat && !probe.streams.some(stream => stream.codec_name === expected && stream.pix_fmt === pixelFormat)) {
    throw new Error(`${file}: expected ${pixelFormat} without a bit-depth downgrade`);
  }
  run(ffmpeg, ['-v', 'error', '-i', file, '-f', 'null', '-']);
  if ((await fs.stat(file)).size === 0) throw new Error(`Empty output: ${file}`);
}
try {
  for (const [encoder, codec, format, extra] of video) {
    const file = path.join(temporary, `${encoder}.${format}`);
    run(ffmpeg, ['-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=96x96:rate=10', '-t', '0.3', '-c:v', encoder, '-threads', '1', ...extra, '-y', file]);
    await inspect(file, codec);
    console.log(`PASS ${encoder}: encode, identify, decode`);
  }
  for (const pixelFormat of ['yuv420p10le', 'yuv420p12le']) {
    const file = path.join(temporary, `libx265-${pixelFormat}.mkv`);
    run(ffmpeg, ['-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=96x96:rate=10', '-t', '0.3',
      '-c:v', 'libx265', '-pix_fmt', pixelFormat, '-x265-params', 'pools=1:frame-threads=1', '-y', file]);
    await inspect(file, 'hevc', pixelFormat);
    console.log(`PASS libx265 ${pixelFormat}: encode, preserve bit depth, decode`);
  }
  for (const [encoder, codec, format] of audio) {
    const file = path.join(temporary, `${encoder}.${format}`);
    run(ffmpeg, ['-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '0.3', '-c:a', encoder, '-y', file]);
    await inspect(file, codec);
    console.log(`PASS ${encoder}: encode, identify, decode`);
  }
  const subtitles = path.join(temporary, 'captions.srt');
  await fs.writeFile(subtitles, '1\n00:00:00,000 --> 00:00:00,300\nConversion tools\n');
  const captioned = path.join(temporary, 'captioned.mp4');
  const escaped = subtitles.replaceAll('\\', '/').replaceAll(':', '\\:').replaceAll("'", "\\'");
  run(ffmpeg, ['-v', 'error', '-f', 'lavfi', '-i', 'color=blue:size=320x180:rate=10', '-t', '0.3', '-vf', `subtitles='${escaped}'`, '-c:v', 'libx264', '-y', captioned]);
  await inspect(captioned, 'h264');
  console.log('PASS subtitle rendering');
} finally {
  await fs.rm(temporary, { recursive: true, force: true });
}
