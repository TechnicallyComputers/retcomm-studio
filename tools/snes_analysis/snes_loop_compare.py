#!/usr/bin/env python3
"""snes_loop_compare.py — compare the SAME scene across successive attract loops.

The bug shape this exists for: a scene renders correctly the first time the
attract loop reaches it and wrongly every time after. A single-shot capture
cannot see that — both captures look like "the demo", and whichever one you
took is the one you believe. This walks the loop, detects each entry into the
target scene, captures an identical battery of state at each, and diffs them.

Scene detection is by a WRAM selector rather than a frame number, because the
loops drift: `--sel-addr` (default $0010, the NMI dispatch index this title
uses) entering `--sel-value` (default 2) marks scene entry.

What it captures per visit, and why each one is here:

  irq_chain     per-handler execution counts over the visit. A raster-split
                title's chain is the thing that programs mid-frame state; if
                it runs on visit 1 and not visit 2, nothing downstream matters.
  cgram         the live palette, plus a census of the most-repeated entry.
                A value repeated across unrelated palettes is a FILL — stale
                data the scene never re-uploaded.
  shadow        the WRAM page the palette is DMA'd from. CGRAM holding a value
                the shadow does not is proof the upload stopped, not that the
                shadow was corrupted.
  reg_writers   which guest function wrote $2100/$2121/$2122 during the visit,
                so a missing upload names its caller.
  dp            direct page $00-$3F: the selectors and flags the chain reads.
  ppu / irq     the register file and comparator state at the sample point.

Nothing here decides what is wrong. It puts visit 1 and visit 2 side by side
and marks what differs; the reading is yours.

Usage:
    python3 snes_loop_compare.py --port 4370 --visits 2 --out analysis/diagnostics

Requires a runtime built with SNESRECOMP_ENABLE_TRACE=ON.
"""

import argparse
import collections
import json
import os
import socket
import sys
import time


# Studio captures this tool's stdout through a pipe, and Python block-buffers
# a pipe — so without this every progress line sits in the buffer until the
# process exits, and a long run looks indistinguishable from a hang. Measured
# exactly that: a run that was working showed nothing for minutes.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def say(msg):
    print(msg, flush=True)


class SingleClientEvicted(Exception):
    """The debug server dropped us because something else connected.

    snesrecomp's server holds exactly ONE client: accept() closes the previous
    socket ("New client; dropping previous"). So this tool cannot share a
    runtime with Studio's live connection — they evict each other in a loop and
    whichever spoke last wins. Studio releases its client around this tool for
    that reason; anything else holding the port must be closed first.
    """


