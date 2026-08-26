#!/usr/bin/env python3
"""fingerprint_diff.py -- find the FIRST frame where two emulators forked.

    python3 fingerprint_diff.py --arm          # start recording on the oracle
    python3 fingerprint_diff.py                # compare what has been recorded

Why this beats comparing pictures
---------------------------------
`frame_fingerprint` is a rolling FNV-1a hash of every guest write since boot,
snapshotted once per frame. Because it is cumulative, frame N's hash answers
"did the entire write history up to frame N match" -- so the first frame whose
hash differs is the frame execution diverged. That is usually many frames
before anything reaches the screen, and it is how a non-visual fault gets
caught before it becomes a visual one downstream.

Four columns, deliberately kept apart (see debug_server.c): main RAM (`wr`),
the store-PC path signature (`pc`), device registers (`mmio`), and scratchpad
(`sp`). Folding device churn into the RAM hash is what previously hid a real
first divergence -- the RAM hash only ever sees the AFTERMATH of a
device-interaction fork, so it points frames too late.

Read the coverage check first
-----------------------------
The two emulators must observe the SAME SET of writes or the hashes compare
nothing. psxrecomp hooks its memory.c write paths; the oracle hooks the
interpreter's store path, which does not see DMA-written RAM. So this tool
compares write COUNTS before it compares hashes, and refuses to report a
divergence frame if the counts show the two are watching different things.
A confident wrong answer here would send you hunting a fork that isn't real.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_DUCKSTATION_PORT, DEFAULT_NATIVE_PORT, DebugConn, DebugError,
)

COLUMNS = [("wr", "wc", "main RAM"), ("pc", None, "store-PC path"),
           ("mmio", "mc", "device registers"), ("sp", "sc", "scratchpad")]


# Frames per request. One fingerprint row is ~150 bytes of JSON, and the
# oracle replies over a socket whose buffer truncates anything much past
# 16 KB -- at which point the read blocks forever waiting for a newline
# that never comes. Asking for a whole 8192-frame ring in one call does
# exactly that. Paging is not an optimisation here; it is the difference
# between working and hanging.
PAGE = 64


def pull(conn, count, arm=None, reset=False):
    """Control-only call: arm/reset, and report the ring header."""
    kw = {"count": 1}
    if arm is not None:
        kw["arm"] = 1 if arm else 0
    if reset:
        kw["reset"] = 1
    rep = conn.cmd("frame_fingerprint", **kw)
    rows = {int(e["frame"]): e for e in rep.get("entries", [])}
    return rows, rep


def pull_window(conn, lo, hi, page=PAGE):
    """Every fingerprint row in [lo, hi], fetched a page at a time."""
    rows = {}
    f = lo
    while f <= hi:
        top = min(hi, f + page - 1)
        rep = conn.cmd("frame_fingerprint", count=page,
                       frame_lo=f, frame_hi=top)
        got = rep.get("entries", [])
        for e in got:
            rows[int(e["frame"])] = e
        f = top + 1
    return rows


def alignment_verdict(a, b, frames):
    """Do equal frame numbers mean the same moment in the game?

    Each emulator counts frames from its OWN boot, so overlapping frame
    numbers prove nothing -- start one thirty seconds before the other and
    both will happily report a frame 500 that are half a minute apart. A
    diff across that reports a divergence at the first frame compared, every
    time, and it looks exactly like a real finding.

    The guest CYCLE counter is the honest key. Measured on a live pair, the
    two agree on cycles-per-frame to about 0.3% (564480 vs 566204), so if
    frame N carries wildly different cycle counts on the two sides they were
    not booted together and nothing downstream is comparable.
    """
    pairs = [(int(a[f]["cyc"]), int(b[f]["cyc"])) for f in frames
             if "cyc" in a[f] and "cyc" in b[f]]
    if not pairs:
        return None, "neither side reports a guest cycle count"
    # Compare the SPAN, not the absolute value: a constant offset just means
    # different boot instants, which run_to_frame can correct. A different
    # slope means the two are not even running the same code.
    offs = [x - y for x, y in pairs]
    drift = max(offs) - min(offs)
    span = max(p[0] for p in pairs) - min(p[0] for p in pairs)
    if span <= 0:
        return None, "only one frame in common — nothing to compare"
    rel = drift / span
    if rel > 0.05:
        return False, (f"guest cycles drift {rel:.1%} across the window — "
                       f"the two are not running in step")
    if abs(offs[0]) > 10 * (span / max(1, len(pairs))):
        return False, (f"frame numbers line up but guest cycles are "
                       f"{offs[0]:+d} apart — the two were booted at "
                       f"different times, so equal frame numbers are "
                       f"different moments. Boot both together.")
    return True, f"guest cycles track (drift {rel:.2%} over the window)"


def coverage_verdict(a, b, frames):
    """Do the two sides appear to be watching the same writes?"""
    if not frames:
        return None, "no overlapping frames"
    ratios = []
    for f in frames:
        wa = int(a[f].get("wc", 0))
        wb = int(b[f].get("wc", 0))
        if wa and wb:
            ratios.append(wb / wa)
    if not ratios:
        return False, "one side recorded no writes at all"
    lo, hi = min(ratios), max(ratios)
    mid = sorted(ratios)[len(ratios) // 2]
    if mid < 0.5 or mid > 2.0:
        return False, (f"write counts differ by {mid:.2f}x — the two are not "
                       f"observing the same set of writes (DMA coverage?)")
    # 2.0 was too loose: a live pair sat at 1.98x -- the oracle's CPU-store hook
    # not seeing DMA-written RAM -- and squeaked through as "comparable".
    if mid < 0.67 or mid > 1.5:
        return False, (f"write counts differ by {mid:.2f}x — the two are not "
                       f"observing the same set of writes (DMA coverage?)")
    if hi / max(lo, 1e-9) > 4.0:
        return False, (f"write-count ratio drifts {lo:.2f}x..{hi:.2f}x across "
                       f"frames — coverage is not comparable")
    return True, f"write counts track within {lo:.2f}x..{hi:.2f}x"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--native-port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--ds-port", type=int, default=DEFAULT_DUCKSTATION_PORT)
    ap.add_argument("--count", type=int, default=600,
                    help="how many recent frames to compare")
    ap.add_argument("--arm", action="store_true",
                    help="arm + reset the oracle's recorder, then exit")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    native = DebugConn(args.host, args.native_port, args.timeout)
    oracle = DebugConn(args.host, args.ds_port, args.timeout)

    if args.arm:
        try:
            _, rep = pull(oracle, 1, arm=True, reset=True)
        except DebugError as e:
            print(f"error: oracle: {e}", file=sys.stderr)
            return 2
        print("oracle fingerprint armed and reset.")
        if not rep.get("interpreter", True):
            print("\nWARNING: the oracle is NOT in interpreter mode. The store "
                  "hook lives in the interpreter's write path, so a recompiler "
                  "build records almost nothing and any 'match' is meaningless.\n"
                  "Set [CPU] ExecutionMode = Interpreter in its settings.ini "
                  "and restart it.", file=sys.stderr)
            return 1
        print("Let both run through the sequence, then re-run without --arm.")
        return 0

    # Ask each side where it is, then page a common window out of both. A frame
    # number means "frames since THIS emulator booted", so the window is chosen
    # from whichever has seen fewer -- reaching past that on the other side
    # would compare a frame against nothing.
    try:
        _, nrep = pull(native, 1)
        nframe = native.frame()
    except DebugError as e:
        print(f"error: psx-runtime: {e}", file=sys.stderr)
        return 2
    try:
        _, orep = pull(oracle, 1)
        oframe = oracle.frame()
    except DebugError as e:
        print(f"error: oracle: {e}", file=sys.stderr)
        return 2

    top = min(nframe, oframe)
    lo = max(0, top - args.count + 1)
    print(f"psx-runtime at frame {nframe}, oracle at {oframe} — "
          f"comparing {lo}..{top}")
    try:
        na = pull_window(native, lo, top)
        ob = pull_window(oracle, lo, top)
    except DebugError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not orep.get("armed", False):
        print("the oracle's recorder is not armed — run with --arm first, then "
              "replay the sequence.", file=sys.stderr)
        return 1
    if not orep.get("interpreter", True):
        print("the oracle is not in interpreter mode; its fingerprint is not "
              "trustworthy. See --arm output.", file=sys.stderr)
        return 1

    frames = sorted(set(na) & set(ob))
    print(f"psx-runtime {len(na)} frame(s), oracle {len(ob)}, "
          f"{len(frames)} overlapping")
    if not frames:
        print("\nNo overlapping frame numbers. The two count frames from their "
              "own boots, so they must be started from the same point — or the "
              "oracle armed at a known frame — before this can align.",
              file=sys.stderr)
        return 1

    aligned, awhy = alignment_verdict(na, ob, frames)
    print(f"alignment: {awhy}")
    if aligned is False:
        print("\nREFUSING to compare: equal frame numbers do not mean the same "
              "moment, so any divergence reported would be an artefact of when "
              "each emulator was started.", file=sys.stderr)
        return 1

    ok, why = coverage_verdict(na, ob, frames)
    print(f"coverage: {why}")
    if not ok:
        print("\nREFUSING to report a divergence frame: the two sides are not "
              "watching the same writes, so a hash mismatch would say nothing "
              "about the guest. Fix coverage first.", file=sys.stderr)
        return 1

    print(f"\ncomparing frames {frames[0]}..{frames[-1]}")
    found = False
    for key, cnt, label in COLUMNS:
        # Per-column coverage. The global check compares total write counts, so
        # a column where one side records thousands and the other records ZERO
        # sails straight through it -- and then reports a "first divergence"
        # that is really "this emulator never watched this at all". Seen live:
        # the oracle logged 0 scratchpad writes against psx-runtime's 6916.
        if cnt:
            ca = sum(int(na[f].get(cnt, 0)) for f in frames)
            cb = sum(int(ob[f].get(cnt, 0)) for f in frames)
            if (ca == 0) != (cb == 0):
                blind = "oracle" if cb == 0 else "psx-runtime"
                print(f"  {label:<18} NOT COMPARABLE — {blind} recorded no "
                      f"writes here at all (other side: "
                      f"{max(ca, cb)}). This column is a coverage hole, not a "
                      f"divergence.")
                continue

        first = None
        for f in frames:
            if na[f].get(key) != ob[f].get(key):
                first = f
                break
        if first is None:
            print(f"  {label:<18} identical across all {len(frames)} frames")
            continue

        # Divergence on the very first frame compared means the fork happened
        # BEFORE the window, not at its edge. Naming that frame would point at
        # an arbitrary boundary -- the frame the window happened to start on.
        if first == frames[0]:
            print(f"  {label:<18} already divergent at the START of the window "
                  f"(frame {first}) — the fork is EARLIER. Widen with --count, "
                  f"or restart both and compare from boot.")
            found = True
            continue

        found = True
        prev = frames[frames.index(first) - 1]
        print(f"  {label:<18} FIRST DIVERGENCE at frame {first} "
              f"(last agreeing: {prev})")
        print(f"      psx-runtime {na[first].get(key)}"
              + (f"  writes={na[first].get(cnt)}" if cnt else ""))
        print(f"      oracle      {ob[first].get(key)}"
              + (f"  writes={ob[first].get(cnt)}" if cnt else ""))

    if not found:
        print("\nVERDICT: no divergence in the recorded window. Either the fork "
              "is outside it, or it is in state this fingerprint does not cover "
              "(GPU-internal, SPU, timers).")
    else:
        print("\nUse the EARLIEST divergent frame, and prefer the device-register "
              "and scratchpad columns when they fire before main RAM — RAM sees "
              "only the aftermath.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
