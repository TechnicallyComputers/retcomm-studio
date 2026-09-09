# Third-party notices and license carve-outs

The [PolyForm Noncommercial License 1.0.0](LICENSE) covers **this project's own
code**. It does not, and cannot, cover the material listed here. This file
records what is not mine to license and under what terms it actually arrives.

## Carve-outs — NOT covered by this repository's license

### Emulator patches under `tools/psx_analysis/`

| Path | Patches |
|---|---|
| `tools/psx_analysis/duckstation/psxrecomp_oracle.patch` | DuckStation (`src/common/log_channels.h`, and others) |
| `tools/psx_analysis/beetle/beetle_wtrace_hook.patch` | Beetle PSX / libretro (`libretro.cpp`) |
| `tools/psx_analysis/beetle/beetle_cdcmd_trace_hook.patch` | Beetle PSX / libretro |
| `tools/psx_analysis/beetle/beetle_sio_trace_hook.patch` | Beetle PSX / libretro |

These files are diffs against those emulators' source. A diff of someone else's
source is a derivative of it, so the upstream project's terms govern these
files — not this repository's license, and not any commercial license granted
for this repository.

The two reference emulators are licensed incompatibly with each other (one
copyleft, one no-derivatives); [`tools/README.md`](tools/README.md) records the
constraint and the clean-room rule that follows from it. Consult each upstream
project for its current terms before redistributing anything derived from it.

Note also what `tools/psx_analysis/duckstation/README.md` already documents:
building one of these emulators for your own use is fine under its license,
**redistributing that build is not** — which is why the install stays on the
machine that built it.

### Fonts

`assets/fonts/LatoLatin-Regular.ttf`, `assets/fonts/LatoLatin-Bold.ttf` —
Copyright (c) 2010-2015 Łukasz Dziedzic, Reserved Font Name "Lato".
[SIL Open Font License 1.1](https://scripts.sil.org/OFL). The OFL has its own
redistribution and naming conditions; see `assets/fonts/NOTICE.md`.

## Bundled source

| Component | Where | License |
|---|---|---|
| `stb_image.h` | `third_party/stb_image.h` | Dual: MIT **or** Public Domain (Unlicense), at your option |
| `imgui_freetype` | `third_party/misc/freetype/` | MIT — vendored from Dear ImGui `v1.91.9b`; see `third_party/misc/freetype/NOTICE.md` |

## Fetched at build time

Not redistributed in this repository, but linked into binaries built from it,
so their notices travel with anything you ship.

| Component | Version | License |
|---|---|---|
| Dear ImGui | `v1.91.9-docking` | MIT — Copyright (c) 2014-2025 Omar Cornut |
| nlohmann/json | `v3.11.3` | MIT — Copyright (c) 2013-2022 Niels Lohmann |
| SDL3 | `release-3.2.16` | Zlib — Copyright (C) 1997-2025 Sam Lantinga |

All three are permissive and combine with PolyForm terms without conflict. You
must still preserve their copyright notices in anything you distribute.

## FreeType — check before you ship

FreeType is an **optional** dependency, detected at configure time. When it is
found, ImGui's FreeType rasterizer is compiled in and the binary links FreeType;
when it is not, the build reports `ImGui FreeType disabled` and does not.

FreeType is dual-licensed (the FreeType License, or GPLv2). If you distribute a
build that links it, pick and comply with one of those. The FTL is the usual
choice for a non-GPL product, and it requires crediting FreeType in your
documentation.

## Scope of a commercial license

A commercial license for this repository (see [COMMERCIAL.md](COMMERCIAL.md))
grants rights to this project's own code only. It cannot grant rights to
anything in this file.
