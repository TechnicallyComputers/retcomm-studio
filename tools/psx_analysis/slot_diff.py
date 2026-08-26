#!/usr/bin/env python3
"""slot_diff.py -- diff the two emulators' CD DMA slot maps for one load.

    python3 tools/slot_diff.py        # both emulators, after playing the scene

The last unmeasured thing. Native's slot map is known in detail -- which
transfer carried which sector to which address -- while the oracle's was only
ever inferred. The palette reaches its table at 0x0E2718 through some DMA on
DuckStation; on psx-runtime that address receives sector 125113 instead. Both
emulators issue byte-identical CD requests and deliver every one of them
exactly, so the divergence is in the TRANSFERS, and this reads them off both
sides instead of reasoning about them.

Entries are keyed by destination address, which is stable across runs (the
game's buffers are fixed), so the two maps line up without any phase matching.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_DUCKSTATION_PORT, DEFAULT_NATIVE_PORT, DebugConn, DebugError,
)

KIND = "psx-slot-diff"


def slot_map(entries, lba_lo, lba_hi):
    """dest -> transfer record, for DMAs whose sector is in the window.

    Keyed by destination: the same buffer address on both sides is the same
    slot of the same file, whatever order the transfers happened in.
    """
    out = {}
    for e in entries:
        lba = int(e.get("delivered_lba", e.get("lba", -1)))
        req = int(e.get("lba", -1))
        if not (lba_lo <= lba <= lba_hi or lba_lo <= req <= lba_hi):
            continue
        dest = int(e["dest"], 16) & 0x1FFFFF
        out[dest] = {"dest": dest, "requested": req, "delivered": lba,
                     "size": int(e["size"]),
                     "data": e.get("first_words"),
                     "frame": int(e.get("frame", -1))}
    return out


def compare(nat, orc):
    """Per-destination verdicts, most interesting first."""
    rows = []
    for dest in sorted(set(nat) | set(orc)):
        a, b = nat.get(dest), orc.get(dest)
        if a and b:
            same = a["delivered"] == b["delivered"]
            rows.append({"dest": dest, "native": a, "oracle": b,
                         "state": "same" if same else "DIFFERENT SECTOR"})
        else:
            rows.append({"dest": dest, "native": a, "oracle": b,
                         "state": "only-native" if a else "only-oracle"})
    rows.sort(key=lambda r: (r["state"] == "same", r["dest"]))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--ds-port", type=int, default=DEFAULT_DUCKSTATION_PORT)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--tail", type=int, default=4096)
    ap.add_argument("--max-entries", type=int, default=256,
                    help="server-side cap; the oracle's socket buffer "
                         "truncates a large reply and the read then hangs")
    ap.add_argument("--lba-lo", type=int, default=125080)
    ap.add_argument("--lba-hi", type=int, default=125130)
    ap.add_argument("--out", default="analysis/frames/slot_diff.json")
    args = ap.parse_args()

    sides = {}
    for label, port in (("native", args.port), ("oracle", args.ds_port)):
        conn = DebugConn(args.host, port, args.timeout)
        try:
            # The oracle filters server-side (its socket buffer truncates a
            # large reply and the read then hangs); psx-runtime ignores the
            # extra fields harmlessly, so one call shape serves both.
            rep = conn.cmd("cd_read_log", tail=args.tail,
                           lba_lo=args.lba_lo, lba_hi=args.lba_hi,
                           max_entries=args.max_entries)
            sides[label] = rep.get("entries", [])
        except DebugError as e:
            print(f"{label}: {e}", file=sys.stderr)
            if label == "oracle":
                print("  (rebuild + reinstall the oracle, restart it, then "
                      "replay the scene there)", file=sys.stderr)
            return 2

    nat = slot_map(sides["native"], args.lba_lo, args.lba_hi)
    orc = slot_map(sides["oracle"], args.lba_lo, args.lba_hi)
    print(f"native: {len(nat)} slot(s) in LBA {args.lba_lo}..{args.lba_hi}; "
          f"oracle: {len(orc)}")
    if not nat or not orc:
        # Distinguish "never loaded it" from "loaded it, then the ring
        # scrolled past" -- the second is what happens when the effect is
        # followed by FMV, and it needs a different response from the user.
        for label, port in (("native", args.port), ("oracle", args.ds_port)):
            if (nat if label == "native" else orc):
                continue
            # The window query was filtered SERVER-side, so an empty reply
            # says nothing about whether a log exists. Ask again unfiltered
            # before reporting -- otherwise "hasn't loaded this file yet"
            # reads as "isn't running the patched build".
            lbas = []
            try:
                probe = DebugConn(args.host, port, args.timeout).cmd(
                    "cd_read_log", tail=65536, max_entries=400)
                lbas = [int(e.get("delivered_lba", e.get("lba", -1)))
                        for e in probe.get("entries", [])]
                lbas = [v for v in lbas if v >= 0]
                total = probe.get("total", 0)
            except DebugError:
                total = 0
            if lbas and min(lbas) > args.lba_hi:
                print(f"  {label}: the log holds LBA {min(lbas)}..{max(lbas)} "
                      f"-- entirely PAST the palette load, so the ring "
                      f"scrolled. Replay the scene and run this promptly, "
                      f"before FMV or further loading flushes it.")
            elif lbas:
                print(f"  {label}: {total} logged DMA(s) spanning LBA "
                      f"{min(lbas)}..{max(lbas)}, but none in "
                      f"{args.lba_lo}..{args.lba_hi} -- this side has not "
                      f"loaded the palette file yet. Play it to the same "
                      f"screen (the artifact/map screen) there.")
            else:
                print(f"  {label} has no CD DMA log at all -- is it running "
                      f"the patched build?")
        return 1

    rows = compare(nat, orc)
    doc = {"kind": KIND, "version": 1, "rows": rows}
    print(f"\n{'dest':>10}  {'native':>26}  {'oracle':>26}  state")
    for r in rows:
        def fmt(x):
            if not x:
                return f"{'--':>26}"
            return (f"req {x['requested']:>7} got {x['delivered']:>7} "
                    f"{x['size']:>5}B")
        print(f"  0x{r['dest']:06X}  {fmt(r['native'])}  {fmt(r['oracle'])}  "
              f"{r['state']}")

    diff = [r for r in rows if r["state"] != "same"]
    doc["differing"] = diff
    if not diff:
        print("\nEvery slot received the same sector on both sides. The "
              "transfers agree -- the divergence is after the DMA, in what "
              "the game does with the bytes.")
    else:
        print(f"\n{len(diff)} slot(s) differ. For each, the two sides drained "
              f"a different sector into the same buffer -- that is the "
              f"mechanism, and the data heads show which bytes each got.")
        for r in diff[:6]:
            for side in ("native", "oracle"):
                x = r[side]
                if x and x.get("data"):
                    print(f"    0x{r['dest']:06X} {side:>7}: sector "
                          f"{x['delivered']} data={x['data'][0]},{x['data'][1]}")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"\nreport: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
