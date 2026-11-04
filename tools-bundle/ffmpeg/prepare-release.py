#!/usr/bin/env python3
"""Attach verified notices and build sources to portable FFmpeg archives."""
import argparse
import copy
import json
import runpy
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
SHARED = HERE / '../imagemagick'
LOCK = json.loads((HERE / 'build.lock.json').read_text())
helpers = runpy.run_path(str(SHARED / 'prepare-release.py'))
relative_file = helpers['relative_file']
verified_notices = helpers['verified_notices']
archive_tree = helpers['archive_tree']
digest = helpers['digest']


def prepare_binary(archive_path, notices, index, output):
    expected = {f'ffmpeg-{LOCK["version"]}-MacOSX-{LOCK["minimumMacOS"]}-{arch}.tar.xz' for arch in ['arm64', 'x86_64']}
    if archive_path.name not in expected:
        raise ValueError('Build archive does not match the pinned version and target')
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        directory = Path(temporary)
        with tarfile.open(archive_path) as archive:
            roots = {PurePosixPath(m.name).parts[0] for m in archive.getmembers() if PurePosixPath(m.name).parts}
            if len(roots) != 1:
                raise ValueError('Expected one archive root')
            archive.extractall(directory, filter='data')
        root = directory / roots.pop()
        for name in ['ffmpeg', 'ffprobe']:
            if not (root / 'bin' / name).is_file():
                raise ValueError(f'Missing executable: {name}')
        for file in (root / '.xcpkg').glob('*'):
            if file.name in ('RECEIPT.yml', 'MANIFEST.txt', 'dependencies.d2', 'dependencies.dot', 'config.log'):
                file.unlink()
        for pattern in ['.xcpkg/dependencies/*.yml', 'bin/*-config', 'lib/*.la', 'lib/*.a', '.xcpkg/dependencies/lib/*.a']:
            for file in root.glob(pattern):
                file.unlink()
        for directory in [root / 'include', root / 'lib/pkgconfig']:
            if directory.exists():
                shutil.rmtree(directory)
        for name in ['ffmpeg', 'ffprobe', 'ffplay']:
            if not (root / 'bin' / name).exists():
                continue
            launcher = root / name
            launcher.write_text('#!/bin/sh\n'
                'TOOL_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
                'export DYLD_LIBRARY_PATH="$TOOL_ROOT/lib:$TOOL_ROOT/.xcpkg/dependencies/lib"\n'
                f'exec "$TOOL_ROOT/bin/{name}" "$@"\n')
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
                'binaryNotices': len(binary_index['notices']), 'root': root.name, 'minimumOS': LOCK['minimumMacOS']}


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
    for source in source_index['sources']:
        if source['kind'] == 'src' and not any(n['component'] == source['component'] and n['origin'].get('sha256') == source['sha256'] for n in index['notices']):
            raise ValueError(f'Notices do not match source input: {source["component"]}')
    if index['rustRuntime']['commit'] != LOCK['rustCommit'] or index['rustRuntime']['release'] != LOCK['rustVersion']:
        raise ValueError('Notices do not match the pinned Rust runtime')
    results = [prepare_binary(archive, args.notices, index, args.out) for archive in args.archive]
    with tempfile.TemporaryDirectory(dir=args.out) as temporary:
        root = Path(temporary)
        shutil.copytree(args.sources / 'recipes', root / 'recipes')
        shutil.copyfile(args.sources / 'source-index.json', root / 'source-index.json')
        shutil.copytree(args.notices, root / 'notices')
        files = {
            'ffmpeg': ['README.md', 'LICENSE', 'APACHE-2.0.txt', 'NOTICE', 'build.mjs', 'build.lock.json',
                'ffmpeg.yml', 'libx264.yml', 'libvpx.yml', 'collect-sources.py', 'verify.mjs', 'prepare-release.py'],
            'imagemagick': ['README.md', 'LICENSE', 'APACHE-2.0.txt', 'NOTICE', 'libaom.yml', 'librav1e.yml',
                'libfreetype.yml', 'libx265.yml', 'build.mjs', 'build.lock.json', 'imagemagick.yml', 'libheif.yml',
                'collect-sources.py', 'collect-notices.py', 'prepare-release.py',
                'xcpkg-command-line-tools.patch', 'xcpkg-relocation.patch', 'xcpkg-macos-cross.patch'],
        }
        for folder, names in files.items():
            destination = root / 'tools-bundle' / folder
            destination.mkdir(parents=True)
            for name in names:
                shutil.copyfile(HERE.parent / folder / name, destination / name)
        target = args.out / 'ffmpeg-build-sources.tar.gz'
        archive_tree(root, target, 'ffmpeg-build', 'gz')
        results.append({'filename': target.name, 'sha256': digest(target), 'size': target.stat().st_size})
    (args.out / 'artifacts.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
