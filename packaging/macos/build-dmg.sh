#!/usr/bin/env bash
# Wrap PyInstaller onedir into RetComM Studio.app + DMG.
#
# Usage:
#   packaging/macos/build-dmg.sh <pyinstaller-onedir> <version> [arch]
set -euo pipefail

BUNDLE="${1:?pyinstaller onedir}"
VERSION="${2:?version}"
ARCH="${3:-$(uname -m)}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT_DIR="${ROOT}/dist"
APP_NAME="RetComM Studio"
APP="${OUT_DIR}/${APP_NAME}.app"
DMG="${OUT_DIR}/RetComM-Studio-macos-${ARCH}.dmg"
BIN_NAME="RetComM-Studio"

rm -rf "${APP}"
mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources"

if [[ ! -d "${BUNDLE}" ]]; then
  echo "error: missing ${BUNDLE}" >&2
  exit 1
fi

# Place the whole onedir under MacOS/ so relative _internal / toolkit resolve.
cp -a "${BUNDLE}/." "${APP}/Contents/MacOS/"
chmod +x "${APP}/Contents/MacOS/${BIN_NAME}" || true

# Thin launcher so CFBundleExecutable is a small wrapper.
cat > "${APP}/Contents/MacOS/RetComM-Studio-launch" <<'EOF'
#!/bin/bash
HERE="$(cd "$(dirname "$0")" && pwd)"
export RETCOMM_STUDIO_FROZEN=1
if [[ -f "${HERE}/VERSION" ]]; then
  export RETCOMM_STUDIO_VERSION="$(tr -d '[:space:]' < "${HERE}/VERSION")"
fi
if [[ -d "${HERE}/toolkit" ]]; then
  export RETCOMM_STUDIO_TOOLKIT="${HERE}/toolkit"
fi
if [[ -d "${HERE}/assets" ]]; then
  export RETCOMM_STUDIO_ASSETS="${HERE}/assets"
fi
exec "${HERE}/RetComM-Studio" "$@"
EOF
chmod +x "${APP}/Contents/MacOS/RetComM-Studio-launch"

sed "s|@VERSION@|${VERSION}|g" "${ROOT}/packaging/macos/Info.plist.in" \
  > "${APP}/Contents/Info.plist"

# Icon
if [[ -f "${ROOT}/assets/retcomm-studio.icns" ]]; then
  install -m 644 "${ROOT}/assets/retcomm-studio.icns" \
    "${APP}/Contents/Resources/AppIcon.icns"
elif [[ -f "${ROOT}/assets/retcomm-studio.png" ]] && command -v iconutil >/dev/null 2>&1; then
  ICONSET="${OUT_DIR}/retcomm-studio.iconset"
  rm -rf "${ICONSET}"
  mkdir -p "${ICONSET}"
  PNG="${ROOT}/assets/retcomm-studio.png"
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
  iconutil -c icns "${ICONSET}" -o "${APP}/Contents/Resources/AppIcon.icns"
  rm -rf "${ICONSET}"
fi

rm -f "${DMG}"
hdiutil create -volname "${APP_NAME}" -srcfolder "${APP}" -ov -format UDZO "${DMG}"
ls -lah "${DMG}"
echo "Wrote ${DMG} (version ${VERSION})"
