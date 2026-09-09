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

// SNES only: the execution-policy DEFAULT a build was configured with.
//
// snesrecomp resolves off/on/force/verify/auto at RUNTIME
// (runner/src/execution_mode.c); -DSNESRECOMP_EXECUTION_DEFAULT only picks
// which one a run starts from, and SNESRECOMP_EXECUTION_MODE /
// SNESRECOMP_FORCE_FLOOR still override it in the launched process. So this
// probe answers "what does this build default to", not "what will the next
// run do" — the tab says so rather than implying the stronger claim.
//
// `supported` is the load-bearing field. The option landed in snesrecomp on
// feat/execution-mode-policy; a port pinned to an older framework has no such
// cache variable, and sending -D for it would earn cmake's "Manually-specified
// variables were not used by the project" warning — the exact thing the
// toolchain-repair code takes care to avoid. Read from the port's own pinned
// runner.cmake, so the answer is about THIS repo rather than about whichever
// snesrecomp Studio was built beside.
struct ExecModeInfo {
    bool supported = false;         // this port's pinned snesrecomp has the option
    bool configured = false;        // a CMakeCache.txt exists in the build dir
    bool from_cache = false;        // the value below came from the cache
    std::string mode;               // "off" | "on" | "force" | "verify" | "auto"
    std::string cache_path;
    std::string summary;            // one line, ready to show
};

// build_dir may be relative to root. Never throws. On a non-SNES port, or a
// SNES port whose framework predates the option, comes back supported=false
// with a summary that says which.
ExecModeInfo probe_exec_mode(const std::string& root, const std::string& build_dir);

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

// The project's disc roster from game.toml, in disc order, index 0 the disc
// that boots. `[game] discs` when present, else `[game] disc` as a one-image
// set -- the loader's own precedence, where `disc` is sugar for `discs =
// [disc]`. A multi-disc title written by probe_disc.py carries `discs` only.
//
// Entries that are not on disk are KEPT, marked !present: these positions are
// what settings.toml's `[disc] selected` indexes, so dropping a missing row
// would renumber the ones after it.
std::vector<DiscEntry> game_discs_for(const std::string& root);

// The boot disc from that roster, resolved against the repo root, or "" when
// the project names none or the image is gone. The oracle is only meaningful
// booted on the SAME disc psx-runtime is running.
std::string game_disc_for(const std::string& root);

// 1-based disc psx-runtime last booted, from the `[disc] selected` the launcher
// persists in the settings.toml beside the built executable. 0 when there is no
// build, no settings file, or no such key.
//
// This is what makes the oracle's disc default to the right one instead of
// always disc 1: a comparison against a different disc of the set is not a
// comparison at all.
int runtime_selected_disc(const StudioModel& model, const std::string& root);

// The memory card the runtime uses for this project. ONE card serves the whole
// set -- see MemcardRef.
MemcardRef game_memcard_for(const StudioModel& model, const std::string& root);

// The game product binary under a CMake build tree, or "" if there is none.
// Mirrors buildops.py's find_runtime_exe() ranking so the Frames tab launches
// exactly what the Build tab would.
std::string find_runtime_exe(const std::string& build_dir);

// The executable the Build tab would run: its Exe override when set, else the
// ranked scan of its build directory. Use this for anything that launches the
// game, so Build and both Diagnostics pages cannot disagree.
std::string selected_game_exe(const StudioModel& model, const std::string& root);

// <root>/analysis/frames — where captures land, next to the analyzer bundle.
std::string frames_dir_for(const std::string& root);

// Absolute path to one of the psxrecomp GP0 tools inside the selected project,
// or "" when the submodule is not checked out. Studio never ships a copy: the
// tools must match the runtime that produced the dump.
std::string gpu_tool_path(const std::string& root, const std::string& tool);

// Generated oracle capability tables (tools/psx_analysis/oracle_caps.json).
// Loaded once; a failure leaves the tables empty, which gates nothing.
void load_oracle_caps(StudioModel& model);

// Why `tool` cannot run against the currently selected oracle, or empty when
// it can. Decided from the generated tables, never from a hand-kept list.
std::string oracle_tool_blocker(const StudioModel& model, const std::string& tool);

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
