"""Persistent indexed list of local game repos for Project Studio."""

from __future__ import annotations

import json
import os
import re
import shlex
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import platforms

if TYPE_CHECKING:
    from collections.abc import Sequence

_TOOLKIT = Path(__file__).resolve().parent.parent
# Kept for callers that predate the platform split; new code asks
# default_index_path() so a SNES session never writes the PSX index.
DEFAULT_INDEX_PATH = _TOOLKIT / platforms.PSX.repo_index_file


def default_index_path(platform: str | None = None) -> Path:
    """Index file for `platform` (default: this process's platform).

    One file per console rather than one file with a platform column: the two
    lists are edited by different sessions, and a shared file would have every
    SNES add rewrite the PSX list it never read.
    """
    profile = platforms.get(platform) if platform else platforms.current()
    return _TOOLKIT / profile.repo_index_file


@dataclass
class RepoEntry:
    path: str
    name: str = ""
    cue: str = ""  # absolute path to Redump .cue when known
    # Per-project Build tab state (exe / dir / launch) — keyed by repo path so
    # switching projects never leaks another title's binary.
    build: dict | None = None

    def resolved(self) -> Path:
        return Path(self.path).expanduser().resolve()

    def display(self) -> str:
        name = (self.name or "").strip() or Path(self.path).name
        return name

    def label(self) -> str:
        """Unique-ish label for dropdowns (name · basename if needed)."""
        p = Path(self.path)
        name = self.display()
        if name != p.name:
            return f"{name}  ({p.name})"
        return name

    def cue_path(self) -> Path | None:
        raw = (self.cue or "").strip()
        if not raw:
            return None
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.resolved() / p
        try:
            p = p.resolve()
        except OSError:
            return p
        return p if p.is_file() else p

    def build_settings(self) -> dict:
        return dict(self.build) if isinstance(self.build, dict) else {}


@dataclass
class RepoIndex:
    repos: list[RepoEntry]
    last: str = ""
    path: Path = field(default_factory=default_index_path)
    log_height: int = 160  # Studio activity-log pane height (px)
    catalog_only: bool = False  # Filter Game-repo dropdown to catalog titles
    bulk_jobs: int = 2  # Parallel workers for Bulk tab (1–4)

    def to_dict(self) -> dict:
        repos_out: list[dict] = []
        for r in self.repos:
            d = asdict(r)
            if not d.get("build"):
                d.pop("build", None)
            if not d.get("cue"):
                # Keep empty cue for compatibility with existing index files.
                pass
            repos_out.append(d)
        h = int(self.log_height) if self.log_height else 160
        if h < 100:
            h = 100
        if h > 800:
            h = 800
        jobs = int(self.bulk_jobs) if self.bulk_jobs else 2
        if jobs < 1:
            jobs = 1
        if jobs > 4:
            jobs = 4
        return {
            "last": self.last,
            "log_height": h,
            "catalog_only": bool(self.catalog_only),
            "bulk_jobs": jobs,
            "repos": repos_out,
        }

    def find(self, root: Path | str) -> RepoEntry | None:
        try:
            key = str(Path(str(root)).expanduser().resolve())
        except OSError:
            key = str(root).strip()
        for entry in self.repos:
            if entry.path == key:
                return entry
        return None


def _toml_game_field(root: Path, field: str) -> str:
    gt = root / "game.toml"
    if not gt.is_file():
        return ""
    try:
        text = gt.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    in_game = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_game = s == "[game]"
            continue
        if not in_game or "=" not in s or s.startswith("#"):
            continue
        key, _, val = s.partition("=")
        if key.strip() != field:
            continue
        return val.strip().strip('"').strip("'")
    return ""


def _game_toml_name(root: Path) -> str:
    return _toml_game_field(root, "name") or root.name


def discover_cue(root: Path) -> str:
    """Best-effort .cue path from game.toml or common disc/ layouts."""
    root = root.expanduser().resolve()
    rel = _toml_game_field(root, "disc")
    if rel:
        cand = Path(rel)
        if not cand.is_absolute():
            cand = root / cand
        try:
            cand = cand.resolve()
        except OSError:
            pass
        if cand.is_file():
            return str(cand)
    # Fallbacks: disc/*.cue (prefer name matching game.toml cue_name)
    cue_name = ""
    gt = root / "game.toml"
    if gt.is_file():
        try:
            text = gt.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        in_prep = False
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                in_prep = s == "[prepare_disc]"
                continue
            if not in_prep or "=" not in s or s.startswith("#"):
                continue
            key, _, val = s.partition("=")
            if key.strip() == "cue_name":
                cue_name = val.strip().strip('"').strip("'")
                break
    disc_dir = root / "disc"
    if disc_dir.is_dir():
        if cue_name:
            named = disc_dir / cue_name
            if named.is_file():
                return str(named.resolve())
        cues = sorted(disc_dir.glob("*.cue"))
        if len(cues) == 1:
            return str(cues[0].resolve())
        if cues and cue_name:
            for c in cues:
                if c.name == cue_name:
                    return str(c.resolve())
    return ""


