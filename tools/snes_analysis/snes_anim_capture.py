#!/usr/bin/env python3
"""snes_anim_capture.py — burst-capture CONSECUTIVE frames of a live scene.

For a defect that lasts one frame: an animation whose last loop frame is
corrupt, a flash of the wrong palette, a sprite that tears on the frame its
tiles are re-uploaded. Those are the defects a screen recorder loses and a
single screenshot never lands on.

Why a dedicated tool rather than repeated `dump_frame_raw`: that command
returns at the moment frame N is recorded but polls on a 10 ms tick, and a
frame is 16.7 ms — so by the time the caller wakes, round-trips and arms
N+1, N+1 has already gone by. Every frame after the first times out. This
uses `dump_frame_range`, which is armed once and written by the emulation
thread as each frame passes. Nothing is paused; the game free-runs.

Typical use — you drive the game, this watches:

    # 1. start the TRACE build (the normal build has no debug server)
    ./build-release/GundamWingEndlessDuelSNESRecomp

    # 2. reach the scene, then, from another terminal:
    python3 snes_anim_capture.py --attach --count 120 --out analysis/anim

It writes fNNNNNN.png per frame, a contact sheet, and diff.json listing the
mean absolute difference between each frame and the one before it. In a clean
animation loop that series is periodic; the frame where it spikes out of
period is the corrupt one, named by number so you can go straight to it.

Requires SNESRECOMP_ENABLE_TRACE=ON and exclusive use of the debug port (the
server holds ONE client — close Studio's Diagnostics connection first).
"""

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def say(m):
    print(m, flush=True)


class Evicted(Exception):
    """The debug server dropped us; something else connected to the port."""


