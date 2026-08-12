"""Load retcomm-catalog index + title JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import CatalogTitle


def _dig(d: dict[str, Any], *keys: str, default: Any = "") -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default


def load_title(path: Path) -> CatalogTitle:
    raw = json.loads(path.read_text(encoding="utf-8"))
    github = (
        _dig(raw, "release", "github")
        or _dig(raw, "build", "source", "github")
        or ""
    )
    engine = _dig(raw, "build", "generate", "engine") or ""
    return CatalogTitle(
        id=str(raw.get("id") or path.stem),
        name=str(raw.get("name") or path.stem),
        platform=str(raw.get("platform") or "unknown"),
        kind=str(raw.get("kind") or "recomp"),
        homepage=str(raw.get("homepage") or ""),
        github=str(github),
        install_dir_name=str(raw.get("install_dir_name") or ""),
        engine=str(engine),
        build_ref=str(_dig(raw, "build", "source", "ref") or "main"),
        raw=raw,
    )


def load_catalog(catalog_root: Path) -> list[CatalogTitle]:
    catalog_root = catalog_root.expanduser().resolve()
    index_path = catalog_root / "index.json"
    titles_dir = catalog_root / "titles"
    if not index_path.is_file():
        raise FileNotFoundError(f"catalog index missing: {index_path}")
    if not titles_dir.is_dir():
        raise FileNotFoundError(f"catalog titles/ missing: {titles_dir}")

    index = json.loads(index_path.read_text(encoding="utf-8"))
    ids = list(index.get("titles") or [])
    out: list[CatalogTitle] = []
    missing: list[str] = []
    for tid in ids:
        path = titles_dir / f"{tid}.json"
        if not path.is_file():
            missing.append(tid)
            continue
        out.append(load_title(path))
    if missing:
        raise FileNotFoundError(
            "catalog titles listed in index.json but missing on disk: "
            + ", ".join(missing)
        )
    return out


def filter_titles(
    titles: list[CatalogTitle],
    *,
    platforms: set[str] | None = None,
    ids: set[str] | None = None,
    engines: set[str] | None = None,
) -> list[CatalogTitle]:
    out = titles
    if platforms:
        plat = {p.lower() for p in platforms}
        out = [t for t in out if t.platform.lower() in plat]
    if ids:
        want = {i.lower() for i in ids}
        out = [t for t in out if t.id.lower() in want]
    if engines:
        eng = {e.lower() for e in engines}
        out = [t for t in out if (t.engine or "").lower() in eng]
    return out
