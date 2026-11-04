#!/usr/bin/env python3
"""Exercise document/ebook round trips and optional PDF output with a rebuilt Pandoc."""
import argparse
import json
import os
from pathlib import Path
import struct
import subprocess
import zlib


def run(args, directory, env):
    result = subprocess.run([str(arg) for arg in args], cwd=directory, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout


def fixture_image(path):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    pixels = b''.join(b'\0' + bytes([120, 80, 200]) * 32 for _ in range(24))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 32, 24, 8, 2, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pandoc', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pdf-engine', type=Path)
    args = parser.parse_args()
    pandoc = args.pandoc.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    fixture_image(output / 'image.png')
    (output / 'input.md').write_text('''---
title: Conversion verification
---

# Conversion verification

A paragraph with **bold text**, *italics*, a [link](https://example.com), and café.

| Item | Value |
| --- | --- |
| table | 42 |

![Purple rectangle](image.png)

Inline math: $x^2 + y^2$.
''', encoding='utf-8')
    results = []
    for extension in ('docx', 'epub', 'odt', 'html', 'rtf', 'tex'):
        path = output / ('document.' + extension)
        run([pandoc, 'input.md', '--standalone', '-o', path], output, env)
        if not path.stat().st_size:
            raise RuntimeError('Empty ' + extension + ' output')
        if extension != 'rtf':  # Pandoc writes RTF but does not read it.
            text = run([pandoc, path, '-t', 'plain'], output, env)
            for marker in ('Conversion verification', 'bold text', 'table', 'café'):
                if marker not in text:
                    raise RuntimeError(f'{extension} round trip lost {marker!r}')
        results.append({'conversion': 'markdown-' + extension, 'bytes': path.stat().st_size, 'passed': True})
    (output / 'uppercase.lua').write_text('function Header(h) h.content = pandoc.Str(string.upper(pandoc.utils.stringify(h))); return h end\n', encoding='utf-8')
    text = run([pandoc, 'input.md', '--lua-filter=uppercase.lua', '-t', 'plain'], output, env)
    if 'CONVERSION VERIFICATION' not in text:
        raise RuntimeError('Lua filter did not run')
    results.append({'conversion': 'lua-filter', 'passed': True})
    if args.pdf_engine:
        engine = args.pdf_engine.absolute()
        env['PATH'] = str(engine.parent) + os.pathsep + env['PATH']
        tex_root = engine.parent.parent.parent
        for key, folder in [('TEXMFVAR', 'texmf-var'), ('TEXMFCONFIG', 'texmf-config'), ('TEXMFHOME', 'texmf-home')]:
            destination = tex_root / folder
            destination.mkdir(parents=True, exist_ok=True)
            env[key] = str(destination)
        path = output / 'document.pdf'
        run([pandoc, 'input.md', '--pdf-engine=' + str(engine), '-o', path], output, env)
        if not path.read_bytes().startswith(b'%PDF-'):
            raise RuntimeError('PDF output is invalid')
        results.append({'conversion': 'markdown-pdf', 'bytes': path.stat().st_size, 'passed': True})
    (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
