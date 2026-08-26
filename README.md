# RetComM Studio

Developer studio for RetComM recomp titles: catalog-backed bulk Git/GitHub ops
and the Project Studio toolkit (migrate / audit / new project / GUI), for
**PlayStation** (psxrecomp) and **Super Nintendo** (snesrecomp).

## Choosing a platform

Studio opens on a platform picker and nothing else is shown until it is
answered. That is not ceremony: the choice decides which repo index is read,
which framework submodule Git and Bulk operate on, which scaffolder New Project
drives and which migration ops exist. There is no sensible default for those
before the question is asked, and loading "the PSX one for now" would race the
answer.

| | PlayStation | Super Nintendo |
|---|---|---|
| Framework | `psxrecomp` | `snesrecomp` |
| Repo index | `project_studio_repos.json` | `project_studio_repos_snes.json` |
| Game image | Redump `.cue` | `.sfc` / `.smc` ROM |
| Generate | `psxrecomp_cli generate` (ROM + BIOS C) | the project's own `tools/regen.sh` |
| Build target | `psx-runtime` | the repo's CMake `project()` name |
| Tabs | Migrate · New Project · Git · Bulk · Build · Functions · Frames | Migrate · New Project · Git · Bulk · Build |

**Change** in the header returns to the picker. Each console keeps its own repo
list, so switching loses nothing.

Functions and Frames are PSX-only, and absent rather than empty under SNES:
they read psxrecomp's `analysis/` bundle and speak its debug protocol, neither
of which the SNES runner has. The same rule applies to the buttons that would
be dead ends — no BIOS staging, no emitter build, no MinGW cross-build on the
SNES Build tab, and the CLI refuses those subcommands under `--platform snes`
rather than failing deep inside cmake.

Every Studio → toolkit call carries `--platform`, injected once in the runner
rather than at each of the ~90 call sites.

## Super Nintendo

**New Project** drives snesrecomp's `tools/new_project/setup_project.sh`:
probe the ROM, lay out the repo, wire the submodules, seed `recomp/*.cfg`, then
generate / build / publish. Studio prefers a live snesrecomp checkout
(`$SNESRECOMP_ROOT`, the selected project's own submodule, or a sibling
checkout) and falls back to the copy vendored under
`tools/new_project_layout/snes/` so a packaged install works with no framework
on disk. The ROM is probed where it lies and never enters the repository.
Fields the SNES scaffolder has no flag for (BIOS, boot EXE, lobby, boxart) are
named in the log rather than silently dropped.

On a terminal that wizard **prompts** for name, region, description, publisher
and year, with the probed cartridge identity as each default. Studio runs it
with `--yes`, which takes every default silently — so the page carries those
fields and **Probe ROM** fills them with the same values the prompts would have
offered (and warns about a coprocessor or a header checksum that does not
validate). Anything typed there is passed as a flag; a field that never became
one would be lost without a word.

**Region is blank by default on SNES**, meaning *use the cartridge header*.
Studio does not carry the PSX habit of `USA` here: sending it would relabel a
Japanese cartridge in the README and in the packaged zip name.

**Migrate** audits a SNES port against that scaffold — framework and recomp-ui
submodules, nested `lib/recomp-net` + `lib/retcomm-rbengine`, `.gitignore`,
committed generated C, `recomp/` analysis config, `VERSION`, `tools/regen.sh`,
`scripts/package_release.sh`, CI, and whether `framework_pins.txt` still
matches the gitlinks — and applies the fixes.

Two things it deliberately will **not** do:

* **Untracking generated C keeps the working tree** (`git rm --cached` only), so
  a developer mid-build does not lose the C they just generated.
* **It refuses to emit `tools/regen.sh` when the ROM digests cannot be
  resolved.** Digests are carried across from what a previous scaffold already
  baked in, or probed from a ROM you point it at — never invented. A regen.sh
  carrying made-up digests verifies a ROM nobody owns and fails at the least
  useful moment.

**Build → Regenerate C from ROM** runs the project's own `tools/regen.sh` rather
than reimplementing generation, because regen.sh verifies the ROM against the
digests that port was pinned against first. Turning that check off is possible
and says so in orange.

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

## Frames tab — which function drew this?

A rendering bug in a recomp is usually a question about authorship: *which guest
function issued these primitives, and with which blend mode?* The Frames tab
connects to a running game, captures a frame's GP0 stream, and answers it.

* **Runtime / Ring** — connect to the debug server (default 4370) and see which
  frames the GP0 ring can still be asked for, plus savestate slot 1. There is no
  pause/step here and cannot be: psx-runtime removed those commands, because it
  is designed to be read from rather than steered. The ring holds ~1M packets —
  several hundred frames — so you play until the bug is on screen and then reach
  *backwards* for that frame and the ones before it, which is the only thing
  that works for a glitch you cannot reliably stop on.
* **Capture** — take the frame as `good` or `bad`, at the newest frame or any
  frame still in the ring (out-of-ring frames are flagged, because a dump of an
  evicted frame is empty and reads like "drew nothing"). Runs the game's own
  `psxrecomp/tools/gpu_frame_capture.py`, which writes the dump, a compact
  summary, the opcode histogram and a screenshot into `<root>/analysis/frames`.