ROM_EXTS: tuple[str, ...] = ("*.sfc", "*.smc")
ROM_DIRS_ENV = "RETCOMM_SNES_ROM_DIRS"
# A headered SNES dump tops out well under this; the cap only exists so a
# stray same-named file cannot cost a multi-gigabyte read to reject.
_MAX_ROM_BYTES = 16 * 1024 * 1024

# `for cand in "A.sfc" "B.sfc"; do` — the only place a scaffolded repo records
# what its ROM is *called*. The wizard writes it; nothing writes where it lives.
_REGEN_CANDS_RE = re.compile(r"^\s*for\s+cand\s+in\s+(.+?);\s*do\s*$", re.MULTILINE)
_REGEN_CRC32_RE = re.compile(
    r'EXPECTED_CRC32="?\$\{SNESRECOMP_EXPECTED_CRC32:-([0-9a-fA-F]+)'
)


def _regen_text(root: Path) -> str:
    regen = root / "tools" / "regen.sh"
    if not regen.is_file():
        return ""
    try:
        return regen.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def regen_rom_names(root: Path) -> list[str]:
    """ROM filenames ``tools/regen.sh`` will accept, in its own order."""
    m = _REGEN_CANDS_RE.search(_regen_text(root))
    if not m:
        return []
    try:
        names = shlex.split(m.group(1))
    except ValueError:
        return []
    out: list[str] = []
    for n in names:
        n = n.strip()
        # Only literals: an unexpanded $VAR names no file, and guessing at one
        # is exactly the habit this module refuses.
        if n and "$" not in n and n not in out:
            out.append(n)
    return out


def regen_expected_crc32(root: Path) -> str:
    m = _REGEN_CRC32_RE.search(_regen_text(root))
    return m.group(1).lower() if m else ""


