"""Plugin registry."""

from __future__ import annotations

from ..workspace import Workspace
from .psx import PsxPlugin, supports_platform as psx_supports
from .unsupported import UnsupportedPlugin


def get_plugin(workspace: Workspace, platform: str):
    if psx_supports(platform):
        return PsxPlugin(workspace)
    return UnsupportedPlugin(platform.lower())
