"""Plugin protocol for platform/engine-specific title ops."""

from __future__ import annotations

from typing import Protocol

from ..models import OpResult, TitleContext, TitleStatus


class TitlePlugin(Protocol):
    """One plugin per platform (or engine)."""

    platform: str

    def status(self, ctx: TitleContext) -> TitleStatus: ...

    def ensure_modules(self, ctx: TitleContext, *, nested: bool = True) -> list[OpResult]: ...

    def update_modules(self, ctx: TitleContext, *, remote: bool = False, nested: bool = False) -> OpResult | list[OpResult]: ...

    def commit_nested(self, ctx: TitleContext, message: str) -> OpResult: ...

    def pull(self, ctx: TitleContext) -> OpResult: ...

    def commit(self, ctx: TitleContext, message: str) -> OpResult: ...

    def push(self, ctx: TitleContext) -> OpResult: ...

    def release(
        self,
        ctx: TitleContext,
        *,
        version: str = "",
        bump: str = "patch",
        publish: bool = True,
        reuse_cached_emitters: bool = True,
    ) -> OpResult: ...
