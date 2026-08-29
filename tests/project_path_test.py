#!/usr/bin/env python3
"""A project root CMake cannot build from must be refused up front.

Marvel vs. Capcom was checked out into a directory whose name carried the
title's colon. Configuring it produced six copies of

    Attempt to add a custom rule to output
      /home/alex/Documents/GitHub/Marvel vs. Capcom.rule
    which already has a custom rule

-- a path the user never typed, cut at the colon, blamed on FetchContent. The
directory needed renaming; nothing about the build was wrong. These tests pin
the guard that says so, and pin the boundary: spaces are fine, and a Windows
drive letter is not a colon fault.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLKIT = Path(__file__).resolve().parents[1] / "tools" / "new_project_layout"
sys.path.insert(0, str(TOOLKIT))

from fill_tokens import path_build_problems, safe_path_suggestion  # noqa: E402

failures = 0


def check(ok, what):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
    if not ok:
        failures += 1


def main():
    # ---- what is fatal ------------------------------------------------------
    check(path_build_problems("/home/a/Marvel vs. Capcom: Clash of Super Heroes Recomp"),
          "a ':' in the path is refused")
    check(path_build_problems("/home/a/Marvel vs. Capcom; Clash"),
          "a ';' in the path is refused")
    check(path_build_problems("/home/a/weird\nname"), "a newline in the path is refused")
    check(path_build_problems('/home/a/what?'), "a Windows-illegal char is refused")

    # ---- what must NOT be flagged -------------------------------------------
    # Half this workspace has spaces in its project names and builds fine;
    # flagging them would refuse ~10 working repos.
    check(not path_build_problems("/home/a/Crash Bash Recomp"),
          "spaces are accepted (measured: they configure fine)")
    check(not path_build_problems("/home/a/Star wars - Episode I - Jedi Power Battles Recomp"),
          "spaces, dashes and dots together are accepted")
    check(not path_build_problems("/home/a/Klonoa - Door to Phantomile"),
          "an ordinary port path is accepted")
    check(not path_build_problems(r"C:\Users\alex\CrashBashRecomp"),
          "a Windows drive letter is not read as a colon fault")

    # ---- the suggestion has to actually be safe -----------------------------
    bad = "/home/a/Marvel vs. Capcom: Clash of Super Heroes Recomp"
    fixed = safe_path_suggestion(bad)
    check(":" not in fixed, "the suggested rename drops the colon")
    check(not path_build_problems(fixed), "the suggested rename is itself accepted")
    check(Path(fixed).parent == Path(bad).parent, "the suggestion stays in the same parent")
    check("Marvel vs. Capcom" in fixed, "the suggestion keeps the title readable")

    # ---- the CLI refuses before it reaches CMake ----------------------------
    with tempfile.TemporaryDirectory() as td:
        hostile = Path(td) / "Some Game: Subtitle"
        hostile.mkdir()
        r = subprocess.run(
            [sys.executable, "-m", "project_studio", "build", "configure",
             "--root", str(hostile)],
            capture_output=True, text=True,
            env={"PYTHONPATH": str(TOOLKIT), "PATH": "/usr/bin:/bin", "HOME": td},
        )
        check(r.returncode != 0, "the CLI exits non-zero on a hostile root")
        check("cannot be built from" in r.stderr, "it explains why")
        check("Rename the directory" in r.stderr, "it says what to do about it")
        check("CMake Error" not in r.stderr and "custom rule" not in r.stderr,
              "and it never reaches CMake to produce the confusing error")

    print("FAILED" if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
