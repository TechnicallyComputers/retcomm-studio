#!/usr/bin/env python3
"""wedge_scan.py -- rank every frame the GP0 ring still holds by how much its
geometry looks like the hard-edged wedge fan, and dump the winners.

    # play until the wedges are on screen, THEN run this
    python3 tools/wedge_scan.py --last 240 --out analysis/frames

Why this shape
--------------
Triggering live on a guessed predicate does not work: the threshold has to be
calibrated against a wedge frame's packet list, and that list is exactly what
nobody has ever captured. So do not guess. The ring holds several hundred
frames, so the wedges are still in it a good few seconds after they flash --
scan backwards, score every frame, and rank. Whatever the wedge frame is, it
sorts to the top of a ranking; it does not have to clear a number I made up.

Ranking is by the count of large untextured polygons, because that is what a
wedge fan is made of, with large textured polygons and the largest shared
vertex (the hub a fan radiates from) reported alongside so a wrong guess about
which class carries the wedges is visible in the table rather than silently
filtered out.

If the top of the ranking looks like every other frame, that is a result too:
it means the packet lists are ordinary and the fault is in how psx-runtime
rasterises them, not in what the game submitted.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_NATIVE_PORT, DebugConn, DebugError, capture, save_dump, save_summary,
)

KIND = "psx-wedge-scan"


def prim_span(prim):
    """Longest axis of a primitive's bounding box, in pixels."""
    vs = prim.get("verts") or []
    if len(vs) < 3:
        return 0
    xs = [v[0] for v in vs]
    ys = [v[1] for v in vs]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def frame_metrics(dump, big=120):
    """Geometry profile of one frame. No thresholds decide anything here."""
    big_untex = big_tex = 0
    max_span = 0
    stp = collections.Counter()
    hub = collections.Counter()
    xs_all = []
    ys_all = []
    colours = set()
    for p in dump.get("prims", []):
        vs = p.get("verts") or []
        if len(vs) < 3:
            continue
        s = prim_span(p)
        if s > max_span:
            max_span = s
        if p.get("semi"):
            stp[int(p.get("stp") or 0)] += 1
        if s >= big:
            if p.get("textured"):
                big_tex += 1
            else:
                big_untex += 1
                for v in vs:
                    hub[tuple(v)] += 1
        for v in vs:
            xs_all.append(v[0])
            ys_all.append(v[1])
        for c in p.get("colors") or []:
            colours.add(tuple(c))
    hub_v, hub_n = (hub.most_common(1) or [((0, 0), 0)])[0]
    return {
        "frame": dump.get("frame"),
        "prims": len(dump.get("prims", [])),
        "additive": stp[1],
        "subtractive": stp[2],
        "big_untextured": big_untex,
        "big_textured": big_tex,
        "max_span": max_span,
        "fan_hub": list(hub_v),
        "fan_hub_count": hub_n,
        "extent": [min(xs_all, default=0), min(ys_all, default=0),
                   max(xs_all, default=0), max(ys_all, default=0)],
        "distinct_colours": len(colours),
    }


def rank_key(m):
    """Sort order: wedge-likeness. Most large untextured polys first, then the
    biggest fan hub, then raw size."""
    return (m["big_untextured"], m["fan_hub_count"], m["max_span"])


