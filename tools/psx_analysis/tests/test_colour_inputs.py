#!/usr/bin/env python3
"""Pacing and cleanup around the oracle's PC breakpoint.

Both of the things tested here already went wrong once, and neither failed in a
way that pointed at its cause.

A DuckStation breakpoint PAUSES the emulator when it fires, and a paused oracle
pumps its debug socket only from a Qt idle timer — 1 Hz with no gamepad
attached. Polling it at 100 ms stacks up connections it cannot accept, and the
result surfaces as "server closed without replying": a network-shaped error
with a pacing-shaped cause.

And a tool that parks the oracle then exits without resuming leaves it crippled
for every later run. The first attempt fails for one reason; every attempt after
it fails for a different one, which is a miserable thing to debug.
"""

import importlib.util
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


CI = _load("colour_inputs")
# Reach the library THROUGH the module under test. Loading psx_gpu_frame a
# second time here would create a distinct DebugError class, and colour_inputs'
# `except DebugError` would not catch the one this file raises — the test would
# fail for a reason that exists only in the test.
GF = sys.modules[CI.DebugError.__module__]


class FakeOracle:
    """Answers pc_hit_last as invalid until `hit_after` polls have gone by."""

    def __init__(self, hit_after=2, drop_once=False):
        self.calls = []
        self.hit_after = hit_after
        self.polls = 0
        self.drop_once = drop_once
        self.running = True

    def cmd(self, name, **kw):
        self.calls.append(name)
        if name == "continue":
            self.running = True
            return {"ok": True}
        if name == "pc_break":
            return {"ok": True}
        if name == "pc_hit_last":
            self.polls += 1
            if self.drop_once and self.polls == 1:
                raise GF.DebugError("pc_hit_last: server closed without replying")
            if self.polls >= self.hit_after:
                self.running = False        # a hit parks the emulator
                return {"ok": True, "valid": True,
                        "regs": {"s4": "0x0011506C", "s6": "0x00000060"}}
            return {"ok": True, "valid": False}
        if name == "pc_hit_clear":
            return {"ok": True}
        raise GF.DebugError(f"unknown {name}")


class TestWaitForHit(unittest.TestCase):
    def test_resumes_before_arming(self):
        # Recovery from a previous run that left the oracle parked. Without
        # this the tool can never succeed again without a manual restart.
        f = FakeOracle()
        CI.wait_for_hit(f, 0x8006844C, timeout=5, poll=0)
        self.assertEqual(f.calls[0], "continue",
                         f"first call was {f.calls[0]}, not a resume")

    def test_returns_the_registers_on_hit(self):
        f = FakeOracle(hit_after=2)
        rep = CI.wait_for_hit(f, 0x8006844C, timeout=5, poll=0)
        self.assertIsNotNone(rep)
        self.assertEqual(rep["regs"]["s4"], "0x0011506C")

    def test_a_dropped_reply_is_survived_not_fatal(self):
        # The hit itself parks the oracle mid-exchange, so losing one reply
        # around that moment is expected rather than a failure.
        f = FakeOracle(hit_after=3, drop_once=True)
        self.assertIsNotNone(CI.wait_for_hit(f, 0x8006844C, timeout=5, poll=0))

    def test_gives_up_and_returns_none_rather_than_hanging(self):
        f = FakeOracle(hit_after=10**9)
        self.assertIsNone(CI.wait_for_hit(f, 0x8006844C, timeout=0.2, poll=0.05))

    def test_default_poll_matches_a_paused_oracle(self):
        # The idle timer is 1 Hz without a gamepad. Polling faster than that is
        # what produced "server closed without replying".
        self.assertGreaterEqual(GF.ORACLE_PAUSED_POLL_S, 1.0)


class TestOracleResume(unittest.TestCase):
    def test_reports_success(self):
        f = FakeOracle()
        self.assertTrue(GF.oracle_resume(f))
        self.assertIn("continue", f.calls)

    def test_a_failure_is_reported_not_raised(self):
        class Dead:
            def cmd(self, *a, **k):
                raise GF.DebugError("no debug server")
        self.assertFalse(GF.oracle_resume(Dead()))


if __name__ == "__main__":
    unittest.main()


