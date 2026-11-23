#!/bin/bash
# Builds PixelPress.app with ffmpeg/ffprobe bundled inside, then wraps it in:
#   Deployment/Releases/<version>/PixelPress-<version>.pkg  → installs to /Applications
#        (+ /usr/local/bin/pixelpress, the ffmpeg-style command line)
#   Deployment/Releases/<version>/PixelPress-<version>.zip  → zipped app (safe to sync/share)
#   …plus the licence files and the source packages that must be published with
#   every public release (LGPL): PixelPress-<version>-ffmpeg-source.tar and the
#   PixelPress HAP (pphap) source zips.
# Every run makes a NEW version: if the current version already has a release,
# the patch number is bumped (1.5.2 → 1.5.3) in pixelpress.py before building - unless that release is
# the Windows build (make_installer_windows.py) of exactly this pixelpress.py, which the Mac build joins.
# To jump to a specific version instead:  ./"Make Installer.command" 1.6.0
#
# Signing: with these three set, the app is signed with your Developer ID (hardened runtime), the .pkg
# with your Developer ID Installer certificate, and both are notarized and stapled. Without them the
# build is ad-hoc signed, which is fine for local testing but shows Gatekeeper warnings elsewhere.
#   export PIXELPRESS_CODESIGN_IDENTITY="Developer ID Application: Pixel Mosaic LLC (TEAMID)"
#   export PIXELPRESS_INSTALLER_IDENTITY="Developer ID Installer: Pixel Mosaic LLC (TEAMID)"
#   export PIXELPRESS_NOTARY_PROFILE=<profile saved once with: xcrun notarytool store-credentials>
set -e
cd "$(dirname "$0")"
SRC_DIR="$(pwd)"
TP="../3rd Party"; DEPLOY="../Deployment"; RELEASES="$DEPLOY/Releases"
# Python for the app: its Tk must be 8.6. customtkinter is built for Tk 8.6; with Tk 9 (Homebrew's
# Python 3.14) every hidden dropdown menu shows up as an empty "!dropdownmenu" window on macOS.
# python.org's 3.13 / 3.12 installers ship Tk 8.6. Override with PIXELPRESS_PYTHON=/path/to/python3.
PY=""
for c in "${PIXELPRESS_PYTHON:-}" /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 /opt/homebrew/bin/python3.13 \
         /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 python3; do
  [ -n "$c" ] || continue
  command -v "$c" >/dev/null 2>&1 || continue
  TKV=$("$c" -c "import tkinter; print(tkinter.TkVersion)" 2>/dev/null) || continue
  case "$TKV" in 8.*) PY="$c"; break;; esac
done
OLD_VERSION=$(grep -m1 "^VERSION = " pixelpress.py | sed "s/.*'\(.*\)'.*/\1/")
fail() { echo; echo "✗ $1"
         if [ "$VERSION" != "$OLD_VERSION" ]; then set_version "$OLD_VERSION"; echo "  (version reverted to $OLD_VERSION)"; fi
         read -n1 -p "Press any key..."; exit 1; }
set_version() {   # rewrites VERSION = '…' and the "PixelPress vX.Y.Z" header line
  perl -pi -e "s/^VERSION = '[^']*'/VERSION = '$1'/; s/^PixelPress v[0-9][0-9.]*/PixelPress v$1/" "$SRC_DIR/pixelpress.py"
}

src_md5() { tr -d '\r' < pixelpress.py | md5 -q; }
taken() {   # a release exists, unless it is the Windows build of exactly this source with no Mac installer yet
  local v=$1 d="$RELEASES/$1"
  [ -e "$d" ] || return 1
  [ -e "$d/PixelPress-$v.pkg" ] && return 0
  [ -f "$d/pixelpress.py.md5" ] || return 0
  cp pixelpress.py /tmp/pp-ver.py; set_version "$v"; local m; m=$(src_md5); cp /tmp/pp-ver.py pixelpress.py
  [ "$(awk '{print $1}' "$d/pixelpress.py.md5")" = "$m" ] && return 1 || return 0
}
VERSION="$OLD_VERSION"
if [ -n "$1" ]; then
  VERSION="$1"
