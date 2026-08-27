#!/usr/bin/env python3
"""Free-form messages in the GUI must wrap.

`ImGui::TextColored` does not wrap. A long line therefore widens the content
region, and the page grows a horizontal scrollbar — so the window appears to
stretch and every control on it shifts under the mouse. The messages this
catches are the long ones by nature: a tool's captured stderr, a diagnosis, an
absolute path, a list of missing symbols.

The rule was already true and already broken once, which is why it is a test
rather than a comment: a Mesen abort message ran off a 1913px window because
one call site used TextColored instead of the wrapping helper beside it.

Table cells are exempt. Inside a table a cell should clip, not wrap — wrapping
there grows the row height instead of fixing anything, so `TextColored` within
three lines of a TableNextColumn is left alone.

Run:  python3 tests/ui_wrapping_test.py
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
SRC = _REPO / "src" / "studio"

# The panes that render tool output. Migrate/Build/Git render short, structured
# fields; these three are where captured text lands.
PANES = ["studio_frames.cpp", "studio_snes.cpp", "studio_snes_functions.cpp"]

# TextColored whose argument is a runtime string — i.e. something whose length
# nobody controls.
FREE_FORM = re.compile(
    r'ImGui::TextColored\(\s*th\.\w+\s*,\s*"[^"]*%s[^"]*"\s*,[^;]*?\.c_str\(\)'
)
IN_TABLE = ("TableNextColumn", "TableSetColumnIndex")

failures = 0


def check(cond: bool, what: str) -> None:
    global failures
    if cond:
        print(f"  ok    {what}")
    else:
        print(f"  FAIL  {what}")
        failures += 1


def offenders(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        if not FREE_FORM.search(line):
            continue
        ctx = "\n".join(lines[max(0, i - 3):i])
        if any(t in ctx for t in IN_TABLE):
            continue
        out.append(f"{path.name}:{i + 1}: {line.strip()[:88]}")
    return out


def main() -> int:
    print("wrapped message text")
    for name in PANES:
        path = SRC / name
        if not path.is_file():
            check(False, f"{name} is missing")
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        if "wrapped(" in body or "TextWrapped" in body:
            check(True, f"{name} has a wrapping path")
        else:
            check(False, f"{name} renders no wrapped text at all")
        bad = offenders(path)
        check(not bad, f"{name}: every free-form message wraps"
                       + (f"\n        " + "\n        ".join(bad[:6]) if bad else ""))
    print("FAILED" if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
