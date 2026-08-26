#!/usr/bin/env python3
"""slice_check.py -- are all of the effect's colour words rewritten each frame?

    python3 tools/slice_check.py

Why this decides things
-----------------------
Per frame, the colour routine provably loads ONE source pair and ONE scale
(vertex_taps), and every helper it runs on is verified correct -- yet the
DMA'd buffer carries ~151 distinct colours in a single frame. The one
mechanism that reconciles those, and matches the hard-edged pie sectors on
screen, is AMORTISATION: the game patching only a slice of the 256 colour
words per frame, cycling. With $s6 falling 4 per frame, the resident buffer
then holds colours computed at many different scales, and each stale slice is
a visibly different brightness -- a wedge.

This measures it directly. The write trace records every colour-word write
with its frame number and value, so stepping a few frames yields writes per
frame per colour word. 256 per frame kills the hypothesis; a fraction of 256
confirms it, and the fraction predicts how many scales are resident:

    distinct buffer colours ~= sources x (256 / writes_per_frame)

If confirmed, the game-side code is identical on DuckStation, so the real
divergence is in what drives $s6 -- and the next measurement is DS's $s6
timeline, not more colour forensics.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from packet_writers import BUFFER_STRIDE, fold_to_walked  # noqa: E402
from psx_gpu_frame import (  # noqa: E402
    DEFAULT_NATIVE_PORT, DebugConn, DebugError, capture,
)

KIND = "psx-slice-check"


def colour_addrs(dump, op_name="PolyG4+semi", stp=1):
    """Colour-word addresses of the effect's quads, from a ring frame.

    Shaded quad layout is C0 V0 C1 V1 C2 V2 C3 V3, so colours sit at
    src + 0, 8, 16, 24 bytes.
    """
    out = set()
    for p in dump.get("prims", []):
        if p.get("op_name") != op_name or int(p.get("stp") or 0) != stp:
            continue
        if not p.get("semi"):
            continue
        src = int(p["src"], 16) & 0x1FFFFF
        for k in range(4):
            out.add(src + 8 * k)
    return out


def per_frame(entries, addrs, lo, hi):
    """Group trace entries by frame: distinct colour addresses and values."""
    frames = collections.defaultdict(lambda: {"addrs": set(), "vals": set()})
    for e in entries:
        a = fold_to_walked(int(e["addr"], 16) & 0x1FFFFF, lo, hi)
        if a is None or a not in addrs:
            continue
        f = frames[int(e["frame"])]
        f["addrs"].add(a)
        f["vals"].add(int(e["new"], 16) & 0xFFFFFF)
    return frames


def verdict_of(counts, total):
    if not counts:
        return ("no-writes",
                "no colour-word writes landed in the traced frames -- the "
                "effect was not being built while this ran.")
    typical = sorted(counts)[len(counts) // 2]
    if typical >= total * 0.9:
        return ("full-rewrite",
                f"~{typical} of {total} colour words are rewritten every "
                f"frame. The buffer cannot hold stale scales, so the "
                f"amortisation hypothesis is dead and the extra colours must "
                f"enter somewhere else.")
    resident = max(1, round(total / max(typical, 1)))
    return ("amortised-slice",
            f"only ~{typical} of {total} colour words are rewritten per "
            f"frame -- a slice, cycling. The buffer therefore holds colours "
            f"computed over ~{resident} different frames' scales at once. "
            f"With $s6 changing every frame that is ~{resident} brightness "
            f"bands resident -- the hard-edged wedges. The same code runs on "
            f"DuckStation, so the divergence is in what drives $s6: measure "
            f"the oracle's $s6 timeline next.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--frames", type=int, default=6,
                    help="frames to step while tracing (even; both buffer copies)")
    ap.add_argument("--out", default="analysis/frames/slice_check.json")
    args = ap.parse_args()

    conn = DebugConn(args.host, args.port, args.timeout)
    print("locating the effect's colour words — have the effect playing",
          flush=True)
    try:
        conn.cmd("pause")
        dump = capture(conn, label="slice")
        addrs = colour_addrs(dump)
        if not addrs:
            print("the newest ring frame holds no additive shaded quads -- "
                  "replay the effect and rerun promptly.")
            return 1
        lo, hi = min(addrs), max(addrs)
        print(f"  {len(addrs)} colour words at 0x{lo:06X}..0x{hi:06X}")

        conn.cmd("wtrace_reset")
        conn.cmd("wtrace_add", lo=f"0x{lo - BUFFER_STRIDE:08X}",
                 hi=f"0x{hi + BUFFER_STRIDE + 4:08X}")
        f0 = conn.frame()
        want = max(2, args.frames + (args.frames & 1))
        conn.cmd("step", n=want)
        for _ in range(400):
            st = conn.raw("pause_state")
            if st.get("paused") and conn.frame() > f0:
                break
            time.sleep(0.02)
        f1 = conn.frame()
        rep = conn.cmd("wtrace_dump", addr_lo=f"0x{lo - BUFFER_STRIDE:08X}",
                       addr_hi=f"0x{hi + BUFFER_STRIDE + 4:08X}",
                       frame_lo=f0, frame_hi=f1, count=2048)
        frames = per_frame(rep.get("entries", []), addrs, lo, hi)
    finally:
        try:
            conn.cmd("continue")
        except DebugError:
            pass

    doc = {"kind": KIND, "version": 1, "colour_words": len(addrs),
           "frames": {}}
    counts = []
    for f in sorted(frames):
        info = frames[f]
        counts.append(len(info["addrs"]))
        doc["frames"][str(f)] = {
            "colour_words_written": len(info["addrs"]),
            "distinct_values": len(info["vals"]),
            "values": [f"0x{v:06X}" for v in sorted(info["vals"])[:12]],
        }
        print(f"  frame {f}: {len(info['addrs'])}/{len(addrs)} colour words "
              f"written, {len(info['vals'])} distinct values")

    v, why = verdict_of(counts, len(addrs))
    doc["verdict"], doc["explanation"] = v, why
    print(f"\nVERDICT: {v}\n{why}")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