* **Attribution** — one row per guest function: primitives issued, how many
  carried the semi-transparency bit, which blend modes, OT rank span, and the
  static name if the analysis bundle knows the address. **Save name** writes
  straight into `symbols.toml` through the same CLI the Functions tab uses.
* **Diff** — good vs bad, headlining the two findings that name a bug on their
  own: a blend mode that disappeared, and a function that stopped drawing.
* **Layers** — one image per function, so you can see each one's contribution
  in isolation.
* **Oracle** — set up, start and stop the patched DuckStation used for parity.
  It installs **machine-wide**, into the RetComM data root
  (`~/.local/share/retcomm/oracle/duckstation`, the same root as the toolchains
  and catalog), so one build serves every title and none of these buttons take a
  repo. Start uses the selected project's `[game] disc`, because parity is
  meaningless unless both emulators boot the same image.
* **DuckStation parity** — same frame on both emulators; identical images mean
  the guest ran the same and the bug is in the renderer. Enabled only when the
  oracle is actually answering.

**The game must be built with the debug server in it.** `PSX_DEBUG_TOOLS`
defaults OFF for Release builds, and without it the runtime never calls
`debug_server_init()` — nothing listens on any port, so no amount of retrying
the connection helps. The **Build tab → Debug tools** row shows what the
configured build dir will do, injects `-DPSX_DEBUG_TOOLS=ON/OFF` into Configure,
and has a **Configure for debugging** button that sets Release + ON in one click
(a real Debug build of a recomp is far too slow to reach the frame you want).
The Frames tab prints the same verdict when it cannot connect. The port itself
is a runtime setting — `game.toml` `[runtime] debug_port` — and the Frames tab
defaults to whatever the selected project declares.

Studio never decodes a GP0 packet itself. The tools live in the game's engine
submodule (they must match the runtime that produced the dump) and Studio is a
viewer over the JSON and PNG they write, so a headless capture and this tab can
never disagree. If the tab reports the tools are missing, the project needs
`git submodule update --init`. See `psxrecomp/docs/GPU_FRAME_TOOLS.md`.

Everything the tab shows is **observed** from one execution of one frame, and is
labelled as such — it is never folded into the Functions tab's static claims.

### Two job slots

Studio serialises work so two jobs cannot race the same build directory. There
are two locks, not one:

| Slot | Guards | Examples |
|---|---|---|
| **project** | the selected repo | Configure, Build, Analyze, Git, capture, diff |
| **global** | machine-wide toolbox work | building the DuckStation oracle |

The oracle build takes about ten minutes and touches nothing a project job
touches, so it runs on the global slot and the rest of Studio stays usable while
it works. A job that would race another in the same slot is refused with a
message rather than queued.

## Updates (Studio + shared toolchain + catalog)

On startup (and via **Check updates** in the header) Studio checks GitHub for:

1. A newer **RetComM Studio** release (`TechnicallyComputers/retcomm-studio`)
2. A newer shared **retcomm-toolchain** pack (`TechnicallyComputers/retcomm-toolchains`)
3. A newer **retcomm-catalog** zip (same shared cache as RetComM Hub/Launcher)

Catalog sync writes into the shared data root and immediately refreshes:

- Game repo dropdown **Catalog only** membership (`in_catalog`)
- Bulk **Catalog only** selection ticks

| OS | Catalog cache |
|----|---------------|
| Linux / macOS | `~/.local/share/retcomm/catalog/` |
| Windows | `%LOCALAPPDATA%\retcomm\catalog\` |

Toolchain packs install into the same cache as RetComM Launcher and game apps:

| OS | Path |
|----|------|
| Linux / macOS | `~/.local/share/retcomm/toolchains/cmake-clang-v1/<tag>/` |
| Windows | `%LOCALAPPDATA%\retcomm\toolchains\cmake-clang-v1\<tag>\` |

Override roots with `RETCOMM_DATA_DIR` / `RETCOMM_TOOLCHAIN_DIR` / `RETCOMM_CATALOG_DIR`
(same as the launcher).  
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
    studio_functions.*       #   Functions tab (static discovery)
    studio_frames.*          #   Frames tab (live GP0 capture / attribution)
    studio_frames_data.*     #     its loaders — ImGui-free, unit tested
    studio_debug.*           #   TCP debug-server client (shared by both)
  VERSION
  assets/                    # icons + fonts/
  packaging/
  .github/workflows/release.yml
  retcomm_studio/            # catalog bulk CLI
  tools/new_project_layout/
    project_studio/          # Python engine (migrate / git / build / …)
      platforms.py           #   the psx/snes profile — one per process
      snesops.py             #   SNES audit / plan / apply
      snes_paths.py          #   locate the snesrecomp wizard
    templates/               # PSX scaffold templates
    snes/                    # vendored snesrecomp wizard (see snes/VENDOR.md)
    ci_templates/
  tests/
    snes_platform_test.py    # platform split + SNES migration, no GPU needed
    json_null_test.cpp       # null-vs-absent at the toolkit JSON boundary
```

## Tests

```bash
cmake --build build --target snes_platform_test   # or: python3 tests/snes_platform_test.py
./build/json_null_test                            # toolkit JSON → model boundary
./build/analysis_load_test <repo-root>
./build/frames_load_test [analysis/frames]
./build/spawn_test
./build/debug_client_test
```
