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
import tempfile
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
    "snes_ensure_framework_submodule",
    "snes_ensure_recomp_ui_submodule",
    "snes_ensure_nested_modules",
    "snes_merge_gitignore",
    "snes_untrack_generated",
    "snes_ensure_src_gen",
    "snes_emit_version",
    "snes_emit_regen",
    "snes_emit_packager",
    "snes_emit_ci_workflow",
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
                ):
                    val = str(data.get(key) or "").strip()
                    if val:
                        out[token] = val
                out["rom_file"] = rom_path.name
        if "rom_file" not in out:
            out["rom_file"] = rom_path.name
        return out

    regen = root / "tools" / "regen.sh"
    if regen.is_file():
        try:
            text = regen.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for pattern, key in _DIGEST_PATTERNS:
            m = pattern.search(text)
            if m:
                out[key] = m.group(1)
        m = re.search(r'for cand in "([^"]+\.s[fm]c)"', text)
        if m:
            out["rom_file"] = m.group(1)
    return out


def _run_probe(probe: Path, rom: Path) -> tuple[int, str]:
    """Run probe_rom.py and return its JSON.

    The wizard's probe writes JSON to a file (``--json-out``) and prints a
    human summary to stdout — there is no ``--json`` that puts it on stdout, so
    reading stdout would parse the summary and silently find no digests.
    """
    python = os.environ.get("PYTHON") or shutil.which("python3") or shutil.which("python")
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


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
def audit_project(root: Path) -> AuditReport:
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
    if live:
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
    report = report or audit_project(root)

    wanted = set(report.failing_ops())
    if options.record_pins:
        wanted.add("snes_record_framework_pins")
    else:
        wanted.discard("snes_record_framework_pins")
    if not options.enable_ci:
        wanted.discard("snes_emit_ci_workflow")
    if not options.merge_gitignore:
        wanted.discard("snes_merge_gitignore")
    if not options.enable_recomp_ui:
        wanted.discard("snes_ensure_recomp_ui_submodule")

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
                       ("ROM_FILE", "rom_file")):
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


def _op_emit_regen(root: Path, opts: MigrateOptions) -> ApplyResult:
    return _fill_template(root, opts, "snes_emit_regen", "regen.sh.in", "tools/regen.sh")


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
}
