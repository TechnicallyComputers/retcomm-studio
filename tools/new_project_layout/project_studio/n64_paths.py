"""Locate the n64lle wizard (templates + probe) Studio should drive.

The N64 counterpart to :mod:`snes_paths`, and the search order is the same for
the same reason: a migration is measured against the framework revision the
port is actually pinned to, so the project's own ``n64lle/`` submodule outranks
anything Studio ships. The vendored copy under the toolkit is a last resort
that only exists so a packaged install can scaffold a *new* project with no
checkout on disk.

WHAT IS DELIBERATELY NOT HERE. snes_paths carries a second half — "can the
snesrecomp this port is pinned to run the tools/regen.sh that wizard emits" —
because a SNES port owns a regen script that calls the framework CLI by name.
An n64lle port owns no such script: generation is a target in its own CMake
graph (``<slug>-generate``), driven by the framework libraries the pinned
submodule builds. There is no CLI subcommand vocabulary to skew, so there is
no gap to compute, and inventing one would be a check that cannot fail.
"""

from __future__ import annotations

import os
from pathlib import Path

from .paths import toolkit_dir

MARKER = Path("runtime") / "runtime.cmake"
_WIZARD_REL = Path("tools") / "new_project"


def _is_framework(root: Path) -> bool:
    return (root / MARKER).is_file()


def _env_root() -> Path | None:
    raw = (os.environ.get("N64LLE_ROOT") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return None
    return p if _is_framework(p) else None


def n64lle_root(game_root: Path | str | None = None) -> Path | None:
    """A real n64lle checkout, or None."""
    env = _env_root()
    if env is not None:
        return env
    if game_root:
        root = Path(str(game_root)).expanduser()
        try:
            root = root.resolve()
        except OSError:
            pass
        for cand in (root / "n64lle", root):
            if _is_framework(cand):
                return cand
    # …/retcomm-studio/tools/new_project_layout → …/GitHub/n64lle
    base = toolkit_dir()
    for parent in (base.parent.parent, base.parent.parent.parent):
        cand = parent / "n64lle"
        if _is_framework(cand):
            return cand.resolve()
    return None


def vendored_wizard_dir() -> Path:
    """The copy shipped with Studio (see n64/VENDOR.md)."""
    return toolkit_dir() / "n64"


def wizard_dir(game_root: Path | str | None = None) -> Path:
    """Directory holding setup_project.sh / probe_rom.py / templates/."""
    root = n64lle_root(game_root)
    if root is not None:
        live = root / _WIZARD_REL
        if (live / "setup_project.sh").is_file():
            return live
    return vendored_wizard_dir()


def wizard_source(game_root: Path | str | None = None) -> str:
    """Human-readable provenance for the log — which copy is being driven."""
    d = wizard_dir(game_root)
    return "vendored" if d == vendored_wizard_dir() else f"checkout {d}"


def templates_dir(game_root: Path | str | None = None) -> Path:
    return wizard_dir(game_root) / "templates"


def setup_script(game_root: Path | str | None = None) -> Path:
    return wizard_dir(game_root) / "setup_project.sh"


def probe_rom_script(game_root: Path | str | None = None) -> Path:
    return wizard_dir(game_root) / "probe_rom.py"


# ---------------------------------------------------------------------------
# The out-of-tree framework build every n64lle port needs before it configures
# ---------------------------------------------------------------------------
# n64lle is NOT add_subdirectory()'d: a port's CMakeLists includes
# runtime/runtime.cmake and calls n64lle_runtime_resolve_framework(<root>,
# <build>), which looks for already-built libraries and tools under
# build-n64lle/. So "configure the project" has a prerequisite that "cmake -S ."
# does not state, and a port that skips it fails inside a resolve function
# rather than at the missing step. Naming it here is what lets buildops check
# for it first.
FRAMEWORK_BUILD_DIR = "build-n64lle"
FRAMEWORK_BUILD_SCRIPT = Path("tools") / "build_framework.sh"


def framework_build_dir(game_root: Path | str) -> Path:
    return Path(str(game_root)).expanduser().resolve() / FRAMEWORK_BUILD_DIR


def framework_build_script(game_root: Path | str) -> Path | None:
    """The port's own ``tools/build_framework.sh``, or None.

    Scaffolded into every n64lle port (templates/build_framework.sh.in) and
    owned by the port, exactly as SNES's tools/regen.sh is: it carries the
    workarounds that port needs for the framework revision it is pinned to.
    """
    p = Path(str(game_root)).expanduser().resolve() / FRAMEWORK_BUILD_SCRIPT
    return p if p.is_file() else None


def framework_is_built(game_root: Path | str) -> bool:
    """Has build_framework.sh actually produced what resolve expects?

    Checked by artifact, not by "the directory exists": an aborted build leaves
    build-n64lle/ behind with a CMakeCache and nothing else, and treating that
    as built moves the failure into n64lle_runtime_resolve_framework().
    """
    build = framework_build_dir(game_root)
    if not build.is_dir():
        return False
    # The emitter is the tool the generate step runs, and the last thing the
    # framework build produces that a port cannot do without.
    for name in ("n64emit", "n64emit.exe"):
        if list(build.rglob(name)):
            return True
    return False
