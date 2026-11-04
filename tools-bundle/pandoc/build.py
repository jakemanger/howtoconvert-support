#!/usr/bin/env python3
"""Rebuild Pandoc 3.6.2 with the pinned macOS compiler and Stack configuration."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile

RECIPE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('sources', RECIPE / 'collect-sources.py')
sources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sources)


def extract(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as bundle:
        roots = {Path(member.name).parts[0] for member in bundle if member.name and member.name != '.'}
        if len(roots) != 1:
            raise ValueError(f'Expected one archive root in {archive.name}')
        bundle.extractall(destination, filter='data')
    return destination / roots.pop()


def run(args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def verify_coverage(lock, actual):
    covered = {(p['name'], p['version']) for p in lock['packages'] + lock['compilerPackages'] + lock['projectPackages']}
    missing = {(p['name'], p['version']) for p in actual} - covered
    if missing:
        raise ValueError('Source companion does not cover: ' + repr(sorted(missing)))


def verify_portability(linked, commands, target):
    for line in linked.splitlines()[1:]:
        library = line.strip().split(' (', 1)[0]
        if not library.startswith(('/usr/lib/', '/System/Library/')):
            raise ValueError('Non-system runtime dependency: ' + library)
    versions = []
    for block in commands.split('Load command'):
        if 'LC_BUILD_VERSION' in block or 'LC_VERSION_MIN_MACOSX' in block:
            match = re.search(r'^\s*(?:minos|version)\s+(\d+(?:\.\d+)+)', block, re.M)
            if match:
                versions.append(match.group(1))
    def number(version):
        parts = [int(p) for p in version.split('.')]
        return tuple((parts + [0, 0])[:3])
    if not versions or any(number(version) > number(target) for version in versions):
        raise ValueError('Unexpected minimum macOS version: ' + repr(versions))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True, help='Dedicated build directory')
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    if platform.system() != 'Darwin' or platform.machine() not in ('arm64', 'x86_64'):
        raise SystemExit('This recipe uses the macOS GHC distributions; run it on macOS.')
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    work = args.work.resolve()
    downloads = work / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    lock = json.loads((RECIPE / 'sources.lock.json').read_text())
    toolchain = lock['toolchains']['mac_' + platform.machine()]
    stack_archive = sources.fetch(toolchain['stack'], downloads)
    ghc_archive = sources.fetch(toolchain['ghc'], downloads)
    project_archive = sources.fetch(lock['archives'][0], downloads)
    stack_source = extract(stack_archive, work / 'toolchain')
    stack = stack_source / 'stack'
    ghc_prefix = work / 'toolchain' / ('ghc-' + lock['compiler'])
    if not (ghc_prefix / '.installation-complete').exists():
        ghc_source = extract(ghc_archive, work / 'compiler-inputs')
        run(['./configure', '--prefix=' + str(ghc_prefix)], cwd=ghc_source)
        run(['make', 'install'], cwd=ghc_source)
        (ghc_prefix / '.installation-complete').write_text(lock['compiler'] + '\n')
    project = extract(project_archive, work / 'sources')
    for name in ('stack.yaml', 'stack.yaml.lock'):
        shutil.copyfile(RECIPE / name, project / name)
    env = os.environ.copy()
    target = toolchain['deploymentTarget']
    configuration = project / 'stack.yaml'
    text = configuration.read_text()
    if text.count('ghc-options:\n') != 1 or '$everything' in text:
        raise ValueError('Unexpected GHC options in pinned configuration')
    text = text.replace('ghc-options:\n', 'ghc-options:\n   \"$everything\": \"-optc-mmacosx-version-min=' + target + ' -optl-mmacosx-version-min=' + target + '\"\n')
    configuration.write_text(text)
    env['MACOSX_DEPLOYMENT_TARGET'] = target
    env['STACK_ROOT'] = str(work / 'stack-root')
    env['PATH'] = str(ghc_prefix / 'bin') + os.pathsep + env['PATH']
    env.pop('STACK_YAML', None)
    common = [stack, '--stack-yaml', project / 'stack.yaml', '--system-ghc',
              '--no-install-ghc', '--lock-file', 'error-on-write', '--jobs', args.jobs]
    run(common + ['build', 'pandoc-cli:exe:pandoc', '--no-haddock', '--no-test', '--no-bench'], env=env)
    output = work / 'output'
    output.mkdir(exist_ok=True)
    run(common + ['--local-bin-path', output, 'install', 'pandoc-cli:exe:pandoc'], env=env)
    # Match the upstream macOS release recipe; Apple strip retains its ad-hoc signature.
    run(['strip', output / 'pandoc'])
    if platform.machine() == 'arm64':
        run(['codesign', '--verify', '--strict', output / 'pandoc'])
    run([output / 'pandoc', '--version'])
    with (output / 'dependencies.json').open('w') as handle:
        run(common + ['ls', 'dependencies', 'json', 'pandoc-cli'], env=env, stdout=handle)
    actual = json.loads((output / 'dependencies.json').read_text())
    verify_coverage(lock, actual)
    run(['python3', RECIPE / 'collect-sources.py', '--output', work / 'source-companion'])
    shutil.copytree(work / 'source-companion' / 'licenses', output / 'licenses', dirs_exist_ok=True)
    linked = subprocess.check_output(['otool', '-L', str(output / 'pandoc')], text=True)
    commands = subprocess.check_output(['otool', '-l', str(output / 'pandoc')], text=True)
    verify_portability(linked, commands, target)
    (output / 'linked-libraries.txt').write_text(linked)
    (output / 'load-commands.txt').write_text(commands)
    shutil.copyfile(project / 'COPYING.md', output / 'COPYING.md')
    for name in ('NOTICE.md', 'GPL-3.0.txt', 'GFDL-1.3.txt'):
        shutil.copyfile(RECIPE / name, output / name)
    digest = hashlib.sha256((output / 'pandoc').read_bytes()).hexdigest()
    (output / 'SHA256SUMS').write_text(f'{digest}  pandoc\n')
    print(f'Built {output / "pandoc"} with notices and verified source companion.')


if __name__ == '__main__':
    main()
