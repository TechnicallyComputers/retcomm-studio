#!/usr/bin/env python3
"""cd_diff.py -- diff the game's CD request streams between the two emulators.

    python3 tools/cd_diff.py            # both emulators at/after the scene

Every layer below the game has measured clean on psx-runtime: requests are
executed exactly (delivered == requested, data heads match the ISO), INT1s are
presented (lost=0), the arithmetic and tables downstream are correct. What
diverges is the game's OWN request stream -- native asks Setloc 125111 then
125113 where the oracle's table proves it asked for 125112. The game computes
those requests from what the emulator tells it, so the first differing command
between the two streams, and the traffic just before it, IS the runtime bug.

Both emulators now keep a flood-immune per-command history (psx-runtime:
cdrom_command_history; oracle: cd_history, added for this). Play the scene on
BOTH, then run this: it aligns the streams on the last common Setloc and
prints the fork.
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
from cd_verify import CMD_NAMES, bcd  # noqa: E402

KIND = "psx-cd-diff"


def normalise(entries):
    """Common shape: [{cmd, params, lba?, frame}] oldest first."""
    out = []
    for e in entries:
        cmd = e.get("cmd")
        cmd = int(cmd, 16) if isinstance(cmd, str) else int(cmd or 0)
        ps = [int(x, 16) if isinstance(x, str) else int(x)
              for x in (e.get("params") or [])]
        row = {"cmd": CMD_NAMES.get(cmd, f"0x{cmd:02X}"), "params": ps,
               "frame": int(e.get("frame", 0))}
        if cmd == 0x02 and len(ps) >= 3:
            m, s, f = (bcd(x) for x in ps[:3])
            row["lba"] = (m * 60 + s) * 75 + f - 150
        out.append(row)
    return out


def setloc_runs(rows, lo, hi):
    """Indexes of Setloc commands whose LBA falls inside the target file."""
    return [i for i, r in enumerate(rows)
            if r["cmd"] == "Setloc" and lo <= r.get("lba", -1) <= hi]


def lba_sequence(rows, lo, hi):
    return [rows[i]["lba"] for i in setloc_runs(rows, lo, hi)]


def command_key(row):
    return (row["cmd"], row.get("lba", tuple(row["params"])))


def find_fork(a, b, window):
    """Align the two streams on their last long common run inside `window`
    and return the index pair where they first differ afterwards."""
    ka = [command_key(r) for r in a]
    kb = [command_key(r) for r in b]
    best = None
    for i in range(len(ka)):
        for j in range(len(kb)):
            if ka[i] != kb[j]:
                continue
            n = 0
            while (i + n < len(ka) and j + n < len(kb)
                   and ka[i + n] == kb[j + n]):
                n += 1
            if n >= window and (best is None or n > best[2]):
                best = (i, j, n)
    if best is None:
        return None
    i, j, n = best
    return {"a_start": i, "b_start": j, "common": n,
            "a_fork": i + n, "b_fork": j + n}


def in_lba_range(rows, lo, hi):
    return [r for r in rows if lo <= r.get("lba", -1) <= hi
            or True]  # keep all; range used only for reporting focus


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--ds-port", type=int, default=DEFAULT_DUCKSTATION_PORT)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--count", type=int, default=2048)
    ap.add_argument("--lba-lo", type=int, default=125000)
    ap.add_argument("--lba-hi", type=int, default=125200,
                    help="the palette file's LBA neighbourhood; alignment "
                         "anchors on Setlocs into this range, because generic "
                         "alignment latches onto the periodic Getstat "
                         "heartbeat and forks on unrelated journey points")
    ap.add_argument("--window", type=int, default=6,
                    help="minimum matching run to anchor the alignment")
    ap.add_argument("--out", default="analysis/frames/cd_diff.json")
    args = ap.parse_args()

    doc = {"kind": KIND, "version": 1}
    try:
        nat = normalise(DebugConn(args.host, args.port, args.timeout)
                        .cmd("cdrom_command_history",
                             count=args.count).get("entries", []))
    except DebugError as e:
        print(f"psx-runtime: {e}", file=sys.stderr)
        return 2
    try:
        orc = normalise(DebugConn(args.host, args.ds_port, args.timeout)
                        .cmd("cd_history", count=args.count)
                        .get("entries", []))
    except DebugError as e:
        print(f"oracle: {e} -- rebuild it (duckstation_oracle.py build + "
              f"install) and replay the scene there", file=sys.stderr)
        return 2

    # psx-runtime's history is newest-first; oracle's is oldest-first.
    if nat and len(nat) > 1 and nat[0]["frame"] > nat[-1]["frame"]:
        nat = nat[::-1]
    if orc and len(orc) > 1 and orc[0]["frame"] > orc[-1]["frame"]:
        orc = orc[::-1]
    doc["native_count"], doc["oracle_count"] = len(nat), len(orc)
    print(f"native: {len(nat)} commands; oracle: {len(orc)} commands")

    # Anchor on the palette file itself. Generic longest-common-run
    # alignment latched onto the periodic Getstat heartbeat (every 30
    # frames, identical everywhere) and reported a fork between two
    # unrelated journey points.
    na = setloc_runs(nat, args.lba_lo, args.lba_hi)
    ob = setloc_runs(orc, args.lba_lo, args.lba_hi)
    seq_n = lba_sequence(nat, args.lba_lo, args.lba_hi)
    seq_o = lba_sequence(orc, args.lba_lo, args.lba_hi)
    doc["native_lbas"], doc["oracle_lbas"] = seq_n, seq_o
    for label, seq in (("psx-runtime", seq_n), ("oracle", seq_o)):
        if not seq:
            print(f"\n{label}: NO Setloc into LBA {args.lba_lo}.."
                  f"{args.lba_hi} in its history -- the palette load is not "
                  f"captured there. Play the scene on {label} (through the "
                  f"land-creation moment) and rerun.")
    if not seq_n or not seq_o:
        _save(doc, args)
        return 1

    print(f"\nSetloc sequences into the palette file:")
    print(f"  psx-runtime: {seq_n}")
    print(f"  oracle:      {seq_o}")
    fork_at = next((k for k in range(min(len(seq_n), len(seq_o)))
                    if seq_n[k] != seq_o[k]), None)
    if fork_at is None and len(seq_n) != len(seq_o):
        fork_at = min(len(seq_n), len(seq_o))
    doc["lba_fork_index"] = fork_at

    def show(rows, idxs, label, upto):
        print(f"\n  {label} (full command window):")
        lo_i = max(0, idxs[0] - 4)
        hi_i = min(len(rows), idxs[-1] + 10)
        fork_row = idxs[upto] if upto is not None and upto < len(idxs) else None
        for k in range(lo_i, hi_i):
            r = rows[k]
            lba = f" -> lba {r['lba']}" if "lba" in r else ""
            mark = "  <<< FIRST DIVERGED LBA" if k == fork_row else ""
            print(f"    f{r['frame']}: {r['cmd']}"
                  f"({' '.join(f'{x:02X}' for x in r['params'])}){lba}{mark}")

    show(nat, na, "psx-runtime", fork_at)
    show(orc, ob, "oracle", fork_at)

    if fork_at is None:
        print("\nThe two request sequences into the palette file are "
              "IDENTICAL. The divergence is not in which sectors were "
              "requested -- re-examine what was DELIVERED for them.")
    else:
        a = seq_n[fork_at] if fork_at < len(seq_n) else None
        b = seq_o[fork_at] if fork_at < len(seq_o) else None
        print(f"\nFORK at request #{fork_at} into the file: native asked "
              f"lba {a}, oracle asked lba {b}. The game computes these from "
              f"what the emulator told it during the requests BEFORE this "
              f"one -- that is where the runtime diverges.")
    _save(doc, args)
    return 0


def _save(doc, args):
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"\nreport: {args.out}")


if __name__ == "__main__":
    sys.exit(main())
