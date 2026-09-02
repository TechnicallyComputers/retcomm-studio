"""Locate the snesrecomp wizard (templates + probe) Studio should drive.

Order matters, and it is not arbitrary. A migration is measured against the
framework revision the port is actually pinned to, so the project's own
``snesrecomp/`` submodule outranks anything Studio ships. The vendored copy
under the toolkit is a last resort that only exists so a packaged install can
scaffold a *new* project with no checkout on disk.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .paths import toolkit_dir

MARKER = Path("runner") / "runner.cmake"
_WIZARD_REL = Path("tools") / "new_project"


def _is_framework(root: Path) -> bool:
    return (root / MARKER).is_file()


def _env_root() -> Path | None:
    raw = (os.environ.get("SNESRECOMP_ROOT") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return None
    return p if _is_framework(p) else None


def snesrecomp_root(game_root: Path | str | None = None) -> Path | None:
    """A real snesrecomp checkout, or None."""
    env = _env_root()
    if env is not None:
        return env
    if game_root:
        root = Path(str(game_root)).expanduser()
        try:
            root = root.resolve()
        except OSError:
            pass
        for cand in (root / "snesrecomp", root):
            if _is_framework(cand):
                return cand
    # …/retcomm-studio/tools/new_project_layout → …/GitHub/snesrecomp
    base = toolkit_dir()
    for parent in (base.parent.parent, base.parent.parent.parent):
        cand = parent / "snesrecomp"
        if _is_framework(cand):
            return cand.resolve()
    return None


def vendored_wizard_dir() -> Path:
    """The copy shipped with Studio (see snes/VENDOR.md)."""
    return toolkit_dir() / "snes"


def wizard_dir(game_root: Path | str | None = None) -> Path:
    """Directory holding setup_project.sh / probe_rom.py / templates/."""
    root = snesrecomp_root(game_root)
    if root is not None:
        live = root / _WIZARD_REL
        if (live / "setup_project.sh").is_file():
            return live
    return vendored_wizard_dir()


def wizard_source(game_root: Path | str | None = None) -> str:
    """Human-readable provenance for the log — which copy is being driven."""
    d = wizard_dir(game_root)
    return "vendored" if d == vendored_wizard_dir() else f"checkout {d}"


def templates_dir(game_root: Path | str | None = None) -> Path:
    return wizard_dir(game_root) / "templates"


def setup_script(game_root: Path | str | None = None) -> Path:
    return wizard_dir(game_root) / "setup_project.sh"


def probe_rom_script(game_root: Path | str | None = None) -> Path:
    return wizard_dir(game_root) / "probe_rom.py"


# ---------------------------------------------------------------------------
# What the pinned framework can actually do
# ---------------------------------------------------------------------------
# Locating the wizard is not the same question as "can the snesrecomp this port
# is *pinned* to run the script that wizard emits". On a fork those two come
# apart: the wizard falls back to a sibling or the vendored copy while the
# submodule stays at whatever ancient gitlink the fork recorded, and the
# emitted tools/regen.sh then calls subcommands the pinned CLI has never heard
# of. Both halves of that comparison live here so nobody answers it twice.

# regen.sh's one invocation idiom: `"$PYTHON" "$CLI" <cmd> …`. Requiring
# $PYTHON is what separates a call site from prose — the script also says
# `echo "regen.sh: $CLI missing"`, and matching on $CLI alone reads that as a
# subcommand named "missing". If the idiom ever changes this finds no calls and
# reports no gap, which is the right way for a gate to fail.
_CLI_CALL_RE = re.compile(
    r'"?\$\{?PYTHON\}?"?\s+"?\$\{?CLI\}?"?\s+([a-z][a-z0-9-]*)'
)

# The one call regen.sh guards behind --verify. Named rather than inferred: the
# alternative is parsing shell control flow to find out.
VERIFY_ONLY_COMMAND = "verify-rom"


def regen_framework_root(game_root: Path | str) -> Path:
    """The framework ``tools/regen.sh`` will use — regen.sh's own rule.

    Deliberately *not* :func:`snesrecomp_root`: regen.sh honours
    ``$SNESRECOMP_ROOT`` and otherwise takes ``snesrecomp`` relative to the repo
    root it cd's into, with no fallback to a sibling checkout. Checking a
    different framework than the one about to run is worse than not checking.
    """
    root = Path(str(game_root)).expanduser().resolve()
    raw = (os.environ.get("SNESRECOMP_ROOT") or "").strip()
    if not raw:
        return root / "snesrecomp"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (root / p)


def cli_commands(cli: Path) -> set[str] | None:
    """Subcommands the framework CLI offers, asked of the CLI itself.

    Parsed from ``--help`` rather than grepped out of the source: the question
    is what argparse will accept, and argparse is the only thing that knows.
    ``None`` means the question could not be put — a caller may not treat its
    own inability to ask as a finding.
    """
    if not cli.is_file():
        return None
    try:
        proc = subprocess.run(
            [sys.executable, str(cli), "--help"],
            capture_output=True, text=True, timeout=60, cwd=str(cli.parent),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    blob = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"\{([a-z0-9,_-]+)\}", blob)
    if not m:
        return None
    return {c for c in (x.strip() for x in m.group(1).split(",")) if c}


def regen_cli_commands(regen_text: str, *, verify: bool = True) -> list[str]:
    """Subcommands a ``tools/regen.sh`` invokes, in first-seen order.

    Read out of the script rather than assumed, because the script is the thing
    that will run. Prose mentioning a command name does not count — only an
    actual ``$CLI <cmd>`` call site.
    """
    out: list[str] = []
    for m in _CLI_CALL_RE.finditer(regen_text or ""):
        cmd = m.group(1)
        if not verify and cmd == VERIFY_ONLY_COMMAND:
            continue
        if cmd not in out:
            out.append(cmd)
    return out


def regen_framework_gap(
    game_root: Path | str, regen_text: str, *, verify: bool = True
) -> tuple[list[str], set[str]] | None:
    """``(missing commands, what the CLI offers)`` — or None when it fits.

    None is also the answer when the CLI could not be asked; a caller that
    wants to report a missing checkout must check for that itself, since an
    absent framework is a different finding with a different fix.
    """
    cli = regen_framework_root(game_root) / "snesrecomp_cli.py"
    have = cli_commands(cli)
    if have is None:
        return None
    missing = [c for c in regen_cli_commands(regen_text, verify=verify) if c not in have]
    return (missing, have) if missing else None

