#!/usr/bin/env bash
# Cross-compile a Windows x64 .exe for a snesrecomp title from Linux.
#
# The SNES counterpart to build_windows_mingw.sh. Kept as its own script rather
# than a --platform flag on that one: the PSX build cross-compiles TWO
# emitters (psxrecomp-game / psxrecomp-bios), stages OpenBIOS, and drives
# PSX_NETPLAY. None of that exists on a cartridge. What is left after removing
# it is short enough that sharing the file would cost more in conditionals than
# it saves.
#
# Usage:
#   bash build_windows_mingw_snes.sh --root ~/GitHub/MetalWarriorsSNESRecomp
#   bash build_windows_mingw_snes.sh --last --package
#
# Studio CLI equivalent:
#   python3 -m project_studio --platform snes build mingw --root … [--package]
#
# Prerequisites (Arch / CachyOS):
#   pacman -S --needed mingw-w64-gcc mingw-w64-sdl2 cmake ninja zip
#
# SDL: the backend is chosen from what the MinGW sysroot actually has. SNES
# builds default to SDL3 natively, but distro MinGW packages are usually SDL2
# only, and configuring for a backend the sysroot lacks fails deep inside
# CMake with a message about neither.
#
# Writes:
#   <root>/<build-dir>/<Product>.exe
#   dist/<prefix>-windows-x64-mingw.zip   (with --package)
#   Final machine line: MINGW_ZIP=/abs/path.zip  (when a zip was produced)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOOLKIT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INDEX_JSON="${TOOLKIT}/project_studio_repos_snes.json"
TOOLCHAIN_DEFAULT="${SCRIPT_DIR}/toolchain-mingw-w64.cmake"

ROOT=""
NAME=""
USE_LAST=0
BUILD_DIR="build-mingw"
DO_PACKAGE=0
PACKAGE_ONLY=0
DRY_RUN=0
JOBS="$(nproc 2>/dev/null || echo 4)"
SDL_BACKEND=""

die() { echo "error: $*" >&2; exit 1; }

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)        ROOT="$2"; shift 2 ;;
    --name)        NAME="$2"; shift 2 ;;
    --last)        USE_LAST=1; shift ;;
    --build-dir)   BUILD_DIR="$2"; shift 2 ;;
    --jobs)        JOBS="$2"; shift 2 ;;
    --sdl)         SDL_BACKEND="$2"; shift 2 ;;
    --package)     DO_PACKAGE=1; shift ;;
    --package-only) PACKAGE_ONLY=1; DO_PACKAGE=1; shift ;;
    --dry-run)     DRY_RUN=1; shift ;;
    -h|--help)     usage 0 ;;
    *) die "unknown flag: $1 (try --help)" ;;
  esac
done

# ---- resolve the project root ------------------------------------------------
resolve_from_index() {
  [[ -f "${INDEX_JSON}" ]] || return 1
  python3 - "$1" "$2" <<'PY'
import json, sys
want_last, want_name = sys.argv[1] == "1", sys.argv[2]
try:
    doc = json.load(open(sys.argv[3] if len(sys.argv) > 3 else
                         __import__("os").environ["INDEX_JSON"]))
except Exception:
    sys.exit(1)
repos = doc.get("repos") or []
if want_last:
    last = (doc.get("last") or "").strip()
    if last:
        print(last); sys.exit(0)
if want_name:
    needle = want_name.lower()
    for r in repos:
        hay = f"{r.get('name','')} {r.get('path','')}".lower()
        if needle in hay:
            print(r.get("path", "")); sys.exit(0)
sys.exit(1)
PY
}

if [[ -z "${ROOT}" ]]; then
  INDEX_JSON="${INDEX_JSON}" export INDEX_JSON
  ROOT="$(resolve_from_index "${USE_LAST}" "${NAME}" || true)"
fi
[[ -n "${ROOT}" ]] || die "no project selected (pass --root, --name or --last)"
ROOT="$(cd "${ROOT}" && pwd)" || die "not a directory: ${ROOT}"
[[ -f "${ROOT}/CMakeLists.txt" ]] || die "no CMakeLists.txt under ${ROOT}"
[[ -f "${ROOT}/snesrecomp/runner/runner.cmake" ]] || \
  die "snesrecomp submodule missing under ${ROOT} (git submodule update --init --recursive)"

OUT_DIR="${ROOT}/${BUILD_DIR}"

# ---- host prerequisites ------------------------------------------------------
need_host() {
  command -v "$1" >/dev/null 2>&1 || die "missing $1 — $2"
}
if [[ "${PACKAGE_ONLY}" -eq 0 ]]; then
  need_host x86_64-w64-mingw32-gcc "pacman -S --needed mingw-w64-gcc"
  need_host cmake "pacman -S --needed cmake"
  need_host ninja "pacman -S --needed ninja"
fi

# Pick the SDL the MinGW sysroot actually carries, unless told.
mingw_pkgconfig() {
  if command -v x86_64-w64-mingw32-pkg-config >/dev/null 2>&1; then
    x86_64-w64-mingw32-pkg-config "$@"
  else
    PKG_CONFIG_LIBDIR="/usr/x86_64-w64-mingw32/lib/pkgconfig" pkg-config "$@"
  fi
}
if [[ -z "${SDL_BACKEND}" && "${PACKAGE_ONLY}" -eq 0 ]]; then
  if mingw_pkgconfig --exists sdl3 2>/dev/null; then
    SDL_BACKEND="SDL3"
  elif mingw_pkgconfig --exists sdl2 2>/dev/null; then
    SDL_BACKEND="SDL2"
  else
    die "no MinGW SDL found — pacman -S --needed mingw-w64-sdl2"
  fi
  echo "sdl backend: ${SDL_BACKEND} (from the MinGW sysroot)" >&2
