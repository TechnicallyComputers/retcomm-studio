"""Thin launcher used by some packaging layouts — prefers the native binary."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    # Prefer sibling RetComM-Studio binary (onedir / AppImage / install prefix).
    here = Path(__file__).resolve().parent
    for cand in (
        here / "RetComM-Studio",
        here / "RetComM-Studio.exe",
        here.parent / "bin" / "RetComM-Studio",
        here.parent / "bin" / "RetComM-Studio.exe",
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            os.execv(str(cand), [str(cand), *sys.argv[1:]])

    # Fall back to Python CLI gui command (locates binary or prints build help).
    root = here.parent if (here.parent / "tools" / "new_project_layout").is_dir() else here
    toolkit = root / "tools" / "new_project_layout"
    if toolkit.is_dir():
        sys.path.insert(0, str(toolkit))
        os.environ.setdefault("RETCOMM_STUDIO_TOOLKIT", str(toolkit.resolve()))
    from project_studio.gui import run_gui

    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
