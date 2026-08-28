"""CLI: audit / plan / apply / gui for Project Studio."""

from __future__ import annotations

import argparse
import json
import re
import os
import sys
from pathlib import Path

# Allow `python migrate_project.py` and `python -m project_studio`
_TOOLKIT = Path(__file__).resolve().parent.parent
if str(_TOOLKIT) not in sys.path:
    sys.path.insert(0, str(_TOOLKIT))

from project_studio import __version__, platforms  # noqa: E402
from project_studio.models import MigrateOptions  # noqa: E402


def _print_audit(report, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0 if report.to_dict()["fail_count"] == 0 else 1

    print(f"Project Studio audit  v{__version__}")
    print(f"  root:    {report.root}")
    print(f"  name:    {report.project_name}")
    print(f"  layout:  {report.layout.value}")
    print(f"  boot:    {report.boot_exe or '(unknown)'}")
    print()
    width = max(len(c.title) for c in report.checks) if report.checks else 10
    for c in report.checks:
        mark = {
            "pass": "OK  ",
            "fail": "FAIL",
            "warn": "WARN",
            "skip": "SKIP",
        }.get(c.status.value, "????")
        print(f"  [{mark}] {c.title:<{width}}  {c.detail}")
        if c.fix_op and c.status.value in ("fail", "warn"):
            print(f"         → op: {c.fix_op}")
    if report.notes:
        print()
        for n in report.notes:
            print(f"  note: {n}")
    fails = sum(1 for c in report.checks if c.status.value == "fail")
    warns = sum(1 for c in report.checks if c.status.value == "warn")
    print()
    print(f"Summary: {fails} fail, {warns} warn  (setup-host releases only)")
    return 1 if fails else 0


def _options_from_args(args: argparse.Namespace) -> MigrateOptions:
    only = [x.strip() for x in (args.only or "").split(",") if x.strip()]
    skip = [x.strip() for x in (args.skip or "").split(",") if x.strip()]
    return MigrateOptions(
        disc=args.disc,
        project_name=args.name,
        boot_exe=args.boot_exe,
        players=args.players,
        zip_prefix=args.zip_prefix,
        github_owner=getattr(args, "github_owner", None) or None,
        github_repo=getattr(args, "github_repo", None) or None,
        window_title=args.window_title,
        enable_recomp_ui=not args.no_recomp_ui,
        enable_wizard=not args.no_wizard,
        enable_netplay=args.enable_netplay,
        lobby_url=args.lobby_url,
        enable_ci=not args.no_ci,
        relocate_boxart=not args.no_boxart,
        rewrite_cmake=not args.no_rewrite_cmake,
        merge_gitignore=not args.no_gitignore,
        probe_disc=bool(args.disc) and not args.no_probe,
        record_pins=not args.no_pins,
        force=args.force,
        only=only,
        skip=skip,
        dry_run=args.dry_run,
    )


def _migration_backend():
    """The audit/plan/apply implementation for this session's platform.

    Two independent implementations rather than one parametrised one: the PSX
    migration is about game.toml, disc probing, BIOS backends and a CMake
    rewrite, none of which exists on SNES. Forcing them through one code path
    would mean a pile of `if platform ==` inside every op.
    """
    if platforms.current().key == "snes":
        from project_studio import snesops

        return snesops
    import types

    from project_studio import detect, ops as psx_ops, plan as psx_plan

    return types.SimpleNamespace(
        audit_project=detect.audit_project,
        build_plan=psx_plan.build_plan,
        apply_plan=psx_ops.apply_plan,
        list_ops=psx_ops.list_ops,
    )


def cmd_audit(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    return _print_audit(_migration_backend().audit_project(root), as_json=args.json)


def cmd_plan(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    opts = _options_from_args(args)
    plan = _migration_backend().build_plan(root, opts)
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
        return 0
    print(f"Project Studio plan  ({plan.layout.value})")
    print(f"  root: {plan.root}")
    print(f"  dry-run default for apply: use --dry-run")
    print()
    if not plan.steps:
        print("  (no steps — audit looks clean for selected options)")
        return 0
    for i, s in enumerate(plan.steps, 1):
        print(f"  {i:2d}. [{s.op_id}] {s.title}")
        if s.detail:
            print(f"      {s.detail}")
    print()
    print("Apply with: migrate_project.py apply --root … [--dry-run]")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    opts = _options_from_args(args)
    # Setup-host exclusive hard rules
    opts.enable_wizard = True
    opts.enable_recomp_ui = True
    if opts.players < 2:
        opts.enable_netplay = False

    backend = _migration_backend()
    report = backend.audit_project(root)
    plan = backend.build_plan(root, opts, report)
    if args.json_plan:
        print(json.dumps(plan.to_dict(), indent=2))

    if not plan.steps:
        print("Nothing to apply.")
        return 0

    print(f"Applying {len(plan.steps)} step(s) to {root}"
          + (" [DRY-RUN]" if opts.dry_run else ""))
    results = backend.apply_plan(plan)
    failed = 0
    for r in results:
        mark = "OK" if r.ok else "FAIL"
        print(f"  [{mark}] {r.op_id}: {r.message}")
        for p in r.changed_paths:
            print(f"         · {p}")
        if not r.ok:
            failed += 1
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    return 1 if failed else 0


def cmd_ops(_: argparse.Namespace) -> int:
    for op in _migration_backend().list_ops():
        print(op)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """Launch the native Dear ImGui Studio binary when available."""
    import os
    import shutil
    import subprocess

    root = Path(args.root).expanduser().resolve() if args.root else None
    candidates: list[Path] = []
    env_bin = (os.environ.get("RETCOMM_STUDIO_BIN") or "").strip()
    if env_bin:
        candidates.append(Path(env_bin))
    here = Path(__file__).resolve()
    # tools/new_project_layout/project_studio/cli.py → repo root
    repo = here.parents[3] if len(here.parents) > 3 else here.parents[2]
    for rel in (
        Path("build") / "RetComM-Studio",
        Path("build") / "retcomm-studio",
        Path("build-release") / "RetComM-Studio",
        Path("out") / "bin" / "RetComM-Studio",
    ):
        candidates.append(repo / rel)
    which = shutil.which("RetComM-Studio") or shutil.which("retcomm-studio")
    if which:
        candidates.append(Path(which))

    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            cmd = [str(cand)]
            if root is not None:
                os.environ["RETCOMM_STUDIO_INITIAL_ROOT"] = str(root)
            return int(subprocess.call(cmd))

    print(
        "RetComM Studio GUI is Dear ImGui (native).\n"
        "Build it first:\n"
        "  cmake -S . -B build && cmake --build build\n"
        "  ./build/RetComM-Studio\n"
        "Or set RETCOMM_STUDIO_BIN to the executable path.",
        file=sys.stderr,
    )
    return 2


def _index_to_json_dict(index) -> dict:
    from project_studio.repo_index import labels_for_repos

    from project_studio.bulkops import filter_indexed_catalog
    from project_studio.naming import configured_players

    data = index.to_dict()
    labels = labels_for_repos(index.repos)
    catalog_hits, _note = filter_indexed_catalog(index)
    catalog_paths = {e.path for e in catalog_hits}
    repos_out = []
    for entry, label in zip(index.repos, labels):
        try:
            players = configured_players(entry.resolved())
        except OSError:
            players = 2
        repos_out.append(
            {
                "path": entry.path,
                "name": entry.name,
                "cue": entry.cue,
                "label": label,
                "in_catalog": entry.path in catalog_paths,
                "players": players,
            }
        )
    data["repos"] = repos_out
    return data


def _print_index_json(index) -> int:
    print(json.dumps(_index_to_json_dict(index), indent=2))
    return 0


def cmd_repos_list(_: argparse.Namespace) -> int:
    from project_studio.repo_index import load_index

    return _print_index_json(load_index())


def cmd_repos_add(args: argparse.Namespace) -> int:
    from project_studio.repo_index import add_repo, load_index, looks_like_game_repo

    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    if not looks_like_game_repo(root) and not args.force:
        print(
            f"error: does not look like a game repo (pass --force): {root}",
            file=sys.stderr,
        )
        return 2
    idx = load_index()
    add_repo(idx, root, name=args.name or "", cue=args.cue or "")
    return _print_index_json(idx)


def cmd_repos_remove(args: argparse.Namespace) -> int:
    from project_studio.repo_index import load_index, remove_repo

    idx = load_index()
    if not remove_repo(idx, args.path):
        print(f"error: not in index: {args.path}", file=sys.stderr)
        return 2
    return _print_index_json(idx)


def cmd_repos_set_cue(args: argparse.Namespace) -> int:
    from project_studio.repo_index import load_index, set_repo_cue

    cue = (getattr(args, "cue", None) or "").strip()
    if not cue:
        label = "ROM (--rom)" if platforms.is_snes() else "disc .cue (--cue)"
        print(f"error: no {label} given", file=sys.stderr)
        return 2
    args.cue = cue
    idx = load_index()
    entry = set_repo_cue(idx, args.path, args.cue)
    if entry is None:
        print(f"error: not in index: {args.path}", file=sys.stderr)
        return 2
    return _print_index_json(idx)


def cmd_repos_clear_cue(args: argparse.Namespace) -> int:
    from project_studio.repo_index import clear_repo_cue, load_index

    idx = load_index()
    if idx.find(args.path) is None:
        print(f"error: not in index: {args.path}", file=sys.stderr)
        return 2
    clear_repo_cue(idx, args.path)
    return _print_index_json(idx)


def cmd_repos_set_last(args: argparse.Namespace) -> int:
    from project_studio.repo_index import load_index, set_last

    idx = load_index()
    set_last(idx, args.path)
    return _print_index_json(idx)


def cmd_repos_set_flags(args: argparse.Namespace) -> int:
    from project_studio.repo_index import load_index, save_index

    idx = load_index()
    if args.catalog_only is not None:
        idx.catalog_only = str(args.catalog_only).strip() in ("1", "true", "yes", "on")
    if args.bulk_jobs is not None:
        try:
            jobs = int(args.bulk_jobs)
        except (TypeError, ValueError):
            jobs = idx.bulk_jobs
        idx.bulk_jobs = max(1, min(4, jobs))
    if args.log_height is not None:
        try:
            h = int(args.log_height)
        except (TypeError, ValueError):
            h = idx.log_height
        idx.log_height = max(100, min(800, h))
    save_index(idx)
    return _print_index_json(idx)


def cmd_repos_filter_catalog(_: argparse.Namespace) -> int:
    """JSON paths for indexed repos that map to retcomm-catalog titles."""
    from project_studio.bulkops import filter_indexed_catalog
    from project_studio.repo_index import load_index

    hits, note = filter_indexed_catalog(load_index())
    print(
        json.dumps(
            {
                "note": note,
                "paths": [e.path for e in hits],
                "labels": [e.label() for e in hits],
            },
            indent=2,
        )
    )
    return 0


def cmd_repos_filter_catalog_contributors(_: argparse.Namespace) -> int:
    """JSON paths for catalog repos where the viewer has WRITE+ (gh)."""
    from project_studio.bulkops import filter_indexed_catalog_contributors
    from project_studio.repo_index import load_index

    hits, note, logs = filter_indexed_catalog_contributors(load_index())
    for line in logs:
        print(line, file=sys.stderr)
    print(
        json.dumps(
            {
                "note": note,
                "paths": [e.path for e in hits],
                "labels": [e.label() for e in hits],
                "logs": logs,
            },
            indent=2,
        )
    )
    return 0


def cmd_updates_check(args: argparse.Namespace) -> int:
    from project_studio.updater import check_updates, check_updates_on_startup_enabled

    if getattr(args, "startup", False) and not check_updates_on_startup_enabled():
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "skipped": True,
                        "message": "Startup update check disabled in config.",
                        "studio": {"available": False},
                        "toolchain": {"available": False},
                        "catalog": {"available": False, "skipped": True,
                                    "message": "Startup update check disabled."},
                    },
                    indent=2,
                )
            )
        else:
            print("Startup update check disabled in config.")
        return 0

    def prog(msg: str) -> None:
        print(msg, file=sys.stderr)

    result = check_updates(on_progress=prog)
    if args.json:
        def comp(c) -> dict:
            out = {
                "kind": c.kind,
                "current": c.current,
                "latest": c.latest,
                "available": c.available,
                "supported": c.supported,
                "message": c.message,
                "asset_name": c.asset_name,
            }
            if c.kind == "catalog":
                out["downloaded"] = bool(getattr(c, "downloaded", False))
                out["skipped"] = bool(getattr(c, "skipped", False))
            return out

        catalog_obj = result.catalog
        catalog_json = (
            comp(catalog_obj)
            if catalog_obj is not None
            else {
                "kind": "catalog",
                "current": "(none)",
                "latest": "",
                "available": False,
                "supported": False,
                "message": "Catalog not checked.",
                "downloaded": False,
                "skipped": True,
            }
        )
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "message": result.message,
                    "studio": comp(result.studio),
                    "toolchain": comp(result.toolchain),
                    "catalog": catalog_json,
                },
                indent=2,
            )
        )
    else:
        print(result.studio.message)
        print(result.toolchain.message)
        if result.catalog:
            print(result.catalog.message)
        if result.message:
            print(result.message)
    return 0 if result.ok else 1


