#!/usr/bin/env bash
# Rasterize assets/retcomm-studio.svg → PNG / ICO (and ICNS on macOS).
# Master PNG is 512x512 — linuxdeploy / AppImage reject oversized icons.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SVG="${ROOT}/assets/retcomm-studio.svg"
PNG="${ROOT}/assets/retcomm-studio.png"
ICO="${ROOT}/assets/retcomm-studio.ico"
ICNS="${ROOT}/assets/retcomm-studio.icns"
SIZE=512

if [[ ! -f "${SVG}" ]]; then
  echo "missing ${SVG}" >&2
  exit 1
fi

if command -v rsvg-convert >/dev/null 2>&1; then
  rsvg-convert -w "${SIZE}" -h "${SIZE}" "${SVG}" -o "${PNG}"
elif command -v magick >/dev/null 2>&1; then
  magick -background none "${SVG}" -resize "${SIZE}x${SIZE}" "${PNG}"
elif command -v convert >/dev/null 2>&1; then
  convert -background none "${SVG}" -resize "${SIZE}x${SIZE}" "${PNG}"
elif command -v inkscape >/dev/null 2>&1; then
  inkscape "${SVG}" -w "${SIZE}" -h "${SIZE}" -o "${PNG}"
else
  python3 - "${PNG}" "${SIZE}" <<'PY'
import struct, zlib, sys
from pathlib import Path

def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

out = Path(sys.argv[1])
w = h = int(sys.argv[2])
# Teal-on-dark placeholder approximating the SVG.
rows = []
for y in range(h):
    row = bytearray([0])
    for x in range(w):
        # rounded-ish dark card
        edge = min(x, y, w - 1 - x, h - 1 - y)
        if edge < int(w * 0.04):
            r, g, b = 13, 18, 26
        else:
            r, g, b = 26, 35, 50
        # accent ring + brackets (rough)
        cx, cy = w / 2, h / 2
        if abs(x - (cx - w * 0.12)) < w * 0.03 and abs(y - cy) < h * 0.18:
            r, g, b = 46, 212, 191
        if abs(x - (cx + w * 0.12)) < w * 0.03 and abs(y - cy) < h * 0.18:
            r, g, b = 46, 212, 191
        if abs(x - cx) < w * 0.06 and abs(y - cy) < h * 0.04:
            r, g, b = 94, 234, 212
        row += bytes((r, g, b, 255))
    rows.append(bytes(row))
raw = b"".join(rows)
comp = zlib.compress(raw, 9)
ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", comp) + chunk(b"IEND", b"")
out.write_bytes(png)
print(f"wrote placeholder {out}", file=sys.stderr)
PY
fi

if command -v magick >/dev/null 2>&1; then
  magick "${PNG}" -define icon:auto-resize=256,128,64,48,32,16 "${ICO}"
elif command -v convert >/dev/null 2>&1; then
  convert "${PNG}" -define icon:auto-resize=256,128,64,48,32,16 "${ICO}"
else
  python3 - "${PNG}" "${ICO}" <<'PY'
# Minimal multi-size ICO from PNG via Pillow if present; else copy single PNG as ICO-ish.
import sys
from pathlib import Path
png, ico = Path(sys.argv[1]), Path(sys.argv[2])
try:
    from PIL import Image
    im = Image.open(png).convert("RGBA")
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    im.save(ico, format="ICO", sizes=sizes)
except Exception:
    # Fallback: write PNG bytes with .ico name (Windows accepts PNG-in-ICO poorly;
    # CI installs imagemagick / pillow). Still produce a file for packaging probes.
    ico.write_bytes(png.read_bytes())
    print("warning: wrote PNG bytes as ICO fallback", file=sys.stderr)
PY
fi

if [[ "$(uname -s)" == "Darwin" ]] && command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
  ICONSET="${ROOT}/assets/retcomm-studio.iconset"
  rm -rf "${ICONSET}"
  mkdir -p "${ICONSET}"
  sips -z 16 16     "${PNG}" --out "${ICONSET}/icon_16x16.png" >/dev/null
  sips -z 32 32     "${PNG}" --out "${ICONSET}/icon_16x16@2x.png" >/dev/null
  sips -z 32 32     "${PNG}" --out "${ICONSET}/icon_32x32.png" >/dev/null
  sips -z 64 64     "${PNG}" --out "${ICONSET}/icon_32x32@2x.png" >/dev/null
  sips -z 128 128   "${PNG}" --out "${ICONSET}/icon_128x128.png" >/dev/null
  sips -z 256 256   "${PNG}" --out "${ICONSET}/icon_128x128@2x.png" >/dev/null
  sips -z 256 256   "${PNG}" --out "${ICONSET}/icon_256x256.png" >/dev/null
  sips -z 512 512   "${PNG}" --out "${ICONSET}/icon_256x256@2x.png" >/dev/null
  sips -z 512 512   "${PNG}" --out "${ICONSET}/icon_512x512.png" >/dev/null
  cp "${PNG}" "${ICONSET}/icon_512x512@2x.png"
  iconutil -c icns "${ICONSET}" -o "${ICNS}"
  rm -rf "${ICONSET}"
fi

echo "icons: ${PNG} ${ICO}${ICNS:+ ${ICNS}}"
