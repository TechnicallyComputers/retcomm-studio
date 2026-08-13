# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for RetComM Studio GUI.
# Built by packaging/build_pyinstaller.py (sets SPECPATH / work paths).

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent  # packaging/ → repo root when SPECPATH=packaging
if (ROOT / "packaging").is_dir() and (ROOT / "VERSION").is_file():
    pass
elif (ROOT.parent / "VERSION").is_file():
    ROOT = ROOT.parent

TOOLKIT = ROOT / "tools" / "new_project_layout"
ASSETS = ROOT / "assets"
ENTRY = ROOT / "packaging" / "entry.py"
NAME = os.environ.get("RETCOMM_STUDIO_APP_NAME", "RetComM-Studio")
ICON_ICO = ASSETS / "retcomm-studio.ico"
ICON_PNG = ASSETS / "retcomm-studio.png"

datas = []
binaries = []
hiddenimports = [
    "customtkinter",
    "project_studio",
    "project_studio.gui",
    "project_studio.cli",
    "project_studio.bulkops",
    "project_studio.discmeta",
    "project_studio.newproject",
    "project_studio.gitops",
    "project_studio.repo_index",
    "PIL",
    "PIL._tkinter_finder",
]

# Analysis datas must be (src, dest) 2-tuples — not Tree TOC 3-tuples
# (Tree → datas caused ValueError: too many values to unpack in PyInstaller 6).
# Bundle toolkit / assets; __pycache__ / .venv are skipped by PyInstaller's
# default ignores, and build_pyinstaller.py also sidecars a clean copy.
if TOOLKIT.is_dir():
    datas.append((str(TOOLKIT), "toolkit"))

if ASSETS.is_dir():
    datas.append((str(ASSETS), "assets"))

tmp_ret = collect_all("customtkinter")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# Optional dark theme extras
try:
    tmp_dark = collect_all("darkdetect")
    datas += tmp_dark[0]
    binaries += tmp_dark[1]
    hiddenimports += tmp_dark[2]
except Exception:
    pass

icon = None
if ICON_ICO.is_file():
    icon = str(ICON_ICO)
elif ICON_PNG.is_file():
    icon = str(ICON_PNG)

a = Analysis(
    [str(ENTRY)],
    pathex=[str(TOOLKIT), str(ROOT / "packaging")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=NAME,
)
