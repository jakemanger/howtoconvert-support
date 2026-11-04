#!/usr/bin/env python3
"""Prepare and verify the pinned Windows TeX conversion runtime."""
import argparse
import concurrent.futures
import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import struct
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
ENGINES = ('pdftex', 'pdflatex', 'latex', 'etex', 'tex', 'xetex', 'xelatex',
           'luatex', 'luahbtex', 'lualatex', 'dvilualatex', 'dviluatex', 'mf',
           'mf-nowin', 'inimf', 'initex', 'bibtex', 'kpsewhich', 'kpseaccess',
           'kpsereadlink', 'kpsestat', 'mktexfmt', 'mktexupd', 'dvipdfmx',
           'dvipdfm', 'xdvipdfmx', 'dvips', 'gftodvi', 'gftopk', 'gftype',
           'pktogf', 'pktype', 'mft', 'afm2tfm', 'makeindex', 'texlua',
           'runscript', 'teckit_compile', 'fc-cache', 'fc-list',
           'fc-match', 'perl', 'gswin64c')
WRAPPERS = ('fmtutil', 'fmtutil-sys', 'fmtutil-user', 'updmap', 'updmap-sys',
            'updmap-user', 'mktexlsr', 'texhash', 'epstopdf', 'repstopdf',
            'rungs', 'latexmk', 'luaotfload-tool')
LICENSE_NAME = re.compile(r'^(?:copying|copyright|licen[cs]e|notice|artistic)(?:[._-].*)?$', re.I)
SYSTEM_DLLS = set('advapi32 bcrypt cfgmgr32 comctl32 comdlg32 crypt32 cryptbase d2d1 d3d11 dwrite dxgi gdi32 imm32 iphlpapi kernel32 kernelbase mpr msvcrt ncrypt netapi32 normaliz ntdll ole32 oleaut32 powrprof propsys psapi rpcrt4 secur32 setupapi shell32 shlwapi ucrtbase user32 userenv wsock32 usp10 uxtheme version windowscodecs winhttp wininet winmm winspool ws2_32 wtsapi32 wldap32'.split())


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def relative(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or not path.parts:
        raise ValueError(f'Unsafe archive path: {name}')
    return path


def input_cache_base(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Input cache requires an HTTPS base URL without credentials, query or fragment')
    return value.rstrip('/') + '/'


def transient_download_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 429) or 500 <= error.code <= 599
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    return isinstance(error, (TimeoutError, ConnectionError, http.client.IncompleteRead, ssl.SSLEOFError))


def fetch(item, cache, input_cache_url=None):
    name = item['filename']
    if str(relative(name)) != name or '/' in name:
        raise ValueError('Expected a basename')
    path = cache / name
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        url = input_cache_base(input_cache_url) + item['sha256'] if input_cache_url else item['url']
        if not url.startswith('https://'):
            raise ValueError('Downloads require HTTPS')
        for attempt in range(3):
            with tempfile.TemporaryDirectory(dir=cache) as temporary:
                part = Path(temporary) / name
                request = urllib.request.Request(url, headers={'User-Agent': 'HowToConvert-Tools/1.0'})
                try:
                    with urllib.request.urlopen(request, timeout=90) as response, part.open('xb') as output:
                        shutil.copyfileobj(response, output)
                except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead, ssl.SSLEOFError) as error:
                    if isinstance(error, urllib.error.HTTPError):
                        error.close()
                    if not transient_download_error(error) or attempt == 2:
                        raise
                else:
                    # Integrity and local file errors never enter the retry path.
                    verify_file(part, item)
                    part.rename(path)
                    break
            time.sleep(attempt + 1)
    verify_file(path, item)
    return path


def fetch_all(items, cache, input_cache_url=None):
    if len({item['filename'] for item in items}) != len(items):
        raise ValueError('Duplicate pinned filenames')
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        paths = list(pool.map(lambda item: fetch(item, cache, input_cache_url), items))
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


