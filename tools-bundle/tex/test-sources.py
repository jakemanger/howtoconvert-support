import gzip
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

collect = load('collect_sources', 'collect-sources.py')
restore = load('restore_source', 'restore-source.py')


def archive(file, malicious=None, change=False):
    with tarfile.open(file, 'w:gz') as out:
        for name in ('tree', 'tree/libs', 'tree/libs/lua', 'tree/texk'):
            info = tarfile.TarInfo(name); info.type = tarfile.DIRTYPE; info.mode = 0o755
            out.addfile(info)
        for name, data in [('tree/Build', b'#!/bin/sh\n'), ('tree/libs/lua/COPYRIGHT', b'original license'), ('tree/texk/README', b'changed' if change else b'source')]:
            info = tarfile.TarInfo(name); info.mode = 0o755 if name.endswith('Build') else 0o644
            info.size = len(data); out.addfile(info, io.BytesIO(data))
        if malicious:
            info = tarfile.TarInfo(malicious); info.size = 1; out.addfile(info, io.BytesIO(b'x'))
        info = tarfile.TarInfo('tree/libs/lua/license-link'); info.type = tarfile.SYMTYPE; info.linkname = 'COPYRIGHT'; info.mode=0o777
        out.addfile(info)


class SourceTests(unittest.TestCase):
    def test_components_deduplicate_and_restore_every_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary); out=base/'parts';out.mkdir();known={};engines=[]
            for revision, changed in [('svn1',False),('svn2',True)]:
                file=base/(revision+'.tar.gz');archive(file,change=changed)
                result=collect.split_engine(file,out,known)
                manifest=out/(revision+'.json');manifest.write_text(json.dumps(result['files']))
                engines.append({'revision':revision,'parts':result['parts'],'manifest':manifest.name,'manifestSha256':collect.digest(manifest)})
            self.assertEqual(len(known),3)
            self.assertEqual(len(engines[0]['parts']),2)
            (out/'source-index.json').write_text(json.dumps({'engines':engines}))
            self.assertEqual(restore.restore(out,'svn1',base/'restored'),7)
            self.assertEqual((base/'restored/libs/lua/COPYRIGHT').read_bytes(),b'original license')
            self.assertTrue((base/'restored/libs/lua/license-link').is_symlink())
            for part in known.values():
                source=out/part['filename']; source.rename(out/('source-'+part['filename']))
            self.assertEqual(restore.restore(out,'svn1',base/'release-restored'),7)
            for part in known.values():
                source=out/('source-'+part['filename']); source.rename(out/part['filename'])
            self.assertEqual((base/'restored/Build').stat().st_mode&0o777,0o755)
            with self.assertRaisesRegex(ValueError,'already exists'): restore.restore(out,'svn1',base/'restored')
            part=out/engines[0]['parts'][0]['filename'];part.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'checksum'): restore.restore(out,'svn1',base/'changed')
            self.assertFalse((base/'changed').exists())

    def test_unsafe_paths_and_input_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);file=base/'source.tar.gz';out=base/'parts';out.mkdir()
            archive(file,malicious='tree/../escape')
            with self.assertRaisesRegex(ValueError,'Unsafe'):collect.split_engine(file,out,{})
            item={'filename':file.name,'size':file.stat().st_size,'sha256':collect.digest(file)}
            self.assertEqual(collect.locate(item,[base]),file)
            item['sha256']='0'*64
            with self.assertRaisesRegex(ValueError,'mismatch'):collect.locate(item,[base])
            file.unlink();file.symlink_to('absent')
            with self.assertRaisesRegex(ValueError,'regular file'):collect.locate(item,[base])

    def test_directory_escaping_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);file=base/'source.tar.gz';out=base/'parts';out.mkdir()
            with tarfile.open(file,'w:gz') as t:
                info=tarfile.TarInfo('tree/link');info.type=tarfile.SYMTYPE;info.linkname='../../outside';t.addfile(info)
            with self.assertRaisesRegex(ValueError,'escapes'):collect.split_engine(file,out,{})


if __name__=='__main__':unittest.main()
