"""Audit / plan / apply for a SNES title repo.

The PSX migration in ``ops.py`` is not reusable here and should not be forced
to be: it is almost entirely about ``game.toml``, disc probing, BIOS backends
and a CMake rewrite that has no SNES counterpart. What the two consoles share
is the *shape* — audit produces checks, checks name fix ops, plan orders them,
apply runs them — so this module reimplements the shape and nothing else.

Every op here is either idempotent or a no-op. Anything that cannot be derived
from the repository as it stands (ROM digests, above all) is reported as a
warning with no fix op rather than guessed at: a regen.sh carrying invented
digests would verify a ROM nobody owns and fail at the least useful moment.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import snes_paths
from .models import (
    ApplyResult,
    AuditReport,
    CheckResult,
    CheckStatus,
    LayoutClass,
    MigrateOptions,
    Plan,
    PlanStep,
    Severity,
)

# Ordered for apply: submodules first (later ops read their contents), pins
# last (they record what everything above settled on).
OP_ORDER: tuple[str, ...] = (
    "snes_repair_framework_submodule",
    "snes_ensure_framework_submodule",
    "snes_ensure_recomp_ui_submodule",
    "snes_ensure_nested_modules",
    "snes_enable_netplay",
    "snes_disable_netplay",
    "snes_merge_gitignore",
    "snes_untrack_generated",
    "snes_ensure_src_gen",
    "snes_emit_version",
    "snes_probe_rom_refresh",
    "snes_emit_rom_identity",
    "snes_emit_codegen_setup",
    "snes_emit_regen",
    "snes_relocate_boxart",
    "snes_emit_boxart_stub",
    "snes_emit_packager",
    "snes_emit_ci_workflow",
    "snes_patch_readme_metrics",
    "snes_record_framework_pins",
)

OP_TITLES: dict[str, str] = {
    "snes_ensure_framework_submodule": "Add snesrecomp submodule",
    "snes_ensure_recomp_ui_submodule": "Add recomp-ui submodule",
    "snes_ensure_nested_modules": "Init lib/recomp-net + lib/retcomm-rbengine",
    "snes_merge_gitignore": "Merge SNES .gitignore rules (src/gen, *.sfc)",
    "snes_untrack_generated": "Untrack committed generated C / funcs.h",
    "snes_ensure_src_gen": "Create src/gen/.gitkeep",
    "snes_emit_version": "Emit VERSION",
    "snes_emit_regen": "Emit tools/regen.sh",
    "snes_emit_packager": "Emit scripts/package_release.sh",
    "snes_emit_ci_workflow": "Emit .github/workflows/release.yml",
    "snes_record_framework_pins": "Write framework_pins.txt",
    "snes_repair_framework_submodule": "Repair broken snesrecomp/ git checkout",
    "snes_probe_rom_refresh": "Refresh ROM identity via probe_rom.py",
    "snes_emit_rom_identity": "Emit rom_identity.txt",
    "snes_emit_codegen_setup": "Emit src/codegen_setup.c / .h",
    "snes_relocate_boxart": "Relocate boxart → launcher_assets/img/",
    "snes_emit_boxart_stub": "Create launcher_assets/img stub dir",
    "snes_patch_readme_metrics":
        "Patch README badges, RetComM Launcher, and R.A.I.D. footer",
    "snes_enable_netplay": "Wire netplay (snesrecomp_enable_recomp_net)",
    "snes_disable_netplay": "Unwire netplay (comment the call out)",
}

# Matches snesrecomp's tools/new_project/templates/gitignore.in. The launcher
# entries are not cosmetic: a port that wires the recomp-ui launcher gets
# rom.cfg / keybinds.ini / config.ini written beside the executable — and in
# the repo root whenever the game is run from there — which shows up as
# untracked files nobody meant to commit.
GITIGNORE_RULES: tuple[str, ...] = (
    "/src/gen/",
    "/recomp/funcs.h",
    "*.sfc",
    "*.smc",
    "*.srm",
    "/build/",
    "/build-*/",
    "/dist/",
    "/saves/",
    "/rom.cfg",
    "/keybinds.ini",
    "/config.ini",
    "/input.ini",
    # Capture bundles from tools/snes_analysis embed ROM-derived
    # VRAM/CGRAM dumps — evidence, not source.
    "/analysis/",
)

FRAMEWORK = "snesrecomp"
NESTED_PATHS = ("lib/recomp-net", "lib/retcomm-rbengine")

# ---------------------------------------------------------------------------
# ROM identity carriers
# ---------------------------------------------------------------------------
# Which file carries ROM identity is the framework's call, not Studio's, and it
# changed: snesrecomp now scaffolds a single ``rom_identity.txt`` that CMake
# turns into snesrecomp_rom_identity.h and that tools/regen.sh and the release
# workflow read directly, replacing the src/codegen_setup.c/.h pair older
# revisions emitted. Studio drives whichever wizard the port is *pinned* to
# (see snes_paths), so it has to serve both eras — and the templates present in
# that wizard are the only honest signal of which one this is. Hardcoding
# either set is what made a migration against a current checkout die with
# "Template not found: .../codegen_setup.c.in".
IDENTITY_FILE = "rom_identity.txt"

_IDENTITY_CARRIERS: dict[str, tuple[tuple[str, str], ...]] = {
    "file": (("rom_identity.txt.in", IDENTITY_FILE),),
    "codegen": (
        ("codegen_setup.c.in", "src/codegen_setup.c"),
        ("codegen_setup.h.in", "src/codegen_setup.h"),
    ),
}

_IDENTITY_OPS: dict[str, str] = {
    "file": "snes_emit_rom_identity",
    "codegen": "snes_emit_codegen_setup",
}


def identity_layout(game_root: Path | str | None = None) -> str:
    """``"file"``, ``"codegen"``, or ``""`` when the wizard offers neither."""
    tdir = snes_paths.templates_dir(game_root)
    if (tdir / "rom_identity.txt.in").is_file():
        return "file"
    if (tdir / "codegen_setup.c.in").is_file():
        return "codegen"
    return ""


def identity_carriers(game_root: Path | str | None = None) -> tuple[tuple[str, str], ...]:
    """``(template, repo-relative path)`` pairs this wizard scaffolds."""
    return _IDENTITY_CARRIERS.get(identity_layout(game_root), ())


def list_ops() -> list[str]:
    return list(OP_ORDER)


# ---------------------------------------------------------------------------
# Reading the repo
# ---------------------------------------------------------------------------
def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return 1, ""
    return proc.returncode, (proc.stdout or "").strip()


def _is_git_repo(root: Path) -> bool:
    code, out = _git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out == "true"


_PROJECT_RE = re.compile(r"^\s*project\s*\(\s*([A-Za-z0-9_.+-]+)", re.MULTILINE)


def project_name(root: Path) -> str:
    cml = root / "CMakeLists.txt"
    if cml.is_file():
        try:
            m = _PROJECT_RE.search(cml.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            m = None
        if m:
            return m.group(1)
    return root.name


def display_name(root: Path) -> str:
    """Title from README's first heading, else the CMake project name."""
    readme = root / "README.md"
    if readme.is_file():
        try:
            for line in readme.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("# "):
                    return line[2:].strip() or project_name(root)
        except OSError:
            pass
    return project_name(root)


def _zip_prefix(root: Path) -> str:
    """Reuse the existing packager's prefix so a re-emit keeps asset names."""
    pkg = root / "scripts" / "package_release.sh"
    if pkg.is_file():
        try:
            m = re.search(
                r'^ZIP_PREFIX="?\$\{ZIP_PREFIX:-([^}"]+)\}"?',
                pkg.read_text(encoding="utf-8", errors="replace"),
                re.MULTILINE,
            )
        except OSError:
            m = None
        if m:
            return m.group(1).strip()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", project_name(root)).strip("-")
    return slug or "game"