class Debug:
    """One connection, one command at a time.

    snesrecomp's protocol is a bare line in, one JSON line out, with no request
    ids — so replies can only be matched to commands by keeping the exchange
    strictly serial. Reusing the socket is also what makes two reads describe
    the same moment rather than two moments a reconnect apart.
    """

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
                raise SingleClientEvicted(str(exc))
            if not chunk:
                raise SingleClientEvicted("server closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode(errors="replace")

    def json(self, text):
        raw = self.cmd(text)
        try:
            return json.loads(raw)
        except Exception as exc:
            raise ValueError("non-JSON reply to %r: %s (%.120s)"
                             % (text, exc, raw))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# The IRQ dispatch chain. Addresses are this title's; override with --chain.
# How many of the newest ring entries to examine per visit. The ring holds
# 262144; a visit is ~90 frames, and 40k entries covers that comfortably while
# keeping the reply small enough to parse quickly.
RING_WINDOW = 40000

DEFAULT_CHAIN = {
    "0x008860": "irq_entry",
    "0x00888e": "h1_line21",
    "0x0088e7": "h2_line23",
    "0x008900": "h3_line71",
    "0x00891f": "h4_line72",
    "0x008950": "h5_line215",
}


def byte_at(dbg, addr):
    return int(dbg.json("read_ram %x 1" % addr)["hex"][:2], 16)


def read_page(dbg, addr, count=256):
    """count 16-bit words starting at addr."""
    hexs = dbg.json("read_ram %x %d" % (addr, count * 2))["hex"].split()
    if len(hexs) < count * 2:          # some builds answer unsplit
        raw = dbg.json("read_ram %x %d" % (addr, count * 2))["hex"]
        hexs = [raw[i:i + 2] for i in range(0, len(raw), 2)]
    return [int(hexs[i + 1], 16) << 8 | int(hexs[i], 16)
            for i in range(0, min(len(hexs), count * 2) - 1, 2)]


def cgram_words(dbg):
    raw = dbg.json("dump_cgram")["hex"]
    b = bytes.fromhex("".join(raw.split()))
    return [b[i] | b[i + 1] << 8 for i in range(0, 512, 2)]


def census(words):
    c = collections.Counter(words)
    top, n = c.most_common(1)[0] if c else (0, 0)
    return {
        "distinct": len(c),
        "zero": c.get(0, 0),
        "top_value": "0x%04X" % top,
        "top_count": n,
        # 16 is a whole palette's worth: past that, "repeated" stops being
        # plausible art and starts being a fill.
        "looks_like_fill": bool(n >= 16),
    }


def arm(dbg, chain):
    lo = min(int(k, 16) for k in chain)
    hi = max(int(k, 16) for k in chain) + 2
    out = {}
    # Both range tables accumulate server-side across runs, and the block table
    # holds only BLOCK_TRACE_RANGE_MAX=8 entries — the 5th run of this tool
    # would have failed with "range table full". A stale reg range also
    # silently widens every later census. Start both clean.
    out["reset_reg"] = dbg.cmd("trace_reg_reset")
    out["reset_blocks"] = dbg.cmd("trace_blocks_reset")
    out["blocks"] = dbg.cmd("trace_blocks_range %06x %06x" % (lo, hi))
    # trace_blocks_reset clears active; trace_blocks_range does NOT set it
    # again — only the plain command does. Without this the ring stays off and
    # every chain count reads 0, which looks exactly like "the chain never
    # ran". Measured: a run reported irq_entry=0 for both visits purely
    # because the instrument was dead.
    out["enable"] = dbg.cmd("trace_blocks")
    out["reg"] = dbg.cmd("trace_reg 2100 2100")
    out["reg2"] = dbg.cmd("trace_reg 2121 2122")
    return out, lo, hi


def capture(dbg, chain, lo, hi, frame_lo, frame_hi):
    """Everything for one visit, sampled at the end of its window."""
    # get_block_trace paginates OLDEST-FIRST: idx_from defaults to 0, so a
    # plain query returns the START of the ring and silently omits everything
    # recent. That is a false-negative generator, not a truncation you notice —
    # it reported "the IRQ chain never runs on loop 2" for three sessions when
    # the chain was running fine and simply lived past the returned slice.
    # Page to the END: ask for the count, then read the last window.
    total = dbg.json("get_block_trace idx_lim=1").get("entries", 0)
    idx_from = max(0, total - RING_WINDOW)
    raw = dbg.json("get_block_trace pc_lo=%06x pc_hi=%06x idx_from=%d idx_lim=%d"
                   % (lo, hi, idx_from, RING_WINDOW))
    blocks = raw.get("log", [])
    # A dead instrument reports zero exactly like a chain that never ran, and
    # the second reading is a finding while the first is a bug. `entries` counts
    # the WHOLE ring, so a populated ring with nothing in our range is a real
    # zero; an empty ring means the trace is off and the zero means nothing.
    ring_entries = raw.get("entries", 0)
    seen = collections.Counter(
        e.get("pc") for e in blocks if frame_lo <= e.get("f", -1) <= frame_hi)
    irq_chain = {label: seen.get(pc, 0) for pc, label in chain.items()}

    regs = dbg.json("get_reg_trace nostack").get("log", [])
    # get_reg_trace has no pagination controls at all — it returns what fits in
    # the server's reply buffer. If NONE of what came back falls inside our
    # frame window, the ring has already moved past it and the writer census
    # below would read as "nothing wrote this", which is a lie. Say so instead.
    reg_frames = [e.get("f", -1) for e in regs]
    reg_covers_window = bool(reg_frames) and max(reg_frames) >= frame_lo
    in_window = [e for e in regs if frame_lo <= e.get("f", -1) <= frame_hi]
    writers = collections.Counter(
        (e["adr"], e.get("func", "?")) for e in in_window)
    reg_writers = [{"reg": a, "func": f, "count": n}
                   for (a, f), n in writers.most_common(12)]
    per_frame = collections.Counter(
        e["f"] for e in in_window if e["adr"] == "0x2122")

    cg = cgram_words(dbg)
    shadow = read_page(dbg, 0x900, 128)
    top = census(cg)["top_value"]
    top_v = int(top, 16)

    return {
        "frame_window": [frame_lo, frame_hi],
        "irq_chain": irq_chain,
        "block_ring_entries": ring_entries,
        "reg_trace_covers_window": reg_covers_window,
        # False => every irq_chain zero above is meaningless, not a finding.
        "irq_chain_trustworthy": bool(ring_entries > 0),
        "cgram": census(cg),
        "shadow_0900": census(shadow),
        # The discriminator: CGRAM holding a value its source page does not
        # means the upload stopped, not that the source was corrupted.
        "cgram_top_present_in_shadow": bool(top_v in shadow),
        "cgram_upload_frames": len(per_frame),
        "cgram_bytes_per_frame": dict(collections.Counter(per_frame.values())),
        "reg_writers": reg_writers,
        "dp": dbg.json("read_ram 0 64")["hex"],
        "ppu": dbg.json("get_ppu_state"),
        "irq": dbg.json("get_interrupt_state"),
        "unresolved": dbg.json("unresolved_stub_get"),
    }


def wait_for_scene(dbg, sel_addr, sel_value, want_in_scene, timeout_s, what):
    """Block until the selector enters (or leaves) the target scene.

    Reports progress while it waits. A silent wait cannot be told apart from a
    hang, and a selector that never matches (wrong address, wrong value) looks
    identical to a scene that has not come round yet — so the heartbeat names
    every value the selector HAS taken, which is what tells those apart.
    """
    deadline = time.time() + timeout_s
    seen = collections.Counter()
    last = 0.0
    while time.time() < deadline:
        val = byte_at(dbg, sel_addr)
        seen[val] += 1
        if (val == sel_value) == want_in_scene:
            return dbg.json("frame")["frame"]
        now = time.time()
        if now - last >= 3.0:
            last = now
            frame = dbg.json("frame")["frame"]
            values = ", ".join("0x%02X x%d" % (v, n)
                               for v, n in seen.most_common(4))
            say("   %s ... frame %d, selector $%04X has been [%s], want 0x%02X"
                % (what, frame, sel_addr, values, sel_value))
        time.sleep(0.05)
    say("   TIMED OUT after %ds. Selector $%04X only ever read [%s] — if 0x%02X "
        "is not among them the selector is wrong, not the scene."
        % (timeout_s, sel_addr,
           ", ".join("0x%02X" % v for v in sorted(seen)), sel_value))
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4370)
    ap.add_argument("--visits", type=int, default=2,
                    help="how many times to catch the scene (default 2)")
    ap.add_argument("--sel-addr", default="0010",
                    help="WRAM selector, hex (default 0010)")
    ap.add_argument("--sel-value", type=int, default=2,
                    help="selector value marking the scene (default 2)")
    ap.add_argument("--settle", type=int, default=90,
                    help="frames to let the scene settle before sampling")
    ap.add_argument("--timeout", type=int, default=240,
                    help="seconds to wait for each visit")
    ap.add_argument("--out", default="analysis/diagnostics")
    args = ap.parse_args()

    sel_addr = int(args.sel_addr, 16)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "loop_compare.json")

    try:
        dbg = Debug(args.host, args.port)
    except Exception as exc:
        say("cannot reach the runtime on %s:%d — %s" % (args.host, args.port, exc))
        say("a build without SNESRECOMP_ENABLE_TRACE opens no debug port.")
        return 2

    chain = DEFAULT_CHAIN
    try:
        armed, lo, hi = arm(dbg, chain)
    except SingleClientEvicted as exc:
        say("evicted while arming: %s" % exc)
        say(EVICTED_HELP)
        return 3
    say("armed: %s" % json.dumps(armed)[:160])

    result = {
        "selector": {"addr": "0x%04X" % sel_addr, "value": args.sel_value},
        "visits": [],
    }

    try:
        run_visits(dbg, args, sel_addr, chain, lo, hi, result, path)
    except SingleClientEvicted as exc:
        say("\nevicted mid-run: %s" % exc)
        say(EVICTED_HELP)
        if not result["visits"]:
            return 3
        say("keeping the %d visit(s) captured before the eviction."
              % len(result["visits"]))

    return finish(result, path, dbg)


