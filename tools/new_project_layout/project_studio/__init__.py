"""PSXRecomp Project Studio — audit / plan / apply for New Project Layout.

Setup-host releases only (no prebuilt game-C packaging path).
"""

from __future__ import annotations

import sys as _sys

__version__ = "0.1.0"


def _force_utf8_streams() -> None:
    """Make stdout/stderr able to carry the arrows this toolkit prints.

    Messages here are full of "A -> B" written with U+2192, plus a few other
    non-Latin-1 glyphs. On Windows the default console encoding is cp1252,
    which cannot encode any of them, so a single such message aborts the whole
    command with UnicodeEncodeError instead of printing. Studio itself sets
    PYTHONUTF8/PYTHONIOENCODING before it runs the toolkit, but a developer
    running "python -m project_studio ..." in a terminal gets no such help,
    and neither do the test scripts.

    Reconfiguring is best-effort: a stream that is already UTF-8, or is not a
    reconfigurable text wrapper (a StringIO in a harness, a closed stream), is
    left exactly as it is.
    """
    for stream in (_sys.stdout, _sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc in {"utf8", "utf8mb4"}:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_streams()

from .detect import audit_project
from .models import AuditReport, CheckResult, MigrateOptions, Plan, PlanStep
from .plan import build_plan
from .ops import apply_plan

__all__ = [
    "AuditReport",
    "CheckResult",
    "MigrateOptions",
    "Plan",
    "PlanStep",
    "audit_project",
    "build_plan",
    "apply_plan",
    "__version__",
]
