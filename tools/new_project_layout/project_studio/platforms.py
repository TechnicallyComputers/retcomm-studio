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

WHY THE CAPABILITY FIELDS EXIST (2026-09-07, adding Nintendo 64).
With two consoles, every branch could be written ``if key == "snes"`` and the
``else`` meant PSX. Those two spellings are not the same claim, and adding a
third console is what proved it: ``== "snes"`` silently routes N64 into the
PSX arm, so a cartridge would have been asked for a BIOS, looked up in Redump,
and cross-built with psxrecomp's MinGW script — each one failing deep inside
somebody else's tool rather than at the question. So the branches below are
written as capability questions the profile answers. A fourth console then
gets its answers wrong LOUDLY, at one table, instead of inheriting PSX's by
default (PRINCIPLES.md, "Enforce the Rule in the Artifact").
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

ENV_VAR = "RETCOMM_STUDIO_PLATFORM"


@dataclass(frozen=True)
class PlatformProfile:
    key: str  # psx | snes | n64
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

    # --- capabilities ------------------------------------------------------
    # Each one replaces a branch that used to read `key == "snes"`. The name
    # says what is being asked, so the answer for a console added later is a
    # decision someone makes here rather than one they inherit by accident.

    # A firmware image the build has to stage, and the separate emitter build
    # that goes with it. PSX recompiles its own BIOS; a cartridge boots from
    # its own reset vector and there is nothing to stage.
    has_bios: bool = False
    # Redump / libretro lookup, and the multi-disc set that goes with it. Both
    # are keyed on disc identity, which a cartridge does not have.
    has_disc_meta: bool = False
    # Upper bound on an image set. Four covers every PS1 release we know of; a
    # cartridge is one image and two would let them disagree.
    max_images: int = 1
    # What New Project sends for --region when the user left it blank. PSX
    # habitually says USA; the cartridge consoles let the header decide, and
    # sending a default would relabel a Japanese cartridge.
    region_default: str = ""
    # The scaffolder ships a probe that reads identity off the image, so the
    # New Project page can show the defaults `--yes` would silently take.
    has_image_probe: bool = False
    # Netplay (recomp-net + retcomm-rbengine) is wired into the framework.
    has_netplay: bool = True
    # Nested submodules inside the framework checkout that Studio manages.
    nested_paths: tuple[str, ...] = ("lib/recomp-net", "lib/retcomm-rbengine")

    # How image -> C generation is driven. "psxrecomp-cli": Studio calls
    # psxrecomp_cli generate (ROM + BIOS C). "regen-script": the project owns
    # tools/regen.sh. "cmake-target": generation is a target in the project's
    # own CMake graph (n64lle harvests and emits inside the build).
    generate_kind: str = "psxrecomp-cli"
    # The analysis story behind the Functions/Frames tabs and `analyze`.
    # "psxrecomp-analyze": a separate discovery pass Studio runs. "codegen":
    # the analyser runs inside generation and leaves a manifest. "": the
    # framework has none, so the tabs are absent and the CLI refuses.
    analysis_kind: str = "psxrecomp-analyze"
    # Windows cross-build script under scripts/, "" = this console has none.
    mingw_script: str = "build_windows_mingw.sh"
    # Migration/audit implementation module under project_studio, "" = the PSX
    # composite (detect + plan + ops).
    migrate_module: str = ""

    # --- derived -----------------------------------------------------------
    @property
    def is_cartridge(self) -> bool:
        return self.image_kind == "rom"

    @property
    def has_analysis(self) -> bool:
        return bool(self.analysis_kind)


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
    has_bios=True,
    has_disc_meta=True,
    max_images=4,
    region_default="USA",
    has_image_probe=False,
    has_netplay=True,
    generate_kind="psxrecomp-cli",
    analysis_kind="psxrecomp-analyze",
    mingw_script="build_windows_mingw.sh",
    migrate_module="",
)

