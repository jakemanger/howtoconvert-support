#!/usr/bin/env python3
"""Preserve TeX engine and package notices beside unchanged upstream binaries."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('source_collector', HERE / 'collect-sources.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
NOTICE = re.compile(r'(?i)(copyright|copyrite|copying|licen[sc]|notice|legal|authors|readme|^ofl(?:[-.]|$)|^lppl(?:[-.]|$)|^gpl(?:[v.-]|$)|^lgpl(?:[v.-]|$)|^ftl[.]|^apache|^mit[.])')
ENGINE_COMPONENTS = {'libs-freetype2','libs-graphite2','libs-harfbuzz','libs-icu','libs-libpng','libs-lua53','libs-pplib','libs-teckit','libs-xpdf','libs-zlib','libs-zziplib','texk-kpathsea','texk-web2c','texk-dvipsk','texk-dvipdfm-x'}
HEADER_FILES = {'texk/web2c/bibtex.web','texk/web2c/tex.web','texk/web2c/mf.web','texk/web2c/gftodvi.web','texk/web2c/gftopk.web','texk/web2c/gftype.web','texk/web2c/mft.web','texk/web2c/pktogf.web','texk/web2c/pktype.web','texk/dvipsk/afm2tfm.c','texk/dvipsk/dvips.h'}


def pinned(file, item):
    if file.is_symlink() or not file.is_file() or file.stat().st_size != item['size'] or collector.digest(file) != item['sha256']:
        raise ValueError(f'Archive checksum or size mismatch: {file.name}')


def write_notice(root, relative, data, records):
    collector.safe_name(relative)
    target = root / relative
    sha = hashlib.sha256(data).hexdigest()
    if target.exists():
        if collector.digest(target) != sha:
            raise ValueError(f'Conflicting notice: {relative}')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    records.append({'path': relative, 'sha256': sha, 'size': len(data)})


def notices(sources, index, destination):
    records, coverage = [], {}
    for part in index['components']:
        if part['component'] not in ENGINE_COMPONENTS:
            continue
        source = sources / collector.plain(part['filename']);pinned(source,part)
        count = 0
        with tarfile.open(source) as archive:
            for member in archive:
                if member.isfile() and (NOTICE.search(PurePosixPath(member.name).name) or member.name in HEADER_FILES):
                    data = archive.extractfile(member).read()
                    # Complete original files are retained, never abbreviated
                    # legal headers or generated summaries of license terms.
                    write_notice(destination, f"engines/{part['treeSha256'][:16]}/{member.name}", data, records)
                    count += 1
        coverage[part['component']] = coverage.get(part['component'],0)+count
    if any(not coverage.get(component) for component in ENGINE_COMPONENTS):
        raise ValueError('A bundled engine or library has no preserved notices')
    for part in index['packages']:
        source=sources/collector.plain(part['filename']);pinned(source,part)
        if part['kind'] == 'font-source':
            with zipfile.ZipFile(source) as archive:
                for name in archive.namelist():
                    if not name.endswith('/') and NOTICE.search(PurePosixPath(name).name):
                        write_notice(destination,'fonts/'+name,archive.read(name),records)
            continue
        if part['kind'] != 'package-documentation':
            continue
        with tarfile.open(source) as archive:
            for member in archive:
                if member.isfile() and NOTICE.search(PurePosixPath(member.name).name):
                    write_notice(destination,'packages/'+member.name,archive.extractfile(member).read(),records)
    (destination/'index.json').write_text(json.dumps(records,indent=2)+'\n')
    return records


def pack(root, members, target):
    def normalize(info):
        info.uid=info.gid=info.mtime=0;info.uname=info.gname='';info.pax_headers={}
        return info
    with target.open('wb') as stream,gzip.GzipFile(fileobj=stream,mode='wb',filename='',mtime=0) as compressed:
        with tarfile.open(fileobj=compressed,mode='w') as archive:
            for member in members:
                archive.add(root/member,arcname=member,filter=normalize,recursive=True)


def prepare(runtime,sources,output):
    if output.exists() or output.is_symlink():
        raise ValueError('Output already exists')
    lock=json.loads((sources/'source-lock.json').read_text())
    index=json.loads((sources/'source-index.json').read_text())
    pinned(runtime,lock['runtime'])
    for part in index['components']+index['packages']:
        pinned(sources/collector.plain(part['filename']),part)
    output.mkdir(parents=True)
    try:
        with tempfile.TemporaryDirectory(prefix='tex-runtime-',dir=output) as temporary:
            work=Path(temporary)
            with tarfile.open(runtime) as archive:
                if {collector.safe_name(m.name).parts[0] for m in archive} != {lock['runtime']['root']}:
                    raise ValueError('Unexpected runtime root')
                archive.extractall(work,filter='data')
            root=work/lock['runtime']['root']
            for item in lock['macBinaries']:
                binary=root/'bin/universal-darwin'/collector.plain(item['name'])
                if collector.digest(binary) != item['sha256']:
                    raise ValueError(f"Upstream executable changed: {item['name']}")
            # Scripts, macros, METAFONT programs and original font files are
            # their supplied source form. Preserve them separately from engines.
            native={f"bin/universal-darwin/{item['name']}" for item in lock['macBinaries']}
            name='tex-2026.10-runtime-sources.tar.gz'
            with (output/name).open('wb') as stream,gzip.GzipFile(fileobj=stream,mode='wb',filename='',mtime=0) as compressed:
                with tarfile.open(fileobj=compressed,mode='w') as destination:
                    for file in sorted(root.rglob('*')):
                        relative=file.relative_to(root).as_posix()
                        if relative in native or relative.startswith('texmf-var/') or file.is_dir():
                            continue
                        info=destination.gettarinfo(str(file),arcname=relative)
                        info.uid=info.gid=info.mtime=0;info.uname=info.gname='';info.pax_headers={}
                        if info.isfile():
                            with file.open('rb') as data: destination.addfile(info,data)
                        else: destination.addfile(info)
            runtime_source={'filename':name,'sha256':collector.digest(output/name),'size':(output/name).stat().st_size,'kind':'runtime-source','package':'texlive-runtime'}
            records=notices(sources,index,root/'licenses')
            shutil.copyfile(HERE/'NOTICE',root/'licenses/NOTICE')
            shutil.copyfile(HERE/'APACHE-2.0.txt',root/'licenses/APACHE-2.0.txt')
            name='tex-2026.10-darwin-with-notices.tar.gz';pack(work,[root.name],output/name)
            binary={'filename':name,'sha256':collector.digest(output/name),'size':(output/name).stat().st_size,'root':root.name,'bin':'bin/universal-darwin/pdflatex'}
        with tempfile.TemporaryDirectory(prefix='tex-build-',dir=output) as temporary:
            work=Path(temporary)
            for name in ('collect-sources.py','restore-source.py','prepare-release.py','sources.lock.json','README.md','NOTICE','APACHE-2.0.txt','test-sources.py'):
                shutil.copyfile(HERE/name,work/name)
            for file in sources.glob('texlive-*-files.json'):
                shutil.copyfile(file,work/file.name)
            final_index=json.loads(json.dumps(index))
            final_index['packages'].append(runtime_source)
            (work/'source-index.json').write_text(json.dumps(final_index,indent=2)+'\n')
            shutil.copytree(sources/'mac-build',work/'mac-build')
            name='tex-2026.10-build-sources.tar.gz';pack(work,sorted(x.name for x in work.iterdir()),output/name)
            companion={'filename':name,'sha256':collector.digest(output/name),'size':(output/name).stat().st_size,'includesBuildInstructions':True}
        result={'binary':binary,'buildSources':companion,'runtimeSources':runtime_source,'noticeFiles':len(records),'sourceParts':len(index['components'])+len(index['packages'])+2}
        (output/'artifacts.json').write_text(json.dumps(result,indent=2)+'\n')
        return result
    except Exception:
        shutil.rmtree(output)
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--sources',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(args.runtime,args.sources,args.out),indent=2))
