#!/usr/bin/env python3
"""Does the colour scale animate on each side?

Every vertex colour in this effect is source_rgb * $s6 >> 7, so $s6 IS the
fade: 128 leaves the colour unchanged, smaller values darken it. A bright,
unfaded effect is what a scale pinned at 128 would produce.

This compares VARIATION rather than values, which is the point. Frame numbers,
buffer halves and animation phase have each produced a confident wrong answer
in this investigation; a register that sweeps on one side and sits still on the
other is a difference none of those can manufacture.
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


ST = _load("scale_trace")


class TestDescribe(unittest.TestCase):
    def d(self, vals):
        return ST.describe(vals, "x", out=io.StringIO())

    def test_a_constant_series_is_flagged_constant(self):
        d = self.d([128] * 10)
        self.assertTrue(d["constant"])
        self.assertEqual(d["distinct"], 1)

    def test_the_neutral_value_is_called_out_specifically(self):
        # 128 is not just "constant" — it is the value at which the multiply
        # does nothing, which is why a pinned 128 and a pinned 64 mean very
        # different things.
        self.assertTrue(self.d([128] * 5)["neutral_only"])
        self.assertFalse(self.d([64] * 5)["neutral_only"])

    def test_a_varying_series_is_not_constant(self):
        d = self.d([52, 64, 68, 76, 96, 126, 128])
        self.assertFalse(d["constant"])
        self.assertEqual(d["min"], 52)
        self.assertEqual(d["max"], 128)

    def test_no_samples_yields_nothing_rather_than_a_default(self):
        self.assertIsNone(self.d([]))


class TestVerdictLogic(unittest.TestCase):
    """The four outcomes point in four different directions."""

    @staticmethod
    def shape(vals):
        return ST.describe(vals, "x", out=io.StringIO())

    def test_native_pinned_and_oracle_varying_is_the_suspected_fault(self):
        a = self.shape([128] * 12)
        b = self.shape([52, 64, 76, 96])
        self.assertTrue(a["constant"] and not b["constant"])
        self.assertTrue(a["neutral_only"])

    def test_both_varying_means_the_fade_is_not_missing(self):
        a = self.shape([60, 90, 128])
        b = self.shape([52, 96, 128])
        self.assertFalse(a["constant"])
        self.assertFalse(b["constant"])

    def test_both_constant_is_inconclusive_not_a_finding(self):
        # Most likely the effect simply is not animating on either side right
        # now, which says nothing about the bug.
        a = self.shape([128] * 5)
        b = self.shape([128] * 5)
        self.assertTrue(a["constant"] and b["constant"])

    def test_the_reverse_asymmetry_is_distinguished(self):
        # Oracle pinned while ours varies is the opposite of the expectation and
        # should not be reported as if it confirmed it.
        a = self.shape([60, 90, 128])
        b = self.shape([128] * 5)
        self.assertFalse(a["constant"])
        self.assertTrue(b["constant"])


if __name__ == "__main__":
    unittest.main()


class TestOneSidedResults(unittest.TestCase):
    """A side that produced nothing must say WHY, and the other side still counts.

    The first real run came back with oracle: null and verdict "incomplete",
    which reports that there is no comparison without reporting whether the
    emulator was unreachable, never reached the PC, or refused the breakpoint.
    Those call for different responses and the reason is known at the point of
    failure and nowhere else.

    It also threw away the half that DID answer — and that half disproved a
    hypothesis outright: psx-runtime's $s6 read 66, 72 and 128 across three
    samples, so it is not pinned at the neutral value after all.
    """

    def d(self, vals):
        return ST.describe(vals, "x", out=io.StringIO())

    def test_a_varying_side_disproves_the_pinned_hypothesis(self):
        got = self.d([66, 72, 128])
        self.assertFalse(got["constant"])
        self.assertFalse(got["neutral_only"])
        self.assertEqual(got["distinct"], 3)

    def test_three_identical_samples_would_have_supported_it(self):
        # Which is what three separate single-sample runs looked like, and why
        # sampling repeatedly is the difference between a pattern and an
        # artefact of when each run happened to look.
        got = self.d([128, 128, 128])
        self.assertTrue(got["constant"])
        self.assertTrue(got["neutral_only"])

    def test_samplers_return_a_reason_alongside_the_values(self):
        # Both return (values, reason) so an empty result can explain itself.
        import inspect
        for fn in (ST.sample_native, ST.sample_oracle):
            src = inspect.getsource(fn)
            self.assertIn("return vals", src)
            self.assertIn("Returns (values", src,
                          f"{fn.__name__} does not document the reason it returns")


class TestGranularity(unittest.TestCase):
    """Both animating does not mean both animating the SAME WAY.

    Measured: the oracle stepped 122/124/126/128 — by 2, across a 6-wide band —
    while psx-runtime showed 28 and 128 with nothing between. A fade that sweeps
    smoothly and one that jumps between extremes produce very different pictures
    from identical geometry, and min/max alone cannot tell them apart.
    """

    def d(self, vals):
        return ST.describe(vals, "x", out=io.StringIO())

    def test_step_size_is_reported(self):
        d = self.d([122, 124, 126, 128])
        self.assertEqual(d["max_step"], 2)
        self.assertEqual(d["median_step"], 2)

    def test_a_jump_between_extremes_shows_a_large_step(self):
        self.assertEqual(self.d([28, 128])["max_step"], 100)

    def test_range_alone_would_not_separate_them(self):
        # Same min and max, completely different behaviour.
        smooth = self.d(list(range(28, 129, 4)))
        jumpy = self.d([28, 128])
        self.assertEqual((smooth["min"], smooth["max"]),
                         (jumpy["min"], jumpy["max"]))
        self.assertLess(smooth["max_step"], jumpy["max_step"])

    def test_a_single_value_has_no_steps(self):
        d = self.d([128] * 5)
        self.assertEqual(d["max_step"], 0)
        self.assertEqual(d["median_step"], 0)

    def test_two_distinct_of_three_samples_is_not_evidence(self):
        # Which is exactly what the first complete run produced. Sparse
        # sampling of a smooth ramp looks identical to a genuine jump.
        d = self.d([28, 128, 128])
        self.assertEqual(d["samples"], 3)
        self.assertEqual(d["distinct"], 2)


class TestMinimumSamplesForAVerdict(unittest.TestCase):
    """"Never moved" is a claim about a series, and one sample is not a series.

    The tool reported verdict "native-not-animating" from ONE psx-runtime
    sample, with the note "never moved from 128 across 1 samples". That is not
    a claim anyone can make — and it happened to agree with a hypothesis this
    investigation had already disproved twice, which is the most dangerous
    shape a bug can take: it confirms what someone already suspects.

    The cause was upstream. Caching the block leader to speed up sampling
    removed the fallback search, so the moment the cached address stopped
    firing the run ended — turning a 24-sample request into one sample and then
    drawing a conclusion from it.
    """

    def d(self, vals):
        return ST.describe(vals, "x", out=io.StringIO())

    def test_one_sample_cannot_establish_constancy(self):
        d = self.d([128])
        self.assertTrue(d["constant"], "trivially true of a single value")
        self.assertEqual(d["samples"], 1)
        # ...which is exactly why the verdict must not rest on `constant` alone.

    def test_five_identical_samples_is_the_threshold(self):
        d = self.d([128] * 5)
        self.assertTrue(d["constant"])
        self.assertEqual(d["samples"], 5)

    def test_a_varying_side_needs_no_such_floor(self):
        # Variation is positive evidence: seeing two different values proves it
        # moves, however few samples there are.
        d = self.d([112, 128])
        self.assertFalse(d["constant"])

    def test_the_oracle_series_from_the_live_run_is_a_smooth_ramp(self):
        d = self.d([112, 116, 120, 124, 128])
        self.assertEqual(d["max_step"], 4)
        self.assertEqual(d["distinct"], 5)
        self.assertFalse(d["constant"])


class TestSamplingOrder(unittest.TestCase):
    """Both sides must be watched during the SAME pass.

    Reported from a live session: psx-runtime visibly parked while the oracle
    ran on untouched, then the oracle reported no samples. That is exactly what
    sequential sampling produces — psx-runtime is sampled to completion first
    and the oracle asked afterwards, by which time the effect has finished on
    that side and the breakpoint can never fire. Replaying the animation does
    not help, because the two halves of the run are minutes apart.
    """

    def test_both_samplers_run_on_threads(self):
        import inspect
        src = inspect.getsource(ST.main)
        self.assertIn("threading.Thread", src)
        self.assertIn("nt.start()", src)
        self.assertIn("ot.start()", src)
        # Started before either is joined, or they are still sequential.
        self.assertLess(src.index("ot.start()"), src.index("nt.join()"))

    def test_a_thread_failure_becomes_a_reason_not_a_crash(self):
        src = __import__("inspect").getsource(ST.main)
        self.assertIn("except DebugError", src)


class TestPerFrameOrder(unittest.TestCase):
    """arm -> STEP -> read. In that order, or nothing is ever captured.

    probe_registers arms the probe and then sleeps waiting for it to fire. On a
    paused emulator no code executes, so the block leader is never reached and
    every sample after the first comes back empty — which turned a 24-sample
    run into one sample, and then a verdict was drawn from it.
    """

    def test_the_step_happens_between_arming_and_reading(self):
        import inspect
        src = inspect.getsource(ST.sample_per_frame)
        arm = src.index("pc_probe_arm")
        step = src.index('"step"')
        dump = src.index("pc_probe_dump")
        self.assertLess(arm, step, "arming must precede the step")
        self.assertLess(step, dump, "the step must precede the read")

    def test_it_does_not_delegate_to_the_sleeping_prober(self):
        # probe_registers is fine on a RUNNING emulator and useless on a paused
        # one; per-frame sampling pauses by design.
        import inspect
        src = inspect.getsource(ST.sample_per_frame)
        self.assertNotIn("probe_registers(", src)

    def test_a_frame_that_misses_the_block_resets_the_cached_leader(self):
        import inspect
        src = inspect.getsource(ST.sample_per_frame)
        self.assertIn("leader = None", src)


class TestNativeOnlyIsEnough(unittest.TestCase):
    """"Does OUR fade sweep or jump" needs one emulator, not two.

    Consecutive frames give the real animation increment. No oracle, no
    alignment, no shared phase. Requiring both sides forced the operator to keep
    two emulators inside the same effect simultaneously — the hardest part of
    this whole exercise, and unnecessary for this question.
    """

    def d(self, vals):
        return ST.describe(vals, "x", out=io.StringIO())

    def test_a_smooth_per_frame_sweep_is_recognisable(self):
        d = self.d([128, 126, 124, 122, 120, 118])
        self.assertLessEqual(d["max_step"], 8)
        self.assertEqual(d["samples"], 6)

    def test_a_coarse_per_frame_fade_is_recognisable(self):
        d = self.d([128, 94, 48, 20, 128])
        self.assertGreater(d["max_step"], 8)

    def test_five_samples_is_the_floor_for_a_standalone_verdict(self):
        # Fewer cannot distinguish a sweep from sparse sampling of one, which
        # is the mistake this tool has already made twice.
        self.assertEqual(self.d([128, 126, 124, 122, 120])["samples"], 5)

    def test_the_oracle_series_remains_an_upper_bound(self):
        # Free-running samples land ~90 frames apart, so the oracle's apparent
        # step can only overstate how finely it moves, never understate it.
        d = self.d([116, 120, 124, 128])
        self.assertEqual(d["max_step"], 4)


class TestPerFrameFallback(unittest.TestCase):
    """Per-frame is the better measurement; an empty result is not.

    Reported: repeated runs capturing nothing from psx-runtime while the effect
    was being replayed. The free-running probe demonstrably works on this code —
    it returned 4800 hits and real register values — so per-frame capturing
    nothing is a property of stepping, not of the code being unreachable.

    Falling back produces data with a stated caveat instead of a fourth empty
    run, and the report records WHICH method produced the numbers so a reader
    does not treat aliased steps as an animation increment.
    """

    def test_the_fallback_is_recorded_not_silent(self):
        import inspect
        src = inspect.getsource(ST.main)
        self.assertIn("free-running-fallback", src)

    def test_a_fallback_result_does_not_claim_a_per_frame_verdict(self):
        # The standalone per-frame verdict says "steps up to N is the real
        # animation increment". That claim is false for aliased samples.
        import inspect
        src = inspect.getsource(ST.main)
        i = src.index('native_sampling") != "free-running-fallback"')
        j = src.index("native-per-frame-only")
        self.assertLess(i, j, "the guard must precede the verdict it protects")

    def test_the_scene_is_checked_before_resuming(self):
        # Checking after the resume reports what is on screen seconds later,
        # which reliably says "not on screen" for a brief effect that was there
        # throughout sampling.
        import inspect
        src = inspect.getsource(ST.sample_per_frame)
        check = src.index("still_on, drawing_now = class_on_screen")
        resume = src.index('conn.cmd("continue")')
        self.assertLess(check, resume)

    def test_a_leader_that_fired_is_distinguished_from_none_firing(self):
        # "no candidate fired" and "one fired but gave no sample" have
        # different causes and different fixes.
        import inspect
        src = inspect.getsource(ST.sample_per_frame)
        self.assertIn("any_fired", src)
        self.assertIn("NO candidate fired", src)


class TestAliasedDataIsNeverCalledPerFrame(unittest.TestCase):
    """The one claim that must never be made from free-running samples.

    A report came back with native_sampling "free-running-fallback" AND
    granularity "native-measured-per-frame" — saying in the same document that
    the data was aliased and that its steps were the animation increment. The
    guard existed, but on a different branch than the one that fired.

    Free-running samples land about ninety frames apart, so their differences
    are aliasing. Presenting them as an increment invites exactly the wrong
    conclusion: that our fade jumps by 44 when nothing measured that.
    """

    def test_every_per_frame_claim_checks_the_sampling_method(self):
        import inspect
        src = inspect.getsource(ST.main)
        # Each place that sets a per-frame verdict must be guarded.
        for marker in ('"native-measured-per-frame"', '"native-per-frame-only"'):
            i = src.index(marker)
            window = src[max(0, i - 400):i]
            # Either spelling of the guard is fine — excluding the fallback, or
            # requiring per-frame outright. What matters is that the claim is
            # conditioned on HOW the numbers were obtained.
            self.assertIn("native_sampling", window,
                          f"{marker} is set without checking native_sampling")

    def test_a_fallback_run_gets_its_own_granularity_label(self):
        import inspect
        self.assertIn('"native-aliased"', inspect.getsource(ST.main))


class TestSampleSelection(unittest.TestCase):
    """Take a register sample from whichever fired leader has one.

    Insisting on the single nearest leader discarded frames where a leader
    fired and the recorded sample belonged to a different one — reported as "a
    leader fired but no register sample came with it", which is true of that
    leader and false of the frame.
    """

    def test_it_prefers_the_nearest_leader_at_or_below_the_target(self):
        import inspect
        src = inspect.getsource(ST.sample_per_frame)
        self.assertIn("key=lambda x: -int(x[\"pc\"], 16)", src,
                      "candidates must be ranked nearest-first")

    def test_it_falls_through_to_another_leader_rather_than_giving_up(self):
        import inspect
        src = inspect.getsource(ST.sample_per_frame)
        self.assertIn("for cand in ranked:", src)
        self.assertIn("break", src)


class TestSamplingMethodIsAlwaysRecorded(unittest.TestCase):
    """The report must say HOW the numbers were obtained, on every path.

    A clean run came back with four psx-runtime samples stepping by 2 and no
    native_sampling field at all, because it was only set on the fallback path.
    A per-frame series and a free-running one then look identical in the report
    — and they support completely different claims. Steps of 2 across
    consecutive frames is a smooth fade; steps of 2 across samples ninety
    frames apart is nothing.
    """

    def test_a_default_is_set_before_either_path_runs(self):
        import inspect
        src = inspect.getsource(ST.main)
        self.assertIn('doc["native_sampling"] = "free-running"', src)

    def test_success_labels_itself_per_frame(self):
        import inspect
        src = inspect.getsource(ST.main)
        self.assertIn('doc["native_sampling"] = "per-frame"', src)

    def test_the_fallback_still_labels_itself(self):
        import inspect
        src = inspect.getsource(ST.main)
        self.assertIn('"free-running-fallback"', src)


class TestConsecutiveFramesNeedNoQuorum(unittest.TestCase):
    """Four adjacent frames stepping by 2 IS the step.

    Per-frame samples measure the increment directly rather than sampling a
    distribution, so the eight-sample floor that free-running data needs does
    not apply — and applying it discarded a measurement that was already
    conclusive.
    """

    def test_four_per_frame_samples_are_enough(self):
        import inspect
        src = inspect.getsource(ST.main)
        i = src.index('doc["granularity"] = "native-measured-per-frame"')
        # The condition sits above the comment block explaining it.
        self.assertIn('a["samples"] >= 4', src[max(0, i - 700):i])

    def test_free_running_still_needs_more(self):
        import inspect
        src = inspect.getsource(ST.main)
        self.assertIn('a["samples"] >= 8 and b["samples"] >= 8', src)
