"""Tests for slice_check's address derivation and per-frame grouping."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slice_check as sc  # noqa: E402


def quad(src, op="PolyG4+semi", stp=1, semi=True):
    return {"op_name": op, "stp": stp, "semi": semi, "src": f"0x{src:08X}"}


class ColourAddrTest(unittest.TestCase):
    def test_four_colour_words_per_quad(self):
        a = sc.colour_addrs({"prims": [quad(0x0010D078)]})
        self.assertEqual(a, {0x10D078, 0x10D080, 0x10D088, 0x10D090})

    def test_other_classes_excluded(self):
        d = {"prims": [quad(0x0010D078, op="PolyF4+semi", stp=2)]}
        self.assertEqual(sc.colour_addrs(d), set())

    def test_opaque_excluded(self):
        d = {"prims": [quad(0x0010D078, semi=False)]}
        self.assertEqual(sc.colour_addrs(d), set())


class PerFrameTest(unittest.TestCase):
    ADDRS = {0x10D078, 0x10D080}

    def entry(self, frame, addr, val):
        return {"frame": frame, "addr": f"0x{addr:08X}", "new": f"0x{val:08X}"}

    def test_groups_by_frame(self):
        es = [self.entry(10, 0x10D078, 0x3A0011), self.entry(11, 0x10D080, 5)]
        fr = sc.per_frame(es, self.ADDRS, 0x10D078, 0x10D090)
        self.assertEqual(len(fr[10]["addrs"]), 1)
        self.assertEqual(len(fr[11]["addrs"]), 1)

    def test_other_buffer_copy_folds_in(self):
        import packet_writers as pw
        es = [self.entry(10, 0x10D078 + pw.BUFFER_STRIDE, 7)]
        fr = sc.per_frame(es, self.ADDRS, 0x10D078, 0x10D090)
        self.assertIn(0x10D078, fr[10]["addrs"])

    def test_command_byte_masked_from_values(self):
        es = [self.entry(10, 0x10D078, 0x3A005160),
              self.entry(10, 0x10D080, 0x00005160)]
        fr = sc.per_frame(es, self.ADDRS, 0x10D078, 0x10D090)
        self.assertEqual(fr[10]["vals"], {0x005160})

    def test_unrelated_addresses_ignored(self):
        es = [self.entry(10, 0x080000, 7)]
        self.assertEqual(len(sc.per_frame(es, self.ADDRS, 0x10D078,
                                          0x10D090)), 0)


class VerdictTest(unittest.TestCase):
    def test_full_rewrite_kills_the_hypothesis(self):
        v, why = sc.verdict_of([250, 256, 251], 256)
        self.assertEqual(v, "full-rewrite")
        self.assertIn("dead", why)

    def test_slice_confirms_and_predicts_residency(self):
        v, why = sc.verdict_of([16, 16, 16], 256)
        self.assertEqual(v, "amortised-slice")
        self.assertIn("~16", why)
        self.assertIn("$s6", why)

    def test_no_writes_refuses(self):
        v, _ = sc.verdict_of([], 256)
        self.assertEqual(v, "no-writes")
