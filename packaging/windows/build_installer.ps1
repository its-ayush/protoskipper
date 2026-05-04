# packaging/windows/build_installer.ps1 — P6.C.1 of EXECUTION_PLAN.md
#
# Builds the Windows Inno Setup installer for ProtoSkipper.
#
# Prerequisites:
#   pip install pyinstaller
#   Inno Setup 6 installed (adds iscc.exe to PATH by default)
#
# Optional code-signing:
#   $env:WINDOWS_SIGNING_CERT  — path to .pfx certificate
#   $env:WINDOWS_SIGNING_PWD   — certificate password
#
# Usage:
#   pwsh packaging/windows/build_installer.ps1

$ErrorActionPreference = "Stop"

$RootDir = Resolve-Path "$PSScriptRoot\..\.."
Push-Location $RootDir

try {
    # --- 1. Determine version ---
    $Version = (python -c "import protoskipper; print(protoskipper.__version__)").Trim()
    Write-Host "==> Building ProtoSkipper $Version Windows installer"

    # Write version.txt for Inno Setup to read
    Set-Content -Path "packaging\windows\version.txt" -Value "[meta]`r`nversion=$Version"

    # --- 2. PyInstaller freeze ---
    Write-Host "==> PyInstaller freeze"
    pyinstaller `
        --name "ProtoSkipper" `
        --windowed `
        --noconfirm `
        --clean `
        --add-data "src/protoskipper/gui/theme;protoskipper/gui/theme" `
        --hidden-import "PySide6.QtSvg" `
        --hidden-import "PySide6.QtPrintSupport" `
        --distpath "dist" `
        --workpath "build/windows" `
        src/protoskipper/__main__.py

    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

    # --- 3. Optional: sign the frozen binary ---
    if ($env:WINDOWS_SIGNING_CERT) {
        Write-Host "==> Signing ProtoSkipper.exe with signtool"
        $signtool = (Get-Command signtool.exe -ErrorAction SilentlyContinue)?.Source
        if (-not $signtool) {
            $signtool = "${env:ProgramFiles(x86)}\Windows Kits\10\bin\x64\signtool.exe"
        }
        & $signtool sign `
            /fd SHA256 `
            /f $env:WINDOWS_SIGNING_CERT `
            /p $env:WINDOWS_SIGNING_PWD `
            /tr "http://timestamp.digicert.com" `
            /td SHA256 `
            "dist\ProtoSkipper\ProtoSkipper.exe"
        if ($LASTEXITCODE -ne 0) { throw "signtool failed" }
    } else {
        Write-Host "==> Skipping signing (WINDOWS_SIGNING_CERT not set)"
    }

    # --- 4. Run Inno Setup ---
    Write-Host "==> Running Inno Setup compiler"
    $iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue)?.Source
    if (-not $iscc) {
        $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    }
    & $iscc "packaging\windows\protoskipper.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed" }

    # --- 5. Optional: sign the installer ---
    $InstallerPath = "dist\ProtoSkipper-$Version-Setup.exe"
    if ($env:WINDOWS_SIGNING_CERT -and (Test-Path $InstallerPath)) {
        Write-Host "==> Signing installer"
        & $signtool sign `
            /fd SHA256 `
            /f $env:WINDOWS_SIGNING_CERT `
            /p $env:WINDOWS_SIGNING_PWD `
            /tr "http://timestamp.digicert.com" `
            /td SHA256 `
            $InstallerPath
        if ($LASTEXITCODE -ne 0) { throw "signtool (installer) failed" }
    }

    Write-Host "==> Built: $InstallerPath"
}
finally {
    Pop-Location
}
