"""Static function discovery for the SNES Functions tab.

The SNES counterpart to ``analyzeops.py``, and deliberately not a port of it:
psxrecomp runs a separate ``psxrecomp-analyze`` binary over a boot executable,
while snesrecomp's analyzer runs as part of ``tools/regen.sh`` and leaves its
result behind as ``src/gen/program_manifest.json``. So there is nothing to
invoke here — the discovery already happened, and this reads what it decided.

Two artifacts, with two different owners:

``src/gen/program_manifest.json``  generated, and never edited. Every node the
    analyzer proved, its disposition (``aot_eligible`` / ``lle_only``), the
    call and dispatch demands it resolved, and for anything it could not prove,
    the reasons why. Regenerated wholesale by ``tools/regen.sh``.

``recomp/symbols.toml``            authored. Names an address as a function and
    sets ``emit`` to promote it into ahead-of-time codegen. This is the human
    triage step: ``PRINCIPLES.md`` requires discovered coverage to be written
    into config by a person rather than applied automatically, precisely so a
    mis-execution cannot launder itself into trusted static code. Studio edits
    this file and nothing else.

Nothing here talks to a running emulator. Every claim the Functions tab shows
was proven from the ROM.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from project_studio.gitops import CmdResult

MANIFEST_REL = Path("src") / "gen" / "program_manifest.json"
SYMBOLS_REL = Path("recomp") / "symbols.toml"


def manifest_path(root: Path | str) -> Path:
    return Path(str(root)).expanduser() / MANIFEST_REL


def symbols_path(root: Path | str) -> Path:
    return Path(str(root)).expanduser() / SYMBOLS_REL


# ---------------------------------------------------------------------------
# symbols.toml
#
# Parsed and rewritten with a line scanner rather than a TOML round-trip. The
# file is authored, commented, and read by people; tomllib cannot write, and
# every writing library reflows the document and drops the comments that
# explain why a function is held at emit = false. Those comments are the most
# valuable thing in the file.
# ---------------------------------------------------------------------------
_TABLE = re.compile(r"^\s*\[\[func\]\]\s*$")
_KEY = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$')


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _norm_pc(text: str) -> str:
    """`8000`, `0x8000`, `00:8000` -> a bare uppercase hex string."""
    t = _unquote(str(text)).strip().replace(":", "").replace("_", "")
    if t.lower().startswith("0x"):
        t = t[2:]
    return t.upper().lstrip("0") or "0"


def read_symbols(root: Path | str) -> list[dict[str, Any]]:
    path = symbols_path(root)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for i, line in enumerate(lines):
        if _TABLE.match(line):
            cur = {"_line": i}
            out.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("[") or (not s and cur.get("addr")):
            # A blank line only ends the table once the entry has content;
            # a stray blank right after [[func]] is not a terminator.
            if s.startswith("["):
                cur = None
            continue
        if s.startswith("#") or "=" not in s:
            continue
        m = _KEY.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if key == "emit":
            cur[key] = _unquote(raw).lower() == "true"
        elif key in ("bank",):
            try:
                cur[key] = int(_unquote(raw), 0)
            except ValueError:
                cur[key] = _unquote(raw)
        else:
            cur[key] = _unquote(raw)
    return [e for e in out if e.get("addr")]


def set_symbol(root: Path | str, pc: str, *, name: str = "",
               emit: bool | None = None, note: str | None = None) -> CmdResult:
    """Name a function, or flip its ``emit``. Rewrites only the lines it owns."""
    path = symbols_path(root)
    if not path.is_file():
        return CmdResult(False, f"no {SYMBOLS_REL} in {root}")
    want = _norm_pc(pc)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return CmdResult(False, f"cannot read {path}: {exc}")

    entries = read_symbols(root)
    target = next((e for e in entries if _norm_pc(str(e.get("addr", ""))) == want), None)

    if target is None:
        if emit is None:
            emit = False
        block = ["", "[[func]]", f'name = "{name or ("func_" + want.lower())}"',
                 f'addr = "{want.lower()}"', f"emit = {'true' if emit else 'false'}"]
        if note:
            block.append(f'note = "{note}"')
        lines.extend(block)
        try:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            return CmdResult(False, f"cannot write {path}: {exc}")
        return CmdResult(True, f"added {want} to {SYMBOLS_REL}")

    # Rewrite in place: walk this entry's lines only, so comments and every
    # other entry survive untouched.
    start = int(target["_line"])
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _TABLE.match(lines[j]) or lines[j].strip().startswith("["):
            end = j
            break
    changed: list[str] = []
    seen_emit = False
    for j in range(start + 1, end):
        m = _KEY.match(lines[j])
        if not m:
            continue
        key = m.group(1)
        if key == "name" and name:
            lines[j] = f'name = "{name}"'
            changed.append("name")
        elif key == "emit" and emit is not None:
            seen_emit = True
            lines[j] = f"emit = {'true' if emit else 'false'}"
            changed.append("emit")
        elif key == "note" and note is not None:
            lines[j] = f'note = "{note}"'
            changed.append("note")
    if emit is not None and not seen_emit:
        lines.insert(end, f"emit = {'true' if emit else 'false'}")
        changed.append("emit")
    if not changed:
        return CmdResult(True, f"{want}: nothing to change")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        return CmdResult(False, f"cannot write {path}: {exc}")
    return CmdResult(True, f"{want}: set {', '.join(changed)}",
                     "run tools/regen.sh to re-emit with this change")


def clear_symbol(root: Path | str, pc: str) -> CmdResult:
    path = symbols_path(root)
    if not path.is_file():
        return CmdResult(False, f"no {SYMBOLS_REL} in {root}")
    want = _norm_pc(pc)
    entries = read_symbols(root)
    target = next((e for e in entries if _norm_pc(str(e.get("addr", ""))) == want), None)
    if target is None:
        return CmdResult(False, f"{want} is not in {SYMBOLS_REL}")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = int(target["_line"])
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _TABLE.match(lines[j]) or lines[j].strip().startswith("["):
            end = j
            break
    # Also take the blank line that separated this entry from the one above,
    # or repeated add/remove cycles leave a growing gap behind and the file
    # drifts even when the symbols come back identical.
    while start > 0 and not lines[start - 1].strip():
        start -= 1
        break
    del lines[start:end]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return CmdResult(True, f"removed {want} from {SYMBOLS_REL}")


# ---------------------------------------------------------------------------
# program_manifest.json
# ---------------------------------------------------------------------------
def _pc_hex(pc: int) -> str:
    return f"{int(pc) & 0xFFFFFF:06X}"


def read_manifest(root: Path | str) -> dict[str, Any]:
    """Nodes, roots and the unproven worklist, flattened for the GUI.

    Returns ``{"present": False, ...}`` rather than raising when the manifest
    is absent: before the first `tools/regen.sh` there is genuinely nothing to
    show, and that is a normal state to render, not an error.
    """
    path = manifest_path(root)
    if not path.is_file():
        return {"present": False, "path": str(path),
                "error": "no src/gen/program_manifest.json — run tools/regen.sh"}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"present": False, "path": str(path), "error": str(exc)}

    named = {_norm_pc(str(e.get("addr", ""))): e for e in read_symbols(root)}
    nodes: list[dict[str, Any]] = []
    for key, n in (doc.get("nodes") or {}).items():
        pc = int(n.get("min_pc24", 0))
        sym = named.get(_norm_pc(_pc_hex(pc)))
        demands = n.get("demands") or []
        nodes.append({
            "key": key,
            "pc": _pc_hex(pc),
            "end": _pc_hex(int(n.get("max_pc24", pc))),
            "bank": (pc >> 16) & 0xFF,
            "disposition": n.get("disposition", ""),
            "instructions": int(n.get("instruction_count", 0) or 0),
            "reasons": list(n.get("reasons") or []),
            "demands": len(demands),
            "unresolved": sum(1 for d in demands
                              if str(d.get("resolution", "")).startswith("unproven")
                              or not d.get("resolution")),
            "name": (sym or {}).get("name", ""),
            "emit": bool((sym or {}).get("emit", False)) if sym else None,
        })
    nodes.sort(key=lambda e: e["pc"])

    roots = [_pc_hex(int(r.get("pc24", 0))) for r in (doc.get("roots") or [])]
    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["disposition"]] = counts.get(n["disposition"], 0) + 1
    return {
        "present": True,
        "path": str(path),
        "format_version": doc.get("format_version"),
        "roots": roots,
        "nodes": nodes,
        "counts": counts,
        # The triage worklist: what the analyzer could not prove, and why. A
        # node here is not a failure to fix by hand — it is a question about
        # discovery, which is where the fix belongs.
        "unproven": [
            {"pc": n["pc"], "name": n["name"], "reasons": n["reasons"]}
            for n in nodes if n["reasons"]
        ],
    }


def status(root: Path | str) -> dict[str, Any]:
    man = read_manifest(root)
    syms = read_symbols(root)
    return {
        "kind": "snes-analysis-status",
        "root": str(Path(str(root)).expanduser()),
        "manifest": man,
        "symbols": [
            {"name": e.get("name", ""), "addr": _norm_pc(str(e.get("addr", ""))),
             "bank": e.get("bank", 0), "emit": bool(e.get("emit", False)),
             "note": e.get("note", "")}
            for e in syms
        ],
        "symbols_path": str(symbols_path(root)),
        "promoted": sum(1 for e in syms if e.get("emit")),
    }
