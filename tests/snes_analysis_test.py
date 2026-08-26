#!/usr/bin/env python3
"""The SNES asset tools: decode math, attribution join, and tool inventory.

Everything runs against synthetic fixtures — hand-assembled VRAM/CGRAM/OAM
with known contents — because the classic failure mode of a tile decoder is
producing PLAUSIBLE garbage: wrong plane interleave, wrong palette window and
an image still comes out. Exact-index and exact-pixel assertions are the only
checks that catch that class.

Run:  python3 tests/snes_analysis_test.py
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import zlib
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
TOOLS = _REPO / "tools" / "snes_analysis"
sys.path.insert(0, str(TOOLS))

import snes_asset_decode as decode  # noqa: E402
import snes_frame as lib  # noqa: E402
import snes_frame_attribute as attribute  # noqa: E402

failures = 0


def check(ok: bool, what: str) -> None:
    global failures
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures += 1


# ── inventory: what Studio will launch must actually ship ────────────────────

def test_inventory() -> None:
    for name in ("snes_frame.py", "snes_frame_capture.py",
                 "snes_asset_decode.py", "snes_frame_attribute.py"):
        check((TOOLS / name).is_file(), f"tools/snes_analysis/{name} ships")


# ── pixel helpers ────────────────────────────────────────────────────────────

def test_rgb555() -> None:
    check(lib.rgb555_to_rgb888(0x7FFF) == (255, 255, 255),
          "RGB555 white expands to full 255 (bit replication)")
    check(lib.rgb555_to_rgb888(0x0000) == (0, 0, 0), "RGB555 black")
    check(lib.rgb555_to_rgb888(0x001F) == (255, 0, 0),
          "RGB555 low 5 bits are RED (BGR order, the classic swap bug)")
    check(lib.rgb555_to_rgb888(0x7C00) == (0, 0, 255), "RGB555 high 5 bits are blue")


def png_pixels(path: Path) -> tuple[int, int, bytes]:
    """Decode our own minimal PNG (8-bit RGBA, filter 0) back to raw pixels."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    width = height = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height = struct.unpack(">II", payload[:8])
        elif kind == b"IDAT":
            idat += payload
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4 + 1
    out = b"".join(raw[y * stride + 1:(y + 1) * stride]
                   for y in range(height))
    return width, height, out


def test_png_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as d:
        px = bytes(range(64))        # 8x2 RGBA = 64 bytes
        p = Path(d) / "t.png"
        lib.write_png(str(p), 8, 2, px)
        w, h, back = png_pixels(p)
        check((w, h, back) == (8, 2, px), "PNG writer round-trips exactly")


# ── tile decode: exact indices from hand-built planes ────────────────────────

def test_tile_decode_2bpp() -> None:
    vram = bytearray(0x10000)
    # Row 0: plane0 = 1000_0001, plane1 = 0000_0001
    #   → x=0 index 1, x=7 index 3 (both planes), rest 0.
    vram[0], vram[1] = 0b10000001, 0b00000001
    t = decode.decode_tile(bytes(vram), 0, 2)
    check(t[0][0] == 1 and t[0][7] == 3 and t[0][1] == 0,
          "2bpp: planes interleave into indices (1 and 3 where expected)")
    check(all(v == 0 for v in t[1]), "2bpp: untouched rows decode to 0")


def test_tile_decode_4bpp() -> None:
    vram = bytearray(0x10000)
    # 4bpp planes 0/1 in words 0-7, planes 2/3 in words 8-15.
    # Set only plane 3, row 0, leftmost pixel → index 8.
    vram[16 + 1] = 0b10000000   # word 8 (byte 16), second byte = plane 3
    t = decode.decode_tile(bytes(vram), 0, 4)
    check(t[0][0] == 8 and t[0][1] == 0,
          "4bpp: plane 3 sits 8 words in (index 8, not a shifted 2bpp read)")


def test_tilemap_layer_compose() -> None:
    """A 1-tile map entry with palette 2 + hflip renders to the exact pixels."""
    vram = bytearray(0x10000)
    cgram = bytearray(512)

    # Char base $2000 words: tile 5 (2bpp) row 0 = index 1 at x=0 only.
    char_words = 0x2000
    tile_addr = (char_words + 5 * 16) * 2
    vram[tile_addr] = 0b10000000
    # Tilemap at $0000 words: entry (0,0) = tile 5, palette 2, hflip.
    entry = 5 | (2 << 10) | 0x4000
    vram[0], vram[1] = entry & 0xFF, entry >> 8
    # Mode 1 BG3 is the 2bpp layer; its palettes are 4 entries wide from 0.
    # Palette 2 entry 1 = pure red.
    cgram[(2 * 4 + 1) * 2] = 0x1F
    ppu = {"bgmode": 1, "bgXsc": ["0x00", "0x00", "0x00", "0x00"],
           "bgTileAdr": hex(0x2 << 8),   # BG3 nibble → char base $2000 words
           "hScroll": [0, 0, 0, 0], "vScroll": [0, 0, 0, 0]}
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "bg3.png"
        meta = decode.render_bg_layer(bytes(vram), decode.cgram_words(bytes(cgram)),
                                      ppu, 2, str(out))
        w, h, px = png_pixels(out)
        check(meta["bpp"] == 2 and meta["char_words"] == 0x2000,
              "layer metadata reflects mode 1 BG3 + char base")
        # hflip moves the lit pixel from x=0 to x=7.
        def pixel(x, y):
            i = (y * w + x) * 4
            return tuple(px[i:i + 4])
        check(pixel(7, 0) == (255, 0, 0, 255),
              "hflip entry renders palette-2 red at x=7")
        check(pixel(0, 0) == (0, 0, 0, 0),
              "index 0 stays transparent at x=0")


