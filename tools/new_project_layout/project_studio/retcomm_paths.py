"""Shared RetComM data/config roots (same layout as retcomm-launcher)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def host_os_key() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def user_home() -> Path:
    return Path.home()


def xdg_data_home() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local)
        return user_home() / "AppData" / "Local"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg)
    return user_home() / ".local" / "share"


def xdg_config_home() -> Path:
    if sys.platform == "win32":
        # Launcher uses LOCALAPPDATA\retcomm for data; config is still under
        # %APPDATA%\retcomm when present, else Local.
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata)
        return xdg_data_home()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg)
    return user_home() / ".config"


@dataclass(frozen=True)
class RetcommPaths:
    """Mirrors retcomm::Paths from the launcher."""

    config_dir: Path
    data_dir: Path
    apps_dir: Path
    toolchains_dir: Path
    sdks_dir: Path
    engines_dir: Path
    catalog_dir: Path
    config_path: Path
    studio_config_path: Path
    cache_dir: Path
    self_update_dir: Path


def default_paths() -> RetcommPaths:
    """Resolve the shared RetComM roots (honors RETCOMM_* overrides)."""
    data_override = (os.environ.get("RETCOMM_DATA_DIR") or "").strip()
    if data_override:
        data = Path(data_override).expanduser().resolve()
    else:
        data = (xdg_data_home() / "retcomm").resolve()

    config_override = (os.environ.get("RETCOMM_CONFIG_DIR") or "").strip()
    if config_override:
        config = Path(config_override).expanduser().resolve()
    else:
        config = (xdg_config_home() / "retcomm").resolve()

    return RetcommPaths(
        config_dir=config,
        data_dir=data,
        apps_dir=data / "apps",
        toolchains_dir=data / "toolchains",
        sdks_dir=data / "sdks",
        engines_dir=data / "engines",
        catalog_dir=data / "catalog",
        config_path=config / "config.json",
        studio_config_path=config / "studio.json",
        cache_dir=data / "cache",
        self_update_dir=data / "self-update" / "studio",
    )


def ensure_dirs(paths: RetcommPaths | None = None) -> RetcommPaths:
    p = paths or default_paths()
    for d in (
        p.config_dir,
        p.data_dir,
        p.apps_dir,
        p.toolchains_dir,
        p.sdks_dir,
        p.engines_dir,
        p.catalog_dir,
        p.cache_dir,
        p.self_update_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
    return p