fi

# ---- the CMake target --------------------------------------------------------
# A SNES project names its target after project(), unlike psx-runtime. Read it
# rather than guessing, so the .exe is found no matter what the title is called.
TARGET="$(sed -n 's/^[[:space:]]*project([[:space:]]*\([A-Za-z0-9_.+-]*\).*/\1/p' \
          "${ROOT}/CMakeLists.txt" | head -n1)"
[[ -n "${TARGET}" ]] || die "could not read project() from ${ROOT}/CMakeLists.txt"

# ---- configure + build -------------------------------------------------------
if [[ "${PACKAGE_ONLY}" -eq 0 ]]; then
  TOOLCHAIN="${TOOLCHAIN_DEFAULT}"
  [[ -f "${ROOT}/snesrecomp/cmake/toolchain-mingw-w64.cmake" ]] && \
    TOOLCHAIN="${ROOT}/snesrecomp/cmake/toolchain-mingw-w64.cmake"
  [[ -f "${TOOLCHAIN}" ]] || die "missing toolchain file ${TOOLCHAIN}"

  # Both halves of "regen has run" are checked here, by name. Generated output
  # is gitignored, so a fresh clone (or a tree whose regen was interrupted) has
  # neither -- and without this the cross-build fails a minute later as
  # `fatal error: funcs.h: No such file or directory` from inside a generated
  # C file, which reads like a broken toolchain rather than a missing step.
  if [[ ! -d "${ROOT}/src/gen" ]] || [[ -z "$(ls -A "${ROOT}/src/gen" 2>/dev/null)" ]]; then
    die "src/gen/ is empty — run tools/regen.sh (or Studio: Regenerate C from ROM) first.
  A cartridge build links the generated C; there is nothing to cross-compile without it."
  fi
  if [[ ! -f "${ROOT}/recomp/funcs.h" ]]; then
    die "recomp/funcs.h is missing — run tools/regen.sh (or Studio: Regenerate C
  from ROM) first. It is generated beside src/gen/ and gitignored, so a clone
  or an interrupted regen leaves the tree looking half-built."
  fi

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "DRY: cmake -S ${ROOT} -B ${OUT_DIR} -G Ninja -DCMAKE_TOOLCHAIN_FILE=${TOOLCHAIN} -DCMAKE_BUILD_TYPE=Release -DSNESRECOMP_SDL_BACKEND=${SDL_BACKEND}" >&2
    echo "DRY: cmake --build ${OUT_DIR} --target ${TARGET} -j${JOBS}" >&2
  else
    cmake -S "${ROOT}" -B "${OUT_DIR}" -G Ninja \
      -DCMAKE_TOOLCHAIN_FILE="${TOOLCHAIN}" \
      -DCMAKE_BUILD_TYPE=Release \
      -DSNESRECOMP_SDL_BACKEND="${SDL_BACKEND}" >&2
    cmake --build "${OUT_DIR}" --target "${TARGET}" -j"${JOBS}" >&2
  fi
fi

EXE="${OUT_DIR}/${TARGET}.exe"
if [[ "${DRY_RUN}" -eq 0 && ! -f "${EXE}" ]]; then
  cand="$(find "${OUT_DIR}" -maxdepth 1 -type f -name '*.exe' 2>/dev/null | head -n1 || true)"
  [[ -n "${cand}" ]] && EXE="${cand}"
fi
[[ "${DRY_RUN}" -eq 1 || -f "${EXE}" ]] || die "no .exe produced under ${OUT_DIR}"

# ---- package -----------------------------------------------------------------
if [[ "${DO_PACKAGE}" -eq 1 && "${DRY_RUN}" -eq 0 ]]; then
  BUNDLER="${SCRIPT_DIR}/bundle_mingw_dlls.sh"
  if [[ -f "${BUNDLER}" ]]; then
    bash "${BUNDLER}" --exe "${EXE}" --dest "${OUT_DIR}" \
      --search-dir "/usr/x86_64-w64-mingw32/bin" >&2 || \
      echo "warn: DLL bundling reported a problem; the zip may not run on a clean host" >&2
  else
    echo "warn: ${BUNDLER} not found — SDL/GCC DLLs will not be bundled" >&2
  fi

  VERSION="$(tr -d ' \t\r\n' < "${ROOT}/VERSION" 2>/dev/null || echo dev)"
  PREFIX="$(printf '%s' "${TARGET}" | tr '[:upper:]' '[:lower:]')"
  DIST="${ROOT}/dist"
  mkdir -p "${DIST}"
  ZIP="${DIST}/${PREFIX}-${VERSION}-windows-x64-mingw.zip"
  rm -f "${ZIP}"
  ( cd "${OUT_DIR}" && zip -qr "${ZIP}" . \
      -x '*.o' -x 'CMakeFiles/*' -x '*.ninja*' -x 'CMakeCache.txt' ) \
    || die "zip failed"
  echo "MINGW_ZIP=${ZIP}"
fi

echo "mingw build OK: ${EXE}" >&2
