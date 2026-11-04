import hashlib
import importlib.util
import io
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('build', ROOT / 'build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class RecipeTests(unittest.TestCase):
    def test_portability_rejects_private_libraries_and_newer_macos(self):
        linked = 'pandoc:\n /usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n'
        commands = 'Load command 1\n cmd LC_BUILD_VERSION\n minos 11.0\n sdk 27.0\n'
        build.verify_portability(linked, commands, '11.0')
        with self.assertRaisesRegex(ValueError, 'Non-system runtime dependency'):
            build.verify_portability(linked.replace('/usr/lib/', '/opt/homebrew/lib/'), commands, '11.0')
        with self.assertRaisesRegex(ValueError, 'minimum macOS'):
            build.verify_portability(linked, commands.replace('minos 11.0', 'minos 27.0'), '11.0')

    def test_source_archive_excludes_unrelated_local_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.txt'
            source.write_text('approved source')
            (root / 'private-review.txt').write_text('not a release input')
            archive = root / 'sources.tar.gz'
            build.sources.package_sources(root, archive, [source])
            with tarfile.open(archive) as bundle:
                self.assertEqual(bundle.getnames(), ['pandoc-3.6.2-sources/source.txt'])

    def test_changed_download_is_rejected_and_partial_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            item = {'filename': 'source.tar.gz', 'url': 'https://example.invalid/source',
                    'size': 4, 'sha256': hashlib.sha256(b'good').hexdigest()}
            def download(args, **kwargs):
                Path(args[args.index('--output') + 1]).write_bytes(b'evil')
            with patch.object(build.sources.subprocess, 'run', download):
                with self.assertRaisesRegex(ValueError, 'Checksum or size mismatch'):
                    build.sources.fetch(item, root)
            self.assertEqual(list(root.iterdir()), [])

    def test_malformed_chunk_response_retries_without_appending_partial_bytes(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                self.send_response(200)
                if len(calls) == 1:
                    self.send_header('Transfer-Encoding', 'chunked'); self.end_headers()
                    self.wfile.write(b'4\r\nbad!\r\nZ\r\n'); self.close_connection = True
                else:
                    self.send_header('Content-Length', '4'); self.end_headers(); self.wfile.write(b'good')
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                item = {'filename': 'source.cabal', 'url': f'http://127.0.0.1:{server.server_port}/source',
                        'size': 4, 'sha256': hashlib.sha256(b'good').hexdigest()}
                (root / 'source.cabal.partial').write_bytes(b'old incomplete download')
                self.assertEqual(build.sources.fetch(item, root).read_bytes(), b'good')
                self.assertEqual(len(calls), 2)
                self.assertFalse((root / 'source.cabal.partial').exists())
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_transport_failure_has_three_retries_and_cleans_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            item = {'filename': 'source.cabal', 'url': 'https://example.invalid/source',
                    'size': 4, 'sha256': hashlib.sha256(b'good').hexdigest()}
            def fail(args, **kwargs):
                self.assertEqual(args[args.index('--retry') + 1], '3')
                self.assertIn('--retry-all-errors', args)
                self.assertEqual(args[args.index('--max-time') + 1], '300')
                self.assertNotIn('--continue-at', args)
                self.assertTrue(kwargs['check'])
                partial = Path(args[args.index('--output') + 1])
                self.assertFalse(partial.exists())
                partial.write_bytes(b'broken response')
                raise subprocess.CalledProcessError(56, args)
            with patch.object(build.sources.subprocess, 'run', fail):
                with self.assertRaises(subprocess.CalledProcessError): build.sources.fetch(item, root)
            self.assertEqual(list(root.iterdir()), [])

    def test_archive_cannot_write_outside_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'source.tar.gz'
            with tarfile.open(archive, 'w:gz') as bundle:
                entry = tarfile.TarInfo('../escaped')
                entry.size = 4
                bundle.addfile(entry, io.BytesIO(b'evil'))
            with self.assertRaises(tarfile.OutsideDestinationError):
                build.extract(archive, root / 'extract')
            self.assertFalse((root / 'escaped').exists())

    def test_new_dependency_version_requires_corresponding_sources(self):
        lock = {'packages': [{'name': 'example', 'version': '1'}],
                'compilerPackages': [], 'projectPackages': []}
        build.verify_coverage(lock, [{'name': 'example', 'version': '1'}])
        with self.assertRaisesRegex(ValueError, 'does not cover'):
            build.verify_coverage(lock, [{'name': 'example', 'version': '2'}])


if __name__ == '__main__':
    unittest.main()
