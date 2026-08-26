#!/usr/bin/env python3
"""gpu_colour_parity.py -- compare vertex-colour distributions between emulators.

    python3 gpu_colour_parity.py --class "PolyG4+semi|B+F" --samples 40

Why a distribution and not a diff
---------------------------------
Comparing one frame against one frame requires the two emulators to be at the
same instant, and nothing short of deterministic replay gets them there. An
animating effect sampled at two different phases differs everywhere, which says
nothing -- a single-frame byte diff of a live effect is a trap, and reading one
is how an earlier attempt at this went wrong.

A distribution does not care about phase. If one emulator's additive quads peak
at colour 199 and the other's at 40, that is a real difference no matter which
frame each was on. If both peak in the same place, the colours are fine and the
divergence is elsewhere.

Each side is sampled the way it can be: psx-runtime through its GP0 ring, the
oracle by walking the ordering table out of RAM, since it has no ring.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_DUCKSTATION_PORT, DEFAULT_NATIVE_PORT, RAM_SIZE, STP_MODES,
    DebugConn, DebugError, capture, decode_entries, find_display_lists,
    read_ram_range, snapshot_ram, walk_ordering_table,
)

PARITY_KIND = "psx-colour-parity"


def prim_class(p):
    mode = STP_MODES.get(p.get("stp", 0), "?") if p.get("semi") else "opaque"
    return f"{p['op_name']}|{mode}"


def colours_of(prims, want):
    out = []
    for p in prims:
        if p["kind"] not in ("poly", "rect", "line", "fill"):
            continue
        if want and prim_class(p) != want:
            continue
        for c in p.get("colors") or []:
            out.append(c)
    return out


def sample_native(conn, want, samples, gap):
    """psx-runtime: the GP0 ring already holds decoded packets per frame."""
    cols, frames = [], []
    for _ in range(samples):
        try:
            span = conn.ring_span()
            d = capture(conn, frame=span["newest"], count=30000, verify_ring=False)
        except DebugError:
            time.sleep(gap)
            continue
        frames.append(d["frame"])
        cols += colours_of(d["prims"], want)
        time.sleep(gap)
    return cols, frames


def oracle_region(conn, lo, hi, pad=0x2000):
    """Read just the span a display list occupies, placed at its real offset.

    walk_ordering_table() indexes a full-RAM image, so a partial read has to
    be dropped at the address it came from rather than handed over as a
    standalone buffer. Reading 2 MB per sample would be 128 round trips each
    (the oracle caps read_ram at 16 KB); the list itself is a few tens of KB.
    """
    lo = max(0, (lo - pad) & ~3)
    hi = min(RAM_SIZE, hi + pad)
    blob = read_ram_range(conn, 0x80000000 + lo, hi - lo)
    ram = bytearray(RAM_SIZE)
    ram[lo:lo + len(blob)] = blob
    return bytes(ram)


def sample_oracle(conn, want, samples, gap, addr=None):
    """The oracle has no GP0 ring, so walk the ordering table out of RAM.

    Two things this deliberately does NOT do.

    It does not follow DMA channel 2. A finished linked-list transfer leaves
    the 0xFFFFFF terminator in MADR, so between frames MADR names the end of
    the list rather than its start. The list is found by scanning for its
    structure instead (see find_display_lists).

    It does not pause. DuckStation pumps its debug socket from the emulation
    loop, so a paused oracle falls back to the Qt idle timer -- 1 Hz with no
    gamepad attached -- and the reads cannot finish. Sampling a running
    emulator can occasionally straddle a rebuild of the list; that shows up
    as a short or unwalkable chain and is counted as a miss rather than
    folded into the distribution.
    """
    cols, frames = [], []
    misses = 0
    root, span = addr, None
    for _ in range(samples):
        try:
            if root is None or span is None:
                # Locate the list once, then reuse it: a full snapshot plus a
                # scan is expensive, re-walking a known root is not.
                ram = snapshot_ram(conn)
                cands = find_display_lists(ram, near=root)
                if not cands:
                    misses += 1
                    time.sleep(gap)
                    continue
                best = cands[0]
                root, span = best["root"], (best["lo"], best["hi"])
            else:
                ram = oracle_region(conn, span[0], span[1])

            prims = decode_entries(walk_ordering_table(ram, root))
            if not prims:
                # The list moved or was mid-rebuild. Re-scan next time.
                root = span = None
                misses += 1
                time.sleep(gap)
                continue
            frames.append(conn.frame())
            cols += colours_of(prims, want)
        except DebugError:
            misses += 1
            root = span = None
        time.sleep(gap)
    if misses:
        print(f"  ({misses} of {samples} oracle sample(s) could not be walked)",
              file=sys.stderr)
    if root is not None:
        print(f"  oracle display list at 0x{root:06X}", file=sys.stderr)
    return cols, frames


HIST_BUCKETS = 16


def histogram(flat):
    """Normalised 16-bucket histogram of channel values.

    Normalised because the two sides never sample the same number of
    vertices, and every raw count then differs for a reason that has
    nothing to do with the guest.
    """
    h = [0] * HIST_BUCKETS
    for v in flat:
        h[min(HIST_BUCKETS - 1, v * HIST_BUCKETS // 256)] += 1
    total = sum(h) or 1
    return [c / total for c in h]


def overlap(a, b):
    """Histogram intersection: sum of min() over normalised bins, 0..1.

    1.0 means the two distributions are identical in shape; 0.0 means they
    share nothing. This is the statistic the verdict rests on, because it
    is invariant to BOTH animation phase and sample count -- the two things
    that make a naive comparison lie.
    """
    return sum(min(x, y) for x, y in zip(a, b))


def describe(cols, label, out=sys.stdout):
    if not cols:
        print(f"  {label}: no matching primitives sampled", file=out)
        return None
    flat = [v for c in cols for v in c]
    srt = sorted(flat)
    peak = srt[-1]
    mean = statistics.mean(flat)
    p50 = srt[len(srt) // 2]
    p90 = srt[int(len(srt) * 0.9)]
    print(f"  {label:<14} {len(cols):>6} vertices   peak {peak:>3}   "
          f"mean {mean:>6.1f}   p50 {p50:>3}   p90 {p90:>3}", file=out)
    return {"vertices": len(cols), "peak": peak, "mean": round(mean, 2),
            "p50": p50, "p90": p90,
            "hist": [round(x, 5) for x in histogram(flat)]}


def verdict_of(a, b, out=sys.stdout):
    """Compare two distributions, and say when the answer is not safe.

    Deliberately NOT decided on `peak`. A peak is a maximum, so it grows
    with the number of samples drawn; comparing 3008 vertices against 1024
    on peak alone reported "differ" for two distributions whose medians and
    p90s agreed. That was a wrong answer stated confidently, which is worse
    than no answer.
    """
    if not a or not b:
        return None, "one side sampled nothing"
    ov = overlap(a["hist"], b["hist"])
    ratio = max(a["vertices"], b["vertices"]) / max(1, min(a["vertices"],
                                                          b["vertices"]))
    print(f"\n  distribution overlap {ov:.3f}   "
          f"sample ratio {ratio:.2f}x", file=out)
    # A note is not enough past a certain point. At 13x -- seen live, the oracle
    # contributing a single frame of 64 quads against thirteen frames from the
    # runtime -- one side is a snapshot of an ANIMATING effect and the other is
    # an average over its cycle. Those two things differ by construction, and
    # reporting "differ" from them says nothing about the emulators.
    if ratio > 4.0:
        return None, (
            f"sample sizes are {ratio:.0f}x apart — one side contributed roughly "
            f"a single frame of an animating effect while the other averaged over "
            f"many. That is not a comparison. Raise --samples, or check the "
            f"oracle's 'could not be walked' count.")
    if ratio > 2.0:
        print("  note: sample sizes are lopsided; the overlap still holds "
              "(it is normalised) but peaks are not comparable.", file=out)
    if ov >= 0.85:
        return "match", (
            "the colour distributions match. Both emulators build the same "
            "colours, so a visible difference is NOT coming from vertex "
            "colour — look at rasterisation or blending.")
    if ov < 0.70:
        return "differ", (
            "the distributions genuinely differ in shape. The two are "
            "building different colours, so the divergence is upstream of "
            "the renderer.")
    return "inconclusive", (
        "the distributions are close but not clearly the same. Sample more "
        "frames, and make sure both are showing the same effect.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--native-port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--ds-port", type=int, default=DEFAULT_DUCKSTATION_PORT)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--class", dest="want", default=None,
                    help='primitive class, e.g. "PolyG4+semi|B+F" (default: all)')
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--gap", type=float, default=0.25, help="seconds between samples")
    ap.add_argument("--oracle-addr", default=None,
                    help="ordering-table root on the oracle, if DMA cannot be read")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    native = DebugConn(args.host, args.native_port, args.timeout)
    oracle = DebugConn(args.host, args.ds_port, args.timeout)
    try:
        native.frame()
    except DebugError as e:
        print(f"error: psx-runtime unreachable: {e}", file=sys.stderr)
        return 2
    try:
        oracle.frame()
    except DebugError as e:
        print(f"error: oracle unreachable: {e}", file=sys.stderr)
        return 2

    want = args.want
    print(f"sampling {args.samples}x from each"
          + (f", class {want}" if want else ", all classes"))
    ncols, nframes = sample_native(native, want, args.samples, args.gap)
    addr = int(args.oracle_addr, 0) if args.oracle_addr else None
    ocols, oframes = sample_oracle(oracle, want, args.samples, args.gap, addr)

    print(f"\nframes covered — psx-runtime {min(nframes) if nframes else '-'}.."
          f"{max(nframes) if nframes else '-'}   oracle "
          f"{min(oframes) if oframes else '-'}..{max(oframes) if oframes else '-'}")
    print("\ncolour distribution (all channels, 0..255):")
    a = describe(ncols, "psx-runtime")
    b = describe(ocols, "oracle")

    verdict, why = verdict_of(a, b)
    if verdict:
        print(f"\nVERDICT ({verdict}): {why}")
        print("\nNote: this is phase-independent — it does not require the two to "
              "be on the same frame — but both must be showing the same EFFECT.")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            doc = {"kind": PARITY_KIND, "version": 1, "class": want,
                   "native": a, "oracle": b, "verdict": verdict}
            if a and b:
                doc["overlap"] = round(overlap(a["hist"], b["hist"]), 4)
                doc["sample_ratio"] = round(
                    max(a["vertices"], b["vertices"])
                    / max(1, min(a["vertices"], b["vertices"])), 3)
            json.dump(doc, f, indent=1)
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
