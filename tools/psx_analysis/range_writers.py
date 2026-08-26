#!/usr/bin/env python3
"""range_writers.py -- which code writes an arbitrary range of guest RAM.

    python3 range_writers.py --lo 0x000E0BF8 --hi 0x000E6628 \
                             --expect-class PolyG4+semi

Why an arbitrary range
----------------------
packet_writers answers this for a primitive's packets, where the layout is
known and every write can be classified as colour or vertex. This is the same
instrument without that assumption: point it at any span and it reports which
instructions store there, how much, and over what addresses.

It exists because the colour investigation reached a region rather than a
structure. With the effect off, 0x000E0BF8..0x000E6628 is byte-identical
between the two emulators; with it running they differ. So the divergence is in
what the effect WRITES there, and the question became "what writes this?".

Tracing is done over a bounded number of STEPPED frames, for the same reason
packet_writers does it: the game rebuilds its buffers constantly, and a
free-running trace attributes writes to whatever the addresses meant at some
other moment. Registers and code are captured while parked, because these are
overlay addresses -- the same PC decodes differently once another overlay
loads.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from packet_writers import disasm_around  # noqa: E402
from psx_gpu_frame import (  # noqa: E402
    DEFAULT_NATIVE_PORT, DebugConn, DebugError, class_on_screen,
    wait_for_class,
)

KIND = "psx-range-writers"


def summarise(rows, lo, hi, out=sys.stdout, limit=16):
    by_pc = defaultdict(lambda: {"n": 0, "lo": None, "hi": None,
                                 "vals": Counter(), "widths": Counter()})
    for e in rows:
        a = int(e["addr"], 16) & 0x1FFFFFFF
        d = by_pc[e["pc"]]
        d["n"] += 1
        d["lo"] = a if d["lo"] is None else min(d["lo"], a)
        d["hi"] = a if d["hi"] is None else max(d["hi"], a)
        v = e.get("new") or e.get("val")
        if v:
            d["vals"][v] += 1
        if e.get("w"):
            d["widths"][e["w"]] += 1

    ranked = sorted(by_pc.items(), key=lambda kv: -kv[1]["n"])
    print(f"\n{len(rows)} write(s) into 0x{lo:08X}..0x{hi:08X} "
          f"from {len(ranked)} instruction(s)\n", file=out)
    print(f"  {'pc':<12}{'writes':>8}  {'address span':<26}common values", file=out)
    for pc, d in ranked[:limit]:
        span = f"0x{d['lo']:06X}..0x{d['hi']:06X}"
        common = " ".join(f"{v}x{n}" for v, n in d["vals"].most_common(2))
        print(f"  {pc:<12}{d['n']:>8}  {span:<26}{common}", file=out)
    return ranked


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--lo", required=True, help="start of the range (hex)")
    ap.add_argument("--hi", required=True, help="end of the range (hex)")
    ap.add_argument("--frames", type=int, default=1,
                    help="frames to step while tracing")
    ap.add_argument("--expect-class", default=None,
                    help="primitive class the code of interest draws; if it is "
                         "not on screen that is reported instead of an empty "
                         "trace")
    ap.add_argument("--disasm", type=int, default=3,
                    help="how many top writers to disassemble")
    ap.add_argument("--watch", type=float, default=0.0,
                    help="seconds to trace FREE-RUNNING instead of stepping "
                         "frames. Use this for data written at scene load "
                         "rather than per frame -- stepping two frames during "
                         "an effect cannot see a table that was filled before "
                         "it started")
    ap.add_argument("--wait-secs", type=float, default=120.0,
                    help="wait this long for --expect-class to "
                         "appear; 0 disables the wait")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    lo = int(args.lo, 16) & 0x1FFFFFFF
    hi = int(args.hi, 16) & 0x1FFFFFFF
    if hi <= lo:
        print("error: --hi must be above --lo", file=sys.stderr)
        return 2
    doc = {"kind": KIND, "version": 1,
           "lo": f"0x{lo:08X}", "hi": f"0x{hi:08X}", "frames": args.frames}

    conn = DebugConn(args.host, args.port, args.timeout)
    if args.expect_class:
        # class_on_screen walks the display list, which means talking to the
        # emulator. An unreachable one has to read as a message, not as a
        # traceback from somewhere three calls down.
        try:
            # Wait rather than sample once: the effect is transient, and an
            # empty trace looks like a statement about the code.
            if args.wait_secs > 0:
                on, drawing = wait_for_class(conn, args.expect_class,
                                             args.wait_secs, out=sys.stderr)
            else:
                on, drawing = class_on_screen(conn, args.expect_class)
        except DebugError as e:
            print(f"error: {e}", file=sys.stderr)
            doc["error"] = str(e)
            return _finish(doc, args, 2)
        doc["expect_class"] = args.expect_class
        doc["on_screen"] = on
        if not on:
            top = ", ".join(f"{k} x{v}" for k, v in
                            sorted(drawing.items(), key=lambda kv: -kv[1])[:5])
            msg = (f"{args.expect_class} is not being drawn, so the code that "
                   f"writes this range is not running. Currently drawing: "
                   f"{top or 'nothing'}.")
            print(f"error: {msg}", file=sys.stderr)
            doc["error"] = msg
            return _finish(doc, args, 1)

    listings = {}
    try:
        conn.cmd("pause")
        f0 = conn.frame()
        conn.cmd("wtrace_reset")
        conn.cmd("wtrace_add", lo=f"0x{lo:08X}", hi=f"0x{hi:08X}")
        if args.watch > 0:
            # Free-running trace. A table that is STATIC while an effect plays
            # is not written during it -- stepping a couple of frames there
            # proves only that, and reads as "nothing writes this". The fill
            # happens at scene load, so run and let the user walk into the
            # scene with the trace already armed.
            print(f"tracing free-running for {args.watch:.0f}s — enter the "
                  f"scene NOW so the load is inside the window",
                  file=sys.stderr, flush=True)
            conn.cmd("continue")
            time.sleep(args.watch)
            conn.cmd("pause")
            for _ in range(400):
                if conn.raw("pause_state").get("paused"):
                    break
                time.sleep(0.02)
        else:
            conn.cmd("step", n=max(1, args.frames))
            for _ in range(400):
                st = conn.raw("pause_state")
                if st.get("paused") and conn.frame() > f0:
                    break
                time.sleep(0.02)
        advanced = conn.frame() - f0
        doc["frames_advanced"] = advanced
        if advanced <= 0:
            msg = "the emulator did not advance a frame, so nothing was traced"
            print(f"error: {msg}", file=sys.stderr)
            doc["error"] = msg
            return _finish(doc, args, 1)
        rep = conn.cmd("wtrace_dump", addr_lo=f"0x{lo:08X}",
                       addr_hi=f"0x{hi:08X}", count=16000)
        rows = rep.get("entries", [])
        if rows and args.disasm:
            counts = Counter(e["pc"] for e in rows)
            for pc, _ in counts.most_common(args.disasm):
                listings[pc] = disasm_around(conn, int(pc, 16))
    except DebugError as e:
        print(f"error: {e}", file=sys.stderr)
        doc["error"] = str(e)
        return _finish(doc, args, 2)
    finally:
        try:
            conn.cmd("continue")
        except DebugError:
            pass

    doc["writes"] = len(rows)
    if not rows:
        if args.watch > 0:
            msg = (f"nothing wrote to this range in {args.watch:.0f}s of "
                   f"free running ({advanced} frame(s)). If the scene was "
                   f"loaded inside that window, the data did not arrive by a "
                   f"CPU store at all -- a DMA fill that writes RAM directly "
                   f"is not visible to this trace.")
        else:
            msg = (f"nothing wrote to this range across {advanced} frame(s). "
                   f"That means it is STATIC over those frames -- useful in "
                   f"itself -- not that nothing ever fills it. Data written "
                   f"once at scene load needs --watch N with the trace armed "
                   f"BEFORE entering the scene.")
        print(f"error: {msg}", file=sys.stderr)
        doc["error"] = msg
        return _finish(doc, args, 1)

    ranked = summarise(rows, lo, hi)
    doc["writers"] = [{"pc": pc, "writes": d["n"],
                       "lo": f"0x{d['lo']:08X}", "hi": f"0x{d['hi']:08X}",
                       "common": [{"value": v, "count": n}
                                  for v, n in d["vals"].most_common(4)]}
                      for pc, d in ranked]
    doc["listings"] = listings
    for pc in list(listings)[:2]:
        print(f"\n  --- {pc} ---")
        for r in listings[pc]:
            print(f"  {'>>' if r['is_target'] else '  '} {r['pc']}: "
                  f"{r['word']}  {r['text']}")
    return _finish(doc, args, 0)


def _finish(doc, args, rc):
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
        print(f"\nwrote {args.json}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
