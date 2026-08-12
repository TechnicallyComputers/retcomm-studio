"""Bulk Git/GitHub ops across Project Studio's indexed game repos."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .gitops import (
    CmdResult,
    commit_all,
    commit_modules,
    pull,
    pull_modules,
    pull_psxrecomp,
    push,
    push_modules,
    push_psxrecomp,
    repo_status,
    switch_branch,
    switch_modules,
    switch_psxrecomp,
)
from .repo_index import RepoEntry, RepoIndex, load_index

# GitHub viewerPermission values that can push / manage the repo.
_CONTRIBUTOR_PERMS = frozenset({"ADMIN", "MAINTAIN", "WRITE"})


def _compact(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def _initials(s: str) -> str:
    """PascalCase / spaced initials: MastersOfTerasKasi → motk…"""
    out: list[str] = []
    prev_alnum = False
    for c in s:
        if not c.isalnum():
            prev_alnum = False
            continue
        if c.isupper() or not prev_alnum:
            out.append(c.lower())
        prev_alnum = True
    return "".join(out)


def _match_select(entry_label: str, entry_path: str, select: list[str]) -> bool:
    if not select:
        return True
    name = Path(entry_path).name
    # Prefer short names (basename / label), not full path — path subsequence
    # is too fuzzy (e.g. Tomba ⊂ …/Documents/…/Bomberman…).
    text_cands = (entry_label, name)
    compact_cands = tuple(_compact(c) for c in text_cands)
    initial_cands = tuple(_initials(c) for c in (entry_label, name, name.replace("-", " ")))
    for raw in select:
        s = raw.strip()
        if not s:
            continue
        sl = s.lower()
        sc = _compact(s)
        for c in text_cands:
            cl = c.lower()
            if sl == cl or sl in cl:
                return True
        for cc in compact_cands:
            if sc and (sc == cc or sc in cc):
                return True
        # Abbreviations: MotK → MastersOfTerasKasi (initials motkr…)
        if len(sc) >= 3:
            for ic in initial_cands:
                if ic.startswith(sc) or sc == ic:
                    return True
    return False


def indexed_repos(
    *,
    select: list[str] | None = None,
    index: RepoIndex | None = None,
) -> list[tuple[str, Path]]:
    """Return ``(display_label, resolved_path)`` for indexed repos.

    ``select`` filters by label, directory basename, or path substring
    (case-insensitive). Empty / None = all indexed repos.
    """
    idx = index if index is not None else load_index()
    want = [s for s in (select or []) if str(s).strip()]
    out: list[tuple[str, Path]] = []
    for entry in idx.repos:
        label = entry.label()
        if not _match_select(label, entry.path, want):
            continue
        try:
            root = entry.resolved()
        except OSError:
            continue
        if not root.is_dir():
            continue
        out.append((label, root))
    return out


def format_status_brief(root: Path) -> str:
    st = repo_status(root)
    if not st.is_git:
        return "not a git repo"
    dirty = "dirty" if st.dirty else "clean"
    bits = [st.branch or "?", dirty, f"a{st.ahead}/b{st.behind}"]
    if st.upstream:
        bits.insert(1, f"→{st.upstream}")
    return " ".join(bits)


def bulk_status(
    repos: list[tuple[str, Path]],
) -> list[CmdResult]:
    results: list[CmdResult] = []
    for label, root in repos:
        brief = format_status_brief(root)
        results.append(CmdResult(True, f"{label}: {brief}", str(root)))
    return results


def bulk_pull(
    repos: list[tuple[str, Path]],
    *,
    game: bool = True,
    modules: bool = False,
    psxrecomp: bool = False,
    nested: bool = False,
    mode: str = "ff-only",
    dirty: str = "fail",
    dry_run: bool = False,
) -> list[CmdResult]:
    """Pull selected targets for each repo. At least one target should be True."""
    if not (game or modules or psxrecomp or nested):
        return [CmdResult(False, "No pull targets (game/modules/psxrecomp/nested)")]
    results: list[CmdResult] = []
    for label, root in repos:
        if game:
            r = pull(root, mode=mode, dirty=dirty, dry_run=dry_run)
            results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
        if modules:
            for r in pull_modules(
                root, nested=False, mode=mode, dirty=dirty, dry_run=dry_run
            ):
                results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
        if psxrecomp:
            r = pull_psxrecomp(root, mode=mode, dirty=dirty, dry_run=dry_run)
            results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
        if nested:
            for r in pull_modules(
                root, nested=True, mode=mode, dirty=dirty, dry_run=dry_run
            ):
                results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
    return results


def bulk_push(
    repos: list[tuple[str, Path]],
    *,
    game: bool = True,
    modules: bool = False,
    psxrecomp: bool = False,
    nested: bool = False,
    dry_run: bool = False,
) -> list[CmdResult]:
    if not (game or modules or psxrecomp or nested):
        return [CmdResult(False, "No push targets (game/modules/psxrecomp/nested)")]
    results: list[CmdResult] = []
    for label, root in repos:
        if game:
            r = push(root, dry_run=dry_run)
            results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
        if modules:
            for r in push_modules(root, nested=False, dry_run=dry_run):
                results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
        if psxrecomp:
            r = push_psxrecomp(root, dry_run=dry_run)
            results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
        if nested:
            for r in push_modules(root, nested=True, dry_run=dry_run):
                results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
    return results


def bulk_commit(
    repos: list[tuple[str, Path]],
    message: str,
    *,
    game: bool = True,
    modules: bool = False,
    nested: bool = False,
    dry_run: bool = False,
) -> list[CmdResult]:
    message = (message or "").strip()
    if not message:
        return [CmdResult(False, "Commit message required")]
    if not (game or modules or nested):
        return [CmdResult(False, "No commit targets (game/modules/nested)")]
    results: list[CmdResult] = []
    for label, root in repos:
        if game:
            r = commit_all(root, message, dry_run=dry_run)
            results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
        if modules:
            for r in commit_modules(
                root, message, nested=False, dry_run=dry_run
            ):
                results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
        if nested:
            for r in commit_modules(
                root, message, nested=True, dry_run=dry_run
            ):
                results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
    return results


def bulk_switch(
    repos: list[tuple[str, Path]],
    *,
    game: bool = False,
    modules: bool = False,
    psxrecomp: bool = False,
    nested: bool = False,
    game_branch: str = "",
    psxrecomp_branch: str = "",
    recomp_ui_branch: str = "",
    recomp_net_branch: str = "",
    rbengine_branch: str = "",
    create: bool = False,
    set_tracking: bool = True,
    dry_run: bool = False,
) -> list[CmdResult]:
    """``git switch`` (+ optional .gitmodules tracking) across selected repos.

    Branch fields apply to the matching target flags. Empty branch strings are
    skipped (``switch_modules`` can still use existing .gitmodules tracking when
    a path is requested but its override is blank — pass overrides only when set).
    """
    if not (game or modules or psxrecomp or nested):
        return [
            CmdResult(
                False,
                "No switch targets (game/modules/psxrecomp/nested)",
            )
        ]
    game_branch = (game_branch or "").strip()
    psxrecomp_branch = (psxrecomp_branch or "").strip()
    recomp_ui_branch = (recomp_ui_branch or "").strip()
    recomp_net_branch = (recomp_net_branch or "").strip()
    rbengine_branch = (rbengine_branch or "").strip()

    if game and not game_branch:
        return [CmdResult(False, "Game root switch needs a game branch name")]
    if psxrecomp and not psxrecomp_branch and not modules:
        # modules path can supply psx branch; lone --psxrecomp needs a name
        return [CmdResult(False, "psxrecomp switch needs a branch name")]
    if modules and not (psxrecomp_branch or recomp_ui_branch):
        return [
            CmdResult(
                False,
                "Modules switch needs psxrecomp and/or recomp-ui branch",
            )
        ]
    if nested and not (recomp_net_branch or rbengine_branch):
        return [
            CmdResult(
                False,
                "Nested switch needs recomp-net and/or rbengine branch",
            )
        ]

    results: list[CmdResult] = []
    for label, root in repos:
        if game:
            r = switch_branch(
                root, game_branch, create=create, dry_run=dry_run
            )
            results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
        if modules:
            branch_by_path: dict[str, str] = {}
            if psxrecomp_branch:
                branch_by_path["psxrecomp"] = psxrecomp_branch
            if recomp_ui_branch:
                branch_by_path["recomp-ui"] = recomp_ui_branch
            paths = list(branch_by_path.keys()) or None
            for r in switch_modules(
                root,
                paths=paths,
                nested=False,
                branch_by_path=branch_by_path or None,
                create=create,
                set_tracking=set_tracking,
                dry_run=dry_run,
            ):
                results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
        elif psxrecomp:
            r = switch_psxrecomp(
                root, psxrecomp_branch, create=create, dry_run=dry_run
            )
            results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
            if set_tracking and r.ok and psxrecomp_branch:
                from .gitops import set_submodule_branch

                tr = set_submodule_branch(
                    root, "psxrecomp", psxrecomp_branch, dry_run=dry_run
                )
                results.append(
                    CmdResult(tr.ok, f"{label}: {tr.message}", tr.detail)
                )
        if nested:
            branch_by_path = {}
            if recomp_net_branch:
                branch_by_path["lib/recomp-net"] = recomp_net_branch
            if rbengine_branch:
                branch_by_path["lib/retcomm-rbengine"] = rbengine_branch
            paths = list(branch_by_path.keys()) or None
            for r in switch_modules(
                root,
                paths=paths,
                nested=True,
                branch_by_path=branch_by_path or None,
                create=create,
                set_tracking=set_tracking,
                dry_run=dry_run,
            ):
                results.append(CmdResult(r.ok, f"{label}: {r.message}", r.detail))
    return results


# ---------------------------------------------------------------------------
# Selection filters (catalog / GitHub contributor)
# ---------------------------------------------------------------------------


def find_studio_toml() -> Path | None:
    """Locate retcomm-studio ``studio.toml`` (catalog path + title map)."""
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "studio.toml",  # …/retcomm-studio/tools/…/project_studio
        Path.cwd() / "studio.toml",
    ]
    for folder in [Path.cwd().resolve(), *Path.cwd().resolve().parents]:
        candidates.append(folder / "studio.toml")
    seen: set[Path] = set()
    for p in candidates:
        try:
            p = p.resolve()
        except OSError:
            continue
        if p in seen:
            continue
        seen.add(p)
        if p.is_file():
            return p
    return None


def _read_studio_toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore
    if tomllib is not None:
        with path.open("rb") as f:
            return tomllib.load(f)
    # Minimal fallback: catalog = "…" and [titles] key = "path"
    text = path.read_text(encoding="utf-8", errors="replace")
    data: dict = {"titles": {}}
    m = re.search(r'^catalog\s*=\s*"([^"]+)"', text, re.M)
    if m:
        data["catalog"] = m.group(1)
    in_titles = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_titles = s == "[titles]"
            continue
        if not in_titles or "=" not in s or s.startswith("#"):
            continue
        key, _, val = s.partition("=")
        key = key.strip().strip('"').strip("'")
        val = val.strip().strip('"').strip("'")
        if key and val:
            data["titles"][key] = val
    return data


def resolve_catalog_root(studio_toml: Path | None = None) -> Path | None:
    cfg = studio_toml or find_studio_toml()
    if cfg is None:
        return None
    data = _read_studio_toml(cfg)
    raw = data.get("catalog")
    if not raw:
        return None
    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        p = (cfg.parent / p).resolve()
    else:
        p = p.resolve()
    return p if p.is_dir() else None


def _catalog_name_keys(catalog_root: Path) -> set[str]:
    """Folder / github short-name keys (lowercased) for every catalog title."""
    index_path = catalog_root / "index.json"
    titles_dir = catalog_root / "titles"
    if not index_path.is_file() or not titles_dir.is_dir():
        return set()
    try:
        ids = list(json.loads(index_path.read_text(encoding="utf-8")).get("titles") or [])
    except (OSError, json.JSONDecodeError):
        return set()
    keys: set[str] = set()
    for tid in ids:
        keys.add(str(tid).lower())
        path = titles_dir / f"{tid}.json"
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = str(raw.get("install_dir_name") or "").strip()
        if name:
            keys.add(name.lower())
        for nest in (
            ("release", "github"),
            ("build", "source", "github"),
        ):
            cur: object = raw
            ok = True
            for k in nest:
                if not isinstance(cur, dict) or k not in cur:
                    ok = False
                    break
                cur = cur[k]
            if ok and isinstance(cur, str) and "/" in cur:
                keys.add(cur.rsplit("/", 1)[-1].lower())
        homepage = str(raw.get("homepage") or "")
        if "github.com/" in homepage.lower():
            tail = homepage.rstrip("/").split("github.com/")[-1]
            if "/" in tail:
                keys.add(tail.split("/")[-1].lower())
    return keys


def _studio_title_paths(studio_toml: Path | None = None) -> set[Path]:
    cfg = studio_toml or find_studio_toml()
    if cfg is None:
        return set()
    data = _read_studio_toml(cfg)
    out: set[Path] = set()
    for _tid, tpath in (data.get("titles") or {}).items():
        p = Path(str(tpath)).expanduser()
        if not p.is_absolute():
            p = (cfg.parent / p).resolve()
        else:
            p = p.resolve()
        if p.is_dir():
            out.add(p)
    return out


def repo_has_catalog_entry(
    entry: RepoEntry,
    *,
    catalog_root: Path | None = None,
    studio_toml: Path | None = None,
) -> bool:
    """True if this indexed checkout maps to a retcomm-catalog title."""
    try:
        root = entry.resolved()
    except OSError:
        return False
    mapped = _studio_title_paths(studio_toml)
    if root in mapped:
        return True
    cat = catalog_root or resolve_catalog_root(studio_toml)
    if cat is None:
        return False
    keys = _catalog_name_keys(cat)
    if not keys:
        return False
    basename = root.name.lower()
    if basename in keys:
        return True
    name = (entry.name or "").strip().lower()
    if name and name in keys:
        return True
    return False


def viewer_can_contribute(root: Path) -> tuple[bool, str]:
    """Return (has_push_privilege, permission_or_error).

    Uses ``gh repo view --json viewerPermission``. Contributor = ADMIN /
    MAINTAIN / WRITE.
    """
    from .gitops import _run, _which_gh

    root = root.expanduser().resolve()
    if not _which_gh():
        return False, "gh CLI missing"
    code, out, err = _run(
        ["gh", "repo", "view", "--json", "viewerPermission", "-q", ".viewerPermission"],
        root,
    )
    if code != 0:
        return False, (err or out or "gh repo view failed").strip()
    perm = (out or "").strip().upper()
    if not perm:
        return False, "empty viewerPermission"
    return perm in _CONTRIBUTOR_PERMS, perm


def filter_indexed_catalog(
    index: RepoIndex | None = None,
) -> tuple[list[RepoEntry], str]:
    """Indexed repos that have a catalog entry. Second value is a status note."""
    idx = index if index is not None else load_index()
    studio = find_studio_toml()
    cat = resolve_catalog_root(studio)
    if cat is None and not _studio_title_paths(studio):
        return [], "No studio.toml catalog path found"
    hits = [
        e
        for e in idx.repos
        if repo_has_catalog_entry(e, catalog_root=cat, studio_toml=studio)
    ]
    note = f"catalog={cat}" if cat else "studio.toml [titles] only"
    return hits, note


def filter_indexed_contributors(
    index: RepoIndex | None = None,
) -> tuple[list[RepoEntry], list[str]]:
    """Indexed repos where the current GitHub user has WRITE+ privilege.

    Returns (matches, log_lines).
    """
    idx = index if index is not None else load_index()
    hits: list[RepoEntry] = []
    logs: list[str] = []
    for entry in idx.repos:
        try:
            root = entry.resolved()
        except OSError:
            logs.append(f"{entry.label()}: bad path")
            continue
        if not root.is_dir():
            logs.append(f"{entry.label()}: missing dir")
            continue
        ok, detail = viewer_can_contribute(root)
        logs.append(f"{entry.label()}: {detail}")
        if ok:
            hits.append(entry)
    return hits, logs


def filter_indexed_catalog_contributors(
    index: RepoIndex | None = None,
) -> tuple[list[RepoEntry], str, list[str]]:
    """Indexed repos that are both catalog-backed and WRITE+ for the viewer.

    Skips ``gh`` probes for non-catalog entries. Returns
    ``(matches, catalog_note, log_lines)``.
    """
    catalog_hits, note = filter_indexed_catalog(index)
    if not catalog_hits:
        return [], note, [f"catalog filter empty ({note})"]
    hits: list[RepoEntry] = []
    logs: list[str] = [f"checking {len(catalog_hits)} catalog repo(s) ({note})"]
    for entry in catalog_hits:
        try:
            root = entry.resolved()
        except OSError:
            logs.append(f"{entry.label()}: bad path")
            continue
        if not root.is_dir():
            logs.append(f"{entry.label()}: missing dir")
            continue
        ok, detail = viewer_can_contribute(root)
        logs.append(f"{entry.label()}: {detail}")
        if ok:
            hits.append(entry)
    return hits, note, logs
