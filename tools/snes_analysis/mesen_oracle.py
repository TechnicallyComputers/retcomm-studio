#!/usr/bin/env python3
"""Install and drive Mesen2 as the SNES reference oracle.

The SNES sibling of ``tools/psx_analysis/duckstation_oracle.py``, and it
deliberately differs from it in one important way — see "Not the same kind of
oracle" below.

Layout (mirrors the DuckStation oracle so Studio can find either)
-----------------------------------------------------------------
    <root>/app/          the portable install that actually gets run
    <root>/oracle.json   manifest: pinned release, asset hash, install stamp
    <root>/oracle.log    stdout/stderr of runs started by this tool
    <root>/oracle.pid    pid of a run started by this tool

where ``<root>`` is ``<retcomm-data>/oracle/mesen2`` (override with
``RETCOMM_SNES_ORACLE_DIR``).

Not the same kind of oracle
---------------------------
The DuckStation oracle is *patched* to speak the runtime's own TCP debug
protocol, so a tool can drive both processes and diff them command-for-command.
Mesen2 is **not patched**: it already ships a debugger and a Lua scripting API,
so it is consulted the way ``angrylion-rdp-plus`` is (recomp-ai-rules
LICENSING.md §2-3) — a reference emulator run out-of-process whose artifacts we
diff, not a second implementation of our protocol. The manifest records this
explicitly as ``speaks_runtime_protocol: false`` so nothing downstream assumes
otherwise.

That keeps the workspace's standing ruling intact ("Two processes, never one"):
the reference runs as its own process. It just answers via files a Lua script
writes rather than via our socket.

Why a pinned release rather than a source build
-----------------------------------------------
Mesen2 needs the .NET SDK to build, and this host has the .NET 8 *runtime*
only. The published Linux x64 archive is framework-dependent, so it runs
against that runtime as-is. GitHub publishes a SHA-256 digest per release
asset, so the download is verified against a real pin rather than trusted.

Licence note: Mesen2 is GPLv3. It is consulted as a developer tool, never
linked into or shipped with a port. Confirm that reading before relying on it.

Usage
-----
    python3 mesen_oracle.py doctor        # host prerequisites
    python3 mesen_oracle.py setup         # fetch + verify + install
    python3 mesen_oracle.py status
    python3 mesen_oracle.py start --rom /path/to/game.sfc
    python3 mesen_oracle.py stop
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

# --------------------------------------------------------------------------
# pin
# --------------------------------------------------------------------------
#
# Bump all four together. The digest is the one GitHub publishes for the
# asset (`digest` in the releases API), so `setup` verifies rather than trusts.
PIN: Dict[str, Any] = {
    "upstream_url": "https://github.com/SourMesen/Mesen2",
    "tag": "2.1.1",
    "asset": "Mesen_2.1.1_Linux_x64.zip",
    "sha256": "7a9947575cc198209f743fef83fb2b702b786ea705506bdf3f2aea01ab7c1ce9",
}

MANIFEST_KIND = "snesrecomp-mesen2-oracle"


class OracleError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def data_root() -> Path:
    """The Retro shared data root.

    Precedence must match duckstation_oracle.py's data_root() and
    studio_runner.cpp's retcomm_data_dir(); if they disagree, Studio reports an
    oracle somewhere this tool did not install it.
    """
    env = os.environ.get("RETCOMM_DATA_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "retcomm"
    return Path.home() / ".local" / "share" / "retcomm"


def oracle_root() -> Path:
    env = os.environ.get("RETCOMM_SNES_ORACLE_DIR")
    if env:
        return Path(env).expanduser()
    return data_root() / "oracle" / "mesen2"


def download_cache() -> Path:
    return data_root() / "downloads"


class Layout:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or oracle_root()
        self.app = self.root / "app"
        self.manifest = self.root / "oracle.json"
        self.pidfile = self.root / "oracle.pid"
        self.logfile = self.root / "oracle.log"

    @property
    def binary(self) -> Path:
        return self.app / "Mesen"

    def resolved_binary(self) -> Optional[Path]:
        """What `start` should actually run, per the manifest's provider."""
        if self.manifest.is_file():
            try:
                doc = json.loads(self.manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                doc = {}
            if doc.get("provider") == "system" and doc.get("system_binary"):
                p = Path(doc["system_binary"])
                if p.is_file():
                    return p
        return self.binary if self.binary.is_file() else None


# --------------------------------------------------------------------------
# host checks
# --------------------------------------------------------------------------

def dotnet_runtimes() -> list:
    exe = shutil.which("dotnet")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--list-runtimes"], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def has_net8() -> bool:
    return any(r.startswith("Microsoft.NETCore.App 8.") for r in dotnet_runtimes())


SYSTEM_CANDIDATES = ("mesen", "Mesen", "mesen-ce", "Mesen-CE")


def system_binary() -> Optional[Path]:
    """A Mesen built by the distro against this system's libs, if present."""
    for name in SYSTEM_CANDIDATES:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def smoke(binary: Path, timeout: int = 25) -> Optional[str]:
    """Run the binary briefly; return None if it survives, else the reason.

    Worth doing on every install: the upstream Linux x64 archive is built on an
    older distro, and on a host with a newer libstdc++ it aborts in a *static
    initializer* — a std::regex global in Base6502Assembler.cpp calls
    std::use_facet<collate<char>> and throws std::bad_cast before main() runs.
    Installing that and calling it done would hand Studio an oracle that cannot
    start.
    """
    env = dict(os.environ)
    env.setdefault("SDL_VIDEODRIVER", "dummy")
    try:
        proc = subprocess.run([str(binary), "--help"], capture_output=True,
                              text=True, timeout=timeout, env=env,
                              cwd=str(binary.parent))
    except subprocess.TimeoutExpired:
        return None          # still alive at timeout: it got past static init
    except OSError as e:
        return f"could not execute: {e}"
    blob = (proc.stdout or "") + (proc.stderr or "")
    if "bad_cast" in blob or "terminate called" in blob:
        return ("aborts during static initialisation (std::bad_cast from "
                "std::use_facet<collate<char>>) — the prebuilt binary is not "
                "compatible with this host's libstdc++")
    if proc.returncode is not None and proc.returncode < 0:
        return f"killed by signal {-proc.returncode}"
    return None


SYSTEM_HINT = (
    "This host cannot run the upstream prebuilt archive. A distro build linked\n"
    "  against the system libraries works — on Arch/CachyOS:\n"
    "      paru -S mesen-ce-git        # chaotic-aur, already configured here\n"
    "  then re-run:  mesen_oracle.py setup --provider system")


def cmd_doctor(args: argparse.Namespace) -> int:
    lay = Layout(Path(args.root) if args.root else None)
    ok = True

    log(f"root            {lay.root}")

    if shutil.which("dotnet"):
        log(f"dotnet          {shutil.which('dotnet')}")
    else:
        log("dotnet          MISSING — Mesen2's Linux build is framework-dependent")
        ok = False

    if has_net8():
        log("net8 runtime    present")
    else:
        rts = [r for r in dotnet_runtimes() if r.startswith("Microsoft.NETCore.App")]
        log("net8 runtime    MISSING — found: " + (", ".join(rts) or "none"))
        ok = False

    sysbin = system_binary()
    log(f"system mesen    {sysbin if sysbin else 'not on PATH'}")
    log(f"pinned          Mesen2 {PIN['tag']} ({PIN['asset']})")

    # Smoke the binary `start` would actually launch, which the manifest's
    # provider decides. Testing the unpacked release copy after the oracle has
    # been pointed at a system build reports a failure that does not matter.
    resolved = lay.resolved_binary()
    log(f"resolved        {resolved if resolved else 'not installed'}")
    if resolved is None:
        ok = False
    else:
        why = smoke(resolved)
        log(f"smoke           {'ok' if why is None else 'FAILS — ' + why}")
        if why is not None:
            ok = False
            if not sysbin:
                log("  " + SYSTEM_HINT)
    return 0 if ok else 1


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "retcomm-studio"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.replace(dest)


def install_from_system(lay: Layout) -> int:
    """Adopt a distro-built Mesen as the oracle.

    Nothing is copied: the manifest points at the system binary, so a package
    upgrade is picked up rather than silently shadowed by a stale copy here.
    """
    sysbin = system_binary()
    if not sysbin:
        raise OracleError("no system Mesen on PATH.\n  " + SYSTEM_HINT)
    why = smoke(sysbin)
    if why is not None:
        raise OracleError(f"system Mesen at {sysbin} does not run: {why}")
    lay.root.mkdir(parents=True, exist_ok=True)
    write_manifest(lay, stage="install", provider="system",
                   system_binary=str(sysbin), runnable=True)
    log(f"adopted         {sysbin} (system build)")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    lay = Layout(Path(args.root) if args.root else None)
    if getattr(args, "provider", "release") == "system":
        return install_from_system(lay)
    url = f"{PIN['upstream_url']}/releases/download/{PIN['tag']}/{PIN['asset']}"
    archive = download_cache() / PIN["asset"]

    if archive.is_file() and sha256_file(archive) == PIN["sha256"]:
        log(f"cached          {archive}")
    else:
        fetch(url, archive)

    got = sha256_file(archive)
    if got != PIN["sha256"]:
        raise OracleError(
            f"SHA-256 mismatch for {PIN['asset']}\n"
            f"  expected {PIN['sha256']}\n  got      {got}\n"
            "Refusing to install. Either the pin is stale or the download is "
            "not what upstream published.")
    log(f"verified        sha256 {got}")

    if lay.app.exists():
        shutil.rmtree(lay.app)
    lay.app.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(lay.app)
    log(f"extracted       {lay.app}")

    # The archive does not carry the executable bit through zip on every
    # producer; make anything that looks like the entry point runnable.
    for cand in (lay.binary, lay.app / "Mesen.sh"):
        if cand.is_file():
            cand.chmod(cand.stat().st_mode | 0o111)

    if not lay.binary.is_file():
        found = sorted(p.name for p in lay.app.iterdir())[:12]
        raise OracleError(
            f"expected {lay.binary.name} in the archive; got: {found}")

    why = smoke(lay.binary)
    if why is not None:
        write_manifest(lay, stage="install", provider="release", runnable=False)
        raise OracleError(
            f"installed to {lay.binary}, but it does not run: {why}.\n  "
            + SYSTEM_HINT)
    write_manifest(lay, stage="install", provider="release", runnable=True)
    log(f"installed       {lay.binary}")
    return 0


def write_manifest(lay: Layout, stage: str, provider: str = "release",
                   system_binary: Optional[str] = None,
                   runnable: Optional[bool] = None) -> None:
    doc: Dict[str, Any] = {}
    if lay.manifest.is_file():
        try:
            doc = json.loads(lay.manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            doc = {}
    doc.update({
        "kind": MANIFEST_KIND,
        "version": 1,
        "upstream_url": PIN["upstream_url"],
        "upstream_tag": PIN["tag"],
        "asset": PIN["asset"],
        "asset_sha256": PIN["sha256"],
        "root": str(lay.root),
        # Deliberately NOT the runtime's TCP protocol — Mesen2 is unpatched and
        # answers through Lua-written artifacts. Anything that plans to diff
        # command-for-command against the runner must key off this.
        "speaks_runtime_protocol": False,
        "protocol": "lua-script-artifacts",
        "licence": "GPL-3.0 (developer tool; never linked into a port)",
        "provider": provider,
    })
    if system_binary:
        doc["system_binary"] = system_binary
    if runnable is not None:
        doc["runnable"] = runnable
    doc[f"{stage}_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    lay.root.mkdir(parents=True, exist_ok=True)
    lay.manifest.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# status / path
# --------------------------------------------------------------------------

def running_pid(lay: Layout) -> Optional[int]:
    if not lay.pidfile.is_file():
        return None
    try:
        pid = int(lay.pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def cmd_status(args: argparse.Namespace) -> int:
    lay = Layout(Path(args.root) if args.root else None)
    doc = {}
    if lay.manifest.is_file():
        try:
            doc = json.loads(lay.manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            doc = {}
    pid = running_pid(lay)
    # `binary` is what START WILL RUN, resolved through the manifest's provider
    # — not the unpacked release path.
    #
    # These disagreed, and the disagreement was the whole bug: Studio launches
    # whatever this field names, so a host that had already adopted a system
    # Mesen (provider = "system") still got the pinned Ubuntu release, which
    # aborts in static init with std::bad_cast on a rolling distro. The tab
    # then reported "Mesen exited immediately" and advised switching to the
    # provider that was already selected. One resolver, used by both start and
    # status, is the only way those two can't drift again.
    resolved = lay.resolved_binary()
    if args.json:
        print(json.dumps({
            "root": str(lay.root),
            "installed": resolved is not None,
            "binary": str(resolved) if resolved else "",
            # Kept separate so the pane can say WHICH build it is about to run
            # rather than leaving the provider to be inferred from a path.
            "provider": doc.get("provider", "release"),
            "release_binary": str(lay.binary),
            "release_present": lay.binary.is_file(),
            "running_pid": pid,
            "manifest": doc,
        }, indent=1))
        return 0
    print(f"root: {lay.root}")
    for k in ("upstream_tag", "asset", "provider", "system_binary", "runnable",
              "speaks_runtime_protocol", "install_at"):
        if k in doc:
            print(f"  {k:<24} {doc[k]}")
    print(f"  {'resolved':<24} {resolved if resolved else 'nothing runnable'}")
    print(f"  {'installed':<24} {resolved is not None}")
    print(f"  {'running':<24} {pid if pid else 'no'}")
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    lay = Layout(Path(args.root) if args.root else None)
    if not args.binary:
        print(lay.root)
        return 0
    # Same resolver as start and status: `--binary` answers "what runs", which
    # under provider = system is not the unpacked release path.
    resolved = lay.resolved_binary()
    if resolved is None:
        print("error: nothing runnable — run `setup`, or `setup --provider system`",
              file=sys.stderr)
        return 2
    print(resolved)
    return 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    lay = Layout(Path(args.root) if args.root else None)
    resolved = lay.resolved_binary()
    if resolved is None:
        raise OracleError(f"not installed at {lay.binary} — run `setup` first")
    pid = running_pid(lay)
    if pid:
        log(f"already running (pid {pid})")
        return 0

    cmd = [str(resolved)]
    if args.rom:
        rom = Path(args.rom).expanduser().resolve()
        if not rom.is_file():
            raise OracleError(f"rom not found: {rom}")
        cmd.append(str(rom))
    cmd += args.extra or []

    env = dict(os.environ)
    log("$ " + " ".join(cmd))
    lay.root.mkdir(parents=True, exist_ok=True)
    with open(lay.logfile, "ab") as logf:
        logf.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                   f"{' '.join(cmd)} ===\n".encode())
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                cwd=str(resolved.parent), env=env,
                                start_new_session=True)
    lay.pidfile.write_text(str(proc.pid))
    log(f"started pid {proc.pid}; log {lay.logfile}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    lay = Layout(Path(args.root) if args.root else None)
    pid = running_pid(lay)
    if not pid:
        log("not running")
        return 0
    import signal
    os.kill(pid, signal.SIGTERM)
    log(f"stopped pid {pid}")
    try:
        lay.pidfile.unlink()
    except OSError:
        pass
    return 0


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="override the oracle root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="check host prerequisites")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("setup", help="fetch the pinned release, verify, install")
    p.add_argument("--provider", choices=("release", "system"), default="release",
                   help="release: pinned upstream archive (default). "
                        "system: adopt a distro-built Mesen already on PATH.")
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("status", help="where it is and what state it is in")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("path", help="print the install root")
    p.add_argument("--binary", action="store_true")
    p.set_defaults(fn=cmd_path)

    p = sub.add_parser("start", help="launch the oracle")
    p.add_argument("--rom")
    p.add_argument("--extra", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("stop", help="stop a run started by this tool")
    p.set_defaults(fn=cmd_stop)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except OracleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
