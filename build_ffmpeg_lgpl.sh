#!/bin/bash
# Builds the LGPL-only ffmpeg + ffprobe that PixelPress bundles.
#
#   bash "Source Code/build_ffmpeg_lgpl.sh"
#
# Output:
#   3rd Party/ffmpeg, 3rd Party/ffprobe          static, LGPL v2.1+, no GPL code
#   Deployment/ffmpeg-build/PixelPress-ffmpeg-source.tar
#       the exact sources + this script + build-info.txt — the "corresponding source"
#       that has to be published next to every release (Make Installer copies it there)
#
# What's in it: FFmpeg's own (LGPL) decoders, demuxers, filters and encoders, plus
#   snappy (BSD-3)      — required by the Hap encoder/decoder
#   FreeType (FTL)      — text rendering for the timecode burn-in (drawtext)
#   HarfBuzz (Old MIT)  — required by drawtext since FFmpeg 6.1
#   zlib (system)       — PNG decoding
#   VideoToolbox / AudioToolbox (macOS system frameworks)
# Nothing GPL (no x264/x265/etc.), nothing "nonfree".
#
# Needs (once): Xcode Command Line Tools, plus  brew install cmake nasm pkg-config
set -euo pipefail
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
TP="$ROOT/3rd Party"
WORK="${PP_FFMPEG_WORK:-$ROOT/Deployment/ffmpeg-build}"
mkdir -p "$WORK"
# FFmpeg's configure can't cope with spaces in paths ("Pixel Press …"),
# so everything is built through a space-free link to the same folder.
if [[ "$WORK" == *" "* ]]; then
  LINK="/tmp/pixelpress-ffmpeg-build-$(id -u)"
  ln -sfn "$WORK" "$LINK"
  WORK="$LINK"
fi
SRC="$WORK/src"; BLD="$WORK/build"; P="$WORK/prefix"
FFV=7.1.1; SNV=1.2.1; FTV=2.13.3; HBV=10.1.0
JOBS=$(sysctl -n hw.ncpu 2>/dev/null || nproc)
OS=$(uname -s)

need() { command -v "$1" >/dev/null || { echo "✗ $1 not found — run: brew install cmake nasm pkg-config"; exit 1; }; }
need cmake; need pkg-config; need make
[ "$OS" = Darwin ] && { need clang; [ "$(uname -m)" = arm64 ] || { echo "✗ build on an Apple Silicon Mac"; exit 1; }; }

mkdir -p "$SRC" "$BLD" "$P" "$TP"
fetch() {   # url file
  [ -s "$SRC/$2" ] && return
  echo "Downloading $2 …"
  curl -fL --retry 3 --progress-bar -o "$SRC/$2.part" "$1" && mv "$SRC/$2.part" "$SRC/$2"
}
fetch "https://ffmpeg.org/releases/ffmpeg-$FFV.tar.xz"                                   "ffmpeg-$FFV.tar.xz"
fetch "https://github.com/google/snappy/archive/refs/tags/$SNV.tar.gz"                   "snappy-$SNV.tar.gz"
fetch "https://download.savannah.gnu.org/releases/freetype/freetype-$FTV.tar.xz"          "freetype-$FTV.tar.xz"
fetch "https://github.com/harfbuzz/harfbuzz/releases/download/$HBV/harfbuzz-$HBV.tar.xz"  "harfbuzz-$HBV.tar.xz"

unpack() { rm -rf "$BLD/$1"; mkdir -p "$BLD/$1"; tar -xf "$SRC/$2" -C "$BLD/$1" --strip-components=1; }

if [ "$OS" = Darwin ]; then
  export MACOSX_DEPLOYMENT_TARGET=11.0
  CMAKE_OS=(-DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=11.0)
  CXXLIB=-lc++
else
  CMAKE_OS=(); CXXLIB=-lstdc++
fi
CMAKE_COMMON=(-DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DCMAKE_INSTALL_PREFIX="$P"
              -DCMAKE_PREFIX_PATH="$P" -DCMAKE_POSITION_INDEPENDENT_CODE=ON
              -DCMAKE_POLICY_VERSION_MINIMUM=3.5 "${CMAKE_OS[@]}")   # CMake 4 vs older projects

echo "== snappy $SNV"
unpack snappy "snappy-$SNV.tar.gz"
cmake -S "$BLD/snappy" -B "$BLD/snappy/b" "${CMAKE_COMMON[@]}" \
      -DSNAPPY_BUILD_TESTS=OFF -DSNAPPY_BUILD_BENCHMARKS=OFF >/dev/null
cmake --build "$BLD/snappy/b" -j "$JOBS" >/dev/null && cmake --install "$BLD/snappy/b" >/dev/null

echo "== FreeType $FTV"
unpack freetype "freetype-$FTV.tar.xz"
cmake -S "$BLD/freetype" -B "$BLD/freetype/b" "${CMAKE_COMMON[@]}" \
      -DFT_DISABLE_ZLIB=ON -DFT_DISABLE_BZIP2=ON -DFT_DISABLE_PNG=ON \
      -DFT_DISABLE_HARFBUZZ=ON -DFT_DISABLE_BROTLI=ON >/dev/null
cmake --build "$BLD/freetype/b" -j "$JOBS" >/dev/null && cmake --install "$BLD/freetype/b" >/dev/null

