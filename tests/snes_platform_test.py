#!/usr/bin/env python3
"""Headless check of the platform split — the SNES half of Project Studio.

The GUI needs a GPU and a window; the decisions that make a SNES session a
SNES session do not. This drives them directly against a synthetic repo:

  * one platform per process, and the PSX defaults unchanged by its existence
  * a separate repo index per console, so a SNES add never rewrites the PSX list
  * the SNES audit / plan / apply ops, end to end, on a scaffold-shaped tree
  * refusal to emit a file whose ROM digests cannot be resolved — the one
    failure mode worth a test, because inventing them produces a regen.sh that
    verifies a ROM nobody owns

Run:  python3 tests/snes_platform_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "tools" / "new_project_layout"))

from project_studio import platforms  # noqa: E402
from project_studio.models import MigrateOptions  # noqa: E402

failures = 0


def check(cond: bool, what: str) -> None:
    global failures
    if cond:
        print(f"  ok    {what}")
    else:
        print(f"  FAIL  {what}")
        failures += 1


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


def make_repo(root: Path) -> None:
    """A SNES port mid-migration: framework present, scaffold half-missing."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\nproject(ZedSNESRecomp C)\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# Zed\n", encoding="utf-8")
    (root / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    (root / "recomp").mkdir()
    (root / "recomp" / "bank00.cfg").write_text("# seed\n", encoding="utf-8")
    (root / "recomp" / "symbols.toml").write_text("[symbols]\n", encoding="utf-8")
    # A framework checkout, complete with the marker that proves it is one.
    fw = root / "snesrecomp" / "runner"
    fw.mkdir(parents=True)
    (fw / "runner.cmake").write_text("# marker\n", encoding="utf-8")
    # Generated C, wrongly committed — the thing the audit must catch.
    (root / "src" / "gen").mkdir(parents=True)
    (root / "src" / "gen" / "bank00.c").write_text("/* generated */\n", encoding="utf-8")

    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Studio Test")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial")


def test_platform_defaults() -> None:
    print("platform profiles")
    check(platforms.current().key == "psx", "a fresh process defaults to psx")
    check(platforms.get("snes").framework == "snesrecomp", "snes framework is snesrecomp")
    check(platforms.get("psx").framework == "psxrecomp", "psx framework is unchanged")
    check(platforms.normalize("SNES") == "snes", "platform keys are case-insensitive")
    check(platforms.normalize("gameboy") == "psx", "an unknown console falls back to psx")


def test_index_separation() -> None:
    print("repo index")
    from project_studio import repo_index

    platforms.set_current("psx")
    psx_path = repo_index.default_index_path()
    platforms.set_current("snes")
    snes_path = repo_index.default_index_path()
    check(psx_path != snes_path, "each console resolves a different index file")
    check(snes_path.name.endswith("_snes.json"), "the SNES index is named for its console")


def test_repo_recognition(root: Path) -> None:
    print("repo recognition")
    from project_studio import repo_index

    platforms.set_current("snes")
    check(repo_index.looks_like_game_repo(root), "a SNES port is recognised under snes")
    platforms.set_current("psx")
    check(
        not repo_index.looks_like_game_repo(root),
        "the same tree is NOT a PSX port (no game.toml, no psxrecomp/)",
    )
    platforms.set_current("snes")


def test_audit(root: Path) -> None:
    print("audit")
    from project_studio import snesops

    report = snesops.audit_project(root)
    by_id = {c.id: c for c in report.checks}
    check(report.project_name == "ZedSNESRecomp", "project name read from project()")
    check(by_id["framework"].status.value == "pass", "framework checkout found")
    check(by_id["analysis"].status.value == "pass", "recomp/ analysis config found")
    check(by_id["generated"].status.value == "fail", "committed generated C is a failure")
    check(
        by_id["generated"].fix_op == "snes_untrack_generated",
        "committed generated C names its fix op",
    )
    check(by_id["gitignore"].status.value == "fail", "missing .gitignore rules are a failure")
    check(by_id["version"].status.value == "pass", "VERSION present")
    check(
        by_id["regen"].fix_op is None,
        "regen.sh is NOT offered as a fix when ROM digests are unknown",
    )


