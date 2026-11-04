# Windows TeX tools

Prepare a portable TeX runtime with pinned packages and fonts. The native
programs come from MSYS2; their matching source archives include the build
recipes and patches.

Requires Windows 10 or later, Python 3.14, 7-Zip, Git and GCC 14.2.0
(MinGW-w64 12, x64 UCRT). The Windows CI runner already includes this compiler:

```powershell
python prepare.py --work C:\tex-tools
```

Only the small script-launcher DLL is rebuilt to handle paths containing spaces.
Its original source, patch, build command and compiler details accompany the output;
the TeX engines remain unchanged. No extra runtime installer is required.

The script checks every input, keeps the required native libraries, regenerates
TeX formats and tests PDF creation, fonts, EPS conversion and installation paths
containing spaces. It produces a runtime archive, `artifact.json`,
`verification.json` and `source-companion/`. It does not install system software. Original notices and license texts are
retained in `share/licenses/`.

If the historical TeX source mirror is unavailable, add `--input-cache-url URL`
for a mirror whose filenames are the source SHA-256 values. The same pinned
sizes and hashes remain required.

Distribute the source companion and its individual archives alongside the
runtime. It includes original licenses, source files, build metadata and the
packaging recipe. To rebuild a component, extract its source package and use
its `PKGBUILD` in an MSYS2 UCRT64 shell. The saved `.BUILDINFO` files identify
the original toolchain and dependency versions. See the
[MSYS2 build instructions](https://www.msys2.org/wiki/Creating-Packages/).

Run the preparation tests on any supported Python platform:

```sh
python -m unittest discover -s . -p 'test_*.py'
```
