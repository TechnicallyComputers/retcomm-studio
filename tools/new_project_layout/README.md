# New Project Layout + Project Studio

Scaffold **new** titles and **migrate** older ones onto the setup-host layout.

**Policy:** public releases are **setup-host only** (no prebuilt generated game C).

## Platforms

Every command takes a global `--platform {psx,snes}` (default `psx`, so every
pre-SNES invocation is unchanged). It is resolved once per process, before any
ops module runs, because it decides which repo index, which framework submodule
and which scaffolder everything below reaches for:

```bash
python3 -m project_studio --platform snes repos list
python3 -m project_studio --platform snes audit --root ~/src/ZedSNESRecomp
python3 -m project_studio --platform snes build generate --root ~/src/ZedSNESRecomp --rom ~/roms/zed.sfc
```

| | psx | snes |
|---|---|---|
| Framework | `psxrecomp` (master) | `snesrecomp` (main) |
| Index | `project_studio_repos.json` | `project_studio_repos_snes.json` |
| Migration ops | `ops.py` | `snesops.py` |
| Scaffolder | `setup_project.sh` / `.ps1` here | snesrecomp `tools/new_project/` (see `snes/`) |
| `build generate` | `psxrecomp_cli generate` | the project's `tools/regen.sh` |

Two migration implementations rather than one parametrised one: the PSX plan is
about `game.toml`, disc probing, BIOS backends and a CMake rewrite, none of
which exists on SNES.

Where a flag named a console, the platform-neutral spelling is the alias to
prefer — `--framework`, `--framework-branch`, `--framework-ref`, `--rom`. The
old names still work.

`build ensure-bios`, `build ensure-emitters` and `build mingw` are psxrecomp-only
and refuse under `--platform snes` rather than failing inside cmake.

`probe-rom --rom <file.sfc>` prints the cartridge identity as JSON — mapping,
title, region, coprocessor, digests, plus a `zip_prefix` and `project_name`.
It is the non-interactive half of the scaffolder's prompts: snesrecomp's wizard
asks for name / region / description with these as defaults, and any caller
passing `--yes` needs to see them beforehand.

`--region` carries no parser default. On PSX an unset region resolves to `USA`
as before; on SNES it stays blank so the cartridge header decides.

### Where the ROM is

A SNES port never contains its ROM — the scaffold gitignores `*.sfc`/`*.smc`,
and the wizard bakes the dump's *filename* into `tools/regen.sh` while
recording its directory nowhere. So a project scaffolded from `~/roms` indexes
with no image, and both the Migrate field and "Regenerate C from ROM" come up
empty until something says where it went.

`repos set-cue --path <repo> --rom <file.sfc>` records it; Studio now does that
for you whenever you pick or type a ROM on the Migrate tab, so it survives a
restart. Failing that, `repo_index.discover_rom()` looks in this order:

1. `SNESRECOMP_ROM` — the same variable `regen.sh` itself honours.
2. A dump parked in the tree: repo root, `rom/`, `roms/`.
3. A filename `tools/regen.sh` names, sitting in a known ROM directory —
   `RETCOMM_SNES_ROM_DIRS` (`os.pathsep`-separated), or the directory another
   indexed title's ROM was already found in.

Step 3 accepts a file only when its CRC32 matches the digest the port is pinned
to. A matching name in a library folder is a guess; a matching digest is the
ROM, and a port with no pinned digest gets no answer rather than a plausible
one.

## New project

```bash
sh tools/new_project_layout/setup_project.sh --disc /path/to/game.cue --dir ~/src
```

See [`docs/GAME_PROJECT_SETUP.md`](../../docs/GAME_PROJECT_SETUP.md).

## Project Studio (migrate / update)

Shared Python library under `project_studio/` (CLI + engine for the Dear ImGui
GUI). CLI stays stdlib-only. The GUI is built from the repo root:

```bash
cmake -S ../.. -B ../../build && cmake --build ../../build
../../build/Retro-Studio
# or
python3 tools/new_project_layout/project_studio_gui.py
```

