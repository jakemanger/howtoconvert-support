# Conversion tools bundle

Command line conversion tools (FFmpeg, Pandoc, ImageMagick, LibreOffice and TinyTeX) for macOS,
Windows and Linux, from one place. No downloading each tool from its own source, and no package
manager needed.

The scripts that build the bundle are open source and live in this folder. Each tool keeps its own
license, in [`LICENSES/`](LICENSES/).

## Updating

1. Edit `bundle.config.json`.
2. Run **Build conversion tools bundle** from the Actions tab with **draft** ticked.
3. Review the draft release and publish it.
