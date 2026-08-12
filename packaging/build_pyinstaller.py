#!/usr/bin/env python3
"""Build the RetComM Studio onedir bundle with PyInstaller."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="", help="Semver embedded via env")
    ap.add_argument(
        "--name",
        default="RetComM-Studio",
        help="PyInstaller output folder / binary name",
    )
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    version = (args.version or "").strip() or (ROOT / "VERSION").read_text(
        encoding="utf-8"
    ).strip()
    os.environ["RETCOMM_STUDIO_VERSION"] = version
    os.environ["RETCOMM_STUDIO_APP_NAME"] = args.name

    dist = ROOT / "dist"
    build = ROOT / "build" / "pyinstaller"
    if args.clean:
        shutil.rmtree(dist / args.name, ignore_errors=True)
        shutil.rmtree(build, ignore_errors=True)

    # Ensure icons exist for embedding.
    make_icons = ROOT / "packaging" / "make-icons.sh"
    if make_icons.is_file() and not (ROOT / "assets" / "retcomm-studio.png").is_file():
        subprocess.check_call(["bash", str(make_icons)], cwd=ROOT)

    spec = ROOT / "packaging" / "retcomm-studio.spec"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        f"--distpath={dist}",
        f"--workpath={build}",
        str(spec),
    ]
    print("+", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=ROOT)

    out = dist / args.name
    if not out.is_dir():
        print(f"error: missing {out}", file=sys.stderr)
        return 1
    # Sidecar toolkit/assets for path helpers that look next to the exe.
    toolkit_src = ROOT / "tools" / "new_project_layout"
    toolkit_dst = out / "toolkit"
    if toolkit_src.is_dir() and not toolkit_dst.is_dir():
        shutil.copytree(
            toolkit_src,
            toolkit_dst,
            ignore=shutil.ignore_patterns(
                ".venv", "__pycache__", "*.pyc", "project_studio_repos.json", ".cache"
            ),
        )
    assets_src = ROOT / "assets"
    assets_dst = out / "assets"
    if assets_src.is_dir():
        if assets_dst.exists():
            shutil.rmtree(assets_dst)
        shutil.copytree(assets_src, assets_dst)

    print(f"Built {out} (version {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
