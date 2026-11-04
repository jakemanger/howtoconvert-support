#!/usr/bin/env python3
"""Collect pinned TeX sources and preserve complete source trees in smaller parts."""
import argparse
import copy
import gzip
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


def digest(path, algorithm='sha256'):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def plain(name):
    if not name or name in ('.', '..') or '/' in name or '\\' in name:
        raise ValueError('Expected a plain filename')
    return name


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or not path.parts:
        raise ValueError(f'Unsafe archive path: {name}')
    return path


def safe_members(archive):
    members = archive.getmembers()
    roots = {safe_name(member.name).parts[0] for member in members}
    if len(roots) != 1:
        raise ValueError('Engine source must have one root directory')
    root = roots.pop()
    result, seen = [], set()
    for member in members:
        original = safe_name(member.name)
        if len(original.parts) == 1:
            if not member.isdir():
                raise ValueError('Source root must be a directory')
            continue
        relative = PurePosixPath(*original.parts[1:])
        if str(relative) in seen:
            raise ValueError(f'Duplicate archive path: {relative}')
        seen.add(str(relative))
        if not (member.isfile() or member.isdir() or member.issym()):
            raise ValueError(f'Unsupported source member: {relative}')
        if member.issym():
            target = os.path.normpath(str(relative.parent / member.linkname))
            if member.linkname.startswith('/') or target == '..' or target.startswith('../'):
                raise ValueError(f'Source link escapes tree: {relative}')
        result.append((relative, member))
    links = {str(relative) for relative, member in result if member.issym()}
    for relative, _ in result:
        if any(str(parent) in links for parent in relative.parents):
            raise ValueError(f'Source entry traverses a link: {relative}')
    return root, result


def group_for(relative):
    parts = relative.parts
    if len(parts) > 2 and parts[0] in ('libs', 'texk', 'utils'):
        return parts[0] + '-' + parts[1]
    return 'build'


def normalized(member, name):
    value = copy.copy(member)
    value.name = str(name)
    value.uid = value.gid = value.mtime = 0
    value.uname = value.gname = ''
    value.pax_headers = {}
    value.mode &= 0o777
    return value


