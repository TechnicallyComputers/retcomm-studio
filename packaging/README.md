# Packaging RetComM Studio

Native Dear ImGui app (SDL3 + OpenGL3) with the Python Project Studio toolkit
shipped beside the binary. Packaged builds expect **Python 3.11+ on PATH**.

## Build (local)

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
cmake --install build --prefix out
./out/bin/RetComM-Studio
```

## Outputs

| Artifact | How |
|----------|-----|
| Windows portable zip | `cmake --install` → `packaging/windows/package.ps1` |
| Windows Inno installer | same stage → `setup.iss` |
| Linux AppImage | install prefix → `packaging/linux/build-appimage.sh` |
| macOS DMG | install prefix → `packaging/macos/build-dmg.sh` |

## Icon

`assets/retcomm-studio.svg` — same dark/teal language as RetComM Launcher, with
code-bracket motif. Run `packaging/make-icons.sh` for PNG/ICO/(ICNS on macOS).

Fonts: `assets/fonts/LatoLatin-*.ttf` (required for UI).

## Versioning

`VERSION` seeds the first release. CI auto-bumps `vX.Y.Z` on workflow_dispatch
when the version input is left empty (patch/minor/major).
