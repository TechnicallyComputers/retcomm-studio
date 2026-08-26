#!/usr/bin/env python3
"""table_watch.py -- catch the moment the effect's colour table changes.

    python3 tools/table_watch.py            # then play: title -> scene -> effect

Where this fits
---------------
The effect's 64 additive quads read per-vertex source colours from a table
around 0x800E25F0..0x800E2C60. DuckStation's copy holds ~5 distinct colour
words and renders a clean bloom; psx-runtime's holds ~216 and renders the
wedges. Write-tracing that range never fires: CPU stores and every DMA
channel go through psx_write_word and would be visible, so whatever fills it
is one of the paths that bypass the traced write layer entirely -- above all
SAVESTATE and REWIND restores, which memcpy whole RAM images in. If the
corrupt table rides in via a state file, no writer exists to find: the
corruption happened in whatever session CREATED the state.

So do not hunt the writer; watch the DATA. This polls the table region a few
times a second while you play from as far back as you can start, and reports
every transition of its content fingerprint -- along with the distinct
colour-word count before and after, the frame number, and whether the write
trace (armed the whole time) saw CPU/DMA stores in the window. A transition
with trace entries names the writer; a transition with NONE is a wholesale
restore, and the question becomes what loaded the state, not what wrote the
bytes.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_NATIVE_PORT, DebugConn, DebugError, read_ram_range,
)

KIND = "psx-table-watch"


def distinct_colours(blob):
    out = set()
    for i in range(0, len(blob) - 3, 4):
        w = int.from_bytes(blob[i:i + 4], "little")
        out.add(w & 0xFFFFFF)
    return len(out)


def fingerprint(blob):
    return hashlib.sha1(blob).hexdigest()[:16]


def classify(n_writes):
    """What a transition with/without traced writes means."""
    if n_writes > 0:
        return ("written", "CPU or DMA stores were traced in the window -- "
                           "the writer is in the dump.")
    return ("restored", "NO stores were traced, yet the bytes changed: a "
                        "wholesale restore (savestate / rewind) replaced the "
                        "region. The corruption rides in the state file; the "
                        "writer to find is in the session that CREATED it.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--lo", default="0x800E25F0")
    ap.add_argument("--hi", default="0x800E2C60")
    ap.add_argument("--poll", type=float, default=0.4)
    ap.add_argument("--watch-secs", type=float, default=300.0)
    ap.add_argument("--out", default="analysis/frames/table_watch.json")
    args = ap.parse_args()

    lo, hi = int(args.lo, 0), int(args.hi, 0)
    length = ((hi - lo) & ~3) + 4
    conn = DebugConn(args.host, args.port, args.timeout)

    # Arm the trace for the whole watch so any traceable writer is captured
    # retroactively. The oracle has no wtrace; transitions still report, just
    # unattributed -- which is fine, since the oracle side only needs the
    # TIMELINE to compare against native's.
    have_trace = True
    try:
        conn.cmd("wtrace_reset")
        conn.cmd("wtrace_add", lo=f"0x{lo & 0x1FFFFFFF:08X}",
                 hi=f"0x{(lo & 0x1FFFFFFF) + length:08X}")
    except DebugError as e:
        have_trace = False
        print(f"note: write trace unavailable here ({e}); transitions will "
              f"be reported without attribution", file=sys.stderr)

    doc = {"kind": KIND, "version": 1, "lo": args.lo, "hi": args.hi,
           "port": args.port, "attributed": have_trace, "transitions": []}

    def flush():
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(doc, f, indent=1)

    last_fp = None
    last_n = None
    last_seq = 0
    print(f"watching 0x{lo:08X}..0x{hi:08X} — play from as far back as you "
          f"can (title screen onward); every content change is reported",
          flush=True)
    deadline = time.monotonic() + args.watch_secs
    try:
        while time.monotonic() < deadline:
            try:
                blob = read_ram_range(conn, lo, length)
            except DebugError as e:
                print(f"  read failed ({e}); retrying", flush=True)
                time.sleep(args.poll)
                continue
            try:
                frame = conn.frame()
            except DebugError:
                frame = -1
            fp = fingerprint(blob)
            n = distinct_colours(blob)
            if fp != last_fp:
                writes = []
                seq_hi = last_seq
                if have_trace:
                    try:
                        rep = conn.cmd(
                            "wtrace_dump",
                            addr_lo=f"0x{lo & 0x1FFFFFFF:08X}",
                            addr_hi=f"0x{(lo & 0x1FFFFFFF) + length:08X}",
                            count=2048, newest=1)
                        for e in rep.get("entries", []):
                            seq_hi = max(seq_hi, int(e.get("seq", 0)))
                            if int(e.get("seq", 0)) > last_seq:
                                writes.append({"pc": e.get("pc"),
                                               "dma_ch": e.get("dma_ch"),
                                               "frame": e.get("frame")})
                    except DebugError:
                        pass
                if last_fp is not None:
                    kind, meaning = classify(len(writes)) if have_trace \
                        else ("unattributed", "no write trace on this side")
                    by_pc = collections.Counter(
                        (w["pc"], w["dma_ch"]) for w in writes)
                    t = {"frame": frame, "from_colours": last_n,
                         "to_colours": n, "kind": kind,
                         "traced_writes": len(writes),
                         "writers": [{"pc": p, "dma_ch": d, "count": c}
                                     for (p, d), c in by_pc.most_common(8)]}
                    doc["transitions"].append(t)
                    flush()          # survive a Ctrl-C at any moment
                    print(f"  CHANGE at frame {frame}: {last_n} -> {n} "
                          f"distinct colours [{kind}: {len(writes)} traced "
                          f"write(s)]", flush=True)
                    for (p, d), c in by_pc.most_common(4):
                        tag = f" dma_ch={d}" if d is not None and d >= 0 else ""
                        print(f"      writer {p}{tag}  x{c}", flush=True)
                else:
                    doc["baseline"] = {"frame": frame, "colours": n,
                                       "fingerprint": fp}
                    flush()
                    print(f"  baseline at frame {frame}: {n} distinct "
                          f"colours, fingerprint {fp}", flush=True)
                last_fp, last_n, last_seq = fp, n, seq_hi
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nstopped", flush=True)

    doc["final_colours"] = last_n
    flush()
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
