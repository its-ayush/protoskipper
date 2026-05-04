#!/usr/bin/env bash
# packaging/macos/build_dmg.sh — P6.B.1 of EXECUTION_PLAN.md
#
# Builds a macOS DMG containing a codesigned ProtoSkipper.app bundle.
# Optionally notarizes via notarytool when credentials are present.
#
# Prerequisites:
#   pip install pyinstaller
#   brew install create-dmg   # or: pip install dmgbuild
#   Xcode command-line tools (for codesign / notarytool)
#
# Environment variables (all optional):
#   APPLE_DEVELOPER_ID_APP     — e.g. "Developer ID Application: DataSailors Pvt Ltd (TEAMID)"
#   APPLE_NOTARIZE_PROFILE     — keychain credential profile for notarytool
#   SOURCE_DATE_EPOCH          — for reproducible builds
#
# Usage:
#   bash packaging/macos/build_dmg.sh
#   # or: make dmg

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

VERSION="$(python -c 'import protoskipper; print(protoskipper.__version__)')"
ARCH="$(uname -m)"
DMG_NAME="ProtoSkipper-${VERSION}-${ARCH}.dmg"
APP_NAME="ProtoSkipper.app"
BUILD_ROOT="build/macos"

echo "==> Building ProtoSkipper ${VERSION} macOS DMG (arch: ${ARCH})"

# --- 1. PyInstaller freeze ---
echo "==> PyInstaller freeze"
pyinstaller \
    --name "ProtoSkipper" \
    --windowed \
    --noconfirm \
    --clean \
    --osx-bundle-identifier "io.datasailors.protoskipper" \
    --add-data "src/protoskipper/gui/theme:protoskipper/gui/theme" \
    --hidden-import "PySide6.QtSvg" \
    --hidden-import "PySide6.QtPrintSupport" \
    --distpath "${BUILD_ROOT}/pyinstaller_dist" \
    --workpath "${BUILD_ROOT}/pyinstaller_work" \
    src/protoskipper/__main__.py

APP_BUNDLE="${BUILD_ROOT}/pyinstaller_dist/${APP_NAME}"

# --- 2. Code-sign (optional) ---
if [ -n "${APPLE_DEVELOPER_ID_APP:-}" ]; then
    echo "==> Signing with identity: ${APPLE_DEVELOPER_ID_APP}"
    codesign \
        --deep \
        --force \
        --options runtime \
        --entitlements "packaging/macos/entitlements.plist" \
        --sign "${APPLE_DEVELOPER_ID_APP}" \
        "$APP_BUNDLE"
    codesign --verify --deep --strict "$APP_BUNDLE"
    echo "==> Signed OK"
else
    echo "==> Skipping code signing (APPLE_DEVELOPER_ID_APP not set)"
fi

# --- 3. Build DMG ---
echo "==> Building DMG"
mkdir -p dist
create-dmg \
    --volname "ProtoSkipper ${VERSION}" \
    --volicon "packaging/macos/protoskipper.icns" \
    --window-pos 200 120 \
    --window-size 600 400 \
    --icon-size 100 \
    --icon "ProtoSkipper.app" 175 190 \
    --hide-extension "ProtoSkipper.app" \
    --app-drop-link 425 190 \
    "dist/${DMG_NAME}" \
    "$APP_BUNDLE" \
|| {
    # create-dmg not available — fall back to plain hdiutil approach
    echo "==> create-dmg failed or not found, using hdiutil fallback"
    STAGING="$(mktemp -d)"
    cp -r "$APP_BUNDLE" "$STAGING/"
    ln -s /Applications "$STAGING/Applications"
    hdiutil create \
        -volname "ProtoSkipper ${VERSION}" \
        -srcfolder "$STAGING" \
        -ov \
        -format UDZO \
        "dist/${DMG_NAME}"
    rm -rf "$STAGING"
}

# --- 4. Notarize (optional) ---
if [ -n "${APPLE_DEVELOPER_ID_APP:-}" ] && [ -n "${APPLE_NOTARIZE_PROFILE:-}" ]; then
    echo "==> Submitting to Apple notary service"
    xcrun notarytool submit "dist/${DMG_NAME}" \
        --keychain-profile "${APPLE_NOTARIZE_PROFILE}" \
        --wait
    xcrun stapler staple "dist/${DMG_NAME}"
    echo "==> Notarized and stapled OK"
else
    echo "==> Skipping notarization (credentials not set)"
fi

echo "==> Built: dist/${DMG_NAME}"
