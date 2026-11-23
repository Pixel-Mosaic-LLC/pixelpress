#!/bin/bash
# Rebuilds PixelPress.app from pixelpress.py using PixelPress.spec
cd "$(dirname "$0")" || exit 1
DEPLOY="../Deployment"
PY=/opt/homebrew/bin/python3
[ -x "$PY" ] || PY=python3
echo "Compiling Hap R encoder ..."
bash hapr_encoder/build_hapr.sh || { echo "Hap R encoder build failed"; read -n1 -p "Press any key..."; exit 1; }
echo "Building PixelPress with $PY ..."
"$PY" -m PyInstaller --noconfirm --clean --distpath "$DEPLOY/dist" --workpath "$DEPLOY/build" PixelPress.spec || { echo; echo "Build failed. If PyInstaller is missing: $PY -m pip install --break-system-packages pyinstaller"; read -n1 -p "Press any key..."; exit 1; }
rm -rf "$DEPLOY/PixelPress.app"
cp -R "$DEPLOY/dist/PixelPress.app" "$DEPLOY/PixelPress.app"
codesign --force --deep --sign - "$DEPLOY/PixelPress.app" 2>/dev/null
xattr -dr com.apple.quarantine "$DEPLOY/PixelPress.app" 2>/dev/null
echo
echo "Done: $(cd "$DEPLOY" && pwd)/PixelPress.app"
open -R "$DEPLOY/PixelPress.app"
