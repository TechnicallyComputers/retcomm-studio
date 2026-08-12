#!/usr/bin/env python3
"""Frozen / source entry point for RetComM Studio GUI."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _configure_paths() -> None:
    """Make ``project_studio`` importable and point toolkit at bundled files."""
    if getattr(sys, "frozen", False):
        os.environ["RETCOMM_STUDIO_FROZEN"] = "1"
        exe_dir = Path(sys.executable).resolve().parent
        meipass = Path(getattr(sys, "_MEIPASS", exe_dir))
        candidates = [
            exe_dir / "toolkit",
            meipass / "toolkit",
            meipass / "tools" / "new_project_layout",
        ]
        for cand in candidates:
            if cand.is_dir() and (cand / "project_studio").is_dir():
                os.environ["RETCOMM_STUDIO_TOOLKIT"] = str(cand.resolve())
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                break
        assets = exe_dir / "assets"
        if not assets.is_dir():
            assets = meipass / "assets"
        if assets.is_dir():
            os.environ["RETCOMM_STUDIO_ASSETS"] = str(assets.resolve())
        return

    # Source tree: packaging/entry.py → repo root
    root = Path(__file__).resolve().parents[1]
    toolkit = root / "tools" / "new_project_layout"
    if toolkit.is_dir() and str(toolkit) not in sys.path:
        sys.path.insert(0, str(toolkit))
    assets = root / "assets"
    if assets.is_dir():
        os.environ["RETCOMM_STUDIO_ASSETS"] = str(assets.resolve())


def main(argv: list[str] | None = None) -> int:
    _configure_paths()
    from project_studio.gui import run_gui

    initial = None
    args = list(argv if argv is not None else sys.argv[1:])
    if "--root" in args:
        i = args.index("--root")
        if i + 1 < len(args):
            initial = Path(args[i + 1])
    return run_gui(initial_root=initial)


if __name__ == "__main__":
    raise SystemExit(main())