def test_plan_and_apply(root: Path) -> None:
    print("plan + apply")
    from project_studio import snesops

    plan = snesops.build_plan(root, MigrateOptions(dry_run=True))
    ops = [s.op_id for s in plan.steps]
    check("snes_merge_gitignore" in ops, "gitignore merge planned")
    check("snes_untrack_generated" in ops, "untrack planned")
    check("snes_emit_regen" not in ops, "regen.sh not planned without digests")
    check(
        ops.index("snes_merge_gitignore") < ops.index("snes_record_framework_pins"),
        "pins are recorded after the ops that change what is pinned",
    )

    dry = snesops.apply_plan(plan)
    check(all(r.ok for r in dry), "every planned op succeeds as a dry run")
    check(
        not (root / ".gitignore").is_file(),
        "a dry run writes nothing",
    )

    real = snesops.apply_plan(snesops.build_plan(root, MigrateOptions()))
    check(all(r.ok for r in real), "every planned op succeeds for real")
    gi = (root / ".gitignore").read_text(encoding="utf-8")
    check("/src/gen/" in gi and "*.sfc" in gi, ".gitignore now blocks generated C and ROMs")
    out = subprocess.run(
        ["git", "ls-files", "--", "src/gen"], cwd=str(root), capture_output=True, text=True
    ).stdout
    check(out.strip() == "", "generated C is no longer tracked")
    check(
        (root / "src" / "gen" / "bank00.c").is_file(),
        "untracking kept the working-tree file (--cached only)",
    )
    check((root / "framework_pins.txt").is_file(), "framework_pins.txt written")
    pkg = root / "scripts" / "package_release.sh"
    check(pkg.is_file(), "packager emitted from the wizard template")
    check("@ZIP_PREFIX@" not in pkg.read_text(encoding="utf-8"), "no @TOKEN@ survives the fill")
    check(pkg.stat().st_mode & 0o111 != 0, "the emitted packager is executable")
    check(
        (root / ".github" / "workflows" / "release.yml").is_file(),
        "CI workflow emitted from the wizard template",
    )

    after = snesops.audit_project(root)
    ids = {c.id: c.status.value for c in after.checks}
    check(ids["gitignore"] == "pass", "re-audit: gitignore now passes")
    check(ids["generated"] == "pass", "re-audit: generated C now passes")


