import importlib.util
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest

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
