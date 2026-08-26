#!/usr/bin/env python3
"""psx_gpu_frame.py -- capture and decode a frame's GP0 stream, with the guest
function that issued every primitive.

Why this exists
---------------
`gpu_frame_dump` on the runtime's TCP debug server already stamps every GP0
packet with the guest code that issued it (`func`, `pc`, `ra`) and its linked-
list OT rank -- see runtime/src/debug_server.c, handle_gpu_frame_dump(), and
GpuGp0RingEntry in runtime/include/gpu.h. Nothing consumed that attribution.
This module turns the raw ring into decoded primitives, so "which guest
function drew this, and was the semi-transparency bit set" has a mechanical
answer instead of a guess.

Provenance rule
---------------
Everything here is OBSERVED from a running game and is labelled as such. It is
never merged into a static claim. A `func` appearing in a frame dump is
evidence that code executed and issued a primitive -- it is not evidence about
the call graph, and the analyzer's static coverage gap stays exactly as wide as
it was.

Fidelity, stated up front
-------------------------
* Vertices are decoded exactly and the running GP0(E5) draw offset is applied,
  because that is what the rasteriser applies (gpu.c, gp0_exec_* ).
* The texpage latch follows hardware: a textured polygon's own tpage word
  overwrites draw-mode state (gpu.c set_tpage_from_poly), and later untextured
  prims and rectangles consume whatever it left behind. Getting this wrong
  mislabels exactly the semi-transparency mode you are usually chasing.
* Textures are NOT sampled. Texture pages, CLUTs and UVs are decoded and
  reported, but a textured primitive renders as its command colour. The goal is
  attribution and blend-state triage, not a second GPU implementation.
* The ring truncates each packet to GPU_GP0_RING_MAX_WORDS (12). A packet
  longer than that is decoded as far as it goes and marked `truncated`.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DUMP_KIND = "psx-gpu-frame"
DUMP_VERSION = 1

DEFAULT_NATIVE_PORT = 4370
DEFAULT_DUCKSTATION_PORT = 4371

# Semi-transparency modes, GPUSTAT bits 5-6. The names are the blend the
# hardware performs, because that is what you compare against the screenshot.
STP_MODES = {
    0: "0.5B+0.5F",
    1: "B+F",
    2: "B-F",
    3: "B+0.25F",
}


class DebugError(RuntimeError):
    pass


def _looks_complete(buf: bytes) -> bool:
    """Is this a whole JSON document yet?

    Brace counting rather than a parse attempt per chunk: a reply can be a
    megabyte of hex, and re-parsing it on every 1 MB recv would dominate the
    read. Quotes and escapes are tracked so a brace inside a string does not
    count.
    """
    depth = 0
    in_str = False
    esc = False
    seen = False
    for c in buf:
        if esc:
            esc = False
            continue
        if in_str:
            if c == 0x5C:      # backslash
                esc = True
            elif c == 0x22:    # quote
                in_str = False
            continue
        if c == 0x22:
            in_str = True
        elif c == 0x7B:        # {
            depth += 1
            seen = True
        elif c == 0x7D:        # }
            depth -= 1
            if seen and depth <= 0:
                return True
    return False


class DebugConn:
    """JSON client for the runtime (and DuckStation) debug server.

    ONE REQUEST PER CONNECTION. That is the server's actual contract, not a
    conservative choice: runtime/src/debug_server.c's io_thread_main() accepts,
    reads a single line, replies, and sock_close()s. A client that holds the
    socket open and sends a second command gets silence and then EOF, which
    looks exactly like a hung emulator. (`s_client` in that file is vestigial;
    nothing ever assigns it a live socket.) tools/debug_client.py's query()
    documents the same contract -- "connect, send, receive, close".

    So each command opens its own socket and reads to EOF. Replies can be
    megabytes (a full gpu_frame_dump), and the server's send is blocking with a
    starvation watchdog behind it, so the read drains in large chunks and never
    parses incrementally.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_NATIVE_PORT,
                 timeout: float = 15.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._next_id = 1

    def __enter__(self) -> "DebugConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def connect(self) -> None:
        """Probe that a server is there. Not required before cmd()."""
        try:
            socket.create_connection((self.host, self.port),
                                     timeout=self.timeout).close()
        except OSError as e:
            raise DebugError(f"no debug server on {self.host}:{self.port} ({e})") from e

    def close(self) -> None:
        """No persistent socket to close; kept so callers can use `with`."""

    def cmd(self, name: str, **fields: Any) -> Dict[str, Any]:
        """Send one command, return the parsed reply. Raises on ok:false."""
        reply = self.raw(name, **fields)
        if not reply.get("ok", False):
            raise DebugError(f"{name}: {reply.get('error', reply.get('err', reply))}")
        return reply

    def raw(self, name: str, **fields: Any) -> Dict[str, Any]:
        """Like cmd() but returns ok:false replies instead of raising."""
        req = {"id": self._next_id, "cmd": name}
        self._next_id += 1
        req.update(fields)
        line = (json.dumps(req) + "\n").encode()

        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise DebugError(f"{name}: no debug server on "
                             f"{self.host}:{self.port} ({e})") from e
        try:
            sock.settimeout(self.timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                sock.sendall(line)
            except OSError as e:
                raise DebugError(f"{name}: send failed ({e})") from e

            buf = bytearray()
            while True:
                try:
                    chunk = sock.recv(1 << 20)
                except socket.timeout as e:
                    raise DebugError(
                        f"{name}: timed out after {self.timeout}s "
                        f"({len(buf)} bytes received)") from e
                except OSError as e:
                    raise DebugError(f"{name}: recv failed ({e})") from e
                if not chunk:
                    break
                buf += chunk
                # Most replies are one line, and stopping at the newline avoids
                # waiting on the close for peers (DuckStation) that keep the
                # socket around.
                #
                # But not all of them are. pc_probe_dump builds its JSON with
                # repeated send_line() calls -- header, one line per slot, one
                # per sample -- so the first newline lands in the MIDDLE of the
                # document. Reading one line there returns a truncated object
                # that fails to parse, and the error ("malformed reply") points
                # at the server rather than at this loop.
                #
                # So: stop at a newline only if what has arrived so far is a
                # complete JSON document. Otherwise keep reading.
                if b"\n" in buf and _looks_complete(buf):
                    break
        finally:
            sock.close()

        if not buf:
            raise DebugError(f"{name}: server closed without replying")
        # Parse the WHOLE buffer, not just the first line.
        #
        # Fixing the read loop to keep going until the document was complete and
        # then still splitting on the first newline here left the bug exactly
        # where it was: pc_probe_dump spreads one JSON object across several
        # send_line() calls, so the first line is a fragment. JSON treats the
        # embedded newlines as whitespace, so the joined buffer parses directly.
        #
        # The first line is kept as a fallback for a peer that sends more than
        # one document down the same connection (an event followed by a reply,
        # say), where the whole buffer would not parse but the first line does.
        whole = bytes(buf).decode("utf-8", "replace")
        try:
            return json.loads(whole)
        except json.JSONDecodeError:
            pass
        head = whole.split("\n", 1)[0]
        try:
            return json.loads(head)
        except json.JSONDecodeError as e:
            raise DebugError(f"{name}: malformed reply ({e}); "
                             f"first 200 bytes: {head[:200]!r}") from e

    # -- convenience wrappers used by the CLIs ------------------------------

    def frame(self) -> int:
        """Current frame number, from whichever command this peer answers.

        NOT from `ping`. psx-runtime intercepts ping in a lock-free fast path
        that never reaches the command dispatcher, so its reply carries only
        {ok, pong, io_thread} -- no frame. Asking ping for a frame therefore
        returned 0 for every native run, silently, and anything that aligned two
        emulators by frame number was comparing against zero. The DuckStation
        oracle does put a frame in its ping, which is what made the bug look
        like an oracle-only quirk instead of a native-side blind spot.
        """
        r = self.raw("frame")
        if r.get("ok") and "frame" in r:
            return int(r["frame"])
        return int(self.raw("ping").get("frame", 0))

    def ring_span(self) -> Dict[str, Any]:
        """Which frames the GP0 ring can still be asked for.

        This is the replacement for pausing. The ring holds ~1M packets, which
        is several hundred frames, so the workflow is: let the game run, and
        capture the frame AFTER you have seen the bug -- along with the frames
        leading up to it. Reaching backwards beats freezing forwards.
        """
        r = self.cmd("gpu_ring_stats")
        return {
            "oldest": int(r.get("oldest_frame", 0)),
            "newest": int(r.get("newest_frame", 0)),
            "total": int(r.get("total", 0)),
            "capacity": int(r.get("capacity", 0)),
            "max_words": int(r.get("max_words", 12)),
        }

    # pause / continue / step / run_to_frame exist on both ends, but they are
    # debug-server-only -- no key, button, or menu reaches them, so a shipped
    # build cannot be frozen by them. psx-runtime also arms a timeout when it
    # parks (see pause_arm in runtime/src/debug_server.c): if the client stops
    # talking, it resumes on its own, because a debugger that dies must not
    # leave the game stopped forever.
    #
    # Prefer ring_span() + capture() anyway when you want to look at a frame.
    # Reaching backwards through recorded history beats freezing forwards, and
    # it does not perturb the thing you are measuring. Pause is for the oracle,
    # which records nothing, and for holding RAM still across a snapshot.

    def pause(self) -> Dict[str, Any]:
        """Park the emulator. Auto-resumes if this client goes quiet."""
        return self.cmd("pause")

    def resume(self) -> Dict[str, Any]:
        return self.cmd("continue")

    def step(self, frames: int = 1) -> Dict[str, Any]:
        return self.cmd("step", frames=int(frames))

    def run_to_frame(self, frame: int) -> Dict[str, Any]:
        return self.cmd("run_to_frame", frame=int(frame))

    def screenshot(self, path: str) -> Dict[str, Any]:
        """Write a PNG of the presented frame.

        The native server calls this `screenshot_file`; the DuckStation oracle
        patch calls the same operation `screenshot`. Try both so one caller
        works against either end.
        """
        r = self.raw("screenshot_file", path=path)
        if r.get("ok"):
            return r
        r2 = self.raw("screenshot", path=path)
        if r2.get("ok"):
            return r2
        raise DebugError(f"screenshot: {r2.get('error', r.get('error', 'failed'))}")


# ---------------------------------------------------------------------------
# GP0 decoding
# ---------------------------------------------------------------------------

def _sx11(v: int) -> int:
    """Sign-extend an 11-bit GPU coordinate."""
    v &= 0x7FF
    return v - 0x800 if v & 0x400 else v


def parse_vertex(word: int) -> Tuple[int, int]:
    return _sx11(word & 0xFFFF), _sx11((word >> 16) & 0xFFFF)


def parse_color(word: int) -> Tuple[int, int, int]:
    return word & 0xFF, (word >> 8) & 0xFF, (word >> 16) & 0xFF


def poly_flags(op: int) -> Dict[str, bool]:
    return {
        "gouraud": bool(op & 0x10),
        "quad": bool(op & 0x08),
        "textured": bool(op & 0x04),
        "semi": bool(op & 0x02),
        "raw_tex": bool(op & 0x01),
    }


def poly_words(op: int) -> int:
    f = poly_flags(op)
    n = 4 if f["quad"] else 3
    return 1 + n * (2 if f["textured"] else 1) + (n - 1 if f["gouraud"] else 0)


def rect_words(op: int) -> int:
    variable = ((op >> 3) & 3) == 0
    return 1 + 1 + (1 if op & 0x04 else 0) + (1 if variable else 0)


RECT_SIZES = {1: (1, 1), 2: (8, 8), 3: (16, 16)}


def op_name(op: int) -> str:
    """Human name for a GP0 opcode. Primitive names spell out their flags,
    because the flags are the diagnosis."""
    if op == 0x00:
        return "nop"
    if op == 0x01:
        return "clear_cache"
    if op == 0x02:
        return "fill_rect"
    if op == 0x03:
        return "unknown_03"
    if 0x20 <= op <= 0x3F:
        f = poly_flags(op)
        s = "PolyG" if f["gouraud"] else "PolyF"
        if f["textured"]:
            s += "T"
        s += "4" if f["quad"] else "3"
        if f["semi"]:
            s += "+semi"
        if f["raw_tex"] and f["textured"]:
            s += "+raw"
        return s
    if 0x40 <= op <= 0x5F:
        s = "LineG" if op & 0x10 else "LineF"
        if op & 0x08:
            s = "Poly" + s
        if op & 0x02:
            s += "+semi"
        return s
    if 0x60 <= op <= 0x7F:
        size = (op >> 3) & 3
        s = {0: "Rect", 1: "Rect1", 2: "Rect8", 3: "Rect16"}[size]
        if op & 0x04:
            s = "Sprite" + s[4:]
        if op & 0x02:
            s += "+semi"
        if op & 0x01 and op & 0x04:
            s += "+raw"
        return s
    if 0x80 <= op <= 0x9F:
        return "copy_vram_vram"
    if 0xA0 <= op <= 0xBF:
        return "copy_cpu_vram"
    if 0xC0 <= op <= 0xDF:
        return "copy_vram_cpu"
    return {
        0xE1: "draw_mode(E1)",
        0xE2: "tex_window(E2)",
        0xE3: "draw_area_tl(E3)",
        0xE4: "draw_area_br(E4)",
        0xE5: "draw_offset(E5)",
        0xE6: "mask_bit(E6)",
    }.get(op, f"op_{op:02X}")


def op_kind(op: int) -> str:
    if 0x20 <= op <= 0x3F:
        return "poly"
    if 0x40 <= op <= 0x5F:
        return "line"
    if 0x60 <= op <= 0x7F:
        return "rect"
    if op == 0x02:
        return "fill"
    if 0x80 <= op <= 0xDF:
        return "copy"
    if 0xE0 <= op <= 0xEF:
        return "env"
    return "other"


class GpuState:
    """Draw-mode state carried between packets while walking a frame.

    This is the part that makes the decode faithful rather than plausible: the
    semi-transparency mode of an untextured polygon or a rectangle comes from
    whatever the last GP0(E1) -- or the last textured polygon's own tpage word
    -- left in draw-mode state, not from the packet itself.
    """

    def __init__(self) -> None:
        self.offset = (0, 0)
        self.area_tl = (0, 0)
        self.area_br = (1023, 511)
        self.stp = 0
        self.texpage_x = 0
        self.texpage_y = 0
        self.tex_colors = 0
        self.mask_set = False
        self.mask_check = False

    def set_tpage(self, tpage_word: int) -> None:
        self.texpage_x = tpage_word & 0xF
        self.texpage_y = (tpage_word >> 4) & 1
        self.stp = (tpage_word >> 5) & 3
        self.tex_colors = (tpage_word >> 7) & 3

    def apply_env(self, op: int, w0: int) -> None:
        if op == 0xE1:
            self.set_tpage(w0 & 0xFFFF)
        elif op == 0xE3:
            self.area_tl = (w0 & 0x3FF, (w0 >> 10) & 0x1FF)
        elif op == 0xE4:
            self.area_br = (w0 & 0x3FF, (w0 >> 10) & 0x1FF)
        elif op == 0xE5:
            self.offset = (_sx11(w0 & 0x7FF), _sx11((w0 >> 11) & 0x7FF))
        elif op == 0xE6:
            self.mask_set = bool(w0 & 1)
            self.mask_check = bool(w0 & 2)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "offset": list(self.offset),
            "area": [self.area_tl[0], self.area_tl[1], self.area_br[0], self.area_br[1]],
            "stp": self.stp,
            "texpage": [self.texpage_x, self.texpage_y, self.tex_colors],
            "mask": [self.mask_set, self.mask_check],
        }