class TestAddressCorrespondence(unittest.TestCase):
    """Addresses do not carry between the two emulators.

    Measured: the oracle's packet buffer sat at 0x10D9A0 while psx-runtime's was
    at 0x115078 — the same structures, different addresses. Reading the oracle's
    pointer on psx-runtime therefore lands on unrelated memory, and comparing it
    reports a difference the tool invented. The first version of this tool did
    exactly that and called it "source-differs".
    """

    def test_a_16bit_array_is_not_mistaken_for_a_colour_table(self):
        # The bytes actually read from psx-runtime at the oracle's address:
        # 16-bit values, every odd byte zero. Not a colour table at all.
        sixteen = bytes.fromhex("1b0038004d004e001b001d0038001d004e001f00")
        self.assertFalse(CI.looks_like_triplets(sixteen))

    def test_packed_rgb_entries_are_recognised(self):
        # The oracle's real table: (248,80,80),(8,8,8),... on a 4-byte stride.
        triplets = bytes.fromhex("f850500008080800f850503800000000")
        self.assertTrue(CI.looks_like_triplets(triplets))

    def test_the_table_is_found_by_content_not_by_address(self):
        needle = bytes.fromhex("f850500008080800f850503800000000")
        ram = bytearray(4096)
        ram[0x800:0x800 + len(needle)] = needle
        hits = CI.find_table(bytes(ram), needle)
        self.assertEqual(hits, [0x800])

    def test_absence_is_reported_as_absence_not_as_a_difference(self):
        # If the table is nowhere in psx-runtime's RAM, that is a finding about
        # the data — not a byte-level disagreement at some arbitrary address.
        needle = bytes.fromhex("f85050000808080000000000")
        self.assertEqual(CI.find_table(bytes(bytearray(4096)), needle), [])

    def test_a_too_short_needle_is_refused(self):
        # A handful of bytes matches everywhere in 2 MB; a match would mean
        # nothing and the address it returned would be arbitrary.
        ram = bytes(bytearray(4096))
        self.assertEqual(CI.find_table(ram, b"\x00\x00\x00\x00"), [])

    def test_multiple_hits_are_all_reported(self):
        needle = bytes.fromhex("f850500008080800f850503800000000")
        ram = bytearray(8192)
        ram[0x100:0x100 + len(needle)] = needle
        ram[0x900:0x900 + len(needle)] = needle
        self.assertEqual(CI.find_table(bytes(ram), needle), [0x100, 0x900])


class TestGraduatedMatch(unittest.TestCase):
    """An exact miss is not the same finding as absence.

    Measured on a live pair: the oracle's 32-byte colour table was absent from
    psx-runtime as a contiguous run, yet its first 4 bytes appeared six times on
    a 0x18 stride, and a nearby region (0x0E4600) was byte-identical between the
    two emulators. Calling that "the table does not exist anywhere" was wrong,
    and wrong in a direction the operator could not check — it points upstream
    at data generation when the data plainly exists.
    """

    def test_the_longest_present_prefix_is_reported(self):
        needle = bytes.fromhex("f850500008080800f850503800000000")
        ram = bytearray(0x2000)
        ram[0x100:0x104] = needle[:4]        # only the first 4 bytes exist
        k, hits = CI.graduated_find(bytes(ram), needle)
        self.assertEqual(k, 4)
        self.assertEqual(hits, [0x100])

    def test_a_full_match_short_circuits_at_the_longest_length(self):
        needle = bytes.fromhex("f850500008080800f850503800000000") * 2
        ram = bytearray(0x2000)
        ram[0x400:0x400 + 32] = needle[:32]
        k, hits = CI.graduated_find(bytes(ram), needle)
        self.assertEqual(k, 32)

    def test_a_regular_stride_is_detected(self):
        # This is what psx-runtime actually looked like: the same 4-byte entry
        # repeating every 0x18 bytes — a different record size, not absence.
        ram = bytearray(0x2000)
        for i in range(6):
            ram[0x100 + i * 0x18:0x100 + i * 0x18 + 4] = b"\xf8\x50\x50\x00"
        k, hits = CI.graduated_find(bytes(ram), b"\xf8\x50\x50\x00" + b"\xff" * 28)
        self.assertEqual(k, 4)
        self.assertEqual(CI.stride_of(hits), 0x18)

    def test_irregular_hits_report_no_stride(self):
        self.assertEqual(CI.stride_of([0x10, 0x33, 0x91]), 0)

    def test_two_hits_are_too_few_to_claim_a_stride(self):
        # Any two points are evenly spaced; that is not evidence of a layout.
        self.assertEqual(CI.stride_of([0x10, 0x28]), 0)

    def test_nothing_present_reports_zero(self):
        k, hits = CI.graduated_find(bytes(bytearray(0x800)), b"\xf8\x50\x50\x11")
        self.assertEqual((k, hits), (0, []))


