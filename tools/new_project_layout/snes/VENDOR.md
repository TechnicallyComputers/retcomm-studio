# Vendored from snesrecomp

`tools/new_project/` copied verbatim out of **snesrecomp** so a packaged Studio
install can scaffold a SNES title without a framework checkout on disk.

- Upstream: <https://github.com/mstan/snesrecomp> — `tools/new_project/`
- Vendored at: `fe0991fe0aa2e6a5a755c81a7bef9185c965599c` (2026-09-12)

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
