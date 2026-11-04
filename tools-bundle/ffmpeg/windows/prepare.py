#!/usr/bin/env python3
"""Assemble the pinned standalone Windows FFmpeg conversion tools."""
import argparse, concurrent.futures, gzip, hashlib, io, json, os, re, shutil, struct, subprocess, tarfile, tempfile, urllib.request
from pathlib import Path, PurePosixPath
HERE = Path(__file__).resolve().parent
SYSTEM_DLLS = set('advapi32 bcrypt cfgmgr32 comctl32 comdlg32 crypt32 cryptbase d2d1 d3d11 dwrite dxgi gdi32 imm32 iphlpapi kernel32 kernelbase mpr msvcrt ncrypt netapi32 normaliz ntdll ole32 oleaut32 powrprof propsys psapi rpcrt4 secur32 setupapi shell32 shlwapi ucrtbase user32 userenv wsock32 usp10 uxtheme version windowscodecs winhttp wininet winmm winspool ws2_32 wtsapi32 wldap32'.split())

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def relative(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or not path.parts:
        raise ValueError(f'Unsafe archive path: {name}')
    return path

def fetch(item, cache):
    name = item['filename']
    if str(relative(name)) != name or '/' in name:
        raise ValueError('Expected a basename')
    path = cache / name
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        if not item['url'].startswith('https://'):
            raise ValueError('Downloads require HTTPS')
        with tempfile.TemporaryDirectory(dir=cache) as temporary:
            part = Path(temporary) / name
            with urllib.request.urlopen(item['url'], timeout=90) as response, part.open('xb') as output:
                shutil.copyfileobj(response, output)
            verify_file(part, item)
            part.rename(path)
    verify_file(path, item)
    return path

def fetch_all(items, cache):
    if len({item['filename'] for item in items}) != len(items):
        raise ValueError('Duplicate pinned filenames')
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        paths = list(pool.map(lambda item: fetch(item, cache), items))
    return {item['filename']: path for item, path in zip(items, paths)}

def verify_file(path, item):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != item['size'] or digest(path) != item['sha256']:
        raise ValueError(f'Checksum or size mismatch: {path.name}')

def verify_source_pairs(packages, sources):
    checked = {}
    for package in packages:
        source = sources[package['sourceFile']]
        if source not in checked:
            with tarfile.open(source, 'r:*') as archive:
                recipes = [item for item in archive if item.isfile() and item.name.endswith('/PKGBUILD') and item.name.count('/') == 1]
                if len(recipes) != 1:
                    raise ValueError('Expected one source package recipe')
                checked[source] = hashlib.sha256(archive.extractfile(recipes[0]).read()).hexdigest()
        if checked[source] != package['pkgbuildSha256']:
            raise ValueError(f"Source recipe does not match binary build: {package['name']}")

def pe_imports(data):
    if data[:2] != b'MZ':
        raise ValueError('Expected a Windows executable')
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if data[pe:pe+4] != b'PE\0\0':
        raise ValueError('Invalid Windows executable')
    count = struct.unpack_from('<H', data, pe + 6)[0]
    optional = pe + 24
    size = struct.unpack_from('<H', data, pe + 20)[0]
    magic = struct.unpack_from('<H', data, optional)[0]
    if magic not in (0x10b, 0x20b):
        raise ValueError('Unsupported PE header')
    directories = optional + (112 if magic == 0x20b else 96)
    image_base = struct.unpack_from('<Q' if magic == 0x20b else '<I', data, optional + (24 if magic == 0x20b else 28))[0]
    sections = []
    for index in range(count):
        start = optional + size + index * 40
        virtual_size, rva, raw_size, raw = struct.unpack_from('<IIII', data, start + 8)
        sections.append((rva, max(virtual_size, raw_size), raw))
    def offset(rva):
        for start, length, raw in sections:
            if start <= rva < start + length:
                result = raw + rva - start
                if result >= len(data):
                    raise ValueError('PE import points outside file')
                return result
        raise ValueError('PE import is outside sections')
    result = set()
    for directory, stride, name_offset in [(1, 20, 12), (13, 32, 4)]:
        if directories + directory * 8 + 8 > optional + size:
            continue
        table, length = struct.unpack_from('<II', data, directories + directory * 8)
        if not table:
            continue
        cursor = offset(table)
        for _ in range(max(1, length // stride)):
            block = data[cursor:cursor + stride]
            if len(block) != stride:
                raise ValueError('Truncated import descriptor')
            if not any(block):
                break
            name = struct.unpack_from('<I', block, name_offset)[0]
            if directory == 13 and not struct.unpack_from('<I', block)[0] & 1:
                name -= image_base
            start = offset(name)
            end = data.index(b'\0', start)
            result.add(data[start:end].decode('ascii').lower())
            cursor += stride
    return sorted(result)

def is_system_dll(name):
    return name == 'winspool.drv' or name.removesuffix('.dll') in SYSTEM_DLLS or name.startswith(('api-ms-win-', 'ext-ms-win-'))

def write_file(root, name, data):
    target = root / str(relative(name))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != data:
        raise ValueError(f'Conflicting package file: {name}')
    target.write_bytes(data)

SYSTEM_DLLS.update('avicap32 avrt bcryptprimitives dnsapi gdiplus msimg32 winspool'.split())
LICENSE_NAME = re.compile(r'^(?:copying|copyright|licen[cs]e|notice|artistic)[0-9]*(?:[._-].*)?$', re.I)


def resource(name):
    return name.startswith(('etc/fonts/', 'share/fontconfig/'))


def assemble(packages, paths, root):
    native, extras, notices = {}, [], []
    for package in packages:
        with tarfile.open(paths[package['filename']], 'r:*') as archive:
            for member in archive:
                if not member.isfile() or not member.name.startswith('ucrt64/'):
                    continue
                name = member.name.removeprefix('ucrt64/')
                relative(name)
                if name.startswith('bin/') and name.endswith(('.dll', '.exe')):
                    data = archive.extractfile(member).read(); key = Path(name).name.lower()
                    if key in native and native[key][1] != data:
                        raise ValueError('Conflicting native library: ' + key)
                    native[key] = (name, data, package['name'])
                elif resource(name):
                    extras.append((name, archive.extractfile(member).read(), package['name']))
                elif name.startswith('share/licenses/'):
                    notices.append((name, archive.extractfile(member).read(), package['name']))
    owners, copied, external = set(), set(), set()
    pending = ['ffmpeg.exe', 'ffprobe.exe']
    while pending:
        name = pending.pop().lower()
        if name in copied:
            continue
        if name not in native:
            if is_system_dll(name):
                external.add(name); continue
            raise ValueError('Missing required Windows library: ' + name)
        path, data, owner = native[name]
        write_file(root, path, data); owners.add(owner); copied.add(name)
        pending.extend(pe_imports(data))
    for name, data, owner in extras:
        if owner in owners:
            write_file(root, name, data)
    for name, data, owner in notices:
        if owner in owners:
            write_file(root, name, data)
    return {'programsAndLibraries': sorted(copied), 'systemLibraries': sorted(external), 'packages': sorted(owners), 'packageNotices': sorted({owner for _, _, owner in notices if owner in owners})}


def save_notice(root, component, name, data, evidence):
    if not data or len(data) > 4 * 1024 * 1024:
        return
    suffix = hashlib.sha256(name.encode()).hexdigest()[:12] + '-' + Path(name).name
    target = 'licenses/' + component + '/' + suffix
    write_file(root, target, data)
    evidence.append({'component': component, 'sourcePath': name, 'file': target, 'sha256': hashlib.sha256(data).hexdigest()})


def source_notices(source, component, root, evidence):
    with tarfile.open(source, 'r:*') as archive:
        for member in archive:
            if not member.isfile():
                continue
            if LICENSE_NAME.fullmatch(Path(member.name).name):
                save_notice(root, component, member.name, archive.extractfile(member).read(), evidence)
            elif member.name.endswith(('.tar.gz', '.tar.xz', '.tar.bz2', '.tar.zst')):
                with tarfile.open(fileobj=archive.extractfile(member), mode='r|*') as nested:
                    for item in nested:
                        if item.isfile() and LICENSE_NAME.fullmatch(Path(item.name).name):
                            save_notice(root, component, member.name + '/' + item.name, nested.extractfile(item).read(), evidence)


def rust_notices(crates, paths, root, evidence):
    for item in crates:
        source = paths[item['filename']]
        with tarfile.open(source, 'r:*') as archive:
            for member in archive:
                if member.isfile() and LICENSE_NAME.fullmatch(Path(member.name).name):
                    save_notice(root, 'rust-' + item['name'] + '-' + item['version'], member.name,
                                archive.extractfile(member).read(), evidence)


def copy_sources(items, paths, destination):
    for item in items:
        source = paths[item['filename']]
        target = destination / item['filename']
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)


def pack(root, target):
    with target.open('wb') as output, gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w') as archive:
        for file in sorted(root.rglob('*')):
            info = archive.gettarinfo(file, arcname=file.relative_to(root.parent).as_posix())
            info.uid = info.gid = info.mtime = 0; info.uname = info.gname = ''; info.pax_headers = {}
            info.mode = 0o755 if file.is_dir() or file.suffix.lower() in ('.exe', '.dll') else 0o644
            with file.open('rb') if file.is_file() else tempfile.TemporaryFile() as stream:
                archive.addfile(info, stream if file.is_file() else None)


def prepare_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--assemble-only', action='store_true', help='Prepare locally; native Windows checks remain required')
    args = parser.parse_args()
    work = args.work.resolve(); work.mkdir(parents=True, exist_ok=True)
    cache = args.cache.resolve() if args.cache else work / 'downloads'; cache.mkdir(parents=True, exist_ok=True)
    lock = json.loads((HERE / 'runtime.lock.json').read_text())
    root = work / 'stage/ffmpeg'
    if root.exists():
        raise ValueError('Choose a fresh work directory; existing staging output is not reused')
    root.mkdir(parents=True)
    packages = lock['packages']; paths = fetch_all(packages, cache)
    closure = assemble(packages, paths, root)
    if set(closure['packages']) != {p['name'] for p in packages}:
        raise ValueError('Runtime lock contains unused or missing packages')
    sources = fetch_all(lock['sources'], cache); verify_source_pairs(packages, sources)
    rust = fetch_all(lock['rustCrates'], cache)
    std = fetch_all(lock['rustStandardLibrarySources'], cache)
    evidence = []
    for package in packages:
        component = package['name'].removeprefix('mingw-w64-ucrt-x86_64-')
        source_notices(sources[package['sourceFile']], component, root, evidence)
    rust_notices(lock['rustCrates'], rust, root, evidence)
    for item in lock['rustStandardLibrarySources']:
        source_notices(std[item['filename']], item['filename'].removesuffix('.tar.xz'), root, evidence)
    # A few upstream source packages preserve a bare Git repository.
    for item in lock['gitNotices']:
        with tempfile.TemporaryDirectory(dir=work) as directory:
            temporary = Path(directory)
            with tarfile.open(sources[item['sourceFile']], 'r:*') as archive:
                archive.extractall(temporary, filter='data')
            gitdir = temporary / str(relative(item['repositoryPath']))
            for name in item['files']:
                data = subprocess.check_output(['git', '--git-dir', str(gitdir), 'show', item['commit'] + ':' + name])
                save_notice(root, item['component'], item['commit'] + '/' + name, data, evidence)
    for package in packages:
        component = package['name'].removeprefix('mingw-w64-ucrt-x86_64-')
        if not any(row['component'] == component for row in evidence) and package['name'] not in closure['packageNotices']:
            raise ValueError('No notices collected for ' + component)
    (root / 'licenses/index.json').write_text(json.dumps(evidence, indent=2) + '\n')
    (root / 'SOURCE.txt').write_text('FFmpeg 8.1.1 and its libraries are assembled from pinned MSYS2 packages.\nMatching source archives, recipes and licenses accompany this release.\n')
    companion = work / 'source-companion'; companion.mkdir()
    copy_sources(lock['sources'], sources, companion)
    copy_sources(lock['rustCrates'], rust, companion)
    copy_sources(lock['rustStandardLibrarySources'], std, companion)
    for name in ['prepare.py', 'verify.py', 'runtime.lock.json', 'README.md']:
        shutil.copyfile(HERE / name, companion / name)
    shutil.copytree(HERE / 'cargo-locks', companion / 'cargo-locks')
    build = companion / 'build-info'; build.mkdir()
    for package in packages:
        with tarfile.open(paths[package['filename']], 'r:*') as archive:
            for name in ['.PKGINFO', '.BUILDINFO']:
                (build / (package['name'] + name)).write_bytes(archive.extractfile(name).read())
    report = {'nativeVerified': False, 'closure': closure, 'notices': len(evidence)}
    if not args.assemble_only:
        if os.name != 'nt':
            raise RuntimeError('Use Windows for native verification, or --assemble-only for local preparation')
        subprocess.run([os.sys.executable, str(HERE / 'verify.py'), '--ffmpeg', str(root / 'bin/ffmpeg.exe'), '--ffprobe', str(root / 'bin/ffprobe.exe'), '--report', str(work / 'native-verification.json')], check=True)
        relocated = work / 'relocated with spaces/ffmpeg'; relocated.parent.mkdir()
        shutil.move(root, relocated)
        subprocess.run([os.sys.executable, str(HERE / 'verify.py'), '--ffmpeg', str(relocated / 'bin/ffmpeg.exe'), '--ffprobe', str(relocated / 'bin/ffprobe.exe'), '--report', str(work / 'relocated-verification.json')], check=True)
        shutil.move(relocated, root); report['nativeVerified'] = True
    artifact = work / 'ffmpeg-8.1.1-windows_x86_64.tar.gz'; pack(root, artifact)
    artifact_record = {'filename': artifact.name, 'sha256': digest(artifact), 'size': artifact.stat().st_size, 'bin': 'ffmpeg/bin/ffmpeg.exe'}
    (work / 'artifact.json').write_text(json.dumps(artifact_record, indent=2) + '\n')
    verification = {'status': 'passed' if report['nativeVerified'] else 'prepared', 'platform': 'windows', 'checks': [
        {'name': 'pinned-sources-and-library-closure', 'passed': True, 'packages': len(closure['packages'])},
        {'name': 'native-codecs', 'passed': report['nativeVerified']},
        {'name': 'relocated-native-codecs', 'passed': report['nativeVerified']}]}
    (work / 'verification.json').write_text(json.dumps(verification, indent=2) + '\n')


def main():
    try:
        prepare_main()
    except Exception as error:
        if '--work' in os.sys.argv:
            index = os.sys.argv.index('--work') + 1
            if index < len(os.sys.argv):
                directory = Path(os.sys.argv[index]); directory.mkdir(parents=True, exist_ok=True)
                (directory / 'verification.json').write_text(json.dumps({'status': 'failed', 'platform': 'windows', 'checks': [], 'error': str(error)}, indent=2) + '\n')
        raise


if __name__ == '__main__':
    main()
