#!/usr/bin/env python3
"""beetle_oracle.py -- set up, build, and run the patched Beetle PSX oracle.

    python3 beetle_oracle.py doctor      # can this machine build it?
    python3 beetle_oracle.py all         # fetch + patch + build + install
    python3 beetle_oracle.py start --disc disc/game.cue
    python3 beetle_oracle.py status

Why a second oracle
-------------------
DuckStation answers "did the guest render the same?" by image. Beetle answers a
different question: it carries our own RAM **write trace** (`wtrace_dump`), the
same instrument psx-runtime has. That means a divergence can be diffed by value
and order with the *identical* probe on both sides, instead of comparing two
different instruments and arguing about the gap.

The three hook patches under `beetle/` are what make it emit that trace. They
are versioned here, next to the tool, for the reason `tools/psx_analysis/
README.md` gives: instrumentation that lives only in a submodule working tree
gets lost to a routine `git pull --rebase`, and that has already happened once.

Licensing -- read this before editing the checkout
--------------------------------------------------
Beetle PSX is mednafen-derived and copyleft. `recomp-ai-rules/LICENSING.md` §2
and `PSX/PRINCIPLES.md` §11 both say its source may not be READ by anyone who
will touch the recompiler implementation -- reading contaminates the
clean-room. Applying a committed patch and running the result is fine; opening
its sources to write new hooks is not, and is a job for someone outside the
implementation.

This tool therefore never authors code inside the checkout. It fetches, pins,
applies our committed patches, repairs one known collision *between our own
patches*, builds, and reports precisely what is missing. See `doctor`.

Where it lives
--------------
NOT in the game repo. Shared by every recomp title, so it installs into the
Retro data root alongside the toolchains, catalog and the DuckStation oracle:

    ~/.local/share/retcomm/oracle/beetle/          (Linux / macOS)
    %LOCALAPPDATA%\\retcomm\\oracle\\beetle\\        (Windows)

Override with RETCOMM_BEETLE_DIR, or move the whole root with RETCOMM_DATA_DIR
(the same variables Retro Launcher and Studio already honour).

    <root>/src/          pinned upstream checkout with our hook patches applied
    <root>/build/        cmake/ninja tree for the psx-beetle frontend
    <root>/app/          the portable install that actually gets run
    <root>/oracle.json   manifest: upstream base, patch hashes, build stamp

How it is built
---------------
Two stages, because the oracle is our frontend around upstream's core:

  1. `make platform=unix STATIC_LINKING=1 HAVE_LIGHTREC=0` in the checkout,
     producing an ar archive that upstream names `mednafen_psx_libretro.so`.
     It is staged as `libmednafen_psx.a`, the name psxrecomp's
     `runtime/CMakeLists.txt` looks for.
  2. `ninja psx-beetle` in psxrecomp's `runtime/`, which links that archive
     with `beetle_libretro.cpp` + `beetle_debug_server.c` -- our SDL frontend
     and the JSON-over-TCP debug server, on port 4380.

Stage 2 needs a psxrecomp checkout: `--psxrecomp <path>`, else $PSXRECOMP_DIR,
else autodetected from the current project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
PIN_PATH = HERE / "beetle" / "pin.json"
PATCH_DIR = HERE / "beetle"

# The three symbols psxrecomp's beetle_libretro.cpp references that our
# committed patches do NOT provide. Named explicitly so `doctor` can say what is
# missing before a build burns ten minutes discovering it, and so a compile
# failure is diagnosed instead of dumped.
MISSING_HOOKS = [
    ("g_psxrecomp_rtrace_cb",
     "read-trace callback (rtrace_* debug commands)"),
    ("g_psxrecomp_irq_cb",
     "IRQ event callback (irq/device-event ring)"),
    ("PSXRecomp_GetDecodeVolume",
     "PS_CDC method for CD-audio decode-volume ground truth"),
]


class OracleError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def data_root() -> Path:
    """The Retro shared data root.

    Precedence mirrors duckstation_oracle.py's data_root() and
    studio_runner.cpp's retcomm_data_dir() exactly, XDG_DATA_HOME included. If
    these three disagree, the GUI reports an oracle that is not where this tool
    installed it, and neither side looks wrong.
    """
    env = os.environ.get("RETCOMM_DATA_DIR")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("USERPROFILE", str(Path.home())) + "/AppData/Local")
        return Path(base) / "retcomm"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "retcomm"
    return Path.home() / ".local" / "share" / "retcomm"


def oracle_root() -> Path:
    env = os.environ.get("RETCOMM_BEETLE_DIR")
    if env:
        return Path(env).expanduser()
    return data_root() / "oracle" / "beetle"


def exe_name() -> str:
    return "psx-beetle.exe" if sys.platform == "win32" else "psx-beetle"


class Layout:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or oracle_root()
        self.src = self.root / "src"
        self.build = self.root / "build"
        self.app = self.root / "app"
        self.manifest = self.root / "oracle.json"
        self.pidfile = self.root / "oracle.pid"
        self.logfile = self.root / "oracle.log"

    @property
    def core_lib(self) -> Path:
        return self.src / "libmednafen_psx.a"

    @property
    def binary(self) -> Path:
        return self.app / exe_name()

    @property
    def built_binary(self) -> Path:
        return self.build / exe_name()


def load_pin() -> Dict[str, Any]:
    if not PIN_PATH.is_file():
        raise OracleError(f"missing pin file: {PIN_PATH}")
    with open(PIN_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def find_psxrecomp(explicit: Optional[str]) -> Optional[Path]:
    """Locate a psxrecomp checkout, which stage 2 links against."""
    cands: List[Path] = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    env = os.environ.get("PSXRECOMP_DIR")
    if env:
        cands.append(Path(env).expanduser())
    cwd = Path.cwd()
    for base in (cwd, *cwd.parents):
        cands.append(base / "psxrecomp")
        if base.name == "psxrecomp":
            cands.append(base)
    for c in cands:
        if (c / "runtime" / "CMakeLists.txt").is_file() and \
           (c / "runtime" / "src" / "beetle_libretro.cpp").is_file():
            return c.resolve()
    return None


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

def run(cmd: List[str], cwd: Optional[Path] = None, check: bool = True,
        quiet: bool = False, log_to: Optional[Path] = None) -> int:
    if not quiet:
        log("$ " + " ".join(str(c) for c in cmd))
    out = None
    if log_to is not None:
        log_to.parent.mkdir(parents=True, exist_ok=True)
        out = open(log_to, "wb")
    try:
        rc = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                            stdout=out, stderr=subprocess.STDOUT if out else None).returncode
    finally:
        if out:
            out.close()
    if check and rc != 0:
        raise OracleError(f"command failed (rc={rc}): {' '.join(str(c) for c in cmd)}"
                          + (f"\n  log: {log_to}" if log_to else ""))
    return rc


def capture(cmd: List[str], cwd: Optional[Path] = None) -> Tuple[int, str]:
    p = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jobs_arg(n: int) -> int:
    return n if n > 0 else (os.cpu_count() or 4)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def doctor(args: argparse.Namespace) -> int:
    pin = load_pin()
    lay = Layout(Path(args.root).expanduser() if args.root else None)
    ok = True

    log("Beetle PSX oracle -- host check")
    log(f"  install root      : {lay.root}")
    log(f"  upstream base     : {pin['upstream_base'][:12]}  "
        f"({pin.get('upstream_base_subject','')})")
    log("")

    for tool, why in (("git", "fetch the pinned checkout"),
                      ("make", "build the libretro core"),
                      ("cc", "C compiler"),
                      ("c++", "C++ compiler"),
                      ("cmake", "configure the psx-beetle frontend"),
                      ("ninja", "build the psx-beetle frontend")):
        p = which(tool)
        log(f"  {'ok ' if p else 'MISSING'}  {tool:<8} {p or '-':<28} {why}")
        if not p:
            ok = False

    px = find_psxrecomp(args.psxrecomp)
    log("")
    if px:
        log(f"  ok       psxrecomp checkout: {px}")
    else:
        log("  MISSING  psxrecomp checkout — pass --psxrecomp <path> or set "
            "$PSXRECOMP_DIR")
        ok = False

    for name in (p for p in pin["patches"]):
        f = PATCH_DIR / name
        log(f"  {'ok ' if f.is_file() else 'MISSING'}  patch {name}")
        if not f.is_file():
            ok = False

    log("")
    log("  KNOWN BLOCKER — the committed patches are not sufficient to link:")
    for sym, what in MISSING_HOOKS:
        log(f"      {sym:<28} {what}")
    log("    psxrecomp/runtime/src/beetle_libretro.cpp references these")
    log("    unconditionally, but no committed patch defines them. Landing them")
    log("    means writing code inside the Beetle checkout, which LICENSING.md §2")
    log("    and PSX/PRINCIPLES.md §11 forbid to anyone touching the recompiler.")
    log("    Two lawful resolutions:")
    log("      (a) someone outside the implementation lands the hooks and")
    log("          regenerates the patches under beetle/;")
    log("      (b) guard the three references in beetle_libretro.cpp behind a")
    log("          feature macro, so the committed patch set is self-sufficient")
    log("          and rtrace/irq/decode-volume report unavailable rather than")
    log("          returning zeros. wtrace_dump — the reason this oracle exists —")
    log("          is unaffected either way.")
    log("")
    bios = pin.get("bios", {})
    log(f"  note: the core expects a BIOS named {bios.get('expects','?')} "
        f"(sha1 prefix {bios.get('sha1_prefix','?')}, full digest unverified here).")

    log("")
    log("READY" if ok else "NOT READY (see MISSING above; the KNOWN BLOCKER "
                          "applies regardless)")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# setup: fetch + patch
# ---------------------------------------------------------------------------

def fetch_source(lay: Layout, pin: Dict[str, Any], force: bool = False) -> None:
    base = pin["upstream_base"]
    if force and lay.src.exists():
        shutil.rmtree(lay.src)
    if (lay.src / ".git").is_dir():
        rc, out = capture(["git", "rev-parse", "HEAD"], cwd=lay.src)
        if rc == 0 and out.strip() == base:
            log(f"  source already at {base[:12]}")
            return
    lay.src.mkdir(parents=True, exist_ok=True)
    if not (lay.src / ".git").is_dir():
        run(["git", "init", "-q", "."], cwd=lay.src)
        run(["git", "remote", "add", "origin", pin["upstream_url"]], cwd=lay.src)
    # A blobless clone-equivalent: full history is needed because the pin is an
    # old commit and a shallow fetch of a short SHA is refused by the server.
    run(["git", "fetch", "-q", "--filter=blob:none", "origin"], cwd=lay.src)
    rc, out = capture(["git", "rev-parse", "--verify", base + "^{commit}"], cwd=lay.src)
    if rc != 0:
        raise OracleError(f"pinned commit {base} not found upstream\n{out}")
    run(["git", "checkout", "-q", "--force", base], cwd=lay.src)
    run(["git", "clean", "-qfd"], cwd=lay.src)


def _dedupe_our_globals(src: Path) -> int:
    """Remove duplicate definitions of OUR OWN hook globals in libretro.cpp.

    beetle_cdcmd_trace_hook.patch and beetle_wtrace_hook.patch each insert the
    same `g_psxrecomp_wtrace_cb` / `g_psxrecomp_fntrace_cb` definitions, so
    applying both yields a redefinition error. psxrecomp/docs/beetle-linux.md
    already tells a human to "drop the duplicate block"; this does it, keeping
    the FIRST definition of each symbol.

    Only lines defining `g_psxrecomp_*` are touched -- our symbols, inserted by
    our patches. No upstream code is read or modified, and the edit is driven by
    a symbol match rather than line numbers so a patch reshuffle cannot make it
    silently cut the wrong thing.
    """
    f = src / "libretro.cpp"
    if not f.is_file():
        raise OracleError(f"expected {f} in the checkout")
    pat = re.compile(r"^\s*psxrecomp_\w+_cb_t\s+(g_psxrecomp_\w+)\s*=\s*NULL\s*;")
    seen: set = set()
    out: List[str] = []
    removed = 0
    for line in f.read_text(encoding="utf-8", errors="surrogateescape").splitlines(True):
        m = pat.match(line)
        if m:
            sym = m.group(1)
            if sym in seen:
                removed += 1
                continue
            seen.add(sym)
        out.append(line)
    if removed:
        f.write_text("".join(out), encoding="utf-8", errors="surrogateescape")
    return removed


def apply_patches(lay: Layout, pin: Dict[str, Any]) -> None:
    patch_bin = which("patch") or which("git")
    if not patch_bin:
        raise OracleError("need `patch` or `git` to apply the hook patches")
    for name in pin["patches"]:
        p = PATCH_DIR / name
        if not p.is_file():
            raise OracleError(f"missing patch: {p}")
        rc, out = capture(["patch", "-p1", "--forward", "--batch", "-i", str(p)],
                          cwd=lay.src)
        if rc != 0 and "Reversed (or previously applied)" not in out:
            raise OracleError(f"failed to apply {name}:\n{out}")
        log(f"  applied {name}" if rc == 0 else f"  {name} already applied")
    removed = _dedupe_our_globals(lay.src)
    if removed:
        log(f"  de-duplicated {removed} repeated hook global definition(s) "
            f"(cdcmd/wtrace patch overlap)")
    for orig in lay.src.rglob("*.orig"):
        orig.unlink()
    for rej in lay.src.rglob("*.rej"):
        log(f"  WARNING: reject file left behind: {rej}")


def cmd_setup(args: argparse.Namespace) -> int:
    pin = load_pin()
    lay = Layout(Path(args.root).expanduser() if args.root else None)
    lay.root.mkdir(parents=True, exist_ok=True)
    log(f"fetching {pin['upstream_url']} @ {pin['upstream_base'][:12]}")
    fetch_source(lay, pin, force=args.force)
    log("applying hook patches")
    apply_patches(lay, pin)
    write_manifest(lay, pin, "setup")
    log(f"ok: {lay.src}")
    return 0


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def build_core(lay: Layout, jobs: int) -> None:
    """Stage 1 -- upstream's libretro core as a static archive."""
    logf = lay.root / "build-core.log"
    run(["make", "platform=unix", "STATIC_LINKING=1", "HAVE_LIGHTREC=0",
         f"-j{jobs}"], cwd=lay.src, log_to=logf)
    # STATIC_LINKING=1 still names the artifact .so; it is an ar archive.
    produced = lay.src / "mednafen_psx_libretro.so"
    if not produced.is_file():
        raise OracleError(f"core build produced no artifact (see {logf})")
    shutil.copy2(produced, lay.core_lib)
    log(f"  staged {lay.core_lib.name} "
        f"({lay.core_lib.stat().st_size // (1 << 20)} MB)")


