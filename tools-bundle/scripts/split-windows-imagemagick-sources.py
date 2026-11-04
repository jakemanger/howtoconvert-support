#!/usr/bin/env python3
"""Split a verified upstream Windows source companion into smaller archives."""
import argparse
import contextlib
import copy
import gzip
import hashlib
import json
import os
import re
import posixpath
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



def source_groups(tree):
    roots = {item.name for item in tree.iterdir()}
    modern = {'Configure', 'Dependencies', 'ImageMagick'}
    legacy = modern | {'Artifacts', 'Build', 'OptionalDependencies', 'Projects'}
    groups = {'core': [Path('ImageMagick')], 'configure': [Path('Configure'), Path('ImageMagick/LICENSE')]}
    if roots == legacy:
        groups['build-recipes'] = [Path(name) for name in ('Artifacts', 'Build', 'Projects')]
        dependency_roots = [Path('Dependencies'), Path('OptionalDependencies')]
    elif roots == modern:
        categories = {'Dependencies', 'OptionalDependencies', 'NonWindowsDependencies'}
        groups['build-recipes'] = [p.relative_to(tree) for p in (tree / 'Dependencies').iterdir() if p.name not in categories]
        dependency_roots = [Path('Dependencies') / category for category in sorted(categories)]
    else:
        raise ValueError(f'Unexpected source tree roots: {sorted(roots)}')
    groups['build-recipes'].append(Path('ImageMagick/LICENSE'))
    for category in dependency_roots:
        for component in sorted((tree / category).iterdir()):
            if not component.is_dir():
                raise ValueError(f'Unexpected dependency entry: {component.name}')
            name = f'dependency-{component.name}'
            if name in groups:
                raise ValueError(f'Duplicate dependency component: {component.name}')
            groups[name] = [component.relative_to(tree)]
    return groups


def member_groups(names):
    paths = [PurePosixPath(name) for name in names]
    roots = {path.parts[0] for path in paths}
    modern = {'Configure', 'Dependencies', 'ImageMagick'}
    legacy = modern | {'Artifacts', 'Build', 'OptionalDependencies', 'Projects'}
    if roots not in (modern, legacy):
        raise ValueError(f'Unexpected source tree roots: {sorted(roots)}')
    legacy_layout = roots == legacy
    categories = {'Dependencies', 'OptionalDependencies', 'NonWindowsDependencies'}
    mapping = {}
    component_roots = {}
    for path in paths:
        parts = path.parts
        if '.git' in parts:
            continue
        groups = []
        if parts[0] == 'ImageMagick':
            groups = ['core']
            if str(path) == 'ImageMagick/LICENSE':
                groups += ['configure', 'build-recipes']
        elif parts[0] == 'Configure':
            groups = ['configure']
        elif legacy_layout and parts[0] in {'Artifacts', 'Build', 'Projects'}:
            groups = ['build-recipes']
        else:
            offset = 1 if legacy_layout else 2
            dependency = legacy_layout or (len(parts) > 1 and parts[1] in categories)
            if dependency and len(parts) > offset:
                component = parts[offset]
                root = '/'.join(parts[:offset + 1])
                key = 'dependency-' + component
                if key in component_roots and component_roots[key] != root:
                    raise ValueError(f'Duplicate dependency component: {component}')
                component_roots[key] = root
                groups = [key]
            elif not dependency and len(parts) > 1:
                groups = ['build-recipes']
        mapping[str(path)] = groups
    return mapping


def split(source, expected, version, output):
    if digest(source) != expected:
        raise ValueError('Source companion checksum mismatch')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+-[0-9]+', version):
        raise ValueError('Invalid ImageMagick release version')
    names = subprocess.check_output(['tar', '-tf', str(source)], text=True).splitlines()
    normalized = []
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] not in ['Artifacts', 'Build', 'Configure', 'Dependencies', 'ImageMagick', 'OptionalDependencies', 'Projects']:
            raise ValueError(f'Unexpected source archive entry: {name}')
        normalized.append(str(path))
    if len(normalized) != len(set(normalized)):
        raise ValueError('Duplicate source archive entry')
    groups = member_groups(normalized)
    output.mkdir(parents=True, exist_ok=False)
    parts = []
    # BSD tar reads the verified 7z once. Stream each member directly into its
    # component archive; no expanded source tree or second large copy is needed.
    with contextlib.ExitStack() as stack:
        archives = {}
        for group in sorted({group for values in groups.values() for group in values}):
            filename = f'imagemagick-{version}-windows-{group}-source.tar.gz'
            target = stack.enter_context((output / filename).open('wb'))
            compressed = stack.enter_context(gzip.GzipFile(fileobj=target, mode='wb', filename='', mtime=0))
            archives[group] = stack.enter_context(tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT))
            parts.append({'component': group, 'filename': filename})
        errors = stack.enter_context(tempfile.TemporaryFile())
        process = subprocess.Popen(['tar', '-cf', '-', '--format', 'pax', '@' + str(source)], stdout=subprocess.PIPE, stderr=errors)
        seen = set()
        try:
            with tarfile.open(fileobj=process.stdout, mode='r|') as incoming:
                for member in incoming:
                    name = str(PurePosixPath(member.name))
                    if '.git' in PurePosixPath(name).parts:
                        continue
                    if name not in groups or name in seen:
                        raise ValueError(f'Unexpected streamed source entry: {name}')
                    seen.add(name)
                    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                        raise ValueError(f'Unsupported source entry type: {name}')
                    if member.issym() or member.islnk():
                        link = PurePosixPath(member.linkname)
                        parent = str(PurePosixPath(name).parent) if member.issym() else ''
                        target = posixpath.normpath(posixpath.join(parent, str(link)))
                        if link.is_absolute() or target == '..' or target.startswith('../'):
                            raise ValueError(f'Source link escapes its tree: {name}')
                    destinations = groups[name]
                    if not destinations:
                        if not member.isdir():
                            raise ValueError(f'Unassigned source file: {name}')
                        continue
                    member = copy.copy(member)
                    member.uid = member.gid = member.mtime = 0
                    member.uname = member.gname = ''
                    member.pax_headers = {}
                    if member.isfile() and len(destinations) > 1:
                        # Only the small original core licence is intentionally
                        # repeated alongside configuration/build files.
                        if name != 'ImageMagick/LICENSE' or member.size > 1024 * 1024:
                            raise ValueError(f'Unexpected repeated source file: {name}')
                        import io
                        data = incoming.extractfile(member).read()
                        for group in destinations:
                            archives[group].addfile(member, io.BytesIO(data))
                    else:
                        contents = incoming.extractfile(member) if member.isfile() else None
                        archives[destinations[0]].addfile(member, contents)
            process.stdout.close()
            if process.wait() != 0:
                errors.seek(0)
                raise ValueError('Source archive streaming failed: ' + errors.read(4096).decode('utf-8', 'replace'))
            if seen != set(groups):
                raise ValueError('Source stream did not contain every listed member')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
    for part in parts:
        file = output / part['filename']
        part.update(sha256=digest(file), size=file.stat().st_size)
    index = {'schema': 1, 'version': version, 'original': {'filename': source.name, 'sha256': expected}, 'sources': parts}
    (output / 'source-index.json').write_text(json.dumps(index, indent=2) + '\n')
    return index


