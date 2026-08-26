"""Tests for cd_fork's load filtering and sector slicing."""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cd_fork as cf  # noqa: E402


def load(lba, dest, size):
    return {"lba": str(lba), "dest": f"0x{dest:08X}", "size": str(size)}


class OverlapTest(unittest.TestCase):
    LO, HI = 0x0E25F0, 0x0E2C60

    def test_load_inside_region_matches(self):
        hits = cf.overlapping([load(1000, 0x800E2600, 296)], self.LO, self.HI)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["lba"], 1000)

    def test_load_straddling_start_matches(self):
        hits = cf.overlapping([load(1, 0x800E2000, 0x700)], self.LO, self.HI)
        self.assertEqual(len(hits), 1)

    def test_load_elsewhere_ignored(self):
        hits = cf.overlapping([load(1, 0x80100000, 4096)], self.LO, self.HI)
        self.assertEqual(hits, [])

    def test_kseg_bits_stripped(self):
        hits = cf.overlapping([load(1, 0x000E2600, 8)], self.LO, self.HI)
        self.assertEqual(len(hits), 1)


class SectorTest(unittest.TestCase):
    def test_mode2_user_data_extracted(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            # two raw sectors: header junk + recognisable user data
            for k in range(2):
                f.write(bytes([0xEE] * cf.MODE2_HEADER))
                f.write(bytes([k] * cf.DATA_BYTES))
                f.write(bytes([0xDD] * (cf.RAW_SECTOR - cf.MODE2_HEADER
                                        - cf.DATA_BYTES)))
            path = f.name
        try:
            blob = cf.sector_bytes(path, 0, 2)
            self.assertEqual(blob[:4], b"\x00\x00\x00\x00")
            self.assertEqual(blob[cf.DATA_BYTES:cf.DATA_BYTES + 4],
                             b"\x01\x01\x01\x01")
            self.assertEqual(len(blob), 2 * cf.DATA_BYTES)
        finally:
            os.unlink(path)


class DistinctTest(unittest.TestCase):
    def test_masks_command_byte(self):
        blob = b"\x01\x02\x03\x38" + b"\x01\x02\x03\x00"
        self.assertEqual(cf.distinct_colours(blob), 1)
