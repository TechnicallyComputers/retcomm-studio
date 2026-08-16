#!/usr/bin/env bash
# Cross-compile a Windows x64 Release binary for any Studio-indexed recomp
# title from Linux (MinGW-w64) — local smoke builds without GitHub CI.
#
# Usage (from anywhere):
#   bash tools/new_project_layout/scripts/build_windows_mingw.sh --root ~/GitHub/TwistedMetal4Recomp
#   bash tools/new_project_layout/scripts/build_windows_mingw.sh --name "Twisted Metal"
#   bash tools/new_project_layout/scripts/build_windows_mingw.sh --last
#   bash tools/new_project_layout/scripts/build_windows_mingw.sh --name TM4 --setup-host --package
#   bash tools/new_project_layout/scripts/build_windows_mingw.sh --root … --package-only
#
# Studio CLI equivalent:
#   python3 tools/new_project_layout/migrate_project.py build mingw --root …
#   python3 tools/new_project_layout/migrate_project.py build mingw --name "Twisted Metal"
#   python3 tools/new_project_layout/migrate_project.py build mingw --root … --package-only
#
# Modes:
#   (default) Full playable .exe — needs generated game C already present
#             (run Studio `build generate` / host `build compile` first).
#   --setup-host  CI-parity setup wizard host (FORCE_SETUP_HOST; no prebuilt
#                 generated game C). Optional --package → scripts/package_setup_release.sh.
#   --package-only  Skip configure/build; package an existing MinGW build dir into
#                   dist/*.zip (Studio Bundle+Export). Implies --package.
#
# Prerequisites (Arch / CachyOS):
#   pacman -S --needed mingw-w64-gcc mingw-w64-sdl2 cmake ninja zip
#
# Writes:
#   <root>/<build-dir>/<Product>.exe
#   dist/<prefix>-<VERSION>-windows-x64-mingw.zip  (with --package / --package-only)
#   Final machine line: MINGW_ZIP=/abs/path.zip (when a zip was produced)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOOLKIT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INDEX_JSON="${TOOLKIT}/project_studio_repos.json"

ROOT=""
NAME=""
USE_LAST=0
BUILD_DIR=""
DO_PACKAGE=0
PACKAGE_ONLY=0
SETUP_HOST=0
STATIC_RUNTIME=1
ENSURE=0
JOBS="$(nproc 2>/dev/null || echo 4)"
ARTIFACT_TAG="windows-x64-mingw"
TRIPLE="x86_64-w64-mingw32"
RUNTIME_BIN_DIR="/usr/${TRIPLE}/bin"
TOOLCHAIN=""
DRY_RUN=0
EXTRA_CMAKE=()

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --root) ROOT="${2:?}"; shift 2 ;;
    --name) NAME="${2:?}"; shift 2 ;;
    --last) USE_LAST=1; shift ;;
    --build-dir) BUILD_DIR="${2:?}"; shift 2 ;;
    --package) DO_PACKAGE=1; shift ;;
    --package-only) PACKAGE_ONLY=1; DO_PACKAGE=1; shift ;;
    --no-package) DO_PACKAGE=0; PACKAGE_ONLY=0; shift ;;
    --setup-host) SETUP_HOST=1; shift ;;
    --dynamic) STATIC_RUNTIME=0; shift ;;
    --static) STATIC_RUNTIME=1; shift ;;
    --ensure) ENSURE=1; shift ;;
    --jobs) JOBS="${2:?}"; shift 2 ;;
    --artifact-tag) ARTIFACT_TAG="${2:?}"; shift 2 ;;
    --runtime-bin-dir) RUNTIME_BIN_DIR="${2:?}"; shift 2 ;;
    --toolchain) TOOLCHAIN="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --extra)
      # shellcheck disable=SC2206
      EXTRA_CMAKE+=(${2:?})
      shift 2
      ;;
    *)
      echo "unknown arg: $1" >&2
      usage 2
      ;;
  esac
done

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: missing required tool: $1" >&2
    exit 1
  fi
}

