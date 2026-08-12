"""Bulk fan-out across catalog titles."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from .catalog import filter_titles, load_catalog
from .models import BulkReport, CatalogTitle, OpResult, TitleContext, TitleStatus
from .plugins import get_plugin
from .workspace import Workspace


def select_titles(
    ws: Workspace,
    *,
    platforms: Sequence[str] | None = None,
    title_ids: Sequence[str] | None = None,
    engines: Sequence[str] | None = None,
    only_resolved: bool = False,
) -> list[tuple[CatalogTitle, Path | None]]:
    titles = load_catalog(ws.catalog_root)
    titles = filter_titles(
        titles,
        platforms=set(platforms) if platforms else None,
        ids=set(title_ids) if title_ids else None,
        engines=set(engines) if engines else None,
    )
    pairs: list[tuple[CatalogTitle, Path | None]] = []
    for t in titles:
        root = ws.resolve_title_root(t)
        if only_resolved and root is None:
            continue
        pairs.append((t, root))
    return pairs


def _ctx(
    title: CatalogTitle,
    root: Path,
    dry_run: bool,
    *,
    pull_mode: str = "ff-only",
    pull_dirty: str = "fail",
) -> TitleContext:
    return TitleContext(
        title=title,
        root=root,
        dry_run=dry_run,
        pull_mode=pull_mode,
        pull_dirty=pull_dirty,
    )


def collect_status(
    ws: Workspace,
    pairs: Iterable[tuple[CatalogTitle, Path | None]],
) -> list[TitleStatus]:
    out: list[TitleStatus] = []
    for title, root in pairs:
        if root is None:
            out.append(
                TitleStatus(
                    title_id=title.id,
                    name=title.name,
                    platform=title.platform,
                    root="",
                    resolved=False,
                    error="checkout not found (map in studio.toml [titles] or checkout_roots)",
                )
            )
            continue
        plugin = get_plugin(ws, title.platform)
        out.append(plugin.status(_ctx(title, root, False)))
    return out


def _run_one(
    ws: Workspace,
    title: CatalogTitle,
    root: Path | None,
    dry_run: bool,
    fn: Callable,
    *,
    pull_mode: str = "ff-only",
    pull_dirty: str = "fail",
) -> list[OpResult]:
    if root is None:
        return [
            OpResult(
                False,
                "checkout not found",
                title_id=title.id,
            )
        ]
    plugin = get_plugin(ws, title.platform)
    ctx = _ctx(
        title,
        root,
        dry_run,
        pull_mode=pull_mode,
        pull_dirty=pull_dirty,
    )
    result = fn(plugin, ctx)
    if isinstance(result, list):
        for r in result:
            r.title_id = r.title_id or title.id
        return result
    result.title_id = result.title_id or title.id
    return [result]


def bulk_map(
    ws: Workspace,
    pairs: Sequence[tuple[CatalogTitle, Path | None]],
    *,
    op: str,
    fn: Callable,
    dry_run: bool = False,
    jobs: int = 1,
    continue_on_error: bool = True,
    pull_mode: str = "ff-only",
    pull_dirty: str = "fail",
) -> BulkReport:
    report = BulkReport(op=op)
    jobs = max(1, jobs)

    if jobs == 1:
        for title, root in pairs:
            results = _run_one(
                ws,
                title,
                root,
                dry_run,
                fn,
                pull_mode=pull_mode,
                pull_dirty=pull_dirty,
            )
            report.results.extend(results)
            if not continue_on_error and any(not r.ok for r in results):
                break
        return report

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = {
            pool.submit(
                _run_one,
                ws,
                title,
                root,
                dry_run,
                fn,
                pull_mode=pull_mode,
                pull_dirty=pull_dirty,
            ): title.id
            for title, root in pairs
        }
        for fut in as_completed(futs):
            try:
                report.results.extend(fut.result())
            except Exception as exc:  # noqa: BLE001 — surface per-title failure
                report.results.append(
                    OpResult(False, f"exception: {exc}", title_id=futs[fut])
                )
    # Stable-ish order by title id
    report.results.sort(key=lambda r: (r.title_id, r.message))
    return report
