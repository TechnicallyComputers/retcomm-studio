#!/usr/bin/env python3
"""Tests for the coverage gate in fingerprint_diff.

The gate is the whole point of the tool being trustworthy. Two emulators can
only be compared by write-hash if they observe the same set of writes, and they
do not by default: psxrecomp hooks its memory.c write paths, the oracle hooks
the interpreter's store path, and DMA-written RAM passes through one and not
the other. A hash mismatch caused by that is indistinguishable from a real
guest divergence unless something checks first -- so these pin the refusal.
"""

import importlib.util
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


FD = _load("fingerprint_diff")


def rows(counts, h="0xabc"):
    return {f: {"frame": f, "wc": c, "wr": h} for f, c in counts.items()}


class TestCoverageGate(unittest.TestCase):
    def test_matching_counts_pass(self):
        a = rows({1: 1000, 2: 2100, 3: 3050})
        b = rows({1: 1010, 2: 2090, 3: 3070})
        ok, why = FD.coverage_verdict(a, b, [1, 2, 3])
        self.assertTrue(ok, why)

    def test_a_side_seeing_far_fewer_writes_is_refused(self):
        # The recompiler-mode trap: the oracle's hook is never reached, so it
        # records a trickle. Hashes would "differ" for a reason that has
        # nothing to do with the guest.
        a = rows({1: 100000, 2: 200000})
        b = rows({1: 300, 2: 610})
        ok, why = FD.coverage_verdict(a, b, [1, 2])
        self.assertFalse(ok)
        self.assertIn("not observing the same", why)

    def test_a_side_recording_nothing_is_refused(self):
        a = rows({1: 5000})
        b = rows({1: 0})
        ok, why = FD.coverage_verdict(a, b, [1])
        self.assertFalse(ok)
        self.assertIn("no writes", why)

    def test_drifting_ratio_is_refused_even_when_the_median_looks_fine(self):
        # Median ~1.0 but the ratio swings wildly: the two are tracking
        # different things and happen to cross over. A median-only check would
        # wave this through.
        a = rows({1: 1000, 2: 1000, 3: 1000})
        b = rows({1: 100, 2: 1000, 3: 9000})
        ok, why = FD.coverage_verdict(a, b, [1, 2, 3])
        self.assertFalse(ok)
        self.assertIn("drifts", why)

    def test_no_overlap_is_not_a_pass(self):
        ok, why = FD.coverage_verdict({}, {}, [])
        self.assertIsNone(ok)


class TestColumns(unittest.TestCase):
    def test_ram_scratchpad_mmio_and_pc_are_kept_separate(self):
        keys = [c[0] for c in FD.COLUMNS]
        self.assertEqual(keys, ["wr", "pc", "mmio", "sp"])

    def test_every_counted_column_names_its_count_field(self):
        # pc has no count of its own; the rest must carry one or the report
        # cannot show how many writes backed a hash.
        for key, cnt, _ in FD.COLUMNS:
            if key != "pc":
                self.assertIsNotNone(cnt, f"{key} has no count field")


if __name__ == "__main__":
    unittest.main()
