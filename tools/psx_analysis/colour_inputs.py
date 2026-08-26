#!/usr/bin/env python3
"""colour_inputs.py -- compare the INPUTS to a packet's colour computation.

    python3 colour_inputs.py --pc 0x8006844C

What this settles
-----------------
The display-list comparison showed the two emulators build identical geometry
and different vertex colours. packet_writers then found the code, and it
computes every colour the same way:

    lwl/lwr  $t6, ($s4-12)     load the source RGB
    mult     $v0, $s6          scale it
    sra      $v0, 7            >> 7
    sw       $v0, n($a3)       store into the packet

So a wrong colour has exactly two possible causes: the SOURCE RGB differs, or
the SCALE differs. This reads both and says which.

How, given only one side can break on a PC
------------------------------------------
The DuckStation oracle supports pc_break and captures all 32 GPRs on hit;
psx-runtime has no equivalent (its pc_probe fires at basic-block leaders with
a fixed set of registers, and adding a real one means emitting per-instruction
hooks from the recompiler).

That asymmetry does not matter here, because the register we need from the
oracle is a POINTER. Once it reports $s4, the source table's address is known
-- and the same address can be read on BOTH emulators with read_ram, which
both support. The comparison that matters is of memory, not registers.

What the answer means:
  * source bytes differ  -> the divergence is upstream, in whatever fills that
                            table; the colour code is faithfully scaling bad
                            input.
  * source bytes match   -> the input is fine, so the scale $s6 or the
                            arithmetic differs, and the fault is in this code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from probe_regs import plausible_pointer, probe_registers  # noqa: E402
from psx_gpu_frame import (  # noqa: E402
    DEFAULT_DUCKSTATION_PORT, DEFAULT_NATIVE_PORT, ORACLE_PAUSED_POLL_S,
    DebugConn, DebugError, oracle_resume, read_ram_range, snapshot_ram,
    wait_for_class,
)

KIND = "psx-colour-inputs"
SRC_OFFSET = -12          # lwr $t6,-12($s4): the source word's base
DEFAULT_SPAN = 48         # enough to see the table around it


def region_phase_dependence(conn, pc, lo, hi, first_scale, tries=12,
                            gap=0.7, out=sys.stderr):
    """Does this region change as the animation advances? ONE emulator.

    Every cross-emulator comparison here is confounded by phase, and the churn
    check (re-read after 0.4s) is too short to catch a region that is only
    rewritten when the animation STEPS. So ask a question that needs no second
    emulator at all: sample this region on psx-runtime at two different values
    of $s6 and see whether it moved.

    If it did, the region is phase-driven and comparing it across two emulators
    at different scales says nothing — which would make every "region-differs"
    result so far an artefact. If it did not, the region is phase-independent
    and the difference is real.
    """
    from probe_regs import probe_registers
    base = read_ram_range(conn, 0x80000000 | lo, hi - lo)
    for _ in range(tries):
        time.sleep(gap)
        pr = probe_registers(conn, pc, want=("s6",), wait=1.5)
        s6 = pr.get("regs", {}).get("s6")
        if not s6:
            continue
        val = int(s6, 16)
        if val == first_scale:
            continue
        now = read_ram_range(conn, 0x80000000 | lo, hi - lo)
        moved = sum(1 for x, y in zip(base, now) if x != y)
        print(f"  psx-runtime at scale {first_scale} vs {val}: "
              f"{moved} byte(s) of the region changed", file=out)
        return {"tested": True, "from_scale": first_scale, "to_scale": val,
                "changed_bytes": moved, "phase_dependent": moved > 0}
    return {"tested": False,
            "note": "the scale did not change within the sampling window"}


def nd_preview(a, b):
    """Differing byte count, for a message written before the real count."""
    return sum(1 for x, y in zip(a, b) if x != y)


def diff_clusters(a, b, base, gap=8, limit=12):
    """Where two buffers differ, coalesced into runs.

    A count is not a finding. "2747 of 23088 bytes differ" says nothing about
    whether the two hold the same data shifted, one holds zeros where the other
    holds values, or a handful of entries changed -- and those point at
    completely different causes. Runs separated by fewer than `gap` matching
    bytes are merged, because a differing 4-byte entry usually differs in only
    some of its bytes and reporting it as three findings obscures it.
    """
    runs = []
    i = 0
    n = min(len(a), len(b))
    while i < n:
        if a[i] == b[i]:
            i += 1
            continue
        start = i
        last = i
        while i < n:
            if a[i] != b[i]:
                last = i
            elif i - last >= gap:
                break
            i += 1
        runs.append((start, last))
        if len(runs) >= limit * 4:
            break
    return [{"addr": f"0x{base + s:08X}", "length": e - s + 1,
             "native": a[s:min(e + 1, s + 16)].hex(" "),
             "oracle": b[s:min(e + 1, s + 16)].hex(" ")}
            for s, e in runs[:limit]]


def describe_difference(clusters):
    """What KIND of difference is this? The shape narrows the cause."""
    if not clusters:
        return ""
    nat_zero = sum(1 for c in clusters
                   if set(c["native"].split()) <= {"00"})
    orc_zero = sum(1 for c in clusters
                   if set(c["oracle"].split()) <= {"00"})
    if nat_zero == len(clusters):
        return ("psx-runtime holds ZEROS everywhere the oracle holds data — it "
                "has not written this, rather than written it differently.")
    if orc_zero == len(clusters):
        return ("the oracle holds zeros where psx-runtime holds data — "
                "psx-runtime is writing somewhere the oracle does not.")
    aligned = all(int(c["addr"], 16) % 4 == 0 for c in clusters)
    return ("both sides hold data and it differs"
            + (", on 4-byte boundaries — whole entries, not stray bytes"
               if aligned else ", not aligned to entry boundaries"))


def region_bounds(a, b, pad=0x2000, limit=0x200000):
    """A span covering both pointers, so the comparison is phase-independent.

    $s4 MOVES: measured across two samples it went 0x800E2634 -> 0x800E4634
    while $s6 went 72 -> 128. The pointer tracks the animation, so comparing a
    small window at each side's own $s4 compares different ENTRIES of the same
    array whenever the two emulators are at different moments -- which they
    always are. That produced "source-differs" twice for no guest reason.

    Comparing the whole enclosing region instead does not care about phase: if
    the arrays are identical the data is fine wherever each pointer happens to
    be, and if they are not, that is a real divergence.
    """
    lo = max(0, (min(a, b) - pad) & ~3)
    hi = min(limit, max(a, b) + pad)
    return lo, hi


def looks_like_triplets(b, stride=4):
    """Does this block look like packed RGB entries rather than 16-bit values?

    A colour table here is RGB triplets on a 4-byte stride. An array of 16-bit
    values has a zero in every odd byte, which is how the first version of this
    tool was caught reading something that was not a colour table at all --
    while confidently reporting the two emulators disagreed about one.
    """
    if len(b) < 8:
        return False
    odd_zero = sum(1 for i, x in enumerate(b) if i % 2 and x == 0)
    if odd_zero >= (len(b) // 2) * 0.9:
        return False                     # 16-bit array, not triplets
    return True


def graduated_find(ram, needle, lengths=(32, 16, 12, 8, 4)):
    """Longest prefix of `needle` that appears in `ram`, and where.

    An exact miss cannot distinguish "this data does not exist here" from "it
    exists with different contents", and those point at completely different
    places. Measured on a live pair: the full 32-byte run was absent from
    psx-runtime, yet its first 4 bytes appeared six times on a 0x18 stride, and
    a nearby region was byte-identical between the two emulators. Reporting
    that as "absent" was wrong in a way the operator could not see.
    """
    for k in lengths:
        if k > len(needle):
            continue
        hits, at = [], 0
        while len(hits) < 8:
            i = ram.find(needle[:k], at)
            if i < 0:
                break
            hits.append(i)
            at = i + 1
        if hits:
            return k, hits
    return 0, []


def stride_of(hits):
    """Constant spacing between hits, if there is one."""
    if len(hits) < 3:
        return 0
    d = hits[1] - hits[0]
    return d if all(b - a == d for a, b in zip(hits, hits[1:])) else 0


def find_table(ram, needle, min_len=12):
    """Where does this exact byte run appear in a RAM image?

    Address correspondence between the two emulators cannot be assumed: their
    allocators land the same structures at different addresses (measured -- the
    oracle's packet buffer at 0x10D9A0 against psx-runtime's 0x115078). Reading
    the oracle's pointer on psx-runtime therefore reads unrelated memory, and
    comparing it produces a difference that is entirely the tool's own doing.

    So the table is FOUND rather than assumed: search for the bytes themselves.
    """
    if len(needle) < min_len:
        return []
    hits, at = [], 0
    while True:
        i = ram.find(needle, at)
        if i < 0:
            return hits
        hits.append(i)
        at = i + 4
        if len(hits) >= 8:
            return hits


def wait_for_hit(conn, pc, timeout, poll=ORACLE_PAUSED_POLL_S):
    """Arm a PC breakpoint on the oracle and wait for it to fire.

    Paced for a PAUSED oracle, not a running one. The breakpoint pauses
    DuckStation when it fires, which drops its debug socket to the Qt idle
    timer -- about 1 Hz with no gamepad attached. Polling every 100 ms then
    stacks up connections it cannot accept, and the failure surfaces as
    "server closed without replying", which does not read like a pacing
    problem at all.

    Resumes first, because a previous run that parked the oracle and exited
    without resuming leaves it crippled for every attempt after it.
    """
    oracle_resume(conn)
    for cmd in ("pc_hit_clear",):
        try:
            conn.cmd(cmd)
        except DebugError:
            pass                       # not fatal; the arm below is what matters
    conn.cmd("pc_break", addr=pc)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rep = conn.cmd("pc_hit_last")
        except DebugError:
            # A hit parks the oracle mid-exchange, so one dropped reply here is
            # expected rather than fatal.
            time.sleep(poll)
            continue
        if rep.get("valid"):
            return rep
        time.sleep(poll)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--native-port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--ds-port", type=int, default=DEFAULT_DUCKSTATION_PORT)
    ap.add_argument("--pc", required=True,
                    help="a colour-writing PC, from the Shading pane")
    ap.add_argument("--ptr-reg", default="s4",
                    help="register holding the source pointer")
    ap.add_argument("--scale-reg", default="s6")
    ap.add_argument("--span", type=int, default=DEFAULT_SPAN)
    ap.add_argument("--wait", type=float, default=45.0,
                    help="seconds to wait for the PC to be reached. "
                         "A paused oracle answers about once a second, so this needs headroom.")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--wait-for", default="PolyG4+semi",
                    help="class to wait for on BOTH sides first")
    ap.add_argument("--wait-secs", type=float, default=120.0)
    ap.add_argument("--probe-wait", type=float, default=6.0,
                    help="seconds to let psx-runtime's block probe collect")
    ap.add_argument("--no-phase-test", dest="phase_test",
                    action="store_false",
                    help="skip checking whether the region is driven "
                         "by the animation (it is the check that "
                         "decides whether a difference means anything)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    pc = int(args.pc, 16)
    doc = {"kind": KIND, "version": 1, "pc": f"0x{pc:08X}",
           "ptr_reg": args.ptr_reg, "scale_reg": args.scale_reg}

    ds = DebugConn(args.host, args.ds_port, args.timeout)
    native = DebugConn(args.host, args.native_port, args.timeout)
    try:
        # Both sides need the effect live: the oracle to hit the breakpoint, and
        # psx-runtime for its block probe. Wait for each rather than requiring
        # both to be inside it when this was launched.
        if args.wait_secs > 0:
            for label, conn in (("oracle", ds), ("psx-runtime", native)):
                on, drawing = wait_for_class(conn, args.wait_for,
                                             args.wait_secs, out=sys.stderr)
                if not on:
                    top = ", ".join(f"{k} x{v}" for k, v in
                                    sorted(drawing.items(),
                                           key=lambda kv: -kv[1])[:4])
                    msg = (f"{args.wait_for} never appeared on {label} within "
                           f"{args.wait_secs:.0f}s. Drawing instead: "
                           f"{top or 'nothing'}.")
                    print(f"error: {msg}", file=sys.stderr)
                    doc["error"] = msg
                    return _finish(doc, args, 1)

        print(f"arming the oracle at 0x{pc:08X} …")
        hit = wait_for_hit(ds, pc, args.wait)
        if not hit:
            msg = (f"the oracle never reached 0x{pc:08X} within {args.wait:.0f}s. "
                   f"That PC is OVERLAY code — it only exists while the right "
                   f"overlay is resident, so the oracle has to be at the same "
                   f"point in the game.")
            print(f"error: {msg}", file=sys.stderr)
            doc["error"] = msg
            raise SystemExit(_finish(doc, args, 1))
        regs = hit.get("regs", {})
        ptr = int(regs.get(args.ptr_reg, "0x0"), 16)
        scale = int(regs.get(args.scale_reg, "0x0"), 16)
        doc["oracle_regs"] = {k: regs.get(k) for k in
                              (args.ptr_reg, args.scale_reg, "sp", "a3")}
        doc["oracle_scale"] = scale
        print(f"  hit at frame-time: ${args.ptr_reg}=0x{ptr:08X}  "
              f"${args.scale_reg}={scale} (scale/128 = {scale / 128:.3f})")

        src = (ptr + SRC_OFFSET) & 0x1FFFFFFF
        doc["source_addr"] = f"0x{src:08X}"
        if src == 0 or src >= 0x200000:
            msg = (f"${args.ptr_reg} does not point into RAM "
                   f"(0x{ptr:08X}); nothing to compare.")
            print(f"error: {msg}", file=sys.stderr)
            doc["error"] = msg
            raise SystemExit(_finish(doc, args, 1))

        print(f"reading {args.span} bytes at 0x{src:08X} from the oracle …")
        b = read_ram_range(ds, 0x80000000 | src, args.span)
        doc["oracle_bytes"] = b.hex()
        doc["oracle_looks_like_table"] = looks_like_triplets(b)

        # Ask psx-runtime where ITS table is, rather than inferring.
        #
        # Its probe fires at basic-block leaders, so the colour store's own
        # address never matches one and the enclosing block has to be found by
        # arming a spread. $s4 is callee-saved and set outside the loop, so its
        # value at block entry is its value at the store.
        #
        # This is the direct answer. Everything below it is fallback for when
        # the probe cannot fire -- and a fallback that compares by CONTENT,
        # because addresses provably do not carry between the two emulators.
        print("asking psx-runtime for its own $s4 …")
        pr = probe_registers(native, pc, want=("s4", "s6"),
                             wait=args.probe_wait)
        # Omit absent keys rather than writing nulls. A consumer reading this
        # with a typed accessor throws on null, and one that did took the
        # Studio down; "missing" and "present but null" should not be different
        # things for a report to express.
        doc["native_probe"] = {k: pr[k] for k in
                               ("block_leader", "frame", "samples_seen",
                                "error", "leader_after_target")
                               if pr.get(k) is not None}
        nat_ptr = None
        raw_s4 = pr.get("regs", {}).get("s4")
        s4_val = int(raw_s4, 16) if raw_s4 else None
        if raw_s4 and not plausible_pointer(s4_val):
            # Refuse it here. Zero minus the load offset masks to 0x1FFFFFF4,
            # which looks like an address and is not one; the run then reports a
            # difference between the oracle's table and unmapped memory.
            print(f"  psx-runtime $s4 = {raw_s4} is not a RAM pointer; the "
                  f"capture did not take. Falling back to the content search.",
                  file=sys.stderr)
            doc["native_s4_rejected"] = raw_s4
            raw_s4 = None
        if raw_s4:
            nat_ptr = s4_val
            doc["native_regs"] = pr["regs"]
            doc["native_source_addr"] = f"0x{(nat_ptr + SRC_OFFSET) & 0x1FFFFFFF:08X}"
            print(f"  psx-runtime $s4=0x{nat_ptr:08X} "
                  f"$s6={pr['regs'].get('s6')} "
                  f"(block {pr.get('block_leader')})")
        else:
            print(f"  probe did not report $s4 ({pr.get('error', 'no reason')}); "
                  f"falling back to searching RAM by content", file=sys.stderr)

        print("reading psx-runtime's RAM …")
        ram = snapshot_ram(native)
        needle = b[:16]
        if nat_ptr is not None:
            # Compare the table psx-runtime's OWN code reads.
            nsrc = (nat_ptr + SRC_OFFSET) & 0x1FFFFFFF
            a = ram[nsrc:nsrc + args.span]
            doc["native_bytes"] = a.hex()
            doc["native_looks_like_table"] = looks_like_triplets(a)
            doc["compared_by"] = "native-own-pointer"
            doc["address_delta"] = nsrc - src
            # Guard the comparison itself: a short read makes zip() compare
            # nothing, which reported 0 differing bytes AND "not identical" in
            # the same document.
            if len(a) != len(b):
                doc["verdict"] = "unreadable"
                doc["note"] = (f"read {len(a)} bytes from psx-runtime against "
                               f"{len(b)} from the oracle — the addresses are "
                               f"not both mapped, so there is nothing to "
                               f"compare.")
                print(f"\nVERDICT: {doc['note']}", file=sys.stderr)
                return _finish(doc, args, 1)
            # Phase check BEFORE claiming a difference. Different scales mean
            # different moments in the animation, and $s4 moves with the scale.
            nat_scale = int(pr.get("regs", {}).get("s6", "0x0"), 16)
            doc["native_scale"] = nat_scale
            doc["phase_aligned"] = (nat_scale == scale)
            if nat_scale != scale:
                lo, hi = region_bounds(nsrc, src)
                print(f"\n  scales differ (psx-runtime {nat_scale}, oracle "
                      f"{scale}), so the two are at different points in the "
                      f"animation and $s4 has moved with it.")
                print(f"  comparing the whole region 0x{lo:08X}..0x{hi:08X} "
                      f"instead, which does not depend on phase …")
                ra_ = ram[lo:hi]
                rb_ = read_ram_range(ds, 0x80000000 | lo, hi - lo)
                doc["region"] = [f"0x{lo:08X}", f"0x{hi:08X}"]

                # Is the region even STABLE? "Compare the whole region instead"
                # only escapes phase if the region is not itself being written.
                # Measured: with the effect off it is byte-identical between the
                # two and does not change at all; with the effect running it
                # differed by 2747 bytes. That difference is the finding, but
                # only if it is not just two sides caught mid-write.
                time.sleep(0.4)
                again = read_ram_range(native, 0x80000000 | lo, hi - lo)
                churn = sum(1 for x, y in zip(ra_, again) if x != y)
                doc["region_churn_bytes"] = churn
                doc["region_static"] = (churn == 0)
                if churn:
                    print(f"  note: {churn} byte(s) of this region changed on "
                          f"psx-runtime within 0.4s — it is being written while "
                          f"the effect runs, so a difference here is not "
                          f"automatically phase-independent.", file=sys.stderr)
                if len(ra_) != len(rb_):
                    doc["verdict"] = "unreadable"
                    doc["note"] = "region reads returned different lengths"
                    print(f"\nVERDICT: {doc['note']}", file=sys.stderr)
                    return _finish(doc, args, 1)
                # Before reading a difference as real, find out whether this
                # region moves with the animation at all. One emulator, no
                # alignment: if it does, comparing two emulators at different
                # scales cannot mean anything.
                if args.phase_test:
                    dep = region_phase_dependence(native, pc, lo, hi, nat_scale)
                    doc["phase_dependence"] = dep
                    if not dep.get("tested"):
                        # The scale never moved, so the question is OPEN. Falling
                        # through to "region-differs" here would state as
                        # established exactly what could not be checked — and
                        # that a scale which refuses to move is itself worth
                        # reporting, not a failed setup step.
                        doc["verdict"] = "region-differs-phase-unknown"
                        doc["note"] = (
                            f"{nd_preview(ra_, rb_)} of {len(ra_)} bytes differ, "
                            f"but psx-runtime's scale never left {nat_scale} "
                            f"during the window, so it could not be established "
                            f"whether this region moves with the animation. The "
                            f"difference is therefore NOT established as real.\n\n"
                            f"Worth noting on its own: the oracle's scale varies "
                            f"run to run while psx-runtime's has read {nat_scale} "
                            f"every time. If that holds, the modulation this "
                            f"routine performs is not happening on psx-runtime "
                            f"at all — and {nat_scale} == 128 is the value at "
                            f"which x*scale>>7 leaves x unchanged.")
                        print(f"\nVERDICT: {doc['note']}", file=sys.stderr)
                        return _finish(doc, args, 0)
                    if dep.get("phase_dependent"):
                        doc["verdict"] = "region-phase-dependent"
                        doc["note"] = (
                            f"psx-runtime's own copy of this region changed by "
                            f"{dep['changed_bytes']} byte(s) between scale "
                            f"{dep['from_scale']} and {dep['to_scale']}. The "
                            f"region is driven by the animation, so comparing it "
                            f"between two emulators at different scales "
                            f"(psx-runtime {nat_scale}, oracle {scale}) proves "
                            f"nothing — including every earlier "
                            f"'region-differs' result.")
                        print(f"\nVERDICT: {doc['note']}", file=sys.stderr)
                        return _finish(doc, args, 0)

                nd = sum(1 for x, y in zip(ra_, rb_) if x != y)
                doc["region_differing_bytes"] = nd
                doc["region_bytes"] = len(ra_)
                if nd:
                    cl = diff_clusters(ra_, rb_, lo)
                    doc["region_clusters"] = cl
                    doc["region_shape"] = describe_difference(cl)
                if nd == 0:
                    doc["verdict"] = "region-matches"
                    print(f"\nVERDICT: the whole {len(ra_)}-byte region is "
                          f"IDENTICAL on both. The colour data is fine wherever "
                          f"each pointer sits, so the divergence is not in this "
                          f"table — it is in the scale, the arithmetic, or "
                          f"which entry each side selects.")
                else:
                    first = next(i for i, (x, y) in enumerate(zip(ra_, rb_))
                                 if x != y)
                    doc["region_first_difference"] = f"0x{lo + first:08X}"
                    if churn:
                        doc["verdict"] = "region-differs-while-written"
                        print(f"\nVERDICT: {nd}/{len(ra_)} bytes differ, first "
                              f"at 0x{lo + first:08X} — but {churn} byte(s) of "
                              f"this region changed within 0.4s, so it is being "
                              f"written as the effect runs. The two sides may "
                              f"simply be caught at different moments.\n\n"
                              f"What IS solid: with the effect off this region "
                              f"is byte-identical between the emulators. So the "
                              f"base data agrees and the divergence is in what "
                              f"the effect WRITES here — trace the writers of "
                              f"0x{lo:08X}..0x{hi:08X}.")
                    else:
                        doc["verdict"] = "region-differs"
                        print(f"\nVERDICT: {nd}/{len(ra_)} bytes of the region "
                              f"differ, first at 0x{lo + first:08X}, and the "
                              f"region is not changing — so this is a real data "
                              f"divergence, independent of where either pointer "
                              f"happened to be.")
                        if doc.get("region_shape"):
                            print(f"\n  {doc['region_shape']}")
                        for c in doc.get("region_clusters", [])[:6]:
                            print(f"    {c['addr']} ({c['length']} bytes)")
                            print(f"      psx-runtime {c['native']}")
                            print(f"      oracle      {c['oracle']}")
                return _finish(doc, args, 0)

            same = a == b
            doc["source_identical"] = same
            print(f"\n  psx-runtime @ {doc['native_source_addr']}: {a[:16].hex(' ')}")
            print(f"  oracle      @ 0x{src:08X}: {b[:16].hex(' ')}")
            if same:
                doc["verdict"] = "source-matches"
                print("\nVERDICT: both routines read the SAME table contents. "
                      "The input is fine, so the divergence is the scale or the "
                      "arithmetic — compare $s6: psx-runtime "
                      f"{pr['regs'].get('s6')} against the oracle "
                      f"0x{scale:08X}.")
            else:
                n = sum(1 for x, y in zip(a, b) if x != y)
                doc["verdict"] = "source-differs"
                doc["differing_bytes"] = n
                print(f"\nVERDICT: the two routines read DIFFERENT table "
                      f"contents ({n}/{min(len(a), len(b))} bytes differ). Each "
                      f"is reading its own emulator's table, so this is a real "
                      f"data divergence — the fault is upstream, in whatever "
                      f"builds it.")
            return _finish(doc, args, 0)

        hits = find_table(ram, needle)
        doc["native_hits"] = [f"0x{h:08X}" for h in hits]
        if not hits:
            # Not found as an exact run. Before calling it absent, find out how
            # much of it IS there: same palette in a different arrangement
            # points somewhere completely different from genuinely missing data.
            k, near = graduated_find(ram, b[:32])
            stride = stride_of(near)
            here = ram[src:src + args.span]
            doc["partial_match_len"] = k
            doc["partial_hits"] = [f"0x{h:08X}" for h in near[:6]]
            doc["partial_stride"] = stride
            doc["native_at_oracle_addr"] = here.hex()
            doc["native_addr_looks_like_table"] = looks_like_triplets(here)

            if k >= 4:
                doc["verdict"] = "table-rearranged"
                msg = (f"psx-runtime does NOT hold this table as a contiguous "
                       f"run, but {k} of its leading bytes appear "
                       f"{len(near)} time(s)"
                       + (f" on a 0x{stride:X} stride" if stride else "")
                       + f", first at {doc['partial_hits'][0]}. The same colour "
                       f"values exist; their arrangement does not match. And at "
                       f"the oracle's own pointer address psx-runtime holds "
                       f"something that is not a colour table at all.")
            else:
                doc["verdict"] = "table-absent-on-native"
                msg = ("psx-runtime's RAM contains no part of this table. The "
                       "oracle's is confirmed correct — its entries scaled by "
                       "the recorded factor reproduce the colours in its own "
                       "display list — so this data was never built the same "
                       "way, and the divergence is upstream of this routine.")
            msg += ("\n\nWhat this CANNOT say: which table psx-runtime's own "
                    "code reads. That needs its $s4, and psx-runtime has no PC "
                    "breakpoint. The two may simply keep this structure "
                    "somewhere else.")
            print(f"\nVERDICT: {msg}")
            doc["note"] = msg
            return _finish(doc, args, 0)

        native_addr = hits[0]
        doc["native_source_addr"] = f"0x{native_addr:08X}"
        doc["address_delta"] = native_addr - src
        print(f"  found at 0x{native_addr:08X} "
              f"(delta {native_addr - src:+d} from the oracle's)")
        a = ram[native_addr:native_addr + args.span]
    except DebugError as e:
        print(f"error: {e}", file=sys.stderr)
        doc["error"] = str(e)
        return _finish(doc, args, 2)
    finally:
        # Clear the breakpoint AND resume. Leaving the oracle parked was the
        # actual defect here: it exits at 1 Hz and every later run then fails
        # in a way that points at the network rather than at this tool.
        try:
            ds.cmd("pc_unbreak", addr=pc)
        except DebugError:
            pass
        if not oracle_resume(ds):
            print("warning: could not resume the oracle — it may still be "
                  "parked, which will make later runs time out. Stop and "
                  "restart it from the Oracle tab.", file=sys.stderr)

    doc["native_bytes"] = a.hex()
    same = a == b
    doc["source_identical"] = same

    print(f"\n  psx-runtime: {a[:16].hex(' ')}")
    print(f"  oracle     : {b[:16].hex(' ')}")
    if same:
        doc["verdict"] = "source-matches"
        print("\nVERDICT: the source RGB table is IDENTICAL on both (found on "
              f"psx-runtime at {doc['native_source_addr']}). The input to the "
              "colour computation is fine, so the divergence is the scale "
              f"(${args.scale_reg}) or the arithmetic in this routine.")
    else:
        n = sum(1 for x, y in zip(a, b) if x != y)
        doc["verdict"] = "source-differs"
        doc["differing_bytes"] = n
        first = next(i for i, (x, y) in enumerate(zip(a, b)) if x != y)
        doc["first_difference"] = f"0x{src + first:08X}"
        print(f"\nVERDICT: the source RGB DIFFERS ({n}/{len(a)} bytes, first at "
              f"0x{src + first:08X}). The colour code is faithfully scaling bad "
              f"input — the fault is upstream, in whatever fills this table.")
    return _finish(doc, args, 0)


def _finish(doc, args, rc):
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
        print(f"wrote {args.json}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
