#!/usr/bin/env python3
"""Tests for ram_parity.py's comparison logic.

The tool exists to answer one question — did the guest compute the same numbers
on both emulators — so what is pinned here is that it answers it correctly and
reports it legibly. Chunking, run coalescing and the word view are all things
that were wrong on the first attempt and produced unreadable output.
"""

import importlib.util
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("ram_parity", ROOT / "ram_parity.py")
RP = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(RP)


def packet(x, y):
    """A vertex word the way the GPU stores it: 11-bit signed x, then y."""
    return struct.pack("<I", ((y & 0x7FF) << 16) | (x & 0x7FF))


class TestRuns(unittest.TestCase):
    def test_identical_buffers_have_no_runs(self):
        a = bytes(range(64))
        self.assertEqual(RP.diff_runs(a, a, 0x1000, 8), [])
        self.assertEqual(RP.differing_words(a, a), (0, 16))

    def test_a_changed_coordinate_is_one_run_not_three(self):
        """Only the low bytes of a coordinate change, so an uncoalesced diff
        reports one wrong vertex as a scatter of one-byte runs."""
        a = packet(100, 120) + packet(101, 121)
        b = packet(460, 120) + packet(462, 121)
        runs = RP.diff_runs(a, b, 0x0010D078, 8)
        self.assertEqual(len(runs), 1, "the two vertices merge into one run")
        self.assertEqual(runs[0]["addr"], 0x0010D078)

    def test_runs_far_apart_stay_separate(self):
        a = bytes(64)
        b = bytearray(64)
        b[0] = 1
        b[40] = 1
        runs = RP.diff_runs(a, bytes(b), 0x1000, 8)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["addr"], 0x1000)
        self.assertEqual(runs[1]["addr"], 0x1028)

    def test_run_count_is_capped(self):
        a = bytes(256)
        b = bytes(range(256))
        self.assertEqual(len(RP.diff_runs(a, b, 0, 3)), 1)   # all one long run
        alt = bytes(0 if i % 16 else 1 for i in range(256))
        self.assertLessEqual(len(RP.diff_runs(bytes(256), alt, 0, 4)), 4)

    def test_words_counts_the_unit_packets_are_built_in(self):
        a = packet(1, 1) * 4
        b = packet(1, 1) * 3 + packet(9, 9)
        self.assertEqual(RP.differing_words(a, b), (1, 4))


class TestReadRange(unittest.TestCase):
    """DuckStation caps a read at 64 KB, so a range has to be chunked — and a
    peer that returns short must not spin the loop forever."""

    class FakeConn:
        host, port = "test", 0

        def __init__(self, blob, cap):
            self.blob = blob
            self.cap = cap
            self.calls = []

        def cmd(self, name, **kw):
            addr = int(kw["addr"], 16)
            n = min(kw["len"], self.cap)
            self.calls.append((addr, kw["len"]))
            chunk = self.blob[addr:addr + n]
            return {"ok": True, "addr": kw["addr"], "len": len(chunk),
                    "hex": chunk.hex()}

    def test_chunks_a_long_range(self):
        blob = bytes((i * 7) & 0xFF for i in range(5000))
        c = self.FakeConn(blob, cap=1024)
        got = RP.read_range(c, 0, 5000, 1024)
        self.assertEqual(got, blob)
        self.assertEqual(len(c.calls), 5, "asked in 1 KB chunks")

    def test_reply_without_hex_is_rejected(self):
        class NoHex(self.FakeConn):
            def cmd(self, name, **kw):
                return {"ok": True, "len": 4}
        with self.assertRaises(RP.DebugError) as cm:
            RP.read_range(NoHex(b"", 16), 0, 4, 16)
        self.assertIn("psxrecomp or patched-DuckStation", str(cm.exception))

    def test_short_reply_terminates(self):
        class Short(self.FakeConn):
            def cmd(self, name, **kw):
                return {"ok": True, "len": 2, "hex": "0000"}
        got = RP.read_range(Short(b"", 16), 0, 64, 64)
        self.assertEqual(len(got), 64 - 62 or 2, "stops instead of spinning")


class TestWords(unittest.TestCase):
    def test_little_endian_word_view(self):
        buf = packet(0x1CC, 0x078)      # x=460, y=120
        self.assertEqual(RP.words(buf, 0, 1), [0x007801CC])

    def test_word_view_stops_at_the_end(self):
        self.assertEqual(len(RP.words(b"\x00\x01\x02", 0, 4)), 0)


if __name__ == "__main__":
    unittest.main()
