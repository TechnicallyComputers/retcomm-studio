#!/usr/bin/env python3
"""notify_diff.py -- diff how the two emulators NOTIFY the guest per sector.

    python3 tools/notify_diff.py     # both at/after the artifact screen

The transfers are understood: psx-runtime fills the game's buffers
109,111,111,113,113 where DuckStation fills 109,110,111,112,113. Sectors 110
and 112 -- the second of each two-sector read, one of which carries the
effect's palette -- are READ on psx-runtime (delivery record [D], never lost)
and simply never drained. Continuations are not broken in general: the
five-sector read 113..117 drains completely on both. It fails on the
TWO-sector reads.

So the question is no longer what arrives, but what the guest is TOLD about
what arrived. psx-runtime raises an immediate INT1 for a sector arriving with
the flag clear. DuckStation may instead DROP the notification and re-announce
it once the guest finishes draining. This prints, per sector, which of those
happened on each side.

  psx-runtime: data / pended / lost      (its per-sector timing records)
  oracle:      data / dropped / queued / delivered / redelivered / drained
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_DUCKSTATION_PORT, DEFAULT_NATIVE_PORT, DebugConn, DebugError,
)

KIND = "psx-notify-diff"


def native_rows(entries):
    """LBA -> flags from the MOST RECENT record for that sector.

    These rings accumulate for the whole session, so replaying the scene
    legitimately produces several records per sector -- accumulating them
    conflates passes, and one earlier degraded pass then poisons every
    sector's summary. Entries arrive oldest-first, so the last one wins.
    """
    out = {}
    for e in entries:
        lba = int(e.get("lba", -1))
        if lba < 0:
            continue
        prev = out.get(lba, {"passes": 0})
        out[lba] = {"passes": prev["passes"] + 1,
                    "data": int(e.get("data", 0)),
                    "dma": int(e.get("dma", 0)),
                    "pended": int(e.get("pended", 0)),
                    "lost": int(e.get("lost", 0))}
    return out


def oracle_rows(entries):
    """LBA -> flags from the MOST RECENT event for that sector. See above."""
    out = {}
    for e in entries:
        lba = int(e.get("lba", -1))
        if lba < 0:
            continue
        prev = out.get(lba, {"passes": 0})
        row = {"passes": prev["passes"] + 1}
        for k in ("data", "dropped", "queued", "delivered", "redelivered",
                  "drained"):
            row[k] = int(e.get(k, 0))
        out[lba] = row
    return out


def marks(r, keys):
    return "".join(k[0].upper() if r.get(k) else "-" for k in keys)


NAT_KEYS = ("data", "dma", "pended", "lost")
ORC_KEYS = ("data", "dropped", "queued", "delivered", "redelivered", "drained")


def interpret(nat, orc, lo, hi):
    """What the two notification patterns say about the missed sectors."""
    notes = []
    for lba in range(lo, hi + 1):
        n, o = nat.get(lba), orc.get(lba)
        if not n or not o:
            continue
        # The interesting case: psx-runtime raised an immediate INT1 (data,
        # not pended) while the oracle deliberately withheld and re-announced.
        nat_immediate = n["data"] and not n["pended"]
        orc_withheld = o["dropped"] or o["redelivered"]
        if nat_immediate and orc_withheld:
            notes.append(
                f"LBA {lba}: psx-runtime raised an immediate INT1; the oracle "
                f"{'dropped it' if o['dropped'] else 'withheld it'}"
                f"{' and re-announced after the drain' if o['redelivered'] else ''}."
                f" Announcing straight away moves the read slot before the "
                f"guest has drained the previous sector -- which is how the "
                f"sector goes missing.")
    return notes


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--ds-port", type=int, default=DEFAULT_DUCKSTATION_PORT)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--lba-lo", type=int, default=125104)
    ap.add_argument("--lba-hi", type=int, default=125117,
                    help="last sector of the load. The game issues Pause "
                         "after 125117; sectors past it are read-ahead it "
                         "never consumes, so THEIR notifications are lost by "
                         "design and must not be read as degradation")
    ap.add_argument("--ds-offset", type=int, default=150,
                    help="the oracle counts LBA from disc start; subtract the "
                         "2-second pregap to match psx-runtime's game LBAs")
    ap.add_argument("--out", default="analysis/frames/notify_diff.json")
    args = ap.parse_args()

    try:
        nrep = DebugConn(args.host, args.port, args.timeout).cmd(
            "cdrom_timing_dump", tail=4096,
            lba_lo=args.lba_lo, lba_hi=args.lba_hi)
        nat = native_rows(nrep.get("entries", []))
    except DebugError as e:
        print(f"psx-runtime: {e}", file=sys.stderr)
        return 2
    try:
        orep = DebugConn(args.host, args.ds_port, args.timeout).cmd(
            "cd_sector_events", tail=400,
            lba_lo=args.lba_lo + args.ds_offset,
            lba_hi=args.lba_hi + args.ds_offset)
        raw = orep.get("entries", [])
        for e in raw:
            e["lba"] = int(e["lba"]) - args.ds_offset
        orc = oracle_rows(raw)
    except DebugError as e:
        print(f"oracle: {e}", file=sys.stderr)
        print("  (rebuild + reinstall the oracle, restart it, replay the "
              "scene there)", file=sys.stderr)
        return 2

    # A contaminated psx-runtime session invalidates the whole comparison:
    # `lost` set, or a sector read more than once, is the degraded state, not
    # the clean one. Refuse rather than diff it -- the first run of this tool
    # compared a stale 2x-experiment session against a healthy oracle.
    # Loss only counts inside the load itself. Past its last sector the drive
    # keeps reading until the game's Pause, and those notifications are lost
    # by design -- flagging them made a clean session look degraded.
    dirty = sorted(lba for lba, r in nat.items()
                   if r["lost"] and lba <= args.lba_hi)
    if dirty:
        print(f"psx-runtime's most recent pass is DEGRADED inside the load: "
              f"sector(s) {dirty} lost their notification. That is the "
              f"failure state, not the baseline.\n"
              f"Restart psx-runtime -- it picks up the reverted authentic-1x "
              f"game.toml -- replay the scene, and rerun.", file=sys.stderr)
        return 1

    if not nat or not orc:
        for label, m in (("psx-runtime", nat), ("oracle", orc)):
            if not m:
                print(f"  {label} has no sector records in LBA "
                      f"{args.lba_lo}..{args.lba_hi} -- play the palette load "
                      f"there and rerun.")
        return 1

    print(f"{'LBA':>8}  psx-runtime {'/'.join(k[:3] for k in NAT_KEYS):>16}"
          f"   oracle {'/'.join(k[:3] for k in ORC_KEYS)}")
    for lba in range(args.lba_lo, args.lba_hi + 1):
        n, o = nat.get(lba), orc.get(lba)
        ns = f"{marks(n, NAT_KEYS)} p{n['passes']}" if n else "--"
        os_ = f"{marks(o, ORC_KEYS)} p{o['passes']}" if o else "--"
        print(f"{lba:>8}  {ns:>27}   {os_}")

    notes = interpret(nat, orc, args.lba_lo, args.lba_hi)
    doc = {"kind": KIND, "version": 1, "native": nat, "oracle": orc,
           "notes": notes}
    if notes:
        print("\nnotification differences on the sectors that go missing:")
        for t in notes:
            print(f"  - {t}")
    else:
        print("\nNo sector shows psx-runtime announcing immediately where the "
              "oracle withheld. The notification patterns agree, so what the "
              "guest is TOLD is not the difference either.")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"\nreport: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
