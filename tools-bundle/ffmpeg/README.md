# Build FFmpeg

Pinned video, audio, image and subtitle codecs for macOS 14 or newer.
Building requires macOS 15+, Xcode, Node.js 20+, Python 3.12+ and Rust 1.92.0.

```sh
rustup toolchain install 1.92.0 --profile minimal --component rust-src,rust-docs
node tools-bundle/ffmpeg/build.mjs --arch arm64 --work .ffmpeg-work --out dist/ffmpeg
```

For Intel, add the Rust `x86_64-apple-darwin` target and use `--arch x86_64`.
Build sequentially when sharing a work directory. An existing patched xcpkg
0.30.0 can be supplied with `--xcpkg /path/to/xcpkg`; see the adjacent
ImageMagick instructions for the pinned build-tool patches.

Collect the matching sources and notices, then package the release:

```sh
python3 tools-bundle/ffmpeg/collect-sources.py \
  --work .ffmpeg-work --out dist/ffmpeg-sources \
  --xcpkg .ffmpeg-work/bootstrap/bin/xcpkg
RUSTUP_TOOLCHAIN=1.92.0 python3 tools-bundle/imagemagick/collect-notices.py \
  --sources dist/ffmpeg-sources --out dist/ffmpeg-notices
python3 tools-bundle/ffmpeg/prepare-release.py \
  --archive dist/ffmpeg/ffmpeg-8.1.1-MacOSX-14.0-arm64.tar.xz \
  --sources dist/ffmpeg-sources --notices dist/ffmpeg-notices \
  --out dist/ffmpeg-release
```

Extract the release archive and run `./ffmpeg -i input.mp4 output.webm`.
The `ffmpeg`, `ffprobe` and `ffplay` launchers select the included libraries.
Verify the packaged codecs with actual encode/decode checks:

```sh
node tools-bundle/ffmpeg/verify.mjs /path/to/extracted-package
```

Publish the source archives from `dist/ffmpeg-sources` and the build-source
archive alongside the binaries. The package retains the GPL and dependency
license notices.
