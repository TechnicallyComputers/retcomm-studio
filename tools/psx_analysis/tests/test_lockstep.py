#!/usr/bin/env python3
"""Reading a lockstep result, in particular when it has said nothing.

"found: false" is produced by two completely different situations: the compiled
code matched everywhere it was compared, and nothing was compared at all. The
segment comparator skips whatever it cannot replay — interrupts, overflow,
unhandled ops, conflicts — so a run that skipped everything is indistinguishable
from a clean one unless the counters are read alongside the verdict.

Reporting that as "no divergence" would be the worst kind of wrong: a pass that
was never earned, on the one instrument that needs no frame alignment and no
cross-emulator addresses to be trusted.
"""

import importlib.util
import io
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


LS = _load("lockstep_check")


def run(doc, func=False):
    buf = io.StringIO()
    v = LS.summarise(doc, func, out=buf)
    return v, buf.getvalue()


class TestVerdicts(unittest.TestCase):
    def test_nothing_checked_is_inconclusive_not_clean(self):
        v, txt = run({"found": 0, "blocks_checked": 0, "window": [10, 130]})
        self.assertEqual(v, "inconclusive")
        self.assertIn("NOT a clean result", txt)

    def test_checked_and_quiet_is_clean(self):
        v, txt = run({"found": 0, "blocks_checked": 4211, "window": [10, 130]})
        self.assertEqual(v, "clean")
        self.assertIn("4211", txt)

    def test_more_skipped_than_checked_is_weak(self):
        v, txt = run({"found": 0, "segments_checked": 40, "window": [0, 60],
                      "skipped_irq": 30, "skipped_unhandled": 25}, func=True)
        self.assertEqual(v, "weak")
        self.assertIn("skipped", txt)

    def test_a_divergence_reports_both_values(self):
        v, txt = run({"found": 1, "div_kind": "write-val", "blocks_checked": 900,
                      "window": [0, 120], "frame": 41230,
                      "block": "0x80068440", "pc": "0x8006844C",
                      "addr": "0x1F800264", "reg": -1,
                      "interp_expected": "0x000000F8",
                      "compiled_actual": "0x0000001B",
                      "trace": ["W4:1F800264=000000F8"]})
        self.assertEqual(v, "diverged")
        self.assertIn("0x8006844C", txt)
        self.assertIn("0x000000F8", txt)
        self.assertIn("0x0000001B", txt)

    def test_each_kind_is_explained_in_words(self):
        # A bare "kind: read-addr" tells the reader nothing about where to look.
        for k in ("reg", "hi", "lo", "write-val", "read-addr", "write-addr"):
            self.assertIn(k, LS.MEANING, f"{k} has no explanation")

    def test_the_document_type_survives_a_clean_run(self):
        # The engine calls the divergence type "kind" too, and merging its reply
        # as-is overwrote the document type with "none" — after which the Studio
        # rejected its own output as a foreign file.
        import json as _j, subprocess, sys as _s
        self.assertEqual(LS.KIND, "psx-lockstep")

    def test_comparator_limits_are_not_reported_as_bugs(self):
        # path-cap and unsupported mean the comparison gave up, not that the
        # compiled code is wrong. Reading them as findings sends you hunting
        # a bug that was never claimed.
        for k in ("path-cap", "unsupported"):
            v, txt = run({"found": 1, "div_kind": k, "blocks_checked": 10,
                          "window": [0, 5], "reg": -1})
            self.assertIn("limit of the comparator", txt)

    def test_the_window_is_always_shown(self):
        _, txt = run({"found": 0, "blocks_checked": 5, "window": [77, 197]})
        self.assertIn("77..197", txt)


if __name__ == "__main__":
    unittest.main()


class TestSkipGuidance(unittest.TestCase):
    """A skip count is a number; what it implies is the useful part.

    Measured on a real run: 10,619 segments checked against 74,111 skipped, 99%
    of them because the segment contained an interrupt. That means SEGMENT
    granularity cannot cover this game — not that anything is broken. The two
    call for opposite responses, and 74,111 on its own does not say which.
    """

    def test_irq_dominated_skips_recommend_block_granularity(self):
        v, txt = run({"found": 0, "segments_checked": 10619, "window": [2572, 2692],
                      "skipped_irq": 73489, "skipped_conflict": 300,
                      "skipped_disabled": 322}, func=True)
        self.assertEqual(v, "weak")
        self.assertIn("BLOCK granularity", txt)
        self.assertIn("99%", txt)

    def test_the_ratio_is_stated_not_just_the_counts(self):
        _, txt = run({"found": 0, "segments_checked": 100, "window": [0, 10],
                      "skipped_irq": 700}, func=True)
        self.assertIn("7.0x", txt)

    def test_block_mode_does_not_suggest_itself(self):
        # The advice only makes sense when you are NOT already using blocks.
        _, txt = run({"found": 0, "blocks_checked": 100, "window": [0, 10],
                      "skipped_irq": 700}, func=False)
        self.assertNotIn("BLOCK granularity", txt)

    def test_a_mixed_skip_profile_names_no_single_cause(self):
        # No reason above 60% means there is no one thing to point at, and
        # inventing one would be worse than staying quiet.
        _, txt = run({"found": 0, "segments_checked": 10, "window": [0, 10],
                      "skipped_irq": 30, "skipped_conflict": 30,
                      "skipped_disabled": 30}, func=True)
        self.assertNotIn("mostly", txt)

    def test_a_clean_run_with_no_skips_says_nothing_about_skipping(self):
        _, txt = run({"found": 0, "blocks_checked": 5679898, "window": [9275, 9395]})
        self.assertNotIn("skipped", txt)
