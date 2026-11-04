#!/usr/bin/env python3
"""Split a verified upstream Windows source companion into smaller archives."""
import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def digest(file):
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def pack(source, entries, destination):
    def normalized(info):
        info.uid = info.gid = info.mtime = 0
        info.uname = info.gname = ''
        return info
    with destination.open('wb') as output:
        with gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', dereference=False) as archive:
                for entry in sorted(entries):
                    archive.add(source / entry, arcname=str(entry), filter=normalized)


def split(source, expected, version, output):
    if digest(source) != expected:
        raise ValueError('Source companion checksum mismatch')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+-[0-9]+', version):
        raise ValueError('Invalid ImageMagick release version')
    names = subprocess.check_output(['tar', '-tf', str(source)], text=True).splitlines()
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or path.parts[0] not in ['Configure', 'Dependencies', 'ImageMagick']:
            raise ValueError(f'Unexpected source archive entry: {name}')
    output.mkdir(parents=True, exist_ok=False)
    parts = []
    with tempfile.TemporaryDirectory(prefix='source-', dir=output) as temporary:
        tree = Path(temporary)
        subprocess.run(['tar', '-xf', str(source), '--exclude=*/.git', '-C', str(tree)], check=True)
        for current, directories, files in os.walk(tree):
            if '.git' in directories:
                raise ValueError('Git history was not excluded')
            for name in directories + files:
                file = Path(current) / name
                if file.is_symlink() and not file.resolve().is_relative_to(tree):
                    raise ValueError(f'Source symlink escapes its tree: {file.relative_to(tree)}')
        groups = {'core': [Path('ImageMagick')], 'configure': [Path('Configure'), Path('ImageMagick/LICENSE')]}
        dependency_roots = {'Dependencies', 'OptionalDependencies', 'NonWindowsDependencies'}
        groups['build-recipes'] = [p.relative_to(tree) for p in (tree / 'Dependencies').iterdir() if p.name not in dependency_roots]
        for category in sorted(dependency_roots):
            for component in sorted((tree / 'Dependencies' / category).iterdir()):
                if not component.is_dir():
                    raise ValueError(f'Unexpected dependency entry: {component.name}')
                groups[f'dependency-{component.name}'] = [component.relative_to(tree)]
        for name, entries in groups.items():
            filename = f'imagemagick-{version}-windows-{name}-source.tar.gz'
            file = output / filename
            pack(tree, entries, file)
            parts.append({'component': name, 'filename': filename, 'sha256': digest(file), 'size': file.stat().st_size})
    index = {'schema': 1, 'version': version, 'original': {'filename': source.name, 'sha256': expected}, 'sources': parts}
    (output / 'source-index.json').write_text(json.dumps(index, indent=2) + '\n')
    return index


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('sha256')
    parser.add_argument('version')
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    index = split(args.archive.resolve(), args.sha256, args.version, args.output.resolve())
    print(f'Created {len(index["sources"])} matching source archives.')
