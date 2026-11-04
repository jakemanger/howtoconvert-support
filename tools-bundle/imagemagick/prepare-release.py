#!/usr/bin/env python3
"""Attach verified notices and build sources to portable ImageMagick archives."""
import argparse
import copy
import gzip
import hashlib
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def digest(file):
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def relative_file(root, name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError(f'Unsafe relative path: {name}')
    file = root / path
    if not file.is_file() or file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Expected a regular file: {name}')
    return file


def verified_notices(root):
    index = json.loads((root / 'index.json').read_text())
    if index.get('schema') != 1 or index.get('gaps') or not index.get('notices'):
        raise ValueError('Notices must be complete')
    seen = set()
    for notice in index['notices']:
        if notice['scope'] not in ('binary', 'source-only') or notice['path'] in seen:
            raise ValueError('Invalid notice scope or duplicate path')
        seen.add(notice['path'])
        if digest(relative_file(root, notice['path'])) != notice['sha256']:
            raise ValueError(f'Notice checksum mismatch: {notice["path"]}')
    return index


def archive_tree(root, target, prefix, mode):
    def add(archive):
        for file in [root, *sorted(root.rglob('*'))]:
            name = str(PurePosixPath(prefix) / file.relative_to(root).as_posix())
            info = archive.gettarinfo(str(file), arcname=name)
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ''
            if info.isfile():
                with file.open('rb') as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
    if mode == 'gz':
        with target.open('wb') as output, gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                add(archive)
    else:
        with tarfile.open(target, 'w:xz') as archive:
            add(archive)


def prepare_binary(archive_path, notices, index, output):
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        directory = Path(temporary)
        with tarfile.open(archive_path) as archive:
            members = archive.getmembers()
            roots = {PurePosixPath(m.name).parts[0] for m in members if PurePosixPath(m.name).parts}
            if len(roots) != 1:
                raise ValueError('Expected one archive root')
            # The data filter rejects traversal, unsafe links and special files.
            archive.extractall(directory, filter='data')
        root = directory / roots.pop()
        if not (root / 'bin/magick').is_file():
            raise ValueError('Missing ImageMagick executable')
        # Build-machine receipts and SDK helper files are not runtime inputs.
        for file in (root / '.xcpkg').glob('*'):
            if file.name in ('RECEIPT.yml', 'MANIFEST.txt', 'config.log', 'dependencies.d2', 'dependencies.dot'):
                file.unlink()
        for file in (root / '.xcpkg/dependencies').glob('*.yml'):
            file.unlink()
        for pattern in ('bin/*-config', 'lib/*.la'):
            for file in root.glob(pattern):
                file.unlink()
        for directory in (root / 'include', root / 'lib/pkgconfig'):
            if directory.exists():
                shutil.rmtree(directory)
        # External delegates must use the destination system's basic utilities.
        delegates = root / 'etc/ImageMagick-7/delegates.xml'
        text = delegates.read_text()
        text = re.sub(r'[^\s\"<>;]+/uppm/installed/coreutils/bin/(rm|mv|mkdir)\b', r'/bin/\1', text)
        delegates.write_text(text)
        for file in root.glob('lib/ImageMagick-*/config-*/configure.xml'):
            text = file.read_text()
            text = re.sub(r'\s*<configure\b[^>]*(?:/Volumes/|/Users/|/home/|/tmp/)[^>]*/>', '', text)
            file.write_text(text)
        launcher = root / 'magick'
        launcher.write_text('''#!/bin/sh
# Run the portable CLI with its own decoder modules and libraries.
MAGICK_HOME=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
export MAGICK_HOME
export MAGICK_CONFIGURE_PATH="$MAGICK_HOME/etc/ImageMagick-7"
for module_path in "$MAGICK_HOME"/lib/ImageMagick-*/modules-*/coders; do
    [ -d "$module_path" ] || exit 1
    export MAGICK_CODER_MODULE_PATH="$module_path"
    break
done
export DYLD_LIBRARY_PATH="$MAGICK_HOME/lib:$MAGICK_HOME/.xcpkg/dependencies/lib"
exec "$MAGICK_HOME/bin/magick" "$@"
''')
        launcher.chmod(0o755)
        target = root / 'HTC-LICENSES/components'
        binary_index = copy.deepcopy(index)
        binary_index['notices'] = [n for n in index['notices'] if n['scope'] == 'binary']
        for notice in binary_index['notices']:
            destination = target / notice['path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(relative_file(notices, notice['path']), destination)
        (target / 'index.json').write_text(json.dumps(binary_index, indent=2) + '\n')
        result = output / archive_path.name
        archive_tree(root, result, root.name, 'xz')
        return {'filename': result.name, 'sha256': digest(result), 'size': result.stat().st_size,
                'binaryNotices': len(binary_index['notices']), 'root': root.name}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', action='append', type=Path, required=True)
    parser.add_argument('--notices', type=Path, required=True)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if any((args.out / archive.name).resolve() == archive.resolve() for archive in args.archive):
        raise ValueError('Output must not overwrite a build archive')
    index = verified_notices(args.notices)
    source_index = json.loads((args.sources / 'source-index.json').read_text())
    for source in source_index['sources']:
        if digest(relative_file(args.sources, source['filename'])) != source['sha256']:
            raise ValueError(f'Source checksum mismatch: {source["filename"]}')
    results = [prepare_binary(archive, args.notices, index, args.out) for archive in args.archive]
    with tempfile.TemporaryDirectory(dir=args.out) as temporary:
        root = Path(temporary)
        shutil.copytree(args.sources / 'recipes', root / 'recipes')
        shutil.copyfile(args.sources / 'source-index.json', root / 'source-index.json')
        shutil.copytree(args.notices, root / 'notices')
        here = Path(__file__).resolve().parent
        build_files = [
            'README.md', 'LICENSE', 'APACHE-2.0.txt', 'NOTICE', 'build.mjs', 'build.lock.json',
            'imagemagick.yml', 'libheif.yml', 'librav1e.yml', 'libaom.yml', 'libfreetype.yml', 'libx265.yml',
            'xcpkg-command-line-tools.patch', 'xcpkg-relocation.patch', 'xcpkg-macos-cross.patch',
            'collect-sources.py', 'collect-notices.py', 'prepare-release.py',
        ]
        for name in build_files:
            shutil.copyfile(here / name, root / name)
        target = args.out / 'imagemagick-build-sources.tar.gz'
        archive_tree(root, target, 'imagemagick-build', 'gz')
        results.append({'filename': target.name, 'sha256': digest(target), 'size': target.stat().st_size})
    (args.out / 'artifacts.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
