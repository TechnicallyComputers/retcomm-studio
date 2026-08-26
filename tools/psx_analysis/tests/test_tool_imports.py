#!/usr/bin/env python3
"""Every global a tool references must actually resolve.

This exists because of a real failure: a patch script used an unasserted
str.replace() to add `find_display_lists` to an import list, the anchor text
had drifted, the replace silently did nothing, and the tool shipped calling a
name it had never imported. Nothing caught it -- the file parsed, the module
imported, the tests passed -- and it only surfaced as a NameError in the GUI
when a user clicked the button.

Import-time checks cannot catch this: a missing global is only an error when
the line executes. So instead of running the code, this walks each function's
BYTECODE for LOAD_GLOBAL and checks the name exists. That reaches every branch,
including the ones a test would never take.
"""

import builtins
import dis
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The GPU/analysis tools this suite owns. Anything importable without side
# effects can be added; the check is generic.
TOOLS = [
    "psx_gpu_frame", "gpu_display_list", "gpu_colour_parity",
    "gpu_frame_scan", "gpu_frame_diff", "ram_parity", "fingerprint_diff",
    "packet_writers", "gte_check", "gpu_parity", "colour_inputs",
    "lockstep_check", "probe_regs", "range_writers", "scale_trace", "class_census",
]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def globals_used(obj, seen=None):
    """Every LOAD_GLOBAL name reachable from a module, functions included.

    Restricted to code defined in THIS module's own file. A module's namespace
    also holds the classes and functions it imported, and those resolve their
    globals in the module that defined them -- following them would report
    psx_gpu_frame's `json` and `socket` as missing from every tool.
    """
    out = set()
    seen = seen if seen is not None else set()
    own = getattr(obj, "__file__", None)
    for code in codes_of(obj, seen):
        if own and code.co_filename != own:
            continue
        for ins in dis.get_instructions(code):
            if ins.opname in ("LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"):
                name = ins.argval
                if isinstance(name, str):
                    out.add(name)
    return out


def codes_of(obj, seen):
    """Code objects for every function/method/class body in a module."""
    stack, out = [], []
    if isinstance(obj, types.ModuleType):
        for v in vars(obj).values():
            stack.append(v)
    else:
        stack.append(obj)
    while stack:
        v = stack.pop()
        code = getattr(v, "__code__", None)
        if code is not None and id(code) not in seen:
            seen.add(id(code))
            out.append(code)
            for const in code.co_consts:
                if isinstance(const, types.CodeType) and id(const) not in seen:
                    seen.add(id(const))
                    out.append(const)
        elif isinstance(v, type):
            for m in vars(v).values():
                if getattr(m, "__code__", None) is not None:
                    stack.append(m)
    return out


class TestNoUndefinedGlobals(unittest.TestCase):
    def test_every_referenced_global_resolves(self):
        for name in TOOLS:
            with self.subTest(tool=name):
                mod = load(name)
                missing = sorted(
                    g for g in globals_used(mod)
                    if g not in vars(mod) and not hasattr(builtins, g)
                )
                self.assertEqual(
                    missing, [],
                    f"{name}.py references undefined global(s) {missing} — "
                    f"most likely an import that was never added")


class TestSharedApiIsReal(unittest.TestCase):
    """Names the tools rely on psx_gpu_frame exporting."""

    REQUIRED = [
        "RAM_SIZE", "OT_END", "MAX_READ_RAM", "DebugConn", "DebugError",
        "read_ram_range", "snapshot_ram", "walk_ordering_table",
        "find_display_lists", "dma_gpu_list_root", "decode_entries",
        "capture", "frame_signature", "signature_delta", "STP_MODES",
    ]

    def test_library_exports_what_the_tools_import(self):
        GF = load("psx_gpu_frame")
        for n in self.REQUIRED:
            with self.subTest(name=n):
                self.assertTrue(hasattr(GF, n), f"psx_gpu_frame lost {n}")

    def test_read_cap_is_the_measured_one(self):
        # 16384 round-trips against the oracle; 32768 stalls and appears to
        # take the emulator with it. Not a guess -- do not raise it.
        GF = load("psx_gpu_frame")
        self.assertEqual(GF.MAX_READ_RAM, 16384)


if __name__ == "__main__":
    unittest.main()
