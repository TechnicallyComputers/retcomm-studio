"""Self-update for RetComM Studio + shared retcomm-toolchain packs.

Uses the same data root as retcomm-launcher / game launchers:
  ~/.local/share/retcomm/toolchains/<id>/<tag>/   (Windows: %LOCALAPPDATA%\\retcomm\\…)
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from .retcomm_paths import RetcommPaths, default_paths, ensure_dirs, host_os_key

ProgressFn = Callable[[str], None]

DEFAULT_STUDIO_SLUG = "TechnicallyComputers/retcomm-studio"
DEFAULT_TOOLCHAIN_SLUG = "TechnicallyComputers/retcomm-toolchains"
DEFAULT_TOOLCHAIN_ID = "cmake-clang-v1"
TOOLCHAIN_GLOBS = {
    "linux": "*cmake-clang-v1*linux*",
    "windows": "*cmake-clang-v1*windows*",
    "macos": "*cmake-clang-v1*macos*",
}
USER_AGENT = "RetComM-Studio-Updater/1.0 (+https://github.com/TechnicallyComputers/retcomm-studio)"


# ---------------------------------------------------------------------------
# Version / config helpers
# ---------------------------------------------------------------------------


def normalize_tag(tag: str) -> str:
    t = (tag or "").strip()
    if t.lower().startswith("v") and len(t) > 1 and t[1].isdigit():
        t = t[1:]
    return t


def version_tuple(tag: str) -> tuple[int, ...]:
    t = normalize_tag(tag)
    parts: list[int] = []
    for bit in re.split(r"[^0-9]+", t):
        if bit.isdigit():
            parts.append(int(bit))
    return tuple(parts) if parts else (0,)


def version_newer(latest: str, current: str) -> bool:
    return version_tuple(latest) > version_tuple(current)


def studio_app_version() -> str:
    env = (os.environ.get("RETCOMM_STUDIO_VERSION") or "").strip()
    if env:
        return normalize_tag(env)
    # Frozen sidecar / source VERSION file
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve().parent
        meipass = Path(getattr(sys, "_MEIPASS", exe))
        candidates += [exe / "VERSION", meipass / "VERSION", exe / "assets" / "VERSION"]
    try:
        from .paths import toolkit_dir

        toolkit = toolkit_dir()
        candidates.append(toolkit.parent.parent / "VERSION")
    except Exception:
        pass
    candidates.append(Path(__file__).resolve().parents[3] / "VERSION")
    for p in candidates:
        try:
            if p.is_file():
                return normalize_tag(p.read_text(encoding="utf-8").splitlines()[0])
        except OSError:
            continue
    try:
        from . import __version__

        return normalize_tag(str(__version__))
    except Exception:
        return "0.0.0"


def studio_github_slug() -> str:
    return (os.environ.get("RETCOMM_STUDIO_GITHUB_SLUG") or DEFAULT_STUDIO_SLUG).strip()


def load_studio_config(paths: RetcommPaths | None = None) -> dict:
    p = paths or default_paths()
    try:
        if p.studio_config_path.is_file():
            return json.loads(p.studio_config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_studio_config(data: dict, paths: RetcommPaths | None = None) -> None:
    p = ensure_dirs(paths)
    p.studio_config_path.parent.mkdir(parents=True, exist_ok=True)
    p.studio_config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def check_updates_on_startup_enabled(paths: RetcommPaths | None = None) -> bool:
    cfg = load_studio_config(paths)
    if "check_updates_on_startup" in cfg:
        return bool(cfg["check_updates_on_startup"])
    # Fall back to launcher config.json when present.
    p = paths or default_paths()
    try:
        if p.config_path.is_file():
            hub = json.loads(p.config_path.read_text(encoding="utf-8"))
            if "check_updates_on_startup" in hub:
                return bool(hub["check_updates_on_startup"])
    except (OSError, json.JSONDecodeError):
        pass
    return True


# ---------------------------------------------------------------------------
# Install channel detection
# ---------------------------------------------------------------------------


@dataclass
class InstallInfo:
    channel: str  # appimage | macos-app | windows-installer | windows-portable | source
    supported: bool
    path: Path | None = None
    hint: str = ""


def detect_install_info() -> InstallInfo:
    """How this Studio process was launched (for self-update asset picking)."""
    if not getattr(sys, "frozen", False) and not os.environ.get("RETCOMM_STUDIO_FROZEN"):
        return InstallInfo(
            channel="source",
            supported=False,
            hint="Dev/source builds update via git; install a release package for self-update.",
        )

    exe = Path(sys.executable).resolve()
    parent = exe.parent

    # Explicit channel.json (written by packaging)
    channel_file = parent / "channel.json"
    channel = ""
    portable_exe = ""
    if channel_file.is_file():
        try:
            raw = json.loads(channel_file.read_text(encoding="utf-8"))
            channel = str(raw.get("channel") or "").lower()
            portable_exe = str(raw.get("portable_exe") or "")
        except (OSError, json.JSONDecodeError):
            pass
    channel_env = (os.environ.get("RETCOMM_STUDIO_INSTALL_CHANNEL") or "").strip().lower()
    if channel_env:
        channel = channel_env

    if sys.platform.startswith("linux"):
        appimage = (os.environ.get("APPIMAGE") or "").strip()
        if appimage and Path(appimage).is_file():
            p = Path(appimage)
            writable = os.access(p.parent, os.W_OK)
            return InstallInfo(
                channel="appimage",
                supported=writable,
                path=p,
                hint="" if writable else f"AppImage directory not writable: {p.parent}",
            )
        return InstallInfo(
            channel="source",
            supported=False,
            hint="Self-update needs the Linux AppImage release.",
        )

    if sys.platform == "darwin":
        # …/RetComM Studio.app/Contents/MacOS/<bin>
        for cand in (parent, *parent.parents):
            if cand.suffix == ".app" and cand.name.endswith(".app"):
                writable = os.access(cand.parent, os.W_OK)
                return InstallInfo(
                    channel="macos-app",
                    supported=writable,
                    path=cand,
                    hint="" if writable else f"Cannot write next to app bundle: {cand.parent}",
                )
        return InstallInfo(
            channel="source",
            supported=False,
            hint="Self-update needs the macOS .app / DMG release.",
        )

    # Windows
    if channel in ("portable",) or portable_exe:
        pe = Path(portable_exe) if portable_exe else exe
        return InstallInfo(
            channel="windows-portable",
            supported=pe.is_file() and os.access(pe.parent, os.W_OK),
            path=pe,
            hint="",
        )
    if channel in ("installer", "zip") or (parent / "unins000.exe").is_file():
        return InstallInfo(
            channel="windows-installer",
            supported=os.access(parent, os.W_OK),
            path=parent,
            hint="",
        )
    # Frozen but unknown layout — treat as portable onedir next to exe.
    return InstallInfo(
        channel="windows-portable",
        supported=os.access(parent, os.W_OK),
        path=exe,
        hint="",
    )


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


@dataclass
class GhAsset:
    name: str
    url: str
    size: int = 0


@dataclass
class GhRelease:
    tag: str
    assets: list[GhAsset] = field(default_factory=list)
    prerelease: bool = False


def _http_json(url: str) -> object:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_release(obj: dict) -> GhRelease:
    assets = []
    for a in obj.get("assets") or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        if name and url:
            assets.append(GhAsset(name=name, url=url, size=int(a.get("size") or 0)))
    return GhRelease(
        tag=str(obj.get("tag_name") or "").strip(),
        assets=assets,
        prerelease=bool(obj.get("prerelease")),
    )


def fetch_latest_release(slug: str, *, allow_prerelease: bool = True) -> GhRelease:
    if allow_prerelease:
        data = _http_json(f"https://api.github.com/repos/{slug}/releases?per_page=15")
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict) or item.get("draft"):
                    continue
                if item.get("prerelease") and not allow_prerelease:
                    continue
                rel = _parse_release(item)
                if rel.tag:
                    return rel
    data = _http_json(f"https://api.github.com/repos/{slug}/releases/latest")
    if not isinstance(data, dict):
        raise RuntimeError("unexpected GitHub release payload")
    rel = _parse_release(data)
    if not rel.tag:
        raise RuntimeError("latest release missing tag_name")
    return rel


def pick_asset(rel: GhRelease, patterns: list[str]) -> GhAsset | None:
    names = [(a, a.name.lower()) for a in rel.assets]
    for pat in patterns:
        pl = pat.lower()
        for asset, low in names:
            if fnmatch(low, pl.lower()) or fnmatch(asset.name, pat):
                return asset
    return None


def download_file(url: str, dest: Path, on_progress: ProgressFn | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if on_progress:
        on_progress(f"Downloading {dest.name}…")
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if on_progress and total:
                on_progress(f"Downloading {dest.name}: {done * 100 // total}%")


def extract_archive(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
        return
    if name.endswith((".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tar")):
        with tarfile.open(archive) as tf:
            tf.extractall(dest)
        return
    raise RuntimeError(f"unsupported archive type: {archive.name}")


def unwrap_single_subdir(root: Path) -> Path:
    try:
        kids = [p for p in root.iterdir() if p.name not in (".DS_Store",)]
    except OSError:
        return root
    if len(kids) == 1 and kids[0].is_dir():
        return kids[0]
    return root


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@dataclass
class ComponentUpdate:
    kind: str  # studio | toolchain
    current: str
    latest: str
    available: bool
    supported: bool
    message: str
    asset_name: str = ""
    release: GhRelease | None = None
    asset: GhAsset | None = None


@dataclass
class UpdateCheckResult:
    ok: bool
    studio: ComponentUpdate
    toolchain: ComponentUpdate
    message: str = ""


def _studio_asset_patterns(channel: str) -> list[str]:
    machine = platform.machine().lower()
    if channel == "appimage":
        return [
            "*Studio*linux*x86_64*.AppImage",
            "*Studio*linux*.AppImage",
            "*linux*x86_64*.AppImage",
        ]
    if channel == "macos-app":
        if machine in ("arm64", "aarch64"):
            return ["*Studio*macos*arm64*.dmg", "*macos*arm64*.dmg", "*macos*.dmg"]
        return ["*Studio*macos*x86_64*.dmg", "*macos*x86_64*.dmg", "*macos*.dmg"]
    if channel == "windows-installer":
        return [
            "*Studio*windows*setup*.exe",
            "*Studio*windows*x64*setup*.exe",
            "*windows*setup*.exe",
        ]
    if channel == "windows-portable":
        return [
            "*Studio*portable*windows*.zip",
            "*portable*windows*.zip",
            "*Studio*windows*.zip",
        ]
    return []


def installed_toolchain_tag(paths: RetcommPaths, pack_id: str = DEFAULT_TOOLCHAIN_ID) -> str:
    base = paths.toolchains_dir / pack_id
    latest = base / "latest"
    path_file = base / "latest.path"
    candidates: list[Path] = []
    if latest.exists():
        try:
            candidates.append(latest.resolve() if latest.is_symlink() else latest)
        except OSError:
            candidates.append(latest)
    if path_file.is_file():
        try:
            line = path_file.read_text(encoding="utf-8").splitlines()[0].strip()
            if line:
                candidates.append(Path(line))
        except OSError:
            pass
    # Prefer stamp JSON inside a versioned dir
    for cand in candidates:
        stamp = cand / ".retcomm-pack.json"
        if stamp.is_file():
            try:
                meta = json.loads(stamp.read_text(encoding="utf-8"))
                tag = str(meta.get("tag") or "").strip()
                if tag:
                    return tag
            except (OSError, json.JSONDecodeError):
                pass
        # Directory name is often the tag
        if cand.parent.name == pack_id and cand.name not in ("latest", ".staging"):
            return cand.name
    # Scan versioned folders
    if base.is_dir():
        tags = []
        for child in base.iterdir():
            if child.name.startswith(".") or child.name == "latest":
                continue
            if (child / ".retcomm-pack.json").is_file() or (child / "bin").is_dir():
                tags.append(child.name)
        if tags:
            tags.sort(key=version_tuple, reverse=True)
            return tags[0]
    return ""


def check_updates(
    paths: RetcommPaths | None = None,
    *,
    allow_prerelease: bool = True,
    on_progress: ProgressFn | None = None,
) -> UpdateCheckResult:
    p = ensure_dirs(paths)
    install = detect_install_info()
    current = studio_app_version()

    def prog(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    studio = ComponentUpdate(
        kind="studio",
        current=current,
        latest=current,
        available=False,
        supported=install.supported,
        message=install.hint or "Studio update check pending…",
    )
    toolchain = ComponentUpdate(
        kind="toolchain",
        current=installed_toolchain_tag(p) or "(none)",
        latest="",
        available=False,
        supported=True,
        message="Toolchain update check pending…",
    )

    try:
        prog(f"Checking {studio_github_slug()}…")
        rel = fetch_latest_release(studio_github_slug(), allow_prerelease=allow_prerelease)
        studio.latest = normalize_tag(rel.tag)
        studio.release = rel
        if install.supported:
            patterns = _studio_asset_patterns(install.channel)
            asset = pick_asset(rel, patterns) if patterns else None
            studio.asset = asset
            studio.asset_name = asset.name if asset else ""
            if not asset:
                studio.supported = False
                studio.message = (
                    f"Latest Studio {studio.latest} has no asset for channel "
                    f"'{install.channel}'."
                )
            elif version_newer(studio.latest, studio.current):
                studio.available = True
                studio.message = f"Studio update: {studio.current} → {studio.latest}"
            else:
                studio.message = f"Studio is up to date ({studio.latest})."
        else:
            if version_newer(normalize_tag(rel.tag), current):
                studio.message = (
                    f"Studio {normalize_tag(rel.tag)} is available, but this install "
                    f"cannot self-update ({install.hint or install.channel})."
                )
            else:
                studio.message = install.hint or "Studio self-update not supported."
    except Exception as exc:
        studio.message = f"Studio check failed: {exc}"

    try:
        prog(f"Checking {DEFAULT_TOOLCHAIN_SLUG}…")
        trel = fetch_latest_release(DEFAULT_TOOLCHAIN_SLUG, allow_prerelease=True)
        toolchain.latest = normalize_tag(trel.tag)
        toolchain.release = trel
        glob = TOOLCHAIN_GLOBS.get(host_os_key(), TOOLCHAIN_GLOBS["linux"])
        tasset = pick_asset(trel, [glob, f"*{DEFAULT_TOOLCHAIN_ID}*"])
        toolchain.asset = tasset
        toolchain.asset_name = tasset.name if tasset else ""
        cur = installed_toolchain_tag(p)
        toolchain.current = cur or "(none)"
        if not tasset:
            toolchain.message = f"No toolchain asset matching {glob} on {trel.tag}"
        elif not cur or version_newer(toolchain.latest, cur):
            toolchain.available = True
            toolchain.message = (
                f"Toolchain update: {toolchain.current} → {toolchain.latest}"
            )
        else:
            toolchain.message = f"Toolchain is up to date ({toolchain.latest})."
    except Exception as exc:
        toolchain.message = f"Toolchain check failed: {exc}"

    bits = [studio.message, toolchain.message]
    any_avail = studio.available or toolchain.available
    return UpdateCheckResult(
        ok=True,
        studio=studio,
        toolchain=toolchain,
        message=("Updates available. " if any_avail else "") + " · ".join(bits),
    )


# ---------------------------------------------------------------------------
# Apply updates
# ---------------------------------------------------------------------------


def _set_latest_pointer(cache_base: Path, target: Path) -> None:
    latest = cache_base / "latest"
    path_file = cache_base / "latest.path"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
    except OSError:
        try:
            shutil.rmtree(latest)
        except OSError:
            pass
    try:
        latest.symlink_to(target, target_is_directory=True)
    except OSError:
        # Windows without symlink privilege — write latest.path fallback.
        path_file.write_text(str(target) + "\n", encoding="utf-8")
        return
    path_file.write_text(str(target) + "\n", encoding="utf-8")


def apply_toolchain_update(
    info: ComponentUpdate,
    paths: RetcommPaths | None = None,
    *,
    on_progress: ProgressFn | None = None,
) -> tuple[bool, str]:
    if not info.available or not info.release or not info.asset:
        return False, info.message or "No toolchain update to apply."
    p = ensure_dirs(paths)
    pack_id = DEFAULT_TOOLCHAIN_ID
    tag = normalize_tag(info.latest) or normalize_tag(info.release.tag)
    dest = p.toolchains_dir / pack_id / tag
    staging = p.toolchains_dir / pack_id / ".staging"
    dl_dir = p.cache_dir / "studio-updates"
    dl_dir.mkdir(parents=True, exist_ok=True)
    archive = dl_dir / info.asset.name

    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        download_file(info.asset.url, archive, on_progress)
        if on_progress:
            on_progress(f"Extracting {info.asset.name}…")
        extract_archive(archive, staging)
        root = unwrap_single_subdir(staging)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root), str(dest))
        meta = {
            "id": pack_id,
            "tag": info.release.tag,
            "asset": info.asset.name,
            "github": DEFAULT_TOOLCHAIN_SLUG,
        }
        (dest / ".retcomm-pack.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
        _set_latest_pointer(p.toolchains_dir / pack_id, dest.resolve())
        shutil.rmtree(staging, ignore_errors=True)
        try:
            archive.unlink()
        except OSError:
            pass
        return True, f"Installed toolchain {pack_id} {tag} → {dest}"
    except Exception as exc:
        return False, f"Toolchain update failed: {exc}"


def _schedule_unix_replace(src: Path, dest: Path, relaunch: list[str]) -> Path:
    script = src.parent / "apply_studio_update.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"SRC={shlex_quote(str(src))}\n"
        f"DEST={shlex_quote(str(dest))}\n"
        "sleep 1\n"
        'cp -f "$SRC" "$DEST"\n'
        'chmod +x "$DEST"\n'
        f"exec { ' '.join(shlex_quote(a) for a in relaunch) }\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    subprocess.Popen(["bash", str(script)], start_new_session=True)
    return script


def shlex_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


def apply_studio_update(
    info: ComponentUpdate,
    paths: RetcommPaths | None = None,
    *,
    on_progress: ProgressFn | None = None,
) -> tuple[bool, str, bool]:
    """Returns (ok, message, should_exit)."""
    if not info.available:
        return False, info.message or "No Studio update to apply.", False
    install = detect_install_info()
    if not install.supported or install.path is None:
        # Open releases page as fallback
        url = f"https://github.com/{studio_github_slug()}/releases/latest"
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", url])
            elif sys.platform == "win32":
                os.startfile(url)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", url])
        except OSError:
            pass
        return (
            False,
            (install.hint or "Self-update unsupported.") + f" Opened {url}",
            False,
        )
    if not info.asset or not info.release:
        return False, "No matching Studio release asset.", False

    p = ensure_dirs(paths)
    dl_dir = p.self_update_dir
    dl_dir.mkdir(parents=True, exist_ok=True)
    archive = dl_dir / info.asset.name
    try:
        download_file(info.asset.url, archive, on_progress)
    except Exception as exc:
        return False, f"Studio download failed: {exc}", False

    channel = install.channel
    if channel == "appimage":
        staged = dl_dir / "RetComM-Studio.AppImage.new"
        shutil.copy2(archive, staged)
        staged.chmod(0o755)
        _schedule_unix_replace(staged, install.path, [str(install.path)])
        return True, f"Studio {info.latest} staged; restarting…", True

    if channel == "macos-app":
        # Open DMG for the user (safe cross-version).
        try:
            subprocess.Popen(["open", str(archive)])
        except OSError as exc:
            return False, f"Could not open DMG: {exc}", False
        return (
            True,
            f"Downloaded Studio {info.latest}. Replace the app from the DMG, then relaunch.",
            False,
        )

    if channel == "windows-installer":
        setup = dl_dir / info.asset.name
        if setup.resolve() != archive.resolve():
            shutil.copy2(archive, setup)
        # Launch setup detached; user completes wizard.
        subprocess.Popen([str(setup)], cwd=str(dl_dir), shell=False)
        return True, f"Launched Studio {info.latest} installer.", True

    if channel == "windows-portable":
        extract_to = dl_dir / "portable-stage"
        if extract_to.exists():
            shutil.rmtree(extract_to, ignore_errors=True)
        extract_to.mkdir(parents=True, exist_ok=True)
        extract_archive(archive, extract_to)
        root = unwrap_single_subdir(extract_to)
        # Prefer friendly exe name if present
        new_exe = None
        for name in ("RetComM Studio.exe", "RetComM-Studio.exe"):
            cand = root / name
            if cand.is_file():
                new_exe = cand
                break
        if new_exe is None:
            exes = list(root.glob("*.exe"))
            new_exe = exes[0] if exes else None
        if new_exe is None:
            return False, "Portable zip missing Studio exe.", False
        dest_dir = install.path.parent if install.path.is_file() else install.path
        ps1 = dl_dir / "apply_studio_portable_update.ps1"
        ps1.write_text(
            "$ErrorActionPreference='Stop'\n"
            f"$Src = {json.dumps(str(root))}\n"
            f"$Dest = {json.dumps(str(dest_dir))}\n"
            "Start-Sleep -Seconds 2\n"
            "Copy-Item -Path (Join-Path $Src '*') -Destination $Dest -Recurse -Force\n"
            f"$Exe = Join-Path $Dest {json.dumps(new_exe.name)}\n"
            "Start-Process -FilePath $Exe\n",
            encoding="utf-8",
        )
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1),
            ],
            cwd=str(dl_dir),
        )
        return True, f"Studio {info.latest} staged; restarting…", True

    return False, f"Unhandled channel: {channel}", False


def apply_updates(
    result: UpdateCheckResult,
    *,
    update_studio: bool,
    update_toolchain: bool,
    paths: RetcommPaths | None = None,
    on_progress: ProgressFn | None = None,
) -> tuple[str, bool]:
    """Apply selected updates. Returns (summary, should_exit)."""
    lines: list[str] = []
    should_exit = False
    p = ensure_dirs(paths)
    if update_toolchain and result.toolchain.available:
        ok, msg = apply_toolchain_update(result.toolchain, p, on_progress=on_progress)
        lines.append(("OK: " if ok else "FAIL: ") + msg)
    if update_studio and result.studio.available:
        ok, msg, exit_now = apply_studio_update(
            result.studio, p, on_progress=on_progress
        )
        lines.append(("OK: " if ok else "FAIL: ") + msg)
        should_exit = should_exit or exit_now
    if not lines:
        return "Nothing to update.", False
    return "\n".join(lines), should_exit


def check_updates_async(
    callback: Callable[[UpdateCheckResult], None],
    *,
    on_progress: ProgressFn | None = None,
) -> None:
    def worker() -> None:
        try:
            result = check_updates(on_progress=on_progress)
        except Exception as exc:
            cur = studio_app_version()
            result = UpdateCheckResult(
                ok=False,
                studio=ComponentUpdate(
                    "studio", cur, cur, False, False, str(exc)
                ),
                toolchain=ComponentUpdate(
                    "toolchain", "(unknown)", "", False, False, str(exc)
                ),
                message=str(exc),
            )
        callback(result)

    threading.Thread(target=worker, daemon=True).start()
