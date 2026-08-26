#!/usr/bin/env python3
"""cd_fork.py -- which CD loads filled the effect's colour table, and what the
disc actually holds at those sectors.

    python3 tools/cd_fork.py                      # loads targeting the table
    python3 tools/cd_fork.py --iso /path/to/game.bin   # + verify against disc

Where this fits
---------------
table_watch on both emulators pinned the fork to one moment: entering the
scene, psx-runtime's table region receives two CD-ROM DMA loads (74 then 339
words) leaving 216 distinct raw colour words, while DuckStation's region goes
to the correct 5-colour table -- and both then hold their state for the whole
effect. Everything before the fork is byte-identical (same fingerprint).

psx-runtime logs every ch3 DMA with its LBA, destination and size
(cd_read_log). So: find the loads that intersect the table, and read those
very sectors from the ISO. If the disc's own bytes at those LBAs are the
216-colour data, psx-runtime delivered exactly what the game asked for -- and
the divergence is UPSTREAM, in why the game asked (or where it pointed the
DMA). If the disc holds something else, the CD stack is delivering wrong
sector data, and that is the bug.

The log survives only as long as psx-runtime runs; capture after playing the
scene, before restarting.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import DEFAULT_NATIVE_PORT, DebugConn, DebugError  # noqa: E402

KIND = "psx-cd-fork"

RAW_SECTOR = 2352
MODE2_HEADER = 24          # sync 12 + header 4 + subheader 8
DATA_BYTES = 2048


def overlapping(entries, lo, hi):
    """CD loads whose [dest, dest+size) intersects [lo, hi)."""
    out = []
    for e in entries:
        dest = int(e["dest"], 16) & 0x1FFFFF
        size = int(e["size"])
        if dest < hi and dest + size > lo:
            out.append({"lba": int(e["lba"]), "dest": dest, "size": size})
    return out


def sector_bytes(iso, lba, count=1):
    """User data of `count` sectors from a raw Mode2 .bin image."""
    out = bytearray()
    with open(iso, "rb") as f:
        for k in range(count):
            f.seek((lba + k) * RAW_SECTOR + MODE2_HEADER)
            out += f.read(DATA_BYTES)
    return bytes(out)


def distinct_colours(blob):
    seen = set()
    for i in range(0, len(blob) - 3, 4):
        seen.add(int.from_bytes(blob[i:i + 4], "little") & 0xFFFFFF)
    return len(seen)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--lo", default="0x000E25F0",
                    help="table region start (physical)")
    ap.add_argument("--hi", default="0x000E2C60")
    ap.add_argument("--tail", type=int, default=65536)
    ap.add_argument("--iso", default=None,
                    help="raw .bin disc image; enables sector verification")
    ap.add_argument("--out", default="analysis/frames/cd_fork.json")
    args = ap.parse_args()

    lo, hi = int(args.lo, 0) & 0x1FFFFF, int(args.hi, 0) & 0x1FFFFF
    conn = DebugConn(args.host, args.port, args.timeout)
    try:
        rep = conn.cmd("cd_read_log", tail=args.tail)
    except DebugError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    entries = rep.get("entries", [])
    hits = overlapping(entries, lo, hi)
    doc = {"kind": KIND, "version": 1, "lo": args.lo, "hi": args.hi,
           "log_total": rep.get("total"), "loads": hits}

    if not hits:
        print(f"no CD load in the log intersects 0x{lo:06X}..0x{hi:06X}. "
              f"The log holds {rep.get('total')} entries and only survives "
              f"while psx-runtime runs -- play the scene first, then run this "
              f"without restarting.")
        return 1

    print(f"{len(hits)} CD load(s) intersect the table region:")
    for h in hits:
        print(f"  LBA {h['lba']:>7}  ->  0x{h['dest']:06X}  {h['size']} bytes")

    if args.iso:
        if not os.path.exists(args.iso):
            print(f"error: no such file: {args.iso}", file=sys.stderr)
            return 2
        print(f"\nreading those sectors from {os.path.basename(args.iso)}:")
        for h in hits:
            nsec = (h["size"] + DATA_BYTES - 1) // DATA_BYTES
            blob = sector_bytes(args.iso, h["lba"], nsec)
            # Only the slice that actually landed inside the table matters.
            skip = max(0, lo - h["dest"])
            take = min(h["dest"] + h["size"], hi) - max(h["dest"], lo)
            piece = blob[skip:skip + take]
            n = distinct_colours(piece)
            h["disc_distinct_colours"] = n
            h["disc_first_bytes"] = piece[:32].hex()
            print(f"  LBA {h['lba']}: the {take}-byte slice covering the "
                  f"table holds {n} distinct colour word(s)")
        worst = max(h.get("disc_distinct_colours", 0) for h in hits)
        if worst > 32:
            doc["verdict"] = "disc-holds-the-raw-data"
            doc["explanation"] = (
                "the ISO's own sectors at the logged LBAs contain the "
                "many-colour raw data. psx-runtime delivered exactly what was "
                "asked: the divergence is UPSTREAM -- in why the game "
                "requested this load or where it pointed the DMA -- not in "
                "the CD stack's data path.")
        else:
            doc["verdict"] = "disc-differs-from-ram"
            doc["explanation"] = (
                "the ISO's sectors at the logged LBAs do NOT contain the "
                "many-colour data that landed in RAM: the CD stack delivered "
                "wrong bytes for these reads. That is the bug's home.")
        print(f"\nVERDICT: {doc['verdict']}\n{doc['explanation']}")
    else:
        print("\npass --iso /path/to/disc.bin to check what the disc itself "
              "holds at these LBAs -- that is the half that decides.")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
