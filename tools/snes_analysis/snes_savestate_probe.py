#!/usr/bin/env python3
"""snes_savestate_probe.py — do save states actually round-trip?

"Save states don't seem to work" is not one claim, it is four, and they fail
in different places:

  NOT_WIRED     The host never calls RtlSaveLoad at all. The TCP command is
                accepted, nothing happens, and nothing on disk changes.
                -> no slot file appears after `savestate`.
  NOT_WRITTEN   RtlSaveLoad runs but RtlSaveSnapshot fails (no save dir, no
                permission, serializer refuses). -> command consumed, file
                absent or zero-length.
  NOT_RESTORED  The file is written and read back, but loading it does not
                move the guest — the snapshot is incomplete, or the load is
                dropped. -> the picture after `loadstate` still matches the
                later scene, not the saved one.
  WORKS         Loading visibly returns the guest to the saved scene.

The verdict comes from pixels, not from counters: a savestate that "loaded"
according to every instrument and left the screen on the wrong scene is the
exact failure this is looking for. Three screenshots are taken —

    A  right after `savestate <slot>`
    B  well after, once the scene has moved on   (must differ from A)
    C  right after `loadstate <slot>`            (must resemble A, not B)

— and compared by mean absolute pixel difference. B differing from A is a
control: if the scene did not move on its own, C resembling A proves nothing,
and the probe says INCONCLUSIVE rather than passing itself.

Usage:
    python3 snes_savestate_probe.py --exe build-release/GundamWingEndlessDuelSNESRecomp
    python3 snes_savestate_probe.py --port 4370 --attach     # already running

Requires a runtime built with SNESRECOMP_ENABLE_TRACE=ON, and exclusive access
to the debug port — the server holds ONE client.
"""

import argparse
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def say(msg):
    print(msg, flush=True)


class Evicted(Exception):
    """The debug server dropped us; something else connected to the port."""


