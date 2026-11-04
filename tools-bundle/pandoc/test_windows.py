import importlib.util
import json
import io
import hashlib
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('pandoc_windows', ROOT / 'windows.py')
windows = importlib.util.module_from_spec(spec)
spec.loader.exec_module(windows)


def pe_fixture(dll=b'kernel32.dll', delayed=False):
    data = bytearray(1024)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HH', data, 0x84, 0x8664, 1)
    struct.pack_into('<H', data, 0x94, 240)
    optional = 0x98
    struct.pack_into('<H', data, optional, 0x20b)
    directory = 13 if delayed else 1
    struct.pack_into('<II', data, optional + 112 + directory * 8, 0x1000, 64 if delayed else 40)
    struct.pack_into('<IIII', data, optional + 240 + 8, 512, 0x1000, 512, 512)
    if delayed:
        struct.pack_into('<8I', data, 512, 1, 0x1080, 0, 0, 0, 0, 0, 0)
    else:
        struct.pack_into('<5I', data, 512, 0, 0, 0, 0x1080, 0)
    data[640:640 + len(dll)] = dll
    return data


class WindowsRecipeTests(unittest.TestCase):
    def test_native_first_install_output_does_not_enter_dependency_json(self):
        fixture = json.loads((ROOT / 'windows-dependencies.fixture.json').read_text())
        common, _ = windows.load_lock()
        windows.base.verify_coverage(common, fixture['graph'])
        self.assertEqual(len(fixture['graph']), 255)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(fixture['preamble'] + json.dumps(fixture['graph']))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            compiler = root / 'ghc-9.8.4.exe'; compiler.write_bytes(b'pinned compiler')
            launcher = root / 'ghc.exe'; launcher.write_bytes(b'distinct launcher')
            report = root / 'dependencies.json'
            args = ['stack.exe', '--system-ghc', '--no-install-ghc']
            environment = {'STACK_ROOT': folder}
            calls = []
            def capture(command, **kwargs):
                calls.append(command)
                if len(calls) == 1:
                    self.assertEqual(command, args + ['path', '--compiler-exe'])
                    return fixture['preamble'] + str(compiler) + '\r\n'
                self.assertEqual(command, args + ['ls', 'dependencies', 'json', 'pandoc-cli'])
                return json.dumps(fixture['graph'])
            with mock.patch.object(windows.subprocess, 'check_output', side_effect=capture), mock.patch('builtins.print'):
                self.assertEqual(windows.dependency_graph(args, environment, report, compiler), fixture['graph'])
            self.assertEqual(json.loads(report.read_text()), fixture['graph'])
            # Captured Windows output names the real versioned executable,
            # while the same pinned archive's ghc.exe is a distinct launcher.
            with mock.patch.object(windows.subprocess, 'check_output', return_value=str(compiler)), mock.patch('builtins.print'):
                with self.assertRaisesRegex(ValueError, 'pinned GHC'):
                    windows.dependency_graph(args, environment, report, launcher)
            with mock.patch.object(windows.subprocess, 'check_output', side_effect=[str(compiler), fixture['preamble'] + '[]']), mock.patch('builtins.print'):
                with self.assertRaises(json.JSONDecodeError):
                    windows.dependency_graph(args, environment, report, compiler)
            self.assertEqual(report.read_text(), fixture['preamble'] + '[]')
            other = root / 'unreviewed compiler.exe'; other.touch()
            with mock.patch.object(windows.subprocess, 'check_output', return_value=str(other)) as run, mock.patch('builtins.print'):
                with self.assertRaisesRegex(ValueError, 'pinned GHC'):
                    windows.dependency_graph(args, environment, report, compiler)
                self.assertEqual(run.call_count, 1)

    def test_reviewed_source_cache_is_hash_checked_before_safe_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder); cache = work / 'reviewed-source-cache'; cache.mkdir()
            archive = cache / 'sources.tar.gz'
            with tarfile.open(archive, 'w:gz') as bundle:
                entry = tarfile.TarInfo('reviewed/file.cabal'); entry.size = 4
                bundle.addfile(entry, io.BytesIO(b'good'))
            item = {'filename': archive.name, 'url': 'https://example.invalid/pinned',
                    'size': archive.stat().st_size, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                    'root': 'reviewed'}
            with mock.patch.object(windows, 'load_lock', return_value=({}, {'sourceCache': item})), mock.patch('builtins.print'):
                result = windows.prepare_source_cache(work)
            self.assertEqual((result / 'file.cabal').read_bytes(), b'good')
            archive.write_bytes(b'bad archive')
            with mock.patch.object(windows, 'load_lock', return_value=({}, {'sourceCache': item})), mock.patch.object(windows.base.sources.subprocess, 'run') as download, mock.patch.object(windows.base, 'extract') as extract:
                with self.assertRaises(ValueError): windows.prepare_source_cache(work)
                download.assert_called_once(); extract.assert_not_called()

    def test_project_extraction_preserves_linked_licences_and_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); archive = root / 'project.tar.gz'
            with tarfile.open(archive, 'w:gz') as source:
                for name, data in [('pandoc/COPYING.md', b'exact GPL notice\n'),
                                   ('pandoc/data/translations/nb.yaml', b'abstract: Summary\n')]:
                    entry = tarfile.TarInfo(name); entry.size = len(data)
                    source.addfile(entry, io.BytesIO(data))
                for package in ['pandoc-cli', 'pandoc-lua-engine', 'pandoc-server']:
                    link = tarfile.TarInfo(f'pandoc/{package}/COPYING.md')
                    link.type = tarfile.SYMTYPE; link.linkname = '../COPYING.md'
                    source.addfile(link)
                link = tarfile.TarInfo('pandoc/data/translations/no.yaml')
                link.type = tarfile.SYMTYPE; link.linkname = 'nb.yaml'; source.addfile(link)
            original_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
            project = windows.extract_project(archive, root / 'sources')
            for package in ['pandoc-cli', 'pandoc-lua-engine', 'pandoc-server']:
                self.assertEqual((project / package / 'COPYING.md').read_bytes(), b'exact GPL notice\n')
            self.assertEqual((project / 'data/translations/no.yaml').read_bytes(), b'abstract: Summary\n')
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), original_hash)

    def test_project_filter_normalizes_windows_link_separators_after_safety_check(self):
        with tempfile.TemporaryDirectory() as folder:
            link = tarfile.TarInfo('pandoc/pandoc-cli/COPYING.md')
            link.type = tarfile.SYMTYPE; link.linkname = '../COPYING.md'
            with mock.patch.object(windows.os, 'sep', '\\'):
                filtered = windows.project_source_filter(link, folder)
            self.assertEqual(filtered.linkname, '..\\COPYING.md')
            self.assertEqual(link.linkname, '../COPYING.md')
            link.linkname = '../../../outside'
            with self.assertRaises(tarfile.LinkOutsideDestinationError):
                windows.project_source_filter(link, folder)

    def test_project_extraction_rejects_broken_link_before_compilation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); archive = root / 'project.tar.gz'
            with tarfile.open(archive, 'w:gz') as source:
                link = tarfile.TarInfo('pandoc/pandoc-server/COPYING.md')
                link.type = tarfile.SYMTYPE; link.linkname = '../missing'
                source.addfile(link)
            # Some Windows accounts cannot create symlinks: tarfile's fallback
            # also rejects the absent target instead of leaving a usable source.
            with self.assertRaises((ValueError, tarfile.ExtractError)):
                windows.extract_project(archive, root / 'sources')

    def test_regular_and_delayed_system_imports_are_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'test.exe'
            for delayed in [False, True]:
                exe.write_bytes(pe_fixture(delayed=delayed))
                self.assertEqual(windows.verify_windows_runtime(exe), ['kernel32.dll'])

    def test_non_system_dll_and_other_architectures_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'test.exe'
            exe.write_bytes(pe_fixture(b'zlib1.dll'))
            with self.assertRaisesRegex(ValueError, 'non-system DLLs'):
                windows.verify_windows_runtime(exe)
            data = pe_fixture()
            struct.pack_into('<H', data, 0x84, 0x14c)
            exe.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'Windows x64'):
                windows.verify_windows_runtime(exe)

    def test_truncated_and_out_of_bounds_imports_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'test.exe'
            for data in [b'MZ', pe_fixture()[:525], pe_fixture()[:650]]:
                exe.write_bytes(data)
                with self.assertRaises(ValueError):
                    windows.pe_imports(exe)

    def test_windows_sources_cover_conditional_packages_and_match_common_lock(self):
        common, lock = windows.load_lock()
        windows.base.verify_coverage(common, [{'name': 'Win32', 'version': '2.13.4.0'},
                                             {'name': 'zlib-clib', 'version': '1.3.1'}])
        self.assertEqual(len(lock['runtimeSources']), 6)
        self.assertTrue(all(item['sha256'] and item['size'] > 0 for item in windows.source_items(common, lock)))
        with self.assertRaisesRegex(ValueError, 'does not cover'):
            windows.base.verify_coverage(common, [{'name': 'new-native-dependency', 'version': '1'}])

    def test_package_contains_only_runtime_and_notices(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'output'
            output.mkdir()
            for name in ['pandoc.exe', 'COPYING.md', 'NOTICE.md', 'GPL-3.0.txt', 'GFDL-1.3.txt']:
                (output / name).write_text('release content')
            (output / 'licenses').mkdir()
            (output / 'licenses/LICENSE').write_text('license')
            (output / 'private-build.log').write_text('private machine path')
            archive = root / 'tool.tar.gz'
            first = windows.package_output(output, archive)
            second = windows.package_output(output, archive)
            self.assertEqual(first['sha256'], second['sha256'])
            with tarfile.open(archive) as bundle:
                self.assertEqual(len(bundle.getnames()), 6)
                self.assertNotIn('pandoc/private-build.log', bundle.getnames())
                self.assertEqual(bundle.getmember('pandoc/pandoc.exe').mode, 0o755)

    def test_stack_setup_pins_every_external_tool_download(self):
        _, lock = windows.load_lock()
        config = windows.setup_info(lock['toolchain'])
        for item in [config['sevenzexe-info'], config['sevenzdll-info'], config['msys2']['windows64']]:
            self.assertEqual(len(item['sha256']), 64)
            self.assertGreater(item['content-length'], 0)
            self.assertIn('/download/', item['url'])


if __name__ == '__main__':
    unittest.main()
