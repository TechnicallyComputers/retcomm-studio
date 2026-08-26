#!/usr/bin/env python3
"""effect_palette.py -- does the oracle build the same effect geometry we do?

    python3 tools/effect_palette.py --samples 8

The land-creation effect renders on psx-runtime as hard-edged coloured
wedges. Measured from psx-runtime's own GP0 ring, the additive Gouraud quads
that make them carry a signature that a correctly-rendering frame of the same
game does not:

    frame                  quads  colours  saturated  vertex-y span
    good  (renders right)    144        5          0          155 px
    wedge (the effect)        64      151         68          599 px

Five distinct vertex colours against 151, and geometry spanning 599 lines on
a 240-line screen. The question this tool answers is whether DuckStation,
running the same effect, builds the same thing.

Why a signature and not a diff
------------------------------
Every attempt to compare the two emulators frame-against-frame in this
investigation has foundered on phase: the effect is a fade, so two captures
taken seconds apart differ for reasons that have nothing to do with the bug,
and locking the phase needs a register match that is itself unreliable. A
signature sidesteps that entirely. "How many distinct colours do the additive
quads carry" is a property of the geometry the effect builds, not of where it
is in its fade -- the same way class counts were phase-robust while pixel
diffs were not. 5-vs-151 is not a number that drifts with phase.

psx-runtime is measured from the GP0 ring (retrospective, nothing paused).
The oracle has no ring, so it is parked on the effect's own code and its
ordering table is walked out of guest RAM -- parking on a PC that only
executes during the effect is what puts it inside the animation without
anyone timing anything.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gpu_display_list import blend_of, walk_side  # noqa: E402
from psx_gpu_frame import (  # noqa: E402
    DEFAULT_DUCKSTATION_PORT, DEFAULT_NATIVE_PORT,
    DebugConn, DebugError, capture, oracle_clear_breaks, oracle_resume,
)

KIND = "psx-effect-palette"

SATURATION = 80     # max-min channel spread that counts as "saturated"


VERT_RE = re.compile(r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)")


def parse_verts(v):
    """Vertices as a list of (x, y), from either prim shape.

    The GP0 ring dump carries verts as [[x, y], ...]; gpu_display_list's
    report() formats them as the string "(x,y) (x,y) ...". A tool that reads
    both sides has to accept both, and indexing the string shape as if it
    were the list shape is a crash, not a wrong answer -- which is what it
    did.
    """
    if not v:
        return []
    if isinstance(v, str):
        return [(int(a), int(b)) for a, b in VERT_RE.findall(v)]
    out = []
    for item in v:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((int(item[0]), int(item[1])))
    return out


def parse_colors(c):
    """Vertex colours as a list of (r, g, b), from either prim shape."""
    if not c:
        return []
    if isinstance(c, str):
        return [tuple(int(x) for x in m.split(","))
                for m in re.findall(r"\(([^)]*)\)", c)
                if len(m.split(",")) == 3]
    out = []
    for item in c:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            out.append(tuple(int(x) for x in item[:3]))
    return out


def additive_shaded_quads(prims):
    """The primitive class the wedges are made of.

    Matched by properties rather than by op name, because the ring dump and
    the RAM walk spell the name differently ('PolyG4+semi' vs whatever the
    walker produced) and a class that silently matches nothing would report
    a clean signature for a broken frame.
    """
    out = []
    for p in prims:
        name = p.get("op_name") or p.get("op") or ""
        if "G4" not in name:
            continue
        blend = p.get("blend") or blend_of(p)
        if blend not in ("B+F",):
            continue
        if len(parse_verts(p.get("verts"))) < 3:
            continue
        out.append(p)
    return out


def signature(prims):
    """Phase-robust description of what the effect's quads look like."""
    quads = additive_shaded_quads(prims)
    colours = collections.Counter()
    ys = []
    for q in quads:
        for c in parse_colors(q.get("colors")):
            colours[c] += 1
        for v in parse_verts(q.get("verts")):
            ys.append(v[1])
    sat = [c for c in colours if max(c) - min(c) > SATURATION]
    peak = max((max(c) for c in colours), default=0)
    return {
        "quads": len(quads),
        "distinct_colours": len(colours),
        "saturated_colours": len(sat),
        "peak_channel": peak,
        "y_span": (max(ys) - min(ys)) if ys else 0,
        "top_colours": [list(c) for c, _ in colours.most_common(6)],
        # The full set, not just the top few: the union across samples is the
        # only phase-free way to compare two emulators sweeping the same fade.
        # At any one instant they are at different points of the ramp, so the
        # sets differ for reasons that are not the bug -- but over the WHOLE
        # effect both traverse the same scales over the same sources, so a
        # colour one side never produces at any point is a real difference.
        "all_colours": sorted(list(c) for c in colours),
    }


