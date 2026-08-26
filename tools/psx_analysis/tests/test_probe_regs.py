#!/usr/bin/env python3
"""Choosing the enclosing block, and being honest about what was captured.

psx-runtime's probe fires at basic-block LEADERS, so an arbitrary mid-block
address never matches one and the block containing it has to be found by
arming a spread and seeing what fires. Two things follow, and both are easy to
get quietly wrong:

The leader must be at or BELOW the target. A leader after it belongs to the
next block, whose registers describe what happens afterwards — real values,
answering a different question.

And the values are read at block ENTRY. For callee-saved registers set outside
the loop that is the same value as at the target; for one the block computes it
is not. Reporting both identically would hand back a number that is real,
plausible, and taken from the wrong instant.
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


PR = _load("probe_regs")


class TestCandidates(unittest.TestCase):
    def test_the_target_itself_is_tried_first(self):
        c = PR.candidates(0x8006844C, 0x100, 0x10)
        self.assertEqual(c[0], 0x8006844C)

    def test_candidates_go_BACKWARDS_from_the_target(self):
        # A block leader is at or before the instruction it contains. Searching
        # forward would find the next block entirely.
        c = PR.candidates(0x8006844C, 0x100, 0x10)
        self.assertTrue(all(a <= 0x8006844C for a in c))
        self.assertEqual(c[1], 0x8006843C)

    def test_the_probe_slot_limit_is_respected(self):
        # PC_PROBE_MAX_PCS is 16 in the engine; asking for more silently drops
        # the excess there, so it is capped here where it is visible.
        c = PR.candidates(0x8006844C, 0x10000, 0x4)
        self.assertLessEqual(len(c), PR.MAX_PCS)

    def test_the_range_is_not_exceeded(self):
        c = PR.candidates(0x80068400, 0x40, 0x10)
        self.assertTrue(all(a > 0x80068400 - 0x40 for a in c))


class TestSavedRegisterClassification(unittest.TestCase):
    def test_callee_saved_registers_are_known(self):
        # $s4 and $s6 carry the colour routine's source pointer and scale, are
        # set outside the loop, and are therefore trustworthy at block entry.
        for r in ("s4", "s6", "sp", "ra"):
            self.assertIn(r, PR.SAVED)

    def test_temporaries_are_not_treated_as_stable(self):
        # $v0/$t6/$a0 are recomputed inside the colour block; their entry value
        # is not their value at the store.
        for r in ("v0", "t6", "a0", "at"):
            self.assertNotIn(r, PR.SAVED)


if __name__ == "__main__":
    unittest.main()


class TestPointerPlausibility(unittest.TestCase):
    """A register that was never captured must not become an address.

    Seen live: every register came back 0x00000000 because the engine emitted
    the GPR array without ever filling it. Zero then flowed downstream — 0 minus
    the load offset masks to 0x1FFFFFF4, a real-looking address — and the run
    reported a difference between the oracle's table and unmapped memory, with
    "0 differing bytes" and "not identical" in the same document.
    """

    def test_zero_is_not_a_pointer(self):
        self.assertFalse(PR.plausible_pointer(0))

    def test_the_masked_underflow_is_not_a_pointer(self):
        # (0 - 12) & 0x1FFFFFFF, the exact value that got through.
        self.assertFalse(PR.plausible_pointer(0x1FFFFFF4))

    def test_a_real_ram_pointer_passes(self):
        self.assertTrue(PR.plausible_pointer(0x800E4C04))
        self.assertTrue(PR.plausible_pointer(0x000E4C04))

    def test_an_address_past_ram_is_rejected(self):
        self.assertFalse(PR.plausible_pointer(0x00200000))

    def test_none_is_rejected(self):
        self.assertFalse(PR.plausible_pointer(None))

    def test_scratchpad_is_not_treated_as_main_ram(self):
        # $sp legitimately points into scratchpad, but a colour TABLE does not
        # live there; accepting it would compare against the wrong region.
        self.assertFalse(PR.plausible_pointer(0x1F800234))


class TestAdaptiveSearch(unittest.TestCase):
    """One fixed window is a guess about where the enclosing block starts.

    Measured: step 0x10 over 0x100 found a leader for 0x8006844C and none at all
    for 0x800684C0 — two colour writers sixteen bytes of code apart. The failure
    message was accurate and useless: "none of these is a basic-block leader"
    tells the operator nothing they can act on.
    """

    def test_windows_start_at_the_target_and_walk_backwards(self):
        w = PR.search_windows(0x800684C0, max_back=0x100)
        self.assertEqual(w[0][0], 0x800684C0)
        self.assertLess(w[1][0], w[0][0])

    def test_candidates_are_every_instruction_not_every_fourth(self):
        # Any instruction can be a block leader, so a coarse step can step over
        # the only address that would have fired.
        w = PR.search_windows(0x800684C0, max_back=0x100)
        self.assertEqual(w[0][0] - w[0][1], 4)

    def test_windows_do_not_overlap_or_leave_gaps(self):
        w = PR.search_windows(0x80068000, max_back=0x200)
        for a, b in zip(w, w[1:]):
            self.assertEqual(a[-1] - b[0], 4,
                             "consecutive windows must be contiguous — a gap "
                             "can hide the one leader that would have fired")

    def test_the_slot_limit_is_respected_per_window(self):
        for win in PR.search_windows(0x80068000, max_back=0x400):
            self.assertLessEqual(len(win), PR.MAX_PCS)

    def test_the_search_is_bounded(self):
        w = PR.search_windows(0x80068000, max_back=0x100)
        self.assertLessEqual(len(w), 0x100 // (PR.MAX_PCS * 4) + 1)
