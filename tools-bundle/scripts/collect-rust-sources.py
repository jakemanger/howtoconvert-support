#!/usr/bin/env python3
"""Collect the crate sources pinned by a Cargo.lock file."""
import argparse
import concurrent.futures
import gzip
import hashlib
import json
import re
import shutil
import tarfile
import tomllib
import urllib.request
from pathlib import Path


def digest(file):
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def collect(lock, output):
    data = tomllib.loads(lock.read_text())
    packages = []
    for package in data['package']:
        source = package.get('source')
        if not source:
            continue
        if source != 'registry+https://github.com/rust-lang/crates.io-index':
            raise ValueError(f'Uncollected Rust source: {package["name"]}')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', package['name']) or not re.fullmatch(r'[A-Za-z0-9.+-]+', package['version']) or not re.fullmatch(r'[a-f0-9]{64}', package.get('checksum', '')):
            raise ValueError('Invalid crate source pin')
        packages.append(package)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'crates').mkdir(exist_ok=True)
    shutil.copyfile(lock, output / 'Cargo.lock')

    def fetch(package):
        name, version, expected = package['name'], package['version'], package['checksum']
        filename = f'{name}-{version}.crate'
        destination = output / 'crates' / filename
        if not destination.exists() or digest(destination) != expected:
            candidates = list((Path.home() / '.cargo/registry/cache').glob(f'*/{filename}'))
            cached = next((file for file in candidates if digest(file) == expected), None)
            if cached:
                shutil.copyfile(cached, destination)
            else:
                url = f'https://static.crates.io/crates/{name}/{filename}'
                temporary = destination.with_suffix('.partial')
                with urllib.request.urlopen(url, timeout=60) as response, temporary.open('wb') as file:
                    shutil.copyfileobj(response, file)
                if digest(temporary) != expected:
                    raise ValueError(f'Crate checksum mismatch: {filename}')
                temporary.rename(destination)
        return {'name': name, 'version': version, 'filename': filename, 'sha256': expected, 'size': destination.stat().st_size}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        crates = list(pool.map(fetch, packages))
    index = {'schema': 1, 'lockSha256': digest(lock), 'crates': crates}
    (output / 'source-index.json').write_text(json.dumps(index, indent=2) + '\n')
    archive = output / 'rust-dependencies-source.tar.gz'
    with archive.open('wb') as file:
        with gzip.GzipFile(fileobj=file, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as tar:
                for item in [output / 'Cargo.lock', output / 'source-index.json', *[output / 'crates' / entry['filename'] for entry in sorted(crates, key=lambda entry: entry['filename'])]]:
                    info = tar.gettarinfo(str(item), arcname=str(item.relative_to(output)))
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ''
                    info.mode = 0o644
                    with item.open('rb') as stream:
                        tar.addfile(info, stream)
    return {'filename': archive.name, 'sha256': digest(archive), 'size': archive.stat().st_size, 'crates': len(crates)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('lock', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(json.dumps(collect(args.lock, args.output), indent=2))
