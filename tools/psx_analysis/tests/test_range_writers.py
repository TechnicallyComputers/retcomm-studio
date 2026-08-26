#!/usr/bin/env python3
"""Attributing writes over an arbitrary address range.

This is packet_writers without the packet-layout assumption, built because the
colour investigation reached a REGION rather than a structure: with the effect
off, 0x000E0BF8..0x000E6628 is byte-identical between the two emulators, and
with it running they differ. So the question became "what writes this", and the
answer has to come from an address range rather than from known field offsets.

The summary is the product here, so what it groups and what it reports are what
these pin.
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


RW = _load("range_writers")


def w(pc, addr, new=None, width=4):
    e = {"pc": pc, "addr": addr, "w": width}
    if new:
        e["new"] = new
    return e


class TestSummary(unittest.TestCase):
    def run_it(self, rows, lo=0xE0000, hi=0xE6000):
        buf = io.StringIO()
        ranked = RW.summarise(rows, lo, hi, out=buf)
        return ranked, buf.getvalue()

    def test_writers_are_ranked_by_volume(self):
        rows = ([w("0xAAA", "0x000E1000")] * 5 +
                [w("0xBBB", "0x000E2000")] * 20)
        ranked, _ = self.run_it(rows)
        self.assertEqual(ranked[0][0], "0xBBB")
        self.assertEqual(ranked[0][1]["n"], 20)

    def test_each_writer_reports_the_span_it_touched(self):
        # A store that walks a whole array is a different thing from one that
        # hits a single field, and the span is what tells them apart.
        rows = [w("0xAAA", "0x000E1000"), w("0xAAA", "0x000E5FF0")]
        ranked, txt = self.run_it(rows)
        d = ranked[0][1]
        self.assertEqual(d["lo"], 0x000E1000)
        self.assertEqual(d["hi"], 0x000E5FF0)
        self.assertIn("0E1000", txt)

    def test_common_values_are_surfaced(self):
        # A writer that stores the same value everywhere is clearing; one
        # storing varied values is computing. The report should show which.
        rows = [w("0xAAA", "0x000E1000", new="0x00000000") for _ in range(9)]
        ranked, txt = self.run_it(rows)
        self.assertEqual(ranked[0][1]["vals"].most_common(1)[0],
                         ("0x00000000", 9))
        self.assertIn("0x00000000", txt)

    def test_the_range_is_echoed_so_the_report_stands_alone(self):
        _, txt = self.run_it([w("0xAAA", "0x000E1000")], 0xE0BF8, 0xE6628)
        self.assertIn("0x000E0BF8", txt)
        self.assertIn("0x000E6628", txt)

    def test_an_address_outside_the_range_is_still_attributed(self):
        # wtrace filters server-side; if something slips through it should be
        # counted rather than silently dropped, since a write just outside the
        # watched span is itself worth seeing.
        ranked, _ = self.run_it([w("0xAAA", "0x000F0000")])
        self.assertEqual(ranked[0][1]["n"], 1)

    def test_no_rows_produces_no_writers(self):
        ranked, _ = self.run_it([])
        self.assertEqual(ranked, [])
