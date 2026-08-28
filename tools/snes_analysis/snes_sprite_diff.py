#!/usr/bin/env python3
"""snes_sprite_diff.py — one command: is a sprite wrong, and why?

Compares a LIVE snesrecomp session against a Mesen capture of the same screen
and, for every frame that differs, says whether each offending sprite is
MISSING from OAM (the game never emitted it) or MISPLACED (emitted at the
wrong coordinates). Those are different defects with different fixes.

The whole reason this exists is that doing it by hand kept producing confident
wrong answers. Four separate "findings" in one session dissolved on re-check,
every one of them because two captures were not on the same screen or not on
the same frame. So the alignment is done for you, and refused when unsafe:

  1. SCENE GATE. If no recomp frame closely resembles any oracle frame, the
     two runs are on different screens and NOTHING is reported. A frame-number
     match is not evidence; Mesen's Lua frame counter starts at script load
     and drifts by hundreds of frames between launches.
  2. ALIGNMENT BY PIXELS. Every oracle frame is matched against every recomp
     frame; the offset is read off the result, never assumed, and never
     searched in a narrow window. A too-narrow search is how a -44 and a +1317 offset were
     both missed.
  3. FULL CYCLE. A defect at one phase of a 128-frame loop is invisible both
     to a short sample and to a periodicity test (which sees a consistently
     wrong frame as consistent). Capture more than one full cycle.

Usage — with the game already sitting on the screen of interest:

    # 1. Mesen, driven BY HAND to the same screen (it captures itself):
    GW_WATCH=0012=14 GW_COUNT=200 GW_OAM=1 GW_DIR=analysis/oracle_cap \\
      mesen-ce <rom> mesen_frames.lua

    # 2. then, with snesrecomp on that screen:
    python3 snes_sprite_diff.py --oracle analysis/oracle_cap --count 300

GW_WATCH takes ADDR=VALUE in hex against WRAM; 0012=14 is Gundam Wing's
pre-fight screen. Find a marker for another scene by dumping WRAM on the
screen you want and on one you don't, and taking a byte that differs.
"""

import argparse
import glob
import json
import os
import shutil
import socket
import subprocess
import sys

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

N = 256 * 224 * 3


def say(m):
    print(m, flush=True)


