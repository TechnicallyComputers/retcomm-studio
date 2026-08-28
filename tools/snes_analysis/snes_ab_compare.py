#!/usr/bin/env python3
"""snes_ab_compare.py — frame-locked A/B of the recomp against Mesen.

Every comparison in this toolset before this one matched scenes by eye or by
phase, and that is where the mistakes came from: an emulator sampled at an
arbitrary instant against one sampled at end-of-frame will manufacture a
difference on any per-frame animation, and a "control" that happens not to
change between adjacent phases will not catch it.

This removes the alignment question instead of managing it. Both emulators are
launched from power-on and driven by the SAME button script, so frame N on one
side is frame N on the other, and the comparison is exact rather than
approximate. Any difference that survives is real.

    python3 snes_ab_compare.py --press 2800:t,3000:t,3300:t,3600:t \
                               --from 3560 --count 40 --out analysis/ab

Writes per-frame PNGs from both sides, a diff table, and a verdict. Frame
numbering can still be offset by a boot frame or two between the two
emulators, so the tool searches a small offset window and reports the best
alignment along with how much better it is than its neighbours -- an offset
that is not clearly better than the ones around it means the run did not
replay deterministically, and the tool says so rather than reporting the
minimum as if it were a match.

Requires SNESRECOMP_ENABLE_TRACE=ON, mesen-ce on PATH, and Mesen's
Debug > ScriptWindow > AllowIoOsAccess enabled.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

TOOLS = os.path.dirname(os.path.abspath(__file__))


def say(m):
    print(m, flush=True)


class Debug:
    def __init__(self, port, timeout=120.0):
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


def connect(port, proc, deadline=90.0):
    end = time.time() + deadline
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise SystemExit("emulator exited early (rc=%s)" % proc.returncode)
        try:
            d = Debug(port)
            if d.json("ping").get("ok"):
                return d
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    raise SystemExit("debug port %d never answered" % port)


def press_to_mesen(spec, hold=15):
    """Insert explicit releases for the Lua side.

    mesen_scripted_input.lua HOLDS the buttons of an entry until the next
    entry, whereas the runner's script is read the same way but the caller
    (here) supplies releases. Passing the bare spec to one and a
    release-annotated version to the other is not the same experiment: Start
    ends up held for hundreds of frames on the oracle and tapped on the
    recomp, and the two walk through the menus at different speeds. That is
    not a rendering difference, but it looks exactly like one."""
    out = []
    for step in spec.split(","):
        if not step.strip():
            continue
        f, btns = step.split(":", 1)
        f = int(f); btns = btns.strip()
        out.append("%d:%s" % (f, btns))
        if btns:
            out.append("%d:" % (f + hold))
    out.sort(key=lambda s: int(s.split(":")[0]))
    return ",".join(out)


def press_to_recomp(spec):
    """'2800:t,3000:t' -> the runner's hex-bit script, with releases inserted.

    The two emulators take different notations, so the caller gives one and
    this derives both -- otherwise the scripts drift apart and the run stops
    being a controlled comparison."""
    BIT = {"b": 0, "y": 1, "s": 2, "t": 3, "u": 4, "d": 5,
           "n": 6, "m": 7, "a": 8, "x": 9, "l": 10, "r": 11}
    out = []
    for step in spec.split(","):
        if not step.strip():
            continue
        f, btns = step.split(":", 1)
        f = int(f)
        mask = 0
        for c in btns.strip():
            if c not in BIT:
                raise SystemExit("unknown button %r in --press" % c)
            mask |= 1 << BIT[c]
        out.append("%d:%x" % (f, mask))
        if mask:
            out.append("%d:0" % (f + 15))      # release after ~a quarter second
    out.sort(key=lambda s: int(s.split(":")[0]))
    return ",".join(out)


def raw_to_rgb(path):
    d = open(path, "rb").read()
    need = 256 * 224 * 4
    if len(d) < need:
        raise ValueError("%s truncated" % path)
    out = bytearray(256 * 224 * 3)
    for i in range(0, need, 4):
        j = (i >> 2) * 3
        out[j] = d[i + 2]; out[j + 1] = d[i + 1]; out[j + 2] = d[i]
    return bytes(out)


def png_to_rgb(path):
    """Via ImageMagick rather than Pillow: this has to run wherever the
    runtime does, and a probe that cannot start reports nothing."""
    ppm = path + ".ppm"
    subprocess.run(["magick", path, "-depth", "8", "ppm:" + ppm],
                   check=True, capture_output=True)
    d = open(ppm, "rb").read()
    os.remove(ppm)
    tok, i = [], 2
    while len(tok) < 3:
        while d[i:i + 1].isspace():
            i += 1
        if d[i:i + 1] == b"#":
            while d[i:i + 1] != b"\n":
                i += 1
            continue
        j = i
        while not d[j:j + 1].isspace():
            j += 1
        tok.append(int(d[i:j])); i = j
    return tok[0], tok[1], d[i + 1:]


def diff(a, b):
    n = 0
    tot = 0
    for i in range(len(a)):
        v = a[i] - b[i]
        if v:
            tot += v if v > 0 else -v
            n += 1
    return n, tot / float(len(a))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--press", required=True,
                    help="button script, e.g. 2800:t,3000:t (letters: abxylr s=select t=start udnm=dpad)")
    ap.add_argument("--from", dest="start", type=int, required=True)
    ap.add_argument("--count", type=int, default=32)
    ap.add_argument("--out", default="analysis/ab")
    ap.add_argument("--rom", default="/mnt/crucial4tb/Emulation/roms/snes/"
                                     "Shin Kidou Senki Gundam W - Endless Duel (Japan).sfc")
    ap.add_argument("--exe", default="build-release/GundamWingEndlessDuelSNESRecomp")
    ap.add_argument("--port", type=int, default=4460)
    ap.add_argument("--offsets", type=int, default=3,
                    help="search +/- this many frames for boot-frame skew")
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    rec_dir = os.path.join(out, "recomp")
    mes_dir = os.path.join(out, "mesen")
    for d in (rec_dir, mes_dir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)

    lo = args.start - args.offsets
    hi = args.start + args.count + args.offsets
    rec_script = press_to_recomp(args.press)
    mes_script = press_to_mesen(args.press)
    say("recomp input script: %s" % rec_script)
    say("mesen  input script: %s" % mes_script)

    # ---- recomp ---------------------------------------------------------
    exe = os.path.abspath(args.exe)
    env = dict(os.environ)
    env.update(SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy",
               SNESRECOMP_DEBUG_PORT=str(args.port),
               SNESRECOMP_INPUT_SCRIPT=rec_script)
    rp = subprocess.Popen([exe], cwd=os.path.dirname(exe), env=env,
                          stdout=open(os.path.join(out, "recomp.log"), "wb"),
                          stderr=subprocess.STDOUT)
    try:
        d = connect(args.port, rp)
        # Wait for the target frame HERE rather than inside dump_frame_range:
        # the server's arm-and-block has a fixed ~16s budget, which is fine
        # for a frame a moment away and hopeless for one a minute into the
        # run. Arming early just times out and reports "already passed?",
        # which is a misleading error for "not reached yet".
        say("recomp: waiting for frame %d ..." % (lo - 60))
        while True:
            cur = int(d.json("freeze_status")["frame"])
            if cur >= lo - 60:
                break
            time.sleep(0.25)
        say("recomp: at frame %d, capturing %d..%d" % (cur, lo, hi))
        r = d.json("dump_frame_range %d %d %s" % (lo, hi - lo + 1, rec_dir))
        if "error" in r:
            raise SystemExit("recomp capture failed: %s" % r["error"])
        say("recomp: wrote %d frames" % r.get("written", 0))
    finally:
        rp.terminate()
        try:
            rp.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rp.kill()

    # ---- mesen ----------------------------------------------------------
    dumps = ",".join(str(f) for f in range(lo, hi + 1))
    menv = dict(os.environ)
    menv.update(GW_SCRIPT=mes_script, GW_DUMP=dumps, GW_DIR=mes_dir,
                GW_UNTIL=str(hi + 120))
    say("mesen: launching (a window will open; it closes itself)")
    mp = subprocess.Popen(["mesen-ce", args.rom,
                           os.path.join(TOOLS, "mesen_scripted_input.lua")],
                          env=menv,
                          stdout=open(os.path.join(out, "mesen.log"), "wb"),
                          stderr=subprocess.STDOUT)
    want = hi - lo + 1
    end = time.time() + 300
    while time.time() < end:
        have = len([n for n in os.listdir(mes_dir) if n.endswith(".png")])
        if have >= want:
            break
        if mp.poll() is not None:
            break
        time.sleep(1.0)
    time.sleep(1.5)
    mp.terminate()
    try:
        mp.wait(timeout=8)
    except subprocess.TimeoutExpired:
        mp.kill()
    have = sorted(n for n in os.listdir(mes_dir) if n.endswith(".png"))
    say("mesen: %d frames dumped" % len(have))
    if not have:
        raise SystemExit("mesen produced no frames — check AllowIoOsAccess "
                         "and %s/mesen.log" % out)

    # ---- compare --------------------------------------------------------
    rec = {}
    for n in os.listdir(rec_dir):
        if n.endswith(".raw"):
            rec[int(n[1:-4])] = raw_to_rgb(os.path.join(rec_dir, n))
    mes = {}
    for n in have:
        w, h, px = png_to_rgb(os.path.join(mes_dir, n))
        if (w, h) != (256, 224):
            raise SystemExit("mesen frame %s is %dx%d" % (n, w, h))
        mes[int(n[1:-4])] = px

    # ---- SCENE GATE ---------------------------------------------------
    # Refuse to compare two runs that are not on the same screen.
    #
    # Mesen's Lua frame counter starts when the SCRIPT loads, which lands a
    # couple of frames differently every launch, so "frame N" is not a stable
    # address into a run. Two captures taken with identical input can sit on
    # different SCENES entirely -- one on the VS screen, one already past it.
    # Diffing those produces confident, detailed, completely wrong findings;
    # it has done so repeatedly. A frame-number match is not evidence of
    # alignment, so this gate demands pixel evidence before any diff is read.
    gate = []
    for mf in sorted(mes):
        best = min(((diff(mes[mf], rec[rf])[1], rf) for rf in rec), default=None)
        if best:
            gate.append(best[0])
    if gate:
        floor = min(gate)
        say("scene gate: best achievable frame difference is %.1f mean abs" % floor)
        if floor > 40.0:
            say("")
            say("ABORT: no recomp frame resembles any mesen frame (floor %.1f)."
                % floor)
            say("  The two runs are on different SCENES, not different phases.")
            say("  Nothing below would be a rendering comparison, so it is not")
            say("  printed. Re-capture; do not adjust offsets to 'fix' this.")
            return 2

    say("")
    results = {}
    for off in range(-args.offsets, args.offsets + 1):
        pairs = [(f, f + off) for f in range(args.start, args.start + args.count)
                 if f in mes and (f + off) in rec]
        if len(pairs) < args.count // 2:
            continue
        exact = sum(1 for a, b in pairs if mes[a] == rec[b])
        tot = sum(diff(mes[a], rec[b])[1] for a, b in pairs) / len(pairs)
        results[off] = (exact, len(pairs), tot)
        say("  offset %+d : %d/%d frames byte-identical, mean abs diff %.3f"
            % (off, exact, len(pairs), tot))
    if not results:
        raise SystemExit("no overlapping frames to compare")

    best = min(results, key=lambda o: results[o][2])
    exact, npairs, mean = results[best]
    others = [v[2] for k, v in results.items() if k != best]
    margin = (min(others) / mean) if others and mean > 0 else float("inf")
    say("")
    say("best alignment: offset %+d — %d/%d frames byte-identical, mean %.3f"
        % (best, exact, npairs, mean))
    if margin < 1.5 and mean > 0.5:
        say("WARNING: no offset is clearly better than its neighbours "
            "(next best is only %.2fx worse)." % margin)
        say("  The two runs did not replay deterministically, so a per-frame")
        say("  diff is not meaningful. Do not read the numbers below as a")
        say("  rendering comparison.")
    say("")
    worst = sorted(((diff(mes[f], rec[f + best]), f) for f in
                    range(args.start, args.start + args.count)
                    if f in mes and (f + best) in rec), key=lambda t: -t[0][0])
    say("worst frames (mesen frame -> differing pixels, mean):")
    for (npix, mn), f in worst[:12]:
        say("   f%-7d %6d px   %.3f" % (f, npix, mn))
    with open(os.path.join(out, "verdict.json"), "w") as fh:
        json.dump({"offset": best, "exact": exact, "pairs": npairs,
                   "mean": mean, "press": args.press,
                   "worst": [{"frame": f, "pixels": n, "mean": m}
                             for (n, m), f in worst]}, fh, indent=1)
    say("")
    say("frames + verdict.json in %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
