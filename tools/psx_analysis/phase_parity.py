#!/usr/bin/env python3
"""phase_parity.py -- compare the two IMAGES at the same animation phase.

    python3 phase_parity.py --pc 0x8006844C --out analysis/frames/phase

The measurement everything else has been clearing the way for
--------------------------------------------------------------
Every guest-side explanation is exhausted. Geometry matches primitive for
primitive, the GTE is unused, the translation is clean over millions of blocks,
the colour table is byte-identical at rest, the fade sweeps smoothly by 2 per
frame on both sides, and a class census taken while both sat at the same moment
found ten of eleven classes matching EXACTLY.

So the display lists are equivalent, and the only difference anyone has ever
seen is in the pictures. But those were captured 310 frames apart -- five
seconds of an animating effect -- and a pixel diff between two different
moments means nothing. Frame numbers cannot fix it either: they count from each
emulator's own boot.

The animation itself provides the clock. $s6 IS the fade, it sweeps by 2 per
frame, and both sides can be read. So: catch the oracle at a breakpoint, take
whatever phase it stopped at as the target, then step psx-runtime frame by
frame until its own $s6 matches. Both are then parked at the SAME point in the
effect, and the images become comparable for the first time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gpu_parity import compare_images, display_origin  # noqa: E402
from probe_regs import search_windows  # noqa: E402
from psx_gpu_frame import (  # noqa: E402
    DEFAULT_DUCKSTATION_PORT, DEFAULT_NATIVE_PORT, ORACLE_PAUSED_POLL_S,
    DebugConn, DebugError, OracleBreak, oracle_resume, wait_for_class,
)

KIND = "psx-phase-parity"


def oracle_phase(conn, pc, reg, tries=25, out=sys.stderr, reject=()):
    """Break on `pc` and return the phase the oracle stopped at, still parked.

    `reject` lists phase values that are too common to be a useful lock. The
    fade rests at its neutral value (128) for most of the scene, so matching
    on it says almost nothing about the two emulators being at the same
    moment; a mid-ramp value occurs on only a frame or two. Keep resuming
    and re-breaking until the oracle stops somewhere distinctive.
    """
    reject = set(reject)
    seen = []
    # NOTE: the breakpoint is armed ONCE, outside the loop, and removed by the
    # guard on every exit path. Re-arming per iteration used to leave a live
    # breakpoint behind whenever this returned early, and DuckStation refuses
    # a duplicate address -- so the next tool's pc_break failed while the
    # leaked one kept pausing the emulator.
    brk = OracleBreak(conn, pc)
    brk.__enter__()
    keep_parked = False
    try:
        for _ in range(tries):
            oracle_resume(conn)
            try:
                conn.cmd("pc_hit_clear")
            except DebugError:
                pass
            time.sleep(ORACLE_PAUSED_POLL_S)
            try:
                rep = conn.cmd("pc_hit_last")
            except DebugError:
                continue
            if not rep.get("valid"):
                continue
            v = rep.get("regs", {}).get(reg)
            if not v:
                continue
            val = int(v, 16)
            seen.append(val)
            if val in reject:
                print(f"  oracle at ${reg} = {val} (too common to lock on); "
                      f"resuming for a mid-ramp value", file=out)
                continue
            # Leave it PARKED here: this is the phase to match, and resuming
            # would lose it before psx-runtime catches up. The breakpoint is
            # still removed -- parked is deliberate, armed is a leak.
            keep_parked = True
            conn.raw("pc_unbreak", addr=brk.addr)
            return val
    finally:
        if not keep_parked:
            brk.__exit__(None, None, None)
    if seen:
        print(f"  oracle only ever stopped at {sorted(set(seen))}, all of "
              f"which are too common to lock on", file=out)
    return None


def step_native_to_phase(conn, pc, reg, target, max_frames=200, tol=0,
                         out=sys.stderr):
    """Step psx-runtime until its phase matches, then leave it parked."""
    conn.cmd("pause")
    windows = search_windows(pc, 0x400)
    leader = None
    seen = []
    for i in range(max_frames):
        cands = [leader] if leader else windows[i % len(windows)]
        try:
            conn.cmd("pc_probe_clear")
            conn.cmd("pc_probe_arm", n=8,
                     pcs=",".join(f"0x{c:08X}" for c in cands))
            f0 = conn.frame()
            conn.cmd("step", n=1)
            for _ in range(150):
                st = conn.raw("pause_state")
                if st.get("paused") and conn.frame() > f0:
                    break
                time.sleep(0.02)
            rep = conn.cmd("pc_probe_dump")
        except DebugError as e:
            return None, f"stepping failed: {e}", seen

        hit = [x for x in rep.get("slots", []) if int(x.get("count", 0)) > 0]
        got = None
        if hit:
            below = [x for x in hit
                     if (int(x["pc"], 16) & 0x1FFFFFFF) <= (pc & 0x1FFFFFFF)]
            by_pc = {s["pc"]: s["regs"] for s in rep.get("samples", [])
                     if s.get("regs")}
            for cand in sorted(below or hit, key=lambda x: -int(x["pc"], 16)):
                regs = by_pc.get(cand["pc"])
                if regs and regs.get(reg):
                    leader = int(cand["pc"], 16)
                    got = int(regs[reg], 16)
                    break
        if got is None:
            leader = None
            continue
        seen.append(got)
        if abs(got - target) <= tol:
            return got, None, seen
    return None, (f"psx-runtime did not reach phase {target} within "
                  f"{max_frames} frames; it passed through {sorted(set(seen))}"), seen


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--native-port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--ds-port", type=int, default=DEFAULT_DUCKSTATION_PORT)
    ap.add_argument("--pc", default="0x8006844C")
    ap.add_argument("--reg", default="s6")
    ap.add_argument("--exclude-phase", default="128",
                    help="comma-separated phase values too common to lock "
                         "on (default: 128, the neutral top-of-fade)")
    ap.add_argument("--tol", type=int, default=0,
                    help="phase tolerance; 0 means exactly the same value")
    ap.add_argument("--wait-for", default="PolyG4+semi")
    ap.add_argument("--wait-secs", type=float, default=120.0)
    ap.add_argument("--max-frames", type=int, default=200)
    ap.add_argument("--out", default="analysis/frames/phase")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    pc = int(args.pc, 16)
    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    doc = {"kind": KIND, "version": 1, "pc": f"0x{pc:08X}", "reg": args.reg}

    n = DebugConn(args.host, args.native_port, args.timeout)
    o = DebugConn(args.host, args.ds_port, args.timeout)

    try:
        for label, conn in (("psx-runtime", n), ("the oracle", o)):
            on, drawing = wait_for_class(conn, args.wait_for, args.wait_secs,
                                         out=sys.stderr)
            if not on:
                top = ", ".join(f"{k} x{v}" for k, v in
                                sorted(drawing.items(), key=lambda kv: -kv[1])[:4])
                doc["error"] = (f"{args.wait_for} never appeared on {label}. "
                                f"Drawing instead: {top or 'nothing'}.")
                print(f"error: {doc['error']}", file=sys.stderr)
                return _finish(doc, outdir, 1)

        print("catching the oracle at a breakpoint to fix the phase …")
        reject = {int(x, 0) for x in args.exclude_phase.split(",") if x.strip()}
        target = oracle_phase(o, pc, args.reg, reject=reject)
        if target is None:
            doc["error"] = "the oracle never hit the breakpoint"
            print(f"error: {doc['error']}", file=sys.stderr)
            return _finish(doc, outdir, 1)
        doc["target_phase"] = target
        print(f"  oracle parked at ${args.reg} = {target}")

        print(f"stepping psx-runtime until it reaches {target} …")
        got, why, seen = step_native_to_phase(n, pc, args.reg, target,
                                              args.max_frames, args.tol)
        doc["native_phase"] = got
        doc["native_phases_seen"] = sorted(set(seen))
        if got is None:
            doc["error"] = why
            print(f"error: {why}", file=sys.stderr)
            return _finish(doc, outdir, 1)
        print(f"  psx-runtime parked at ${args.reg} = {got}")

        pa = os.path.join(outdir, "native.png")
        pb = os.path.join(outdir, "duckstation.png")
        n.screenshot(pa)
        o.screenshot(pb)
        st_a, st_b = n.cmd("gpu_state"), o.cmd("gpu_state")
        doc["origin_native"] = list(display_origin(st_a))
        doc["origin_oracle"] = list(display_origin(st_b))
    except DebugError as e:
        doc["error"] = str(e)
        print(f"error: {e}", file=sys.stderr)
        return _finish(doc, outdir, 2)
    finally:
        for conn in (n, o):
            try:
                conn.cmd("continue")
            except DebugError:
                pass

    doc["image"] = compare_images(pa, pb, os.path.join(outdir, "diff.png"),
                                  origin_a=tuple(doc["origin_native"]),
                                  origin_b=tuple(doc["origin_oracle"]))
    img = doc["image"]
    print(f"\n  {img['differing_pixels']}/{img['total_pixels']} pixels differ "
          f"({img['differing_pct']}%), max channel delta "
          f"{img['max_channel_delta']}")
    if img.get("note"):
        print(f"  {img['note']}")
    if img["differing_pct"] < 2.0:
        doc["verdict"] = "images-match"
        print("\nVERDICT: at the same animation phase the two images agree. The "
              "renderer is not the fault either — which would mean the visible "
              "difference is a phase or timing effect rather than a rendering "
              "one.")
    else:
        doc["verdict"] = "images-differ"
        print(f"\nVERDICT: the two images differ at the SAME phase "
              f"({img['differing_pct']}% of pixels, bbox {img['diff_bbox']}). "
              f"The display lists agree and the phase is matched, so this is "
              f"the renderer.")
    return _finish(doc, outdir, 0)


def _finish(doc, outdir, rc):
    with open(os.path.join(outdir, "phase_parity.json"), "w",
              encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print(f"wrote {os.path.join(outdir, 'phase_parity.json')}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