SNES = PlatformProfile(
    key="snes",
    display="Super Nintendo",
    framework="snesrecomp",
    # The live repo is mstan/snesrecomp — the same owner psxrecomp lives under,
    # and neither moved in the RetroPortingToolKit transfer. The scaffolder bug
    # this comment used to warn about is fixed upstream: snesrecomp's own
    # tools/new_project/setup_project.sh now falls back to mstan/snesrecomp too.
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
    has_bios=False,
    # libretro-database keys SNES metadata (publisher, developer, year) by the
    # ROM's CRC32; discmeta.lookup_rom resolves it, plus the catalog.
    has_disc_meta=True,
    max_images=1,
    region_default="",
    has_image_probe=True,
    has_netplay=True,
    generate_kind="regen-script",
    analysis_kind="codegen",
    mingw_script="build_windows_mingw_snes.sh",
    migrate_module="snesops",
)

N64 = PlatformProfile(
    key="n64",
    display="Nintendo 64",
    framework="n64lle",
    framework_url="https://github.com/RetroPortingToolKit/n64lle.git",
    framework_branch="main",
    repo_index_file="project_studio_repos_n64.json",
    image_kind="rom",
    image_exts=(".z64", ".n64", ".v64"),
    image_label="ROM (.z64)",
    # An n64lle port's executable target is <slug>-runtime, which is NOT the
    # project() name (GloverRecomp -> glover-runtime). So there is no constant
    # AND no project() to read: buildops resolves it from the scaffolder's own
    # rule. See buildops.default_target.
    default_target="",
    about_brand="n64lle",
    about_console="Nintendo 64",
    framework_marker="runtime/runtime.cmake",
    repo_markers=("game.toml",),
    has_bios=False,
    has_disc_meta=False,
    max_images=1,
    region_default="",
    has_image_probe=True,
    # n64lle vendors no recomp-net and no retcomm-rbengine, and its scaffolder
    # adds neither. Claiming netplay here would have Studio offer a --netplay
    # flag the setup script does not have (an unknown option aborts the run)
    # and hunt for nested submodules that are not there.
    has_netplay=False,
    nested_paths=(),
    generate_kind="cmake-target",
    # n64lle's debug server speaks the family protocol but carries only
    # ping/ring_stats/ring_query/help — no fn_stats/fn_query — and a port has
    # no analysis/ bundle or symbols file. So there is no analysis to drive,
    # and saying so is better than an empty tab. (Verified against
    # n64lle runtime/src/debug_server.c at 55f955e.)
    #
    # This governs FUNCTIONS and `analyze`, and both are still correctly
    # absent. It no longer governs DIAGNOSTICS: as of 2026-09-09 that tab
    # exists on N64, driving n64lle's Ares oracle and its differential gates
    # through tools/n64_analysis. The oracle is a different surface from the
    # runtime debug server whose thinness this comment is about — see
    # src/studio/studio_n64.hpp. If a future field gates the Diagnostics tab
    # from here, it must not be this one.
    analysis_kind="",
    # n64lle has no Windows cross-build script of its own yet; psxrecomp's
    # would stage OpenBIOS and drive PSX_NETPLAY on a cartridge.
    mingw_script="",
    migrate_module="n64ops",
)

PROFILES: dict[str, PlatformProfile] = {PSX.key: PSX, SNES.key: SNES, N64.key: N64}
KEYS = tuple(PROFILES)
DEFAULT_KEY = PSX.key


def normalize(key: str | None) -> str:
    k = (key or "").strip().lower()
    if k in ("ps1", "playstation", "psx"):
        return "psx"
    if k in ("snes", "sfc", "supernintendo", "super-nintendo"):
        return "snes"
    if k in ("n64", "nintendo64", "nintendo-64", "n64lle", "ultra64"):
        return "n64"
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


def is_n64() -> bool:
    return current().key == "n64"
