import importlib.util
import io
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('prepare', HERE / 'prepare.py')
prepare = importlib.util.module_from_spec(spec); spec.loader.exec_module(prepare)
verify_spec = importlib.util.spec_from_file_location('verify', HERE / 'verify.py')
verify = importlib.util.module_from_spec(verify_spec); verify_spec.loader.exec_module(verify)


def executable(normal=None, delayed=None):
    data = bytearray(4096); data[:2] = b'MZ'; struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'; struct.pack_into('<H', data, 0x86, 1)
    struct.pack_into('<H', data, 0x94, 240); optional = 0x98
    struct.pack_into('<H', data, optional, 0x20b); struct.pack_into('<Q', data, optional+24, 0x140000000)
    struct.pack_into('<IIII', data, optional+240+8, 2048, 0x1000, 2048, 0x200)
    if normal:
        struct.pack_into('<II', data, optional+112+8, 0x1100, 40)
        struct.pack_into('<I', data, 0x300+12, 0x1180)
        data[0x380:0x380+len(normal)+1] = normal.encode()+b'\0'
    if delayed:
        struct.pack_into('<II', data, optional+112+13*8, 0x1200, 64)
        struct.pack_into('<II', data, 0x400, 1, 0x1280)
        data[0x480:0x480+len(delayed)+1] = delayed.encode()+b'\0'
    return bytes(data)


def archive(file, contents):
    with tarfile.open(file, 'w:gz') as out:
        for name, data in contents.items():
            member = tarfile.TarInfo(name); member.size = len(data)
            out.addfile(member, io.BytesIO(data))


class PreparationTests(unittest.TestCase):
    def test_windows_environment_names_preserve_standalone_path_check(self):
        for key in ['SYSTEMROOT', 'SystemRoot', 'systemroot']:
            source = {key: r'C:\Windows', 'Path': r'C:\unrelated-tools', 'TEMP': r'C:\Temp',
                      'Pathext': '.COM;.EXE;.BAT;.CMD'}
            environment = verify.verification_environment(source, windows=True)
            self.assertEqual(environment['PATH'], r'C:\Windows\System32;C:\Windows')
            self.assertEqual(environment['TEMP'], r'C:\Temp')
            self.assertEqual(environment['PATHEXT'], '.COM;.EXE;.BAT;.CMD')
            self.assertNotIn('Path', environment)
            self.assertEqual(source['Path'], r'C:\unrelated-tools')

    def test_imports_include_delayed_libraries(self):
        self.assertEqual(prepare.pe_imports(executable('KERNEL32.dll', 'codec.dll')), ['codec.dll', 'kernel32.dll'])

    def test_only_reachable_payload_is_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); package = root/'package.tar.gz'; stage=root/'stage'
            archive(package, {'ucrt64/bin/ffmpeg.exe':executable('runtime.dll','late.dll'),
                              'ucrt64/bin/ffprobe.exe':executable('KERNEL32.dll'),
                              'ucrt64/bin/runtime.dll':executable(), 'ucrt64/bin/late.dll':executable(),
                              'ucrt64/bin/unrelated.exe':executable(), 'ucrt64/include/unneeded.h':b'header',
                              'ucrt64/share/licenses/runtime/LICENSE':b'license'})
            result=prepare.assemble([{'name':'runtime','filename':package.name}], {package.name:package}, stage)
            self.assertEqual(set(result['programsAndLibraries']),{'ffmpeg.exe','ffprobe.exe','runtime.dll','late.dll'})
            self.assertFalse((stage/'bin/unrelated.exe').exists());self.assertFalse((stage/'include').exists())
            self.assertTrue((stage/'share/licenses/runtime/LICENSE').is_file())

    def test_missing_non_system_import_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);package=root/'package.tar.gz'
            archive(package,{'ucrt64/bin/ffmpeg.exe':executable('missing.dll'),'ucrt64/bin/ffprobe.exe':executable()})
            with self.assertRaisesRegex(ValueError,'missing.dll'):
                prepare.assemble([{'name':'runtime','filename':package.name}],{package.name:package},root/'stage')

    def test_unsafe_archive_paths_are_rejected(self):
        for name in ['../outside','/absolute','C:/absolute','dir\\outside']:
            with self.assertRaises(ValueError):prepare.relative(name)

    def test_gnu_license_names_and_nested_notices_are_preserved(self):
        self.assertTrue(prepare.LICENSE_NAME.fullmatch('COPYING3.LIB'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);inner=root/'upstream.tar.gz';outer=root/'source.tar.gz'
            archive(inner,{'source/COPYING3':b'original license','source/NOTICE':b'copyright notice'})
            archive(outer,{'package/upstream.tar.gz':inner.read_bytes()});evidence=[]
            prepare.source_notices(outer,'component',root/'stage',evidence)
            self.assertEqual(len(evidence),2)
            self.assertEqual({(root/'stage'/e['file']).read_bytes() for e in evidence},{b'original license',b'copyright notice'})

    def test_archive_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);stage=root/'ffmpeg';stage.mkdir();(stage/'file.txt').write_text('fixed input')
            a=root/'a.tar.gz';b=root/'b.tar.gz';prepare.pack(stage,a);prepare.pack(stage,b)
            self.assertEqual(a.read_bytes(),b.read_bytes())


if __name__ == '__main__':unittest.main()