class Debug:
    def __init__(self, host, port, timeout=180.0):
        self.sock = socket.create_connection((host, port), 10.0)
        self.sock.settimeout(timeout)
        self.buf = b""

    def cmd(self, text):
        try:
            self.sock.sendall((text + "\n").encode())
        except OSError as exc:
            raise Evicted(str(exc))
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(1 << 20)
            except (OSError, socket.timeout) as exc:
                raise Evicted(str(exc))
            if not chunk:
                raise Evicted("server closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode(errors="replace")

    def json(self, text):
        raw = self.cmd(text)
        try:
            return json.loads(raw)
        except Exception as exc:
            raise ValueError("non-JSON reply to %r: %s (%.120s)" % (text, exc, raw))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def connect_when_ready(port, proc, deadline_s):
    """Open THE debug connection, retrying until the server answers `ping`.

    Not "poll the port, then connect": the server holds one client, so a
    throwaway connection made to test the port IS that client, and racing a
    second one against its teardown looks exactly like a dead runtime."""
    end = time.time() + deadline_s
    last = None
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise SystemExit("runtime exited before opening the debug port "
                             "(rc=%s)" % proc.returncode)
        try:
            dbg = Debug("127.0.0.1", port)
            if dbg.json("ping").get("ok"):
                return dbg
            dbg.close()
        except (OSError, Evicted, ValueError) as exc:
            last = str(exc)
        time.sleep(0.25)
    raise SystemExit("debug port %d never became usable (%s)" % (port, last))


def raw_to_rgb(path):
    """The capture is BGRX 256x224x4 straight from the PPU composite."""
    d = open(path, "rb").read()
    need = 256 * 224 * 4
    if len(d) < need:
        raise ValueError("%s is %d bytes, expected %d" % (path, len(d), need))
    out = bytearray(256 * 224 * 3)
    for i in range(0, need, 4):
        j = (i >> 2) * 3
        out[j] = d[i + 2]
        out[j + 1] = d[i + 1]
        out[j + 2] = d[i]
    return bytes(out)


def write_ppm(path, rgb):
    with open(path, "wb") as f:
        f.write(b"P6\n256 224\n255\n")
        f.write(rgb)


def mad(a, b):
    return sum(abs(a[i] - b[i]) for i in range(len(a))) / float(len(a))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--attach", action="store_true",
                    help="use an already-running runtime (you drive the game)")
    ap.add_argument("--exe", help="launch this runtime headless instead")
    ap.add_argument("--port", type=int, default=4370)
    ap.add_argument("--count", type=int, default=120,
                    help="consecutive frames to capture (default 120 = 2s)")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds to wait before arming, so you can get set")
    ap.add_argument("--loadstate", type=int, default=None,
                    help="load this slot first (headless runs)")
    ap.add_argument("--out", default="analysis/anim")
    args = ap.parse_args()

    if not args.attach and not args.exe:
        ap.error("give --attach (you drive) or --exe (headless)")

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)

    # Clear frames from a previous run. Leaving them turns the next capture
    # into a silent mixture of two scenes: the boundary between the old and
    # new frame numbers shows up as one enormous delta that outranks every
    # real finding, and the frame count in the summary is wrong. Only our own
    # output shape is removed.
    stale = [n for n in os.listdir(outdir)
             if (n.startswith("f") and (n.endswith(".raw") or n.endswith(".ppm")))
             or n == "diff.json"]
    for n in stale:
        os.remove(os.path.join(outdir, n))
    if stale:
        say("cleared %d file(s) from a previous run in %s" % (len(stale), outdir))

    proc = None
    if args.exe:
        exe = os.path.abspath(args.exe)
        env = dict(os.environ)
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "dummy"
        env["SNESRECOMP_DEBUG_PORT"] = str(args.port)
        proc = subprocess.Popen([exe], cwd=os.path.dirname(exe), env=env,
                                stdout=open(os.path.join(outdir, "runtime.log"), "wb"),
                                stderr=subprocess.STDOUT)
        say("launched %s headless on port %d" % (exe, args.port))

    dbg = connect_when_ready(args.port, proc, 90.0)
    try:
        if args.loadstate is not None:
            say("loadstate %d: %s" % (args.loadstate,
                                      dbg.cmd("loadstate %d" % args.loadstate)))
            time.sleep(1.5)
        if args.delay > 0:
            say("arming in %.1fs — get to the moment you want" % args.delay)
            time.sleep(args.delay)

        # Arm the VRAM write trace on the sprite tile region BEFORE capturing.
        # When a game animates by re-uploading tile data into fixed tile
        # indices -- which is what the OAM says is happening here: identical
        # slots and identical tile numbers across every phase, yet a different
        # picture -- the animation IS the VRAM stream, and nothing about it is
        # visible in OAM. The range comes from OBSEL rather than a constant so
        # this is right for whatever screen you are on.
        try:
            obsel = int(dbg.json("get_ppu_state")["obsel"], 16)
            objbase = (obsel & 7) * 0x4000          # 8K-word steps, in bytes
            lo, hi = objbase, min(objbase + 0x7FFF, 0xFFFF)
            dbg.cmd("trace_vram_reset")
            r = dbg.json("trace_vram %04x %04x" % (lo, hi))
            say("VRAM trace armed on OBJ tiles 0x%04X..0x%04X (OBSEL=0x%02X): %s"
                % (lo, hi, obsel, "ok" if "error" not in r else r["error"]))
        except (Evicted, ValueError, KeyError) as exc:
            say("could not arm the VRAM trace (%s) — frames only" % exc)

        base = int(dbg.json("freeze_status")["frame"]) + 12
        say("capturing frames %d..%d" % (base, base + args.count - 1))
        # The directory is created by us but resolved by the RUNTIME, so it
        # must be absolute — a relative path would land under the runtime's
        # own working directory instead.
        reply = dbg.json("dump_frame_range %d %d %s" % (base, args.count, outdir))
        if "error" in reply:
            raise SystemExit("capture failed: %s" % reply["error"])
        say("wrote %d frames" % reply.get("written", 0))

        # Pixels say a frame is wrong; these say why. The OAM render ring is
        # per-frame and always on, so the sprite table for the frames just
        # captured is still in it — tile index, palette, size and priority
        # for each sprite, which is what separates "wrong tile" from "wrong
        # palette" from "wrong blend" without another trip to the scene.
        # Smallest and most time-sensitive first. One oversized reply used to
        # break the connection partway through and take the rest of the
        # captures with it, and the one that got dropped was the journal.
        side = {"range_reply": reply}
        for name, cmd in (("raster_journal", "raster_journal"),
                          ("ppu", "get_ppu_state"),
                          ("dma", "get_dma_state"),
                          ("oam_state", "oam_state"),
                          ("cgram", "dump_cgram"),
                          ("oam", "dump_oam"),
                          ("oam_render", "oam_render_get 8 128")):
            try:
                side[name] = json.loads(dbg.cmd(cmd))
            except Exception as exc:
                side[name] = {"error": "%s: %s" % (cmd, exc)}
        # Page the VRAM write ring backwards. A single call returns only the
        # newest window that fits in the reply buffer -- about 1.5 frames when
        # a game streams ~2k bytes of tiles per frame -- which is not enough to
        # compare one phase of a 4-frame loop against the others. Walk back
        # until we hold a few complete frames or the ring runs out.
        vt_rows, vt_meta, seen_frames = [], None, set()
        try:
            cur = None
            for _ in range(12):
                q = "get_vram_trace nostack"
                if cur is not None:
                    if cur <= 0:
                        break
                    q += " idx_from=%d" % cur
                page = json.loads(dbg.cmd(q))
                if "error" in page:
                    vt_meta = page
                    break
                vt_meta = {k: v for k, v in page.items() if k != "log"}
                rows = page.get("log") or []
                if not rows:
                    break
                vt_rows = rows + vt_rows
                seen_frames |= {r.get("f") for r in rows if r.get("f") is not None}
                cur = int(page.get("idx_from", 0)) - len(rows)
                if len(seen_frames) >= 10:
                    break
        except (Evicted, ValueError, OSError) as exc:
            vt_meta = {"error": "%s (frames were captured; VRAM rows may be "
                                "partial)" % exc}
            say("VRAM paging stopped early: %s" % exc)
        side["vram_trace"] = dict(vt_meta or {}, log=vt_rows)

        with open(os.path.join(outdir, "sprite_state.json"), "w") as f:
            json.dump(side, f, indent=1)
        say("wrote sprite_state.json (OAM ring, CGRAM, PPU/DMA regs, journal,"
            " VRAM writes)")
        vt = side.get("vram_trace", {})
        # "log" holds the returned rows; "entries" is the ring's total count,
        # which is an int and was silently iterated as if it were the rows.
        rows = vt.get("log") or []
        if rows:
            byframe = {}
            for e in rows:
                byframe[e.get("f")] = byframe.get(e.get("f"), 0) + 1
            recent = sorted(k for k in byframe if k is not None)[-10:]
            say("OBJ-tile VRAM writes per frame (last %d of %d rows, ring holds %s):"
                % (len(recent), len(rows), vt.get("entries")))
            say("   " + ", ".join("%s:%d" % (f, byframe[f]) for f in recent))
            say("   A phase that uploads a different number of bytes than the")
            say("   others is the frame whose tiles are wrong.")
        elif "error" in vt:
            say("VRAM trace: %s" % vt["error"])
        else:
            say("VRAM trace returned no writes in the OBJ tile range.")
    finally:
        dbg.close()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    raws = sorted(f for f in os.listdir(outdir) if f.endswith(".raw"))
    if not raws:
        raise SystemExit("no frames on disk — nothing to analyse")
    say("decoding %d frames ..." % len(raws))
    frames = []
    for name in raws:
        rgb = raw_to_rgb(os.path.join(outdir, name))
        write_ppm(os.path.join(outdir, name[:-4] + ".ppm"), rgb)
        frames.append((int(name[1:-4]), rgb))

    diffs = []
    for i in range(1, len(frames)):
        diffs.append({"frame": frames[i][0],
                      "vs_prev": round(mad(frames[i - 1][1], frames[i][1]), 3)})
    with open(os.path.join(outdir, "diff.json"), "w") as f:
        json.dump(diffs, f, indent=1)

    # Refuse to present a dead capture as a result. A run of identical frames
    # ranked by a difference of 0.00 reads exactly like "I looked and found
    # nothing wrong", and that is the most expensive kind of wrong output a
    # probe can produce. It has already happened once: the capture was
    # re-rendering the PPU instead of copying the presented composite and
    # returned 120 byte-identical black frames off a game that was visibly
    # rendering.
    distinct = len(set(rgb for _, rgb in frames))
    blank = all(max(rgb) == 0 for _, rgb in frames)
    say("")
    if blank:
        say("NO SIGNAL: every captured frame is entirely black.")
        say("  The game may genuinely be blanked, but check the capture first:")
        say("  a runtime older than the dump_frame_range composite fix returns")
        say("  black for any game that ends its frame in forced blank.")
        say("  Cross-check with:  screenshot <abs path>.bmp")
        return 2
    if distinct <= 1:
        say("NO SIGNAL: all %d frames are byte-identical." % len(frames))
        say("  Nothing animated during the capture window. Either the game was")
        say("  paused/static, or the capture is not reading live pixels.")
        return 2

    vals = [d["vs_prev"] for d in diffs]
    mean = sum(vals) / len(vals)
    say("%d of %d frames distinct; per-frame change mean %.2f."
        % (distinct, len(frames), mean))
    say("Largest jumps — a corrupt frame in an otherwise periodic loop shows")
    say("up here:")
    for d in sorted(diffs, key=lambda x: -x["vs_prev"])[:10]:
        say("   frame %d   %.2f" % (d["frame"], d["vs_prev"]))
    say("")
    say("frames + diff.json in %s" % outdir)
    say("contact sheet:  magick montage %s/f*.ppm -tile 10x -geometry +2+2 "
        "-label %%t %s/sheet.png" % (outdir, outdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
