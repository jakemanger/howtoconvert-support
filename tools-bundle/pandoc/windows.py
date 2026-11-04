#!/usr/bin/env python3
"""Collect matching sources or build the pinned Windows x64 Pandoc executable."""
import argparse
import concurrent.futures
import hashlib
import gzip
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tarfile

RECIPE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('pandoc_build', RECIPE / 'build.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
RECIPE_FILES = ('windows.py', 'windows.lock.json', 'WINDOWS.md', 'test_windows.py',
                'sources.lock.json', 'stack.yaml', 'stack.yaml.lock', 'build.py',
                'collect-sources.py', 'test_conversions.py', 'test_recipe.py',
                'README.md', 'LICENSE', 'NOTICE.md', 'WINDOWS-NOTICE.md', 'GPL-3.0.txt', 'GFDL-1.3.txt',
                'windows-dependencies.fixture.json')
SYSTEM_DLLS = set('advapi32.dll bcrypt.dll crypt32.dll dnsapi.dll gdi32.dll '
                  'iphlpapi.dll kernel32.dll msvcrt.dll ntdll.dll ole32.dll oleaut32.dll '
                  'rpcrt4.dll secur32.dll shell32.dll shlwapi.dll user32.dll userenv.dll '
                  'version.dll winmm.dll ws2_32.dll ucrtbase.dll dbghelp.dll'.split())


def load_lock():
    windows = json.loads((RECIPE / 'windows.lock.json').read_text())
    common_path = RECIPE / windows['baseLock']['filename']
    if hashlib.sha256(common_path.read_bytes()).hexdigest() != windows['baseLock']['sha256']:
        raise ValueError('Common source lock changed; review the Windows source mapping')
    common = json.loads(common_path.read_text())
    common['packages'] += windows['packages']
    common['compilerPackages'] += windows['compilerPackages']
    return common, windows


def source_items(common, windows):
    return (common['archives'] + [item for package in common['packages']
                                 for item in (package['archive'], package['cabal'])]
            + windows['runtimeSources'])


def collect_sources(destination, cache=None):
    common, windows = load_lock()
    destination.mkdir(parents=True, exist_ok=True)
    items = source_items(common, windows)
    def fetch(item):
        target = destination / item['filename']
        if not base.sources.verify(target, item) and cache and base.sources.verify(cache / item['filename'], item):
            shutil.copyfile(cache / item['filename'], target)
        return base.sources.fetch(item, destination)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(fetch, items))
    # These are the source packages selected by this exact GHC source release.
    with tarfile.open(destination / ('ghc-' + common['compiler'] + '-src.tar.xz')) as archive:
        recipe = archive.extractfile('ghc-' + common['compiler'] + '/mk/get-win32-tarballs.py').read().decode()
        if "TARBALL_VERSION = '" + windows['runtimeSourceVersion'] + "'" not in recipe:
            raise ValueError('Windows C runtime sources do not match the GHC build recipe')
    base.sources.write_notices(destination, common)
    for name in RECIPE_FILES:
        shutil.copyfile(RECIPE / name, destination / name)
    (destination / 'source-files.json').write_text(json.dumps(items, indent=2) + '\n')
    print(f'Verified {len(items)} source files; native build still verifies the selected dependency graph.')
    return destination


def prepare_source_cache(work):
    _, windows = load_lock()
    item = windows['sourceCache']
    destination = work / 'reviewed-source-cache'
    destination.mkdir(parents=True, exist_ok=True)
    archive = base.sources.fetch(item, destination)
    # fetch checks the whole archive before extract applies tarfile's data filter.
    root = base.extract(archive, destination / 'unpacked')
    if root.name != item['root']:
        raise ValueError('Unexpected reviewed source-cache root')
    print(f'Prepared verified source cache: {root}')
    return root


def project_source_filter(member, destination):
    # Python 3.12 leaves POSIX link separators broken on Windows (CPython #57911).
    # Keep the data-filter checks, then use native separators for the link itself.
    filtered = tarfile.data_filter(member, destination)
    if filtered is not None and filtered.issym():
        filtered = filtered.replace(linkname=filtered.linkname.replace('/', os.sep))
    return filtered


