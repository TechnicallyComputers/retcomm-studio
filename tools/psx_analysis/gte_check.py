#!/usr/bin/env python3
"""gte_check.py -- validate psx-runtime's GTE colour maths against the spec.

    python3 gte_check.py intpl          # re-derive every recorded INTPL
    python3 gte_check.py stats          # per-frame GTE counters
    python3 gte_check.py latch          # saturated projections, with inputs

Why this needs no oracle
------------------------
The display-list comparison showed the two emulators build identical geometry
but different vertex COLOURS: for the same 64 additive quads the oracle emits 3
distinct colours and psx-runtime emits 153. Flat-coloured primitives agree
exactly; shaded ones do not. That puts the fault in the guest's colour
computation, which on PSX means the GTE.

The GTE is fully specified arithmetic, and the runtime's INTPL ring records the
INPUTS and the OUTPUTS of every operation. So the check does not need a second
emulator to disagree with: re-derive the result from the inputs and see whether
the recorded output is what the hardware would have produced. A mismatch is a
bug in our GTE; a match means the inputs were already wrong when they arrived,
and the fault is upstream of the GTE.

That distinction is the whole point. "The colours are wrong" is not actionable;
"the colours are wrong and the arithmetic is right, so the inputs are wrong" is.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import DEFAULT_NATIVE_PORT, DebugConn, DebugError  # noqa: E402


def sat_ir(v, lm):
    """IR1-3 saturation: [0,0x7FFF] when lm is set, else [-0x8000,0x7FFF]."""
    lo = 0 if lm else -0x8000
    return max(lo, min(0x7FFF, v))


def intpl_reference(ir0, ir, fc, shift, lm):
    """Depth-cue interpolation, straight from the hardware description.

        base = IR << 12
        step = lim(((FC << 12) - base) >> sf)   ; +/-0x8000, lm FORCED off
        MAC  = (base + IR0 * step) >> sf
        IR   = lim(MAC, lm)

    Written from the spec rather than from runtime/src/gte.cpp on purpose --
    a reference copied from the implementation under test agrees with it by
    construction and proves nothing.
    """
    mac, out = [], []
    for i in range(3):
        base = ir[i] * 4096
        step = sat_ir(((fc[i] * 4096) - base) >> shift, False)
        m = (base + ir0 * step) >> shift
        mac.append(m)
        out.append(sat_ir(m, lm))
    return mac, out


GTE_KIND = "psx-gte-check"


def check_intpl(entries, out=sys.stdout, doc=None):
    """Re-derive each record; report which (sf, lm) explains it, if any."""
    total = len(entries)
    explained = {}
    bad = []
    for e in entries:
        ir0 = int(e["ir0"])
        ir = [int(x) for x in e["in"]]
        fc = [int(x) for x in e["fc"]]
        mac = [int(x) for x in e["mac"]]
        got = [int(x) for x in e["out"]]
        hit = None
        for shift in (12, 0):
            for lm in (True, False):
                rmac, rout = intpl_reference(ir0, ir, fc, shift, lm)
                if rmac == mac and rout == got:
                    hit = (shift, lm)
                    break
            if hit:
                break
        if hit:
            explained[hit] = explained.get(hit, 0) + 1
        else:
            bad.append(e)

    print(f"{total} INTPL record(s) checked", file=out)
    for (shift, lm), n in sorted(explained.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>6}  reproduced exactly with sf={'1' if shift else '0'} "
              f"lm={int(lm)}", file=out)
    if doc is not None:
        doc["intpl_checked"] = total
        doc["intpl_bad"] = len(bad)
        doc["intpl_modes"] = [{"sf": 1 if sh else 0, "lm": int(lm), "count": n}
                              for (sh, lm), n in explained.items()]
        doc["verdict"] = "arithmetic-ok" if not bad else "arithmetic-diverges"
        doc["bad"] = [{"seq": e["seq"], "frame": e["frame"], "ra": e["ra"],
                       "ir0": e["ir0"], "in": e["in"], "fc": e["fc"],
                       "mac": e["mac"], "out": e["out"]} for e in bad[:16]]
    if not bad:
        print("\nVERDICT: every recorded INTPL matches the hardware description.\n"
              "The GTE's interpolation arithmetic is NOT the bug. If colours are\n"
              "still wrong, the inputs (IR1-3 / FC / IR0) were already wrong when\n"
              "they reached it — look upstream, at whatever computes them.",
              file=out)
        return 0

    print(f"\n  {len(bad)} record(s) reproduced by NO (sf, lm) combination:",
          file=out)
    for e in bad[:8]:
        ir0 = int(e["ir0"])
        ir = [int(x) for x in e["in"]]
        fc = [int(x) for x in e["fc"]]
        rmac, rout = intpl_reference(ir0, ir, fc, 12, False)
        print(f"    seq {e['seq']} frame {e['frame']} ra {e['ra']}", file=out)
        print(f"      ir0={ir0} in={ir} fc={fc}", file=out)
        print(f"      recorded mac={e['mac']} out={e['out']}", file=out)
        print(f"      expected mac={rmac} out={rout}  (sf=1 lm=0)", file=out)
    print("\nVERDICT: the GTE's own arithmetic diverges from the hardware "
          "description. This is the bug.", file=out)
    return 1


def _merge_stats(conn, doc):
    """Attach recent per-frame GTE counters.

    nintpl and nsat are the two that decide where to look next: nintpl=0 means
    the depth-cue path is not merely correct but unused, and nsat=0 rules out
    saturation in projection. Both are far more useful attached to the verdict
    than sitting behind a separate command.
    """
    try:
        rep = conn.cmd("gte_frame_stats", frames=60)
    except DebugError:
        return
    fr = rep.get("frames", [])
    doc["frames"] = [{k: int(v) for k, v in f.items()} for f in fr[:30]]
    if fr:
        doc["nintpl_total"] = sum(int(f.get("nintpl", 0)) for f in fr)
        doc["nsat_total"] = sum(int(f.get("nsat", 0)) for f in fr)
        doc["nproj_last"] = int(fr[0].get("nproj", 0))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["intpl", "stats", "latch"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--count", type=int, default=512)
    ap.add_argument("--frame", type=int, default=-1)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    conn = DebugConn(args.host, args.port, args.timeout)
    try:
        if args.what == "intpl":
            rep = conn.cmd("gte_intpl_dump", count=args.count, frame=args.frame)
            es = rep.get("entries", [])
            doc = {"kind": GTE_KIND, "version": 1, "what": "intpl"}
            if not es:
                note = ("no INTPL records — this game does not use INTPL at "
                        "all. The colour path is therefore NCDS/NCCS or a plain "
                        "CPU write, not depth-cue interpolation.")
                print(note, file=sys.stderr)
                doc.update({"intpl_checked": 0, "intpl_bad": 0,
                            "verdict": "not-used", "note": note,
                            "intpl_modes": [], "bad": []})
                rc = 1
            else:
                rc = check_intpl(es, doc=doc)
            if args.json:
                _merge_stats(conn, doc)
                with open(args.json, "w", encoding="utf-8") as f:
                    json.dump(doc, f, indent=1)
            return rc
        if args.what == "stats":
            rep = conn.cmd("gte_frame_stats", frames=args.count)
            for f in rep.get("frames", [])[-20:]:
                print("  " + "  ".join(f"{k}={v}" for k, v in f.items()))
            if args.json:
                doc = {"kind": GTE_KIND, "version": 1, "what": "stats"}
                _merge_stats(conn, doc)
                with open(args.json, "w", encoding="utf-8") as f:
                    json.dump(doc, f, indent=1)
            return 0
        rep = conn.cmd("gte_latch_dump", count=min(args.count, 256))
        print(f"latched saturated projections: {rep.get('latch_total')}")
        for e in rep.get("entries", [])[:10]:
            print("  " + str(e)[:200])
        return 0
    except DebugError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