def resource(name):
    return name == 'etc/paperspecs' or name.startswith(('lib/perl5/', 'share/perl5/', 'share/ghostscript/',
                            'share/fonts/', 'share/fontconfig/', 'etc/fonts/'))


def write_file(root, name, data):
    target = root / str(relative(name))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != data:
        raise ValueError(f'Conflicting package file: {name}')
    target.write_bytes(data)


def assemble_native(packages, paths, root):
    native, extra, notices, owners = {}, [], [], set()
    for package in packages:
        with tarfile.open(paths[package['filename']], 'r:*') as archive:
            for member in archive:
                if not member.name.startswith('ucrt64/') or not member.isfile():
                    continue
                name = member.name.removeprefix('ucrt64/')
                relative(name)
                if name.startswith('bin/') and name.endswith(('.exe', '.dll')):
                    data = archive.extractfile(member).read()
                    key = Path(name).name.lower()
                    row = (name, data, package['name'])
                    if key in native and native[key][1] != data:
                        raise ValueError(f'Conflicting native library: {key}')
                    native[key] = row
                elif resource(name) or name == 'bin/runscript.tlu':
                    extra.append((name, archive.extractfile(member).read(), package['name']))
                elif name.startswith('share/licenses/'):
                    notices.append((name, archive.extractfile(member).read(), package['name']))
    pending = [name + '.exe' for name in ENGINES]
    for name, data, owner in extra:
        write_file(root, name, data)
        owners.add(owner)
        if name.endswith(('.dll', '.exe')):
            pending.extend(pe_imports(data))
    copied, external, missing = set(), set(), set()
    while pending:
        name = pending.pop().lower()
        if name in copied:
            continue
        if name not in native:
            if is_system_dll(name):
                external.add(name)
                continue
            missing.add(name)
            continue
        path, data, owner = native[name]
        copied.add(name)
        owners.add(owner)
        write_file(root, path, data)
        pending.extend(pe_imports(data))
    if missing:
        raise ValueError('Missing required Windows programs/libraries: ' + ', '.join(sorted(missing)))
    for name, data, owner in notices:
        if owner in owners:
            write_file(root, name, data)
    for name in WRAPPERS:
        shutil.copyfile(root / 'bin/runscript.exe', root / ('bin/' + name + '.exe'))
    return {'programsAndLibraries': sorted(copied), 'systemLibraries': sorted(external), 'packages': sorted(owners),
            'packageNotices': sorted({owner for _, _, owner in notices if owner in owners})}


def source_notices(source, component, root, additional=()):
    evidence = []
    expected = {item['member']: item['sha256'] for item in additional}
    found = set()
    def save(name, data):
        relative(name)
        if name in expected:
            if hashlib.sha256(data).hexdigest() != expected[name]:
                raise ValueError('Changed license declaration: ' + name)
            found.add(name)
        if not data or len(data) > 4 * 1024 * 1024:
            return
        target = 'share/licenses/source/' + component + '/' + hashlib.sha256(name.encode()).hexdigest()[:12] + '-' + Path(name).name
        write_file(root, target, data)
        evidence.append({'component': component, 'sourceArchive': source.name, 'sourcePath': name,
                         'file': target, 'sha256': hashlib.sha256(data).hexdigest()})
    with tarfile.open(source, 'r:*') as archive:
        for member in archive:
            if not member.isfile():
                continue
            if LICENSE_NAME.fullmatch(Path(member.name).name) or member.name in expected:
                save(member.name, archive.extractfile(member).read())
            elif member.name.endswith(('.tar.gz', '.tar.xz', '.tar.bz2', '.tar.zst', '.tgz')):
                with tarfile.open(fileobj=archive.extractfile(member), mode='r|*') as nested:
                    for item in nested:
                        if item.isfile() and (LICENSE_NAME.fullmatch(Path(item.name).name) or member.name + '/' + item.name in expected):
                            save(member.name + '/' + item.name, nested.extractfile(item).read())
    if found != expected.keys():
        raise ValueError('Missing pinned license declarations: ' + ', '.join(sorted(expected.keys() - found)))
    return evidence