def _diagnose_frontend_failure(logf: Path) -> Optional[str]:
    """Turn the known missing-hook link failure into a verdict, not a log dump."""
    if not logf.is_file():
        return None
    text = logf.read_text(encoding="utf-8", errors="replace")
    hits = [(sym, what) for sym, what in MISSING_HOOKS if sym in text]
    if not hits:
        return None
    lines = [
        "psx-beetle failed on the KNOWN missing-hook blocker, not on anything new.",
        "",
        "  Unresolved, referenced by psxrecomp/runtime/src/beetle_libretro.cpp:",
    ]
    for sym, what in hits:
        lines.append(f"      {sym:<28} {what}")
    lines += [
        "",
        "  No committed patch under beetle/ defines these. Landing them means",
        "  writing code inside the Beetle checkout — which LICENSING.md §2 and",
        "  PSX/PRINCIPLES.md §11 forbid to anyone who touches the recompiler.",
        "",
        "  Resolutions:",
        "    (a) someone outside the implementation lands the hooks upstream-side",
        "        and regenerates the patches here;",
        "    (b) guard those three references in beetle_libretro.cpp behind a",
        "        feature macro so the committed set is self-sufficient. Costs the",
        "        rtrace / irq / decode-volume commands, which must then report",
        "        UNAVAILABLE rather than returning zeros. wtrace_dump is unaffected.",
        "",
        f"  full log: {logf}",
    ]
    return "\n".join(lines)


