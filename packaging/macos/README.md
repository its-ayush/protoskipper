# macOS Packaging — Signing and Notarization

## Overview

The `build_dmg.sh` script automates building a macOS `.dmg` containing a
PyInstaller-frozen `ProtoSkipper.app`.

Code-signing and notarization are **optional**: the script skips both silently
when credentials are absent. Unsigned builds work for development; signed+
notarized builds are required for distribution outside the App Store.

## Prerequisites

```bash
# Required
pip install pyinstaller
brew install create-dmg     # DMG builder

# For signing (requires Apple Developer Program membership)
# Xcode command-line tools already installed with `xcode-select --install`
```

## Code-Signing Setup

1. Download your **Developer ID Application** certificate from
   [developer.apple.com](https://developer.apple.com/account/resources/certificates/list)
   and install it into your login keychain.
2. Verify the identity name:
   ```bash
   security find-identity -p codesigning -v
   # Should show: Developer ID Application: DataSailors Pvt Ltd (TEAMID)
   ```
3. Set the environment variable:
   ```bash
   export APPLE_DEVELOPER_ID_APP="Developer ID Application: DataSailors Pvt Ltd (YOUR_TEAM_ID)"
   ```

## Notarization Setup (notarytool)

1. Generate an **App-Specific Password** at appleid.apple.com and store a
   credential profile once:
   ```bash
   xcrun notarytool store-credentials "protoskipper-notary" \
       --apple-id "your@email.com" \
       --team-id "YOUR_TEAM_ID" \
       --password "xxxx-xxxx-xxxx-xxxx"
   ```
2. Set the environment variable:
   ```bash
   export APPLE_NOTARIZE_PROFILE="protoskipper-notary"
   ```

## Full Signed + Notarized Build

```bash
export APPLE_DEVELOPER_ID_APP="Developer ID Application: DataSailors Pvt Ltd (TEAMID)"
export APPLE_NOTARIZE_PROFILE="protoskipper-notary"
make dmg
```

## Entitlements

`entitlements.plist` in this directory contains the minimal hardened-runtime
entitlements. Edit it only if a new capability (e.g. camera access) is needed.

## CI / GitHub Actions

The `.github/workflows/release.yml` workflow automatically signs and notarizes
when `APPLE_DEVELOPER_ID_APP` and `APPLE_NOTARIZE_PROFILE` secrets are set in
the repository settings. See that workflow for the secret names expected.
