# Windows Packaging — Installer Build and Code-Signing

## Overview

`build_installer.ps1` automates:
1. Freezing ProtoSkipper with PyInstaller into `dist\ProtoSkipper\`
2. (Optional) signing `ProtoSkipper.exe` with `signtool`
3. Compiling the Inno Setup installer (`protoskipper.iss`)
4. (Optional) signing the resulting `.exe` installer

Code-signing requires a valid **Authenticode** certificate (EV or OV) issued
by a Microsoft-trusted CA.

## Prerequisites

| Tool | Notes |
|------|-------|
| Python ≥ 3.10 | `python.exe` on PATH |
| PyInstaller | `pip install pyinstaller` |
| Inno Setup 6 | [jrsoftware.org/isdl.php](https://jrsoftware.org/isdl.php) — adds `iscc.exe` to PATH |
| Windows SDK | For `signtool.exe` (comes with Visual Studio or SDK installer) |

## Running Without Signing

```powershell
pwsh packaging/windows/build_installer.ps1
```

Output: `dist\ProtoSkipper-<version>-Setup.exe`

## Running With Code-Signing

```powershell
$env:WINDOWS_SIGNING_CERT = "C:\certs\datasailors.pfx"
$env:WINDOWS_SIGNING_PWD  = "certificate-password"
pwsh packaging/windows/build_installer.ps1
```

The script signs both the frozen `.exe` bundle and the final installer,
using a DigiCert RFC 3161 timestamp server so signatures remain valid after
certificate expiry.

## CI / GitHub Actions

The `.github/workflows/release.yml` workflow reads secrets:
- `WINDOWS_SIGNING_CERT_BASE64` — base64-encoded PFX
- `WINDOWS_SIGNING_PWD`

It decodes the PFX to a temp file, sets `WINDOWS_SIGNING_CERT`, then calls
`build_installer.ps1` automatically.

## Customising the Installer

Edit `protoskipper.iss` to:
- Change installation defaults (`DefaultDirName`, `DefaultGroupName`)
- Add/remove desktop shortcuts (the `[Tasks]` section)
- Modify the PATH integration (`addtopath` task)
- Add additional bundled files (`[Files]` section)
