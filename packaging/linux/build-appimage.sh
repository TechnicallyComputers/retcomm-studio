#!/usr/bin/env bash
# Assemble RetComM Studio AppImage from a PyInstaller onedir.
#
# Usage:
#   packaging/linux/build-appimage.sh <pyinstaller-onedir> <version> [arch]
# Example:
#   packaging/linux/build-appimage.sh dist/RetComM-Studio 0.1.0 x86_64
set -euo pipefail

BUNDLE="${1:?pyinstaller onedir}"
VERSION="${2:?version}"
ARCH="${3:-$(uname -m)}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT_DIR="${ROOT}/dist"
APPDIR="${OUT_DIR}/RetComM-Studio.AppDir"
TOOL_DIR="${OUT_DIR}/tools"
APP_NAME="RetComM-Studio"

rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/bin" "${TOOL_DIR}" "${OUT_DIR}"

if [[ ! -d "${BUNDLE}" ]]; then
  echo "error: missing bundle ${BUNDLE}" >&2
  exit 1
fi
if [[ ! -x "${BUNDLE}/${APP_NAME}" && ! -f "${BUNDLE}/${APP_NAME}" ]]; then
  echo "error: missing ${BUNDLE}/${APP_NAME}" >&2
  exit 1
fi

cp -a "${BUNDLE}/." "${APPDIR}/usr/bin/"
chmod +x "${APPDIR}/usr/bin/${APP_NAME}" || true

install -m 755 "${ROOT}/packaging/linux/AppRun.in" "${APPDIR}/AppRun"
install -m 644 "${ROOT}/packaging/linux/retcomm-studio.desktop" \
  "${APPDIR}/retcomm-studio.desktop"
# linuxdeploy / appimagetool expect Icon= name without path; also place PNG.
if [[ -f "${ROOT}/assets/retcomm-studio.png" ]]; then
  install -m 644 "${ROOT}/assets/retcomm-studio.png" "${APPDIR}/retcomm-studio.png"
  mkdir -p "${APPDIR}/usr/share/icons/hicolor/512x512/apps"
  install -m 644 "${ROOT}/assets/retcomm-studio.png" \
    "${APPDIR}/usr/share/icons/hicolor/512x512/apps/retcomm-studio.png"
fi

# Prefer appimagetool; fall back to downloading a pinned continuous build.
APPIMAGETOOL=""
if command -v appimagetool >/dev/null 2>&1; then
  APPIMAGETOOL="$(command -v appimagetool)"
else
  case "${ARCH}" in
    x86_64|amd64) AI_ARCH=x86_64 ;;
    aarch64|arm64) AI_ARCH=aarch64 ;;
    *) AI_ARCH=x86_64 ;;
  esac
  URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${AI_ARCH}.AppImage"
  APPIMAGETOOL="${TOOL_DIR}/appimagetool-${AI_ARCH}.AppImage"
  if [[ ! -x "${APPIMAGETOOL}" ]]; then
    curl -fsSL -o "${APPIMAGETOOL}" "${URL}"
    chmod +x "${APPIMAGETOOL}"
  fi
fi

export VERSION
OUT_APPIMAGE="${OUT_DIR}/RetComM-Studio-linux-${ARCH}.AppImage"
rm -f "${OUT_APPIMAGE}"
# ARCH for appimagetool output naming; we rename to a stable name afterward.
ARCH="${ARCH}" "${APPIMAGETOOL}" "${APPDIR}" "${OUT_APPIMAGE}.tmp"
# appimagetool may ignore our outfile name; locate the newest AppImage and rename.
if [[ -f "${OUT_APPIMAGE}.tmp" ]]; then
  mv -f "${OUT_APPIMAGE}.tmp" "${OUT_APPIMAGE}"
elif [[ -f "${OUT_DIR}/${APP_NAME}-${ARCH}.AppImage" ]]; then
  mv -f "${OUT_DIR}/${APP_NAME}-${ARCH}.AppImage" "${OUT_APPIMAGE}"
else
  FOUND="$(find "${OUT_DIR}" -maxdepth 1 -name '*.AppImage' -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2- || true)"
  if [[ -n "${FOUND}" && -f "${FOUND}" ]]; then
    mv -f "${FOUND}" "${OUT_APPIMAGE}"
  else
    echo "error: appimagetool did not produce an AppImage" >&2
    ls -lah "${OUT_DIR}" >&2 || true
    exit 1
  fi
fi
chmod +x "${OUT_APPIMAGE}"
ls -lah "${OUT_APPIMAGE}"
echo "Wrote ${OUT_APPIMAGE} (version ${VERSION})"
