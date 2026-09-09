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
    check(by_id["identity_carrier"].status.value == "fail",
          "a missing ROM identity carrier fails")
    check(by_id["identity_carrier"].fix_op is None,
          "the carrier is NOT offered as a fix without digests")
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
    # Which carrier is incomplete depends on the wizard this port is pinned to
    # — codegen_setup.c without its header, or a rom_identity.txt that does not
    # exist yet — but either way it fails and names an op that can be applied.
    layout = snesops.identity_layout(root)
    check(layout in ("file", "codegen"),
          f"the wizard declares an identity layout ({layout or 'neither'})")
    expected_op = {"file": "snes_emit_rom_identity",
                   "codegen": "snes_emit_codegen_setup"}[layout]
    check(mid["identity_carrier"].status.value == "fail",
          "an incomplete carrier still fails (the build needs all of it)")
    check(mid["identity_carrier"].fix_op == expected_op,
          f"and names the emit op this wizard can honour ({expected_op})")

    # rom_identity.txt is the current carrier: digests recover from it, and it
    # outranks a codegen_setup.c the older layout left behind.
    (root / "rom_identity.txt").write_text(
        "# ROM identity\n"
        "display_name    = Zed\n"
        'rom_file        = "Zed (World).sfc"\n'
        "expected_crc32  = deadbeef\n"
        "expected_sha256 = bb\n"
        "mapping         = hirom\n"
        "region          = PAL\n",
        encoding="utf-8",
    )
    fid = snesops.rom_identity(root)
    check(fid.get("crc32") == "deadbeef", "digests recover from rom_identity.txt")
    check(fid.get("rom_file") == "Zed (World).sfc",
          "a quoted value is unquoted, as regen.sh identity_get does")
    check(fid.get("mapping") == "hirom",
          "rom_identity.txt outranks a stale codegen_setup.c")
    if layout == "file":
        fchecks = {c.id: c for c in snesops.audit_project(root).checks}
        check(fchecks["identity_carrier"].status.value == "pass",
              "a filled rom_identity.txt passes")
        check(fchecks["identity_stale"].status.value == "warn",
              "and a superseded codegen_setup.c is named, not silently kept")
    (root / "rom_identity.txt").unlink()

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


def test_netplay_flip(root: Path) -> None:
    print("netplay flip")
    from project_studio import snesops

    cml = root / "CMakeLists.txt"
    base = cml.read_text(encoding="utf-8")
    # The scaffold fixture has no launcher call; the op appends after
    # add_executable-derived target discovery... it needs add_executable.
    cml.write_text(base + "add_executable(ZedSNESRecomp src/main.c)\n",
                   encoding="utf-8")

    plan = snesops.build_plan(root, MigrateOptions(dry_run=True))
    check(not any("netplay" in st.op_id for st in plan.steps),
          "no netplay op planned without the flag")
    plan = snesops.build_plan(root, MigrateOptions(enable_netplay=True))
    check("snes_enable_netplay" in [st.op_id for st in plan.steps],
          "--enable-netplay plans the enable op")
    plan = snesops.build_plan(root,
                              MigrateOptions(enable_netplay=True, players=1))
    check("snes_enable_netplay" not in [st.op_id for st in plan.steps],
          "1-player titles cannot opt in")

    res = snesops._op_enable_netplay(root, MigrateOptions())
    text = cml.read_text(encoding="utf-8")
    check(res.ok and "snesrecomp_enable_recomp_net(ZedSNESRecomp)" in text,
          "enable wires the call with the discovered target")
    audit = {c.id: c for c in snesops.audit_project(root).checks}
    check(audit["netplay"].status.value == "pass",
          "audit reports netplay wired")

    res = snesops._op_disable_netplay(root, MigrateOptions())
    text = cml.read_text(encoding="utf-8")
    check(res.ok and "# snesrecomp_enable_recomp_net(" in text,
          "disable comments the call out")
    res = snesops._op_enable_netplay(root, MigrateOptions())
    text = cml.read_text(encoding="utf-8")
    check(res.ok and "\nsnesrecomp_enable_recomp_net(" in text.replace(
              "# snesrecomp", "XX"),
          "re-enable uncomments rather than duplicating")
    check(text.count("snesrecomp_enable_recomp_net(") == 1,
          "exactly one call after the round trip")
    cml.write_text(base, encoding="utf-8")


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

    # The op itself, not just the probe behind it. This is the one that died
    # with "Template not found: .../codegen_setup.c.in" once snesrecomp moved
    # identity into rom_identity.txt: it must write whatever carrier the wizard
    # in front of it scaffolds, and never name a template that wizard lacks.
    res = snesops._op_probe_rom_refresh(root, MigrateOptions(disc=str(rom), force=True))
    check(res.ok, f"probe refresh applies against the live wizard ({res.message})")
    for _tpl, rel in snesops.identity_carriers(root):
        check((root / rel).is_file(), f"probe refresh wrote {rel}")
    regen_after = (root / "tools" / "regen.sh").read_text(encoding="utf-8")
    check(ident["crc32"] in regen_after
          or "identity_get expected_crc32" in regen_after,
          "regen.sh either carries the fresh CRC32 or reads it from the file")

