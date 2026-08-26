# PSX analysis tools

The tools the Studio **Frames** tab drives: GPU frame capture and comparison,
display-list walking, colour parity, RAM parity, CD-ROM verification, register
probes, and the rest of the diagnostic family.

They live here rather than in `psxrecomp/tools/` on purpose. Read the next
section before moving them back.

## Why Studio owns these

> The rule for what lives here versus in the runtime — and why — is
> [`../README.md`](../README.md). This section is the PSX-specific history
> behind it.

They were maintained on a psxrecomp feature branch that never merged. Two
things followed from that, both of which cost real sessions:

- **They were lost outright once.** `psxrecomp` commit `b5764b6e` is titled
  *"debug: restore pause/step, repaint hold, and the GPU analysis tools"*, and
  its message says: *"All of this existed and was lost to a `git pull --rebase`
  in this submodule while uncommitted."* Work that lives only in a submodule's
  working tree is one routine command away from gone.
- **Which tools a project HAD depended on the framework revision it pinned.**
  Every port pins its own `psxrecomp`, so the same Frames button worked in one
  repo and was greyed out in the next, for reasons three directories away from
  where anyone was looking.

Shipping them with Studio makes the answer the same in every repo and
independent of anything merging upstream — which is the whole point.

## Provenance

Copied from `psxrecomp` `origin/feat/rbengine-bak` at
`c1f72dce89a2b4b549dd2549dad70dab984aabb7` (2026-08-25). That branch's versions,
not master's: several tools exist only there, and the shared ones
(`psx_gpu_frame.py` +763, `gpu_parity.py` +271, `duckstation_oracle.py` +120,
`disasm_ram.py`) are substantially ahead.

This is now the source of truth. Fix a tool here. Do not fix it in a game's
`psxrecomp/tools/` and expect it to survive — that is exactly the failure above.

## How Studio finds them

`gpu_tool_path()` (`src/studio/studio_frames_data.cpp`) resolves, in order:

1. `<RETCOMM_STUDIO_TOOLKIT>/../psx_analysis/<tool>` — this directory.
2. `<project>/psxrecomp/tools/<tool>` — the port's own engine checkout.
3. `<project>/tools/<tool>` — the engine repo opened directly as the project.

Studio's copy wins so the tool set does not vary by port. Steps 2–3 remain so a
tool Studio does not ship still resolves, and so someone actively editing one of
these inside a psxrecomp working tree can still reach it by opening that
checkout as the project root.

The lookup is `TOOLKIT/../psx_analysis`, so this directory must stay a **sibling
of the toolkit directory** in both layouts — `tools/new_project_layout` beside
`tools/psx_analysis` in the source tree, and `share/retcomm-studio/toolkit`
beside `share/retcomm-studio/psx_analysis` when installed. `CMakeLists.txt`
carries the install rule that keeps the packaged half true.

## They need an engine that answers `pause`

Fourteen of these tools park the runtime while they walk a structure that must
not move under them — display lists, vertex taps, range writers, colour
sources. That needs `pause` / `continue` / `step` / `run_to_frame` on the debug
server, and **psxrecomp master does not have them**; they live on the same
branch these tools came from.

Against a master-built `psx-runtime` those tools will fail to park. The engine
half is a separate change, proposed to psxrecomp on its own branch, because it
is C in the runtime and belongs to the repo that owns the runtime.

`tests/test_park_guard.py` is the guard for the tools' half of that contract:
the emulator must be handed back on **every** exit path, including the error
ones. Leaving it parked is worse than any error a tool can report — the game
stops advancing, so the effect never comes round again and every retry walks the
same stale display list and fails identically.

## The DuckStation oracle

`duckstation_oracle.py` resolves its patch directory relative to itself
(`PATCH_DIR = HERE / "duckstation"`), so `duckstation/` travels with it: the
pin, the Containerfile, the setup scripts, and `psxrecomp_oracle.patch`.

The patch here differs from psxrecomp master's. If you have an oracle installed
already, `status` will report a digest that matches neither — re-run
`setup` → `build` → `install` to realign it. That takes about ten minutes and
is a deliberate choice, not something Studio does behind you.

## Tests

```sh
cd tools/psx_analysis/tests && python3 -m unittest discover -s . -p "test_*.py"
```

502 tests, no network and no emulator required. Some import NumPy; a broken
system NumPy shows up here as three import errors that have nothing to do with
these tools — the toolchain Python
(`~/.local/share/retcomm/toolchains/.../python/bin/python3`) is the one Studio
actually runs them with.
