# Studio tooling — hooks in the runtime, tools in Studio

RetComM Studio is the debugging suite for the recomp runtimes. This file is the
rule that decides where a given piece of that suite lives, and why.

> **Hooks belong to the runtime. Tools belong to Studio.**
>
> They meet at a wire protocol, never at a shared file.

Everything under `tools/` is Studio's to maintain and version. The runtimes
(`psxrecomp`, `snesrecomp`, …) own the instrumentation *inside* the emulated
machine. Neither side vendors the other's half.

---

## The two halves

|  | **Hook** — belongs to the runtime | **Tool** — belongs to Studio |
|---|---|---|
| What it is | Code that runs inside the runtime process: a ring buffer, a trace callback, a debug-server command, a store/IRQ choke point | Code that connects to a running runtime, drives it, and interprets what comes back |
| Examples | `wtrace` ring, `gpu_frame_dump`, `pc_break`, the GP0 packet ring, a `raise_irq` hook | `gpu_frame_capture.py`, `duckstation_oracle.py`, `beetle_oracle.py`, `ot_base_parity.py`, `mesen_oracle.py` |
| Language | C / C++ in the runtime | Python (and Lua for emulator-side scripting) |
| Review | Peer review on the runtime repo, deliberately | Ours, here, at our pace |
| Versioned by | The runtime's pin | This repository |

The dividing question is simple: **does it have to run inside the machine to
observe what it observes?** A store trace does. A tool that reads that trace
over TCP does not.

---

## Why the split is worth the friction

Three specific failures, all of them already paid for.

**1. Tools kept in a submodule get deleted.** `psxrecomp` commit `b5764b6e` is
titled *"debug: restore pause/step, repaint hold, and the GPU analysis tools"*,
and its message reads: *"All of this existed and was lost to a `git pull
--rebase` in this submodule while uncommitted."* Work that lives only in a
submodule working tree is one routine command from gone.

**2. Which tools a project HAD depended on the framework revision it pinned.**
Every port pins its own runtime, so the same Studio button worked in one repo
and was greyed out in the next, for reasons three directories away from where
anyone was looking. Shipping the tools with Studio makes the answer the same
everywhere and independent of anything merging upstream.

**3. Runtime review overhead is correct, and fatal to iteration speed.** A bad
change in the runtime corrupts every port's generated output, so it *should*
carry review. A diagnostic script has no such blast radius — it reads and
reports. Forcing the second through the gate built for the first means either
the review becomes a rubber stamp or the tooling stops being written. Splitting
them lets the runtime keep a slow gate and the tools keep a fast one.

None of this is a complaint about the runtime's maintainers. It is a statement
about which artifact each change can damage.

---

## Rules that follow

**Fix a tool here.** Not in a game's `psxrecomp/tools/` and not in a runtime
branch. A fix over there does not survive, and does not reach any other port.

**Studio's copy wins at resolution time.** `gpu_tool_path()`
(`src/studio/studio_frames_data.cpp`) tries this directory first and falls back
to the project's engine checkout, so a tool Studio does not ship still resolves
and someone actively editing one inside a runtime tree can still reach it. See
[`psx_analysis/README.md`](psx_analysis/README.md) for the exact order.

**Forking upstream is fine. Not knowing is the bug.** Several tools here began
as forks of a runtime's copy and are now substantially ahead. That is a
legitimate state. The failure mode is silent: upstream fixes its copy and the
fork never hears. [`psx_analysis/tool_drift.py`](psx_analysis/tool_drift.py)
records the upstream hash each fork was taken from and flags when upstream
moves — `--check` is CI-shaped, `--accept` re-baselines once a human has looked.

It also distinguishes a *fork* from a *vendored copy* (byte-identical, shipped
only so availability does not depend on the pinned revision) and reports
neither as a problem until upstream actually changes. A check that always fails
is a check everyone learns to ignore.

**When a hook really is needed, it goes to the runtime on its own branch.** Not
smuggled in, not worked around. Worked example: `psx-beetle` referenced four
Beetle hooks that no committed patch provided, so it could not link at all. The
fix — gate them, default off, and make every affected command report
UNAVAILABLE rather than serve an empty ring — is C in the runtime, so it went to
`psxrecomp` as branch `beetle-oracle-hook-guards`, reviewable on its own.

**A disabled probe must refuse, not return zero.** This is the rule that makes
the split safe. An unfed ring answers `total: 0`, which reads exactly like a
truthful "those events never happened", and has cost real sessions. Every
command whose producer is absent reports unavailable and names what is missing.
Where the value is a measurement rather than a count — a guest-cycle stamp, say
— it is **omitted** rather than zeroed, because a zero is a plausible reading
and an absent field is not.

---

## The case where a hook cannot be ours at all

Some diagnostics need instrumentation inside a third-party emulator used as an
oracle, and there the licence decides, not us.

`recomp-ai-rules/LICENSING.md` §2 and `PSX/PRINCIPLES.md` §11: the accurate PSX
reference emulators are licensed incompatibly — one copyleft, one
no-derivatives — and **neither one's source may be read by anyone who will touch
the recompiler implementation.** Reading contaminates the clean-room.

So:

- Applying a committed patch, building it, and running it: fine.
- Opening those sources to write a new hook: **not** something the person
  working on the recompiler can do. It needs someone outside the implementation.

The practical consequence is that oracle instrumentation patches are a scarce,
expensive artifact — which is exactly why they are **versioned here, next to the
tool that applies them**, rather than left in a working tree:
[`psx_analysis/duckstation/`](psx_analysis/duckstation/) and
[`psx_analysis/beetle/`](psx_analysis/beetle/) each carry their pin, their
patches, and a README recording what the thing is licensed as — so a future
session reads that *before* opening the source, not after.

---

## Layout

```
tools/
  README.md              this file — the rule
  psx_analysis/          PSX: frame capture, parity, oracles, drift check
    duckstation/         DuckStation oracle: pin + patch + README
    beetle/              Beetle PSX oracle: pin + hook patches + README
    tests/
  snes_analysis/         SNES: frame capture, asset decode, Mesen oracle + Lua
  new_project_layout/    project scaffolding (not diagnostics)
```

Each platform directory has its own README for the specifics — how its tools are
found, what engine features they depend on, and what its oracles are licensed
as. This file only settles *where things live and who versions them*.

## Tests

```sh
cd tools/psx_analysis/tests && python3 -m unittest discover -s . -p "test_*.py"
python3 tools/psx_analysis/tool_drift.py --check
```
