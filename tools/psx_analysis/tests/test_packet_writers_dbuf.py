"""Tests for packet_writers' double-buffer handling.

The game alternates between two packet buffers 0x8000 apart. A trace armed on
the range walked this frame sees nothing when the game writes the other copy
next frame -- reported as "no writes were recorded", with the packets plainly
present. And re-walking lands on whichever copy is current, so a normal swap
was being reported as the field map going stale.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import packet_writers as pw  # noqa: E402


class FoldTest(unittest.TestCase):
    LO, HI = 0x0010D078, 0x0010D974

    def test_address_in_walked_copy_unchanged(self):
        self.assertEqual(pw.fold_to_walked(0x0010D100, self.LO, self.HI),
                         0x0010D100)

    def test_address_in_other_copy_folds_back(self):
        other = 0x0010D100 + pw.BUFFER_STRIDE
        self.assertEqual(pw.fold_to_walked(other, self.LO, self.HI),
                         0x0010D100)

    def test_folds_from_the_lower_copy_too(self):
        """Which copy the walk landed on is not known in advance."""
        lower = 0x0010D100 - pw.BUFFER_STRIDE
        self.assertEqual(pw.fold_to_walked(lower, self.LO, self.HI),
                         0x0010D100)

    def test_xor_would_fold_to_a_wrong_address(self):
        """Guard the actual bug: XOR does not carry out of bit 15."""
        self.assertNotEqual(0x0010D078 ^ pw.BUFFER_STRIDE,
                            0x0010D078 + pw.BUFFER_STRIDE)

    def test_unrelated_address_is_rejected(self):
        self.assertIsNone(pw.fold_to_walked(0x00080000, self.LO, self.HI))

    def test_fold_is_symmetric(self):
        a = 0x0010D200
        b = a + pw.BUFFER_STRIDE
        self.assertEqual(pw.fold_to_walked(a, self.LO, self.HI),
                         pw.fold_to_walked(b, self.LO, self.HI))

    def test_stride_matches_observed_buffers(self):
        """Roots 0x0010Dxxx and 0x00115xxx, measured from the capture."""
        self.assertEqual(0x0010D078 + pw.BUFFER_STRIDE, 0x00115078)


class StepCountTest(unittest.TestCase):
    def test_default_frames_is_even(self):
        """An odd step can rebuild only the copy that was not walked."""
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "packet_writers.py")).read()
        decl = src.split('ap.add_argument("--frames"', 1)[1].split(")", 1)[0]
        self.assertIn("default=2", decl)

    def test_odd_request_is_rounded_up(self):
        for req, want in ((1, 2), (2, 2), (3, 4), (4, 4)):
            self.assertEqual(max(2, req + (req & 1)), want)
