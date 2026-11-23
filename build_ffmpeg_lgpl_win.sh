#!/bin/bash
# PixelPress (Windows app): builds the LGPL ffmpeg.exe + ffprobe.exe bundled inside the app.
# Same recipe as build_ffmpeg_lgpl.sh (Mac app): FFmpeg's LGPL code + snappy (Hap) + zlib (PNG) +
# FreeType / HarfBuzz (drawtext: text and timecode burn-in). No GPL, no nonfree. Statically linked.
# Cross-compiled on Linux with MinGW-w64 - run it on Linux (or WSL on Windows):
#
#   bash build_ffmpeg_lgpl_win.sh OUTDIR       e.g.  bash build_ffmpeg_lgpl_win.sh "../3rd Party/Windows"
#
# OUTDIR also gets build-info.txt and source/ (the exact source archives = the LGPL "corresponding source").
# Needs: mingw-w64, cmake, nasm, make, curl, pkg-config   (Ubuntu: apt install mingw-w64 nasm cmake make pkg-config curl)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$(mkdir -p "${1:-$HERE/../3rd Party/Windows}" && cd "${1:-$HERE/../3rd Party/Windows}" && pwd)"
WORK="${PP_FFWIN_WORK:-/tmp/pixelpress-app-ffmpeg-win}"
FFV=7.1.1; SNV=1.2.1; ZV=1.3.1; FTV=2.13.3; HBV=10.1.0
HOST=x86_64-w64-mingw32
JOBS=$(nproc 2>/dev/null || echo 4)
SRC="$WORK/src"; BLD="$WORK/build"; P="$WORK/prefix"
mkdir -p "$SRC" "$BLD" "$P"
for t in $HOST-gcc $HOST-g++ cmake nasm make curl; do command -v "$t" >/dev/null || { echo "missing $t"; exit 1; }; done

fetch() {   # url file
  [ -s "$SRC/$2" ] && return
  echo "Downloading $2"
  curl -fsSL --retry 3 -o "$SRC/$2.part" "$1" && mv "$SRC/$2.part" "$SRC/$2"
}
fetch "https://github.com/FFmpeg/FFmpeg/archive/refs/tags/n$FFV.tar.gz"     "FFmpeg-n$FFV.tar.gz"
fetch "https://github.com/google/snappy/archive/refs/tags/$SNV.tar.gz"      "snappy-$SNV.tar.gz"
fetch "https://github.com/madler/zlib/releases/download/v$ZV/zlib-$ZV.tar.gz" "zlib-$ZV.tar.gz"
# freetype.org's download site, else Ubuntu's copy of the same release (Debian's "+dfsg" repack drops
# a few non-library files with unclear licences; the library sources are identical)
fetch "https://download.savannah.gnu.org/releases/freetype/freetype-$FTV.tar.xz" "freetype-$FTV.tar.xz" 2>/dev/null ||
  fetch "http://archive.ubuntu.com/ubuntu/pool/main/f/freetype/freetype_$FTV+dfsg.orig.tar.xz" "freetype-$FTV.tar.xz"
fetch "https://github.com/harfbuzz/harfbuzz/releases/download/$HBV/harfbuzz-$HBV.tar.xz" "harfbuzz-$HBV.tar.xz"
unpack() { rm -rf "$BLD/$1"; mkdir -p "$BLD/$1"; tar -xf "$SRC/$2" -C "$BLD/$1" --strip-components=1; }

TC="$WORK/mingw.cmake"
cat > "$TC" <<EOF
set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)
set(CMAKE_C_COMPILER $HOST-gcc)
set(CMAKE_CXX_COMPILER $HOST-g++)
set(CMAKE_RC_COMPILER $HOST-windres)
set(CMAKE_FIND_ROOT_PATH $P)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
EOF
CM=(-DCMAKE_TOOLCHAIN_FILE="$TC" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF
    -DCMAKE_INSTALL_PREFIX="$P" -DCMAKE_POLICY_VERSION_MINIMUM=3.5)

echo "== zlib $ZV"
unpack zlib "zlib-$ZV.tar.gz"
( cd "$BLD/zlib" && make -f win32/Makefile.gcc PREFIX=$HOST- -j"$JOBS" libz.a >/dev/null &&
  mkdir -p "$P/lib" "$P/include" && cp libz.a "$P/lib/" && cp zlib.h zconf.h "$P/include/" )

echo "== snappy $SNV"
unpack snappy "snappy-$SNV.tar.gz"
cmake -S "$BLD/snappy" -B "$BLD/snappy/b" "${CM[@]}" -DSNAPPY_BUILD_TESTS=OFF -DSNAPPY_BUILD_BENCHMARKS=OFF >/dev/null
cmake --build "$BLD/snappy/b" -j "$JOBS" >/dev/null && cmake --install "$BLD/snappy/b" >/dev/null

# FreeType + HarfBuzz: ffmpeg's drawtext filter (timecode / text burn-in in the PixelPress app),
# same as the Mac app's ffmpeg. FreeType under its FreeType licence (BSD-style), HarfBuzz MIT.
echo "== FreeType $FTV"
unpack freetype "freetype-$FTV.tar.xz"
cmake -S "$BLD/freetype" -B "$BLD/freetype/b" "${CM[@]}" -DFT_DISABLE_ZLIB=ON -DFT_DISABLE_BZIP2=ON \
      -DFT_DISABLE_PNG=ON -DFT_DISABLE_HARFBUZZ=ON -DFT_DISABLE_BROTLI=ON >/dev/null
cmake --build "$BLD/freetype/b" -j "$JOBS" >/dev/null && cmake --install "$BLD/freetype/b" >/dev/null

