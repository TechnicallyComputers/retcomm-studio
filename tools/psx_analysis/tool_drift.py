#!/usr/bin/env python3
"""tool_drift.py -- which Studio tools have an upstream twin, and has it moved?

    python3 tool_drift.py                    # report
    python3 tool_drift.py --check            # exit 1 on unexpected drift (CI)
    python3 tool_drift.py --accept           # record current upstream hashes

Why
---
`gpu_tool_path()` (studio_frames_data.cpp) resolves **Studio's** copy of a tool
first and falls back to `psxrecomp/tools/`. That is deliberate -- these tools
were lost once to a `git pull --rebase` in a game's submodule, and which of them
a project HAD used to depend on the framework revision it pinned.

The cost of that choice is silent divergence in the other direction: some of
Studio's tools began as forks of upstream's, and when upstream fixes one, the
fork never hears about it. This session is the worked example -- an entire
debugging pass ran on psxrecomp's 89-line `disasm_ram.py` because
`docs/GPU_FRAME_TOOLS.md` points at `psxrecomp/tools/`, while Studio's 119-line
copy sat unused.

So: forking is fine, and drifting is fine. **Not knowing** is the bug. This
records the upstream hash we forked from and tells you when upstream moves.

`drift.json` holds `{tool: {"upstream_sha256": ..., "note": ...}}`. A tool that
is Studio-only has no entry and is never reported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
STATE = HERE / "drift.json"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def find_psxrecomp_tools(explicit: Optional[str]) -> Optional[Path]:
    cands: List[Path] = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    env = os.environ.get("PSXRECOMP_DIR")
    if env:
        cands.append(Path(env).expanduser() / "tools")
    cwd = Path.cwd()
    for base in (cwd, *cwd.parents):
        cands.append(base / "psxrecomp" / "tools")
    for c in cands:
        if c.is_dir():
            return c.resolve()
    return None


def load_state() -> Dict[str, Dict[str, str]]:
    if STATE.is_file():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {}


def survey(up: Path) -> Tuple[List[tuple], List[str]]:
    """Return (rows, studio_only). rows = (tool, status, detail)."""
    state = load_state()
    rows: List[tuple] = []
    studio_only: List[str] = []
    for f in sorted(HERE.glob("*.py")):
        if f.name == Path(__file__).name:
            continue
        twin = up / f.name
        if not twin.is_file():
            studio_only.append(f.name)
            continue
        mine, theirs = sha256(f), sha256(twin)
        rec = state.get(f.name)
        kind = "vendored copy" if mine == theirs else "fork"
        # What matters is whether UPSTREAM has moved since we last looked --
        # not whether our copy happens to be identical. A vendored copy is a
        # legitimate steady state (we ship it so availability does not depend
        # on the pinned framework revision), so flagging it forever would make
        # --check fail every run, and a check that always fails is a check
        # everyone learns to ignore.
        if rec is None:
            rows.append((f.name, f"{kind}, UNRECORDED", True,
                         "no upstream baseline recorded; run --accept once you "
                         "have looked at what it was taken from"))
        elif rec.get("upstream_sha256") != theirs:
            rows.append((f.name, f"{kind}, UPSTREAM MOVED", True,
                         "upstream changed since this baseline — review the "
                         "upstream diff, port anything that matters, re-accept"))
        else:
            rows.append((f.name, f"{kind}, upstream unchanged", False, ""))
    return rows, studio_only


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--psxrecomp-tools", default=None)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if anything needs attention (CI)")
    ap.add_argument("--accept", action="store_true",
                    help="record current upstream hashes as the fork baseline")
    args = ap.parse_args(argv)

    up = find_psxrecomp_tools(args.psxrecomp_tools)
    if not up:
        print("no psxrecomp/tools checkout found — nothing to compare against.")
        print("(that is fine: Studio's tools stand alone by design)")
        return 0

    rows, studio_only = survey(up)

    if args.accept:
        state = load_state()
        n = 0
        for name, status, needs, _ in rows:
            if needs:
                state[name] = {
                    "upstream_sha256": sha256(up / name),
                    "note": "baseline recorded by tool_drift.py --accept",
                }
                n += 1
        STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        print(f"recorded {n} upstream baseline(s) in {STATE.name}")
        return 0

    print(f"Studio tools: {HERE}")
    print(f"upstream    : {up}")
    print()
    attention = 0
    for name, status, flag, detail in rows:
        if flag:
            attention += 1
        print(f"  {'! ' if flag else '  '}{name:<26} {status}")
        if detail:
            print(f"      {detail}")
    print()
    print(f"  {len(studio_only)} Studio-only tool(s) with no upstream twin "
          f"(nothing to drift)")
    if attention:
        print()
        print(f"{attention} tool(s) need attention. `--accept` records the "
              f"current upstream state as the baseline once you have looked.")
    return 1 if (args.check and attention) else 0


if __name__ == "__main__":
    raise SystemExit(main())
