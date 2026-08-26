"""Static function-discovery operations for the Studio Functions tab.

Wraps ``psxrecomp_cli.py analyze`` (which wraps the ``psxrecomp-analyze``
binary) and owns the ``symbols.toml`` read/write loop. Nothing here touches the
runtime or any capture set: the whole point of the Functions tab is that every
claim it shows was proven from the boot executable alone.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from project_studio.buildops import (
    CmdResult,
    LogFn,
    _run_stream,
    find_psxrecomp_cli,
    resolve_framework_root,
)

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - toolchain is pinned to 3.11+
    tomllib = None  # type: ignore[assignment]


ANALYSIS_DIRNAME = "analysis"


def analysis_dir(root: Path) -> Path:
    return root / ANALYSIS_DIRNAME


def symbols_path(root: Path) -> Path:
    return root / "symbols.toml"


def _game_exe(root: Path) -> str:
    cfg = root / "game.toml"
    if not cfg.is_file() or tomllib is None:
        return ""
    try:
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    return str((data.get("game") or {}).get("exe") or "")


def status(root: Path) -> dict[str, Any]:
    """What the tab needs to render before anything has been run."""
    root = root.expanduser().resolve()
    out = analysis_dir(root)
    exe_rel = _game_exe(root)
    exe = (root / exe_rel) if exe_rel and not Path(exe_rel).is_absolute() else Path(exe_rel or "")
    info: dict[str, Any] = {
        "root": str(root),
        "analysis_dir": str(out),
        "has_analysis": (out / "analysis.json").is_file(),
        "symbols": str(symbols_path(root)),
        "has_symbols": symbols_path(root).is_file(),
        "exe": str(exe) if exe_rel else "",
        "exe_exists": bool(exe_rel) and exe.is_file(),
        "framework": str(resolve_framework_root(root) or ""),
    }
    if info["has_analysis"]:
        try:
            data = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
            info["image"] = data.get("image", "")
            info["stats"] = data.get("stats", {})
        except Exception as exc:  # noqa: BLE001
            info["error"] = f"analysis.json unreadable: {exc}"
    return info


def run_analysis(
    root: Path,
    *,
    exe: str = "",
    exact: bool = False,
    with_refs: bool = False,
    emit_symbols: bool = False,
    min_confidence: str = "high",
    emit_ghidra: bool = False,
    emit_symbol_addrs: bool = False,
    diff: bool = True,
    widescreen: bool = False,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    root = root.expanduser().resolve()
    cli = find_psxrecomp_cli(root)
    if cli is None:
        return CmdResult(False, "psxrecomp_cli.py not found (is psxrecomp checked out?)")
    py = sys.executable or shutil.which("python3") or shutil.which("python")
    if not py:
        return CmdResult(False, "No Python interpreter for analyze")

    cmd = [str(py), str(cli), "analyze", "--project-root", str(root)]
    if exe:
        cmd += ["--exe", exe]
    if exact:
        cmd.append("--exact")
    if not with_refs:
        cmd.append("--no-refs")
    if emit_symbols:
        cmd += ["--emit-symbols", "--min-confidence", min_confidence]
    if emit_ghidra:
        cmd.append("--emit-ghidra")
    if emit_symbol_addrs:
        cmd.append("--emit-symbol-addrs")
    if diff:
        cmd.append("--diff")
    if widescreen:
        cmd.append("--scan-widescreen")

    if dry_run:
        return CmdResult(True, "dry-run: " + " ".join(cmd))
    if log:
        log("Static function discovery (no runtime input)")
    return _run_stream(cmd, root, log=log)


# ---------------------------------------------------------------------------
# symbols.toml
# ---------------------------------------------------------------------------
def read_symbols(root: Path) -> list[dict[str, Any]]:
    path = symbols_path(root)
    if not path.is_file() or tomllib is None:
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for e in data.get("func") or []:
        if "pc" not in e:
            continue
        out.append(
            {
                "pc": int(e["pc"]),
                "name": str(e.get("name") or ""),
                "emit": bool(e.get("emit", False)),
                "status": str(e.get("status") or "guessed"),
                "note": str(e.get("note") or ""),
            }
        )
    return out


def _func_block_re(pc: int) -> re.Pattern[str]:
    """Match the ``[[func]]`` block whose ``pc`` is `pc`, up to the next section."""
    return re.compile(
        r"(^\[\[func\]\][^\[]*?^pc\s*=\s*0x0*%X\s*$[^\[]*)" % pc,
        re.MULTILINE | re.IGNORECASE,
    )


def _set_field(block: str, key: str, value: str) -> str:
    """Replace ``key = ...`` inside one block, appending it if absent."""
    pat = re.compile(r"^%s\s*=.*$" % re.escape(key), re.MULTILINE)
    if pat.search(block):
        return pat.sub("%s = %s" % (key, value), block, count=1)
    return block.rstrip("\n") + "\n%s = %s\n" % (key, value)


def _quote(v: str) -> str:
    return '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"')


def _edit_symbols(root: Path, pc: int, mutate) -> str:
    """Apply `mutate` to one ``[[func]]`` block, rewriting nothing else.

    symbols.toml is hand-authored research, and it carries more than functions:
    Ape Escape has 11 ``[[object]]`` entries and a top-level ``game`` key, Crash
    Team Racing has 33 ``[[site]]`` entries. An earlier version of this rebuilt
    the file from parsed ``[[func]]`` entries alone, which silently deleted all
    of that. Editing the target block in place is the only version that cannot
    lose someone's notes.
    """
    path = symbols_path(root)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    block_re = _func_block_re(pc)
    m = block_re.search(text)
    if m:
        new_block = mutate(m.group(1))
        text = text[: m.start(1)] + new_block + text[m.end(1) :]
    else:
        new_block = mutate(None)
        if new_block:
            if text and not text.endswith("\n"):
                text += "\n"
            text += "\n" + new_block
    path.write_text(text, encoding="utf-8")
    return text


def set_symbol(
    root: Path,
    pc: int,
    name: str,
    *,
    status_value: str = "",
    note: str = "",
    emit: bool | None = None,
) -> CmdResult:
    """Create or update one entry. This is the write half of the naming loop:
    a name typed in the Functions table has to survive the next analysis run,
    and symbols.toml is the only thing that carries it across."""
    root = root.expanduser().resolve()
    name = name.strip()
    if not name:
        return CmdResult(False, "empty name")

    def mutate(block: str | None) -> str:
        if block is None:
            lines = [
                "[[func]]",
                "pc = 0x%08X" % pc,
                "name = %s" % _quote(name),
                "emit = %s" % ("true" if emit else "false"),
                'status = "%s"' % (status_value or "guessed"),
            ]
            if note:
                lines.append("note = %s" % _quote(note))
            return "\n".join(lines) + "\n"
        block = _set_field(block, "name", _quote(name))
        if status_value:
            block = _set_field(block, "status", '"%s"' % status_value)
        if note:
            block = _set_field(block, "note", _quote(note))
        if emit is not None:
            block = _set_field(block, "emit", "true" if emit else "false")
        return block

    _edit_symbols(root, pc, mutate)
    return CmdResult(True, "0x%08X = %s" % (pc, name))


def clear_symbol(root: Path, pc: int) -> CmdResult:
    """Remove exactly one ``[[func]]`` block, leaving the rest of the file alone."""
    root = root.expanduser().resolve()
    path = symbols_path(root)
    if not path.is_file():
        return CmdResult(True, "nothing to clear")
    text = path.read_text(encoding="utf-8")
    m = _func_block_re(pc).search(text)
    if not m:
        return CmdResult(True, "no entry for 0x%08X" % pc)
    text = text[: m.start(1)] + text[m.end(1) :]
    path.write_text(text, encoding="utf-8")
    return CmdResult(True, "cleared 0x%08X" % pc)
