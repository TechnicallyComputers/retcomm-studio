#!/usr/bin/env python3
"""The MIPS decoder used to read colour-writing code.

Only a store and the arithmetic feeding it need to be legible, but those have
to be RIGHT: a mis-decoded store operand sends the reader after the wrong
register, and there is nothing in the output to suggest it. These pin the forms
that actually appear around a packet write.
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


def enc_i(op, rs, rt, imm):
    return (op << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)


def enc_r(fn, rs, rt, rd, sh=0):
    return (rs << 21) | (rt << 16) | (rd << 11) | (sh << 6) | fn


class TestDisasm(unittest.TestCase):
    def d(self, w, pc=0x80068000):
        return PW.disasm_one(w, pc)

    def test_store_word_operand_order(self):
        # sw $t0, 4($a1)  -- rt is the SOURCE, rs the base. Swapping them reads
        # as a completely different instruction and still looks sensible.
        self.assertEqual(self.d(enc_i(0x2B, 5, 8, 4)), "sw $t0,4($a1)")

    def test_negative_store_offset_is_signed(self):
        self.assertEqual(self.d(enc_i(0x2B, 29, 31, -8 & 0xFFFF)),
                         "sw $ra,-8($sp)")

    def test_load_word(self):
        self.assertEqual(self.d(enc_i(0x23, 4, 2, 0x10)), "lw $v0,16($a0)")

    def test_byte_and_half_stores_are_distinguished(self):
        self.assertEqual(self.d(enc_i(0x28, 5, 8, 0)), "sb $t0,0($a1)")
        self.assertEqual(self.d(enc_i(0x29, 5, 8, 0)), "sh $t0,0($a1)")

    def test_shift_uses_shamt_not_rs(self):
        # sll $t0,$t1,3 -- the shift amount lives in shamt; printing rs here
        # is a classic decoder bug and yields plausible nonsense.
        self.assertEqual(self.d(enc_r(0x00, 0, 9, 8, 3)), "sll $t0,$t1,3")

    def test_three_operand_alu(self):
        self.assertEqual(self.d(enc_r(0x21, 4, 5, 2)), "addu $v0,$a0,$a1")
        self.assertEqual(self.d(enc_r(0x25, 4, 5, 2)), "or $v0,$a0,$a1")

    def test_andi_masks_are_readable(self):
        self.assertEqual(self.d(enc_i(0x0C, 2, 3, 0x00FF)), "andi $v1,$v0,255")

    def test_lui(self):
        self.assertEqual(self.d(enc_i(0x0F, 0, 8, 0x8006)), "lui $t0,0x8006")

    def test_nop_is_not_sll_zero(self):
        self.assertEqual(self.d(0), "nop")

    def test_gte_ops_are_not_mistaken_for_alu(self):
        # cop2 covers two different things: register moves (bit 25 clear) and
        # GTE commands (bit 25 set). Decoding either as ordinary arithmetic
        # would hide the operations this investigation exists to rule in or out.
        move = 0x12 << 26 | 0x0180001            # bit 25 clear -> mfc2
        self.assertTrue(self.d(move).startswith("mfc2"), self.d(move))
        cmd = 0x4B400006                          # bit 25 set -> NCLIP
        self.assertTrue(self.d(cmd).startswith("NCLIP"), self.d(cmd))

    def test_branch_target_is_pc_relative(self):
        w = enc_i(5, 4, 5, 2)          # bne $a0,$a1,+2
        self.assertIn("0x80068010", self.d(w, 0x80068004))


class TestDisasmAround(unittest.TestCase):
    class FakeConn:
        def __init__(self, words):
            self.words = words

        def cmd(self, name, **kw):
            addr = int(kw["addr"], 16) & 0x1FFFFFFF
            n = int(kw["len"])
            blob = b"".join(
                self.words.get((addr + i) & 0x1FFFFFFF, 0).to_bytes(4, "little")
                for i in range(0, n, 4))
            return {"ok": True, "hex": blob.hex()}

    def test_marks_the_target_instruction(self):
        pc = 0x80068100
        words = {(pc & 0x1FFFFFFF) + i * 4: enc_i(0x2B, 5, 8, i)
                 for i in range(-4, 8)}
        rows = PW.disasm_around(self.FakeConn(words), pc, before=4, count=8)
        marked = [r for r in rows if r["is_target"]]
        self.assertEqual(len(marked), 1, "exactly one row is the target")
        self.assertEqual(marked[0]["pc"], f"0x{pc:08X}")

    def test_includes_context_before_the_target(self):
        pc = 0x80068100
        words = {(pc & 0x1FFFFFFF) + i * 4: 0 for i in range(-6, 6)}
        rows = PW.disasm_around(self.FakeConn(words), pc, before=6, count=12)
        idx = [i for i, r in enumerate(rows) if r["is_target"]][0]
        self.assertEqual(idx, 6,
                         "the store must not be the first line — the value it "
                         "writes is computed above it")


if __name__ == "__main__":
    unittest.main()


class TestUnalignedAndCop2(unittest.TestCase):
    """The instructions the colour path is actually built from.

    Legend of Mana loads each vertex RGB with an LWL/LWR pair, stages it with
    SWL/SWR, then scales it. Those four printed as "op22"/"op26"/"op2a"/"op2e"
    — an instruction the reader cannot weigh, in the exact window under
    investigation. GTE ops printed as a bare cop2 word had the same problem:
    the whole question was whether the GTE was involved.
    """

    def d(self, w, pc=0x80068000):
        return PW.disasm_one(w, pc)

    def test_unaligned_loads_and_stores_are_named(self):
        # Real words lifted from 0x80068450..0x80068460 in the trace.
        self.assertEqual(self.d(0x8A8EFFF7), "lwl $t6,-9($s4)")
        self.assertEqual(self.d(0x9A8EFFF4), "lwr $t6,-12($s4)")
        self.assertEqual(self.d(0xABAE0043), "swl $t6,67($sp)")
        self.assertEqual(self.d(0xBBAE0040), "swr $t6,64($sp)")

    def test_the_canonical_pair_addresses_one_word(self):
        # lwl at A+3 and lwr at A load the unaligned word at A. If the operand
        # decode were off by one the two would look like unrelated accesses.
        lwl, lwr = self.d(0x8A8EFFF7), self.d(0x9A8EFFF4)
        self.assertIn("-9(", lwl)
        self.assertIn("-12(", lwr)

    def test_gte_commands_are_named_not_shown_as_raw_words(self):
        self.assertTrue(self.d(0x4B400006).startswith("NCLIP"))

    def test_gte_shift_and_limit_flags_are_shown(self):
        # sf and lm change the result of every GTE colour op; a listing that
        # hides them cannot be checked against the hardware description.
        t = self.d(0x4B400006)
        self.assertIn("sf=", t)
        self.assertIn("lm=", t)

    def test_cop2_loads_and_stores_name_a_gte_register(self):
        # swc2 $c2r12,0($t0) — rt indexes a GTE register, not a GPR. Printing
        # "$t4" there would send the reader after the wrong thing entirely.
        self.assertEqual(self.d(0xE90C0000), "swc2 $c2r12,0($t0)")

    def test_cop2_moves_are_distinguished_from_commands(self):
        mtc2 = (0x12 << 26) | (0x04 << 21) | (8 << 16) | (9 << 11)
        self.assertEqual(self.d(mtc2), "mtc2 $t0,$c2r9")