EVICTED_HELP = """
The runtime's debug server holds ONE client at a time — a new connection closes
the previous one (debug_server.c: "New client; dropping previous"). Something
else is connected to this port, so the two of us kept evicting each other.

  * From Studio: the Loops pane releases Studio's own connection while this
    runs and restores it afterwards; use the button rather than a terminal.
  * From a terminal: press Disconnect in Studio's Diagnostics tab first, or
    point --port at a runtime Studio is not attached to.
"""


def run_visits(dbg, args, sel_addr, chain, lo, hi, result, path):
    for visit in range(args.visits):
        # Leave the scene first, so visit N+1 is a genuine re-entry rather
        # than the tail of visit N.
        if visit:
            say("waiting for the scene to end ...")
            if wait_for_scene(dbg, sel_addr, args.sel_value, False,
                              args.timeout, "scene still on") is None:
                say("timed out waiting to leave the scene")
                break
        say("waiting for scene entry (visit %d of %d) ..."
            % (visit + 1, args.visits))
        start = wait_for_scene(dbg, sel_addr, args.sel_value, True,
                               args.timeout, "not in scene yet")
        if start is None:
            say("timed out waiting for the scene; captured %d visit(s)"
                % len(result["visits"]))
            break
        say("  entered at frame %d; settling %d frames" % (start, args.settle))
        while dbg.json("frame")["frame"] < start + args.settle:
            time.sleep(0.05)
        now = dbg.json("frame")["frame"]
        cap = capture(dbg, chain, lo, hi, start, now)
        cap["visit"] = visit + 1
        cap["entered_frame"] = start
        result["visits"].append(cap)
        say("  captured visit %d at frame %d" % (visit + 1, now))
        if not cap.get("reg_trace_covers_window", True):
            say("  WARNING: the register ring no longer covers this visit's "
                "frames — the writer census is incomplete, not empty.")
        if not cap["irq_chain_trustworthy"]:
            say("  WARNING: the block-trace ring is EMPTY, so every irq_chain "
                "count above is a dead instrument, not a measurement. Treat "
                "those zeros as unknown.")
        # Write after EVERY visit, not only at the end. A run interrupted
        # during visit 2 must still leave visit 1 on disk — otherwise a long
        # wait that gets abandoned yields nothing at all, which is what
        # happened the first time this was used for real.
        flush_result(result, path)
        say("  partial result written (%d visit(s)) — loadable now"
            % len(result["visits"]))


