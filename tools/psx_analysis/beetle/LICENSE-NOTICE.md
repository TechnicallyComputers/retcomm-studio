# Not covered by this repository's license

The hook patches in this directory —

* `beetle_wtrace_hook.patch`
* `beetle_cdcmd_trace_hook.patch`
* `beetle_sio_trace_hook.patch`

— are diffs against **Beetle PSX / libretro** source (`libretro.cpp` and
neighbours). A diff of someone else's source is a derivative of it, so that
project's own terms govern these files — not the [PolyForm Noncommercial
License](../../../LICENSE) that covers the rest of this repository, and not any
commercial license granted for it.

Beetle PSX is the copyleft half of the pair described in
[`tools/README.md`](../../README.md); consult its current license before
redistributing anything derived from it, and note that a copyleft license
propagates to a combined work in a way permissive licenses do not.

The same clean-room rule applies here as for DuckStation: these sources must
not be read by anyone who will touch the recompiler implementation.

The Python tooling in this directory that *applies* and *drives* these patches
is this project's own code and is covered by the repository license as normal.
The carve-out is the `.patch` files.
