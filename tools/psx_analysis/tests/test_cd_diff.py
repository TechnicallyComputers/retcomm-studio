"""Tests for the cross-emulator CD command stream diff."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cd_diff as cd  # noqa: E402


def setloc(lba, frame=1):
    t = lba + 150
    m, s, f = t // 4500, (t // 75) % 60, t % 75

    def b2(v):
        return ((v // 10) << 4) | (v % 10)
    return {"cmd": "0x02", "frame": frame,
            "params": [f"0x{b2(m):02X}", f"0x{b2(s):02X}", f"0x{b2(f):02X}"]}


def readn(frame=1):
    return {"cmd": "0x06", "params": [], "frame": frame}


class NormaliseTest(unittest.TestCase):
    def test_setloc_decoded(self):
        r = cd.normalise([setloc(125112)])
        self.assertEqual(r[0]["lba"], 125112)

    def test_hex_and_int_params_both_accepted(self):
        r = cd.normalise([{"cmd": 0x06, "params": [1, 2], "frame": 3}])
        self.assertEqual(r[0]["cmd"], "ReadN")
        self.assertEqual(r[0]["params"], [1, 2])


class ForkTest(unittest.TestCase):
    def test_finds_the_lom_fork(self):
        """Native requests 111 where the oracle requests a different tail."""
        common = [setloc(125105), readn(), setloc(125106), readn(),
                  setloc(125107), readn()]
        nat = cd.normalise(common + [setloc(125111), readn(),
                                     setloc(125113), readn()])
        orc = cd.normalise(common + [setloc(125110), readn(),
                                     setloc(125111), readn()])
        f = cd.find_fork(nat, orc, 4)
        self.assertIsNotNone(f)
        self.assertEqual(nat[f["a_fork"]]["lba"], 125111)
        self.assertEqual(orc[f["b_fork"]]["lba"], 125110)

    def test_identical_streams_fork_at_the_end(self):
        rows = cd.normalise([setloc(1), readn(), setloc(2), readn()])
        f = cd.find_fork(rows, rows, 2)
        self.assertEqual(f["a_fork"], len(rows))

    def test_offset_alignment(self):
        """The oracle's capture may include extra leading traffic."""
        common = [setloc(50), readn(), setloc(51), readn(), setloc(52),
                  readn()]
        nat = cd.normalise(common)
        orc = cd.normalise([setloc(9), readn()] + common)
        f = cd.find_fork(nat, orc, 3)
        self.assertEqual(f["a_start"], 0)
        self.assertEqual(f["b_start"], 2)

    def test_no_overlap_returns_none(self):
        a = cd.normalise([setloc(1), readn()])
        b = cd.normalise([setloc(9), readn()])
        self.assertIsNone(cd.find_fork(a, b, 2))


class LbaAnchorTest(unittest.TestCase):
    """Alignment must anchor on the palette file's own Setlocs.

    Generic longest-common-run alignment latched onto the periodic Getstat
    heartbeat -- identical everywhere -- and reported a fork between two
    unrelated journey points (native mid-scene vs oracle at boot)."""

    def test_sequence_extraction(self):
        rows = cd.normalise([setloc(304), readn(), setloc(125105), readn(),
                             setloc(125111), readn()])
        self.assertEqual(cd.lba_sequence(rows, 125000, 125200),
                         [125105, 125111])

    def test_boot_loads_excluded(self):
        rows = cd.normalise([setloc(304), setloc(2864), setloc(12422)])
        self.assertEqual(cd.lba_sequence(rows, 125000, 125200), [])

    def test_fork_index_by_position(self):
        nat = [125105, 125106, 125111, 125113]
        orc = [125105, 125106, 125110, 125111]
        fork = next((k for k in range(min(len(nat), len(orc)))
                     if nat[k] != orc[k]), None)
        self.assertEqual(fork, 2)
