#!/usr/bin/env bash
# packaging/linux/build_rpm.sh — P6.A.2 of EXECUTION_PLAN.md
#
# Builds a .rpm package using fpm.
#
# Prerequisites:
#   pip install pyinstaller
#   gem install fpm        # or: dnf install ruby-devel && gem install fpm
#   dnf install rpm-build  # provides rpmbuild used by fpm internally
#
# Usage:
#   bash packaging/linux/build_rpm.sh
#   # or: make rpm

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

VERSION="$(python -c 'import protoskipper; print(protoskipper.__version__)')"
ARCH="$(uname -m)"
RPM_NAME="protoskipper-${VERSION}-1.${ARCH}.rpm"
BUILD_ROOT="packaging/linux/build_root"
STAGE_DIR="${BUILD_ROOT}/rpm_stage"

echo "==> Building ProtoSkipper ${VERSION} .rpm"

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

# --- 2. Stage install tree ---
INSTALL_BASE="${STAGE_DIR}/opt/protoskipper"
BIN_DIR="${STAGE_DIR}/usr/bin"
DESKTOP_DIR="${STAGE_DIR}/usr/share/applications"
mkdir -p "$INSTALL_BASE" "$BIN_DIR" "$DESKTOP_DIR"

cp -r "${BUILD_ROOT}/pyinstaller_dist/ProtoSkipper/." "$INSTALL_BASE/"
ln -sf "/opt/protoskipper/ProtoSkipper" "${BIN_DIR}/protoskipper"

cat > "${DESKTOP_DIR}/protoskipper.desktop" << EOF
[Desktop Entry]
Name=ProtoSkipper
Exec=/opt/protoskipper/ProtoSkipper
Icon=protoskipper
Type=Application
Categories=Development;Network;
Comment=SCADA/BMS protocol testing toolkit
EOF

# --- 3. Build with fpm ---
echo "==> fpm packaging"
mkdir -p dist
fpm \
    --input-type dir \
    --output-type rpm \
    --name protoskipper \
    --version "$VERSION" \
    --architecture "$ARCH" \
    --description "SCADA/BMS protocol testing and commissioning toolkit" \
    --url "https://github.com/datasailors/protoskipper" \
    --maintainer "DataSailors Pvt Ltd <support@datasailors.io>" \
    --license "GPL-3.0-or-later" \
    --category "Development/Tools" \
    --rpm-os linux \
    --package "dist/${RPM_NAME}" \
    --chdir "$STAGE_DIR" \
    .

echo "==> Built: dist/${RPM_NAME}"
