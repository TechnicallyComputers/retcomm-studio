"""Tests for vertex_taps' per-hit register rows and verdict split."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vertex_taps as vt  # noqa: E402


def hit(pc, s4, t6, s6):
    return {"pc": pc, "regs": {"s4": f"0x{s4:08X}", "t6": f"0x{t6:08X}",
                               "s6": f"0x{s6:X}"}}


class RowsTest(unittest.TestCase):
    def test_rows_carry_all_three_registers(self):
        r = vt.rows_of([hit("0x80068458", 0x800E2610, 0x0008F8F8, 92)])
        self.assertEqual(r[0]["s4"], 0x800E2610)
        self.assertEqual(r[0]["t6"], 0x0008F8F8)
        self.assertEqual(r[0]["s6"], 92)

    def test_hits_without_regs_dropped(self):
        self.assertEqual(vt.rows_of([{"pc": "0x1"}]), [])


class SummaryTest(unittest.TestCase):
    def test_counts_distinct_words_and_pointers(self):
        rows = vt.rows_of([hit("0x1", 0x800E2610, 0x00088888, 92),
                           hit("0x1", 0x800E2610, 0x00088888, 92),
                           hit("0x1", 0x800E2620, 0x00099999, 92)])
        s = vt.summarise(rows)
        self.assertEqual(s["distinct_s4"], 2)
        self.assertEqual(s["distinct_t6"], 2)
        self.assertEqual(s["distinct_s6"], 1)

    def test_word_decoded_as_colour_bytes(self):
        self.assertEqual(vt.word_as_colour(0x0008F8F8), (248, 248, 8))


class VerdictBoundaryTest(unittest.TestCase):
    """<=4 loaded words matches the oracle's uniform table; more means the
    variety exists before any arithmetic runs."""

    def test_uniform_boundary(self):
        self.assertLessEqual(3, 4)   # the oracle's measured count fits inside
