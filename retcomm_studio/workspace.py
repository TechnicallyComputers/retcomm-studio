"""Workspace config: catalog path, checkout roots, explicit title → path map."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import CatalogTitle

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore


@dataclass
class Workspace:
    config_path: Path
    catalog_root: Path
    checkout_roots: list[Path] = field(default_factory=list)
    title_paths: dict[str, Path] = field(default_factory=dict)
    psxrecomp_toolkit: Path | None = None
    default_jobs: int = 4

    def resolve_title_root(self, title: CatalogTitle) -> Path | None:
        if title.id in self.title_paths:
            p = self.title_paths[title.id]
            return p if p.is_dir() else None

        candidates: list[str] = []
        if title.install_dir_name:
            candidates.append(title.install_dir_name)
        if title.github and "/" in title.github:
            candidates.append(title.github.rsplit("/", 1)[-1])
        # Also try id as folder name
        candidates.append(title.id)

        def _looks_like_game(hit: Path) -> bool:
            return hit.is_dir() and (
                (hit / ".git").exists()
                or (hit / "game.toml").is_file()
                or (hit / "CMakeLists.txt").is_file()
            )

        seen: set[str] = set()
        for name in candidates:
            if not name or name in seen:
                continue
            seen.add(name)
            for root in self.checkout_roots:
                hit = root / name
                if _looks_like_game(hit):
                    return hit.resolve()

        # Soft match: spacey / trailing-Recomp folders vs install_dir / github slug.
        try:
            from fill_tokens import repo_match_keys
        except ImportError:
            return None
        want: set[str] = set()
        for c in candidates:
            want.update(repo_match_keys(c))
        want.discard("")
        if not want:
            return None
        for root in self.checkout_roots:
            if not root.is_dir():
                continue
            try:
                children = list(root.iterdir())
            except OSError:
                continue
            for child in children:
                if repo_match_keys(child.name) & want and _looks_like_game(child):
                    return child.resolve()
        return None


def _as_path(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    else:
        p = p.resolve()
    return p


def load_workspace(path: Path | None = None) -> Workspace:
    """Load studio.toml from path, cwd, or parents."""
    cfg = _find_config(path)
    data = _read_toml(cfg)
    base = cfg.parent

    catalog = data.get("catalog")
    if not catalog:
        raise ValueError(f"{cfg}: missing required 'catalog' path")
    catalog_root = _as_path(base, str(catalog))
    # Prefer the shared RetComM catalog cache when Studio/Hub has synced it.
    env_cat = (os.environ.get("RETCOMM_CATALOG_DIR") or "").strip()
    if env_cat:
        env_path = Path(env_cat).expanduser().resolve()
        if (env_path / "index.json").is_file():
            catalog_root = env_path
    else:
        try:
            from project_studio.catalog_sync import catalog_cache_valid
            from project_studio.retcomm_paths import default_paths

            paths = default_paths()
            if catalog_cache_valid(paths):
                catalog_root = paths.catalog_dir.resolve()
        except Exception:
            pass

    roots_raw = data.get("checkout_roots") or data.get("roots") or []
    if isinstance(roots_raw, dict):
        roots_raw = roots_raw.get("checkouts") or roots_raw.get("paths") or []
    checkout_roots = [_as_path(base, str(r)) for r in roots_raw]
    if not checkout_roots:
        checkout_roots = [base.parent.resolve()]

    title_paths: dict[str, Path] = {}
    for tid, tpath in (data.get("titles") or {}).items():
        title_paths[str(tid)] = _as_path(base, str(tpath))

    toolkit = data.get("psxrecomp_toolkit") or data.get("psxrecomp")
    toolkit_path = _as_path(base, str(toolkit)) if toolkit else _guess_psx_toolkit(base)

    jobs = int(data.get("jobs") or data.get("default_jobs") or 4)
    return Workspace(
        config_path=cfg,
        catalog_root=catalog_root,
        checkout_roots=checkout_roots,
        title_paths=title_paths,
        psxrecomp_toolkit=toolkit_path,
        default_jobs=max(1, jobs),
    )


def _find_config(path: Path | None) -> Path:
    if path is not None:
        p = path.expanduser().resolve()
        if p.is_dir():
            p = p / "studio.toml"
        if not p.is_file():
            raise FileNotFoundError(f"studio.toml not found: {p}")
        return p

    cwd = Path.cwd().resolve()
    for folder in [cwd, *cwd.parents]:
        candidate = folder / "studio.toml"
        if candidate.is_file():
            return candidate
    # Default next to this package's repo root
    here = Path(__file__).resolve().parents[1] / "studio.toml"
    if here.is_file():
        return here
    raise FileNotFoundError(
        "studio.toml not found (cwd/parents or retcomm-studio/studio.toml). "
        "Copy studio.toml.example → studio.toml and edit paths."
    )


def _read_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:
        raise RuntimeError("Python 3.11+ required (tomllib)")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _guess_psx_toolkit(base: Path) -> Path | None:
    """Prefer the toolkit bundled in retcomm-studio, then a sibling psxrecomp."""
    candidates = [
        base / "tools" / "new_project_layout",
        Path(__file__).resolve().parents[1] / "tools" / "new_project_layout",
        base.parent / "psxrecomp" / "tools" / "new_project_layout",
        base / ".." / "psxrecomp" / "tools" / "new_project_layout",
    ]
    for c in candidates:
        p = c.resolve()
        if (p / "project_studio").is_dir():
            return p
    return None


def ensure_psx_toolkit_on_path(ws: Workspace) -> Path:
    toolkit = ws.psxrecomp_toolkit or _guess_psx_toolkit(ws.config_path.parent)
    if toolkit is None or not (toolkit / "project_studio").is_dir():
        raise FileNotFoundError(
            "Project Studio toolkit not found. Expected "
            "retcomm-studio/tools/new_project_layout (or set psxrecomp_toolkit "
            "in studio.toml)."
        )
    s = str(toolkit)
    if s not in sys.path:
        sys.path.insert(0, s)
    return toolkit
