"""Tests for phase_parity's phase selection.

The fade register rests at its neutral value for most of the scene, so
locking both emulators to it does not establish that they are at the same
moment. oracle_phase must hold out for a distinctive value.
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import phase_parity  # noqa: E402


class FakeConn:
    """Replays a scripted sequence of pc_hit_last values.

    Also models the oracle's breakpoint table, because a leaked breakpoint is
    the failure this code exists to prevent: DuckStation refuses a duplicate
    address, so one left armed makes the next run's pc_break fail while the
    old one keeps pausing the emulator.
    """

    def __init__(self, phases):
        self.phases = list(phases)
        self.resumes = 0
        self.breaks = 0
        self.armed = set()          # addresses currently breakpointed
        self.sent = []              # every command name, in order

    def _dispatch(self, name, **kw):
        self.sent.append(name)
        if name == "pc_break":
            addr = kw.get("addr")
            self.breaks += 1
            if addr in self.armed:          # duplicate refused, as DuckStation does
                return {"ok": False, "addr": addr, "slot": -1}
            self.armed.add(addr)
            return {"ok": True, "addr": addr, "slot": 0}
        if name == "pc_unbreak":
            self.armed.discard(kw.get("addr"))
            return {"ok": True}
        if name == "pc_break_list":
            return {"breaks": sorted(self.armed)}
        if name == "pc_hit_last":
            if not self.phases:
                return {"valid": False}
            v = self.phases.pop(0)
            if v is None:
                return {"valid": False}
            return {"valid": True, "regs": {"s6": f"0x{v:X}"}}
        return {}

    def cmd(self, name, **kw):
        return self._dispatch(name, **kw)

    def raw(self, name, **kw):
        return self._dispatch(name, **kw)


class OraclePhaseTest(unittest.TestCase):
    def setUp(self):
        self._sleep = phase_parity.time.sleep
        phase_parity.time.sleep = lambda *_: None
        self._resume = phase_parity.oracle_resume
        phase_parity.oracle_resume = self._count_resume

    def tearDown(self):
        phase_parity.time.sleep = self._sleep
        phase_parity.oracle_resume = self._resume

    def _count_resume(self, conn):
        conn.resumes += 1

    def test_skips_rejected_phase(self):
        """A neutral reading is resumed past, not accepted as the lock."""
        conn = FakeConn([128, 128, 96])
        got = oracle = phase_parity.oracle_phase(
            conn, 0x8006844C, "s6", reject={128}, out=io.StringIO())
        self.assertEqual(got, 96)
        # It had to resume repeatedly to get past the two neutral frames,
        # but the breakpoint is armed once, not per iteration.
        self.assertGreaterEqual(conn.resumes, 3)
        self.assertEqual(conn.breaks, 1)
        del oracle

    def test_all_rejected_returns_none(self):
        """If only the common value is ever seen, that is a failure, not a lock."""
        conn = FakeConn([128] * 8)
        got = phase_parity.oracle_phase(
            conn, 0x8006844C, "s6", tries=8, reject={128}, out=io.StringIO())
        self.assertIsNone(got)

    def test_accepts_when_nothing_rejected(self):
        conn = FakeConn([128])
        got = phase_parity.oracle_phase(
            conn, 0x8006844C, "s6", reject=set(), out=io.StringIO())
        self.assertEqual(got, 128)

    def test_tolerates_invalid_readings(self):
        conn = FakeConn([None, None, 64])
        got = phase_parity.oracle_phase(
            conn, 0x8006844C, "s6", reject={128}, out=io.StringIO())
        self.assertEqual(got, 64)


if __name__ == "__main__":
    unittest.main()


class BreakpointLeakTest(unittest.TestCase):
    """A leaked breakpoint keeps pausing the oracle across tool runs."""

    def setUp(self):
        self._sleep = phase_parity.time.sleep
        phase_parity.time.sleep = lambda *_: None

    def tearDown(self):
        phase_parity.time.sleep = self._sleep

    def test_no_breakpoint_left_after_success(self):
        conn = FakeConn([96])
        got = phase_parity.oracle_phase(conn, 0x8006844C, "s6",
                                        reject={128}, out=io.StringIO())
        self.assertEqual(got, 96)
        self.assertEqual(conn.armed, set(), "breakpoint leaked on success")

    def test_no_breakpoint_left_after_giving_up(self):
        conn = FakeConn([128] * 6)
        got = phase_parity.oracle_phase(conn, 0x8006844C, "s6", tries=6,
                                        reject={128}, out=io.StringIO())
        self.assertIsNone(got)
        self.assertEqual(conn.armed, set(), "breakpoint leaked on failure")

    def test_stale_breakpoint_from_earlier_run_is_cleared(self):
        """This is the exact state that made effect_palette fail."""
        conn = FakeConn([96])
        conn.armed.add("0x8006844C")      # left by a previous tool
        got = phase_parity.oracle_phase(conn, 0x8006844C, "s6",
                                        reject={128}, out=io.StringIO())
        self.assertEqual(got, 96)
        self.assertEqual(conn.armed, set())

    def test_oracle_resumed_when_arming_fails(self):
        class Refusing(FakeConn):
            def _dispatch(self, name, **kw):
                if name == "pc_break":
                    return {"ok": False, "addr": kw.get("addr"), "slot": -1}
                return FakeConn._dispatch(self, name, **kw)

        conn = Refusing([96])
        with self.assertRaises(Exception):
            phase_parity.oracle_phase(conn, 0x8006844C, "s6",
                                      reject={128}, out=io.StringIO())
        # Observed on the wire, not via a monkeypatch: a 'continue' must have
        # been sent, whichever module actually issued it.
        self.assertIn("continue", conn.sent,
                      "oracle left paused when arming failed")


class ClearBreaksTest(unittest.TestCase):
    """Removal must be verified against pc_break_list, not assumed.

    A paused oracle serves its socket at about 1 Hz, so an unbreak can time
    out; swallowing that is how a breakpoint outlives the tool that set it.
    """

    def setUp(self):
        import psx_gpu_frame
        self.mod = psx_gpu_frame
        self._sleep = psx_gpu_frame.time.sleep
        psx_gpu_frame.time.sleep = lambda *_: None

    def tearDown(self):
        self.mod.time.sleep = self._sleep

    def test_retries_until_list_is_empty(self):
        class Flaky(FakeConn):
            """Ignores the first unbreak, as a timed-out one would."""

            def __init__(self, phases):
                FakeConn.__init__(self, phases)
                self.ignored = 0

            def _dispatch(self, name, **kw):
                if name == "pc_unbreak" and self.ignored < 1:
                    self.ignored += 1
                    self.sent.append(name)
                    return {"ok": False}
                return FakeConn._dispatch(self, name, **kw)

        conn = Flaky([])
        conn.armed.add("0x8006844C")
        self.mod.oracle_clear_breaks(conn)
        self.assertEqual(conn.armed, set())

    def test_raises_when_it_cannot_clear(self):
        class Stuck(FakeConn):
            def _dispatch(self, name, **kw):
                if name == "pc_unbreak":
                    self.sent.append(name)
                    return {"ok": False}
                return FakeConn._dispatch(self, name, **kw)

        conn = Stuck([])
        conn.armed.add("0x8006844C")
        with self.assertRaises(self.mod.DebugError) as cm:
            self.mod.oracle_clear_breaks(conn, tries=2)
        self.assertIn("restart DuckStation", str(cm.exception))

    def test_no_breaks_is_a_no_op(self):
        conn = FakeConn([])
        self.assertEqual(self.mod.oracle_clear_breaks(conn), 0)