def _crc32_of(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_ROM_BYTES:
            return ""
        crc = 0
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                crc = zlib.crc32(chunk, crc)
    except OSError:
        return ""
    return f"{crc & 0xFFFFFFFF:08x}"


def rom_library_dirs() -> list[Path]:
    """Directories the user keeps dumps in (``RETCOMM_SNES_ROM_DIRS``)."""
    raw = os.environ.get(ROM_DIRS_ENV) or ""
    out: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        try:
            d = Path(part).expanduser().resolve()
        except OSError:
            continue
        if d.is_dir() and d not in out:
            out.append(d)
    return out


def discover_rom(root: Path, extra_dirs: "Sequence[Path | str]" = ()) -> str:
    """Best-effort SNES ROM path for a title repo.

    The ROM is never committed (snesrecomp's scaffold gitignores *.sfc/*.smc)
    and the wizard never records where the copy it was run against lives, so
    the repo alone is usually not enough. Three sources, in falling order of
    how directly they were stated:

      1. ``SNESRECOMP_ROM`` — the same variable ``regen.sh`` itself honours.
      2. A dump parked in the working tree (root, ``rom/``, ``roms/``).
      3. A file named by ``tools/regen.sh`` sitting in a known ROM directory —
         ``RETCOMM_SNES_ROM_DIRS`` or a directory another indexed title's ROM
         was already found in.

    Source 3 is only accepted when its CRC32 matches the digest this port was
    pinned to. A matching name in a library folder is a guess; a matching
    digest is the ROM. Finding nothing stays a normal outcome, not a failure.
    """
    root = root.expanduser().resolve()

    env = (os.environ.get("SNESRECOMP_ROM") or "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            try:
                return str(p.resolve())
            except OSError:
                return str(p)

    for sub in (root, root / "rom", root / "roms"):
        if not sub.is_dir():
            continue
        hits = sorted(
            [p for ext in ROM_EXTS for p in sub.glob(ext)],
            key=lambda p: p.name.lower(),
        )
        if hits:
            return str(hits[0].resolve())

    names = regen_rom_names(root)
    if not names:
        return ""
    want = regen_expected_crc32(root)
    seen: set[Path] = set()
    dirs: list[Path] = []
    for d in list(extra_dirs) + list(rom_library_dirs()):
        try:
            dp = Path(str(d)).expanduser().resolve()
        except OSError:
            continue
        if dp in seen or not dp.is_dir():
            continue
        seen.add(dp)
        dirs.append(dp)
    for d in dirs:
        for name in names:
            cand = d / name
            if not cand.is_file():
                continue
            # No pinned digest means nothing here can prove the match, and a
            # wrong ROM regenerates wrong C. Name alone is not enough.
            if not want or _crc32_of(cand) != want:
                continue
            return str(cand)
    return ""


def discover_image(root: Path, extra_dirs: "Sequence[Path | str]" = ()) -> str:
    """Platform-appropriate game image: .cue for PSX, ROM for SNES."""
    if platforms.current().image_kind == "rom":
        return discover_rom(root, extra_dirs)
    return discover_cue(root)


def looks_like_game_repo(root: Path) -> bool:
    """Does `root` look like a port for this process's platform?

    Deliberately platform-scoped: adding a PSX repo to the SNES index would
    hand every later Bulk/Build op a tree whose framework it cannot find, and
    the failure would surface as a confusing git error rather than "wrong
    list".
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        return False
    profile = platforms.current()
    if profile.key == "snes":
        if not (root / "CMakeLists.txt").is_file():
            return False
        # A SNES port has no game.toml. Its identity is the framework checkout
        # plus the per-title analysis config the recompiler consumes.
        if (root / profile.framework).exists():
            return True
        recomp = root / "recomp"
        if recomp.is_dir() and (
            (recomp / "symbols.toml").is_file() or any(recomp.glob("bank*.cfg"))
        ):
            return True
        return False
    if (root / "game.toml").is_file():
        return True
    if (root / "CMakeLists.txt").is_file() and (
        (root / profile.framework).exists() or (root / "runtime").exists()
    ):
        return True
    return False


def load_index(path: Path | None = None) -> RepoIndex:
    path = path or default_index_path()
    if not path.is_file():
        return RepoIndex(
            repos=[], last="", path=path, log_height=160, catalog_only=False, bulk_jobs=2
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return RepoIndex(repos=[], last="", path=path, log_height=160)
    repos: list[RepoEntry] = []
    seen: set[str] = set()
    for raw in data.get("repos") or []:
        if isinstance(raw, str):
            p = raw
            name = ""
            cue = ""
            build = None
        elif isinstance(raw, dict):
            p = str(raw.get("path") or "").strip()
            name = str(raw.get("name") or "").strip()
            cue = str(raw.get("cue") or raw.get("disc") or "").strip()
            raw_build = raw.get("build")
            build = dict(raw_build) if isinstance(raw_build, dict) else None
        else:
            continue
        if not p:
            continue
        try:
            key = str(Path(p).expanduser().resolve())
        except OSError:
            key = p
        if key in seen:
            continue
        seen.add(key)
        root_p = Path(key)
        if not name:
            try:
                name = _game_toml_name(root_p) if root_p.is_dir() else Path(p).name
            except OSError:
                name = Path(p).name
        if cue:
            try:
                cue_p = Path(cue).expanduser()
                if not cue_p.is_absolute() and root_p.is_dir():
                    cue_p = root_p / cue_p
                cue = str(cue_p.resolve())
            except OSError:
                pass
        repos.append(RepoEntry(path=key, name=name, cue=cue, build=build))
    # Discovery runs in a second pass so every image the index already states
    # can serve as a search directory for the entries that state none — one
    # title pointed at your ROM folder is enough to find the rest.
    known_dirs: list[Path] = []
    for entry in repos:
        if not entry.cue:
            continue
        d = Path(entry.cue).parent
        if d not in known_dirs:
            known_dirs.append(d)
    for entry in repos:
        if entry.cue:
            continue
        root_p = Path(entry.path)
        if root_p.is_dir():
            entry.cue = discover_image(root_p, known_dirs)
    last = str(data.get("last") or "").strip()
    if last:
        try:
            last = str(Path(last).expanduser().resolve())
        except OSError:
            pass
    log_height = 160
    try:
        log_height = int(data.get("log_height") or 160)
    except (TypeError, ValueError):
        log_height = 160
    if log_height < 100:
        log_height = 100
    if log_height > 800:
        log_height = 800
    catalog_only = bool(data.get("catalog_only"))
    bulk_jobs = 2
    try:
        bulk_jobs = int(data.get("bulk_jobs") or 2)
    except (TypeError, ValueError):
        bulk_jobs = 2
    if bulk_jobs < 1:
        bulk_jobs = 1
    if bulk_jobs > 4:
        bulk_jobs = 4
    return RepoIndex(
        repos=repos,
        last=last,
        path=path,
        log_height=log_height,
        catalog_only=catalog_only,
        bulk_jobs=bulk_jobs,
    )


def save_index(index: RepoIndex) -> None:
    path = index.path or default_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(index.to_dict(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def add_repo(
    index: RepoIndex,
    root: Path,
    *,
    name: str = "",
    cue: str = "",
) -> RepoEntry:
    root = root.expanduser().resolve()
    key = str(root)
    cue_s = ""
    if cue:
        try:
            cue_s = str(Path(cue).expanduser().resolve())
        except OSError:
            cue_s = str(cue).strip()
    else:
        # discover_image, not discover_cue: a SNES add that fell through to the
        # PSX-only probe recorded nothing and looked like "no ROM exists".
        cue_s = discover_image(
            root, [Path(e.cue).parent for e in index.repos if e.cue]
        )
    for existing in index.repos:
        if existing.path == key:
            if name and name != existing.name:
                existing.name = name
            if cue_s and not existing.cue:
                existing.cue = cue_s
            elif cue and cue_s:
                existing.cue = cue_s
            index.last = key
            save_index(index)
            return existing
    entry = RepoEntry(
        path=key,
        name=(name or _game_toml_name(root)),
        cue=cue_s,
    )
    index.repos.append(entry)
    index.repos.sort(key=lambda e: e.display().lower())
    index.last = key
    save_index(index)
    return entry


def set_repo_cue(index: RepoIndex, root: Path | str, cue: Path | str) -> RepoEntry | None:
    """Store / update the .cue path for an indexed repo."""
    entry = index.find(root)
    if entry is None:
        return None
    cue_p = Path(str(cue)).expanduser()
    try:
        cue_p = cue_p.resolve()
    except OSError:
        pass
    entry.cue = str(cue_p)
    save_index(index)
    return entry


def clear_repo_cue(index: RepoIndex, root: Path | str) -> bool:
    entry = index.find(root)
    if entry is None:
        return False
    if not entry.cue:
        return False
    entry.cue = ""
    save_index(index)
    return True


def set_repo_build(
    index: RepoIndex,
    root: Path | str,
    settings: dict | None,
) -> RepoEntry | None:
    """Persist Build-tab settings for one indexed repo (or clear with None/{})."""
    entry = index.find(root)
    if entry is None:
        return None
    if not settings:
        entry.build = None
    else:
        clean: dict = {}
        for key, val in settings.items():
            if val is None:
                continue
            if isinstance(val, str) and not val.strip():
                continue
            clean[str(key)] = val.strip() if isinstance(val, str) else val
        entry.build = clean or None
    save_index(index)
    return entry


def get_repo_build(index: RepoIndex, root: Path | str) -> dict:
    entry = index.find(root)
    if entry is None:
        return {}
    return entry.build_settings()


def remove_repo(index: RepoIndex, root: Path | str) -> bool:
    try:
        key = str(Path(str(root)).expanduser().resolve())
    except OSError:
        key = str(root).strip()
    before = len(index.repos)
    index.repos = [r for r in index.repos if r.path != key]
    if index.last == key:
        index.last = index.repos[0].path if index.repos else ""
    if len(index.repos) != before:
        save_index(index)
        return True
    return False


def set_last(index: RepoIndex, root: Path | str) -> None:
    try:
        key = str(Path(str(root)).expanduser().resolve())
    except OSError:
        key = str(root).strip()
    if any(r.path == key for r in index.repos):
        index.last = key
        save_index(index)


def labels_for_repos(repos: list[RepoEntry]) -> list[str]:
    """Build unique dropdown labels; disambiguate duplicate names with parent."""
    counts: dict[str, int] = {}
    for r in repos:
        counts[r.display()] = counts.get(r.display(), 0) + 1
    labels: list[str] = []
    for r in repos:
        base = r.display()
        if counts[base] > 1:
            parent = Path(r.path).parent.name
            labels.append(f"{base}  [{parent}]")
        else:
            labels.append(base)
    seen: dict[str, int] = {}
    out: list[str] = []
    for lab in labels:
        if lab not in seen:
            seen[lab] = 0
            out.append(lab)
            continue
        seen[lab] += 1
        out.append(f"{lab}  #{seen[lab]+1}")
    return out


def labels_for(index: RepoIndex) -> list[str]:
    return labels_for_repos(index.repos)


def path_for_label(
    index: RepoIndex,
    label: str,
    *,
    repos: list[RepoEntry] | None = None,
) -> str | None:
    entries = list(repos) if repos is not None else index.repos
    labs = labels_for_repos(entries)
    for lab, entry in zip(labs, entries):
        if lab == label:
            return entry.path
    for entry in entries:
        if entry.display() == label or entry.path == label:
            return entry.path
    return None


def entry_for_label(index: RepoIndex, label: str) -> RepoEntry | None:
    path = path_for_label(index, label)
    if not path:
        return None
    return index.find(path)
