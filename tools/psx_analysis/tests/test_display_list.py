#!/usr/bin/env python3
"""Tests for the ordering-table walker and the two tools built on it.

The walker reads a linked list out of an untrusted RAM image, which means every
malformed shape it can meet is a hang or a crash waiting to happen: a cycle, a
tag pointing past the end of RAM, a length field claiming more words than the
list can hold. Those cases are the point of this file -- the happy path is the
easy half.

The colour comparison is tested for its verdict boundaries, since a tool that
answers "match" or "differ" is only useful if it draws that line where it says.
"""

import importlib.util
import struct
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
DL = _load("gpu_display_list")
CP = _load("gpu_colour_parity")


def blank_ram(size=GF.RAM_SIZE):
    return bytearray(size)


def put(ram, off, *words):
    for i, w in enumerate(words):
        struct.pack_into("<I", ram, off + 4 * i, w & 0xFFFFFFFF)


def tag(nwords, nxt):
    return ((nwords & 0xFF) << 24) | (nxt & 0xFFFFFF)


def vert(x, y):
    return (y & 0x7FF) << 16 | (x & 0x7FF)


def color(r, g, b):
    return (b << 16) | (g << 8) | r


def polyg3(r, g, b, x, y):
    """A gouraud triangle: 6 payload words after the tag."""
    return [0x30 << 24 | color(r, g, b), vert(x, y),
            color(1, 1, 1), vert(x + 50, y),
            color(2, 2, 2), vert(x, y + 50)]


class TestWalker(unittest.TestCase):
    def test_walks_a_two_node_list_and_stops_at_the_terminator(self):
        ram = blank_ram()
        a, b = 0x1000, 0x2000
        put(ram, a, tag(6, b), *polyg3(200, 80, 40, 10, 10))
        put(ram, b, tag(6, GF.OT_END), *polyg3(90, 90, 90, 20, 20))

        entries = GF.walk_ordering_table(bytes(ram), a)
        self.assertEqual(len(entries), 2)
        prims = GF.decode_entries(entries)
        self.assertEqual([p["op_name"] for p in prims], ["PolyG3", "PolyG3"])
        self.assertEqual(prims[0]["colors"][0], [200, 80, 40])
        self.assertEqual(prims[0]["verts"][0], [10, 10])

    def test_a_cycle_terminates_instead_of_hanging(self):
        # Self-referential and mutually-referential lists both occur in real
        # RAM, usually because the root was not actually an OT node.
        ram = blank_ram()
        put(ram, 0x1000, tag(6, 0x2000), *polyg3(10, 10, 10, 0, 0))
        put(ram, 0x2000, tag(6, 0x1000), *polyg3(20, 20, 20, 0, 0))
        entries = GF.walk_ordering_table(bytes(ram), 0x1000)
        self.assertEqual(len(entries), 2)          # each node visited once

        put(ram, 0x3000, tag(6, 0x3000), *polyg3(30, 30, 30, 0, 0))
        self.assertEqual(len(GF.walk_ordering_table(bytes(ram), 0x3000)), 1)

    def test_next_pointer_past_end_of_ram_ends_the_walk(self):
        ram = blank_ram()
        put(ram, 0x1000, tag(6, 0xFFFFF0), *polyg3(10, 10, 10, 0, 0))
        entries = GF.walk_ordering_table(bytes(ram), 0x1000)
        self.assertEqual(len(entries), 1)

    def test_root_past_end_of_ram_yields_nothing(self):
        self.assertEqual(GF.walk_ordering_table(bytes(blank_ram()),
                                                GF.RAM_SIZE + 0x100), [])

    def test_max_nodes_bounds_a_very_long_list(self):
        ram = blank_ram()
        for i in range(40):
            nxt = 0x1000 + (i + 1) * 0x40 if i < 39 else GF.OT_END
            put(ram, 0x1000 + i * 0x40, tag(6, nxt), *polyg3(i, i, i, 0, 0))
        self.assertEqual(len(GF.walk_ordering_table(bytes(ram), 0x1000)), 40)
        self.assertEqual(
            len(GF.walk_ordering_table(bytes(ram), 0x1000, max_nodes=5)), 5)

    def test_kuseg_and_kseg_addresses_reach_the_same_node(self):
        # Game code hands DMA a physical address, but pointers seen elsewhere
        # are cached (0x8...) or uncached (0xA...) views of the same RAM.
        ram = blank_ram()
        put(ram, 0x1000, tag(6, GF.OT_END), *polyg3(70, 70, 70, 5, 5))
        for root in (0x1000, 0x80001000, 0xA0001000):
            with self.subTest(root=hex(root)):
                got = GF.walk_ordering_table(bytes(ram), root)
                self.assertEqual(len(got), 1, f"root {root:#x} walked nothing")

    def test_entries_are_shaped_like_ring_entries(self):
        # decode_entries() is shared with the GP0 ring path; the walker has to
        # emit the same shape or the two diverge silently.
        ram = blank_ram()
        put(ram, 0x1000, tag(6, GF.OT_END), *polyg3(1, 2, 3, 0, 0))
        e = GF.walk_ordering_table(bytes(ram), 0x1000)[0]
        for key in ("op", "n", "w", "src"):
            self.assertIn(key, e, f"missing {key}")
        self.assertEqual(len(e["w"]), e["n"])


