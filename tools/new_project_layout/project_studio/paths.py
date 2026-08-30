"""Locate toolkit roots (templates, CI helpers) relative to this package."""

from __future__ import annotations

import os
import shutil
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


def find_bash() -> str | None:
    """Absolute path to a POSIX shell that understands Windows paths, or None.

    The toolkit drives several .sh scripts the projects own (regen.sh,
    package_release.sh, regen_bios.sh, the MinGW cross-build). On Linux and
    macOS bash is simply on PATH. On Windows it usually is NOT, even though it
    is installed: Git for Windows puts git.exe in "Git/cmd" (which the
    installer adds to PATH) but bash.exe in "Git/bin" and "Git/usr/bin" (which
    it does not). So a plain shutil.which("bash") reports "no bash" on a
    machine that has one, and every one of those features turns itself off.

    The System32 bash.exe is deliberately NOT accepted. That is the WSL
    launcher: it runs a Linux bash in a Linux filesystem namespace, so handing
    it a "C:/Users/..." script path fails in a way that reads like a broken
    script rather than the wrong shell.
    """
    if os.name != "nt":
        return shutil.which("bash")

    def usable(cand: Path) -> str | None:
        try:
            if not cand.is_file():
                return None
            if cand.parent.name.lower() in {"system32", "sysnative"}:
                return None
        except OSError:
            return None
        return str(cand)

    rels = (Path("bin") / "bash.exe", Path("usr") / "bin" / "bash.exe")

    # Most reliable: derive it from the git we already require. git.exe lives
    # in Git/cmd (or Git/mingw64/bin); bash.exe is its sibling under Git/bin.
    git = shutil.which("git")
    if git:
        base = Path(git).resolve().parent
        for up in (base.parent, base.parent.parent):
            for rel in rels:
                hit = usable(up / rel)
                if hit:
                    return hit

    for root in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramW6432"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    ):
        if not root:
            continue
        for rel in rels:
            hit = usable(Path(root) / "Git" / rel)
            if hit:
                return hit

    # Last resort: whatever is on PATH, provided it is not the WSL shim.
    for name in ("bash", "bash.exe", "sh", "sh.exe"):
        found = shutil.which(name)
        if found:
            hit = usable(Path(found))
            if hit:
                return hit
    return None
