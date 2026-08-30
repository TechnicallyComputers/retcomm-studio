#!/usr/bin/env python3
"""Project Studio CLI entry (migrate / update title repos → setup-host layout).

Examples:
  python3 tools/new_project_layout/migrate_project.py audit --root ~/src/ApeEscapeRecomp
  python3 tools/new_project_layout/migrate_project.py plan  --root ~/src/ApeEscapeRecomp
  python3 tools/new_project_layout/migrate_project.py apply --root ~/src/ApeEscapeRecomp --dry-run
  python3 tools/new_project_layout/migrate_project.py git status --root ~/src/ApeEscapeRecomp
  python3 tools/new_project_layout/migrate_project.py gui
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from project_studio.cli import main

def _force_utf8_streams() -> None:
    """Let this tool print the arrows and box glyphs it formats with.

    The Windows console default is cp1252, which cannot encode U+2192 or the
    box-drawing characters used in the tables below, so a single such line
    would abort the tool with UnicodeEncodeError. Studio sets PYTHONUTF8 for
    everything it spawns; a developer running this script straight from a
    terminal gets no such help. Best effort: a stream that is already UTF-8,
    or is not a reconfigurable text wrapper, is left alone.
    """
    for _stream in (sys.stdout, sys.stderr):
        _enc = (getattr(_stream, "encoding", "") or "").lower().replace("-", "")
        if _enc.startswith("utf8"):
            continue
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_streams()

if __name__ == "__main__":
    raise SystemExit(main())
