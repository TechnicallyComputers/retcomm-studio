# Packaging RetComM Studio

Builds a frozen CustomTkinter GUI with the Project Studio toolkit embedded.

## Outputs

| Artifact | How |
|----------|-----|
| Windows portable zip | PyInstaller onedir → `packaging/windows/package.ps1` |
| Windows Inno installer | same stage → `setup.iss` |
| Linux AppImage | onedir → `packaging/linux/build-appimage.sh` |
| macOS DMG | onedir → `.app` → `packaging/macos/build-dmg.sh` |

## Icon

`assets/retcomm-studio.svg` — same dark/teal language as RetComM Launcher, with
code-bracket motif. Run `packaging/make-icons.sh` for PNG/ICO/(ICNS on macOS).

## Versioning

`VERSION` seeds the first release. CI auto-bumps `vX.Y.Z` on workflow_dispatch
when the version input is left empty (patch/minor/major).
