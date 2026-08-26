# SNES asset + attribution tools

> Tools here are maintained and versioned by Studio, not by
> `snesrecomp`. The rule and its rationale: [`../README.md`](../README.md).

Dump SNES assets **as composed** and attribute what is on screen to the source
function that drew it. The SNES sibling of `tools/psx_analysis/`, with the
same division of labour: these tools own the debug protocol and the decode
and write JSON/PNG/WAV artifacts; Studio is a viewer and a launcher.

Requires a game built with `SNESRECOMP_ENABLE_TRACE=ON` (production builds
open no debug port), running on the same machine. Nothing here pauses the
game: the runner's history ring keeps full per-frame WRAM/VRAM/CGRAM/OAM, so
capture reaches backwards after the fact.

```sh
# 1. capture a frame (newest in the ring, or --frame N) into a bundle
python3 snes_frame_capture.py --port 4380 --tag bad --out analysis/frames

# 2. decode the bundle offline: palettes, tilesheets, BG layers, sprites
python3 snes_asset_decode.py analysis/frames/bad.json --out analysis/assets
#    add --spc <file>  (from the server's `spc_dump`) for BRR -> WAV samples

# 3. who drew what: per-slot/region writers + draft symbols.toml entries
python3 snes_frame_attribute.py analysis/frames/bad.json --out analysis/assets
```

| File | Role |
|---|---|
| `snes_frame.py` | shared: wire protocol, bundle IO, RGB555, PNG writer |
| `snes_frame_capture.py` | live game → self-contained versioned bundle |
| `snes_asset_decode.py` | bundle → palettes / tiles / layers / sprites / samples |
| `snes_frame_attribute.py` | bundle → `attribution.json` + `suggestions.toml` |

Attribution rides the runtime's write rings, whose entries are stamped with
the writing function — `interp@$XXXXXX` for interpreted code (snesrecomp
`87c2109` or later; older builds attribute interpreted writes to the stale
enclosing AOT frame, and layer decode falls back to live registers with a
`ppu_source` label). Draft `[[func]]` entries are **suggestions**: nothing is
appended to a project's `symbols.toml` automatically — discovered attribution
is triaged by a human, the same rule the tier2 promotion flow follows.

Bundles derive from ROM data. Keep output directories out of git.

Deferred, and flagged rather than silently wrong: Mode 7 layers, hires,
interlace, coprocessor-originated writes (attributed `unknown`).

Tests: `python3 tests/snes_analysis_test.py` — exact-index tile decode,
exact-pixel layer compose, BRR, attribution join, bundle round-trip.
