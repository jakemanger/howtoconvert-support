# Windows TeX tools

Prepare a portable TeX runtime with pinned packages and fonts. The native
programs come from MSYS2; their matching source archives include the build
recipes and patches.

Requires Windows 10 or later, Python 3.14 and 7-Zip:

```powershell
python prepare.py --work C:\tex-tools
```

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
