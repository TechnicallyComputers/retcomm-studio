cmake_minimum_required(VERSION 3.20)
project(retcomm-studio VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

file(READ "${CMAKE_CURRENT_SOURCE_DIR}/VERSION" RETCOMM_STUDIO_VERSION_FILE)
string(STRIP "${RETCOMM_STUDIO_VERSION_FILE}" RETCOMM_STUDIO_VERSION)

# ---------------------------------------------------------------------------
# Dear ImGui (SDL3 + OpenGL3) — same sourcing strategy as retcomm-hub
# ---------------------------------------------------------------------------
set(RETCOMM_IMGUI_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../recomp-ui/src/third_party/imgui"
    CACHE PATH "Path to Dear ImGui sources")

find_package(SDL3 QUIET)
find_package(OpenGL QUIET)
if(NOT TARGET OpenGL::GL)
    find_package(OpenGL REQUIRED)
endif()

if(NOT SDL3_FOUND AND NOT TARGET SDL3::SDL3)
    find_package(PkgConfig QUIET)
    if(PkgConfig_FOUND)
        pkg_check_modules(SDL3 IMPORTED_TARGET sdl3)
    endif()
endif()

set(RETCOMM_HAVE_SDL3 FALSE)
set(RETCOMM_STUDIO_SDL_TARGET "")
if(TARGET SDL3::SDL3)
    set(RETCOMM_HAVE_SDL3 TRUE)
    set(RETCOMM_STUDIO_SDL_TARGET SDL3::SDL3)
elseif(TARGET PkgConfig::SDL3)
    set(RETCOMM_HAVE_SDL3 TRUE)
    set(RETCOMM_STUDIO_SDL_TARGET PkgConfig::SDL3)
endif()

if(NOT RETCOMM_HAVE_SDL3)
    message(FATAL_ERROR "retcomm-studio requires SDL3 (find_package SDL3 or pkg-config sdl3)")
endif()

set(RETCOMM_IMGUI_READY FALSE)
if(EXISTS "${RETCOMM_IMGUI_DIR}/imgui.cpp"
   AND EXISTS "${RETCOMM_IMGUI_DIR}/backends/imgui_impl_sdl3.cpp")
    set(RETCOMM_IMGUI_READY TRUE)
else()
    include(FetchContent)
    message(STATUS "retcomm-studio: fetching Dear ImGui (SDL3 backend)")
    FetchContent_Declare(retcomm_imgui_src
        GIT_REPOSITORY https://github.com/ocornut/imgui.git
        GIT_TAG v1.91.9-docking
        GIT_SHALLOW TRUE
    )
    FetchContent_GetProperties(retcomm_imgui_src)
    if(NOT retcomm_imgui_src_POPULATED)
        FetchContent_Populate(retcomm_imgui_src)
    endif()
    set(RETCOMM_IMGUI_DIR "${retcomm_imgui_src_SOURCE_DIR}" CACHE PATH
        "Path to Dear ImGui sources" FORCE)
    if(EXISTS "${RETCOMM_IMGUI_DIR}/backends/imgui_impl_sdl3.cpp")
        set(RETCOMM_IMGUI_READY TRUE)
    endif()
endif()

if(NOT RETCOMM_IMGUI_READY)
    message(FATAL_ERROR "Dear ImGui with imgui_impl_sdl3 not available")
endif()

include(FetchContent)
FetchContent_Declare(json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG v3.11.3
    GIT_SHALLOW TRUE
)
FetchContent_MakeAvailable(json)

set(RETCOMM_IMGUI_FREETYPE FALSE)
set(RETCOMM_IMGUI_FREETYPE_TARGET "")
find_package(Freetype QUIET)
if(TARGET Freetype::Freetype)
    set(RETCOMM_IMGUI_FREETYPE TRUE)
    set(RETCOMM_IMGUI_FREETYPE_TARGET Freetype::Freetype)
else()
    find_package(PkgConfig QUIET)
    if(PkgConfig_FOUND)
        pkg_check_modules(RETCOMM_FT2 IMPORTED_TARGET freetype2)
        if(TARGET PkgConfig::RETCOMM_FT2)
            set(RETCOMM_IMGUI_FREETYPE TRUE)
            set(RETCOMM_IMGUI_FREETYPE_TARGET PkgConfig::RETCOMM_FT2)
        endif()
    endif()
endif()

set(RETCOMM_IMGUI_SOURCES
    ${RETCOMM_IMGUI_DIR}/imgui.cpp
    ${RETCOMM_IMGUI_DIR}/imgui_draw.cpp
    ${RETCOMM_IMGUI_DIR}/imgui_tables.cpp
    ${RETCOMM_IMGUI_DIR}/imgui_widgets.cpp
    ${RETCOMM_IMGUI_DIR}/backends/imgui_impl_sdl3.cpp
    ${RETCOMM_IMGUI_DIR}/backends/imgui_impl_opengl3.cpp
)
set(RETCOMM_IMGUI_FREETYPE_SRC
    "${CMAKE_CURRENT_SOURCE_DIR}/third_party/misc/freetype/imgui_freetype.cpp")
if(RETCOMM_IMGUI_FREETYPE AND EXISTS "${RETCOMM_IMGUI_FREETYPE_SRC}")
    list(APPEND RETCOMM_IMGUI_SOURCES ${RETCOMM_IMGUI_FREETYPE_SRC})
else()
    set(RETCOMM_IMGUI_FREETYPE FALSE)
endif()

add_library(retcomm_studio_imgui STATIC ${RETCOMM_IMGUI_SOURCES})
target_include_directories(retcomm_studio_imgui PUBLIC
    ${RETCOMM_IMGUI_DIR}
    ${RETCOMM_IMGUI_DIR}/backends
)
target_link_libraries(retcomm_studio_imgui PUBLIC ${RETCOMM_STUDIO_SDL_TARGET} OpenGL::GL)
if(RETCOMM_IMGUI_FREETYPE)
    target_include_directories(retcomm_studio_imgui PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/third_party
        ${CMAKE_CURRENT_SOURCE_DIR}/third_party/misc/freetype)
    target_compile_definitions(retcomm_studio_imgui PUBLIC IMGUI_ENABLE_FREETYPE)
    target_link_libraries(retcomm_studio_imgui PUBLIC ${RETCOMM_IMGUI_FREETYPE_TARGET})
    message(STATUS "retcomm-studio: ImGui FreeType enabled")
else()
    message(STATUS "retcomm-studio: ImGui FreeType disabled")
endif()

set(RETCOMM_STUDIO_SOURCES
    src/studio/studio_main.cpp
    src/studio/studio_model.cpp
    src/studio/studio_runner.cpp
)

# Windows .exe icon (explorer / taskbar) from assets/retcomm-studio.ico
set(RETCOMM_STUDIO_WIN_RC "")
if(WIN32 AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/assets/retcomm-studio.ico")
    string(REPLACE "\\" "/" RETCOMM_STUDIO_ICO_PATH
           "${CMAKE_CURRENT_SOURCE_DIR}/assets/retcomm-studio.ico")
    configure_file("${CMAKE_CURRENT_SOURCE_DIR}/packaging/windows/retcomm-studio.rc.in"
                   "${CMAKE_BINARY_DIR}/retcomm-studio.rc" @ONLY)
    set(RETCOMM_STUDIO_WIN_RC "${CMAKE_BINARY_DIR}/retcomm-studio.rc")
endif()

if(WIN32)
    add_executable(retcomm-studio WIN32 ${RETCOMM_STUDIO_SOURCES} ${RETCOMM_STUDIO_WIN_RC})
    if(MSVC)
        target_link_options(retcomm-studio PRIVATE "/ENTRY:mainCRTStartup")
    endif()
else()
    add_executable(retcomm-studio ${RETCOMM_STUDIO_SOURCES})
endif()

target_include_directories(retcomm-studio PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
)
# stb_image for window icon (PNG)
target_include_directories(retcomm-studio PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/third_party")
target_compile_definitions(retcomm-studio PRIVATE
    RETCOMM_STUDIO_VERSION="${RETCOMM_STUDIO_VERSION}"
)
target_link_libraries(retcomm-studio PRIVATE
    retcomm_studio_imgui
    nlohmann_json::nlohmann_json
)

if(UNIX AND NOT APPLE)
    set_target_properties(retcomm-studio PROPERTIES INSTALL_RPATH "$ORIGIN/../lib;$ORIGIN")
endif()

# Friendly name for packaging / desktop launchers
set_target_properties(retcomm-studio PROPERTIES OUTPUT_NAME "RetComM-Studio")

install(TARGETS retcomm-studio RUNTIME DESTINATION bin)
install(DIRECTORY tools/new_project_layout/
        DESTINATION share/retcomm-studio/toolkit
        PATTERN ".venv" EXCLUDE
        PATTERN "__pycache__" EXCLUDE
        PATTERN "*.pyc" EXCLUDE
        PATTERN "project_studio_repos.json" EXCLUDE
        PATTERN ".cache" EXCLUDE)
install(DIRECTORY assets/
        DESTINATION share/retcomm-studio/assets
        PATTERN "*.svg" EXCLUDE)
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/assets/fonts/LatoLatin-Regular.ttf")
    install(FILES
        assets/fonts/LatoLatin-Regular.ttf
        assets/fonts/LatoLatin-Bold.ttf
        assets/fonts/NOTICE.md
        DESTINATION share/retcomm-studio/fonts)
endif()
install(FILES VERSION DESTINATION share/retcomm-studio)

message(STATUS "retcomm-studio ${RETCOMM_STUDIO_VERSION}: ImGui=${RETCOMM_IMGUI_DIR}")
