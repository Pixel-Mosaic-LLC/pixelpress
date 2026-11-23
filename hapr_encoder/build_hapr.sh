#!/bin/bash
# Builds hapr_enc — pure Metal/Swift Hap R encoder
# No external frameworks needed beyond what ships with macOS

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_BIN="$DIR/../../Deployment/hapr_enc"

echo ""
echo "Building Hap R encoder (Metal BC7)..."

command -v swiftc &>/dev/null || { echo "ERROR: swiftc not found. Run: xcode-select --install"; exit 1; }

ARCH="$(uname -m)"
TARGET="${ARCH}-apple-macos12.0"
echo "  Arch: $ARCH"

echo "  Compiling hapr_enc..."
# Disable set -e temporarily so we can capture the exit code
set +e
swiftc -O -whole-module-optimization \
    -framework AVFoundation \
    -framework CoreMedia \
    -framework CoreVideo \
    -framework Metal \
    -framework Accelerate \
    -framework Foundation \
    -target "$TARGET" \
    -o "$OUT_BIN" \
    "$DIR/hapr_encoder.swift"
SWIFTC_EXIT=$?
set -e
echo "  swiftc exit code: $SWIFTC_EXIT"
if [ $SWIFTC_EXIT -ne 0 ]; then
    echo "  ERROR: Swift compilation failed (exit $SWIFTC_EXIT)"
    exit 1
fi

if [ -f "$OUT_BIN" ]; then
    chmod +x "$OUT_BIN"
    # Sign with hardened runtime + JIT entitlement so VideoToolbox's hardware
    # decoder can map JIT memory pages when launched from a signed .app bundle.
    # Without --options=runtime + allow-jit, macOS Sequoia kills the process
    # with SIGTRAP the moment the decoder allocates JIT-protected memory.
    ENTS="$DIR/hapr_entitlements.plist"
    codesign --force --sign - --options=runtime --entitlements "$ENTS" "$OUT_BIN" 2>/dev/null && \
        echo "  ✓ hapr_enc signed (ad-hoc, hardened runtime, JIT allowed)" || \
        echo "  ⚠ codesign unavailable — binary unsigned"
    echo "  ✓ hapr_enc compiled (Metal BC7 encoder)"
    echo ""
    echo "Restart PixelPress — title bar will show  Hap R ✓"
else
    echo "  ERROR: Compilation failed"
    exit 1
fi
