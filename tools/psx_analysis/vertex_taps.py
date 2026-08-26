#!/usr/bin/env python3
"""vertex_taps.py -- what the colour routine actually loads, per vertex.

    python3 tools/vertex_taps.py

Native-only, phase-free. The oracle side of this investigation is settled:
across 9 samples spanning the whole fade (brightness 3..220) its 64 additive
quads carry exactly 3 colours; psx-runtime's carry 122-153 at every
brightness. What is NOT known is where native's extra values enter. The
routine per vertex is

    lwl $t6,-9($s4) / lwr $t6,-12($s4)     ; source colour word -> $t6
    swl/swr -> stack ; lbu ; mult $s6 ; sra ; sb ; lw ; sw -> packet

The probe records every GPR, so one frame of hits yields, per vertex, the
source POINTER ($s4), the loaded WORD ($t6), and the scale ($s6). If the
$t6 values are already varied, the corruption is upstream in the table or the
pointer; if $t6 is uniform and the packet is varied, it is in the staging
between $t6 and the packet -- a dozen instructions.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import DEFAULT_NATIVE_PORT, DebugConn, DebugError  # noqa: E402
from scale_within_frame import sample_one_frame  # noqa: E402

KIND = "psx-vertex-taps"


def rows_of(hits, regs=("s4", "t6", "s6")):
    """One row per probe hit: pc + the requested registers."""
    out = []
    for h in hits:
        r = h.get("regs") or {}
        if not r:
            continue
        row = {"pc": h.get("pc", "?")}
        for name in regs:
            v = r.get(name)
            row[name] = int(v, 16) if v else None
        out.append(row)
    return out


def word_as_colour(w):
    return (w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF)


def summarise(rows):
    """Distinct pointers and loaded words across one frame."""
    ptrs = collections.Counter(r["s4"] for r in rows if r["s4"] is not None)
    words = collections.Counter(r["t6"] for r in rows if r["t6"] is not None)
    scales = collections.Counter(r["s6"] for r in rows if r["s6"] is not None)
    return {
        "hits": len(rows),
        "distinct_s4": len(ptrs),
        "s4_values": [f"0x{v:08X}" for v, _ in ptrs.most_common(12)],
        "distinct_t6": len(words),
        "t6_as_colours": [[list(word_as_colour(w)), n]
                          for w, n in words.most_common(16)],
        "distinct_s6": len(scales),
        "s6_values": sorted(scales),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--pc", default="0x80068458",
                    help="just after the lwl/lwr pair completes in $t6")
    ap.add_argument("--n", type=int, default=64)
    ap.add_argument("--frames", type=int, default=4)
    ap.add_argument("--wait-secs", type=float, default=120.0)
    ap.add_argument("--out", default="analysis/frames/vertex_taps.json")
    args = ap.parse_args()

    pc = int(args.pc, 0)
    conn = DebugConn(args.host, args.port, args.timeout)
    print(f"tapping $s4/$t6/$s6 at {args.pc} — trigger the effect now",
          flush=True)

    frames = []
    deadline = time.monotonic() + args.wait_secs
    try:
        conn.cmd("pause")
        while len(frames) < args.frames and time.monotonic() < deadline:
            try:
                hits, _ = sample_one_frame(conn, pc, args.n)
            except DebugError as e:
                print(f"  probe failed: {e}", file=sys.stderr)
                break
            rows = rows_of(hits)
            if not rows:
                continue
            s = summarise(rows)
            s["rows"] = rows[:96]
            frames.append(s)
            print(f"  frame {len(frames)}: {s['hits']} hits, "
                  f"{s['distinct_s4']} pointers, {s['distinct_t6']} loaded "
                  f"words, s6={s['s6_values']}", flush=True)
            for c, n in s["t6_as_colours"][:8]:
                print(f"      loaded {tuple(c)}  x{n}", flush=True)
    finally:
        try:
            conn.cmd("continue")
        except DebugError:
            pass

    if not frames:
        print("no hits -- the probe did not fire inside the effect.")
        return 1

    worst = max(frames, key=lambda f: f["distinct_t6"])
    doc = {"kind": KIND, "version": 1, "pc": args.pc, "frames": frames}
    if worst["distinct_t6"] <= 4:
        doc["verdict"] = "source-loads-uniform"
        doc["explanation"] = (
            f"the routine loads at most {worst['distinct_t6']} distinct "
            f"source words per frame, matching the oracle's 3 -- so the extra "
            f"packet colours are created BETWEEN $t6 and the packet word, in "
            f"the staging/scale/reassemble sequence.")
    else:
        doc["verdict"] = "source-loads-varied"
        doc["explanation"] = (
            f"the routine loads {worst['distinct_t6']} distinct source words "
            f"in one frame -- the variety exists before any arithmetic, so "
            f"the pointer or the table it walks is already wrong.")
    print(f"\nVERDICT: {doc['verdict']}\n{doc['explanation']}")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
