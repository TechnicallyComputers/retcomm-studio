#!/usr/bin/env python3
"""gpu_display_list.py -- read a display list out of guest RAM and decode it.

    python3 gpu_display_list.py                            # scan RAM for lists
    python3 gpu_display_list.py --addr 0x0010D05C          # a known buffer
    python3 gpu_display_list.py --port 4371                # the oracle

Why this exists
---------------
A PSX game does not usually poke GP0 directly. It builds a linked list in RAM
and hands it to DMA channel 2, so the display list is a data structure sitting
in memory, and reading it is a different act from watching commands go by.

That matters for three things:

  * The oracle has no GP0 ring, so walking RAM is the ONLY way to see what
    DuckStation was about to draw.
  * For decompilation, seeing what a routine builds tells you what the routine
    IS. Pair this with a write-trace ("code at 0x80061364 fills this buffer")
    and the function names itself -- which works even for overlay code the
    static analyser never sees.
  * For modding, finding the display list that draws a thing is the
    prerequisite for changing it.

The list is walked from a snapshot of RAM rather than node by node: a display
list runs to hundreds of nodes, and a round trip each would be absurd.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import threading
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_DUCKSTATION_PORT, DEFAULT_NATIVE_PORT, STP_MODES, DebugConn,
    DebugError, decode_entries, dma_gpu_list_root, find_display_lists,
    read_ram_range, snapshot_ram, snapshot_ram_window, walk_ordering_table,
)

LIST_KIND = "psx-display-list"


DRAWING = ("poly", "rect", "line", "fill")


def blend_of(p):
    return STP_MODES.get(p.get("stp", 0), "?") if p.get("semi") else "opaque"


def cmax(p):
    """Brightest channel over a primitive's vertices.

    The useful number for an additive effect: what the game asked the blender
    to add. Comparing it across emulators separates "built different colours"
    from "blended the same colours differently".
    """
    return max((v for c in (p.get("colors") or []) for v in c), default=0)


def report(prims, root, port):
    """The same summary the CLI prints, as data. Studio reads this."""
    drawing = [p for p in prims if p["kind"] in DRAWING]
    classes = Counter(f"{p['op_name']}|{blend_of(p)}" for p in drawing)
    return {
        "kind": LIST_KIND, "version": 1,
        "root": f"0x{root:08X}", "port": port,
        "nodes": len(prims), "drawing": len(drawing),
        "classes": [{"key": k, "count": n} for k, n in classes.most_common()],
        "prims": [{
            "op": p["op_name"],
            "blend": blend_of(p),
            "src": p.get("src", ""),
            "cmax": cmax(p),
            "verts": " ".join(f"({x},{y})" for x, y in (p.get("verts") or [])),
            "colors": " ".join(str(tuple(c)) for c in (p.get("colors") or [])),
        } for p in drawing],
    }


def summarise(prims, out=sys.stdout, limit=20):
    drawing = [p for p in prims if p["kind"] in DRAWING]
    print(f"{len(prims)} node(s), {len(drawing)} drawing", file=out)
    if not drawing:
        return
    classes = Counter(f"{p['op_name']}|{blend_of(p)}" for p in drawing)
    print("\nclasses:", file=out)
    for k, n in classes.most_common():
        print(f"  {k:<26} {n:>5}", file=out)

    print(f"\nfirst {min(limit, len(drawing))} primitives:", file=out)
    print(f"  {'#':>4}  {'opcode':<16} {'blend':<10} {'src':<12} vertices / colours",
          file=out)
    for i, p in enumerate(drawing[:limit]):
        mode = blend_of(p)
        verts = " ".join(f"({x},{y})" for x, y in (p.get("verts") or [])[:4])
        cols = " ".join(str(tuple(c)) for c in (p.get("colors") or [])[:4])
        print(f"  {i:>4}  {p['op_name']:<16} {mode:<10} {p['src']:<12} {verts}",
              file=out)
        if cols:
            print(f"  {'':>4}  {'':<16} {'':<10} {'':<12} {cols}", file=out)


def _list_shape(entries):
    """The structure of a walked list, ignoring the data inside it.

    Two reads taken a moment apart during an animation legitimately differ in
    vertex colours and positions; they must not differ in how many packets
    there are or in each packet's opcode and length. That is what separates
    "the game is drawing" from "we read while it was being rebuilt".
    """
    return [(e.get("op"), e.get("n_words")) for e in entries]


def walk_side(conn, label, *, addr=None, near=None, pause=False,
              from_dma=False, max_nodes=8192, candidates=6, window=None,
              ram_span=None, park_for_reread=True, out=sys.stdout):
    """Snapshot one emulator and walk its display list.

    Returns (report, meta) or (None, meta) if nothing walkable was found.
    `meta` always carries the frame numbers seen either side of the
    snapshot, because a read of a RUNNING emulator takes long enough for
    the game to rebuild the list underneath it -- and a walk that
    straddled a rebuild should be visibly suspect rather than quietly
    reported as fact.
    """
    meta = {"label": label, "paused": False,
            "frame_before": None, "frame_after": None}
    paused = False
    try:
        # Pause FIRST, then read the root, then snapshot: the root and the
        # list it names have to come from the same instant.
        if pause:
            try:
                conn.cmd("pause")
                paused = meta["paused"] = True
            except DebugError as e:
                print(f"  [{label}] could not pause: {e}", file=out)
        try:
            meta["frame_before"] = conn.frame()
        except DebugError:
            pass

        root = int(addr, 0) if isinstance(addr, str) else addr
        if root is None and from_dma:
            root = dma_gpu_list_root(conn)
            if root is None:
                print(f"  [{label}] DMA ch2 MADR holds the end-of-list "
                      f"terminator, not a root. Scanning instead.", file=out)

        # With the root already known, read only the span around it. The full
        # 2 MB snapshot is ~128 round trips, and against the oracle those run
        # on the emulator thread -- repeated sampling makes it look frozen.
        if ram_span:
            # An explicit region, not a root-relative window. The ordering
            # table and the packet buffers sit in known, separate places
            # (packets ~0x0010Dxxx / 0x00115xxx, tables ~0x129xxx / 0x131xxx),
            # so one span covering both reads ~16 chunks instead of 128.
            # Primitives outside it are simply not seen -- fine when the
            # caller filters to a class that lives inside it, and the reason
            # this is opt-in rather than a default.
            rl, rh = ram_span
            ram = snapshot_ram_window(conn, rl & 0x1FFFFF,
                                      (rh - rl) & 0x1FFFFF)
        elif root is not None and window:
            ram = snapshot_ram_window(conn, max(0, (root & 0x1FFFFF) - 0x2000),
                                      window)
        else:
            ram = snapshot_ram(conn)
        try:
            meta["frame_after"] = conn.frame()
        except DebugError:
            pass
    except DebugError as e:
        meta["error"] = str(e)
        return None, meta
    finally:
        if paused:
            try:
                conn.cmd("continue")
            except DebugError:
                pass

    entries = walk_ordering_table(ram, root, max_nodes=max_nodes) if root else []
    if not entries:
        cands = find_display_lists(ram, near=near, limit=candidates)
        if not cands:
            meta["error"] = ("no display list found — if the game is on a "
                             "menu or load screen it may have none")
            return None, meta
        meta["candidates"] = [
            {"root": f"0x{c['root']:06X}", "nodes": c["nodes"],
             "prims": c["prims"]} for c in cands]
        root = cands[0]["root"]
        entries = walk_ordering_table(ram, root, max_nodes=max_nodes)

    # ---- coherent re-read ------------------------------------------------
    # The first pass found WHERE the list is; it may not be a coherent picture
    # of it. A 2 MB snapshot of a running emulator takes ~128 reads, during
    # which the game rebuilds the list underneath -- which is how the oracle
    # came back with a whole frame of additive quads carrying just 4 distinct
    # colour values, a torn read that looks like data.
    #
    # Now that the span is known, the re-read is a few KB, so it is affordable
    # even at the 1 Hz the oracle drops to while parked. Park, re-read just the
    # span, resume: a snapshot from ONE instant.
    if ram_span and entries:
        # An explicitly scoped read is already coherent enough: 256 KB is ~16
        # chunks, comparable to a single frame, so the snapshot IS the
        # picture. Re-reading to check it is what destroys it -- three
        # different second-read tests (byte identity, list shape, node count)
        # each rejected every frame in which the list changed, and during an
        # animation that is every frame. The last one discarded 147 of 147.
        meta["coherent"] = True
        meta["coherence"] = "scoped-single-read"
    elif not meta.get("paused") and entries:
        addrs = [int(e["src"], 16) & 0x1FFFFF for e in entries]
        lo, hi = min(min(addrs), root & 0x1FFFFF), max(addrs)
        span_lo = lo & ~3
        span_len = (hi - span_lo) + 0x400
        coherent = False

        def _accept(blob):
            buf = bytearray(len(ram))
            buf[span_lo:span_lo + len(blob)] = blob
            again = walk_ordering_table(bytes(buf), root, max_nodes=max_nodes)
            return again if len(again) >= len(entries) * 0.9 else None

        if park_for_reread:
            try:
                conn.cmd("pause")
                try:
                    f0 = conn.frame()
                    got = _accept(read_ram_range(conn, 0x80000000 + span_lo,
                                                 span_len))
                    if got is not None:
                        entries, coherent = got, True
                        meta["coherent_frame"] = f0
                finally:
                    conn.cmd("continue")
            except DebugError as e:
                print(f"  [{label}] coherent re-read failed ({e}); using the "
                      f"running snapshot", file=out)
        else:
            # Read once and let the WALK validate itself. No pausing, and no
            # comparison against a second read.
            #
            # Two attempts at a two-read check both failed the same way, and
            # the failure is worth recording. Byte-identity rejected every
            # frame in which any colour changed; comparing the list SHAPE
            # rejected every frame in which the list changed SIZE. During an
            # animation that is all of them -- so both filters kept only the
            # frames WITHOUT the animation and reported the rest as
            # corruption, which is the exact opposite of the job.
            #
            # A genuinely torn read is malformed, not merely different: packet
            # word counts stop matching their opcodes. walk_ordering_table
            # already refuses those entries, so the walk succeeding IS the
            # coherence check, and it costs one read instead of two.
            try:
                first = read_ram_range(conn, 0x80000000 + span_lo, span_len)
                walked = _accept(first)
                if walked is not None:
                    # Trust the walk's OWN validation rather than comparing
                    # two reads. walk_ordering_table only admits entries whose
                    # word count matches their opcode, so torn data drops out
                    # there; requiring two reads to agree on the whole list
                    # shape rejected every frame in which the list CHANGED
                    # SIZE -- which during an animation is all of them. That
                    # is the same mistake as requiring byte-identity, one step
                    # weaker: it discarded precisely the frames being looked
                    # for, and reported them as corruption.
                    entries, coherent = walked, True
                else:
                    meta["torn"] = True
            except DebugError as e:
                print(f"  [{label}] verify re-read failed ({e}); using the "
                      f"running snapshot", file=out)
        meta["coherent"] = coherent
    else:
        meta["coherent"] = bool(meta.get("paused"))

    rep = report(decode_entries(entries), root, conn.port)
    rep["meta"] = meta
    return rep, meta


def frames_spanned(meta):
    a, b = meta.get("frame_before"), meta.get("frame_after")
    if a is None or b is None:
        return None
    return b - a


def colour_shape(prims, op=None, blend=None):
    """How much STRUCTURE a class's vertex colours have.

    A procedurally generated effect reuses a handful of colours across all
    its primitives -- a two-tone additive glow is literally two or three
    distinct values repeated. So "how many distinct colours" is a shape
    measurement, and it survives the thing that defeats every other colour
    comparison: it does not need the two emulators to be on the same frame.
    An effect at a different phase still has few distinct colours; an
    effect whose colours are being computed wrongly does not.
    """
    sel = [p for p in prims
           if (op is None or p["op"] == op)
           and (blend is None or p["blend"] == blend)]
    tuples, patterns = [], set()
    for p in sel:
        vals = [int(x) for x in re.findall(r"\d+", p.get("colors", ""))]
        quad = [tuple(vals[i:i + 3]) for i in range(0, len(vals), 3)]
        tuples += quad
        patterns.add(tuple(quad))
    return {"prims": len(sel), "vertex_colours": len(tuples),
            "distinct": len(set(tuples)), "patterns": len(patterns),
            "top": Counter(tuples).most_common(3)}


def compare_colours(nat, orc, key, out=sys.stdout):
    """Report colour structure for one class on both sides."""
    op, _, blend = key.partition("|")
    a = colour_shape(nat["prims"], op, blend)
    b = colour_shape(orc["prims"], op, blend)
    if not a["prims"] or not b["prims"]:
        return None
    print(f"\ncolour structure of {key}:", file=out)
    for lbl, d in (("psx-runtime", a), ("oracle", b)):
        print(f"  {lbl:<12} {d['prims']:>4} prims  "
              f"{d['distinct']:>4} distinct colours  "
              f"{d['patterns']:>4} distinct per-prim patterns", file=out)
        print(f"               most common: "
              + "  ".join(f"{t} x{k}" for t, k in d["top"]), file=out)
    # An order-of-magnitude gap in distinct-colour count is not phase.
    if a["distinct"] and b["distinct"]:
        r = max(a["distinct"], b["distinct"]) / min(a["distinct"], b["distinct"])
        if r >= 4.0:
            worse = "psx-runtime" if a["distinct"] > b["distinct"] else "oracle"
            print(f"\n  FINDING: {worse} produces {r:.0f}x more distinct "
                  f"colours for the same {a['prims']} primitives. A "
                  f"procedural effect reuses a few colours; this side is "
                  f"not reusing them. That is a colour-COMPUTATION "
                  f"divergence upstream of the renderer, not a phase "
                  f"difference — phase changes which colours, not how "
                  f"many.", file=out)
    return {"native": a, "oracle": b}


def compare(nat, orc, out=sys.stdout):
    """Class-count delta between two walks."""
    an = {c["key"]: c["count"] for c in nat["classes"]}
    on = {c["key"]: c["count"] for c in orc["classes"]}
    keys = list(an) + [k for k in on if k not in an]
    print(f"\n{'class':<28}{'runtime':>9}{'oracle':>9}{'delta':>9}", file=out)
    for k in keys:
        a, b = an.get(k, 0), on.get(k, 0)
        mark = "" if a == b else ("  <-- only one side" if not a or not b else "")
        print(f"  {k:<26}{a:>9}{b:>9}{b - a:>+9}{mark}", file=out)
    # Vertices first: if those differ the colour question is premature.
    # Tolerate a report without per-primitive detail: an older file, or one
    # written before `prims` was recorded, should still give a class delta
    # rather than raise.
    same_verts = 0
    pairs = 0
    for a, b in zip(nat.get("prims") or [], orc.get("prims") or []):
        if a["op"] == b["op"] and a["blend"] == b["blend"]:
            pairs += 1
            if a.get("verts") == b.get("verts"):
                same_verts += 1
    if pairs:
        print(f"\ngeometry: {same_verts}/{pairs} paired primitives have "
              f"identical vertices", file=out)

    shapes = {}
    for k in keys:
        if an.get(k) and on.get(k) and nat.get("prims") and orc.get("prims"):
            r = compare_colours(nat, orc, k, out=out)
            if r:
                shapes[k] = r
    return {"classes": [{"key": k, "native": an.get(k, 0),
                         "oracle": on.get(k, 0)} for k in keys],
            "geometry_identical": same_verts, "geometry_pairs": pairs,
            "colour_shape": shapes}


def run_both(args):
    """Walk both emulators at once and compare.

    Each side runs in its own thread with its own connection. DebugConn
    opens a fresh socket per command and keeps no shared state, so the two
    do not interfere.

    The two sides are NOT treated symmetrically, and that is deliberate:
    psx-runtime is parked for its snapshot (its park loop keeps serving
    commands, so this is safe) while the oracle is read running, because
    pausing DuckStation drops its debug socket to the Qt idle timer -- 1 Hz
    with no gamepad attached -- and the read cannot finish.
    """
    out_dir = args.out_dir or "."
    os.makedirs(out_dir, exist_ok=True)
    near = int(args.near, 0) if args.near else None
    results = {}
    lock = threading.Lock()

    def go(label, port, pause):
        buf = io.StringIO()
        conn = DebugConn(args.host, port, args.timeout)
        rep, meta = walk_side(conn, label, addr=args.addr, near=near,
                              pause=pause, from_dma=args.from_dma,
                              max_nodes=args.max_nodes,
                              candidates=args.candidates, out=buf)
        with lock:
            results[label] = (rep, meta, buf.getvalue())

    threads = [
        threading.Thread(target=go, args=("native", args.port, args.pause)),
        threading.Thread(target=go, args=("oracle", args.ds_port, False)),
    ]
    print("walking both emulators concurrently …")
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rc = 0
    wrote = []
    for label, fname in (("native", "dlist-native.json"),
                         ("oracle", "dlist-oracle.json")):
        rep, meta, log = results.get(label, (None, {}, ""))
        if log:
            sys.stdout.write(log)
        if rep is None:
            print(f"  {label}: {meta.get('error', 'failed')}", file=sys.stderr)
            rc = 1
            continue
        span = frames_spanned(meta)
        if meta.get("coherent"):
            note = (" [coherent]" if meta.get("paused") else
                    f" [coherent, re-read parked at frame "
                    f"{meta.get('coherent_frame')}]")
        else:
            note = (f", advanced {span} frame(s) during the read — THIS WALK MAY "
                    f"BE TORN" if span else "")
        print(f"  {label}: root {rep['root']}, {rep['nodes']} node(s), "
              f"{rep['drawing']} drawing{note}")
        if span and span > 2 and not meta.get("paused"):
            print(f"    warning: {label} advanced {span} frames while being "
                  f"read, so this walk may straddle a rebuild of the list.",
                  file=sys.stderr)
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1)
        wrote.append(fname)

    nat = results.get("native", (None,))[0]
    orc = results.get("oracle", (None,))[0]
    if nat and orc:
        compare(nat, orc)
        print("\nA delta only means something if both sides are at the same "
              "point in the animation — an effect mid-cycle differs from "
              "itself frame to frame.")
    # Only claim what actually happened. Saying "wrote both" after a failed
    # side leaves a stale file from an earlier run looking like fresh output.
    if wrote:
        print(f"\nwrote {', '.join(wrote)} to {out_dir}")
    else:
        print("\nnothing written — neither side could be walked", file=sys.stderr)
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--addr", default=None, help="root of the list (hex ok)")
    ap.add_argument("--near", default=None,
                    help="prefer a chain covering this address (e.g. a src "
                         "address the GP0 ring reported)")
    ap.add_argument("--from-dma", action="store_true",
                    help="take the root from DMA ch2 MADR (usually wrong — a "
                         "finished transfer leaves the terminator there)")
    ap.add_argument("--candidates", type=int, default=6,
                    help="how many scanned chains to list")
    ap.add_argument("--max-nodes", type=int, default=8192)
    ap.add_argument("--limit", type=int, default=20, help="primitives to print")
    ap.add_argument("--json", default=None)
    ap.add_argument("--both", action="store_true",
                    help="walk psx-runtime AND the oracle concurrently, and "
                         "compare. Concurrent rather than sequential because "
                         "the two snapshots should land as close together in "
                         "time as possible — the effect is animating, and every "
                         "second between them is phase drift in the comparison.")
    ap.add_argument("--ds-port", type=int, default=DEFAULT_DUCKSTATION_PORT,
                    help="the oracle's port, for --both")
    ap.add_argument("--out-dir", default=None,
                    help="with --both, write dlist-native.json and "
                         "dlist-oracle.json here")
    ap.add_argument("--pause", action="store_true",
                    help="park the emulator around the snapshot. Safe against "
                         "psx-runtime, which keeps serving while parked. NOT "
                         "safe against the DuckStation oracle: pausing it drops "
                         "its socket to the idle poll timer (1 Hz with no "
                         "gamepad present), and a 2 MB read cannot finish.")
    args = ap.parse_args(argv)

    if args.both:
        return run_both(args)

    conn = DebugConn(args.host, args.port, args.timeout)
    paused = False
    try:
        # Pause FIRST, then read the root, then snapshot. The root and the list
        # it points at have to come from the same instant: a game rebuilds its
        # ordering table every frame, so a root read while running can name a
        # list that the snapshot no longer contains -- which walks into whatever
        # replaced it and reports a display list that never existed.
        if args.pause:
            try:
                conn.cmd("pause")
                paused = True
            except DebugError as e:
                print(f"  warning: could not pause: {e}", file=sys.stderr)

        root = int(args.addr, 0) if args.addr else None
        if root is None and args.from_dma:
            root = dma_gpu_list_root(conn)
            if root is None:
                print("  DMA ch2 MADR holds the end-of-list terminator, not a "
                      "root — the transfer already finished. Scanning instead.",
                      file=sys.stderr)
            else:
                print(f"DMA ch2 MADR -> 0x{root:08X}")

        print(f"snapshotting RAM from {args.host}:{args.port} …")
        ram = snapshot_ram(conn)
        print(f"  {len(ram)} bytes")
    except DebugError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        if paused:
            try:
                conn.cmd("continue")
            except DebugError:
                pass

    near = int(args.near, 0) if args.near else None
    entries = walk_ordering_table(ram, root, max_nodes=args.max_nodes) if root else []
    if root and not entries:
        print(f"nothing walkable from 0x{root:08X} — the tag there is not an "
              f"ordering-table node. Scanning for one instead.", file=sys.stderr)

    if not entries:
        print("scanning RAM for ordering-table chains …")
        cands = find_display_lists(ram, near=near, limit=args.candidates)
        if not cands:
            print("no display lists found. If the game is on a menu or a load "
                  "screen it may genuinely have none on screen — capture while "
                  "the effect is visible.", file=sys.stderr)
            return 1
        print(f"  {len(cands)} candidate chain(s), best first:")
        for c in cands:
            print(f"    0x{c['root']:06X}  {c['nodes']:>5} nodes  {c['prims']:>5} "
                  f"prims  src 0x{c['lo']:06X}..0x{c['hi']:06X}  "
                  f"{', '.join(c['classes'][:4])}")
        root = cands[0]["root"]
        print(f"  walking the best: 0x{root:06X}")
        entries = walk_ordering_table(ram, root, max_nodes=args.max_nodes)

    prims = decode_entries(entries)
    summarise(prims, limit=args.limit)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report(prims, root, args.port), f, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
