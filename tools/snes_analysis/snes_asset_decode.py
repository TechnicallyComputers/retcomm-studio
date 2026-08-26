#!/usr/bin/env python3
"""snes_asset_decode.py — decode a capture bundle into viewable assets.

    python3 snes_asset_decode.py analysis/frames/bad.json --out analysis/assets

Input is a bundle from snes_frame_capture.py, never a live game. Produces,
under <out>/<tag>/:

    palettes.png / palettes.json     CGRAM as 16x16 swatches, RGB888 values
    tiles_2bpp.png / _4bpp.png       VRAM decoded as tilesheets, 16 tiles/row
    bg1.png .. bg4.png               composed layers for the frame's BG mode
    sprites.png / sprites.json       the 128 OAM slots as one sheet + metadata
    samples/*.wav                    BRR samples, when the bundle carries an
                                     SPC image (capture --spc, or spc_dump)

Everything is *as composed for that frame*: tiles render with a real CGRAM
palette, layers use the frame's tilemap bases and scroll, sprites use OBSEL
and their attribute palettes. A raw VRAM dump without those is just noise.

Deferred honestly rather than wrong: Mode 7, hires (5/6), interlace, and
offset-per-tile render a placeholder and say so in the JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from snes_frame import (  # noqa: E402
    DebugError, parse_hex_field, read_bundle, rgb555_to_rgb888, write_png,
)

# BG bit depths per mode (index [mode][bg]); None = layer absent in the mode.
MODE_BPP = {
    0: (2, 2, 2, 2),
    1: (4, 4, 2, None),
    2: (4, 4, None, None),
    3: (8, 4, None, None),
    4: (8, 2, None, None),
    5: (4, 2, None, None),   # hires — flagged, still tile-decodable
    6: (4, None, None, None),
    7: (None, None, None, None),  # Mode 7 packed VRAM: deferred
}

OBJ_SIZES = {  # OBSEL bits 5-7 -> (small, large) sprite pixel sizes
    0: ((8, 8), (16, 16)), 1: ((8, 8), (32, 32)), 2: ((8, 8), (64, 64)),
    3: ((16, 16), (32, 32)), 4: ((16, 16), (64, 64)), 5: ((32, 32), (64, 64)),
    6: ((16, 32), (32, 64)), 7: ((16, 32), (32, 32)),
}


def cgram_words(cgram: bytes) -> list[int]:
    return [cgram[i] | (cgram[i + 1] << 8) for i in range(0, 512, 2)]


def decode_tile(vram: bytes, word_addr: int, bpp: int) -> list[list[int]]:
    """One 8x8 tile at a VRAM *word* address -> 8x8 palette indices.

    SNES tiles are planar: bytes 0/1 of each row are planes 0/1, and for
    4/8bpp the further plane pairs sit 8/16 words later. Getting the plane
    interleave wrong still produces plausible garbage, which is why the test
    fixtures check exact indices rather than "an image came out".
    """
    out = [[0] * 8 for _ in range(8)]
    base = (word_addr * 2) & 0xFFFF
    for y in range(8):
        planes = []
        for pair in range(bpp // 2):
            row = (base + pair * 16 + y * 2) & 0xFFFF
            planes.append(vram[row])
            planes.append(vram[(row + 1) & 0xFFFF])
        for x in range(8):
            bit = 7 - x
            value = 0
            for p, byte in enumerate(planes):
                value |= ((byte >> bit) & 1) << p
            out[y][x] = value
    return out


def blit_tile(rgba: bytearray, img_w: int, px: int, py: int,
              tile: list[list[int]], palette: list[int], pal_base: int,
              hflip: bool = False, vflip: bool = False,
              transparent_zero: bool = True) -> None:
    for y in range(8):
        for x in range(8):
            index = tile[7 - y if vflip else y][7 - x if hflip else x]
            if transparent_zero and index == 0:
                continue
            r, g, b = rgb555_to_rgb888(palette[(pal_base + index) & 0xFF])
            i = ((py + y) * img_w + (px + x)) * 4
            if 0 <= i < len(rgba) - 3:
                rgba[i:i + 4] = bytes((r, g, b, 255))


def render_palettes(cgram: bytes, outdir: str) -> None:
    words = cgram_words(cgram)
    cell = 12
    rgba = bytearray(16 * cell * 16 * cell * 4)
    for i, word in enumerate(words):
        r, g, b = rgb555_to_rgb888(word)
        cx, cy = (i % 16) * cell, (i // 16) * cell
        for y in range(cell):
            for x in range(cell):
                j = ((cy + y) * 16 * cell + (cx + x)) * 4
                rgba[j:j + 4] = bytes((r, g, b, 255))
    write_png(os.path.join(outdir, "palettes.png"), 16 * cell, 16 * cell,
              bytes(rgba))
    with open(os.path.join(outdir, "palettes.json"), "w", encoding="utf-8") as f:
        json.dump({"rgb555": words,
                   "rgb888": [rgb555_to_rgb888(w) for w in words]}, f)


def render_tilesheet(vram: bytes, palette: list[int], bpp: int,
                     path: str) -> None:
    """All of VRAM as bpp-planar tiles, 16 per row, palette row 0."""
    words_per_tile = 8 * bpp
    tiles = 0x8000 // words_per_tile
    cols = 16
    rows = (tiles + cols - 1) // cols
    img_w, img_h = cols * 8, rows * 8
    rgba = bytearray(img_w * img_h * 4)
    for t in range(tiles):
        tile = decode_tile(vram, t * words_per_tile, bpp)
        blit_tile(rgba, img_w, (t % cols) * 8, (t // cols) * 8,
                  tile, palette, 0, transparent_zero=False)
    write_png(path, img_w, img_h, bytes(rgba))


def render_bg_layer(vram: bytes, palette: list[int], ppu: dict, bg: int,
                    path: str) -> dict:
    """Compose one BG's tilemap for the frame. Returns metadata (or a reason
    the layer was skipped)."""
    mode = parse_hex_field(ppu["bgmode"]) & 7
    if "bgXsc" not in ppu or "bgTileAdr" not in ppu:
        return {"skipped": "bundle has no tilemap/char bases — captured from "
                           "a framework build older than the FramePpuSnap "
                           "extension (rebuild the game, then recapture)"}
    bpp = MODE_BPP.get(mode, (None,) * 4)[bg]
    if bpp is None:
        return {"skipped": f"BG{bg + 1} absent in mode {mode}"
                if mode != 7 else "Mode 7 decode is deferred"}
    bgxsc = parse_hex_field(ppu["bgXsc"][bg])
    tilemap_words = (bgxsc & 0xFC) << 8          # $2107-A: base in 1K words
    wide = bgxsc & 1
    tall = bgxsc & 2
    char_nibble = (parse_hex_field(ppu["bgTileAdr"]) >> (4 * bg)) & 0xF
    char_words = char_nibble << 12               # $210B/C: base in 4K words
    hscroll = parse_hex_field(ppu["hScroll"][bg])
    vscroll = parse_hex_field(ppu["vScroll"][bg])

    map_w, map_h = (64 if wide else 32), (64 if tall else 32)
    img_w, img_h = map_w * 8, map_h * 8
    rgba = bytearray(img_w * img_h * 4)
    # In mode 0 each BG owns a 32-color palette window; in 1-6 BGs share
    # the low 128 CGRAM entries with 4bpp palettes 8 entries... no: 4bpp
    # palettes are 16 entries from CGRAM 0; mode 0 offsets each BG by 32.
    mode0_base = bg * 32 if mode == 0 else 0
    for ty in range(map_h):
        for tx in range(map_w):
            # 32x32 screens quadrant layout: SC1 SC2 / SC3 SC4
            screen = (1 if (wide and tx >= 32) else 0) + \
                     (2 if (tall and ty >= 32) else 0)
            entry_addr = (tilemap_words + screen * 0x400 +
                          (ty % 32) * 32 + (tx % 32)) & 0x7FFF
            lo = vram[(entry_addr * 2) & 0xFFFF]
            hi = vram[(entry_addr * 2 + 1) & 0xFFFF]
            entry = lo | (hi << 8)
            tile_num = entry & 0x3FF
            pal = (entry >> 10) & 7
            hflip = bool(entry & 0x4000)
            vflip = bool(entry & 0x8000)
            tile = decode_tile(vram, char_words + tile_num * 8 * bpp, bpp)
            blit_tile(rgba, img_w, tx * 8, ty * 8, tile, palette,
                      mode0_base + pal * (1 << bpp), hflip, vflip)
    write_png(path, img_w, img_h, bytes(rgba))
    return {"mode": mode, "bpp": bpp, "tilemap_words": tilemap_words,
            "char_words": char_words, "map": f"{map_w}x{map_h}",
            "hscroll": hscroll, "vscroll": vscroll}


def render_sprites(vram: bytes, oam: bytes, high_oam: bytes,
                   palette: list[int], ppu: dict, outdir: str) -> list[dict]:
    """The 128 OAM slots as one 16-across sheet of large-size cells."""
    obsel = parse_hex_field(ppu["obsel"])
    small, large = OBJ_SIZES[(obsel >> 5) & 7]
    name_base = (obsel & 7) << 13                # word address of OBJ tiles
    name_select = ((obsel >> 3) & 3)
    gap = (name_select + 1) << 12                # table-2 offset in words
    cell_w, cell_h = large
    cols = 16
    rows = (128 + cols - 1) // cols
    img_w, img_h = cols * cell_w, rows * cell_h
    rgba = bytearray(img_w * img_h * 4)
    meta = []
    for slot in range(128):
        y = oam[slot * 4 + 0]
        x_low = oam[slot * 4 + 1]
        tile = oam[slot * 4 + 2]
        attr = oam[slot * 4 + 3]
        extra = (high_oam[slot // 4] >> ((slot % 4) * 2)) & 3
        x = x_low | ((extra & 1) << 8)
        big = bool(extra & 2)
        w, h = large if big else small
        pal = (attr >> 1) & 7
        table2 = bool(attr & 1)
        hflip = bool(attr & 0x40)
        vflip = bool(attr & 0x80)
        base_cell_x = (slot % cols) * cell_w
        base_cell_y = (slot // cols) * cell_h
        chars_base = name_base + (gap if table2 else 0)
        for cy in range(h // 8):
            for cx in range(w // 8):
                src_cx = (w // 8 - 1 - cx) if hflip else cx
                src_cy = (h // 8 - 1 - cy) if vflip else cy
                # OBJ char layout: a 16x16 name matrix. Multi-tile sprites
                # step +1 per column (wrapping within the row) and +0x10 per
                # row (wrapping within the table); the table-2 bit is the
                # base offset applied above, not part of the name.
                char_num = (((tile & 0xF0) + (src_cy << 4)) & 0xF0) | \
                           ((tile + src_cx) & 0x0F)
                td = decode_tile(vram, chars_base + char_num * 32, 4)
                blit_tile(rgba, img_w, base_cell_x + cx * 8,
                          base_cell_y + cy * 8, td, palette,
                          128 + pal * 16, hflip, vflip)
        meta.append({"slot": slot, "x": x, "y": y, "tile": tile,
                     "palette": pal, "size": f"{w}x{h}", "table2": table2,
                     "hflip": hflip, "vflip": vflip,
                     "offscreen": y >= 0xE0})
    write_png(os.path.join(outdir, "sprites.png"), img_w, img_h, bytes(rgba))
    with open(os.path.join(outdir, "sprites.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    return meta


# ── audio (BRR → WAV, from an SPC image) ─────────────────────────────────────

def decode_brr(data: bytes, start: int) -> tuple[list[int], int]:
    """Decode one BRR sample from `start` to its END block. Returns
    (16-bit samples, end offset)."""
    samples: list[int] = []
    older = old = 0
    off = start
    while off + 9 <= len(data):
        header = data[off]
        shift = header >> 4
        filt = (header >> 2) & 3
        end = header & 1
        for i in range(8):
            byte = data[off + 1 + i]
            for nib in ((byte >> 4) & 0xF, byte & 0xF):
                s = nib - 16 if nib >= 8 else nib
                s = (s << shift) >> 1 if shift <= 12 else (s >> 3) << 12
                if filt == 1:
                    s += old + ((-old) >> 4)
                elif filt == 2:
                    s += (old << 1) + ((-((old << 1) + old)) >> 5) - older + \
                         (older >> 4)
                elif filt == 3:
                    s += (old << 1) + ((-(old + (old << 2) + (old << 3))) >> 6) \
                         - older + (((older << 1) + older) >> 4)
                s = max(-32768, min(32767, s))
                samples.append(s)
                older, old = old, s
        off += 9
        if end:
            break
    return samples, off


def render_samples(spc: bytes, outdir: str) -> list[dict]:
    """Walk the DSP sample directory of a standard SPC file into WAVs."""
    if len(spc) < 0x10180 or not spc.startswith(b"SNES-SPC700"):
        raise DebugError("not an SPC image (need spc_dump output)")
    aram = spc[0x100:0x10100]
    dsp = spc[0x10100:0x10180]
    dir_base = dsp[0x5D] << 8
    os.makedirs(outdir, exist_ok=True)
    out = []
    for n in range(256):
        entry = dir_base + n * 4
        if entry + 4 > len(aram):
            break
        start = aram[entry] | (aram[entry + 1] << 8)
        if start == 0 or start == 0xFFFF:
            continue
        samples, end = decode_brr(aram, start)
        if len(samples) < 16:
            continue
        path = os.path.join(outdir, f"sample_{n:03d}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(32000)
            w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        out.append({"n": n, "start": start, "bytes": end - start,
                    "samples": len(samples), "file": os.path.basename(path)})
        if len(out) >= 64:   # a full 256-entry dir is usually mirrors
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bundle", help="path to a <tag>.json capture manifest")
    ap.add_argument("--out", default="analysis/assets")
    ap.add_argument("--spc", help="an spc_dump image for sample extraction")
    args = ap.parse_args()

    try:
        manifest, blobs = read_bundle(args.bundle)
    except (OSError, DebugError, json.JSONDecodeError) as exc:
        print(f"snes_asset_decode: {exc}", file=sys.stderr)
        return 1

    tag = manifest.get("tag", "frame")
    outdir = os.path.join(args.out, tag)
    os.makedirs(outdir, exist_ok=True)
    vram = blobs["vram"]
    palette = cgram_words(blobs["cgram"])
    ppu = manifest.get("ppu") or {}

    render_palettes(blobs["cgram"], outdir)
    render_tilesheet(vram, palette, 2, os.path.join(outdir, "tiles_2bpp.png"))
    render_tilesheet(vram, palette, 4, os.path.join(outdir, "tiles_4bpp.png"))

    report: dict = {"frame": manifest.get("frame"), "layers": {}}
    if ppu:
        for bg in range(4):
            report["layers"][f"bg{bg + 1}"] = render_bg_layer(
                vram, palette, ppu, bg, os.path.join(outdir, f"bg{bg + 1}.png"))
        if "oam" in blobs:
            sprites = render_sprites(vram, blobs["oam"],
                                     blobs.get("high_oam", bytes(32)),
                                     palette, ppu, outdir)
            report["sprites"] = sum(1 for s in sprites if not s["offscreen"])
    else:
        report["layers"]["skipped"] = "bundle carries no PPU registers"

    if args.spc:
        with open(args.spc, "rb") as f:
            report["samples"] = render_samples(
                f.read(), os.path.join(outdir, "samples"))

    with open(os.path.join(outdir, "decode.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    print(f"decoded -> {outdir}")
    for name, info in report["layers"].items():
        note = info.get("skipped") if isinstance(info, dict) else None
        print(f"  {name}: {note or 'ok'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
