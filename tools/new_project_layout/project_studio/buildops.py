"""Local CMake configure / build / launch for game repos.

Cross-platform (Windows / macOS / Linux). No force flags; builds stay under
the chosen build directory (default ``build-release``).

Before configure, missing OpenBIOS generated C under ``psxrecomp/generated/``
is regenerated (MIT OpenBIOS — no retail dump required) so runtime.cmake can
link a BIOS backend.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import platforms
from .gitops import CmdResult

DEFAULT_BUILD_DIR = "build-release"
DEFAULT_TARGET = "psx-runtime"
DEFAULT_BUILD_TYPE = "Release"
OPENBIOS_PROFILE = "bios/OpenBIOS.toml"
OPENBIOS_STEM = "OpenBIOS"
SCPH1001_PROFILE = "bios/SCPH1001.toml"
SCPH1001_STEM = "SCPH1001"
LogFn = Callable[[str], None]


@dataclass
class BuildHost:
    system: str  # Windows | Darwin | Linux | …
    label: str  # windows | macos | linux | other
    cmake: str | None
    ninja: str | None
    jobs: int


@dataclass
class LaunchHandle:
    proc: subprocess.Popen
    exe: Path
    cwd: Path
    env_overlay: dict[str, str] = field(default_factory=dict)

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def poll(self) -> int | None:
        return self.proc.poll() if self.proc else None

    def terminate(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


_active_launch: LaunchHandle | None = None
_launch_lock = threading.Lock()


def _launch_pid_path() -> Path:
    """Cross-process launch PID file (Studio Stop vs blocked ``build run``)."""
    try:
        from .retcomm_paths import default_paths

        base = default_paths().cache_dir
    except Exception:
        base = Path.home() / ".local" / "share" / "retcomm" / "cache"
    base.mkdir(parents=True, exist_ok=True)
    return base / "studio-launch.pid"


def _write_launch_pid(pid: int, *, exe: Path, root: Path) -> None:
    path = _launch_pid_path()
    path.write_text(
        f"pid={pid}\nexe={exe}\nroot={root}\n",
        encoding="utf-8",
    )


def _read_launch_pid() -> tuple[int, str, str] | None:
    path = _launch_pid_path()
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pid = 0
    exe = ""
    root = ""
    for line in text.splitlines():
        if line.startswith("pid="):
            try:
                pid = int(line[4:].strip())
            except ValueError:
                pid = 0
        elif line.startswith("exe="):
            exe = line[4:].strip()
        elif line.startswith("root="):
            root = line[5:].strip()
    if pid <= 0:
        return None
    return pid, exe, root


def _clear_launch_pid() -> None:
    try:
        _launch_pid_path().unlink(missing_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _kill_launch_pid(pid: int) -> None:
    """Terminate a launched game (process group when possible)."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        # Best-effort tree kill; fall back to terminate.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
            return
        except OSError:
            pass
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        return
    try:
        os.killpg(pid, 15)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    # Brief wait then escalate.
    for _ in range(20):
        if not _pid_alive(pid):
            return
        threading.Event().wait(0.1)
    try:
        os.killpg(pid, 9)
    except OSError:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _flush_log(log: LogFn | None, msg: str) -> None:
    if not log:
        return
    try:
        log(msg)
    except TypeError:
        # Plain print works; callers should prefer flush wrappers.
        print(msg, flush=True)


def detect_host() -> BuildHost:
    system = platform.system()
    if system == "Windows":
        label = "windows"
    elif system == "Darwin":
        label = "macos"
    elif system == "Linux":
        label = "linux"
    else:
        label = "other"
    jobs = os.cpu_count() or 4
    return BuildHost(
        system=system,
        label=label,
        cmake=shutil.which("cmake"),
        ninja=shutil.which("ninja") or shutil.which("ninja-build"),
        jobs=jobs,
    )


# --- retcomm toolchain packs -------------------------------------------------
#
# The cmake-clang-v1 pack ships its own clang, its own sysroot, and its own
# pinned dependencies (SDL3, zlib) under ``<pack>/deps``. The pack's clang.cfg
# passes ``--sysroot=<pack>/sysroot``, so the host distribution's /usr/include
# is NOT on the compiler's search path. A find_package() that resolves to the
# host's SDL3 therefore configures cleanly and then fails every compile with
# "'SDL3/SDL.h' file not found" — the header is real, the compiler just cannot
# see it. The pack's env.sh exports SDL3_DIR / ZLIB_ROOT to prevent that, but
# Studio invokes cmake directly and cannot assume env.sh was sourced, so it
# supplies the same contract itself.

_TOOLCHAIN_MARKER = "retcomm-toolchain.json"


def _toolchain_root_from(path: str | Path | None) -> Path | None:
    """Walk up from a file inside a toolchain pack to the pack root."""
    if not path:
        return None
    try:
        cur = Path(path).expanduser().resolve()
    except OSError:
        return None
    for cand in (cur, *cur.parents):
        if (cand / _TOOLCHAIN_MARKER).is_file():
            return cand
    return None


def toolchain_root(host: BuildHost | None = None) -> Path | None:
    """Active retcomm toolchain pack, or None when building with host tools.

    Only a pack that owns the running interpreter or the cmake we are about to
    invoke counts — that is the pack whose sysroot the compile will use. An
    installed-but-unused pack is deliberately ignored: its dependencies are
    built against its own sysroot and would be the wrong answer for a build
    driven by host clang.
    """
    env_dir = (os.environ.get("RETCOMM_TOOLCHAIN_DIR") or "").strip()
    if env_dir:
        cand = Path(env_dir).expanduser()
        if (cand / _TOOLCHAIN_MARKER).is_file():
            return cand.resolve()
    host = host or detect_host()
    for probe in (sys.executable, host.cmake):
        found = _toolchain_root_from(probe)
        if found is not None:
            return found
    return None


def toolchain_env(host: BuildHost | None = None) -> dict[str, str]:
    """Environment overlay pointing find_package() at the pack's deps.

    Mirrors ``<pack>/env.sh``. Empty when no pack drives the build. These go
    in the environment rather than on the command line because cmake warns
    about ``-D`` variables a project never reads, and a project that links
    SDL3 but not zlib would otherwise warn on every configure.
    """
    pack = toolchain_root(host)
    if pack is None:
        return {}
    deps = pack / "deps"
    if not deps.is_dir():
        return {}
    overlay: dict[str, str] = {}
    sdl3_cfg = deps / "lib" / "cmake" / "SDL3"
    if (sdl3_cfg / "SDL3Config.cmake").is_file() or (
        sdl3_cfg / "SDL3-config.cmake"
    ).is_file():
        overlay["SDL3_DIR"] = str(sdl3_cfg)
    if (deps / "include" / "zlib.h").is_file():
        overlay["ZLIB_ROOT"] = str(deps)
    prior = (os.environ.get("CMAKE_PREFIX_PATH") or "").strip()
    overlay["CMAKE_PREFIX_PATH"] = (
        f"{deps}{os.pathsep}{prior}" if prior else str(deps)
    )
    return overlay