def build_frontend(lay: Layout, px: Path, jobs: int, reconfigure: bool) -> None:
    """Stage 2 -- our SDL + debug-server frontend, linked against the core."""
    if not lay.core_lib.is_file():
        raise OracleError(f"missing {lay.core_lib}; run `build` (stage 1) first")
    # runtime/CMakeLists.txt looks for ../beetle-psx/libmednafen_psx.a relative
    # to its own source dir. Provide that path without copying 12 MB around.
    link = px / "beetle-psx"
    if link.is_symlink() or link.exists():
        if link.is_symlink() and Path(os.readlink(link)) == lay.src:
            pass
        elif link.is_symlink():
            link.unlink()
            link.symlink_to(lay.src, target_is_directory=True)
        # a real directory there is a developer's own checkout; leave it alone
    else:
        link.symlink_to(lay.src, target_is_directory=True)

    if reconfigure and lay.build.exists():
        shutil.rmtree(lay.build)
    lay.build.mkdir(parents=True, exist_ok=True)
    run(["cmake", "-S", str(px / "runtime"), "-B", str(lay.build), "-G", "Ninja",
         "-DCMAKE_BUILD_TYPE=Release", "-DPSX_RECOMP_UI=OFF",
         "-DPSX_DEBUG_TOOLS=ON"])
    logf = lay.root / "build-frontend.log"
    rc = run(["ninja", "-C", str(lay.build), "psx-beetle"], check=False, log_to=logf)
    if rc != 0:
        verdict = _diagnose_frontend_failure(logf)
        raise OracleError(verdict or f"psx-beetle build failed (see {logf})")


