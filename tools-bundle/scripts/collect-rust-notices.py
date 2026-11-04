#!/usr/bin/env python3
"""Collect license notices for a verified set of redistributed Rust sources."""
import argparse
import gzip
import json
import runpy
import tarfile
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    shared = runpy.run_path(str(Path(__file__).resolve().parent / '../imagemagick/collect-notices.py'))
    supplements = json.loads(Path(__file__).with_name('rust-notices.lock.json').read_text())
    shared['SUPPLEMENTAL_NOTICES'].update(supplements)
    args.output.mkdir(parents=True, exist_ok=False)
    index = json.loads((args.sources / 'source-index.json').read_text())
    with tempfile.TemporaryDirectory(prefix='rust-notice-cache-') as temporary:
        collector = shared['Collector'](args.output, Path(temporary))
        seen = set()
        for crate in index['crates']:
            name = shared['safe'](crate['filename'])
            if len(name.parts) != 1 or name.name in seen:
                raise ValueError('Invalid or duplicate source filename')
            seen.add(name.name)
            data = shared['read_verified'](args.sources / 'crates' / str(name), crate['sha256'])
            collector.crate(data, name.stem, 'source-only', {'crateSha256': crate['sha256']})
        result = {'schema': 1, 'lockSha256': index['lockSha256'], 'crates': sorted(seen),
                  'notices': collector.records, 'gaps': collector.gaps}
        (args.output / 'index.json').write_text(json.dumps(result, indent=2) + '\n')
        if collector.gaps:
            raise ValueError(f'{len(collector.gaps)} unresolved source notice gaps')
    output = args.output.with_suffix('.tar.gz')
    with output.open('wb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for file in sorted(args.output.rglob('*')):
                    if not file.is_file():
                        continue
                    info = archive.gettarinfo(str(file), arcname=str(file.relative_to(args.output)))
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ''
                    info.mode = 0o644
                    with file.open('rb') as stream:
                        archive.addfile(info, stream)
    print(f'Collected {len(collector.records)} notices for {len(seen)} source crates')


if __name__ == '__main__':
    main()