class Debug:
    def __init__(self, port, timeout=240.0):
        self.sock = socket.create_connection(("127.0.0.1", port), 10.0)
        self.sock.settimeout(timeout)
        self.buf = b""

    def cmd(self, text):
        try:
            self.sock.sendall((text + "\n").encode())
        except OSError as exc:
            raise SystemExit("debug server went away: %s" % exc)
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(1 << 20)
            except OSError as exc:
                raise SystemExit("debug server went away: %s" % exc)
            if not chunk:
                raise SystemExit("debug server closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode(errors="replace")

    def json(self, text):
        return json.loads(self.cmd(text))


def raw_rgb(path):
    d = open(path, "rb").read()
    o = bytearray(N)
    for i in range(0, 256 * 224 * 4, 4):
        j = (i >> 2) * 3
        o[j] = d[i + 2]; o[j + 1] = d[i + 1]; o[j + 2] = d[i]
    return bytes(o)


def png_rgb(path):
    tmp = path + ".ppm"
    subprocess.run(["magick", path, "-depth", "8", "ppm:" + tmp],
                   check=True, capture_output=True)
    d = open(tmp, "rb").read()
    os.remove(tmp)
    tok, i = [], 2
    while len(tok) < 3:
        while d[i:i + 1].isspace():
            i += 1
        j = i
        while not d[j:j + 1].isspace():
            j += 1
        tok.append(int(d[i:j])); i = j
    return d[i + 1:]


def ndiff(a, b):
    x = (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(N, "big")
    return N - x.count(0)


def sprites(buf):
    """544-byte OAM -> {slot: (x, y, tile, attr, size)} for visible sprites."""
    out = {}
    for s in range(128):
        y = buf[s * 4 + 1]
        if y >= 0xE0:            # parked off-screen; never rendered
            continue
        hi = (buf[512 + (s >> 2)] >> ((s & 3) * 2)) & 3
        x = buf[s * 4 + 0] | ((hi & 1) << 8)
        if x >= 256:
            x -= 512
        out[s] = (x, y, buf[s * 4 + 2], buf[s * 4 + 3], (hi >> 1) & 1)
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--oracle", required=True, help="dir written by mesen_frames.lua")
    ap.add_argument("--port", type=int, default=4370)
    ap.add_argument("--count", type=int, default=300, help="frames to capture from the recomp")
    ap.add_argument("--out", default="analysis/sprite_diff")
    ap.add_argument("--gate", type=float, default=8.0,
                    help="max %% difference for 'same scene' (default 8)")
    args = ap.parse_args()

    oracle = os.path.abspath(args.oracle)
    opng = sorted(glob.glob(os.path.join(oracle, "f*.png")))
    if not opng:
        raise SystemExit("no oracle frames in %s — run mesen_frames.lua first" % oracle)
    out = os.path.abspath(args.out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)

    d = Debug(args.port)
    marker = d.json("dump_ram 12 1").get("hex", "??")
    say("recomp scene marker $7E:0012 = %s" % marker)
    f = int(d.json("freeze_status")["frame"]) + 12
    say("capturing %d frames from the live session at frame %d ..." % (args.count, f))
    r = d.json("dump_frame_range %d %d %s" % (f, args.count, out))
    if "error" in r:
        raise SystemExit("capture failed: %s" % r["error"])

    R = {int(os.path.basename(p)[1:-4]): raw_rgb(p)
         for p in glob.glob(os.path.join(out, "f*.raw"))}
    M = {int(os.path.basename(p)[1:-4]): png_rgb(p) for p in opng}
    say("recomp %d frames, oracle %d frames" % (len(R), len(M)))

    # ---- 1. scene gate -------------------------------------------------
    # Gate on the BEST match over every oracle frame, not one arbitrary
    # reference. Using max(M) alone reported 9.68% on a pair whose true
    # closest match was 0.24%: a single reference can land on a transition
    # frame, or on an animation phase the other side never happens to sample,
    # and then a perfectly good capture is refused as "different screens".
    floor = min(ndiff(M[m], R[k]) for m in M for k in R)
    pct = 100.0 * floor / N
    say("scene gate: closest recomp frame to the oracle = %.2f%%" % pct)
    if pct > args.gate:
        say("")
        say("ABORT: the two captures are not on the same screen (%.2f%% > %.1f%%)."
            % (pct, args.gate))
        say("  Nothing below would be a comparison. Re-capture both on the same")
        say("  screen; do NOT widen the gate to make this pass.")
        return 2

    # ---- 2. alignment by pixels ---------------------------------------
    # Scored by the MEDIAN per-pair difference, with exact matches only as a
    # tiebreak. Exact-match count alone is the wrong score: when no frame
    # matches exactly the count is 0 at EVERY offset, so the "winner" is
    # just whichever offset was tried first. That is not a weaker result,
    # it is a fabricated one — it reported a confident +7719 that meant
    # nothing. The median is also robust to the screen wipe at the head of
    # a watch-triggered capture, which a mean would let dominate.
    lo, hi = min(R) - max(M), max(R) - min(M)
    scored = []
    for off in range(lo, hi + 1):
        pr = [(m, m + off) for m in M if m + off in R]
        if len(pr) < max(20, len(M) // 3):
            continue
        ds = sorted(ndiff(M[a], R[b]) for a, b in pr)
        ex = sum(1 for a, b in pr if M[a] == R[b])
        scored.append((ds[len(ds) // 2], -ex, off, len(pr)))
    if not scored:
        raise SystemExit("no usable overlap between the captures")
    scored.sort()
    med, negex, best, besttot = scored[0]
    bestex = -negex
    say("alignment: offset %+d — %d/%d byte-identical, median %d px"
        % (best, bestex, besttot, med // 3))

    # Is that offset a real peak, or is every offset equally bad? A handful
    # of rival offsets is EXPECTED (an animation loop aliases at its own
    # period), so compare against the typical offset, not the runner-up.
    typical = scored[len(scored) // 2][0]
    if med > typical * 0.66:
        say("")
        say("REFUSING to report: offset %+d (median %d px) is no better than a"
            % (best, med // 3))
        say("  typical offset (%d px). The captures did not line up, so a"
            % (typical // 3))
        say("  per-frame diff below would be noise, not a rendering comparison.")
        say("  Re-capture with both sides settled on the same screen.")
        return 2

    # ---- 3. per-frame diff --------------------------------------------
    pairs = sorted((m, m + best) for m in M if m + best in R)
    bad = [(m, rf, ndiff(M[m], R[rf])) for m, rf in pairs if M[m] != R[rf]]
    say("frames differing: %d of %d" % (len(bad), len(pairs)))
    if not bad:
        say("")
        say("No frame differs. Note this proves only that the phases CAPTURED")
        say("match — if the loop is longer than the capture, widen --count.")
        return 0

    # ---- 4. sprite-level verdict --------------------------------------
    oam_ok = os.path.exists(os.path.join(oracle, "f%06d_oam.bin" % bad[0][0]))
    if not oam_ok:
        say("")
        say("Oracle capture has no OAM (re-run mesen_frames.lua with GW_OAM=1)")
        say("— reporting pixel differences only.")
        for m, rf, n in sorted(bad, key=lambda t: -t[2])[:10]:
            say("   oracle f%-7d vs recomp f%-7d : %6d px (%.2f%%)"
                % (m, rf, n // 3, 100.0 * n / N))
        return 1

    say("")
    say("sprite-level verdict on the worst frames:")
    for m, rf, n in sorted(bad, key=lambda t: -t[2])[:5]:
        mo = sprites(open(os.path.join(oracle, "f%06d_oam.bin" % m), "rb").read())
        ro = json.loads(d.cmd("get_frame_extended %d" % rf))
        if not ro.get("oam"):
            say("   recomp frame %d not in the history ring any more" % rf)
            continue
        rr = sprites(bytes.fromhex(ro["oam"]) + bytes.fromhex(ro.get("highOam", "")))
        missing = [s for s in mo if s not in rr]
        extra = [s for s in rr if s not in mo]
        moved = [s for s in mo if s in rr and mo[s] != rr[s]]
        say("  oracle f%d / recomp f%d — %d px differ" % (m, rf, n // 3))
        say("     visible sprites: oracle %d, recomp %d" % (len(mo), len(rr)))
        say("     MISSING from recomp (game never emitted): %s" % (missing[:10] or "none"))
        say("     EXTRA in recomp (emitted but not on hardware): %s" % (extra[:10] or "none"))
        for s in moved[:6]:
            say("     MISPLACED slot %-3d oracle x=%-4d y=%-3d t=%-3d a=%02X sz=%d"
                " | recomp x=%-4d y=%-3d t=%-3d a=%02X sz=%d"
                % (s, mo[s][0], mo[s][1], mo[s][2], mo[s][3], mo[s][4],
                   rr[s][0], rr[s][1], rr[s][2], rr[s][3], rr[s][4]))
    say("")
    say("frames + raws in %s" % out)
    return 1


if __name__ == "__main__":
    sys.exit(main())