def cmd_build(args: argparse.Namespace) -> int:
    pin = load_pin()
    lay = Layout(Path(args.root).expanduser() if args.root else None)
    if not lay.src.is_dir():
        raise OracleError("no checkout; run `setup` first")
    px = find_psxrecomp(args.psxrecomp)
    if not px:
        raise OracleError("no psxrecomp checkout found; pass --psxrecomp <path> "
                          "or set $PSXRECOMP_DIR")
    jobs = jobs_arg(args.jobs)
    if not args.frontend_only:
        log("stage 1: libretro core")
        build_core(lay, jobs)
    log("stage 2: psx-beetle frontend")
    build_frontend(lay, px, jobs, args.reconfigure)
    write_manifest(lay, pin, "build", psxrecomp=str(px))
    log(f"ok: {lay.built_binary}")
    return 0


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

def cmd_install(args: argparse.Namespace) -> int:
    pin = load_pin()
    lay = Layout(Path(args.root).expanduser() if args.root else None)
    if not lay.built_binary.is_file():
        raise OracleError(f"nothing built at {lay.built_binary}; run `build`")
    lay.app.mkdir(parents=True, exist_ok=True)
    shutil.copy2(lay.built_binary, lay.binary)
    lay.binary.chmod(0o755)
    write_manifest(lay, pin, "install")
    log(f"installed: {lay.binary}")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    rc = cmd_setup(args)
    if rc:
        return rc
    rc = cmd_build(args)
    if rc:
        return rc
    return cmd_install(args)


