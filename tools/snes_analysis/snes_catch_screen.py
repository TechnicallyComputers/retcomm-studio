#!/usr/bin/env python3
"""snes_catch_screen.py — wait for a screen to appear, then dump everything.

For a defect you can reach by hand but not describe to a script: menus, pause
screens, anything behind an input sequence. Leave this running, reproduce the
screen on the controller, and it captures the full PPU state at that moment
rather than a screenshot you then have to interpret.

Matching is by colour signature, because that is what a person can describe:

    --dominant 0F3F0F     the screen's most common colour, hex RGB
    --tolerance 40        per-channel slack (default 40)
    --min-share 0.20      that colour must cover this fraction (default 0.20)
    --max-colours 40      and the screen must be this flat (default 0 = any)

On a match it writes <out>/catch.json with the PPU register file, CGRAM census,
DMA/HDMA channel state, the raster journal (the per-line waveform the renderer
actually replayed), and a PNG of the frame — plus the same for a frame just
before, so a flickering screen can be compared against itself.

Usage:
    python3 snes_catch_screen.py --port 4370 --dominant 0F3F0F --out analysis/diagnostics

Requires a runtime built with SNESRECOMP_ENABLE_TRACE=ON and exclusive access
to the debug port (the server holds ONE client).
"""

import argparse
import collections
import json
import os
import socket
import sys
import time

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def say(m):
    print(m, flush=True)


class Debug:
    def __init__(self, host, port, timeout=20.0):
        self.sock = socket.create_connection((host, port), timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

    def cmd(self, text):
        self.sock.sendall((text + "\n").encode())
        while b"\n" not in self.buf:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError(
                    "debug server closed the connection — the server holds ONE "
                    "client, so disconnect Studio's Diagnostics tab first")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode(errors="replace")

    def json(self, text):
        return json.loads(self.cmd(text))


def grab(dbg, path):
    try:
        os.remove(path)
    except OSError:
        pass
    dbg.cmd("screenshot " + path)
    for _ in range(30):
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            return True
        time.sleep(0.02)
    return False


def snapshot(dbg, png_path, bmp):
    """Everything worth having about one frame."""
    from PIL import Image
    im = Image.open(bmp).convert("RGB")
    im.save(png_path)
    counts = collections.Counter(im.getdata())
    cg = dbg.json("dump_cgram")["hex"]
    b = bytes.fromhex("".join(cg.split()))
    words = [b[i] | b[i + 1] << 8 for i in range(0, 512, 2)]
    census = collections.Counter(words)
    top_cg, top_cg_n = census.most_common(1)[0]
    try:
        journal = dbg.json("raster_journal")
    except Exception:
        journal = {"error": "raster_journal unavailable in this build"}
    return {
        "frame": dbg.json("frame")["frame"],
        "png": os.path.basename(png_path),
        "colours": len(counts),
        "dominant": "#%02X%02X%02X" % counts.most_common(1)[0][0],
        "dominant_share": counts.most_common(1)[0][1] / float(im.size[0] * im.size[1]),
        "ppu": dbg.json("get_ppu_state"),
        "irq": dbg.json("get_interrupt_state"),
        "dma": dbg.json("get_dma_state"),
        "cgram": {"distinct": len(census),
                  "top_value": "0x%04X" % top_cg, "top_count": top_cg_n},
        "raster_journal": journal,
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4370)
    ap.add_argument("--dominant", required=True, help="hex RGB, e.g. 0F3F0F")
    ap.add_argument("--tolerance", type=int, default=40)
    ap.add_argument("--min-share", type=float, default=0.20)
    ap.add_argument("--max-colours", type=int, default=0,
                    help="0 = any; else the screen must have at most this many")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--out", default="analysis/diagnostics")
    args = ap.parse_args()

    want = tuple(int(args.dominant[i:i + 2], 16) for i in (0, 2, 4))
    os.makedirs(args.out, exist_ok=True)
    bmp = os.path.join(args.out, "_catch.bmp")

    try:
        dbg = Debug(args.host, args.port)
    except Exception as exc:
        say("cannot reach the runtime on %s:%d — %s" % (args.host, args.port, exc))
        return 2

    from PIL import Image
    say("watching for a screen dominated by #%02X%02X%02X (+/-%d), share >= %.0f%%%s"
        % (want + (args.tolerance, args.min_share * 100,
                   ", <= %d colours" % args.max_colours if args.max_colours else "")))
    say("reproduce it on the controller whenever you like; Ctrl-C to give up.")

    previous = None
    deadline = time.time() + args.timeout
    last_note = 0.0
    while time.time() < deadline:
        # Paced deliberately. An unpaced loop asks the runtime for a fresh
        # screenshot as fast as the socket allows, which is thousands of BMP
        # writes a minute and was measured killing the runtime outright — the
        # probe must not be the reason the thing it is watching dies.
        time.sleep(0.15)
        if not grab(dbg, bmp):
            continue
        im = Image.open(bmp).convert("RGB")
        counts = collections.Counter(im.getdata())
        (col, n) = counts.most_common(1)[0]
        share = n / float(im.size[0] * im.size[1])
        hit = (all(abs(col[i] - want[i]) <= args.tolerance for i in range(3))
               and share >= args.min_share
               and (not args.max_colours or len(counts) <= args.max_colours))
        if hit:
            say("caught it at frame %s: #%02X%02X%02X covering %.0f%%, %d colours"
                % (dbg.json("frame")["frame"], col[0], col[1], col[2],
                   share * 100, len(counts)))
            result = {"match": snapshot(dbg, os.path.join(args.out, "catch.png"), bmp)}
            if previous:
                result["previous_frame"] = previous
            path = os.path.join(args.out, "catch.json")
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(result, fh, indent=2)
            os.replace(tmp, path)
            say("wrote %s and catch.png" % path)
            return 0
        # Keep the frame before, so a FLICKERING screen can be diffed against
        # its own good frame — the two differ by exactly the defect.
        if share > 0.15:
            try:
                previous = snapshot(dbg, os.path.join(args.out, "catch_prev.png"), bmp)
            except Exception:
                pass
        now = time.time()
        if now - last_note > 10:
            last_note = now
            say("   still watching ... current screen #%02X%02X%02X %.0f%%, %d colours"
                % (col[0], col[1], col[2], share * 100, len(counts)))
    say("timed out after %ds without a match." % args.timeout)
    return 1


if __name__ == "__main__":
    sys.exit(main())
