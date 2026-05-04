#!/usr/bin/env bash
# packaging/linux/build_appimage.sh — P6.A.1 of EXECUTION_PLAN.md
#
# Builds a self-contained AppImage for ProtoSkipper on Linux x86_64.
#
# Prerequisites:
#   pip install pyinstaller
#   wget https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
#   chmod +x appimagetool-x86_64.AppImage  # and put on PATH as "appimagetool"
#
# Usage:
#   bash packaging/linux/build_appimage.sh
#   # or: make appimage

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

VERSION="$(python -c 'import protoskipper; print(protoskipper.__version__)')"
ARCH="$(uname -m)"
APPIMAGE_NAME="ProtoSkipper-${VERSION}-${ARCH}.AppImage"
BUILD_ROOT="packaging/linux/build_root"
APP_DIR="${BUILD_ROOT}/AppDir"

echo "==> Building ProtoSkipper ${VERSION} AppImage"

# --- 1. PyInstaller freeze ---
echo "==> PyInstaller freeze"
pyinstaller \
    --name "ProtoSkipper" \
    --windowed \
    --noconfirm \
    --clean \
    --add-data "src/protoskipper/gui/theme:protoskipper/gui/theme" \
    --hidden-import "PySide6.QtSvg" \
    --hidden-import "PySide6.QtPrintSupport" \
    --distpath "${BUILD_ROOT}/pyinstaller_dist" \
    --workpath "${BUILD_ROOT}/pyinstaller_work" \
    src/protoskipper/__main__.py

# --- 2. Stage AppDir structure ---
echo "==> Staging AppDir"
rm -rf "$APP_DIR"
mkdir -p "${APP_DIR}/usr/bin"
cp -r "${BUILD_ROOT}/pyinstaller_dist/ProtoSkipper/." "${APP_DIR}/usr/bin/"

# AppRun launcher
cat > "${APP_DIR}/AppRun" << 'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/ProtoSkipper" "$@"
EOF
chmod +x "${APP_DIR}/AppRun"

# .desktop file
cat > "${APP_DIR}/ProtoSkipper.desktop" << EOF
[Desktop Entry]
Name=ProtoSkipper
Exec=ProtoSkipper
Icon=protoskipper
Type=Application
Categories=Development;Network;
Comment=SCADA/BMS protocol testing toolkit
EOF

# Icon — fall back to a placeholder if none shipped
if [ -f "packaging/linux/protoskipper.png" ]; then
    cp "packaging/linux/protoskipper.png" "${APP_DIR}/protoskipper.png"
else
    # Minimal 1x1 transparent PNG
    printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82' > "${APP_DIR}/protoskipper.png"
fi

# --- 3. Build AppImage ---
echo "==> Running appimagetool"
APPIMAGETOOL="$(command -v appimagetool || echo 'appimagetool-x86_64.AppImage')"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || echo 0)}"
export SOURCE_DATE_EPOCH

"$APPIMAGETOOL" "$APP_DIR" "dist/${APPIMAGE_NAME}"

echo "==> Built: dist/${APPIMAGE_NAME}"
