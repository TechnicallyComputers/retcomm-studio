#pragma once

// studio_frames.hpp — the Frames tab: live GP0 capture, per-function
// attribution, good/bad diff, and per-function layer images.
//
// The division of labour matches the Functions tab, for the same reason.
// psxrecomp/tools/gpu_frame_*.py own the debug protocol, the GP0 decode and
// the rasteriser; they write JSON and PNG; Studio is a viewer and a launcher.
// Nothing here decodes a GP0 packet, so a headless capture and this tab can
// never disagree about what a frame contained.
//
// Studio does keep one live connection (shared_debug_client()) for what a
// viewer legitimately needs: is a game running, which frame is it on, and the
// pause / step / run-to controls that put it on the frame you want to capture.

#include "studio/studio_model.hpp"

#include <string>

struct SDL_Window;

namespace retcomm::studio {

struct Theme;

// Whether a configured build will actually start the TCP debug server, and on
// which port.
//
// This is the question every "why won't it connect" starts with, and the answer
// is not obvious: runtime/src/debug_server.c is ALWAYS compiled, but
// debug_server_init() is only called when PSX_NO_DEBUG_TOOLS is undefined
// (main.cpp). PSX_DEBUG_TOOLS defaults ON for Debug/RelWithDebInfo and OFF for
// Release/MinSizeRel (runtime.cmake), so a plain Release build opens no port at
// all — and a Debug build of a recomp is far too slow to reach the frame you
// are chasing. Release + -DPSX_DEBUG_TOOLS=ON is the combination that works.
struct DebugToolsInfo {
    bool configured = false;        // a CMakeCache.txt exists in the build dir
    bool enabled = false;           // the runtime will call debug_server_init()
    bool from_cache = false;        // PSX_DEBUG_TOOLS was in the cache, not inferred
    std::string build_type;         // CMAKE_BUILD_TYPE from the cache
    int  port = 4370;               // [runtime] debug_port, else the compiled default
    bool port_from_game_toml = false;
    std::string cache_path;
    std::string summary;            // one line, ready to show
};

// build_dir may be relative to root. Never throws; an unconfigured or
// unreadable build dir comes back with configured=false and says so.
DebugToolsInfo probe_debug_tools(const std::string& root, const std::string& build_dir);

// What the GP0 ring can still be asked for, parsed from a gpu_ring_stats reply.
//
// This is the replacement for pause/step/run_to_frame, which psx-runtime
// removed: the ring holds ~1M packets — several hundred frames — so a frame is
// captured by reaching backwards into history rather than by freezing the game
// on it. Knowing the span matters because a dump of an evicted frame comes back
// EMPTY, which reads exactly like "that frame drew nothing".
struct RingSpan {
    bool     valid = false;
    uint32_t oldest = 0;
    uint32_t newest = 0;
    uint32_t capacity = 0;
    uint64_t total = 0;
    std::string error;      // set when valid is false
};

// Never throws; a malformed or ok:false reply comes back with valid=false and
// an error that names what happened.
RingSpan parse_ring_stats(const std::string& reply);

OracleStatus parse_oracle_status(const std::string& json_text);

PauseState parse_pause_state(const std::string& reply);

// End of the boot executable's text, from game.toml's [game] load_address +
// text_size. A PC at or above this is in a runtime-loaded OVERLAY, which is why
// psxrecomp-analyze — which reads only the boot EXE — has no function for it.
// Returns 0 when game.toml does not say.
uint32_t game_text_end(const std::string& root);

// `[game] disc` from the project's game.toml, resolved against the repo root.
// The oracle is only meaningful booted on the SAME disc psx-runtime is running,
// and this is the only place Studio knows which that is.
std::string game_disc_for(const std::string& root);

// The game product binary under a CMake build tree, or "" if there is none.
// Mirrors buildops.py's find_runtime_exe() ranking so the Frames tab launches
// exactly what the Build tab would.
std::string find_runtime_exe(const std::string& build_dir);

// <root>/analysis/frames — where captures land, next to the analyzer bundle.
std::string frames_dir_for(const std::string& root);

// Absolute path to one of the psxrecomp GP0 tools inside the selected project,
// or "" when the submodule is not checked out. Studio never ships a copy: the
// tools must match the runtime that produced the dump.
std::string gpu_tool_path(const std::string& root, const std::string& tool);

// Artifact loaders. Each fills `.error` and returns false rather than throwing,
// because a half-written capture from a crashed run is a normal thing to open.
bool load_frame_summary(FrameSummary& out, const std::string& path);
bool load_frame_diff(FrameDiff& out, const std::string& path);
bool load_frame_layers(FrameLayers& out, const std::string& dir);
bool load_frame_scan(FrameScan& out, const std::string& path);
bool load_display_list(DisplayList& out, const std::string& path);
bool load_colour_parity(ColourParity& out, const std::string& path);
bool load_packet_writers(PacketWriters& out, const std::string& path);
bool load_gte_check(GteCheck& out, const std::string& path);
bool load_colour_inputs(ColourInputs& out, const std::string& path);
bool load_lockstep(Lockstep& out, const std::string& path);
bool load_range_writers(RangeWriters& out, const std::string& path);
bool load_scale_trace(ScaleTrace& out, const std::string& path);
bool load_class_census(ClassCensus& out, const std::string& path);
// Reduce a gpu_parity report to a one-line alignment verdict. An image
// diff of two DIFFERENT frames is noise, so whether the two were parked
// on the same frame decides whether the rest means anything.
std::string parity_alignment(const std::string& path);

// Reduce a wtrace_dump reply to "which code sites wrote here, and how often".
// Raw entries are one row per write and run to thousands; the useful shape is
// the histogram.
std::vector<WtraceWriter> parse_wtrace(const std::string& reply, std::string* err);

// Load the JSON ram_parity.py writes.
bool load_ram_parity(RamParity& out, const std::string& path);

// Refresh model.frm_tags from the *.summary.json files in model.frm_dir.
void scan_frame_tags(StudioModel& model);

// Attach names from an already-loaded analysis bundle (model.fn_rows) to the
// observed functions. Labelled as what it is: a static name for an address
// that was seen executing, never a claim that the analyser predicted the call.
void name_frame_funcs(StudioModel& model, FrameSummary& summary);

void draw_frames(StudioModel& model, const Theme& th, SDL_Window* window);

} // namespace retcomm::studio
