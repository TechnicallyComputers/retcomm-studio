"""Tests for splitting the inter-sector gap into guest vs emulator time.

The gap before each individually-requested sector is ~2.5M cycles where the
2x cadence is 225,792. Frame numbers cannot separate "the guest had not asked
yet" from "the command was queued behind an unacked INT"; guest cycle stamps
can, and the two need different fixes.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gap_anatomy as g  # noqa: E402


def b2(v):
    return ((v // 10) << 4) | (v % 10)


def setloc_entry(lba, cycle):
    t = lba + 150
    m, s, f = t // 4500, (t // 75) % 60, t % 75
    return {"cmd": "0x02", "cycle": cycle,
            "params": [f"0x{b2(m):02X}", f"0x{b2(s):02X}", f"0x{b2(f):02X}"]}


class DecodeTest(unittest.TestCase):
    def test_setloc_lba_and_cycle(self):
        out = g.setloc_cycles([setloc_entry(125111, 4242)])
        self.assertEqual(out[125111], 4242)

    def test_non_setloc_ignored(self):
        self.assertEqual(g.setloc_cycles([{"cmd": "0x06", "cycle": 1}]), {})

    def test_latest_setloc_wins(self):
        out = g.setloc_cycles([setloc_entry(1, 10), setloc_entry(1, 99)])
        self.assertEqual(out[1], 99)


class AnatomyTest(unittest.TestCase):
    SEC = {109: 1_000_000, 110: 1_225_792, 111: 3_726_897}

    def test_splits_silence_from_after_ask(self):
        rows = g.anatomy(self.SEC, {111: 1_300_000}, 104, 117)
        self.assertEqual(rows[0]["gap"], 2_501_105)
        self.assertEqual(rows[0]["silence"], 74_208)
        self.assertEqual(rows[0]["after_issue"], 2_426_897)

    def test_sector_without_setloc_skipped(self):
        """Continuations are not individually requested."""
        self.assertEqual(g.anatomy(self.SEC, {}, 104, 117), [])

    def test_out_of_window_skipped(self):
        self.assertEqual(g.anatomy(self.SEC, {111: 1}, 200, 300), [])


class VerdictTest(unittest.TestCase):
    def test_late_ask_is_guest_silence(self):
        rows = [{"lba": 1, "gap": 1000, "silence": 900, "after_issue": 100}]
        v, why = g.verdict_of(rows)
        self.assertEqual(v, "guest-silence")
        self.assertIn("waiting on the game", why)

    def test_late_service_is_emulator_latency(self):
        rows = [{"lba": 1, "gap": 2_500_000, "silence": 70_000,
                 "after_issue": 2_430_000}]
        v, why = g.verdict_of(rows)
        self.assertEqual(v, "emulator-latency")
        self.assertIn("queueing", why)

    def test_no_rows_refuses(self):
        self.assertEqual(g.verdict_of([])[0], "no-data")


class SteadyStateTest(unittest.TestCase):
    """After the fix, requests that continue a read cost no restart.

    Gaps collapse to one 2x sector period (225,792). The old verdict still
    subtracted a fixed read-start constant and reported "451,584 of read-start
    delay" for a 215,394-cycle interval -- arithmetic that cannot be true.
    """

    def test_steady_cadence_is_healthy(self):
        rows = [{"lba": 1, "gap": 225_792, "silence": 10_475,
                 "after_issue": 215_317}]
        v, why = g.verdict_of(rows)
        self.assertEqual(v, "streaming")
        self.assertIn("2x cadence", why)

    def test_read_start_only_claimed_when_it_fits(self):
        rows = [{"lba": 1, "gap": 600_000, "silence": 10_000,
                 "after_issue": 590_000}]
        v, why = g.verdict_of(rows)
        self.assertEqual(v, "emulator-latency")
        self.assertIn("read-start", why)

    def test_small_gap_is_not_the_cadence(self):
        """An arbitrarily small gap is a different situation, not streaming."""
        rows = [{"lba": 1, "gap": 1000, "silence": 900, "after_issue": 100}]
        self.assertEqual(g.verdict_of(rows)[0], "guest-silence")

    def test_no_impossible_read_start_claim(self):
        rows = [{"lba": 1, "gap": 300_000, "silence": 10_000,
                 "after_issue": 290_000}]
        _, why = g.verdict_of(rows)
        self.assertNotIn("451,584", why)