```bash
# Audit layout gaps
python3 tools/new_project_layout/migrate_project.py audit \
  --root /path/to/ApeEscapeRecomp

# Show ordered plan
python3 tools/new_project_layout/migrate_project.py plan \
  --root /path/to/ApeEscapeRecomp

# Dry-run apply (recommended first)
python3 tools/new_project_layout/migrate_project.py apply \
  --root /path/to/ApeEscapeRecomp --dry-run

# Apply for real (rewrites CMake with .pre_migrate.bak)
python3 tools/new_project_layout/migrate_project.py apply \
  --root /path/to/ApeEscapeRecomp \
  --disc /path/to/game.cue

# GUI (Dear ImGui native binary)
python3 tools/new_project_layout/migrate_project.py gui
# or
python3 tools/new_project_layout/project_studio_gui.py

# Git / GitHub (also available as the GUI "Git / GitHub" tab)
python3 tools/new_project_layout/migrate_project.py git status \
  --root /path/to/ApeEscapeRecomp
python3 tools/new_project_layout/migrate_project.py git ensure-submodules \
  --root /path/to/ApeEscapeRecomp \
  --psxrecomp-branch master --recomp-ui-branch master
python3 tools/new_project_layout/migrate_project.py git switch \
  --root /path/to/ApeEscapeRecomp --branch main
python3 tools/new_project_layout/migrate_project.py git switch \
  --root /path/to/ApeEscapeRecomp --modules --branch master
python3 tools/new_project_layout/migrate_project.py git switch \
  --root /path/to/ApeEscapeRecomp --nested
python3 tools/new_project_layout/migrate_project.py git set-branch \
  --root /path/to/ApeEscapeRecomp --submodule psxrecomp --branch master
python3 tools/new_project_layout/migrate_project.py git ensure-nested \
  --root /path/to/ApeEscapeRecomp \
  --recomp-net-branch main --rbengine-branch main
python3 tools/new_project_layout/migrate_project.py git update-nested \
  --root /path/to/ApeEscapeRecomp --remote
python3 tools/new_project_layout/migrate_project.py git commit-nested \
  --root /path/to/ApeEscapeRecomp -m "chore: bump nested modules"
python3 tools/new_project_layout/migrate_project.py git update-submodules \
  --root /path/to/ApeEscapeRecomp --remote
python3 tools/new_project_layout/migrate_project.py git commit \
  --root /path/to/ApeEscapeRecomp -m "chore: bump submodules"
python3 tools/new_project_layout/migrate_project.py git push \
  --root /path/to/ApeEscapeRecomp
python3 tools/new_project_layout/migrate_project.py git release \
  --root /path/to/ApeEscapeRecomp --bump patch
```

### Bundle a local build (Studio "Bundle + Export")

Zip the host build exactly as it stands — exe + staged `assets/` + bundled
OpenBIOS + `game.toml` / `VERSION`, never a disc image or retail BIOS dump:

```bash
python3 tools/new_project_layout/migrate_project.py build package \
  --root /path/to/MotK --build-dir build-release
```

Writes `dist/<prefix>-<VERSION>-<host-tag>.zip` (e.g. `motk-0.1.0-linux-x64.zip`)
and prints a `BUNDLE_ZIP=` / JSON trailer. When the repo ships
`scripts/package_release.sh` that script is used (CI-parity payload and name);
otherwise a built-in stager produces the same layout, so this also works on
Windows without bash or `zip`. Add `--no-repo-script` to force the built-in
stager, `--tag` to override the platform tag.

Studio's **Build** tab runs this from the **Bundle + Export** button next to
Launch, then opens the native OS save dialog to copy the zip anywhere. It does
not rebuild — Build first.

### Local Windows builds (MinGW cross, no CI)

From Linux, cross-compile any Studio-indexed title to a Windows `.exe` for USB /
Wine testing without dispatching GitHub Actions:

```bash
# By Studio index name (substring) or --last / --root
python3 tools/new_project_layout/migrate_project.py build mingw \
  --name "Twisted Metal"
python3 tools/new_project_layout/migrate_project.py build mingw --last
python3 tools/new_project_layout/migrate_project.py build mingw \
  --root /path/to/TwistedMetal4Recomp --ensure

# Same script directly:
bash tools/new_project_layout/scripts/build_windows_mingw.sh --name TM4
bash tools/new_project_layout/scripts/build_windows_mingw.sh \
  --root /path/to/MotK --setup-host --package
```

Prereqs (Arch/CachyOS): `mingw-w64-gcc mingw-w64-sdl2 cmake ninja zip`.  
Full playable builds need generated game C first (`build generate` / `--ensure`).  
`--setup-host` mirrors CI setup-wizard packages.

Git ops shell out to `git` / `gh` (no force-push, amend, or hook skips). **`git switch`** moves working-tree HEAD (game, `--modules`, or `--nested`); type any branch name, or omit `--branch` on modules to use `.gitmodules` tracking. Submodule **branch** in `.gitmodules` is for `update --remote`; CI builds the committed **gitlink SHAs**.

For **bulk ops across many titles / platforms**, use the sibling tool
`retcomm-studio` (`~/…/GitHub/retcomm-studio` — catalog-backed CLI + plugin API).

### Ops (subset)

| Op | Purpose |
|----|---------|
| `rename_psxrecomp_submodule` | Promote/keep `psxrecomp/`; delete leftover `psxrecomp-v4` |
| `repair_psxrecomp_submodule` | Re-clone when `psxrecomp/.git` gitdir is broken / absorbed |
| `ensure_recomp_ui_submodule` | Add `recomp-ui` |
| `emit_codegen_setup` | Thin `codegen_setup.c/.h` |
| `rewrite_cmake_setup_host` | `psxrecomp_add_game_runtime` + wizard |
| `emit_packager` | `scripts/package_setup_release.sh` |
| `emit_ci_workflow` | Setup-host `release.yml` |
| `probe_disc_refresh` | TOC / catalog / seeds (needs `--disc`) |
| `annotate_legacy_packaging` | Mark old prebuilt packagers obsolete |

Wizard + `recomp-ui` are forced on for `apply` (setup-host requirement).

Helpers reused: `probe_disc.py`, `fill_tokens.py`, `sync_symbols.py`, `templates/*`.