else
  while taken "$VERSION"; do IFS=. read -r MA MI PA <<< "$VERSION"; VERSION="$MA.$MI.$((PA+1))"; done
fi
[ "$VERSION" != "$OLD_VERSION" ] && { set_version "$VERSION"; echo "Version bumped: $OLD_VERSION → $VERSION"; }

[ "$(uname -m)" = "arm64" ] || fail "Build this on an Apple Silicon Mac."

# Signing set-up (see the top of this file)
SIGN_ID="${PIXELPRESS_CODESIGN_IDENTITY:--}"
INST_ID="${PIXELPRESS_INSTALLER_IDENTITY:-}"
NOTARY="${PIXELPRESS_NOTARY_PROFILE:-}"
if [ "$SIGN_ID" != "-" ]; then
  security find-identity -v -p codesigning | grep -qF "$SIGN_ID" \
    || fail "Signing certificate not found in the keychain: $SIGN_ID"
  [ -n "$INST_ID" ] || fail "Set PIXELPRESS_INSTALLER_IDENTITY too (your Developer ID Installer certificate)"
  security find-identity -v | grep -qF "$INST_ID" \
    || fail "Installer certificate not found in the keychain: $INST_ID"
  [ -n "$NOTARY" ] || fail "Set PIXELPRESS_NOTARY_PROFILE too (from: xcrun notarytool store-credentials)"
  [ -f entitlements.plist ] || fail "Source Code/entitlements.plist is missing"
  echo "Signing as: $SIGN_ID"
else
  echo "⚠ No Developer ID set: ad-hoc signed build (local testing only)"
fi

# Signs the app inside-out: every Mach-O file (keeping any entitlements it already has, e.g. hapr_enc's),
# then nested bundles, then the app itself with entitlements.plist. Ad-hoc mode keeps the old behaviour.
sign_app() {
  local app="$1" f
  if [ "$SIGN_ID" = "-" ]; then codesign --force --deep --sign - "$app" 2>/dev/null || true; return 0; fi
  while IFS= read -r -d '' f; do
    file -b "$f" | grep -q 'Mach-O' || continue
    [ "$f" = "$app/Contents/MacOS/PixelPress" ] && continue
    codesign --force --timestamp --options runtime --preserve-metadata=entitlements \
             --sign "$SIGN_ID" "$f" >/dev/null 2>&1 || fail "could not sign $f"
  done < <(find "$app/Contents" -type f -print0)
  while IFS= read -r -d '' f; do
    codesign --force --timestamp --options runtime --sign "$SIGN_ID" "$f" >/dev/null 2>&1 || fail "could not sign $f"
  done < <(find "$app/Contents" -depth -type d \( -name '*.framework' -o -name '*.bundle' -o -name '*.app' \) -print0)
  codesign --force --timestamp --options runtime --entitlements entitlements.plist \
           --sign "$SIGN_ID" "$app" || fail "could not sign PixelPress.app"
  codesign --verify --strict --deep "$app" || fail "PixelPress.app signature check failed"
  echo "✓ PixelPress.app signed with Developer ID"
}
[ -n "$PY" ] || fail "No Python with Tk 8.6 found (Homebrew's Python 3.14 has Tk 9, which shows the dropdowns as empty windows).
  Install Python 3.13 from python.org (macOS 64-bit universal2 installer), then run this again."
echo "Python: $PY (Tk $("$PY" -c 'import tkinter; print(tkinter.TkVersion)'))"
"$PY" -c "import customtkinter" 2>/dev/null || "$PY" -m pip install --break-system-packages --quiet customtkinter \
  || fail "could not install customtkinter: $PY -m pip install customtkinter"
"$PY" -c "import PyInstaller" 2>/dev/null || "$PY" -m pip install --break-system-packages --quiet pyinstaller \
  || fail "could not install PyInstaller: $PY -m pip install pyinstaller"
echo "== PixelPress $VERSION installer build =="

