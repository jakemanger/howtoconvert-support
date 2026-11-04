# Build and release

The scripts prepare checksum-pinned conversion tools and matching sources. Use
Node.js 20+, tar, unzip, xz and 7-Zip. `.deb` extraction needs `dpkg-deb` or BSD tar.
The working filesystem must support Unix permissions and symlinks.

```sh
git clone --branch tools-bundle https://github.com/jakemanger/howtoconvert-support.git
cd howtoconvert-support
node tools-bundle/scripts/build-bundle.mjs --out dist --work .bundle-work \
  --platform mac_arm64,windows_x86_64
```

To reproduce a published release, clone its `tools-YYYY.MM.N` tag instead.
The pinned versions and checksums are in [bundle.config.json](bundle.config.json).
Source build instructions: [FFmpeg](ffmpeg/README.md),
[ImageMagick](imagemagick/README.md), [Pandoc](pandoc/README.md),
[TeX](tex/README.md).

## Packaging

| Input | Output |
| --- | --- |
| ZIP, 7z or TinyTeX archive | Tool directory with its libraries and notices |
| ImageMagick AppImage | Extracted files and a launcher that works without FUSE |
| Original Windows ImageMagick EXE or LibreOffice MSI | Direct download recorded in the signed manifest |
| LibreOffice `.deb` | Extracted LibreOffice program tree |
| LibreOffice DMG | Unchanged upstream disk image |

Windows ImageMagick and LibreOffice use unchanged original installers. Their
download URLs and checksums are signed in the manifest; the installers are not
copied into the hosted release. Matching sources are separate release downloads
listed in `SOURCES.md`.

`--platform mac_arm64,windows_x86_64` selects platforms; `--only ffmpeg,pandoc` selects tools.
`--reuse` checks existing archives and their notices. `--skip-sources` produces
local drafts, which cannot pass release verification.
For locally collected source archives, add `--source-dir PATH` (repeatable).
Expected filenames and checksums are verified before inclusion.

## First-release inputs

For binaries built with the linked source recipes, stage their archives and source
companions before assembling the first release:

```sh
node tools-bundle/scripts/input-cache.mjs stage \
  --url https://downloads.example.com/tool-inputs/ \
  --input-dir build-output --out input-cache \
  --platform mac_arm64,windows_x86_64
```

This verifies the configured hashes and source mappings, then writes only required
files to `input-cache/objects/`, named by SHA-256. Upload those objects to the given
URL without changing existing objects. Review and use the generated
`input-cache/bundle.config.json`; its pinned URLs let later builds fetch the same
inputs. `inputs.json` lists exactly what was staged. `--input-dir`, `--platform`
and `--only` work as above.

The manual workflow accepts this URL as `input_cache_url`. Its `platforms` input
accepts comma-separated names from the configuration, or `all`; the default is
`mac_arm64,windows_x86_64`. Only selected platforms are checked, fetched and built.
Every selected tool still needs complete source mappings, verified hashes and a
signed manifest before release staging.

## Sources and licenses

Retain upstream copyright, attribution and license notices, including those for
included libraries and TeX packages. Include matching source, patches and build
instructions wherever the component’s license requires them. A project homepage
or a different source version is insufficient.

Each platform’s `sourceBundle` names one source entry or a list of smaller source
archives. Each must have `attach: true`, `sha256` and the binary’s upstream hash
in `binaryArchives`; at least one must set `includesBuildInstructions: true`. Review the actual contents before recording this mapping.
The scripts check mappings and integrity; they cannot establish legal compliance.
`node tools-bundle/scripts/build-bundle.mjs --check --platform mac_arm64,windows_x86_64`
checks these mappings without downloads.

## Release

Set `TOOLS_MANIFEST_SIGNING_KEY` and `TOOLS_MANIFEST_PUBLIC_KEY` securely, then:

```sh
node tools-bundle/scripts/sign-manifest.mjs dist
node tools-bundle/scripts/stage-release.mjs dist release \
  --vendor-dir .bundle-work/downloads
```

Staging verifies the signature and archive hashes, then copies only named release
files into a new `release/` directory. Publish that directory under the configured
release tag. The manual workflow does this and creates a draft tied to its build
commit. Keep existing release assets unchanged. Repacked archive timestamps may
differ between builds; output hashes are recorded in `manifest.json`.