def test_parity_checks(root: Path) -> None:
    """The PSX-parity additions: identity, codegen_setup, boxart, README,
    legacy classification, checkout repair diagnosis, and probe gating."""
    print("parity checks")
    from project_studio import snesops

    # make_repo committed generated C → the legacy layout class.
    before = snesops.audit_project(root)
    check(before.layout.value == "legacy-packaging",
          "committed generated C classifies as legacy-packaging")

    by_id = {c.id: c for c in before.checks}
    check(by_id["rom_identity"].status.value == "warn",
          "no recoverable digests → ROM identity warns")
    check(by_id["rom_identity"].fix_op == "snes_probe_rom_refresh",
          "identity warn names the probe op")
    check(by_id["codegen_setup"].status.value == "fail",
          "missing codegen_setup fails")
    check(by_id["codegen_setup"].fix_op is None,
          "codegen_setup is NOT offered as a fix without digests")
    check(by_id["boxart"].status.value == "warn", "no boxart warns (optional)")
    check(by_id["readme_metrics"].status.value == "warn",
          "bare README warns with the metrics op")

    # Digest recovery unlocks codegen emission.
    (root / "src" / "codegen_setup.c").write_text(
        '#include "codegen_setup.h"\n'
        "const GameCodegenIdentity kGameCodegenIdentity = {\n"
        '    .display_name   = "Zed",\n'
        '    .rom_file       = "Zed (World).sfc",\n'
        '    .expected_crc32 = "12345678",\n'
        '    .expected_sha256= "aa" ,\n'
        '    .mapping        = "lorom",\n'
        '    .region         = "NTSC",\n'
        "};\n",
        encoding="utf-8",
    )
    ident = snesops.rom_identity(root)
    check(ident.get("crc32") == "12345678", "digests recover from codegen_setup.c")
    check(ident.get("mapping") == "lorom", "mapping recovers from codegen_setup.c")
    mid = {c.id: c for c in snesops.audit_project(root).checks}
    check(mid["rom_identity"].status.value == "pass",
          "identity passes once recoverable")
    check(mid["codegen_setup"].status.value == "fail",
          "codegen_setup.c without its header still fails (build needs both)")
    check(mid["codegen_setup"].fix_op == "snes_emit_codegen_setup",
          "and now names the emit op")

    # Probe gating: never planned without a ROM, planned with one.
    plan = snesops.build_plan(root, MigrateOptions(dry_run=True, probe_disc=True))
    check("snes_probe_rom_refresh" not in [st.op_id for st in plan.steps],
          "probe never planned without a ROM path")
    (root / "src" / "codegen_setup.c").unlink()

    # Boxart relocation: legacy file moves to launcher_assets/img/.
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets" / "boxart.png").write_bytes(b"\x89PNG fake")
    box = {c.id: c for c in snesops.audit_project(root).checks}["boxart"]
    check(box.fix_op == "snes_relocate_boxart", "legacy boxart names relocate")
    res = snesops._op_relocate_boxart(root, MigrateOptions())
    check(res.ok and (root / "launcher_assets" / "img" / "boxart.png").is_file(),
          "relocate moves the file into launcher_assets/img/")
    check(not (root / "assets" / "boxart.png").is_file(),
          "and removes the legacy copy")

    # Broken-checkout diagnosis: a .git file pointing nowhere is named.
    (root / "snesrecomp" / ".git").write_text(
        "gitdir: ../.git/modules/gone\n", encoding="utf-8")
    reason = snesops.diagnose_framework_checkout(root)
    check(reason is not None and "missing gitdir" in reason,
          "stale gitdir pointer is diagnosed")
    fw_check = {c.id: c for c in snesops.audit_project(root).checks}["framework"]
    check(fw_check.fix_op == "snes_repair_framework_submodule",
          "broken checkout names the repair op")
    (root / "snesrecomp" / ".git").unlink()


def test_version_stamp(root: Path) -> None:
    print("lobby pin stamp")
    from project_studio import snesops

    build = root / "build-release"
    build.mkdir(exist_ok=True)
    (build / "snes_game_version.txt").write_text("0.9.9\n", encoding="utf-8")
    stamp = {c.id: c for c in snesops.audit_project(root).checks}.get(
        "version_stamp_match")
    check(stamp is not None and stamp.status.value == "fail",
          "stamp drift against VERSION fails")
    (build / "snes_game_version.txt").write_text("0.2.0\n", encoding="utf-8")
    stamp = {c.id: c for c in snesops.audit_project(root).checks}.get(
        "version_stamp_match")
    check(stamp is not None and stamp.status.value == "pass",
          "matching stamp passes")
    (build / "snes_game_version.txt").unlink()


