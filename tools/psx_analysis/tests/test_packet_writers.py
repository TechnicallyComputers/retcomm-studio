#!/usr/bin/env python3
"""Packet field mapping: which words of a packet are COLOUR words.

Getting this wrong is worse than not having it. A wrong map still produces a
confident-looking table of "instructions that write colour" -- it just names
the wrong instructions, and the reader has no way to tell. So the layout is
derived from each decoded packet rather than from an assumed stride, and these
tests pin the four layouts that matter.
"""

import importlib.util
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


GF = _load("psx_gpu_frame")
PW = _load("packet_writers")


def packet(op, payload, at=0x1000):
    ram = bytearray(GF.RAM_SIZE)
    words = [((len(payload) & 0xFF) << 24) | GF.OT_END] + payload
    for i, w in enumerate(words):
        struct.pack_into("<I", ram, at + 4 * i, w & 0xFFFFFFFF)
    return GF.decode_entries(GF.walk_ordering_table(bytes(ram), at))


class TestFieldMap(unittest.TestCase):
    def roles(self, prims, want):
        m = PW.field_map(prims, want)
        return [m[a] for a in sorted(m)]

    def test_gouraud_quad_interleaves_colour_and_vertex(self):
        p = packet(0x38, [(0x38 << 24) | 1, 0x10, 2, 0x20, 3, 0x30, 4, 0x40])
        self.assertEqual(self.roles(p, "PolyG4|x"),
                         ["colour", "vertex"] * 4)

    def test_flat_triangle_has_one_leading_colour(self):
        p = packet(0x20, [(0x20 << 24) | 1, 0x10, 0x20, 0x30])
        self.assertEqual(self.roles(p, "PolyF3|x"),
                         ["colour", "vertex", "vertex", "vertex"])

    def test_textured_flat_quad_carries_a_uv_after_each_vertex(self):
        p = packet(0x2C, [(0x2C << 24) | 1, 0x10, 0, 0x20, 0, 0x30, 0, 0x40, 0])
        self.assertEqual(self.roles(p, "PolyFT4|x"),
                         ["colour"] + ["vertex", "uv"] * 4)

    def test_textured_gouraud_quad(self):
        p = packet(0x3C, [(0x3C << 24) | 1, 0x10, 0, 2, 0x20, 0,
                          3, 0x30, 0, 4, 0x40, 0])
        self.assertEqual(self.roles(p, "PolyGT4|x"),
                         ["colour", "vertex", "uv"] * 4)

    def test_the_class_filter_excludes_other_opcodes(self):
        ram = bytearray(GF.RAM_SIZE)

        def put(off, *w):
            for i, x in enumerate(w):
                struct.pack_into("<I", ram, off + 4 * i, x & 0xFFFFFFFF)

        put(0x1000, (8 << 24) | 0x2000, (0x38 << 24) | 1, 0x10, 2, 0x20,
            3, 0x30, 4, 0x40)
        put(0x2000, (4 << 24) | GF.OT_END, (0x20 << 24) | 9, 0x10, 0x20, 0x30)
        prims = GF.decode_entries(GF.walk_ordering_table(bytes(ram), 0x1000))
        only_g4 = PW.field_map(prims, "PolyG4|x")
        self.assertEqual(len(only_g4), 8, "should map the gouraud quad alone")

    def test_addresses_are_absolute_not_offsets(self):
        p = packet(0x38, [(0x38 << 24) | 1, 0x10, 2, 0x20, 3, 0x30, 4, 0x40],
                   at=0x10D1C0)
        m = PW.field_map(p, "PolyG4|x")
        # src is the payload start, i.e. the tag address + 4.
        self.assertIn(0x10D1C4, m)
        self.assertEqual(m[0x10D1C4], "colour")


if __name__ == "__main__":
    unittest.main()
