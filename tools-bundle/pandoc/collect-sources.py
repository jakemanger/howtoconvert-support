#!/usr/bin/env python3
"""Download the exact Pandoc build sources recorded in sources.lock.json."""
import argparse
import concurrent.futures
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile


def write_notices(destination, lock):
    notices = destination / 'licenses'
    notices.mkdir(exist_ok=True)
    written = set()
    pattern = re.compile(r'^(licen[cs]e|copying|copyright|notice)([._-].*|v?\d.*)?$', re.I)
    def copy_notices(archive):
        for member in archive:
            relative = Path(member.name)
            syntax_notice = relative.parts and relative.parts[0].startswith('skylighting-core-') and 'xml' in relative.parts
            if (member.isfile() and (pattern.fullmatch(relative.name) or syntax_notice)
                    and relative.suffix.lower() not in ('.hs', '.json', '.html')
                    and not relative.is_absolute() and '..' not in relative.parts):
                if member.size > 2_000_000:
                    raise ValueError(f'Unexpectedly large license file: {member.name}')
                output = notices / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(archive.extractfile(member).read())
                written.add(output)
            if relative.name in ('gmp-6.2.1-nodoc.tar.bz2', 'libffi-3.4.6.tar.gz'):
                with tarfile.open(fileobj=io.BytesIO(archive.extractfile(member).read())) as nested:
                    copy_notices(nested)
    for item in lock['archives'] + [p['archive'] for p in lock['packages']]:
        path = destination / item['filename']
        if not tarfile.is_tarfile(path):
            continue
        with tarfile.open(path) as archive:
            copy_notices(archive)
    for alias in lock.get('sharedNotices', []):
        original = notices / alias['from']
        target = notices / alias['to']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        written.add(target)
    missing = [p['name'] for p in lock['packages']
               if not any((notices / (p['name'] + '-' + p['version'])).rglob('*'))]
    if missing:
        raise ValueError('Missing package license notices: ' + ', '.join(missing))
    for name in ('GPL-3.0.txt', 'GFDL-1.3.txt'):
        target = notices / name
        shutil.copyfile(Path(__file__).resolve().parent / name, target)
        written.add(target)
    rows = ['# Pandoc third-party notices', '',
            'The accompanying source archives preserve each project’s copyright and license notices.', '',
            '| Package | Version | License declared by package |', '| --- | --- | --- |']
    for package in sorted(lock['packages'] + lock['compilerPackages'] + lock['projectPackages'], key=lambda p: p['name']):
        rows.append(f'| {package["name"]} | {package["version"]} | {package["license"]} |')
    (notices / 'README.md').write_text('\n'.join(rows) + '\n', encoding='utf-8')
    written.add(notices / 'README.md')
    return written


def package_sources(destination, output, files):
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.partial')
    with temporary.open('wb') as raw:
        with gzip.GzipFile(fileobj=raw, filename='', mode='wb', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for path in sorted(set(files)):
                    if path.is_symlink() or not path.is_file():
                        raise ValueError(f'Expected a regular source file: {path}')
                    name = 'pandoc-3.6.2-sources/' + str(path.relative_to(destination))
                    info = archive.gettarinfo(str(path), name)
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ''
                    info.mode = 0o644
                    with path.open('rb') as stream:
                        archive.addfile(info, stream)
    temporary.replace(output)


def verify(path, item):
    if not path.is_file() or path.stat().st_size != item['size']:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == item['sha256']


def fetch(item, destination):
    name = item['filename']
    if Path(name).name != name:
        raise ValueError('Source filenames must not contain directories')
    path = destination / name
    if verify(path, item):
        return path
    temporary = path.with_name(name + '.partial')
    temporary.unlink(missing_ok=True)
    try:
        subprocess.run(['curl', '--fail', '--location', '--retry', '3', '--retry-all-errors',
                        '--connect-timeout', '30', '--max-time', '300', '--silent',
                        '--show-error', '--output', str(temporary), item['url']], check=True)
        if not verify(temporary, item):
            raise ValueError(f'Checksum or size mismatch: {name}')
        temporary.replace(path)
        return path
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--archive', type=Path, help='Also package the verified sources and build instructions')
    args = parser.parse_args()
    if args.verify_only and args.archive:
        parser.error('--verify-only cannot create an archive')
    recipe = Path(__file__).resolve().parent
    lock = json.loads((recipe / 'sources.lock.json').read_text(encoding='utf-8'))
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    items = lock['archives'] + [a for p in lock['packages'] for a in (p['archive'], p['cabal'])]
    if args.verify_only:
        failures = [item['filename'] for item in items if not verify(destination / item['filename'], item)]
        if failures:
            raise SystemExit('Missing or changed sources: ' + ', '.join(failures))
        print(f'Verified {len(items)} source files for Pandoc {lock["version"]}.')
        return
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda item: fetch(item, destination), items))
    files = {destination / item['filename'] for item in items}
    files.update(write_notices(destination, lock))
    for name in ('sources.lock.json', 'stack.yaml', 'stack.yaml.lock', 'build.py', 'collect-sources.py', 'README.md', 'LICENSE', 'NOTICE.md', 'GPL-3.0.txt', 'GFDL-1.3.txt', 'test_conversions.py', 'test_recipe.py', 'windows.py', 'windows.lock.json', 'WINDOWS.md', 'WINDOWS-NOTICE.md', 'test_windows.py'):
        path = recipe / name
        if path.exists() and path.resolve() != (destination / name).resolve():
            shutil.copyfile(path, destination / name)
        if path.exists():
            files.add(destination / name)
    if args.archive:
        package_sources(destination, args.archive, files)
    print(f'Verified {len(items)} source files for Pandoc {lock["version"]}.')


if __name__ == '__main__':
    main()
