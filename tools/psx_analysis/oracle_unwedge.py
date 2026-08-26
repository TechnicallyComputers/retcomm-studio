#!/usr/bin/env python3
"""oracle_unwedge.py -- get a stuck DuckStation oracle running again.

    python3 tools/oracle_unwedge.py

A breakpoint left armed by an earlier tool keeps firing and re-pausing the
emulator, so unpausing it by hand bounces straight back. That state lives in
DuckStation, not in the tool that set it, so it survives restarting the
script -- but not restarting DuckStation.

This removes every execute breakpoint the oracle holds, resumes it, and says
what it found. Run it whenever the oracle seems stuck.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_DUCKSTATION_PORT, DebugConn, DebugError, oracle_break_list,
    oracle_clear_breaks, oracle_resume,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_DUCKSTATION_PORT)
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    conn = DebugConn(args.host, args.port, args.timeout)
    try:
        held = oracle_break_list(conn)
    except DebugError as e:
        print(f"could not reach the oracle on port {args.port}: {e}")
        return 1

    if held:
        print("breakpoints held: " + ", ".join(f"0x{a:08X}" for a in held))
    else:
        print("no breakpoints held")

    # Resume first: a paused oracle answers at about 1 Hz, which is what makes
    # the removal below time out in the first place.
    oracle_resume(conn)
    try:
        n = oracle_clear_breaks(conn)
        if n:
            print(f"removed {n} breakpoint(s)")
    except DebugError as e:
        print(f"FAILED: {e}")
        return 1
    running = oracle_resume(conn)
    print("oracle resumed" if running else
          "could not confirm the oracle resumed; restart DuckStation")
    return 0 if running else 1


if __name__ == "__main__":
    sys.exit(main())