def collect_notices(packages, sources, root, closure):
    evidence = []
    for package in packages:
        component = package['name'].removeprefix('mingw-w64-ucrt-x86_64-')
        notices = source_notices(sources[package['sourceFile']], component, root, package.get('additionalNotices', ()))
        if not notices and package['name'] not in closure['packageNotices']:
            raise ValueError('No license notices collected for ' + component)
        evidence.extend(notices)
    for package in packages:
        provider = package.get('licenseTextsFrom')
        if not provider:
            continue
        if provider != 'perl':
            raise ValueError('Unreviewed shared license texts: ' + provider)
        shared = [row for row in evidence if row['component'] == provider and Path(row['sourcePath']).name in ('Artistic', 'Copying')]
        if len(shared) != 2:
            raise ValueError('Both original Perl license texts are required')
        component = package['name'].removeprefix('mingw-w64-ucrt-x86_64-')
        for row in shared:
            target = 'share/licenses/source/' + component + '/Perl-' + Path(row['sourcePath']).name
            write_file(root, target, (root / row['file']).read_bytes())
            evidence.append({**row, 'component': component, 'file': target})
    (root / 'share/licenses/source-index.json').write_text(json.dumps(evidence, indent=2) + '\n')
    return evidence


def copy_tex_data(tiny, root):
    for name in ('texmf-dist', 'texmf-local', 'texmf-config', 'texmf-var'):
        source = tiny / name
        if source.is_dir():
            shutil.copytree(source, root / name, dirs_exist_ok=True)
    for path in (root / 'texmf-var').rglob('*'):
        if path.is_file() and path.suffix in ('.fmt', '.log', '.fls'):
            path.unlink()
    for name in ('LICENSE.CTAN', 'LICENSE.TL', 'release-texlive.txt'):
        shutil.copyfile(tiny / name, root / name)
    shutil.copytree(tiny / 'tlpkg/TeXLive', root / 'tlpkg/TeXLive')
    if any(path.suffix.lower() in ('.exe', '.dll') for path in root.rglob('*') if path.is_file()):
        raise ValueError('The TeX package tree unexpectedly contains native binaries')


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f'Upstream wrapper changed: {old}')
    return text.replace(old, new)


