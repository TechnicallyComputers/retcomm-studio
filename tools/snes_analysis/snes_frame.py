"""Shared library for the SNES asset tools.

Owns the three things every tool in this directory needs, so no tool grows a
private copy that can drift:

  * DebugConn — the snesrecomp debug-server wire protocol (one \\n-terminated
    command line out, one \\n-terminated JSON line back; binary payloads ride
    inside the JSON as hex blobs).
  * Bundle IO — the versioned on-disk capture format. Capture writes it once;
    decode and attribution read it offline, so a bug filed with a bundle
    attached needs nothing still running.
  * Pixel helpers — RGB555 conversion and a dependency-free PNG writer, so
    the tools run on a bare python3 the way the psx_analysis set does.

Division of labour (mirrors tools/psx_analysis/psx_gpu_frame.py): these tools
own the protocol and the decode; Retro Studio is a viewer and a launcher.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import zlib


def _force_utf8_streams() -> None:
    """Let these tools print the arrows and box glyphs they format with.

    The Windows console default is cp1252, which cannot encode U+2192 or the
    box-drawing characters the tables use, so one such line would abort the
    tool with UnicodeEncodeError. Studio sets PYTHONUTF8 for everything it
    spawns; a developer running these straight from a terminal gets no such
    help. It lives here for the same reason DebugConn does -- every tool in
    this directory goes through this module, so none needs a private copy.

    Best effort: a stream that is already UTF-8, or is not a reconfigurable
    text wrapper, is left exactly as it is.
    """
    for _stream in (sys.stdout, sys.stderr):
        _enc = (getattr(_stream, "encoding", "") or "").lower().replace("-", "")
        if _enc.startswith("utf8"):
            continue
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_streams()

# MetalWarriorsSNESRecomp uses 4380; snesrecomp's own harnesses use 4377
# (recomp) / 4378 (oracle). There is no single reserved port — pass --port.
DEFAULT_PORT = 4377

BUNDLE_FORMAT = "snes-frame"
BUNDLE_VERSION = 2

RECV_BUFFER = 262144


class DebugError(RuntimeError):
    pass


class DebugConn:
    """Persistent line/JSON connection to a running snesrecomp debug server."""

    def __init__(self, port: int = DEFAULT_PORT, host: str = "127.0.0.1",
                 timeout: float = 30.0):
        self.host = host
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect((host, port))
        except OSError as exc:
            raise DebugError(
                f"cannot connect to {host}:{port} — is the game running, and "
                f"was it built with SNESRECOMP_ENABLE_TRACE=ON? ({exc})"
            ) from exc
        self._buf = b""

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _recv_line(self) -> str:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(RECV_BUFFER)
            if not chunk:
                raise DebugError("server closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("utf-8", errors="replace").strip()

    def query(self, cmd: str) -> dict:
        """Send one command, return its JSON reply (banner lines skipped)."""
        self._sock.sendall((cmd + "\n").encode())
        while True:
            line = self._recv_line()
            if not line:
                continue
            reply = json.loads(line)
            # Connect-time banner: {"server": ...} greeting, not a reply.
            if isinstance(reply, dict) and "server" in reply and cmd not in ("info",):
                continue
            if isinstance(reply, dict) and "error" in reply:
                raise DebugError(f"{cmd.split()[0]}: {reply['error']}")
            return reply

    def query_hex(self, cmd: str, key: str = "hex") -> bytes:
        """A command whose reply carries a hex blob; returns the raw bytes."""
        return bytes.fromhex(self.query(cmd)[key])


# ── bundle IO ────────────────────────────────────────────────────────────────

def write_bundle(outdir: str, tag: str, manifest: dict,
                 blobs: dict[str, bytes]) -> str:
    """Write <tag>.json + the raw blobs it names. Returns the manifest path.

    The manifest names every blob file it owns, so a reader never globs — a
    stray file in the directory is not silently treated as part of a capture.
    """
    os.makedirs(outdir, exist_ok=True)
    manifest = dict(manifest)
    manifest["format"] = BUNDLE_FORMAT
    manifest["version"] = BUNDLE_VERSION
    manifest["blobs"] = {}
    for name, data in blobs.items():
        fname = f"{tag}.{name}.bin"
        with open(os.path.join(outdir, fname), "wb") as f:
            f.write(data)
        manifest["blobs"][name] = {"file": fname, "size": len(data)}
    path = os.path.join(outdir, f"{tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    return path


def read_bundle(manifest_path: str) -> tuple[dict, dict[str, bytes]]:
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    if manifest.get("format") != BUNDLE_FORMAT:
        raise DebugError(f"{manifest_path}: not a {BUNDLE_FORMAT} bundle")
    if manifest.get("version", 0) > BUNDLE_VERSION:
        raise DebugError(
            f"{manifest_path}: bundle version {manifest['version']} is newer "
            f"than this tool ({BUNDLE_VERSION}) — update the tools")
    base = os.path.dirname(os.path.abspath(manifest_path))
    blobs = {}
    for name, entry in manifest.get("blobs", {}).items():
        with open(os.path.join(base, entry["file"]), "rb") as f:
            data = f.read()
        if len(data) != entry["size"]:
            raise DebugError(f"{entry['file']}: size mismatch — truncated bundle?")
        blobs[name] = data
    return manifest, blobs


# ── pixels ───────────────────────────────────────────────────────────────────

def rgb555_to_rgb888(word: int) -> tuple[int, int, int]:
    """SNES CGRAM word (BGR555, little-endian already assembled) → RGB888.

    5-bit channels expand with the standard (c << 3) | (c >> 2) replication so
    that 31 maps to 255 and 0 to 0 — a plain <<3 leaves the top of the range
    dark and every dump looks slightly wrong next to an emulator's.
    """
    r = word & 0x1F
    g = (word >> 5) & 0x1F
    b = (word >> 10) & 0x1F
    return ((r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2))


def write_png(path: str, width: int, height: int,
              rgba: bytes) -> None:
    """Minimal RGBA PNG writer (stdlib only, like the rest of the toolset)."""
    if len(rgba) != width * height * 4:
        raise ValueError(f"pixel buffer is {len(rgba)} bytes, "
                         f"expected {width * height * 4}")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4]
                   for y in range(height))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                           8, 6, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        f.write(chunk(b"IEND", b""))


def parse_hex_field(value) -> int:
    """The debug server emits numbers as both 123 and "0x7b"; accept either."""
    if isinstance(value, str):
        return int(value, 0)
    return int(value)
