# RetComM Studio

Developer studio for RetComM recomp titles: catalog-backed bulk Git/GitHub ops
and the PSX Project Studio toolkit (migrate / audit / new project / GUI).

## Setup

```bash
cd ~/Documents/GitHub/retcomm-studio
cp studio.toml.example studio.toml
# edit catalog / checkout_roots / [titles] as needed
```

Requires Python 3.11+ (toolkit engine), `git`, and `gh` (for release dispatch).  
GUI is a native **Dear ImGui** app (SDL3 + OpenGL3), same stack as RetComM Hub.

```bash
# Build + run GUI
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
./build/RetComM-Studio

# Or via Python entry (locates the binary / prints build help)
python3 project_studio_gui.py
```

## Updates (Studio + shared toolchain)

On startup (and via **Check for updates** in the header) Studio checks GitHub for:

1. A newer **RetComM Studio** release (`TechnicallyComputers/retcomm-studio`)
2. A newer shared **retcomm-toolchain** pack (`TechnicallyComputers/retcomm-toolchains`)

Toolchain packs install into the same cache as RetComM Launcher and game apps:

| OS | Path |
|----|------|
| Linux / macOS | `~/.local/share/retcomm/toolchains/cmake-clang-v1/<tag>/` |
| Windows | `%LOCALAPPDATA%\retcomm\toolchains\cmake-clang-v1\<tag>\` |

Override roots with `RETCOMM_DATA_DIR` / `RETCOMM_TOOLCHAIN_DIR` (same as the launcher).  
Disable startup checks via `~/.config/retcomm/studio.json`:

```json
{ "check_updates_on_startup": false }
```

(Also honors launcher `config.json` → `check_updates_on_startup` when studio.json is unset.)

## Releases (GUI packages)

Manual workflow: Actions → **Release**. Leave **version** empty to auto-bump
`x.x.x` from the latest `vX.Y.Z` tag (or `VERSION` on first release).

| Asset | Platform |
|-------|----------|
| `RetComM-Studio-linux-x86_64.AppImage` | Linux |
| `RetComM-Studio-portable-windows.zip` | Windows portable |
| `RetComM-Studio-windows-x64-setup.exe` | Windows installer |
| `RetComM-Studio-macos-arm64.dmg` | macOS Apple Silicon |
| `RetComM-Studio-macos-x86_64.dmg` | macOS Intel |

Icon: `assets/retcomm-studio.svg` (teal-on-dark, launcher-matched). Packaging under `packaging/`.

## Layout

```
retcomm-studio/
  studio.toml.example
  retcomm_studio_cli.py
  migrate_project.py
  project_studio_gui.py      # launches native ImGui binary
  CMakeLists.txt             # RetComM-Studio (Dear ImGui)
  src/studio/                # ImGui shell + Python runner
  VERSION
  assets/                    # icons + fonts/
  packaging/
  .github/workflows/release.yml
  retcomm_studio/            # catalog bulk CLI
  tools/new_project_layout/
    project_studio/          # Python engine (migrate / git / build / …)
    templates/
    ci_templates/
```
