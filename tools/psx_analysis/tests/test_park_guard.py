#!/usr/bin/env python3
"""The emulator must be handed back on every path.

Reported: "Find colour writers keeps pausing the native and leaving it paused,
causing it to fail to find the writer." Leaving it parked is worse than any
error the tool can report — the game stops advancing, so the effect never comes
round again, every retry walks the same stale display list and fails
identically, and the cause is invisible from outside.

The park deliberately spans the walk AND the trace, because the layout has to
still describe the buffer when the writes happen. So a try/finally around
either half alone is not enough; every exit between them needs the guarantee.
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


PW = _load("packet_writers")


class FakeConn:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def cmd(self, name, **kw):
        self.calls.append(name)
        if name == self.fail_on:
            raise PW.DebugError(f"{name} failed")
        return {"ok": True}


class TestParkGuard(unittest.TestCase):
    def test_it_resumes_on_a_normal_exit(self):
        c = FakeConn()
        with PW.ParkGuard(c):
            pass
        self.assertEqual(c.calls, ["pause", "continue"])

    def test_it_resumes_when_the_body_raises(self):
        c = FakeConn()
        with self.assertRaises(ValueError):
            with PW.ParkGuard(c):
                raise ValueError("boom")
        self.assertIn("continue", c.calls)

    def test_it_resumes_when_the_body_returns_early(self):
        # The actual defect: an early return from the middle of the parked
        # section left the game stopped.
        c = FakeConn()

        def body():
            with PW.ParkGuard(c):
                return 1
        self.assertEqual(body(), 1)
        self.assertIn("continue", c.calls)

    def test_resuming_twice_only_resumes_once(self):
        # The trace block resumes explicitly when it is done; the guard must
        # not send a second continue on the way out.
        c = FakeConn()
        with PW.ParkGuard(c) as p:
            p.resume()
        self.assertEqual(c.calls.count("continue"), 1)

    def test_a_failed_resume_does_not_mask_the_real_error(self):
        c = FakeConn(fail_on="continue")
        with self.assertRaises(ValueError):
            with PW.ParkGuard(c):
                raise ValueError("the real problem")

    def test_it_does_not_swallow_exceptions(self):
        c = FakeConn()
        with self.assertRaises(KeyError):
            with PW.ParkGuard(c):
                raise KeyError("k")


if __name__ == "__main__":
    unittest.main()