class TestReadChunking(unittest.TestCase):
    """read_ram_range must never ask for more than a peer will answer.

    The oracle advertises a 65536 cap in its handler but cannot actually
    reply that large: the response goes out over a buffered socket that
    silently truncates it, and the read then blocks forever waiting for a
    newline. Measured against a live oracle, 16384 works and 32768 hangs.
    A caller passing the advertised cap must be clamped, not obeyed --
    an oversized read does not fail cleanly, it wedges the emulator.
    """

    class FakeConn:
        def __init__(self, size):
            self.asks = []
            self.size = size

        def cmd(self, name, **kw):
            assert name == "read_ram"
            n = int(kw["len"])
            self.asks.append(n)
            return {"ok": True, "hex": "ab" * n}

    def test_caller_asking_for_too_much_is_clamped(self):
        c = self.FakeConn(1 << 20)
        GF.read_ram_range(c, 0, 65536, chunk=65536)
        self.assertTrue(all(a <= GF.MAX_READ_RAM for a in c.asks),
                        f"asked for {max(c.asks)} bytes in one read")

    def test_snapshot_uses_a_safe_chunk_by_default(self):
        c = self.FakeConn(GF.RAM_SIZE)
        blob = GF.read_ram_range(c, 0, 64 * 1024)
        self.assertEqual(len(blob), 64 * 1024)
        self.assertTrue(all(a <= GF.MAX_READ_RAM for a in c.asks))

    def test_a_short_reply_advances_by_what_arrived(self):
        # A peer is allowed to return less than asked; the walk must not
        # assume it got a full chunk or it will misalign every later read.
        class Short(self.FakeConn):
            def cmd(self, name, **kw):
                n = min(int(kw["len"]), 100)
                self.asks.append(n)
                return {"ok": True, "hex": "cd" * n}
        c = Short(1000)
        self.assertEqual(len(GF.read_ram_range(c, 0, 500)), 500)


