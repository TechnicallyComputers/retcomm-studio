#!/usr/bin/env python3
"""gpu_frame_scan.py -- find the frame where the rendering turns.

    python3 gpu_frame_scan.py --last 400
    python3 gpu_frame_scan.py --from 43000 --to 43300 --out analysis/frames/scan.json

Why this exists
---------------
Diffing one "good" frame against one "bad" frame only works if you already know
which two frames those are. Pick them wrong -- one before an effect starts and
one during it -- and the diff faithfully reports that the effect exists, which
is true and useless. That is exactly what a first attempt produces.

This walks the GP0 ring instead and asks a different question: between which two
consecutive frames did the picture change most? The answer is the frame the bug
happens on, and you did not have to know it in advance.

It also does not depend on function attribution, which does not work on a game
that builds an ordering table and DMAs it -- every packet in such a frame
reports the same submit PC, so "which function drew this" has one answer for the
whole screen. Frames are compared by (opcode, blend mode) instead, and each
change carries the packet's source address in guest RAM, which is the thing you
hand to wtrace to find the code that built it.

Two passes: a coarse sweep to bracket the transition, then frame-by-frame inside
the winning interval. Scanning a few hundred frames one at a time would move
hundreds of megabytes over the socket for no extra information.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_NATIVE_PORT, DebugConn, DebugError, capture, frame_signature,
    signature_delta,
)

SCAN_KIND = "psx-gpu-frame-scan"


def sample(conn: DebugConn, frame: int, count: int):
    """Signature for one frame, or None if the ring no longer has it."""
    try:
        dump = capture(conn, frame=frame, count=count, verify_ring=False)
    except DebugError:
        return None
    if dump["raw_count"] == 0:
        return None
    return frame_signature(dump)


def sweep(conn, frames, count, label, quiet=False):
    """Signatures for a list of frames, skipping ones the ring cannot serve."""
    out = []
    for i, f in enumerate(frames):
        sig = sample(conn, f, count)
        if sig is not None:
            out.append(sig)
        if not quiet:
            print(f"\r  {label}: {i + 1}/{len(frames)} frames", end="", flush=True)
    if not quiet:
        print()
    return out


def rank(sigs):
    """Consecutive deltas, worst first."""
    return sorted((signature_delta(sigs[i], sigs[i + 1])
                   for i in range(len(sigs) - 1)),
                  key=lambda d: -d["score"])


def report(deltas, top, out=sys.stdout):
    if not deltas:
        print("nothing to compare — the range produced fewer than two frames "
              "with packets", file=out)
        return
    print(f"\n{'frames':>16}  {'score':>9}  biggest change", file=out)
    for d in deltas[:top]:
        head = d["changes"][0] if d["changes"] else None
        desc = (f"{head['key']}  {head['a']}->{head['b']}"
                if head else "(counts equal)")
        print(f"{d['a']:>7} -> {d['b']:<6}  {d['score']:>9.1f}  {desc}", file=out)

    best = deltas[0]
    print(f"\n=== biggest transition: frame {best['a']} -> {best['b']} ===", file=out)
    for c in best["changes"][:8]:
        print(f"  {c['key']:<26} {c['a']:>5} -> {c['b']:<5} ({c['delta']:+d})", file=out)
        if c["bbox_a"] != c["bbox_b"]:
            print(f"  {'':<26} bbox {c['bbox_a']} -> {c['bbox_b']}", file=out)
        if c["cmax_a"] != c["cmax_b"]:
            print(f"  {'':<26} peak colour {c['cmax_a']} -> {c['cmax_b']}", file=out)
        if c["src_b"]:
            print(f"  {'':<26} packets built at {c['src_b']} — trace the builder "
                  f"with:  wtrace_add addr={c['src_b']}", file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--from", dest="lo", type=int, default=None)
    ap.add_argument("--to", dest="hi", type=int, default=None)
    ap.add_argument("--last", type=int, default=0,
                    help="scan the last N frames the ring holds")
    ap.add_argument("--coarse", type=int, default=24,
                    help="samples in the bracketing pass")
    ap.add_argument("--count", type=int, default=20000, help="packet cap per frame")
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--out", default=None, help="write the scan as JSON here")
    ap.add_argument("--no-refine", action="store_true",
                    help="skip the frame-by-frame pass")
    args = ap.parse_args(argv)

    try:
        with DebugConn(args.host, args.port, args.timeout) as conn:
            span = conn.ring_span()
            lo, hi = args.lo, args.hi
            if args.last:
                hi = span["newest"]
                lo = max(span["oldest"], hi - args.last)
            if lo is None:
                lo = span["oldest"]
            if hi is None:
                hi = span["newest"]
            lo = max(lo, span["oldest"])
            hi = min(hi, span["newest"])
            if hi - lo < 2:
                print(f"error: range {lo}..{hi} is too small (ring holds "
                      f"{span['oldest']}..{span['newest']})", file=sys.stderr)
                return 2
            print(f"ring holds {span['oldest']}..{span['newest']}; scanning {lo}..{hi}")

            step = max(1, (hi - lo) // max(2, args.coarse))
            coarse_frames = list(range(lo, hi + 1, step))
            coarse = sweep(conn, coarse_frames, args.count, "coarse")
            deltas = rank(coarse)
            if not deltas:
                print("no comparable frames in that range", file=sys.stderr)
                return 1

            fine = []
            if not args.no_refine and step > 1:
                a, b = deltas[0]["a"], deltas[0]["b"]
                print(f"  refining {a}..{b} frame by frame")
                fine = sweep(conn, list(range(a, b + 1)), args.count, "refine")
                fdeltas = rank(fine)
                if fdeltas:
                    deltas = fdeltas

            report(deltas, args.top)

            if args.out:
                doc = {
                    "kind": SCAN_KIND, "version": 1,
                    "range": [lo, hi], "ring": span,
                    "coarse_step": step,
                    "transitions": deltas[:args.top],
                }
                with open(args.out, "w", encoding="utf-8") as f:
                    json.dump(doc, f, indent=1)
                print(f"\nwrote {args.out}")
    except DebugError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