class Debug:
    def __init__(self, host, port, timeout=20.0):
        self.sock = socket.create_connection((host, port), timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

    def cmd(self, text):
        self.sock.sendall((text + "\n").encode())
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(1 << 20)
            except (ConnectionResetError, socket.timeout) as exc:
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
            raise ValueError("non-JSON reply to %r: %s (%.100s)" % (text, exc, raw))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def read_bmp(path):
    """Minimal BMP reader -> (w, h, bytes of packed RGB), top-down.

    Handles the 24- and 32-bit bottom-up DIBs the debug server writes. Rows in
    a 24bpp BMP are padded to a 4-byte boundary; ignoring that pad shears the
    image by a pixel per row, which still compares "mostly equal" and would
    quietly weaken every threshold below.

    Deliberately not Pillow: this has to run wherever the runtime does, and a
    probe that cannot start because an image library is missing reports
    nothing, which reads as "no problem found"."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 54 or data[:2] != b"BM":
        raise ValueError("%s is not a BMP (%d bytes)" % (path, len(data)))
    off = struct.unpack_from("<I", data, 10)[0]
    w = struct.unpack_from("<i", data, 18)[0]
    h_signed = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    if bpp not in (24, 32):
        raise ValueError("%s is %d bpp; this reader handles 24 and 32"
                         % (path, bpp))
    h = abs(h_signed)
    bypp = bpp // 8
    stride = ((w * bypp + 3) // 4) * 4
    need = off + stride * h
    if len(data) < need:
        raise ValueError("%s is truncated (%d bytes, need %d)"
                         % (path, len(data), need))
    out = bytearray(w * h * 3)
    for y in range(h):
        # A positive height means the DIB is stored bottom-up.
        src_y = (h - 1 - y) if h_signed > 0 else y
        row = off + src_y * stride
        dst = y * w * 3
        for x in range(w):
            sp = row + x * bypp
            out[dst + x * 3 + 0] = data[sp + 2]   # BGR(A) -> RGB
            out[dst + x * 3 + 1] = data[sp + 1]
            out[dst + x * 3 + 2] = data[sp + 0]
    return w, h, bytes(out)


def mean_abs_diff(a, b):
    """Mean absolute per-channel difference, 0..255, over packed RGB."""
    wa, ha, pa = a
    wb, hb, pb = b
    if (wa, ha) != (wb, hb):
        raise ValueError("size mismatch %dx%d vs %dx%d" % (wa, ha, wb, hb))
    total = 0
    for i in range(len(pa)):
        total += abs(pa[i] - pb[i])
    return total / float(len(pa))


def connect_when_ready(port, proc, deadline_s):
    """Open THE debug connection, retrying until the server answers `ping`.

    Deliberately not "poll the port, then connect": the server holds exactly
    one client, and a throwaway connection made just to test the port is that
    client. Opening a second one while the server is still tearing the first
    one down loses the race often enough to look like a runtime that died on
    startup. So the first connection made is the one that is kept."""
    end = time.time() + deadline_s
    last = None
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise SystemExit("runtime exited before opening the debug port "
                             "(rc=%s)" % proc.returncode)
        try:
            dbg = Debug("127.0.0.1", port, timeout=5.0)
            reply = dbg.json("ping")
            if reply.get("ok"):
                return dbg
            dbg.close()
            last = "ping answered %r" % reply
        except (OSError, Evicted, ValueError) as exc:
            last = str(exc)
        time.sleep(0.25)
    raise SystemExit("debug port %d never became usable (%s)" % (port, last))


def frame_now(dbg):
    st = dbg.json("freeze_status")
    for key in ("frame", "snes_frame_counter", "frame_counter"):
        if key in st:
            return int(st[key])
    raise ValueError("freeze_status carries no frame number: %r" % st)


def run_to_frame(dbg, target, timeout_s=120.0):
    end = time.time() + timeout_s
    last = None
    while time.time() < end:
        f = frame_now(dbg)
        if f >= target:
            return f
        if last is not None and f == last:
            time.sleep(0.5)
            if frame_now(dbg) == f:
                raise SystemExit("guest stopped advancing at frame %d — the "
                                 "save-state question cannot be answered "
                                 "until it runs" % f)
        last = f
        time.sleep(0.25)
    raise SystemExit("frame %d not reached within %.0fs" % (target, timeout_s))


def shoot(dbg, workdir, name):
    """Take a screenshot and read it back. The server writes relative to its
    own cwd, which is why the runtime is launched with cwd=workdir."""
    path = os.path.join(workdir, name)
    if os.path.exists(path):
        os.remove(path)
    reply = dbg.json("screenshot %s" % name)
    if "error" in reply:
        raise SystemExit("screenshot failed: %s" % reply["error"])
    for _ in range(40):
        if os.path.exists(path) and os.path.getsize(path) > 54:
            break
        time.sleep(0.05)
    else:
        raise SystemExit("screenshot %s never appeared (server said %r)"
                         % (path, reply))
    return path


def slot_files(workdir, slot):
    saves = os.path.join(workdir, "saves")
    if not os.path.isdir(saves):
        return []
    return sorted(os.path.join(saves, n) for n in os.listdir(saves)
                  if ("%d.sav" % slot) in n)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exe", help="runtime to launch (omit with --attach)")
    ap.add_argument("--attach", action="store_true",
                    help="use an already-running runtime on --port")
    ap.add_argument("--port", type=int, default=4370)
    ap.add_argument("--slot", type=int, default=0)
    ap.add_argument("--save-frame", type=int, default=600,
                    help="frame to take the state at")
    ap.add_argument("--gap", type=int, default=600,
                    help="frames to let the scene move on before loading back")
    ap.add_argument("--outdir", default="analysis/savestate_probe")
    ap.add_argument("--keep-running", action="store_true")
    args = ap.parse_args()

    if not args.attach and not args.exe:
        ap.error("give --exe, or --attach to use a running runtime")

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)

    proc = None
    if args.attach:
        workdir = os.getcwd()
        say("attaching to 127.0.0.1:%d (cwd assumed %s)" % (args.port, workdir))
    else:
        exe = os.path.abspath(args.exe)
        if not os.path.exists(exe):
            raise SystemExit("no such executable: %s" % exe)
        workdir = os.path.dirname(exe)
        env = dict(os.environ)
        env["SDL_VIDEODRIVER"] = "dummy"     # also skips the GUI launcher
        env["SDL_AUDIODRIVER"] = "dummy"
        env["SNESRECOMP_DEBUG_PORT"] = str(args.port)
        say("launching %s (headless, port %d)" % (exe, args.port))
        log_path = os.path.join(outdir, "runtime.log")
        log = open(log_path, "wb")
        proc = subprocess.Popen([exe], cwd=workdir, env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        say("  runtime output -> %s" % log_path)

    # A slot left over from a previous run would make NOT_WIRED look like a
    # pass, so clear it and prove it is gone before asking for a new one.
    stale = slot_files(workdir, args.slot)
    for p in stale:
        os.remove(p)
    if stale:
        say("cleared %d stale slot file(s)" % len(stale))

    dbg = connect_when_ready(args.port, proc, 90.0)
    findings = {}
    try:
        say("waiting for frame %d ..." % args.save_frame)
        f0 = run_to_frame(dbg, args.save_frame)
        say("at frame %d — requesting savestate %d" % (f0, args.slot))
        say("  server: %s" % dbg.cmd("savestate %d" % args.slot))
        a = shoot(dbg, workdir, "ssprobe_a.bmp")

        # The write happens on a frame boundary, so give it frames, not just
        # wall time, to land.
        run_to_frame(dbg, f0 + 30)
        written = slot_files(workdir, args.slot)
        sizes = [(os.path.basename(p), os.path.getsize(p)) for p in written]
        findings["slot_files"] = sizes
        say("slot files after save: %s" % (sizes or "NONE"))
        if not written:
            findings["verdict"] = "NOT_WIRED"
            findings["why"] = ("`savestate %d` was accepted but no slot file "
                               "appeared. The host is not consuming "
                               "debug_server_consume_savestate(), or "
                               "RtlSaveLoad is never reached."
                               % args.slot)
            return report(findings, outdir, None)
        if all(sz == 0 for _, sz in sizes):
            findings["verdict"] = "NOT_WRITTEN"
            findings["why"] = ("the slot file exists but is empty — "
                               "RtlSaveSnapshot ran and produced nothing.")
            return report(findings, outdir, None)

        say("letting the scene move on %d frames ..." % args.gap)
        f1 = run_to_frame(dbg, f0 + args.gap)
        b = shoot(dbg, workdir, "ssprobe_b.bmp")

        say("at frame %d — requesting loadstate %d" % (f1, args.slot))
        say("  server: %s" % dbg.cmd("loadstate %d" % args.slot))
        run_to_frame(dbg, f1 + 4)
        c = shoot(dbg, workdir, "ssprobe_c.bmp")

        ia, ib, ic = read_bmp(a), read_bmp(b), read_bmp(c)
        d_ab = mean_abs_diff(ia, ib)
        d_ac = mean_abs_diff(ia, ic)
        d_bc = mean_abs_diff(ib, ic)
        findings["diff_a_b"] = round(d_ab, 3)
        findings["diff_a_c"] = round(d_ac, 3)
        findings["diff_b_c"] = round(d_bc, 3)
        say("mean abs pixel diff:  A~B %.2f   A~C %.2f   B~C %.2f"
            % (d_ab, d_ac, d_bc))

        for src, dst in ((a, "A_at_save.bmp"), (b, "B_later.bmp"),
                         (c, "C_after_load.bmp")):
            shutil.copyfile(src, os.path.join(outdir, dst))

        # The control first: if the scene never moved, C resembling A is
        # meaningless and the probe must not award itself a pass.
        if d_ab < 2.0:
            findings["verdict"] = "INCONCLUSIVE"
            findings["why"] = ("the scene did not change between the save "
                               "(frame %d) and %d frames later (A~B = %.2f), "
                               "so 'the picture came back' cannot distinguish "
                               "a working load from no load at all. Re-run "
                               "with --save-frame on an animated scene."
                               % (f0, args.gap, d_ab))
        elif d_ac < d_ab * 0.25:
            findings["verdict"] = "WORKS"
            findings["why"] = ("after loading, the picture returned to the "
                               "saved scene: A~C = %.2f against a A~B "
                               "baseline of %.2f." % (d_ac, d_ab))
        elif d_bc < d_ab * 0.25:
            findings["verdict"] = "NOT_RESTORED"
            findings["why"] = ("after loading, the picture still matched the "
                               "later scene (B~C = %.2f) rather than the "
                               "saved one (A~C = %.2f). The file was written "
                               "but loading it did not move the guest."
                               % (d_bc, d_ac))
        else:
            findings["verdict"] = "PARTIAL"
            findings["why"] = ("after loading, the picture matched neither "
                               "the saved scene (A~C = %.2f) nor the later "
                               "one (B~C = %.2f). The snapshot is restoring "
                               "some state and not the rest."
                               % (d_ac, d_bc))
        return report(findings, outdir, (a, b, c))
    finally:
        dbg.close()
        if proc is not None and not args.keep_running:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def report(findings, outdir, shots):
    path = os.path.join(outdir, "verdict.json")
    with open(path, "w") as f:
        json.dump(findings, f, indent=2)
    say("")
    say("VERDICT: %s" % findings.get("verdict", "NO VERDICT REACHED"))
    say("  %s" % findings.get("why", "(the probe did not finish)"))
    say("")
    say("wrote %s" % path)
    if shots:
        say("screenshots in %s" % outdir)
    return 0 if findings.get("verdict") == "WORKS" else 1


if __name__ == "__main__":
    sys.exit(main())