echo "== HarfBuzz $HBV"
unpack harfbuzz "harfbuzz-$HBV.tar.xz"
cmake -S "$BLD/harfbuzz" -B "$BLD/harfbuzz/b" "${CMAKE_COMMON[@]}" \
      -DHB_HAVE_FREETYPE=ON -DHB_HAVE_GLIB=OFF -DHB_HAVE_ICU=OFF -DHB_HAVE_GOBJECT=OFF \
      -DHB_HAVE_CORETEXT=OFF -DHB_BUILD_SUBSET=OFF -DHB_BUILD_UTILS=OFF >/dev/null
cmake --build "$BLD/harfbuzz/b" -j "$JOBS" >/dev/null && cmake --install "$BLD/harfbuzz/b" >/dev/null

echo "== FFmpeg $FFV (LGPL)"
unpack ffmpeg "ffmpeg-$FFV.tar.xz"
EXTRA_CFLAGS="-I$P/include"; EXTRA_LDFLAGS="-L$P/lib"
OS_FLAGS=()
if [ "$OS" = Darwin ]; then
  EXTRA_CFLAGS="$EXTRA_CFLAGS -mmacosx-version-min=11.0"
  EXTRA_LDFLAGS="$EXTRA_LDFLAGS -mmacosx-version-min=11.0"
  OS_FLAGS=(--enable-videotoolbox --enable-audiotoolbox)
fi
CONFIGURE=(--prefix="$P" --pkg-config-flags=--static --enable-static --disable-shared
           --disable-autodetect --enable-zlib --enable-libsnappy --enable-libfreetype --enable-libharfbuzz
           "${OS_FLAGS[@]}"
           --disable-ffplay --disable-doc --disable-network --disable-debug
           --extra-version=PixelPress-LGPL
           --extra-cflags="$EXTRA_CFLAGS" --extra-ldflags="$EXTRA_LDFLAGS" --extra-libs="$CXXLIB -lm")
( cd "$BLD/ffmpeg" && PKG_CONFIG_PATH="$P/lib/pkgconfig" ./configure "${CONFIGURE[@]}" >/dev/null ) \
  || { echo "✗ configure failed — see $BLD/ffmpeg/ffbuild/config.log"; exit 1; }
make -C "$BLD/ffmpeg" -j "$JOBS" >/dev/null
for t in ffmpeg ffprobe; do
  cp "$BLD/ffmpeg/$t" "$TP/$t"; chmod 755 "$TP/$t"
  [ "$OS" = Darwin ] && { strip "$TP/$t"; xattr -c "$TP/$t" 2>/dev/null || true; codesign --force --sign - "$TP/$t" 2>/dev/null || true; }
done

echo "== checks"
# (outputs captured first: with pipefail, grep -q closing the pipe early would fail the check)
LIC=$("$TP/ffmpeg" -hide_banner -L); CONF=$("$TP/ffmpeg" -hide_banner -buildconf)
ENC=$("$TP/ffmpeg" -hide_banner -encoders); FLT=$("$TP/ffmpeg" -hide_banner -filters)
grep -q "GNU Lesser General Public" <<< "$LIC" || { echo "✗ not an LGPL build"; exit 1; }
if grep -qE -- "--enable-(gpl|nonfree)" <<< "$CONF"; then echo "✗ GPL/nonfree enabled"; exit 1; fi
grep -q " hap " <<< "$ENC" || { echo "✗ no Hap encoder"; exit 1; }
grep -q " drawtext " <<< "$FLT" || { echo "✗ no drawtext filter"; exit 1; }
"$TP/ffmpeg" -hide_banner -v error -f lavfi -i testsrc=size=320x180:rate=30 -frames:v 5 -c:v hap -format hap_q -f null - \
  || { echo "✗ Hap test encode failed"; exit 1; }

# Corresponding source for the release folder
{
  echo "PixelPress — ffmpeg / ffprobe build"
  echo "Built $(date -u '+%Y-%m-%d %H:%M UTC') on $(uname -sm)"
  echo
  echo "FFmpeg $FFV  (LGPL v2.1 or later)   https://ffmpeg.org/releases/ffmpeg-$FFV.tar.xz"
  echo "snappy $SNV  (BSD-3-Clause)          https://github.com/google/snappy"
  echo "FreeType $FTV (FreeType License)     https://freetype.org"
  echo "HarfBuzz $HBV (Old MIT)              https://harfbuzz.github.io"
  echo "zlib, VideoToolbox, AudioToolbox: macOS system libraries (not included)"
  echo
  echo "SHA-256 of the source archives in this folder:"
  ( cd "$SRC" && shasum -a 256 *.tar.* 2>/dev/null || sha256sum *.tar.* )
  echo
  echo "FFmpeg configure:"
  printf '  %q\n' "${CONFIGURE[@]}"
  echo
  echo "To rebuild (or relink against your own build of any library): run build_ffmpeg_lgpl.sh"
  echo "with these archives in Deployment/ffmpeg-build/src/."
} > "$WORK/build-info.txt"
STAGE=$(mktemp -d); mkdir -p "$STAGE/PixelPress-ffmpeg-source"
cp "$SRC"/*.tar.* "$SELF" "$WORK/build-info.txt" "$STAGE/PixelPress-ffmpeg-source/"
tar -cf "$WORK/PixelPress-ffmpeg-source.tar" -C "$STAGE" PixelPress-ffmpeg-source
rm -rf "$STAGE"
echo "✓ LGPL ffmpeg/ffprobe in 3rd Party ($("$TP/ffmpeg" -hide_banner -version | head -1))"
echo "✓ source package: $WORK/PixelPress-ffmpeg-source.tar"
