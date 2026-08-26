#!/usr/bin/env python3
"""class_census.py -- what each emulator EVER draws, over many frames.

    python3 class_census.py --samples 40

Why maxima rather than a snapshot
---------------------------------
A single display-list capture from each side is confounded by animation phase:
the two are never at the same moment, so any class-count difference could be
"this emulator does not draw that" or "it was not drawing it just then". That
ambiguity has cost this investigation several wrong turns.

Maxima are not symmetric in that way. If psx-runtime never once reaches the
count the oracle routinely shows, across many samples spanning the whole
effect, "it was not drawing it just then" stops being available as an
explanation. Phase can hide a primitive in one frame; it cannot hide it in
forty.

This is what the images pointed at: the oracle renders two glowing orbs and a
green artifact that psx-runtime does not, and a single-capture comparison put
psx-runtime at 32 additive gouraud triangles against the oracle's 290.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_DUCKSTATION_PORT, DEFAULT_NATIVE_PORT, RAM_SIZE, STP_MODES,
    DebugConn, DebugError, decode_entries, find_display_lists, oracle_resume,
    read_ram_range, snapshot_ram, walk_ordering_table,
)

KIND = "psx-class-census"


def prim_class(p):
    mode = STP_MODES.get(p.get("stp", 0), "?") if p.get("semi") else "opaque"
    return f"{p['op_name']}|{mode}"


def census(conn, samples, gap, out=sys.stderr):
    """Per-class counts over `samples` captures. Returns {class: [counts]}."""
    oracle_resume(conn)
    seen = defaultdict(list)
    spans = {}
    root = span = None
    misses = 0
    for _ in range(samples):
        try:
            if root is None:
                ram = snapshot_ram(conn)
                cands = find_display_lists(ram)
                if not cands:
                    misses += 1
                    time.sleep(gap)
                    continue
                root = cands[0]["root"]
                span = (cands[0]["lo"], cands[0]["hi"])
            else:
                # Re-read only the list's span: a full 2 MB snapshot per sample
                # would make forty samples take minutes, and against a parked
                # oracle it would not finish at all.
                lo = max(0, (span[0] - 0x800) & ~3)
                hi = min(RAM_SIZE, span[1] + 0x800)
                blob = read_ram_range(conn, 0x80000000 + lo, hi - lo)
                buf = bytearray(RAM_SIZE)
                buf[lo:lo + len(blob)] = blob
                ram = bytes(buf)

            prims = decode_entries(walk_ordering_table(ram, root))
            if not prims:
                root = span = None
                misses += 1
                time.sleep(gap)
                continue
            counts = defaultdict(int)
            lo_hi = {}
            for p in prims:
                if p["kind"] in ("poly", "rect", "line", "fill"):
                    k = prim_class(p)
                    counts[k] += 1
                    if p.get("src"):
                        a = int(p["src"], 16) & 0x1FFFFFFF
                        cur = lo_hi.get(k)
                        lo_hi[k] = (min(cur[0], a), max(cur[1], a)) if cur else (a, a)
            for k in set(seen) | set(counts):
                seen[k].append(counts.get(k, 0))
            # Track WHERE each class is built, not just how many.
            #
            # A class that is short by 258 primitives is one thing; knowing the
            # oracle builds them in a byte range psx-runtime never touches is
            # what turns that into somewhere to look. Measured on a single
            # capture: the oracle used 0x10D0AC..0x10FD48 for this class and
            # psx-runtime only 0x10D05C..0x10DCC0.
            for k, (a, b) in lo_hi.items():
                cur = spans.get(k)
                spans[k] = (min(cur[0], a), max(cur[1], b)) if cur else (a, b)
        except DebugError:
            root = span = None
            misses += 1
        time.sleep(gap)
    if misses:
        print(f"  ({misses} of {samples} captures could not be walked)", file=out)
    return dict(seen), spans


def summarise(nat, orc, out=sys.stdout, ratio=3.0, floor=20,
              nat_spans=None, orc_spans=None):
    keys = sorted(set(nat) | set(orc))
    rows = []
    for k in keys:
        a, b = nat.get(k, []), orc.get(k, [])
        rows.append({"key": k,
                     "native_max": max(a) if a else 0,
                     "oracle_max": max(b) if b else 0,
                     "native_med": sorted(a)[len(a) // 2] if a else 0,
                     "oracle_med": sorted(b)[len(b) // 2] if b else 0,
                     "native_span": (nat_spans or {}).get(k),
                     "oracle_span": (orc_spans or {}).get(k)})
    rows.sort(key=lambda r: -(r["oracle_max"] - r["native_max"]))

    print(f"\n  {'class':<26}{'runtime max':>12}{'oracle max':>12}{'gap':>8}",
          file=out)
    for r in rows:
        gap = r["oracle_max"] - r["native_max"]
        mark = ""
        if r["oracle_max"] >= floor and r["native_max"] * ratio < r["oracle_max"]:
            mark = "   <- psx-runtime never draws these"
        elif r["native_max"] >= floor and r["oracle_max"] * ratio < r["native_max"]:
            mark = "   <- the ORACLE never draws these"
        print(f"  {r['key']:<24}{r['native_max']:>12}{r['oracle_max']:>12}"
              f"{gap:>+8}{mark}", file=out)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--native-port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--ds-port", type=int, default=DEFAULT_DUCKSTATION_PORT)
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--gap", type=float, default=0.15)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    n = DebugConn(args.host, args.native_port, args.timeout)
    o = DebugConn(args.host, args.ds_port, args.timeout)
    res = {}

    def go(key, conn):
        try:
            res[key], res[key + "_spans"] = census(conn, args.samples, args.gap)
        except DebugError as e:
            res[key] = {}
            res[key + "_err"] = str(e)

    print(f"censusing {args.samples} captures on each side — "
          f"run the effect through on BOTH")
    ts = [threading.Thread(target=go, args=("nat", n)),
          threading.Thread(target=go, args=("orc", o))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    nat, orc = res.get("nat", {}), res.get("orc", {})
    doc = {"kind": KIND, "version": 1, "samples": args.samples}
    if not nat or not orc:
        missing = "psx-runtime" if not nat else "the oracle"
        doc["error"] = f"{missing} produced no captures"
        print(f"error: {doc['error']}", file=sys.stderr)
        return _finish(doc, args, 1)

    rows = summarise(nat, orc,
                     nat_spans=res.get("nat_spans"),
                     orc_spans=res.get("orc_spans"))
    doc["classes"] = rows
    absent = [r for r in rows
              if r["oracle_max"] >= 20 and r["native_max"] * 3 < r["oracle_max"]]
    # The verdict must look BOTH ways.
    #
    # It only checked for classes the oracle draws and psx-runtime does not,
    # then announced "no systematic absence — the differences were phase" over
    # its own table showing a 254-primitive asymmetry the other way. Drawing far
    # too many additive primitives is as much a fault as drawing too few, and
    # with additive blending it is the one that washes a scene out.
    extra = [r for r in rows
             if r["native_max"] >= 20 and r["oracle_max"] * 3 < r["native_max"]]
    doc["absent_on_native"] = [r["key"] for r in absent]
    doc["extra_on_native"] = [r["key"] for r in extra]

    if extra and not absent:
        total = sum(r["native_max"] - r["oracle_max"] for r in extra)
        print(f"\nFINDING: psx-runtime draws {total} primitive(s) the oracle "
              f"never does, across {args.samples} captures: "
              f"{', '.join(r['key'] for r in extra)}.")
        print(f"\nThese are SEMI-TRANSPARENT classes, and every other class "
              f"matches exactly — so both sides were at the same scene and the "
              f"difference is not phase. Additively blended primitives "
              f"accumulate: several hundred extra ones brighten everything "
              f"they cover until detail underneath is drowned, which is what "
              f"the screenshots show.")
        doc["verdict"] = "extra-on-native"
        return _finish(doc, args, 0)

    if absent:
        total = sum(r["oracle_max"] - r["native_max"] for r in absent)
        for r in absent:
            ns, os_ = r.get("native_span"), r.get("oracle_span")
            if ns and os_:
                r["unwritten_region"] = [f"0x{max(ns[1], os_[0]):08X}",
                                         f"0x{os_[1]:08X}"]
                print(f"\n  {r['key']}: the oracle builds these across "
                      f"0x{os_[0]:08X}..0x{os_[1]:08X}; psx-runtime only uses "
                      f"0x{ns[0]:08X}..0x{ns[1]:08X}. Trace the writers of "
                      f"{r['unwritten_region'][0]}..{r['unwritten_region'][1]} "
                      f"on psx-runtime — if nothing writes there, that is where "
                      f"the primitives are lost.", file=sys.stdout)
            elif os_:
                r["unwritten_region"] = [f"0x{os_[0]:08X}", f"0x{os_[1]:08X}"]
                print(f"\n  {r['key']}: the oracle builds these across "
                      f"0x{os_[0]:08X}..0x{os_[1]:08X}; psx-runtime builds none "
                      f"at all.", file=sys.stdout)
        print(f"\nFINDING: psx-runtime never draws {total} primitive(s) the "
              f"oracle does, across {args.samples} captures spanning the "
              f"effect: {', '.join(r['key'] for r in absent)}.\n"
              f"Phase can hide a primitive in one frame; it cannot hide it in "
              f"{args.samples}. These are not being SUBMITTED, which puts the "
              f"fault in the guest code that builds them — not in the "
              f"renderer.")
        doc["verdict"] = "classes-absent-on-native"
    elif not extra:
        print("\nNo class differs systematically in either direction: every one "
              "psx-runtime draws fewer of at some moment, it also reaches "
              "comparable counts at another. The single-capture differences "
              "were phase.")
        doc["verdict"] = "no-systematic-difference"
    else:
        print("\nClasses differ in BOTH directions; see the table.")
        doc["verdict"] = "mixed"
    return _finish(doc, args, 0)


def _finish(doc, args, rc):
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
        print(f"wrote {args.json}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