def _hexw(words: Sequence[Any]) -> List[int]:
    out = []
    for w in words:
        out.append(int(w, 16) if isinstance(w, str) else int(w))
    return out


def decode_entry(entry: Dict[str, Any], state: GpuState) -> Dict[str, Any]:
    """Decode one gpu_frame_dump ring entry against the running draw state.

    `state` is mutated: env commands and textured-poly tpage latches change it
    for everything that follows, exactly as on hardware.
    """
    op = int(entry["op"], 16) if isinstance(entry["op"], str) else int(entry["op"])
    words = _hexw(entry.get("w", []))
    n_words = int(entry.get("n", len(words)))
    kind = op_kind(op)

    prim: Dict[str, Any] = {
        "seq": int(entry.get("seq", 0)),
        "op": op,
        "op_name": op_name(op),
        "kind": kind,
        "n_words": n_words,
        "have_words": len(words),
        "truncated": n_words > len(words),
        "func": entry.get("func", "0x00000000"),
        "pc": entry.get("pc", "0x00000000"),
        "ra": entry.get("ra", "0x00000000"),
        "src": entry.get("src", "0x00000000"),
        "ot": int(entry.get("ot", 0xFFFF)),
        "words": [f"0x{w:08X}" for w in words],
        "verts": [],
        "colors": [],
        "uvs": [],
        "semi": False,
        "stp": state.stp,
        "textured": False,
        "raw_tex": False,
        "gouraud": False,
        "size": None,
        "clut": None,
        "tpage": None,
    }
    if not words:
        prim["state"] = state.snapshot()
        return prim

    if kind == "env":
        state.apply_env(op, words[0])
        prim["state"] = state.snapshot()
        prim["stp"] = state.stp
        return prim

    ox, oy = state.offset

    if kind == "poly":
        f = poly_flags(op)
        n = 4 if f["quad"] else 3
        prim.update(semi=f["semi"], textured=f["textured"],
                    raw_tex=f["raw_tex"] and f["textured"], gouraud=f["gouraud"])
        # Hardware latches the poly's own tpage word into draw-mode state, so
        # do it before reading stp -- and do it even if the packet is truncated
        # past the vertices, because the real GPU latched it too.
        tp_idx = 5 if f["gouraud"] else 4
        if f["textured"] and len(words) > tp_idx:
            tpage_word = (words[tp_idx] >> 16) & 0xFFFF
            state.set_tpage(tpage_word)
            prim["tpage"] = tpage_word
        prim["stp"] = state.stp
        i = 0
        color = parse_color(words[0])
        i = 1
        for v in range(n):
            if f["gouraud"] and v > 0:
                if i >= len(words):
                    break
                color = parse_color(words[i])
                i += 1
            if i >= len(words):
                break
            prim["verts"].append([parse_vertex(words[i])[0] + ox,
                                  parse_vertex(words[i])[1] + oy])
            prim["colors"].append(list(color))
            i += 1
            if f["textured"]:
                if i >= len(words):
                    break
                uvw = words[i]
                prim["uvs"].append([uvw & 0xFF, (uvw >> 8) & 0xFF])
                if v == 0:
                    prim["clut"] = (uvw >> 16) & 0xFFFF
                i += 1

    elif kind == "rect":
        variable = ((op >> 3) & 3) == 0
        prim.update(semi=bool(op & 0x02), textured=bool(op & 0x04),
                    raw_tex=bool(op & 0x01) and bool(op & 0x04))
        prim["stp"] = state.stp
        color = parse_color(words[0])
        if len(words) > 1:
            x, y = parse_vertex(words[1])
            prim["verts"].append([x + ox, y + oy])
            prim["colors"].append(list(color))
        i = 2
        if prim["textured"] and len(words) > i:
            uvw = words[i]
            prim["uvs"].append([uvw & 0xFF, (uvw >> 8) & 0xFF])
            prim["clut"] = (uvw >> 16) & 0xFFFF
            i += 1
        if variable:
            if len(words) > i:
                prim["size"] = [words[i] & 0x3FF, (words[i] >> 16) & 0x1FF]
        else:
            prim["size"] = list(RECT_SIZES[(op >> 3) & 3])

    elif kind == "line":
        gouraud = bool(op & 0x10)
        polyline = bool(op & 0x08)
        prim.update(semi=bool(op & 0x02), gouraud=gouraud)
        prim["stp"] = state.stp
        prim["polyline"] = polyline
        i = 0
        color = parse_color(words[0])
        i = 1
        while i < len(words):
            if words[i] == 0x55555555:
                break
            if gouraud and prim["verts"]:
                color = parse_color(words[i])
                i += 1
                if i >= len(words):
                    break
            x, y = parse_vertex(words[i])
            prim["verts"].append([x + ox, y + oy])
            prim["colors"].append(list(color))
            i += 1

    elif kind == "fill":
        # GP0(02): fill is in raw VRAM coordinates -- the draw offset and the
        # draw area do NOT apply, and it ignores the mask bit.
        prim["colors"].append(list(parse_color(words[0])))
        if len(words) > 1:
            prim["verts"].append([words[1] & 0x3FF, (words[1] >> 16) & 0x1FF])
        if len(words) > 2:
            prim["size"] = [words[2] & 0x3FF, (words[2] >> 16) & 0x1FF]

    elif kind == "copy":
        if len(words) > 1:
            prim["verts"].append([words[1] & 0x3FF, (words[1] >> 16) & 0x1FF])
        if op < 0xA0 and len(words) > 2:
            prim["verts"].append([words[2] & 0x3FF, (words[2] >> 16) & 0x1FF])
            if len(words) > 3:
                prim["size"] = [words[3] & 0x3FF, (words[3] >> 16) & 0x1FF]
        elif len(words) > 2:
            prim["size"] = [words[2] & 0x3FF, (words[2] >> 16) & 0x1FF]
        if entry.get("bld"):
            prim["bld"] = entry["bld"]

    prim["state"] = state.snapshot()
    return prim