_CODEGEN_SETUP_C = """const GameCodegenIdentity kGameCodegenIdentity = {
    .display_name   = "@DISPLAY_NAME@",
    .rom_file       = "@ROM_FILE@",
    .expected_crc32 = "@ROM_CRC32@",
    .expected_sha256= "@ROM_SHA256@",
    .mapping        = "@ROM_MAPPING@",
    .region         = "@REGION@",
};
"""

_ROM_IDENTITY_TXT = """# ROM identity for @DISPLAY_NAME@.
display_name    = @DISPLAY_NAME@
rom_file        = @ROM_FILE@
expected_crc32  = @ROM_CRC32@
expected_sha256 = @ROM_SHA256@
mapping         = @ROM_MAPPING@
region          = @REGION@
"""

_REGEN_SH = 'EXPECTED_CRC32="${SNESRECOMP_EXPECTED_CRC32:-@ROM_CRC32@}"\n'


def _fake_wizard(base: Path, templates: dict[str, str]) -> Path:
    """The smallest thing snes_paths will accept as a snesrecomp checkout."""
    (base / "runner").mkdir(parents=True, exist_ok=True)
    (base / "runner" / "runner.cmake").write_text("", encoding="utf-8")
    wiz = base / "tools" / "new_project"
    (wiz / "templates").mkdir(parents=True, exist_ok=True)
    (wiz / "setup_project.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    for name, body in templates.items():
        (wiz / "templates" / name).write_text(body, encoding="utf-8")
    return base


def _seed_port(root: Path) -> None:
    """A port already carrying identity, in the shape the old wizard left."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "codegen_setup.c").write_text(
        'const GameCodegenIdentity kGameCodegenIdentity = {\n'
        '    .display_name   = "Zed",\n'
        '    .rom_file       = "Zed (USA).sfc",\n'
        '    .expected_crc32 = "deadbeef",\n'
        '    .expected_sha256= "abc123",\n'
        '    .mapping        = "lorom",\n'
        '    .region         = "USA",\n'
        "};\n",
        encoding="utf-8",
    )


def test_identity_layouts() -> None:
    """Studio follows the wizard's identity carrier, across the move.

    snesrecomp replaced src/codegen_setup.c/.h with a single rom_identity.txt
    that CMake, regen.sh and the release workflow all read. Studio drives
    whichever wizard a port is *pinned* to, so both eras run through the same
    code — and neither may name a template the other lacks. Hardcoding the old
    pair is what made a migration against a current checkout die with
    "Template not found: .../codegen_setup.c.in".

    Both wizards are synthetic on purpose: this must keep testing the skew long
    after every checkout on this machine has moved past it.
    """
    print("identity layouts")
    from project_studio import snesops

    cases = (
        ("file",
         {"rom_identity.txt.in": _ROM_IDENTITY_TXT, "regen.sh.in": _REGEN_SH},
         ["rom_identity.txt"],
         "snes_emit_rom_identity"),
        ("codegen",
         {"codegen_setup.c.in": _CODEGEN_SETUP_C,
          "codegen_setup.h.in": "/* @DISPLAY_NAME@ */\n",
          "regen.sh.in": _REGEN_SH},
         ["src/codegen_setup.c", "src/codegen_setup.h"],
         "snes_emit_codegen_setup"),
    )
    prev = os.environ.get("SNESRECOMP_ROOT")
    try:
        for layout, templates, rels, emit_op in cases:
            with tempfile.TemporaryDirectory() as td:
                os.environ["SNESRECOMP_ROOT"] = str(
                    _fake_wizard(Path(td) / "snesrecomp", templates))
                root = Path(td) / "Port"
                _seed_port(root)

                check(snesops.identity_layout(root) == layout,
                      f"{layout}: the wizard's templates decide the layout")
                check([r for _, r in snesops.identity_carriers(root)] == rels,
                      f"{layout}: carriers are {', '.join(rels)}")

                by_id = {c.id: c for c in snesops.audit_project(root).checks}
                check(by_id["identity_carrier"].fix_op == emit_op,
                      f"{layout}: the audit names {emit_op}")

                res = snesops._OPS[emit_op](root, MigrateOptions(force=True))
                check(res.ok, f"{layout}: {emit_op} applies ({res.message})")
                for rel in rels:
                    body = (root / rel).read_text(encoding="utf-8")
                    check("@" not in body, f"{layout}: no @TOKEN@ survives in {rel}")
                check("deadbeef" in (root / rels[0]).read_text(encoding="utf-8"),
                      f"{layout}: the recovered CRC32 is carried across, not re-derived")

                # The legacy op id routes to whatever this wizard scaffolds; it
                # must never reach for a template the wizard dropped.
                legacy = snesops._op_emit_codegen_setup(root, MigrateOptions(force=True))
                check(legacy.ok,
                      f"{layout}: the legacy op id still applies ({legacy.message})")

                after = {c.id: c for c in snesops.audit_project(root).checks}
                check(after["identity_carrier"].status.value == "pass",
                      f"{layout}: the carrier passes once written")
                check(("identity_stale" in after) == (layout == "file"),
                      f"{layout}: a superseded codegen_setup.c is named only "
                      "when the framework has moved past it")

        # A wizard offering neither template is a broken tool, and the ops say
        # so by name instead of surfacing a bare missing-file path.
        with tempfile.TemporaryDirectory() as td:
            os.environ["SNESRECOMP_ROOT"] = str(
                _fake_wizard(Path(td) / "snesrecomp", {"regen.sh.in": _REGEN_SH}))
            root = Path(td) / "Port"
            _seed_port(root)
            check(snesops.identity_layout(root) == "",
                  "neither template present → no layout claimed")
            res = snesops._op_emit_rom_identity(root, MigrateOptions(force=True))
            check(not res.ok and "neither rom_identity.txt.in" in res.message,
                  f"and the op refuses by name ({res.message})")
            stuck = {c.id: c for c in snesops.audit_project(root).checks}
            check(stuck["identity_carrier"].fix_op is None,
                  "the audit offers no fix op it could not honour")
    finally:
        if prev is None:
            os.environ.pop("SNESRECOMP_ROOT", None)
        else:
            os.environ["SNESRECOMP_ROOT"] = prev

def test_readme_toggle() -> None:
    """The README & About switch gates the audit row, not just the op.

    One switch for both because one op writes both: README.md's badge, boxart,
    launcher and R.A.I.D. blocks *and* the repository's GitHub About blurb. A
    port that hand-writes its README should stop being told about it on every
    run, so the row goes to SKIP rather than disappearing — a row that vanishes
    reads as "nothing to do here", which is the opposite of what was asked for.

    Both consoles, because the two migrations are independent implementations
    and a switch wired into only one of them is the bug this guards against.
    """
    print("README & About toggle")
    from project_studio import detect, plan as psx_plan, snesops

    backends = (
        ("snes", snesops.audit_project, snesops.build_plan, "snes_patch_readme_metrics"),
        ("psx", detect.audit_project, psx_plan.build_plan, "patch_readme_metrics"),
    )
    for name, audit, build, op in backends:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Port"
            root.mkdir()
            # No README at all: the loudest thing the check can say, so a SKIP
            # here cannot be mistaken for the repo simply being clean.
            on = {c.id: c for c in audit(root).checks}["readme_metrics"]
            check(on.status.value == "warn", f"{name}: on → the row warns")
            check(on.fix_op == op, f"{name}: on → the row names {op}")

            off_opts = MigrateOptions(patch_readme=False)
            off = {c.id: c for c in audit(root, off_opts).checks}["readme_metrics"]
            check(off.status.value == "skip", f"{name}: off → the row is SKIP, not absent")
            check(off.fix_op is None, f"{name}: off → the row names no op")
            check(off.severity.value == "info",
                  f"{name}: off → INFO, so it stops counting toward the layout class")

            steps_on = [st.op_id for st in build(root, MigrateOptions()).steps]
            check(op in steps_on, f"{name}: on → {op} is planned")
            steps_off = [st.op_id for st in build(root, off_opts).steps]
            check(op not in steps_off, f"{name}: off → {op} is not planned")
            # --only is the explicit escape hatch every other switch honours;
            # asking for the op by name still gets it.
            forced = [st.op_id for st in build(root, MigrateOptions(
                patch_readme=False, only=[op])).steps]
            check(op in forced, f"{name}: off → --only {op} still plans it")

_FAKE_CLI = """#!/usr/bin/env python3
import argparse
ap = argparse.ArgumentParser(prog="snesrecomp")
sub = ap.add_subparsers(dest="command", required=True)
%s
ap.parse_args()
"""


def _fake_framework(base: Path, commands: tuple[str, ...]) -> Path:
    """A snesrecomp checkout whose CLI offers exactly `commands`."""
    base.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f'sub.add_parser({c!r})' for c in commands)
    (base / "snesrecomp_cli.py").write_text(_FAKE_CLI % body, encoding="utf-8")
    return base


_REGEN_SH_MODERN = """#!/usr/bin/env bash
IDENTITY="$ROOT/rom_identity.txt"
"$PYTHON" "$CLI" verify-rom --rom "$ROM"
"$PYTHON" "$CLI" generate --rom "$ROM"
"""


def test_generate_preflight() -> None:
    """Generate refuses, by name, when the pinned framework cannot run regen.sh.

    A port forked from GitHub carries a regen.sh emitted by whatever wizard was
    current, and a snesrecomp submodule pinned to whatever that fork pointed at.
    When those disagree the raw failure is an argparse "invalid choice:
    'verify-rom'" with exit 2, attributed to Studio's Generate button. The
    preflight has to name the skew instead — and it must ask the CLI what it
    supports rather than assume, because assuming is how this happened.
    """
    print("generate preflight")
    from project_studio import buildops

    prev = os.environ.get("SNESRECOMP_ROOT")
    os.environ.pop("SNESRECOMP_ROOT", None)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Port"
            (root / "tools").mkdir(parents=True)
            (root / "tools" / "regen.sh").write_text(_REGEN_SH_MODERN, encoding="utf-8")

            # 1. No framework checked out at all.
            r = buildops.preflight_snes_generate(root)
            check(r is not None and "git submodule update" in r.message,
                  f"an absent snesrecomp names the submodule command ({r.message[:48]}…)")

            # 2. The reported case: a framework older than its own regen.sh.
            fw = _fake_framework(root / "snesrecomp", ("build",))
            check(buildops.snes_cli_commands(fw / "snesrecomp_cli.py") == {"build"},
                  "the CLI is asked what it supports, not assumed")
            r = buildops.preflight_snes_generate(root)
            check(r is not None and "older than its own" in r.message,
                  "an old framework is named as skew, not as an argparse error")
            check(r is not None and "'verify-rom'" in r.message and "'generate'" in r.message,
                  "and both missing subcommands are named")

            # 3. --no-verify does not need verify-rom, but still needs generate.
            r = buildops.preflight_snes_generate(root, verify=False)
            check(r is not None and "'verify-rom'" not in r.message,
                  "no-verify stops asking for verify-rom")
            check(r is not None and "'generate'" in r.message,
                  "but generate is still required")

            # 4. A current framework, but no digests for --verify to check.
            _fake_framework(root / "snesrecomp", ("build", "generate", "verify-rom"))
            r = buildops.preflight_snes_generate(root)
            check(r is not None and "rom_identity.txt" in r.message and "is missing" in r.message,
                  "an absent rom_identity.txt is caught before regen.sh verifies nothing")
            (root / "rom_identity.txt").write_text(
                "expected_crc32  =\nexpected_sha256 =\n", encoding="utf-8")
            r = buildops.preflight_snes_generate(root)
            check(r is not None and "carries no digests" in r.message,
                  "and so is one that carries empty digests")

            # 5. Everything in place — the preflight gets out of the way.
            (root / "rom_identity.txt").write_text(
                "expected_crc32  = deadbeef\nexpected_sha256 = abc123\n", encoding="utf-8")
            check(buildops.preflight_snes_generate(root) is None,
                  "a pinned framework that can run regen.sh is not blocked")
            check(buildops.preflight_snes_generate(root, verify=False) is None,
                  "and neither is the no-verify path")

            # 6. Whatever the preflight cannot foresee is still read, not
            #    passed through as somebody else's stack trace.
            hint = buildops.diagnose_generate_failure(
                "snesrecomp: error: argument command: invalid choice: 'verify-rom' "
                "(choose from build)", root)
            check(hint is not None and "predates" in hint,
                  "the raw argparse error is translated after the fact too")
    finally:
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev

def test_regen_framework_skew() -> None:
    """Studio must not write a regen.sh the port's own snesrecomp cannot run.

    This is the defect behind the Generate failure, not just its symptom. A
    fork carries whatever snesrecomp gitlink its parent recorded; Studio drives
    whichever wizard it can find, which on such a fork is a sibling or the
    vendored copy. Emitting that wizard's regen.sh into the fork bakes in calls
    the pinned CLI has never heard of, and nothing notices until somebody
    presses Generate. Refusing to write the file is the fix — writing it anyway
    only moves the failure somewhere less legible.
    """
    print("regen.sh vs pinned framework")
    from project_studio import buildops, snes_paths, snesops

    prev = os.environ.get("SNESRECOMP_ROOT")
    os.environ.pop("SNESRECOMP_ROOT", None)
    try:
        # Read out of the script's own call sites: prose naming a command is
        # not a call, which is what `echo "regen.sh: $CLI missing"` looks like.
        text = (
            'echo "regen.sh: $CLI missing — run: git submodule update" >&2\n'
            '"$PYTHON" "$CLI" verify-rom --rom "$ROM"\n'
            '"$PYTHON" "$CLI" generate --rom "$ROM"\n'
        )
        check(snes_paths.regen_cli_commands(text) == ["verify-rom", "generate"],
              "regen.sh's calls are read from its call sites, not from prose")
        check("missing" not in snes_paths.regen_cli_commands(text),
              "an echo mentioning $CLI is not mistaken for a subcommand")
        check(snes_paths.regen_cli_commands(text, verify=False) == ["generate"],
              "--no-verify drops the one call regen.sh gates on verification")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Fork"
            (root / "tools").mkdir(parents=True)
            (root / "tools" / "regen.sh").write_text(text, encoding="utf-8")
            _fake_framework(root / "snesrecomp", ("build",))

            gap = snes_paths.regen_framework_gap(root, text)
            check(gap is not None and gap[0] == ["verify-rom", "generate"],
                  "the gap names what the pinned CLI is missing")
            check(gap is not None and gap[1] == {"build"},
                  "and what it does offer")

            # The audit says so on the Migrate tab, before Generate is pressed.
            by_id = {c.id: c for c in snesops.audit_project(root).checks}
            check(by_id["regen_framework"].status.value == "fail",
                  "the audit fails on the skew")
            check(by_id["regen_framework"].fix_op is None,
                  "and offers no fix op — moving a framework pin is a human's call")
            check(by_id["wizard_source"].status.value == "warn",
                  "and names the wizard being driven, which is where this came from")

            # The emit ops refuse rather than overwrite with a broken script.
            before = (root / "tools" / "regen.sh").read_text(encoding="utf-8")
            r = snesops._op_emit_regen(root, MigrateOptions(force=True))
            check(not r.ok and "would not run against" in r.message,
                  f"emit regen.sh refuses ({r.message[:56]}…)")
            check((root / "tools" / "regen.sh").read_text(encoding="utf-8") == before,
                  "and leaves the existing script untouched")

            # One wording, wherever the skew is met.
            cli = snes_paths.regen_framework_root(root) / "snesrecomp_cli.py"
            shared = buildops.framework_gap_message(cli, ["generate"], {"build"})
            check("that has it" in shared, "a single missing command reads as 'it'")
            check("that has them" in buildops.framework_gap_message(
                cli, ["generate", "verify-rom"], {"build"}),
                  "and two or more as 'them'")

            # A framework that can run it is not blocked.
            _fake_framework(root / "snesrecomp", ("build", "generate", "verify-rom"))
            check(snes_paths.regen_framework_gap(root, text) is None,
                  "a current framework reports no gap")
            ok_ids = {c.id: c for c in snesops.audit_project(root).checks}
            check(ok_ids["regen_framework"].status.value == "pass",
                  "and the audit row passes")
    finally:
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev

_CLI_WITH = """import argparse
ap = argparse.ArgumentParser(prog="snesrecomp")
s = ap.add_subparsers(dest="command", required=True)
%s
ap.parse_args()
"""

# A local `file://` submodule is refused by default (git's CVE-2022-39253
# mitigation). Injected through the environment rather than written into the
# user's global config, and only for the fixture.
_FILE_PROTOCOL_ENV = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "protocol.file.allow",
    "GIT_CONFIG_VALUE_0": "always",
}


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return (r.stdout or "").strip()


def _stale_fork(base: Path) -> tuple[Path, str, str]:
    """A port pinned to a framework revision older than its own regen.sh.

    Real git objects, not a mock: the thing under test is whether a *gitlink*
    moves and gets staged, and a fake directory cannot answer that.
    """
    env = {**os.environ, **_FILE_PROTOCOL_ENV,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def run(cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True)

    fw = base / "fw"
    fw.mkdir(parents=True)
    run(fw, "init", "-q", "-b", "main")
    (fw / "runner").mkdir()
    (fw / "runner" / "runner.cmake").write_text("", encoding="utf-8")
    (fw / "snesrecomp_cli.py").write_text(
        _CLI_WITH % 's.add_parser("build")', encoding="utf-8")
    # A module one level down, where a PSX port keeps most of what it pins.
    lib = base / "netlib"
    lib.mkdir()
    run(lib, "init", "-q", "-b", "main")
    (lib / "v.txt").write_text("1\n", encoding="utf-8")
    run(lib, "add", "-A")
    run(lib, "commit", "-qm", "lib old")
    run(fw, "submodule", "add", "-q", "-b", "main", str(lib), "lib/recomp-net")
    run(fw, "add", "-A")
    run(fw, "commit", "-qm", "old")
    old = _git(fw, "rev-parse", "HEAD")
    (lib / "v.txt").write_text("2\n", encoding="utf-8")
    run(lib, "add", "-A")
    run(lib, "commit", "-qm", "lib new")
    lib_new = _git(lib, "rev-parse", "HEAD")
    (fw / "snesrecomp_cli.py").write_text(
        _CLI_WITH % ('s.add_parser("build")\ns.add_parser("generate")\n'
                     's.add_parser("verify-rom")'), encoding="utf-8")
    run(fw, "add", "-A")
    run(fw, "commit", "-qm", "new")
    new = _git(fw, "rev-parse", "HEAD")

    port = base / "port"
    port.mkdir()
    run(port, "init", "-q", "-b", "main")
    run(port, "submodule", "add", "-q", "-b", "main", "../fw", "snesrecomp")
    run(port / "snesrecomp", "checkout", "-q", old)
    (port / "tools").mkdir()
    (port / "tools" / "regen.sh").write_text(
        '"$PYTHON" "$CLI" verify-rom --rom "$ROM"\n'
        '"$PYTHON" "$CLI" generate --rom "$ROM"\n', encoding="utf-8")
    run(port, "add", "-A")
    run(port, "commit", "-qm", "init")
    return port, old, new, lib_new


def test_advance_pins() -> None:
    """Moving a stale fork's framework pin, which `submodule update` cannot do.

    `git submodule update` checks out the gitlink the superproject *already
    records*, so on a fork carrying an old pin it puts the old revision back —
    which is why reaching for it leaves the pin exactly where it was. Advancing
    is a different operation, and the half that is easy to forget is staging
    the new gitlink: without it nothing about the superproject has changed.
    """
    print("advance framework pins")
    from project_studio import gitops, snesops

    prev = os.environ.get("SNESRECOMP_ROOT")
    os.environ.pop("SNESRECOMP_ROOT", None)
    saved = {k: os.environ.get(k) for k in _FILE_PROTOCOL_ENV}
    os.environ.update(_FILE_PROTOCOL_ENV)
    try:
        with tempfile.TemporaryDirectory() as td:
            port, old, new, lib_new = _stale_fork(Path(td))
            check(old != new and len(old) == 40, "the fixture has two real revisions")
            check(_git(port / "snesrecomp", "rev-parse", "HEAD") == old,
                  "the port starts pinned to the old one")

            before = {c.id: c for c in snesops.audit_project(port).checks}
            check(before["regen_framework"].status.value == "fail",
                  "and the audit fails on the skew")

            # The operation people reach for first, and what it actually does.
            gitops.update_submodules(port, paths=["snesrecomp"])
            check(_git(port / "snesrecomp", "rev-parse", "HEAD") == old,
                  "`submodule update` puts the recorded pin back — it cannot advance")

            dry = gitops.advance_submodule_pins(port, paths=["snesrecomp"], dry_run=True)
            check(all(r.ok for r in dry) and "would advance" in dry[0].message,
                  "dry-run says what it would do")
            check(_git(port / "snesrecomp", "rev-parse", "HEAD") == old,
                  "and moves nothing")

            res = gitops.advance_submodule_pins(port, paths=["snesrecomp"])
            check(all(r.ok for r in res), f"advance succeeds ({res[0].message})")
            check(_git(port / "snesrecomp", "rev-parse", "HEAD") == new,
                  "the checkout is at the tracked branch tip")
            check(old[:9] in res[0].message and new[:9] in res[0].message,
                  "and the move is reported as from → to, not just 'done'")
            staged = _git(port, "diff", "--cached", "--name-only")
            check("snesrecomp" in staged,
                  "the new gitlink is staged, so the superproject records the move")
            check(_git(port, "log", "--oneline", "-1", "--format=%s") == "init",
                  "but nothing is committed — the pin change stays reviewable")

            after = {c.id: c for c in snesops.audit_project(port).checks}
            check(after["regen_framework"].status.value == "pass",
                  "and the audit that flagged the skew now passes")

            again = gitops.advance_submodule_pins(port, paths=["snesrecomp"])
            check(all(r.ok for r in again) and "already at" in again[0].message,
                  "a second run is a no-op that says so")

            gone = gitops.advance_submodule_pins(port, paths=["recomp-ui"])
            check(not gone[0].ok and "Ensure submodules first" in gone[0].message,
                  "a module that is not checked out names the op that fixes it")

            # Nested: the PSX shape, where what a port pins lives inside the
            # framework rather than beside it. Same op, one level down.
            fw_dir = port / "snesrecomp"
            nested = gitops.advance_submodule_pins(
                port, paths=["lib/recomp-net"], nested=True)
            check(all(r.ok for r in nested), f"nested advance succeeds ({nested[0].message})")
            check(_git(fw_dir / "lib" / "recomp-net", "rev-parse", "HEAD") == lib_new,
                  "the nested checkout moves to its tracked tip")
            check("staged in snesrecomp" in nested[0].message,
                  "and the message says which repo the gitlink was staged in")
            check("lib/recomp-net" in _git(fw_dir, "diff", "--cached", "--name-only"),
                  "the gitlink is staged inside the framework, not the game repo")
            check("lib/recomp-net" not in _git(port, "diff", "--cached", "--name-only"),
                  "the game repo records nothing until the framework is committed")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev

def test_module_urls() -> None:
    """Repointing a module at a fork, without committing it for everyone.

    Three settings answer "which repo is this" and they are not the same one:
    the tracked `.gitmodules` URL, this clone's `.git/config` override, and the
    checkout's own `origin`. A contributor working from a fork needs the last
    two moved and the first left alone — committing their fork into the port
    would repoint it for everybody who clones it. The scope is therefore the
    caller's to state, and the default is the one that cannot surprise anyone.
    """
    print("module remote URLs")
    from project_studio import gitops

    prev = os.environ.get("SNESRECOMP_ROOT")
    os.environ.pop("SNESRECOMP_ROOT", None)
    saved = {k: os.environ.get(k) for k in _FILE_PROTOCOL_ENV}
    os.environ.update(_FILE_PROTOCOL_ENV)
    FORK = "https://github.com/alex/snesrecomp.git"
    try:
        with tempfile.TemporaryDirectory() as td:
            port, _old, _new, _lib = _stale_fork(Path(td))
            rows = {r.path: r for r in gitops.module_urls(port)}
            check("snesrecomp" in rows and "lib/recomp-net" in rows,
                  "both levels are listed — submodules and nested modules")
            check(rows["lib/recomp-net"].nested,
                  "and the nested one is marked as such")
            check(rows["recomp-ui"].present is False,
                  "a module that is not checked out still gets a row to edit")
            tracked_before = _git(port, "show", "HEAD:.gitmodules")

            bad = gitops.set_module_url(port, "snesrecomp", "my fork")
            check(not bad.ok and "does not look like a git remote" in bad.message,
                  "a URL that is not a URL is refused before anything is written")

            scoped = gitops.set_module_url(port, "snesrecomp", FORK, scope="nonsense")
            check(not scoped.ok and "Unknown scope" in scoped.message,
                  "and so is an unknown scope")

            r = gitops.set_module_url(port, "snesrecomp", FORK)
            check(r.ok, f"local scope applies ({r.message})")
            after = {x.path: x for x in gitops.module_urls(port)}
            check(after["snesrecomp"].origin_url == FORK,
                  "origin moves, so push and pull go to the fork")
            check(after["snesrecomp"].local_url == FORK,
                  "the .git/config override moves, so submodule update follows")
            check(after["snesrecomp"].gitmodules_url != FORK,
                  "but .gitmodules is untouched")
            check(_git(port, "diff", "--name-only", "--", ".gitmodules") == "",
                  "and nothing tracked is modified — the fork stays private")
            check(after["snesrecomp"].effective_url == FORK,
                  "the effective URL is what git will actually reach for")

            back = gitops.reset_module_url(port, "snesrecomp")
            check(back.ok, f"reset applies ({back.message})")
            check(gitops.module_urls(port)[0].origin_url != FORK,
                  "and origin goes back to what .gitmodules says")

            shared = gitops.set_module_url(port, "snesrecomp", FORK, scope="gitmodules")
            check(shared.ok and "commit to share it" in shared.message,
                  "the tracked scope says it has to be committed")
            check(_git(port, "diff", "--name-only", "--", ".gitmodules") == ".gitmodules",
                  "because it modifies a tracked file")
            check(_git(port, "show", "HEAD:.gitmodules") == tracked_before,
                  "and still commits nothing itself")

            # Nested modules live in the framework's .gitmodules, not the port's.
            # Init it first: with no checkout only the config override can move,
            # which is a different (also correct) path.
            subprocess.run(["git", "submodule", "update", "--init", "--", "lib/recomp-net"],
                           cwd=str(port / "snesrecomp"), capture_output=True, text=True)
            nested = gitops.set_module_url(
                port, "lib/recomp-net", "https://github.com/alex/recomp-net.git",
                nested=True)
            check(nested.ok, f"a nested module can be repointed too ({nested.message})")
            fw_rows = {x.path: x for x in gitops.module_urls(port)}
            check(fw_rows["lib/recomp-net"].origin_url
                  == "https://github.com/alex/recomp-net.git",
                  "and its origin moves")
            check(fw_rows["lib/recomp-net"].owner.endswith("snesrecomp"),
                  "with the framework named as the repo that owns the setting")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if prev is not None:
            os.environ["SNESRECOMP_ROOT"] = prev


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


def test_github_about_names_the_console() -> None:
    """The About line must name the console the port is actually built on.

    Both migrations call apply_github_about(), and it sent one hardcoded PSX
    string — so migrating a SNES port advertised it as "Made with PSXrecomp, a
    Sony PlayStation game static recompiler ecosystem" on a Super Nintendo
    repository. The README was right the whole time; only the About was wrong,
    which is the field nobody re-reads after it is set once.
    """
    print("github About")
    from project_studio.readme_metrics import apply_github_about

    def about(kind: str) -> str:
        platforms.set_current(kind)
        ok, msg = apply_github_about("TechnicallyComputers", "Example", dry_run=True)
        check(ok, f"{kind}: dry-run builds a command")
        return msg

    snes = about("snes")
    check("SNESrecomp" in snes, "a SNES session says SNESrecomp")
    check("Super Nintendo" in snes, "...and Super Nintendo")
    check("PSXrecomp" not in snes and "PlayStation" not in snes,
          "and never mentions the PlayStation")

    psx = about("psx")
    # Byte-identical to the string shipped before this was made per-console, so
    # re-migrating a PSX port does not rewrite its About.
    check(
        "Made with PSXrecomp, a Sony PlayStation game static recompiler "
        "ecosystem · Part of the R.A.I.D. community" in psx,
        "a PSX session is unchanged, word for word",
    )
    platforms.set_current("snes")


def test_moved_repo_urls() -> None:
    """A .gitmodules URL naming a repo that moved is a dead pointer, not a fork.

    recomp-net and rbengine changed owner. GitHub redirects the old slugs, so
    nothing looks broken — and the stale URL survives every Reset, because
    `git submodule sync` writes .gitmodules straight back over the override.
    Studio therefore knows the old names, says so on the row, and resets to the
    live URL. A fork under any other owner is absent from the map and is left
    alone, which is the whole point of the dialog.
    """
    print("moved repo URLs")
    from project_studio import gitops

    check(
        gitops.github_slug("git@github.com:TechnicallyComputers/recomp-net.git")
        == "technicallycomputers/recomp-net",
        "the ssh spelling of a remote parses to the same slug as https")
    check(
        gitops.moved_url("https://github.com/TechnicallyComputers/recomp-net.git")
        == gitops.DEFAULT_RECOMP_NET_URL,
        "the old recomp-net slug resolves to its new home")
    check(
        gitops.moved_url("https://github.com/mstan/n64lle")
        == "https://github.com/RetroPortingToolKit/n64lle.git",
        "a URL with no .git suffix still matches")
    check(
        gitops.moved_url("https://github.com/somefork/recomp-net.git") == "",
        "somebody else's fork is not mistaken for a repo that moved")
    check(
        gitops.moved_url(gitops.DEFAULT_RECOMP_NET_URL) == "",
        "and the current URL does not report itself as moved")

    old = "https://github.com/TechnicallyComputers/recomp-net.git"
    stale = gitops.ModuleUrl(path="lib/recomp-net", nested=True, gitmodules_url=old)
    check(stale.moved_to == gitops.DEFAULT_RECOMP_NET_URL,
          "a row still using the old URL is flagged as moved")
    fixed = gitops.ModuleUrl(
        path="lib/recomp-net", nested=True, gitmodules_url=old,
        local_url=gitops.DEFAULT_RECOMP_NET_URL,
        origin_url=gitops.DEFAULT_RECOMP_NET_URL)
    check(fixed.moved_to == "",
          "and stops being flagged once the override points at the live repo")
    check(fixed.to_dict().get("moved_to") == "",
          "the flag reaches the dialog over json")


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
        test_netplay_flip(root)
        test_version_stamp(root)
        test_plan_and_apply(root)
        test_digest_recovery(root)
        test_probe_rom(root)
    test_rom_discovery()
    test_identity_layouts()
    test_readme_toggle()
    test_generate_preflight()
    test_regen_framework_skew()
    test_advance_pins()
    test_module_urls()
    test_moved_repo_urls()
    test_region_default()
    test_probe_rom_cli()
    test_dispatch_inputs()
    test_new_project_command()
    test_launch_rom()
    test_module_targets()
    test_snes_functions()
    test_github_about_names_the_console()
    print("FAILED" if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