def extract_project(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        roots = {Path(member.name).parts[0] for member in members
                 if member.name and member.name != '.'}
        if len(roots) != 1:
            raise ValueError('Expected one Pandoc source archive root')
        source.extractall(destination, filter=project_source_filter)
        # Fail before compiling if a linked notice/data fixture is unreadable or
        # differs from its original archive target; no source bytes are changed.
        for member in members:
            if member.issym() or member.islnk():
                path = destination / member.name
                if not path.is_file():
                    raise ValueError('Unreadable Pandoc source link: ' + member.name)
                with source.extractfile(member) as expected:
                    if path.read_bytes() != expected.read():
                        raise ValueError('Pandoc source link content changed: ' + member.name)
    return destination / roots.pop()


def pe_imports(path):
    """Read regular and delayed PE imports without executing the binary."""
    data = Path(path).read_bytes()
    def unpack(fmt, offset):
        if offset < 0 or offset + struct.calcsize(fmt) > len(data):
            raise ValueError('Truncated PE file')
        return struct.unpack_from(fmt, data, offset)
    if data[:2] != b'MZ':
        raise ValueError('Expected a Windows PE executable')
    pe, = unpack('<I', 0x3c)
    if data[pe:pe + 4] != b'PE\0\0':
        raise ValueError('Invalid PE signature')
    machine, sections = unpack('<HH', pe + 4)
    optional_size, = unpack('<H', pe + 20)
    optional = pe + 24
    magic, = unpack('<H', optional)
    if machine != 0x8664 or magic != 0x20b or optional_size < 240:
        raise ValueError('Expected a Windows x64 PE32+ executable')
    section_table = optional + optional_size
    def offset(rva):
        for index in range(sections):
            entry = section_table + index * 40
            size, start, raw_size, raw = unpack('<IIII', entry + 8)
            if start <= rva < start + max(size, raw_size):
                delta = rva - start
                if delta >= raw_size or raw + delta >= len(data):
                    raise ValueError('PE import points outside file data')
                return raw + delta
        raise ValueError('PE import points outside a section')
    def name(rva):
        start = offset(rva)
        end = data.find(b'\0', start, start + 256)
        if end < 0:
            raise ValueError('Invalid DLL import name')
        value = data[start:end].decode('ascii').lower()
        if '/' in value or '\\' in value or not value.endswith('.dll'):
            raise ValueError('Invalid DLL import name')
        return value
    imports = set()
    for directory, stride, name_index in [(1, 20, 3), (13, 32, 1)]:
        rva, size = unpack('<II', optional + 112 + directory * 8)
        if not rva:
            continue
        start = offset(rva)
        for index in range(min(size // stride, 4096)):
            fields = unpack('<' + 'I' * (stride // 4), start + index * stride)
            if not any(fields):
                break
            if directory == 13 and fields[0] != 1:
                raise ValueError('Unsupported absolute-address delay import')
            imports.add(name(fields[name_index]))
        else:
            raise ValueError('Unterminated PE import directory')
    return sorted(imports)


def verify_windows_runtime(path):
    imports = pe_imports(path)
    unexpected = [name for name in imports if name not in SYSTEM_DLLS
                  and not name.startswith(('api-ms-win-', 'ext-ms-win-'))]
    if unexpected:
        raise ValueError('Executable requires non-system DLLs: ' + ', '.join(unexpected))
    return imports


def package_output(output, destination):
    allowed = ['pandoc.exe', 'COPYING.md', 'NOTICE.md', 'GPL-3.0.txt', 'GFDL-1.3.txt']
    notices = sorted((output / 'licenses').rglob('*'))
    if not any(path.is_file() for path in notices):
        raise ValueError('Runtime license notices are missing')
    files = [output / name for name in allowed] + notices
    temporary = destination.with_name(destination.name + '.partial')
    try:
        with temporary.open('wb') as stream:
            with gzip.GzipFile(fileobj=stream, filename='', mode='wb', mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode='w') as archive:
                    for path in files:
                        if path.is_symlink():
                            raise ValueError('Unexpected link in release files: ' + str(path))
                        if path.is_dir():
                            continue
                        if not path.is_file():
                            raise ValueError('Missing release file: ' + str(path))
                        relative = path.relative_to(output)
                        info = archive.gettarinfo(str(path), 'pandoc/' + relative.as_posix())
                        info.uid = info.gid = info.mtime = 0
                        info.uname = info.gname = ''
                        info.mode = 0o755 if relative.as_posix() == 'pandoc.exe' else 0o644
                        with path.open('rb') as body:
                            archive.addfile(info, body)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {'filename': destination.name, 'size': destination.stat().st_size,
            'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(), 'bin': 'pandoc/pandoc.exe'}


def setup_info(toolchain):
    def entry(item):
        return {'url': item['url'], 'content-length': item['size'], 'sha256': item['sha256']}
    return {'sevenzexe-info': entry(toolchain['sevenzexe']),
            'sevenzdll-info': entry(toolchain['sevenzdll']),
            'msys2': {'windows64': {'version': toolchain['msys2']['version'], **entry(toolchain['msys2'])}}}


def dependency_graph(args, env, report, expected_compiler):
    # Unlike `setup --no-install-ghc`, `path --compiler-exe` initializes MSYS2.
    # Its final line is the compiler path; any first-install chatter stays in the log.
    setup = subprocess.check_output([str(x) for x in args + ['path', '--compiler-exe']],
                                    env=env, text=True)
    print(setup, end='' if setup.endswith('\n') else '\n', flush=True)
    lines = setup.strip().splitlines()
    if not lines or not Path(lines[-1]).samefile(expected_compiler):
        raise ValueError('Stack did not select the pinned GHC executable')
    text = subprocess.check_output([str(x) for x in args + ['ls', 'dependencies', 'json', 'pandoc-cli']],
                                   env=env, text=True)
    report.write_text(text, encoding='utf-8')
    return json.loads(text)


def build(work, jobs, cache):
    if platform.system() != 'Windows' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise ValueError('Run the build on Windows x64; --collect-only works on other platforms')
    common, windows = load_lock()
    sources = collect_sources(work / 'source-companion', cache)
    downloads = work / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    toolchain = windows['toolchain']
    stack = base.extract(base.sources.fetch(toolchain['stack'], downloads), work / 'toolchain') / 'stack.exe'
    ghc = base.extract(base.sources.fetch(toolchain['ghc'], downloads), work / 'toolchain')
    project = extract_project(sources / common['archives'][0]['filename'], work / 'sources')
    for name in ('stack.yaml', 'stack.yaml.lock'):
        shutil.copyfile(RECIPE / name, project / name)
    setup = work / 'stack-setup.json'
    setup.write_text(json.dumps(setup_info(toolchain), indent=2) + '\n')
    env = os.environ.copy()
    env['STACK_ROOT'] = str(work / 'stack-root')
    env['PATH'] = str(ghc / 'bin') + os.pathsep + env['PATH']
    env.pop('STACK_YAML', None)
    # The pinned Windows archive has a separate ghc.exe launcher. Stack
    # reports the versioned compiler, so verify that exact extracted file.
    compiler = ghc / 'bin' / f"ghc-{common['compiler']}.exe"
    for command, expected in [([stack, '--numeric-version'], common['stackVersion']),
                              ([compiler, '--numeric-version'], common['compiler'])]:
        if subprocess.check_output(command, env=env, text=True).strip() != expected:
            raise ValueError('Unexpected compiler/build tool version')
    args = [stack, '--stack-yaml', project / 'stack.yaml', '--system-ghc', '--no-install-ghc',
            '--install-msys', '--setup-info-yaml', setup, '--lock-file', 'error-on-write', '--jobs', jobs]
    output = work / 'output'
    output.mkdir(exist_ok=True)
    graph = dependency_graph(args, env, output / 'dependencies.json', compiler)
    base.verify_coverage(common, graph)
    base.run(args + ['build', 'pandoc-cli:exe:pandoc', '--no-haddock', '--no-test', '--no-bench'], env=env)
    base.run(args + ['--local-bin-path', output, 'install', 'pandoc-cli:exe:pandoc'], env=env)
    executable = output / 'pandoc.exe'
    version = subprocess.check_output([executable, '--version'], text=True)
    if not version.startswith('pandoc ' + common['version'] + '\n'):
        raise ValueError('Unexpected Pandoc executable version')
    imports = verify_windows_runtime(executable)
    licenses = output / 'licenses'
    shutil.copytree(sources / 'licenses', licenses, dirs_exist_ok=True)
    runtime_notices = ghc / 'mingw/share/licenses'
    if not runtime_notices.is_dir():
        raise ValueError('GHC Windows runtime notices are missing')
    shutil.copytree(runtime_notices, licenses / 'windows-runtime', dirs_exist_ok=True)
    for name in ('GPL-3.0.txt', 'GFDL-1.3.txt'):
        shutil.copyfile(RECIPE / name, output / name)
    shutil.copyfile(RECIPE / 'WINDOWS-NOTICE.md', output / 'NOTICE.md')
    shutil.copyfile(project / 'COPYING.md', output / 'COPYING.md')
    # Keep dependency versions without exposing build-machine paths.
    for package in graph:
        location = package.get('location', {})
        if location and location.get('type') != 'hackage':
            package['location'] = {'type': 'project package'}
    (output / 'dependencies.json').write_text(json.dumps(graph, indent=2) + '\n')
    (output / 'linked-libraries.txt').write_text('\n'.join(imports) + '\n')
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    (output / 'SHA256SUMS').write_text(digest + '  pandoc.exe\n')
    base.run([sys.executable, RECIPE / 'test_conversions.py', '--pandoc', executable,
              '--output', work / 'conversion-tests'])
    artifact = package_output(output, work / ('pandoc-' + common['version'] + '-windows_x86_64.tar.gz'))
    (work / 'artifact.json').write_text(json.dumps(artifact, indent=2) + '\n')
    print('Built Windows x64 Pandoc with verified sources, notices and conversion checks.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--source-cache', type=Path, help='Reuse hash-verified source downloads')
    parser.add_argument('--collect-only', action='store_true')
    parser.add_argument('--prepare-source-cache', action='store_true', help='Fetch the pinned reusable source archive')
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    work = args.work.resolve()
    if args.prepare_source_cache:
        prepare_source_cache(work)
    elif args.collect_only:
        collect_sources(work / 'source-companion', args.source_cache)
    else:
        build(work, args.jobs, args.source_cache)


if __name__ == '__main__':
    main()