def test_digest_recovery(root: Path) -> None:
    print("template tokens")
    from project_studio import snesops

    check(snesops.rom_identity(root) == {}, "no digests recoverable from a repo without regen.sh")
    (root / "tools").mkdir(exist_ok=True)
    (root / "tools" / "regen.sh").write_text(
        'EXPECTED_CRC32="${SNESRECOMP_EXPECTED_CRC32:-deadbeef}"\n'
        'EXPECTED_SHA256="${SNESRECOMP_EXPECTED_SHA256:-abc123}"\n'
        '  for cand in "Zed (USA).sfc" ; do :; done\n',
        encoding="utf-8",
    )
    ident = snesops.rom_identity(root)
    check(ident.get("crc32") == "deadbeef", "CRC32 carried across from the existing regen.sh")
    check(ident.get("sha256") == "abc123", "SHA256 carried across")
    check(ident.get("rom_file") == "Zed (USA).sfc", "ROM filename carried across")

    values = snesops._template_values(root, MigrateOptions())
    check(values.get("ROM_CRC32") == "deadbeef", "recovered digests reach the template values")
    check(values.get("ROM_SLUG") == "Zed (USA)", "ROM_SLUG derived from the recovered filename")

    # With digests in hand a forced re-emit of regen.sh now resolves every
    # token — the same file, carried forward rather than re-derived.
    r = snesops._op_emit_regen(root, MigrateOptions(force=True))
    check(r.ok, f"regen.sh re-emits once digests are known ({r.message})")
    regen = (root / "tools" / "regen.sh").read_text(encoding="utf-8")
    check("deadbeef" in regen, "the re-emitted regen.sh keeps the original CRC32")
    check("@ROM_SHA256@" not in regen, "no ROM token survives the fill")


def test_probe_rom(root: Path) -> None:
    """Probing a real ROM, not just recovering digests from an old regen.sh.

    The wizard's probe writes JSON to --json-out and prints a human summary to
    stdout. Reading stdout instead parses the summary, finds no digests, and
    fails as "cannot emit regen.sh" — a wrong answer that looks like the
    correct refusal, which is exactly why this is tested against a real file.
    """
    print("probe ROM")
    from project_studio import snes_paths, snesops

    probe = snes_paths.probe_rom_script(root)
    if not probe.is_file():
        print("  skip  no probe_rom.py available")
        return
    # A minimal LoROM image the probe can read: 32 KiB with a header at $7FC0.
    rom = root / "Synthetic (USA).sfc"
    raw = bytearray(b"\x00" * 0x8000)
    title = b"SYNTHETIC TEST      "  # 21 bytes
    raw[0x7FC0:0x7FC0 + 21] = title[:21].ljust(21, b" ")
    raw[0x7FDB] = 0x00  # version
    raw[0x7FD9] = 0x01  # region: USA
    raw[0x7FFC:0x7FFE] = b"\x00\x80"  # reset vector
    rom.write_bytes(bytes(raw))

    ident = snesops.rom_identity(root, str(rom))
    check(len(ident.get("crc32", "")) == 8, f"CRC32 probed from the ROM ({ident.get('crc32')})")
    check(len(ident.get("sha256", "")) == 64, "SHA256 probed from the ROM")
    check(ident.get("rom_file") == rom.name, "ROM filename recorded")
    check(
        ident.get("crc32") != "deadbeef",
        "a probed ROM overrides the digests recovered from regen.sh",
    )


def test_region_default() -> None:
    """--region defaults to USA on PSX and to nothing on SNES.

    The parser cannot carry one default for both: "USA" is a PSX habit, and
    sending it at a cartridge relabels a Japanese ROM as North American in the
    README and the packaged zip name.
    """
    print("region default")
    from project_studio import cli

    ap = cli.build_parser()
    args = ap.parse_args(["new-project", "--name", "X", "--disc", "/tmp/x.cue"])
    check(args.region == "", "the parser itself carries no region default")

    def resolved(platform: str) -> str:
        platforms.set_current(platform)
        raw = (getattr(args, "region", None) or "").strip()
        return raw if platforms.current().key == "snes" else (raw or "USA")

    check(resolved("psx") == "USA", "PSX still resolves to USA when unset")
    check(resolved("snes") == "", "SNES resolves to blank — the header decides")
    platforms.set_current("snes")


