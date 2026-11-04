import hashlib
import importlib.util
import io
import struct
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

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
    def test_source_cache_preserves_pins_and_rejects_changed_or_html_responses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data = b'pinned source archive'
            item = {'filename': 'source.tar.xz', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                    'url': 'https://upstream.example/source.tar.xz'}
            base = 'https://cache.example/sources/'
            with patch.object(prepare.urllib.request, 'urlopen', return_value=io.BytesIO(data)) as request:
                result = prepare.fetch(item, root / 'good', base)
                self.assertEqual(result.read_bytes(), data)
                request.assert_called_once()
                sent = request.call_args.args[0]
                self.assertEqual(sent.full_url, base + item['sha256'])
                self.assertEqual(sent.get_header('User-agent'), 'HowToConvert-Tools/1.0')
                self.assertEqual(request.call_args.kwargs, {'timeout': 90})
            with patch.object(prepare.urllib.request, 'urlopen', return_value=io.BytesIO(data)) as request:
                self.assertEqual(prepare.fetch(item, root / 'upstream').read_bytes(), data)
                self.assertEqual(request.call_args.args[0].full_url, item['url'])
                self.assertEqual(request.call_args.args[0].get_header('User-agent'), 'HowToConvert-Tools/1.0')
            with patch.object(prepare.urllib.request, 'urlopen', return_value=io.BytesIO(b'<html>Access challenge</html>')):
                with self.assertRaisesRegex(ValueError, 'Checksum or size mismatch'):
                    prepare.fetch(item, root / 'bad', base)
            self.assertFalse((root / 'bad' / item['filename']).exists())
            for value in ('http://cache.example/', 'https://user:pass@cache.example/',
                          'https://cache.example/?token=secret', 'https://cache.example/#part', 'file:///tmp/'):
                with self.assertRaisesRegex(ValueError, 'HTTPS base URL'):
                    prepare.input_cache_base(value)

    def test_transient_download_retries_clean_partial_files_before_backoff(self):
        class InterruptedResponse(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise TimeoutError('read timed out')
                return super().read(size)
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary); data = b'verified source'
            item = {'filename': 'source.tar.xz', 'url': 'https://example.test/source',
                    'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            busy = prepare.urllib.error.HTTPError(item['url'], 503, 'temporarily unavailable', {}, None)
            responses = [InterruptedResponse(b'partial download'), busy, io.BytesIO(data)]
            def backoff(seconds):
                self.assertEqual(list(cache.iterdir()), [], 'partial input must be removed before retrying')
            with patch.object(prepare.urllib.request, 'urlopen', side_effect=responses) as request, \
                    patch.object(prepare.time, 'sleep', side_effect=backoff) as sleep:
                self.assertEqual(prepare.fetch(item, cache).read_bytes(), data)
                self.assertEqual(request.call_count, 3)
                self.assertEqual(sleep.call_args_list, [call(1), call(2)])
            self.assertEqual([p.name for p in cache.iterdir()], ['source.tar.xz'])

    def test_download_retry_limit_is_three_attempts(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            item = {'filename': 'source.tar.xz', 'url': 'https://example.test/source', 'size': 1, 'sha256': '0' * 64}
            with patch.object(prepare.urllib.request, 'urlopen', side_effect=prepare.urllib.error.URLError(TimeoutError('timeout'))) as request, \
                    patch.object(prepare.time, 'sleep') as sleep:
                with self.assertRaises(prepare.urllib.error.URLError):
                    prepare.fetch(item, cache)
                self.assertEqual(request.call_count, 3)
                self.assertEqual(sleep.call_args_list, [call(1), call(2)])
            self.assertEqual(list(cache.iterdir()), [])

    def test_only_transient_http_and_connection_errors_retry(self):
        for status in (408, 429, 500, 502, 503, 599):
            with prepare.urllib.error.HTTPError('https://example.test', status, 'error', {}, None) as error:
                self.assertTrue(prepare.transient_download_error(error))
        for status in (400, 401, 403, 404, 410, 600):
            with prepare.urllib.error.HTTPError('https://example.test', status, 'error', {}, None) as error:
                self.assertFalse(prepare.transient_download_error(error))
        for error in (TimeoutError(), ConnectionResetError(), prepare.http.client.IncompleteRead(b'partial'), prepare.ssl.SSLEOFError()):
            self.assertTrue(prepare.transient_download_error(error))
            self.assertTrue(prepare.transient_download_error(prepare.urllib.error.URLError(error)))
        for error in (ValueError('invalid URL'), PermissionError('file write'), prepare.ssl.SSLCertVerificationError('certificate')):
            self.assertFalse(prepare.transient_download_error(error))
            self.assertFalse(prepare.transient_download_error(prepare.urllib.error.URLError(error)))

    def test_checksum_size_url_and_permanent_http_failures_never_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data = b'correct'
            item = {'filename': 'source.tar.xz', 'url': 'https://example.test/source',
                    'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            for index, response in enumerate((b'changed', b'short', b'<html>Access denied</html>')):
                cache = root / str(index)
                with patch.object(prepare.urllib.request, 'urlopen', return_value=io.BytesIO(response)) as request, \
                        patch.object(prepare.time, 'sleep') as sleep:
                    with self.assertRaisesRegex(ValueError, 'Checksum or size mismatch'):
                        prepare.fetch(item, cache)
                    request.assert_called_once(); sleep.assert_not_called()
                self.assertEqual(list(cache.iterdir()), [])
            for index, error in enumerate((prepare.urllib.error.HTTPError(item['url'], 403, 'forbidden', {}, None),
                                           prepare.urllib.error.URLError(ValueError('invalid URL')),
                                           prepare.urllib.error.URLError(prepare.ssl.SSLCertVerificationError('certificate')))):
                with patch.object(prepare.urllib.request, 'urlopen', side_effect=error) as request, patch.object(prepare.time, 'sleep') as sleep:
                    with self.assertRaises(prepare.urllib.error.URLError):
                        prepare.fetch(item, root / ('error-' + str(index)))
                    request.assert_called_once(); sleep.assert_not_called()
            with patch.object(prepare.urllib.request, 'urlopen') as request, patch.object(prepare.time, 'sleep') as sleep:
                with self.assertRaises(ValueError):
                    prepare.fetch(item, root / 'invalid-url', 'http://invalid.example/')
                request.assert_not_called(); sleep.assert_not_called()

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

    def test_paper_size_definitions_are_retained_with_the_library(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); package = root / 'native.tar.gz'; sizes = b'A4,210,297,mm\nLetter,8.5,11,in\n'
            archive(package, {'ucrt64/bin/sample.exe': executable('libpaper-2.dll'),
                              'ucrt64/bin/libpaper-2.dll': executable('KERNEL32.dll'),
                              'ucrt64/etc/paperspecs': sizes,
                              'ucrt64/etc/unrelated.conf': b'not a required runtime resource'})
            with patch.object(prepare, 'ENGINES', ('sample',)), patch.object(prepare, 'WRAPPERS', ()):
                prepare.assemble_native([{'name': 'native', 'filename': package.name}], {package.name: package}, root / 'output')
            self.assertEqual((root / 'output/etc/paperspecs').read_bytes(), sizes)
            self.assertFalse((root / 'output/etc/unrelated.conf').exists())

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
