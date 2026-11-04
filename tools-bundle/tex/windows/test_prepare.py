import hashlib
import importlib.util
import io
import struct
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('tex_prepare', Path(__file__).with_name('prepare.py'))
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def executable(imported=None, delayed=None):
    data = bytearray(2048)
    data[:2] = b'MZ'; struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<H', data, 0x86, 1)
    struct.pack_into('<H', data, 0x94, 240)
    opt = 0x98
    struct.pack_into('<H', data, opt, 0x20b)
    struct.pack_into('<Q', data, opt + 24, 0x140000000)
    struct.pack_into('<IIII', data, opt + 240 + 8, 1536, 0x1000, 1536, 512)
    if imported:
        struct.pack_into('<II', data, opt + 112 + 8, 0x1000, 40)
        struct.pack_into('<I', data, 512 + 12, 0x1080)
        text = imported.encode() + b'\0'; data[640:640+len(text)] = text
    if delayed:
        struct.pack_into('<II', data, opt + 112 + 13 * 8, 0x1100, 64)
        struct.pack_into('<II', data, 768, 1, 0x1180)
        text = delayed.encode() + b'\0'; data[896:896+len(text)] = text
    return bytes(data)


def archive(path, files):
    with tarfile.open(path, 'w:gz') as target:
        for name, data in files.items():
            item = tarfile.TarInfo(name); item.size = len(data)
            target.addfile(item, io.BytesIO(data))


class PreparationTests(unittest.TestCase):
    def test_imports_include_delayed_libraries(self):
        self.assertEqual(prepare.pe_imports(executable('KERNEL32.dll', 'library.dll')), ['kernel32.dll', 'library.dll'])

    def test_transitive_closure_keeps_native_libraries_and_rejects_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); package = root / 'native.tar.gz'
            archive(package, {'ucrt64/bin/sample.exe': executable('one.dll'),
                              'ucrt64/bin/one.dll': executable(delayed='two.dll'),
                              'ucrt64/bin/two.dll': executable('KERNEL32.dll')})
            with patch.object(prepare, 'ENGINES', ('sample',)), patch.object(prepare, 'WRAPPERS', ()):
                result = prepare.assemble_native([{'name': 'native', 'filename': package.name}], {package.name: package}, root / 'output')
                self.assertEqual(result['programsAndLibraries'], ['one.dll', 'sample.exe', 'two.dll'])
                self.assertEqual(result['systemLibraries'], ['kernel32.dll'])
                archive(package, {'ucrt64/bin/sample.exe': executable('missing.dll')})
                with self.assertRaisesRegex(ValueError, 'Missing required'):
                    prepare.assemble_native([{'name': 'native', 'filename': package.name}], {package.name: package}, root / 'missing')

    def test_unknown_import_is_not_treated_as_a_system_library(self):
        self.assertFalse(prepare.is_system_dll('msys-2.0.dll'))
        self.assertFalse(prepare.is_system_dll('libgcc_s_seh-1.dll'))
        self.assertTrue(prepare.is_system_dll('api-ms-win-crt-string-l1-1-0.dll'))

    def test_source_recipe_must_match_binary_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source.tar.gz'; recipe = b'pkgver=1\n'
            archive(source, {'package/PKGBUILD': recipe})
            package = {'name': 'sample', 'sourceFile': source.name, 'pkgbuildSha256': hashlib.sha256(recipe).hexdigest()}
            prepare.verify_source_pairs([package], {source.name: source})
            package['pkgbuildSha256'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'does not match'):
                prepare.verify_source_pairs([package], {source.name: source})

    def test_corrupt_input_and_unsafe_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); file = root / 'archive'; file.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'Checksum'):
                prepare.verify_file(file, {'size': 7, 'sha256': hashlib.sha256(b'correct').hexdigest()})
            for name in ('../outside', '/outside', 'a/../../outside', 'a\\outside', 'C:/outside', 'file:stream'):
                with self.assertRaises(ValueError):
                    prepare.write_file(root, name, b'x')

    def test_source_license_texts_are_preserved_from_nested_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); nested = root / 'upstream.tar.gz'; source = root / 'source.tar.gz'
            archive(nested, {'upstream/COPYING.LESSER': b'original lesser license',
                             'upstream/subcomponent/Artistic': b'original artistic license'})
            archive(source, {'package/upstream.tar.gz': nested.read_bytes(), 'package/LICENSE': b'original package notice'})
            rows = prepare.source_notices(source, 'sample', root / 'output')
            self.assertEqual(len(rows), 3)
            self.assertEqual({(root / 'output' / row['file']).read_bytes() for row in rows},
                             {b'original lesser license', b'original artistic license', b'original package notice'})
            for row in rows:
                self.assertEqual(prepare.digest(root / 'output' / row['file']), row['sha256'])
                self.assertEqual(row['sourceArchive'], source.name)

    def test_embedded_declaration_requires_exact_member_and_shared_license_texts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'extension.tar.gz'; perl = root / 'perl.tar.gz'
            declaration = b'Original author; redistribute under the same terms as Perl.'
            archive(source, {'extension/Module.pm': declaration})
            archive(perl, {'perl/Artistic': b'Original Artistic terms', 'perl/Copying': b'Original GPL terms'})
            pin = {'member': 'extension/Module.pm', 'sha256': hashlib.sha256(declaration).hexdigest()}
            packages = [{'name': 'mingw-w64-ucrt-x86_64-perl-extension', 'sourceFile': source.name,
                         'additionalNotices': [pin], 'licenseTextsFrom': 'perl'},
                        {'name': 'mingw-w64-ucrt-x86_64-perl', 'sourceFile': perl.name}]
            paths = {source.name: source, perl.name: perl}
            rows = prepare.collect_notices(packages, paths, root / 'output', {'packageNotices': []})
            self.assertEqual(len([row for row in rows if row['component'] == 'perl-extension']), 3)
            self.assertEqual((root / 'output/share/licenses/source/perl-extension/Perl-Copying').read_bytes(), b'Original GPL terms')
            pin['sha256'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'Changed license declaration'):
                prepare.collect_notices(packages, paths, root / 'changed', {'packageNotices': []})
            pin['member'] = 'extension/Missing.pm'
            with self.assertRaisesRegex(ValueError, 'Missing pinned license'):
                prepare.collect_notices(packages, paths, root / 'missing', {'packageNotices': []})

    def test_metadata_without_any_license_text_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'source.tar.gz'
            archive(source, {'package/PKGBUILD': b'license=(GPL)\n'})
            with self.assertRaisesRegex(ValueError, 'No license notices'):
                prepare.collect_notices([{'name': 'sample', 'sourceFile': source.name}],
                                        {source.name: source}, root, {'packageNotices': []})

    def test_upstream_wrapper_change_fails_instead_of_silently_skipping(self):
        with self.assertRaisesRegex(ValueError, 'Upstream wrapper changed'):
            prepare.replace_once('changed input', 'expected input', 'new input')
        with self.assertRaises(ValueError):
            prepare.replace_once('input input', 'input', 'new input')


if __name__ == '__main__':
    unittest.main()
