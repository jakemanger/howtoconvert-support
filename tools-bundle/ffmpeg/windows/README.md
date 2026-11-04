# Windows FFmpeg

These conversion tool downloads save you from finding or compiling FFmpeg and its libraries yourself. The preparation script assembles pinned MSYS2 packages into a portable folder containing FFmpeg, ffprobe, their required libraries and licenses.

With Python 3.14 and Git on Windows:

```sh
python tools-bundle/ffmpeg/windows/prepare.py --work C:\ffmpeg-build
```

The command verifies downloads and matching source recipes, checks the Windows library dependencies, runs conversion tests and creates `ffmpeg-8.1.1-windows_x86_64.tar.gz`. It repeats the tests after moving the folder. `--assemble-only` prepares an archive without claiming native verification.

`source-companion` contains the exact component sources, upstream `PKGBUILD` recipes, build-environment records, Rust crate sources and dependency locks. Rebuild a component in MSYS2 UCRT64 from its included recipe and recorded dependency versions with `makepkg-mingw -sCLf`. The prepared Rust locks retain the Windows dependency adjustment in the upstream recipe. This reproduces the selected inputs; it does not promise byte-identical compiler output.
