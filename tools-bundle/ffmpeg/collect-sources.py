#!/usr/bin/env python3
"""Collect checksum-matched sources and recipes for an FFmpeg build."""
import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--work', required=True, type=Path)
parser.add_argument('--out', required=True, type=Path)
parser.add_argument('--xcpkg', required=True)
parser.add_argument('--cargo-cache', type=Path, default=Path.home() / '.cargo/registry/cache')
args = parser.parse_args()
recipes = args.work / 'xcpkg/repos.d/official-core/formula'
cache = args.work / 'xcpkg/downloads'
args.out.mkdir(parents=True, exist_ok=True)
(args.out / 'recipes').mkdir(exist_ok=True)

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def copy_verified(source, destination, expected):
    if digest(source) != expected:
        raise ValueError(f'Checksum mismatch: {source.name}')
    if not destination.exists() or digest(destination) != expected:
        shutil.copyfile(source, destination)

formulas = {}
pending = ['ffmpeg']
while pending:
    name = pending.pop()
    if name in formulas:
        continue
    formula = recipes / f'{name}.yml'
    data = json.loads(subprocess.check_output([args.xcpkg, 'formula-parse', str(formula), '--json']))
    formulas[name] = data
    pending.extend(data.get('dep-pkg', '').split())
    shutil.copyfile(formula, args.out / 'recipes' / formula.name)

sources = []
for name, formula in sorted(formulas.items()):
    for kind in ['src', 'fix', 'res']:
        if kind == 'src' and formula.get('git-url') and not formula.get('src-url'):
            raise ValueError(f'Uncollected Git source: {name}; pin a source archive first')
        url = formula.get(f'{kind}-url')
        if not url:
            continue
        expected = formula.get(f'{kind}-sha')
        matches = list(cache.glob(f'{expected}.*')) if expected else []
        if len(matches) != 1:
            raise ValueError(f'Missing pinned {kind} for {name}; finish the build first')
        source = matches[0]
        filename = f'{name}-{formula["version"]}-{kind}{source.suffix}'
        copy_verified(source, args.out / filename, expected)
        sources.append({'component': name, 'version': formula['version'], 'kind': kind,
                        'url': url, 'sha256': expected, 'filename': filename})
        if name != 'librav1e' or kind != 'src':
            continue
        with tarfile.open(source) as archive:
            lock_member = next(m for m in archive if m.name.endswith('/Cargo.lock') and m.name.count('/') == 1)
            lock = archive.extractfile(lock_member).read()
        packages = tomllib.loads(lock.decode())['package']
        vendor_name = f'librav1e-{formula["version"]}-rust-sources.tar.gz'
        crates = []
        with tempfile.TemporaryDirectory(dir=args.out) as temporary:
            directory = Path(temporary)
            (directory / 'Cargo.lock').write_bytes(lock)
            for package in packages:
                if not package.get('source', '').startswith('registry+'):
                    if package.get('source'):
                        raise ValueError(f'Uncollected non-registry Rust source: {package["name"]}')
                    continue
                crate_name = f'{package["name"]}-{package["version"]}.crate'
                matches = list(args.cargo_cache.glob(f'*/{crate_name}'))
                if not matches:
                    raise ValueError(f'Missing Rust source archive: {crate_name}')
                copy_verified(matches[0], directory / crate_name, package['checksum'])
                crates.append({'name': package['name'], 'version': package['version'],
                               'sha256': package['checksum'], 'filename': crate_name})
            with (args.out / vendor_name).open('wb') as output:
                with gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0) as compressed:
                    with tarfile.open(fileobj=compressed, mode='w') as archive:
                        for file in sorted(directory.iterdir()):
                            info = archive.gettarinfo(str(file), arcname=f'crates/{file.name}')
                            info.uid = info.gid = info.mtime = 0
                            info.uname = info.gname = ''
                            info.mode = 0o644
                            with file.open('rb') as stream:
                                archive.addfile(info, stream)
        sources.append({'component': 'librav1e', 'version': formula['version'], 'kind': 'rust-dependencies',
                        'filename': vendor_name, 'sha256': digest(args.out / vendor_name), 'crates': crates})

# Include the exact standard-library sources for statically linked Rust code.
lock = json.loads(Path(__file__).with_name('build.lock.json').read_text())
runtime_cache = args.work / 'runtime-source-cache'
runtime_cache.mkdir(parents=True, exist_ok=True)
for source in lock['rustSources']:
    cached = runtime_cache / source['filename']
    if not cached.exists() or digest(cached) != source['sha256']:
        temporary = cached.with_name(cached.name + '.part')
        with urllib.request.urlopen(source['url'], timeout=90) as response, temporary.open('wb') as stream:
            shutil.copyfileobj(response, stream)
        if digest(temporary) != source['sha256']:
            raise ValueError(f'Runtime source checksum mismatch: {source["filename"]}')
        temporary.replace(cached)
    copy_verified(cached, args.out / source['filename'], source['sha256'])
    sources.append(source)

index = {'sources': sources}
(args.out / 'source-index.json').write_text(json.dumps(index, indent=2) + '\n')
print(f'Collected {len(sources)} source archives and {len(formulas)} recipes')