class TestFindDisplayLists(unittest.TestCase):
    def _ram_with_list(self, prim_at=0x10D05C, count=64):
        ram = blank_ram()
        for i in range(count):
            off = prim_at + i * 0x24
            nxt = prim_at + (i + 1) * 0x24 if i < count - 1 else GF.OT_END
            put(ram, off, tag(8, nxt),
                (0x3A << 24) | color(199 - i, 40, 20), vert(133, 12),
                color(12, 12, 12), vert(203, 12),
                color(13, 13, 13), vert(133, 119),
                color(14, 14, 14), vert(203, 119))
        return ram

    def test_finds_a_list_and_names_its_head(self):
        ram = self._ram_with_list()
        c = GF.find_display_lists(bytes(ram))
        self.assertTrue(c, "found no chain")
        self.assertEqual(c[0]["root"], 0x10D05C)
        self.assertEqual(c[0]["prims"], 64)

    def test_zeroed_ram_yields_nothing(self):
        # Every zero word looks like a tag with length 0 pointing at 0.
        # If those counted as nodes the scan would report megabytes of
        # phantom lists, so a payload that is not a real GP0 command of
        # exactly the right length must not seed a chain.
        self.assertEqual(GF.find_display_lists(bytes(blank_ram())), [])

    def test_near_prefers_a_chain_covering_that_address(self):
        ram = self._ram_with_list(prim_at=0x10D05C, count=8)
        # A longer, unrelated chain elsewhere.
        for i in range(40):
            off = 0x60000 + i * 0x24
            nxt = 0x60000 + (i + 1) * 0x24 if i < 39 else GF.OT_END
            put(ram, off, tag(8, nxt),
                (0x3A << 24) | color(9, 9, 9), vert(1, 1),
                color(1, 1, 1), vert(2, 2), color(1, 1, 1), vert(3, 3),
                color(1, 1, 1), vert(4, 4))
        plain = GF.find_display_lists(bytes(ram))
        self.assertEqual(plain[0]["root"], 0x60000, "longest should win")
        hinted = GF.find_display_lists(bytes(ram), near=0x10D060)
        self.assertEqual(hinted[0]["root"], 0x10D05C,
                         "a caller-known address must outrank chain length")


class TestMadr(unittest.TestCase):
    def test_terminator_in_madr_is_not_reported_as_an_address(self):
        # A finished linked-list DMA leaves 0xFFFFFF in MADR. Masking that
        # into 2 MB yields 0x1FFFFF, which looks like a valid address and
        # is the end of the list, not its start.
        class C:
            def cmd(self, name, **kw):
                return {"ok": True, "channels": [{"ch": 2, "madr": "0x00FFFFFF"}]}
        self.assertIsNone(GF.dma_gpu_list_root(C()))

    def test_a_real_address_still_comes_back(self):
        class C:
            def cmd(self, name, **kw):
                return {"ok": True, "channels": [{"ch": 2, "madr": "0x8010D05C"}]}
        self.assertEqual(GF.dma_gpu_list_root(C()), 0x10D05C)


class TestDisplayListSummary(unittest.TestCase):
    def _prims(self):
        ram = blank_ram()
        a, b = 0x1000, 0x2000
        semi = [(0x32 << 24) | color(20, 20, 20), vert(0, 0),
                color(21, 21, 21), vert(40, 0),
                color(22, 22, 22), vert(0, 40)]
        put(ram, a, tag(6, b), *semi)
        put(ram, b, tag(6, GF.OT_END), *polyg3(9, 9, 9, 3, 3))
        return GF.decode_entries(GF.walk_ordering_table(bytes(ram), a))

    def test_summary_names_classes_and_survives_a_short_limit(self):
        import io
        buf = io.StringIO()
        DL.summarise(self._prims(), out=buf, limit=1)
        text = buf.getvalue()
        self.assertIn("2 node(s), 2 drawing", text)
        self.assertIn("PolyG3+semi", text)
        self.assertIn("PolyG3", text)

    def test_summary_of_an_empty_list_does_not_crash(self):
        import io
        buf = io.StringIO()
        DL.summarise([], out=buf)
        self.assertIn("0 node(s)", buf.getvalue())