resolve_root() {
  if [[ -n "${ROOT}" ]]; then
    ROOT="$(cd "${ROOT}" && pwd)"
    return 0
  fi
  if [[ ! -f "${INDEX_JSON}" ]]; then
    echo "error: Studio index missing: ${INDEX_JSON}" >&2
    echo "  pass --root explicitly" >&2
    exit 1
  fi
  need python3
  local py
  py="$(
    NAME="${NAME}" USE_LAST="${USE_LAST}" INDEX_JSON="${INDEX_JSON}" python3 - <<'PY'
import json, os, sys
from pathlib import Path

idx_path = Path(os.environ["INDEX_JSON"])
data = json.loads(idx_path.read_text(encoding="utf-8"))
repos = data.get("repos") or []
name = (os.environ.get("NAME") or "").strip().lower()
use_last = os.environ.get("USE_LAST") == "1"
last = (data.get("last") or "").strip()

def norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())

if name:
    q = name
    qn = norm(q)
    hits = []
    for r in repos:
        path = (r.get("path") or "").strip()
        label = (r.get("name") or Path(path).name if path else "").strip()
        blob = f"{label} {path}".lower()
        bn = norm(label) + norm(Path(path).name if path else "")
        if q in blob or qn in bn or qn == norm(Path(path).name if path else ""):
            hits.append((path, label))
    if not hits:
        print(f"error: no Studio repo matched --name {name!r}", file=sys.stderr)
        for r in repos:
            print(f"  - {r.get('name')}: {r.get('path')}", file=sys.stderr)
        sys.exit(1)
    if len(hits) > 1:
        # Prefer exact / shorter label match
        hits.sort(key=lambda t: (0 if q == t[1].lower() else 1, len(t[1])))
        print(f"note: multiple matches for {name!r}; using {hits[0][1]} ({hits[0][0]})", file=sys.stderr)
    print(hits[0][0])
    sys.exit(0)

if use_last or not name:
    if not last:
        print("error: Studio index has no 'last' — pass --root or --name", file=sys.stderr)
        sys.exit(1)
    print(last)
    sys.exit(0)

print("error: pass --root, --name, or --last", file=sys.stderr)
sys.exit(1)
PY
  )" || exit 1
  ROOT="$(cd "${py}" && pwd)"
}

resolve_root

if [[ -z "${BUILD_DIR}" ]]; then
  if [[ "${SETUP_HOST}" -eq 1 ]]; then
    BUILD_DIR="${ROOT}/build-mingw-setup"
  else
    BUILD_DIR="${ROOT}/build-mingw"
  fi
