#!/usr/bin/env python3
"""ring_probe.py -- did the sector ring starve the guest's DMA?

    python3 tools/ring_probe.py        # after playing to the artifact screen

This reads one number from a DIAGNOSTIC build. It is not a fix and the game
is expected to still render wrong; the question is only WHY the ring
regresses, which three attempts failed to answer because they all differed in
the notification path and all failed identically.

The suspect is deliver_read_sector_without_irq(): it raises no INT1 by
design, existing purely to refill the buffer so an in-flight multi-sector DMA
keeps draining. With one buffer the refill lands in the buffer the DMA is
reading. With a ring it lands in a NEW slot, and nothing moves the read
pointer there -- so the DMA starves mid-transfer and the guest retries.

  ring_starved > 0  -> confirmed. A drain found its slot exhausted while a
                       newer slot held data. The fix follows: the no-IRQ
                       refill must advance the read pointer, because it is a
                       continuation of the drain already in progress.
  ring_starved == 0 -> dead. The ring fails for some other reason, and this
                       line of attack stops.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import DEFAULT_NATIVE_PORT, DebugConn, DebugError  # noqa: E402

KIND = "psx-ring-probe"
FIELDS = ("ring_starved", "ring_norq_refill", "ring_read_moves",
          "ring_dropped", "int1_lost", "int1_pended")


def verdict_of(st):
    starved = int(st.get("ring_starved", 0))
    norq = int(st.get("ring_norq_refill", 0))
    if "ring_starved" not in st:
        return ("no-instrumentation",
                "this psx-runtime has no ring counters -- it is not the "
                "diagnostic build. Rebuild and restart it.")
    if starved > 0:
        return ("starvation-confirmed",
                f"{starved} drain(s) found the read slot exhausted while "
                f"another slot held unread data, and {norq} no-IRQ refill(s) "
                f"landed off the read slot. That is the regression: a "
                f"multi-sector DMA stalls because its continuation was filed "
                f"in a slot nothing announces. The fix is that the no-IRQ "
                f"refill must advance the read pointer -- it is a "
                f"continuation of the drain already in progress, not a new "
                f"notification.")
    if norq > 0:
        return ("refills-off-slot-but-no-starvation",
                f"{norq} no-IRQ refill(s) landed off the read slot, yet no "
                f"drain ever starved. The refills are reaching the guest some "
                f"other way, so starvation is not the regression -- and this "
                f"hypothesis is dead.")
    return ("hypothesis-dead",
            "no starvation and no off-slot refills. The ring regresses for a "
            "reason unrelated to the no-IRQ refill path, and I stop proposing "
            "the ring.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--out", default="analysis/frames/ring_probe.json")
    args = ap.parse_args()

    try:
        st = DebugConn(args.host, args.port, args.timeout).cmd("cdrom_state")
    except DebugError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print("ring counters:")
    for f in FIELDS:
        print(f"  {f:<18} {st.get(f, '(absent)')}")

    v, why = verdict_of(st)
    doc = {"kind": KIND, "version": 1,
           "counters": {f: st.get(f) for f in FIELDS},
           "verdict": v, "explanation": why}
    print(f"\nVERDICT: {v}\n{why}")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"\nreport: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