class TestNoNullsInReports(unittest.TestCase):
    """A report must not express "missing" as an explicit null.

    A consumer reading these with a typed accessor throws on null rather than
    falling back to a default, and one did: "block_leader": null, written for a
    probe that had not fired, aborted the Studio outright. The two ways of
    saying "this key has no value" should not behave differently.
    """

    def test_absent_probe_fields_are_omitted_not_nulled(self):
        pr = {"error": "no candidate fired"}      # nothing else was learned
        keys = ("block_leader", "frame", "samples_seen", "error",
                "leader_after_target")
        doc = {k: pr[k] for k in keys if pr.get(k) is not None}
        self.assertEqual(doc, {"error": "no candidate fired"})
        self.assertNotIn("block_leader", doc)

    def test_present_fields_survive(self):
        pr = {"block_leader": "0x8006842C", "frame": 9001, "samples_seen": 12}
        keys = ("block_leader", "frame", "samples_seen", "error",
                "leader_after_target")
        doc = {k: pr[k] for k in keys if pr.get(k) is not None}
        self.assertEqual(doc["block_leader"], "0x8006842C")
        self.assertEqual(doc["samples_seen"], 12)

    def test_a_false_value_is_kept_not_treated_as_missing(self):
        # leader_after_target=False is a real answer; filtering on truthiness
        # instead of "is not None" would silently drop it.
        pr = {"leader_after_target": False}
        doc = {k: pr[k] for k in ("leader_after_target",)
               if pr.get(k) is not None}
        self.assertIn("leader_after_target", doc)
        self.assertIs(doc["leader_after_target"], False)


class TestPhaseIndependentRegion(unittest.TestCase):
    """$s4 moves with the animation, so a window at each side's own $s4 is not
    a comparison.

    Measured across two samples: psx-runtime's $s4 went 0x800E2634 -> 0x800E4634
    while $s6 went 72 -> 128. The pointer tracks the effect. Comparing 48 bytes
    at each side's own pointer therefore compares different ENTRIES of the same
    array whenever the two emulators are at different moments — which they
    always are — and it reported "source-differs" twice for no guest reason.
    """

    def test_the_region_covers_both_pointers(self):
        lo, hi = CI.region_bounds(0x000E4628, 0x000E2BF8)
        self.assertLess(lo, 0x000E2BF8)
        self.assertGreater(hi, 0x000E4628)

    def test_the_region_is_order_independent(self):
        self.assertEqual(CI.region_bounds(0x1000, 0x9000),
                         CI.region_bounds(0x9000, 0x1000))

    def test_it_is_word_aligned(self):
        lo, _ = CI.region_bounds(0x000E4629, 0x000E2BFB)
        self.assertEqual(lo & 3, 0)

    def test_it_is_clamped_to_ram(self):
        lo, hi = CI.region_bounds(0x10, 0x1FFFF0)
        self.assertGreaterEqual(lo, 0)
        self.assertLessEqual(hi, 0x200000)

    def test_identical_pointers_still_give_a_usable_span(self):
        lo, hi = CI.region_bounds(0x000E4000, 0x000E4000)
        self.assertGreater(hi - lo, 0x1000,
                           "a zero-width region would compare nothing and "
                           "report a match")