def split_engine(source, output, known):
    # A temporary uncompressed tar permits seeking between components without
    # repeatedly decompressing the complete upstream archive.
    with tempfile.TemporaryDirectory(prefix='tex-split-', dir=output) as temporary:
        raw = Path(temporary) / 'source.tar'
        with gzip.open(source, 'rb') as compressed, raw.open('wb') as target:
            shutil.copyfileobj(compressed, target)
        with tarfile.open(raw, 'r:') as archive:
            _, members = safe_members(archive)
            groups, manifest = {}, []
            for relative, member in sorted(members, key=lambda pair: str(pair[0])):
                record = {'path': str(relative), 'mode': member.mode & 0o777,
                          'type': 'link' if member.issym() else 'dir' if member.isdir() else 'file'}
                if member.issym():
                    record['target'] = member.linkname
                elif member.isfile():
                    record['size'] = member.size
                    record['sha256'] = hashlib.file_digest(archive.extractfile(member), 'sha256').hexdigest()
                manifest.append(record)
                groups.setdefault(group_for(relative), []).append((relative, member, record))
            parts = []
            for group, entries in sorted(groups.items()):
                tree_hash = hashlib.sha256(json.dumps([entry[2] for entry in entries], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                if tree_hash in known:
                    parts.append(known[tree_hash])
                    continue
                filename = f'texlive-{group}-{tree_hash[:16]}-sources.tar.gz'
                target = output / filename
                if target.exists():
                    raise ValueError(f'Conflicting source part: {filename}')
                with target.open('wb') as stream, gzip.GzipFile(fileobj=stream, mode='wb', filename='', mtime=0) as compressed:
                    with tarfile.open(fileobj=compressed, mode='w') as destination:
                        for relative, member, _ in entries:
                            destination.addfile(normalized(member, relative), archive.extractfile(member) if member.isfile() else None)
                part = {'component': group, 'filename': filename, 'sha256': digest(target), 'size': target.stat().st_size, 'treeSha256': tree_hash, 'files': len(entries)}
                known[tree_hash] = part
                parts.append(part)
            return {'parts': parts, 'files': manifest}


def locate(item, caches, download=False):
    name = plain(item['filename'])
    algorithm = 'sha512' if 'sha512' in item else 'sha256'
    expected = item[algorithm]
    if not re.fullmatch('[0-9a-f]{' + str(128 if algorithm == 'sha512' else 64) + '}', expected):
        raise ValueError(f'Invalid checksum: {name}')
    for cache in caches:
        for directory in (cache, cache / 'upstream', cache / 'packages', cache / 'package-docs'):
            file = directory / name
            if file.exists() or file.is_symlink():
                if file.is_symlink() or not file.is_file():
                    raise ValueError(f'Input must be a regular file: {name}')
                if file.stat().st_size != item['size'] or digest(file, algorithm) != expected:
                    raise ValueError(f'Input checksum or size mismatch: {name}')
                return file
    if not download:
        raise ValueError(f'Missing pinned input: {name}')
    if not item['url'].startswith('https://'):
        raise ValueError('Source download must use HTTPS')
    caches[0].mkdir(parents=True, exist_ok=True)
    file = caches[0] / name
    temporary = file.with_suffix(file.suffix + '.partial')
    try:
        with urllib.request.urlopen(item['url']) as response, temporary.open('xb') as stream:
            shutil.copyfileobj(response, stream)
        if temporary.stat().st_size != item['size'] or digest(temporary, algorithm) != expected:
            raise ValueError(f'Download checksum or size mismatch: {name}')
        temporary.rename(file)
    finally:
        temporary.unlink(missing_ok=True)
    return file


def collect(lock, caches, output, download=False):
    if lock.get('schema') != 1 or not lock.get('engines'):
        raise ValueError('Invalid TeX source lock')
    if output.exists() or output.is_symlink():
        raise ValueError('Output already exists')
    inputs = lock['engines'] + lock['packages'] + lock['documentation'] + lock.get('fontDistributions', []) + [lock['buildDocumentation']]
    resolved = {item['filename']: locate(item, caches, download) for item in inputs}
    if len(resolved) != len(inputs):
        raise ValueError('Duplicate input filenames')
    output.mkdir(parents=True)
    known, engines, artifacts = {}, [], []
    try:
        for engine in lock['engines']:
            result = split_engine(resolved[engine['filename']], output, known)
            if not re.fullmatch(r'svn[0-9]+', engine['revision']):
                raise ValueError('Invalid source revision')
            manifest_name = f"texlive-{engine['revision']}-files.json"
            (output / manifest_name).write_text(json.dumps(result['files'], separators=(',', ':')) + '\n')
            engines.append({'revision': engine['revision'], 'commit': engine['commit'], 'upstreamSha256': engine['sha256'], 'manifest': manifest_name, 'manifestSha256': digest(output / manifest_name), 'parts': result['parts']})
            print(f"Prepared {engine['revision']}: {len(result['files'])} files in {len(result['parts'])} components", flush=True)
        for item in lock['packages'] + lock['documentation'] + lock.get('fontDistributions', []):
            source = resolved[item['filename']]
            shutil.copyfile(source, output / item['filename'])
            artifacts.append({'filename': item['filename'], 'sha256': digest(output / item['filename']), 'size': item['size'], 'kind': item['kind'], 'package': item['package']})
        documentation = resolved[lock['buildDocumentation']['filename']]
        docs = output / 'mac-build'; docs.mkdir()
        with zipfile.ZipFile(documentation) as archive:
            for name in lock['buildDocumentation']['members']:
                safe_name(name)
                data = archive.read(name)
                target = docs / plain(PurePosixPath(name).name)
                if target.exists():
                    raise ValueError('Duplicate build document filename')
                target.write_bytes(data)
        (output / 'source-lock.json').write_text(json.dumps(lock, indent=2) + '\n')
        index = {'schema': 1, 'engines': engines, 'components': list(known.values()), 'packages': artifacts}
        (output / 'source-index.json').write_text(json.dumps(index, indent=2) + '\n')
        return index
    except Exception:
        shutil.rmtree(output)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=Path(__file__).with_name('sources.lock.json'))
    parser.add_argument('--cache', type=Path, action='append', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    collect(json.loads(args.lock.read_text()), args.cache, args.out, args.download)
