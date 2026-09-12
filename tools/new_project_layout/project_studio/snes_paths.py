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
    """Directory holding setup_project.sh / probe_rom.py / templates/.

    With a project, its own submodule (or $SNESRECOMP_ROOT) is the framework
    to measure against. WITHOUT one -- a new project -- only $SNESRECOMP_ROOT
    outranks the vendored copy. The sibling checkout beside retcomm-studio
    used to win here too, and it sat on a months-old branch: a Super Metroid
    scaffold pinned the framework's current main but was rendered from that
    checkout's old templates, built, and did not boot. The wizard now renders
    from the framework it pins regardless, but only a copy of the wizard that
    HAS that fix can do so, and the vendored copy is the one this tree keeps
    current (snes/VENDOR.md).
    """
    if game_root is None or str(game_root) == "":
        env = _env_root()
        if env is not None and (env / _WIZARD_REL / "setup_project.sh").is_file():
            return env / _WIZARD_REL
        return vendored_wizard_dir()
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


def regen_options(regen_text: str) -> set[str]:
    """Command-line options a ``tools/regen.sh`` accepts, from its case arms.

    Studio passes flags to a script the PORT owns, and not every port owns the
    wizard's script. MegaManXSNESRecomp hand-wrote a multi-variant driver: it
    selects a region positionally (``usa`` | ``jp`` | ``all``), stages each
    variant's ROM at a fixed path, and answers ``--rom`` with
    ``unknown argument: --rom``. Reading the arms is how Studio can know that
    before passing one, instead of relaying the script's refusal as a Generate
    failure.

    Comment lines are skipped: the help text above the parser names the same
    flags in prose, and a gate that reads documentation rather than code would
    pass a script whose parser had dropped an option.
    """
    found: set[str] = set()
    for line in (regen_text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        head, sep, _rest = line.partition(")")
        if not sep:
            continue
        for token in head.split("|"):
            token = token.strip()
            # One word only. A shell array line such as
            # `emit_extra=(--profile-manifest recomp/x.json)` also ends in ")",
            # and taking it whole would report an "option" that is really a
            # flag plus its argument — reported back to the user as the
            # script's interface, so accuracy is the point.
            if token.startswith("-") and len(token) > 1 and not token.split()[1:]:
                found.add(token)
    return found


# The wizard's regen.sh interface. Present in every generation of it that
# takes a ROM at all, which is what makes their absence meaningful.
WIZARD_REGEN_FLAGS = ("--rom", "--no-verify", "--cfg-roots")
_HELP_FLAGS = ("-h", "--help")


def regen_is_port_authored(regen_text: str) -> bool:
    """True when this ``tools/regen.sh`` is the port's own, not the wizard's.

    A narrower question than "does this look like the current template", and
    it has to be: a regen.sh from an OLDER wizard generation also fails to look
    current, and re-emitting that one is exactly what Emit tools/regen.sh is
    for. Conflating the two would protect the scripts that most need replacing.

    What marks a port-authored driver is an option parser of its own whose
    vocabulary has nothing to do with the wizard's. MegaManXSNESRecomp's takes
    ``--no-tests`` and ``--strict-idempotent`` and a regional variant
    positional, and none of :data:`WIZARD_REGEN_FLAGS`.

    A script with no option parser at all is deliberately *not* port-authored
    here: that is the shape of the early wizard scripts that baked their
    digests in, and those are Studio's to replace. ``-h``/``--help`` alone does
    not count as a vocabulary either.
    """
    opts = {o for o in regen_options(regen_text) if o not in _HELP_FLAGS}
    if not opts:
        return False
    return not any(flag in opts for flag in WIZARD_REGEN_FLAGS)


# A `case` arm's pattern list: `usa)` / `usa|jp|all)` / `-h|--help)`.
_CASE_ARM_RE = re.compile(r"^\s*\(?\s*([^()#]+?)\s*\)(?!\s*[;{])")
# A framework tool driven directly rather than through snesrecomp_cli.py.
_FW_TOOL_RE = re.compile(r"tools/([A-Za-z0-9_]+)\.py")
# Any long option mentioned in code (not prose).
_LONG_FLAG_RE = re.compile(r"(--[a-z][a-z0-9-]+)")


def _code_lines(text: str) -> list[str]:
    return [ln for ln in (text or "").splitlines() if not ln.strip().startswith("#")]


def regen_positionals(regen_text: str) -> set[str]:
    """Bare-word ``case`` patterns a regen.sh dispatches on — its subcommands.

    A hand-written driver can take a selector this way rather than as a flag
    (MegaManX's regional variants: ``usa`` | ``jp`` | ``all``), so a reader
    that looks only at :func:`regen_options` concludes the script "takes no
    arguments" and is wrong about what it can do.
    """
    found: set[str] = set()
    for line in _code_lines(regen_text):
        m = _CASE_ARM_RE.match(line)
        if not m:
            continue
        for token in m.group(1).split("|"):
            token = token.strip().strip('"')
            if token and token.isidentifier() and not token.startswith("-"):
                found.add(token)
    return found


def regen_capability_delta(
    port_text: str, template_text: str
) -> tuple[list[str], list[str]]:
    """``(blocking, notes)``: capability that would be lost, and mere interface.

    Derived by comparison, never from a list kept here — a hardcoded inventory
    of "things a port might have added" is stale the first time someone adds
    something else, and this gate exists to be trusted when it says nothing
    would be lost.

    The split is load-bearing, and the first version without it was wrong:
    :func:`regen_is_port_authored` only calls a script port-authored when it
    has an option vocabulary of its own, so counting *any* extra option as
    lost capability made every port-authored script permanently un-adoptable —
    the automation refusing every case it existed for. An extra flag on a
    wrapper is an interface difference. What shows the script is doing
    something the framework's cannot is one of the other three: a selector it
    dispatches on, a framework tool it drives directly, or a flag it hands the
    generator.
    """
    blocking: list[str] = []
    notes: list[str] = []

    extra_pos = regen_positionals(port_text) - regen_positionals(template_text)
    if extra_pos:
        blocking.append(
            "selects " + "/".join(sorted(extra_pos)) + " positionally "
            "(the wizard's script regenerates one target and takes no selector)"
        )

    extra_opts = regen_options(port_text) - regen_options(template_text) - set(_HELP_FLAGS)

    port_tools = {m.group(1) for m in _FW_TOOL_RE.finditer("\n".join(_code_lines(port_text)))}
    tpl_tools = {m.group(1) for m in _FW_TOOL_RE.finditer("\n".join(_code_lines(template_text)))}
    extra_tools = port_tools - tpl_tools
    if extra_tools:
        blocking.append(
            "drives the framework's "
            + ", ".join(f"tools/{t}.py" for t in sorted(extra_tools))
            + " directly, which the wizard's script does not call"
        )

    port_flags = {
        m.group(1) for line in _code_lines(port_text)
        for m in _LONG_FLAG_RE.finditer(line)
    }
    tpl_flags = {
        m.group(1) for line in _code_lines(template_text)
        for m in _LONG_FLAG_RE.finditer(line)
    }
    extra_flags = port_flags - tpl_flags - extra_opts - set(_HELP_FLAGS)
    if extra_flags:
        blocking.append(
            "passes " + ", ".join(sorted(extra_flags)) + " to the generator"
        )

    if extra_opts:
        notes.append(
            "the old script also accepted "
            + ", ".join(sorted(extra_opts))
            + " — anything calling it with those needs updating"
        )
    return blocking, notes


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

