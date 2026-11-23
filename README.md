# PixelPress

A free, open-source standalone encoder for the Hap video family (**Hap, Hap Alpha, Hap Q and Hap R**)
for **Apple Silicon Macs** and **Windows x64**, from Pixel Mosaic LLC.

- Drag in files or whole folders, pick a format and encode.
- Batch queue with per-video settings (format, frame rate and more for each clip).
- An ffmpeg-style command line (`pixelpress -i in.mov -c:v hap_r out.mov -progress pipe:1`) for
  scripts and other programs.
- Hap R is encoded by `pphap`, the encoder from
  [PixelPress HAP](https://github.com/Pixel-Mosaic-LLC/pixelpress-hap), our Adobe CC export plugin.

## Download

Installers are on the [Releases](https://github.com/Pixel-Mosaic-LLC/pixelpress/releases) page,
with SHA-256 checksums and the source packages the LGPL requires.

The builds are not code-signed yet. On a Mac, the first launch shows a security prompt: open
**System Settings › Privacy & Security** and click **Open Anyway**. On Windows, SmartScreen may
show "Windows protected your PC": click **More info › Run anyway**.

## Building from source

The build scripts expect this repository to sit in a folder called `Source Code` next to two
folders they read from and write to:

```
Pixel Press Standalone/
  Source Code/    ← this repository
  3rd Party/      ← ffmpeg, ffprobe and pphap for bundling
  Deployment/     ← build output and releases
```

**Mac (Apple Silicon)**

1. Install Xcode Command Line Tools, `brew install cmake nasm pkg-config`, and Python 3.12 or 3.13
   from python.org (it ships Tk 8.6, which the interface needs).
2. Build the LGPL-only ffmpeg and ffprobe: `bash "Source Code/build_ffmpeg_lgpl.sh"`.
3. Put `pphap` from a PixelPress HAP build in `3rd Party/`.
4. Run `Make Installer.command` for a `.pkg` and `.zip`, or `Build PixelPress.command` for the app only.

**Windows x64**

1. Install Python 3.12+ from python.org (`winget install Python.Python.3.12`) and Inno Setup 6
   (`winget install JRSoftware.InnoSetup`).
2. Build ffmpeg.exe and ffprobe.exe with `build_ffmpeg_lgpl_win.sh` (MinGW cross-compile) and put them,
   with `pphap.exe` and its Visual C++ DLLs, in `3rd Party/Windows/`.
3. Double-click `Make Installer (Windows).bat`.

The full requirements are at the top of each script.

## Licence

PixelPress is released under the [MIT License](LICENSE). It bundles third-party components (FFmpeg
under the LGPL v2.1, Python, Tcl/Tk, CustomTkinter and others); their notices and licences are in
[`licenses/THIRD-PARTY-LICENSES.txt`](licenses/THIRD-PARTY-LICENSES.txt).

"Hap" is the codec by Tom Butterworth and Vidvox. PixelPress is an independent project and is not
made or endorsed by Vidvox or disguise.

## Reporting problems

Bugs and feature requests: [Issues](https://github.com/Pixel-Mosaic-LLC/pixelpress/issues).
Security problems: see [SECURITY.md](SECURITY.md).
