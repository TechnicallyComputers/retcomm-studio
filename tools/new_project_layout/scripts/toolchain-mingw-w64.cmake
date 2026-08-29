# Cross-compile toolchain: Linux host → Windows x86_64 (MinGW-w64).
#
# Studio-owned, so a cross-build does not require the target console's
# framework to ship one. psxrecomp has its own copy at
# psxrecomp/cmake/toolchain-mingw-w64.cmake and the PSX script prefers it;
# snesrecomp has none, which is why this exists.
#
# Requires (Arch / CachyOS):
#   mingw-w64-gcc  mingw-w64-sdl2  (and cmake, ninja, zip)
# Debian / Ubuntu:
#   g++-mingw-w64-x86-64  + a MinGW SDL (distro or self-built)

set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)

set(MINGW_TRIPLE "x86_64-w64-mingw32" CACHE STRING "MinGW target triple")

set(CMAKE_C_COMPILER   "${MINGW_TRIPLE}-gcc")
set(CMAKE_CXX_COMPILER "${MINGW_TRIPLE}-g++")
set(CMAKE_RC_COMPILER  "${MINGW_TRIPLE}-windres")
set(CMAKE_RANLIB       "${MINGW_TRIPLE}-ranlib")
set(CMAKE_AR           "${MINGW_TRIPLE}-ar")
set(CMAKE_STRIP        "${MINGW_TRIPLE}-strip")

# Prefer the triple-prefixed pkg-config so the build does not pick up the
# host's Linux SDL and then fail at link with confusingly ELF-shaped errors.
find_program(_MINGW_PKG_CONFIG NAMES "${MINGW_TRIPLE}-pkg-config" pkg-config)
if(_MINGW_PKG_CONFIG)
  set(PKG_CONFIG_EXECUTABLE "${_MINGW_PKG_CONFIG}" CACHE FILEPATH
      "pkg-config for the MinGW sysroot" FORCE)
endif()

set(CMAKE_FIND_ROOT_PATH "/usr/${MINGW_TRIPLE}")
# Programs come from the host (cmake, ninja, python); everything linked or
# included must come from the MinGW sysroot.
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
