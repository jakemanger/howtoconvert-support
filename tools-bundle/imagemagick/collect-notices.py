#!/usr/bin/env python3
"""Collect binary and source-redistribution notices for the pinned tools build."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request

NOTICE = re.compile(r'^(licen[cs]e|copying|copyright|notice|patents|authors)(?:[._-].*)?$', re.I)
TARGETS = ('aarch64-apple-darwin', 'x86_64-apple-darwin')
MAX_NOTICE = 4 * 1024 * 1024

# Notices omitted from published .crate files. Each mapping is bound to the
# original crate checksum and an immutable upstream notice revision.
SUPPLEMENTAL_NOTICES = {'anes-0.1.6': {'crateCommit': '378ac7461ecac29fde9e10df7b08359d1315a9ad',
                'crateSha256': '4b46cbb362ab8752921c97e041f5e366ee6297bd428a31275b9fcf1e380f7299',
                'files': [{'path': 'LICENSE-APACHE',
                           'sha256': 'a60eea817514531668d7e00765731449fe14d059d3249e0bc93b36de45f759f2'},
                          {'path': 'LICENSE-MIT',
                           'sha256': '23f18e03dc49df91622fe2a76176497404e46ced8a715d9d2b67a7446571cca3'}],
                'noticeCommit': '378ac7461ecac29fde9e10df7b08359d1315a9ad',
                'provenance': 'crate-vcs-commit',
                'repository': 'zrzka/anes-rs'},
 'difflib-0.4.0': {'crateSha256': '6184e33543162437515c2e2b48714794e37845ec9851711914eec9d308f6ebe8',
                   'files': [{'path': 'LICENSE',
                              'sha256': '6725d1437fc6c77301f2ff0e7d52914cf4f9509213e1078dc77d9356dbe6eac5'}],
                   'matchedPublishedFiles': 7,
                   'noticeCommit': 'f035fb8e656f27119e23eca9d5b996df14f3885e',
                   'provenance': 'published-content-match',
                   'repository': 'DimaKudosh/difflib'},
 'profiling-1.0.16': {'crateCommit': '0ba524064aae5b4972e6be3424a2acb316f93ba3',
                      'crateSha256': 'afbdc74edc00b6f6a218ca6a5364d6226a259d4b8ea1af4a0ea063f27e179f4d',
                      'files': [{'path': 'LICENSE-APACHE',
                                 'sha256': '10d30a673cd5e9349bdc02aeb48f14b3386d27d0da32df8f0a555d4aa16aa551'},
                                {'path': 'LICENSE-MIT',
                                 'sha256': 'c8167fdeeed46d3f244d3f85c5bf998ce889343691c32be2c61a8bc4b5c08333'}],
                      'noticeCommit': '0ba524064aae5b4972e6be3424a2acb316f93ba3',
                      'provenance': 'crate-vcs-commit',
                      'repository': 'aclysma/profiling'},
 'profiling-procmacros-1.0.16': {'crateCommit': '0ba524064aae5b4972e6be3424a2acb316f93ba3',
                                 'crateSha256': 'a65f2e60fbf1063868558d69c6beacf412dc755f9fc020f514b7955fc914fe30',
                                 'files': [{'path': 'LICENSE-APACHE',
                                            'sha256': '10d30a673cd5e9349bdc02aeb48f14b3386d27d0da32df8f0a555d4aa16aa551'},
                                           {'path': 'LICENSE-MIT',
                                            'sha256': 'c8167fdeeed46d3f244d3f85c5bf998ce889343691c32be2c61a8bc4b5c08333'}],
                                 'noticeCommit': '0ba524064aae5b4972e6be3424a2acb316f93ba3',
                                 'provenance': 'crate-vcs-commit',
                                 'repository': 'aclysma/profiling'},
 'simd_helpers-0.1.0': {'crateCommit': 'ca1a2f84aa386d758e98f8a609d990263932fb85',
                        'crateSha256': '95890f873bec569a0362c235787f3aca6e1e887302ba4840839bcc6459c42da6',
                        'files': [{'path': 'LICENSE',
                                   'sha256': 'd69f24ad84ec2ade64c0b68bdb31b41170e997b158370342056918329cc9af1e'}],
                        'licenseOnlyCommitParent': 'ca1a2f84aa386d758e98f8a609d990263932fb85',
                        'noticeCommit': '82040194cd05affb060bf94d6f19f82a771d07fb',
                        'provenance': 'license-only-direct-child',
                        'repository': 'lu-zero/simd_helpers'},
 'valuable-0.1.1': {'crateCommit': '9efc29b6e58cef28f6566a47aa7e142a55fead77',
                    'crateSha256': 'ba73ea9cf16a25df0c8caa16c51acb937d5712a8429db78a3ee29d5dcacd3a65',
                    'files': [{'path': 'LICENSE',
                               'sha256': 'ed60d479b8fd1f64e9cbc3de449a16a53ac1b3d1b6aeb9bf9d190a8e93061b44'}],
                    'noticeCommit': '9efc29b6e58cef28f6566a47aa7e142a55fead77',
                    'provenance': 'crate-vcs-commit',
                    'repository': 'tokio-rs/valuable'},
 'winapi-i686-pc-windows-gnu-0.4.0': {'crateSha256': 'ac3b87c63620426dd9b991e5ce0329eff545bccbbb34f3be09ff6fb6ab51b7b6',
                                      'files': [{'path': 'LICENSE-APACHE',
                                                 'sha256': 'b40930bbcf80744c86c46a12bc9da056641d722716c378f5659b9e555ef833e1'},
                                                {'path': 'LICENSE-MIT',
                                                 'sha256': '5b19674a1db628a475850a131956ed49521b744e3dda8f5a94141f9aba681219'}],
                                      'matchedPublishedFiles': 1390,
                                      'noticeCommit': '9497609ef44cc9bcd16cd2411c0ee6ccaf5483aa',
                                      'provenance': 'published-content-match',
                                      'repository': 'retep998/winapi-rs'},
 'winapi-x86_64-pc-windows-gnu-0.4.0': {'crateSha256': '712e227841d057c1ee1cd2fb22fa7e5a5461ae8e48fa2ca79ec42cfc1931183f',
                                        'files': [{'path': 'LICENSE-APACHE',
                                                   'sha256': 'b40930bbcf80744c86c46a12bc9da056641d722716c378f5659b9e555ef833e1'},
                                                  {'path': 'LICENSE-MIT',
                                                   'sha256': '5b19674a1db628a475850a131956ed49521b744e3dda8f5a94141f9aba681219'}],
                                        'matchedPublishedFiles': 1419,
                                        'noticeCommit': '9497609ef44cc9bcd16cd2411c0ee6ccaf5483aa',
                                        'provenance': 'published-content-match',
                                        'repository': 'retep998/winapi-rs'},
 'wit-bindgen-rt-0.39.0': {'crateCommit': 'f2393e6e98fa5f9236cac580db8a3fc9de6a4b70',
                           'crateSha256': '6f42320e61fe2cfd34354ecb597f86f413484a798ba44a8ca1165c58d42da6c1',
                           'files': [{'path': 'LICENSE-APACHE',
                                      'sha256': 'a60eea817514531668d7e00765731449fe14d059d3249e0bc93b36de45f759f2'},
                                     {'path': 'LICENSE-Apache-2.0_WITH_LLVM-exception',
                                      'sha256': '268872b9816f90fd8e85db5a28d33f8150ebb8dd016653fb39ef1f94f2686bc5'},
                                     {'path': 'LICENSE-MIT',
                                      'sha256': '23f18e03dc49df91622fe2a76176497404e46ced8a715d9d2b67a7446571cca3'}],
                           'noticeCommit': 'f2393e6e98fa5f9236cac580db8a3fc9de6a4b70',
                           'provenance': 'crate-vcs-commit',
                           'repository': 'bytecodealliance/wit-bindgen'}}


def safe(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError(f'Unsafe archive path: {name}')
    return path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_verified(path, expected):
    data = path.read_bytes()
    if digest(data) != expected:
        raise ValueError(f'Checksum mismatch: {path.name}')
    return data


def fetch_verified(url, expected, cache):
    file = cache / expected
    if not file.exists():
        request = urllib.request.Request(url, headers={'User-Agent': 'conversion-tools-notices/1'})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024 or digest(data) != expected:
            raise ValueError(f'Unexpected upstream content: {url}')
        file.write_bytes(data)
    return read_verified(file, expected)


def command(argv):
    return subprocess.check_output(argv, text=True, stderr=subprocess.PIPE)


def build_closure(source_archive, cargo):
    with tempfile.TemporaryDirectory(prefix='conversion-tools-notices-') as temporary:
        with tarfile.open(source_archive) as archive:
            for member in archive:
                safe(member.name)
            archive.extractall(temporary, filter='data')
        manifests = list(Path(temporary).glob('*/Cargo.toml'))
        if len(manifests) != 1:
            raise ValueError('Expected one rav1e source root')
        manifest = manifests[0]
        lock = manifest.with_name('Cargo.lock')
        original = lock.read_bytes()
        targets = {}
        for target in TARGETS:
            tree = command([cargo, 'tree', '--locked', '--offline', '--manifest-path', str(manifest),
                '--package', 'rav1e', '--target', target, '--features', 'capi',
                '--edges', 'normal,build', '--prefix', 'none', '--format', '{p}'])
            packages = sorted({f'{m[1]}-{m[2]}' for line in tree.splitlines()
                               if (m := re.match(r'^([A-Za-z0-9_-]+) v([^ ]+)', line))})
            if not packages:
                raise ValueError(f'Empty Rust build closure: {target}')
            targets[target] = packages
        if lock.read_bytes() != original:
            raise ValueError('Cargo changed the source lockfile')
        return {'features': ['default', 'capi'], 'edges': ['normal', 'build'],
                'lockSha256': digest(original), 'targets': targets,
                'localPackages': sorted(f'{p["name"]}-{p["version"]}' for p in tomllib.loads(original.decode())['package'] if not p.get('source'))}


class Collector:
    def __init__(self, output, cache):
        self.output, self.cache = output, cache
        self.records, self.gaps = [], []

    def write(self, scope, component, name, data, origin):
        if len(data) > MAX_NOTICE:
            raise ValueError(f'Unexpected notice size: {component}/{name}')
        relative = safe(scope) / safe(component) / safe(name)
        target = self.output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != data:
                raise ValueError(f'Conflicting notice path: {relative}')
            return
        target.write_bytes(data)
        self.records.append({'component': component, 'scope': scope, 'path': str(relative),
                             'sha256': digest(data), 'origin': origin})

    def archive(self, archive, scope, component, origin):
        count = 0
        for member in archive:
            name = safe(member.name)
            if member.isfile() and NOTICE.match(name.name):
                self.write(scope, component, str(name), archive.extractfile(member).read(), origin)
                count += 1
        return count

    def crate(self, data, crate_id, scope, origin):
        with tarfile.open(fileobj=io.BytesIO(data)) as crate:
            count = self.archive(crate, scope, 'rust/' + crate_id, origin)
        if count:
            return
        supplement = SUPPLEMENTAL_NOTICES.get(crate_id)
        if not supplement or supplement['crateSha256'] != digest(data):
            self.gaps.append({'component': crate_id, 'scope': scope, 'reason': 'missing-license-notice'})
            return
        for notice in supplement['files']:
            url = f'https://raw.githubusercontent.com/{supplement["repository"]}/{supplement["noticeCommit"]}/{notice["path"]}'
            text = fetch_verified(url, notice['sha256'], self.cache)
            provenance = {key: value for key, value in supplement.items() if key != 'files'}
            provenance['url'] = url
            self.write(scope, 'rust/' + crate_id, 'upstream/' + notice['path'], text, provenance)

    def runtime(self, rustc, source_override=None, sysroot_override=None):
        verbose = command([rustc, '--version', '--verbose'])
        version = dict(line.split(': ', 1) for line in verbose.splitlines()[1:] if ': ' in line)
        commit, release = version['commit-hash'], version['release']
        sysroot = Path(sysroot_override) if sysroot_override else Path(command([rustc, '--print', 'sysroot']).strip())
        copyright_file = next((p for p in [sysroot / 'share/doc/rust/COPYRIGHT-library.html',
            sysroot / 'share/doc/rustc/COPYRIGHT-library.html'] if p.exists()), None)
        if copyright_file is None:
            raise ValueError('Rust standard-library copyright notice is missing; install matching rust-docs')
        self.write('binary', 'rust-runtime', 'COPYRIGHT-library.html', copyright_file.read_bytes(),
                   {'toolchain': release, 'rustCommit': commit, 'path': 'COPYRIGHT-library.html'})
        source = Path(source_override) if source_override else sysroot / 'lib/rustlib/src/rust'
        if not (source / 'library/Cargo.lock').exists():
            # A second installation of the exact same Rust commit may carry
            # rust-src even when the active rustup installation does not.
            alternative = shutil.which('rustc')
            if alternative:
                other = command([alternative, '--version', '--verbose'])
                if f'commit-hash: {commit}\n' in other:
                    source = Path(command([alternative, '--print', 'sysroot']).strip()) / 'lib/rustlib/src/rust'
        lock_path = source / 'library/Cargo.lock'
        if not lock_path.exists():
            raise ValueError('Matching rust-src is required; install it or pass --rust-source-root')
        # Confirm the source lock against the immutable compiler revision.
        lock = lock_path.read_bytes()
        lock_url = f'https://raw.githubusercontent.com/rust-lang/rust/{commit}/library/Cargo.lock'
        fetch_verified(lock_url, digest(lock), self.cache)
        for name in ['LICENSE-MIT', 'LICENSE-APACHE']:
            candidates = [sysroot / name, source / name]
            local = next((p for p in candidates if p.exists()), None)
            if local:
                data = local.read_bytes()
                url = f'https://raw.githubusercontent.com/rust-lang/rust/{commit}/{name}'
                fetch_verified(url, digest(data), self.cache)
            else:
                # Release notices are embedded in the official documentation.
                files = list((sysroot / 'share/doc/rust/html/static.files').glob(name + '-*'))
                if len(files) != 1:
                    raise ValueError(f'Missing Rust runtime notice: {name}')
                data = files[0].read_bytes()
                url = f'https://raw.githubusercontent.com/rust-lang/rust/{commit}/{name}'
                fetch_verified(url, digest(data), self.cache)
            self.write('binary', 'rust-runtime', name, data, {'rustCommit': commit, 'url': url})
        for folder in ['library/backtrace', 'library/stdarch', 'library/portable-simd',
                       'library/compiler-builtins', 'src/llvm-project/libunwind']:
            directory = source / folder
            notices = sorted(p for p in directory.rglob('*') if p.is_file() and NOTICE.match(p.name))
            if not notices:
                raise ValueError(f'Missing Rust source runtime notices: {folder}')
            for notice in notices:
                relative = notice.relative_to(source)
                self.write('binary', 'rust-runtime', str(relative), notice.read_bytes(),
                           {'rustCommit': commit, 'sourcePath': str(relative)})
        installed = set()
        for target in TARGETS:
            lib = sysroot / 'lib/rustlib' / target / 'lib'
            installed.update(re.sub(r'-[0-9a-f]+\.rlib$', '', p.name[3:]) for p in lib.glob('lib*.rlib'))
        if not installed:
            raise ValueError('No installed Apple Rust standard library found')
        runtime_crates = []
        for package in tomllib.loads(lock.decode())['package']:
            if not package.get('source', '').startswith('registry+') or package['name'].replace('-', '_') not in installed:
                continue
            name, ver = package['name'], package['version']
            crate_id = f'{name}-{ver}'
            url = f'https://static.crates.io/crates/{name}/{crate_id}.crate'
            data = fetch_verified(url, package['checksum'], self.cache)
            self.crate(data, crate_id, 'binary', {'runtime': release, 'url': url,
                                                'archiveSha256': package['checksum']})
            runtime_crates.append(crate_id)
        # Unicode tables carry their own copyright in COPYRIGHT-library.html.
        # Preserve the complete Unicode-3.0 terms from the pinned dependency.
        unicode_notice = next((record for record in self.records
            if record['component'].startswith('rust/unicode-ident-')
            and record['path'].endswith('LICENSE-UNICODE')), None)
        if unicode_notice is None:
            raise ValueError('Unicode-3.0 license text for Rust runtime tables is missing')
        data = (self.output / unicode_notice['path']).read_bytes()
        if b'UNICODE LICENSE V3' not in data:
            raise ValueError('Unexpected Unicode runtime license version')
        self.write('binary', 'rust-runtime', 'Unicode-3.0.txt', data,
                   {'notice': unicode_notice['path'], 'copyright': 'COPYRIGHT-library.html'})
        return {'release': release, 'commit': commit, 'sourceLockSha256': digest(lock),
                'registryCrates': sorted(runtime_crates)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--notice-cache', type=Path)
    cargo_home = Path(os.environ.get('CARGO_HOME', str(Path.home() / '.cargo')))
    parser.add_argument('--cargo', default=str(cargo_home / 'bin/cargo'))
    parser.add_argument('--rustc', default=str(cargo_home / 'bin/rustc'))
    parser.add_argument('--rust-source-root', type=Path)
    parser.add_argument('--rust-sysroot', type=Path)
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError('Use an empty notice output directory')
    args.out.mkdir(parents=True, exist_ok=True)
    temporary_cache = tempfile.TemporaryDirectory(prefix='conversion-tools-notice-cache-') if args.notice_cache is None else None
    cache = args.notice_cache or Path(temporary_cache.name)
    cache.mkdir(parents=True, exist_ok=True)
    source_index = json.loads((args.sources / 'source-index.json').read_text())
    for source in source_index['sources']:
        read_verified(args.sources / safe(source['filename']), source['sha256'])
    rav1e = next(source for source in source_index['sources']
                 if source['component'] == 'librav1e' and source['kind'] == 'src')
    closure = build_closure(args.sources / rav1e['filename'], args.cargo)
    used = set().union(*(set(packages) for packages in closure['targets'].values()))
    collector = Collector(args.out, cache)
    distributed_crates, binary_crates = [], []
    native_components = []
    for source in source_index['sources']:
        file = args.sources / safe(source['filename'])
        origin = {key: source[key] for key in ['url', 'filename', 'sha256'] if key in source}
        if source['kind'] == 'rust-dependencies':
            expected = {entry['filename']: entry['sha256'] for entry in source['crates']}
            with tarfile.open(file) as crates:
                for member in crates:
                    safe(member.name)
                    if not member.isfile() or not member.name.endswith('.crate'):
                        continue
                    crate_id = Path(member.name).stem
                    data = crates.extractfile(member).read()
                    if expected.get(Path(member.name).name) != digest(data):
                        raise ValueError(f'Rust crate checksum mismatch: {crate_id}')
                    scope = 'binary' if crate_id in used else 'source-only'
                    collector.crate(data, crate_id, scope, {**origin, 'crateSha256': digest(data)})
                    distributed_crates.append(crate_id)
                    if crate_id in used:
                        binary_crates.append(crate_id)
            if len(distributed_crates) != len(expected) or len(set(distributed_crates)) != len(distributed_crates):
                raise ValueError('Rust source archive is incomplete or duplicated')
            continue
        if source['kind'] != 'src':
            continue
        native_components.append(source['component'])
        if tarfile.is_tarfile(file):
            with tarfile.open(file) as archive:
                count = collector.archive(archive, 'binary', source['component'], origin)
        else:
            count = 0
            for name in command(['tar', '-tf', str(file)]).splitlines():
                if NOTICE.match(safe(name).name) and not name.endswith('/'):
                    data = subprocess.check_output(['tar', '-xOf', str(file), '--', name])
                    collector.write('binary', source['component'], name, data, origin)
                    count += 1
        if not count:
            collector.gaps.append({'component': source['component'], 'reason': 'missing-license-notice'})
    local_packages = set(closure['localPackages'])
    if used - set(binary_crates) - local_packages:
        raise ValueError('A compiled Rust dependency is absent from the source archive')
    runtime = collector.runtime(args.rustc, args.rust_source_root, args.rust_sysroot)
    index = {'schema': 1, 'nativeComponents': sorted(native_components), 'rustBuild': closure,
             'rustBinaryCrates': sorted(binary_crates), 'rustDistributedCrates': sorted(distributed_crates),
             'rustRuntime': runtime, 'notices': collector.records, 'gaps': collector.gaps}
    (args.out / 'index.json').write_text(json.dumps(index, indent=2) + '\n')
    if collector.gaps:
        raise ValueError(f'{len(collector.gaps)} unresolved notice gaps; see index.json')
    if temporary_cache is not None:
        temporary_cache.cleanup()
    print(f'Collected {len(collector.records)} notices: {len(native_components)} native components, '
          f'{len(binary_crates)} build crates, {len(distributed_crates)} redistributed crates, Rust {runtime["release"]}')


if __name__ == '__main__':
    main()