def test_probe_rom_cli() -> None:
    """`probe-rom` must emit JSON on stdout, since the GUI parses it."""
    print("probe-rom CLI")
    import json as _json

    from project_studio import snes_paths

    if not snes_paths.probe_rom_script(None).is_file():
        print("  skip  no probe_rom.py available")
        return
    with tempfile.TemporaryDirectory() as td:
        rom = Path(td) / "Probe Me (USA).sfc"
        raw = bytearray(b"\x00" * 0x8000)
        raw[0x7FC0:0x7FC0 + 21] = b"PROBE ME".ljust(21, b" ")
        raw[0x7FD9] = 0x01
        rom.write_bytes(bytes(raw))
        proc = subprocess.run(
            [sys.executable, "-m", "project_studio", "--platform", "snes",
             "probe-rom", "--rom", str(rom)],
            cwd=str(_REPO / "tools" / "new_project_layout"),
            capture_output=True, text=True,
        )
        check(proc.returncode == 0, f"probe-rom exits 0 ({proc.stderr.strip()[:80]})")
        if proc.returncode != 0:
            return
        data = _json.loads(proc.stdout)
        check(data.get("display_name") == "Probe Me", "display_name comes from the filename")
        check(data.get("region") == "USA", "region decoded from the header byte")
        check(data.get("zip_prefix") == "probe-me", "zip_prefix slugged for packaging")
        check(bool(data.get("project_name")), "project_name offered for the GitHub repo field")


def test_dispatch_inputs() -> None:
    """`gh workflow run` rejects an --f the workflow does not declare.

    psxrecomp's release.yml takes four dispatch inputs; snesrecomp's takes
    none and releases off a tag. Sending PSX's four at a SNES repo fails the
    whole dispatch, so the flags are filtered to what the file declares.
    """
    print("release dispatch inputs")
    from project_studio import snes_paths
    from project_studio.gitops import declared_dispatch_inputs

    toolkit = _REPO / "tools" / "new_project_layout"
    psx_wf = toolkit / "ci_templates" / "setup-release.yml"
    snes_wf = snes_paths.vendored_wizard_dir() / "templates" / "release.yml.in"
    if psx_wf.is_file():
        psx_inputs = declared_dispatch_inputs(psx_wf)
        check("version" in psx_inputs and "bump" in psx_inputs,
              f"PSX workflow declares version/bump ({sorted(psx_inputs)})")
        check("publish" in psx_inputs, "PSX workflow declares publish")
    if snes_wf.is_file():
        check(
            declared_dispatch_inputs(snes_wf) == set(),
            "SNES workflow declares no dispatch inputs",
        )