def scan_frames(span, last, stride):
    """Newest-first frame list to profile."""
    newest, oldest = span["newest"], span["oldest"]
    lo = oldest if last <= 0 else max(oldest, newest - last + 1)
    return list(range(newest, lo - 1, -stride))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--last", type=int, default=0,
                    help="how many of the newest ring frames to cover "
                         "(0 = all, the default: the effect is usually not in "
                         "the newest frames)")
    ap.add_argument("--stride", type=int, default=4,
                    help="profile every Nth frame")
    ap.add_argument("--rank", choices=("wedge", "glow"), default="wedge",
                    help="wedge = most large untextured polys; "
                         "glow = most additive primitives (finds the effect)")
    ap.add_argument("--big", type=int, default=120,
                    help="pixels: what counts as a large polygon (reporting only)")
    ap.add_argument("--count", type=int, default=20000, help="packet cap per frame")
    ap.add_argument("--top", type=int, default=10, help="rows to print")
    ap.add_argument("--save", type=int, default=1,
                    help="dump this many top-ranked frames")
    ap.add_argument("--context", type=int, default=2,
                    help="also dump this many frames before each saved frame")
    ap.add_argument("--tag", default="wedge")
    ap.add_argument("--out", default="analysis/frames")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    conn = DebugConn(args.host, args.port, timeout=args.timeout)
    span = conn.ring_span()
    if span["total"] == 0:
        print("the GP0 ring is empty -- is psx-runtime running?")
        return 1
    frames = scan_frames(span, args.last, args.stride)
    print(f"ring holds {span['oldest']}..{span['newest']}; "
          f"profiling {len(frames)} frames", flush=True)

    rows = []
    dumps = {}
    for i, fr in enumerate(frames):
        try:
            d = capture(conn, frame=fr, count=args.count, label=args.tag)
        except DebugError:
            continue          # evicted while we scanned; keep going
        m = frame_metrics(d, big=args.big)
        rows.append(m)
        dumps[fr] = d
        if i % 20 == 0:
            print(f"  ...{i}/{len(frames)} (frame {fr})", flush=True)

    if not rows:
        print("no frames could be read from the ring")
        return 1

    # Where is the effect at all? Additive primitives are the glow; a window
    # with none of them did not contain the animation, and any ranking over
    # it is a ranking of identical frames.
    glow = [m for m in rows if m["additive"] > 0]
    if glow:
        lo = min(m["frame"] for m in glow)
        hi = max(m["frame"] for m in glow)
        peak = max(glow, key=lambda m: m["additive"])
        print(f"\nglow (additive prims) present in {len(glow)}/{len(rows)} "
              f"profiled frames, {lo}..{hi}; peak {peak['additive']} at frame "
              f"{peak['frame']}")
    else:
        print(f"\nNO additive primitives in any of {len(rows)} profiled "
              f"frames -- the effect did not play inside this window, so the "
              f"ranking below is a ranking of identical frames. Replay the "
              f"animation and scan again.")

    key = rank_key if args.rank == "wedge" else (
        lambda m: (m["additive"], m["big_untextured"], m["max_span"]))
    rows.sort(key=key, reverse=True)
    print(f"\n{'frame':>8} {'prims':>6} {'big/untex':>10} {'big/tex':>8} "
          f"{'maxspan':>8} {'fanhub':>7} {'add':>6} {'sub':>6} {'colours':>8}  extent")
    for m in rows[:args.top]:
        print(f"{m['frame']:>8} {m['prims']:>6} {m['big_untextured']:>10} "
              f"{m['big_textured']:>8} {m['max_span']:>8} "
              f"{m['fan_hub_count']:>7} {m['additive']:>6} {m['subtractive']:>6} "
              f"{m['distinct_colours']:>8}  {m['extent']}")

    saved = []
    for m in rows[:max(0, args.save)]:
        fr = m["frame"]
        tag = args.tag if not saved else f"{args.tag}{len(saved) + 1}"
        base = os.path.join(args.out, tag)
        save_dump(dumps[fr], base + ".json")
        save_summary(dumps[fr], base + ".summary.json",
                     dump_name=os.path.basename(base + ".json"))
        ctx = []
        for k in range(1, args.context + 1):
            try:
                cd = capture(conn, frame=fr - k, count=args.count,
                             label=f"{tag}-minus{k}")
                cb = os.path.join(args.out, f"{tag}-minus{k}")
                save_dump(cd, cb + ".json")
                ctx.append(cb + ".json")
            except DebugError:
                break
        saved.append({"frame": fr, "dump": base + ".json",
                      "summary": base + ".summary.json",
                      "context_dumps": ctx, "metrics": m})
        print(f"\nsaved frame {fr} -> {base}.json")

    doc = {"kind": KIND, "version": 1, "ring": span, "big": args.big,
           "profiled": len(rows), "ranking": rows[:args.top], "saved": saved,
           "note": "ranking is relative; a flat ranking means the packet lists "
                   "are ordinary and the fault is in rasterisation, not "
                   "submission"}
    rp = os.path.join(args.out, f"{args.tag}_scan.json")
    with open(rp, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"report: {rp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