class TestColourParity(unittest.TestCase):
    def test_prim_class_separates_blend_modes(self):
        opaque = {"op_name": "PolyG4", "semi": False, "stp": 0}
        additive = {"op_name": "PolyG4+semi", "semi": True, "stp": 1}
        self.assertNotEqual(CP.prim_class(opaque), CP.prim_class(additive))
        self.assertIn("B+F", CP.prim_class(additive))

    def test_colours_of_filters_by_class_and_skips_non_drawing(self):
        prims = [
            {"kind": "poly", "op_name": "PolyG4", "semi": True, "stp": 1,
             "colors": [[10, 10, 10], [20, 20, 20]]},
            {"kind": "poly", "op_name": "PolyG4", "semi": False, "stp": 0,
             "colors": [[99, 99, 99]]},
            {"kind": "state", "op_name": "DrawMode", "colors": [[77, 77, 77]]},
        ]
        want = CP.prim_class(prims[0])
        got = CP.colours_of(prims, want)
        self.assertEqual(got, [[10, 10, 10], [20, 20, 20]])
        self.assertEqual(len(CP.colours_of(prims, None)), 3)

    def test_describe_reports_peak_over_all_channels(self):
        import io
        buf = io.StringIO()
        stats = CP.describe([[10, 200, 30], [40, 50, 60]], "x", out=buf)
        self.assertEqual(stats["peak"], 200)
        self.assertEqual(stats["vertices"], 2)

    def test_describe_of_nothing_is_none_not_a_crash(self):
        import io
        self.assertIsNone(CP.describe([], "x", out=io.StringIO()))


if __name__ == "__main__":
    unittest.main()


class TestOrderingTableCoverage(unittest.TestCase):
    """The whole frame, not one bucket of it.

    A real ordering table is an ARRAY of link-only entries, each chaining to a
    run of primitives and then on to the next bucket. Skipping link entries when
    building the graph makes every bucket's first primitive look like an
    unpointed head, so the scan reports one bucket and calls it the display
    list. That is not a cosmetic inaccuracy: the bucket it happens to pick can
    omit the exact effect being investigated, and the output looks complete.
    """

    OT = 0x100000
    A = 0x10D000      # bucket 0's primitives -- opaque textured quads
    B = 0x10E000      # bucket 1's primitives -- the additive burst

    def _ram(self, buckets=2, per=6):
        ram = blank_ram()
        starts = [self.A, self.B]
        # Primitive runs. Bucket 1 uses the semi-transparent gouraud quads that
        # stand in for the effect; bucket 0 uses opaque textured quads.
        for b in range(buckets):
            base = starts[b]
            for i in range(per):
                off = base + i * 0x28
                nxt = base + (i + 1) * 0x28 if i < per - 1 else (
                    self.OT + (b + 1) * 4 if b + 1 < buckets else GF.OT_END)
                if b == 0:
                    put(ram, off, tag(8, nxt),
                        (0x2C << 24) | color(64, 64, 64), vert(10, 10), 0,
                        vert(60, 10), 0, vert(10, 60), 0)
                else:
                    put(ram, off, tag(8, nxt),
                        (0x3A << 24) | color(199, 40, 20), vert(0, 0),
                        color(9, 9, 9), vert(40, 0),
                        color(9, 9, 9), vert(0, 40),
                        color(9, 9, 9), vert(40, 40))
        # The OT array: each bucket links into its primitive run.
        for b in range(buckets):
            put(ram, self.OT + b * 4, tag(0, starts[b]))
        return ram

    def test_the_scan_covers_every_bucket_not_just_one(self):
        ram = self._ram()
        cands = GF.find_display_lists(bytes(ram))
        self.assertTrue(cands, "found no chain at all")
        best = cands[0]
        prims = GF.decode_entries(
            GF.walk_ordering_table(bytes(ram), best["root"]))
        ops = {p["op_name"] for p in prims if p["kind"] == "poly"}
        self.assertIn("PolyG4+semi", ops,
                      "the additive bucket was dropped — this is the failure "
                      "that silently hid the effect from the inspector")
        self.assertIn("PolyFT4", ops, "the opaque bucket was dropped")
        self.assertEqual(best["prims"], 12, "expected both buckets' primitives")

    def test_the_best_candidate_is_the_table_head_not_a_bucket(self):
        ram = self._ram()
        best = GF.find_display_lists(bytes(ram))[0]
        self.assertEqual(best["root"], self.OT,
                         "should start at the ordering table, not mid-chain")

    def test_zeroed_ram_still_yields_nothing(self):
        # Admitting link entries to the graph must not make every zero word a
        # node: a zero word reads as length 0 pointing at address 0.
        self.assertEqual(GF.find_display_lists(bytes(blank_ram())), [])

    def test_noise_does_not_manufacture_a_display_list(self):
        import random
        rnd = random.Random(11)
        ram = blank_ram()
        for a in range(0x40000, 0xC0000, 4):
            struct.pack_into("<I", ram, a, rnd.getrandbits(32))
        self.assertEqual(GF.find_display_lists(bytes(ram)), [])