# 1. LGPL-only ffmpeg + ffprobe, built from source (build_ffmpeg_lgpl.sh), plus its source package
mkdir -p "$TP"
FFSRC="$DEPLOY/ffmpeg-build/PixelPress-ffmpeg-source.tar"
lgpl_ok() { [ -x "$TP/ffmpeg" ] && [ -x "$TP/ffprobe" ] && [ -f "$FFSRC" ] || return 1
            local lic conf; lic=$("$TP/ffmpeg" -hide_banner -L 2>/dev/null); conf=$("$TP/ffmpeg" -hide_banner -buildconf 2>/dev/null)
            grep -q "GNU Lesser General Public" <<< "$lic" && ! grep -qE -- "--enable-(gpl|nonfree)" <<< "$conf"; }
if ! lgpl_ok; then
  echo "Building the LGPL ffmpeg (first time: ~10 min) ..."
  bash build_ffmpeg_lgpl.sh || fail "LGPL ffmpeg build failed (needs: brew install cmake nasm pkg-config)"
fi
lgpl_ok || fail "3rd Party/ffmpeg is not the LGPL build"
"$TP/ffmpeg" -hide_banner -encoders 2>/dev/null | grep -q " hap " || fail "Bundled ffmpeg has no Hap encoder"
echo "✓ LGPL ffmpeg with Hap support ready ($("$TP/ffmpeg" -hide_banner -version | head -1 | cut -d' ' -f1-3))"

# 1a. Licence files
[ -f licenses/THIRD-PARTY-LICENSES.txt ] && [ -f licenses/LICENSE.txt ] || fail "Source Code/licenses/ is missing its licence files"

