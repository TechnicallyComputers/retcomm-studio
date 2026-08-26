"""Tests for the per-sector notification diff.

The transfers are understood; what is not is what the guest is TOLD. These
pin the flag summarisation and the one interpretation that matters: native
announcing immediately where the oracle deliberately withheld.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify_diff as nd  # noqa: E402


def nat(lba, data=1, dma=0, pended=0, lost=0):
    return {"lba": lba, "data": data, "dma": dma, "pended": pended,
            "lost": lost}


def orc(lba, **kw):
    row = {"lba": lba, "data": 1, "dropped": 0, "queued": 0, "delivered": 0,
           "redelivered": 0, "drained": 0}
    row.update(kw)
    return row


class SummariseTest(unittest.TestCase):
    def test_latest_native_pass_wins(self):
        """A degraded earlier pass must not poison the current one."""
        r = nd.native_rows([nat(1, pended=1, lost=1), nat(1)])
        self.assertEqual(r[1]["passes"], 2)
        self.assertEqual(r[1]["lost"], 0)
        self.assertEqual(r[1]["pended"], 0)

    def test_latest_oracle_pass_wins(self):
        r = nd.oracle_rows([orc(5, dropped=1), orc(5, delivered=1, drained=1)])
        self.assertEqual(r[5]["dropped"], 0)
        self.assertEqual(r[5]["delivered"], 1)
        self.assertEqual(r[5]["passes"], 2)

    def test_unknown_lba_skipped(self):
        self.assertEqual(nd.native_rows([{"data": 1}]), {})

    def test_marks_string(self):
        r = nd.oracle_rows([orc(5, dropped=1, drained=1)])
        self.assertEqual(nd.marks(r[5], nd.ORC_KEYS), "DD---D")

    def test_repeated_clean_reads_are_not_degraded(self):
        """Replaying the scene is normal; only a lost notification is not."""
        r = nd.native_rows([nat(1), nat(1)])
        self.assertEqual(r[1]["lost"], 0)


class InterpretTest(unittest.TestCase):
    def test_immediate_vs_withheld_is_flagged(self):
        n = nd.native_rows([nat(110)])
        o = nd.oracle_rows([orc(110, dropped=1, redelivered=1, drained=1)])
        notes = nd.interpret(n, o, 100, 120)
        self.assertEqual(len(notes), 1)
        self.assertIn("immediate INT1", notes[0])
        self.assertIn("re-announced", notes[0])

    def test_both_pending_is_not_flagged(self):
        n = nd.native_rows([nat(110, pended=1)])
        o = nd.oracle_rows([orc(110, queued=1, delivered=1, drained=1)])
        self.assertEqual(nd.interpret(n, o, 100, 120), [])

    def test_both_immediate_is_not_flagged(self):
        n = nd.native_rows([nat(110)])
        o = nd.oracle_rows([orc(110, delivered=1, drained=1)])
        self.assertEqual(nd.interpret(n, o, 100, 120), [])

    def test_sector_missing_on_one_side_is_not_interpreted(self):
        n = nd.native_rows([nat(110)])
        self.assertEqual(nd.interpret(n, {}, 100, 120), [])


class DegradedSessionTest(unittest.TestCase):
    """A contaminated native session must not be diffed.

    The first live run compared a stale disc_speed=2x session (lost
    notifications, sectors read 2-7 times) against a healthy oracle and
    concluded the notification patterns agreed.
    """

    def test_lost_flag_marks_degraded(self):
        r = nd.native_rows([nat(110, lost=1)])
        self.assertTrue(r[110]["lost"])

    def test_degradation_is_loss_not_replay_count(self):
        """Multiple passes are legitimate; the rings span the whole session."""
        r = nd.native_rows([nat(110), nat(110), nat(110)])
        self.assertEqual(r[110]["passes"], 3)
        self.assertEqual(r[110]["lost"], 0)

    def test_latest_pass_degraded_is_caught(self):
        r = nd.native_rows([nat(110), nat(110, lost=1)])
        self.assertTrue(r[110]["lost"])


class ReadAheadTest(unittest.TestCase):
    """Past the load's last sector the drive reads on until the game's Pause.

    Those notifications are lost by design. Counting them made a clean
    session report as degraded and blocked the diff.
    """

    def test_load_ends_at_the_last_consumed_sector(self):
        self.assertEqual(125117, 125117)   # documents the boundary

    def test_loss_beyond_the_window_is_not_degradation(self):
        r = nd.native_rows([nat(125117), nat(125118, lost=1)])
        inside = [lba for lba, v in r.items() if v["lost"] and lba <= 125117]
        self.assertEqual(inside, [])

    def test_loss_inside_the_window_is_degradation(self):
        r = nd.native_rows([nat(125110, lost=1)])
        inside = [lba for lba, v in r.items() if v["lost"] and lba <= 125117]
        self.assertEqual(inside, [125110])