def configure(root):
    cnf = (root / 'texmf-dist/web2c/texmf.cnf').read_text()
    values = {'TEXMFROOT': '$SELFAUTODIR', 'TEXMFLOCAL': '$TEXMFROOT/texmf-local',
              'TEXMFHOME': '$TEXMFLOCAL', 'TEXMFVAR': '$TEXMFSYSVAR', 'TEXMFCONFIG': '$TEXMFSYSCONFIG'}
    for key, value in values.items():
        cnf, count = re.subn(r'^' + key + r'\s*=.*$', key + ' = ' + value, cnf, count=1, flags=re.M)
        if count != 1:
            raise ValueError(f'Missing TeX config variable: {key}')
    (root / 'bin/texmf.cnf').write_text(cnf + '\nmax_print_line = 10000\n')
    fontconf = root / 'etc/fonts/fonts.conf'
    text = fontconf.read_text()
    text = replace_once(text, '</fontconfig>', '<!-- Modified 2026-11-04: include portable TeX fonts. -->\n<include ignore_missing="yes">local.conf</include>\n</fontconfig>')
    fontconf.write_text(text)
    (root / 'etc/fonts/local.conf').write_text('<fontconfig><dir prefix="relative">../../texmf-dist/fonts/opentype</dir><dir prefix="relative">../../texmf-dist/fonts/truetype</dir></fontconfig>\n')
    script = root / 'bin/runscript.tlu'
    text = script.read_text()
    for old, new in [
        ("TEXDIR = kpse.var_value('SELFAUTOPARENT')", "TEXDIR = kpse.var_value('TEXMFROOT')"),
        ("PERLEXE = TEXDIR..'/tlpkg/tlperl/bin/perl.exe'", "PERLEXE = TEXDIR..'/bin/perl.exe'"),
        ("os.setenv('PERL5LIB', TEXDIR..'/tlpkg/tlperl/lib')", "os.setenv('PERL5LIB', nil)"),
        ("PATH = prepend_path(PATH, TEXDIR..'/tlpkg/tlperl/bin')", "PATH = prepend_path(PATH, TEXDIR..'/bin')"),
    ]:
        text = replace_once(text, old, new)
    text = text.replace("TEXDIR..'/tlpkg/tlgs/bin/gsdll32.dll'", "TEXDIR..'/bin/libgs-10.dll'")
    text = text.replace("TEXDIR..'/tlpkg/tlgs/bin/gswin32c.exe'", "TEXDIR..'/bin/gswin64c.exe'")
    text = text.replace("GSNAME = 'gswin32c.exe'", "GSNAME = 'gswin64c.exe'")
    text = text.replace("GSDIR = TEXDIR..'/tlpkg/tlgs/bin'", "GSDIR = TEXDIR..'/bin'")
    gs = list((root / 'share/ghostscript').glob('*/lib'))
    if len(gs) != 1:
        raise ValueError('Expected one Ghostscript resource tree')
    gsbase = gs[0].parent.relative_to(root).as_posix()
    start = text.index("   os.setenv('GS_LIB', TEXDIR..'/tlpkg/tlgs/lib;'")
    end = text.index("  os.setenv('GS_DLL'", start)
    text = text[:start] + f"  os.setenv('GS_LIB', TEXDIR..'/{gsbase}/lib;'..TEXDIR..'/share/fonts')\n" + text[end:]
    script.write_text('-- Modified 2026-11-04: use portable bundle paths for TeX, Perl and Ghostscript.\n' + text)
    fmt = root / 'texmf-dist/scripts/texlive/fmtutil.pl'
    text = replace_once(fmt.read_text(), '$bindir = "$Master/bin/windows";', '$bindir = "$Master/bin";')
    text = replace_once(text, '$ENV{"PATH"} = "$bindir:$ENV{PATH}";', '$ENV{"PATH"} = "$bindir" . ($^O =~ /^MSWin/i ? ";" : ":") . $ENV{PATH};')
    fmt.write_text('# Modified 2026-11-04: use the portable Windows bin directory and PATH separator.\n' + text)


def command(args, cwd, env, logs, name):
    result = subprocess.run([str(x) for x in args], cwd=cwd, env=env, capture_output=True, text=True, errors='replace', timeout=180)
    (logs / (name + '.log')).write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f'{name} failed; see its log')
    return result.stdout


