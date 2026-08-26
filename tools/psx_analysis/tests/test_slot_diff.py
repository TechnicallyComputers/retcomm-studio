"""Tests for the cross-emulator DMA slot map diff."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slot_diff as sd  # noqa: E402


def dma(dest, req, got, size=2048, data=None):
    return {"lba": req, "delivered_lba": got, "dest": f"0x{dest:08X}",
            "size": size, "frame": 1,
            "first_words": data or ["0x00000000", "0x00000000"]}


class SlotMapTest(unittest.TestCase):
    def test_keyed_by_destination(self):
        m = sd.slot_map([dma(0x000E2718, 125113, 125113)], 125080, 125130)
        self.assertIn(0x0E2718, m)
        self.assertEqual(m[0x0E2718]["delivered"], 125113)

    def test_out_of_window_dropped(self):
        m = sd.slot_map([dma(0x000E2718, 126793, 126803)], 125080, 125130)
        self.assertEqual(m, {})

    def test_kseg_bits_masked(self):
        m = sd.slot_map([dma(0x800E2718, 125113, 125113)], 125080, 125130)
        self.assertIn(0x0E2718, m)

    def test_later_transfer_to_same_slot_wins(self):
        """The last write to a buffer is what the game reads."""
        m = sd.slot_map([dma(0x000E2718, 125111, 125111),
                         dma(0x000E2718, 125113, 125113)], 125080, 125130)
        self.assertEqual(m[0x0E2718]["delivered"], 125113)


class CompareTest(unittest.TestCase):
    def test_different_sector_flagged(self):
        nat = sd.slot_map([dma(0x000E2718, 125113, 125113)], 125080, 125130)
        orc = sd.slot_map([dma(0x000E2718, 125113, 125112)], 125080, 125130)
        rows = sd.compare(nat, orc)
        self.assertEqual(rows[0]["state"], "DIFFERENT SECTOR")

    def test_agreement_reported(self):
        m = sd.slot_map([dma(0x000E2718, 125113, 125113)], 125080, 125130)
        rows = sd.compare(m, dict(m))
        self.assertEqual(rows[0]["state"], "same")

    def test_one_sided_slots_named(self):
        nat = sd.slot_map([dma(0x000E2718, 125113, 125113)], 125080, 125130)
        rows = sd.compare(nat, {})
        self.assertEqual(rows[0]["state"], "only-native")

    def test_differences_sort_first(self):
        nat = sd.slot_map([dma(0x000E1F18, 125111, 125111),
                           dma(0x000E2718, 125113, 125113)], 125080, 125130)
        orc = sd.slot_map([dma(0x000E1F18, 125111, 125111),
                           dma(0x000E2718, 125113, 125112)], 125080, 125130)
        rows = sd.compare(nat, orc)
        self.assertNotEqual(rows[0]["state"], "same")