class TestColourVerdict(unittest.TestCase):
    """The verdict must not rest on a statistic that scales with sample count.

    Deciding on `peak` once reported "differ" for two distributions whose
    medians and p90s agreed, purely because one side had drawn three times as
    many samples and had therefore seen more of the tail. A maximum is the
    worst possible choice here; histogram intersection over normalised bins is
    invariant to both sample count and animation phase.
    """

    @staticmethod
    def _stats(values):
        cols = [[v, v, v] for v in values]
        import io
        return CP.describe(cols, "x", out=io.StringIO())

    def test_same_shape_different_sample_count_is_not_a_difference(self):
        base = [3, 5, 8, 13, 21, 40, 80, 144, 200]
        a = self._stats(base * 300)     # many samples -> sees the tail
        b = self._stats(base * 100)     # few samples -> same shape
        v, _ = CP.verdict_of(a, b)
        self.assertEqual(v, "match",
                         "identical shapes must match regardless of sample count")

    def test_genuinely_different_distributions_still_differ(self):
        a = self._stats([200, 210, 220, 230] * 500)   # bright
        b = self._stats([5, 8, 11, 14] * 500)         # dark
        v, why = CP.verdict_of(a, b)
        self.assertEqual(v, "differ", why)

    def test_overlap_is_one_for_identical_and_zero_for_disjoint(self):
        h = CP.histogram([10] * 100)
        self.assertAlmostEqual(CP.overlap(h, h), 1.0, places=6)
        self.assertAlmostEqual(CP.overlap(CP.histogram([5] * 50),
                                          CP.histogram([250] * 50)), 0.0,
                               places=6)

    def test_histogram_is_normalised(self):
        small = CP.histogram([100] * 10)
        big = CP.histogram([100] * 10000)
        self.assertEqual(small, big, "raw counts must not leak into the shape")

    def test_one_empty_side_yields_no_verdict(self):
        a = self._stats([10] * 50)
        v, why = CP.verdict_of(a, None)
        self.assertIsNone(v)
        self.assertIn("sampled nothing", why)


class TestBothSidesReport(unittest.TestCase):
    """The concurrent walk must not overstate what it managed to do."""

    def test_compare_shows_classes_present_on_only_one_side(self):
        import io
        nat = {"classes": [{"key": "PolyG4+semi|B+F", "count": 144},
                           {"key": "PolyFT4|opaque", "count": 584}]}
        orc = {"classes": [{"key": "PolyFT4|opaque", "count": 584}]}
        buf = io.StringIO()
        res = DL.compare(nat, orc, out=buf)
        text = buf.getvalue()
        self.assertIn("only one side", text,
                      "a class missing entirely from one emulator is the "
                      "headline, not a row to scan past")
        row = {c["key"]: c for c in res["classes"]}
        self.assertEqual(row["PolyG4+semi|B+F"]["oracle"], 0)
        self.assertEqual(row["PolyFT4|opaque"]["native"], 584)

    def test_frames_spanned_reports_none_when_unknown(self):
        self.assertIsNone(DL.frames_spanned({}))
        self.assertIsNone(DL.frames_spanned({"frame_before": 5}))
        self.assertEqual(DL.frames_spanned({"frame_before": 5,
                                            "frame_after": 9}), 4)

    def test_a_paused_side_spans_no_frames(self):
        # psx-runtime is parked for its snapshot, so it should not report drift;
        # the oracle is read running and legitimately may.
        meta = {"paused": True, "frame_before": 100, "frame_after": 100}
        self.assertEqual(DL.frames_spanned(meta), 0)


