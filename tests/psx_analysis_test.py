#!/usr/bin/env python3
"""The PSX analysis tools Studio ships, and the layout that lets it find them.

Three things can silently break the Frames tab, and none of them fails a build:

  * Studio names a tool in studio_frames.cpp that nobody shipped. The button
    greys out with "can't find <tool>" and the reason is a missing file, not a
    missing checkout — indistinguishable from the case that message describes.
  * The tools directory stops being a sibling of the toolkit directory.
    gpu_tool_path() resolves <RETCOMM_STUDIO_TOOLKIT>/../psx_analysis, so a
    move that looks tidy in the source tree makes every tool unreachable.
  * The install rule stops shipping them, so a packaged Studio has the buttons
    and none of the tools behind them.

Run:  python3 tests/psx_analysis_test.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
TOOLS = _REPO / "tools" / "psx_analysis"
TOOLKIT = _REPO / "tools" / "new_project_layout"

failures = 0


def check(cond: bool, what: str) -> None:
    global failures
    if cond:
        print(f"  ok    {what}")
    else:
        print(f"  FAIL  {what}")
        failures += 1


def studio_tool_names() -> list[str]:
    """Every `const char* kSomethingTool = "x.py";` in the Frames source."""
    src = (_REPO / "src" / "studio" / "studio_frames.cpp").read_text(
        encoding="utf-8", errors="replace"
    )
    return sorted(set(re.findall(r'const char\* k\w+Tool = "([^"]+)";', src)))


def test_every_named_tool_ships() -> None:
    print("tools Studio names")
    names = studio_tool_names()
    check(len(names) >= 15, f"the Frames source names a tool set ({len(names)} found)")
    for name in names:
        check((TOOLS / name).is_file(), f"{name} is shipped")


def test_sibling_layout() -> None:
    """gpu_tool_path() resolves TOOLKIT/../psx_analysis. Both halves of that."""
    print("layout")
    check(TOOLS.is_dir(), "tools/psx_analysis exists")
    check(TOOLKIT.is_dir(), "tools/new_project_layout exists")
    check(
        TOOLS.parent == TOOLKIT.parent,
        "the tools directory is a sibling of the toolkit directory",
    )

    cml = (_REPO / "CMakeLists.txt").read_text(encoding="utf-8", errors="replace")
    check(
        "DESTINATION share/retcomm-studio/psx_analysis" in cml,
        "a packaged install ships the tools",
    )
    check(
        "DESTINATION share/retcomm-studio/toolkit" in cml,
        "...beside the toolkit, so the same ../ lookup resolves when installed",
    )

    data = (_REPO / "src" / "studio" / "studio_frames_data.cpp").read_text(
        encoding="utf-8", errors="replace"
    )
    check(
        'parent_path() / "psx_analysis"' in data,
        "gpu_tool_path() looks for Studio's own copy",
    )
    check(
        data.index('parent_path() / "psx_analysis"')
        < data.index('fs::path(root) / "psxrecomp" / "tools"'),
        "and prefers it over the port's psxrecomp checkout",
    )


def test_oracle_carries_its_patch() -> None:
    """duckstation_oracle.py resolves PATCH_DIR = HERE / 'duckstation'."""
    print("oracle")
    oracle = TOOLS / "duckstation_oracle.py"
    check(oracle.is_file(), "duckstation_oracle.py is shipped")
    if not oracle.is_file():
        return
    body = oracle.read_text(encoding="utf-8", errors="replace")
    check('PATCH_DIR = HERE / "duckstation"' in body, "it resolves its patch dir itself")
    for name in ("pin.json", "psxrecomp_oracle.patch", "Containerfile"):
        check((TOOLS / "duckstation" / name).is_file(), f"duckstation/{name} travelled with it")
    check(
        "--memcard" in body,
        "start takes --memcard (Studio passes it; argparse aborts on an unknown flag)",
    )


def test_tools_import_only_siblings() -> None:
    """A tool that imports something left behind fails at the first click.

    The tools use `sys.path.insert(0, dirname(__file__))` and import each other
    by bare name, so a missing sibling is an ImportError at run time — not at
    copy time, and not in any build.
    """
    print("imports")
    import ast

    stdlib = set(sys.stdlib_module_names)
    optional = {"numpy", "PIL"}  # imported behind try/except, or for one report
    here = {p.stem for p in TOOLS.glob("*.py")}
    unresolved: list[str] = []
    for tool in sorted(TOOLS.glob("*.py")):
        # ast, not a regex: these files document their own usage in prose, and
        # a docstring line beginning "from a fresh boot" is not an import.
        try:
            tree = ast.parse(tool.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            unresolved.append(f"{tool.name} does not parse: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [(node.module or "").split(".")[0]] if node.level == 0 else []
            else:
                continue
            for mod in mods:
                if not mod or mod in stdlib or mod in optional or mod in here:
                    continue
                unresolved.append(f"{tool.name} -> {mod}")
    check(not unresolved, f"every import resolves to a sibling or the stdlib ({unresolved[:4]})")


def main() -> int:
    if not TOOLS.is_dir():
        print(f"FAIL  {TOOLS} is missing")
        return 1
    test_every_named_tool_ships()
    test_sibling_layout()
    test_oracle_carries_its_patch()
    test_tools_import_only_siblings()
    print("FAILED" if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
