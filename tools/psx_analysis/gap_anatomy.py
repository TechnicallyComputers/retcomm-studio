#!/usr/bin/env python3
"""gap_anatomy.py -- split the 2.5M-cycle gap between sectors into its parts.

    python3 tools/gap_anatomy.py      # after playing the palette load

Measured: sectors the game individually Setlocs arrive ~2,500,000 guest cycles
(4.4 frames) after the previous one, while continuations arrive 225,792
(0.40 frames, the exact 2x cadence). due == buffer on every record, so the
drive is never late -- the READ IS ARMED late. DuckStation moves the same 14
sectors in 2 guest frames.

That gap has three possible tenants, needing three different fixes:

  1. guest silence   -- the sector arrived and the guest issued nothing for a
                        long time. A guest-side stall (or it never saw the
                        notification).
  2. queued command  -- the guest DID issue Setloc/ReadN promptly, but it sat
                        queued because try_execute_queued_command() only runs
                        when irq_flag == 0. Then the ack latency is the cost,
                        and that is ours, not the game's.
  3. read start      -- initial_read_delay_cycles(), 451584 at 2x.

The command history now carries a guest cycle stamp, so this prints, for each
individually-requested sector, how many cycles fell into each bucket.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import DEFAULT_NATIVE_PORT, DebugConn, DebugError  # noqa: E402

KIND = "psx-gap-anatomy"
CYCLES_PER_FRAME = 33868800 / 60.0
READ_START_2X = 451584


def bcd(v):
    return (v >> 4) * 10 + (v & 0x0F)


def setloc_cycles(entries):
    """LBA -> guest cycle at which its Setloc was recorded (latest wins)."""
    out = {}
    for e in entries:
        cmd = e.get("cmd")
        cmd = int(cmd, 16) if isinstance(cmd, str) else int(cmd or 0)
        if cmd != 0x02:
            continue
        ps = [int(x, 16) if isinstance(x, str) else int(x)
              for x in (e.get("params") or [])]
        if len(ps) < 3:
            continue
        m, s, f = (bcd(x) for x in ps[:3])
        out[(m * 60 + s) * 75 + f - 150] = int(e.get("cycle", 0))
    return out


def sector_cycles(entries):
    """LBA -> cycle the sector landed in the buffer (latest wins)."""
    out = {}
    for e in entries:
        lba = int(e.get("lba", -1))
        if lba >= 0:
            out[lba] = int(e.get("buffer", 0))
    return out


def anatomy(sectors, setlocs, lo, hi):
    """Per requested sector: silence / queue+start, from the previous sector."""
    rows = []
    for lba in sorted(sectors):
        if not (lo <= lba <= hi) or lba not in setlocs:
            continue
        prev = max((v for k, v in sectors.items()
                    if k < lba and sectors[k] < sectors[lba]), default=None)
        if prev is None:
            continue
        arrive = sectors[lba]
        issued = setlocs[lba]
        rows.append({
            "lba": lba,
            "gap": arrive - prev,
            "silence": max(0, issued - prev),
            "after_issue": max(0, arrive - issued),
        })
    return rows


STEADY_2X = 225792   # one sector period at double speed


def verdict_of(rows):
    if not rows:
        return ("no-data", "no individually-requested sector had both a "
                          "Setloc stamp and a delivery record.")
    tot = sum(r["gap"] for r in rows)
    sil = sum(r["silence"] for r in rows)
    aft = sum(r["after_issue"] for r in rows)
    n = len(rows)
    per_gap = tot / n

    # Healthy: the drive is streaming and a request that continues it costs
    # nothing extra, so sectors arrive one steady period apart.
    # A WINDOW, not a ceiling: an arbitrarily small gap is not "the cadence",
    # it is a different situation entirely.
    if STEADY_2X * 0.9 <= per_gap <= STEADY_2X * 1.1:
        return ("streaming",
                f"sectors arrive {per_gap:,.0f} cycles apart -- the 2x "
                f"cadence ({STEADY_2X:,}). Requests that continue the current "
                f"read cost no restart, so there is no per-request read-start "
                f"penalty left in the gap.")
    if sil > aft:
        return ("guest-silence",
                f"{sil/tot:.0%} of the gap is the guest issuing nothing after "
                f"the previous sector arrived ({sil/n:,.0f} cycles each). The "
                f"drive is idle and waiting on the game.")
    # Only claim a read-start when the gap is actually big enough to hold one.
    start = READ_START_2X if aft / n >= READ_START_2X else 0
    queue = max(0, aft / n - start)
    detail = (f"about {start:,} of read-start delay and {queue:,.0f} of "
              f"command queueing" if start else
              f"{queue:,.0f} of it beyond the steady cadence")
    return ("emulator-latency",
            f"{aft/tot:.0%} of the gap falls AFTER the guest asked "
            f"({aft/n:,.0f} cycles each): {detail}. That part is ours, not "
            f"the game's.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--lba-lo", type=int, default=125104)
    ap.add_argument("--lba-hi", type=int, default=125117)
    ap.add_argument("--out", default="analysis/frames/gap_anatomy.json")
    args = ap.parse_args()

    conn = DebugConn(args.host, args.port, args.timeout)
    try:
        tim = conn.cmd("cdrom_timing_dump", tail=4096,
                       lba_lo=args.lba_lo - 4, lba_hi=args.lba_hi)
        # These rings span the WHOLE session and are not reset here (cd_verify
        # arms them; this tool reads them after the fact). A sector read in
        # several passes therefore carries records from all of them, and an
        # earlier degraded pass looks exactly like current behaviour. Refuse
        # rather than analyse a mixture: a timeline built from one was read as
        # the baseline once, and a fix was written against a failure mode the
        # clean pass does not have.
        per_lba = {}
        for e in tim.get("entries", []):
            per_lba.setdefault(int(e.get("lba", -1)), []).append(e)
        multi = {k: v for k, v in per_lba.items() if len(v) > 1}
        lost = {k for k, v in per_lba.items() if any(int(x.get("lost", 0))
                                                     for x in v)}
        if multi or lost:
            print(f"these rings hold {len(multi)} sector(s) with several "
                  f"records and {len(lost)} with a lost notification -- more "
                  f"than one pass, possibly including a degraded one. Mixing "
                  f"them produces a timeline that belongs to no single pass.\n"
                  f"Run `python3 tools/cd_verify.py` first (it resets the "
                  f"timing ring), replay the load once, then rerun this.",
                  file=sys.stderr)
            return 1
        hist = conn.cmd("cdrom_command_history", count=4096)
    except DebugError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    sectors = sector_cycles(tim.get("entries", []))
    setlocs = setloc_cycles(hist.get("entries", []))
    if hist.get("entries") and "cycle" not in hist["entries"][0]:
        print("this psx-runtime predates the command-history cycle stamp -- "
              "rebuild and restart it.", file=sys.stderr)
        return 1

    rows = anatomy(sectors, setlocs, args.lba_lo, args.lba_hi)
    if not rows:
        print(f"no requested sector in {args.lba_lo}..{args.lba_hi} has both "
              f"a Setloc and a delivery record -- replay the load and rerun.")
        return 1

    print(f"{'LBA':>7} {'gap':>10} {'silence':>10} {'after ask':>10}"
          f"   {'silence':>8} {'after':>8}  (frames)")
    for r in rows:
        print(f"{r['lba']:>7} {r['gap']:>10,} {r['silence']:>10,} "
              f"{r['after_issue']:>10,}   {r['silence']/CYCLES_PER_FRAME:>8.2f} "
              f"{r['after_issue']/CYCLES_PER_FRAME:>8.2f}")

    v, why = verdict_of(rows)
    doc = {"kind": KIND, "version": 1, "rows": rows,
           "verdict": v, "explanation": why}
    print(f"\nVERDICT: {v}\n{why}")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"\nreport: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
