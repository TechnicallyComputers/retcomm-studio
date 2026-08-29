#!/usr/bin/env python3
"""Generate `oracle_caps.json` — what each oracle's debug server can answer.

Studio greys out a Frames tool the selected oracle cannot serve. That decision
has to come from the servers' own dispatch tables, not from a list someone
maintains by hand: the two tables are 41 and 61 commands today, they are edited
in two different repos, and a hand-written copy is wrong the first time either
gains a command. `PRINCIPLES.md` calls this out directly — one protocol document
described 47 commands while the servers registered 292, and the fix was to
generate the index and fail CI on drift.

So: generate, commit the result, and run `--check` in CI.

    python3 gen_oracle_caps.py                 # write oracle_caps.json
    python3 gen_oracle_caps.py --check         # exit 1 if it has drifted

Sources
-------
DuckStation  duckstation/psxrecomp_oracle.patch, beside this file. Studio owns
             it, so this half is always checkable.
Beetle       runtime/src/beetle_debug_server.c in a psxrecomp checkout, found
             via --psxrecomp, $PSXRECOMP_ROOT, or a sibling checkout. Beetle's
             engine belongs to psxrecomp and is not vendored here, so when no
             checkout is reachable this half is left as it stands and reported
             as unchecked. Silently rewriting it to empty would tell Studio the
             oracle can answer nothing, and every trace button would go grey.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "oracle_caps.json"

# `if (std::strcmp(cmd, "ping") == 0 || ...)` in psxrecomp_debug_server.cpp,
# which the oracle patch adds to DuckStation wholesale.
_DS_CMD = re.compile(r'std::strcmp\(cmd,\s*"([a-z_0-9]+)"\)')

# The `static const CmdEntry CMDS[] = { { "ping", h_ping }, ... }` table.
_BEETLE_TABLE = re.compile(
    r"CmdEntry\s+CMDS\s*\[\s*\]\s*=\s*\{(.*?)\{\s*NULL\s*,\s*NULL\s*\}", re.S
)
_BEETLE_CMD = re.compile(r'\{\s*"([a-z_0-9]+)"\s*,')

# psx-runtime's own table, `static const CmdEntry s_commands[] = {...};`.
# Needed as an EXCLUSION set, not as a third oracle -- see native_commands().
_NATIVE_TABLE = re.compile(
    r"static const CmdEntry s_commands\s*\[\s*\]\s*=\s*\{(.*?)^\};", re.S | re.M
)


def duckstation_commands() -> tuple[list[str], str]:
    patch = HERE / "duckstation" / "psxrecomp_oracle.patch"
    if not patch.is_file():
        return [], ""
    text = patch.read_text(encoding="utf-8", errors="replace")
    return sorted(set(_DS_CMD.findall(text))), str(patch.relative_to(HERE))


def find_psxrecomp(explicit: str | None) -> Path | None:
    """A psxrecomp checkout holding beetle_debug_server.c, or None."""
    marker = Path("runtime") / "src" / "beetle_debug_server.c"
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    env = (os.environ.get("PSXRECOMP_ROOT") or "").strip()
    if env:
        cands.append(Path(env).expanduser())
    # …/retcomm-studio/tools/psx_analysis -> …/GitHub/psxrecomp
    for up in (HERE.parent.parent, HERE.parent.parent.parent):
        cands.append(up.parent / "psxrecomp")
    for c in cands:
        try:
            c = c.resolve()
        except OSError:
            continue
        if (c / marker).is_file():
            return c
    return None


def beetle_commands(root: Path | None) -> tuple[list[str], str]:
    if root is None:
        return [], ""
    text, src = _read_pinned(root, "runtime/src/beetle_debug_server.c")
    if not text:
        return [], ""
    m = _BEETLE_TABLE.search(text)
    if not m:
        raise SystemExit(
            f"gen_oracle_caps: no CmdEntry CMDS[] table in {src}.\n"
            "  The dispatch table moved or changed shape. Fix this parser rather\n"
            "  than hand-writing the list it produces."
        )
    return sorted(set(_BEETLE_CMD.findall(m.group(1)))), src


def _read_pinned(root: Path, rel: str) -> tuple[str, str]:
    """File contents from a FIXED ref, falling back to the working tree.

    The psxrecomp checkout is shared, and other sessions switch branches in it
    while this runs — which silently changed the native command count and made
    `--check` fail for reasons that had nothing to do with this repo. Reading a
    pinned ref makes the generated table reproducible no matter what anyone has
    checked out.
    """
    for ref in ("origin/master", "master"):
        r = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{rel}"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout:
            return r.stdout, f"{rel}@{ref}"
    path = root / rel
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace"), f"{rel}@working-tree"
    return "", ""


def native_commands(root: Path | None) -> tuple[list[str], str]:
    """psx-runtime's debug commands.

    Not an oracle: it is the code under test. It is here because it is what
    makes the per-tool derivation SOUND. `pause` / `step` / `pc_break` are
    registered by psx-runtime AND by DuckStation, so a tool issuing them says
    nothing about which oracle it uses -- and reading it as "needs DuckStation"
    pins range_writers.py, a wtrace tool that exists to be pointed at Beetle,
    to the one oracle that cannot serve it. Only a command that exactly one
    ORACLE has and the runtime does NOT have can identify the connection it
    was sent on.
    """
    if root is None:
        return [], ""
    text, src = _read_pinned(root, "runtime/src/debug_server.c")
    if not text:
        return [], ""
    m = _NATIVE_TABLE.search(text)
    if not m:
        return [], ""
    return sorted(set(_BEETLE_CMD.findall(m.group(1)))), src


# What each oracle is FOR, in one line, shown beside the selector. Editorial,
# so it stays here rather than being derived from a command list.
BLURB = {
    "duckstation": "Frames and memory: VRAM, GPU/DMA/IRQ state, breakpoints, "
                   "and the pause/step park the display-list tools need.",
    "beetle": "Traces: write, read, function, SIO, CD-command and device "
              "traces, plus SPU/audio rings. No pause and no VRAM readback.",
}
def _pin_port(rel: str, fallback: int) -> int:
    """The port the MANAGER actually uses, not a source-file default.

    beetle_main.cpp defaults to 4382 and its debug_server module falls back to
    4380; beetle_oracle.py starts it on whatever beetle/pin.json says, and that
    is the number a running oracle is listening on. Reading the source instead
    of the pin is how you end up pinging a port nothing is bound to and calling
    a healthy oracle down.
    """
    try:
        return int(json.loads((HERE / rel).read_text(encoding="utf-8"))["oracle_port"])
    except (OSError, ValueError, KeyError, TypeError):
        return fallback


PORT = {
    "duckstation": _pin_port("duckstation/pin.json", 4371),
    "beetle": _pin_port("beetle/pin.json", 4380),
}


# Commands a tool sends over its debug connection.
_TOOL_CMD = re.compile(r'\.cmd\(\s*"([a-z_0-9]+)"')
# A tool that takes --ds-port has a SECOND side, and that second side is the
# oracle. A tool with only --port talks to psx-runtime and the oracle selector
# does not concern it -- which is most of them, and worth knowing before
# greying anything out.
_HAS_ORACLE_PORT = re.compile(r'"--(?:ds-port|oracle-port)"')


def tool_requirements(ds_cmds: list[str], bee_cmds: list[str],
                      native_cmds: list[str]) -> dict:
    """Per-tool: does it use an oracle, and which commands pin it to one?

    Deliberately NOT a guess at which connection each .cmd() goes to. 99 of the
    112 call sites in this directory name their receiver `conn`, so nothing in
    the text says whether a given command is aimed at the runtime or at the
    oracle. What IS decidable is narrower and enough: a command only one oracle
    registers cannot be served by the other, so a tool that issues it and has
    an oracle side is pinned to that oracle. Everything else stays unpinned
    rather than being assigned on a hunch.
    """
    ds, bee, native = set(ds_cmds), set(bee_cmds), set(native_cmds)
    # "only this oracle, and not the runtime either" -- the only class of
    # command that identifies which connection carried it.
    ds_only, bee_only = ds - bee - native, bee - ds - native
    out: dict[str, dict] = {}
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith("gen_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        entry = {
            "oracle_facing": bool(_HAS_ORACLE_PORT.search(text)),
            "needs_duckstation": [],
            "needs_beetle": [],
        }
        cmds = set(_TOOL_CMD.findall(text))
        entry["needs_duckstation"] = sorted(cmds & ds_only)
        entry["needs_beetle"] = sorted(cmds & bee_only)
        if entry["oracle_facing"] or entry["needs_duckstation"] or entry["needs_beetle"]:
            out[path.name] = entry
    return out


def build(psxrecomp: str | None) -> tuple[dict, list[str]]:
    notes: list[str] = []
    ds_cmds, ds_src = duckstation_commands()
    if not ds_cmds:
        raise SystemExit(
            "gen_oracle_caps: duckstation/psxrecomp_oracle.patch is missing or "
            "has no commands — refusing to write a table that says the oracle "
            "can answer nothing."
        )

    root = find_psxrecomp(psxrecomp)
    bee_cmds, bee_src = beetle_commands(root)
    nat_cmds, nat_src = native_commands(root)
    prior = {}
    if OUT.is_file():
        try:
            prior = json.loads(OUT.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior = {}
    if not bee_cmds:
        keep = (prior.get("oracles", {}).get("beetle") or {}).get("commands") or []
        if not keep:
            raise SystemExit(
                "gen_oracle_caps: no psxrecomp checkout with "
                "runtime/src/beetle_debug_server.c, and no previous beetle table "
                "to keep.\n"
                "  Pass --psxrecomp <path> or set PSXRECOMP_ROOT. Writing an "
                "empty table would grey out every trace button and look like "
                "Beetle answering nothing."
            )
        bee_cmds = keep
        bee_src = (prior.get("oracles", {}).get("beetle") or {}).get("source", "")
        notes.append("beetle: kept the existing table (no psxrecomp checkout found)")
    if not nat_cmds:
        nat_src = prior.get("native_source", "")
        notes.append(
            "native: no runtime table available — tools using only commands "
            "psx-runtime shares with an oracle will read as pinned to it")

    doc = {
        "kind": "psx-oracle-capabilities",
        "version": 1,
        "_comment": [
            "GENERATED by gen_oracle_caps.py — do not hand-edit.",
            "Studio greys out a Frames tool whose commands the selected oracle",
            "does not register. Run gen_oracle_caps.py --check to catch drift.",
        ],
        "oracles": {
            "duckstation": {
                "display": "DuckStation",
                "port": PORT["duckstation"],
                "blurb": BLURB["duckstation"],
                "source": ds_src,
                "commands": ds_cmds,
            },
            "beetle": {
                "display": "Beetle PSX",
                "port": PORT["beetle"],
                "blurb": BLURB["beetle"],
                "source": bee_src,
                "commands": bee_cmds,
            },
        },
        "native_commands": len(nat_cmds),
        "native_source": nat_src,
        "tools": tool_requirements(ds_cmds, bee_cmds, nat_cmds),
    }
    return doc, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--psxrecomp", default=None,
                    help="checkout holding runtime/src/beetle_debug_server.c")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if oracle_caps.json is out of date")
    args = ap.parse_args()

    doc, notes = build(args.psxrecomp)
    for n in notes:
        print(f"note: {n}", file=sys.stderr)
    text = json.dumps(doc, indent=2) + "\n"

    if args.check:
        if not OUT.is_file():
            print(f"error: {OUT.name} is missing — run gen_oracle_caps.py",
                  file=sys.stderr)
            return 1
        if OUT.read_text(encoding="utf-8") != text:
            print(f"error: {OUT.name} is out of date — run gen_oracle_caps.py",
                  file=sys.stderr)
            return 1
        ds = len(doc["oracles"]["duckstation"]["commands"])
        bee = len(doc["oracles"]["beetle"]["commands"])
        print(f"{OUT.name} is current (duckstation {ds}, beetle {bee})")
        return 0

    OUT.write_text(text, encoding="utf-8")
    for key, o in doc["oracles"].items():
        print(f"{key}: {len(o['commands'])} commands  <- {o['source'] or '(kept)'}")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
