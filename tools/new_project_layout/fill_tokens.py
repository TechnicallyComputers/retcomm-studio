#!/usr/bin/env python3
"""Replace @TOKEN@ placeholders (and CI YOUR_* tokens) in scaffold files."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

def _force_utf8_streams() -> None:
    """Let this tool print the arrows and box glyphs it formats with.

    The Windows console default is cp1252, which cannot encode U+2192 or the
    box-drawing characters used in the tables below, so a single such line
    would abort the tool with UnicodeEncodeError. Studio sets PYTHONUTF8 for
    everything it spawns; a developer running this script straight from a
    terminal gets no such help. Best effort: a stream that is already UTF-8,
    or is not a reconfigurable text wrapper, is left alone.
    """
    for _stream in (sys.stdout, sys.stderr):
        _enc = (getattr(_stream, "encoding", "") or "").lower().replace("-", "")
        if _enc.startswith("utf8"):
            continue
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_streams()

def github_about_description(framework: str = "PSXrecomp",
                             console: str = "Sony PlayStation") -> str:
    """The GitHub About line, for the console the port is actually built on.

    This used to be a single hardcoded PSX string, and BOTH migrations set it —
    so migrating a SNES port stamped "Made with PSXrecomp, a Sony PlayStation
    game static recompiler ecosystem" onto a Super Nintendo repository. The
    README got this right all along (it cites snesrecomp/LICENSE); only the
    About was wrong, which is exactly the field nobody re-reads after the first
    time it is set.
    """
    return (
        f"Made with {framework}, a {console} game static recompiler ecosystem · "
        "Part of the R.A.I.D. community"
    )


# PSX-shaped default: setup_project.sh (the PSX scaffolder) reads this constant
# directly, and that scaffolder only ever builds PlayStation projects.
GITHUB_ABOUT_DESCRIPTION = github_about_description()
GITHUB_ABOUT_HOMEPAGE = "https://discord.gg/Ad9BwSzctP"


def derive_zip_prefix(name: str) -> str:
    base = re.sub(r"(?i)recomp(iled)?$", "", name).strip()
    caps = re.findall(r"[A-Z][a-z0-9]*|[0-9]+", base)
    if len(caps) >= 3:
        acr = "".join(w[0] for w in caps if w and w[0].isalpha()).lower()
        if 3 <= len(acr) <= 8:
            return acr
    slug = re.sub(r"[^a-z0-9]+", "", base.lower())
    return (slug or "game")[:20]


# ---- project path safety -----------------------------------------------------
# Characters that make a filesystem path unusable as a CMake project root.
#
# ':' and ';' are not cosmetic. CMake writes custom-command outputs into
# makefile/ninja dependency rules, where ':' is the rule separator, and it joins
# list arguments with ';'. A root containing either is silently split, and the
# build dies far from the cause: FetchContent's ExternalProject sub-build
# reports "Attempt to add a custom rule to output <path cut at the colon>.rule
# which already has a custom rule" once per step, naming a path nobody typed.
#
# Measured against the pinned toolchain CMake (3.31.12), configuring a
# FetchContent dependency from a source dir whose name contains each character:
#   "plain"           -> OK
#   "with spaces"     -> OK          <- spaces are fine; do not flag them
#   "with: colon"     -> fails in ExternalProject
#   "with; semicolon" -> fails earlier, in compiler detection
#
# The second set is what Windows cannot put in a filename at all. A port whose
# directory carries one cannot be cloned by the players it ships to, however
# well it builds on the host that made it.
PATH_FATAL_CHARS = {
    ":": "separates targets from dependencies in build rules",
    ";": "separates list elements in CMake",
    "\n": "newline",
    "\r": "carriage return",
}
PATH_WINDOWS_ILLEGAL = '*?"<>|'


# A leading Windows drive letter, matched by shape rather than by Path.drive:
# Path("C:/x").drive is "C:" on Windows but "" on Linux, and this check has to
# give the same verdict wherever it runs -- Studio is cross-platform, and a
# guard that fires on one host and not the other is worse than none.
_DRIVE_RE = re.compile(r"^[A-Za-z]:(?=[\\/])")


def _path_body(path) -> str:
    """The path minus any drive letter, so "C:/…" is not read as a colon fault."""
    return _DRIVE_RE.sub("", str(path))


def path_build_problems(path) -> list[str]:
    """Human-readable reasons `path` cannot serve as a project root. Empty = fine."""
    body = _path_body(path)
    out: list[str] = []
    for ch, why in PATH_FATAL_CHARS.items():
        if ch in body:
            shown = {"\n": "\\n", "\r": "\\r"}.get(ch, ch)
            out.append(f"contains '{shown}', which {why} — CMake cannot build from it")
    for ch in PATH_WINDOWS_ILLEGAL:
        if ch in body:
            out.append(f"contains '{ch}', which Windows cannot put in a path — "
                       "players could not clone this repo")
    return out


def safe_path_suggestion(path) -> str:
    """`path` with the hostile characters replaced, for a rename hint."""
    p = Path(path)
    name = p.name
    for ch in PATH_FATAL_CHARS:
        name = name.replace(ch, " -" if ch == ":" else " ")
    for ch in PATH_WINDOWS_ILLEGAL:
        name = name.replace(ch, "")
    name = re.sub(r"\s{2,}", " ", name).strip(" .-") or "project"
    return str(p.with_name(name))


def sanitize_github_name(name: str) -> str:
    """GitHub owner/repo slug: spaces → '-', drop illegal chars, keep [A-Za-z0-9._-]."""
    s = (name or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^A-Za-z0-9._-]+", "", s)
    s = re.sub(r"-{2,}", "-", s)
    s = s.strip(".-")
    return s[:100] or "repo"


def install_dir_name(name: str) -> str:
    """Local checkout / launcher ``apps/`` folder — same slug as the GitHub repo name.

    New-project wizard must create this folder (not the display name with spaces)
    so Studio catalog matching and Retro ``install_dir_name`` stay consistent.
    """
    return sanitize_github_name(name)


def normalize_repo_key(name: str) -> str:
    """Casefold + collapse whitespace/underscores to hyphens for folder/catalog match.

    Lets ``Wipeout 3 Special Edition Recomp`` match catalog
    ``Wipeout-3-Special-Edition-Recomp`` / github short name without renaming.
    """
    s = (name or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9._-]+", "", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip(".-")


def repo_match_keys(name: str) -> set[str]:
    """Match keys for a folder / install_dir / github short name.

    Includes the normalized form and a form with a trailing ``-recomp`` /
    ``-recompiled`` stripped so local ``… Recomp`` checkouts still match
    catalog ``install_dir_name`` / github slugs that omit that suffix
    (e.g. Klonoa).
    """
    k = normalize_repo_key(name)
    if not k:
        return set()
    out = {k}
    stripped = re.sub(r"(-recomp(iled)?)+$", "", k).strip("-")
    if stripped:
        out.add(stripped)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument(
        "--set-file",
        action="append",
        default=[],
        metavar="KEY=PATH",
        help="Read VALUE from a file (for multiline CMake blocks)",
    )
    ap.add_argument(
        "--ci-placeholders",
        action="store_true",
        help="Also replace YOUR_ZIP_PREFIX / YOUR_GAME_TITLE / yourgame-release",
    )
    args = ap.parse_args()

    repl: dict[str, str] = {}
    for item in args.set:
        if "=" not in item:
            print(f"bad --set {item!r} (want KEY=VALUE)", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        repl[k] = v
    for item in args.set_file:
        if "=" not in item:
            print(f"bad --set-file {item!r} (want KEY=PATH)", file=sys.stderr)
            return 2
        k, path = item.split("=", 1)
        repl[k] = Path(path).read_text(encoding="utf-8").rstrip("\n")

    text = Path(args.src).read_text(encoding="utf-8")
    for k, v in repl.items():
        text = text.replace(f"@{k}@", v)

    if args.ci_placeholders:
        zp = repl.get("ZIP_PREFIX", "game")
        title = repl.get("GAME_TITLE") or repl.get("WINDOW_TITLE") or "Game"
        # Collapse runs of whitespace; escape for YAML double-quoted release names
        # (template uses name: "YOUR_GAME_TITLE ${{ … }}").
        title = re.sub(r"\s+", " ", title.strip())
        title_yaml = (
            title.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", " ")
            .replace("\r", "")
        )
        text = text.replace("YOUR_ZIP_PREFIX", zp)
        text = text.replace("YOUR_GAME_TITLE", title_yaml)
        text = text.replace("yourgame-release", f"{zp}-release")

    Path(args.dst).parent.mkdir(parents=True, exist_ok=True)
    Path(args.dst).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
