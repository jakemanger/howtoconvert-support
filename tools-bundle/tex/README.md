# TeX tools

Pinned TeX Live engines, fonts and packages for PDF typesetting. Python 3.12+
collects their matching sources and retains the original notices.

```sh
python3 collect-sources.py --cache downloads --download --out sources
python3 restore-source.py sources svn78081 source-tree
```

The smaller component archives reconstruct each complete upstream source tree,
including the required libraries, patches and build scripts. Every restored file is
checked against its recorded hash. For release downloads, extract
`source-tex-2026.10-build-sources.tar.gz` beside the other source archives and run
`restore-source.py` there. Both original filenames and the release's `source-`
prefix are accepted.

Build a restored tree with `./Build`; see its `README.1prerequisites` and
`README.2building`. The original Mac instructions and universal-binary scripts
are in `sources/mac-build/`. `sources.lock.json` maps each supplied Mac executable
to its source revision. Developer signing identities are not needed to rebuild
locally; upstream signatures cannot be reproduced.

To retain notices with the pinned, unchanged Mac binaries:

```sh
python3 prepare-release.py --runtime downloads/TinyTeX-1-darwin-v2026.10.tar.xz \
  --sources sources --out release
```

The result includes a runtime archive and a build-source companion. Distribute
that companion with the component, package and font archives listed in
`sources/source-index.json`. Licenses differ by component; retain their original
copyright, attribution and license files.
