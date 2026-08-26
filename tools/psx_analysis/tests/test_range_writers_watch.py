"""Tests for range_writers' free-running watch and its empty-result message.

A table that is static while an effect plays is not written during it.
Stepping two frames there proves only that -- and the old message read as
"nothing ever writes this", which sent the investigation looking for a
missing writer instead of tracing the scene load where the fill happens.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import range_writers  # noqa: E402

SRC = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "range_writers.py")).read()


class WatchOptionTest(unittest.TestCase):
    def test_watch_flag_exists_and_defaults_off(self):
        decl = SRC.split('ap.add_argument("--watch"', 1)[1].split(")", 1)[0]
        self.assertIn("default=0.0", decl)

    def test_watch_path_runs_free_not_stepped(self):
        branch = SRC.split("if args.watch > 0:", 1)[1].split("else:", 1)[0]
        self.assertIn('conn.cmd("continue")', branch)
        self.assertNotIn('conn.cmd("step"', branch)

    def test_stepped_path_still_steps(self):
        branch = SRC.split("if args.watch > 0:", 1)[1].split("else:", 1)[1]
        self.assertIn('conn.cmd("step"', branch)


class EmptyMessageTest(unittest.TestCase):
    def test_stepped_empty_says_static_not_never_written(self):
        msg = SRC.split("That means it is STATIC", 1)
        self.assertEqual(len(msg), 2, "static wording missing")
        self.assertIn("--watch N", SRC)

    def test_watched_empty_names_dma_as_the_reason(self):
        self.assertIn("not visible to this trace", SRC)
        self.assertIn("DMA fill", SRC)