# ---------------------------------------------------------------------------
# manifest / status / run
# ---------------------------------------------------------------------------

def write_manifest(lay: Layout, pin: Dict[str, Any], stage: str,
                   **extra: Any) -> None:
    doc: Dict[str, Any] = {}
    if lay.manifest.is_file():
        try:
            doc = json.loads(lay.manifest.read_text())
        except Exception:
            doc = {}
    doc.update({
        "oracle": "beetle",
        "upstream_url": pin["upstream_url"],
        "upstream_base": pin["upstream_base"],
        "patches": {n: sha256_file(PATCH_DIR / n)[:16]
                    for n in pin["patches"] if (PATCH_DIR / n).is_file()},
        "port": pin.get("oracle_port", 4380),
        "stage": stage,
        "stamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    doc.update(extra)
    lay.manifest.parent.mkdir(parents=True, exist_ok=True)
    lay.manifest.write_text(json.dumps(doc, indent=2) + "\n")


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def oracle_ping(port: int, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.sendall(b'{"id":1,"cmd":"ping"}\n')
        buf = b""
        s.settimeout(timeout)
        while b"\n" not in buf and len(buf) < (1 << 16):
            c = s.recv(4096)
            if not c:
                break
            buf += c
        s.close()
        return json.loads(buf.split(b"\n")[0].decode())
    except Exception:
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status_doc(lay: Layout, pin: Dict[str, Any]) -> Dict[str, Any]:
    port = pin.get("oracle_port", 4380)
    doc: Dict[str, Any] = {
        "oracle": "beetle",
        "root": str(lay.root),
        "src": lay.src.is_dir(),
        "core_lib": lay.core_lib.is_file(),
        "built": lay.built_binary.is_file(),
        "installed": lay.binary.is_file(),
        "port": port,
        "listening": port_open(port),
        "known_blocker": [s for s, _ in MISSING_HOOKS],
    }
    if lay.manifest.is_file():
        try:
            doc["manifest"] = json.loads(lay.manifest.read_text())
        except Exception:
            pass
    if lay.pidfile.is_file():
        try:
            pid = int(lay.pidfile.read_text().strip())
            doc["pid"] = pid
            doc["running"] = pid_alive(pid)
        except Exception:
            pass
    if doc["listening"]:
        doc["ping"] = oracle_ping(port)
    return doc


def cmd_status(args: argparse.Namespace) -> int:
    pin = load_pin()
    lay = Layout(Path(args.root).expanduser() if args.root else None)
    doc = status_doc(lay, pin)
    if args.json:
        print(json.dumps(doc, indent=2))
        return 0
    log(f"beetle oracle @ {doc['root']}")
    for k in ("src", "core_lib", "built", "installed", "listening"):
        log(f"  {k:<12} {doc[k]}")
    if doc.get("ping"):
        log(f"  ping         {json.dumps(doc['ping'])[:120]}")
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    lay = Layout(Path(args.root).expanduser() if args.root else None)
    print(str(lay.binary if args.binary else lay.root))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    pin = load_pin()
    lay = Layout(Path(args.root).expanduser() if args.root else None)
    if not lay.binary.is_file():
        raise OracleError(f"not installed at {lay.binary}; run `all`")
    port = args.port or pin.get("oracle_port", 4380)
    if port_open(port):
        raise OracleError(f"something is already listening on {port}")
    if not args.bios:
        raise OracleError("--bios is required (path to the PSX BIOS image the "
                          "core should load)")
    cmd: List[str] = [str(lay.binary), str(Path(args.bios).expanduser().resolve())]
    if args.disc:
        cmd += ["--disc", str(Path(args.disc).expanduser().resolve())]
    cmd += ["--port", str(port)]
    if args.extra:
        cmd += [a for a in args.extra if a != "--"]
    env = dict(os.environ)
    env.setdefault("SDL_RENDER_DRIVER", "software")
    lay.root.mkdir(parents=True, exist_ok=True)
    logh = open(lay.logfile, "ab")
    log("$ " + " ".join(cmd))
    p = subprocess.Popen(cmd, stdout=logh, stderr=subprocess.STDOUT, env=env)
    lay.pidfile.write_text(str(p.pid))
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if p.poll() is not None:
            raise OracleError(f"oracle exited early (rc={p.returncode}); "
                              f"see {lay.logfile}")
        if oracle_ping(port):
            log(f"listening on {port} (pid {p.pid})")
            return 0
        time.sleep(0.5)
    log(f"warning: no ping within {args.wait}s; pid {p.pid}, log {lay.logfile}")
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    lay = Layout(Path(args.root).expanduser() if args.root else None)
    if not lay.pidfile.is_file():
        log("no pidfile; nothing started by this tool")
        return 0
    try:
        pid = int(lay.pidfile.read_text().strip())
    except Exception:
        lay.pidfile.unlink(missing_ok=True)
        return 0
    if pid_alive(pid):
        os.kill(pid, 15)
        for _ in range(40):
            if not pid_alive(pid):
                break
            time.sleep(0.1)
        if pid_alive(pid):
            os.kill(pid, 9)
        log(f"stopped {pid}")
    lay.pidfile.unlink(missing_ok=True)
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None,
                    help="install root (default: $RETCOMM_BEETLE_DIR, else "
                         "<retcomm data root>/oracle/beetle)")
    ap.add_argument("--psxrecomp", default=None,
                    help="psxrecomp checkout (default: $PSXRECOMP_DIR, else "
                         "autodetected from the current project)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="check host prerequisites")
    p.set_defaults(func=doctor)

    p = sub.add_parser("setup", help="fetch the pinned checkout + apply hook patches")
    p.add_argument("--force", action="store_true", help="re-fetch even if present")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("build", help="build the core, then the psx-beetle frontend")
    p.add_argument("--jobs", type=int, default=0)
    p.add_argument("--reconfigure", action="store_true")
    p.add_argument("--frontend-only", action="store_true",
                   help="skip stage 1 (core already staged)")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("install", help="stage the portable app dir")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("all", help="setup + build + install")
    p.add_argument("--force", action="store_true")
    p.add_argument("--jobs", type=int, default=0)
    p.add_argument("--reconfigure", action="store_true")
    p.add_argument("--frontend-only", action="store_true")
    p.set_defaults(func=cmd_all)

    p = sub.add_parser("start", help="launch the oracle")
    p.add_argument("--bios", default=None, help="PSX BIOS image for the core")
    p.add_argument("--disc", default=None, help="cue/bin/chd to boot")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--wait", type=float, default=45.0)
    p.add_argument("--extra", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="stop an oracle started by this tool")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("status", help="where it is and what state it is in")
    p.add_argument("--json", action="store_true",
                   help="machine-readable, for Retro Studio")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("path", help="print the install root")
    p.add_argument("--binary", action="store_true")
    p.set_defaults(func=cmd_path)

    args = ap.parse_args(argv)
    for attr, default in (("jobs", 0), ("reconfigure", False), ("force", False),
                          ("frontend_only", False), ("json", False),
                          ("binary", False), ("root", None), ("psxrecomp", None)):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    try:
        return args.func(args)
    except OracleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
