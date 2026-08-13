"""GUI entry — launches the native Dear ImGui RetComM Studio binary."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _candidates(repo: Path) -> list[Path]:
    out: list[Path] = []
    env_bin = (os.environ.get("RETCOMM_STUDIO_BIN") or "").strip()
    if env_bin:
        out.append(Path(env_bin))
    for rel in (
        Path("build") / "RetComM-Studio",
        Path("build") / "Release" / "RetComM-Studio.exe",
        Path("build") / "RetComM-Studio.exe",
        Path("build-release") / "RetComM-Studio",
        Path("out") / "bin" / "RetComM-Studio",
        Path("out") / "bin" / "RetComM-Studio.exe",
    ):
        out.append(repo / rel)
    which = shutil.which("RetComM-Studio") or shutil.which("retcomm-studio")
    if which:
        out.append(Path(which))
    return out


def run_gui(*, initial_root: Path | None = None) -> int:
    here = Path(__file__).resolve()
    # …/tools/new_project_layout/project_studio/gui.py → repo root
    repo = here.parents[3] if len(here.parents) > 3 else here.parents[2]
    if initial_root is not None:
        os.environ["RETCOMM_STUDIO_INITIAL_ROOT"] = str(initial_root.resolve())

    for cand in _candidates(repo):
        if not cand.is_file():
            continue
        # Windows .exe often fails os.access(X_OK); still launchable.
        if os.name != "nt" and not os.access(cand, os.X_OK):
            continue
        return int(subprocess.call([str(cand)]))

    print(
        "RetComM Studio GUI is Dear ImGui (native).\n"
        "Build it first:\n"
        "  cmake -S . -B build && cmake --build build\n"
        "  ./build/RetComM-Studio\n"
        "Or set RETCOMM_STUDIO_BIN to the executable path.\n"
        "Requires Python 3.11+ on PATH for the toolkit engine.",
        file=sys.stderr,
    )
    return 2
