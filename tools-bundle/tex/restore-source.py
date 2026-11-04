#!/usr/bin/env python3
"""Restore one pinned TeX Live source tree from its verified component archives."""
import argparse
import hashlib
import json
import os
import shutil
import tarfile
from pathlib import Path


def digest(file):
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def plain(name):
    if not name or name in ('.', '..') or '/' in name or '\\' in name:
        raise ValueError('Expected a plain filename')
    return name


def source_file(directory, name):
    name = plain(name)
    for candidate in (directory / name, directory / ('source-' + name)):
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError(f'Source part must be a regular file: {name}')
            return candidate
    raise ValueError(f'Missing source part: {name}')


def restore(directory, revision, output):
    if output.exists() or output.is_symlink():
        raise ValueError('Output already exists')
    index = json.loads((directory / 'source-index.json').read_text())
    matches = [item for item in index['engines'] if item['revision'] == revision]
    if len(matches) != 1:
        raise ValueError('Unknown or duplicate source revision')
    engine = matches[0]
    manifest = directory / plain(engine['manifest'])
    if digest(manifest) != engine['manifestSha256']:
        raise ValueError('File manifest checksum mismatch')
    expected = json.loads(manifest.read_text())
    for part in engine['parts']:
        file = source_file(directory, part['filename'])
        if file.is_symlink() or not file.is_file() or file.stat().st_size != part['size'] or digest(file) != part['sha256']:
            raise ValueError(f"Source part checksum or size mismatch: {part['filename']}")
    output.mkdir(parents=True)
    try:
        for part in engine['parts']:
            with tarfile.open(source_file(directory, part['filename'])) as archive:
                # data_filter rejects absolute links, escapes and special files.
                archive.extractall(output, filter='data')
        actual = []
        for file in sorted(output.rglob('*'), key=lambda value: value.relative_to(output).as_posix()):
            name = file.relative_to(output).as_posix()
            record = {'path': name, 'mode': file.lstat().st_mode & 0o777,
                      'type': 'link' if file.is_symlink() else 'dir' if file.is_dir() else 'file'}
            if file.is_symlink():
                record['target'] = os.readlink(file)
            elif file.is_file():
                record.update(size=file.stat().st_size, sha256=digest(file))
            actual.append(record)
        # The safe filter strips writable bits and platforms differ in link
        # modes. Restore the pinned modes after verifying every path and byte.
        for a, b in zip(actual, expected):
            if a['type'] == b['type'] and a['path'] == b['path']:
                a['mode'] = b['mode']
        if actual != expected:
            raise ValueError('Restored tree does not match the complete source manifest')
        for item in expected:
            if item['type'] != 'link':
                os.chmod(output / item['path'], item['mode'])
            elif hasattr(os, 'lchmod'):
                os.lchmod(output / item['path'], item['mode'])
        return len(expected)
    except Exception:
        shutil.rmtree(output)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', type=Path)
    parser.add_argument('revision')
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    count = restore(args.sources, args.revision, args.output)
    print(f'Restored {count} verified source entries.')