# 1b. pphap — the PixelPress HAP plugin's encoder, used for Hap R (same output as the plugin)
[ -x "$TP/pphap" ] || fail "3rd Party/pphap missing — copy it from the plugin (Mac: /usr/local/bin/pphap)"
# a binary copied over an older one keeps macOS's cached signature → "Killed: 9"; re-sign a fresh copy
cp "$TP/pphap" "$TP/pphap.tmp" && mv -f "$TP/pphap.tmp" "$TP/pphap"
xattr -c "$TP/pphap" 2>/dev/null || true
codesign --force --sign - "$TP/pphap" 2>/dev/null || true
"$TP/pphap" -version >/dev/null 2>&1 || fail "3rd Party/pphap doesn't run on this Mac"
[ -f "$TP/pphap-licenses.txt" ] || fail "3rd Party/pphap-licenses.txt missing (plugin license.txt)"
PPV=$("$TP/pphap" -version 2>/dev/null | awk '{print $2}')
echo "✓ pphap $PPV bundled for Hap R"
# plugin source zips: Pixel Press Plugin/Deployment/Releases/<v>/ (2026-10-30 layout), or the older
# Pixel Press Plugin/Releases/<v>/Public source (ship with installers)/
PLUGIN_SRC="../../Pixel Press Plugin/Deployment/Releases/$PPV"
ls "$PLUGIN_SRC"/*-source.zip >/dev/null 2>&1 || PLUGIN_SRC="../../Pixel Press Plugin/Deployment/Releases/$PPV/Public source (ship with installers)"
ls "$PLUGIN_SRC"/*-source.zip >/dev/null 2>&1 || PLUGIN_SRC="../../Pixel Press Plugin/Releases/$PPV/Public source (ship with installers)"
PUBLIC_OK=1
if [ ! -d "$PLUGIN_SRC" ] || ! ls "$PLUGIN_SRC"/*-source.zip >/dev/null 2>&1; then
  PUBLIC_OK=0
  echo "⚠ No published source zips for pphap $PPV — this build is for internal use only."
  echo "  For a public release, put the pphap binary from the plugin's 1.1.4+ installer in 3rd Party/."
fi

# 2. Legacy Hap R encoder (fallback only)
bash hapr_encoder/build_hapr.sh || fail "Hap R encoder build failed"

# 2b. Drag and drop onto the window needs the tkinterdnd2 package (tkdnd)
"$PY" -c "import tkinterdnd2" 2>/dev/null || "$PY" -m pip install --break-system-packages --quiet tkinterdnd2 \
  || fail "could not install tkinterdnd2 (drag and drop): $PY -m pip install --break-system-packages tkinterdnd2"

# 3. App bundle
echo "Building PixelPress.app ..."
"$PY" -m PyInstaller --noconfirm --clean --distpath "$DEPLOY/dist" --workpath "$DEPLOY/build" PixelPress.spec >/dev/null || fail "PyInstaller build failed (install with: $PY -m pip install --break-system-packages pyinstaller)"
APP="$DEPLOY/dist/PixelPress.app"
[ -x "$APP/Contents/Frameworks/ffmpeg" ] || fail "ffmpeg didn't make it into the app bundle"
[ -x "$APP/Contents/Frameworks/pphap" ] || fail "pphap didn't make it into the app bundle"
find "$APP" -path "*tkinterdnd2/tkdnd/osx-arm64*" -name "*.dylib" | grep -q . || fail "drag and drop library (tkdnd) didn't make it into the app bundle"
[ -f "$APP/Contents/Frameworks/THIRD-PARTY-LICENSES.txt" ] || [ -f "$APP/Contents/Resources/THIRD-PARTY-LICENSES.txt" ] \
  || fail "licence file didn't make it into the app bundle"
sign_app "$APP"
rm -rf "$DEPLOY/PixelPress.app" && cp -R "$APP" "$DEPLOY/PixelPress.app"

# 3b. Command-line self-test (ffmpeg-style CLI used by Smart Preso etc.)
BIN="$(cd "$APP/Contents/MacOS" && pwd)/PixelPress"
"$BIN" -version | grep -q "pixelpress $VERSION" || fail "CLI -version check failed"
CT=$(mktemp -d)
"$APP/Contents/Frameworks/ffmpeg" -v error -f lavfi -i testsrc=size=640x360:rate=30 -t 1 -c:v prores "$CT/in.mov" \
  || fail "test clip could not be made"
CIN="$CT/in.mov"
pushd "$CT" >/dev/null      # pphap writes PPHAP-log.txt into the current folder
for c in hap_q hap_r; do
  "$BIN" -i "$CIN" -c:v $c -y -nostats -progress pipe:1 "$CT/$c.mov" > "$CT/$c.txt" || fail "CLI $c encode failed"
  grep -q "progress=end" "$CT/$c.txt" || fail "CLI $c gave no progress output"
done
"$BIN" -i "$CIN" -vf "drawtext=fontfile=/System/Library/Fonts/Menlo.ttc:text=TC" -c:v hap -y -nostats "$CT/tc.mov" \
  || fail "timecode burn-in (drawtext) doesn't work with the bundled ffmpeg"
LTXT=$("$BIN" -L) && grep -q "PIXELPRESS - LICENCES" <<< "$LTXT" || fail "CLI -L doesn't print the licences"
popd >/dev/null
rm -rf "$CT"
echo "✓ command line (hap_q, hap_r, -progress pipe:1, burn-in, -L) works"

# 4. Installer package
echo "Packaging installer ..."
STAGE=$(mktemp -d)
mkdir -p "$STAGE/root/Applications" "$STAGE/root/Library/Application Support/PixelPress"
cp -R "$APP" "$STAGE/root/Applications/"
cp licenses/THIRD-PARTY-LICENSES.txt licenses/LICENSE.txt "$TP/pphap-licenses.txt" \
   "$STAGE/root/Library/Application Support/PixelPress/"
pkgbuild --analyze --root "$STAGE/root" "$STAGE/comp.plist" >/dev/null
# install to /Applications even if another copy of PixelPress exists elsewhere
plutil -replace 0.BundleIsRelocatable -bool NO "$STAGE/comp.plist"
OUT="$RELEASES/$VERSION"; mkdir -p "$OUT"
PKG="$OUT/PixelPress-$VERSION.pkg"
# postinstall: /usr/local/bin/pixelpress → the app's ffmpeg-style command line
mkdir -p "$STAGE/scripts"
cat > "$STAGE/scripts/postinstall" <<'POST'
#!/bin/sh
mkdir -p /usr/local/bin
cat > /usr/local/bin/pixelpress <<'SHIM'
#!/bin/sh
exec "/Applications/PixelPress.app/Contents/MacOS/PixelPress" "$@"
SHIM
chmod 755 /usr/local/bin/pixelpress
exit 0
POST
chmod 755 "$STAGE/scripts/postinstall"
pkgbuild --root "$STAGE/root" --component-plist "$STAGE/comp.plist" \
         --scripts "$STAGE/scripts" \
         --identifier com.pixelpress.app --version "$VERSION" \
         --install-location / "$STAGE/component.pkg" >/dev/null || fail "pkgbuild failed"
# product archive: shows the licences before installing
mkdir -p "$STAGE/res"
cp licenses/THIRD-PARTY-LICENSES.txt "$STAGE/res/License.txt"
cat > "$STAGE/distribution.xml" <<DIST
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
    <title>PixelPress $VERSION</title>
    <license file="License.txt"/>
    <options customize="never" require-scripts="false" hostArchitectures="arm64"/>
    <domains enable_localSystem="true"/>
    <choices-outline>
        <line choice="com.pixelpress.app"/>
    </choices-outline>
    <choice id="com.pixelpress.app" visible="false">
        <pkg-ref id="com.pixelpress.app"/>
    </choice>
    <pkg-ref id="com.pixelpress.app" version="$VERSION" onConclusion="none">component.pkg</pkg-ref>
</installer-gui-script>
DIST
PB_SIGN=(); [ "$SIGN_ID" != "-" ] && PB_SIGN=(--sign "$INST_ID" --timestamp)
productbuild "${PB_SIGN[@]}" --distribution "$STAGE/distribution.xml" --resources "$STAGE/res" \
             --package-path "$STAGE" "$PKG" >/dev/null || fail "productbuild failed"
rm -rf "$STAGE"

# 4a. Notarize and staple (Developer ID builds only)
if [ "$SIGN_ID" != "-" ]; then
  echo "Notarizing (usually a few minutes) ..."
  NLOG=$(mktemp)
  xcrun notarytool submit "$PKG" --keychain-profile "$NOTARY" --wait | tee "$NLOG"
  grep -q "status: Accepted" "$NLOG" \
    || fail "Notarization was not accepted. Details: xcrun notarytool log <submission id> --keychain-profile $NOTARY"
  rm -f "$NLOG"
  xcrun stapler staple "$PKG" >/dev/null || fail "stapling the .pkg failed"
  xcrun stapler staple "$APP" >/dev/null || echo "⚠ could not staple the app itself (the zip still passes Gatekeeper when online)"
  spctl -a -vv -t install "$PKG" 2>&1 | grep -q accepted || fail "Gatekeeper does not accept the .pkg"
  echo "✓ notarized and stapled"
fi

# 4b. Licences + source packages for the release folder
cp licenses/THIRD-PARTY-LICENSES.txt licenses/LICENSE.txt "$OUT/"
echo "$(src_md5)  pixelpress.py" > "$OUT/pixelpress.py.md5"   # lets the Windows build of this source share the version
cp "$FFSRC" "$OUT/PixelPress-$VERSION-ffmpeg-source.tar"
if [ "$PUBLIC_OK" = 1 ]; then
  cp "$PLUGIN_SRC"/*-source.zip "$OUT/"
else
  printf '%s\n' "INTERNAL BUILD - NOT FOR PUBLIC RELEASE" "" \
    "The bundled pphap ($PPV) has no published source package. Before releasing," \
    "replace 3rd Party/pphap with the binary from the PixelPress HAP 1.1.4+ installer" \
    "and rebuild; the matching PixelPressHAP source zips are then copied here." > "$OUT/NOT-FOR-PUBLIC-RELEASE.txt"
fi

# 5. Zipped app — one plain file, keeps the bundle's internal links intact for syncing
ditto -c -k --keepParent "$APP" "$OUT/PixelPress-$VERSION.zip" || fail "zip failed"

echo
[ "$PUBLIC_OK" = 1 ] || echo "⚠ Internal build only — see NOT-FOR-PUBLIC-RELEASE.txt"
echo "✓ PixelPress $VERSION done: $(cd "$OUT" && pwd)"
ls -lh "$OUT"
open "$OUT"