def _wanted(sig, spec):
    """Does this sample show the object we are after?

    Without this, a quota fills up on whatever is on screen first -- the
    land-placement glow is 144 quads over 155 lines and is always available,
    so twelve samples of it would be collected and compared while the effect
    itself was never seen.
    """
    if not spec or spec == "any":
        return True
    q, _, ln = spec.partition("x")
    return sig["quads"] == int(q) and sig["y_span"] == int(ln)


def group_key(sig):
    """Which on-screen object a sample is of.

    Quad count and vertical span identify the object: the effect draws 64
    quads across 599 lines, the land-placement glow draws 144 across 155.
    Mixing them is not a detail -- taking maxima over both compared
    psx-runtime's EFFECT against the oracle's PLACEMENT SCREEN, which is a
    comparison of two different things that happens to produce a number.
    """
    return (sig["quads"], sig["y_span"])


def merge(sigs):
    """Combine per-sample signatures by taking maxima, per object.

    Maxima, not means: a sample that caught the effect mid-build has fewer
    quads than one that caught it whole, and averaging those understates the
    frame that actually matters. This is the same reason class_census tracks
    maxima. Grouping first keeps that from averaging across objects.
    """
    live = [s for s in sigs if s["quads"] > 0]
    groups = {}
    for sig in live:
        k = group_key(sig)
        g = groups.setdefault(k, {"quads": k[0], "y_span": k[1],
                                  "distinct_colours": 0,
                                  "saturated_colours": 0, "samples": 0,
                                  "peak_max": 0, "peak_min": None})
        g["distinct_colours"] = max(g["distinct_colours"],
                                    sig["distinct_colours"])
        g["saturated_colours"] = max(g["saturated_colours"],
                                     sig["saturated_colours"])
        peak = sig.get("peak_channel", 0)
        g["peak_max"] = max(g["peak_max"], peak)
        # The DIMMEST sample is the load-bearing one: the effect is a fade, so
        # the question is not how bright it gets but whether it ever goes out.
        g["peak_min"] = peak if g["peak_min"] is None else min(g["peak_min"],
                                                               peak)
        g["samples"] += 1
        g.setdefault("union", set()).update(
            tuple(c) for c in sig.get("all_colours", []))
    for g in groups.values():
        g["union_size"] = len(g.get("union", ()))
        g["union_list"] = sorted(list(c) for c in g.get("union", ()))
    return {"groups": groups, "samples": len(sigs),
            "samples_with_quads": len(live)}


def union_compare(nat, orc, key):
    """Colours each side produced at ANY point, and what only one side has."""
    a = nat["groups"].get(key, {}).get("union", set())
    b = orc["groups"].get(key, {}).get("union", set())
    return {"native_only": sorted(a - b), "oracle_only": sorted(b - a),
            "shared": sorted(a & b), "native_total": len(a),
            "oracle_total": len(b)}


def common_groups(nat, orc):
    """Objects both sides actually saw, largest first.

    Only these can be compared. An object one side never sampled is a gap in
    the evidence, not a difference between the emulators.
    """
    shared = set(nat["groups"]) & set(orc["groups"])
    return sorted(shared, key=lambda k: -(k[0] * max(k[1], 1)))