elif [[ "${BUILD_DIR}" != /* ]]; then
  BUILD_DIR="${ROOT}/${BUILD_DIR}"
fi

VERSION=""
if [[ -f "${ROOT}/VERSION" ]]; then
  VERSION="$(tr -d '[:space:]' < "${ROOT}/VERSION")"
fi
if [[ -z "${VERSION}" ]]; then
  VERSION="dev"
fi

STATIC_FLAG=ON
if [[ "${STATIC_RUNTIME}" -eq 0 ]]; then
  STATIC_FLAG=OFF
fi

echo "==> title:  ${ROOT}"
echo "    mode:   $([ "${SETUP_HOST}" -eq 1 ] && echo setup-host || echo full playable)$([ "${PACKAGE_ONLY}" -eq 1 ] && echo ' (package-only)' || true)"
echo "    build:  ${BUILD_DIR}"
echo "    version:${VERSION}"
echo "    static: ${STATIC_FLAG}"

find_exe() {
  local d="$1"
  # Prefer *Recompil*.exe at build root, then nested Release/, then any .exe.
  local cand
  cand="$(find "${d}" -maxdepth 1 -type f -name '*Recompil*.exe' 2>/dev/null | head -n1 || true)"
  if [[ -z "${cand}" ]]; then
    cand="$(find "${d}" -maxdepth 2 -type f -name '*Recompil*.exe' 2>/dev/null | head -n1 || true)"
  fi
  if [[ -z "${cand}" ]]; then
    cand="$(find "${d}" -maxdepth 1 -type f -name 'psx-runtime.exe' 2>/dev/null | head -n1 || true)"
  fi
  if [[ -z "${cand}" ]]; then
    cand="$(find "${d}" -maxdepth 2 -type f -name '*.exe' ! -path '*/CMakeFiles/*' ! -path '*/_deps/*' 2>/dev/null | head -n1 || true)"
  fi
  printf '%s' "${cand}"
}

emit_mingw_zip_line() {
  local zip_cand=""
  # Prefer newest artifact matching our tag under dist/.
  zip_cand="$(ls -t "${ROOT}/dist/"*-"${ARTIFACT_TAG}".zip 2>/dev/null | head -n1 || true)"
  if [[ -z "${zip_cand}" ]]; then
    zip_cand="$(ls -t "${ROOT}/dist/"*.zip 2>/dev/null | head -n1 || true)"
  fi
  if [[ -n "${zip_cand}" && -f "${zip_cand}" ]]; then
    echo "MINGW_ZIP=$(cd "$(dirname "${zip_cand}")" && pwd)/$(basename "${zip_cand}")"
  fi
}

# Prefer Studio toolkit bundler (transitive zlib→libssp, etc.) over a stale
# game psxrecomp submodule copy.
resolve_mingw_bundler() {
  local cand
  for cand in \
    "${TOOLKIT}/scripts/bundle_mingw_dlls.sh" \
    "${ROOT}/psxrecomp/tools/bundle_mingw_dlls.sh"
  do
    if [[ -f "${cand}" ]]; then
      printf '%s' "${cand}"
      return 0
    fi
  done
  return 1
}

run_mingw_bundle() {
  local exe="$1"
  local dest="${2:-}"
  local bundler=""
  bundler="$(resolve_mingw_bundler || true)"
  if [[ -z "${bundler}" ]]; then
    echo "warn: bundle_mingw_dlls.sh not found (toolkit or psxrecomp)" >&2
    return 0
  fi
  echo "==> bundle MinGW DLLs via ${bundler}" >&2
  local args=(
    --runtime-bin "${RUNTIME_BIN_DIR}"
    --exe "${exe}"
    --require libgcc_s_seh-1.dll
    --require libstdc++-6.dll
    --require libwinpthread-1.dll
    --require libssp-0.dll
    --require zlib1.dll
    --require z.dll
  )
  if [[ -n "${dest}" ]]; then
    args+=(--dest "${dest}")
  fi
  bash "${bundler}" "${args[@]}"
}

# Title packagers often copy only the .exe into dist/ and re-bundle with a
# stale submodule script. Re-run Studio's bundler and inject DLLs at zip root.
inject_mingw_dlls_into_zip() {
  local zip_cand="" host="" dest="" bundler=""
  zip_cand="$(ls -t "${ROOT}/dist/"*-"${ARTIFACT_TAG}".zip 2>/dev/null | head -n1 || true)"
  if [[ -z "${zip_cand}" || ! -f "${zip_cand}" ]]; then
    return 0
  fi
  bundler="$(resolve_mingw_bundler || true)"
  if [[ -z "${bundler}" ]]; then
    return 0
  fi
  host="${EXE:-}"
  if [[ -z "${host}" || ! -f "${host}" ]]; then
    host="$(find_exe "${BUILD_DIR}")"
  fi
  if [[ -z "${host}" || ! -f "${host}" ]]; then
    return 0
  fi
  dest="$(dirname "${host}")"
  echo "==> Studio MinGW bundler → ${dest} + zip root" >&2
  run_mingw_bundle "${host}" "${dest}"
  local stage="${ROOT}/dist/stage-setup-${ARTIFACT_TAG}"
  if [[ -d "${stage}" ]]; then
    local stage_exe=""
    stage_exe="$(find_exe "${stage}")"
    if [[ -n "${stage_exe}" && -f "${stage_exe}" ]]; then
      run_mingw_bundle "${stage_exe}" "${stage}"
      dest="${stage}"
    fi
  fi
  need zip
  local dll
  shopt -s nullglob
  for dll in "${dest}"/*.dll "${dest}"/*.DLL; do
    echo "    zip +$(basename "${dll}")" >&2
    zip -jq "${zip_cand}" "${dll}"
  done
  shopt -u nullglob
}

# Setup-host Windows zips require MinGW emitter .exes (not Linux build-recompiler).
# Prints the build dir path on stdout; status lines go to stderr.
ensure_mingw_emitters() {
  local out_dir="${1:-${ROOT}/build-recompiler-mingw}"
  local tc="${2:-}"
  local game_exe="${out_dir}/psxrecomp-game.exe"
  local bios_exe="${out_dir}/psxrecomp-bios.exe"
  if [[ -f "${game_exe}" && -f "${bios_exe}" ]]; then
    echo "==> reusing MinGW emitters under ${out_dir}" >&2
    printf '%s\n' "${out_dir}"
    return 0
  fi
  if [[ -z "${tc}" ]]; then
    if [[ -f "${ROOT}/psxrecomp/cmake/toolchain-mingw-w64.cmake" ]]; then
      tc="${ROOT}/psxrecomp/cmake/toolchain-mingw-w64.cmake"
    else
      echo "error: missing MinGW toolchain file for emitters" >&2
      return 1
    fi
  fi
  if [[ ! -d "${ROOT}/psxrecomp/recompiler" ]]; then
    echo "error: missing ${ROOT}/psxrecomp/recompiler" >&2
    return 1
  fi
  need cmake
  need ninja
  need "${TRIPLE}-gcc"
  need "${TRIPLE}-g++"
  echo "==> configure MinGW emitters → ${out_dir}" >&2
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "DRY: cmake -S ${ROOT}/psxrecomp/recompiler -B ${out_dir} -G Ninja -DCMAKE_TOOLCHAIN_FILE=${tc} -DCMAKE_BUILD_TYPE=Release -DPSXRECOMP_STATIC_CLI=ON" >&2
    echo "DRY: cmake --build ${out_dir} --target psxrecomp-game psxrecomp-bios -j${JOBS}" >&2
    printf '%s\n' "${out_dir}"
    return 0
  fi
  env -u PSXRECOMP_TOOLCHAIN_DIR -u RETCOMM_TOOLCHAIN_DIR -u TOOLCHAIN_DIR \
    cmake \
      -S "${ROOT}/psxrecomp/recompiler" \
      -B "${out_dir}" \
      -G Ninja \
      -DCMAKE_TOOLCHAIN_FILE="${tc}" \
      -DCMAKE_BUILD_TYPE=Release \
      -DPSXRECOMP_STATIC_CLI=ON >&2
  echo "==> build MinGW emitters (-j${JOBS})" >&2
  cmake --build "${out_dir}" --target psxrecomp-game psxrecomp-bios -j"${JOBS}" >&2
  if [[ ! -f "${game_exe}" || ! -f "${bios_exe}" ]]; then
    echo "error: MinGW emitters missing after build under ${out_dir}" >&2
    ls -la "${out_dir}" >&2 || true
    return 1
  fi
  printf '%s\n' "${out_dir}"
}

if [[ "${PACKAGE_ONLY}" -eq 1 ]]; then
  need zip
  if [[ ! -d "${BUILD_DIR}" ]]; then
    echo "error: build dir missing for --package-only: ${BUILD_DIR}" >&2
    echo "  run MinGW Configure + Build first" >&2
    exit 1
  fi
  EXE=""
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    EXE="$(find_exe "${BUILD_DIR}")"
    if [[ -z "${EXE}" || ! -f "${EXE}" ]]; then
      echo "error: no .exe found under ${BUILD_DIR}" >&2
      exit 1
    fi
    echo "Packaging ${EXE}"
    run_mingw_bundle "${EXE}"
  else
    echo "DRY: would package exe under ${BUILD_DIR}"
  fi
else
  if [[ -z "${TOOLCHAIN}" ]]; then
    if [[ -f "${ROOT}/psxrecomp/cmake/toolchain-mingw-w64.cmake" ]]; then
      TOOLCHAIN="${ROOT}/psxrecomp/cmake/toolchain-mingw-w64.cmake"
    else
      echo "error: missing ${ROOT}/psxrecomp/cmake/toolchain-mingw-w64.cmake" >&2
      echo "  run: git -C \"${ROOT}\" submodule update --init --recursive" >&2
      exit 1
    fi
  fi

  need cmake
  need ninja
  need "${TRIPLE}-gcc"
  need "${TRIPLE}-g++"
  if ! command -v "${TRIPLE}-pkg-config" >/dev/null 2>&1; then
    echo "error: missing ${TRIPLE}-pkg-config (need MinGW SDL2 + pkg-config)" >&2
    exit 1
  fi
  if ! "${TRIPLE}-pkg-config" --exists sdl2; then
    echo "error: MinGW sdl2.pc not found via ${TRIPLE}-pkg-config" >&2
    echo "  Arch: pacman -S mingw-w64-sdl2" >&2
    exit 1
  fi

  if [[ ! -f "${ROOT}/CMakeLists.txt" ]]; then
    echo "error: not a game repo (no CMakeLists.txt): ${ROOT}" >&2
    exit 1
  fi
  if [[ ! -f "${ROOT}/psxrecomp/runtime/runtime.cmake" ]]; then
    echo "error: psxrecomp submodule missing under ${ROOT}" >&2
    exit 1
  fi
  if [[ ! -f "${ROOT}/recomp-ui/recomp_ui.cmake" ]]; then
    echo "error: recomp-ui submodule missing under ${ROOT}" >&2
    exit 1
  fi

  export PKG_CONFIG_PATH=""
  export PKG_CONFIG_LIBDIR="/usr/${TRIPLE}/lib/pkgconfig"
  MINGW_PKG_CONFIG="$(command -v "${TRIPLE}-pkg-config")"

  if [[ "${ENSURE}" -eq 1 ]]; then
    MIGRATE="${TOOLKIT}/migrate_project.py"
    if [[ ! -f "${MIGRATE}" ]]; then
      echo "error: missing ${MIGRATE}" >&2
      exit 1
    fi
    echo "==> ensure emitters + bios (host)"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      echo "DRY: python3 ${MIGRATE} build ensure-emitters --root ${ROOT}"
      echo "DRY: python3 ${MIGRATE} build ensure-bios --root ${ROOT}"
    else
      python3 "${MIGRATE}" build ensure-emitters --root "${ROOT}" || true
      python3 "${MIGRATE}" build ensure-bios --root "${ROOT}" || true
    fi
    if [[ "${SETUP_HOST}" -eq 0 ]]; then
      # Full playable needs generated game C; try generate if disc is indexed.
      echo "==> ensure generated sources (host generate if needed)"
      if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "DRY: python3 ${MIGRATE} build generate --root ${ROOT}"
      else
        python3 "${MIGRATE}" build generate --root "${ROOT}" || {
          echo "warn: generate failed — continuing if generated/ already exists" >&2
        }
      fi
    fi
  fi

  if [[ "${SETUP_HOST}" -eq 0 ]]; then
    # Heuristic: full builds need at least one generated TU or game.iso prepare.
    if [[ ! -d "${ROOT}/generated" ]] && [[ ! -d "${ROOT}/psxrecomp/generated" ]]; then
      echo "warn: no generated/ tree found — full MinGW build usually needs" >&2
      echo "      Studio: build generate --root \"${ROOT}\"  (or pass --ensure)" >&2
    fi
  fi

  CMAKE_ARGS=(
    -S "${ROOT}"
    -B "${BUILD_DIR}"
    -G Ninja
    -DCMAKE_TOOLCHAIN_FILE="${TOOLCHAIN}"
    -DCMAKE_BUILD_TYPE=Release
    -DPKG_CONFIG_EXECUTABLE="${MINGW_PKG_CONFIG}"
    -DPSX_STATIC_RUNTIME="${STATIC_FLAG}"
    -DPSX_GAME_VERSION="${VERSION}"
    -DRNET_ENABLE_ICE=ON
    -DRNET_BUILD_EXAMPLES=OFF
    -DRNET_BUILD_TESTS=OFF
  )

  # Native Linux caches often already have PSX_NETPLAY=ON; a fresh MinGW
  # tree defaults OFF (option() in runtime.cmake). Titles with recomp-net +
  # game.toml [netplay] (or CMakeLists FORCE ON) need the launcher netplay UI.
  extra_has_netplay=0
  for _a in "${EXTRA_CMAKE[@]+"${EXTRA_CMAKE[@]}"}"; do
    if [[ "${_a}" == *PSX_NETPLAY* ]]; then
      extra_has_netplay=1
      break
    fi
  done
  if [[ "${extra_has_netplay}" -eq 0 ]]; then
    want_netplay=0
    if grep -qE '^[[:space:]]*set\(PSX_NETPLAY[[:space:]]+ON' "${ROOT}/CMakeLists.txt" 2>/dev/null; then
      want_netplay=1
    elif [[ -f "${ROOT}/game.toml" ]] && grep -qE '^\[netplay\]' "${ROOT}/game.toml" \
      && [[ -f "${ROOT}/psxrecomp/lib/recomp-net/CMakeLists.txt" ]]; then
      want_netplay=1
    fi
    if [[ "${want_netplay}" -eq 1 ]]; then
      CMAKE_ARGS+=(-DPSX_NETPLAY=ON)
      echo "    netplay: ON (-DPSX_NETPLAY=ON)"
    else
      echo "    netplay: OFF (no CMakeLists FORCE / game.toml [netplay])"
    fi
  fi

  if [[ "${SETUP_HOST}" -eq 1 ]]; then
    CMAKE_ARGS+=(
      -DPSXRECOMP_FORCE_SETUP_HOST=ON
      -DPSXRECOMP_ALLOW_NO_BIOS=ON
      -DPSX_SETUP_WIZARD=ON
    )
  fi

  if [[ ${#EXTRA_CMAKE[@]} -gt 0 ]]; then
    CMAKE_ARGS+=("${EXTRA_CMAKE[@]}")
  fi

  echo "==> configure"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf 'DRY: cmake'
    printf ' %q' "${CMAKE_ARGS[@]}"
    printf '\n'
  else
    # Clear host toolchain pack so MinGW gcc wins (mirrors CI setup-host).
    env -u PSXRECOMP_TOOLCHAIN_DIR -u RETCOMM_TOOLCHAIN_DIR -u TOOLCHAIN_DIR \
      cmake "${CMAKE_ARGS[@]}"
  fi

  echo "==> build psx-runtime (-j${JOBS})"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "DRY: cmake --build ${BUILD_DIR} --target psx-runtime -j${JOBS}"
  else
    cmake --build "${BUILD_DIR}" --target psx-runtime -j"${JOBS}"
  fi

  EXE=""
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    EXE="$(find_exe "${BUILD_DIR}")"
    if [[ -z "${EXE}" || ! -f "${EXE}" ]]; then
      echo "error: no .exe found under ${BUILD_DIR}" >&2
      exit 1
    fi
    echo "Built ${EXE}"
    if command -v "${TRIPLE}-objdump" >/dev/null 2>&1; then
      echo "==> non-system DLL imports (empty is ideal with PSX_STATIC_RUNTIME=ON):"
      "${TRIPLE}-objdump" -p "${EXE}" 2>/dev/null \
        | awk '/DLL Name:/{print $3}' \
        | grep -viE '^(KERNEL32|USER32|GDI32|ADVAPI32|SHELL32|OLE32|OLEAUT32|WS2_32|WINMM|IMM32|SETUPAPI|VERSION|OPENGL32|COMCTL32|COMDLG32|RPCRT4|SHLWAPI|CRYPT32|BCRYPT|IPHLPAPI|NSI|DNSAPI|MSVCRT|ucrtbase|VCRUNTIME|api-ms-).*\.dll$' \
        || true
    fi
    # Always stage imported MinGW DLLs next to the exe for USB/copy testing.
    run_mingw_bundle "${EXE}"
  else
    echo "DRY: would locate .exe under ${BUILD_DIR}"
  fi
fi

if [[ "${DO_PACKAGE}" -eq 1 ]]; then
  PACK="${ROOT}/scripts/package_setup_release.sh"
  ALT="${ROOT}/scripts/package_release.sh"
  echo "==> package ${ARTIFACT_TAG}"
  RECOMPILER_MINGW="${ROOT}/build-recompiler-mingw"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    if [[ -f "${PACK}" ]]; then
      echo "DRY: ensure MinGW emitters under ${RECOMPILER_MINGW}"
      echo "DRY: PSXRECOMP_RUNTIME_BIN_DIR=${RUNTIME_BIN_DIR} bash ${PACK} ${BUILD_DIR} ${ARTIFACT_TAG} ${RECOMPILER_MINGW}"
    elif [[ -f "${ALT}" ]]; then
      echo "DRY: BPE_RUNTIME_BIN_DIR=${RUNTIME_BIN_DIR} bash ${ALT} ${BUILD_DIR} ${ARTIFACT_TAG}"
    else
      echo "DRY: no package_setup_release.sh / package_release.sh — skip zip"
    fi
  else
    export RELEASE_VERSION="${VERSION}"
    export PSXRECOMP_RUNTIME_BIN_DIR="${RUNTIME_BIN_DIR}"
    export BPE_RUNTIME_BIN_DIR="${RUNTIME_BIN_DIR}"
    _studio_bundler="$(resolve_mingw_bundler || true)"
    if [[ -n "${_studio_bundler}" ]]; then
      export PSXRECOMP_BUNDLE_MINGW_DLLS="${_studio_bundler}"
    fi
    if [[ -f "${PACK}" ]]; then
      # package_setup_host stages Windows emitters next to the .exe host.
      # Linux build-recompiler/psxrecomp-game is not enough — cross-build .exes.
      TC_FOR_EMITTERS="${TOOLCHAIN:-}"
      if [[ -z "${TC_FOR_EMITTERS}" && -f "${ROOT}/psxrecomp/cmake/toolchain-mingw-w64.cmake" ]]; then
        TC_FOR_EMITTERS="${ROOT}/psxrecomp/cmake/toolchain-mingw-w64.cmake"
      fi
      RECOMPILER_MINGW="$(ensure_mingw_emitters "${RECOMPILER_MINGW}" "${TC_FOR_EMITTERS}")"
      RECOMPILER_MINGW="${RECOMPILER_MINGW%%$'\n'*}"
      RECOMPILER_MINGW="${RECOMPILER_MINGW%%$'\r'*}"
      bash "${PACK}" "${BUILD_DIR}" "${ARTIFACT_TAG}" "${RECOMPILER_MINGW}"
    elif [[ -f "${ALT}" ]]; then
      bash "${ALT}" "${BUILD_DIR}" "${ARTIFACT_TAG}"
    else
      # Fallback: zip exe + sibling DLLs / bios / assets for USB testing.
      need zip
      if [[ -z "${EXE}" || ! -f "${EXE}" ]]; then
        EXE="$(find_exe "${BUILD_DIR}")"
      fi
      if [[ -z "${EXE}" || ! -f "${EXE}" ]]; then
        echo "error: no .exe to package under ${BUILD_DIR}" >&2
        exit 1
      fi
      mkdir -p "${ROOT}/dist"
      STAGE="${ROOT}/dist/stage-mingw-${ARTIFACT_TAG}"
      rm -rf "${STAGE}"
      mkdir -p "${STAGE}"
      EXEDIR="$(dirname "${EXE}")"
      cp -a "${EXE}" "${STAGE}/"
      shopt -s nullglob
      for f in "${EXEDIR}"/*.dll "${EXEDIR}"/*.DLL; do
        cp -a "${f}" "${STAGE}/"
      done
      shopt -u nullglob
      if [[ -d "${EXEDIR}/bios" ]]; then
        cp -a "${EXEDIR}/bios" "${STAGE}/bios"
      fi
      if [[ -d "${EXEDIR}/assets" ]]; then
        cp -a "${EXEDIR}/assets" "${STAGE}/assets"
      fi
      for f in game.toml VERSION README.md README-SETUP.txt keybinds.ini; do
        if [[ -f "${ROOT}/${f}" ]]; then
          cp -a "${ROOT}/${f}" "${STAGE}/${f}"
        fi
      done
      ZIP_PREFIX="$(basename "${ROOT}" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9._-')"
      ZIP_NAME="${ZIP_PREFIX}-${VERSION}-${ARTIFACT_TAG}.zip"
      (
        cd "${STAGE}"
        zip -qr "${ROOT}/dist/${ZIP_NAME}" .
      )
      echo "Wrote ${ROOT}/dist/${ZIP_NAME}"
      rm -rf "${STAGE}"
    fi
    inject_mingw_dlls_into_zip
    emit_mingw_zip_line
  fi
fi

echo "==> done"
if [[ -n "${EXE:-}" ]]; then
  echo "    exe: ${EXE}"
fi
echo "    copy to a Windows box (or wine) for local testing — no CI required"
