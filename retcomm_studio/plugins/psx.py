"""PSX plugin — delegates to psxrecomp Project Studio gitops."""

from __future__ import annotations

from ..models import ModuleInfo, OpResult, TitleContext, TitleStatus
from ..workspace import Workspace, ensure_psx_toolkit_on_path


class PsxPlugin:
    platform = "psx"

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _gitops(self):
        ensure_psx_toolkit_on_path(self.workspace)
        from project_studio import gitops  # type: ignore

        return gitops

    def status(self, ctx: TitleContext) -> TitleStatus:
        gitops = self._gitops()
        st = gitops.repo_status(ctx.root)
        modules = [
            ModuleInfo(
                path=s.path,
                branch=s.branch,
                sha=s.sha,
                present=s.present,
                url=s.url,
                nested=False,
            )
            for s in st.submodules
        ]
        for s in st.nested_submodules:
            modules.append(
                ModuleInfo(
                    path=f"psxrecomp/{s.path}",
                    branch=s.branch,
                    sha=s.sha,
                    present=s.present,
                    url=s.url,
                    nested=True,
                )
            )
        return TitleStatus(
            title_id=ctx.title.id,
            name=ctx.title.name,
            platform=ctx.title.platform,
            root=str(ctx.root),
            resolved=True,
            is_git=st.is_git,
            branch=st.branch,
            dirty=st.dirty,
            ahead=st.ahead,
            behind=st.behind,
            remote_url=st.remote_url,
            gh_repo=st.gh_repo,
            modules=modules,
            notes=list(st.notes),
        )

    def ensure_modules(self, ctx: TitleContext, *, nested: bool = True) -> list[OpResult]:
        gitops = self._gitops()
        results = gitops.ensure_known_submodules(ctx.root, dry_run=ctx.dry_run)
        out = [
            OpResult(ok=r.ok, message=r.message, detail=r.detail, title_id=ctx.title.id)
            for r in results
        ]
        if nested:
            for r in gitops.ensure_nested_modules(ctx.root, dry_run=ctx.dry_run):
                out.append(
                    OpResult(
                        ok=r.ok,
                        message=r.message,
                        detail=r.detail,
                        title_id=ctx.title.id,
                    )
                )
        return out

    def update_modules(
        self,
        ctx: TitleContext,
        *,
        remote: bool = False,
        nested: bool = False,
    ) -> list[OpResult]:
        gitops = self._gitops()
        results: list[OpResult] = []
        if nested:
            r = gitops.update_nested_modules(
                ctx.root, remote=remote, stage=True, dry_run=ctx.dry_run
            )
            results.append(
                OpResult(ok=r.ok, message=r.message, detail=r.detail, title_id=ctx.title.id)
            )
        else:
            r = gitops.update_submodules(
                ctx.root,
                paths=["psxrecomp", "recomp-ui"],
                remote=remote,
                dry_run=ctx.dry_run,
            )
            results.append(
                OpResult(ok=r.ok, message=r.message, detail=r.detail, title_id=ctx.title.id)
            )
        return results

    def commit_nested(self, ctx: TitleContext, message: str) -> OpResult:
        gitops = self._gitops()
        r = gitops.commit_nested(ctx.root, message, dry_run=ctx.dry_run)
        return OpResult(ok=r.ok, message=r.message, detail=r.detail, title_id=ctx.title.id)

    def pull(self, ctx: TitleContext) -> OpResult:
        gitops = self._gitops()
        r = gitops.pull(
            ctx.root,
            mode=getattr(ctx, "pull_mode", "ff-only") or "ff-only",
            dirty=getattr(ctx, "pull_dirty", "fail") or "fail",
            dry_run=ctx.dry_run,
        )
        return OpResult(ok=r.ok, message=r.message, detail=r.detail, title_id=ctx.title.id)

    def commit(self, ctx: TitleContext, message: str) -> OpResult:
        gitops = self._gitops()
        r = gitops.commit_all(ctx.root, message, dry_run=ctx.dry_run)
        return OpResult(ok=r.ok, message=r.message, detail=r.detail, title_id=ctx.title.id)

    def push(self, ctx: TitleContext) -> OpResult:
        gitops = self._gitops()
        r = gitops.push(ctx.root, dry_run=ctx.dry_run)
        return OpResult(ok=r.ok, message=r.message, detail=r.detail, title_id=ctx.title.id)

    def release(
        self,
        ctx: TitleContext,
        *,
        version: str = "",
        bump: str = "patch",
        publish: bool = True,
        reuse_cached_emitters: bool = True,
    ) -> OpResult:
        gitops = self._gitops()
        r = gitops.run_release_workflow(
            ctx.root,
            version=version,
            bump=bump,
            publish=publish,
            reuse_cached_emitters=reuse_cached_emitters,
            dry_run=ctx.dry_run,
        )
        return OpResult(ok=r.ok, message=r.message, detail=r.detail, title_id=ctx.title.id)


def supports_platform(platform: str) -> bool:
    return platform.lower() in {"psx", "ps1", "ps"}