def test_rom_discovery() -> None:
    """A ROM that lives outside the repo is still findable — by digest.

    The wizard bakes the dump's filename into tools/regen.sh and its directory
    nowhere, so a port scaffolded from ~/roms indexes with no image at all and
    both the Migrate field and "Regenerate C from ROM" come up empty. Recovery
    is allowed to search a known ROM folder, but only to *accept* a file whose
    CRC32 is the one this port was pinned to.
    """
    print("rom discovery")
    import zlib

    from project_studio import repo_index

    platforms.set_current("snes")
    # A tree of its own: the shared fixture has a probe ROM parked in it, and
    # the point here is the repo that contains no ROM at all.
    with tempfile.TemporaryDirectory() as lib_s, tempfile.TemporaryDirectory() as repo_s:
        lib = Path(lib_s)
        root = Path(repo_s)
        blob = bytes(range(256)) * 64
        crc = f"{zlib.crc32(blob) & 0xFFFFFFFF:08x}"
        (lib / "Zed (Japan).sfc").write_bytes(blob)
        (lib / "Wrong Game (USA).sfc").write_bytes(b"\x00" * 4096)

        regen = root / "tools" / "regen.sh"
        regen.parent.mkdir(parents=True, exist_ok=True)
        regen.write_text(
            "#!/usr/bin/env bash\n"
            f'EXPECTED_CRC32="${{SNESRECOMP_EXPECTED_CRC32:-{crc}}}"\n'
            '  for cand in "Zed (Japan).sfc" "zed.smc"; do\n'
            "    if [ -f \"$cand\" ]; then ROM=\"$cand\"; break; fi\n"
            "  done\n",
            encoding="utf-8",
        )

        check(
            repo_index.regen_rom_names(root) == ["Zed (Japan).sfc", "zed.smc"],
            "the filenames regen.sh accepts are recovered in its own order",
        )
        check(repo_index.regen_expected_crc32(root) == crc, "the pinned CRC32 is recovered")
        check(
            repo_index.discover_rom(root) == "",
            "no ROM in the tree and no library dir finds nothing",
        )
        check(
            repo_index.discover_rom(root, [lib]) == str(lib / "Zed (Japan).sfc"),
            "a named ROM in a known folder is found",
        )
        check(
            repo_index.discover_image(root, [lib]) == str(lib / "Zed (Japan).sfc"),
            "discover_image routes SNES to the ROM probe",
        )

        # Same name, wrong dump: the digest is what decides, not the filename.
        (lib / "Zed (Japan).sfc").write_bytes(b"\xff" * 4096)
        check(
            repo_index.discover_rom(root, [lib]) == "",
            "a name match whose CRC32 disagrees is refused, not returned",
        )

        # No pinned digest at all — nothing can prove a match, so nothing is claimed.
        regen.write_text('  for cand in "Zed (Japan).sfc"; do\n  done\n', encoding="utf-8")
        check(
            repo_index.discover_rom(root, [lib]) == "",
            "with no digest to check against, a library hit is not guessed at",
        )


def test_module_targets() -> None:
    """A --modules op covers THIS platform's framework, not always psxrecomp.

    The failure was silent in the worst direction: under --platform snes every
    module op reported "psxrecomp: checkout missing" and never touched
    snesrecomp, so a framework commit was never committed, pulled, or pushed —
    and CI then met a submodule pin the remote had never seen.
    """
    print("module targets")
    from project_studio import gitops

    platforms.set_current("snes")
    snes = gitops.default_module_paths()
    check("snesrecomp" in snes, f"a SNES session targets snesrecomp ({snes})")
    check("psxrecomp" not in snes, "and never psxrecomp")
    check("recomp-ui" in snes, "recomp-ui is shared by both consoles")

    platforms.set_current("psx")
    psx = gitops.default_module_paths()
    check("psxrecomp" in psx, f"a PSX session is unchanged ({psx})")
    check("snesrecomp" not in psx, "and never snesrecomp")

    # Nested modules live inside whichever framework is checked out, so their
    # paths are the same on both consoles.
    platforms.set_current("snes")
    nested = gitops.default_module_paths(nested=True)
    check(
        nested == ("lib/recomp-net", "lib/retcomm-rbengine"),
        f"nested module paths are console-independent ({nested})",
    )
    check(
        not hasattr(gitops, "KNOWN_SUBMODULES"),
        "the PSX-shaped constant is gone, so the bug cannot be re-imported",
    )


def test_launch_rom() -> None:
    """The recorded ROM reaches the runner's argv.

    A SNES runner takes the ROM as a positional and exits 1 with its usage
    without one, so a Launch that forgets to pass it looks exactly like a
    broken build. The path is already known — Migrate wrote it to the index —
    which is why nothing should have to restate it at launch time.
    """
    print("launch ROM")
    from project_studio import buildops, repo_index

    platforms.set_current("snes")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "ZedSNESRecomp"
        root.mkdir()
        rom = Path(td) / "zed.sfc"
        rom.write_bytes(b"\x00" * 64)
        idx = repo_index.RepoIndex(
            repos=[repo_index.RepoEntry(path=str(root), name="Zed", cue=str(rom))],
            path=Path(td) / "index.json",
        )
        real = repo_index.load_index
        repo_index.load_index = lambda path=None: idx  # type: ignore[assignment]
        try:
            got, whence = buildops.launch_rom_for(root)
            check(got == str(rom), f"the indexed ROM is what a launch runs ({got})")
            check(whence == "the repo index", "and the log says where it came from")

            got, whence = buildops.launch_rom_for(root, "/elsewhere/other.sfc")
            check(got == "/elsewhere/other.sfc", "an explicit --rom wins over the index")
            check(whence == "explicit", "an explicit ROM is not announced as recovered")

            idx.repos[0].cue = ""
            check(
                buildops.launch_rom_for(root) == ("", ""),
                "a repo with no recorded ROM adds no positional",
            )

            idx.repos[0].cue = str(rom)
            platforms.set_current("psx")
            check(
                buildops.launch_rom_for(root) == ("", ""),
                "PSX launches are untouched — a disc is not a runner argument",
            )
        finally:
            repo_index.load_index = real  # type: ignore[assignment]
            platforms.set_current("snes")