class TestVerdictRefusesLopsidedSamples(unittest.TestCase):
    """An extreme sample imbalance is not a comparison.

    Seen live: the oracle contributed 256 vertices (one frame of 64 quads) while
    psx-runtime contributed 3328 (about thirteen frames). A snapshot of an
    animating effect versus an average over its cycle differ by construction,
    and the tool reported "differ" from exactly that.
    """

    @staticmethod
    def _stats(values):
        import io
        return CP.describe([[v, v, v] for v in values], "x", out=io.StringIO())

    def test_thirteen_to_one_is_refused(self):
        a = self._stats([10, 40, 90, 150] * 832)
        b = self._stats([10, 40, 90, 150] * 64)
        v, why = CP.verdict_of(a, b)
        self.assertIsNone(v, "must refuse, not decide")
        self.assertIn("apart", why)

    def test_a_mild_imbalance_still_decides(self):
        a = self._stats([10, 40, 90, 150] * 300)
        b = self._stats([10, 40, 90, 150] * 200)
        v, _ = CP.verdict_of(a, b)
        self.assertEqual(v, "match")


class TestColourShape(unittest.TestCase):
    """Distinct-colour COUNT is the phase-independent colour measurement.

    Every other colour comparison needs the two emulators on the same frame,
    which nothing short of deterministic replay achieves. But a procedural
    effect reuses a handful of colours across all its primitives, and that
    stays true at every phase: an effect at a different moment has DIFFERENT
    colours, not more of them. So an order-of-magnitude gap in how many
    distinct colours a class carries is evidence about the computation, not
    about when each side was sampled.
    """

    @staticmethod
    def _prims(op, blend, colour_sets):
        return [{"op": op, "blend": blend,
                 "colors": " ".join(str(c) for c in cs)} for cs in colour_sets]

    def test_a_reused_palette_reads_as_structured(self):
        dark, bright = (7, 7, 7), (240, 77, 77)
        p = self._prims("PolyG4+semi", "B+F",
                        [[dark, bright, dark, bright]] * 64)
        d = DL.colour_shape(p, "PolyG4+semi", "B+F")
        self.assertEqual(d["prims"], 64)
        self.assertEqual(d["distinct"], 2)
        self.assertEqual(d["patterns"], 1)

    def test_per_primitive_variation_reads_as_unstructured(self):
        sets = [[(i, 0, 0), (155, 159, i), (i + 1, 0, 0), (150, 159, i)]
                for i in range(64)]
        d = DL.colour_shape(self._prims("PolyG4+semi", "B+F", sets),
                            "PolyG4+semi", "B+F")
        self.assertGreater(d["distinct"], 100)
        self.assertEqual(d["patterns"], 64)

    def test_the_filter_selects_only_the_named_class(self):
        p = (self._prims("PolyG4+semi", "B+F", [[(1, 1, 1)] * 4]) +
             self._prims("PolyFT4", "opaque", [[(9, 9, 9)] * 4]))
        self.assertEqual(DL.colour_shape(p, "PolyG4+semi", "B+F")["prims"], 1)
        self.assertEqual(DL.colour_shape(p, "PolyFT4", "opaque")["prims"], 1)
        self.assertEqual(DL.colour_shape(p)["prims"], 2)

    def test_an_absent_class_on_one_side_reports_nothing_rather_than_a_finding(self):
        import io
        nat = {"prims": self._prims("PolyG4+semi", "B+F", [[(1, 1, 1)] * 4])}
        orc = {"prims": self._prims("PolyFT4", "opaque", [[(9, 9, 9)] * 4])}
        self.assertIsNone(DL.compare_colours(nat, orc, "PolyG4+semi|B+F",
                                             out=io.StringIO()))