def cmd_updates_apply(args: argparse.Namespace) -> int:
    from project_studio.updater import apply_updates, check_updates

    def prog(msg: str) -> None:
        print(msg, file=sys.stderr)

    result = check_updates(on_progress=prog)
    summary, should_exit = apply_updates(
        result,
        update_studio=not args.toolchain_only,
        update_toolchain=not args.studio_only,
        on_progress=prog,
    )
    print(summary)
    if should_exit:
        return 0
    return 0


def cmd_updates_ensure_toolchain(args: argparse.Namespace) -> int:
    from project_studio.updater import ensure_toolchain, resolve_toolchain_python

    def prog(msg: str) -> None:
        print(msg, file=sys.stderr)

    ok, msg = ensure_toolchain(on_progress=prog)
    print(f"[{'OK' if ok else 'FAIL'}] {msg}")
    py = resolve_toolchain_python()
    if py:
        print(f"python: {py}")
    return 0 if ok else 1


def cmd_new_project(args: argparse.Namespace) -> int:
    from project_studio.newproject import (
        NewProjectOptions,
        index_new_project,
        project_root_for,
        run_new_project,
        validate_options,
    )

    # --disc / --rom both append, so args.disc is a list in disc order.
    _np_discs = [d.strip() for d in (args.disc or []) if (d or "").strip()]

    opts = NewProjectOptions(
        name=(args.name or "").strip(),
        disc=_np_discs[0] if _np_discs else "",
        extra_discs=_np_discs[1:],
        parent_dir=(args.dir or ".").strip(),
        bios=(getattr(args, "bios", None) or "").strip(),
        boot_exe=(getattr(args, "boot_exe", None) or "").strip(),
        players=int(getattr(args, "players", 2) or 2),
        zip_prefix=(getattr(args, "zip_prefix", None) or "").strip(),
        github_owner=(getattr(args, "github_owner", None) or "").strip(),
        github_repo=(getattr(args, "github_repo", None) or "").strip(),
        description=(getattr(args, "description", None) or "").strip(),
        publisher=(getattr(args, "publisher", None) or "").strip(),
        year=(getattr(args, "year", None) or "").strip(),
        region=(
            (getattr(args, "region", None) or "").strip()
            if platforms.current().key == "snes"
            else (getattr(args, "region", None) or "USA").strip()
        ),
        enable_recomp_ui=not bool(getattr(args, "no_recomp_ui", False)),
        enable_wizard=not bool(getattr(args, "no_wizard", False)),
        enable_netplay=bool(getattr(args, "enable_netplay", False)),
        lobby_url=(getattr(args, "lobby_url", None) or "netplay.retcomm.net").strip(),
        enable_ci=not bool(getattr(args, "no_ci", False)),
        fetch_boxart=not bool(getattr(args, "no_fetch_boxart", False)),
        stage_disc=bool(getattr(args, "stage_disc", False)),
        do_generate=bool(getattr(args, "generate", False)),
        do_build=bool(getattr(args, "enable_build", False)),
        create_github=bool(getattr(args, "create_github", False)),
        github_visibility=(
            getattr(args, "github_visibility", None) or "private"
        ).strip(),
        platform=platforms.current().key,
        snesrecomp_ref=(getattr(args, "snesrecomp_ref", None) or "main").strip(),
        multitap=(getattr(args, "multitap", None) or "").strip(),
        enable_rollback=bool(getattr(args, "enable_rollback", False)),
        psxrecomp_ref=(getattr(args, "psxrecomp_ref", None) or "master").strip(),
        recomp_ui_ref=(getattr(args, "recomp_ui_ref", None) or "master").strip(),
        recomp_net_ref=(getattr(args, "recomp_net_ref", None) or "").strip(),
        rbengine_ref=(getattr(args, "rbengine_ref", None) or "").strip(),
        dry_run=bool(getattr(args, "dry_run", False)),
    )

    if bool(getattr(args, "autofill_meta", False)) and platforms.current().key != "snes":
        # Redump/libretro lookup is keyed on disc identity; a cartridge has no
        # entry there, so on SNES this is skipped rather than failed.
        from project_studio.discmeta import apply_hit_to_options, lookup_cue

        print("Looking up disc metadata (Redump / libretro / catalog)…", flush=True)
        hit = lookup_cue(opts.disc)
        for note in hit.notes:
            print(f"  meta: {note}", flush=True)
        filled = apply_hit_to_options(opts, hit, only_empty=True)
        if filled:
            print("  filled: " + ", ".join(filled), flush=True)
        else:
            print(f"  no empty fields filled (source={hit.source})", flush=True)

    errs = validate_options(opts)
    if errs:
        for e in errs:
            print(f"error: {e}", file=sys.stderr)
        return 2

    def on_line(msg: str) -> None:
        print(msg, flush=True)

    r = run_new_project(opts, on_line=on_line)
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    if not r.ok:
        return 1
    if opts.dry_run:
        return 0
    root = project_root_for(opts)
    ir = index_new_project(root, name=opts.name, cue=opts.disc)
    print(f"[{'OK' if ir.ok else 'FAIL'}] {ir.message}")
    return 0 if ir.ok else 1


