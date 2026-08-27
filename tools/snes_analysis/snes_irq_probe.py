#!/usr/bin/env python3
"""snes_irq_probe.py — why is the raster IRQ chain not firing?

`snes_loop_compare.py` can show that a chain ran on one pass of a scene and not
on the next. That is a symptom with several distinct causes, and they are not
distinguishable from the handler counts alone. This samples the IRQ machinery
itself and reports which gate is actually shut.

The candidates, and the observation that separates each from the others:

  ARMED_OFF     NMITIMEN's IRQ bits are clear, so the comparator never runs.
                -> hIrqEnabled and vIrqEnabled both false.
  BEAM_FROZEN   The beam is not advancing, so it can never cross the timer
                line. -> vPos/hPos stop changing.
  IRQ_STUCK     A latched IRQ was never acknowledged. The beam walk refuses to
                advance while one is pending, so this presents as BEAM_FROZEN
                with the extra fact that inIrq is stuck true.
  TARGET_UNREACHABLE  vTimer names a line outside the field, so the comparator
                is armed but can never match. -> vTimer >= 262.
  FIRING        IRQs are latching normally; the chain's absence is downstream
                (dispatch, or the handler itself), not in the machinery.

The verdict is computed from what was observed, never assumed. Where two
causes share a signature the report says so rather than picking one.

Also traces writes to $4200 (NMITIMEN), because "the game disarmed it on
purpose and we failed to re-arm" and "we lost the arm" look identical in a
snapshot and completely different in a write log.

Usage:
    python3 snes_irq_probe.py --port 4370 --seconds 20
    python3 snes_irq_probe.py --port 4370 --watch-selector 0010 --watch-value 2

Requires a runtime built with SNESRECOMP_ENABLE_TRACE=ON, and exclusive access
to the debug port — the server holds ONE client (see the Loops pane, which
releases Studio's own connection for the duration).
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


def verdict(obs):
    """Name the shut gate from the observations, or say why it cannot be named.

    Deliberately returns a list: where the evidence genuinely does not
    separate two causes, reporting one of them would be a guess wearing a
    verdict's clothes.
    """
    out = []
    if not obs["ever_armed"]:
        out.append(("ARMED_OFF",
                    "NMITIMEN's IRQ bits were clear for the whole window "
                    "(hIrq=%d/%d samples, vIrq=%d/%d) — the comparator never "
                    "ran. Look at who wrote $4200."
                    % (obs["h_armed"], obs["n"], obs["v_armed"], obs["n"])))
        return out

    if obs["beam_moved"] == 0 and obs["n"] > 1:
        if obs["inirq_true"] == obs["n"]:
            out.append(("IRQ_STUCK",
                        "The beam never advanced AND inIrq was true in every "
                        "sample: a latched IRQ was never acknowledged, and the "
                        "walk will not advance past a pending one."))
        else:
            out.append(("BEAM_FROZEN",
                        "The beam never advanced (vPos stayed %d) though no IRQ "
                        "was pending — emulated time is not reaching the PPU."
                        % obs["last_vpos"]))
        return out

    if obs["max_vtimer"] >= 262:
        out.append(("TARGET_UNREACHABLE",
                    "vTimer reached %d, outside the 262-line field: armed, but "
                    "it can never match." % obs["max_vtimer"]))

    if obs["missed_matches"] > 0:
        out.append(("MATCH_MISSED",
                    "%d field sweeps completed with the comparator armed and "
                    "no latch — targets %s. The beam is stepping OVER the match "
                    "point: snes_advance_beam's test is a window (target inside "
                    "[h, h+span)), so a step wider than the remaining distance "
                    "yields no IRQ at all rather than a late one."
                    % (obs["missed_matches"], obs["missed_targets"])))

    if obs["inirq_true"] > 0:
        out.append(("FIRING",
                    "IRQs latched %d times in %d samples — the machinery works. "
                    "If handlers still do not run, the fault is downstream: "
                    "dispatch, or the handler itself."
                    % (obs["inirq_true"], obs["n"])))
    elif not out:
        out.append(("ARMED_BUT_SILENT",
                    "Armed (vTimer=%s) and the beam is moving, yet inIrq was "
                    "never observed true in %d samples. Either the match is "
                    "being stepped over, or it is serviced faster than this "
                    "probe samples — raise --rate before concluding."
                    % (obs["vtimers"], obs["n"])))
    return out


def run(dbg, args, out_path):
    say("tracing $4200 (NMITIMEN) and $4207-$420A (raster timer) ...")
    dbg.cmd("trace_reg_reset")
    dbg.cmd("trace_reg 4200 4200")
    dbg.cmd("trace_reg 4207 420a")

    sel_addr = int(args.watch_selector, 16) if args.watch_selector else None
    if sel_addr is not None:
        say("waiting for selector $%04X == 0x%02X ..." % (sel_addr, args.watch_value))
        deadline = time.time() + args.timeout
        seen = collections.Counter()
        while time.time() < deadline:
            v = int(dbg.json("read_ram %x 1" % sel_addr)["hex"][:2], 16)
            seen[v] += 1
            if v == args.watch_value:
                break
            time.sleep(0.05)
        else:
            say("   selector never reached 0x%02X; it read [%s]. Probing anyway."
                % (args.watch_value, ", ".join("0x%02X" % k for k in sorted(seen))))

    start_frame = dbg.json("frame")["frame"]
    say("sampling for %ds from frame %d ..." % (args.seconds, start_frame))

    obs = {"n": 0, "h_armed": 0, "v_armed": 0, "inirq_true": 0,
           "beam_moved": 0, "last_vpos": -1, "max_vtimer": -1,
           "ever_armed": False, "vtimers": "",
           "missed_matches": 0, "missed_targets": collections.Counter()}
    prev_state = None
    latched_since_wrap = False
    vtimers = collections.Counter()
    vpos_seen = set()
    samples = []
    prev_beam = None
    t_start = time.time()
    end = t_start + args.seconds
    while time.time() < end:
        st = dbg.json("get_interrupt_state")
        obs["n"] += 1
        if st["hIrqEnabled"]:
            obs["h_armed"] += 1
        if st["vIrqEnabled"]:
            obs["v_armed"] += 1
        if st["hIrqEnabled"] or st["vIrqEnabled"]:
            obs["ever_armed"] = True
        if st["inIrq"]:
            obs["inirq_true"] += 1
        beam = (st["vPos"], st["hPos"])
        if prev_beam is not None and beam != prev_beam:
            obs["beam_moved"] += 1
        prev_beam = beam
        obs["last_vpos"] = st["vPos"]
        vpos_seen.add(st["vPos"])
        vtimers[st["vTimer"]] += 1
        obs["max_vtimer"] = max(obs["max_vtimer"], st["vTimer"])

        # Missed-match detection. If the comparator is armed for line T and the
        # beam WRAPS the field (vPos goes backwards = new frame) while T stayed
        # armed and nothing latched, then the beam swept past T without firing.
        # That is a lost interrupt, not a pending one, and it is invisible to
        # any snapshot: armed + moving + never fires looks identical to
        # "the scene simply has no split there".
        if prev_state is not None:
            wrapped = beam[0] < prev_state["vPos"]
            same_target = st["vTimer"] == prev_state["vTimer"]
            armed_now = st["hIrqEnabled"] or st["vIrqEnabled"]
            if wrapped and same_target and armed_now and not latched_since_wrap:
                obs["missed_matches"] += 1
                obs["missed_targets"][st["vTimer"]] += 1
            if wrapped:
                latched_since_wrap = False
        if st["inIrq"]:
            latched_since_wrap = True
        prev_state = st
        if len(samples) < 400:
            samples.append(st)
        time.sleep(max(0.0, 1.0 / args.rate))

    obs["vtimers"] = ", ".join("%d x%d" % (k, n) for k, n in vtimers.most_common(5))
    obs["elapsed"] = max(1, int(time.time() - t_start))
    obs["distinct_vpos"] = len(vpos_seen)
    obs["missed_targets"] = dict(obs["missed_targets"])
    end_frame = dbg.json("frame")["frame"]

    regs = dbg.json("get_reg_trace nostack").get("log", [])
    window = [e for e in regs if start_frame <= e.get("f", -1) <= end_frame]
    nmitimen = [e for e in window if e["adr"] == "0x4200"]
    writers = collections.Counter((e["adr"], e.get("func", "?")) for e in window)

    say("")
    say("frames %d..%d, %d samples" % (start_frame, end_frame, obs["n"]))
    say("  armed:        hIrq %d/%d, vIrq %d/%d"
        % (obs["h_armed"], obs["n"], obs["v_armed"], obs["n"]))
    say("  inIrq true:   %d/%d samples" % (obs["inirq_true"], obs["n"]))
    say("  beam moved:   %d transitions, %d distinct scanlines"
        % (obs["beam_moved"], obs["distinct_vpos"]))
    say("  vTimer:       %s" % obs["vtimers"])
    targets = ", ".join("line %d x%d" % (k, n)
                        for k, n in obs["missed_targets"].most_common(5))
    say("  MISSED (polled): %d field sweeps ended armed with nothing latched%s"
        % (obs["missed_matches"], ("  targets: " + targets) if targets else ""))
    say("      NOTE: polled over TCP at ~%d samples/sec against 60 fps, so this "
        "cannot see every field. Trust the runtime's own counters below."
        % (obs["n"] // max(1, obs["elapsed"])))
    say("  $4200 writes: %d" % len(nmitimen))
    for e in nmitimen[:8]:
        say("      f=%s = %s by %s" % (e.get("f"), e.get("val"), e.get("func", "?")))
    say("  timer writers: %s"
        % ", ".join("%s by %s x%d" % (a, f, n) for (a, f), n in writers.most_common(5)))

    verdicts = verdict(obs)
    say("")
    say("VERDICT:")
    for name, why in verdicts:
        say("  [%s] %s" % (name, why))

    result = {
        "frame_window": [start_frame, end_frame],
        "observations": obs,
        "verdicts": [{"gate": n, "why": w} for n, w in verdicts],
        "nmitimen_writes": [{"frame": e.get("f"), "value": e.get("val"),
                             "func": e.get("func", "?")} for e in nmitimen],
        "timer_writers": [{"reg": a, "func": f, "count": n}
                          for (a, f), n in writers.most_common(12)],
        "samples": samples[:400],
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(result, fh, indent=2)
    os.replace(tmp, out_path)
    say("\nwrote %s" % out_path)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4370)
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--rate", type=float, default=30.0, help="samples/second")
    ap.add_argument("--watch-selector", default=None,
                    help="wait for this WRAM byte (hex) before sampling")
    ap.add_argument("--watch-value", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--out", default="analysis/diagnostics")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "irq_probe.json")
    try:
        dbg = Debug(args.host, args.port)
    except Exception as exc:
        say("cannot reach the runtime on %s:%d — %s" % (args.host, args.port, exc))
        say("a build without SNESRECOMP_ENABLE_TRACE opens no debug port.")
        return 2
    try:
        return run(dbg, args, path)
    except Evicted as exc:
        say("evicted: %s" % exc)
        say("The debug server holds ONE client. Disconnect Studio's Diagnostics "
            "tab, or drive this from the pane, which releases it for you.")
        return 3
    finally:
        dbg.close()


if __name__ == "__main__":
    sys.exit(main())
