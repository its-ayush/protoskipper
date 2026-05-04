# ProtoSkipper — Top-level Makefile
#
# Targets:
#   make appimage   — build Linux AppImage (P6.A.1)
#   make deb        — build .deb package via fpm (P6.A.2)
#   make rpm        — build .rpm package via fpm (P6.A.2)
#   make dmg        — build macOS DMG (P6.B.1)
#   make installer  — build Windows Inno Setup installer (P6.C.1)
#   make wheel      — build a pure-Python wheel (for reproducibility check)
#   make dist       — build all distribution formats appropriate for the host
#   make clean      — remove build artefacts
#
# Prerequisite tools (see packaging/README.md for install instructions):
#   Linux:   PyInstaller, appimagetool, fpm
#   macOS:   PyInstaller, create-dmg (or dmgbuild), optionally codesign/notarytool
#   Windows: PyInstaller, Inno Setup 6 (iscc)
#
# Environment variables consumed:
#   APPLE_DEVELOPER_ID_APP     — macOS signing identity (optional)
#   APPLE_NOTARIZE_PROFILE     — notarytool credential profile (optional)
#   WINDOWS_SIGNING_CERT       — path to .pfx certificate for signtool (optional)
#   SOURCE_DATE_EPOCH          — set for reproducible builds (P6.E); defaults
#                                to the timestamp of the last git commit.

APP_NAME     := ProtoSkipper
VERSION      := $(shell python -c "import protoskipper; print(protoskipper.__version__)")
ARCH         := $(shell uname -m)
DIST_DIR     := dist
BUILD_DIR    := build

# Reproducible builds: derive SOURCE_DATE_EPOCH from last git commit if not set.
SOURCE_DATE_EPOCH ?= $(shell git log -1 --format=%ct 2>/dev/null || echo 0)
export SOURCE_DATE_EPOCH

.PHONY: all wheel appimage deb rpm dmg installer dist clean help

all: help

help:
	@echo "Available targets:"
	@echo "  wheel      Build a pure-Python wheel"
	@echo "  appimage   Build Linux AppImage (requires appimagetool)"
	@echo "  deb        Build .deb package (requires fpm)"
	@echo "  rpm        Build .rpm package (requires fpm)"
	@echo "  dmg        Build macOS DMG (requires create-dmg)"
	@echo "  installer  Build Windows Inno Setup installer (requires iscc)"
	@echo "  dist       Build all formats for this platform"
	@echo "  clean      Remove build artefacts"

wheel:
	python -m build --wheel --outdir $(DIST_DIR)

appimage: packaging/linux/build_appimage.sh
	@bash packaging/linux/build_appimage.sh

deb: packaging/linux/build_deb.sh
	@bash packaging/linux/build_deb.sh

rpm: packaging/linux/build_rpm.sh
	@bash packaging/linux/build_rpm.sh

dmg: packaging/macos/build_dmg.sh
	@bash packaging/macos/build_dmg.sh

installer: packaging/windows/build_installer.ps1
	@echo "Run on Windows: pwsh packaging/windows/build_installer.ps1"
	@echo "Or call: iscc packaging/windows/protoskipper.iss"

dist:
ifeq ($(shell uname -s),Linux)
	$(MAKE) appimage deb rpm
else ifeq ($(shell uname -s),Darwin)
	$(MAKE) dmg
else
	@echo "Run 'make installer' on Windows (requires PowerShell + Inno Setup)."
endif

clean:
	rm -rf $(BUILD_DIR) $(DIST_DIR) packaging/linux/build_root
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
