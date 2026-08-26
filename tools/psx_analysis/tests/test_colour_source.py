"""Tests for locating and decoding the effect's colour source table."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import colour_source as cs  # noqa: E402


def sample(v, reg="s4"):
    return {"regs": {reg: f"0x{v:08X}"}}


class PointerSpanTest(unittest.TestCase):
    def test_span_widened_backwards(self):
        """The code reads $s4-12..$s4-1, so the table starts before $s4."""
        (lo, hi), vals = cs.pointer_span([sample(0x80100010),
                                          sample(0x80100020)])
        self.assertEqual(lo, 0x80100010 - cs.SRC_BACK)
        self.assertEqual(hi, 0x80100020)
        self.assertEqual(vals, [0x80100010, 0x80100020])

    def test_no_samples_returns_none(self):
        self.assertIsNone(cs.pointer_span([{"regs": {}}, {}]))

    def test_duplicate_pointers_collapsed(self):
        _, vals = cs.pointer_span([sample(0x80100010)] * 5)
        self.assertEqual(vals, [0x80100010])


class DecodeTest(unittest.TestCase):
    def test_decodes_little_endian_colour_words(self):
        blob = bytes([0x93, 0x50, 0x04, 0x00,      # (147, 80, 4)
                      0x93, 0x93, 0x68, 0x00])     # (147, 147, 104)
        cols = cs.colours_in(blob, 0x80100000, 0x80100000, 0x80100004)
        self.assertEqual(cols[(147, 80, 4)], 1)
        self.assertEqual(cols[(147, 147, 104)], 1)

    def test_command_byte_ignored(self):
        """The top byte is the GP0 command, not colour."""
        blob = bytes([0x93, 0x50, 0x04, 0x38])
        cols = cs.colours_in(blob, 0x80100000, 0x80100000, 0x80100000)
        self.assertEqual(list(cols), [(147, 80, 4)])

    def test_out_of_range_offsets_skipped(self):
        cols = cs.colours_in(b"\x01\x02\x03\x04", 0x80100000,
                             0x80100000, 0x80100100)
        self.assertEqual(sum(cols.values()), 1)


class VerdictTest(unittest.TestCase):
    def test_uniform_table_points_downstream(self):
        v, why = cs.verdict_of(3)
        self.assertEqual(v, "source-is-uniform")
        self.assertIn("not in the table", why)

    def test_varied_table_points_at_its_filler(self):
        v, why = cs.verdict_of(153)
        self.assertEqual(v, "source-is-varied")
        self.assertIn("FILLS this table", why)

    def test_empty_refuses(self):
        v, _ = cs.verdict_of(0)
        self.assertEqual(v, "no-source")

    def test_boundary_is_inclusive(self):
        self.assertEqual(cs.verdict_of(8)[0], "source-is-uniform")
        self.assertEqual(cs.verdict_of(9)[0], "source-is-varied")


class OraclePointerTest(unittest.TestCase):
    """DuckStation has pc_break (all GPRs) but no pc_probe.

    One hit is enough: the question is which table the pointer names, not how
    it varies. The breakpoint must not survive -- a leaked one re-pauses the
    emulator and outlives the tool that set it.
    """

    class Conn:
        def __init__(self, vals):
            self.vals = list(vals)
            self.armed = set()
            self.sent = []

        def _d(self, name, **kw):
            self.sent.append(name)
            if name == "pc_break":
                a = kw.get("addr")
                if a in self.armed:
                    return {"ok": False, "slot": -1}
                self.armed.add(a)
                return {"ok": True, "slot": 0}
            if name == "pc_unbreak":
                self.armed.discard(kw.get("addr"))
                return {"ok": True}
            if name == "pc_break_list":
                return {"breaks": sorted(self.armed)}
            if name == "pc_hit_last":
                if not self.vals:
                    return {"valid": False}
                v = self.vals.pop(0)
                return {"valid": True, "regs": {"s4": f"0x{v:08X}"}}
            return {}

        def cmd(self, n, **k):
            return self._d(n, **k)

        def raw(self, n, **k):
            return self._d(n, **k)

    def setUp(self):
        import psx_gpu_frame
        self.m = psx_gpu_frame
        self._s = cs.time.sleep
        cs.time.sleep = lambda *_: None
        self._s2 = psx_gpu_frame.time.sleep
        psx_gpu_frame.time.sleep = lambda *_: None

    def tearDown(self):
        cs.time.sleep = self._s
        self.m.time.sleep = self._s2

    def test_collects_distinct_pointers(self):
        c = self.Conn([0x800E2610, 0x800E2610, 0x800E2640])
        vals = cs.oracle_pointer(c, "0x80068450", tries=3)
        self.assertEqual(vals, [0x800E2610, 0x800E2640])

    def test_breakpoint_never_leaks(self):
        c = self.Conn([0x800E2610])
        cs.oracle_pointer(c, "0x80068450", tries=2)
        self.assertEqual(c.armed, set())

    def test_no_hits_returns_empty_not_error(self):
        c = self.Conn([])
        self.assertEqual(cs.oracle_pointer(c, "0x80068450", tries=2), [])