def cmd_probe_rom(args: argparse.Namespace) -> int:
    """Cartridge identity as JSON, for the GUI's New Project defaults.

    On a terminal snesrecomp's wizard prompts for name / region / description
    with the probed identity as each default. Studio always runs it with
    --yes, which takes every default silently — so the GUI has to show those
    same values *before* the run, and this is where it gets them.
    """
    import json as _json
    import subprocess as _sp
    import tempfile as _tf

    from project_studio import snes_paths

    rom = Path((getattr(args, "rom", None) or "").strip()).expanduser()
    if not rom.is_file():
        print(f"error: ROM not found: {rom}", file=sys.stderr)
        return 2
    probe = snes_paths.probe_rom_script(getattr(args, "root", None) or None)
    if not probe.is_file():
        print(f"error: probe_rom.py not found ({probe})", file=sys.stderr)
        return 2

    with _tf.TemporaryDirectory() as td:
        out = Path(td) / "probe.json"
        # --json-out, not stdout: the probe prints a human summary to stdout
        # and only writes machine-readable JSON to a file.
        r = _sp.run(
            [sys.executable, str(probe), str(rom.resolve()), "--json-out", str(out), "--quiet"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if r.returncode != 0 or not out.is_file():
            print(r.stderr or "probe_rom failed", file=sys.stderr)
            return 1
        data = _json.loads(out.read_text(encoding="utf-8"))

    # Zip prefix follows the scaffolder's own slug rule, so a project made from
    # these defaults packages under the name CI will later expect.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(data.get("display_name") or "")).strip("-").lower()
    data["zip_prefix"] = slug or "game"
    data["wizard"] = snes_paths.wizard_source(getattr(args, "root", None) or None)
    print(json.dumps(data, indent=2))
    return 0


def cmd_lookup_disc_meta(args: argparse.Namespace) -> int:
    from project_studio.discmeta import lookup_cue, lookup_digests

    force = bool(getattr(args, "force_refresh", False))
    if getattr(args, "disc", None):
        hit = lookup_cue(args.disc, force_refresh=force)
    else:
        hit = lookup_digests(
            crc32=getattr(args, "crc32", "") or "",
            md5=getattr(args, "md5", "") or "",
            sha1=getattr(args, "sha1", "") or "",
            serial=getattr(args, "serial", "") or "",
            force_refresh=force,
        )
    if args.json:
        print(json.dumps(hit.to_dict(), indent=2))
    else:
        print(f"source: {hit.source}")
        print(f"name: {hit.name}")
        print(f"serial: {hit.serial}")
        print(f"region: {hit.region}")
        print(f"players: {hit.players}")
        print(f"publisher: {hit.publisher}")
        print(f"year: {hit.year}")
        print(f"description: {(hit.description or '')[:200]}")
        print(f"crc32/md5/sha1: {hit.crc32} / {hit.md5} / {hit.sha1}")
        for n in hit.notes:
            print(f"note: {n}")
    return 0 if hit.source != "none" else 1


def _root_or_die(args: argparse.Namespace) -> Path | None:
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return None
    return root


def cmd_git_branches(args: argparse.Namespace) -> int:
    """List game + module branch names as JSON for Studio dropdowns."""
    from project_studio.gitops import (
        DEFAULT_RBENGINE_URL,
        DEFAULT_RECOMP_NET_URL,
        DEFAULT_RECOMP_UI_URL,
        current_branch,
        framework_name,
        framework_url,
        list_branches,
        list_module_branches,
        list_remote_head_branches,
        resolve_module_dir,
    )

    fw = framework_name()
    fw_url = framework_url()

    def prefer_current(names: list[str], cur: str) -> list[str]:
        cur = (cur or "").strip()
        if not cur:
            return names
        rest = [n for n in names if n != cur]
        return [cur, *rest]

    fetch = bool(getattr(args, "fetch", False))
    root_s = (getattr(args, "root", None) or "").strip()
    # Keyed by the live framework name, with a stable "framework" alias so the
    # GUI reads one shape on both consoles instead of guessing the key.
    out: dict = {
        "game": [],
        fw: [],
        "recomp-ui": [],
        "recomp-net": [],
        "rbengine": [],
        "current": {
            "game": "",
            fw: "",
            "recomp-ui": "",
            "recomp-net": "",
            "rbengine": "",
        },
    }
    if root_s:
        root = Path(root_s).expanduser().resolve()
        if not root.is_dir():
            print(f"error: not a directory: {root}", file=sys.stderr)
            return 2
        out["game"] = list_branches(root, remotes=True, fetch=fetch)
        out[fw] = list_module_branches(
            root, fw, remotes=True, fetch=fetch, url_fallback=fw_url
        )
        out["recomp-ui"] = list_module_branches(
            root, "recomp-ui", remotes=True, fetch=fetch, url_fallback=DEFAULT_RECOMP_UI_URL
        )
        out["recomp-net"] = list_module_branches(
            root,
            "lib/recomp-net",
            nested=True,
            remotes=True,
            fetch=fetch,
            url_fallback=DEFAULT_RECOMP_NET_URL,
        )
        out["rbengine"] = list_module_branches(
            root,
            "lib/retcomm-rbengine",
            nested=True,
            remotes=True,
            fetch=fetch,
            url_fallback=DEFAULT_RBENGINE_URL,
        )
        cur = out["current"]
        cur["game"] = current_branch(root) or ""
        for key, path, nested in (
            (fw, fw, False),
            ("recomp-ui", "recomp-ui", False),
            ("recomp-net", "lib/recomp-net", True),
            ("rbengine", "lib/retcomm-rbengine", True),
        ):
            sub = resolve_module_dir(root, path, nested=nested)
            cur[key] = (current_branch(sub) or "") if sub is not None else ""
        # Put live checkouts first so dropdowns open on the active branch.
        out["game"] = prefer_current(out["game"], cur["game"])
        out[fw] = prefer_current(out[fw], cur[fw])
        out["recomp-ui"] = prefer_current(out["recomp-ui"], cur["recomp-ui"])
        out["recomp-net"] = prefer_current(out["recomp-net"], cur["recomp-net"])
        out["rbengine"] = prefer_current(out["rbengine"], cur["rbengine"])
    else:
        # New-project / no checkout: ls-remote default module URLs.
        out[fw] = list_remote_head_branches(fw_url)
        out["recomp-ui"] = list_remote_head_branches(DEFAULT_RECOMP_UI_URL)
        out["recomp-net"] = list_remote_head_branches(DEFAULT_RECOMP_NET_URL)
        out["rbengine"] = list_remote_head_branches(DEFAULT_RBENGINE_URL)
        # Sensible game-branch placeholders when scaffolding.
        out["game"] = ["main", "master"]

    # Always offer (default) for nested/ref menus that support it.
    for key in ("recomp-net", "rbengine"):
        if "(default)" not in out[key]:
            out[key] = ["(default)", *out[key]]

    out["framework"] = out[fw]
    out["framework_name"] = fw
    out["current"]["framework"] = out["current"][fw]

    print(json.dumps(out, indent=2))
    return 0


def cmd_git_status(args: argparse.Namespace) -> int:
    from project_studio.gitops import repo_status

    root = _root_or_die(args)
    if root is None:
        return 2
    st = repo_status(root)
    if args.json:
        print(json.dumps(st.to_dict(), indent=2))
        return 0 if st.is_git else 1
    print(f"Project Studio git  v{__version__}")
    print(f"  root:     {st.root}")
    print(f"  git:      {st.is_git}")
    if not st.is_git:
        return 1
    print(f"  branch:   {st.branch}" + (f" → {st.upstream}" if st.upstream else ""))
    print(f"  ahead/behind: {st.ahead}/{st.behind}")
    print(f"  dirty:    {st.dirty}  (staged={st.staged} unstaged={st.unstaged} untracked={st.untracked})")
    print(f"  origin:   {st.remote_url or '(none)'}")
    print(f"  gh:       {st.gh_repo or ('available' if st.gh_available else 'missing')}")
    fw = platforms.current().framework
    if st.framework_root:
        print(f"  {fw}: {st.framework_root}")
    print()
    print("Submodules:")
    for s in st.submodules:
        mark = "OK" if s.present else "MISSING"
        print(
            f"  [{mark}] {s.path:<12} branch={s.branch or '-':<16} "
            f"sha={s.sha or '-':<12} {s.url}"
        )
    if st.nested_submodules and st.framework_root != st.root:
        print()
        print(f"Nested (inside {fw}):")
        for s in st.nested_submodules:
            mark = "OK" if s.present else "MISSING"
            print(
                f"  [{mark}] {s.path:<22} branch={s.branch or '-':<16} "
                f"sha={s.sha or '-':<12} {s.url}"
            )
    if st.short_status:
        print()
        print("Status:")
        print(st.short_status)
    for n in st.notes:
        print(f"note: {n}")
    return 0


def cmd_git_ensure_submodules(args: argparse.Namespace) -> int:
    from project_studio.gitops import ensure_known_submodules

    root = _root_or_die(args)
    if root is None:
        return 2
    results = ensure_known_submodules(
        root,
        framework_branch_name=args.psxrecomp_branch,
        recomp_ui_branch=args.recomp_ui_branch,
        dry_run=args.dry_run,
    )
    failed = 0
    for r in results:
        print(f"  [{'OK' if r.ok else 'FAIL'}] {r.message}")
        if r.detail:
            print(f"         {r.detail}")
        if not r.ok:
            failed += 1
    return 1 if failed else 0


def cmd_git_ensure_nested(args: argparse.Namespace) -> int:
    from project_studio.gitops import ensure_nested_modules

    root = _root_or_die(args)
    if root is None:
        return 2
    results = ensure_nested_modules(
        root,
        recomp_net_branch=args.recomp_net_branch,
        rbengine_branch=args.rbengine_branch,
        dry_run=args.dry_run,
    )
    failed = 0
    for r in results:
        print(f"  [{'OK' if r.ok else 'FAIL'}] {r.message}")
        if r.detail:
            print(f"         {r.detail}")
        if not r.ok:
            failed += 1
    return 1 if failed else 0


def cmd_git_set_branch(args: argparse.Namespace) -> int:
    from project_studio.gitops import (
        set_nested_branch,
        set_submodule_branch,
        switch_branch,
    )

    root = _root_or_die(args)
    if root is None:
        return 2
    if args.nested:
        if not args.submodule:
            print("error: --nested requires --submodule PATH", file=sys.stderr)
            return 2
        r = set_nested_branch(root, args.submodule, args.branch, dry_run=args.dry_run)
    elif args.submodule:
        r = set_submodule_branch(
            root, args.submodule, args.branch, dry_run=args.dry_run
        )
    else:
        r = switch_branch(
            root, args.branch, create=args.create, dry_run=args.dry_run
        )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    if r.detail:
        print(r.detail)
    return 0 if r.ok else 1


def _git_target_flags(args: argparse.Namespace) -> dict[str, bool]:
    """Resolve Game / Modules / Nested / psxrecomp targets for a single-root op.

    When no target flags are set, default to the game root (legacy ``git pull`` /
    ``git switch --branch``).
    """
    game = bool(getattr(args, "game", False))
    modules = bool(getattr(args, "modules", False))
    nested = bool(getattr(args, "nested", False))
    psx = bool(getattr(args, "psxrecomp", False))
    if not (game or modules or nested or psx):
        game = True
    return {
        "game": game,
        "modules": modules,
        "nested": nested,
        "psxrecomp": psx,
    }


def cmd_git_switch(args: argparse.Namespace) -> int:
    from project_studio.gitops import (
        CmdResult,
        default_module_paths,
        framework_name,
        switch_branch,
        switch_framework,
        switch_modules,
    )

    root = _root_or_die(args)
    if root is None:
        return 2
    branch = (getattr(args, "branch", None) or "").strip()
    create = bool(getattr(args, "create", False))
    set_tracking = not bool(getattr(args, "no_track", False))
    submodule = (getattr(args, "submodule", None) or "").strip()
    psx_branch = (getattr(args, "psxrecomp_branch", None) or "").strip()
    ui_branch = (getattr(args, "ui_branch", None) or "").strip()
    net_branch = (getattr(args, "net_branch", None) or "").strip()
    rb_branch = (getattr(args, "rb_branch", None) or "").strip()
    per_module = bool(psx_branch or ui_branch or net_branch or rb_branch)
    multi = bool(getattr(args, "game", False)) or (
        sum(
            bool(x)
            for x in (
                getattr(args, "modules", False),
                getattr(args, "nested", False),
                getattr(args, "psxrecomp", False),
            )
        )
        > 1
    )

    # Single-path legacy: --submodule PATH [--nested] [--branch]
    if submodule and not multi and not per_module and not getattr(args, "game", False):
        nested = bool(getattr(args, "nested", False))
        paths = [submodule]
        branch_by_path = None
        if branch:
            branch_by_path = {submodule.strip().replace("\\", "/"): branch}
        results = switch_modules(
            root,
            paths=paths,
            nested=nested,
            branch_by_path=branch_by_path,
            create=create,
            set_tracking=set_tracking,
            dry_run=args.dry_run,
        )
        return _print_module_results(results)

    t = _git_target_flags(args)
    results: list[CmdResult] = []

    if t["game"]:
        if not branch:
            print("error: --game / game root requires --branch NAME", file=sys.stderr)
            return 2
        r = switch_branch(root, branch, create=create, dry_run=args.dry_run)
        results.append(CmdResult(r.ok, r.message, r.detail))

    if t["modules"]:
        branch_by_path: dict[str, str] | None = None
        paths = _module_paths_from_args(args)
        if psx_branch or ui_branch:
            branch_by_path = {}
            if psx_branch:
                branch_by_path[framework_name()] = psx_branch
            if ui_branch:
                branch_by_path["recomp-ui"] = ui_branch
            paths = list(branch_by_path.keys())
        elif branch and not t["game"]:
            # Legacy: ``git switch --modules --branch X`` applies X to all modules.
            want = paths or list(default_module_paths(nested=False))
            branch_by_path = {p.strip().replace("\\", "/"): branch for p in want}
        results.extend(
            switch_modules(
                root,
                paths=paths,
                nested=False,
                branch_by_path=branch_by_path,
                create=create,
                set_tracking=set_tracking,
                dry_run=args.dry_run,
            )
        )
    elif t["psxrecomp"]:
        use = psx_branch or branch
        if not use:
            print(
                "error: --framework requires --branch or --framework-branch",
                file=sys.stderr,
            )
            return 2
        r = switch_framework(root, use, create=create, dry_run=args.dry_run)
        results.append(CmdResult(r.ok, r.message, r.detail))

    if t["nested"]:
        branch_by_path = None
        paths = _module_paths_from_args(args)
        if net_branch or rb_branch:
            branch_by_path = {}
            if net_branch:
                branch_by_path["lib/recomp-net"] = net_branch
            if rb_branch:
                branch_by_path["lib/retcomm-rbengine"] = rb_branch
            paths = list(branch_by_path.keys())
        elif branch and not t["modules"] and not t["game"]:
            # Legacy: ``git switch --nested --branch X`` applies X to all nested.
            want = paths or list(default_module_paths(nested=True))
            branch_by_path = {p.strip().replace("\\", "/"): branch for p in want}
        results.extend(
            switch_modules(
                root,
                paths=paths,
                nested=True,
                branch_by_path=branch_by_path,
                create=create,
                set_tracking=set_tracking,
                dry_run=args.dry_run,
            )
        )

    if not results:
        print(
            "error: nothing to switch (pass --game / --modules / --nested)",
            file=sys.stderr,
        )
        return 2
    return _print_module_results(results)


def cmd_git_update_submodules(args: argparse.Namespace) -> int:
    from project_studio.gitops import update_submodules

    root = _root_or_die(args)
    if root is None:
        return 2
    paths = [p.strip() for p in (args.paths or "").split(",") if p.strip()] or None
    r = update_submodules(
        root, paths=paths, remote=args.remote, dry_run=args.dry_run
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    if r.detail:
        print(r.detail)
    return 0 if r.ok else 1


def cmd_git_update_nested(args: argparse.Namespace) -> int:
    from project_studio.gitops import update_nested_modules

    root = _root_or_die(args)
    if root is None:
        return 2
    paths = [p.strip() for p in (args.paths or "").split(",") if p.strip()] or None
    r = update_nested_modules(
        root,
        paths=paths,
        remote=args.remote,
        stage=not args.no_stage,
        dry_run=args.dry_run,
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    if r.detail:
        print(r.detail)
    return 0 if r.ok else 1


def cmd_git_commit_nested(args: argparse.Namespace) -> int:
    from project_studio.gitops import commit_nested

    root = _root_or_die(args)
    if root is None:
        return 2
    r = commit_nested(root, args.message, dry_run=args.dry_run)
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    if r.detail:
        print(r.detail)
    return 0 if r.ok else 1


def _module_paths_from_args(args: argparse.Namespace) -> list[str] | None:
    raw = getattr(args, "paths", None) or ""
    paths = [p.strip() for p in raw.split(",") if p.strip()]
    return paths or None


def _print_module_results(results: list) -> int:
    failed = 0
    for r in results:
        print(f"  [{'OK' if r.ok else 'FAIL'}] {r.message}")
        if r.detail:
            print(f"         {r.detail}")
        if not r.ok:
            failed += 1
    return 1 if failed else 0


def _bulk_select_from_args(args: argparse.Namespace) -> list[str] | None:
    raw = getattr(args, "select", None) or ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or None


def _bulk_repos_or_die(args: argparse.Namespace):
    from project_studio.bulkops import indexed_repos

    repos = indexed_repos(select=_bulk_select_from_args(args))
    if not repos:
        print("No indexed repos matched (Add… in GUI, or --select filter).")
        return None
    return repos


def _bulk_targets_from_args(args: argparse.Namespace) -> dict:
    game = bool(getattr(args, "game", False))
    modules = bool(getattr(args, "modules", False))
    psx = bool(getattr(args, "psxrecomp", False))
    nested = bool(getattr(args, "nested", False))
    # Default: game root only when nothing specified.
    if not (game or modules or psx or nested):
        game = True
    return {
        "game": game,
        "modules": modules,
        "psxrecomp": psx,
        "nested": nested,
    }


def cmd_git_bulk_status(args: argparse.Namespace) -> int:
    from project_studio.bulkops import bulk_status

    repos = _bulk_repos_or_die(args)
    if repos is None:
        return 2
    return _print_module_results(bulk_status(repos))


def cmd_git_bulk_pull(args: argparse.Namespace) -> int:
    from project_studio.bulkops import bulk_pull

    repos = _bulk_repos_or_die(args)
    if repos is None:
        return 2
    return _print_module_results(
        bulk_pull(
            repos,
            **_bulk_targets_from_args(args),
            mode=getattr(args, "mode", None) or "ff-only",
            dirty=getattr(args, "dirty", None) or "fail",
            dry_run=args.dry_run,
        )
    )


def cmd_git_bulk_push(args: argparse.Namespace) -> int:
    from project_studio.bulkops import bulk_push

    repos = _bulk_repos_or_die(args)
    if repos is None:
        return 2
    return _print_module_results(
        bulk_push(
            repos,
            **_bulk_targets_from_args(args),
            dry_run=args.dry_run,
        )
    )


def cmd_git_bulk_commit(args: argparse.Namespace) -> int:
    from project_studio.bulkops import bulk_commit

    repos = _bulk_repos_or_die(args)
    if repos is None:
        return 2
    t = _bulk_targets_from_args(args)
    return _print_module_results(
        bulk_commit(
            repos,
            args.message,
            game=t["game"],
            modules=t["modules"],
            nested=t["nested"],
            dry_run=args.dry_run,
        )
    )


def cmd_git_bulk_switch(args: argparse.Namespace) -> int:
    from project_studio.bulkops import bulk_switch

    repos = _bulk_repos_or_die(args)
    if repos is None:
        return 2
    t = _bulk_targets_from_args(args)
    # For switch, defaulting to game-only when no flags is surprising if they
    # only passed --psxrecomp-branch — require at least one explicit target.
    if not any(
        (
            getattr(args, "game", False),
            getattr(args, "modules", False),
            getattr(args, "psxrecomp", False),
            getattr(args, "nested", False),
        )
    ):
        print(
            "error: pass --game / --modules / --psxrecomp / --nested "
            "(which checkouts to switch)",
            file=sys.stderr,
        )
        return 2
    return _print_module_results(
        bulk_switch(
            repos,
            game=t["game"],
            modules=t["modules"],
            psxrecomp=t["psxrecomp"],
            nested=t["nested"],
            game_branch=getattr(args, "branch", None) or "",
            psxrecomp_branch=getattr(args, "psxrecomp_branch", None) or "",
            recomp_ui_branch=getattr(args, "ui_branch", None) or "",
            recomp_net_branch=getattr(args, "net_branch", None) or "",
            rbengine_branch=getattr(args, "rb_branch", None) or "",
            create=bool(getattr(args, "create", False)),
            set_tracking=not bool(getattr(args, "no_track", False)),
            dry_run=args.dry_run,
        )
    )


def cmd_git_pull(args: argparse.Namespace) -> int:
    from project_studio.gitops import CmdResult, pull, pull_modules, pull_framework

    root = _root_or_die(args)
    if root is None:
        return 2
    mode = getattr(args, "mode", None) or "ff-only"
    dirty = getattr(args, "dirty", None) or "fail"
    t = _git_target_flags(args)
    results: list[CmdResult] = []
    if t["game"]:
        r = pull(root, mode=mode, dirty=dirty, dry_run=args.dry_run)
        results.append(CmdResult(r.ok, r.message, r.detail))
    if t["modules"]:
        results.extend(
            pull_modules(
                root,
                paths=_module_paths_from_args(args),
                nested=False,
                mode=mode,
                dirty=dirty,
                dry_run=args.dry_run,
            )
        )
    elif t["psxrecomp"]:
        r = pull_framework(root, mode=mode, dirty=dirty, dry_run=args.dry_run)
        results.append(CmdResult(r.ok, r.message, r.detail))
    if t["nested"]:
        results.extend(
            pull_modules(
                root,
                paths=_module_paths_from_args(args),
                nested=True,
                mode=mode,
                dirty=dirty,
                dry_run=args.dry_run,
            )
        )
    if not results:
        print("error: nothing to pull (pass --game / --modules / --nested)", file=sys.stderr)
        return 2
    return _print_module_results(results)


def cmd_git_commit(args: argparse.Namespace) -> int:
    from project_studio.gitops import CmdResult, commit_all, commit_modules

    root = _root_or_die(args)
    if root is None:
        return 2
    t = _git_target_flags(args)
    results: list[CmdResult] = []
    if t["game"]:
        r = commit_all(root, args.message, dry_run=args.dry_run)
        results.append(CmdResult(r.ok, r.message, r.detail))
    if t["modules"]:
        results.extend(
            commit_modules(
                root,
                args.message,
                paths=_module_paths_from_args(args),
                nested=False,
                dry_run=args.dry_run,
            )
        )
    if t["nested"]:
        results.extend(
            commit_modules(
                root,
                args.message,
                paths=_module_paths_from_args(args),
                nested=True,
                dry_run=args.dry_run,
            )
        )
    if not results:
        print(
            "error: nothing to commit (pass --game / --modules / --nested)",
            file=sys.stderr,
        )
        return 2
    return _print_module_results(results)


def cmd_git_push(args: argparse.Namespace) -> int:
    from project_studio.gitops import (
        CmdResult,
        default_module_paths,
        push,
        push_modules,
        push_framework,
    )

    root = _root_or_die(args)
    if root is None:
        return 2
    branch = (getattr(args, "branch", None) or "").strip()
    t = _git_target_flags(args)
    results: list[CmdResult] = []
    if t["game"]:
        r = push(root, branch=branch, dry_run=args.dry_run)
        results.append(CmdResult(r.ok, r.message, r.detail))
    if t["modules"]:
        paths = _module_paths_from_args(args)
        branch_by_path = None
        if branch and paths and len(paths) == 1:
            branch_by_path = {paths[0]: branch}
        elif branch and not paths:
            branch_by_path = {p: branch for p in default_module_paths(nested=False)}
        results.extend(
            push_modules(
                root,
                paths=paths,
                nested=False,
                branch_by_path=branch_by_path,
                dry_run=args.dry_run,
            )
        )
    elif t["psxrecomp"]:
        r = push_framework(root, branch=branch, dry_run=args.dry_run)
        results.append(CmdResult(r.ok, r.message, r.detail))
    if t["nested"]:
        paths = _module_paths_from_args(args)
        branch_by_path = None
        if branch and paths and len(paths) == 1:
            branch_by_path = {paths[0]: branch}
        elif branch and not paths and not t["modules"] and not t["game"]:
            branch_by_path = {p: branch for p in default_module_paths(nested=True)}
        results.extend(
            push_modules(
                root,
                paths=paths,
                nested=True,
                branch_by_path=branch_by_path,
                dry_run=args.dry_run,
            )
        )
    if not results:
        print(
            "error: nothing to push (pass --game / --modules / --nested)",
            file=sys.stderr,
        )
        return 2
    return _print_module_results(results)


def cmd_git_bulk_release(args: argparse.Namespace) -> int:
    from project_studio.bulkops import bulk_release

    repos = _bulk_repos_or_die(args)
    if repos is None:
        return 2
    results = bulk_release(
        repos,
        version=getattr(args, "version", "") or "",
        bump=getattr(args, "bump", "patch") or "patch",
        publish=not bool(getattr(args, "no_publish", False)),
        reuse_cached_emitters=not bool(
            getattr(args, "no_reuse_cached_emitters", False)
        ),
        dry_run=bool(getattr(args, "dry_run", False)),
        skip_missing_workflow=not bool(getattr(args, "strict", False)),
    )
    return _print_module_results(results)


def cmd_git_bulk_install_ci(args: argparse.Namespace) -> int:
    from project_studio.bulkops import bulk_install_ci

    repos = _bulk_repos_or_die(args)
    if repos is None:
        return 2
    results = bulk_install_ci(
        repos,
        force=bool(getattr(args, "force", False)),
        push_remote=not bool(getattr(args, "no_push", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    return _print_module_results(results)


def cmd_git_release(args: argparse.Namespace) -> int:
    from project_studio.gitops import run_release_workflow

    root = _root_or_die(args)
    if root is None:
        return 2
    r = run_release_workflow(
        root,
        version=args.version or "",
        bump=args.bump,
        publish=not args.no_publish,
        reuse_cached_emitters=not args.no_reuse_cached_emitters,
        dry_run=args.dry_run,
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    if r.detail:
        print(r.detail)
    return 0 if r.ok else 1


def cmd_git_install_ci(args: argparse.Namespace) -> int:
    from project_studio.gitops import install_and_push_release_ci

    root = _root_or_die(args)
    if root is None:
        return 2
    r = install_and_push_release_ci(
        root,
        zip_prefix=getattr(args, "zip_prefix", "") or "",
        force=bool(getattr(args, "force", False)),
        push_remote=not bool(getattr(args, "no_push", False)),
        dry_run=args.dry_run,
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    if r.detail:
        print(r.detail)
    return 0 if r.ok else 1


def cmd_build_mingw(args: argparse.Namespace) -> int:
    """Linux → Windows MinGW cross-build via scripts/build_windows_mingw.sh."""
    import json
    import shutil
    import subprocess

    if platforms.current().key == "snes":
        # The script configures PSX_NETPLAY, stages OpenBIOS and builds
        # psx-runtime. Refusing is honest; running it against a SNES tree would
        # fail deep inside cmake with a message about none of that.
        print(
            "error: build mingw is a psxrecomp cross-build; there is no snesrecomp "
            "counterpart yet. Use CI, or build natively on Windows.",
            file=sys.stderr,
        )
        return 2

    script = _TOOLKIT / "scripts" / "build_windows_mingw.sh"
    if not script.is_file():
        print(f"error: missing {script}", file=sys.stderr)
        return 2

    bash = shutil.which("bash")
    if not bash:
        print("error: bash not found (required for MinGW cross script)", file=sys.stderr)
        return 2

    cmd: list[str] = [bash, str(script)]
    if getattr(args, "root", None):
        cmd += ["--root", str(Path(args.root).expanduser().resolve())]
    if getattr(args, "name", None):
        cmd += ["--name", args.name]
    if getattr(args, "last", False) or (
        not getattr(args, "root", None) and not getattr(args, "name", None)
    ):
        cmd.append("--last")
    if args.build_dir and args.build_dir != "build-mingw":
        cmd += ["--build-dir", args.build_dir]
    if getattr(args, "package_only", False):
        cmd.append("--package-only")
    if getattr(args, "setup_host", False):
        cmd.append("--setup-host")
    if getattr(args, "package", False) and not getattr(args, "package_only", False):
        cmd.append("--package")
    if getattr(args, "dynamic", False):
        cmd.append("--dynamic")
    if getattr(args, "ensure", False) and not getattr(args, "package_only", False):
        cmd.append("--ensure")
    if getattr(args, "jobs", 0):
        cmd += ["--jobs", str(args.jobs)]
    if getattr(args, "dry_run", False):
        cmd.append("--dry-run")
    if getattr(args, "extra", ""):
        # "--extra=<value>", not two argv entries. cmake args start with '-',
        # and argparse rejects a leading-dash value for an option that expects
        # one argument ("expected one argument"). The '=' form is the only
        # spelling that survives it.
        cmd.append(f"--extra={args.extra}")

    print("+", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, check=False)
    # Studio Bundle+Export parses the JSON trailer / MINGW_ZIP line.
    root = ""
    if getattr(args, "root", None):
        root = str(Path(args.root).expanduser().resolve())
    zip_path = ""
    if root:
        dist = Path(root) / "dist"
        if dist.is_dir():
            zips = sorted(
                dist.glob("*-windows-x64-mingw.zip"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not zips:
                zips = sorted(dist.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
            if zips:
                zip_path = str(zips[0].resolve())
    if zip_path and r.returncode == 0:
        print(f"MINGW_ZIP={zip_path}", flush=True)
        print(json.dumps({"ok": True, "zip": zip_path}), flush=True)
    elif getattr(args, "package_only", False) or getattr(args, "package", False):
        print(json.dumps({"ok": False, "zip": "", "error": "no zip produced"}), flush=True)
    return int(r.returncode)


def cmd_build_configure(args: argparse.Namespace) -> int:
    import shlex

    from project_studio.buildops import configure

    root = _root_or_die(args)
    if root is None:
        return 2
    extra = shlex.split(args.extra, posix=os.name != "nt") if args.extra else []
    r = configure(
        root,
        build_dir=args.build_dir,
        build_type=args.build_type,
        generator=args.generator if args.generator is not None else None,
        extra_args=extra,
        dry_run=args.dry_run,
        log=print,
        ensure_bios=not bool(getattr(args, "skip_bios", False)),
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    return 0 if r.ok else 1


def cmd_build_ensure_bios(args: argparse.Namespace) -> int:
    if platforms.current().key == "snes":
        print(
            "error: ensure-bios is psxrecomp-only — a SNES cartridge has no BIOS "
            "backend and no separate emitter build. Use `build generate` "
            "(tools/regen.sh) instead.",
            file=sys.stderr,
        )
        return 2
    from project_studio.buildops import ensure_bios_backends

    root = _root_or_die(args)
    if root is None:
        return 2
    r = ensure_bios_backends(
        root,
        force=bool(getattr(args, "force", False)),
        include_scph1001=not bool(getattr(args, "openbios_only", False)),
        dry_run=args.dry_run,
        log=print,
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    return 0 if r.ok else 1


def cmd_build_generate(args: argparse.Namespace) -> int:
    from project_studio.buildops import generate_rom_and_bios, generate_snes_c

    root = _root_or_die(args)
    if root is None:
        return 2
    if platforms.current().key == "snes":
        # --disc carries the ROM path here, same single-image rule as
        # new-project. There is no BIOS half on a cartridge.
        r = generate_snes_c(
            root,
            rom=getattr(args, "disc", "") or "",
            cfg_roots=bool(getattr(args, "cfg_roots", False)),
            verify=not bool(getattr(args, "no_verify", False)),
            dry_run=args.dry_run,
            log=print,
        )
        print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
        if r.detail and not r.ok:
            print(r.detail)
        return 0 if r.ok else 1
    r = generate_rom_and_bios(
        root,
        disc=getattr(args, "disc", "") or "",
        bios=getattr(args, "bios", "") or "",
        force_bios=not bool(getattr(args, "no_force_bios", False)),
        dry_run=args.dry_run,
        log=print,
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    if r.detail and not r.ok:
        print(r.detail)
    return 0 if r.ok else 1


def cmd_build_ensure_emitters(args: argparse.Namespace) -> int:
    if platforms.current().key == "snes":
        print(
            "error: ensure-emitters is psxrecomp-only — a SNES cartridge has no BIOS "
            "backend and no separate emitter build. Use `build generate` "
            "(tools/regen.sh) instead.",
            file=sys.stderr,
        )
        return 2
    from project_studio.buildops import ensure_emitters

    root = _root_or_die(args)
    if root is None:
        return 2
    r = ensure_emitters(
        root,
        force=bool(getattr(args, "force", False)),
        dry_run=args.dry_run,
        log=print,
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    if r.detail and not r.ok:
        print(r.detail)
    return 0 if r.ok else 1


def cmd_build_compile(args: argparse.Namespace) -> int:
    from project_studio.buildops import build, default_target

    root = _root_or_die(args)
    if root is None:
        return 2
    target = (getattr(args, "target", None) or "").strip() or default_target(root)
    r = build(
        root,
        build_dir=args.build_dir,
        target=target,
        jobs=args.jobs or None,
        dry_run=args.dry_run,
        log=print,
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    return 0 if r.ok else 1


def cmd_build_package(args: argparse.Namespace) -> int:
    """Bundle the existing local build into dist/*.zip (Studio Bundle+Export)."""
    import json
    from pathlib import Path

    from project_studio.buildops import package_local

    root = _root_or_die(args)
    if root is None:
        return 2

    def _log(line: str) -> None:
        # Piped into Studio — must flush or activity log stalls until buffer fill.
        print(line, flush=True)

    r = package_local(
        root,
        build_dir=args.build_dir,
        artifact_tag=args.tag,
        exe=Path(args.exe) if args.exe else None,
        use_repo_script=not args.no_repo_script,
        dry_run=args.dry_run,
        log=_log,
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}", flush=True)
    # Studio Bundle+Export parses the BUNDLE_ZIP line / JSON trailer.
    zip_path = str(r.zip_path) if r.zip_path else ""
    if r.ok and zip_path:
        print(f"BUNDLE_ZIP={zip_path}", flush=True)
        print(json.dumps({"ok": True, "zip": zip_path}), flush=True)
    elif not args.dry_run:
        print(json.dumps({"ok": False, "zip": "", "error": r.message}), flush=True)
    return 0 if r.ok else 1


def cmd_build_run(args: argparse.Namespace) -> int:
    import shlex
    from pathlib import Path

    from project_studio.buildops import launch, launch_rom_for

    root = _root_or_die(args)
    if root is None:
        return 2
    extra = shlex.split(args.args, posix=os.name != "nt") if args.args else []

    rom, whence = launch_rom_for(root, getattr(args, "rom", None) or "")
    if rom and whence != "explicit":
        print(f"rom: {rom} (from {whence})", flush=True)
    if rom:
        rom_p = Path(rom).expanduser()
        if not rom_p.is_file():
            print(f"error: ROM not found: {rom_p}", file=sys.stderr)
            return 2
        # FIRST, not appended. Both the scaffolded host and the runner's
        # snesrecomp_launcher_resolve_rom_sha256() read the ROM from argv[1],
        # so a flag ahead of it (e.g. --args "--launcher") makes the ROM
        # invisible to both and the host falls through to a file picker.
        extra.insert(0, str(rom_p))

    def _log(line: str) -> None:
        # Piped into Studio — must flush or activity log stalls until buffer fill.
        print(line, flush=True)

    r = launch(
        root,
        build_dir=args.build_dir,
        exe=Path(args.exe) if args.exe else None,
        env_text=args.env or "",
        extra_args=extra,
        dry_run=args.dry_run,
        log=_log,
        wait=True,
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}", flush=True)
    return 0 if r.ok else 1


def cmd_build_stop(args: argparse.Namespace) -> int:
    from project_studio.buildops import stop_launch

    r = stop_launch()
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}", flush=True)
    return 0 if r.ok else 1


def cmd_build_status(args: argparse.Namespace) -> int:
    from project_studio.buildops import (
        detect_host,
        find_runtime_exe,
        launch_status,
        resolve_build_dir,
    )

    root = _root_or_die(args)
    if root is None:
        return 2
    host = detect_host()
    bdir = resolve_build_dir(root, args.build_dir)
    exe = find_runtime_exe(bdir)
    print(f"host:      {host.label} ({host.system})")
    print(f"cmake:     {host.cmake or '(missing)'}")
    print(f"ninja:     {host.ninja or '(missing)'}")
    print(f"jobs:      {host.jobs}")
    print(f"build_dir: {bdir}  exists={bdir.is_dir()}")
    print(f"exe:       {exe or '(not found)'}")
    print(f"launch:    {launch_status()}")
    return 0 if exe else 1


def _analyze_run_is_psx_only() -> bool:
    """Only `analyze run` is psxrecomp-specific.

    It invokes psxrecomp-analyze over a boot executable. snesrecomp has no
    equivalent step to invoke: its analyzer runs inside tools/regen.sh and
    leaves src/gen/program_manifest.json behind, so on SNES the discovery has
    already happened and the other subcommands read its result. Refusing all
    of `analyze` under --platform snes, as this once did, is what left the
    console with no Functions tab at all.
    """
    if platforms.current().key != "snes":
        return False
    print(
        "error: analyze run wraps psxrecomp-analyze. snesrecomp analyses as part "
        "of code generation — run tools/regen.sh, which writes "
        "src/gen/program_manifest.json, then `analyze status` reads it.",
        file=sys.stderr,
    )
    return True


def cmd_analyze_status(args: argparse.Namespace) -> int:
    root = _root_or_die(args)
    if root is None:
        return 2
    if platforms.is_snes():
        from project_studio.snes_analyzeops import status as snes_status

        print(json.dumps(snes_status(root), indent=2))
        return 0
    from project_studio.analyzeops import status

    print(json.dumps(status(root), indent=2))
    return 0


def cmd_analyze_run(args: argparse.Namespace) -> int:
    if _analyze_run_is_psx_only():
        return 2
    from project_studio.analyzeops import run_analysis

    root = _root_or_die(args)
    if root is None:
        return 2
    r = run_analysis(
        root,
        exe=getattr(args, "exe", "") or "",
        exact=bool(getattr(args, "exact", False)),
        with_refs=bool(getattr(args, "refs", False)),
        emit_symbols=bool(getattr(args, "emit_symbols", False)),
        min_confidence=getattr(args, "min_confidence", "high") or "high",
        emit_ghidra=bool(getattr(args, "emit_ghidra", False)),
        emit_symbol_addrs=bool(getattr(args, "emit_symbol_addrs", False)),
        diff=not bool(getattr(args, "no_diff", False)),
        widescreen=bool(getattr(args, "widescreen", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        log=print,
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    if r.detail and not r.ok:
        print(r.detail)
    return 0 if r.ok else 1


def _parse_pc(text: str) -> int:
    text = text.strip()
    return int(text, 16 if text.lower().startswith("0x") else 0)


def cmd_analyze_set_symbol(args: argparse.Namespace) -> int:
    root = _root_or_die(args)
    if root is None:
        return 2
    if platforms.is_snes():
        # Not through _parse_pc: it reads a bare "838C" as decimal-or-invalid,
        # and bare hex is how SNES addresses are written everywhere in this
        # ecosystem — symbols.toml, bank*.cfg, the manifest keys.
        # snes_analyzeops normalises the forms it accepts.
        from project_studio.snes_analyzeops import set_symbol as snes_set

        r = snes_set(
            root, args.pc,
            name=getattr(args, "name", "") or "",
            emit=(None if getattr(args, "emit", "") == "" else args.emit == "true"),
            note=(getattr(args, "note", "") or None),
        )
        print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
        if r.detail:
            print(f"       {r.detail}")
        return 0 if r.ok else 1
    from project_studio.analyzeops import set_symbol

    try:
        pc = _parse_pc(args.pc)
    except ValueError:
        print(f"[FAIL] bad --pc {args.pc!r}")
        return 2
    r = set_symbol(
        root,
        pc,
        args.name,
        status_value=getattr(args, "status", "") or "",
        note=getattr(args, "note", "") or "",
        emit=(None if getattr(args, "emit", "") == "" else args.emit == "true"),
    )
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    return 0 if r.ok else 1


def cmd_analyze_clear_symbol(args: argparse.Namespace) -> int:
    root = _root_or_die(args)
    if root is None:
        return 2
    if platforms.is_snes():
        from project_studio.snes_analyzeops import clear_symbol as snes_clear

        r = snes_clear(root, args.pc)
        print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
        return 0 if r.ok else 1
    from project_studio.analyzeops import clear_symbol

    try:
        pc = _parse_pc(args.pc)
    except ValueError:
        print(f"[FAIL] bad --pc {args.pc!r}")
        return 2
    r = clear_symbol(root, pc)
    print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
    return 0 if r.ok else 1


def cmd_analyze_symbols(args: argparse.Namespace) -> int:
    root = _root_or_die(args)
    if root is None:
        return 2
    if platforms.is_snes():
        from project_studio.snes_analyzeops import read_symbols as snes_read

        print(json.dumps({"symbols": snes_read(root)}, indent=2))
        return 0
    from project_studio.analyzeops import read_symbols

    print(json.dumps({"symbols": read_symbols(root)}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="migrate_project",
        description=(
            "PSXRecomp Project Studio — migrate / update title repos to the "
            "New Project Layout (setup-host releases only)."
        ),
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # Global, not per-command: the platform decides which repo index, which
    # framework submodule and which scaffolder every subcommand reaches for, so
    # it has to be resolved before the subparser runs rather than threaded
    # through each one. Defaults to psx so every pre-SNES invocation is
    # byte-for-byte unchanged.
    ap.add_argument(
        "--platform",
        choices=list(platforms.KEYS),
        default=platforms.DEFAULT_KEY,
        help="Console this session works on (default: psx)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_root(p: argparse.ArgumentParser, required: bool = True) -> None:
        p.add_argument(
            "--root",
            required=required,
            help="Game repository root",
        )

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--disc", help="Redump .cue for probe_disc refresh")
        p.add_argument("--name", help="Project name override")
        p.add_argument("--boot-exe", help="Boot EXE basename (e.g. SCUS_944.23)")
        p.add_argument("--players", type=int, default=2)
        p.add_argument("--zip-prefix", help="CI/zip prefix")
        p.add_argument("--github-owner", help="GitHub owner/org for README download badges")
        p.add_argument("--github-repo", help="GitHub repo name for README download badges")
        p.add_argument("--window-title", help="WINDOW_TITLE override")
        p.add_argument("--enable-netplay", action="store_true")
        p.add_argument("--lobby-url", default="ws://netplay.retcomm.net:8765")
        p.add_argument("--no-recomp-ui", action="store_true",
                       help="Ignored for setup-host apply (forced on)")
        p.add_argument("--no-wizard", action="store_true",
                       help="Ignored for setup-host apply (forced on)")
        p.add_argument("--no-ci", action="store_true")
        p.add_argument("--no-boxart", action="store_true")
        p.add_argument("--no-rewrite-cmake", action="store_true")
        p.add_argument("--no-gitignore", action="store_true")
        p.add_argument("--no-probe", action="store_true")
        p.add_argument("--no-pins", action="store_true")
        p.add_argument("--force", action="store_true", help="Overwrite existing stubs")
        p.add_argument("--only", help="Comma-separated op ids")
        p.add_argument("--skip", help="Comma-separated op ids to skip")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--json", action="store_true")

    p_audit = sub.add_parser("audit", help="Audit a title repo")
    add_root(p_audit)
    p_audit.add_argument("--json", action="store_true")
    p_audit.set_defaults(func=cmd_audit)

    p_plan = sub.add_parser("plan", help="Show migration plan")
    add_root(p_plan)
    add_common(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="Apply migration plan")
    add_root(p_apply)
    add_common(p_apply)
    p_apply.add_argument("--json-plan", action="store_true")
    p_apply.set_defaults(func=cmd_apply)

    p_ops = sub.add_parser("ops", help="List op ids")
    p_ops.set_defaults(func=cmd_ops)

    p_gui = sub.add_parser("gui", help="Open Project Studio GUI (Dear ImGui)")
    p_gui.add_argument("--root", default=None, help="Optional initial game root")
    p_gui.set_defaults(func=cmd_gui)

    p_repos = sub.add_parser("repos", help="Manage local game-repo index")
    repos_sub = p_repos.add_subparsers(dest="repos_cmd", required=True)
    p_rl = repos_sub.add_parser("list", help="Print index as JSON")
    p_rl.add_argument("--json", action="store_true", default=True,
                      help="Always JSON (accepted for ImGui runner symmetry)")
    p_rl.set_defaults(func=cmd_repos_list)
    p_ra = repos_sub.add_parser("add", help="Add a game repo")
    p_ra.add_argument("--path", required=True)
    p_ra.add_argument("--name", default="")
    p_ra.add_argument("--cue", default="")
    p_ra.add_argument("--force", action="store_true")
    p_ra.add_argument("--json", action="store_true", default=True)
    p_ra.set_defaults(func=cmd_repos_add)
    p_rr = repos_sub.add_parser("remove", help="Remove a game repo")
    p_rr.add_argument("--path", required=True)
    p_rr.add_argument("--json", action="store_true", default=True)
    p_rr.set_defaults(func=cmd_repos_remove)
    p_rsc = repos_sub.add_parser(
        "set-cue", help="Set the game image (.cue / ROM) for an indexed repo"
    )
    p_rsc.add_argument("--path", required=True)
    # Not argparse-required: --rom is a second action on the same dest, and
    # argparse enforces required per action, so --rom alone would still fail.
    # cmd_repos_set_cue reports a missing image in the platform's own words.
    p_rsc.add_argument("--cue", default="")
    # --rom for the same reason new-project takes one: a cartridge has no cue,
    # and the flag that reads wrong is the flag that gets left unset.
    p_rsc.add_argument("--rom", dest="cue", help="Alias for --cue (SNES)")
    p_rsc.add_argument("--json", action="store_true", default=True)
    p_rsc.set_defaults(func=cmd_repos_set_cue)
    p_rcc = repos_sub.add_parser(
        "clear-cue", help="Forget the game image recorded for an indexed repo"
    )
    p_rcc.add_argument("--path", required=True)
    p_rcc.add_argument("--json", action="store_true", default=True)
    p_rcc.set_defaults(func=cmd_repos_clear_cue)
    p_rsl = repos_sub.add_parser("set-last", help="Remember last-selected repo")
    p_rsl.add_argument("--path", required=True)
    p_rsl.add_argument("--json", action="store_true", default=True)
    p_rsl.set_defaults(func=cmd_repos_set_last)
    p_rsf = repos_sub.add_parser("set-flags", help="Update catalog_only / bulk_jobs / log_height")
    p_rsf.add_argument("--catalog-only", default=None)
    p_rsf.add_argument("--bulk-jobs", default=None)
    p_rsf.add_argument("--log-height", default=None)
    p_rsf.add_argument("--json", action="store_true", default=True)
    p_rsf.set_defaults(func=cmd_repos_set_flags)

    p_rfc = repos_sub.add_parser(
        "filter-catalog",
        help="List indexed repos that have a retcomm-catalog entry (JSON paths)",
    )
    p_rfc.add_argument("--json", action="store_true", default=True)
    p_rfc.set_defaults(func=cmd_repos_filter_catalog)

    p_rfcc = repos_sub.add_parser(
        "filter-catalog-contributors",
        help="Catalog-backed indexed repos with WRITE+ for the current gh user",
    )
    p_rfcc.add_argument("--json", action="store_true", default=True)
    p_rfcc.set_defaults(func=cmd_repos_filter_catalog_contributors)

    p_upd = sub.add_parser("updates", help="Studio + toolchain updates")
    upd_sub = p_upd.add_subparsers(dest="updates_cmd", required=True)
    p_uc = upd_sub.add_parser("check", help="Check for updates")
    p_uc.add_argument("--json", action="store_true")
    p_uc.add_argument(
        "--startup",
        action="store_true",
        help="Honor check_updates_on_startup from studio/hub config (skip when false)",
    )
    p_uc.set_defaults(func=cmd_updates_check)
    p_ua = upd_sub.add_parser("apply", help="Apply available updates")
    p_ua.add_argument("--studio-only", action="store_true")
    p_ua.add_argument("--toolchain-only", action="store_true")
    p_ua.set_defaults(func=cmd_updates_apply)
    p_uet = upd_sub.add_parser(
        "ensure-toolchain",
        help="Install cmake-clang-v1 if missing (github.com downloads, no API listing)",
    )
    p_uet.set_defaults(func=cmd_updates_ensure_toolchain)

    p_np = sub.add_parser(
        "new-project",
        help="Run setup_project.sh/.ps1 (OS-routed) then index the new repo",
    )
    p_np.add_argument("--name", required=True, help="Project folder / display name")
    # Not argparse-required: --rom is a second action on the same dest, and
    # argparse enforces required per action, so --rom alone would still fail.
    # validate_options() reports the missing image with a platform-correct name.
    # Repeatable, so args.disc is always a list. On PSX the repeats are discs
    # 2..N of a set; SNES still takes exactly one image and validate_options
    # rejects extras there.
    p_np.add_argument(
        "--disc",
        action="append",
        default=None,
        metavar="IMAGE",
        help="Game image: Redump .cue (psx) or ROM .sfc/.smc (snes). "
             "Repeatable on PSX for a multi-disc title — pass in disc order, "
             "boot disc first (max 4).",
    )
    # --rom reads better for a cartridge and writes the same field. One field,
    # because a project has one game image and two would let them disagree.
    # It must append too: mixing a store action into an append dest would make
    # args.disc a string on one path and a list on the other.
    p_np.add_argument(
        "--rom", dest="disc", action="append", help="Alias for --disc (SNES)"
    )
    p_np.add_argument(
        "--dir",
        default=".",
        help="Parent directory for the new repo (default: .)",
    )
    p_np.add_argument("--bios", default="", help="Optional SCPH1001.BIN")
    p_np.add_argument("--boot-exe", default="")
    p_np.add_argument("--players", type=int, default=2)
    p_np.add_argument("--zip-prefix", default="")
    p_np.add_argument("--github-owner", default="")
    p_np.add_argument("--github-repo", default="")
    p_np.add_argument("--description", default="")
    p_np.add_argument("--publisher", default="")
    p_np.add_argument("--year", default="")
    # No default here: "USA" is a PSX habit, and on SNES an unset region means
    # "use the cartridge header", which is a better answer than any guess.
    # cmd_new_project restores the PSX default.
    p_np.add_argument("--region", default="")
    p_np.add_argument("--lobby-url", default="netplay.retcomm.net")
    p_np.add_argument("--no-recomp-ui", action="store_true")
    p_np.add_argument("--no-wizard", action="store_true")
    p_np.add_argument("--enable-netplay", action="store_true")
    p_np.add_argument("--no-ci", action="store_true")
    p_np.add_argument("--no-fetch-boxart", action="store_true")
    p_np.add_argument("--stage-disc", action="store_true")
    p_np.add_argument("--generate", action="store_true")
    p_np.add_argument("--enable-build", action="store_true")
    p_np.add_argument("--create-github", action="store_true")
    p_np.add_argument(
        "--github-visibility",
        choices=("private", "public", "internal"),
        default="private",
    )
    p_np.add_argument("--psxrecomp-ref", "--framework-ref", dest="psxrecomp_ref",
                      default="master")
    p_np.add_argument("--snesrecomp-ref", default="main")
    p_np.add_argument(
        "--multitap",
        choices=("port1", "port2", "both", "off"),
        default="",
        help="SNES: override the seat-count-derived tap layout",
    )
    p_np.add_argument(
        "--enable-rollback",
        action="store_true",
        help="SNES: build retcomm-rbengine in (implies netplay)",
    )
    p_np.add_argument("--recomp-ui-ref", default="master")
    p_np.add_argument("--recomp-net-ref", default="")
    p_np.add_argument("--rbengine-ref", default="")
    p_np.add_argument("--dry-run", action="store_true")
    p_np.add_argument(
        "--autofill-meta",
        action="store_true",
        help="Fill empty players/description/publisher/year/region from disc digests",
    )
    p_np.set_defaults(func=cmd_new_project)

    p_pr = sub.add_parser(
        "probe-rom",
        help="SNES cartridge identity as JSON (New Project defaults)",
    )
    p_pr.add_argument("--rom", required=True, help="Path to a .sfc / .smc ROM")
    p_pr.add_argument(
        "--root",
        default="",
        help="Project whose snesrecomp/ checkout should supply the probe",
    )
    p_pr.set_defaults(func=cmd_probe_rom)

    p_meta = sub.add_parser(
        "lookup-disc-meta",
        help="Lookup players/description/publisher/year/region from disc digests",
    )
    p_meta.add_argument("--disc", default="", help="Redump .cue to probe")
    p_meta.add_argument("--crc32", default="")
    p_meta.add_argument("--md5", default="")
    p_meta.add_argument("--sha1", default="")
    p_meta.add_argument("--serial", default="")
    p_meta.add_argument("--force-refresh", action="store_true")
    p_meta.add_argument("--json", action="store_true")
    p_meta.set_defaults(func=cmd_lookup_disc_meta)

    # --- git / GitHub ---
    p_git = sub.add_parser("git", help="Git / GitHub ops on a game repo")
    git_sub = p_git.add_subparsers(dest="git_cmd", required=True)

    def add_git_root(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", required=True, help="Game repository root")
        p.add_argument("--dry-run", action="store_true")

    p_gs = git_sub.add_parser("status", help="Repo + submodule status")
    add_git_root(p_gs)
    p_gs.add_argument("--json", action="store_true")
    p_gs.set_defaults(func=cmd_git_status)

    p_gbrs = git_sub.add_parser(
        "branches",
        help="List game/module branch names (JSON) for Studio dropdowns",
    )
    p_gbrs.add_argument(
        "--root",
        default="",
        help="Game repo root (omit to ls-remote default module URLs)",
    )
    p_gbrs.add_argument(
        "--fetch",
        action="store_true",
        help="git fetch --prune before listing local checkouts",
    )
    p_gbrs.add_argument("--json", action="store_true", default=True)
    p_gbrs.set_defaults(func=cmd_git_branches)

    p_ge = git_sub.add_parser("ensure-submodules", help="Add psxrecomp + recomp-ui")
    add_git_root(p_ge)
    # --framework-branch is the platform-neutral spelling the GUI emits;
    # --psxrecomp-branch stays as an alias so existing scripts keep working.
    p_ge.add_argument(
        "--psxrecomp-branch", "--framework-branch", dest="psxrecomp_branch", default=""
    )
    p_ge.add_argument("--recomp-ui-branch", default="master")
    p_ge.set_defaults(func=cmd_git_ensure_submodules)

    p_gen = git_sub.add_parser(
        "ensure-nested",
        help="Add lib/recomp-net + lib/retcomm-rbengine inside psxrecomp",
    )
    add_git_root(p_gen)
    p_gen.add_argument("--recomp-net-branch", default="main")
    p_gen.add_argument("--rbengine-branch", default="main")
    p_gen.set_defaults(func=cmd_git_ensure_nested)

    p_gb = git_sub.add_parser(
        "set-branch",
        help="Switch game branch (git switch) or set submodule tracking in .gitmodules",
    )
    add_git_root(p_gb)
    p_gb.add_argument("--branch", required=True)
    p_gb.add_argument(
        "--submodule",
        help="Submodule path (e.g. psxrecomp). Omit to switch the game working tree.",
    )
    p_gb.add_argument(
        "--nested",
        action="store_true",
        help="Treat --submodule as a path inside psxrecomp (e.g. lib/recomp-net)",
    )
    p_gb.add_argument(
        "--create",
        action="store_true",
        help="Create the game branch (-c) if it does not exist",
    )
    p_gb.set_defaults(func=cmd_git_set_branch)

    p_gsw = git_sub.add_parser(
        "switch",
        help="git switch on game / --modules / --nested (combinable)",
    )
    add_git_root(p_gsw)
    p_gsw.add_argument(
        "--branch",
        default="",
        help="Game-root branch (with --game / default). "
        "With --modules/--nested alone, omit to use each .gitmodules tracking branch.",
    )
    p_gsw.add_argument(
        "--game",
        action="store_true",
        help="Switch the game repo root (default when no other target flags)",
    )
    p_gsw.add_argument(
        "--modules",
        action="store_true",
        help="Switch game submodules (psxrecomp, recomp-ui)",
    )
    p_gsw.add_argument(
        "--nested",
        action="store_true",
        help="Switch nested libs inside psxrecomp (recomp-net, rbengine)",
    )
    p_gsw.add_argument(
        "--psxrecomp",
        "--framework",
        dest="psxrecomp",
        action="store_true",
        help="Switch the psxrecomp checkout itself",
    )
    p_gsw.add_argument(
        "--psxrecomp-branch",
        "--framework-branch",
        dest="psxrecomp_branch",
        default="",
        help="psxrecomp branch (with --modules or --psxrecomp)",
    )
    p_gsw.add_argument(
        "--ui-branch",
        default="",
        help="recomp-ui branch (with --modules)",
    )
    p_gsw.add_argument(
        "--net-branch",
        default="",
        help="lib/recomp-net branch (with --nested)",
    )
    p_gsw.add_argument(
        "--rb-branch",
        default="",
        help="lib/retcomm-rbengine branch (with --nested)",
    )
    p_gsw.add_argument(
        "--submodule",
        default="",
        help="Single module path (with --nested if inside psxrecomp)",
    )
    p_gsw.add_argument(
        "--paths",
        help="Comma-separated module paths (defaults depend on --modules/--nested)",
    )
    p_gsw.add_argument(
        "--create",
        action="store_true",
        help="Create the branch (-c) if it does not exist locally or remotely",
    )
    p_gsw.add_argument(
        "--no-track",
        action="store_true",
        help="Do not update .gitmodules branch= when switching modules",
    )
    p_gsw.set_defaults(func=cmd_git_switch)

    p_gu = git_sub.add_parser("update-submodules", help="git submodule update")
    add_git_root(p_gu)
    p_gu.add_argument(
        "--remote",
        action="store_true",
        help="Update to remote tracking branch tip (then commit gitlinks)",
    )
    p_gu.add_argument("--paths", help="Comma-separated submodule paths")
    p_gu.set_defaults(func=cmd_git_update_submodules)

    p_gun = git_sub.add_parser(
        "update-nested",
        help="Update nested modules inside psxrecomp (recomp-net, rbengine)",
    )
    add_git_root(p_gun)
    p_gun.add_argument("--remote", action="store_true")
    p_gun.add_argument("--paths", help="Comma-separated nested paths")
    p_gun.add_argument(
        "--no-stage",
        action="store_true",
        help="Do not git-add nested gitlinks inside psxrecomp",
    )
    p_gun.set_defaults(func=cmd_git_update_nested)

    p_gcn = git_sub.add_parser(
        "commit-nested",
        help="Commit inside psxrecomp (after update-nested)",
    )
    add_git_root(p_gcn)
    p_gcn.add_argument("-m", "--message", required=True)
    p_gcn.set_defaults(func=cmd_git_commit_nested)

    p_gpull = git_sub.add_parser(
        "pull",
        help="git pull (game / --modules / --nested; combinable)",
    )
    add_git_root(p_gpull)
    p_gpull.add_argument(
        "--game",
        action="store_true",
        help="Pull the game repo root (default when no other target flags)",
    )
    p_gpull.add_argument(
        "--modules",
        action="store_true",
        help="Pull game submodules (psxrecomp, recomp-ui)",
    )
    p_gpull.add_argument(
        "--nested",
        action="store_true",
        help="Pull nested libs inside psxrecomp (recomp-net, rbengine)",
    )
    p_gpull.add_argument(
        "--psxrecomp",
        "--framework",
        dest="psxrecomp",
        action="store_true",
        help="Pull the psxrecomp checkout itself",
    )
    p_gpull.add_argument(
        "--paths",
        help="Comma-separated module paths (defaults depend on --modules/--nested)",
    )
    p_gpull.add_argument(
        "--mode",
        choices=("ff-only", "rebase", "merge", "reset"),
        default="ff-only",
        help="ff-only (default), rebase, merge (--no-rebase), or reset "
        "(fetch + reset --hard upstream = match origin)",
    )
    p_gpull.add_argument(
        "--dirty",
        choices=("fail", "stash", "discard"),
        default="fail",
        help="If working tree dirty: fail (default), stash, or discard "
        "(reset --hard HEAD). Ignored for --mode reset.",
    )
    p_gpull.set_defaults(func=cmd_git_pull)

    p_gc = git_sub.add_parser(
        "commit",
        help="git add -A && git commit (game / --modules / --nested; combinable)",
    )
    add_git_root(p_gc)
    p_gc.add_argument("-m", "--message", required=True)
    p_gc.add_argument(
        "--game",
        action="store_true",
        help="Commit the game repo root (default when no other target flags)",
    )
    p_gc.add_argument(
        "--modules",
        action="store_true",
        help="Commit inside game submodules (psxrecomp, recomp-ui)",
    )
    p_gc.add_argument(
        "--nested",
        action="store_true",
        help="Commit inside nested libs (recomp-net, rbengine)",
    )
    p_gc.add_argument(
        "--paths",
        help="Comma-separated module paths (defaults depend on --modules/--nested)",
    )
    p_gc.set_defaults(func=cmd_git_commit)

    p_gpush = git_sub.add_parser(
        "push",
        help="git push -u origin HEAD (game / --modules / --nested; combinable)",
    )
    add_git_root(p_gpush)
    p_gpush.add_argument(
        "--game",
        action="store_true",
        help="Push the game repo root (default when no other target flags)",
    )
    p_gpush.add_argument(
        "--modules",
        action="store_true",
        help="Push game submodules (psxrecomp, recomp-ui)",
    )
    p_gpush.add_argument(
        "--nested",
        action="store_true",
        help="Push nested libs inside psxrecomp (recomp-net, rbengine)",
    )
    p_gpush.add_argument(
        "--psxrecomp",
        "--framework",
        dest="psxrecomp",
        action="store_true",
        help="Push the psxrecomp checkout itself",
    )
    p_gpush.add_argument(
        "--paths",
        help="Comma-separated module paths (defaults depend on --modules/--nested)",
    )
    p_gpush.add_argument(
        "--branch",
        default="",
        help="Branch name for detached-HEAD pushes (HEAD:refs/heads/BRANCH)",
    )
    p_gpush.set_defaults(func=cmd_git_push)

    def add_bulk_select(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--select",
            default="",
            help="Comma-separated name/path filters (default: all indexed repos)",
        )
        p.add_argument("--dry-run", action="store_true")

    def add_bulk_targets(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--game",
            action="store_true",
            help="Operate on game repo root (default if no target flags)",
        )
        p.add_argument(
            "--modules",
            action="store_true",
            help="Operate on psxrecomp + recomp-ui",
        )
        p.add_argument(
            "--psxrecomp",
        "--framework",
        dest="psxrecomp",
            action="store_true",
            help="Operate on the psxrecomp checkout",
        )
        p.add_argument(
            "--nested",
            action="store_true",
            help="Operate on nested libs (recomp-net, rbengine)",
        )

    p_gbs = git_sub.add_parser(
        "bulk-status",
        help="Status for indexed repos (project_studio_repos.json)",
    )
    add_bulk_select(p_gbs)
    p_gbs.set_defaults(func=cmd_git_bulk_status)

    p_gbp = git_sub.add_parser(
        "bulk-pull",
        help="Pull indexed repos (see --mode / --dirty / targets)",
    )
    add_bulk_select(p_gbp)
    add_bulk_targets(p_gbp)
    p_gbp.add_argument(
        "--mode",
        choices=("ff-only", "rebase", "merge", "reset"),
        default="ff-only",
    )
    p_gbp.add_argument(
        "--dirty",
        choices=("fail", "stash", "discard"),
        default="fail",
    )
    p_gbp.set_defaults(func=cmd_git_bulk_pull)

    p_gbpush = git_sub.add_parser(
        "bulk-push",
        help="Push indexed repos (game / modules / psxrecomp / nested)",
    )
    add_bulk_select(p_gbpush)
    add_bulk_targets(p_gbpush)
    p_gbpush.set_defaults(func=cmd_git_bulk_push)

    p_gbc = git_sub.add_parser(
        "bulk-commit",
        help="Commit indexed repos (game / modules / nested)",
    )
    add_bulk_select(p_gbc)
    add_bulk_targets(p_gbc)
    p_gbc.add_argument("-m", "--message", required=True)
    p_gbc.set_defaults(func=cmd_git_bulk_commit)

    p_gbsw = git_sub.add_parser(
        "bulk-switch",
        help="git switch submodule / nested / game branches on indexed repos",
    )
    add_bulk_select(p_gbsw)
    add_bulk_targets(p_gbsw)
    p_gbsw.add_argument(
        "--branch",
        default="",
        help="Game-root branch (with --game)",
    )
    p_gbsw.add_argument(
        "--psxrecomp-branch",
        "--framework-branch",
        dest="psxrecomp_branch",
        default="",
        help="psxrecomp branch (with --modules or --psxrecomp)",
    )
    p_gbsw.add_argument(
        "--ui-branch",
        default="",
        help="recomp-ui branch (with --modules)",
    )
    p_gbsw.add_argument(
        "--net-branch",
        default="",
        help="lib/recomp-net branch (with --nested)",
    )
    p_gbsw.add_argument(
        "--rb-branch",
        default="",
        help="lib/retcomm-rbengine branch (with --nested)",
    )
    p_gbsw.add_argument(
        "--create",
        action="store_true",
        help="Create the branch (-c) if missing",
    )
    p_gbsw.add_argument(
        "--no-track",
        action="store_true",
        help="Do not update .gitmodules branch= tracking",
    )
    p_gbsw.set_defaults(func=cmd_git_bulk_switch)


    p_gbr = git_sub.add_parser(
        "bulk-release",
        help="Dispatch release.yml on selected indexed repos (gh workflow run)",
    )
    add_bulk_select(p_gbr)
    p_gbr.add_argument("--version", default="", help="Empty = auto-bump per repo")
    p_gbr.add_argument(
        "--bump", choices=("patch", "minor", "major"), default="patch"
    )
    p_gbr.add_argument("--no-publish", action="store_true")
    p_gbr.add_argument("--no-reuse-cached-emitters", action="store_true")
    p_gbr.add_argument(
        "--strict",
        action="store_true",
        help="Fail hard when release.yml is missing (default: skip)",
    )
    p_gbr.set_defaults(func=cmd_git_bulk_release)

    p_gbci = git_sub.add_parser(
        "bulk-install-ci",
        help="Install/push setup-host release.yml on selected indexed repos",
    )
    add_bulk_select(p_gbci)
    p_gbci.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an already-filled release.yml",
    )
    p_gbci.add_argument(
        "--no-push",
        action="store_true",
        help="Commit locally only (do not push)",
    )
    p_gbci.set_defaults(func=cmd_git_bulk_install_ci)

    p_gr = git_sub.add_parser("release", help="gh workflow run release.yml")
    add_git_root(p_gr)
    p_gr.add_argument("--version", default="", help="Empty = auto-bump")
    p_gr.add_argument(
        "--bump", choices=("patch", "minor", "major"), default="patch"
    )
    p_gr.add_argument("--no-publish", action="store_true")
    p_gr.add_argument("--no-reuse-cached-emitters", action="store_true")
    p_gr.set_defaults(func=cmd_git_release)

    p_gci = git_sub.add_parser(
        "install-ci",
        help="Write psxrecomp setup-release.yml, commit, and push",
    )
    add_git_root(p_gci)
    p_gci.add_argument("--zip-prefix", default="", help="CI asset zip prefix")
    p_gci.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an already-filled release.yml",
    )
    p_gci.add_argument(
        "--no-push",
        action="store_true",
        help="Commit locally only (do not push)",
    )
    p_gci.set_defaults(func=cmd_git_install_ci)

    # --- local cmake build / launch ---
    p_build = sub.add_parser("build", help="Local CMake configure / build / launch")
    build_sub = p_build.add_subparsers(dest="build_cmd", required=True)

    def add_build_root(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", required=True, help="Game repository root")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--build-dir", default="build-release")

    p_bm = build_sub.add_parser(
        "mingw",
        help="Linux→Windows MinGW cross-build (local testing; no GitHub CI)",
    )
    p_bm.add_argument(
        "--root",
        default="",
        help="Game repository root (default: Studio index --last / --name)",
    )
    p_bm.add_argument(
        "--name",
        default="",
        help="Match a Studio-indexed title by name (substring)",
    )
    p_bm.add_argument(
        "--last",
        action="store_true",
        help="Use Studio index 'last' project (default when --root/--name omitted)",
    )
    p_bm.add_argument("--build-dir", default="build-mingw")
    p_bm.add_argument(
        "--setup-host",
        action="store_true",
        help="CI-parity setup-host configure (FORCE_SETUP_HOST + wizard)",
    )
    p_bm.add_argument(
        "--package",
        action="store_true",
        help="Run scripts/package_setup_release.sh after build",
    )
    p_bm.add_argument(
        "--package-only",
        action="store_true",
        help="Skip configure/build; package existing MinGW build dir into dist/*.zip",
    )
    p_bm.add_argument(
        "--dynamic",
        action="store_true",
        help="PSX_STATIC_RUNTIME=OFF (ship SDL2/libgcc DLLs)",
    )
    p_bm.add_argument(
        "--ensure",
        action="store_true",
        help="Host ensure-emitters / ensure-bios / generate before cross-build",
    )
    p_bm.add_argument("--jobs", type=int, default=0)
    p_bm.add_argument(
        "--extra",
        default="",
        help="Extra cmake -D args, one shell string. Spell it --extra=-DFOO=ON: "
        "a leading-dash value after a space is parsed as an option, not a value.",
    )
    p_bm.add_argument("--dry-run", action="store_true")
    p_bm.set_defaults(func=cmd_build_mingw)

    p_bc = build_sub.add_parser("configure", help="cmake -S . -B <dir>")
    add_build_root(p_bc)
    p_bc.add_argument("--build-type", default="Release")
    p_bc.add_argument(
        "--generator",
        default=None,
        help="Ninja / 'Unix Makefiles' / Auto (empty: reuse CMakeCache, else Ninja if available)",
    )
    p_bc.add_argument(
        "--extra",
        default="",
        help="Extra cmake args, shell-quoted. Spell it --extra=-DPSX_DEBUG_TOOLS=ON: "
        "a leading-dash value after a space is parsed as an option, not a value.",
    )
    p_bc.add_argument(
        "--skip-bios",
        action="store_true",
        help="Do not auto-regen missing OpenBIOS before cmake",
    )
    p_bc.set_defaults(func=cmd_build_configure)

    p_beb = build_sub.add_parser(
        "ensure-bios",
        help="Regen OpenBIOS (+ SCPH1001 if dump present) under psxrecomp/generated",
    )
    add_build_root(p_beb)
    p_beb.add_argument(
        "--force",
        action="store_true",
        help="Regen even when generated backends already exist",
    )
    p_beb.add_argument(
        "--openbios-only",
        action="store_true",
        help="Skip SCPH1001 even if bios/SCPH1001.BIN exists",
    )
    p_beb.set_defaults(func=cmd_build_ensure_bios)

    p_bg = build_sub.add_parser(
        "generate",
        help="psxrecomp_cli generate: BIOS backends + disc prepare + game C",
    )
    add_build_root(p_bg)
    p_bg.add_argument(
        "--disc",
        default="",
        help="Source .cue (default: game.toml game.disc / indexed cue)",
    )
    p_bg.add_argument(
        "--bios",
        default="",
        help="Optional retail SCPH1001.BIN (omit for OpenBIOS only)",
    )
    p_bg.add_argument(
        "--no-force-bios",
        action="store_true",
        help="Skip BIOS regen when backends already exist",
    )
    p_bg.add_argument("--rom", dest="disc", help="Alias for --disc (SNES)")
    p_bg.add_argument(
        "--cfg-roots",
        action="store_true",
        help="SNES: seed analysis from every func declaration in recomp/*.cfg",
    )
    p_bg.add_argument(
        "--no-verify",
        action="store_true",
        help="SNES: skip regen.sh's ROM digest check",
    )
    p_bg.set_defaults(func=cmd_build_generate)

    p_bee = build_sub.add_parser(
        "ensure-emitters",
        help="Build psxrecomp-game + psxrecomp-bios (psxrecomp_cli ensure-emitters)",
    )
    add_build_root(p_bee)
    p_bee.add_argument(
        "--force",
        action="store_true",
        help="Rebuild even if emitter binaries already exist",
    )
    p_bee.set_defaults(func=cmd_build_ensure_emitters)

    p_bb = build_sub.add_parser("compile", help="cmake --build (alias: build)")
    add_build_root(p_bb)
    # Empty = ask buildops, which reads project() for SNES. A literal default
    # here would silently build the wrong (or no) target on a cartridge port.
    p_bb.add_argument("--target", default="")
    p_bb.add_argument("--jobs", type=int, default=0)
    p_bb.set_defaults(func=cmd_build_compile)

    p_bp = build_sub.add_parser(
        "package",
        help="Bundle the existing local build into dist/<prefix>-<ver>-<tag>.zip",
    )
    add_build_root(p_bp)
    p_bp.add_argument(
        "--tag",
        default="",
        help="Artifact tag (default: host, e.g. linux-x64 / windows-x64)",
    )
    p_bp.add_argument("--exe", default="", help="Override executable to package")
    p_bp.add_argument(
        "--no-repo-script",
        action="store_true",
        help="Ignore scripts/package_release.sh and use the built-in stager",
    )
    p_bp.set_defaults(func=cmd_build_package)

    p_br = build_sub.add_parser("run", help="Launch product binary with env")
    add_build_root(p_br)
    p_br.add_argument("--exe", default="", help="Override executable path")
    p_br.add_argument(
        "--env",
        default="",
        help='Env pairs, e.g. \'RBE_CROSS_OS_PACING_DIAG=1 FOO="bar baz"\'',
    )
    p_br.add_argument("--args", default="", help="Extra CLI args for the game")
    p_br.add_argument(
        "--rom",
        default="",
        help="SNES: ROM to run (default: the one recorded for this repo)",
    )
    p_br.set_defaults(func=cmd_build_run)

    p_bs = build_sub.add_parser("stop", help="Stop Studio-launched process")
    p_bs.set_defaults(func=cmd_build_stop)

    p_bst = build_sub.add_parser("status", help="Host + exe detection")
    add_build_root(p_bst)
    p_bst.set_defaults(func=cmd_build_status)

    # ---- analyze: static function discovery (Functions tab) -----------------
    ana = sub.add_parser("analyze", help="Static function discovery / symbols")
    ana_sub = ana.add_subparsers(dest="analyze_cmd", required=True)

    p_as = ana_sub.add_parser("status", help="Analysis + symbols presence (JSON)")
    p_as.add_argument("--root", required=True)
    p_as.set_defaults(func=cmd_analyze_status)

    p_ar = ana_sub.add_parser("run", help="Analyze the boot EXE (no runtime input)")
    p_ar.add_argument("--root", required=True)
    p_ar.add_argument("--dry-run", action="store_true")
    p_ar.add_argument("--exe", default="", help="Override the boot EXE path")
    p_ar.add_argument("--exact", action="store_true", help="Reachability-only partition")
    p_ar.add_argument("--refs", action="store_true", help="Also write refs.json")
    p_ar.add_argument("--no-diff", action="store_true", help="Skip the previous-run diff")
    p_ar.add_argument("--emit-symbols", action="store_true")
    p_ar.add_argument("--min-confidence", default="high")
    p_ar.add_argument("--emit-ghidra", action="store_true")
    p_ar.add_argument(
        "--widescreen", action="store_true", help="also scan [widescreen.cull] sites"
    )
    p_ar.add_argument("--emit-symbol-addrs", action="store_true")
    p_ar.set_defaults(func=cmd_analyze_run)

    p_asy = ana_sub.add_parser("symbols", help="Current symbols.toml entries (JSON)")
    p_asy.add_argument("--root", required=True)
    p_asy.set_defaults(func=cmd_analyze_symbols)

    p_ass = ana_sub.add_parser("set-symbol", help="Name one function in symbols.toml")
    p_ass.add_argument("--root", required=True)
    p_ass.add_argument("--pc", required=True)
    # Not required: on SNES the common edit is flipping `emit` on a function
    # that already has a name, and demanding one back would make Studio resend
    # the name it just read.
    p_ass.add_argument("--name", default="")
    p_ass.add_argument("--status", default="", help="guessed|confirmed|hot")
    p_ass.add_argument("--note", default="")
    p_ass.add_argument("--emit", default="", choices=["", "true", "false"])
    p_ass.set_defaults(func=cmd_analyze_set_symbol)

    p_acs = ana_sub.add_parser("clear-symbol", help="Remove one symbols.toml entry")
    p_acs.add_argument("--root", required=True)
    p_acs.add_argument("--pc", required=True)
    p_acs.set_defaults(func=cmd_analyze_clear_symbol)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    # Publish the choice before any ops module is imported and asked which
    # framework it is looking at.
    platforms.set_current(getattr(args, "platform", None))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
