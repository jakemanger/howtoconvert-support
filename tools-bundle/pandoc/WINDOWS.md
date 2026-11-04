# Windows x64

Build Pandoc 3.6.2 with Lua and server support. Install Python 3.12 or newer, then run in PowerShell:

```powershell
python windows.py --work C:\htc-pandoc
```

The recipe downloads hash-pinned Stack, GHC and MSYS2 tools into the build directory. It checks dependency sources before compiling, rejects non-system DLL dependencies, and tests document and ebook conversions. The resulting tool archive is `pandoc-3.6.2-windows_x86_64.tar.gz`; matching source archives and build recipes are in `source-companion/`.

Collect sources on any platform without building:

```sh
python3 windows.py --work /path/to/build-directory --collect-only
```

Use `--source-cache /path/to/existing/source-companion` to reuse verified downloads. The Windows lock extends the shared source lock with zlib, Win32 and GHC's matching Windows C-runtime sources. Preserve the accompanying notices and distribute those sources alongside the executable.
