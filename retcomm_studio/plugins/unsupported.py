"""Fallback plugin for platforms without a dedicated implementation yet."""

from __future__ import annotations

from ..models import OpResult, TitleContext, TitleStatus


class UnsupportedPlugin:
    def __init__(self, platform: str) -> None:
        self.platform = platform

    def _unsupported(self, ctx: TitleContext, op: str) -> OpResult:
        return OpResult(
            False,
            f"No plugin for platform={self.platform!r} (op={op})",
            title_id=ctx.title.id,
        )

    def status(self, ctx: TitleContext) -> TitleStatus:
        return TitleStatus(
            title_id=ctx.title.id,
            name=ctx.title.name,
            platform=ctx.title.platform,
            root=str(ctx.root),
            resolved=True,
            error=f"No plugin for platform={self.platform!r}",
        )

    def ensure_modules(self, ctx: TitleContext, *, nested: bool = True) -> list[OpResult]:
        return [self._unsupported(ctx, "ensure_modules")]

    def update_modules(
        self, ctx: TitleContext, *, remote: bool = False, nested: bool = False
    ) -> OpResult:
        return self._unsupported(ctx, "update_modules")

    def commit_nested(self, ctx: TitleContext, message: str) -> OpResult:
        return self._unsupported(ctx, "commit_nested")

    def pull(self, ctx: TitleContext) -> OpResult:
        return self._unsupported(ctx, "pull")

    def commit(self, ctx: TitleContext, message: str) -> OpResult:
        return self._unsupported(ctx, "commit")

    def push(self, ctx: TitleContext) -> OpResult:
        return self._unsupported(ctx, "push")

    def release(
        self,
        ctx: TitleContext,
        *,
        version: str = "",
        bump: str = "patch",
        publish: bool = True,
        reuse_cached_emitters: bool = True,
    ) -> OpResult:
        return self._unsupported(ctx, "release")
