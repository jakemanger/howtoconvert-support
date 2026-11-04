# Pandoc

Build Pandoc 3.6.2 with Lua and server support from pinned sources, using Stack 3.11.1, GHC 9.8.4 and LTS 23.0.

For Windows x64, use the [Windows build recipe](WINDOWS.md).

On macOS, install Python 3.12 or newer and the Xcode command-line tools, then run:

```sh
python3 build.py --work /path/to/build-directory
```

The compiler and build cache stay in that directory. The executable, license notices and checksums are written to `output/`; matching sources and build instructions are written to `source-companion/`.

Check document and ebook conversions (add `--pdf-engine /path/to/pdflatex` to check PDF output):

```sh
python3 test_conversions.py --pandoc /path/to/build-directory/output/pandoc --output /path/to/test-results
```

To download or verify the sources separately:

```sh
python3 collect-sources.py --output /path/to/sources --archive /path/to/pandoc-sources.tar.gz
python3 collect-sources.py --output /path/to/sources --verify-only
```

`sources.lock.json` pins archive hashes, exact Cabal revisions and toolchain versions. The source companion includes Pandoc, its dependencies and GHC's runtime sources, including GMP and libffi sources and patches. Preserve the accompanying notices and provide this source companion alongside the resulting executable.

Pandoc is GPL-2.0-or-later; this build selects GPL version 3. Dependencies retain their own licenses. The packaging scripts are MIT licensed.
