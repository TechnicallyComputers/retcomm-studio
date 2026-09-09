# Not covered by this repository's license

`psxrecomp_oracle.patch` is a diff against **DuckStation** source. A diff of
someone else's source is a derivative of it, so DuckStation's own terms govern
this file — not the [PolyForm Noncommercial License](../../../LICENSE) that
covers the rest of this repository, and not any commercial license granted for
it.

Before redistributing anything derived from it, consult DuckStation's current
license.

Two things already recorded elsewhere in this repo and repeated here because
this is where the file lives:

* Building DuckStation for your own use is fine under its license.
  **Redistributing that build is not** — which is why `install` keeps the build
  on the machine that produced it. See [`README.md`](README.md).
* The accurate PSX reference emulators are licensed incompatibly, and their
  source **must not be read by anyone who will touch the recompiler
  implementation** — reading contaminates the clean room. See
  [`tools/README.md`](../../README.md).

The Python tooling in this directory that *applies* and *drives* the patch is
this project's own code and is covered by the repository license as normal. The
carve-out is the `.patch` file.
