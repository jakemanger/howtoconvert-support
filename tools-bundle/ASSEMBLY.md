# Build and release

The scripts repackage checksum-pinned upstream binaries. Use Node.js 20+, tar,
unzip, xz and 7-Zip. MSI extraction needs `msiexec` (Windows) or `msitools`;
`.deb` extraction needs `dpkg-deb` or BSD tar. The working filesystem must support
Unix permissions and symlinks.

```sh
git clone --branch tools-bundle https://github.com/jakemanger/howtoconvert-support.git
cd howtoconvert-support
node tools-bundle/scripts/build-bundle.mjs --out dist --work .bundle-work
```

To reproduce a published bundle, clone its `tools-YYYY.MM.N` tag instead.
The pinned versions and checksums are in [bundle.config.json](bundle.config.json).

## Packaging

| Input | Output |
| --- | --- |
| ZIP, 7z or TinyTeX archive | Tool directory with its libraries and notices |
| ImageMagick AppImage | Extracted files and a launcher that works without FUSE |
| LibreOffice MSI or `.deb` | Extracted LibreOffice program tree |
| LibreOffice DMG | Unchanged upstream disk image |

`--platform mac_arm64` selects a platform; `--only ffmpeg,pandoc` selects tools.
`--reuse` checks existing archives and their notices. `--skip-sources` produces
local drafts, which cannot pass release verification.

## Sources and licenses

Retain upstream copyright, attribution and license notices, including those for
bundled libraries and TeX packages. Include matching source, patches and build
instructions wherever the component’s license requires them. A project homepage
or a different source version is insufficient.

Each platform’s `sourceBundle` references an attached source entry with `sha256`,
`includesBuildInstructions: true` and the binary’s upstream hash in
`binaryArchives`. Review the actual contents before recording this mapping.
The scripts check mappings and integrity; they cannot establish legal compliance.
`node tools-bundle/scripts/build-bundle.mjs --check` checks these mappings without downloads.

## Release

Set `TOOLS_MANIFEST_SIGNING_KEY` and `TOOLS_MANIFEST_PUBLIC_KEY` securely, then:

```sh
node tools-bundle/scripts/sign-manifest.mjs dist
node tools-bundle/scripts/stage-release.mjs dist release
```

Staging verifies the signature and archive hashes, then copies only named release
files into a new `release/` directory. Publish that directory under the configured
bundle tag. The manual workflow does this and creates a draft tied to its build
commit. Keep existing release assets unchanged. Repacked archive timestamps may
differ between builds; output hashes are recorded in `manifest.json`.
