#!/usr/bin/env python3
"""snes_frame_capture.py — capture one frame's state + attribution windows
from a running snesrecomp title into a self-contained bundle.

    # the newest frame, tagged "bad"
    python3 snes_frame_capture.py --port 4380 --tag bad --out analysis/frames

    # a specific past frame still held in the history ring
    python3 snes_frame_capture.py --port 4380 --frame 41230 --tag bad

You do not pause the game to use this. The runner's history ring keeps full
per-frame WRAM/VRAM/CGRAM/OAM plus CPU/PPU/DMA snapshots, so the workflow is
to play until the thing you care about is on screen, then reach backwards.

Writes <out>/<tag>.json (a versioned snes-frame manifest) plus the raw blobs
it names, a <tag>.png of the presented frame, and the OAM/VRAM write-trace and
dispatch-log windows — so decode and attribution later need nothing running.

The bundle is derived from ROM data: keep the output directory out of git.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from snes_frame import (  # noqa: E402
    DEFAULT_PORT, DebugConn, DebugError, parse_hex_field, write_bundle,
    write_png,
)


def bmp_to_png(bmp_path: str, png_path: str) -> tuple[int, int]:
    """The server's screenshot command writes a 24-bit top-down BMP to a path
    on its own filesystem; convert it so the bundle carries one format."""
    with open(bmp_path, "rb") as f:
        data = f.read()
    if data[:2] != b"BM":
        raise DebugError(f"{bmp_path}: not a BMP")
    offset = int.from_bytes(data[10:14], "little")
    width = int.from_bytes(data[18:22], "little")
    height = int.from_bytes(data[22:26], "little", signed=True)
    top_down = height < 0
    height = abs(height)
    stride = (width * 3 + 3) & ~3
    rgba = bytearray(width * height * 4)
    for y in range(height):
        src_y = y if top_down else height - 1 - y
        row = offset + src_y * stride
        for x in range(width):
            b, g, r = data[row + x * 3: row + x * 3 + 3]
            i = (y * width + x) * 4
            rgba[i:i + 4] = bytes((r, g, b, 255))
    write_png(png_path, width, height, bytes(rgba))
    return width, height


def capture(conn: DebugConn, outdir: str, tag: str, frame: int | None,
            trace_entries: int, wram: bool) -> str:
    info = conn.query("frame")
    live_frame = int(info.get("frame", info.get("frame_number", 0)))
    # The history ring records a frame as it completes, so its newest entry
    # is live-1 while the next frame is still running. Asking for the live
    # counter is therefore always one too new — target the ring's own newest.
    hist = conn.query("history").get("history", {})
    newest = int(hist.get("newest", live_frame - 1))
    oldest = int(hist.get("oldest", 0))
    target = newest if frame is None else frame
    if not oldest <= target <= newest:
        raise DebugError(
            f"frame {target} is not in the history ring "
            f"(holds {oldest}..{newest}) — it was evicted or never recorded")

    # Frame-keyed state from the history ring. get_frame_extended carries the
    # PPU regs the decoders need (incl. bgXsc/bgTileAdr as of the same
    # framework change that ships these tools) + CGRAM/OAM as hex blobs.
    ext = conn.query(f"get_frame_extended {target}")
    # Older framework builds snapshot fewer PPU registers than the decoders
    # need. Fall back to the LIVE registers, labelled: for a newest-frame
    # capture they are one frame stale at worst, and honest labelling beats
    # a silent layer-decode failure later.
    ppu = ext.get("ppu") or {}
    ppu_source = "ring"
    if "bgXsc" not in ppu:
        try:
            live = conn.query("get_ppu_state")
            for key in ("bgXsc", "bgTileAdr"):
                if key in live:
                    ppu[key] = live[key]
            ppu_source = "ring+live_bases"
        except DebugError:
            pass
    blobs: dict[str, bytes] = {}
    blobs["vram"] = conn.query_hex(f"dump_frame_vram {target} 0 65536")
    if wram:
        blobs["wram"] = conn.query_hex(f"dump_frame_wram {target} 0 131072")
    for name in ("cgram", "oam", "high_oam", "highOam"):
        if name in ext:
            key = "high_oam" if name in ("high_oam", "highOam") else name
            blobs[key] = bytes.fromhex(ext[name])
    if "cgram" not in blobs:
        blobs["cgram"] = conn.query_hex("dump_cgram")
    if "oam" not in blobs:
        oam = conn.query_hex("dump_oam")     # 512 low + 32 high, one blob
        blobs["oam"], blobs["high_oam"] = oam[:512], oam[512:]

    # Attribution windows. Sized generously; the joiner filters by frame.
    oam_writes = conn.query(f"oam_write_get {trace_entries}")
    try:
        vram_trace = conn.query("get_vram_trace")
    except DebugError as exc:
        vram_trace = {"unavailable": str(exc)}
    try:
        dispatch = conn.query("dispatch_log_get")
    except DebugError as exc:
        dispatch = {"unavailable": str(exc)}

    # Presented frame — live-only by nature (the ring keeps state, not pixels).
    # Labelled with the frame it was taken at so a reach-back capture is not
    # mistaken for a picture of the target frame.
    shot_bmp = os.path.abspath(os.path.join(outdir, f"{tag}.screenshot.bmp"))
    os.makedirs(outdir, exist_ok=True)
    png_size = None
    try:
        conn.query(f"screenshot {shot_bmp}")
        png_size = bmp_to_png(shot_bmp, os.path.join(outdir, f"{tag}.png"))
        os.unlink(shot_bmp)
    except (DebugError, OSError) as exc:
        print(f"warning: screenshot unavailable ({exc})", file=sys.stderr)

    manifest = {
        "tag": tag,
        "frame": target,
        "live_frame_at_capture": live_frame,
        "screenshot": {"file": f"{tag}.png", "frame": live_frame,
                       "size": png_size} if png_size else None,
        "ppu": ppu,
        "ppu_source": ppu_source,
        "cpu": ext.get("cpu"),
        "dma": ext.get("dma"),
        "oam_writes": oam_writes,
        "vram_trace": vram_trace,
        "dispatch_log": dispatch,
    }
    path = write_bundle(outdir, tag, manifest, blobs)
    digest = hashlib.sha256(blobs["vram"]).hexdigest()[:16]
    print(f"captured frame {target} (live {live_frame}) -> {path}")
    print(f"  vram sha256:{digest}  blobs: {', '.join(sorted(blobs))}")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--frame", type=int,
                    help="a past frame from the history ring (default: newest)")
    ap.add_argument("--tag", default="frame")
    ap.add_argument("--out", default="analysis/frames")
    ap.add_argument("--trace-entries", type=int, default=4000,
                    help="OAM write events to pull (server caps at 4000)")
    ap.add_argument("--wram", action="store_true",
                    help="include the 128 KiB WRAM snapshot in the bundle")
    args = ap.parse_args()

    try:
        conn = DebugConn(port=args.port, host=args.host)
    except DebugError as exc:
        print(f"snes_frame_capture: {exc}", file=sys.stderr)
        return 1
    try:
        capture(conn, args.out, args.tag, args.frame,
                args.trace_entries, args.wram)
    except DebugError as exc:
        print(f"snes_frame_capture: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
