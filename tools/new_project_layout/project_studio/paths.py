"""Locate toolkit roots (templates, CI helpers) relative to this package."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def toolkit_dir() -> Path:
    """tools/new_project_layout/ (or frozen bundle equivalent)."""
    env = (os.environ.get("RETCOMM_STUDIO_TOOLKIT") or "").strip()
    if env:
        p = Path(env).expanduser()
        try:
            p = p.resolve()
        except OSError:
            p = Path(env)
        if p.is_dir():
            return p
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        meipass = Path(getattr(sys, "_MEIPASS", exe_dir))
        for cand in (
            exe_dir / "toolkit",
            meipass / "toolkit",
            meipass / "tools" / "new_project_layout",
        ):
            if cand.is_dir():
                return cand.resolve()
    return Path(__file__).resolve().parent.parent


def assets_dir() -> Path | None:
    """Repo ``assets/`` or frozen bundle assets (icons)."""
    env = (os.environ.get("RETCOMM_STUDIO_ASSETS") or "").strip()
    if env:
        p = Path(env).expanduser()
        try:
            p = p.resolve()
        except OSError:
            p = Path(env)
        if p.is_dir():
            return p
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        meipass = Path(getattr(sys, "_MEIPASS", exe_dir))
        for cand in (exe_dir / "assets", meipass / "assets"):
            if cand.is_dir():
                return cand.resolve()
    # …/retcomm-studio/tools/new_project_layout → …/retcomm-studio/assets
    repo_assets = toolkit_dir().parent.parent / "assets"
    if repo_assets.is_dir():
        return repo_assets.resolve()
    return None


def templates_dir() -> Path:
    return toolkit_dir() / "templates"


def psxrecomp_root_from_toolkit() -> Path | None:
    """Find a psxrecomp checkout for CI templates / helpers.

    Search order:
    1. Toolkit lives inside psxrecomp (…/psxrecomp/tools/new_project_layout)
    2. Sibling of retcomm-studio (…/GitHub/psxrecomp next to …/GitHub/retcomm-studio)
    3. Sibling of toolkit parent chain
    """
    toolkit = toolkit_dir()
    # …/psxrecomp/tools/new_project_layout
    candidate = toolkit.parent.parent
    if (candidate / "runtime" / "runtime.cmake").is_file():
        return candidate
    # …/retcomm-studio/tools/new_project_layout → …/psxrecomp
    for base in (toolkit.parent.parent, toolkit.parent.parent.parent):
        sib = base / "psxrecomp"
        if (sib / "runtime" / "runtime.cmake").is_file():
            return sib.resolve()
    return None


def ci_setup_release_template(game_root: Path | None = None) -> Path | None:
    """Prefer game submodule copy; then vendored toolkit copy; then psxrecomp docs."""
    if game_root is not None:
        for rel in (
            Path("psxrecomp") / "docs" / "ci" / "templates" / "setup-release.yml",
            Path("psxrecomp-v4") / "docs" / "ci" / "templates" / "setup-release.yml",
        ):
            p = game_root / rel
            if p.is_file():
                return p
    vendored = toolkit_dir() / "ci_templates" / "setup-release.yml"
    if vendored.is_file():
        return vendored
    root = psxrecomp_root_from_toolkit()
    if root is None:
        return None
    p = root / "docs" / "ci" / "templates" / "setup-release.yml"
    return p if p.is_file() else None
