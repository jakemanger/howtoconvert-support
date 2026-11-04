import hashlib
import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('split_windows_sources', ROOT / 'split-windows-imagemagick-sources.py')
splitter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(splitter)


class SourceSplitTests(unittest.TestCase):
    def make_source(self, root, legacy):
        tree = root / 'input'; tree.mkdir()
        files = {'ImageMagick/LICENSE': b'original core license',
                 'ImageMagick/NOTICE': b'original component notices',
                 'ImageMagick/code.c': b'core source',
                 'Configure/Configure.cpp': b'build configuration'}
        if legacy:
            files.update({'Artifacts/bin/delegates.xml': b'original configuration',
                          'Build/package.version.h.in': b'original template',
                          'Projects/MagickCore.vcxproj': b'original project',
                          'Dependencies/raw/COPYING': b'original raw license',
                          'Dependencies/raw/libraw.c': b'raw source',
                          'OptionalDependencies/flif/LICENSE': b'original flif license'})
        else:
            files.update({'Dependencies/build.ps1': b'original build script',
                          'Dependencies/Dependencies/raw/COPYING': b'original raw license',
                          'Dependencies/OptionalDependencies/flif/LICENSE': b'original flif license',
                          'Dependencies/NonWindowsDependencies/helper/LICENSE': b'original helper license'})
        for name, data in files.items():
            path = tree / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        history = tree / 'ImageMagick/.git/config'; history.parent.mkdir(); history.write_text('private git history')
        archive = root / 'source.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            for item in sorted(tree.iterdir()): output.add(item, arcname=item.name)
        return archive, files

    def test_both_vendor_layouts_preserve_all_source_build_and_notice_bytes(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); source, expected = self.make_source(root, legacy)
                index = splitter.split(source, splitter.digest(source), '7.1.1-47', root / 'parts')
                actual = {}
                for part in index['sources']:
                    archive = root / 'parts' / part['filename']
                    self.assertEqual(splitter.digest(archive), part['sha256'])
                    self.assertEqual(archive.stat().st_size, part['size'])
                    with tarfile.open(archive) as package:
                        for item in package:
                            self.assertNotIn('.git', Path(item.name).parts)
                            if item.isfile():
                                data = package.extractfile(item).read()
                                if item.name in actual: self.assertEqual(actual[item.name], data)
                                actual[item.name] = data
                self.assertEqual(actual, expected)
                second = splitter.split(source, splitter.digest(source), '7.1.1-47', root / 'second')
                self.assertEqual(index, second)

    def test_unverified_archive_and_unknown_roots_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, _ = self.make_source(root, True)
            with self.assertRaisesRegex(ValueError, 'checksum'):
                splitter.split(source, '0' * 64, '7.1.1-47', root / 'bad')
            (root / 'input/unexpected').mkdir()
            with self.assertRaisesRegex(ValueError, 'Unexpected source tree roots'):
                splitter.source_groups(root / 'input')

    def test_streaming_rejects_a_link_outside_the_source_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = self.make_source(root, True)
            (root / 'input/ImageMagick/unsafe').symlink_to('../../outside')
            with tarfile.open(source, 'w:gz') as output:
                for item in sorted((root / 'input').iterdir()):
                    output.add(item, arcname=item.name)
            with self.assertRaisesRegex(ValueError, 'link escapes'):
                splitter.split(source, splitter.digest(source), '7.1.1-47', root / 'rejected')
            self.assertFalse((root / 'outside').exists())

    def test_source_only_recipe_retains_code_and_original_notices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = f'ImageMagick-Windows-{splitter.WINDOWS_RECIPE_COMMIT}'
            source = root / 'recipe.tar.gz'
            import io
            with tarfile.open(source, 'w:gz') as archive:
                for name, data in [('Readme.md', b'original build instructions'), ('Build/nasm.exe', b'original BSD helper'),
                                   ('Installer/Inno/PathTool/PathTool.exe', b'optional installer helper')]:
                    item = tarfile.TarInfo(prefix + '/' + name); item.size = len(data)
                    archive.addfile(item, io.BytesIO(data))
            license = root / 'license'; license.write_bytes(b'original BSD terms')
            guide = root / 'guide'; guide.write_bytes(b'Build the pinned source snapshots')
            first = root / 'first'; first.mkdir()
            second = root / 'second'; second.mkdir()
            args = (source, splitter.digest(source), license, splitter.digest(license), guide, {'schema': 1})
            part = splitter.package_build_recipe(*args, first)
            self.assertEqual(part, splitter.package_build_recipe(*args, second))
            with tarfile.open(first / part['filename']) as archive:
                self.assertNotIn(prefix + '/Installer/Inno/PathTool/PathTool.exe', archive.getnames())
                self.assertEqual(archive.extractfile(prefix + '/Readme.md').read(), b'original build instructions')
                self.assertEqual(archive.extractfile(prefix + '/Build/nasm.exe').read(), b'original BSD helper')
                self.assertEqual(archive.extractfile(prefix + '/Build/nasm-LICENSE.txt').read(), b'original BSD terms')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                splitter.package_build_recipe(source, '0'*64, license, splitter.digest(license), guide, {}, root)

    def test_duplicate_component_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.make_source(root, True)
            (root / 'input/OptionalDependencies/raw').mkdir()
            with self.assertRaisesRegex(ValueError, 'Duplicate dependency'):
                splitter.source_groups(root / 'input')


if __name__ == '__main__':
    unittest.main()
