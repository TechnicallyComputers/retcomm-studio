#pragma once

// studio_snes.hpp — the SNES Diagnostics tab: run snesrecomp and Mesen side by
// side on the SAME ROM, and drive the tools/snes_analysis toolset against them.
//
// Division of labour matches the Frames tab, for the same reason. The tools in
// tools/snes_analysis own the debug protocol, the asset decode and the
// attribution; they write JSON/PNG; Studio is a viewer and a launcher. Nothing
// here decodes a tile or a palette entry itself, so a headless capture and this
// tab can never disagree about what a frame contained.
//
// The one thing Studio does own is a live connection, because "is a game
// running, on what frame, and what is in CGRAM right now" is what a viewer
// legitimately needs to decide WHEN to capture.
//
// Why a second client rather than reusing DebugClient: snesrecomp's debug
// server speaks a bare line protocol ("frame\n" -> one JSON line), while
// DebugClient frames every command as {"id":N,...}. Pointing the latter at a
// snesrecomp port produces a parse error on the runtime side, not a connection.

#include "studio/studio_model.hpp"

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

struct SDL_Window;

namespace retcomm::studio {

struct Theme;

// ---- live connection to a snesrecomp runtime -------------------------------

class SnesDebugClient {
public:
    enum class State { Disconnected, Connecting, Connected, Failed };

    struct Snapshot {
        State       state = State::Disconnected;
        uint64_t    frame = 0;
        std::string error;
        std::string last_command;
    };

    ~SnesDebugClient();

    void start(std::string host, int port);
    void stop();
    bool running() const { return running_.load(); }

    // Queue a command whose FULL reply is retained under `label` until taken.
    // dump_vram answers with 128 KB of hex, so there is deliberately no
    // truncating variant here — everything a caller sends, it means to parse.
    void request(std::string command, std::string label);

    // Take (and clear) a retained reply. False when it has not arrived yet.
    bool take_reply(const std::string& label, std::string& out);

    // True while a request with this label is queued or in flight.
    bool pending(const std::string& label) const;

    Snapshot snapshot();

private:
    struct Job { std::string command, label; };

    void worker();
    bool ensure_connected();
    bool exchange(const std::string& command, std::string& response);
    void close_socket();

    std::thread             thread_;
    std::atomic<bool>       running_{false};
    std::atomic<bool>       quit_{false};

    mutable std::mutex      mu_;
    std::condition_variable cv_;
    Snapshot                snap_;
    std::vector<Job>        outbox_;
    std::map<std::string, std::string> replies_;
    std::vector<std::string>           in_flight_;

    std::string host_ = "127.0.0.1";
    int         port_ = 4370;
    int         sock_ = -1;
    std::string rx_;
};

// One per Studio process. Two sockets racing one debug server would interleave
// replies on a protocol that has no request ids to sort them back out with.
SnesDebugClient& snes_debug_client();

// ---- probes ----------------------------------------------------------------

// The ROM the built runtime actually boots. snesrecomp's launcher takes argv[1],
// then <exe_dir>/rom.cfg, then a file picker — so rom.cfg is what a Launch from
// here will use, and therefore the only ROM an oracle run may be compared with.
// Empty when the build has not been run yet (rom.cfg is written on first boot).
std::string snes_rom_for(const std::string& build_dir);

// Where Studio's own SNES tools live (tools/snes_analysis beside the toolkit),
// or "" when the toolkit could not be located. Studio never ships a second copy
// inside a project: the tools must match the runtime that produced the dump.
std::string snes_tool_path(const std::string& tool);

// Mesen's resolved binary, via `mesen_oracle.py path --binary`. Cached in the
// model because probing costs a process launch.
// Parses `mesen_oracle.py status --json`. Never throws; a malformed reply comes
// back with installed=false and an error naming what happened.
MesenStatus parse_mesen_status(const std::string& json_text);

// ---- CGRAM -----------------------------------------------------------------

// 256 BGR555 entries decoded from a dump_cgram reply.
//
// The census is the point, not the swatches. A real SNES palette repeats a
// colour a handful of times; a *fill* repeats one 70+ times across unrelated
// palettes, which is what a stale or never-uploaded palette block looks like.
// That distinction — and specifically "which entries were NOT written" — is
// what separates a corrupt write from a missing producer.
bool parse_cgram(CgramView& out, const std::string& reply);

// Read a Mesen `*_cgram.bin` (512 raw bytes, from mesen_vram_dump.lua) for the
// reference side of a palette diff.
bool load_cgram_bin(CgramView& out, const std::string& path);

// How many of the 256 entries differ. -1 when either side is not loaded.
int cgram_diff_count(const CgramView& a, const CgramView& b);

// BGR555 -> 0xRRGGBB, with the 5->8 bit replication the PPU does.
uint32_t bgr555_to_rgb(uint16_t v);

// ---- artifacts -------------------------------------------------------------

// A capture bundle written by snes_frame_capture.py: <tag>.json plus whatever
// snes_asset_decode.py / snes_frame_attribute.py have since produced beside it.
// Refresh the bundle list from <root>/analysis/frames.
void scan_snes_bundles(StudioModel& model);

// One row of mesen_raster_trace.lua's CSV: a register write stamped with the
// beam position. This is the measurement our own runtime cannot make — its
// trace records (frame, addr, value) with no scanline, and for a title that
// raster-splits the screen the scanline IS the question.
bool load_raster_csv(std::vector<RasterRow>& out, const std::string& path,
                     std::string* err);

// Load what snes_loop_compare.py wrote. Never throws; a half-written file from
// an interrupted run comes back with .error set, which is a normal thing to
// open — the tool writes only at the end, so an aborted run leaves the
// PREVIOUS result in place and the frame window says which run it was.
bool load_loop_compare(LoopCompare& out, const std::string& path);

void draw_snes(StudioModel& model, const Theme& th, SDL_Window* window);

// SNES Functions tab. Reads src/gen/program_manifest.json (what the analyzer
// proved) joined with recomp/symbols.toml (what a human has named and
// promoted). Never invokes an analyzer: on SNES discovery happens inside
// tools/regen.sh, so this reports its result rather than re-deriving it.
void load_snes_functions(StudioModel& model, const std::string& root);
bool parse_snes_functions(SnesFunctions& out, const std::string& json_text);
void draw_snes_functions(StudioModel& model, const Theme& th, SDL_Window* window);

} // namespace retcomm::studio
