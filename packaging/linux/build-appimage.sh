#!/usr/bin/env bash
# Assemble RetComM Studio AppImage from a CMake install prefix.
#
# Usage:
#   packaging/linux/build-appimage.sh <install-prefix> <version> [arch]
set -euo pipefail

PREFIX="${1:?install prefix}"
VERSION="${2:?version}"
ARCH="${3:-$(uname -m)}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT_DIR="${ROOT}/dist"
APPDIR="${OUT_DIR}/RetComM-Studio.AppDir"
TOOL_DIR="${OUT_DIR}/tools"
APP_NAME="RetComM-Studio"

rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr" "${TOOL_DIR}" "${OUT_DIR}"

if [[ ! -d "${PREFIX}" ]]; then
  echo "error: missing install prefix ${PREFIX}" >&2
  exit 1
fi
if [[ ! -x "${PREFIX}/bin/${APP_NAME}" && ! -f "${PREFIX}/bin/${APP_NAME}" ]]; then
  echo "error: missing ${PREFIX}/bin/${APP_NAME}" >&2
  exit 1
fi

cp -a "${PREFIX}/." "${APPDIR}/usr/"
chmod +x "${APPDIR}/usr/bin/${APP_NAME}" || true

# Also place fonts/toolkit next to the binary for SDL_GetBasePath resolution.
if [[ -d "${APPDIR}/usr/share/retcomm-studio/fonts" ]]; then
  mkdir -p "${APPDIR}/usr/bin/fonts"
  cp -a "${APPDIR}/usr/share/retcomm-studio/fonts/." "${APPDIR}/usr/bin/fonts/"
fi
if [[ -d "${APPDIR}/usr/share/retcomm-studio/toolkit" ]]; then
  mkdir -p "${APPDIR}/usr/bin/toolkit"
  cp -a "${APPDIR}/usr/share/retcomm-studio/toolkit/." "${APPDIR}/usr/bin/toolkit/"
fi
if [[ -d "${APPDIR}/usr/share/retcomm-studio/assets" ]]; then
  mkdir -p "${APPDIR}/usr/bin/assets"
  cp -a "${APPDIR}/usr/share/retcomm-studio/assets/." "${APPDIR}/usr/bin/assets/"
fi
cp -f "${ROOT}/VERSION" "${APPDIR}/usr/bin/VERSION" || true

install -m 755 "${ROOT}/packaging/linux/AppRun.in" "${APPDIR}/AppRun"
install -m 644 "${ROOT}/packaging/linux/retcomm-studio.desktop" \
  "${APPDIR}/retcomm-studio.desktop"
if [[ -f "${ROOT}/assets/retcomm-studio.png" ]]; then
  install -m 644 "${ROOT}/assets/retcomm-studio.png" "${APPDIR}/retcomm-studio.png"
  mkdir -p "${APPDIR}/usr/share/icons/hicolor/512x512/apps"
  install -m 644 "${ROOT}/assets/retcomm-studio.png" \
    "${APPDIR}/usr/share/icons/hicolor/512x512/apps/retcomm-studio.png"
fi

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

OUT="${OUT_DIR}/RetComM-Studio-linux-${ARCH}.AppImage"
rm -f "${OUT}"
ARCH="${ARCH}" VERSION="${VERSION}" "${APPIMAGETOOL}" "${APPDIR}" "${OUT}"
chmod +x "${OUT}"
ls -lah "${OUT}"
echo "Wrote ${OUT} (version ${VERSION})"
