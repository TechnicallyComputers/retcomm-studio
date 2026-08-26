#!/usr/bin/env python3
"""Waiting for the effect instead of racing it.

Every tool here needs the effect RUNNING — a write trace over its packets, a
block probe at one of its instructions, a walk of the list it builds. Requiring
it to be on screen at the instant a button is clicked means racing a transient
animation, and losing that race produces a different-looking failure in each
tool: "no writes recorded", "no candidate fired", "no table found", "the block
was not reached". Four symptoms, one cause, none of them naming it.
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


GF = _load("psx_gpu_frame")


class Appears:
    """class_on_screen stand-in: absent for `after` calls, then present."""

    def __init__(self, after=2, drawing=None):
        self.calls = 0
        self.after = after
        self.drawing = drawing or {"PolyFT4": 584}

    def __call__(self, conn, op):
        self.calls += 1
        if self.calls > self.after:
            return True, dict(self.drawing, **{op: 64})
        return False, self.drawing


class TestWaitForClass(unittest.TestCase):
    def test_it_returns_as_soon_as_the_class_appears(self):
        on, drawing = GF.wait_for_class(None, "PolyG4+semi", timeout=5, poll=0,
                                        check=Appears(after=2))
        self.assertTrue(on)
        self.assertEqual(drawing["PolyG4+semi"], 64)

    def test_it_gives_up_and_reports_what_WAS_drawing(self):
        # The failure has to name what was on screen instead, or the reader
        # cannot tell "wrong scene" from "tool is broken".
        on, drawing = GF.wait_for_class(None, "PolyG4+semi", timeout=0.05,
                                        poll=0.01, check=Appears(after=10**6))
        self.assertFalse(on)
        self.assertIn("PolyFT4", drawing)

    def test_an_already_present_class_does_not_wait(self):
        on, _ = GF.wait_for_class(None, "PolyG4+semi", timeout=5, poll=99,
                                  check=Appears(after=0))
        self.assertTrue(on, "should not have slept at all")

    def test_a_transient_debug_error_does_not_abort_the_wait(self):
        # The emulator can be mid-pause while polling; one failed poll is not a
        # reason to stop waiting for an effect that has not happened yet.
        calls = {"n": 0}

        def flaky(conn, op):
            calls["n"] += 1
            if calls["n"] == 1:
                raise GF.DebugError("read_ram: timed out")
            return True, {op: 64}

        on, _ = GF.wait_for_class(None, "PolyG4+semi", timeout=5, poll=0,
                                  check=flaky)
        self.assertTrue(on)

    def test_it_tells_the_operator_to_trigger_the_effect(self):
        buf = io.StringIO()
        GF.wait_for_class(None, "PolyG4+semi", timeout=5, poll=0, out=buf,
                          check=Appears(after=3))
        self.assertIn("trigger the effect", buf.getvalue())

    def test_the_prompt_is_printed_once_not_per_poll(self):
        buf = io.StringIO()
        GF.wait_for_class(None, "PolyG4+semi", timeout=5, poll=0, out=buf,
                          check=Appears(after=5))
        self.assertEqual(buf.getvalue().count("trigger the effect"), 1)


if __name__ == "__main__":
    unittest.main()


class TestWaitIsAffordable(unittest.TestCase):
    """The cost of the check is the whole design problem.

    class_on_screen() snapshots all 2 MB of RAM — 128 read_ram calls. Against a
    PAUSED oracle, whose socket falls back to a ~1 Hz idle timer, that is 128
    seconds for ONE poll. A 120-second wait then completes a single partial read
    and reports "Drawing instead: nothing", which is exactly what it did.
    """

    class CountingConn:
        def __init__(self, present_after=1):
            self.reads = 0
            self.calls = []
            self.present_after = present_after

        def cmd(self, name, **kw):
            self.calls.append(name)
            if name == "read_ram":
                self.reads += 1
                n = int(kw["len"])
                return {"ok": True, "hex": ("00" * n)}
            return {"ok": True}

        def raw(self, name, **kw):
            return self.cmd(name, **kw)

    def test_it_resumes_before_polling(self):
        # A tool that parked the emulator earlier is the usual reason it is
        # answering at 1 Hz, and nothing else works at that rate.
        c = self.CountingConn()
        GF.wait_for_class(c, "PolyG4+semi", timeout=0.05, poll=0.01)
        self.assertEqual(c.calls[0], "continue")

    def test_a_full_scan_is_not_repeated_when_a_root_is_known(self):
        # The first poll pays for 2 MB; later polls must not. This asserts the
        # cheap path exists in the source, since exercising it needs a real
        # display list in the fake RAM.
        import inspect
        src = inspect.getsource(GF.wait_for_class)
        self.assertIn("read_ram_range", src)
        self.assertIn("root = span = None", src,
                      "a stale root must trigger a rescan, not stick forever")

    def test_the_seam_bypasses_the_scan_entirely(self):
        c = self.CountingConn()
        on, _ = GF.wait_for_class(c, "X", timeout=1, poll=0,
                                  check=lambda conn, op: (True, {op: 1}))
        self.assertTrue(on)
        self.assertEqual(c.reads, 0, "the seam must not fall through to a scan")