def verdict(nat, orc, colour_ratio=4.0):
    """Compare the two signatures, object by object.

    Ratios, not absolute thresholds: what matters is whether one side builds
    an order of magnitude more colour variety than the other, which is
    scale-free and does not need calibrating against a frame nobody has
    captured.
    """
    if not nat["samples_with_quads"]:
        return ("no-native-samples",
                "psx-runtime's ring held no additive shaded quads -- the "
                "effect did not play inside the scanned window.", None)
    if not orc["samples_with_quads"]:
        return ("no-oracle-samples",
                "no oracle read caught the effect, so nothing is compared. "
                "This is not evidence that its list is clean.", None)
    # The biggest object either side saw is the effect. If only ONE side
    # sampled it, there is nothing to compare -- falling back to a smaller
    # object and reporting agreement from it is how this tool once answered
    # "signatures-agree" using the placement glow while the effect object was
    # missing from psx-runtime entirely.
    everything = set(nat["groups"]) | set(orc["groups"])
    if everything:
        biggest = max(everything, key=lambda k: k[0] * max(k[1], 1))
        if biggest not in nat["groups"] or biggest not in orc["groups"]:
            missing = "psx-runtime" if biggest not in nat["groups"] else "the oracle"
            return ("effect-object-one-sided",
                    f"the effect object ({biggest[0]} quads spanning "
                    f"{biggest[1]} lines) was sampled only on "
                    f"{'the oracle' if missing == 'psx-runtime' else 'psx-runtime'}"
                    f"; {missing} never saw it, so there is nothing to compare. "
                    f"Replay the effect on BOTH emulators while this runs. "
                    f"Smaller objects present on both sides are NOT a "
                    f"substitute -- they are a different thing.", None)
    shared = common_groups(nat, orc)
    if not shared:
        return ("no-common-object",
                "the two sides never sampled the same object: psx-runtime saw "
                + ", ".join(f"{q} quads/{s} lines" for q, s in nat["groups"])
                + "; the oracle saw "
                + ", ".join(f"{q} quads/{s} lines" for q, s in orc["groups"])
                + ". Replay the effect on BOTH and sample again.", None)
    for k in shared:
        n, o = nat["groups"][k], orc["groups"][k]
        dn, do = n["distinct_colours"], o["distinct_colours"]
        if dn / max(do, 1) >= colour_ratio:
            extra = ""
            if n["peak_min"] is not None and o["peak_min"] is not None:
                extra = (f" Across samples psx-runtime's dimmest frame of this "
                         f"object still peaks at {n['peak_min']} while the "
                         f"oracle's reaches {o['peak_min']}: the fade never "
                         f"goes out here, which is what leaves the mesh "
                         f"visible as hard-edged quads."
                         if n["peak_min"] > 4 * max(o["peak_min"], 1) else "")
            return ("native-builds-different-geometry",
                    f"on the same object ({k[0]} quads spanning {k[1]} lines), "
                    f"psx-runtime builds {dn} distinct vertex colours against "
                    f"the oracle's {do}. The display list already differs, so "
                    f"the fault is upstream of the renderer -- in the code "
                    f"that computes this effect's colours." + extra, k)
        if do / max(dn, 1) >= colour_ratio:
            return ("oracle-builds-more",
                    f"on {k[0]} quads/{k[1]} lines the ORACLE builds {do} "
                    f"distinct colours against psx-runtime's {dn} -- the "
                    f"reverse of the wedge symptom. Treat the sampling as "
                    f"suspect before concluding anything.", k)
    k = shared[0]
    return ("signatures-agree",
            f"on {k[0]} quads/{k[1]} lines both sides build the same colour "
            f"variety ({nat['groups'][k]['distinct_colours']} vs "
            f"{orc['groups'][k]['distinct_colours']}). The lists agree, so the "
            f"wedges are produced when this list is RASTERISED.", k)


def sample_native(conn, args, out=sys.stderr):
    """Signatures from the GP0 ring: retrospective, nothing paused."""
    span = conn.ring_span()
    back = span["oldest"] if not args.ring_frames else max(
        span["oldest"], span["newest"] - args.ring_frames)
    frames = list(range(span["newest"], back - 1, -args.stride))
    sigs = []
    for fr in frames:
        try:
            d = capture(conn, frame=fr, count=args.count, label="native")
        except DebugError:
            continue
        s = signature(d.get("prims", []))
        if s["quads"] and _wanted(s, args.object):
            sigs.append(s)
        if len(sigs) >= args.samples:
            break
    print(f"  psx-runtime: {len(sigs)} ring frames carried additive shaded "
          f"quads", file=out)
    return sigs


