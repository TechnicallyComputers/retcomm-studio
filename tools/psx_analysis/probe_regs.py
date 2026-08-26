#!/usr/bin/env python3
"""probe_regs.py -- capture psx-runtime's registers near a PC.

    python3 probe_regs.py --pc 0x8006844C --want s4,s6

Why this is not simply a breakpoint
-----------------------------------
psx-runtime has no PC breakpoint. Its probe fires at BASIC-BLOCK LEADERS, which
the recompiler emits observers for, and an arbitrary mid-block address such as
a colour store never matches one. Adding a real breakpoint would mean emitting
per-instruction hooks from the recompiler -- a much larger change.

So the block containing the address is found by arming a SPREAD of candidates
around it and seeing which ones actually fire. Whatever fires is a real block
leader; the nearest one at or below the target encloses it.

What that costs in accuracy, stated plainly
-------------------------------------------
The registers are read at block ENTRY, not at the target instruction. For
callee-saved registers ($s0-$s7) set outside the loop -- which is what $s4 and
$s6 are in the colour routine -- those are the same value. For a register the
block itself computes ($v0, $t6, $a0 there) they are NOT, and this tool will
report a value that is real but taken earlier than asked for. It flags which
class each requested register falls into rather than leaving that to be
remembered.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_NATIVE_PORT, DebugConn, DebugError, class_on_screen,
)

KIND = "psx-probe-regs"
MAX_PCS = 16          # PC_PROBE_MAX_PCS in debug_server.c
SAVED = {"s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "sp", "fp", "gp", "ra"}


def candidates(pc, back, step, limit=MAX_PCS):
    """Addresses to arm: the target, then backwards in `step` increments.

    Backwards because a block leader is at or BEFORE the instruction it
    contains. Arming forward addresses would find the next block, whose
    registers describe what happens after the one being asked about.
    """
    out = [pc]
    a = pc - step
    while len(out) < limit and a > pc - back:
        out.append(a)
        a -= step
    return out


def plausible_pointer(v):
    """Could this register value be a RAM pointer?

    Zero is the value an uncaptured register has, and it sails through every
    later step: 0 - 12 masks to 0x1FFFFFF4, which is a real-looking address
    that reads as garbage. A register that was never captured must be rejected
    here, not turned into a plausible-looking result downstream.
    """
    if v is None:
        return False
    phys = v & 0x1FFFFFFF
    return 0 < phys < 0x200000


def search_windows(pc, max_back=0x400, slots=MAX_PCS):
    """Successive candidate sets to try, nearest the target first.

    A single fixed window is a guess about where the enclosing block starts, and
    a wrong guess reports "none of these is a block leader" — true, and useless.
    Measured: step 0x10 over 0x100 found a leader for 0x8006844C and none at all
    for 0x800684C0, sixteen bytes of code apart.

    So sweep instead: 4-byte steps immediately before the target (every
    instruction is a possible leader), then further back in equal windows. Fine
    granularity first, because the enclosing block usually starts close.
    """
    span = slots * 4
    out = []
    off = 0
    while off < max_back:
        base = pc - off
        out.append([base - i * 4 for i in range(slots)])
        off += span
    return out


def probe_registers(conn, pc, want=("s4", "s6"), back=0x100, step=0x10,
                    samples=32, wait=6.0, expect_class=None, leader=None,
                    out=sys.stderr):
    """Registers at the block leader enclosing `pc`. Returns a dict.

    Shared with colour_inputs so there is ONE implementation of "find the
    enclosing block". Two copies of this would drift, and the failure would be
    silent: both would return real register values, just from different blocks.
    """
    res = {"pc": f"0x{pc:08X}"}
    slots = []
    rep = {}
    tried = 0
    # A caller sampling repeatedly already knows the leader from the first
    # call. Re-running the sweep every time is what limited a run to three
    # samples, which is far too few to compare an animating quantity.
    # A cached leader is a fast path, NOT a replacement for the search. It can
    # stop firing -- an overlay reloads at a different address, or that block
    # simply is not reached this time -- and forcing a single window then gave
    # up instead of looking, which capped a 24-sample run at one sample.
    windows = search_windows(pc, max_back=max(back, 0x400))
    if leader:
        lead = int(leader, 16) if isinstance(leader, str) else leader
        windows = [[lead]] + windows
    # Sweep backwards until something fires. One window is a guess about where
    # the block starts; a wrong guess reports "not a block leader", which is
    # true and tells the operator nothing they can act on.
    for cands in windows:
        tried += 1
        res["candidates"] = [f"0x{c:08X}" for c in cands]
        try:
            conn.cmd("pc_probe_clear")
            conn.cmd("pc_probe_arm", n=samples,
                     pcs=",".join(f"0x{c:08X}" for c in cands))
            time.sleep(wait)
            rep = conn.cmd("pc_probe_dump")
        except DebugError as e:
            res["error"] = str(e)
            return res
        finally:
            try:
                conn.cmd("pc_probe_clear")
            except DebugError:
                pass
        slots = [x for x in rep.get("slots", []) if int(x.get("count", 0)) > 0]
        if slots:
            break
    res["windows_tried"] = tried
    res["fired"] = [{"pc": x["pc"], "count": int(x["count"])} for x in slots]
    if not slots:
        # Two very different causes, and the message used to offer both. Ask.
        if expect_class:
            on, drawing = class_on_screen(conn, expect_class)
            res["expect_class"] = expect_class
            res["on_screen"] = on
            if not on:
                top = ", ".join(f"{k} x{v}" for k, v in
                                sorted(drawing.items(), key=lambda kv: -kv[1])[:5])
                res["error"] = (f"{expect_class} is not being drawn, so this "
                                f"code never ran and no candidate could fire. "
                                f"Currently drawing: {top or 'nothing'}.")
                return res
        res["error"] = (f"no basic-block leader found within "
                        f"0x{tried * MAX_PCS * 4:X} bytes before 0x{pc:08X}, "
                        f"across {tried} window(s), and the effect IS on "
                        f"screen. Raise --back.")
        return res

    below = [x for x in slots
             if (int(x["pc"], 16) & 0x1FFFFFFF) <= (pc & 0x1FFFFFFF)]
    chosen = max(below, key=lambda x: int(x["pc"], 16)) if below else slots[0]
    res["block_leader"] = chosen["pc"]
    res["leader_after_target"] = not below

    samples_here = [x for x in rep.get("samples", [])
                    if x.get("pc") == chosen["pc"] and x.get("regs")]
    res["samples_seen"] = len(samples_here)
    if not samples_here:
        res["error"] = f"{chosen['pc']} fired but captured no registers"
        return res
    last = samples_here[-1]["regs"]
    res["frame"] = samples_here[-1].get("frame")
    res["regs"] = {w: last.get(w) for w in want}
    res["all_regs"] = last

    # $sp is never zero in running code. All-zero registers mean the capture did
    # not happen, not that the guest held zeros, and saying nothing here lets a
    # zero pointer become an address several steps later.
    try:
        sp = int(last.get("sp", "0x0"), 16)
    except (TypeError, ValueError):
        sp = 0
    if sp == 0 and all(int(v, 16) == 0 for v in last.values() if v):
        res["error"] = ("every register read back as zero, including $sp, which "
                        "cannot happen in running code — the capture did not "
                        "take. Is this runtime built with the GPR probe?")
        res["capture_empty"] = True
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--pc", required=True, help="the address of interest")
    ap.add_argument("--want", default="s4,s6",
                    help="comma-separated register names to report")
    ap.add_argument("--back", type=lambda v: int(v, 0), default=0x100,
                    help="how far back to look for the block leader")
    ap.add_argument("--step", type=lambda v: int(v, 0), default=0x10)
    ap.add_argument("--samples", type=int, default=32)
    ap.add_argument("--wait", type=float, default=6.0)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--expect-class", default=None,
                    help="primitive class this code draws (e.g. PolyG4+semi). If it is not on screen the code cannot have run, and that is reported instead of blaming block leaders.")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    pc = int(args.pc, 16)
    want = [w.strip() for w in args.want.split(",") if w.strip()]
    cands = candidates(pc, args.back, args.step)
    doc = {"kind": KIND, "version": 1, "pc": f"0x{pc:08X}", "want": want,
           "candidates": [f"0x{c:08X}" for c in cands]}

    conn = DebugConn(args.host, args.port, args.timeout)
    res = probe_registers(conn, pc, want=want, back=args.back, step=args.step,
                          samples=args.samples, wait=args.wait,
                          expect_class=args.expect_class)
    doc.update(res)
    if res.get("error"):
        print(f"error: {res['error']}", file=sys.stderr)
        return _finish(doc, args, 1)

    print(f"\n{len(res.get('fired', []))} candidate(s) are real block leaders:")
    for f in res.get("fired", []):
        print(f"  {f['pc']}  hit {f['count']} time(s)")
    if res.get("leader_after_target"):
        print("\nwarning: every firing leader is AFTER the target, so none "
              "encloses it. The values below are from the wrong side of the "
              "instruction.", file=sys.stderr)
    print(f"\nblock leader {res.get('block_leader')}, "
          f"{res.get('samples_seen')} sample(s), frame {res.get('frame')}")
    for w in want:
        note = ("" if w in SAVED else
                "   <- computed inside the block; this is its value at ENTRY, "
                "not at the target instruction")
        print(f"  ${w:<4} = {res.get('regs', {}).get(w, '?')}{note}")
    return _finish(doc, args, 0)


def _finish(doc, args, rc):
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
        print(f"wrote {args.json}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