def flush_result(result, path):
    """Atomic write: a reader must never see a half-serialised file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(result, fh, indent=2)
    os.replace(tmp, path)


def finish(result, path, dbg):
    # Diff: report every field that is not identical across visits.
    diffs = []
    if len(result["visits"]) >= 2:
        a, b = result["visits"][0], result["visits"][-1]
        for label in sorted(a["irq_chain"]):
            if a["irq_chain"][label] != b["irq_chain"][label]:
                diffs.append({"field": "irq_chain." + label,
                              "visit1": a["irq_chain"][label],
                              "visit2": b["irq_chain"][label]})
        for key in ("cgram", "shadow_0900"):
            for sub in ("distinct", "top_value", "top_count", "looks_like_fill"):
                if a[key][sub] != b[key][sub]:
                    diffs.append({"field": "%s.%s" % (key, sub),
                                  "visit1": a[key][sub], "visit2": b[key][sub]})
        for key in ("cgram_upload_frames", "cgram_top_present_in_shadow", "dp"):
            if a[key] != b[key]:
                diffs.append({"field": key, "visit1": a[key], "visit2": b[key]})
    result["diffs"] = diffs

    flush_result(result, path)
    say("\nwrote %s" % path)
    say("visits captured: %d, differing fields: %d"
        % (len(result["visits"]), len(diffs)))
    if len(result["visits"]) < 2:
        say("only one visit — nothing to diff. Let it run through a second "
            "pass of the scene for a comparison.")
    for d in diffs[:20]:
        say("  %-34s visit1=%-14s visit2=%s"
            % (d["field"], str(d["visit1"])[:14], str(d["visit2"])[:40]))
    dbg.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