echo "== HarfBuzz $HBV"
unpack harfbuzz "harfbuzz-$HBV.tar.xz"
cmake -S "$BLD/harfbuzz" -B "$BLD/harfbuzz/b" "${CM[@]}" -DHB_HAVE_FREETYPE=ON -DHB_HAVE_GLIB=OFF \
      -DHB_HAVE_ICU=OFF -DHB_HAVE_GOBJECT=OFF -DHB_HAVE_GDI=OFF -DHB_HAVE_UNISCRIBE=OFF \
      -DHB_HAVE_DIRECTWRITE=OFF -DHB_BUILD_SUBSET=OFF -DHB_BUILD_UTILS=OFF >/dev/null
cmake --build "$BLD/harfbuzz/b" -j "$JOBS" >/dev/null && cmake --install "$BLD/harfbuzz/b" >/dev/null

echo "== FFmpeg $FFV (LGPL)"
unpack ffmpeg "FFmpeg-n$FFV.tar.gz"
CONFIGURE=(--enable-cross-compile --target-os=mingw32 --arch=x86_64 --cross-prefix=$HOST-
           --prefix="$P" --pkg-config=pkg-config --pkg-config-flags=--static
           --enable-static --disable-shared --disable-autodetect
           --enable-zlib --enable-libsnappy --enable-libfreetype --enable-libharfbuzz --enable-w32threads
           --disable-ffplay --disable-doc --disable-network --disable-debug
           --extra-version=PixelPress-LGPL
           --extra-cflags="-I$P/include" --extra-ldflags="-L$P/lib -static"
           --extra-libs="-lstdc++ -lm")
( cd "$BLD/ffmpeg" && PKG_CONFIG_LIBDIR="$P/lib/pkgconfig" ./configure "${CONFIGURE[@]}" >/dev/null ) \
  || { echo "configure failed - see $BLD/ffmpeg/ffbuild/config.log"; tail -30 "$BLD/ffmpeg/ffbuild/config.log"; exit 1; }
make -C "$BLD/ffmpeg" -j"$JOBS" >/dev/null
for t in ffmpeg ffprobe; do
  cp "$BLD/ffmpeg/$t.exe" "$OUT/$t.exe"
  $HOST-strip "$OUT/$t.exe"
done

# checks: LGPL only, needed components present, no DLL dependencies beyond Windows itself
CONF=$(grep -m1 'FFMPEG_CONFIGURATION' "$BLD/ffmpeg/config.h")
if grep -qE -- "--enable-(gpl|nonfree)" <<< "$CONF"; then echo "GPL/nonfree enabled"; exit 1; fi
grep -q 'CONFIG_HAP_DECODER 1' "$BLD/ffmpeg/config_components.h" || { echo "no Hap decoder"; exit 1; }
grep -q 'CONFIG_HAP_ENCODER 1' "$BLD/ffmpeg/config_components.h" || { echo "no Hap encoder"; exit 1; }
grep -q 'CONFIG_PRORES_KS_ENCODER 1' "$BLD/ffmpeg/config_components.h" || { echo "no ProRes encoder"; exit 1; }
grep -q 'CONFIG_PNG_DECODER 1' "$BLD/ffmpeg/config_components.h" || { echo "no PNG decoder (zlib)"; exit 1; }
grep -q 'CONFIG_DRAWTEXT_FILTER 1' "$BLD/ffmpeg/config_components.h" || { echo "no drawtext filter (freetype)"; exit 1; }
DLLS=$($HOST-objdump -p "$OUT/ffmpeg.exe" | sed -n 's/^\s*DLL Name: //p' | tr 'A-Z' 'a-z' | sort -u | tr '\n' ' ')
for d in $DLLS; do
  case "$d" in kernel32.dll|user32.dll|advapi32.dll|bcrypt.dll|ole32.dll|oleaut32.dll|shell32.dll|shlwapi.dll|psapi.dll|msvcrt.dll|api-ms-win-*|ws2_32.dll|secur32.dll|gdi32.dll|vfw32.dll|avicap32.dll|crypt32.dll|mfplat.dll|mfuuid.dll|strmiids.dll) ;;
    *) echo "unexpected DLL dependency: $d"; exit 1 ;;
  esac
done

{
  echo "PixelPress - ffmpeg.exe / ffprobe.exe (Windows x64), bundled in the PixelPress app"
  echo "Built $(date -u '+%Y-%m-%d %H:%M UTC') by build_ffmpeg_lgpl_win.sh (MinGW-w64 cross-compile)"
  echo
  echo "FFmpeg $FFV  (LGPL v2.1 or later)  https://github.com/FFmpeg/FFmpeg/archive/refs/tags/n$FFV.tar.gz"
  echo "snappy $SNV  (BSD-3-Clause)         https://github.com/google/snappy"
  echo "zlib $ZV    (zlib licence)          https://zlib.net"
  echo "FreeType $FTV (FreeType licence)    https://freetype.org"
  echo "HarfBuzz $HBV (MIT)                 https://harfbuzz.github.io"
  echo "Windows DLLs used: $DLLS"
  echo
  echo "SHA-256 of the source archives:"
  ( cd "$SRC" && sha256sum *.tar.* )
  echo
  echo "FFmpeg configure:"
  printf '  %q\n' "${CONFIGURE[@]}"
  echo
  echo "To rebuild: run build_ffmpeg_lgpl_win.sh with these archives in \$PP_FFWIN_WORK/src."
} > "$OUT/build-info.txt"
mkdir -p "$OUT/source"
cp "$SRC"/*.tar.* "$0" "$OUT/source/"
echo "ffmpeg.exe / ffprobe.exe in $OUT ($(du -h "$OUT/ffmpeg.exe" | cut -f1), $(du -h "$OUT/ffprobe.exe" | cut -f1))"