def default_branch(root: Path) -> str:
    code, out = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if code == 0 and out and out != "HEAD":
        return out
    return "main"


_DIGEST_PATTERNS = (
    # tools/regen.sh, written by the same wizard we are re-emitting from.
    (re.compile(r'EXPECTED_CRC32="?\$\{SNESRECOMP_EXPECTED_CRC32:-([0-9a-fA-Fx]+)'), "crc32"),
    (re.compile(r'EXPECTED_SHA256="?\$\{SNESRECOMP_EXPECTED_SHA256:-([0-9a-fA-F]+)'), "sha256"),
)


def parse_identity_file(path: Path) -> dict[str, str]:
    """``key = value`` lines, ``#`` comments, optionally quoted values.

    Deliberately mirrors the sed in regen.sh's ``identity_get`` rather than
    being stricter: only a line that *starts* with ``#`` is a comment, the
    first occurrence of a key wins, trailing whitespace goes, surrounding
    double quotes come off. Studio agreeing with the file's actual consumer
    matters more than agreeing with a tidier grammar. Values still carrying an
    unfilled ``@TOKEN@`` are dropped, not recovered.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        if key and val and "@" not in val:
            out.setdefault(key, val)
    return out


def _carrier_label(game_root: Path | str | None = None) -> str:
    """What to call the identity carrier in a message, for this wizard."""
    carriers = identity_carriers(game_root)
    return " / ".join(rel for _, rel in carriers) or "the identity carrier"


def _identity_source(root: Path) -> str:
    """The file a recovered digest actually came from, for the audit detail."""
    for rel in (IDENTITY_FILE, "src/codegen_setup.c"):
        if (root / rel).is_file():
            return rel
    return "tools/regen.sh"


def rom_identity(root: Path, rom: str | None = None) -> dict[str, str]:
    """ROM tokens for the templates: probed if a ROM is given, else recovered.

    Recovery reads what a previous scaffold already baked in. That is the whole
    reason it is safe to re-emit these files: the digests are not re-derived,
    they are carried across unchanged.
    """
    out: dict[str, str] = {}
    if rom:
        probe = snes_paths.probe_rom_script(root)
        rom_path = Path(rom).expanduser()
        if probe.is_file() and rom_path.is_file():
            code, text = _run_probe(probe, rom_path)
            if code == 0 and text:
                import json

                try:
                    data = json.loads(text)
                except ValueError:
                    data = {}
                for key, token in (
                    ("crc32", "crc32"),
                    ("sha256", "sha256"),
                    ("display_name", "display_name"),
                    ("mapping", "mapping"),
                    ("region", "region"),
                ):
                    val = str(data.get(key) or "").strip()
                    if val:
                        out[token] = val
                out["rom_file"] = rom_path.name
        if "rom_file" not in out:
            out["rom_file"] = rom_path.name
        return out

    # rom_identity.txt first: it is the current carrier, it holds mapping and
    # region alongside the digests, and it is the very file the build and
    # regen.sh read — so what Studio recovers is what the port actually uses.
    # (Under this layout regen.sh no longer bakes digests in at all, it calls
    # identity_get, so _DIGEST_PATTERNS below has nothing to fall back on.)
    ident_file = root / IDENTITY_FILE
    if ident_file.is_file():
        data = parse_identity_file(ident_file)
        for field, key in (
            ("display_name", "display_name"),
            ("rom_file", "rom_file"),
            ("expected_crc32", "crc32"),
            ("expected_sha256", "sha256"),
            ("mapping", "mapping"),
            ("region", "region"),
        ):
            if data.get(field):
                out.setdefault(key, data[field])

    # codegen_setup.c is the same thing for a port pinned to an older
    # framework: mapping and region alongside the digests, which regen.sh
    # does not carry.
    cg = root / "src" / "codegen_setup.c"
    if cg.is_file():
        try:
            text = cg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for field, key in (
            ("display_name", "display_name"),
            ("rom_file", "rom_file"),
            ("expected_crc32", "crc32"),
            ("expected_sha256", "sha256"),
            ("mapping", "mapping"),
            ("region", "region"),
        ):
            m = re.search(r"\." + field + r"\s*=\s*\"([^\"]*)\"", text)
            if m and m.group(1) and "@" not in m.group(1):
                out.setdefault(key, m.group(1))

    regen = root / "tools" / "regen.sh"
    if regen.is_file():
        try:
            text = regen.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for pattern, key in _DIGEST_PATTERNS:
            m = pattern.search(text)
            if m:
                out.setdefault(key, m.group(1))
        m = re.search(r'for cand in "([^"]+\.s[fm]c)"', text)
        if m:
            out.setdefault("rom_file", m.group(1))
    return out


def _run_probe(probe: Path, rom: Path) -> tuple[int, str]:
    """Run probe_rom.py and return its JSON.

    The wizard's probe writes JSON to a file (``--json-out``) and prints a
    human summary to stdout — there is no ``--json`` that puts it on stdout, so
    reading stdout would parse the summary and silently find no digests.
    """
    # sys.executable before the PATH lookups, as analyzeops/buildops already
    # do. On Windows "python3"/"python" resolve to the WindowsApps App
    # Execution Alias, which is a stub that exits 9009 with a "install from
    # the Store" notice and writes no --json-out -- so the probe reported no
    # CRC32/SHA256 and the ROM looked unreadable. The interpreter already
    # running this toolkit is by definition a working one.
    python = (
        os.environ.get("PYTHON")
        or sys.executable
        or shutil.which("python3")
        or shutil.which("python")
    )
    if not python:
        return 1, ""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "probe.json"
        try:
            proc = subprocess.run(
                [python, str(probe), str(rom), "--json-out", str(out), "--quiet"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return 1, ""
        if proc.returncode != 0 or not out.is_file():
            return proc.returncode or 1, ""
        try:
            return 0, out.read_text(encoding="utf-8")
        except OSError:
            return 1, ""


def _tracked(root: Path, pathspec: str) -> list[str]:
    code, out = _git(root, "ls-files", "--", pathspec)
    if code != 0 or not out:
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def _submodule_present(root: Path, path: str, marker: str = "") -> tuple[bool, bool]:
    """(declared in .gitmodules, initialised on disk)."""
    declared = False
    gm = root / ".gitmodules"
    if gm.is_file():
        try:
            declared = f"path = {path}" in gm.read_text(encoding="utf-8", errors="replace")
        except OSError:
            declared = False
    sub = root / path
    live = sub.is_dir() and any(sub.iterdir())
    if live and marker:
        live = (sub / marker).is_file()
    return declared, live


def diagnose_framework_checkout(root: Path) -> str | None:
    """A human reason when snesrecomp/ has the marker but git cannot use it.

    Same failure family the PSX audit repairs: a .git file pointing at a
    deleted gitdir (renamed submodule), or a tree absorbed into the parent.
    Returns None when the checkout is healthy or simply absent.
    """
    dest = root / FRAMEWORK
    if not dest.is_dir() or not (dest / "runner" / "runner.cmake").is_file():
        return None
    code, out = _git(dest, "rev-parse", "--is-inside-work-tree")
    if code == 0 and out.strip() == "true":
        return None
    git_file = dest / ".git"
    if git_file.is_file():
        try:
            text = git_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        if text.lower().startswith("gitdir:"):
            rel = text.split(":", 1)[1].strip()
            target = (dest / rel).resolve() if rel else None
            if target is not None and not target.exists():
                return (f"{FRAMEWORK}/.git points at missing gitdir ({rel}) — "
                        "broken submodule metadata.")
            return f"{FRAMEWORK}/.git gitdir is unusable ({rel or text})."
    if git_file.is_dir():
        return f"{FRAMEWORK}/.git exists but git rev-parse fails."
    return (f"{FRAMEWORK}/ has runner/runner.cmake but is not a git checkout "
            "(absorbed into the parent tree or missing .git).")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
def _audit_readme(root: Path, add: Callable[..., None]) -> None:
    """The README / GitHub About row.

    Split out so the switch that gates it reads as a switch. One row and one op
    cover both because ``snes_patch_readme_metrics`` writes both: the badges,
    boxart, launcher and R.A.I.D. blocks in README.md, and the repository's
    About blurb over the GitHub API.
    """
    from .readme_metrics import (
        boxart_png_present,
        readme_has_boxart,
        readme_has_launcher,
        readme_has_metrics,
        readme_has_raid,
    )
    readme_path = root / "README.md"
    try:
        readme_text = readme_path.read_text(encoding="utf-8", errors="replace") \
            if readme_path.is_file() else ""
    except OSError:
        readme_text = ""
    missing_readme: list[str] = []
    if not readme_path.is_file():
        missing_readme.append("README.md")
    else:
        if not readme_has_metrics(readme_text):
            missing_readme.append("download badges")
        if not readme_has_boxart(readme_text):
            missing_readme.append("libretro boxart")
        if not readme_has_launcher(readme_text):
            missing_readme.append("RetComM Launcher section")
        if not readme_has_raid(readme_text):
            missing_readme.append("R.A.I.D. Discord footer")
    if not (root / ".github" / "raid-discord.png").is_file():
        missing_readme.append(".github/raid-discord.png")
    if not boxart_png_present(root):
        missing_readme.append("launcher_assets/img/boxart.png")
    if missing_readme:
        add("readme_metrics", "README download metrics / launcher / RAID / boxart",
            CheckStatus.WARN, Severity.RECOMMENDED,
            "Missing: " + ", ".join(missing_readme), "snes_patch_readme_metrics")
    else:
        add("readme_metrics", "README download metrics / launcher / RAID / boxart",
            CheckStatus.PASS, Severity.RECOMMENDED,
            "Badges, boxart, RetComM Launcher, and R.A.I.D. footer present.")


def audit_project(root: Path, options: MigrateOptions | None = None) -> AuditReport:
    options = options or MigrateOptions()
    root = Path(root).expanduser().resolve()
    checks: list[CheckResult] = []
    notes: list[str] = []

    def add(
        cid: str,
        title: str,
        status: CheckStatus,
        severity: Severity,
        detail: str = "",
        fix_op: str | None = None,
    ) -> None:
        checks.append(
            CheckResult(
                id=cid, title=title, status=status, severity=severity, detail=detail,
                fix_op=fix_op,
            )
        )

    is_git = _is_git_repo(root)
    if not is_git:
        notes.append("Not a git repository — submodule and untrack ops cannot run.")

    # --- framework -----------------------------------------------------------
    declared, live = _submodule_present(root, FRAMEWORK, "runner/runner.cmake")
    broken = diagnose_framework_checkout(root) if live else None
    if live and broken:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.FAIL, Severity.REQUIRED,
            broken + " Repair re-clones it as a real submodule.",
            "snes_repair_framework_submodule")
    elif live:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.PASS, Severity.REQUIRED,
            str(root / FRAMEWORK))
    elif declared:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.FAIL, Severity.REQUIRED,
            "Declared in .gitmodules but not initialised (or missing runner/runner.cmake).",
            "snes_ensure_framework_submodule")
    else:
        add("framework", f"{FRAMEWORK}/ checkout", CheckStatus.FAIL, Severity.REQUIRED,
            "No snesrecomp submodule — the project cannot generate or build.",
            "snes_ensure_framework_submodule")

    ui_declared, ui_live = _submodule_present(root, "recomp-ui")
    if ui_live:
        add("recomp_ui", "recomp-ui/ checkout", CheckStatus.PASS, Severity.RECOMMENDED, "")
    else:
        add("recomp_ui", "recomp-ui/ checkout",
            CheckStatus.WARN, Severity.RECOMMENDED,
            "Declared but not initialised." if ui_declared else "Not present (optional).",
            "snes_ensure_recomp_ui_submodule")

    fw = root / FRAMEWORK
    missing_nested = [
        p for p in NESTED_PATHS
        if (fw / p).is_dir() and not any((fw / p).iterdir())
    ]
    if not live:
        add("nested", "Nested libs inside snesrecomp", CheckStatus.SKIP, Severity.OPTIONAL,
            "Needs the framework checkout first.")
    elif missing_nested:
        add("nested", "Nested libs inside snesrecomp", CheckStatus.WARN, Severity.OPTIONAL,
            "Uninitialised: " + ", ".join(missing_nested), "snes_ensure_nested_modules")
    else:
        add("nested", "Nested libs inside snesrecomp", CheckStatus.PASS, Severity.OPTIONAL, "")

    # --- analysis config -----------------------------------------------------
    recomp = root / "recomp"
    cfgs = sorted(recomp.glob("bank*.cfg")) if recomp.is_dir() else []
    if cfgs and (recomp / "symbols.toml").is_file():
        add("analysis", "recomp/ analysis config", CheckStatus.PASS, Severity.REQUIRED,
            f"{len(cfgs)} bank cfg(s) + symbols.toml")
    else:
        # No fix op: seeding these needs the ROM, and a blank seed would look
        # like analysis input while proving nothing.
        add("analysis", "recomp/ analysis config", CheckStatus.FAIL, Severity.REQUIRED,
            "Missing bank*.cfg / symbols.toml. Seed them with the wizard's "
            "probe_rom.py --write-seed-cfg --write-symbols against your ROM.")

    # --- generated output ----------------------------------------------------
    gen_tracked = _tracked(root, "src/gen") + _tracked(root, "recomp/funcs.h")
    if not is_git:
        add("generated", "Generated C not committed", CheckStatus.SKIP, Severity.REQUIRED,
            "Not a git repository.")
    elif gen_tracked:
        add("generated", "Generated C not committed", CheckStatus.FAIL, Severity.REQUIRED,
            f"{len(gen_tracked)} tracked file(s) derived from the ROM.",
            "snes_untrack_generated")
    else:
        add("generated", "Generated C not committed", CheckStatus.PASS, Severity.REQUIRED, "")

    if (root / "src" / "gen").is_dir():
        add("src_gen", "src/gen/ present", CheckStatus.PASS, Severity.OPTIONAL, "")
    else:
        add("src_gen", "src/gen/ present", CheckStatus.WARN, Severity.OPTIONAL,
            "Generator output directory missing.", "snes_ensure_src_gen")

    # --- .gitignore ----------------------------------------------------------
    gi = root / ".gitignore"
    text = ""
    if gi.is_file():
        try:
            text = gi.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    existing = {ln.strip() for ln in text.splitlines()}
    missing_rules = [r for r in GITIGNORE_RULES if r not in existing]
    if not missing_rules:
        add("gitignore", ".gitignore covers ROM + generated C", CheckStatus.PASS,
            Severity.REQUIRED, "")
    else:
        add("gitignore", ".gitignore covers ROM + generated C", CheckStatus.FAIL,
            Severity.REQUIRED, "Missing: " + ", ".join(missing_rules), "snes_merge_gitignore")

    # --- scaffold files ------------------------------------------------------
    for cid, rel, op, sev in (
        ("version", "VERSION", "snes_emit_version", Severity.REQUIRED),
        ("regen", "tools/regen.sh", "snes_emit_regen", Severity.REQUIRED),
        ("packager", "scripts/package_release.sh", "snes_emit_packager", Severity.RECOMMENDED),
        ("ci", ".github/workflows/release.yml", "snes_emit_ci_workflow", Severity.RECOMMENDED),
    ):
        if (root / rel).is_file():
            add(cid, rel, CheckStatus.PASS, sev, "")
            continue
        blocked = op == "snes_emit_regen" and not rom_identity(root)
        add(
            cid,
            rel,
            CheckStatus.FAIL,
            sev,
            "Missing — ROM digests unknown, so it cannot be emitted without a "
            "--disc ROM path." if blocked else "Missing.",
            None if blocked else op,
        )

    # --- regen.sh vs the framework this port is pinned to ---------------------
    # A fork carries whatever gitlink its parent recorded, which may be far
    # older than the wizard Studio is driving. Say so on the Migrate tab rather
    # than letting it surface as an argparse error from the Build tab.
    regen_path = root / "tools" / "regen.sh"
    if regen_path.is_file():
        try:
            regen_text = regen_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            regen_text = ""
        gap = snes_paths.regen_framework_gap(root, regen_text) if regen_text else None
        if gap is not None:
            missing, have = gap
            # No fix op: moving a framework pin is a decision about what this
            # port is measured against, and that belongs to a human.
            add("regen_framework", "tools/regen.sh vs pinned snesrecomp",
                CheckStatus.FAIL, Severity.REQUIRED,
                f"regen.sh calls {', '.join(missing)}, which this port's "
                f"snesrecomp does not have (it offers {', '.join(sorted(have))}). "
                "Generate cannot work until the submodule moves to a revision "
                "that has them.")
        elif regen_text:
            add("regen_framework", "tools/regen.sh vs pinned snesrecomp",
                CheckStatus.PASS, Severity.REQUIRED,
                "The pinned snesrecomp offers everything regen.sh calls.")

    # --- which wizard is being driven ----------------------------------------
    # Provenance, because every emitted file inherits it. Silent until now, and
    # a fallback to the vendored copy is precisely what lets Studio write files
    # the port's own framework has never heard of.
    own_wizard = root / FRAMEWORK / "tools" / "new_project" / "setup_project.sh"
    if not own_wizard.is_file():
        add("wizard_source", "Scaffold templates in use", CheckStatus.WARN,
            Severity.INFO,
            f"{FRAMEWORK}/tools/new_project is absent, so Studio is driving "
            f"{snes_paths.wizard_dir(root)} instead — a different revision from "
            "the one this port builds against.")
    else:
        add("wizard_source", "Scaffold templates in use", CheckStatus.PASS,
            Severity.INFO, f"{snes_paths.wizard_source(root)}.")

    # --- ROM / catalog identity ---------------------------------------------
    ident = rom_identity(root)
    if ident.get("sha256") and ident.get("crc32"):
        add("rom_identity", "ROM identity (digests)", CheckStatus.PASS,
            Severity.RECOMMENDED,
            f"crc32 {ident['crc32']} recovered from {_identity_source(root)}.")
    else:
        # Refresh needs the ROM (--disc) — the plan gates the op on it.
        add("rom_identity", "ROM identity (digests)", CheckStatus.WARN,
            Severity.RECOMMENDED,
            "No ROM digests recoverable — probe with a ROM path to seed "
            f"regen.sh / {_carrier_label(root)}.", "snes_probe_rom_refresh")

    # --- ROM identity carrier -------------------------------------------------
    # Audit whichever carrier the wizard this port is pinned to actually
    # scaffolds, rather than assuming either era (see IDENTITY_FILE above).
    layout = identity_layout(root)
    carriers = identity_carriers(root)
    blocked_id = not (ident.get("sha256") and ident.get("crc32"))
    if not carriers:
        # A wizard that offers neither template is a broken tool, and saying so
        # beats emitting a FAIL whose fix op would die on a missing template.
        add("identity_carrier", "ROM identity carrier", CheckStatus.WARN,
            Severity.REQUIRED,
            f"{snes_paths.templates_dir(root)} has neither rom_identity.txt.in "
            "nor codegen_setup.c.in — cannot tell which carrier this framework "
            "revision wants.")
    else:
        title = " / ".join(rel for _, rel in carriers)
        emit_op = _IDENTITY_OPS[layout]
        present = [root / rel for _, rel in carriers]
        primary = present[0]
        if all(pth.is_file() for pth in present):
            try:
                text = primary.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if layout == "codegen" and "kGameCodegenIdentity" not in text:
                add("identity_carrier", title, CheckStatus.WARN, Severity.REQUIRED,
                    "codegen_setup.c missing kGameCodegenIdentity.", emit_op)
            elif re.search(r"@[A-Z0-9_]+@", text):
                add("identity_carrier", title, CheckStatus.WARN, Severity.REQUIRED,
                    f"{primary.name} still has unfilled @TOKEN@ placeholders.",
                    None if blocked_id else emit_op)
            elif layout == "file" and not all(
                parse_identity_file(primary).get(k)
                for k in ("expected_crc32", "expected_sha256")
            ):
                # An empty digest is not a formatting nit: the build says so and
                # then cannot verify the ROM it is handed.
                add("identity_carrier", title, CheckStatus.WARN, Severity.REQUIRED,
                    "rom_identity.txt carries no digests, so the build cannot "
                    "verify a ROM.", None if blocked_id else emit_op)
            else:
                add("identity_carrier", title, CheckStatus.PASS,
                    Severity.REQUIRED, "Identity present with digests.")
        else:
            add("identity_carrier", title, CheckStatus.FAIL, Severity.REQUIRED,
                "Missing — ROM digests unknown, so it cannot be emitted without "
                "a --disc ROM path." if blocked_id
                else "Missing identity carrier.",
                None if blocked_id else emit_op)

    # A port that predates the move keeps dead codegen_setup sources: this
    # framework's CMakeLists no longer compiles them, so their digests are no
    # longer the ones the build reads — two identities, one of them a lie.
    # Named, not auto-fixed: deleting a tracked source is a human's call.
    if layout == "file" and (root / "src" / "codegen_setup.c").is_file():
        add("identity_stale", "src/codegen_setup.c (superseded)", CheckStatus.WARN,
            Severity.OPTIONAL,
            "This framework revision reads rom_identity.txt and no longer "
            "compiles codegen_setup.c — remove it once rom_identity.txt is in "
            "place.")

    # --- boxart ---------------------------------------------------------------
    modern_box = root / "launcher_assets" / "img"
    modern_hit = next(
        (p for p in (modern_box / "boxart.tga", modern_box / "boxart.png")
         if p.is_file()), None)
    legacy_candidates = [
        root / "assets" / "boxart.tga", root / "assets" / "boxart.png",
        root / "boxart.tga", root / "boxart.png",
    ]
    legacy_box = next((p for p in legacy_candidates if p.is_file()), None)
    if modern_hit is not None:
        add("boxart", "launcher_assets boxart", CheckStatus.PASS, Severity.OPTIONAL,
            str(modern_hit.relative_to(root)))
    elif legacy_box is not None:
        add("boxart", "launcher_assets boxart", CheckStatus.WARN, Severity.OPTIONAL,
            f"Boxart at {legacy_box.relative_to(root)} — relocate to "
            "launcher_assets/img/.", "snes_relocate_boxart")
    else:
        add("boxart", "launcher_assets boxart", CheckStatus.WARN, Severity.OPTIONAL,
            "No boxart found (optional; README patch can fetch libretro art).",
            "snes_emit_boxart_stub")

    # --- README metrics / launcher / RAID ------------------------------------
    # Reported as skipped rather than dropped: a row that silently disappears
    # reads as "nothing to do here", which is the opposite of what the switch
    # means. SKIP carries no fix op, so it never reaches failing_ops() and the
    # layout classification below stops counting it as a recommended warning.
    if not options.patch_readme:
        add("readme_metrics", "README download metrics / launcher / RAID / boxart",
            CheckStatus.SKIP, Severity.INFO,
            "Skipped — README & About is off for this repo.")
    else:
        _audit_readme(root, add)

    # --- netplay wiring -------------------------------------------------------
    # Report-only: netplay is opt-in, so the plan adds the enable/disable op
    # from --enable-netplay / --disable-netplay, never from a failing check.
    cml = root / "CMakeLists.txt"
    cml_text = ""
    if cml.is_file():
        try:
            cml_text = cml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            cml_text = ""
    if _netplay_wired(cml_text):
        add("netplay", "Netplay (recomp-net) wiring", CheckStatus.PASS,
            Severity.INFO,
            "snesrecomp_enable_recomp_net() wired in CMakeLists.txt.")
    elif cml_text:
        add("netplay", "Netplay (recomp-net) wiring", CheckStatus.WARN,
            Severity.INFO,
            "Not wired — apply with --enable-netplay to add the "
            "snesrecomp_enable_recomp_net() call (host code must also wire "
            "the launcher; see MetalWarriorsSNESRecomp src/main.c).")

    # --- lobby pin stamp vs VERSION ------------------------------------------
    # Fires only when a build tree carries a version stamp; drift between the
    # stamp and VERSION splits netplay lobbies onto different pins.
    ver_path = root / "VERSION"
    ver_text = ""
    if ver_path.is_file():
        try:
            ver_text = ver_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            ver_text = ""
    stamp_hits: list[tuple[str, str]] = []
    for build in sorted(root.glob("build*")):
        if not build.is_dir():
            continue
        for stamp in (build / "snes_game_version.txt",
                      build / "Release" / "snes_game_version.txt"):
            if stamp.is_file():
                try:
                    stamp_hits.append(
                        (str(stamp.relative_to(root)).replace("\\", "/"),
                         stamp.read_text(encoding="utf-8",
                                         errors="replace").strip()))
                except OSError:
                    pass
    if ver_text and stamp_hits:
        bad = [(pth, st) for pth, st in stamp_hits
               if st and st.lstrip("vV") != ver_text.lstrip("vV")]
        if bad:
            detail = "; ".join(f"{pth}={st} (VERSION={ver_text})"
                               for pth, st in bad[:3])
            add("version_stamp_match", "Lobby pin stamp", CheckStatus.FAIL,
                Severity.REQUIRED,
                "snes_game_version.txt disagrees with VERSION — rebuild with "
                "-DSNES_GAME_VERSION matching VERSION before releasing. "
                + detail)
        else:
            add("version_stamp_match", "Lobby pin stamp", CheckStatus.PASS,
                Severity.RECOMMENDED, "Build stamp matches VERSION.")

    pins = root / "framework_pins.txt"
    if pins.is_file():
        stale = _stale_pins(root, pins)
        if stale:
            add("pins", "framework_pins.txt matches gitlinks", CheckStatus.WARN,
                Severity.RECOMMENDED, "Stale: " + ", ".join(stale),
                "snes_record_framework_pins")
        else:
            add("pins", "framework_pins.txt matches gitlinks", CheckStatus.PASS,
                Severity.RECOMMENDED, "")
    else:
        add("pins", "framework_pins.txt matches gitlinks", CheckStatus.WARN,
            Severity.RECOMMENDED, "Missing.", "snes_record_framework_pins")

    layout = _classify(checks, live)
    return AuditReport(
        root=str(root),
        layout=layout,
        project_name=project_name(root),
        boot_exe=None,  # SNES boots from the cartridge vector, not a named EXE
        checks=checks,
        notes=notes,
    )


def _classify(checks: list[CheckResult], have_framework: bool) -> LayoutClass:
    fails = sum(1 for c in checks if c.status == CheckStatus.FAIL)
    if not have_framework:
        return LayoutClass.UNKNOWN
    # Committed ROM-derived C is the pre-scaffold layout: the repo was built
    # by checking generator output in rather than regenerating from the ROM.
    if any(c.id == "generated" and c.status == CheckStatus.FAIL for c in checks):
        return LayoutClass.LEGACY_PACKAGING
    if fails == 0:
        return LayoutClass.SCAFFOLD_COMPLETE
    return LayoutClass.SETUP_HOST_PARTIAL


def _current_pins(root: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for path in (FRAMEWORK, "recomp-ui"):
        sub = root / path
        if sub.is_dir():
            code, out = _git(sub, "rev-parse", "HEAD")
            if code == 0 and out:
                pins[path] = out
    fw = root / FRAMEWORK
    for nested in NESTED_PATHS:
        sub = fw / nested
        if sub.is_dir():
            code, out = _git(sub, "rev-parse", "HEAD")
            if code == 0 and out:
                pins[Path(nested).name] = out
    return pins


def _stale_pins(root: Path, pins_file: Path) -> list[str]:
    try:
        text = pins_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ["unreadable"]
    recorded = {}
    for line in text.splitlines():
        key, _, val = line.partition("=")
        if key.strip() and val.strip():
            recorded[key.strip()] = val.strip()
    stale: list[str] = []
    for key, sha in _current_pins(root).items():
        if recorded.get(key) != sha:
            stale.append(key)
    return stale


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def build_plan(
    root: Path,
    options: MigrateOptions | None = None,
    report: AuditReport | None = None,
) -> Plan:
    root = Path(root).expanduser().resolve()
    options = options or MigrateOptions()
    report = report or audit_project(root, options)

    wanted = set(report.failing_ops())
    if options.record_pins:
        wanted.add("snes_record_framework_pins")
    else:
        wanted.discard("snes_record_framework_pins")
    if not options.enable_ci:
        wanted.discard("snes_emit_ci_workflow")
    if not options.patch_readme:
        wanted.discard("snes_patch_readme_metrics")
    if not options.merge_gitignore:
        wanted.discard("snes_merge_gitignore")
    if not options.enable_recomp_ui:
        wanted.discard("snes_ensure_recomp_ui_submodule")
    # The probe rewrites identity files from a real ROM: never planned without
    # one, always planned when the user asked for it and supplied one.
    if options.probe_disc and options.disc:
        wanted.add("snes_probe_rom_refresh")
    else:
        wanted.discard("snes_probe_rom_refresh")
    # Netplay is opt-in both ways: only an explicit flag plans a flip, and the
    # audit row never does (severity INFO, no auto-planned fix op).
    if options.enable_netplay and options.players >= 2:
        wanted.add("snes_enable_netplay")
    if options.disable_netplay:
        wanted.discard("snes_enable_netplay")
        wanted.add("snes_disable_netplay")

    if options.only:
        wanted = {o for o in wanted if o in options.only} | set(options.only)
    if options.skip:
        wanted -= set(options.skip)

    ordered = [op for op in OP_ORDER if op in wanted]
    ordered.extend(sorted(op for op in wanted if op not in ordered))

    steps = [
        PlanStep(
            op_id=op,
            title=OP_TITLES.get(op, op),
            detail=next((c.detail for c in report.checks if c.fix_op == op), ""),
            selected=True,
        )
        for op in ordered
    ]
    return Plan(root=str(root), layout=report.layout, steps=steps, options=options)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def apply_plan(plan: Plan) -> list[ApplyResult]:
    root = Path(plan.root).expanduser().resolve()
    opts = plan.options
    results: list[ApplyResult] = []
    for step in plan.steps:
        if not step.selected:
            continue
        fn = _OPS.get(step.op_id)
        if fn is None:
            results.append(ApplyResult(step.op_id, False, "Unknown op"))
            continue
        try:
            results.append(fn(root, opts))
        except Exception as exc:  # noqa: BLE001 - one bad op must not kill the run
            results.append(ApplyResult(step.op_id, False, f"{type(exc).__name__}: {exc}"))
    return results


def _dry(opts: MigrateOptions) -> bool:
    return bool(opts.dry_run)


def _op_ensure_framework(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _ensure_submodule(root, opts, FRAMEWORK)


def _op_ensure_recomp_ui(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _ensure_submodule(root, opts, "recomp-ui")


def _ensure_submodule(root: Path, opts: MigrateOptions, path: str) -> ApplyResult:
    from .gitops import DEFAULT_RECOMP_UI_URL, ensure_submodule, framework_branch, framework_url

    op = "snes_ensure_framework_submodule" if path == FRAMEWORK \
        else "snes_ensure_recomp_ui_submodule"
    if not _is_git_repo(root):
        return ApplyResult(op, False, f"{root} is not a git repository")
    if path == FRAMEWORK:
        url, branch = framework_url(), framework_branch()
    else:
        url, branch = DEFAULT_RECOMP_UI_URL, "master"
    r = ensure_submodule(root, path, url=url, branch=branch, dry_run=_dry(opts))
    return ApplyResult(op, r.ok, r.message, [path] if r.ok else [])


def _op_ensure_nested(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_ensure_nested_modules"
    fw = root / FRAMEWORK
    if not fw.is_dir():
        return ApplyResult(op, False, "No snesrecomp checkout")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] submodule update --init in {fw}")
    code, out = _git(fw, "submodule", "update", "--init", "--recursive", *NESTED_PATHS)
    ok = code == 0
    return ApplyResult(op, ok, "Initialised nested libs" if ok else f"git failed: {out}",
                       [f"{FRAMEWORK}/{p}" for p in NESTED_PATHS] if ok else [])


def _op_merge_gitignore(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_merge_gitignore"
    gi = root / ".gitignore"
    text = ""
    if gi.is_file():
        text = gi.read_text(encoding="utf-8", errors="replace")
    existing = {ln.strip() for ln in text.splitlines()}
    missing = [r for r in GITIGNORE_RULES if r not in existing]
    if not missing:
        return ApplyResult(op, True, ".gitignore already complete")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would append {len(missing)} rule(s)")
    block = "\n# --- SNES recomp (Studio) ---\n" + "\n".join(missing) + "\n"
    if text and not text.endswith("\n"):
        block = "\n" + block
    gi.write_text(text + block, encoding="utf-8", newline="\n")
    return ApplyResult(op, True, f"Appended {len(missing)} rule(s)", [".gitignore"])


_GENERATED_PATHSPECS = ("src/gen", "recomp/funcs.h")


def _op_untrack_generated(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_untrack_generated"
    # Per pathspec, not one combined call: `git rm` fails the whole command on
    # any pathspec that matches nothing, and the usual case is exactly that —
    # src/gen committed, recomp/funcs.h not.
    hits = {spec: _tracked(root, spec) for spec in _GENERATED_PATHSPECS}
    live = [spec for spec, files in hits.items() if files]
    tracked = [f for files in hits.values() for f in files]
    if not tracked:
        return ApplyResult(op, True, "Nothing tracked")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would untrack {len(tracked)} file(s)")
    # --cached only: the working tree keeps the files, so a developer mid-build
    # does not lose the C they just generated.
    for spec in live:
        code, out = _git(root, "rm", "-r", "--cached", "-q", "--", spec)
        if code != 0:
            return ApplyResult(op, False, f"git rm --cached {spec} failed: {out}")
    return ApplyResult(op, True, f"Untracked {len(tracked)} file(s) (working tree kept)",
                       tracked[:20])


def _op_ensure_src_gen(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_ensure_src_gen"
    keep = root / "src" / "gen" / ".gitkeep"
    if keep.is_file():
        return ApplyResult(op, True, "src/gen/.gitkeep already present")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would create {keep}")
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("", encoding="utf-8")
    return ApplyResult(op, True, "Created src/gen/.gitkeep", ["src/gen/.gitkeep"])


def _op_emit_version(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_emit_version"
    dst = root / "VERSION"
    if dst.is_file() and not opts.force:
        return ApplyResult(op, True, "VERSION already present")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would write {dst}")
    dst.write_text("0.1.0\n", encoding="utf-8", newline="\n")
    return ApplyResult(op, True, "Wrote VERSION (0.1.0)", ["VERSION"])


def _template_values(root: Path, opts: MigrateOptions) -> dict[str, str]:
    name = opts.project_name or project_name(root)
    shown = display_name(root)
    values = {
        "PROJECT_NAME": name,
        "DISPLAY_NAME": shown,
        "ZIP_PREFIX": opts.zip_prefix or _zip_prefix(root),
        "DEFAULT_BRANCH": default_branch(root),
    }
    ident = rom_identity(root, opts.disc)
    if ident.get("display_name"):
        values["DISPLAY_NAME"] = ident["display_name"]
    for token, key in (("ROM_CRC32", "crc32"), ("ROM_SHA256", "sha256"),
                       ("ROM_FILE", "rom_file"), ("ROM_MAPPING", "mapping"),
                       ("REGION", "region")):
        if ident.get(key):
            values[token] = ident[key]
    if values.get("ROM_FILE"):
        values["ROM_SLUG"] = Path(values["ROM_FILE"]).stem
    return values


_TOKEN_RE = re.compile(r"@([A-Z0-9_]+)@")


def _render(text: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """@TOKEN@ substitution, matching the wizard's fill_tokens.py contract.

    Reimplemented rather than imported: the Studio toolkit ships its own
    top-level ``fill_tokens.py`` for PSX, and putting the SNES wizard on
    sys.path makes which one you get depend on import order. Six lines of
    regex is a smaller liability than that. Unknown tokens are returned, never
    silently blanked — a blank in a CI workflow surfaces much later and much
    worse.
    """
    missing: list[str] = []

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return values[key]

    return _TOKEN_RE.sub(replace, text), missing


def _fill_template(
    root: Path, opts: MigrateOptions, op: str, template: str, rel: str
) -> ApplyResult:
    src = snes_paths.templates_dir(root) / template
    if not src.is_file():
        return ApplyResult(op, False, f"Template not found: {src}")
    dst = root / rel
    if dst.is_file() and not opts.force:
        return ApplyResult(op, True, f"{rel} already present (use --force to overwrite)")

    rendered, missing = _render(src.read_text(encoding="utf-8"), _template_values(root, opts))
    if missing:
        # Refuse rather than emit a file with @TOKEN@ or a blank in it. The
        # commonest missing token is a ROM digest, and that is exactly the value
        # nobody should be inventing.
        return ApplyResult(
            op, False,
            f"{rel} not written — unresolved tokens: {', '.join(sorted(set(missing)))}",
        )
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would write {rel} from {template}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rendered, encoding="utf-8", newline="\n")
    if rel.endswith(".sh"):
        dst.chmod(dst.stat().st_mode | 0o111)
    return ApplyResult(op, True, f"Wrote {rel} ({snes_paths.wizard_source(root)})", [rel])


def _regen_gap(root: Path, rendered: str) -> str | None:
    """The reason this regen.sh would not run here, or None.

    Studio drives whichever wizard it can find; the port runs whichever
    snesrecomp its gitlink records. On a fork those are routinely different
    revisions, and writing the newer wizard's regen.sh into the older
    framework's repo is exactly how a port ends up with a script that dies on
    `invalid choice: 'verify-rom'` the first time anyone presses Generate.
    Refusing to write it is the fix: the file Studio was about to create is the
    defect, and creating it anyway only moves the failure later.
    """
    from .buildops import framework_gap_message

    gap = snes_paths.regen_framework_gap(root, rendered)
    if gap is None:
        return None
    missing, have = gap
    cli = snes_paths.regen_framework_root(root) / "snesrecomp_cli.py"
    return framework_gap_message(cli, missing, have)


def _fill_regen(root: Path, opts: MigrateOptions, op: str) -> ApplyResult:
    """tools/regen.sh, but never a version the pinned framework cannot run."""
    src = snes_paths.templates_dir(root) / "regen.sh.in"
    if src.is_file():
        try:
            rendered, _ = _render(src.read_text(encoding="utf-8"),
                                  _template_values(root, opts))
        except OSError:
            rendered = ""
        why = _regen_gap(root, rendered) if rendered else None
        if why:
            return ApplyResult(
                op, False,
                "tools/regen.sh not written — it would not run against this "
                f"port's own snesrecomp. {why}")
    return _fill_template(root, opts, op, "regen.sh.in", "tools/regen.sh")


def _op_emit_regen(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _fill_regen(root, opts, "snes_emit_regen")


def _op_emit_packager(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _fill_template(root, opts, "snes_emit_packager", "package_release.sh.in",
                          "scripts/package_release.sh")


def _op_emit_ci(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _fill_template(root, opts, "snes_emit_ci_workflow", "release.yml.in",
                          ".github/workflows/release.yml")


def _op_record_pins(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_record_framework_pins"
    pins = _current_pins(root)
    if not pins:
        return ApplyResult(op, False, "No initialised submodules to pin")
    text = "".join(f"{k}={v}\n" for k, v in pins.items())
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would write {len(pins)} pin(s)")
    (root / "framework_pins.txt").write_text(text, encoding="utf-8", newline="\n")
    return ApplyResult(op, True, f"Wrote {len(pins)} pin(s)", ["framework_pins.txt"])


_NETPLAY_CALL_RE = re.compile(
    r"^([ \t]*)snesrecomp_enable_recomp_net\(([^)]*)\)", re.M)
_NETPLAY_DISABLED_RE = re.compile(
    r"^([ \t]*)#[ \t]*snesrecomp_enable_recomp_net\(([^)]*)\)", re.M)


def _netplay_wired(cml_text: str) -> bool:
    return bool(_NETPLAY_CALL_RE.search(cml_text))


def _cml_target_name(cml_text: str) -> str | None:
    m = re.search(r"add_executable\(\s*([A-Za-z0-9_.-]+)", cml_text)
    return m.group(1) if m else None


def _op_enable_netplay(root: Path, opts: MigrateOptions) -> ApplyResult:
    """Wire snesrecomp_enable_recomp_net(<target>) into CMakeLists.txt.

    Build-side flip only: it links the delay-sync engine and lobby client and
    defines SNES_HAS_LOBBY_CLIENT. The launcher's netplay button additionally
    needs host wiring in src/main.c (gi.netplay_supported + the barrier loop);
    scaffolds from the current wizard template carry it, older hosts follow
    MetalWarriorsSNESRecomp's src/main.c.
    """
    op = "snes_enable_netplay"
    cml = root / "CMakeLists.txt"
    if not cml.is_file():
        return ApplyResult(op, False, "No CMakeLists.txt")
    text = cml.read_text(encoding="utf-8", errors="replace")
    if _netplay_wired(text):
        return ApplyResult(op, True, "Netplay already wired")
    changed_note = ""
    m = _NETPLAY_DISABLED_RE.search(text)
    if m:
        new_text = text[: m.start()] + m.group(1) \
            + f"snesrecomp_enable_recomp_net({m.group(2)})" + text[m.end():]
        changed_note = "Uncommented the existing call"
    else:
        target = _cml_target_name(text)
        if not target:
            return ApplyResult(op, False,
                               "Could not find add_executable() to name the target")
        block = (
            "\n# Delay-sync netplay + MotK lobby client (defines "
            "SNES_HAS_LOBBY_CLIENT; the\n"
            "# launcher's netplay button needs the host wiring in src/main.c "
            "as well —\n"
            "# see MetalWarriorsSNESRecomp src/main.c for the reference).\n"
            f"snesrecomp_enable_recomp_net({target})\n"
        )
        anchor = re.search(r"^[ \t]*recomp_target_launcher_ui\([^)]*\)",
                           text, re.M | re.S)
        if anchor:
            end = anchor.end()
            new_text = text[:end] + block + text[end:]
        else:
            new_text = text.rstrip() + "\n" + block
        changed_note = f"Added snesrecomp_enable_recomp_net({target})"
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] {changed_note}")
    cml.write_text(new_text, encoding="utf-8", newline="\n")
    # The engine needs its nested submodules; reuse the existing op.
    nested = _op_ensure_nested(root, opts)
    msg = changed_note + ("; " + nested.message if nested.message else "")
    return ApplyResult(op, True, msg, ["CMakeLists.txt"] + nested.changed_paths)


def _op_disable_netplay(root: Path, opts: MigrateOptions) -> ApplyResult:
    """Comment the snesrecomp_enable_recomp_net() call out (reversible)."""
    op = "snes_disable_netplay"
    cml = root / "CMakeLists.txt"
    if not cml.is_file():
        return ApplyResult(op, False, "No CMakeLists.txt")
    text = cml.read_text(encoding="utf-8", errors="replace")
    m = _NETPLAY_CALL_RE.search(text)
    if not m:
        return ApplyResult(op, True, "Netplay already not wired")
    new_text = text[: m.start()] + m.group(1) \
        + f"# snesrecomp_enable_recomp_net({m.group(2)})" + text[m.end():]
    if _dry(opts):
        return ApplyResult(op, True, "[dry-run] would comment the call out")
    cml.write_text(new_text, encoding="utf-8", newline="\n")
    return ApplyResult(op, True,
                       "Commented snesrecomp_enable_recomp_net() out "
                       "(host code compiles the netplay blocks away)",
                       ["CMakeLists.txt"])


def _op_repair_framework(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_repair_framework_submodule"
    broken = diagnose_framework_checkout(root)
    if broken is None:
        return ApplyResult(op, True, "snesrecomp/ checkout is healthy")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would move {FRAMEWORK}/ aside and re-clone")
    import time as _time

    aside = root / f"{FRAMEWORK}.broken-{_time.strftime('%Y%m%d-%H%M%S')}"
    try:
        (root / FRAMEWORK).rename(aside)
    except OSError as exc:
        return ApplyResult(op, False, f"Could not move broken tree aside: {exc}")
    # Drop the stale index entry so ensure_submodule re-adds cleanly.
    _git(root, "rm", "-r", "--cached", "-q", "--", FRAMEWORK)
    res = _ensure_submodule(root, opts, FRAMEWORK)
    msg = (f"Moved broken tree to {aside.name}; " + res.message)
    return ApplyResult(op, res.ok, msg,
                       ([FRAMEWORK, aside.name] if res.ok else [aside.name]))


def _emit_identity(root: Path, opts: MigrateOptions, op: str) -> ApplyResult:
    """Write whichever identity carrier the pinned wizard scaffolds.

    Both op ids route here on purpose. A saved plan, a stale audit or an
    explicit ``--only snes_emit_codegen_setup`` should still produce the file
    the framework in front of it actually builds against; the result message
    names what was written, so nothing is written behind the caller's back.
    """
    carriers = identity_carriers(root)
    if not carriers:
        return ApplyResult(
            op, False,
            f"No identity template in {snes_paths.templates_dir(root)} — "
            "neither rom_identity.txt.in nor codegen_setup.c.in")
    results = [_fill_template(root, opts, op, tpl, rel) for tpl, rel in carriers]
    changed = [pth for r in results for pth in r.changed_paths]
    failed = "; ".join(r.message for r in results if not r.ok)
    return ApplyResult(op, not failed,
                       failed or "; ".join(r.message for r in results), changed)


def _op_emit_codegen_setup(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _emit_identity(root, opts, "snes_emit_codegen_setup")


def _op_emit_rom_identity(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _emit_identity(root, opts, "snes_emit_rom_identity")


def _op_probe_rom_refresh(root: Path, opts: MigrateOptions) -> ApplyResult:
    """Probe the ROM and re-emit the identity carriers with fresh digests."""
    op = "snes_probe_rom_refresh"
    if not opts.disc:
        return ApplyResult(op, False, "No ROM path (--disc) supplied")
    ident = rom_identity(root, opts.disc)
    if not (ident.get("sha256") and ident.get("crc32")):
        return ApplyResult(op, False, f"probe_rom.py produced no digests for {opts.disc}")
    # Re-emit with force: refreshing identity is the whole point of this op,
    # and _fill_template re-reads rom_identity(root, opts.disc) itself.
    import dataclasses as _dc

    forced = _dc.replace(opts, force=True)
    carriers = identity_carriers(root)
    if not carriers:
        return ApplyResult(
            op, False,
            f"No identity template in {snes_paths.templates_dir(root)} — "
            "neither rom_identity.txt.in nor codegen_setup.c.in")
    results = [_fill_template(root, forced, op, tpl, rel) for tpl, rel in carriers]
    results.append(_fill_regen(root, forced, op))
    changed = [pth for r in results for pth in r.changed_paths]
    ok = all(r.ok for r in results)
    detail = f"crc32 {ident['crc32']}, sha256 {ident['sha256'][:12]}…"
    msgs = "; ".join(r.message for r in results if not r.ok) or detail
    return ApplyResult(op, ok, ("Refreshed identity: " + detail) if ok else msgs, changed)


def _op_relocate_boxart(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_relocate_boxart"
    dst_dir = root / "launcher_assets" / "img"
    for name in ("boxart.tga", "boxart.png"):
        if (dst_dir / name).is_file():
            return ApplyResult(op, True, "Modern boxart already present")
    candidates = [
        root / "assets" / "boxart.tga", root / "assets" / "boxart.png",
        root / "boxart.tga", root / "boxart.png",
    ]
    src = next((c for c in candidates if c.is_file()), None)
    if src is None:
        return ApplyResult(op, False, "No legacy boxart found")
    if _dry(opts):
        return ApplyResult(op, True, f"[dry-run] would move {src.name} → launcher_assets/img/")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.move(str(src), str(dst))
    rel = str(dst.relative_to(root)).replace("\\", "/")
    return ApplyResult(op, True, f"Moved {src.name} → {rel}", [rel])


def _op_emit_boxart_stub(root: Path, opts: MigrateOptions) -> ApplyResult:
    op = "snes_emit_boxart_stub"
    img = root / "launcher_assets" / "img"
    if img.is_dir():
        return ApplyResult(op, True, "launcher_assets/img already exists")
    if _dry(opts):
        return ApplyResult(op, True, "[dry-run] mkdir launcher_assets/img")
    img.mkdir(parents=True, exist_ok=True)
    keep = img / ".gitkeep"
    keep.write_text("", encoding="utf-8")
    return ApplyResult(op, True, "Created launcher_assets/img/",
                       ["launcher_assets/img/.gitkeep"])


_SNES_LIBRETRO_SYSTEM = "Nintendo - Super Nintendo Entertainment System"


def _ensure_boxart_png(root: Path, opts: MigrateOptions) -> list[str]:
    """Fetch libretro SNES boxart when the README PNG is missing. Never raises."""
    import sys as _sys

    png = root / "launcher_assets" / "img" / "boxart.png"
    tga = root / "launcher_assets" / "img" / "boxart.tga"
    if png.is_file():
        return []
    if _dry(opts):
        return ["launcher_assets/img/boxart.png"]
    ident = rom_identity(root, opts.disc)
    rom_stem = Path(ident["rom_file"]).stem if ident.get("rom_file") else ""
    display = ident.get("display_name") or display_name(root)
    if not rom_stem and not display:
        return []
    try:
        from .paths import toolkit_dir

        _sys.path.insert(0, str(toolkit_dir()))
        from fetch_boxart import fetch_to_paths  # type: ignore
    except ImportError:
        return []
    try:
        fetch_to_paths(tga, cue_stem=rom_stem, display_name=display,
                       system=_SNES_LIBRETRO_SYSTEM)
    except Exception:
        return []
    changed: list[str] = []
    for rel in ("boxart.png", "boxart.tga", "BOXART_SOURCE.txt"):
        if (tga.parent / rel).is_file():
            changed.append(f"launcher_assets/img/{rel}")
    return changed


def _op_patch_readme_metrics(root: Path, opts: MigrateOptions) -> ApplyResult:
    """Upsert download badges, boxart, RetComM Launcher, and R.A.I.D. footer."""
    op = "snes_patch_readme_metrics"
    from .paths import templates_dir as _toolkit_templates
    from .readme_metrics import (
        apply_github_about,
        boxart_png_present,
        render_boxart_block,
        render_launcher_block,
        render_metrics_block,
        render_raid_block,
        resolve_github_slug,
        upsert_readme_blocks,
    )

    owner, repo = resolve_github_slug(root, opts.github_owner, opts.github_repo)
    zp = opts.zip_prefix or _zip_prefix(root)
    fetched = _ensure_boxart_png(root, opts)
    display = opts.window_title or display_name(root)
    boxart_block = None
    if boxart_png_present(root) or (
        opts.dry_run and "launcher_assets/img/boxart.png" in fetched
    ):
        boxart_block = render_boxart_block(display)

    path = root / "README.md"
    if path.is_file():
        old_text = path.read_text(encoding="utf-8", errors="replace")
    else:
        old_text = f"# {display}\n"
    new_text = upsert_readme_blocks(
        old_text,
        render_metrics_block(owner, repo, zp),
        render_launcher_block(),
        render_raid_block(),
        boxart=boxart_block,
    )
    changed: list[str] = list(fetched)
    if new_text != old_text:
        if not _dry(opts):
            path.write_text(new_text, encoding="utf-8", newline="\n")
        if "README.md" not in changed:
            changed.append("README.md")

    src_img = _toolkit_templates() / "raid-discord.png"
    dst_img = root / ".github" / "raid-discord.png"
    if src_img.is_file() and not dst_img.is_file():
        if not _dry(opts):
            dst_img.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_img, dst_img)
        changed.append(".github/raid-discord.png")

    about_ok, about_msg = apply_github_about(owner, repo, dry_run=_dry(opts))
    parts: list[str] = []
    if changed:
        parts.append(f"Patched README blocks ({owner}/{repo})")
    else:
        parts.append("README already complete")
    if about_msg:
        parts.append(about_msg if about_ok else f"[about] {about_msg}")
    return ApplyResult(op, True, "; ".join(parts), changed)


# Public aliases for the two ops gitops.install_and_push_release_ci reaches
# for directly — it installs CI outside a full plan.
op_emit_packager = _op_emit_packager
op_emit_ci_workflow = _op_emit_ci


_OPS = {
    "snes_ensure_framework_submodule": _op_ensure_framework,
    "snes_ensure_recomp_ui_submodule": _op_ensure_recomp_ui,
    "snes_ensure_nested_modules": _op_ensure_nested,
    "snes_merge_gitignore": _op_merge_gitignore,
    "snes_untrack_generated": _op_untrack_generated,
    "snes_ensure_src_gen": _op_ensure_src_gen,
    "snes_emit_version": _op_emit_version,
    "snes_emit_regen": _op_emit_regen,
    "snes_emit_packager": _op_emit_packager,
    "snes_emit_ci_workflow": _op_emit_ci,
    "snes_record_framework_pins": _op_record_pins,
    "snes_repair_framework_submodule": _op_repair_framework,
    "snes_probe_rom_refresh": _op_probe_rom_refresh,
    "snes_emit_codegen_setup": _op_emit_codegen_setup,
    "snes_emit_rom_identity": _op_emit_rom_identity,
    "snes_relocate_boxart": _op_relocate_boxart,
    "snes_emit_boxart_stub": _op_emit_boxart_stub,
    "snes_patch_readme_metrics": _op_patch_readme_metrics,
    "snes_enable_netplay": _op_enable_netplay,
    "snes_disable_netplay": _op_disable_netplay,
}
