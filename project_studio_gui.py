#!/usr/bin/env python3
"""Forward to tools/new_project_layout/project_studio_gui.py."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = (
    Path(__file__).resolve().parent
    / "tools"
    / "new_project_layout"
    / "project_studio_gui.py"
)
if not _TARGET.is_file():
    sys.stderr.write(f"error: missing {_TARGET}\n")
    raise SystemExit(2)
sys.argv[0] = str(_TARGET)
runpy.run_path(str(_TARGET), run_name="__main__")
