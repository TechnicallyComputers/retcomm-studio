# Vendored from snesrecomp

`tools/new_project/` copied verbatim out of **snesrecomp** so a packaged Studio
install can scaffold a SNES title without a framework checkout on disk.

- Upstream: <https://github.com/mstan/snesrecomp> — `tools/new_project/`
- Vendored at: `0c9a4fd25ba4e16920c97af7a9aa958c73548626` (2026-08-26)

**This copy is the fallback, not the source of truth.** Studio prefers a live
checkout, in this order:

1. `$SNESRECOMP_ROOT`
2. the selected project's own `snesrecomp/` submodule — the exact framework
   revision that port is pinned to, which is what a migration should be
   measured against
3. a sibling `snesrecomp/` checkout beside `retcomm-studio`
4. this directory

Re-vendor with a plain copy and update the SHA above; do not edit the files
here. A local fix would be invisible to snesrecomp and would be silently
reverted by the next re-vendor.
