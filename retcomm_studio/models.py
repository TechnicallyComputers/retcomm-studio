"""Shared data models for RetComM Studio."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CatalogTitle:
    id: str
    name: str
    platform: str
    kind: str = "recomp"
    homepage: str = ""
    github: str = ""
    install_dir_name: str = ""
    engine: str = ""
    build_ref: str = "main"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


@dataclass
class TitleContext:
    title: CatalogTitle
    root: Path
    dry_run: bool = False
    pull_mode: str = "ff-only"
    pull_dirty: str = "fail"


@dataclass
class OpResult:
    ok: bool
    message: str
    detail: str = ""
    title_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ModuleInfo:
    path: str
    branch: str = ""
    sha: str = ""
    present: bool = False
    url: str = ""
    nested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TitleStatus:
    title_id: str
    name: str
    platform: str
    root: str
    resolved: bool
    is_git: bool = False
    branch: str = ""
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    remote_url: str = ""
    gh_repo: str = ""
    modules: list[ModuleInfo] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["modules"] = [m.to_dict() for m in self.modules]
        return d


@dataclass
class BulkReport:
    op: str
    results: list[OpResult] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "ok": self.ok_count,
            "failed": self.failed,
            "results": [r.to_dict() for r in self.results],
        }
