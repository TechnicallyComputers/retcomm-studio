"""Locate the snesrecomp wizard (templates + probe) Studio should drive.

Order matters, and it is not arbitrary. A migration is measured against the
framework revision the port is actually pinned to, so the project's own
``snesrecomp/`` submodule outranks anything Studio ships. The vendored copy
under the toolkit is a last resort that only exists so a packaged install can
scaffold a *new* project with no checkout on disk.
"""

from __future__ import annotations

import os
from pathlib import Path

from .paths import toolkit_dir

MARKER = Path("runner") / "runner.cmake"
_WIZARD_REL = Path("tools") / "new_project"


def _is_framework(root: Path) -> bool:
    return (root / MARKER).is_file()


def _env_root() -> Path | None:
    raw = (os.environ.get("SNESRECOMP_ROOT") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return None
    return p if _is_framework(p) else None


def snesrecomp_root(game_root: Path | str | None = None) -> Path | None:
    """A real snesrecomp checkout, or None."""
    env = _env_root()
    if env is not None:
        return env
    if game_root:
        root = Path(str(game_root)).expanduser()
        try:
            root = root.resolve()
        except OSError:
            pass
        for cand in (root / "snesrecomp", root):
            if _is_framework(cand):
                return cand
    # …/retcomm-studio/tools/new_project_layout → …/GitHub/snesrecomp
    base = toolkit_dir()
    for parent in (base.parent.parent, base.parent.parent.parent):
        cand = parent / "snesrecomp"
        if _is_framework(cand):
            return cand.resolve()
    return None


def vendored_wizard_dir() -> Path:
    """The copy shipped with Studio (see snes/VENDOR.md)."""
    return toolkit_dir() / "snes"


def wizard_dir(game_root: Path | str | None = None) -> Path:
    """Directory holding setup_project.sh / probe_rom.py / templates/."""
    root = snesrecomp_root(game_root)
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