def toolchain_cache_repairs(
    build_dir: Path,
    extra_args: list[str] | None = None,
    host: BuildHost | None = None,
) -> list[str]:
    """``-D`` pins that re-point a build tree already cached to host deps.

    The environment overlay is only a *hint*: find_package() prefers an
    existing ``<pkg>_DIR`` cache entry, so a tree configured before this fix
    keeps resolving to the host's SDL3 and keeps failing to compile. Override
    those entries explicitly. Only entries that are present and point outside
    the pack are touched, so this never introduces an unused-variable warning:
    the entry exists precisely because the project read it.
    """
    pack = toolchain_root(host)
    if pack is None:
        return []
    already = _explicit_cache_vars(extra_args)
    repairs: list[str] = []
    for name, want in toolchain_env(host).items():
        if name == "CMAKE_PREFIX_PATH" or name in already:
            continue
        have = cache_entry(build_dir, name)
        if have and Path(have) != Path(want):
            repairs.append(f"-D{name}:PATH={want}")
    return repairs


def _explicit_cache_vars(extra_args: list[str] | None) -> set[str]:
    """Names of ``-DVAR[:TYPE]=…`` entries a caller already passed."""
    names: set[str] = set()
    for arg in extra_args or []:
        m = re.match(r"-D([A-Za-z0-9_]+)(?::[A-Za-z]+)?=", arg)
        if m:
            names.add(m.group(1))
    return names


def merged_env(overlay: dict[str, str]) -> dict[str, str] | None:
    """os.environ plus ``overlay``, or None when there is nothing to add."""
    if not overlay:
        return None
    env = os.environ.copy()
    env.update(overlay)
    return env


def default_generator(host: BuildHost | None = None) -> str:
    host = host or detect_host()
    if host.ninja:
        return "Ninja"
    if host.label == "windows":
        # Leave empty → cmake picks VS / default generator.
        return ""
    return "Unix Makefiles"


def cache_entry(build_dir: Path, name: str) -> str:
    """Value of ``name`` in an existing CMakeCache.txt, or empty.

    Matches any cache type, so ``SDL3_DIR`` (PATH) and ``CMAKE_GENERATOR``
    (INTERNAL) read the same way.
    """
    cache = Path(build_dir) / "CMakeCache.txt"
    if not cache.is_file():
        return ""
    try:
        text = cache.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    prefix = f"{name}:"
    for line in text.splitlines():
        if line.startswith(prefix) and "=" in line:
            head, val = line.split("=", 1)
            if head.split(":", 1)[0] == name:
                return val.strip()
    return ""


def cached_cmake_generator(build_dir: Path) -> str:
    """CMAKE_GENERATOR from an existing CMakeCache.txt, or empty."""
    return cache_entry(build_dir, "CMAKE_GENERATOR")


def normalize_generator_request(generator: str | None) -> str | None:
    """None / empty / 'auto' → Auto. Otherwise the cmake -G name."""
    if generator is None:
        return None
    g = generator.strip()
    if not g or g.lower() == "auto":
        return None
    return g


def resolve_configure_generator(
    host: BuildHost,
    build_dir: Path,
    generator: str | None,
) -> str:
    """Explicit -G, else the cache's generator, else host default."""
    requested = normalize_generator_request(generator)
    if requested is not None:
        return requested
    cached = cached_cmake_generator(build_dir)
    if cached:
        return cached
    return default_generator(host)


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=VAL`` pairs from free text (space / newline / ``;`` separated).

    Values may be quoted with single or double quotes. Lines starting with ``#``
    are ignored.
    """
    env: dict[str, str] = {}
    if not text or not text.strip():
        return env
    # Normalize separators to newlines, but keep quoted spans intact via a
    # simple token walk on KEY=VAL forms.
    cleaned: list[str] = []
    for raw_line in text.replace(";", "\n").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        cleaned.append(line)
    blob = "\n".join(cleaned)
    # Match KEY=VALUE where VALUE is "…", '…', or non-space / until next KEY=
    pattern = re.compile(
        r"""(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<val>"[^"]*"|'[^']*'|\S+)"""
    )
    for m in pattern.finditer(blob):
        key = m.group("key")
        val = m.group("val")
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        env[key] = val
    return env


def _run_stream(
    cmd: list[str],
    cwd: Path,
    *,
    log: LogFn | None = None,
    env: dict[str, str] | None = None,
) -> CmdResult:
    if log:
        log("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
        )
    except OSError as exc:
        return CmdResult(False, f"Failed to start: {cmd[0]}", str(exc))

    assert proc.stdout is not None
    lines: list[str] = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        if log:
            log(line)
    code = proc.wait()
    detail = "\n".join(lines[-40:])
    if code != 0:
        return CmdResult(False, f"Command failed (exit {code})", detail)
    return CmdResult(True, "OK", detail)


def resolve_framework_root(root: Path) -> Path | None:
    """Return the psxrecomp framework root (bios/ + recompiler/), or None."""
    root = root.expanduser().resolve()
    for cand in (root / "psxrecomp", root):
        if (cand / "bios" / "OpenBIOS.toml").is_file() and (cand / "recompiler").is_dir():
            return cand
    return None


_PROJECT_RE = re.compile(r"^\s*project\s*\(\s*([A-Za-z0-9_.+-]+)", re.MULTILINE)