class TestDifferenceShape(unittest.TestCase):
    """A count is not a finding.

    "2747 of 23088 bytes differ" is compatible with several completely
    different causes: one side has not written the data at all, one is writing
    where the other does not, or both hold values that disagree. Each points
    somewhere else, so the shape has to be reported alongside the number.
    """

    def test_zeros_on_one_side_are_called_out_as_not_written(self):
        a = bytes(8)
        b = bytes.fromhex("f8505000f8f8b000")
        cl = CI.diff_clusters(a, b, 0xE0000)
        self.assertIn("has not written this", CI.describe_difference(cl))

    def test_zeros_on_the_other_side_are_distinguished(self):
        a = bytes.fromhex("f8505000f8f8b000")
        b = bytes(8)
        self.assertIn("oracle holds zeros",
                      CI.describe_difference(CI.diff_clusters(a, b, 0)))

    def test_both_holding_data_is_reported_as_a_real_disagreement(self):
        a = bytes.fromhex("f8505000")
        b = bytes.fromhex("08080800")
        self.assertIn("both sides hold data",
                      CI.describe_difference(CI.diff_clusters(a, b, 0)))

    def test_entry_alignment_is_noted(self):
        # Differences on 4-byte boundaries mean whole colour entries changed,
        # not stray bytes — which separates a data difference from corruption.
        a = bytes.fromhex("00000000" + "f8505000")
        b = bytes.fromhex("00000000" + "08080800")
        self.assertIn("4-byte boundaries",
                      CI.describe_difference(CI.diff_clusters(a, b, 0)))

    def test_nearby_differing_bytes_become_one_cluster(self):
        # A 4-byte entry usually differs in only some of its bytes; reporting
        # that as three findings buries it.
        a = bytes.fromhex("f8005000")
        b = bytes.fromhex("08ff0800")
        cl = CI.diff_clusters(a, b, 0)
        self.assertEqual(len(cl), 1)

    def test_distant_differences_stay_separate(self):
        a = bytes.fromhex("ff" + "00" * 20 + "ff")
        b = bytes.fromhex("00" + "00" * 20 + "00")
        self.assertEqual(len(CI.diff_clusters(a, b, 0)), 2)

    def test_identical_buffers_have_no_clusters(self):
        a = bytes.fromhex("f8505000")
        self.assertEqual(CI.diff_clusters(a, a, 0), [])
        self.assertEqual(CI.describe_difference([]), "")


class TestPhaseDependenceGate(unittest.TestCase):
    """The check that decides whether a region difference means anything.

    Every cross-emulator comparison in this investigation has been confounded by
    phase — frame numbers, buffer halves, array offsets, and now possibly this
    region. The churn check (re-read after 0.4s) is too short to catch a region
    rewritten only when the animation STEPS, which is why a region can read as
    "static" and still be phase-driven.

    Sampling one emulator at two different scales needs no alignment and no
    second emulator, and it answers the question outright.
    """

    class FakeConn:
        """Returns different region bytes once the scale has moved on."""

        def __init__(self, changes=True):
            self.changes = changes
            self.scale = 0x80
            self.reads = 0

        def cmd(self, name, **kw):
            if name == "read_ram":
                self.reads += 1
                n = int(kw["len"])
                fill = b"\xaa" if (self.reads == 1 or not self.changes) else b"\xbb"
                return {"ok": True, "hex": (fill * n).hex()}
            return {"ok": True}

    def test_a_region_that_moves_with_the_scale_is_flagged(self):
        import io
        conn = self.FakeConn(changes=True)
        with unittest.mock.patch.object(
                CI, "region_phase_dependence", wraps=CI.region_phase_dependence):
            pass
        # Drive the helper directly with a stub probe.
        import probe_regs
        orig = probe_regs.probe_registers
        probe_regs.probe_registers = lambda *a, **k: {"regs": {"s6": "0x48"}}
        try:
            r = CI.region_phase_dependence(conn, 0x8006844C, 0x1000, 0x1100,
                                           0x80, tries=2, gap=0,
                                           out=io.StringIO())
        finally:
            probe_regs.probe_registers = orig
        self.assertTrue(r["tested"])
        self.assertTrue(r["phase_dependent"])
        self.assertEqual(r["to_scale"], 0x48)

    def test_a_stable_region_is_not_flagged(self):
        import io, probe_regs
        conn = self.FakeConn(changes=False)
        orig = probe_regs.probe_registers
        probe_regs.probe_registers = lambda *a, **k: {"regs": {"s6": "0x48"}}
        try:
            r = CI.region_phase_dependence(conn, 0x8006844C, 0x1000, 0x1100,
                                           0x80, tries=2, gap=0,
                                           out=io.StringIO())
        finally:
            probe_regs.probe_registers = orig
        self.assertFalse(r["phase_dependent"])
        self.assertEqual(r["changed_bytes"], 0)

    def test_a_scale_that_never_moves_reports_untested(self):
        # Not the same as "not phase dependent" — nothing was learned, and
        # saying otherwise would license the comparison on no evidence.
        import io, probe_regs
        conn = self.FakeConn()
        orig = probe_regs.probe_registers
        probe_regs.probe_registers = lambda *a, **k: {"regs": {"s6": "0x80"}}
        try:
            r = CI.region_phase_dependence(conn, 0x8006844C, 0x1000, 0x1100,
                                           0x80, tries=3, gap=0,
                                           out=io.StringIO())
        finally:
            probe_regs.probe_registers = orig
        self.assertFalse(r["tested"])
        self.assertNotIn("phase_dependent", r)