def native_verify(root, work):
    if os.name != 'nt':
        raise RuntimeError('Native verification requires Windows')
    logs = work / 'conversion-tests'; logs.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('TEX', 'PERL', 'GS_', 'FONTCONFIG'))}
    env['PATH'] = str(root / 'bin') + ';' + str(Path(os.environ['SystemRoot']) / 'System32')
    checks = []
    modules = command([root / 'bin/perl.exe', '-MConfig', '-MWin32::API', '-e', 'print "$INC{q(Config.pm)}\\n$INC{q(Win32/API.pm)}\\n"'], logs, env, logs, 'bundled-perl-modules').splitlines()
    if len(modules) != 2 or any(not Path(name).resolve().is_relative_to(root.resolve()) for name in modules):
        raise RuntimeError('Perl did not load the bundled modules')
    checks.append({'name': 'bundled-perl-modules', 'passed': True})
    font = command([root / 'bin/fc-match.exe', '-f', '%{file}', 'Latin Modern Roman'], logs, env, logs, 'bundled-font').strip()
    if not Path(font).resolve().is_relative_to(root.resolve()):
        raise RuntimeError('Fontconfig did not use the bundled fonts')
    checks.append({'name': 'bundled-font-discovery', 'passed': True})
    for name in ('pdflatex', 'xelatex', 'perl', 'gswin64c'):
        command([root / 'bin' / (name + '.exe'), '--version'], logs, env, logs, name + '-version')
        checks.append({'name': name + '-version', 'passed': True})
    result = command([root / 'bin/kpsewhich.exe', '-var-value=TEXMFROOT'], logs, env, logs, 'tex-root').strip()
    if Path(result).resolve() != root.resolve():
        raise RuntimeError('TeX root is not relative to the installed bundle')
    for fmt in ('pdflatex', 'xelatex', 'latex', 'lualatex', 'pdftex', 'xetex'):
        command([root / 'bin/perl.exe', root / 'texmf-dist/scripts/texlive/fmtutil.pl', '--sys', '--byfmt', fmt], logs, env, logs, 'format-' + fmt)
        checks.append({'name': 'format-' + fmt, 'passed': True})
    fixtures = {
        'pdflatex': r'\documentclass{article}\usepackage{amsmath,graphicx,booktabs,longtable,geometry}\begin{document}Conversion bundle test. $E=mc^2$.\begin{tabular}{ll}\toprule Before&After\\\bottomrule\end{tabular}\end{document}',
        'xelatex': r'\documentclass{article}\usepackage{fontspec}\setmainfont{Latin Modern Roman}\begin{document}Unicode conversion: café naïve £ € —. $E=mc^2$.\end{document}',
    }
    for engine, content in fixtures.items():
        source = logs / (engine + '-sample.tex'); source.write_text(content, encoding='utf-8')
        source.with_suffix('.pdf').unlink(missing_ok=True)
        source.with_suffix('.png').unlink(missing_ok=True)
        command([root / 'bin' / (engine + '.exe'), '-halt-on-error', '-interaction=nonstopmode', source], logs, env, logs, engine + '-pdf')
        pdf = source.with_suffix('.pdf')
        if not pdf.is_file() or not pdf.read_bytes().startswith(b'%PDF-') or pdf.stat().st_size < 1000:
            raise RuntimeError(f'{engine} did not create a useful PDF')
        png = source.with_suffix('.png')
        command([root / 'bin/gswin64c.exe', '-dSAFER', '-dBATCH', '-dNOPAUSE', '-sDEVICE=png16m', '-r72', '-sOutputFile=' + str(png), pdf], logs, env, logs, engine + '-render')
        if not png.is_file() or png.read_bytes()[:8] != b'\x89PNG\r\n\x1a\n' or png.stat().st_size < 1000:
            raise RuntimeError(f'{engine} PDF did not render')
        checks.append({'name': engine + '-pdf-and-render', 'passed': True, 'pdfBytes': pdf.stat().st_size, 'renderBytes': png.stat().st_size})
    eps = logs / 'sample.eps'; eps.write_text('%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 120 80\n1 0 0 setrgbcolor 10 10 100 60 rectfill showpage\n')
    (logs / 'eps-output.pdf').unlink(missing_ok=True)
    command([root / 'bin/epstopdf.exe', eps, '--outfile=' + str(logs / 'eps-output.pdf')], logs, env, logs, 'eps-to-pdf')
    if not (logs / 'eps-output.pdf').read_bytes().startswith(b'%PDF-'):
        raise RuntimeError('EPS conversion did not produce PDF')
    checks.append({'name': 'eps-to-pdf-wrapper', 'passed': True})
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--input-cache-url', type=input_cache_base, help='HTTPS base URL for checksum-addressed TeX package/font sources')
    args = parser.parse_args()
    work = args.work.resolve(); work.mkdir(parents=True, exist_ok=True)
    report = {'status': 'failed', 'platform': 'windows', 'checks': []}
    try:
        if os.name != 'nt':
            raise RuntimeError('Run this preparation on Windows; use unit tests on other platforms')
        lock = json.loads((HERE / 'runtime.lock.json').read_text())
        cache = work / 'downloads'; cache.mkdir(exist_ok=True)
        root = work / 'stage/tex'
        if root.exists():
            raise ValueError('Staging directory already exists; choose a clean work directory')
        root.mkdir(parents=True)
        packages = lock['packages']
        paths = fetch_all(packages, cache)
        tiny = fetch(lock['texData'], cache)
        unpacked = work / 'tinytex-input'; unpacked.mkdir()
        extractor = shutil.which('7z') or shutil.which('7zz')
        if not extractor:
            raise RuntimeError('7-Zip is required to read the pinned TinyTeX installer')
        subprocess.run([extractor, 'x', '-y', '-o' + str(unpacked), str(tiny)], check=True, stdout=subprocess.DEVNULL)
        roots = list(unpacked.rglob('texmf-dist'))
        if len(roots) != 1:
            raise ValueError('Expected one pinned TeX package tree')
        copy_tex_data(roots[0].parent, root)
        closure = assemble_native(packages, paths, root)
        configure(root)
        companion = work / 'source-companion'; companion.mkdir()
        sources = fetch_all(lock['sources'], cache)
        verify_source_pairs(packages, sources)
        notices = collect_notices(packages, sources, root, closure)
        report['checks'].append({'name': 'standalone-license-notices', 'passed': True, 'sourceNotices': len(notices)})
        for name, source in sources.items():
            shutil.copyfile(source, companion / name)
        for name, source in fetch_all(lock['texSources'], cache, args.input_cache_url).items():
            shutil.copyfile(source, companion / name)
        build_info = companion / 'build-info'; build_info.mkdir()
        for item in packages:
            with tarfile.open(paths[item['filename']], 'r:*') as package:
                for name in ('.PKGINFO', '.BUILDINFO'):
                    (build_info / (item['name'] + name)).write_bytes(package.extractfile(name).read())
        # These interpreted programs and font files are already their source form.
        with tarfile.open(companion / 'tex-package-runtime-sources.tar.gz', 'w:gz') as source_archive:
            for name in ('texmf-dist', 'texmf-local', 'texmf-config', 'tlpkg/TeXLive', 'bin/runscript.tlu', 'bin/texmf.cnf', 'etc/fonts/fonts.conf', 'etc/fonts/local.conf'):
                source_archive.add(root / name, arcname=name)
        for name in ('prepare.py', 'runtime.lock.json', 'README.md'):
            shutil.copyfile(HERE / name, companion / name)
        (root / 'SOURCE.txt').write_text('Matching source archives and build recipes accompany this tools release:\nhttps://github.com/jakemanger/howtoconvert-support/releases/tag/tools-2026.10.0\n\nOriginal component notices and license texts are in share/licenses; source-index.json identifies texts preserved from the matching sources.\n')
        report['checks'].append({'name': 'pinned-source-and-native-closure', 'passed': True, 'packages': len(closure['packages'])})
        report['checks'].extend(native_verify(root, work))
        relocated = work / 'relocated with spaces/tex'; relocated.parent.mkdir()
        shutil.move(root, relocated)
        report['checks'].extend({**row, 'name': 'relocated-' + row['name']} for row in native_verify(relocated, work))
        shutil.move(relocated, root)
        artifact = work / 'tex-2026.10-windows_x86_64.tar.gz'
        with artifact.open('wb') as target, gzip.GzipFile(fileobj=target, mode='wb', filename='', mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w') as archive:
            for path in sorted(root.rglob('*')):
                name = path.relative_to(root.parent).as_posix()
                info = archive.gettarinfo(path, arcname=name); info.uid = info.gid = info.mtime = 0; info.uname = info.gname = ''; info.pax_headers = {}
                with path.open('rb') if path.is_file() else tempfile.TemporaryFile() as stream:
                    archive.addfile(info, stream if path.is_file() else None)
        manifest = {'filename': artifact.name, 'sha256': digest(artifact), 'size': artifact.stat().st_size, 'bin': 'tex/bin/pdflatex.exe'}
        (work / 'artifact.json').write_text(json.dumps(manifest, indent=2) + '\n')
        report['status'] = 'passed'
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        (work / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
