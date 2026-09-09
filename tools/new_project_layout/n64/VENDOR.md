# Vendored from n64lle

`tools/new_project/` copied verbatim out of **n64lle** so a packaged Studio
install can scaffold an N64 title without a framework checkout on disk.

- Upstream: <https://github.com/RetroPortingToolKit/n64lle> — `tools/new_project/`
- Vendored at: `55f955eb73f63436a35f998e6d289d4fe37fbba1` (2026-09-07)

**This copy is the fallback, not the source of truth.** Studio prefers a live
checkout, in this order:

1. `$N64LLE_ROOT`
2. the selected project's own `n64lle/` submodule — the exact framework
   revision that port is pinned to, which is what a migration should be
   measured against
3. a sibling `n64lle/` checkout beside `retcomm-studio`
4. this directory

Re-vendor with a plain copy and update the SHA above; do not edit the files
here. A local fix would be invisible to n64lle and would be silently reverted
by the next re-vendor.

## One thing this fallback cannot do

`setup_project.sh` pins the new project's `n64lle` submodule at
`git -C "$N64LLE_ROOT" rev-parse HEAD` — the SHA the scaffold was cut against.
Run from this vendored copy there is no checkout to ask, so the script says so
and leaves the submodule at the branch tip for you to pin by hand. That is the
scaffolder's own honesty path, not a Studio workaround; it is recorded here
because it is the one behavioural difference between the two sources.
