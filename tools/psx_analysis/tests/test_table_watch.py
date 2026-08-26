"""Tests for table_watch's change detection and attribution split."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import table_watch as tw  # noqa: E402


class DistinctTest(unittest.TestCase):
    def test_counts_colour_words(self):
        blob = (b"\x01\x02\x03\x00" * 3) + b"\x04\x05\x06\x00"
        self.assertEqual(tw.distinct_colours(blob), 2)

    def test_command_byte_ignored(self):
        blob = b"\x01\x02\x03\x38" + b"\x01\x02\x03\x00"
        self.assertEqual(tw.distinct_colours(blob), 1)


class FingerprintTest(unittest.TestCase):
    def test_stable_and_sensitive(self):
        a = tw.fingerprint(b"\x00" * 64)
        self.assertEqual(a, tw.fingerprint(b"\x00" * 64))
        self.assertNotEqual(a, tw.fingerprint(b"\x00" * 63 + b"\x01"))


class ClassifyTest(unittest.TestCase):
    """The whole point: a change WITHOUT traced writes is a restore.

    CPU stores and every DMA channel funnel through psx_write_word and are
    traced; savestate and rewind restores memcpy RAM wholesale and are not.
    """

    def test_traced_writes_name_a_writer(self):
        kind, why = tw.classify(12)
        self.assertEqual(kind, "written")
        self.assertIn("dump", why)

    def test_no_writes_means_restore(self):
        kind, why = tw.classify(0)
        self.assertEqual(kind, "restored")
        self.assertIn("savestate", why)
        self.assertIn("CREATED", why)
