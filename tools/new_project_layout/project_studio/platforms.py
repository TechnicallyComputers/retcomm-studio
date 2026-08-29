"""Which console a Studio session is working on.

Studio started as a PSX-only tool, so "the framework" meant ``psxrecomp`` and
nothing had to say so. Adding SNES made that assumption load-bearing in about
forty places. Rather than thread a platform argument through every call, the
CLI resolves ONE profile per process (from ``--platform``, defaulting to psx)
and the ops modules ask for it here.

Process-global is the right scope precisely because it matches the lifetime of
the decision: one Studio window is looking at one console's repos, and a job
that started as SNES work must not finish as PSX work because some helper
forgot to pass a flag.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

ENV_VAR = "RETCOMM_STUDIO_PLATFORM"


@dataclass(frozen=True)
class PlatformProfile:
    key: str  # psx | snes
    display: str  # human name shown in the GUI
    framework: str  # submodule / checkout directory name
    framework_url: str
    framework_branch: str  # default branch of the framework repo
    repo_index_file: str  # per-platform index under the toolkit
    image_kind: str  # disc | rom
    image_exts: tuple[str, ...]  # what the file picker / probe accepts
    image_label: str  # UI label for the game image
    default_target: str  # CMake target, "" = derive from project()
    # Relative file that proves a directory really is the framework checkout
    # (rather than an empty submodule slot with the right name).
    # Brand spelling for prose aimed at people (the GitHub About line), as
    # distinct from `framework`, which is the checkout/submodule directory.
    about_brand: str = ""
    # Console name as it reads in prose. Distinct from `display`, which is the
    # short label the GUI shows: the About line says "Sony PlayStation" where
    # the platform picker says "PlayStation".
    about_console: str = ""
    framework_marker: str = ""
    repo_markers: tuple[str, ...] = field(default_factory=tuple)


PSX = PlatformProfile(
    key="psx",
    display="PlayStation",
    framework="psxrecomp",
    framework_url="https://github.com/mstan/psxrecomp.git",
    framework_branch="master",
    repo_index_file="project_studio_repos.json",
    image_kind="disc",
    image_exts=(".cue",),
    image_label="Disc .cue",
    default_target="psx-runtime",
    about_brand="PSXrecomp",
    about_console="Sony PlayStation",
    framework_marker="runtime/runtime.cmake",
    repo_markers=("game.toml",),
)

SNES = PlatformProfile(
    key="snes",
    display="Super Nintendo",
    framework="snesrecomp",
    # The live repo is mstan/snesrecomp — the same owner psxrecomp lives under.
    # snesrecomp's own setup_project.sh still defaults to a TechnicallyComputers
    # URL; that is the scaffolder's bug, not a second upstream, and Studio does
    # not propagate it.
    framework_url="https://github.com/mstan/snesrecomp.git",
    framework_branch="main",
    repo_index_file="project_studio_repos_snes.json",
    image_kind="rom",
    image_exts=(".sfc", ".smc"),
    image_label="ROM (.sfc)",
    default_target="",  # SNES projects name the target after the project
    about_brand="SNESrecomp",
    about_console="Super Nintendo",
    framework_marker="runner/runner.cmake",
    repo_markers=("recomp",),
)

PROFILES: dict[str, PlatformProfile] = {PSX.key: PSX, SNES.key: SNES}
KEYS = tuple(PROFILES)
DEFAULT_KEY = PSX.key


def normalize(key: str | None) -> str:
    k = (key or "").strip().lower()
    if k in ("ps1", "playstation", "psx"):
        return "psx"
    if k in ("snes", "sfc", "supernintendo", "super-nintendo"):
        return "snes"
    return DEFAULT_KEY


def get(key: str | None) -> PlatformProfile:
    return PROFILES[normalize(key)]


def current() -> PlatformProfile:
    """The profile for this process (set once by the CLI)."""
    return get(os.environ.get(ENV_VAR))


def set_current(key: str | None) -> PlatformProfile:
    profile = get(key)
    os.environ[ENV_VAR] = profile.key
    return profile


def is_snes() -> bool:
    return current().key == "snes"
