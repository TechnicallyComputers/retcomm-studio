#!/usr/bin/env python3
"""The N64 oracle tools: ctest verdict parsing, pin discovery, and refusal.

The two behaviours worth testing here are the two that read as success when
they are wrong:

  * a SKIP counted as a pass. n64lle's gates exit 77 when an anchor ROM is
    absent, so a suite can report green having run almost nothing.
  * a build made against an off-pin Ares tree. That oracle answers every query
    and grades every gate, with a reference nothing in the evidence trail
    describes — so `setup` must REFUSE rather than warn.

The ctest fixture is real output shape; the n64lle tree is synthetic, because
the point is the refusal path and a genuine checkout is always on-pin.

Run:  python3 tests/n64_analysis_test.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
TOOLS = _REPO / "tools" / "n64_analysis"
sys.path.insert(0, str(TOOLS))

import n64_gates as gates  # noqa: E402
import n64_oracle as oracle  # noqa: E402

failures = 0


def check(cond: bool, what: str) -> None:
    global failures
    if not cond:
        print(f"  FAIL  {what}")
        failures += 1
    else:
        print(f"  ok    {what}")


# --------------------------------------------------------------------------
# ctest verdict parsing
# --------------------------------------------------------------------------

CTEST = """Test project /home/x/n64lle/build-oracle
    Start 43: cosim_gate2_oracle
1/4 Test #43: cosim_gate2_oracle ...............   Passed   55.83 sec
    Start 45: rsp_attract_invariant_snap
2/4 Test #45: rsp_attract_invariant_snap .......***Skipped   0.05 sec
    Start 13: fpu_fixture
3/4 Test #13: fpu_fixture ......................***Failed   0.01 sec
    Start 51: rdp_frame_gate_mm
4/4 Test #51: rdp_frame_gate_mm ................   Passed   18.67 sec

75% tests passed, 1 tests failed out of 4
"""


def test_verdicts() -> None:
    rows = gates.parse(CTEST)
    check(len(rows) == 4, "parses every test line")
    by = {r["name"]: r for r in rows}
    check(by["cosim_gate2_oracle"]["status"] == "Passed", "pass is a pass")
    check(by["rsp_attract_invariant_snap"]["status"] == "Skipped",
          "skip stays a skip, not a pass")
    check(by["fpu_fixture"]["status"] == "Failed", "failure is a failure")
    check(abs(by["cosim_gate2_oracle"]["seconds"] - 55.83) < 1e-6,
          "seconds survive the *** prefix stripping")

    passed = sum(1 for r in rows if r["status"] == "Passed")
    skipped = sum(1 for r in rows if r["status"] == "Skipped")
    check(passed == 2 and skipped == 1,
          "counts keep pass and skip apart")


def test_no_match_is_not_a_pass() -> None:
    check(gates.parse("") == [], "empty ctest output yields no rows")
    check(gates.parse("Total Tests: 0") == [],
          "a run that matched nothing yields no rows (the caller "
          "reports it as an error, never as green)")


# --------------------------------------------------------------------------
# pin discovery and refusal
# --------------------------------------------------------------------------

REAL_SHA = "0aafd85789215e84e1e43415c07d4c88461b7899"
OTHER_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def _fake_n64lle(root: Path, sha: str) -> Path:
    n = root / "n64lle"
    (n / "n64ref" / "patches").mkdir(parents=True)
    (n / "n64ref" / "ORACLE-PIN.md").write_text(
        f"| HEAD SHA | **`{sha}`** (2026-05-28) |\n", encoding="utf-8")
    for name in ("0001-a.patch", "0002-b.patch"):
        (n / "n64ref" / "patches" / name).write_text("", encoding="utf-8")
    return n


def test_pin_discovery() -> None:
    with tempfile.TemporaryDirectory() as td:
        n = _fake_n64lle(Path(td), REAL_SHA)
        check(oracle.pinned_sha(n) == REAL_SHA, "pinned SHA read from the record")
        check(oracle.patch_list(n) == ["0001-a.patch", "0002-b.patch"],
              "patch list is every .patch, sorted")
        # No submodule checked out -> None, which `setup` treats as "initialise",
        # NOT as a mismatch. Those are different situations with different fixes.
        check(oracle.submodule_sha(n) is None,
              "absent submodule reads as absent, not as a mismatch")
        check(oracle.find_n64lle(str(n)) == n.resolve(),
              "explicit path wins")
        check(oracle.find_n64lle(str(Path(td) / "nope")) is None or True,
              "a bad explicit path falls through rather than raising")


def test_setup_refuses_off_pin() -> None:
    """`setup` must refuse an off-pin tree, and refuse it BEFORE building."""
    with tempfile.TemporaryDirectory() as td:
        n = _fake_n64lle(Path(td), REAL_SHA)
        # A real git repo at a SHA that is not the pinned one.
        ares = n / "n64ref" / "third_party" / "ares" / "ares" / "ares"
        ares.mkdir(parents=True)
        (ares / "ares.hpp").write_text("", encoding="utf-8")
        repo = n / "n64ref" / "third_party" / "ares"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "x"],
                       check=True, capture_output=True)

        have = oracle.submodule_sha(n)
        check(have is not None and have != REAL_SHA,
              "fixture really is off-pin")

        r = subprocess.run(
            [sys.executable, str(TOOLS / "n64_oracle.py"),
             "--root", str(Path(td) / "oracleroot"),
             "--n64lle", str(n), "setup"],
            capture_output=True, text=True, timeout=120)
        check(r.returncode != 0, "setup refuses an off-pin tree")
        check("owner-signed" in r.stdout or "owner-signed" in r.stderr,
              "the refusal says WHY (a pin change is an owner-signed event)")
        check("build_ares" not in r.stdout,
              "it refuses BEFORE running the Ares build")


def test_status_json_shape() -> None:
    """Studio's parser keys off these names; renaming one silently empties a pane."""
    with tempfile.TemporaryDirectory() as td:
        n = _fake_n64lle(Path(td), REAL_SHA)
        r = subprocess.run(
            [sys.executable, str(TOOLS / "n64_oracle.py"),
             "--root", str(Path(td) / "oracleroot"),
             "--n64lle", str(n), "status", "--json"],
            capture_output=True, text=True, timeout=60)
        check(r.returncode == 0, "status --json succeeds on an unbuilt tree")
        import json
        doc = json.loads(r.stdout)
        for key in ("root", "n64lle", "installed", "binary", "build_dir",
                    "ares_pinned", "ares_present", "pin_ok", "patches",
                    "running_pid", "running_port", "logfile"):
            check(key in doc, f"status --json carries `{key}`")
        check(doc["installed"] is False, "unbuilt tree reports installed=false")
        check(doc["pin_ok"] is False,
              "pin_ok is false when the submodule is absent — never optimistic")


def main() -> int:
    print("ctest verdicts")
    test_verdicts()
    test_no_match_is_not_a_pass()
    print("pin discovery")
    test_pin_discovery()
    print("refusal")
    test_setup_refuses_off_pin()
    print("status contract")
    test_status_json_shape()
    if failures:
        print(f"\n{failures} check(s) failed")
        return 1
    print("\nPASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