def decode_entries(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Decode a whole frame in issue order. Order matters -- see GpuState."""
    state = GpuState()
    ordered = sorted(entries, key=lambda e: int(e.get("seq", 0)))
    return [decode_entry(e, state) for e in ordered]


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def attribute(prims: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Group decoded primitives by the guest function that issued them.

    The per-function numbers are the whole point: a function whose primitives
    all lost their semi-transparency bit between a good and a bad frame is the
    function to go read.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for p in prims:
        fn = p.get("func", "0x00000000")
        rec = out.setdefault(fn, {
            "func": fn,
            "packets": 0,
            "drawing": 0,
            "semi": 0,
            "textured": 0,
            "ops": {},
            "stp_modes": {},
            "ot_min": None,
            "ot_max": None,
            "ras": [],
            "bbox": None,
        })
        rec["packets"] += 1
        name = p["op_name"]
        rec["ops"][name] = rec["ops"].get(name, 0) + 1
        ra = p.get("ra")
        if ra and ra not in rec["ras"] and len(rec["ras"]) < 16:
            rec["ras"].append(ra)
        if p["kind"] in ("poly", "rect", "line", "fill"):
            rec["drawing"] += 1
            if p.get("semi"):
                rec["semi"] += 1
                m = STP_MODES.get(p.get("stp", 0), "?")
                rec["stp_modes"][m] = rec["stp_modes"].get(m, 0) + 1
            if p.get("textured"):
                rec["textured"] += 1
            ot = p.get("ot", 0xFFFF)
            if ot != 0xFFFF:
                rec["ot_min"] = ot if rec["ot_min"] is None else min(rec["ot_min"], ot)
                rec["ot_max"] = ot if rec["ot_max"] is None else max(rec["ot_max"], ot)
            bb = prim_bbox(p)
            if bb:
                rec["bbox"] = bb if rec["bbox"] is None else _union(rec["bbox"], bb)
    return out


def prim_bbox(p: Dict[str, Any]) -> Optional[List[int]]:
    verts = p.get("verts") or []
    if not verts:
        return None
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    size = p.get("size")
    if size and p["kind"] in ("rect", "fill"):
        x1 = x0 + max(0, size[0] - 1)
        y1 = y0 + max(0, size[1] - 1)
    return [x0, y0, x1, y1]


def _union(a: Sequence[int], b: Sequence[int]) -> List[int]:
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


# ---------------------------------------------------------------------------
# Frame signatures — comparing frames without needing function attribution
# ---------------------------------------------------------------------------
#
# `func` attribution is worthless on a game that builds an ordering table and
# DMAs it: no guest code is executing when the GP0 writes happen, so every
# packet in the frame reports the same submit PC. Legend of Mana is one of
# these — 1217 packets, one func, one ra, one pc.
#
# Two handles survive that, and both are already in the dump:
#
#   * (opcode, blend mode) — stable across frames, and it is what a rendering
#     regression actually moves: a layer that loses its semi-transparency bit,
#     a burst that doubles, geometry that escapes its box.
#   * the packet's SOURCE ADDRESS in guest RAM. Packets for one effect are
#     built into one contiguous array (LoM's additive burst lives at
#     0x0010D05C..0x0010EC78, stride 0x1C), so a run of consecutive addresses
#     identifies the effect even when the code that built it does not appear.
#     That address is also what you hand to wtrace to find the builder.

SIGNATURE_KIND = "psx-gpu-frame-signature"


def signature_key(prim: Dict[str, Any]) -> str:
    """Stable identity for a class of primitive within a frame."""
    mode = STP_MODES.get(prim.get("stp", 0), "?") if prim.get("semi") else "opaque"
    return f"{prim['op_name']}|{mode}"


def frame_signature(dump: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a frame to numbers that can be compared against another frame."""
    keys: Dict[str, Any] = {}
    drawing = semi = textured = 0
    for p in dump.get("prims", []):
        if p["kind"] not in ("poly", "rect", "line", "fill"):
            continue
        drawing += 1
        if p.get("semi"):
            semi += 1
        if p.get("textured"):
            textured += 1
        k = signature_key(p)
        rec = keys.setdefault(k, {"n": 0, "bbox": None, "cmax": 0, "csum": 0.0,
                                  "cn": 0, "ots": set(), "src_lo": None,
                                  "src_hi": None})
        rec["n"] += 1
        bb = prim_bbox(p)
        if bb:
            rec["bbox"] = bb if rec["bbox"] is None else _union(rec["bbox"], bb)
        for c in p.get("colors") or []:
            rec["cmax"] = max(rec["cmax"], max(c))
            rec["csum"] += sum(c) / 3.0
            rec["cn"] += 1
        ot = p.get("ot", 0xFFFF)
        if ot != 0xFFFF:
            rec["ots"].add(ot)
        try:
            src = int(p.get("src", "0x0"), 16)
        except ValueError:
            src = 0
        if src:
            rec["src_lo"] = src if rec["src_lo"] is None else min(rec["src_lo"], src)
            rec["src_hi"] = src if rec["src_hi"] is None else max(rec["src_hi"], src)
    for rec in keys.values():
        rec["cmean"] = round(rec["csum"] / rec["cn"], 1) if rec["cn"] else 0.0
        rec["ots"] = sorted(rec["ots"])[:8]
        del rec["csum"], rec["cn"]
    return {
        "kind": SIGNATURE_KIND,
        "frame": dump.get("frame", 0),
        "drawing": drawing,
        "semi": semi,
        "textured": textured,
        "packets": dump.get("raw_count", 0),
        "keys": keys,
    }


def _bbox_area(bb: Optional[Sequence[int]]) -> int:
    if not bb:
        return 0
    return max(0, bb[2] - bb[0] + 1) * max(0, bb[3] - bb[1] + 1)


def signature_delta(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """What moved between two frame signatures, and by how much.

    `score` exists so a scan can rank frames: it weights the things a rendering
    break actually does — primitives appearing or vanishing, and geometry
    escaping the box it lived in — rather than the steady churn of an animation.
    """
    changes = []
    score = 0.0
    for k in sorted(set(a["keys"]) | set(b["keys"])):
        ka = a["keys"].get(k)
        kb = b["keys"].get(k)
        na = ka["n"] if ka else 0
        nb = kb["n"] if kb else 0
        area_a = _bbox_area(ka["bbox"] if ka else None)
        area_b = _bbox_area(kb["bbox"] if kb else None)
        # Relative growth, so a burst going 1 -> 258 outranks 700 -> 760.
        rel = abs(nb - na) / max(1.0, float(min(na, nb) or 1))
        area_rel = abs(area_b - area_a) / max(1.0, float(min(area_a, area_b) or 1))
        if na == nb and area_a == area_b:
            continue
        w = rel + 0.5 * area_rel
        score += w
        changes.append({
            "key": k, "a": na, "b": nb, "delta": nb - na,
            "bbox_a": ka["bbox"] if ka else None,
            "bbox_b": kb["bbox"] if kb else None,
            "area_a": area_a, "area_b": area_b,
            "cmax_a": ka["cmax"] if ka else 0, "cmax_b": kb["cmax"] if kb else 0,
            "src_a": f"0x{ka['src_lo']:08X}" if ka and ka["src_lo"] else None,
            "src_b": f"0x{kb['src_lo']:08X}" if kb and kb["src_lo"] else None,
            "weight": round(w, 3),
        })
    changes.sort(key=lambda c: -c["weight"])
    return {"a": a["frame"], "b": b["frame"], "score": round(score, 3),
            "changes": changes}


# ---------------------------------------------------------------------------
# Capture / dump I/O
# ---------------------------------------------------------------------------

def capture(conn: DebugConn, frame: Optional[int] = None, count: int = 65536,
            label: str = "", verify_ring: bool = True) -> Dict[str, Any]:
    """Pull one frame's GP0 ring and decode it into a dump dict.

    Two failure modes are turned into errors rather than empty dumps, because
    both look exactly like "that frame drew nothing", which is a conclusion you
    might act on:

      * the frame has fallen out of the ring (or has not happened yet), and
      * the peer answered ok with no `entries` key at all -- which is what a
        stub or a mismatched server does.
    """
    span = None
    if verify_ring:
        try:
            span = conn.ring_span()
        except DebugError:
            span = None   # older server without gpu_ring_stats; carry on
    if frame is None:
        frame = span["newest"] if span else conn.frame()
    if span and span["total"] > 0 and not (span["oldest"] <= frame <= span["newest"]):
        raise DebugError(
            f"frame {frame} is not in the GP0 ring (it holds {span['oldest']}.."
            f"{span['newest']}). Capture closer to the moment, or raise the ring "
            f"size; a dump of an evicted frame is empty, not zero-drawn.")

    reply = conn.cmd("gpu_frame_dump", frame=int(frame), count=int(count))
    if "entries" not in reply:
        raise DebugError(
            "gpu_frame_dump replied ok but carried no 'entries' -- this is not a "
            "psx-runtime debug server. Check what is actually listening on "
            f"{conn.host}:{conn.port}.")
    entries = reply.get("entries", [])
    prims = decode_entries(entries)
    raw_count = int(reply.get("count", len(entries)))
    return {
        "kind": DUMP_KIND,
        "version": DUMP_VERSION,
        "label": label,
        "frame": int(reply.get("frame", frame)),
        "source": {"host": conn.host, "port": conn.port},
        "ring": span,
        "raw_count": raw_count,
        "requested": int(count),
        "capped": raw_count >= int(count),
        "max_words": int(reply.get("max_words", 12)),
        "prims": prims,
        "funcs": attribute(prims),
    }


SUMMARY_KIND = "psx-gpu-frame-summary"


def summarise_dump(dump: Dict[str, Any], dump_name: str = "") -> Dict[str, Any]:
    """A compact view of a dump: totals, opcode and blend-mode histograms, and
    the per-function attribution -- everything except the primitives.

    A busy frame's full dump runs to tens of megabytes. Nothing that only wants
    to know *who drew what* should have to parse that, so the summary is a
    separate artifact and RetComM Studio reads this rather than the dump.
    """
    ops: Dict[str, int] = {}
    modes: Dict[str, int] = {}
    drawing = 0
    truncated = 0
    for p in dump.get("prims", []):
        ops[p["op_name"]] = ops.get(p["op_name"], 0) + 1
        if p.get("truncated"):
            truncated += 1
        if p["kind"] in ("poly", "rect", "line", "fill"):
            drawing += 1
            if p.get("semi"):
                m = STP_MODES.get(p.get("stp", 0), "?")
                modes[m] = modes.get(m, 0) + 1
    funcs = sorted(dump.get("funcs", {}).values(),
                   key=lambda r: (-r.get("drawing", 0), r.get("func", "")))
    return {
        "kind": SUMMARY_KIND,
        "version": 1,
        "label": dump.get("label", ""),
        "frame": dump.get("frame", 0),
        "dump": dump_name,
        "packets": dump.get("raw_count", 0),
        "drawing": drawing,
        "capped": bool(dump.get("capped")),
        "truncated": truncated,
        "area": list(draw_area(dump)),
        "ops": ops,
        "modes": modes,
        "funcs": funcs,
    }


def save_summary(dump: Dict[str, Any], path: str, dump_name: str = "") -> Dict[str, Any]:
    summary = summarise_dump(dump, dump_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    return summary


def save_dump(dump: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=1)


def load_dump(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        dump = json.load(f)
    if dump.get("kind") != DUMP_KIND:
        raise ValueError(f"{path}: not a {DUMP_KIND} dump")
    return dump


def draw_area(dump: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """The frame's drawing clip rect, from the last GP0(E3)/GP0(E4) seen.

    Falls back to the bounding box of everything drawn, then to 320x240, so a
    dump that never set a draw area still renders something honest.
    """
    area = None
    for p in dump.get("prims", []):
        st = p.get("state")
        if st and st.get("area"):
            area = st["area"]
    if area and area[2] > area[0] and area[3] > area[1]:
        return int(area[0]), int(area[1]), int(area[2]) + 1, int(area[3]) + 1
    bb = None
    for p in dump.get("prims", []):
        b = prim_bbox(p)
        if b:
            bb = b if bb is None else _union(bb, b)
    if bb:
        return max(0, bb[0]), max(0, bb[1]), min(1024, bb[2] + 1), min(512, bb[3] + 1)
    return 0, 0, 320, 240


# ---------------------------------------------------------------------------
# Reading display lists out of guest RAM
# ---------------------------------------------------------------------------
#
# A PSX game rarely pokes GP0 directly. It builds a linked list in RAM and
# hands it to DMA channel 2, so the display list is a data structure in memory,
# and reading it is a different act from watching commands go by. Two things
# need that: the DuckStation oracle records no GP0 packets at all, so RAM is
# the only place its display list exists; and for decompilation, the buffer a
# routine fills is what tells you what the routine IS.

RAM_SIZE = 2 * 1024 * 1024
OT_END = 0xFFFFFF
GPU_GP0_RING_MAX_WORDS = 12

# Largest read_ram both ends actually answer. The oracle's cap is not the 65536
# its handler advertises: DuckStation replies over a BufferedStreamSocket whose
# send buffer does not hold a reply that big, and Write() copies only what fits
# while SendJSON ignores the short count -- so the reply is truncated and the
# read blocks forever waiting for a newline. Measured against a live oracle:
# 16384 round-trips, 32768 stalls (and appears to take the emulator with it).
# Our own runtime handles more, but 128 reads for 2 MB is fast on both, so one
# number keeps the two paths identical.
MAX_READ_RAM = 16384


# How often a PAUSED DuckStation oracle can answer.
#
# It pumps its debug socket from the emulation loop, so once parked the
# only pump left is a Qt idle timer: 100 ms with a gamepad attached, and
# 1000 ms without. A headless oracle on a machine with no pad is therefore
# a ~1 Hz server, and anything polling it faster stacks up connections it
# cannot accept -- which surfaces as "server closed without replying"
# rather than as a timeout, so it does not read like a pacing problem.
ORACLE_PAUSED_POLL_S = 1.5


def oracle_resume(conn) -> bool:
    """Get a paused oracle running again; True if it is running after.

    Worth calling BEFORE arming anything, not just after. A tool that
    parked the oracle and exited without resuming leaves every later run
    talking to a 1 Hz server, and the symptom -- everything times out --
    looks nothing like the cause.
    """
    try:
        conn.cmd("continue")
        return True
    except DebugError:
        return False


def oracle_break_list(conn) -> list:
    """Addresses the oracle currently has execute breakpoints on."""
    try:
        rep = conn.raw("pc_break_list")
    except DebugError:
        return []
    out = []
    for a in rep.get("breaks") or rep.get("addrs") or []:
        try:
            out.append(int(a, 16) if isinstance(a, str) else int(a))
        except (TypeError, ValueError):
            continue
    return out


def oracle_clear_breaks(conn, tries: int = 6) -> int:
    """Remove every execute breakpoint the oracle holds. Returns how many.

    Stale breakpoints are not inert. DuckStation's AddBreakpointWithCallback
    refuses a duplicate address, so a leaked breakpoint makes the NEXT run's
    pc_break return ok:false -- while the leaked one keeps firing and pausing
    the emulator. Resuming then re-pauses immediately, which is exactly the
    "oracle gets paused and never unpaused" symptom, and it survives across
    tool invocations because it lives in the oracle, not in the tool.

    Removal is VERIFIED against pc_break_list rather than assumed, and
    retried: a paused oracle serves its socket from a Qt idle timer at about
    1 Hz, so an unbreak can simply time out. Swallowing that failure is how a
    breakpoint outlives the tool that set it.
    """
    n = 0
    for _ in range(max(1, tries)):
        remaining = oracle_break_list(conn)
        if not remaining:
            return n
        for addr in remaining:
            try:
                conn.raw("pc_unbreak", addr=f"0x{addr:08X}")
                n += 1
            except DebugError:
                pass
        # A paused oracle answers slowly; give it room before re-checking.
        oracle_resume(conn)
        time.sleep(0.2)
    left = oracle_break_list(conn)
    if left:
        raise DebugError(
            "could not remove oracle breakpoint(s) at "
            + ", ".join(f"0x{a:08X}" for a in left)
            + ". They will keep pausing it; restart DuckStation to clear them.")
    return n


class OracleBreak:
    """Arm one execute breakpoint on the oracle and guarantee its removal.

    Use as a context manager. On exit the breakpoint is removed and the
    oracle is resumed on EVERY path -- normal return, exception, or the
    caller breaking out of a loop mid-way. Tools in this tree have leaked a
    parked oracle repeatedly; the guard is the fix, not remembering to
    resume.
    """

    def __init__(self, conn, addr, clear_stale: bool = True):
        self.conn = conn
        self.addr = addr if isinstance(addr, str) else f"0x{addr:08X}"
        self.clear_stale = clear_stale
        self.armed = False
        self.cleared = 0

    def __enter__(self):
        oracle_resume(self.conn)
        if self.clear_stale:
            self.cleared = oracle_clear_breaks(self.conn)
        try:
            self.conn.raw("pc_hit_clear")
        except DebugError:
            pass
        rep = self.conn.raw("pc_break", addr=self.addr)
        self.armed = bool(rep.get("ok"))
        if not self.armed:
            # Removing our own address and retrying covers the case where the
            # list command is unavailable, so clear_stale found nothing.
            try:
                self.conn.raw("pc_unbreak", addr=self.addr)
            except DebugError:
                pass
            rep = self.conn.raw("pc_break", addr=self.addr)
            self.armed = bool(rep.get("ok"))
        if not self.armed:
            # Make the claim in the message true before making it. An error
            # path that leaves the emulator parked is how this whole class of
            # bug started.
            try:
                self.conn.raw("pc_unbreak", addr=self.addr)
            except DebugError:
                pass
            oracle_resume(self.conn)
            raise DebugError(
                f"pc_break {self.addr} refused ({rep}). The oracle would not "
                f"arm, so nothing was sampled; it has been left running.")
        return self

    def __exit__(self, exc_type, exc, tb):
        # Verify, do not assume. This runs on the exception path too, which is
        # exactly when a swallowed failure leaves the emulator wedged.
        try:
            oracle_clear_breaks(self.conn)
        except DebugError as e:
            print(f"WARNING: {e}", file=sys.stderr)
        oracle_resume(self.conn)
        return False


def read_ram_range(conn: "DebugConn", addr: int, length: int,
                   chunk: int = MAX_READ_RAM) -> bytes:
    """Read guest RAM in chunks, tolerating a peer that returns short."""
    chunk = max(1, min(chunk, MAX_READ_RAM))
    out = bytearray()
    off = 0
    while off < length:
        n = min(chunk, length - off)
        rep = conn.cmd("read_ram", addr=f"0x{addr + off:08X}", len=n)
        hexs = rep.get("hex")
        if hexs is None:
            raise DebugError("read_ram replied without 'hex'")
        blob = bytes.fromhex(hexs)
        if not blob:
            break
        out += blob
        off += len(blob)
    return bytes(out)


def snapshot_ram(conn: "DebugConn", size: int = RAM_SIZE) -> bytes:
    """Whole of main RAM, so a list can be walked offline.

    Walking node by node over the wire would be a round trip per node and a
    display list runs to hundreds of them.
    """
    return read_ram_range(conn, 0x80000000, size)


def snapshot_ram_window(conn: "DebugConn", base: int, length: int,
                        size: int = RAM_SIZE) -> bytes:
    """A RAM image with only [base, base+length) populated.

    Walking an ordering table touches only the list's own span, so snapshotting
    the whole 2 MB to reach something already located is ~128 round trips for
    nothing. Against DuckStation those reads are serviced on the emulator
    thread, so a caller that samples repeatedly makes the emulator appear to
    freeze even though nothing is paused.
    """
    buf = bytearray(size)
    base &= 0x1FFFFF
    length = max(0, min(length, size - base))
    if length:
        data = read_ram_range(conn, 0x80000000 + base, length)
        buf[base:base + len(data)] = data
    return bytes(buf)


def walk_ordering_table(ram: bytes, root: int, max_nodes: int = 8192,
                        max_words: int = GPU_GP0_RING_MAX_WORDS) -> List[Dict[str, Any]]:
    """Follow an ordering table, returning entries shaped like gpu_frame_dump's.

    Defensive by construction: a corrupt tag can point anywhere, and a cycle
    would hang the walk, so nodes are bounded, revisits stop it, and an address
    outside RAM ends it rather than raising.
    """
    entries: List[Dict[str, Any]] = []
    seen = set()
    addr = root & 0x1FFFFF
    seq = 0
    # Bound on nodes VISITED, not entries emitted. An ordering table is mostly
    # empty link entries -- a 1024-entry OT chaining to 30 primitives is 1024
    # nodes and 30 entries -- so bounding the emitted count would leave the
    # traversal itself effectively unbounded.
    visited = 0
    while addr != OT_END and visited < max_nodes:
        visited += 1
        if addr in seen or addr + 4 > len(ram):
            break
        seen.add(addr)
        tag = int.from_bytes(ram[addr:addr + 4], "little")
        n_words = (tag >> 24) & 0xFF
        nxt = tag & 0xFFFFFF
        if n_words:
            end = addr + 4 + n_words * 4
            if end > len(ram):
                break
            words = [int.from_bytes(ram[addr + 4 + i * 4: addr + 8 + i * 4], "little")
                     for i in range(n_words)]
            op = (words[0] >> 24) & 0xFF
            entries.append({
                "seq": seq,
                "op": f"0x{op:02X}",
                "n": n_words,
                # The payload address, matching what the GP0 ring records as the
                # command header -- so the two sources are directly comparable.
                "src": f"0x{addr + 4:08X}",
                "ot": 0xFFFF,
                "pc": "0x00000000",
                "func": "0x00000000",
                "ra": "0x00000000",
                "w": [f"0x{w:08X}" for w in words[:max_words]],
            })
            seq += 1
        addr = nxt & 0x1FFFFF if nxt != OT_END else OT_END
    return entries


def expected_words(op: int) -> Optional[int]:
    """Payload length a GP0 opcode must have, or None if it is not one.

    This is the filter that makes scanning for display lists work at all. A
    2 MB RAM image contains half a million words, and "looks like a tag" alone
    matches a large fraction of them; requiring the payload to also be a GP0
    command of exactly the right length is what separates a real ordering-table
    node from a coincidence.
    """
    kind = op_kind(op)
    if kind == "poly":
        return poly_words(op)
    if kind == "rect":
        return rect_words(op)
    if kind == "line":
        return None          # polylines are variable-length; do not seed on them
    if kind in ("state", "env"):
        return 1
    return None


def find_display_lists(ram: bytes, min_prims: int = 4, limit: int = 8,
                       near: Optional[int] = None,
                       max_nodes: int = 65536) -> List[Dict[str, Any]]:
    """Find ordering-table chains by scanning RAM. Returns best-first.

    Following DMA channel 2's MADR does NOT work for this, which is worth
    stating plainly because it is the obvious thing to try. A linked-list DMA
    advances MADR as it consumes the list and leaves it holding the terminator,
    0xFFFFFF, once the transfer completes. Read between frames -- the only time
    you can pause -- MADR names the end of the list, not its start. Masked into
    2 MB that reads back as 0x1FFFFF, which looks like a plausible address and
    is not one.

    So the list is found by its own structure instead: nodes whose payload is a
    real GP0 command of exactly the right length, chained, and not pointed at by
    any other such node -- i.e. the head of a chain rather than its middle.
    """
    n = len(ram) - (len(ram) % 4)
    words = memoryview(ram)[:n].cast("I")

    # Two node kinds, and both matter.
    #
    # A PRIMITIVE node carries a GP0 packet, and its payload length must match
    # the opcode exactly -- that check is what keeps a 2 MB scan from matching
    # noise.
    #
    # A LINK node carries no payload; it is one bucket of the ordering table
    # array. These were originally skipped, which was the bug: an OT bucket
    # points INTO a primitive chain, so if links are left out of the graph every
    # bucket's first primitive looks like an unpointed head. The scan then
    # reports one bucket per chain -- a partial frame that can easily omit the
    # very effect being investigated -- instead of the whole display list.
    succ: Dict[int, int] = {}       # addr -> next addr (or OT_END)
    prim_nodes = set()
    pointed = set()
    for i in range(len(words) - 1):
        tag = words[i]
        n_words = (tag >> 24) & 0xFF
        nxt = tag & 0xFFFFFF
        if nxt != OT_END and (nxt & 3 or nxt >= n):
            continue
        if n_words:
            if i + 1 + n_words > len(words):
                continue
            want = expected_words(int(words[i + 1]) >> 24 & 0xFF)
            if want is None or want != n_words:
                continue
            prim_nodes.add(i * 4)
        else:
            # A link entry is only plausible if it points somewhere real.
            # Without this every zeroed word in RAM claims to be a node
            # pointing at address 0, and the graph drowns.
            if nxt == OT_END or nxt == 0:
                continue
        succ[i * 4] = nxt
        if nxt != OT_END:
            pointed.add(nxt)

    heads = [a for a in succ if a not in pointed]
    # A head that reaches no primitive at all is noise; one that reaches some is
    # a candidate. Walking from a LINK head is how the whole table gets covered.
    out: List[Dict[str, Any]] = []
    for h in heads:
        entries = walk_ordering_table(ram, h, max_nodes=max_nodes)
        prims = [p for p in decode_entries(entries)
                 if p["kind"] in ("poly", "rect", "line", "fill")]
        if len(prims) < min_prims:
            continue
        addrs = [int(e["src"], 16) for e in entries]
        out.append({
            "root": h,
            "nodes": len(entries),
            "prims": len(prims),
            "lo": min(addrs) if addrs else h,
            "hi": max(addrs) if addrs else h,
            "classes": sorted({p["op_name"] for p in prims}),
        })

    def score(c: Dict[str, Any]) -> Tuple[int, int]:
        # A chain containing an address you already care about wins outright:
        # the caller knows something the scan cannot.
        hit = 1 if (near is not None and c["lo"] <= (near & 0x1FFFFF) <= c["hi"]) else 0
        return (hit, c["prims"])

    out.sort(key=score, reverse=True)
    return out[:limit]


def class_on_screen(conn, op_name: str) -> Tuple[bool, Dict[str, int]]:
    """Is this primitive class currently being drawn? Also returns what is.

    Several tools depend on a routine actually RUNNING -- a write trace over its
    packets, a block probe at one of its instructions -- and when it is not,
    every one of them reports something that sounds like a different problem:
    no writes recorded, no candidate fired, no table found. Checking first turns
    all of those into one plain statement.
    """
    paused = False
    try:
        try:
            conn.cmd("pause")
            paused = True
        except DebugError:
            pass
        ram = snapshot_ram(conn)
    finally:
        if paused:
            try:
                conn.cmd("continue")
            except DebugError:
                pass
    cands = find_display_lists(ram)
    if not cands:
        return False, {}
    prims = decode_entries(walk_ordering_table(ram, cands[0]["root"]))
    counts: Dict[str, int] = {}
    for p in prims:
        if p["kind"] == "poly":
            counts[p["op_name"]] = counts.get(p["op_name"], 0) + 1
    return counts.get(op_name, 0) > 0, counts


def wait_for_class(conn, op_name: str, timeout: float = 120.0,
                   poll: float = 0.6, out=None,
                   check=None) -> Tuple[bool, Dict[str, int]]:
    """Block until a primitive class appears on screen, or time out.

    Every tool here needs the effect RUNNING, and requiring it to be on screen
    at the instant a button is clicked means racing a transient animation.
    Waiting inverts that: start the tool, then trigger the effect.

    The cost of the check is the whole design problem. class_on_screen()
    snapshots all 2 MB of RAM -- 128 reads -- and against a PAUSED oracle,
    whose socket falls back to a ~1 Hz idle timer, that is 128 seconds for ONE
    poll. A 120-second wait then completes a single partial read and reports
    "nothing", which is exactly what it did.

    So: resume first (the oracle may be parked from an earlier breakpoint), pay
    for the full scan once, then re-read only the span the display list
    occupies -- a couple of KB, one or two reads. A cached root that stops
    walking means the list moved, and the next poll pays for a rescan.
    """
    # `check` is a seam: a caller (or a test) can supply its own
    # "what is drawing" probe. The default below is the expensive-then-cheap
    # scan described above.
    if check is not None:
        deadline = time.time() + timeout
        drawing: Dict[str, int] = {}
        said = False
        while time.time() < deadline:
            try:
                on, drawing = check(conn, op_name)
                if on:
                    return True, drawing
            except DebugError:
                pass
            if out and not said:
                said = True
                print(f"  waiting for {op_name} to appear (up to {timeout:.0f}s) "
                      f"— trigger the effect now", file=out)
            time.sleep(poll)
        return False, drawing

    # A parked emulator answers about once a second; nothing below works at that
    # rate, and a tool that parked it earlier is the usual reason.
    try:
        conn.cmd("continue")
    except DebugError:
        pass

    deadline = time.time() + timeout
    said = False
    root: Optional[int] = None
    span: Optional[Tuple[int, int]] = None
    drawing: Dict[str, int] = {}

    while time.time() < deadline:
        try:
            if root is None:
                ram = snapshot_ram(conn)
                cands = find_display_lists(ram)
                if cands:
                    root = cands[0]["root"]
                    span = (cands[0]["lo"], cands[0]["hi"])
            else:
                lo = max(0, (span[0] - 0x400) & ~3)
                hi = min(RAM_SIZE, span[1] + 0x400)
                blob = read_ram_range(conn, 0x80000000 + lo, hi - lo)
                buf = bytearray(RAM_SIZE)
                buf[lo:lo + len(blob)] = blob
                ram = bytes(buf)

            prims = (decode_entries(walk_ordering_table(ram, root))
                     if root is not None else [])
            if not prims:
                root = span = None        # the list moved; rescan next poll
            drawing = {}
            for p in prims:
                if p["kind"] == "poly":
                    drawing[p["op_name"]] = drawing.get(p["op_name"], 0) + 1
            if drawing.get(op_name, 0) > 0:
                return True, drawing
        except DebugError:
            root = span = None
        if out and not said:
            said = True
            print(f"  waiting for {op_name} to appear (up to {timeout:.0f}s) — "
                  f"trigger the effect now", file=out)
        time.sleep(poll)
    return False, drawing


def dma_gpu_list_root(conn: "DebugConn") -> Optional[int]:
    """Channel 2's MADR. Almost never the root -- see find_display_lists.

    Kept because it is occasionally right if you catch a transfer in flight, and
    because knowing WHY it fails is worth more than not having it: a completed
    linked-list DMA leaves MADR holding the 0xFFFFFF terminator, so between
    frames this returns the end of the list. Returns None in that case rather
    than handing back 0x1FFFFF as if it were an address.
    """
    try:
        rep = conn.cmd("dma_state")
    except DebugError:
        return None
    for ch in rep.get("channels", []) or []:
        if ch.get("ch") == 2 or ch.get("name", "").lower().startswith("gpu"):
            madr = ch.get("madr")
            if isinstance(madr, str):
                madr = int(madr, 16)
            if not isinstance(madr, int):
                return None
            if (madr & 0xFFFFFF) == OT_END:
                return None      # the transfer finished; this is the end marker
            return madr & 0x1FFFFF
    return None
