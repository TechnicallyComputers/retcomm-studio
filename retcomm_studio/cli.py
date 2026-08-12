"""CLI: retcomm-studio status | ensure-modules | update-modules | …"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .bulk import bulk_map, collect_status, select_titles
from .workspace import load_workspace


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [x.strip() for x in value.split(",") if x.strip()]


def _add_select(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="Path to studio.toml")
    p.add_argument("--platform", help="Comma-separated platforms (e.g. psx)")
    p.add_argument("--titles", help="Comma-separated catalog title ids")
    p.add_argument("--engine", help="Comma-separated engines (e.g. psxrecomp)")
    p.add_argument(
        "--only-resolved",
        action="store_true",
        help="Skip titles with no local checkout",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="Parallel workers (default from studio.toml)",
    )
    p.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after first failure (forces jobs=1)",
    )
    p.add_argument("--json", action="store_true")


def _load_selection(args: argparse.Namespace):
    ws = load_workspace(Path(args.config) if args.config else None)
    pairs = select_titles(
        ws,
        platforms=_split_csv(args.platform),
        title_ids=_split_csv(args.titles),
        engines=_split_csv(args.engine),
        only_resolved=bool(args.only_resolved),
    )
    jobs = args.jobs if args.jobs is not None else ws.default_jobs
    if args.fail_fast:
        jobs = 1
    return ws, pairs, jobs


def cmd_list(args: argparse.Namespace) -> int:
    ws, pairs, _ = _load_selection(args)
    rows = []
    for title, root in pairs:
        rows.append(
            {
                "id": title.id,
                "name": title.name,
                "platform": title.platform,
                "engine": title.engine,
                "github": title.github,
                "root": str(root) if root else None,
                "resolved": root is not None,
            }
        )
    if args.json:
        print(json.dumps({"catalog": str(ws.catalog_root), "titles": rows}, indent=2))
        return 0
    print(f"RetComM Studio v{__version__}")
    print(f"  config:  {ws.config_path}")
    print(f"  catalog: {ws.catalog_root}")
    print()
    width = max((len(r["id"]) for r in rows), default=10)
    for r in rows:
        mark = "OK  " if r["resolved"] else "MISS"
        print(f"  [{mark}] {r['id']:<{width}}  {r['platform']:<6}  {r['root'] or '-'}")
    resolved = sum(1 for r in rows if r["resolved"])
    print()
    print(f"{resolved}/{len(rows)} resolved")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ws, pairs, _ = _load_selection(args)
    statuses = collect_status(ws, pairs)
    if args.json:
        print(json.dumps([s.to_dict() for s in statuses], indent=2))
        return 0 if all(s.resolved and not s.error for s in statuses) else 1
    print(f"RetComM Studio status  v{__version__}")
    for s in statuses:
        if not s.resolved:
            print(f"  [MISS] {s.title_id}: {s.error}")
            continue
        if s.error:
            print(f"  [FAIL] {s.title_id}: {s.error}")
            continue
        dirty = "dirty" if s.dirty else "clean"
        print(
            f"  [{s.platform}] {s.title_id}  {s.branch or '?'}  {dirty}  "
            f"ahead={s.ahead} behind={s.behind}"
        )
        print(f"         root={s.root}")
        for m in s.modules:
            mark = "ok" if m.present else "missing"
            nest = " nested" if getattr(m, "nested", False) else ""
            print(
                f"         · {m.path:<28} [{mark}]{nest} "
                f"branch={m.branch or '-'} sha={m.sha or '-'}"
            )
    failed = sum(1 for s in statuses if (not s.resolved) or s.error)
    return 1 if failed else 0


def _print_report(report, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
        return 1 if report.failed else 0
    print(f"Bulk {report.op}: {report.ok_count} ok, {report.failed} failed")
    for r in report.results:
        print(f"  [{'OK' if r.ok else 'FAIL'}] {r.title_id}: {r.message}")
        if r.detail:
            for line in str(r.detail).splitlines()[:5]:
                print(f"         {line}")
    return 1 if report.failed else 0


def cmd_ensure_modules(args: argparse.Namespace) -> int:
    ws, pairs, jobs = _load_selection(args)
    nested = not args.no_nested
    report = bulk_map(
        ws,
        pairs,
        op="ensure-modules",
        fn=lambda plugin, ctx: plugin.ensure_modules(ctx, nested=nested),
        dry_run=args.dry_run,
        jobs=jobs,
        continue_on_error=not args.fail_fast,
    )
    return _print_report(report, as_json=args.json)


def cmd_update_modules(args: argparse.Namespace) -> int:
    ws, pairs, jobs = _load_selection(args)
    report = bulk_map(
        ws,
        pairs,
        op="update-modules" + ("-nested" if args.nested else ""),
        fn=lambda plugin, ctx: plugin.update_modules(
            ctx, remote=args.remote, nested=args.nested
        ),
        dry_run=args.dry_run,
        jobs=jobs,
        continue_on_error=not args.fail_fast,
    )
    return _print_report(report, as_json=args.json)


def cmd_commit_nested(args: argparse.Namespace) -> int:
    ws, pairs, jobs = _load_selection(args)
    msg = args.message
    report = bulk_map(
        ws,
        pairs,
        op="commit-nested",
        fn=lambda plugin, ctx: plugin.commit_nested(ctx, msg),
        dry_run=args.dry_run,
        jobs=jobs,
        continue_on_error=not args.fail_fast,
    )
    return _print_report(report, as_json=args.json)


def cmd_pull(args: argparse.Namespace) -> int:
    ws, pairs, jobs = _load_selection(args)
    report = bulk_map(
        ws,
        pairs,
        op="pull",
        fn=lambda plugin, ctx: plugin.pull(ctx),
        dry_run=args.dry_run,
        jobs=jobs,
        continue_on_error=not args.fail_fast,
        pull_mode=getattr(args, "mode", None) or "ff-only",
        pull_dirty=getattr(args, "dirty", None) or "fail",
    )
    return _print_report(report, as_json=args.json)


def cmd_commit(args: argparse.Namespace) -> int:
    ws, pairs, jobs = _load_selection(args)
    msg = args.message
    report = bulk_map(
        ws,
        pairs,
        op="commit",
        fn=lambda plugin, ctx: plugin.commit(ctx, msg),
        dry_run=args.dry_run,
        jobs=jobs,
        continue_on_error=not args.fail_fast,
    )
    return _print_report(report, as_json=args.json)


def cmd_push(args: argparse.Namespace) -> int:
    ws, pairs, jobs = _load_selection(args)
    report = bulk_map(
        ws,
        pairs,
        op="push",
        fn=lambda plugin, ctx: plugin.push(ctx),
        dry_run=args.dry_run,
        jobs=jobs,
        continue_on_error=not args.fail_fast,
    )
    return _print_report(report, as_json=args.json)


def cmd_release(args: argparse.Namespace) -> int:
    ws, pairs, jobs = _load_selection(args)
    report = bulk_map(
        ws,
        pairs,
        op="release",
        fn=lambda plugin, ctx: plugin.release(
            ctx,
            version=args.version or "",
            bump=args.bump,
            publish=not args.no_publish,
            reuse_cached_emitters=not args.no_reuse_cached_emitters,
        ),
        dry_run=args.dry_run,
        jobs=jobs,
        continue_on_error=not args.fail_fast,
    )
    return _print_report(report, as_json=args.json)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="retcomm-studio",
        description=(
            "Catalog-backed bulk Git/GitHub ops across RetComM recomp titles "
            "(plugin API per platform)."
        ),
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List catalog titles + local resolution")
    _add_select(p_list)
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="Per-title git/module status")
    _add_select(p_status)
    p_status.set_defaults(func=cmd_status)

    p_ensure = sub.add_parser("ensure-modules", help="Ensure engine submodules exist")
    _add_select(p_ensure)
    p_ensure.add_argument(
        "--no-nested",
        action="store_true",
        help="Skip nested lib/recomp-net + lib/retcomm-rbengine",
    )
    p_ensure.set_defaults(func=cmd_ensure_modules)

    p_upd = sub.add_parser("update-modules", help="Update engine submodules")
    _add_select(p_upd)
    p_upd.add_argument(
        "--remote",
        action="store_true",
        help="Update to remote tracking tip (then commit gitlinks)",
    )
    p_upd.add_argument(
        "--nested",
        action="store_true",
        help="Update nested modules inside psxrecomp (recomp-net, rbengine)",
    )
    p_upd.set_defaults(func=cmd_update_modules)

    p_cn = sub.add_parser(
        "commit-nested",
        help="Commit inside each title's psxrecomp checkout",
    )
    _add_select(p_cn)
    p_cn.add_argument("-m", "--message", required=True)
    p_cn.set_defaults(func=cmd_commit_nested)

    p_pull = sub.add_parser("pull", help="git pull on each title (default: --mode ff-only)")
    _add_select(p_pull)
    p_pull.add_argument(
        "--mode",
        choices=("ff-only", "rebase", "merge", "reset"),
        default="ff-only",
        help="ff-only (default), rebase, merge, or reset (match origin)",
    )
    p_pull.add_argument(
        "--dirty",
        choices=("fail", "stash", "discard"),
        default="fail",
        help="If dirty: fail (default), stash, or discard. Ignored for --mode reset.",
    )
    p_pull.set_defaults(func=cmd_pull)

    p_commit = sub.add_parser("commit", help="git add -A && commit on each title")
    _add_select(p_commit)
    p_commit.add_argument("-m", "--message", required=True)
    p_commit.set_defaults(func=cmd_commit)

    p_push = sub.add_parser("push", help="git push -u origin HEAD on each title")
    _add_select(p_push)
    p_push.set_defaults(func=cmd_push)

    p_rel = sub.add_parser("release", help="Dispatch release.yml on each title")
    _add_select(p_rel)
    p_rel.add_argument("--version", default="", help="Empty = auto-bump")
    p_rel.add_argument("--bump", choices=("patch", "minor", "major"), default="patch")
    p_rel.add_argument("--no-publish", action="store_true")
    p_rel.add_argument("--no-reuse-cached-emitters", action="store_true")
    p_rel.set_defaults(func=cmd_release)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
