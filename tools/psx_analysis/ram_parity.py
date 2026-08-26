#!/usr/bin/env python3
"""ram_parity.py -- compare guest RAM between psx-runtime and the DuckStation oracle.

    # the packet buffer a frame scan pointed at
    python3 ram_parity.py --lo 0x0010D078 --hi 0x0010D954 --pause

    # anything else
    python3 ram_parity.py --addr 0x80010000 --len 4096

The question this answers
-------------------------
When a frame draws wrongly there are only two possibilities, and they live in
different halves of the codebase:

  * the guest computed different numbers  -> a CPU / GTE / recompilation bug
  * the guest computed the SAME numbers   -> our renderer mishandles correct data

Comparing the bytes the game actually built settles it, and needs no pixels --
which matters, because the oracle's VRAM readback is currently unreliable while
its RAM readback is not.

Reading it right
----------------
Both emulators must be at the same point in the same game, or everything differs
and the answer means nothing. The tool reports both frame counters so you can
see whether they are aligned, and --pause parks both before reading: sampling
two running emulators gives you a race, not a comparison.

Addresses may be given physical (0x0010D078) or KSEG0 (0x8010D078); both servers
accept either. DuckStation caps a read at 64 KB, so ranges are chunked.
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

PARITY_KIND = "psx-ram-parity"
DS_CHUNK_CAP = 65536


def read_range(conn: DebugConn, addr: int, length: int, chunk: int) -> bytes:
    """Read [addr, addr+length) in chunks, because one peer caps a read."""
    out = bytearray()
    off = 0
    while off < length:
        n = min(chunk, length - off)
        rep = conn.cmd("read_ram", addr=f"0x{addr + off:08X}", len=n)
        hexs = rep.get("hex")
        if hexs is None:
            raise DebugError(
                f"read_ram replied without 'hex' — is {conn.host}:{conn.port} a "
                f"psxrecomp or patched-DuckStation debug server?")
        blob = bytes.fromhex(hexs)
        got = rep.get("len", len(blob))
        if not blob:
            raise DebugError(f"read_ram returned nothing at 0x{addr + off:08X}")
        out += blob
        # A peer that silently returns less than asked would otherwise spin here.
        off += len(blob)
        if len(blob) < n and got == len(blob):
            break
    return bytes(out)


# Bytes of agreement allowed inside one reported run. A changed 16-bit
# coordinate usually differs only in its low byte, so an uncoalesced diff
# reports one wrong vertex as a scatter of one-byte runs -- which reads as
# many small unrelated problems instead of one packet being wrong.
COALESCE_GAP = 4


def diff_runs(a: bytes, b: bytes, base: int, max_runs: int,
              gap: int = COALESCE_GAP):
    """Contiguous stretches that differ, as (addr, offset, length).

    Runs separated by fewer than `gap` matching bytes are merged: they are
    almost always one changed field, not two changed things.
    """
    runs = []
    n = min(len(a), len(b))
    i = 0
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
        runs.append({"addr": base + start, "offset": start,
                     "length": last - start + 1})
        if len(runs) >= max_runs:
            break
    return runs


def differing_words(a: bytes, b: bytes):
    """How many 32-bit words differ, and how many were compared.

    Words, not bytes, because packets are built out of words: "3 of 16
    words differ" is a statement about the data structure, while "5 of 64
    bytes differ" is a statement about nothing in particular.
    """
    n = min(len(a), len(b)) // 4
    bad = 0
    for k in range(n):
        p = k * 4
        if a[p:p + 4] != b[p:p + 4]:
            bad += 1
    return bad, n


def words(buf: bytes, off: int, count: int):
    out = []
    for k in range(count):
        p = off + k * 4
        if p + 4 > len(buf):
            break
        out.append(int.from_bytes(buf[p:p + 4], "little"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--native-port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--ds-port", type=int, default=DEFAULT_DUCKSTATION_PORT)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--lo", default=None, help="start address (hex ok)")
    ap.add_argument("--hi", default=None, help="end address, inclusive")
    ap.add_argument("--addr", default=None)
    ap.add_argument("--len", dest="length", type=int, default=0)
    ap.add_argument("--chunk", type=int, default=DS_CHUNK_CAP)
    ap.add_argument("--pause", action="store_true",
                    help="park both emulators around the read, then resume")
    ap.add_argument("--context", type=int, default=8,
                    help="32-bit words of context to print at the first difference")
    ap.add_argument("--max-runs", type=int, default=12)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    def parse_addr(v):
        return int(v, 0) if isinstance(v, str) else v

    if args.lo is not None and args.hi is not None:
        lo, hi = parse_addr(args.lo), parse_addr(args.hi)
        length = hi - lo + 1
    elif args.addr is not None and args.length > 0:
        lo, length = parse_addr(args.addr), args.length
    else:
        print("error: give --lo/--hi or --addr/--len", file=sys.stderr)
        return 2
    if length <= 0:
        print("error: empty range", file=sys.stderr)
        return 2

    result = {"kind": PARITY_KIND, "version": 1,
              "addr": f"0x{lo:08X}", "length": length}
    native = DebugConn(args.host, args.native_port, args.timeout)
    oracle = DebugConn(args.host, args.ds_port, args.timeout)
    paused = []
    try:
        try:
            fa = native.frame()
        except DebugError as e:
            print(f"error: psx-runtime not reachable on {args.native_port}: {e}",
                  file=sys.stderr)
            return 2
        try:
            fb = oracle.frame()
        except DebugError as e:
            print(f"error: DuckStation oracle not reachable on {args.ds_port}: {e}\n"
                  f"  start it with:  python3 duckstation_oracle.py start --disc <cue>",
                  file=sys.stderr)
            return 2
        result["native_frame"], result["oracle_frame"] = fa, fb
        print(f"psx-runtime frame {fa}   ·   oracle frame {fb}")

        if args.pause:
            for name, conn in (("psx-runtime", native), ("oracle", oracle)):
                try:
                    conn.cmd("pause")
                    paused.append(conn)
                except DebugError as e:
                    print(f"  warning: could not pause {name}: {e}", file=sys.stderr)
            if len(paused) == 2:
                print("  both parked for the read")

        print(f"reading 0x{lo:08X}..0x{lo + length - 1:08X}  ({length} bytes)")
        a = read_range(native, lo, length, min(args.chunk, DS_CHUNK_CAP))
        b = read_range(oracle, lo, length, min(args.chunk, DS_CHUNK_CAP))
    except DebugError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        for conn in paused:
            try:
                conn.cmd("continue")
            except DebugError:
                pass
        if paused:
            print("  resumed")

    n = min(len(a), len(b))
    if len(a) != len(b):
        print(f"  note: short read — native {len(a)} bytes, oracle {len(b)}; "
              f"comparing the first {n}")
    differing = sum(1 for i in range(n) if a[i] != b[i])
    runs = diff_runs(a, b, lo, args.max_runs)
    result.update({"compared": n, "differing_bytes": differing,
                   "identical": differing == 0, "runs": runs})

    print()
    if differing == 0:
        print(f"IDENTICAL — {n} bytes match.")
        print("  The guest computed the same data on both emulators. If the "
              "picture still differs, the bug is in how WE render correct data, "
              "not in what the game produced.")
    else:
        pct = 100.0 * differing / max(1, n)
        print(f"DIFFERENT — {differing}/{n} bytes ({pct:.2f}%) in "
              f"{len(runs)}{'+' if len(runs) >= args.max_runs else ''} run(s).")
        print("  The guest produced different data. The bug is upstream of the "
              "renderer — CPU, GTE, or the recompilation.")
        if fa != fb:
            print(f"  CAUTION: the two are on different frames ({fa} vs {fb}), so "
                  f"some of this is just elapsed time. Line them up before "
                  f"trusting the detail.")
        print(f"\n  {'address':>12}  {'offset':>8}  {'bytes':>6}")
        for r in runs:
            print(f"  0x{r['addr']:08X}  {r['offset']:>8}  {r['length']:>6}")

        first = runs[0]["offset"]
        ctx = max(0, (first // 4 - args.context // 2)) * 4
        wa = words(a, ctx, args.context)
        wb = words(b, ctx, args.context)
        print(f"\n  first difference at 0x{lo + first:08X} (+0x{first:X}) — "
              f"32-bit words around it:")
        print(f"  {'address':>12}  {'psx-runtime':>12}  {'oracle':>12}")
        for k, (x, y) in enumerate(zip(wa, wb)):
            mark = "  <-- differs" if x != y else ""
            print(f"  0x{lo + ctx + k * 4:08X}  0x{x:08X}    0x{y:08X}{mark}")
        result["first_difference"] = f"0x{lo + first:08X}"

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
        print(f"\nwrote {args.json}")
    return 0 if differing == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