WINDOWS_RECIPE_SHA = '1f7402d01e0ebea9a82cb774b4a056a85575450e599901b4117b7b629d3a33f0'
WINDOWS_RECIPE_COMMIT = '9becc62004467fb3bc65a03d43eee2402f78be71'


def package_build_recipe(recipe, expected, nasm_license, license_sha, guide, index, output):
    """Retain build sources and notices without the optional prebuilt PATH helper."""
    if digest(recipe) != expected or digest(nasm_license) != license_sha:
        raise ValueError('Build recipe or NASM license checksum mismatch')
    filename = f'imagemagick-windows-build-{WINDOWS_RECIPE_COMMIT}-source.tar.gz'
    prefix = f'ImageMagick-Windows-{WINDOWS_RECIPE_COMMIT}'
    excluded = f'{prefix}/Installer/Inno/PathTool/PathTool.exe'
    destination = output / filename
    additions = {
        f'{prefix}/Build/nasm-LICENSE.txt': nasm_license.read_bytes(),
        f'{prefix}/REBUILD.md': guide.read_bytes(),
        f'{prefix}/SOURCE-INDEX.json': (json.dumps(index, indent=2) + '\n').encode(),
    }
    removed = []
    with recipe.open('rb') as source, destination.open('xb') as target:
        with tarfile.open(fileobj=source, mode='r|gz') as incoming, gzip.GzipFile(fileobj=target, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as archive:
                for member in incoming:
                    path = PurePosixPath(member.name)
                    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != prefix:
                        raise ValueError('Unexpected Windows build recipe path')
                    if member.name == excluded:
                        removed.append(member.name)
                        continue
                    if member.name in additions:
                        raise ValueError('Build recipe already contains an added notice')
                    member.uid = member.gid = member.mtime = 0
                    member.uname = member.gname = ''
                    member.pax_headers = {}
                    archive.addfile(member, incoming.extractfile(member) if member.isfile() else None)
                if removed != [excluded]:
                    raise ValueError('Expected optional prebuilt PATH helper was not found')
                import io
                for name, data in additions.items():
                    member = tarfile.TarInfo(name)
                    member.mode = 0o644
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
    return {'component': 'windows-build-guide', 'filename': filename, 'sha256': digest(destination),
            'size': destination.stat().st_size, 'upstreamSha256': expected, 'excludedPrebuiltFiles': removed}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('sha256')
    parser.add_argument('version')
    parser.add_argument('output', type=Path)
    parser.add_argument('--windows-recipes', type=Path)
    parser.add_argument('--nasm-license', type=Path)
    parser.add_argument('--nasm-license-sha256')
    args = parser.parse_args()
    if (args.windows_recipes or args.nasm_license or args.nasm_license_sha256) and not (args.windows_recipes and args.nasm_license and args.nasm_license_sha256 and args.version == '7.1.1-47'):
        parser.error('Windows recipes require version 7.1.1-47 and a checksum-pinned NASM license')
    index = split(args.archive.resolve(), args.sha256, args.version, args.output.resolve())
    if args.windows_recipes:
        part = package_build_recipe(args.windows_recipes, WINDOWS_RECIPE_SHA, args.nasm_license,
            args.nasm_license_sha256, Path(__file__).resolve().parent.parent / 'imagemagick/WINDOWS.md', index, args.output)
        index['sources'].append(part)
        (args.output / 'source-index.json').write_text(json.dumps(index, indent=2) + '\n')
    print(f'Created {len(index["sources"])} matching source archives.')