def sample_oracle(conn, args, out=sys.stderr):
    """Signatures from RAM walks of a RUNNING oracle. Nothing is paused.

    DuckStation must NOT be paused for this, and every attempt to park it
    here has failed the same way. A paused DuckStation serves its debug
    socket from a Qt idle timer at about 1 Hz, so a walk that needs many
    round trips cannot finish; and a pc_break that re-fires on its next hit
    re-pauses the moment anything resumes it, which wedges the emulator for
    the user as well as for the tool.

    None of it was necessary. The effect lasts seconds and the game rebuilds
    its ordering table every frame, so reading the list repeatedly while it
    runs catches the effect the same way psx-runtime's ring does -- by
    sampling often, not by freezing time.
    """
    # Undo any wedging an earlier run left behind, before anything else.
    oracle_resume(conn)
    try:
        n = oracle_clear_breaks(conn)
        if n:
            print(f"  cleared {n} stale breakpoint(s) left by an earlier run",
                  file=out)
    except DebugError as e:
        print(f"  WARNING: {e}", file=out)
    oracle_resume(conn)

    sigs = []
    empty = 0
    torn = 0
    truncated = 0
    other = collections.Counter()
    ram_span = None
    if args.ram_span and args.ram_span != "all":
        a, _, b = args.ram_span.partition(":")
        ram_span = (int(a, 0), int(b, 0))
        kb = (ram_span[1] - ram_span[0]) // 1024
        print(f"  reading only 0x{ram_span[0]:06X}..0x{ram_span[1]:06X} "
              f"({kb} KB) per sample instead of 2 MB", file=out)
    root = None          # once known, read only the span around it
    expect_nodes = 0     # what a FULL walk found; a windowed one must match
    seen_classes = collections.Counter()
    deadline = time.monotonic() + args.watch_secs
    while time.monotonic() < deadline and len(sigs) < args.samples:
        try:
            # Windowing is off by default and unsafe in general: an
            # ordering table chains to primitives anywhere in RAM, and this
            # game's span 1.5 MB. Only honour it if explicitly asked for.
            use_window = args.window if (root and args.window) else None
            with open(os.devnull, "w") as quiet:
                rep, meta = walk_side(conn, "oracle", pause=False,
                                      addr=root, window=use_window,
                                      ram_span=ram_span,
                                      park_for_reread=False,
                                      max_nodes=args.max_nodes, out=quiet)
                # A window that does not reach the whole list truncates the
                # walk, and it truncates BOTH reads identically -- so the
                # coherence check passes and the missing primitives look like
                # primitives the game never drew. Re-read in full when the
                # node count drops off, and stop trusting the cached root.
                #
                # ONLY when windowing is in use. A full read whose node count
                # dips is not truncated, it is a frame in which the game drew
                # fewer primitives -- which during an animation is normal and
                # frequent. Re-reading those wastes the read and reports them
                # as short, and the count read as "frames being discarded".
                if use_window and rep and expect_nodes and \
                        rep.get("nodes", 0) < expect_nodes * 0.9:
                    truncated += 1
                    root = None
                    rep, meta = walk_side(conn, "oracle", pause=False,
                                          park_for_reread=False,
                                          max_nodes=args.max_nodes, out=quiet)
        except DebugError as e:
            print(f"  oracle read failed: {e}", file=out)
            time.sleep(args.poll)
            continue
        if rep:
            nodes = rep.get("nodes", 0)
            if root is None:
                # This walk read all of RAM, so its node count is the honest
                # size of the list to hold later windowed walks against.
                expect_nodes = max(expect_nodes, nodes)
            for c in rep.get("classes") or []:
                seen_classes[c["key"]] = max(seen_classes[c["key"]],
                                             c["count"])
            # Remember where the list lives; the next read is a few KB rather
            # than 2 MB, which is what stops the oracle stuttering.
            root = rep.get("root") or root
            if not meta.get("coherent"):
                # A read taken while the game rebuilt the list shows up as a
                # frame of quads carrying a handful of near-identical colour
                # values -- which is exactly the finding under test. Torn
                # samples must never reach the comparison.
                torn += 1
                time.sleep(args.poll)
                continue
            sig = signature(rep.get("prims") or [])
            if sig["quads"] and _wanted(sig, args.object):
                sigs.append(sig)
                print(f"  oracle sample {len(sigs)}/{args.samples}: "
                      f"{sig['quads']} quads, {sig['distinct_colours']} "
                      f"colours, {sig['y_span']} line span", file=out)
            elif sig["quads"]:
                other[(sig["quads"], sig["y_span"])] += 1
            else:
                empty += 1
        time.sleep(args.poll)

    if torn:
        print(f"  oracle: discarded {torn} torn read(s) taken while the game "
              f"rebuilt the list", file=out)
    if truncated:
        print(f"  oracle: {truncated} windowed read(s) came back short and "
              f"were re-read in full", file=out)
    if other:
        seen = ", ".join(f"{q}q/{s_}ln x{n}" for (q, s_), n in
                         other.most_common(4))
        print(f"  oracle: skipped samples of other objects ({seen}) -- they "
              f"do not fill the quota for --object {args.object}", file=out)
    if not sigs:
        # Say WHAT was walked. "None held additive shaded quads" is equally
        # consistent with the effect not playing and with the walk reading the
        # wrong thing, and those need different responses from the user.
        print(f"  oracle: {empty} walks read, none held additive shaded "
              f"quads.", file=out)
        if seen_classes:
            top = ", ".join(f"{k} x{v}" for k, v in
                            seen_classes.most_common(8))
            print(f"  oracle: the lists it DID walk contained: {top}",
                  file=out)
            print(f"  oracle: if that looks like the game's normal scene, the "
                  f"effect was not playing on DuckStation while this ran; if "
                  f"it looks sparse or wrong, the walk is reading the wrong "
                  f"buffer.", file=out)
        else:
            print(f"  oracle: no list was walked at all -- nothing was read "
                  f"to look at.", file=out)
    return sigs


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--ds-port", type=int, default=DEFAULT_DUCKSTATION_PORT)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--object", default="64x599", metavar="QUADSxLINES",
                    help="the effect object samples must match to count, as "
                         "quad-count x vertical-span. Samples of anything else "
                         "(the land-placement glow is 144x155) do not fill the "
                         "quota and never drive the verdict. 'any' disables.")
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--watch-secs", type=float, default=90.0,
                    help="how long to keep reading the running oracle")
    ap.add_argument("--ram-span", default="0x00100000:0x00140000",
                    metavar="LO:HI",
                    help="read only this RAM region per oracle sample. The "
                         "packet buffers (~0x0010Dxxx / 0x00115xxx) and the "
                         "ordering tables (~0x129xxx / 0x131xxx) both sit "
                         "inside the default, so the effect's quads are all "
                         "there -- and it is ~16 reads rather than 128, which "
                         "is the difference between a handful of samples per "
                         "animation and dozens. 'all' reads the full 2 MB.")
    ap.add_argument("--window", type=lambda v: int(v, 0), default=0,
                    help="bytes of RAM to re-read per oracle sample once the "
                         "list is located. DEFAULT 0 (always read all 2 MB): "
                         "this game's list reaches primitives from 0x0363B0 "
                         "to 0x1B23E0, so no fixed window around the root "
                         "spans it, and a truncated walk looks exactly like a "
                         "list with nothing in it")
    ap.add_argument("--poll", type=float, default=0.4,
                    help="seconds between oracle reads")
    ap.add_argument("--ring-frames", type=int, default=600,
                    help="how far back through psx-runtime's ring to look")
    ap.add_argument("--stride", type=int, default=4)
    ap.add_argument("--count", type=int, default=20000)
    ap.add_argument("--max-nodes", type=int, default=8192)
    ap.add_argument("--out", default="analysis/frames/effect_palette.json")
    args = ap.parse_args()

    doc = {"kind": KIND, "version": 1}
    # The oracle is watched LIVE and psx-runtime is read retrospectively from
    # its GP0 ring, so the order matters: sampling the ring first captures
    # whatever happened BEFORE the user replayed anything. Watch the oracle
    # first, then read the ring -- by then the effect has been played and is
    # in it.
    print("reading the running oracle (nothing is paused) …", flush=True)
    orc_sigs = sample_oracle(DebugConn(args.host, args.ds_port, args.timeout),
                             args)
    print("sampling psx-runtime's GP0 ring (after the replay) …", flush=True)
    try:
        nat_sigs = sample_native(DebugConn(args.host, args.port, args.timeout),
                                 args)
    except DebugError as e:
        print(f"  psx-runtime unavailable: {e}", file=sys.stderr)
        nat_sigs = []
    if not nat_sigs:
        # One retry with a wider reach: the ring holds several hundred frames,
        # so a miss usually means the effect is further back than the default.
        print("  nothing yet; sweeping the whole ring …", flush=True)
        wide = argparse.Namespace(**vars(args))
        wide.ring_frames = 0
        wide.stride = max(1, args.stride // 2)
        try:
            nat_sigs = sample_native(
                DebugConn(args.host, args.port, args.timeout), wide)
        except DebugError as e:
            print(f"  psx-runtime unavailable: {e}", file=sys.stderr)
            nat_sigs = []

    nat, orc = merge(nat_sigs), merge(orc_sigs)
    def _plain(g):
        return {k: v for k, v in g.items() if k != "union"}
    doc["native"] = {f"{k[0]}x{k[1]}": _plain(v)
                     for k, v in nat["groups"].items()}
    doc["oracle"] = {f"{k[0]}x{k[1]}": _plain(v)
                     for k, v in orc["groups"].items()}
    doc["native_samples"], doc["oracle_samples"] = nat_sigs, orc_sigs
    v, why, which = verdict(nat, orc)
    doc["verdict"], doc["explanation"] = v, why
    if which:
        doc["compared_object"] = {"quads": which[0], "y_span": which[1]}
        u = union_compare(nat, orc, which)
        doc["union"] = {k: ([list(c) for c in v_] if isinstance(v_, list)
                            else v_) for k, v_ in u.items()}
        print(f"\nunion over all samples on {which[0]}q/{which[1]}ln:")
        print(f"  psx-runtime produced {u['native_total']} distinct colour(s) "
              f"at some point")
        print(f"  oracle produced      {u['oracle_total']}")
        print(f"  shared               {len(u['shared'])}")
        print(f"  ONLY psx-runtime     {len(u['native_only'])}  "
              f"{[list(c) for c in u['native_only'][:6]]}")
        print(f"  ONLY oracle          {len(u['oracle_only'])}  "
              f"{[list(c) for c in u['oracle_only'][:6]]}")
        print("\nBoth sweep the same fade over the same sources, so a colour "
              "only one side ever produces is a real difference; a colour "
              "merely absent from one SAMPLE is just phase.")

    print(f"\n{'object':>16}  {'side':<12}{'colours':>9}{'saturated':>11}"
          f"{'dimmest':>9}{'peak':>7}{'samples':>9}")
    seen = sorted(set(nat["groups"]) | set(orc["groups"]),
                  key=lambda k: -(k[0] * max(k[1], 1)))
    for k in seen:
        label = f"{k[0]}q/{k[1]}ln"
        for side, m in (("psx-runtime", nat), ("oracle", orc)):
            g = m["groups"].get(k)
            if not g:
                print(f"{label:>16}  {side:<12}{'-':>9}{'-':>11}{'-':>9}"
                      f"{'-':>7}{0:>9}")
            else:
                print(f"{label:>16}  {side:<12}{g['distinct_colours']:>9}"
                      f"{g['saturated_colours']:>11}{g['peak_min']:>9}"
                      f"{g['peak_max']:>7}{g['samples']:>9}")
    print(f"\nVERDICT: {v}\n{why}")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"\nreport: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