def test_new_project_command() -> None:
    print("new-project command")
    from project_studio import newproject as np

    platforms.set_current("snes")
    with tempfile.NamedTemporaryFile(suffix=".sfc", delete=False) as fh:
        fh.write(b"\0" * 1024)
        rom = fh.name
    try:
        opts = np.NewProjectOptions(
            platform="snes",
            name="Zed",
            disc=rom,
            parent_dir=tempfile.gettempdir(),
            players=4,
            enable_rollback=True,
            bios="/nonexistent/bios.bin",
            description="A mech brawler.",
            publisher="Konami",
            year="1995",
            region="",
            dry_run=True,
        )
        errs = np.validate_options(opts)
        check(errs == [], f"a SNES scaffold with a ROM validates ({errs})")
        cmd, env = np.build_command(opts)
        check("--rom" in cmd, "the ROM is passed as --rom, not --disc")
        check("--rollback" in cmd and "--netplay" in cmd, "rollback implies netplay in the argv")
        check("--bios" not in cmd, "the PSX BIOS field never reaches the SNES scaffolder")
        check(env.get("SNESRECOMP_SETUP_YES") == "1", "non-interactive env is set")
        check("BIOS" in np.snes_ignored_fields(opts), "ignored PSX fields are reported, not dropped")

        # The prompts the wizard grew. Studio always runs it with --yes, which
        # takes every default, so a field that does not become a flag is lost
        # in silence — the failure this pair of checks exists to catch.
        pairs = list(zip(cmd, cmd[1:]))
        check(("--description", "A mech brawler.") in pairs, "description reaches the argv")
        check(("--publisher", "Konami") in pairs, "publisher reaches the argv")
        check(("--year", "1995") in pairs, "year reaches the argv")
        check(
            "--region" not in cmd,
            "a blank region is omitted so the cartridge header decides",
        )
        for label in ("Description", "Publisher", "Year"):
            check(
                label not in np.snes_ignored_fields(opts),
                f"{label} is no longer reported as ignored",
            )

        opts.region = "JPN"
        argv = np.build_command(opts)[0]
        check(
            ("--region", "JPN") in list(zip(argv, argv[1:])),
            "an explicit region overrides the header",
        )

        opts.disc = ""
        check(
            any("ROM" in e for e in np.validate_options(opts)),
            "a missing image is reported in the platform's own words",
        )
    finally:
        os.unlink(rom)


