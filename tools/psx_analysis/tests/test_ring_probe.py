"""Tests for the ring starvation probe's verdict.

One number decides whether the ring hypothesis survives, so the mapping from
counters to verdict must be unambiguous -- including the case where the
running binary lacks the counters entirely, which must not read as "no
starvation".
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ring_probe as rp  # noqa: E402


class VerdictTest(unittest.TestCase):
    def test_starvation_confirms_and_names_the_fix(self):
        v, why = rp.verdict_of({"ring_starved": 42, "ring_norq_refill": 17})
        self.assertEqual(v, "starvation-confirmed")
        self.assertIn("advance the read pointer", why)

    def test_refills_without_starvation_kills_it(self):
        v, why = rp.verdict_of({"ring_starved": 0, "ring_norq_refill": 17})
        self.assertEqual(v, "refills-off-slot-but-no-starvation")
        self.assertIn("dead", why)

    def test_nothing_at_all_kills_it(self):
        v, why = rp.verdict_of({"ring_starved": 0, "ring_norq_refill": 0})
        self.assertEqual(v, "hypothesis-dead")

    def test_missing_counters_is_not_a_zero(self):
        """A non-diagnostic build must not read as 'no starvation'."""
        v, why = rp.verdict_of({"int1_lost": 0})
        self.assertEqual(v, "no-instrumentation")
        self.assertIn("Rebuild", why)

    def test_string_counters_accepted(self):
        v, _ = rp.verdict_of({"ring_starved": "5", "ring_norq_refill": "2"})
        self.assertEqual(v, "starvation-confirmed")
