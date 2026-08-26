#!/usr/bin/env python3
"""
ot_base_parity.py — does the pause-menu glyph emitter insert into the ordering
table the frame is actually rendering?

Native (4370) is measured with the write trace. DuckStation (4371) is measured
with pc_break + pc_hit_last, which the existing oracle patch already exposes —
deliberately NO new DuckStation code and no reading of its source (CC-BY-NC-ND;
see recomp-ai-rules/LICENSING.md §2 and PSX/PRINCIPLES.md §11).

Both sides must be parked at the in-arena pause menu.

  OT_INSERT  0x80029CD8   the emitter's `sw $v0,0($a1)` — a1 = OT slot address
  TABLE A    0x8005B790 .. 0x8005F790
  TABLE B    0x8005F79C .. 0x800637A0

Verdict: the menu glyphs are correct only if their OT slot lies in the same
table the frame cleared (pc=0x80030294) and built (pc=0x8001961C).
"""
import json, socket, struct, sys, time

OT_INSERT = 0x80029CD8
OT_CLEAR_PC = "0x80030294"
SCENE_PC = "0x8001961C"
GLYPH_PC = "0x80029CD8"
TABLES = (("A", 0x5B790, 0x5F790), ("B", 0x5F79C, 0x637A0))


def q(port, cmd, **kw):
    req = {"id": 1, "cmd": cmd}
    req.update(kw)
    s = socket.create_connection(("127.0.0.1", port), timeout=60)
    s.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    while True:
        c = s.recv(1 << 20)
        if not c:
            break
        buf += c
        if buf.count(b"\n") and buf.strip().startswith(b"{") and buf.rstrip().endswith(b"}"):
            break
    s.close()
    return json.loads(buf.replace(b"\n", b"").decode("utf-8", "replace"))


def table_of(addr):
    for name, lo, hi in TABLES:
        if lo <= (addr & 0x1FFFFFFF) < hi:
            return name
    return "?"


def native():
    q(4370, "wtrace_add", lo="0x0005B790", hi="0x000637A0")
    time.sleep(2.0)
    ents = q(4370, "wtrace_dump", addr_lo="0x0005B790", addr_hi="0x000637A0",
             count=2048, newest=1)["entries"]
    per = {}
    for e in ents:
        f = e["frame"]
        d = per.setdefault(f, {"clear": set(), "glyph": {}, "scene": set()})
        t = table_of(int(e["addr"], 16))
        if e["pc"] == OT_CLEAR_PC:
            d["clear"].add(t)
        elif e["pc"] == SCENE_PC:
            d["scene"].add(t)
        elif e["pc"] == GLYPH_PC:
            d["glyph"][t] = d["glyph"].get(t, 0) + 1
    rows = []
    for f in sorted(per):
        d = per[f]
        if not d["glyph"] or not d["clear"]:
            continue
        # the menu run is the bulk insert (~46); the HUD run is the small one (~4)
        menu = max(d["glyph"].items(), key=lambda kv: kv[1])
        rows.append((f, sorted(d["clear"]), menu, dict(d["glyph"])))
    return rows


def duckstation():
    q(4371, "pc_break", addr="0x%08X" % OT_INSERT)
    seen = []
    try:
        for _ in range(40):
            time.sleep(0.15)
            h = q(4371, "pc_hit_last")
            if not h.get("ok"):
                continue
            a1 = None
            for key in ("a1", "gpr_a1", "r5"):
                if key in h:
                    a1 = h[key]
                    break
            if a1 is None and isinstance(h.get("gpr"), list) and len(h["gpr"]) > 5:
                a1 = h["gpr"][5]
            if a1 is None:
                return None, h  # unknown reply shape — report it rather than guess
            v = int(a1, 16) if isinstance(a1, str) else int(a1)
            seen.append(v)
    finally:
        q(4371, "pc_unbreak", addr="0x%08X" % OT_INSERT)
    return seen, None


if __name__ == "__main__":
    print("=== native (4370): write trace ===")
    try:
        for f, clear, menu, allg in native():
            t, n = menu
            verdict = "OK" if t in clear else "*** WRONG TABLE ***"
            print("  frame %-8d cleared=%-6s menu %d inserts -> %s   %s   (all: %s)"
                  % (f, ",".join(clear), n, t, verdict, allg))
    except Exception as e:
        print("  native unavailable:", e)

    print()
    print("=== duckstation (4371): pc_break at 0x%08X ===" % OT_INSERT)
    try:
        seen, raw = duckstation()
        if seen is None:
            print("  pc_hit_last reply shape not recognised; raw reply:")
            print("  ", json.dumps(raw)[:600])
        elif not seen:
            print("  no hits captured (breakpoint may not have fired)")
        else:
            counts = {}
            for v in seen:
                counts[table_of(v)] = counts.get(table_of(v), 0) + 1
            print("  OT slots captured: %d   by table: %s" % (len(seen), counts))
            print("  sample slots: %s" % ["0x%08X" % v for v in seen[:6]])
    except Exception as e:
        print("  duckstation unavailable:", e)
