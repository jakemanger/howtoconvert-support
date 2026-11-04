# ImageMagick for Windows

Windows uses the original ImageMagick 7.1.1-47 x64 Q16-HDRI installer. Its matching
source snapshots, dependency licenses and build files are separate release downloads.

To rebuild with Visual Studio 2022 and its C++, ATL and MFC components:

1. Extract `source-imagemagick-windows-build-9becc62004467fb3bc65a03d43eee2402f78be71-source.tar.gz`
   into an empty directory, removing its outer folder.
2. Extract every `source-imagemagick-7.1.1-47-windows-*-source.tar.gz` into that directory.
   These provide the pinned sources, so do not run `CloneRepositories.IM7.cmd`.
3. Build `Configure/Configure.sln` in Release mode. Run `Configure.exe` and select
   x64, Q16, HDRI and dynamic DLL runtimes.
4. Build the generated `IM7.Dynamic.sln`. Output is in `Artifacts/bin`.

See the [upstream build instructions](https://github.com/ImageMagick/ImageMagick-Windows/blob/9becc62004467fb3bc65a03d43eee2402f78be71/Readme.md).
The guide archive contains source checksums and the NASM build helper's license;
all original component notices remain in their source archives.