def test_snes_functions() -> None:
    """The Functions tab's data layer: read the manifest, edit only symbols.toml.

    The write path is a line scanner, not a TOML round-trip, because the
    comments in symbols.toml are where the reasons live — "held at emit = false
    because ..." is the most valuable content in the file, and every TOML
    writer reflows it away. So the thing worth testing is that an add followed
    by a remove leaves the file byte-identical.
    """
    print("snes functions")
    from project_studio import snes_analyzeops as sa

    platforms.set_current("snes")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "recomp").mkdir()
        (root / "src" / "gen").mkdir(parents=True)
        original = (
            "# Progressive symbol map.\n"
            "# Held at emit = false on purpose — see the note.\n"
            "\n"
            "[[func]]\n"
            'name = "I_RESET"\n'
            'addr = "8000"\n'
            "bank = 0\n"
            "emit = false\n"
            'note = "Emulation RESET vector"\n'
        )
        syms = root / "recomp" / "symbols.toml"
        syms.write_text(original, encoding="utf-8")
        (root / "src" / "gen" / "program_manifest.json").write_text(
            json.dumps({
                "format_version": 3,
                "roots": [{"pc24": 0x8000, "m": 1, "x": 1}],
                "nodes": {
                    "008000:M1X1": {
                        "key": "008000:M1X1", "min_pc24": 0x8000, "max_pc24": 0x8075,
                        "disposition": "lle_only", "instruction_count": 51,
                        "reasons": ["unproven_callee_exit"],
                        "demands": [{"kind": "direct_call", "resolution": "aot_exact"}],
                    },
                    "00828A:M0X0": {
                        "key": "00828A:M0X0", "min_pc24": 0x828A, "max_pc24": 0x8295,
                        "disposition": "aot_eligible", "instruction_count": 11,
                        "reasons": [], "demands": [],
                    },
                },
            }),
            encoding="utf-8",
        )

        man = sa.read_manifest(root)
        check(man["present"], "the manifest is read")
        check(len(man["nodes"]) == 2, f"both nodes surface ({len(man['nodes'])})")
        check(man["counts"] == {"lle_only": 1, "aot_eligible": 1},
              f"dispositions are counted ({man['counts']})")
        check(len(man["unproven"]) == 1, "the unproven worklist holds the one with reasons")
        named = next(n for n in man["nodes"] if n["pc"] == "008000")
        check(named["name"] == "I_RESET", "a node is joined to its symbols.toml name")
        check(named["emit"] is False, "and to its emit state")
        unnamed = next(n for n in man["nodes"] if n["pc"] == "00828A")
        check(unnamed["emit"] is None, "a node with no symbol reports emit as unset")

        # Promote in place: the entry changes, the comments do not.
        r = sa.set_symbol(root, "8000", emit=True)
        check(r.ok, f"emit can be flipped ({r.message})")
        body = syms.read_text(encoding="utf-8")
        check("emit = true" in body, "the flag is written")
        check("# Held at emit = false on purpose" in body, "comments survive the edit")
        check('note = "Emulation RESET vector"' in body, "so does the note")
        check(body.count("[[func]]") == 1, "no duplicate entry was appended")

        # Bare hex, 0x-prefixed and bank:offset all name the same function.
        for form in ("0x8000", "00:8000", "8000"):
            check(sa._norm_pc(form) == "8000", f"{form} normalises to 8000")

        sa.set_symbol(root, "8000", emit=False)
        r = sa.set_symbol(root, "828A", name="sub_828A", emit=True)
        check(r.ok, "a new function can be added")
        check(len(sa.read_symbols(root)) == 2, "both entries are present")
        r = sa.clear_symbol(root, "828a")
        check(r.ok, "and removed again, case-insensitively")
        check(
            syms.read_text(encoding="utf-8") == original,
            "add + remove leaves the file byte-identical",
        )


def main() -> int:
    if not subprocess.run(["git", "--version"], capture_output=True).returncode == 0:
        print("git not available — skipping")
        return 0
    test_platform_defaults()
    test_index_separation()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "ZedSNESRecomp"
        make_repo(root)
        test_repo_recognition(root)
        test_audit(root)
        test_parity_checks(root)
        test_version_stamp(root)
        test_plan_and_apply(root)
        test_digest_recovery(root)
        test_probe_rom(root)
    test_rom_discovery()
    test_region_default()
    test_probe_rom_cli()
    test_dispatch_inputs()
    test_new_project_command()
    test_launch_rom()
    test_module_targets()
    test_snes_functions()
    print("FAILED" if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
