import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('release', Path(__file__).with_name('prepare-release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_missing_or_changed_notices_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'license').write_text('notice')
            index = {'schema': 1, 'gaps': [], 'notices': [{'scope': 'binary', 'path': 'license', 'sha256': release.digest(root / 'license')}]}
            (root / 'index.json').write_text(json.dumps(index))
            release.verified_notices(root)
            (root / 'license').write_text('changed')
            with self.assertRaises(ValueError):
                release.verified_notices(root)

    def test_source_paths_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('../license', '/etc/passwd'):
                with self.assertRaises(ValueError):
                    release.relative_file(Path(tmp), name)

    def test_archive_links_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'unsafe.tar.xz'
            with tarfile.open(archive, 'w:xz') as stream:
                member = tarfile.TarInfo('tool/escape')
                member.type = tarfile.SYMTYPE
                member.linkname = '../../escape'
                stream.addfile(member)
            with self.assertRaises(tarfile.FilterError):
                release.prepare_binary(archive, root, {'notices': []}, root)

    def test_runtime_files_and_notices_survive_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'tool'
            files = {'bin/magick': b'executable', '.xcpkg/RECEIPT.yml': b'build-on: host', '.xcpkg/config.log': b'build log',
                     '.xcpkg/dependencies/lib/codec.dylib': b'codec',
                     'etc/ImageMagick-7/delegates.xml': b'<delegate command="/build/uppm/installed/coreutils/bin/mv x y"/>'}
            for name, data in files.items():
                file = stage / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(data)
            archive = root / 'tool.tar.xz'
            release.archive_tree(stage, archive, 'tool', 'xz')
            notices = root / 'notices'
            notices.mkdir()
            (notices / 'LICENSE').write_text('original copyright and license')
            index = {'schema': 1, 'gaps': [], 'notices': [{'scope': 'binary', 'path': 'LICENSE', 'sha256': release.digest(notices / 'LICENSE')}]}
            output = root / 'out'
            output.mkdir()
            result = release.prepare_binary(archive, notices, index, output)
            with tarfile.open(output / result['filename']) as stream:
                names = stream.getnames()
                self.assertNotIn('tool/.xcpkg/RECEIPT.yml', names)
                self.assertNotIn('tool/.xcpkg/config.log', names)
                self.assertEqual(stream.extractfile('tool/bin/magick').read(), b'executable')
                self.assertEqual(stream.extractfile('tool/.xcpkg/dependencies/lib/codec.dylib').read(), b'codec')
                self.assertEqual(stream.extractfile('tool/HTC-LICENSES/components/LICENSE').read(), b'original copyright and license')
                self.assertIn(b'/bin/mv', stream.extractfile('tool/etc/ImageMagick-7/delegates.xml').read())


if __name__ == '__main__':
    unittest.main()
