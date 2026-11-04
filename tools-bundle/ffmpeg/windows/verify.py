#!/usr/bin/env python3
"""Verify a standalone FFmpeg/ffprobe payload with software conversion fixtures."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def run(command, *, environment, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{Path(command[0]).name} failed ({result.returncode}): {result.stderr[-5000:]}')
    return result.stdout


def verify(ffmpeg, ffprobe, report, expected_version='8.1.1'):
    ffmpeg, ffprobe = Path(ffmpeg).resolve(strict=True), Path(ffprobe).resolve(strict=True)
    if ffmpeg.parent != ffprobe.parent:
        raise RuntimeError('FFmpeg and ffprobe must come from the same candidate directory')
    environment = dict(os.environ)
    if os.name == 'nt':
        system = Path(environment['SystemRoot'])
        environment['PATH'] = os.pathsep.join([str(system / 'System32'), str(system)])
    version = run([str(ffmpeg), '-version'], environment=environment)
    if not version.startswith(f'ffmpeg version {expected_version}'):
        raise RuntimeError('Unexpected FFmpeg version')
    if '--enable-nonfree' in version:
        raise RuntimeError('Nonfree build is not a distributable candidate')
    flags = ['--enable-gpl', '--enable-version3', '--enable-libx264', '--enable-libx265',
             '--enable-libvpx', '--enable-libaom', '--enable-libwebp', '--enable-libmp3lame',
             '--enable-libopus', '--enable-libvorbis']
    for flag in flags:
        if flag not in version:
            raise RuntimeError(f'Missing required configuration: {flag}')
    results = []
    output = {'version': version.splitlines()[0], 'configuration': version,
              'standaloneWindowsPath': os.name == 'nt', 'cases': results, 'complete': False}
    report_path = Path(report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    def save_report():
        report_path.write_text(json.dumps(output, indent=2) + '\n')
    save_report()
    with tempfile.TemporaryDirectory(prefix='htc-ffmpeg-smoke-') as directory:
        work = Path(directory)
        video = [
            ('h264', 'mp4', 'libx264', 'yuv420p', []),
            ('hevc8', 'mp4', 'libx265', 'yuv420p', ['-preset', 'ultrafast']),
            ('hevc10', 'mp4', 'libx265', 'yuv420p10le', ['-preset', 'ultrafast']),
            ('hevc12', 'mp4', 'libx265', 'yuv420p12le', ['-preset', 'ultrafast']),
            ('vp8', 'webm', 'libvpx', 'yuv420p', ['-deadline', 'realtime']),
            ('vp9', 'webm', 'libvpx-vp9', 'yuv420p', ['-deadline', 'realtime']),
            ('av1', 'mkv', 'libaom-av1', 'yuv420p', ['-cpu-used', '8']),
            ('gif', 'gif', 'gif', None, []),
            ('webp', 'webp', 'libwebp', 'yuv420p', []),
        ]
        audio = [
            ('mp3', 'mp3', 'libmp3lame', 44100), ('aac', 'm4a', 'aac', 44100),
            ('alac', 'm4a', 'alac', 44100), ('vorbis', 'ogg', 'libvorbis', 44100),
            ('opus', 'opus', 'libopus', 48000), ('flac', 'flac', 'flac', 44100),
            ('wav', 'wav', 'pcm_s16le', 44100), ('aiff', 'aiff', 'pcm_s16be', 44100),
            ('amr', 'amr', 'libopencore_amrnb', 8000),
        ]
        for kind, cases in [('video', video), ('audio', audio)]:
            for case in cases:
                start = time.monotonic()
                name, suffix, codec = case[:3]
                target = work / f'{name}.{suffix}'
                command = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi']
                if kind == 'video':
                    pixfmt, extra = case[3:]
                    command += ['-i', 'testsrc2=size=96x64:rate=6:duration=0.5', '-frames:v', '3',
                                '-an', '-c:v', codec, '-threads', '2']
                    if pixfmt:
                        command += ['-pix_fmt', pixfmt]
                    command += extra
                    if suffix == 'webp':
                        command[command.index('-frames:v') + 1] = '1'
                else:
                    command += ['-i', f'sine=frequency=440:sample_rate={case[3]}:duration=0.5',
                                '-ac', '1', '-c:a', codec]
                    if name == 'amr':
                        command += ['-b:a', '12.2k']
                command += [str(target)]
                run(command, environment=environment)
                probe = json.loads(run([str(ffprobe), '-v', 'error', '-show_streams', '-show_format',
                                        '-of', 'json', str(target)], environment=environment))
                streams = probe.get('streams', [])
                expected = [stream for stream in streams if stream.get('codec_type') == kind]
                if not expected or target.stat().st_size == 0:
                    raise RuntimeError(f'{name}: output has no expected stream')
                if kind == 'video':
                    if expected[0].get('width') != 96 or expected[0].get('height') != 64:
                        raise RuntimeError(f'{name}: wrong frame dimensions')
                    if name in ['hevc10', 'hevc12'] and expected[0].get('pix_fmt') != case[3]:
                        raise RuntimeError(f'{name}: high bit depth was not preserved')
                run([str(ffmpeg), '-v', 'error', '-xerror', '-i', str(target), '-f', 'null', '-'],
                    environment=environment)
                results.append({'case': name, 'status': 'PASS', 'bytes': target.stat().st_size,
                                'durationSeconds': round(time.monotonic() - start, 3),
                                'streams': expected, 'validation': 'probe + full decode'})
                save_report()
                print(f'PASS {name}', flush=True)
    output['complete'] = True
    save_report()
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg', required=True)
    parser.add_argument('--ffprobe', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--expected-version', default='8.1.1')
    args = parser.parse_args()
    verify(args.ffmpeg, args.ffprobe, args.report, args.expected_version)


if __name__ == '__main__':
    main()