def test_mode7_is_flagged_not_wrong() -> None:
    ppu = {"bgmode": 7, "bgXsc": ["0", "0", "0", "0"], "bgTileAdr": "0",
           "hScroll": [0] * 4, "vScroll": [0] * 4}
    with tempfile.TemporaryDirectory() as d:
        meta = decode.render_bg_layer(bytes(0x10000), [0] * 256, ppu, 0,
                                      str(Path(d) / "x.png"))
        check("deferred" in meta.get("skipped", ""),
              "Mode 7 reports itself deferred instead of emitting garbage")


# ── BRR decode ───────────────────────────────────────────────────────────────

def test_brr_silence_and_termination() -> None:
    # Two blocks: filter 0 / shift 0 nibbles of 0, then an END block.
    block = bytes([0x00] + [0] * 8)
    end = bytes([0x01] + [0] * 8)
    samples, off = decode.decode_brr(block + end, 0)
    check(len(samples) == 32 and all(s == 0 for s in samples),
          "BRR: two silent blocks decode to 32 zero samples")
    check(off == 18, "BRR: END flag terminates the walk")


# ── attribution join ─────────────────────────────────────────────────────────

def _manifest(oam_events, vram_log) -> dict:
    return {"frame": 100,
            "oam_writes": {"events": oam_events},
            "vram_trace": {"log": vram_log}}


def test_attribution_names_both_tiers() -> None:
    m = _manifest(
        oam_events=[
            # slot 3 written twice by an AOT function, once by interp
            {"seq": 1, "f": 100, "h": 0, "i": 6, "v": "0x1234",
             "func": "DrawPlayerSprite"},
            {"seq": 2, "f": 100, "h": 0, "i": 7, "v": "0x0042",
             "func": "DrawPlayerSprite"},
            {"seq": 3, "f": 100, "h": 0, "i": 6, "v": "0x9999",
             "func": "interp@$BB8CB5"},
            # a high-table write must not pollute slot attribution
            {"seq": 4, "f": 100, "h": 1, "i": 0, "v": "0x03",
             "func": "Ignored"},
        ],
        vram_log=[
            {"f": 100, "adr_byte": "0x4000", "val": "0x11",
             "func": "interp@$BB8CB5"},
        ])
    result = attribute.attribute(m)
    slot3 = result["oam_slots"]["3"]
    check(slot3["writer"] == "DrawPlayerSprite" and slot3["count"] == 2,
          "majority writer wins the slot (AOT name)")
    check("interp@$BB8CB5" in slot3["all"],
          "the interpreted writer is still recorded in the breakdown")
    region = result["vram_regions"]["8"]     # byte 0x4000 → word 0x2000 → granule 8
    check(region["writer"] == "interp@$BB8CB5",
          "VRAM region attributes to the interp scope name")

    toml = attribute.draft_suggestions(result)
    check("Unknown_BB8CB5" in toml and 'addr = "8cb5"' in toml
          and "bank = 187" in toml,
          "interp writer becomes a draft [[func]] with addr+bank split out")
    check("DrawPlayerSprite" not in toml,
          "already-named AOT writers draft nothing")


def test_attribution_survives_missing_traces() -> None:
    result = attribute.attribute({"frame": 1,
                                  "vram_trace": {"unavailable": "ring off"}})
    check(result["vram_regions"].get("unavailable") == "ring off",
          "an unavailable trace is reported, not fabricated")
    toml = attribute.draft_suggestions(result)
    check("No interpreted writers" in toml,
          "empty evidence produces an honest empty draft file")


# ── bundle round-trip ────────────────────────────────────────────────────────

def test_bundle_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = lib.write_bundle(d, "t", {"tag": "t", "frame": 7},
                                {"vram": b"\x01\x02", "cgram": b"\x03"})
        manifest, blobs = lib.read_bundle(path)
        check(manifest["frame"] == 7 and blobs["vram"] == b"\x01\x02",
              "bundle round-trips manifest + blobs")
        # a truncated blob must be caught, not silently decoded
        (Path(d) / "t.vram.bin").write_bytes(b"\x01")
        try:
            lib.read_bundle(path)
            check(False, "truncated blob raises")
        except lib.DebugError:
            check(True, "truncated blob raises")


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print(f"\n{'FAILED' if failures else 'PASSED'} "
          f"({failures} failures)" if failures else "\nPASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
