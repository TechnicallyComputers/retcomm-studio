"""Tests for walk_side's coherence check.

Reading a display list out of a RUNNING emulator can catch the game
mid-rebuild. A torn read does not look like an error -- it looks like a frame
of primitives carrying a handful of near-identical colours, which is
indistinguishable from a real finding. It must be detected, and detecting it
must not require pausing the emulator: parking DuckStation for every sample
made it stutter through the very animation it was watching.
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gpu_display_list as gdl  # noqa: E402


class RecordingConn:
    """Serves RAM reads from a script of successive memory states."""

    def __init__(self, states):
        self.states = list(states)
        self.reads = 0
        self.commands = []

    def cmd(self, name, **kw):
        self.commands.append(name)
        if name == "frame":
            return {"frame": 1}
        return {}

    def raw(self, name, **kw):
        self.commands.append(name)
        return {}

    def frame(self):
        return 1

    def read(self):
        """Each read consumes the next state; the last one repeats."""
        self.reads += 1
        return self.states[min(self.reads - 1, len(self.states) - 1)]


class ListShapeTest(unittest.TestCase):
    """Structure, not bytes.

    Byte-identity was the obvious coherence check and it is exactly wrong:
    while the effect plays the game rewrites the list every frame, so two
    consecutive reads never match and every ANIMATED frame is discarded as
    torn -- keeping only the frames without the animation in them. That is
    what made a real run report '172 walks read, none held additive quads'
    after the animation had been played several times.
    """

    def entries(self, ops):
        return [{"op": op, "n_words": n} for op, n in ops]

    def test_same_shape_when_only_data_changed(self):
        a = self.entries([(0x38, 8), (0x30, 6)])
        b = self.entries([(0x38, 8), (0x30, 6)])
        self.assertEqual(gdl._list_shape(a), gdl._list_shape(b))

    def test_differs_when_an_entry_is_lost(self):
        a = self.entries([(0x38, 8), (0x30, 6)])
        b = self.entries([(0x38, 8)])
        self.assertNotEqual(gdl._list_shape(a), gdl._list_shape(b))

    def test_differs_when_a_length_is_malformed(self):
        """A torn read shows up as a word count that no longer fits its op."""
        a = self.entries([(0x38, 8)])
        b = self.entries([(0x38, 3)])
        self.assertNotEqual(gdl._list_shape(a), gdl._list_shape(b))

    def test_differs_when_an_opcode_changes(self):
        a = self.entries([(0x38, 8)])
        b = self.entries([(0x30, 8)])
        self.assertNotEqual(gdl._list_shape(a), gdl._list_shape(b))

    def test_empty_lists_compare_equal(self):
        self.assertEqual(gdl._list_shape([]), gdl._list_shape([]))

    def test_walk_side_accepts_park_for_reread_flag(self):
        """The flag exists and defaults to parking for CLI callers."""
        import inspect
        sig = inspect.signature(gdl.walk_side)
        self.assertIn("park_for_reread", sig.parameters)
        self.assertTrue(sig.parameters["park_for_reread"].default)

    def test_verify_branch_does_not_compare_two_reads(self):
        """Guard against reintroducing either rejected-everything filter.

        Byte-identity rejected every frame in which a colour changed; whole
        list-shape equality rejected every frame in which the list changed
        size. During an animation that is all of them, so both kept only the
        frames without the animation and called the rest corruption.
        """
        src = inspect_source(gdl.walk_side)
        verify = src.split("else:\n            # Read once", 1)[1]
        verify = verify.split('meta["coherent"] = coherent', 1)[0]
        self.assertNotIn("if first == second:", verify)
        self.assertNotIn("_list_shape(", verify)
        self.assertIn("_accept(first)", verify)

    def test_single_read_in_the_verify_branch(self):
        """One read, not two: the walk's own validation is the check."""
        src = inspect_source(gdl.walk_side)
        verify = src.split("else:\n            # Read once", 1)[1]
        verify = verify.split('meta["coherent"] = coherent', 1)[0]
        self.assertEqual(verify.count("read_ram_range("), 1)

    def test_non_parking_path_never_pauses(self):
        """With park_for_reread=False no pause/continue may be issued.

        Asserted against the source of the branch rather than a live socket:
        the whole point is that this path issues no pause at all.
        """
        src = inspect_source(gdl.walk_side)
        marker = "if park_for_reread:"
        self.assertIn(marker, src)
        after = src.split("else:\n            # Read once", 1)[1]
        # The verify branch runs up to the end of the coherence block.
        verify_branch = after.split('meta["coherent"] = coherent', 1)[0]
        self.assertNotIn('conn.cmd("pause")', verify_branch)
        self.assertNotIn('conn.cmd("continue")', verify_branch)

    def test_torn_is_flagged_in_meta(self):
        src = inspect_source(gdl.walk_side)
        self.assertIn('meta["torn"] = True', src)


def inspect_source(fn):
    import inspect
    return inspect.getsource(fn)


if __name__ == "__main__":
    unittest.main()


class ScopedReadTest(unittest.TestCase):
    """A scoped read must not be re-read to 'verify' it.

    Three second-read tests were tried and each rejected every frame in which
    the list changed -- byte identity, list shape, then node count. During an
    animation that is every frame; the last discarded 147 of 147 reads while
    the walked lists plainly contained the quads being looked for.
    """

    SRC = None

    def setUp(self):
        import inspect
        self.SRC = inspect.getsource(gdl.walk_side)

    def test_scoped_span_short_circuits_the_recheck(self):
        self.assertIn("if ram_span and entries:", self.SRC)
        branch = self.SRC.split("if ram_span and entries:", 1)[1]
        branch = branch.split("elif", 1)[0]
        self.assertIn('meta["coherent"] = True', branch)
        self.assertNotIn("read_ram_range(", branch)

    def test_unscoped_path_still_checks(self):
        self.assertIn("elif not meta.get(\"paused\") and entries:", self.SRC)

    def test_coherence_reason_is_recorded(self):
        self.assertIn("scoped-single-read", self.SRC)
