# Build ImageMagick

For Windows, see [the original installer and source instructions](WINDOWS.md).

## macOS

Portable ImageMagick with LibRaw for camera RAW files. Ghostscript is excluded;
applications must supply their own PDF renderer.

On an Apple Silicon Mac with macOS 15+, Xcode, Node.js 20+, Python 3.12+ and Rust 1.92.0:

```sh
rustup toolchain install 1.92.0 --profile minimal
rustup component add rust-src --toolchain 1.92.0
node tools-bundle/imagemagick/build.mjs --arch arm64
node tools-bundle/imagemagick/build.mjs --arch x86_64
```

The build uses the versions and checksums pinned in `build.lock.json` and the
recipe files. `--work` selects the build directory; `--out` selects the output
directory. Build the architectures sequentially when sharing a work directory.

Use `--xcpkg /path/to/xcpkg` for an existing xcpkg 0.30.0 installation. When using
Apple's Command Line Tools, apply `xcpkg-command-line-tools.patch` to the pinned
xcpkg source before building it with its documented CMake dependencies.

Collect the matching sources and notices, then package the release:

```sh
python3 tools-bundle/imagemagick/collect-sources.py \
  --work .imagemagick-work --out dist/imagemagick-sources \
  --xcpkg .imagemagick-work/bootstrap/bin/xcpkg
RUSTUP_TOOLCHAIN=1.92.0 python3 tools-bundle/imagemagick/collect-notices.py \
  --sources dist/imagemagick-sources --out dist/imagemagick-notices
python3 tools-bundle/imagemagick/prepare-release.py \
  --archive dist/imagemagick/imagemagick-7.1.2-35-MacOSX-14.0-arm64.tar.xz \
  --archive dist/imagemagick/imagemagick-7.1.2-35-MacOSX-14.0-x86_64.tar.xz \
  --sources dist/imagemagick-sources --notices dist/imagemagick-notices \
  --out dist/imagemagick-release
```

The release packages require macOS 14+. Extract one and run `./magick input.png output.webp`.
The launcher selects the included libraries and decoder modules.

Publish the source archives from `dist/imagemagick-sources` and the build-source
archive alongside the binaries. The packages retain upstream license and copyright
notices, including those for GPL/LGPL codec libraries.