def default_target(root: Path) -> str:
    """The CMake target to build for this platform's projects.

    PSX ports all build one shared runtime target (``psx-runtime``). SNES ports
    name their executable after the project, so there is no constant to use —
    the target is read out of the repo's own ``project()`` call.
    """
    if platforms.current().default_target:
        return platforms.current().default_target
    cml = Path(root).expanduser().resolve() / "CMakeLists.txt"
    if cml.is_file():
        try:
            m = _PROJECT_RE.search(cml.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            m = None
        if m:
            return m.group(1)
    # `all` builds everything the project defines — correct, if not minimal.
    return "all"


def snes_regen_script(root: Path) -> Path | None:
    """``tools/regen.sh`` — the SNES ROM → C step, owned by the project."""
    p = Path(root).expanduser().resolve() / "tools" / "regen.sh"
    return p if p.is_file() else None


def generate_snes_c(
    root: Path,
    *,
    rom: str = "",
    cfg_roots: bool = False,
    verify: bool = True,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Run the project's own tools/regen.sh.

    Studio deliberately does not reimplement generation: regen.sh carries the
    ROM digests this port was pinned against and verifies them before emitting
    anything. Calling snesrecomp_cli directly would skip that check, which is
    the one thing standing between a mismatched dump and hours of chasing
    divergence that was never in the recompiler.
    """
    root = Path(root).expanduser().resolve()
    script = snes_regen_script(root)
    if script is None:
        return CmdResult(
            False,
            f"No tools/regen.sh in {root} — run Migrate → Emit tools/regen.sh first",
        )
    cmd = ["sh", str(script)]
    if rom:
        rom_p = Path(rom).expanduser()
        if not rom_p.is_file():
            return CmdResult(False, f"ROM not found: {rom}")
        cmd.extend(["--rom", str(rom_p.resolve())])
    if not verify:
        cmd.append("--no-verify")
    if cfg_roots:
        cmd.append("--cfg-roots")
    if dry_run:
        msg = "dry-run: " + " ".join(cmd)
        if log:
            log(msg)
        return CmdResult(True, msg)
    r = _run_stream(cmd, root, log=log)
    if r.ok:
        gen = root / "src" / "gen"
        n = len(list(gen.glob("*.c"))) if gen.is_dir() else 0
        return CmdResult(True, f"Generated {n} C file(s) into src/gen", r.detail)
    return r


_MAX_PLAYERS_CMAKE_RE = re.compile(
    r"^\s*MAX_PLAYERS\s+(\d+)\s*$", re.MULTILINE | re.IGNORECASE
)
_MAX_PLAYERS_RANGE_RE = re.compile(
    r"MAX_PLAYERS must be in\s+(\d+)\.\.(\d+)", re.IGNORECASE
)


def project_max_players(root: Path) -> int | None:
    """``MAX_PLAYERS N`` from the game ``CMakeLists.txt``, if present."""
    cmake = root / "CMakeLists.txt"
    if not cmake.is_file():
        return None
    try:
        text = cmake.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _MAX_PLAYERS_CMAKE_RE.search(text)
    return int(m.group(1)) if m else None


def framework_max_players_range(fw: Path) -> tuple[int, int] | None:
    """``(lo, hi)`` from runtime.cmake's FATAL_ERROR range check, if found."""
    cmake = fw / "runtime" / "runtime.cmake"
    if not cmake.is_file():
        return None
    try:
        text = cmake.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _MAX_PLAYERS_RANGE_RE.search(text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def preflight_max_players(root: Path) -> CmdResult | None:
    """Fail fast when the game asks for MAX_PLAYERS the nested framework rejects.

    Single-player titles (Ape Escape, Tomba, …) use ``MAX_PLAYERS 1``. Older
    psxrecomp pins only allowed 2..5 and abort configure with a cryptic
    FATAL_ERROR. Detect that before running cmake.
    """
    players = project_max_players(root)
    if players is None:
        return None
    fw = resolve_framework_root(root)
    if fw is None:
        return None
    rng = framework_max_players_range(fw)
    if rng is None:
        return None
    lo, hi = rng
    if lo <= players <= hi:
        return None
    pins = root / "framework_pins.txt"
    pin_hint = ""
    if pins.is_file():
        pin_hint = f" Check {pins.name} vs the checked-out psxrecomp commit."
    return CmdResult(
        False,
        f"MAX_PLAYERS {players} is outside nested psxrecomp range {lo}..{hi}. "
        f"Update the psxrecomp submodule to a pin that allows 1..8 "
        f"(runtime.cmake after single-player / rewind support).{pin_hint}",
    )


def diagnose_configure_failure(detail: str, root: Path) -> str | None:
    """Extra hint appended to a failed cmake configure message."""
    blob = detail or ""
    if "MAX_PLAYERS must be in" in blob and "got" in blob:
        pre = preflight_max_players(root)
        if pre is not None:
            return pre.message
        return (
            "MAX_PLAYERS rejected by nested psxrecomp. Single-player titles need "
            "a framework pin whose runtime.cmake allows 1..8 — update the "
            "psxrecomp submodule (and framework_pins.txt)."
        )
    if "generated" in blob.lower() and (
        "GEN_MARKER" in blob or "dispatch.c" in blob or "missing" in blob.lower()
    ):
        return (
            "Game generated C may be missing — run Generate (disc→C) before "
            "Configure, or ensure generated/<boot>_dispatch.c exists."
        )
    if "Does not match the generator used previously" in blob:
        return (
            "This build dir already uses a different CMake generator. "
            "Pick matching Generator (Ninja vs Unix Makefiles) on the Build tab, "
            "or remove CMakeCache.txt and CMakeFiles / use a new build dir."
        )
    return None


def bios_backend_present(fw: Path, stem: str) -> bool:
    """True when generated/<stem>_{full,dispatch}.c look linkable."""
    dispatch = fw / "generated" / f"{stem}_dispatch.c"
    full = fw / "generated" / f"{stem}_full.c"
    if not dispatch.is_file() or not full.is_file():
        return False
    try:
        text = dispatch.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return f"{stem}_psx_bios_backend" in text


def _allow_no_bios(extra_args: list[str] | None) -> bool:
    if not extra_args:
        return False
    blob = " ".join(extra_args)
    return (
        "PSXRECOMP_ALLOW_NO_BIOS=ON" in blob
        or "PSXRECOMP_ALLOW_NO_BIOS:BOOL=ON" in blob
        or "PSXRECOMP_ALLOW_NO_BIOS=1" in blob
    )


def _find_psxrecomp_bios(fw: Path, game_root: Path | None = None) -> Path | None:
    names = ("psxrecomp-bios.exe", "psxrecomp-bios")
    dirs: list[Path] = [
        fw / "recompiler" / "build",
        fw / "recompiler" / "build-t2",
    ]
    if game_root is not None:
        dirs.append(game_root / "build-recompiler")
    for d in dirs:
        if not d.is_dir():
            continue
        for name in names:
            p = d / name
            if p.is_file():
                return p
        # Nested generator layouts (e.g. Debug/Release on MSVC)
        for sub in d.iterdir():
            if not sub.is_dir():
                continue
            for name in names:
                p = sub / name
                if p.is_file():
                    return p
    return None


def _recompiler_build_usable(build_dir: Path) -> bool:
    cache = build_dir / "CMakeCache.txt"
    if not cache.is_file():
        return False
    gen = cached_cmake_generator(build_dir)
    if gen.startswith("Ninja"):
        return (build_dir / "build.ninja").is_file()
    if "Makefiles" in gen:
        return (build_dir / "Makefile").is_file()
    if gen.startswith("Visual Studio"):
        return any(build_dir.glob("*.sln"))
    # Unknown generator — trust the cache and let cmake --build diagnose.
    return True


def _iter_recompiler_build_dirs(fw: Path) -> list[Path]:
    src = fw / "recompiler"
    if not src.is_dir():
        return []
    dirs: list[Path] = []
    for name in ("build-t2", "build"):
        dirs.append(src / name)
    dirs.extend(sorted(src.glob("cmake-build*")))
    return dirs


def _any_usable_recompiler_build(fw: Path) -> bool:
    return any(_recompiler_build_usable(d) for d in _iter_recompiler_build_dirs(fw))


def ensure_bios_emitter(
    fw: Path,
    *,
    game_root: Path | None = None,
    build_type: str = DEFAULT_BUILD_TYPE,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Configure ``recompiler/build`` and build ``psxrecomp-bios`` if needed."""
    host = detect_host()
    if not host.cmake:
        return CmdResult(False, "cmake not found on PATH (needed to build psxrecomp-bios)")

    existing = _find_psxrecomp_bios(fw, game_root)
    if existing is not None and not dry_run:
        if log:
            log(f"BIOS emitter ready: {existing}")
        return CmdResult(True, f"BIOS emitter ready: {existing.name}")

    src = fw / "recompiler"
    if not (src / "CMakeLists.txt").is_file():
        return CmdResult(False, f"recompiler sources missing under {src}")

    # Prefer an already-finished tree (matches regen_bios.sh discovery order).
    build_dir = src / "build"
    for cand in _iter_recompiler_build_dirs(fw):
        if _recompiler_build_usable(cand):
            build_dir = cand
            break

    gen = default_generator(host)
    cfg = [host.cmake, "-S", str(src), "-B", str(build_dir)]
    if gen:
        cfg.extend(["-G", gen])
    cfg.append(f"-DCMAKE_BUILD_TYPE={build_type}")
    cfg.extend(toolchain_cache_repairs(build_dir, host=host))

    if dry_run:
        msg = "dry-run: " + " ".join(cfg)
        if log:
            log(msg)
            log(f"dry-run: {host.cmake} --build {build_dir} --target psxrecomp-bios")
        return CmdResult(True, msg)

    if not _recompiler_build_usable(build_dir):
        if log:
            log("Configuring recompiler for psxrecomp-bios…")
        build_dir.mkdir(parents=True, exist_ok=True)
        r = _run_stream(cfg, fw, log=log, env=merged_env(toolchain_env(host)))
        if not r.ok:
            return CmdResult(
                False,
                "Failed to configure recompiler (needed for OpenBIOS regen)",
                r.detail,
            )

    jobs = str(host.jobs)
    build_cmd = [
        host.cmake,
        "--build",
        str(build_dir),
        "--target",
        "psxrecomp-bios",
        "-j",
        jobs,
    ]
    if log:
        log("Building psxrecomp-bios…")
    r = _run_stream(build_cmd, fw, log=log)
    if not r.ok:
        return CmdResult(False, "Failed to build psxrecomp-bios", r.detail)

    bios = _find_psxrecomp_bios(fw, game_root)
    if bios is None:
        return CmdResult(
            False,
            f"psxrecomp-bios not found after build under {build_dir}",
        )
    return CmdResult(True, f"Built BIOS emitter: {bios.name}")


def _regen_bios_profile(
    fw: Path,
    profile_rel: str,
    *,
    game_root: Path | None = None,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Regen one BIOS profile into ``fw/generated/`` (canonical regen_bios.sh)."""
    profile = fw / profile_rel
    if not profile.is_file():
        return CmdResult(False, f"BIOS profile missing: {profile}")

    script = fw / "tools" / "regen_bios.sh"
    bash = shutil.which("bash") or shutil.which("bash.exe")

    if script.is_file() and bash:
        cmd = [bash, str(script), "--config", profile_rel]
        if dry_run:
            msg = "dry-run: " + " ".join(cmd) + f"  (cwd={fw})"
            if log:
                log(msg)
            return CmdResult(True, msg)
        return _run_stream(cmd, fw, log=log)

    # Fallback without bash: invoke emitter + optional fingerprint helper.
    r = ensure_bios_emitter(fw, game_root=game_root, dry_run=dry_run, log=log)
    if not r.ok:
        return r
    if dry_run:
        return CmdResult(True, f"dry-run: psxrecomp-bios --config {profile_rel}")

    bios = _find_psxrecomp_bios(fw, game_root)
    if bios is None:
        return CmdResult(False, "psxrecomp-bios missing after ensure")
    (fw / "generated").mkdir(parents=True, exist_ok=True)
    r = _run_stream([str(bios), "--config", profile_rel], fw, log=log)
    if not r.ok:
        return CmdResult(False, f"psxrecomp-bios failed for {profile_rel}", r.detail)

    # Best-effort fingerprint (staleness WARN in runtime.cmake).
    fp = fw / "tools" / "bios_emitter_fingerprint.sh"
    if fp.is_file() and bash:
        stem = OPENBIOS_STEM if "OpenBIOS" in profile_rel else SCPH1001_STEM
        try:
            proc = subprocess.run(
                [bash, str(fp), profile_rel],
                cwd=str(fw),
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                out = fw / "generated" / f"{stem}.emitter.sha"
                out.write_text(proc.stdout, encoding="utf-8")
                if log:
                    log(f"Wrote fingerprint {out.name}")
        except OSError:
            pass
    return CmdResult(True, f"Regenerated BIOS profile {profile_rel}")


def ensure_bios_backends(
    root: Path,
    *,
    force: bool = False,
    include_scph1001: bool = True,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Ensure linkable OpenBIOS (and optional SCPH1001) under ``psxrecomp/generated``.

    OpenBIOS is bundled and MIT-licensed. SCPH1001 is regenerated only when the
    retail dump ``bios/SCPH1001.BIN`` is already present beside the profile.
    """
    root = root.expanduser().resolve()
    fw = resolve_framework_root(root)
    if fw is None:
        return CmdResult(True, "No psxrecomp framework — skipping BIOS ensure")

    needed: list[tuple[str, str]] = []
    if force or not bios_backend_present(fw, OPENBIOS_STEM):
        needed.append((OPENBIOS_STEM, OPENBIOS_PROFILE))
    elif log:
        log(f"OpenBIOS backend already present under {fw / 'generated'}")

    scph_rom = fw / "bios" / "SCPH1001.BIN"
    if include_scph1001 and scph_rom.is_file():
        if force or not bios_backend_present(fw, SCPH1001_STEM):
            needed.append((SCPH1001_STEM, SCPH1001_PROFILE))
        elif log:
            log("SCPH1001 backend already present")

    if not needed:
        return CmdResult(True, "BIOS backends already generated")

    # Prefer regen_bios.sh (builds emitter + fingerprints). It does not configure
    # the recompiler — ensure a usable tree (or a found binary) first.
    script = fw / "tools" / "regen_bios.sh"
    bash = shutil.which("bash") or shutil.which("bash.exe")
    need_emitter_setup = not (
        _any_usable_recompiler_build(fw) or _find_psxrecomp_bios(fw, root) is not None
    )
    if need_emitter_setup or not (script.is_file() and bash):
        r = ensure_bios_emitter(fw, game_root=root, dry_run=dry_run, log=log)
        if not r.ok:
            return r

    done: list[str] = []
    for stem, profile in needed:
        if log:
            log(f"Regenerating {stem} via {profile}…")
        r = _regen_bios_profile(
            fw, profile, game_root=root, dry_run=dry_run, log=log
        )
        if not r.ok:
            return CmdResult(
                False,
                f"Failed to regenerate {stem}: {r.message}",
                r.detail,
            )
        if not dry_run and not bios_backend_present(fw, stem):
            return CmdResult(
                False,
                f"Regen finished but {stem} backend still missing under {fw / 'generated'}",
            )
        done.append(stem)

    return CmdResult(True, "BIOS ready: " + ", ".join(done))


def configure(
    root: Path,
    *,
    build_dir: str = DEFAULT_BUILD_DIR,
    build_type: str = DEFAULT_BUILD_TYPE,
    generator: str | None = None,
    extra_args: list[str] | None = None,
    dry_run: bool = False,
    log: LogFn | None = None,
    ensure_bios: bool = True,
) -> CmdResult:
    root = root.expanduser().resolve()
    host = detect_host()
    if not host.cmake:
        return CmdResult(False, "cmake not found on PATH")
    if not (root / "CMakeLists.txt").is_file():
        return CmdResult(False, f"No CMakeLists.txt in {root}")

    # The BIOS backend step is a PSX concept: a SNES cartridge boots from its
    # own reset vector and there is nothing to stage.
    if ensure_bios and platforms.current().key != "snes" and not _allow_no_bios(extra_args):
        bios_r = ensure_bios_backends(root, dry_run=dry_run, log=log)
        if not bios_r.ok:
            return bios_r
        if log and bios_r.message:
            log(bios_r.message)

    pre = preflight_max_players(root) if platforms.current().key != "snes" else None
    if pre is not None:
        if log:
            log(pre.message)
        return pre

    bdir = Path(build_dir)
    if not bdir.is_absolute():
        bdir = root / bdir
    gen = resolve_configure_generator(host, bdir, generator)
    cmd = [host.cmake, "-S", str(root), "-B", str(bdir)]
    if gen:
        cmd.extend(["-G", gen])
    cmd.append(f"-DCMAKE_BUILD_TYPE={build_type}")
    # Bundled deps before caller overrides: a caller's explicit -D wins.
    repairs = toolchain_cache_repairs(bdir, extra_args, host)
    cmd.extend(repairs)
    if extra_args:
        cmd.extend(extra_args)
    tc_env = toolchain_env(host)

    if dry_run:
        msg = "dry-run: " + " ".join(cmd)
        if log:
            log(msg)
        return CmdResult(True, msg)

    if log and repairs:
        log("Re-pointing cached host deps at the toolchain: " + " ".join(repairs))
    r = _run_stream(cmd, root, log=log, env=merged_env(tc_env))
    if r.ok:
        r = CmdResult(
            True,
            f"Configured {bdir.name} ({build_type}" + (f", {gen}" if gen else "") + ")",
            r.detail,
        )
        return r
    hint = diagnose_configure_failure(r.detail or "", root)
    if hint:
        msg = f"{r.message}\n{hint}"
        if log:
            log(hint)
        return CmdResult(False, msg, r.detail)
    return r


def build(
    root: Path,
    *,
    build_dir: str = DEFAULT_BUILD_DIR,
    target: str = DEFAULT_TARGET,
    jobs: int | None = None,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    root = root.expanduser().resolve()
    host = detect_host()
    if not host.cmake:
        return CmdResult(False, "cmake not found on PATH")
    bdir = Path(build_dir)
    if not bdir.is_absolute():
        bdir = root / bdir
    if not bdir.is_dir():
        return CmdResult(False, f"Build dir missing — Configure first: {bdir}")

    j = jobs if jobs and jobs > 0 else host.jobs
    cmd = [host.cmake, "--build", str(bdir), "--target", target, "-j", str(j)]
    if dry_run:
        msg = "dry-run: " + " ".join(cmd)
        if log:
            log(msg)
        return CmdResult(True, msg)

    r = _run_stream(cmd, root, log=log)
    if r.ok:
        exe = find_runtime_exe(bdir)
        hint = f" → {exe.name}" if exe else ""
        r = CmdResult(True, f"Built {target} in {bdir.name}{hint}", r.detail)
    return r


def find_runtime_exe(build_dir: Path) -> Path | None:
    """Locate the game product binary under a CMake build tree."""
    build_dir = build_dir.expanduser().resolve()
    if not build_dir.is_dir():
        return None

    host = detect_host()
    suffixes = {""}
    if host.label == "windows":
        suffixes = {".exe"}

    # Prefer names that look like Recompiled products / known targets.
    ranked: list[tuple[int, Path]] = []
    skip_dirs = {
        "CMakeFiles",
        "_deps",
        ".cmake",
        "Testing",
        "CMakeTmp",
        "assets",
        "bios",
        "fonts",
        "img",
        "mods",
    }

    def consider(p: Path) -> None:
        if not p.is_file():
            return
        name = p.name
        lower = name.lower()
        if host.label == "windows":
            if not lower.endswith(".exe"):
                return
            stem = name[:-4]
        else:
            if any(
                lower.endswith(ext)
                for ext in (".so", ".dll", ".dylib", ".a", ".lib", ".pdb", ".cmake", ".ninja")
            ):
                return
            stem = name
        if stem.lower() in ("cmake", "ninja", "cpack", "ctest"):
            return
        score = 0
        if "recompil" in lower:
            score += 100
        if stem in ("psx-runtime", "psx-runtime.exe") or stem == "psx-runtime":
            score += 50
        if p.parent == build_dir:
            score += 20
        if host.label != "windows" and not os.access(p, os.X_OK):
            return
        ranked.append((score, p))

    for p in build_dir.iterdir():
        if p.is_file():
            consider(p)

    for sub in build_dir.iterdir():
        if not sub.is_dir() or sub.name in skip_dirs or sub.name.startswith("."):
            continue
        if sub.name in ("Debug", "Release", "RelWithDebInfo", "MinSizeRel") or host.label == "windows":
            for p in sub.iterdir():
                if p.is_file():
                    consider(p)

    if not ranked:
        return None
    ranked.sort(key=lambda t: (-t[0], t[1].name.lower()))
    return ranked[0][1]


def resolve_build_dir(root: Path, build_dir: str) -> Path:
    root = root.expanduser().resolve()
    bdir = Path(build_dir)
    if not bdir.is_absolute():
        bdir = root / bdir
    return bdir


def launch_rom_for(root: Path | str, rom: str = "") -> tuple[str, str]:
    """Which ROM a SNES launch should hand the runner, and where it came from.

    Returns ``(path, provenance)``; both empty when there is nothing to pass.
    A cartridge runner takes the ROM as a positional and exits 1 without one,
    and the path is not the caller's to remember — it is whatever New Project
    or the Migrate tab recorded for this repo.
    """
    rom = (rom or "").strip()
    if rom:
        return rom, "explicit"
    if not platforms.is_snes():
        return "", ""
    from .repo_index import load_index

    entry = load_index().find(root)
    if entry is not None and entry.cue:
        return entry.cue, "the repo index"
    return "", ""


def launch(
    root: Path,
    *,
    build_dir: str = DEFAULT_BUILD_DIR,
    exe: Path | str | None = None,
    env_text: str = "",
    extra_args: list[str] | None = None,
    dry_run: bool = False,
    log: LogFn | None = None,
    wait: bool = True,
) -> CmdResult:
    """Start the local product build.

    By default ``wait=True``: stream stdout/stderr into ``log`` until the
    process exits (Studio Launch → activity log). Pass ``wait=False`` to
    detach immediately (legacy fire-and-forget).
    """
    global _active_launch
    root = root.expanduser().resolve()
    bdir = resolve_build_dir(root, build_dir)
    exe_path = Path(exe) if exe else find_runtime_exe(bdir)
    if exe_path is None:
        return CmdResult(False, f"No runtime executable found under {bdir}")
    if not exe_path.is_file():
        return CmdResult(False, f"Executable missing: {exe_path}")

    overlay = parse_env_text(env_text)
    env = os.environ.copy()
    env.update(overlay)
    # Run from game root so relative game.toml / disc / saves resolve.
    cmd = [str(exe_path), *(extra_args or [])]
    # Prefer line-buffered stdio so fprintf diagnostics show up live when piped.
    if sys.platform != "win32" and shutil.which("stdbuf"):
        cmd = ["stdbuf", "-oL", "-eL", *cmd]

    if dry_run:
        preview = " ".join(f"{k}={v}" for k, v in overlay.items())
        msg = "dry-run: " + (f"env {preview} " if preview else "") + " ".join(cmd)
        _flush_log(log, msg)
        return CmdResult(True, msg)

    with _launch_lock:
        if _active_launch and _active_launch.poll() is None:
            return CmdResult(
                False,
                f"Already running (pid {_active_launch.pid}) — Stop first",
            )
        stale = _read_launch_pid()
        if stale and _pid_alive(stale[0]):
            return CmdResult(
                False,
                f"Already running (pid {stale[0]}) — Stop first",
            )
        try:
            kwargs: dict = {
                "cwd": str(root),
                "env": env,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 1,
            }
            host = detect_host()
            if host.label == "windows":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                # New session → killpg works from a separate ``build stop`` process.
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            return CmdResult(False, "Launch failed", str(exc))
        _active_launch = LaunchHandle(
            proc=proc, exe=exe_path, cwd=root, env_overlay=overlay
        )
        if proc.pid:
            _write_launch_pid(proc.pid, exe=exe_path, root=root)

    env_note = ""
    if overlay:
        env_note = " env=[" + ", ".join(sorted(overlay)) + "]"
    msg = f"Launched {exe_path.name} (pid {proc.pid}){env_note}"
    _flush_log(log, msg)
    _flush_log(log, "--- game stdout/stderr (Stop to end) ---")

    if not wait:
        # Detach: drain pipes so the child never blocks on a full pipe.
        def _drain() -> None:
            global _active_launch
            try:
                if proc.stdout:
                    for line in proc.stdout:
                        _flush_log(log, line.rstrip("\n\r"))
            except Exception:
                pass
            code = proc.wait()
            with _launch_lock:
                if _active_launch and _active_launch.proc is proc:
                    _active_launch = None
            _clear_launch_pid()
            _flush_log(log, f"--- game exited (code {code}) ---")

        threading.Thread(target=_drain, daemon=True).start()
        return CmdResult(True, msg)

    # Stream diagnostics until exit (Studio keeps the CLI job alive → activity log).
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            _flush_log(log, line.rstrip("\n\r"))
    except Exception as exc:
        _flush_log(log, f"[warn] log reader: {exc}")
    code = proc.wait()
    with _launch_lock:
        if _active_launch and _active_launch.proc is proc:
            _active_launch = None
    _clear_launch_pid()
    _flush_log(log, f"--- game exited (code {code}) ---")
    if code != 0:
        return CmdResult(False, f"{exe_path.name} exited {code}", msg)
    return CmdResult(True, f"{exe_path.name} exited 0")


def stop_launch() -> CmdResult:
    global _active_launch
    killed_pid: int | None = None
    with _launch_lock:
        h = _active_launch
        if h is not None and h.poll() is None:
            killed_pid = h.pid
            h.terminate()
            try:
                h.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                h.proc.kill()
            _active_launch = None
    # Cross-process Stop (Studio Launch holds ``build run`` in another Python).
    stale = _read_launch_pid()
    if stale and _pid_alive(stale[0]):
        _kill_launch_pid(stale[0])
        killed_pid = stale[0]
    _clear_launch_pid()
    if killed_pid is None:
        return CmdResult(False, "No running launch")
    return CmdResult(True, f"Stopped pid {killed_pid}")


def launch_status() -> str:
    with _launch_lock:
        h = _active_launch
        if h is not None:
            code = h.poll()
            if code is None:
                return f"running pid={h.pid} ({h.exe.name})"
            return f"exited code={code} ({h.exe.name})"
    stale = _read_launch_pid()
    if stale and _pid_alive(stale[0]):
        name = Path(stale[1]).name if stale[1] else "?"
        return f"running pid={stale[0]} ({name})"
    return "not running"


def find_psxrecomp_cli(root: Path) -> Path | None:
    """Locate ``psxrecomp_cli.py`` next to the game's framework checkout.

    ``RETCOMM_PSXRECOMP_CLI`` overrides the search so a framework change can be
    exercised against a game repo before its submodule pin moves.
    """
    root = root.expanduser().resolve()
    override = os.environ.get("RETCOMM_PSXRECOMP_CLI", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p.resolve()
    fw = resolve_framework_root(root)
    candidates: list[Path] = []
    if fw is not None:
        candidates.append(fw / "psxrecomp_cli.py")
    candidates.append(root / "psxrecomp" / "psxrecomp_cli.py")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def generate_rom_and_bios(
    root: Path,
    *,
    disc: str = "",
    bios: str = "",
    force_bios: bool = True,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Run ``psxrecomp_cli.py generate`` (BIOS backends + disc prepare + game C).

    ``bios`` empty → OpenBIOS (bundled MIT). Non-empty path → stage as
    ``bios/SCPH1001.BIN`` and regenerate the retail backend, then game C.
    """
    root = root.expanduser().resolve()
    cli = find_psxrecomp_cli(root)
    if cli is None:
        return CmdResult(False, "psxrecomp_cli.py not found (is psxrecomp checked out?)")
    config = root / "game.toml"
    if not config.is_file():
        return CmdResult(False, f"Missing game.toml in {root}")

    py = sys.executable or shutil.which("python3") or shutil.which("python")
    if not py:
        return CmdResult(False, "No Python interpreter for generate")

    cmd = [
        str(py),
        str(cli),
        "generate",
        "--config",
        str(config),
        "--project-root",
        str(root),
    ]
    disc_s = (disc or "").strip()
    if disc_s:
        cmd.extend(["--disc", disc_s])
    bios_s = (bios or "").strip()
    if bios_s:
        bp = Path(bios_s).expanduser()
        if not bp.is_file():
            return CmdResult(False, f"BIOS dump not found: {bios_s}")
        cmd.extend(["--bios", str(bp.resolve())])
    if force_bios:
        cmd.append("--force-bios")

    if dry_run:
        return CmdResult(True, "dry-run: " + " ".join(cmd))

    if log:
        mode = f"SCPH1001 ({bios_s})" if bios_s else "OpenBIOS"
        log(f"Generate ROM + BIOS C ({mode})")
    return _run_stream(cmd, root, log=log)


def ensure_emitters(
    root: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> CmdResult:
    """Build ``psxrecomp-game`` + ``psxrecomp-bios`` via ``psxrecomp_cli ensure-emitters``."""
    root = root.expanduser().resolve()
    cli = find_psxrecomp_cli(root)
    if cli is None:
        return CmdResult(False, "psxrecomp_cli.py not found (is psxrecomp checked out?)")

    py = sys.executable or shutil.which("python3") or shutil.which("python")
    if not py:
        return CmdResult(False, "No Python interpreter for ensure-emitters")

    cmd = [
        str(py),
        str(cli),
        "ensure-emitters",
        "--project-root",
        str(root),
    ]
    if force:
        cmd.append("--force")

    if dry_run:
        return CmdResult(True, "dry-run: " + " ".join(cmd))

    if log:
        log("Generate emitters (psxrecomp-game + psxrecomp-bios)" + (" [force]" if force else ""))
    return _run_stream(cmd, root, log=log)


# --- local bundle + export -------------------------------------------------
#
# Studio's Build tab "Bundle + Export" for the regular (host) build: package
# what is already in the local build dir into dist/<prefix>-<ver>-<tag>.zip,
# then hand the path back so the GUI can open a native save dialog.


@dataclass
class PackageResult:
    ok: bool
    message: str
    detail: str = ""
    zip_path: Path | None = None


def host_artifact_tag(host: BuildHost | None = None) -> str:
    """`linux-x64`, `windows-x64`, `macos-arm64`, … — matches CI zip naming."""
    host = host or detect_host()
    machine = (platform.machine() or "").lower()
    if machine in ("x86_64", "amd64", "x64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine in ("i386", "i686", "x86"):
        arch = "x86"
    else:
        arch = re.sub(r"[^a-z0-9]+", "", machine) or "unknown"
    return f"{host.label}-{arch}"


def project_version(root: Path) -> str:
    """VERSION file → game.toml `version` → `0.0.0` (mirrors package_release.sh)."""
    vf = root / "VERSION"
    if vf.is_file():
        text = vf.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text.split()[0]
    toml = root / "game.toml"
    if toml.is_file():
        for line in toml.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"""\s*version\s*=\s*["']([^"']+)["']""", line)
            if m:
                return m.group(1).strip()
    return "0.0.0"


def _zip_prefix(root: Path) -> str:
    try:
        from fill_tokens import derive_zip_prefix
    except ImportError:
        derive_zip_prefix = None  # type: ignore[assignment]
    if derive_zip_prefix is not None:
        prefix = derive_zip_prefix(root.name)
        if prefix:
            return prefix
    return re.sub(r"[^a-z0-9._-]+", "", root.name.lower()) or "game"


def _newest_zip(dist: Path, tag: str) -> Path | None:
    if not dist.is_dir():
        return None
    zips = sorted(dist.glob(f"*-{tag}.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not zips:
        zips = sorted(dist.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return zips[0].resolve() if zips else None


def _stage_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
    else:
        shutil.copy2(src, dst)


def _write_zip(stage: Path, out: Path) -> None:
    """Zip `stage` preserving the executable bit (no `zip` binary needed)."""
    import zipfile

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    entries = sorted(p for p in stage.rglob("*"))
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in entries:
            rel = path.relative_to(stage).as_posix()
            if path.is_dir():
                info = zipfile.ZipInfo(rel + "/")
                info.external_attr = (0o40755 << 16) | 0x10
                zf.writestr(info, b"")
                continue
            info = zipfile.ZipInfo.from_file(path, rel)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = path.stat().st_mode & 0o777
            info.external_attr = mode << 16
            with path.open("rb") as fh, zf.open(info, "w") as out_fh:
                shutil.copyfileobj(fh, out_fh)


def _stage_local_bundle(
    root: Path,
    exe: Path,
    stage: Path,
    *,
    log: LogFn | None = None,
) -> str:
    """Built-in stager used when the repo has no scripts/package_release.sh.

    Mirrors that script's payload: exe + assets/{fonts,img} + bundled OpenBIOS
    + game.toml / VERSION. Never stages a disc image or retail BIOS dump.
    """
    exe_dir = exe.parent
    _stage_copy(exe, stage / exe.name)

    # Runtime shared libs sitting next to the exe (dynamic builds).
    for pattern in ("*.dll", "*.DLL", "*.so", "*.so.*", "*.dylib"):
        for lib in sorted(exe_dir.glob(pattern)):
            if lib.is_file():
                _stage_copy(lib, stage / lib.name)

    staged_assets = False
    for sub in ("fonts", "img"):
        src = exe_dir / "assets" / sub
        if src.is_dir():
            _stage_copy(src, stage / "assets" / sub)
            staged_assets = True
    if not staged_assets:
        return f"assets/fonts + assets/img missing next to {exe.name} — rebuild first"

    bios_bin = ""
    for cand in (
        exe_dir / "bios" / "openbios.bin",
        root / "psxrecomp" / "bios" / "openbios.bin",
        root / "bios" / "openbios.bin",
    ):
        if cand.is_file():
            _stage_copy(cand, stage / "bios" / "openbios.bin")
            bios_bin = str(cand)
            break
    if not bios_bin:
        return "bios/openbios.bin not found — rebuild psx-runtime to stage it"
    for cand in (
        exe_dir / "bios" / "OpenBIOS.LICENSE",
        root / "psxrecomp" / "bios" / "OpenBIOS.LICENSE",
    ):
        if cand.is_file():
            _stage_copy(cand, stage / "bios" / "OpenBIOS.LICENSE")
            break

    for name in ("game.toml", "VERSION", "README-SETUP.txt", "keybinds.ini"):
        src = root / name
        if src.is_file():
            _stage_copy(src, stage / name)

    # Exe-relative runtime data the build staged next to the binary. The
    # runtime resolves these from the EXE's own directory (mods via
    # <exe_dir>/mods), so a zip without them ships a game whose Mods page is
    # empty and whose netplay lobbies can never agree on a mod plan.
    # mods/state.toml is the packaging machine's own enable/disable state —
    # preloaded catalogs ship default-disabled, so it must never travel.
    staged_extra = []
    for sub in ("mods", "bezels"):
        src = exe_dir / sub
        if src.is_dir():
            _stage_copy(src, stage / sub)
            staged_extra.append(sub)
    for leftover in ("state.toml", "state.toml.tmp"):
        stale = stage / "mods" / leftover
        if stale.is_file():
            stale.unlink()

    # Project-root data the runtime reads relative to the project (not the
    # exe): translations, and the mods/preloaded source a rebuild restages
    # the catalog from.
    for sub in ("translations",):
        src = root / sub
        if src.is_dir():
            _stage_copy(src, stage / sub)
            staged_extra.append(sub)

    _flush_log(
        log,
        f"    staged {exe.name} + assets + bios (OpenBIOS: {bios_bin})"
        + (f" + {', '.join(staged_extra)}" if staged_extra else ""),
    )
    return ""


def package_local(
    root: Path,
    *,
    build_dir: str = DEFAULT_BUILD_DIR,
    artifact_tag: str = "",
    exe: Path | None = None,
    use_repo_script: bool = True,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> PackageResult:
    """Zip the existing local build into ``<root>/dist``.

    Prefers the repo's own ``scripts/package_release.sh`` (CI-parity payload
    and zip name) when it exists and bash is available; otherwise stages an
    equivalent bundle in Python so this works on Windows too.
    """
    root = root.expanduser().resolve()
    bdir = resolve_build_dir(root, build_dir)
    if not bdir.is_dir():
        return PackageResult(False, f"Build dir missing — Build first: {bdir}")

    binary = exe.expanduser().resolve() if exe else find_runtime_exe(bdir)
    if binary is None or not binary.is_file():
        return PackageResult(False, f"No runtime executable under {bdir} — Build first")

    tag = artifact_tag.strip() or host_artifact_tag()
    version = project_version(root)
    dist = root / "dist"
    script = root / "scripts" / "package_release.sh"
    bash = shutil.which("bash")
    use_script = use_repo_script and script.is_file() and bool(bash)

    if dry_run:
        how = f"{script.name} {bdir.name} {tag}" if use_script else f"built-in stager ({tag})"
        msg = f"dry-run: package {binary.name} via {how} → {dist}"
        _flush_log(log, msg)
        return PackageResult(True, msg)

    if platforms.current().key == "snes" and not use_script:
        # The built-in stager bundles OpenBIOS, game.toml and the psxrecomp
        # mods/bezels layout — none of which a SNES port has. Its own packager
        # is the only correct answer, and it also enforces the no-ROM-bytes
        # rule that a generic stager would not.
        return PackageResult(
            False,
            "No scripts/package_release.sh in this repo — emit it from the "
            "Migrate tab (snes_emit_packager) before packaging.",
        )

    if use_script:
        _flush_log(log, f"==> package {tag} via scripts/package_release.sh")
        if platforms.current().key == "snes":
            # snesrecomp's packager is configured by environment, not argv:
            # positional arguments would be silently ignored and it would
            # package whatever is in ./build.
            env = os.environ.copy()
            env["BUILD_DIR"] = str(bdir)
            env["PLATFORM"] = tag
            r = _run_stream([bash, str(script)], root, log=log, env=env)
        else:
            r = _run_stream([bash, str(script), str(bdir), tag], root, log=log)
        if not r.ok:
            return PackageResult(False, f"package_release.sh failed for {tag}", r.detail)
        zip_path = _newest_zip(dist, tag)
        if zip_path is None:
            return PackageResult(False, f"package_release.sh produced no zip under {dist}", r.detail)
        return PackageResult(True, f"Packaged {zip_path.name}", r.detail, zip_path)

    _flush_log(log, f"==> package {tag} (built-in stager)")
    stage = dist / f"stage-local-{tag}"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    try:
        err = _stage_local_bundle(root, binary, stage, log=log)
        if err:
            return PackageResult(False, err)
        zip_path = dist / f"{_zip_prefix(root)}-{version}-{tag}.zip"
        _write_zip(stage, zip_path)
    except OSError as exc:
        return PackageResult(False, f"Packaging failed: {exc}")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    size_mb = zip_path.stat().st_size / (1024.0 * 1024.0)
    _flush_log(log, f"Wrote {zip_path} ({size_mb:.1f} MiB)")
    return PackageResult(True, f"Packaged {zip_path.name}", "", zip_path.resolve())
