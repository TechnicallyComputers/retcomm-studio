"""Sync retcomm-catalog into the shared RetComM cache (same as the launcher).

Downloads ``catalog.zip`` from GitHub releases into
``~/.local/share/retcomm/catalog`` (Windows: ``%LOCALAPPDATA%\\retcomm\\catalog``).
Studio filters (Game repo dropdown + Bulk Catalog only) prefer this cache when valid.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .retcomm_paths import RetcommPaths, default_paths, ensure_dirs

ProgressFn = Callable[[str], None]

DEFAULT_CATALOG_SLUG = "TechnicallyComputers/retcomm-catalog"
DEFAULT_CATALOG_ASSET = "catalog.zip"
DEFAULT_CATALOG_URL = (
    f"https://github.com/{DEFAULT_CATALOG_SLUG}/releases/latest/download/{DEFAULT_CATALOG_ASSET}"
)
USER_AGENT = "RetComM-Studio-Catalog/1.0 (+https://github.com/TechnicallyComputers/retcomm-studio)"


@dataclass
class CatalogSyncResult:
    ok: bool = False
    skipped: bool = False
    message: str = ""
    release_tag: str = ""
    catalog_date: str = ""
    title_count: int = 0
    downloaded: bool = False


def catalog_state_path(paths: RetcommPaths | None = None) -> Path:
    p = paths or default_paths()
    return p.data_dir / "catalog-state.json"


def catalog_cache_valid(paths: RetcommPaths | None = None) -> bool:
    p = paths or default_paths()
    return (p.catalog_dir / "index.json").is_file() and (p.catalog_dir / "titles").is_dir()


def read_catalog_state(paths: RetcommPaths | None = None) -> dict:
    path = catalog_state_path(paths)
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def write_catalog_state(state: dict, paths: RetcommPaths | None = None) -> None:
    p = ensure_dirs(paths)
    path = catalog_state_path(p)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _count_titles(catalog_root: Path) -> int:
    titles = catalog_root / "titles"
    if not titles.is_dir():
        return 0
    return sum(1 for p in titles.glob("*.json") if p.is_file())


def _find_catalog_root(staging: Path) -> Path | None:
    if (staging / "index.json").is_file():
        return staging
    nested = staging / "catalog"
    if (nested / "index.json").is_file():
        return nested
    try:
        for path in staging.rglob("index.json"):
            return path.parent
    except OSError:
        pass
    return None


def _catalog_download_url() -> str:
    env = (os.environ.get("RETCOMM_CATALOG_URL") or "").strip()
    return env or DEFAULT_CATALOG_URL


def _catalog_github_slug() -> str:
    env = (os.environ.get("RETCOMM_CATALOG_GITHUB") or "").strip()
    return env or DEFAULT_CATALOG_SLUG


def _latest_catalog_tag(paths: RetcommPaths | None = None) -> str:
    """Resolve latest catalog release tag via github.com redirect (no API)."""
    from .updater import cached_latest_tag

    return cached_latest_tag(_catalog_github_slug(), paths=paths)


def sync_remote_catalog(
    paths: RetcommPaths | None = None,
    *,
    force: bool = False,
    on_progress: ProgressFn | None = None,
) -> CatalogSyncResult:
    """Download catalog.zip into the shared cache when missing or outdated."""
    from .updater import download_file, extract_archive

    p = ensure_dirs(paths)
    url = _catalog_download_url()
    state = read_catalog_state(p)
    remote_tag = ""

    def prog(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    try:
        prog(f"Checking {_catalog_github_slug()} catalog…")
        remote_tag = _latest_catalog_tag(p)
    except Exception as exc:
        if catalog_cache_valid(p) and not force:
            return CatalogSyncResult(
                ok=True,
                skipped=True,
                message=f"Catalog update check failed ({exc}); keeping local cache.",
                release_tag=str(state.get("release_tag") or ""),
                catalog_date=str(state.get("catalog_date") or ""),
                title_count=_count_titles(p.catalog_dir),
            )
        if not force and not catalog_cache_valid(p):
            # Still try the stable /latest/download URL without a tag.
            remote_tag = ""
        else:
            return CatalogSyncResult(ok=False, message=f"Catalog check failed: {exc}")

    local_tag = str(state.get("release_tag") or "").strip()
    if not force and catalog_cache_valid(p) and remote_tag and local_tag == remote_tag:
        return CatalogSyncResult(
            ok=True,
            skipped=True,
            message=f"Catalog up to date ({remote_tag}).",
            release_tag=remote_tag,
            catalog_date=str(state.get("catalog_date") or ""),
            title_count=_count_titles(p.catalog_dir),
        )

    work = p.data_dir / "catalog-sync"
    download = work / DEFAULT_CATALOG_ASSET
    staging = work / "staging"
    try:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True, exist_ok=True)

        prog(f"Downloading {DEFAULT_CATALOG_ASSET}…")
        download_file(url, download, on_progress=on_progress)
        prog("Extracting catalog…")
        extract_archive(download, staging)

        catalog_root = _find_catalog_root(staging)
        if catalog_root is None or not (catalog_root / "index.json").is_file():
            return CatalogSyncResult(ok=False, message="catalog archive missing index.json")

        title_count = _count_titles(catalog_root)
        cache = p.catalog_dir
        backup = p.data_dir / "catalog.old"
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        if cache.exists():
            try:
                cache.rename(backup)
            except OSError:
                shutil.rmtree(cache, ignore_errors=True)

        try:
            catalog_root.rename(cache)
        except OSError:
            cache.mkdir(parents=True, exist_ok=True)
            shutil.copytree(catalog_root, cache, dirs_exist_ok=True)

        shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)

        release_tag = remote_tag or local_tag
        # Prefer stamp from extracted index when present.
        catalog_date = ""
        try:
            idx = json.loads((cache / "index.json").read_text(encoding="utf-8"))
            if isinstance(idx, dict):
                catalog_date = str(idx.get("catalog_date") or idx.get("date") or "").strip()
                if not release_tag:
                    release_tag = str(idx.get("release_tag") or idx.get("tag") or "").strip()
        except (OSError, json.JSONDecodeError):
            pass

        synced_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_state = {
            "url": url,
            "github_repo": _catalog_github_slug(),
            "synced_at": synced_at,
            "synced_at_epoch": int(time.time()),
            "release_tag": release_tag,
            "catalog_date": catalog_date,
            "titles": title_count,
        }
        try:
            write_catalog_state(new_state, p)
        except OSError as exc:
            return CatalogSyncResult(
                ok=True,
                skipped=False,
                downloaded=True,
                message=f"Catalog updated but could not write state: {exc}",
                release_tag=release_tag,
                catalog_date=catalog_date,
                title_count=title_count,
            )

        bits = [f"{title_count} titles"]
        if release_tag:
            bits.append(release_tag)
        if catalog_date:
            bits.append(catalog_date)
        return CatalogSyncResult(
            ok=True,
            skipped=False,
            downloaded=True,
            message=f"Catalog updated ({', '.join(bits)}).",
            release_tag=release_tag,
            catalog_date=catalog_date,
            title_count=title_count,
        )
    except Exception as exc:
        return CatalogSyncResult(ok=False, message=f"Catalog sync failed: {exc}")


def maybe_auto_update_catalog(
    paths: RetcommPaths | None = None,
    *,
    on_progress: ProgressFn | None = None,
) -> CatalogSyncResult:
    """Always fetch when cache missing; otherwise download only if GitHub is newer."""
    p = paths or default_paths()
    if not catalog_cache_valid(p):
        return sync_remote_catalog(p, force=True, on_progress=on_progress)
    return sync_remote_catalog(p, force=False, on_progress=on_progress)
