# Beetle PSX oracle (write-trace comparison harness)

The second PSX oracle. DuckStation answers *"did the guest render the same?"* by
image. Beetle answers a different question: it carries our own RAM **write
trace**, the same instrument `psx-runtime` has, so a divergence can be diffed by
value and order with the **identical probe on both sides** — instead of
comparing two different instruments and arguing about the gap.

Driven by [`../beetle_oracle.py`](../beetle_oracle.py):

```bash
P=tools/psx_analysis
python3 $P/beetle_oracle.py doctor      # host check + the known blocker
python3 $P/beetle_oracle.py all         # fetch + patch + build + install
python3 $P/beetle_oracle.py start --bios <SCPH*.BIN> --disc disc/game.cue
python3 $P/beetle_oracle.py status --json
```

Installs into the shared RetComM data root, outside any game repo, so one build
serves every title:

```
~/.local/share/retcomm/oracle/beetle/      ($RETCOMM_DATA_DIR, or $RETCOMM_BEETLE_DIR)
  src/         pinned checkout with our hook patches applied
  build/       cmake/ninja tree for the psx-beetle frontend
  app/         the portable install that actually gets run
  oracle.json  manifest: upstream base, patch hashes, build stamp
```

## Licence — read this before opening the checkout

**Beetle PSX is mednafen-derived and copyleft.** `recomp-ai-rules/LICENSING.md`
§2 and `PSX/PRINCIPLES.md` §11 say its source may **not be read** by anyone who
will touch the recompiler implementation — reading contaminates the clean-room.

- Applying a committed patch, building, and running it: **fine**.
- Opening its sources to write new hooks: **not fine**, and not a job for
  whoever is working on psxrecomp. It needs someone outside the implementation.

`beetle_oracle.py` therefore never authors code inside the checkout. It fetches,
pins, applies the patches here, repairs one collision *between our own patches*,
builds, and reports what is missing.

## Pin

`pin.json` is the single source of truth. Upstream master was converted from C++
to plain C (`libretro.cpp` → `libretro.c`, `PS_CPU` class dropped) after the
pinned commit and no longer compiles against `runtime/src/beetle_libretro.cpp`,
so the pin is the **last C++-tree base we target**:

```
5759277be50052b9f3f388578bf56cc7899d833f
"Beetle PSX libretro: audit pass - exceptions, leaks, hardening, dead code"
```

Bump it only together with regenerated patches.

## The patches, and the collision between two of them

| Patch | Adds |
|---|---|
| `beetle_wtrace_hook.patch` | RAM write-trace callback (`wtrace_dump`) — the reason this oracle exists |
| `beetle_sio_trace_hook.patch` | `FrontIO::SetSIOTraceCallback`, per completed SIO byte exchange |
| `beetle_cdcmd_trace_hook.patch` | CD-command trace (`cdrom_cmd_dump` / `cdrom_cmd_reset`) |

`cdcmd` and `wtrace` **each insert the same `g_psxrecomp_wtrace_cb` /
`g_psxrecomp_fntrace_cb` definitions** into `libretro.cpp`, so applying both
gives:

```
libretro.cpp:517: error: redefinition of 'void (* g_psxrecomp_wtrace_cb)(...)'
```

`beetle_oracle.py` repairs this automatically, keeping the first definition of
each symbol. The edit is driven by a **symbol match, not line numbers**, and
touches only `g_psxrecomp_*` lines — our symbols, inserted by our patches — so a
patch reshuffle cannot make it silently cut the wrong thing.

The proper fix is to de-duplicate the committed patches so the documented manual
recipe works too; until then the tool is the only path that builds.

## Known blocker — the patch set is not sufficient to link

`psxrecomp/runtime/src/beetle_libretro.cpp` references three things
**unconditionally** that no committed patch defines:

| Symbol | What it feeds |
|---|---|
| `g_psxrecomp_rtrace_cb` | read-trace (`rtrace_*` commands) |
| `g_psxrecomp_irq_cb` | IRQ / device-event ring |
| `PS_CDC::PSXRecomp_GetDecodeVolume` | CD-audio decode-volume ground truth |

So `build` reaches stage 2 and stops. It reports this as a **verdict, not a log
dump** — `doctor` names it up front, before a build burns the time.

Two lawful resolutions:

- **(a)** someone outside the implementation lands the hooks and regenerates the
  patches here;
- **(b)** guard those three references in `beetle_libretro.cpp` behind a feature
  macro, so the committed set is self-sufficient. Cost: `rtrace`, the IRQ ring
  and decode-volume become unavailable — and per doctrine they must then report
  **UNAVAILABLE rather than returning zeros**, so a disabled probe can never be
  mistaken for a truthful empty result. `wtrace_dump` is unaffected either way.

(b) is the smaller change and needs no Beetle source. It is a decision about the
oracle's capability surface, so it wants a human.

## BIOS

The core expects a lowercase per-region filename (`scph5501.bin`) in the BIOS
directory. `pin.json` records only the **sha1 prefix `0555c6fa`** — that is all
`psxrecomp/docs/beetle-linux.md` gives, and the full digest has **not** been
verified against a dump here. Complete and verify it before gating on it; PSX
`PRINCIPLES.md` §2 makes firmware identity a checked property, not a comment.

## Build shape

Two stages, because the oracle is our frontend around upstream's core:

1. `make platform=unix STATIC_LINKING=1 HAVE_LIGHTREC=0` in the checkout.
   `STATIC_LINKING=1` still names the artifact `mednafen_psx_libretro.so`, but
   it is an **ar archive**; it is staged as `libmednafen_psx.a`.
2. `ninja psx-beetle` in psxrecomp's `runtime/`, linking that archive with
   `beetle_libretro.cpp` + `beetle_debug_server.c` — our SDL frontend and the
   JSON-over-TCP debug server, on **port 4380**.

Stage 2 needs a psxrecomp checkout (`--psxrecomp`, `$PSXRECOMP_DIR`, or
autodetected). `runtime/CMakeLists.txt` hardcodes
`${CMAKE_SOURCE_DIR}/../beetle-psx/libmednafen_psx.a`, so the tool symlinks
`psxrecomp/beetle-psx` → the managed checkout (the path is gitignored there). A
`-DBEETLE_LIB=` override in that CMakeLists would remove the need for the
symlink and is worth doing.
