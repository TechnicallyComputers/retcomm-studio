#!/usr/bin/env python3
"""packet_writers.py -- find the code that writes a primitive's COLOUR words.

    python3 packet_writers.py --class "PolyG4+semi|B+F"

What this is for
----------------
The display-list comparison narrowed a render bug to vertex colour: geometry is
identical between emulators, flat-coloured primitives agree, shaded ones do
not. The next question is which instruction writes those colour words, and that
cannot be answered by function attribution -- a game that builds an ordering
table and DMAs it reports the same submit PC for every packet on screen.

So it is answered by address. Walk the list, note where each primitive's
colour words actually live, watch those addresses, and see which PCs store to
them. That works even when the writer is overlay code the static analyser never
sees, which is where this game's are.

Why the addresses are derived rather than assumed
-------------------------------------------------
Packet stride is not a constant: it depends on the opcode, and a buffer holds
different opcodes. Assuming one stride and taking (addr - base) % stride
misclassifies every write as soon as the assumption slips -- it reports
confident nonsense rather than failing. Each field's address comes from the
walk itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WRITERS_KIND = "psx-packet-writers"

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_NATIVE_PORT, DebugConn, DebugError, decode_entries,
    find_display_lists, read_ram_range, snapshot_ram, wait_for_class,
    walk_ordering_table,
)


_REG = ["zr", "at", "v0", "v1", "a0", "a1", "a2", "a3",
        "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7",
        "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7",
        "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra"]
_SPECIAL = {0x00: "sll", 0x02: "srl", 0x03: "sra", 0x04: "sllv",
            0x06: "srlv", 0x07: "srav", 0x08: "jr", 0x09: "jalr",
            0x10: "mfhi", 0x12: "mflo", 0x18: "mult", 0x19: "multu",
            0x1A: "div", 0x1B: "divu", 0x20: "add", 0x21: "addu",
            0x22: "sub", 0x23: "subu", 0x24: "and", 0x25: "or",
            0x26: "xor", 0x27: "nor", 0x2A: "slt", 0x2B: "sltu"}
_IMM = {0x08: "addi", 0x09: "addiu", 0x0A: "slti", 0x0B: "sltiu",
        0x0C: "andi", 0x0D: "ori", 0x0E: "xori"}
# The unaligned forms matter here and were previously printed as "op22"/"op2a".
# The code that builds packet colours uses them, and a recompiler getting
# LWL/LWR/SWL/SWR wrong is a classic source of subtly corrupt bytes -- exactly
# the failure being investigated. An instruction printed as "op2a" is one the
# reader cannot weigh.
_MEM = {0x20: "lb", 0x21: "lh", 0x22: "lwl", 0x23: "lw", 0x24: "lbu",
        0x25: "lhu", 0x26: "lwr",
        0x28: "sb", 0x29: "sh", 0x2A: "swl", 0x2B: "sw", 0x2E: "swr",
        0x32: "lwc2", 0x3A: "swc2"}
# GTE commands, so a cop2 op is named rather than shown as a bare word.
_GTE = {0x01: "RTPS", 0x06: "NCLIP", 0x0C: "OP", 0x10: "DPCS", 0x11: "INTPL",
        0x12: "MVMVA", 0x13: "NCDS", 0x14: "CDP", 0x16: "NCDT", 0x1B: "NCCS",
        0x1C: "CC", 0x1E: "NCS", 0x20: "NCT", 0x28: "SQR", 0x29: "DCPL",
        0x2A: "DPCT", 0x2D: "AVSZ3", 0x2E: "AVSZ4", 0x30: "RTPT",
        0x3D: "GPF", 0x3E: "GPL", 0x3F: "NCCT"}
_COP2_MOVE = {0x00: "mfc2", 0x02: "cfc2", 0x04: "mtc2", 0x06: "ctc2"}


def disasm_one(w, pc):
    """One MIPS instruction, enough to read a store and its operands."""
    if w == 0:
        return "nop"
    op = (w >> 26) & 0x3F
    rs, rt, rd = (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31
    sh, fn = (w >> 6) & 31, w & 0x3F
    imm = w & 0xFFFF
    simm = imm - 0x10000 if imm & 0x8000 else imm
    R = lambda n: "$" + _REG[n]
    if op == 0:
        n = _SPECIAL.get(fn, f"spec{fn:02x}")
        if fn in (0x00, 0x02, 0x03):
            return f"{n} {R(rd)},{R(rt)},{sh}"
        if fn == 0x08:
            return f"jr {R(rs)}"
        if fn in (0x18, 0x19, 0x1A, 0x1B):
            return f"{n} {R(rs)},{R(rt)}"
        if fn in (0x10, 0x12):
            return f"{n} {R(rd)}"
        return f"{n} {R(rd)},{R(rs)},{R(rt)}"
    if op in _MEM:
        name = _MEM[op]
        if op in (0x32, 0x3A):        # coprocessor-2 load/store: rt is a GTE reg
            return f"{name} $c2r{rt},{simm}({R(rs)})"
        return f"{name} {R(rt)},{simm}({R(rs)})"
    if op in _IMM:
        return f"{_IMM[op]} {R(rt)},{R(rs)},{simm}"
    if op == 0x0F:
        return f"lui {R(rt)},0x{imm:04X}"
    if op in (4, 5):
        return (f"{'beq' if op == 4 else 'bne'} {R(rs)},{R(rt)},"
                f"0x{pc + 4 + (simm << 2):08X}")
    if op in (2, 3):
        return (f"{'j' if op == 2 else 'jal'} "
                f"0x{(pc & 0xF0000000) | ((w & 0x3FFFFFF) << 2):08X}")
    if op == 0x12:
        if w & (1 << 25):             # GTE command
            fn = w & 0x3F
            sf = "sf=1" if (w >> 19) & 1 else "sf=0"
            lm = "lm=1" if (w >> 10) & 1 else "lm=0"
            return f"{_GTE.get(fn, f'GTE?{fn:02X}')} ({sf} {lm})"
        mv = _COP2_MOVE.get(rs)
        if mv:
            return f"{mv} {R(rt)},$c2r{rd}"
        return f"cop2 0x{w & 0x1FFFFFF:07X}"
    return f"op{op:02x}"


def disasm_around(conn, pc, before=10, count=20):
    """Fetch and decode instructions around a PC.

    Captured DURING the trace, while the emulator is already parked and
    the right overlay is resident. These PCs live above the main EXE
    (0x8003F000), so the code at a given address depends on which overlay
    is loaded -- disassembling later, after the scene changed, would
    decode whatever replaced it and look perfectly plausible.
    """
    # Mask to physical ONCE, up front. Mixing a virtual pc into the offset
    # arithmetic and then comparing against a masked one means no line ever
    # matches, and the listing comes back with nothing marked — which reads as
    # "the instruction is not in this window" rather than as a bug.
    phys = pc & 0x1FFFFFFF
    start = max(0, phys - before * 4)
    try:
        blob = read_ram_range(conn, 0x80000000 | start, count * 4)
    except DebugError:
        return []
    out = []
    for i in range(0, len(blob) - 3, 4):
        w = int.from_bytes(blob[i:i + 4], "little")
        a = start + i
        va = 0x80000000 | a
        out.append({"pc": f"0x{va:08X}", "word": f"0x{w:08X}",
                    "text": disasm_one(w, va),
                    "is_target": a == phys})
    return out


class ParkGuard:
    """Park the emulator, and guarantee it gets handed back.

    Leaving it parked is worse than any error this tool can report: the game
    stops advancing, so the effect never comes round again, and every retry
    walks the same stale display list and fails the same way. The symptom is
    "it keeps failing to find the writer" and the cause is invisible from the
    outside.

    The park deliberately spans the walk AND the trace -- the layout has to
    still describe the buffer when the writes happen -- so a plain try/finally
    around either half is not enough. Every exit between them needs the same
    guarantee.
    """

    def __init__(self, conn):
        self.conn = conn
        self.parked = False

    def __enter__(self):
        self.conn.cmd("pause")
        self.parked = True
        return self

    def resume(self):
        if self.parked:
            self.parked = False
            try:
                self.conn.cmd("continue")
            except DebugError:
                pass

    def __exit__(self, *exc):
        self.resume()
        return False


# The game alternates between two packet buffers this far apart. Measured
# from the capture: roots 0x0010Dxxx and 0x00115xxx.
BUFFER_STRIDE = 0x8000


def fold_to_walked(addr, lo, hi):
    """Map a write address onto the buffer copy that was actually walked.

    A write to the other copy describes the same field, so folding it back is
    what lets one walk explain writes from either frame.

    The two copies are BUFFER_STRIDE apart by ADDITION, not by a toggled bit:
    0x0010D078 and 0x00115078 differ by 0x8000, but XOR-ing 0x8000 gives
    0x00105078 because the add carries out of bit 15. Using XOR here folds
    real writes onto addresses that do not exist.
    """
    if lo <= addr <= hi:
        return addr
    for alt in (addr - BUFFER_STRIDE, addr + BUFFER_STRIDE):
        if lo <= alt <= hi:
            return alt
    return None


def field_map(prims, want=None):
    """addr -> 'colour' | 'vertex', derived from each packet's real layout.

    A gouraud polygon interleaves colour and vertex words (c,v,c,v,...); a flat
    one has a single leading colour. Textured packets carry a UV word after each
    vertex. The layout is read off the opcode rather than guessed.
    """
    roles = {}
    for p in prims:
        if want and p["op_name"] != want.split("|")[0]:
            continue
        if p["kind"] != "poly" or not p.get("src"):
            continue
        base = int(p["src"], 16) & 0x1FFFFFFF
        # Read the layout off the decoded packet, not off the opcode name.
        gouraud = bool(p.get("gouraud"))
        textured = bool(p.get("textured"))
        n = len(p.get("verts") or [])
        off = 0
        for i in range(n):
            if i == 0 or gouraud:
                roles[base + off * 4] = "colour"
                off += 1
            roles[base + off * 4] = "vertex"
            off += 1
            if textured:
                roles[base + off * 4] = "uv"
                off += 1
    return roles


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--class", dest="want", default="PolyG4+semi|B+F")
    ap.add_argument("--frames", type=int, default=2,
                    help="frames to step while tracing. The packet buffer moves "
                         "between frames, so a map built from one walk goes "
                         "stale almost immediately — keep this small.")
    ap.add_argument("--watch", type=float, default=0.0,
                    help="legacy free-running trace, in seconds. Produces "
                         "aliased attribution once the buffer moves; --frames "
                         "is correct.")
    ap.add_argument("--wait-secs", type=float, default=120.0,
                    help="wait this long for the class to appear "
                         "before tracing; 0 disables the wait")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--json", default=None)
    ap.add_argument("--no-disasm", dest="disasm", action="store_false",
                    help="skip capturing code for colour-only writers")
    ap.add_argument("--disasm-before", type=int, default=12)
    ap.add_argument("--disasm-count", type=int, default=28)
    args = ap.parse_args(argv)

    conn = DebugConn(args.host, args.port, args.timeout)

    # Wait for the effect rather than requiring it to be on screen at the
    # instant this was launched. Losing that race produces "no writes recorded",
    # which reads as a statement about the code rather than about the timing.
    if args.wait_secs > 0:
        want_op = args.want.split("|")[0]
        on, drawing = wait_for_class(conn, want_op, args.wait_secs, out=sys.stderr)
        if not on:
            top = ", ".join(f"{k} x{v}" for k, v in
                            sorted(drawing.items(), key=lambda kv: -kv[1])[:5])
            msg = (f"{want_op} never appeared within {args.wait_secs:.0f}s. "
                   f"Drawing instead: {top or 'nothing'}.")
            print(f"error: {msg}", file=sys.stderr)
            return 1

    # Park before walking: everything below depends on the layout in `ram`
    # still describing the buffer when the writes happen, and a walk of a
    # running emulator is stale before it finishes. The guard makes sure the
    # game is handed back on EVERY path out of here.
    with ParkGuard(conn) as park:
        return _parked_main(conn, args, park)


def _parked_main(conn, args, park):
    """Everything that needs the emulator parked.

    Split out so the guard wraps EVERY exit. The class-absent path used to
    return from the middle of this without resuming, which stops the game
    advancing — so the effect never comes round again, and every retry walks
    the same stale list and fails identically. The symptom is "it keeps failing
    to find the writer" and the cause is invisible from outside.
    """
    try:
        ram = snapshot_ram(conn)
        cands = find_display_lists(ram)
        if not cands:
            print("no display list found", file=sys.stderr)
            return 1
        root = cands[0]["root"]
        prims = decode_entries(walk_ordering_table(ram, root))
    except DebugError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    op = args.want.split("|")[0]
    sel = [p for p in prims if p["kind"] == "poly" and p["op_name"] == op]
    if not sel:
        have = Counter(p["op_name"] for p in prims if p["kind"] == "poly")
        msg = (f"no {op} in the current list — the effect has to be ON SCREEN "
               f"for this to find its writers. Present: "
               f"{', '.join(f'{k} x{v}' for k, v in have.most_common(6))}")
        print(msg, file=sys.stderr)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump({"kind": WRITERS_KIND, "version": 1,
                           "class": args.want, "absent": True, "note": msg,
                           "present": [{"key": k, "count": v}
                                       for k, v in have.most_common(12)],
                           "writers": []}, f, indent=1)
        return 1

    roles = field_map(sel, args.want)
    addrs = sorted(roles)
    lo, hi = addrs[0], addrs[-1] + 4
    print(f"{len(sel)} {op} packet(s), fields 0x{lo:08X}..0x{hi:08X}")
    print(f"  {sum(1 for v in roles.values() if v == 'colour')} colour words, "
          f"{sum(1 for v in roles.values() if v == 'vertex')} vertex words")

    # Arm the trace while still parked, advance a bounded number of frames, then
    # re-walk and confirm the layout did not move underneath us.
    #
    # A free-running trace cannot work here. The buffer is rebuilt at a
    # different address most frames, so absolute addresses mapped from one walk
    # describe a different field a frame later. That does not fail loudly -- it
    # smears every instruction to roughly 50% colour and 50% vertex, which reads
    # as "this code writes both" and is entirely an artefact.
    # The packet buffer is DOUBLE BUFFERED: the game alternates between two
    # copies exactly BUFFER_STRIDE apart, so the fields walked this frame are
    # written to the other copy next frame. Tracing only the walked range sees
    # nothing at all on alternate frames -- which is the "no writes were
    # recorded" dead end -- and stepping one frame hits the wrong copy about
    # half the time. Cover both, and step an even number of frames so the
    # walked copy is definitely rebuilt at least once.
    alt_lo, alt_hi = lo + BUFFER_STRIDE, hi + BUFFER_STRIDE
    if lo >= BUFFER_STRIDE:
        # Cover the copy on EITHER side; which one the walk landed on is not
        # known here, and arming only the higher one misses half the frames.
        alt_lo = lo - BUFFER_STRIDE
    stale = False
    try:
        conn.cmd("wtrace_reset")
        conn.cmd("wtrace_add", lo=f"0x{lo:08X}", hi=f"0x{hi:08X}")
        conn.cmd("wtrace_add", lo=f"0x{lo - BUFFER_STRIDE:08X}",
                 hi=f"0x{hi + BUFFER_STRIDE:08X}")
        if args.watch > 0:
            conn.cmd("continue")
            time.sleep(args.watch)
            conn.cmd("pause")
            stale = True
        else:
            # Verify by FRAME NUMBER, not by the paused flag. `step` re-parks
            # itself, and a poll can catch that flag set before any frame has
            # actually run -- which yields a trace of nothing and a table that
            # looks merely empty rather than wrong.
            f_before = conn.frame()
            # An even count guarantees the walked copy is rebuilt, whichever
            # half of the double buffer the walk happened to land on.
            want = max(2, args.frames + (args.frames & 1))
            conn.cmd("step", n=want)
            for _ in range(400):
                st = conn.raw("pause_state")
                if st.get("ok") and st.get("paused") and conn.frame() > f_before:
                    break
                time.sleep(0.05)
            advanced = conn.frame() - f_before
            if advanced <= 0:
                print("error: the emulator did not advance a frame while "
                      "stepping, so nothing was traced. Is the game actually "
                      "running (not on a stalled screen)?", file=sys.stderr)
                return 2
            if advanced != want:
                print(f"  note: asked for {want} frame(s), advanced {advanced}",
                      file=sys.stderr)
            after = snapshot_ram(conn)
            again = field_map(
                [p for p in decode_entries(walk_ordering_table(after, root))
                 if p["kind"] == "poly" and p["op_name"] == op], args.want)
            # A walk landing on the OTHER copy of the double buffer is
            # expected, not corruption: the same fields simply sit
            # BUFFER_STRIDE away. Compare against the folded layout so a
            # normal buffer swap is not reported as the map going stale.
            folded = {}
            for a, r in again.items():
                w = fold_to_walked(a, lo, hi)
                if w is not None:
                    folded[w] = r
            moved = sum(1 for a, r in roles.items() if folded.get(a) != r)
            if moved:
                stale = True
                print(f"  warning: {moved} of {len(roles)} field addresses "
                      f"changed role between the two walks, even after "
                      f"folding the double buffer — the layout really did "
                      f"move.", file=sys.stderr)
        rep = conn.cmd("wtrace_dump", addr_lo=f"0x{lo - BUFFER_STRIDE:08X}",
                       addr_hi=f"0x{hi + BUFFER_STRIDE:08X}", count=16000)
        listings = {}
        if args.disasm:
            # Still parked here, which is the only safe moment: see
            # disasm_around(). Which PCs matter is not known until the writes
            # are tallied, so tally them now rather than after resuming.
            tally = defaultdict(Counter)
            for e in rep.get("entries", []):
                a = fold_to_walked(int(e["addr"], 16) & 0x1FFFFFFC, lo, hi)
                r = roles.get(a) if a is not None else None
                if r:
                    tally[e["pc"]][r] += 1
            for pc, k in tally.items():
                if k["colour"] and not k["vertex"]:
                    listings[pc] = disasm_around(conn, int(pc, 16),
                                                 before=args.disasm_before,
                                                 count=args.disasm_count)
    except DebugError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        park.resume()

    entries = rep.get("entries", [])
    if not entries:
        msg = ("no writes were recorded in the traced window. The packets exist "
               "(they were just walked), so either the buffer is rebuilt less "
               "often than every frame — step more frames — or nothing wrote to "
               "it during the step.")
        print(f"error: {msg}", file=sys.stderr)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump({"kind": WRITERS_KIND, "version": 1,
                           "class": args.want, "absent": False,
                           "packets": len(sel), "writes": 0, "unmapped": 0,
                           "no_writes": True, "note": msg,
                           "lo": f"0x{lo:08X}", "hi": f"0x{hi:08X}",
                           "colour_words": sum(1 for v in roles.values()
                                               if v == "colour"),
                           "vertex_words": sum(1 for v in roles.values()
                                               if v == "vertex"),
                           "writers": []}, f, indent=1)
        return 1

    by_pc = defaultdict(Counter)
    unknown = 0
    for e in entries:
        a = int(e["addr"], 16) & 0x1FFFFFFF
        role = roles.get(a & ~3)
        if role is None:
            unknown += 1
            role = "unmapped"
        by_pc[e["pc"]][role] += 1

    print(f"\n{len(entries)} write(s) recorded"
          + (f", {unknown} to addresses not in the walked layout (the list was "
             f"rebuilt while tracing)" if unknown else ""))
    print(f"\n  {'pc':<12}{'func':<12}{'COLOUR':>8}{'vertex':>8}{'uv':>5}")
    ranked = sorted(by_pc.items(), key=lambda kv: -kv[1]["colour"])
    pcfunc = {e["pc"]: e.get("func", "") for e in entries}
    for pc, k in ranked[:14]:
        print(f"  {pc:<12}{pcfunc.get(pc,''):<12}{k['colour']:>8}"
              f"{k['vertex']:>8}{k['uv']:>5}")
    # A store instruction writes ONE field. If most instructions come back near
    # 50/50 the map went stale, and every row is an artefact -- say so instead
    # of letting it read as "this code writes both".
    mixed = [pc for pc, k in by_pc.items()
             if k["colour"] >= 8 and k["vertex"] >= 8
             and 0.6 <= k["colour"] / max(1, k["vertex"]) <= 1.67]
    aliased = len(mixed) >= max(2, len(by_pc) // 2)
    if aliased or stale:
        print(f"\n  UNRELIABLE: {len(mixed)} of {len(by_pc)} instruction(s) "
              f"split about evenly between colour and vertex words. A single "
              f"store writes one field, so this is the packet buffer having "
              f"moved during the trace, not code that writes both. Re-run with "
              f"--frames 1 (the default) and no --watch.", file=sys.stderr)

    top = [pc for pc, k in ranked if k["colour"] and not k["vertex"]]
    if top:
        print(f"\nWrites ONLY colour words: {', '.join(top[:8])}")
        for pc in top[:3]:
            rows = listings.get(pc) or []
            if not rows:
                continue
            print(f"\n  --- {pc} ---")
            for r in rows:
                mark = ">>" if r["is_target"] else "  "
                print(f"  {mark} {r['pc']}: {r['word']}  {r['text']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({
                "kind": WRITERS_KIND, "version": 1, "class": args.want,
                "absent": False,
                "packets": len(sel),
                "lo": f"0x{lo:08X}", "hi": f"0x{hi:08X}",
                "colour_words": sum(1 for v in roles.values() if v == "colour"),
                "vertex_words": sum(1 for v in roles.values() if v == "vertex"),
                "writes": len(entries), "unmapped": unknown,
                "aliased": bool(aliased or stale),
                "mixed_writers": len(mixed),
                "listings": listings,
                "writers": [{"pc": pc, "func": pcfunc.get(pc, ""),
                             "colour": k["colour"], "vertex": k["vertex"],
                             "uv": k["uv"],
                             "colour_only": bool(k["colour"] and not k["vertex"])}
                            for pc, k in ranked],
            }, f, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
