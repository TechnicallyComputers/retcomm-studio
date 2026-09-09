#!/usr/bin/env python3
"""Run n64lle's differential gates through ctest and report what they said.

Studio shells out to THIS rather than spawning ctest itself, for the same
reason the Frames tab shells out to psxrecomp's own tools: the verdict belongs
to the engine that defines it. Nothing here decides whether two images match —
it selects tests, runs them, and parses the summary.

Two things it does own, because getting either wrong reads as a green suite:

  * A SKIP is reported as a skip. n64lle's gates exit 77 when an anchor ROM is
    absent, and ctest prints "Skipped" rather than failing. "Green because it
    did not run" is the reading this tool exists to prevent, so skipped tests
    are counted separately and the exit code is nonzero only for real failures.
  * The exit code is derived from the parsed results, not passed through.
    ctest's own code conflates "a test failed" with "no tests matched the
    filter", and the second is a typo in a selection, not a red gate.

usage:
    n64_gates.py --build-dir <dir> [-R <regex>] [--json] [--timeout N]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# "  3/10 Test  #43: cosim_gate2_oracle ..........   Passed   55.89 sec"
LINE_RE = re.compile(
    r"Test\s+#\d+:\s+(?P<name>\S+)\s+\.*\s*(?P<verdict>\*{3}\s*\w+|\w+)\s+"
    r"(?P<secs>[\d.]+)?\s*sec"
)


def parse(text: str) -> list:
    out = []
    for line in text.splitlines():
        m = LINE_RE.search(line)
        if not m:
            continue
        verdict = m.group("verdict").replace("*", "").strip()
        out.append({
            "name": m.group("name"),
            "status": verdict,
            "seconds": float(m.group("secs")) if m.group("secs") else 0.0,
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", required=True)
    ap.add_argument("-R", "--tests-regex", default="")
    ap.add_argument("--timeout", type=float, default=5400.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    build = Path(args.build_dir).expanduser()
    if not (build / "CTestTestfile.cmake").is_file():
        msg = f"no configured build at {build}"
        print(json.dumps({"error": msg}) if args.json else msg)
        return 2

    cmd = ["ctest", "--output-on-failure"]
    if args.tests_regex:
        cmd += ["-R", args.tests_regex]

    try:
        r = subprocess.run(cmd, cwd=str(build), capture_output=True, text=True,
                           timeout=args.timeout)
    except subprocess.TimeoutExpired:
        msg = f"ctest timed out after {args.timeout}s"
        print(json.dumps({"error": msg}) if args.json else msg)
        return 2
    except OSError as exc:
        msg = f"could not run ctest: {exc}"
        print(json.dumps({"error": msg}) if args.json else msg)
        return 2

    text = r.stdout + r.stderr
    results = parse(text)
    passed = sum(1 for x in results if x["status"] == "Passed")
    skipped = sum(1 for x in results if x["status"] == "Skipped")
    failed = len(results) - passed - skipped

    if args.json:
        print(json.dumps({
            "results": results,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "matched": len(results),
            "ctest_exit": r.returncode,
        }, indent=1))
    else:
        sys.stdout.write(text)
        print(f"\n{passed} passed, {failed} failed, {skipped} skipped")

    if not results:
        # No test matched. Not a red gate — a selection that named nothing.
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
