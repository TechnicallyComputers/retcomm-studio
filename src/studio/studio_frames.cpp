// studio_frames.cpp — the Frames tab.
//
// Three jobs, in the order you do them:
//
//   1. Put the running game on the frame you care about (pause / step / run to)
//      and capture it. Capture shells out to psxrecomp/tools/gpu_frame_capture.py,
//      which owns the debug protocol and the GP0 decode.
//   2. Read who drew what. The attribution table is the frame's authorship:
//      one row per guest function, with how many primitives it issued, how many
//      carried the semi-transparency bit, and which blend modes it used.
//   3. Compare a good frame with a bad one, and look at the layers.
//
// Everything on this tab is OBSERVED from one execution of one frame. Names
// borrowed from the analysis bundle are labelled as static names attached to an
// address that was seen executing — never as a claim the analyser predicted the
// call. That distinction is the same one the Functions tab makes, and it is the
// reason a live connection can sit next to a static claim without corrupting it.

#include "studio/studio_frames.hpp"
#include "studio/studio_debug.hpp"
#include "studio/studio_functions.hpp"
#include "studio/studio_runner.hpp"
#include "studio/studio_theme.hpp"

#include "imgui.h"
#include <SDL3/SDL_opengl.h>
#include "stb_image.h"

#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <string>
#include <vector>

namespace fs = std::filesystem;

namespace retcomm::studio {

namespace {

const char* kCaptureTool = "gpu_frame_capture.py";
const char* kLayersTool = "gpu_frame_layers.py";
const char* kDiffTool = "gpu_frame_diff.py";
const char* kParityTool = "gpu_parity.py";
const char* kOracleTool = "duckstation_oracle.py";
const char* kScanTool = "gpu_frame_scan.py";
const char* kRamTool = "ram_parity.py";
const char* kDisasmTool = "disasm_ram.py";
const char* kWritersTool = "packet_writers.py";
const char* kGteTool = "gte_check.py";
const char* kInputsTool = "colour_inputs.py";
const char* kLockstepTool = "lockstep_check.py";
const char* kProbeTool = "probe_regs.py";
const char* kRangeTool = "range_writers.py";
const char* kScaleTool = "scale_trace.py";
const char* kCensusTool = "class_census.py";
const char* kDlistTool = "gpu_display_list.py";
const char* kColourTool = "gpu_colour_parity.py";

// Defined below, beside oracle_port(): refresh_oracle() runs before it.
std::string oracle_tool_path(const StudioModel& model, const std::string& root);

// The Activity log is collapsible, and a failure nobody can see is a failure
// that looks like a button doing nothing. Pull the actual message up into the
// tab so the tail of stderr is on screen where the click happened.
std::string tool_error(const RunResult& r) {
    std::string text = r.stderr_text.empty() ? r.stdout_text : r.stderr_text;
    // Last non-empty line: python puts the useful part at the end.
    std::string last;
    size_t pos = 0;
    while (pos <= text.size()) {
        const size_t nl = text.find('\n', pos);
        std::string line = text.substr(pos, nl == std::string::npos ? std::string::npos
                                                                    : nl - pos);
        while (!line.empty() && (line.back() == '\r' || line.back() == ' '))
            line.pop_back();
        if (!line.empty()) last = line;
        if (nl == std::string::npos) break;
        pos = nl + 1;
    }
    if (last.empty()) last = "exit code " + std::to_string(r.exit_code);
    if (last.size() > 240) last = last.substr(0, 240) + " …";
    return last;
}

void left_label(const char* text, float w) {
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(text);
    ImGui::SameLine(w);
}

// Colour AND wrap. ImGui has TextColored and TextWrapped but not both, and an
// unwrapped line widens the content region — the page then scrolls sideways
// and every control on it shifts. Tool errors, oracle-gating reasons and
// captured stderr are all long by nature, so they all come through here.
void wrapped(const ImVec4& col, const char* fmt, ...) IM_FMTARGS(2);
void wrapped(const ImVec4& col, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::TextWrappedV(fmt, args);
    ImGui::PopStyleColor();
    va_end(args);
}

void muted(const Theme& th, const char* fmt, ...) IM_FMTARGS(2);
void muted(const Theme& th, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    ImGui::PushStyleColor(ImGuiCol_Text, th.text_muted);
    ImGui::TextV(fmt, args);
    ImGui::PopStyleColor();
    va_end(args);
}

bool accent_button(const Theme& th, const char* label, bool enabled = true,
                   const ImVec2& size = ImVec2(0, 0)) {
    ImGui::BeginDisabled(!enabled);
    ImGui::PushStyleColor(ImGuiCol_Button, th.accent_button);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, th.accent_button_hovered);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, th.accent_button_active);
    const bool hit = ImGui::Button(label, size);
    ImGui::PopStyleColor(3);
    ImGui::EndDisabled();
    return hit;
}

// ---- textures --------------------------------------------------------------

void release_texture(unsigned& tex) {
    if (tex) {
        GLuint t = static_cast<GLuint>(tex);
        glDeleteTextures(1, &t);
        tex = 0;
    }
}

void release_layer_textures(FrameLayers& l) {
    for (auto& layer : l.layers) release_texture(layer.tex);
    release_texture(l.composite.tex);
    release_texture(l.sheet.tex);
}

// Upload a PNG once, on demand. Layers are RGBA with alpha = coverage, so the
// uncovered part of a layer must stay transparent rather than reading as black
// — that is the difference between "this function drew nothing here" and "this
// function drew black here", which for a subtractive blend is the whole point.
bool ensure_texture(const std::string& dir, FrameLayer& layer) {
    if (layer.tex) return true;
    if (layer.file.empty()) return false;
    const std::string path = (fs::path(dir) / layer.file).string();
    int w = 0, h = 0, comp = 0;
    stbi_uc* px = stbi_load(path.c_str(), &w, &h, &comp, 4);
    if (!px || w <= 0 || h <= 0) {
        if (px) stbi_image_free(px);
        layer.file.clear();   // do not retry every frame
        return false;
    }
    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
    stbi_image_free(px);
    layer.tex = static_cast<unsigned>(tex);
    layer.tw = w;
    layer.th = h;
    return true;
}

// Screenshots are re-taken, so unlike a layer they must be re-uploaded rather
// than cached on first sight.
bool reload_texture(const std::string& path, FrameLayer& img) {
    release_texture(img.tex);
    img.file = path;
    img.tw = img.th = 0;
    int w = 0, h = 0, comp = 0;
    stbi_uc* px = stbi_load(path.c_str(), &w, &h, &comp, 4);
    if (!px || w <= 0 || h <= 0) {
        if (px) stbi_image_free(px);
        return false;
    }
    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
    stbi_image_free(px);
    img.tex = static_cast<unsigned>(tex);
    img.tw = w;
    img.th = h;
    return true;
}

ImTextureID tex_id(unsigned tex) {
    return static_cast<ImTextureID>(static_cast<intptr_t>(tex));
}

// ---- ring ------------------------------------------------------------------

// gpu_ring_stats over the shared debug client, at most once a second.
//
// Note this uses request()/take_reply(), not send(). send() keeps only a
// truncated copy of the reply for the status line, and discards whether it
// even succeeded — which is precisely why the old Pause / Step / Run-to-frame
// buttons looked inert instead of reporting "pause is removed" on every click.
// Anything whose answer the UI depends on has to come back through a retained
// reply.
void poll_ring(StudioModel& model, DebugClient& dbg) {
    if (dbg.snapshot().state != DebugClient::State::Connected) {
        model.frm_ring_valid = false;
        return;
    }
    std::string reply;
    if (dbg.take_reply("gpu_ring_stats", reply)) {
        const RingSpan span = parse_ring_stats(reply);
        model.frm_ring_valid = span.valid;
        model.frm_ring_error = span.error;
        model.frm_ring_oldest = span.oldest;
        model.frm_ring_newest = span.newest;
        model.frm_ring_total = span.total;
        model.frm_ring_capacity = span.capacity;
    }
    const double now = ImGui::GetTime();
    if (now >= model.frm_ring_next_poll) {
        model.frm_ring_next_poll = now + 1.0;
        dbg.request(R"("cmd":"gpu_ring_stats")", "gpu_ring_stats");
    }
}

// Pause state polls faster than the ring: it is what the Pause button renders,
// and it is also how the UI learns that a park auto-resumed behind its back.
void poll_pause(StudioModel& model, DebugClient& dbg) {
    if (dbg.snapshot().state != DebugClient::State::Connected) {
        model.frm_pause = PauseState{};
        return;
    }
    std::string reply;
    if (dbg.take_reply("pause_state", reply))
        model.frm_pause = parse_pause_state(reply);
    const double now = ImGui::GetTime();
    if (now >= model.frm_pause_next_poll) {
        model.frm_pause_next_poll = now + 0.4;
        dbg.request(R"("cmd":"pause_state")", "pause_state");
    }
}

// ---- oracle ----------------------------------------------------------------

void refresh_oracle(StudioModel& model, const std::string& root) {
    const std::string tool = oracle_tool_path(model, root);
    if (tool.empty()) {
        model.frm_oracle = OracleStatus{};
        model.frm_oracle.error =
            std::string(oracle_tool(model.frm_oracle_kind)) + " not found";
        model.frm_oracle_queried = true;
        return;
    }
    model.frm_oracle_queried = true;
    // Global slot: the status query is machine-wide and must not queue behind
    // (or block) whatever the project slot is doing.
    run_python_script_async(model, tool, {"status", "--json"},
                            [&model](RunResult r) {
                                model.frm_oracle = parse_oracle_status(r.stdout_text);
                            },
                            /*log_stdout=*/false, JobSlot::Global);
}

// ---- paths -----------------------------------------------------------------

void sync_dirs(StudioModel& model, const std::string& root) {
    const std::string want = frames_dir_for(root);
    if (want != model.frm_dir) {
        std::snprintf(model.frm_dir, sizeof(model.frm_dir), "%s", want.c_str());
        model.frm_tags.clear();
        model.frm_sel_a = model.frm_sel_b = -1;
        model.frm_a = FrameSummary{};
        model.frm_b = FrameSummary{};
        model.frm_diff = FrameDiff{};
        release_layer_textures(model.frm_layers);
        model.frm_layers = FrameLayers{};
        scan_frame_tags(model);
        // Follow the project's own [runtime] debug_port rather than making the
        // user rediscover it. Only on a root change, so a hand-typed port is
        // never clobbered mid-session.
        model.frm_port = probe_debug_tools(root, model.build_dir).port;
        model.frm_oracle_queried = false;   // the tool path is per-checkout
    }
}

std::string summary_path(const StudioModel& model, int idx) {
    if (idx < 0 || idx >= static_cast<int>(model.frm_tags.size())) return {};
    return (fs::path(model.frm_dir) / (model.frm_tags[idx] + ".summary.json")).string();
}

void load_selected(StudioModel& model, int which) {
    FrameSummary& s = which == 0 ? model.frm_a : model.frm_b;
    const std::string p = summary_path(model, which == 0 ? model.frm_sel_a : model.frm_sel_b);
    if (p.empty()) {
        s = FrameSummary{};
        return;
    }
    load_frame_summary(s, p);
    name_frame_funcs(model, s);
    model.frm_func_sel = -1;
}

const char* tag_at(const StudioModel& model, int idx) {
    if (idx < 0 || idx >= static_cast<int>(model.frm_tags.size())) return "(none)";
    return model.frm_tags[idx].c_str();
}

// ---- panes -----------------------------------------------------------------

// Take a screenshot from one emulator and show it. The native runtime spells
// this `screenshot_file`; the DuckStation oracle calls the same operation
// `screenshot`. Both write a PNG server-side and both run on this machine, so
// Studio just reads the file back.
void shoot(DebugClient& dbg, const char* cmd, const std::string& path,
           const char* label) {
    std::string fields = std::string("\"cmd\":\"") + cmd + "\",\"path\":\"" + path + "\"";
    dbg.request(fields, label);
}

// Why a run button is greyed out, in words, or empty when it is usable.
//
// A disabled button with no explanation is indistinguishable from a broken
// one. The tool-missing case matters most: these scripts live in the psxrecomp
// submodule, and a `git pull --rebase` or `git clean` there deletes any that
// were never committed -- so the button silently stops working and the reason
// is three directories away.
std::string why_disabled(const StudioModel& model, const std::string& tool,
                         const char* tool_name, bool needs_live, bool live) {
    if (tool.empty()) {
        std::string msg = "can't find ";
        msg += tool_name;
        msg += " under <project>/psxrecomp/tools/ — if it was deleted, a pull or "
               "clean in the psxrecomp submodule is the usual cause";
        return msg;
    }
    if (needs_live && !live)
        return "needs a connected game (launch it, and check the port in Runtime)";
    if (model.busy_frames.load())
        return "another Frames job is still running";
    // Wrong oracle selected. Checked here rather than at each of the twenty
    // call sites, so a tool added later cannot forget it — and the message
    // names the oracle to switch to, because "unknown command" surfacing from
    // three layers down is the failure this exists to prevent.
    const std::string blocked = oracle_tool_blocker(model, tool_name ? tool_name : "");
    if (!blocked.empty()) return blocked;
    return {};
}

// The BIOS image a project ships for its own runtime. Beetle loads one at
// startup and refuses without it; DuckStation staged a copy into its portable
// install at `install` time and needs nothing here.
std::string game_bios_for(const std::string& root) {
    if (root.empty()) return {};
    std::error_code ec;
    for (const char* rel : {"psxrecomp/bios/SCPH1001.BIN", "bios/SCPH1001.BIN",
                            "psxrecomp/bios/scph5501.bin", "bios/scph5501.bin"}) {
        const fs::path b = fs::path(root) / rel;
        if (fs::is_regular_file(b, ec)) return b.string();
    }
    return {};
}

// Arguments for launching the selected oracle.
//
// The two managers take different flags, and this used to build DuckStation's
// set for both: Beetle was handed a --memcard it does not accept, argparse
// aborted the whole command, and the retry then failed again on the --bios it
// does require. Ask the oracle what it needs rather than discovering it from
// an error message.
std::vector<std::string> oracle_start_args(const StudioModel& model,
                                           const std::string& root,
                                           const std::string& disc) {
    std::vector<std::string> a{"start", "--disc", disc, "--wait", "90"};
    std::error_code ec;
    if (model.frm_oracle_kind == OracleKind::Beetle) {
        // Required: the core loads a BIOS itself, and psx-beetle exits early
        // without one. Beetle has no memory-card wiring at all, so there is
        // nothing to share here — a save-dependent comparison needs
        // DuckStation.
        const std::string bios = game_bios_for(root);
        if (!bios.empty()) {
            a.push_back("--bios");
            a.push_back(bios);
        }
        return a;
    }
    // DuckStation: boot from a COPY of the game's own card so the oracle can
    // reach the same scene as the runtime it is compared against.
    for (const char* rel : {"saves/card1.mcd", "psxrecomp/card1.mcd"}) {
        const fs::path c = fs::path(root) / rel;
        if (fs::is_regular_file(c, ec)) {
            a.push_back("--memcard");
            a.push_back(c.string());
            break;
        }
    }
    return a;
}

// Start the oracle, and survive a port pinned to an older psxrecomp.
//
// The tool Studio drives here is whatever THAT port's psxrecomp submodule
// contains, and every port pins its own revision — so `start --memcard` is
// present for some and not others. argparse rejects an unknown flag by
// aborting the whole command, which surfaces as "oracle did not come up" and
// reads like a broken oracle rather than an old pin. Retry once without the
// card and say so: a comparison that starts from a fresh card is worse than
// one that starts from the player's save, but it is far better than none.
void start_oracle(StudioModel& model, const std::string& tool, const std::string& root,
                  const std::string& disc) {
    std::vector<std::string> args = oracle_start_args(model, root, disc);
    const bool has_card =
        std::find(args.begin(), args.end(), "--memcard") != args.end();

    auto done = [&model](RunResult r) {
        model.frm_oracle_note =
            r.ok() ? "oracle started" : "oracle did not come up — see the log";
        refresh_oracle(model, model.selected_root());
    };

    if (!has_card) {
        run_python_script_async(model, tool, std::move(args), done, true, JobSlot::Global);
        return;
    }

    std::vector<std::string> bare;
    for (size_t i = 0; i < args.size(); ++i) {
        if (args[i] == "--memcard") {
            ++i;  // skip its value too
            continue;
        }
        bare.push_back(args[i]);
    }

    run_python_script_async(
        model, tool, std::move(args),
        [&model, tool, bare, done](RunResult r) {
            if (r.ok() || r.stderr_text.find("unrecognized arguments: --memcard") ==
                              std::string::npos) {
                done(r);
                return;
            }
            model.append_log(
                "[warn] this oracle manager does not take `start --memcard` — "
                "retrying without the save card. On DuckStation that means the "
                "port's psxrecomp predates the flag; bump its pin to boot from "
                "the player's own save.");
            model.frm_oracle_note = "retrying without the memory card";
            run_python_script_async(model, tool, bare, done, true, JobSlot::Global);
        },
        true, JobSlot::Global);
}

int oracle_port(const StudioModel& model) {
    if (model.frm_oracle.port) return model.frm_oracle.port;
    // No status yet: fall back to the port the SELECTED oracle's manager pins,
    // not DuckStation's, or the first query goes to a socket nothing is bound
    // to and a healthy Beetle reads as down.
    return model.frm_oracle_kind == OracleKind::Beetle ? 4380 : 4371;
}

// The manager script for the selected oracle. Both answer `status --json`,
// `start` and `stop`; their status shapes differ and parse_oracle_status()
// tells them apart.
std::string oracle_tool_path(const StudioModel& model, const std::string& root) {
    return gpu_tool_path(root, oracle_tool(model.frm_oracle_kind));
}

// Walk an ordering table out of guest RAM and show what the game BUILT, as
// opposed to what the ring says was drawn. Two things need this:
//
//   * the oracle has no GP0 ring, so RAM is the only place its display list
//     exists, and
//   * a routine is identified by the buffer it fills -- pair this with the
//     write trace and an overlay function the static analyser never sees can
//     still be named by what it draws.
void draw_dlist_pane(StudioModel& model, const Theme& th, const std::string& root) {
    DebugClient& dbg = shared_debug_client();
    const bool live = dbg.snapshot().state == DebugClient::State::Connected;
    const std::string tool = gpu_tool_path(root, kDlistTool);

    ImGui::TextWrapped(
        "A PSX game rarely pokes GP0 directly. It builds a linked list in RAM "
        "and hands it to DMA channel 2, so the display list is a data structure "
        "in memory — and reading it is a different act from watching commands "
        "go by. The ring says what was drawn; this says what was built.");
    muted(th, "Walking follows the tag word: bits 31..24 are the payload length, "
              "bits 23..0 point at the next node, 0xFFFFFF ends the list. RAM is "
              "snapshotted once and walked here — a round trip per node would be "
              "hundreds of round trips.");
    ImGui::Separator();

    muted(th, "root");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(150.f);
    ImGui::InputTextWithHint("##dl_addr", "scan for it", model.frm_dlist_addr,
                             sizeof(model.frm_dlist_addr));
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("leave blank to scan RAM for ordering-table chains");
    ImGui::SameLine();
    ImGui::Checkbox("read the oracle", &model.frm_dlist_oracle);

    // Pausing is safe against psx-runtime, whose park loop keeps serving
    // commands, and is NOT safe against the oracle: DuckStation pumps its
    // socket from the emulation loop, so a paused oracle falls back to the Qt
    // idle timer -- 1 Hz with no gamepad attached -- and a 2 MB read times out.
    if (model.frm_dlist_oracle) {
        muted(th, "the oracle is read while running: pausing DuckStation drops "
                  "its debug socket to the idle poll timer (1 Hz with no gamepad) "
                  "and the read cannot finish.");
    }

    const std::string blocked =
        why_disabled(model, tool, kDlistTool, !model.frm_dlist_oracle, live);
    ImGui::BeginDisabled(!blocked.empty());
    if (accent_button(th, "Walk the list")) {
        // Per-side filename. One shared dlist.json meant the second walk
        // silently destroyed the first, so the two could never be compared —
        // which is the question worth asking.
        const std::string outp =
            (fs::path(model.frm_dir) /
             (model.frm_dlist_oracle ? "dlist-oracle.json" : "dlist-native.json"))
                .string();
        std::error_code ec;
        fs::create_directories(model.frm_dir, ec);
        const int port = model.frm_dlist_oracle ? oracle_port(model) : model.frm_port;
        std::vector<std::string> args{"--host", model.frm_host,
                                      "--port", std::to_string(port),
                                      "--json", outp};
        if (model.frm_dlist_addr[0]) {
            args.push_back("--addr");
            args.push_back(model.frm_dlist_addr);
        }
        // If the scan has already named the packet buffer this effect uses,
        // hand it over: a chain covering a known address beats a longer chain
        // that has nothing to do with what you are looking at.
        if (!model.frm_scan.transitions.empty()) {
            for (const auto& t : model.frm_scan.transitions) {
                bool done = false;
                for (const auto& c : t.changes) {
                    if (c.src_lo.empty()) continue;
                    args.push_back("--near");
                    args.push_back(c.src_lo);
                    done = true;
                    break;
                }
                if (done) break;
            }
        }
        // Hold psx-runtime still across the snapshot -- RAM read while the game
        // runs can straddle a rebuild of the very list being walked, producing a
        // plausible-looking list that never existed. Never do this to the
        // oracle; see the note above.
        if (!model.frm_dlist_oracle) args.push_back("--pause");
        model.frm_scan_note = model.frm_dlist_addr[0] ? "snapshotting RAM…"
                                                      : "scanning RAM for lists…";
        run_python_script_async(model, tool, args,
                                [&model, outp, oracle = model.frm_dlist_oracle](RunResult r) {
                                    DisplayList& slot = oracle ? model.frm_dlist_oracle_r
                                                               : model.frm_dlist;
                                    if (r.ok()) {
                                        load_display_list(slot, outp);
                                    } else {
                                        slot = DisplayList{};
                                        slot.error = tool_error(r);
                                    }
                                    model.frm_scan_note.clear();
                                },
                                true, JobSlot::Frames);
    }
    // Walking the two sides one after the other lets the effect animate in
    // between, so the delta ends up describing elapsed time as much as any real
    // divergence. One run, two threads, snapshots as close together as the
    // reads allow.
    ImGui::SameLine();
    const std::string both_blocked = why_disabled(model, tool, kDlistTool, true, live);
    ImGui::BeginDisabled(!both_blocked.empty());
    if (accent_button(th, "Walk both")) {
        std::error_code ec;
        fs::create_directories(model.frm_dir, ec);
        std::vector<std::string> args{"--host", model.frm_host,
                                      "--port", std::to_string(model.frm_port),
                                      "--ds-port", std::to_string(oracle_port(model)),
                                      "--both", "--pause",
                                      "--out-dir", model.frm_dir};
        if (model.frm_dlist_addr[0]) {
            args.push_back("--addr");
            args.push_back(model.frm_dlist_addr);
        }
        for (const auto& t : model.frm_scan.transitions) {
            bool done = false;
            for (const auto& c : t.changes) {
                if (c.src_lo.empty()) continue;
                args.push_back("--near");
                args.push_back(c.src_lo);
                done = true;
                break;
            }
            if (done) break;
        }
        const std::string dir = model.frm_dir;
        model.frm_scan_note = "walking both emulators…";
        run_python_script_async(model, tool, args,
                                [&model, dir](RunResult r) {
                                    // --pause applies to psx-runtime only; the
                                    // tool never pauses the oracle.
                                    load_display_list(
                                        model.frm_dlist,
                                        (fs::path(dir) / "dlist-native.json").string());
                                    load_display_list(
                                        model.frm_dlist_oracle_r,
                                        (fs::path(dir) / "dlist-oracle.json").string());
                                    if (!r.ok() && !model.frm_dlist.loaded &&
                                        !model.frm_dlist_oracle_r.loaded)
                                        model.frm_dlist.error = tool_error(r);
                                    model.frm_scan_note.clear();
                                },
                                true, JobSlot::Frames);
    }
    ImGui::EndDisabled();

    ImGui::EndDisabled();
    if (!blocked.empty()) {
        ImGui::SameLine();
        wrapped(th.warn, "%s", blocked.c_str());
    }
    if (!model.frm_scan_note.empty()) muted(th, "%s", model.frm_scan_note.c_str());

    // ---- side-by-side class comparison ------------------------------------
    // Counts per (opcode, blend) class are what a rendering divergence moves:
    // a layer that lost its blend bit, a burst that vanished. Comparing those
    // needs both walks present at once.
    const DisplayList& nat = model.frm_dlist;
    const DisplayList& orc = model.frm_dlist_oracle_r;
    if (nat.loaded && orc.loaded) {
        ImGui::SeparatorText("psx-runtime vs oracle");
        muted(th, "runtime %d drawing @ %s   |   oracle %d drawing @ %s",
              nat.drawing, nat.root.c_str(), orc.drawing, orc.root.c_str());
        if (ImGui::BeginTable("##dl_cmp", 4,
                              ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg)) {
            ImGui::TableSetupColumn("class");
            ImGui::TableSetupColumn("runtime", ImGuiTableColumnFlags_WidthFixed, 80.f);
            ImGui::TableSetupColumn("oracle", ImGuiTableColumnFlags_WidthFixed, 80.f);
            ImGui::TableSetupColumn("delta", ImGuiTableColumnFlags_WidthFixed, 80.f);
            ImGui::TableHeadersRow();
            std::vector<std::string> keys;
            for (const auto& c : nat.classes) keys.push_back(c.first);
            for (const auto& c : orc.classes)
                if (std::find(keys.begin(), keys.end(), c.first) == keys.end())
                    keys.push_back(c.first);
            const auto count_in = [](const DisplayList& d, const std::string& k) {
                for (const auto& c : d.classes) if (c.first == k) return c.second;
                return 0;
            };
            for (const std::string& k : keys) {
                const int a = count_in(nat, k), b = count_in(orc, k);
                ImGui::TableNextRow();
                ImGui::TableNextColumn();
                ImGui::TextUnformatted(k.c_str());
                ImGui::TableNextColumn(); ImGui::Text("%d", a);
                ImGui::TableNextColumn(); ImGui::Text("%d", b);
                ImGui::TableNextColumn();
                if (a == b) muted(th, "same");
                else ImGui::TextColored(a == 0 || b == 0 ? th.bad : th.warn,
                                        "%+d", b - a);
            }
            ImGui::EndTable();
        }
        muted(th, "Both sides must be at the same point in the animation for a "
                  "delta to mean anything — an effect mid-cycle differs from "
                  "itself frame to frame.");
    }

    const DisplayList& dl = model.frm_dlist_oracle ? orc : nat;
    if (!dl.error.empty()) {
        wrapped(th.bad, "%s", dl.error.c_str());
        return;
    }
    if (!dl.loaded) return;
    ImGui::SeparatorText(model.frm_dlist_oracle ? "oracle" : "psx-runtime");

    ImGui::Separator();
    muted(th, "root %s — %d node(s), %d drawing", dl.root.c_str(), dl.nodes,
          dl.drawing);
    for (const auto& c : dl.classes)
        muted(th, "  %-28s %5d", c.first.c_str(), c.second);

    if (ImGui::BeginTable("##dl_prims", 5,
                          ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                          ImGuiTableFlags_ScrollY | ImGuiTableFlags_Resizable,
                          ImVec2(0.f, 320.f))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("opcode", ImGuiTableColumnFlags_WidthFixed, 130.f);
        ImGui::TableSetupColumn("blend", ImGuiTableColumnFlags_WidthFixed, 70.f);
        ImGui::TableSetupColumn("cmax", ImGuiTableColumnFlags_WidthFixed, 50.f);
        ImGui::TableSetupColumn("source", ImGuiTableColumnFlags_WidthFixed, 100.f);
        ImGui::TableSetupColumn("vertices / colours");
        ImGui::TableHeadersRow();
        ImGuiListClipper clip;
        clip.Begin(static_cast<int>(dl.prims.size()));
        while (clip.Step()) {
            for (int i = clip.DisplayStart; i < clip.DisplayEnd; ++i) {
                const DisplayPrim& d = dl.prims[static_cast<size_t>(i)];
                ImGui::TableNextRow();
                ImGui::TableNextColumn();
                ImGui::TextUnformatted(d.op.c_str());
                ImGui::TableNextColumn();
                ImGui::TextUnformatted(d.blend.c_str());
                ImGui::TableNextColumn();
                ImGui::Text("%d", d.cmax);
                ImGui::TableNextColumn();
                // The source address is the handle the write trace takes: it
                // turns "some code built this" into a named PC.
                if (ImGui::Selectable(d.src.c_str())) ImGui::SetClipboardText(d.src.c_str());
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("click to copy — feed this to the write "
                                      "trace to find the code that built it");
                ImGui::TableNextColumn();
                ImGui::TextUnformatted(d.verts.c_str());
                if (!d.colors.empty()) muted(th, "%s", d.colors.c_str());
            }
        }
        ImGui::EndTable();
    }
}

// Shading: where wrong COLOURS come from.
//
// The display-list comparison establishes what this pane is for. Geometry
// matches between the emulators primitive for primitive; flat-coloured
// primitives match exactly; shaded ones do not. So the question is no longer
// "is the renderer wrong" but "which code computes these colours", and there
// are only two candidates: the GTE, or CPU code writing the packet directly.
// This pane answers both without leaving the GUI.
void draw_shading_pane(StudioModel& model, const Theme& th, const std::string& root) {
    DebugClient& dbg = shared_debug_client();
    const bool live = dbg.snapshot().state == DebugClient::State::Connected;
    const std::string wtool = gpu_tool_path(root, kWritersTool);
    const std::string gtool = gpu_tool_path(root, kGteTool);

    ImGui::TextWrapped(
        "Geometry matches between the emulators and flat colours match exactly, "
        "so a colour-only difference is computed somewhere upstream of the "
        "renderer. On PSX that is either the GTE or CPU code writing the packet. "
        "These two buttons separate those cases.");
    ImGui::TextColored(th.accent_text,
        "Press the button FIRST, then trigger the effect in-game.");
    muted(th, "Every tool here needs the effect running, and each waits up to "
              "two minutes for it to appear — on each emulator independently, "
              "so both do not have to be inside it at the same moment. Losing "
              "that race used to produce a different-looking failure in each "
              "tool: no writes recorded, no candidate fired, no table found, "
              "the block was not reached.");
    ImGui::Separator();

    // ---- 1. who writes the colour words ------------------------------------
    ImGui::SeparatorText("Packet colour writers");
    muted(th, "Attribution by ADDRESS, not by function: an ordering-table game "
              "reports the same submit PC for every packet on screen, so 'which "
              "function drew this' has one answer per frame and is useless. This "
              "watches the packet's colour words and reports who stores to them "
              "— which works even for overlay code the analyser never sees.");

    muted(th, "class");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(190.f);
    ImGui::InputText("##wclass", model.frm_writer_class,
                     sizeof(model.frm_writer_class));
    ImGui::SameLine();
    muted(th, "step");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(60.f);
    ImGui::InputInt("##wwatch", &model.frm_writer_watch, 0, 0);
    if (model.frm_writer_watch < 1) model.frm_writer_watch = 1;
    if (model.frm_writer_watch > 8) model.frm_writer_watch = 8;
    ImGui::SameLine();
    muted(th, "frame(s)");
    muted(th, "The game is parked, walked, stepped exactly this many frames, "
              "then walked again to confirm the layout held. Free-running the "
              "trace does not work: the packet buffer is rebuilt at a different "
              "address most frames, so the addresses go stale and every "
              "instruction smears to ~50%% colour / 50%% vertex.");

    const std::string wblocked = why_disabled(model, wtool, kWritersTool, true, live);
    ImGui::BeginDisabled(!wblocked.empty());
    if (accent_button(th, "Find colour writers")) {
        const std::string outp =
            (fs::path(model.frm_dir) / "packet_writers.json").string();
        std::error_code ec;
        fs::create_directories(model.frm_dir, ec);
        model.frm_scan_note = "tracing packet writes…";
        model.frm_writer_disasm.clear();
        model.frm_writer_sel = -1;
        run_python_script_async(model, wtool,
                                {"--host", model.frm_host,
                                 "--port", std::to_string(model.frm_port),
                                 "--class", model.frm_writer_class,
                                 "--frames", std::to_string(model.frm_writer_watch),
                                 "--json", outp},
                                [&model, outp](RunResult r) {
                                    // A non-zero exit also carries a report when
                                    // the class simply was not on screen, which
                                    // is a finding, not a failure.
                                    if (!load_packet_writers(model.frm_writers_pkt, outp)
                                        && !r.ok())
                                        model.frm_writers_pkt.error = tool_error(r);
                                    model.frm_scan_note.clear();
                                },
                                true, JobSlot::Frames);
    }
    ImGui::EndDisabled();
    if (!wblocked.empty()) {
        ImGui::SameLine();
        wrapped(th.warn, "%s", wblocked.c_str());
    }

    const PacketWriters& pw = model.frm_writers_pkt;
    if (!pw.error.empty()) wrapped(th.bad, "%s", pw.error.c_str());
    if (pw.loaded && pw.absent) {
        ImGui::TextColored(th.warn,
            "%s is not on screen right now.", pw.klass.c_str());
        muted(th, "Get the game to the moment the effect is visible, then run "
                  "this again — otherwise it would report on whatever else "
                  "happens to be in the buffer.");
        if (!pw.present.empty()) {
            muted(th, "currently drawing:");
            for (const auto& c : pw.present)
                muted(th, "    %-26s %5d", c.first.c_str(), c.second);
        }
    } else if (pw.loaded && pw.no_writes) {
        // An empty table is indistinguishable from a finished search, which is
        // the worst way for a tool to fail. Say what happened.
        ImGui::TextColored(th.bad,
            "The trace recorded no writes at all, so there is nothing to show.");
        ImGui::TextWrapped(
            "The %d packet(s) were walked successfully, so they ARE on screen — "
            "this means the trace window missed them, not that nothing writes "
            "them. Step more frames: the buffer may be rebuilt less often than "
            "every frame.", pw.packets);
        if (!pw.note.empty()) muted(th, "%s", pw.note.c_str());
    } else if (pw.loaded) {
        muted(th, "%d packet(s), fields %s..%s — %d colour words, %d vertex "
                  "words, %d write(s) seen",
              pw.packets, pw.lo.c_str(), pw.hi.c_str(), pw.colour_words,
              pw.vertex_words, pw.writes);
        if (pw.unmapped)
            muted(th, "%d write(s) landed outside the walked layout — the list "
                      "was rebuilt mid-trace; those are not attributed.",
                  pw.unmapped);
        if (pw.aliased) {
            ImGui::TextColored(th.bad,
                "UNRELIABLE: %d instruction(s) split about evenly between "
                "colour and vertex words.", pw.mixed_writers);
            ImGui::TextWrapped(
                "A single store writes ONE field, so an even split is the packet "
                "buffer having moved during the trace — not code that writes "
                "both. Every row below is an artefact. Step fewer frames.");
        }
        ImGui::BeginDisabled(pw.aliased);
        if (ImGui::BeginTable("##pw", 5,
                              ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_ScrollY, ImVec2(0.f, 210.f))) {
            ImGui::TableSetupScrollFreeze(0, 1);
            ImGui::TableSetupColumn("pc", ImGuiTableColumnFlags_WidthFixed, 110.f);
            ImGui::TableSetupColumn("func", ImGuiTableColumnFlags_WidthFixed, 110.f);
            ImGui::TableSetupColumn("colour", ImGuiTableColumnFlags_WidthFixed, 70.f);
            ImGui::TableSetupColumn("vertex", ImGuiTableColumnFlags_WidthFixed, 70.f);
            ImGui::TableSetupColumn("");
            ImGui::TableHeadersRow();
            for (int i = 0; i < (int)pw.writers.size(); ++i) {
                const PacketWriter& w = pw.writers[(size_t)i];
                ImGui::TableNextRow();
                ImGui::TableNextColumn();
                if (ImGui::Selectable(w.pc.c_str(), model.frm_writer_sel == i,
                                      ImGuiSelectableFlags_SpanAllColumns))
                    model.frm_writer_sel = i;
                ImGui::TableNextColumn();
                ImGui::TextUnformatted(w.func.c_str());
                ImGui::TableNextColumn();
                if (w.colour) ImGui::TextColored(th.accent_text, "%d", w.colour);
                else muted(th, "-");
                ImGui::TableNextColumn();
                ImGui::Text("%d", w.vertex);
                ImGui::TableNextColumn();
                // The interesting row: writes colour and never geometry.
                if (w.colour_only)
                    ImGui::TextColored(th.warn, "colour only");
            }
            ImGui::EndTable();
        }
        ImGui::EndDisabled();

        // Code captured with the trace, shown inline. No second click, and no
        // risk of decoding a different overlay than the one that ran.
        if (model.frm_writer_sel >= 0 && !pw.aliased &&
            model.frm_writer_sel < (int)pw.writers.size()) {
            const PacketWriter& sel = pw.writers[(size_t)model.frm_writer_sel];
            if (!sel.listing.empty()) {
                ImGui::SeparatorText(("code at " + sel.pc).c_str());
                if (ImGui::BeginChild("##lst", ImVec2(0.f, 220.f), true)) {
                    for (const DisasmLine& d : sel.listing) {
                        if (d.is_target) {
                            ImGui::TextColored(th.accent_text, ">> %s: %s  %s",
                                               d.pc.c_str(), d.word.c_str(),
                                               d.text.c_str());
                        } else {
                            muted(th, "   %s: %s  %s", d.pc.c_str(),
                                  d.word.c_str(), d.text.c_str());
                        }
                    }
                }
                ImGui::EndChild();
                muted(th, "Captured while parked for the trace — these addresses "
                          "are overlay code, so the same PC decodes differently "
                          "once another overlay loads.");
            }
        }

        const bool have_sel = !pw.aliased && model.frm_writer_sel >= 0 &&
                              model.frm_writer_sel < (int)pw.writers.size();
        const std::string dtool = gpu_tool_path(root, kDisasmTool);
        ImGui::BeginDisabled(!have_sel || dtool.empty() ||
                             model.busy_frames.load() || !live);
        if (ImGui::Button("Disassemble around this PC")) {
            const std::string pc = pw.writers[(size_t)model.frm_writer_sel].pc;
            model.frm_scan_note = "disassembling " + pc + "…";
            model.frm_writer_disasm.clear();
            run_python_script_async(model, dtool,
                                    {"--host", model.frm_host,
                                     "--port", std::to_string(model.frm_port),
                                     "--pc", pc, "--count", "48"},
                                    [&model](RunResult r) {
                                        model.frm_writer_disasm =
                                            r.ok() ? r.stdout_text : tool_error(r);
                                        model.frm_scan_note.clear();
                                    },
                                    true, JobSlot::Frames);
        }
        ImGui::EndDisabled();
        if (!have_sel) {
            ImGui::SameLine();
            muted(th, "select a row first");
        }
        if (!model.frm_writer_disasm.empty()) {
            ImGui::InputTextMultiline("##wdis",
                                      model.frm_writer_disasm.data(),
                                      model.frm_writer_disasm.size() + 1,
                                      ImVec2(-1.f, 200.f),
                                      ImGuiInputTextFlags_ReadOnly);
        }
    }

    // ---- 1b. which INPUT is wrong ------------------------------------------
    ImGui::SeparatorText("Colour inputs");
    muted(th, "The routine above computes every colour as source_rgb * scale >> 7, "
              "so a wrong colour is either a wrong SOURCE or a wrong SCALE. The "
              "oracle can break on a PC and hand back all 32 registers; one of "
              "them is the source pointer, and once that address is known it can "
              "be read on BOTH emulators. The comparison that settles it is of "
              "memory, not registers — which is why psx-runtime not having a "
              "PC breakpoint does not block this.");

    const bool sel_ok = model.frm_writer_sel >= 0 && !pw.aliased &&
                        model.frm_writer_sel < (int)pw.writers.size();
    const std::string itool = gpu_tool_path(root, kInputsTool);
    const std::string iblocked = why_disabled(model, itool, kInputsTool, true, live);
    ImGui::BeginDisabled(!iblocked.empty() || !sel_ok);
    if (accent_button(th, "Compare colour inputs")) {
        const std::string outp =
            (fs::path(model.frm_dir) / "colour_inputs.json").string();
        std::error_code ec;
        fs::create_directories(model.frm_dir, ec);
        model.frm_scan_note = "waiting for the oracle to reach that PC…";
        run_python_script_async(model, itool,
                                {"--host", model.frm_host,
                                 "--native-port", std::to_string(model.frm_port),
                                 "--ds-port", std::to_string(oracle_port(model)),
                                 "--pc", pw.writers[(size_t)model.frm_writer_sel].pc,
                                 "--json", outp},
                                [&model, outp](RunResult r) {
                                    if (!load_colour_inputs(model.frm_cinputs, outp)
                                        && !r.ok())
                                        model.frm_cinputs.error = tool_error(r);
                                    model.frm_scan_note.clear();
                                },
                                true, JobSlot::Frames);
    }
    ImGui::EndDisabled();
    if (!iblocked.empty()) {
        ImGui::SameLine();
        wrapped(th.warn, "%s", iblocked.c_str());
    } else if (!sel_ok) {
        ImGui::SameLine();
        muted(th, "select a colour-only row above");
    }

    const ColourInputs& ci = model.frm_cinputs;
    if (ci.loaded && !ci.error.empty()) {
        wrapped(th.bad, "%s", ci.error.c_str());
    } else if (ci.loaded) {
        muted(th, "oracle table at %s, scale = %d (%.3f)",
              ci.source_addr.c_str(), ci.scale, ci.scale / 128.0);
        if (!ci.native_s4.empty()) {
            ImGui::TextColored(th.good,
                "psx-runtime reported its OWN $s4 = %s, $s6 = %s (block %s)",
                ci.native_s4.c_str(), ci.native_s6.c_str(),
                ci.native_block.c_str());
            muted(th, "Both sides are being read through their own pointers, so "
                      "this compares the tables each routine actually uses — not "
                      "one address assumed to mean the same thing on both.");
        } else if (!ci.probe_error.empty()) {
            ImGui::TextColored(th.warn,
                "psx-runtime's block probe did not report $s4: %s",
                ci.probe_error.c_str());
            muted(th, "Falling back to searching RAM by content, which cannot "
                      "know which table psx-runtime's code actually reads.");
        }
        if (ci.compared_by == "native-own-pointer" && ci.address_delta)
            muted(th, "address delta %+d (0x%X) — a %s0x8000 delta is the "
                      "double buffer, not a divergence",
                  ci.address_delta, ci.address_delta < 0 ? -ci.address_delta
                                                         : ci.address_delta,
                  ci.address_delta < 0 ? "-" : "+");
        if (ci.verdict == "region-matches" || ci.verdict == "region-differs") {
            muted(th, "scales differ (psx-runtime %d, oracle %d) — the two are "
                      "at different points in the animation and $s4 moves with "
                      "it, so the whole region %s..%s was compared instead.",
                  ci.native_scale, ci.scale, ci.region_lo.c_str(),
                  ci.region_hi.c_str());
            if (ci.verdict == "region-matches") {
                ImGui::TextColored(th.good,
                    "The whole %d-byte region is IDENTICAL on both. The colour "
                    "data is fine wherever each pointer sits — the divergence "
                    "is the scale, the arithmetic, or which entry each side "
                    "selects.", ci.region_bytes);
            } else {
                ImGui::TextColored(th.bad,
                    "%d of %d bytes of the region differ, first at %s. A real "
                    "data divergence — it does not depend on where either "
                    "pointer happened to be.",
                    ci.region_differing, ci.region_bytes,
                    ci.region_first_difference.c_str());
                if (!ci.region_shape.empty())
                    ImGui::TextWrapped("%s", ci.region_shape.c_str());
                for (const auto& k : ci.region_clusters) {
                    muted(th, "  %s (%d bytes)", k.addr.c_str(), k.length);
                    muted(th, "     psx-runtime  %s", k.native.c_str());
                    muted(th, "     oracle       %s", k.oracle.c_str());
                }
            }
        } else if (ci.verdict == "table-rearranged") {
            ImGui::TextColored(th.warn,
                "The same colour values exist on psx-runtime, but not in this "
                "arrangement.");
            ImGui::TextWrapped(
                "%d leading byte(s) of the oracle's table appear %zu time(s)%s. "
                "So this is not missing data — it is a different layout, and at "
                "the oracle's own pointer address psx-runtime holds something "
                "that is not a colour table at all.",
                ci.partial_len, ci.partial_hits.size(),
                ci.partial_stride
                    ? (" on a 0x" + std::to_string(ci.partial_stride) + " stride").c_str()
                    : "");
            ImGui::TextWrapped(
                "What this CANNOT say: which table psx-runtime's own code reads. "
                "That needs its $s4, and psx-runtime has no PC breakpoint.");
        } else if (ci.verdict == "table-absent-on-native") {
            ImGui::TextColored(th.bad,
                "psx-runtime's RAM does not contain this colour table ANYWHERE.");
            ImGui::TextWrapped(
                "The oracle's table is self-confirmed — its entries scaled by the "
                "recorded factor reproduce the colours in its own display list. "
                "So the data psx-runtime is scaling was never built the same way, "
                "and the divergence is upstream of this routine, in whatever "
                "produces the table.");
        } else if (!ci.native_source_addr.empty()) {
            muted(th, "same table found on psx-runtime at %s (delta %+d) — "
                      "located by content, since the two allocators place it at "
                      "different addresses",
                  ci.native_source_addr.c_str(), ci.address_delta);
        }
        if (ci.verdict == "source-matches") {
            ImGui::TextColored(th.warn,
                "The source RGB is IDENTICAL on both. The input is fine, so the "
                "divergence is the SCALE or the arithmetic in this routine — "
                "look at the code above, not upstream.");
        } else if (ci.verdict == "source-differs") {
            ImGui::TextColored(th.bad,
                "The source RGB DIFFERS (%d byte(s), first at %s). This routine "
                "is faithfully scaling bad input — the fault is UPSTREAM, in "
                "whatever fills that table.",
                ci.differing, ci.first_difference.c_str());
        }
        if (!ci.native_bytes.empty()) {
            muted(th, "psx-runtime  %.32s", ci.native_bytes.c_str());
            muted(th, "oracle       %.32s", ci.oracle_bytes.c_str());
        }
    }

    // A bare register read, useful on its own: psx-runtime has no PC
    // breakpoint, so this is the only way to see what its code was holding.
    if (sel_ok) {
        ImGui::SameLine();
        const std::string ptool = gpu_tool_path(root, kProbeTool);
        ImGui::BeginDisabled(ptool.empty() || !live || model.busy_frames.load());
        if (ImGui::Button("Read psx-runtime registers")) {
            const std::string outp =
                (fs::path(model.frm_dir) / "probe_regs.json").string();
            std::error_code ec;
            fs::create_directories(model.frm_dir, ec);
            model.frm_scan_note = "arming block-leader candidates…";
            run_python_script_async(model, ptool,
                                    {"--host", model.frm_host,
                                     "--port", std::to_string(model.frm_port),
                                     "--pc", pw.writers[(size_t)model.frm_writer_sel].pc,
                                     "--want", "s4,s6,sp,a3",
                                     "--json", outp},
                                    [&model](RunResult r) {
                                        model.frm_status = r.ok()
                                            ? "registers captured — see the log"
                                            : tool_error(r);
                                        model.frm_scan_note.clear();
                                    },
                                    true, JobSlot::Frames);
        }
        ImGui::EndDisabled();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Values are read at the enclosing BLOCK's entry. "
                              "Callee-saved registers ($s4/$s6) are the same "
                              "there as at the instruction; temporaries are not.");
    }

    // ---- 1b2. is anything simply NOT DRAWN? --------------------------------
    ImGui::SeparatorText("What each side ever draws");
    muted(th, "A single capture is confounded by phase: a class-count "
              "difference could mean 'this emulator does not draw that' or "
              "'it was not drawing it just then'. Maxima are not symmetric that "
              "way — phase can hide a primitive in one frame, not in forty. A "
              "class the runtime NEVER reaches is not being submitted at all.");

    ImGui::SetNextItemWidth(70.f);
    ImGui::InputInt("##cens", &model.frm_census_samples, 0, 0);
    if (model.frm_census_samples < 5) model.frm_census_samples = 5;
    ImGui::SameLine();
    muted(th, "captures per side");
    ImGui::SameLine();
    {
        const std::string ctool = gpu_tool_path(root, kCensusTool);
        const std::string cblocked = why_disabled(model, ctool, kCensusTool, true, live);
        ImGui::BeginDisabled(!cblocked.empty());
        if (accent_button(th, "Census both sides")) {
            const std::string outp =
                (fs::path(model.frm_dir) / "class_census.json").string();
            std::error_code ec;
            fs::create_directories(model.frm_dir, ec);
            model.frm_scan_note = "capturing display lists on both…";
            run_python_script_async(model, ctool,
                                    {"--host", model.frm_host,
                                     "--native-port", std::to_string(model.frm_port),
                                     "--ds-port", std::to_string(oracle_port(model)),
                                     "--samples", std::to_string(model.frm_census_samples),
                                     "--json", outp},
                                    [&model, outp](RunResult r) {
                                        if (!load_class_census(model.frm_census, outp)
                                            && !r.ok())
                                            model.frm_census.error = tool_error(r);
                                        model.frm_scan_note.clear();
                                    },
                                    true, JobSlot::Frames);
        }
        ImGui::EndDisabled();
        if (!cblocked.empty()) {
            ImGui::SameLine();
            wrapped(th.warn, "%s", cblocked.c_str());
        }
    }

    const ClassCensus& cen = model.frm_census;
    if (cen.loaded && !cen.error.empty()) {
        wrapped(th.bad, "%s", cen.error.c_str());
    } else if (cen.loaded) {
        if (ImGui::BeginTable("##cen", 4, ImGuiTableFlags_Borders |
                              ImGuiTableFlags_RowBg | ImGuiTableFlags_ScrollY,
                              ImVec2(0.f, 180.f))) {
            ImGui::TableSetupScrollFreeze(0, 1);
            ImGui::TableSetupColumn("class");
            ImGui::TableSetupColumn("runtime max", ImGuiTableColumnFlags_WidthFixed, 95.f);
            ImGui::TableSetupColumn("oracle max", ImGuiTableColumnFlags_WidthFixed, 90.f);
            ImGui::TableSetupColumn("gap", ImGuiTableColumnFlags_WidthFixed, 70.f);
            ImGui::TableHeadersRow();
            for (const CensusRow& r : cen.rows) {
                const bool flagged =
                    std::find(cen.absent.begin(), cen.absent.end(), r.key)
                    != cen.absent.end();
                ImGui::TableNextRow();
                ImGui::TableNextColumn();
                if (flagged) ImGui::TextColored(th.bad, "%s", r.key.c_str());
                else ImGui::TextUnformatted(r.key.c_str());
                ImGui::TableNextColumn(); ImGui::Text("%d", r.native_max);
                ImGui::TableNextColumn(); ImGui::Text("%d", r.oracle_max);
                ImGui::TableNextColumn();
                const int gap = r.oracle_max - r.native_max;
                if (gap) ImGui::TextColored(flagged ? th.bad : th.text_muted,
                                            "%+d", gap);
            }
            ImGui::EndTable();
        }
        if (cen.verdict == "classes-absent-on-native") {
            ImGui::TextColored(th.bad,
                "psx-runtime never draws these across %d captures spanning the "
                "effect.", cen.samples);
            ImGui::TextWrapped(
                "Phase can hide a primitive in one frame; it cannot hide it in "
                "%d. These are not being SUBMITTED, which puts the fault in the "
                "guest code that builds them — not in the renderer.",
                cen.samples);
        } else if (cen.verdict == "no-systematic-absence") {
            ImGui::TextColored(th.good,
                "No class is systematically absent — every one the runtime "
                "draws fewer of at some moment, it reaches comparable counts at "
                "another. The single-capture differences were phase.");
        }
    }

    // ---- 1c. is the RECOMPILATION wrong? -----------------------------------
    ImGui::SeparatorText("Compiled vs interpreter (lockstep)");
    muted(th, "Runs the same guest code twice inside psx-runtime — once "
              "compiled, once interpreted — and reports the first place they "
              "disagree. No oracle, no frame alignment, no address "
              "correspondence: the three things every cross-emulator comparison "
              "here has had to fight, each of which has produced a confident "
              "wrong answer at least once.");

    ImGui::SetNextItemWidth(80.f);
    ImGui::InputInt("##lsframes", &model.frm_ls_frames, 0, 0);
    if (model.frm_ls_frames < 1) model.frm_ls_frames = 1;
    ImGui::SameLine();
    muted(th, "frames");
    ImGui::SameLine();
    ImGui::Checkbox("whole dispatch segments", &model.frm_ls_func);
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Blocks are finer and skip less. Segments cover more "
                          "code per comparison but skip anything they cannot "
                          "replay.");

    const std::string lstool = gpu_tool_path(root, kLockstepTool);
    const std::string lsblocked = why_disabled(model, lstool, kLockstepTool, true, live);
    ImGui::BeginDisabled(!lsblocked.empty());
    if (accent_button(th, "Run lockstep")) {
        const std::string outp = (fs::path(model.frm_dir) / "lockstep.json").string();
        std::error_code ec;
        fs::create_directories(model.frm_dir, ec);
        std::vector<std::string> a{"--host", model.frm_host,
                                   "--port", std::to_string(model.frm_port),
                                   "--frames", std::to_string(model.frm_ls_frames),
                                   "--json", outp};
        if (model.frm_ls_func) a.push_back("--func");
        model.frm_scan_note = "running the window, then reading back…";
        run_python_script_async(model, lstool, a,
                                [&model, outp](RunResult r) {
                                    if (!load_lockstep(model.frm_lockstep, outp)
                                        && !r.ok())
                                        model.frm_lockstep.error = tool_error(r);
                                    model.frm_scan_note.clear();
                                },
                                true, JobSlot::Frames);
    }
    ImGui::EndDisabled();
    if (!lsblocked.empty()) {
        ImGui::SameLine();
        wrapped(th.warn, "%s", lsblocked.c_str());
    }

    const Lockstep& ls = model.frm_lockstep;
    if (ls.loaded && !ls.error.empty()) {
        wrapped(th.bad, "%s", ls.error.c_str());
    } else if (ls.loaded) {
        muted(th, "frames %d..%d — %llu checked, %llu skipped",
              ls.window_lo, ls.window_hi,
              (unsigned long long)ls.checked, (unsigned long long)ls.skipped);
        if (ls.verdict == "inconclusive") {
            // Nothing checked produces the same "found: false" a clean run
            // does. Saying "no divergence" here would be the worst kind of
            // wrong: a pass that was never earned.
            ImGui::TextColored(th.bad,
                "INCONCLUSIVE — nothing was checked. This is NOT a clean "
                "result; it is the same output a clean run gives. Widen the "
                "window, or confirm the comparator is enabled in this build.");
        } else if (ls.verdict == "weak") {
            ImGui::TextColored(th.warn,
                "WEAK — %llu skipped against %llu checked (%.1fx). A divergence "
                "could easily sit in what was skipped, so this is not evidence "
                "of anything.",
                (unsigned long long)ls.skipped, (unsigned long long)ls.checked,
                ls.checked ? double(ls.skipped) / double(ls.checked) : 0.0);
            if (ls.dominant_skip == "skipped_irq" && ls.mode == "lockstep_func")
                ImGui::TextWrapped(
                    "Almost all of those were segments containing an interrupt, "
                    "which cannot be replayed. That is a property of SEGMENT "
                    "granularity in an interrupt-heavy game, not a fault — "
                    "untick 'whole dispatch segments' and run it again.");
        } else if (ls.verdict == "clean") {
            ImGui::TextColored(th.good,
                "No divergence across %llu unit(s). Within this window the "
                "compiled code matched the interpreter everywhere it was "
                "compared, so recompilation is not the fault here.",
                (unsigned long long)ls.checked);
        } else if (ls.found) {
            wrapped(th.bad, "FIRST DIVERGENCE (%s)", ls.kind.c_str());
            ImGui::TextWrapped("%s", ls.meaning.empty()
                ? "the interpreter and the compiled code produced different results"
                : ls.meaning.c_str());
            if (ImGui::BeginTable("##ls", 2, ImGuiTableFlags_Borders |
                                  ImGuiTableFlags_RowBg)) {
                const auto row = [&](const char* k, const std::string& v) {
                    if (v.empty()) return;
                    ImGui::TableNextRow();
                    ImGui::TableNextColumn(); muted(th, "%s", k);
                    ImGui::TableNextColumn(); ImGui::TextUnformatted(v.c_str());
                };
                row("frame", ls.frame);
                row(ls.mode == "lockstep_func" ? "segment" : "block", ls.block);
                row("pc", ls.pc);
                row("address", ls.addr);
                if (ls.reg >= 0) row("register", "$" + std::to_string(ls.reg));
                row("interpreter expected", ls.expected);
                row("compiled produced", ls.actual);
                ImGui::EndTable();
            }
            if (!ls.trace.empty()) {
                muted(th, "last memory ops before it:");
                for (size_t i = ls.trace.size() > 10 ? ls.trace.size() - 10 : 0;
                     i < ls.trace.size(); ++i)
                    muted(th, "   %s", ls.trace[i].c_str());
            }
        }
    }

    // ---- 1c2. does the fade animate at all? --------------------------------
    ImGui::SeparatorText("Does the fade animate?");
    muted(th, "Every vertex colour here is source_rgb * $s6 >> 7, so $s6 IS the "
              "fade: 128 leaves the colour unchanged, smaller values darken it. "
              "This samples it repeatedly on both sides and compares VARIATION "
              "rather than values — which needs no frame alignment, no matching "
              "buffer half and no shared phase, the three things that have "
              "produced wrong answers here.");

    if (sel_ok) {
        ImGui::SetNextItemWidth(70.f);
        ImGui::InputInt("##stn", &model.frm_scale_samples, 0, 0);
        if (model.frm_scale_samples < 3) model.frm_scale_samples = 3;
        ImGui::SameLine();
        muted(th, "samples");
        ImGui::SameLine();
        ImGui::Checkbox("per frame", &model.frm_scale_per_frame);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Step ONE frame between reads, so the differences "
                              "are the real animation increment. Free-running "
                              "samples land about 90 frames apart, and their "
                              "differences are aliasing rather than the step.\n\n"
                              "In this mode the ORACLE IS OPTIONAL: consecutive "
                              "frames answer 'does our fade sweep or jump' on "
                              "their own, so both emulators do not have to be "
                              "inside the effect at the same moment.");
        ImGui::SameLine();
        const std::string stool = gpu_tool_path(root, kScaleTool);
        const std::string sblocked = why_disabled(model, stool, kScaleTool, true, live);
        ImGui::BeginDisabled(!sblocked.empty());
        if (accent_button(th, "Trace the scale")) {
            const std::string outp =
                (fs::path(model.frm_dir) / "scale_trace.json").string();
            std::error_code ec;
            fs::create_directories(model.frm_dir, ec);
            model.frm_scan_note = "sampling the scale on both sides…";
            std::vector<std::string> a2{
                "--host", model.frm_host,
                "--native-port", std::to_string(model.frm_port),
                "--ds-port", std::to_string(oracle_port(model)),
                "--pc", pw.writers[(size_t)model.frm_writer_sel].pc,
                "--samples", std::to_string(model.frm_scale_samples),
                "--json", outp};
            // A flag is present or absent; there is no "off" spelling. Passing
            // a bare "--gap" as the off case hands argparse an option with no
            // value, which fails before the tool runs at all.
            if (model.frm_scale_per_frame) a2.push_back("--per-frame");
            run_python_script_async(model, stool, a2,
                                    [&model, outp](RunResult r) {
                                        if (!load_scale_trace(model.frm_scale, outp)
                                            && !r.ok())
                                            model.frm_scale.error = tool_error(r);
                                        model.frm_scan_note.clear();
                                    },
                                    true, JobSlot::Frames);
        }
        ImGui::EndDisabled();
        if (!sblocked.empty()) {
            ImGui::SameLine();
            wrapped(th.warn, "%s", sblocked.c_str());
        }
    } else {
        muted(th, "select a colour-only row above");
    }

    const ScaleTrace& st = model.frm_scale;
    if (st.loaded && !st.error.empty()) {
        wrapped(th.bad, "%s", st.error.c_str());
    } else if (st.loaded) {
        const auto side = [&](const char* label, const ScaleSide& s) {
            if (!s.loaded || !s.samples) {
                muted(th, "  %-12s no samples", label);
                return;
            }
            ImGui::Text("  %-12s %2d sample(s), %d distinct, range %d..%d   [%s]",
                        label, s.samples, s.distinct, s.min, s.max,
                        s.values.c_str());
        };
        side("psx-runtime", st.native);
        side("oracle", st.oracle);

        if (st.verdict == "incomplete") {
            // Say which side, and why — and keep the half that did answer,
            // because one side alone can still settle a question about itself.
            const std::string& why = st.oracle.samples ? st.native_error
                                                       : st.oracle_error;
            ImGui::TextColored(th.warn, "INCOMPLETE — %s produced no samples.",
                               st.oracle.samples ? "psx-runtime" : "the oracle");
            if (!why.empty()) ImGui::TextWrapped("%s", why.c_str());
            const ScaleSide& got = st.native.samples ? st.native : st.oracle;
            if (got.samples && !got.constant)
                ImGui::TextWrapped(
                    "Still recorded: that side's %s DOES vary (%d..%d), so it "
                    "is not pinned.", st.reg.c_str(), got.min, got.max);
        } else if (st.verdict == "native-not-animating") {
            ImGui::TextColored(th.bad,
                "psx-runtime's %s never moves while the oracle's sweeps.",
                st.reg.c_str());
            if (st.native.neutral_only)
                ImGui::TextWrapped(
                    "And it sits at 128 — the NEUTRAL scale, where "
                    "x*128>>7 leaves x unchanged. No fade is being applied at "
                    "all, which is exactly what bright unfaded polygons would "
                    "look like.");
        } else if (st.verdict == "both-animate") {
            ImGui::TextColored(th.good,
                "Both sweep — the fade is not simply missing, so the difference "
                "is in the values rather than in whether it runs.");
            if (st.granularity == "native-measured-per-frame")
                ImGui::TextWrapped(
                    "psx-runtime, sampled on CONSECUTIVE frames, moves in steps "
                    "of up to %d. That is its real animation increment — the "
                    "oracle's series is not frame-adjacent, so its apparent "
                    "step is an upper bound only.", st.native.max_step);
            else if (st.granularity == "native-coarser")
                ImGui::TextColored(th.bad,
                    "psx-runtime moves in far coarser steps (up to %d) than the "
                    "oracle (up to %d).", st.native.max_step, st.oracle.max_step);
            else if (st.granularity == "too-few-samples")
                muted(th, "Too few samples to compare HOW each moves, and "
                          "free-running samples land ~90 frames apart — tick "
                          "'per frame' so the differences mean something.");
        } else if (st.verdict == "both-constant") {
            ImGui::TextColored(th.warn,
                "Neither side varies. Most likely the effect is not animating "
                "right now on either, which says nothing about the bug.");
        } else if (st.verdict == "native-per-frame-only") {
            // A per-frame series from psx-runtime answers this on its own, so
            // an absent oracle is not a failure here.
            ImGui::TextColored(st.native.max_step <= 8 ? th.good : th.bad,
                "psx-runtime over %d CONSECUTIVE frames: steps up to %d "
                "(median %d).", st.native.samples, st.native.max_step,
                st.native.median_step);
            if (!st.note.empty()) ImGui::TextWrapped("%s", st.note.c_str());
        } else if (st.verdict == "oracle-not-animating") {
            ImGui::TextColored(th.warn,
                "The oracle's is constant while ours varies — the opposite of "
                "the expected fault, and worth understanding before going on.");
        }
    }

    // ---- 1d. what writes the SOURCE region ---------------------------------
    ImGui::SeparatorText("Who writes the source region");
    muted(th, "With the effect off, the colour source region is byte-identical "
              "between the two emulators; with it running they differ. So the "
              "divergence is in what the effect WRITES there, and this names "
              "the instructions doing it — the same attribution-by-address the "
              "packet writers use, without assuming a packet layout.");

    // Prefill from whatever colour_inputs last measured, so the region does not
    // have to be copied by hand from one pane to another.
    if (model.frm_range_lo[0] == '\0' && !ci.region_lo.empty()) {
        std::snprintf(model.frm_range_lo, sizeof(model.frm_range_lo), "%s",
                      ci.region_lo.c_str());
        std::snprintf(model.frm_range_hi, sizeof(model.frm_range_hi), "%s",
                      ci.region_hi.c_str());
    }
    muted(th, "range");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(120.f);
    ImGui::InputTextWithHint("##rlo", "0x000E0BF8", model.frm_range_lo,
                             sizeof(model.frm_range_lo));
    ImGui::SameLine();
    muted(th, "..");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(120.f);
    ImGui::InputTextWithHint("##rhi", "0x000E6628", model.frm_range_hi,
                             sizeof(model.frm_range_hi));
    ImGui::SameLine();
    muted(th, "step");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(50.f);
    ImGui::InputInt("##rframes", &model.frm_range_frames, 0, 0);
    if (model.frm_range_frames < 1) model.frm_range_frames = 1;

    const std::string rtool = gpu_tool_path(root, kRangeTool);
    const std::string rblocked = why_disabled(model, rtool, kRangeTool, true, live);
    const bool have_range = model.frm_range_lo[0] && model.frm_range_hi[0];
    ImGui::BeginDisabled(!rblocked.empty() || !have_range);
    if (accent_button(th, "Trace writers of this range")) {
        const std::string outp =
            (fs::path(model.frm_dir) / "range_writers.json").string();
        std::error_code ec;
        fs::create_directories(model.frm_dir, ec);
        model.frm_scan_note = "stepping with the range watched…";
        run_python_script_async(model, rtool,
                                {"--host", model.frm_host,
                                 "--port", std::to_string(model.frm_port),
                                 "--lo", model.frm_range_lo,
                                 "--hi", model.frm_range_hi,
                                 "--frames", std::to_string(model.frm_range_frames),
                                 "--expect-class", model.frm_writer_class,
                                 "--json", outp},
                                [&model, outp](RunResult r) {
                                    if (!load_range_writers(model.frm_range, outp)
                                        && !r.ok())
                                        model.frm_range.error = tool_error(r);
                                    model.frm_scan_note.clear();
                                },
                                true, JobSlot::Frames);
    }
    ImGui::EndDisabled();
    if (!rblocked.empty()) {
        ImGui::SameLine();
        wrapped(th.warn, "%s", rblocked.c_str());
    } else if (!have_range) {
        ImGui::SameLine();
        muted(th, "run Compare colour inputs first, or type a range");
    }

    const RangeWriters& rw = model.frm_range;
    if (rw.loaded && !rw.error.empty()) {
        wrapped(th.warn, "%s", rw.error.c_str());
    } else if (rw.loaded) {
        muted(th, "%d write(s) into %s..%s across %d frame(s), from %zu "
                  "instruction(s)",
              rw.writes, rw.lo.c_str(), rw.hi.c_str(), rw.frames_advanced,
              rw.writers.size());
        if (ImGui::BeginTable("##rw", 4, ImGuiTableFlags_Borders |
                              ImGuiTableFlags_RowBg | ImGuiTableFlags_ScrollY,
                              ImVec2(0.f, 190.f))) {
            ImGui::TableSetupScrollFreeze(0, 1);
            ImGui::TableSetupColumn("pc", ImGuiTableColumnFlags_WidthFixed, 110.f);
            ImGui::TableSetupColumn("writes", ImGuiTableColumnFlags_WidthFixed, 70.f);
            ImGui::TableSetupColumn("address span", ImGuiTableColumnFlags_WidthFixed, 190.f);
            ImGui::TableSetupColumn("common values");
            ImGui::TableHeadersRow();
            for (const RangeWriter& x : rw.writers) {
                ImGui::TableNextRow();
                ImGui::TableNextColumn();
                if (ImGui::Selectable(x.pc.c_str()))
                    ImGui::SetClipboardText(x.pc.c_str());
                ImGui::TableNextColumn(); ImGui::Text("%d", x.writes);
                ImGui::TableNextColumn();
                muted(th, "%s..%s", x.lo.c_str(), x.hi.c_str());
                ImGui::TableNextColumn();
                muted(th, "%s", x.common.c_str());
            }
            ImGui::EndTable();
        }
        if (!rw.listing.empty()) {
            ImGui::SeparatorText(("code at " + rw.listing_pc).c_str());
            if (ImGui::BeginChild("##rwlst", ImVec2(0.f, 190.f), true)) {
                for (const DisasmLine& d : rw.listing) {
                    if (d.is_target)
                        ImGui::TextColored(th.accent_text, ">> %s: %s  %s",
                                           d.pc.c_str(), d.word.c_str(),
                                           d.text.c_str());
                    else
                        muted(th, "   %s: %s  %s", d.pc.c_str(), d.word.c_str(),
                              d.text.c_str());
                }
            }
            ImGui::EndChild();
        }
    }

    // ---- 2. is the GTE doing this? ----------------------------------------
    ImGui::SeparatorText("GTE self-check");
    muted(th, "Re-derives the depth-cue interpolation from the GTE's own "
              "recorded inputs against the hardware description — no oracle "
              "needed. A mismatch means our GTE is wrong; a match means the "
              "inputs were already wrong when they arrived, so the fault is "
              "upstream of it.");

    const std::string gblocked = why_disabled(model, gtool, kGteTool, true, live);
    ImGui::BeginDisabled(!gblocked.empty());
    if (ImGui::Button("Check the GTE")) {
        const std::string outp = (fs::path(model.frm_dir) / "gte_check.json").string();
        std::error_code ec;
        fs::create_directories(model.frm_dir, ec);
        model.frm_scan_note = "re-deriving GTE operations…";
        run_python_script_async(model, gtool,
                                {"intpl", "--host", model.frm_host,
                                 "--port", std::to_string(model.frm_port),
                                 "--json", outp},
                                [&model, outp](RunResult r) {
                                    if (!load_gte_check(model.frm_gte, outp) && !r.ok())
                                        model.frm_gte.error = tool_error(r);
                                    model.frm_scan_note.clear();
                                },
                                true, JobSlot::Frames);
    }
    ImGui::EndDisabled();
    if (!gblocked.empty()) {
        ImGui::SameLine();
        wrapped(th.warn, "%s", gblocked.c_str());
    }

    const GteCheck& g = model.frm_gte;
    if (!g.error.empty()) wrapped(th.bad, "%s", g.error.c_str());
    if (g.loaded) {
        if (g.verdict == "not-used") {
            ImGui::TextColored(th.good,
                "This game issues no INTPL at all (%llu over the recorded "
                "frames). Depth-cue interpolation is not merely correct here — "
                "it is never executed, so it cannot be the source of a colour "
                "difference. Look at the packet writers above.",
                (unsigned long long)g.nintpl_total);
        } else if (g.verdict == "arithmetic-ok") {
            ImGui::TextColored(th.good,
                "All %d recorded INTPL operations match the hardware "
                "description. The GTE's arithmetic is not the bug — its INPUTS "
                "were already wrong, so look upstream of it.", g.checked);
        } else if (g.verdict == "arithmetic-diverges") {
            ImGui::TextColored(th.bad,
                "%d of %d INTPL operations do NOT match the hardware "
                "description. This is the bug.", g.bad, g.checked);
        }
        muted(th, "%llu projection(s) last frame, %llu saturated across the "
                  "window — saturation is what clamps a colour to a wrong value, "
                  "so zero here rules that out.",
              (unsigned long long)g.nproj_last,
              (unsigned long long)g.nsat_total);
        if (!g.frames.empty() &&
            ImGui::BeginTable("##gte", 6, ImGuiTableFlags_Borders |
                              ImGuiTableFlags_RowBg | ImGuiTableFlags_ScrollY,
                              ImVec2(0.f, 150.f))) {
            ImGui::TableSetupScrollFreeze(0, 1);
            for (const char* c : {"frame", "proj", "sat", "flat", "intpl", "tiny"})
                ImGui::TableSetupColumn(c);
            ImGui::TableHeadersRow();
            for (const GteFrameStat& f : g.frames) {
                ImGui::TableNextRow();
                ImGui::TableNextColumn(); ImGui::Text("%u", f.frame);
                ImGui::TableNextColumn(); ImGui::Text("%u", f.nproj);
                ImGui::TableNextColumn();
                if (f.nsat) ImGui::TextColored(th.warn, "%u", f.nsat);
                else muted(th, "0");
                ImGui::TableNextColumn(); ImGui::Text("%u", f.nflat);
                ImGui::TableNextColumn(); ImGui::Text("%u", f.nintpl);
                ImGui::TableNextColumn(); ImGui::Text("%u", f.nintpl_tiny);
            }
            ImGui::EndTable();
        }
    }
    if (!model.frm_scan_note.empty()) muted(th, "%s", model.frm_scan_note.c_str());
}

// Compare vertex-colour distributions between the two emulators.
void draw_colour_pane(StudioModel& model, const Theme& th, const std::string& root) {
    DebugClient& dbg = shared_debug_client();
    const bool live = dbg.snapshot().state == DebugClient::State::Connected;
    const std::string tool = gpu_tool_path(root, kColourTool);

    ImGui::TextWrapped(
        "Comparing one frame against one frame requires both emulators to be at "
        "the same instant, and nothing short of deterministic replay gets them "
        "there. An animating effect sampled at two phases differs everywhere, "
        "which says nothing — reading such a diff as meaningful is a trap.");
    muted(th, "This samples repeatedly from each side and compares the colour "
              "distribution instead, which does not depend on phase. If one peaks "
              "at 199 and the other at 40, that is real regardless of which frame "
              "each was on. It does require both to be showing the same effect.");
    ImGui::Separator();

    muted(th, "class");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(190.f);
    ImGui::InputTextWithHint("##cp_class", "all classes", model.frm_cparity_class,
                             sizeof(model.frm_cparity_class));
    ImGui::SameLine();
    muted(th, "samples");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(70.f);
    ImGui::InputInt("##cp_samples", &model.frm_cparity_samples, 0, 0);
    if (model.frm_cparity_samples < 1) model.frm_cparity_samples = 1;

    const std::string blocked = why_disabled(model, tool, kColourTool, true, live);
    ImGui::BeginDisabled(!blocked.empty());
    if (accent_button(th, "Compare colours")) {
        const std::string outp = (fs::path(model.frm_dir) / "colour_parity.json").string();
        std::error_code ec;
        fs::create_directories(model.frm_dir, ec);
        std::vector<std::string> args{
            "--host", model.frm_host,
            "--native-port", std::to_string(model.frm_port),
            "--ds-port", std::to_string(oracle_port(model)),
            "--samples", std::to_string(model.frm_cparity_samples),
            "--json", outp};
        if (model.frm_cparity_class[0]) {
            args.push_back("--class");
            args.push_back(model.frm_cparity_class);
        }
        model.frm_scan_note = "sampling both emulators…";
        run_python_script_async(model, tool, args,
                                [&model, outp](RunResult r) {
                                    if (r.ok()) {
                                        load_colour_parity(model.frm_cparity, outp);
                                    } else {
                                        model.frm_cparity = ColourParity{};
                                        model.frm_cparity.error = tool_error(r);
                                    }
                                    model.frm_scan_note.clear();
                                },
                                true, JobSlot::Frames);
    }
    ImGui::EndDisabled();
    if (!blocked.empty()) {
        ImGui::SameLine();
        wrapped(th.warn, "%s", blocked.c_str());
    }
    // The oracle is the other half of this comparison and it is easy to forget
    // to start it; say so here rather than letting the run come back one-sided.
    if (blocked.empty() && !model.frm_oracle.running)
        muted(th, "the oracle does not look like it is running — start it in the "
                  "Oracle tab, or this compares psx-runtime against nothing.");
    if (!model.frm_scan_note.empty()) muted(th, "%s", model.frm_scan_note.c_str());

    const ColourParity& cp = model.frm_cparity;
    if (!cp.error.empty()) {
        wrapped(th.bad, "%s", cp.error.c_str());
        return;
    }
    if (!cp.loaded) return;

    ImGui::Separator();
    const auto row = [&](const char* label, const ColourStats& c) {
        if (!c.loaded || c.vertices == 0) {
            muted(th, "%-12s no matching primitives sampled", label);
            return;
        }
        ImGui::Text("%-12s %6d vertices   peak %3d   mean %6.1f   p50 %3d   p90 %3d",
                    label, c.vertices, c.peak, c.mean, c.p50, c.p90);
    };
    row("psx-runtime", cp.native);
    row("oracle", cp.oracle);

    if (cp.overlap > 0.0) {
        ImGui::Text("distribution overlap %.3f", cp.overlap);
        ImGui::SameLine();
        muted(th, "(sample ratio %.2fx)", cp.sample_ratio);
        if (cp.sample_ratio > 3.0)
            muted(th, "sample sizes are lopsided — the overlap still holds (it "
                      "is normalised), but the peaks are not comparable.");
    }
    if (cp.verdict == "inconclusive") {
        ImGui::TextColored(th.warn,
            "Close but not clearly the same. Sample more frames, and make sure "
            "both emulators are showing the same effect.");
    } else if (cp.verdict == "match") {
        ImGui::TextColored(th.good,
            "The colour distributions match: both build the same colours, so a "
            "visible difference is NOT coming from vertex colour. Look at "
            "rasterisation or blending.");
    } else if (cp.verdict == "differ") {
        ImGui::TextColored(th.bad,
            "The distributions differ: the two emulators are building different "
            "colours, so the divergence is upstream of the renderer.");
    }
}

void draw_scan_pane(StudioModel& model, const Theme& th, const std::string& root) {
    DebugClient& dbg = shared_debug_client();
    const bool live = dbg.snapshot().state == DebugClient::State::Connected;
    const std::string tool = gpu_tool_path(root, kScanTool);

    ImGui::TextWrapped(
        "Diffing one good frame against one bad frame only works if you already "
        "know which two those are. Pick them wrong — one before an effect starts "
        "and one during it — and the diff correctly reports that the effect "
        "exists, which is useless. This walks the ring instead and finds where "
        "the picture actually turns.");
    muted(th, "It compares frames by (opcode, blend mode), not by function: a "
              "game that builds an ordering table and DMAs it reports the same "
              "submit PC for every packet on screen, so function attribution has "
              "one answer for the whole frame.");
    ImGui::Separator();

    ImGui::BeginDisabled(tool.empty() || !live || model.busy_frames.load());
    muted(th, "walk the last");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(90.f);
    ImGui::InputInt("##scan_last", &model.frm_scan_last, 0, 0);
    if (model.frm_scan_last < 8) model.frm_scan_last = 8;
    ImGui::SameLine();
    muted(th, "frames");
    ImGui::SameLine();
    if (accent_button(th, "Find where it breaks")) {
        const std::string outp = (fs::path(model.frm_dir) / "scan.json").string();
        std::error_code ec;
        fs::create_directories(model.frm_dir, ec);
        model.frm_scan_note = "walking the ring…";
        run_python_script_async(model, tool,
                                {"--host", model.frm_host,
                                 "--port", std::to_string(model.frm_port),
                                 "--last", std::to_string(model.frm_scan_last),
                                 "--out", outp},
                                [&model, outp](RunResult r) {
                                    if (r.ok()) {
                                        load_frame_scan(model.frm_scan, outp);
                                        model.frm_scan_note.clear();
                                    } else {
                                        model.frm_scan = FrameScan{};
                                        model.frm_scan.error = tool_error(r);
                                        model.frm_scan_note.clear();
                                    }
                                },
                                true, JobSlot::Frames);
    }
    ImGui::EndDisabled();
    if (!live) {
        ImGui::SameLine();
        muted(th, "needs a connected game");
    }
    if (!model.frm_scan_note.empty()) muted(th, "%s", model.frm_scan_note.c_str());

    const FrameScan& sc = model.frm_scan;
    if (!sc.loaded) {
        if (!sc.error.empty()) wrapped(th.bad, "%s", sc.error.c_str());
        return;
    }
    ImGui::Separator();
    muted(th, "scanned frames %d..%d", sc.lo, sc.hi);

    for (size_t i = 0; i < sc.transitions.size(); ++i) {
        const ScanTransition& t = sc.transitions[i];
        ImGui::PushID(static_cast<int>(i));
        const bool first = (i == 0);
        if (first)
            ImGui::TextColored(th.bad, "frame %d -> %d    score %.1f    <- biggest",
                               t.a, t.b, t.score);
        else
            muted(th, "frame %d -> %d    score %.1f", t.a, t.b, t.score);

        if (first || ImGui::TreeNode("details")) {
            for (const ScanChange& c : t.changes) {
                ImGui::BulletText("%s   %d -> %d  (%+d)", c.key.c_str(), c.a, c.b,
                                  c.delta);
                if (c.bbox_a != c.bbox_b)
                    muted(th, "      bbox %s -> %s",
                          c.bbox_a.empty() ? "none" : c.bbox_a.c_str(),
                          c.bbox_b.empty() ? "none" : c.bbox_b.c_str());
                if (c.cmax_a != c.cmax_b)
                    muted(th, "      peak colour %d -> %d", c.cmax_a, c.cmax_b);
                if (c.outside_vram || c.outside_draw_area) {
                    ImGui::TextColored(th.bad,
                        "      !! geometry is %s — %s",
                        c.outside_vram ? "OUTSIDE VRAM (1024x512)"
                                       : "outside the draw area",
                        c.overflow.c_str());
                    muted(th, "         vertices this far out are a blown "
                              "projection or a corrupted packet, not a visible "
                              "effect");
                }
                if (!c.src_lo.empty()) {
                    muted(th, "      packet array at %s..%s", c.src_lo.c_str(),
                          c.src_hi.c_str());
                    muted(th, "         (RAM DATA, not code — this is where the "
                              "packets live. The code that fills it is found by "
                              "watching writes to it.)");
                    ImGui::SameLine();
                    ImGui::BeginDisabled(!live);
                    // Closes the loop the scan opens: the packets exist at a
                    // known address, and whoever WRITES that address is the code
                    // that builds them — which is the thing function
                    // attribution could not tell us on this game.
                    if (ImGui::SmallButton("Watch this buffer")) {
                        // Clear first: the ring still holds boot-era writes, and
                        // dumping without clearing hands back BIOS PCs that have
                        // nothing to do with the buffer you just asked about.
                        dbg.send(R"("cmd":"wtrace_reset")", "wtrace_reset");
                        char cmd[160];
                        std::snprintf(cmd, sizeof(cmd),
                                      "\"cmd\":\"wtrace_add\",\"lo\":\"%s\","
                                      "\"hi\":\"%s\"",
                                      c.src_lo.c_str(), c.src_hi.c_str());
                        dbg.send(cmd, "wtrace_add");
                        model.frm_watch_lo = c.src_lo;
                        model.frm_watch_hi = c.src_hi;
                        model.frm_writers.clear();
                        model.frm_scan_note =
                            "watching " + c.src_lo + ".." + c.src_hi +
                            " — now let the effect happen again, then press "
                            "\"Find the writers\"";
                    }
                    ImGui::EndDisabled();

                    // The decisive experiment, one click from the buffer the
                    // scan just named: do BOTH emulators build the same bytes?
                    // Identical means the data is right and our renderer is
                    // wrong; different means the guest computed wrong numbers.
                    // Either answer eliminates half the codebase.
                    const std::string ram_tool = gpu_tool_path(root, kRamTool);
                    ImGui::SameLine();
                    ImGui::BeginDisabled(ram_tool.empty() || !live ||
                                         model.busy_frames.load());
                    if (accent_button(th, "Compare with oracle")) {
                        const std::string outp =
                            (fs::path(model.frm_dir) / "ram_parity.json").string();
                        std::error_code ec;
                        fs::create_directories(model.frm_dir, ec);
                        model.frm_scan_note =
                            "reading " + c.src_lo + ".." + c.src_hi +
                            " from both emulators…";
                        run_python_script_async(
                            model, ram_tool,
                            {"--host", model.frm_host,
                             "--native-port", std::to_string(model.frm_port),
                             "--lo", c.src_lo, "--hi", c.src_hi,
                             "--pause", "--json", outp},
                            [&model, outp](RunResult r) {
                                // ram_parity exits 1 when the buffers DIFFER,
                                // which is a result, not a failure.
                                if (!load_ram_parity(model.frm_ram, outp))
                                    model.frm_ram.error = tool_error(r);
                                model.frm_scan_note.clear();
                            },
                            true, JobSlot::Frames);
                    }
                    ImGui::EndDisabled();
                }
            }

            // ---- the verdict ------------------------------------------------
            const RamParity& rp = model.frm_ram;
            if (first && (rp.loaded || !rp.error.empty())) {
                ImGui::Spacing();
                if (!rp.error.empty()) {
                    ImGui::TextColored(th.bad, "RAM compare failed: %s",
                                       rp.error.c_str());
                } else if (rp.identical) {
                    ImGui::TextColored(th.good,
                        "IDENTICAL — %d bytes at %s match on both emulators.",
                        rp.compared, rp.addr.c_str());
                    ImGui::TextWrapped(
                        "The guest computed the same data on both. The bug is in "
                        "how WE render correct data — look at gpu.c, not at the "
                        "game code.");
                } else {
                    ImGui::TextColored(th.bad,
                        "DIFFERENT — %d/%d 32-bit words differ, first at %s.",
                        rp.differing_words, rp.total_words,
                        rp.first_difference.c_str());
                    ImGui::TextWrapped(
                        "The guest produced different data, so the bug is upstream "
                        "of the renderer — CPU, GTE, or the recompilation. Next: "
                        "dump the GTE ring at this frame to see whether the "
                        "coordinates were already wrong when they were projected.");
                }
                if (rp.loaded && !rp.frames_aligned)
                    ImGui::TextColored(th.warn,
                        "CAUTION: the two were on different frames (%llu vs %llu), "
                        "so some of this is just elapsed time.",
                        (unsigned long long)rp.native_frame,
                        (unsigned long long)rp.oracle_frame);
            }
            if (!first) ImGui::TreePop();
        }
        ImGui::PopID();
        ImGui::Spacing();
    }

    // ---- GTE ----------------------------------------------------------------
    // If the RAM compare says the guest computed different numbers, the next
    // question is whether they were already wrong when they came out of the
    // GTE. The ring records RTPS/RTPT inputs AND outputs, and exists (per its
    // own comment) to "split game-code input bugs from GTE-math bugs".
    ImGui::Separator();
    ImGui::BeginDisabled(!live);
    if (ImGui::Button("Dump GTE projections at this frame")) {
        char cmd[128];
        std::snprintf(cmd, sizeof(cmd),
                      "\"cmd\":\"gte_ring_dump\",\"count\":64,\"newest\":1,"
                      "\"frame\":%d",
                      sc.transitions.empty() ? -1 : sc.transitions[0].b);
        dbg.request(cmd, "gte_ring_dump");
        model.frm_scan_note = "reading the GTE ring…";
    }
    ImGui::EndDisabled();
    ImGui::SameLine();
    muted(th, "were the coordinates already wrong when they were projected?");
    std::string gte;
    if (dbg.take_reply("gte_ring_dump", gte)) {
        model.frm_disasm_pc = 0;
        model.frm_disasm = gte;      // shown in the same read-only viewer below
    }

    ImGui::Separator();
    if (model.frm_watch_lo.empty()) {
        muted(th, "Pick a buffer above and press \"Watch this buffer\" to find the "
                  "code that builds those packets.");
    } else {
        muted(th, "watching %s..%s", model.frm_watch_lo.c_str(),
              model.frm_watch_hi.c_str());
        ImGui::SameLine();
        ImGui::BeginDisabled(!live);
        if (accent_button(th, "Find the writers")) {
            // Filtered to the watched range. An unfiltered dump returns whatever
            // is oldest in the ring, which is how this first came back full of
            // BIOS addresses from boot.
            char cmd[220];
            std::snprintf(cmd, sizeof(cmd),
                          "\"cmd\":\"wtrace_dump\",\"addr_lo\":\"%s\","
                          "\"addr_hi\":\"%s\",\"count\":512",
                          model.frm_watch_lo.c_str(), model.frm_watch_hi.c_str());
            dbg.request(cmd, "wtrace_dump");
            model.frm_scan_note = "reading the write trace…";
        }
        ImGui::EndDisabled();
    }
    std::string wt;
    if (dbg.take_reply("wtrace_dump", wt)) {
        std::string err;
        model.frm_writers = parse_wtrace(wt, &err);
        model.frm_scan_note = err;
    }
    if (!model.frm_writers.empty()) {
        ImGui::Spacing();
        ImGui::TextColored(th.accent, "code writing into that buffer:");
        if (ImGui::BeginTable("##writers", 6,
                              ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                                  ImGuiTableFlags_SizingFixedFit)) {
            ImGui::TableSetupColumn("writes");
            ImGui::TableSetupColumn("PC");
            ImGui::TableSetupColumn("where that code lives");
            ImGui::TableSetupColumn("returns to");
            ImGui::TableSetupColumn("via");
            ImGui::TableSetupColumn("code");
            ImGui::TableHeadersRow();
            const uint32_t text_end = game_text_end(root);
            int overlay_hits = 0;
            for (const WtraceWriter& w : model.frm_writers) {
                ImGui::TableNextRow();
                ImGui::TableNextColumn();
                ImGui::Text("%d", w.count);
                ImGui::TableNextColumn();
                ImGui::Text("0x%08X", w.pc);

                // The wtrace `func` field is the same DMA pseudo-address the GP0
                // ring reports and means nothing here, so resolve the PC against
                // the analysis bundle instead — and when that has no answer, say
                // WHY rather than showing a blank.
                ImGui::TableNextColumn();
                std::string where;
                for (const auto& fn : model.fn_rows) {
                    if (w.pc >= fn.addr && w.pc < fn.end) {
                        char b[160];
                        std::snprintf(b, sizeof(b), "%s+0x%X", fn.name.c_str(),
                                      w.pc - fn.addr);
                        where = b;
                        break;
                    }
                }
                if (!where.empty()) {
                    ImGui::TextUnformatted(where.c_str());
                } else if (text_end && w.pc >= text_end) {
                    ++overlay_hits;
                    ImGui::TextColored(th.warn, "overlay (above 0x%08X)", text_end);
                } else if (model.fn_rows.empty()) {
                    muted(th, "run Analyze first");
                } else {
                    muted(th, "not in the analysed image");
                }

                ImGui::TableNextColumn();
                ImGui::Text("0x%08X", w.ra);
                ImGui::TableNextColumn();
                if (w.dma_ch >= 0) ImGui::Text("DMA ch%d", w.dma_ch);
                else muted(th, "CPU");

                // Overlay code is resident in RAM right now, so it can be read
                // without extracting the overlay first — which is the whole
                // reason the Functions tab has nothing for these addresses.
                ImGui::TableNextColumn();
                ImGui::PushID(static_cast<int>(w.pc));
                const std::string dis_tool = gpu_tool_path(root, kDisasmTool);
                ImGui::BeginDisabled(dis_tool.empty() || !live ||
                                     model.busy_frames.load());
                if (ImGui::SmallButton("disasm")) {
                    model.frm_disasm_pc = w.pc;
                    model.frm_disasm.clear();
                    char pc[16];
                    std::snprintf(pc, sizeof(pc), "0x%08X", w.pc);
                    run_python_script_async(
                        model, dis_tool,
                        {std::to_string(model.frm_port), pc, "192"},
                        [&model](RunResult r) {
                            model.frm_disasm = r.stdout_text.empty()
                                                   ? tool_error(r)
                                                   : r.stdout_text;
                        },
                        false, JobSlot::Frames);
                }
                ImGui::EndDisabled();
                ImGui::PopID();
            }
            ImGui::EndTable();

            if (overlay_hits) {
                ImGui::TextColored(th.warn,
                    "%d of these live in a runtime-loaded OVERLAY, above the boot "
                    "executable's text end (0x%08X).", overlay_hits, text_end);
                ImGui::TextWrapped(
                    "psxrecomp-analyze only reads the boot EXE, which is why the "
                    "Functions tab has nothing at these addresses — the code is "
                    "streamed off the disc at runtime. To name it, capture the "
                    "overlay that is resident here and analyse that:");
                muted(th, "    overlay_loader_status   — what is resident now");
                muted(th, "    overlay_dump lo=0x%X dir=overlays   — write it out",
                      text_end & 0x1FFFFFFF);
                muted(th, "    then psxrecomp/tools/overlay_xref.py, and see "
                          "docs/OVERLAY_XREF.md");
                ImGui::BeginDisabled(!live);
                if (ImGui::Button("Dump overlays now")) {
                    char cmd[200];
                    std::snprintf(cmd, sizeof(cmd),
                                  "\"cmd\":\"overlay_dump\",\"lo\":\"0x%X\","
                                  "\"dir\":\"%s\"",
                                  text_end & 0x1FFFFFFF,
                                  (fs::path(root) / "overlays").string().c_str());
                    dbg.request(cmd, "overlay_dump");
                    model.frm_scan_note = "dumping resident overlays…";
                }
                ImGui::SameLine();
                if (ImGui::Button("Loader status")) {
                    dbg.request(R"("cmd":"overlay_loader_status")", "overlay_status");
                    model.frm_scan_note = "reading the overlay loader…";
                }
                ImGui::EndDisabled();
                std::string rep;
                if (dbg.take_reply("overlay_dump", rep) ||
                    dbg.take_reply("overlay_status", rep))
                    model.frm_scan_note = rep.size() > 700 ? rep.substr(0, 700) + " …"
                                                           : rep;
            } else {
                muted(th, "These ARE code addresses — name the top one in the "
                          "Functions tab and you have the routine that builds it.");
            }
        }   // BeginTable

        if (!model.frm_disasm.empty()) {
            ImGui::Spacing();
            if (model.frm_disasm_pc)
                ImGui::TextColored(th.accent, "disassembly at 0x%08X",
                                   model.frm_disasm_pc);
            else
                ImGui::TextColored(th.accent, "GTE projections (newest first)");
            ImGui::SameLine();
            if (ImGui::SmallButton("close##dis")) model.frm_disasm.clear();
            ImGui::InputTextMultiline(
                "##disasm", model.frm_disasm.data(), model.frm_disasm.size() + 1,
                ImVec2(-1, 260.f), ImGuiInputTextFlags_ReadOnly);
        }
    }
}

void draw_oracle_pane(StudioModel& model, const Theme& th, const std::string& root) {
    DebugClient& game = shared_debug_client();
    DebugClient& orc = oracle_debug_client();
    const bool game_live = game.snapshot().state == DebugClient::State::Connected;
    const bool orc_live = orc.snapshot().state == DebugClient::State::Connected;

    if (!orc.running() && model.frm_oracle.answering) {
        // The oracle is up but Studio is not talking to it yet.
        orc.start("127.0.0.1", model.frm_oracle.port ? model.frm_oracle.port : 4371);
    }

    muted(th, "The oracle runs offscreen — no window. These are the only way to "
              "see it, and the only way to drive it.");
    ImGui::Text("game: %s", game_live ? "connected" : "not connected");
    ImGui::SameLine();
    ImGui::Text("  oracle: %s", orc_live ? "connected" : "not connected");
    ImGui::SameLine();
    if (ImGui::Button("Reconnect oracle"))
        orc.start("127.0.0.1", model.frm_oracle.port ? model.frm_oracle.port : 4371);

    const std::string dir = model.frm_dir;
    const std::string nat_path = (fs::path(dir) / "shot-native.png").string();
    const std::string orc_path = (fs::path(dir) / "shot-oracle.png").string();

    ImGui::BeginDisabled(!game_live && !orc_live);
    if (accent_button(th, "Capture both")) {
        std::error_code ec;
        fs::create_directories(dir, ec);
        if (game_live) shoot(game, "screenshot_file", nat_path, "shot");
        if (orc_live) shoot(orc, "screenshot", orc_path, "shot");
    }
    ImGui::EndDisabled();
    ImGui::SameLine();
    ImGui::Checkbox("auto", &model.frm_shot_auto);
    ImGui::SameLine();
    muted(th, "(re-capture once a second)");

    // Replies just say "written"; the file is the payload.
    std::string reply;
    if (game.take_reply("shot", reply) && !reload_texture(nat_path, model.frm_shot_native))
        model.frm_shot_error = "could not read " + nat_path;
    if (orc.take_reply("shot", reply) && !reload_texture(orc_path, model.frm_shot_oracle))
        model.frm_shot_error = "could not read " + orc_path;

    const double now = ImGui::GetTime();
    if (model.frm_shot_auto && now >= model.frm_shot_next) {
        model.frm_shot_next = now + 1.0;
        std::error_code ec;
        fs::create_directories(dir, ec);
        if (game_live) shoot(game, "screenshot_file", nat_path, "shot");
        if (orc_live) shoot(orc, "screenshot", orc_path, "shot");
    }
    if (!model.frm_shot_error.empty())
        wrapped(th.bad, "%s", model.frm_shot_error.c_str());

    // side by side, because the whole point is telling them apart
    const float avail = ImGui::GetContentRegionAvail().x;
    const float each = (avail - 24.f) * 0.5f;
    const auto show = [&](const char* title, FrameLayer& img) {
        ImGui::BeginGroup();
        ImGui::TextColored(th.accent, "%s", title);
        if (img.tex && img.tw > 0) {
            const float w = each;
            const float h = w * static_cast<float>(img.th) / static_cast<float>(img.tw);
            ImGui::Image(tex_id(img.tex), ImVec2(w, h));
            muted(th, "%dx%d", img.tw, img.th);
        } else {
            ImGui::Dummy(ImVec2(each, each * 0.75f));
            muted(th, "no capture yet");
        }
        ImGui::EndGroup();
    };
    show("psx-runtime", model.frm_shot_native);
    ImGui::SameLine();
    show("duckstation oracle", model.frm_shot_oracle);

    ImGui::Separator();

    // ---- driving the oracle -------------------------------------------------
    // DuckStation implements pause/continue/step/run_to_frame natively, so
    // unlike the native runtime these need no special handling.
    ImGui::BeginDisabled(!orc_live);
    muted(th, "oracle transport");
    ImGui::SameLine();
    if (ImGui::Button("Pause##orc")) orc.send(R"("cmd":"pause")", "pause");
    ImGui::SameLine();
    if (ImGui::Button("Continue##orc")) orc.send(R"("cmd":"continue")", "continue");
    ImGui::SameLine();
    if (ImGui::Button("Step##orc")) orc.send(R"("cmd":"step")", "step");
    ImGui::SameLine();
    if (ImGui::Button("Run to##orc")) {
        char cmd[80];
        std::snprintf(cmd, sizeof(cmd), "\"cmd\":\"run_to_frame\",\"frame\":%d",
                      model.frm_oracle_run_to);
        orc.send(cmd, "run_to_frame");
    }
    ImGui::SameLine();
    ImGui::SetNextItemWidth(90.f);
    ImGui::InputInt("##orc_runto", &model.frm_oracle_run_to, 0, 0);

    // ---- pad ---------------------------------------------------------------
    // set_input holds an override until it changes, so the mask is rebuilt from
    // which buttons are held THIS frame and only sent when it differs. Holding
    // the mouse holds the button, which is what makes menus navigable at all.
    // PS1 pads are active-low: a 0 bit is pressed.
    muted(th, "oracle pad — hold a button to hold it down");
    uint32_t mask = 0xFFFFu;
    const auto pad = [&](const char* label, int bit, float w = 34.f) {
        ImGui::Button(label, ImVec2(w, 0));
        if (ImGui::IsItemActive()) mask &= ~(1u << bit);
    };
    pad("Up", 4);    ImGui::SameLine();
    pad("Dn", 6);    ImGui::SameLine();
    pad("Lf", 7);    ImGui::SameLine();
    pad("Rt", 5);    ImGui::SameLine();
    ImGui::Dummy(ImVec2(12, 0)); ImGui::SameLine();
    pad("/\\", 12); ImGui::SameLine();   // triangle
    pad("O", 13);    ImGui::SameLine();
    pad("X", 14);    ImGui::SameLine();
    pad("[]", 15);   ImGui::SameLine();
    ImGui::Dummy(ImVec2(12, 0)); ImGui::SameLine();
    pad("Start", 3, 52.f);  ImGui::SameLine();
    pad("Sel", 0, 42.f);    ImGui::SameLine();
    pad("L1", 10);   ImGui::SameLine();
    pad("R1", 11);
    if (orc_live && mask != model.frm_pad_mask) {
        model.frm_pad_mask = mask;
        char cmd[64];
        std::snprintf(cmd, sizeof(cmd), "\"cmd\":\"set_input\",\"buttons\":%u", mask);
        orc.send(cmd, "set_input");
    }
    ImGui::SameLine();
    if (ImGui::Button("Release all")) {
        orc.send(R"("cmd":"clear_input")", "clear_input");
        model.frm_pad_mask = 0xFFFFu;
    }
    ImGui::EndDisabled();
    (void)root;
}

void draw_attribution(StudioModel& model, const Theme& th, FrameSummary& s) {
    if (!s.loaded) {
        muted(th, "%s", s.error.empty()
                            ? "Select a capture above, or take one."
                            : s.error.c_str());
        return;
    }
    ImGui::Text("frame %u", s.frame);
    ImGui::SameLine();
    muted(th, "  %u packets, %u drawing, %zu function(s)",
          s.packets, s.drawing, s.funcs.size());
    if (s.capped) {
        ImGui::PushStyleColor(ImGuiCol_Text, th.warn);
        ImGui::TextWrapped(
            "The ring returned every packet that was asked for, so this frame may "
            "be truncated. Raise the packet cap and capture again before trusting "
            "a count on this page.");
        ImGui::PopStyleColor();
    }
    if (s.truncated) {
        muted(th, "%u packet(s) were longer than the ring's 12-word cap and are "
                  "decoded only as far as recorded.", s.truncated);
    }
    if (!s.modes.empty()) {
        std::string line;
        for (const auto& [m, n] : s.modes) {
            if (!line.empty()) line += "   ";
            line += m + " x" + std::to_string(n);
        }
        wrapped(th.accent, "semi-transparency in use:  %s", line.c_str());
    } else {
        ImGui::TextColored(th.warn,
                           "no semi-transparent primitives in this frame — if the "
                           "effect you are chasing is a glow or a vignette, that is "
                           "the finding");
    }
    ImGui::Separator();

    const ImGuiTableFlags flags = ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                                  ImGuiTableFlags_ScrollY | ImGuiTableFlags_Resizable |
                                  ImGuiTableFlags_SizingStretchProp;
    const float h = ImGui::GetContentRegionAvail().y * (model.frm_func_sel >= 0 ? 0.55f : 1.f);
    if (ImGui::BeginTable("##frm_funcs", 8, flags, ImVec2(0, h))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("function", ImGuiTableColumnFlags_WidthFixed, 100.f);
        ImGui::TableSetupColumn("name (static)", ImGuiTableColumnFlags_WidthStretch, 1.2f);
        ImGui::TableSetupColumn("draw", ImGuiTableColumnFlags_WidthFixed, 56.f);
        ImGui::TableSetupColumn("semi", ImGuiTableColumnFlags_WidthFixed, 56.f);
        ImGui::TableSetupColumn("tex", ImGuiTableColumnFlags_WidthFixed, 50.f);
        ImGui::TableSetupColumn("OT", ImGuiTableColumnFlags_WidthFixed, 76.f);
        ImGui::TableSetupColumn("primitives", ImGuiTableColumnFlags_WidthStretch, 1.6f);
        ImGui::TableSetupColumn("blend", ImGuiTableColumnFlags_WidthStretch, 1.f);
        ImGui::TableHeadersRow();

        for (int i = 0; i < static_cast<int>(s.funcs.size()); ++i) {
            const FrameFuncRow& r = s.funcs[i];
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::PushID(i);
            if (ImGui::Selectable(r.func.c_str(), model.frm_func_sel == i,
                                  ImGuiSelectableFlags_SpanAllColumns)) {
                model.frm_func_sel = i;
                model.frm_rename[0] = '\0';
            }
            ImGui::PopID();
            ImGui::TableNextColumn();
            if (r.name.empty()) muted(th, "—");
            else ImGui::TextUnformatted(r.name.c_str());
            ImGui::TableNextColumn();
            ImGui::Text("%u", r.drawing);
            ImGui::TableNextColumn();
            if (r.semi) ImGui::TextColored(th.accent, "%u", r.semi);
            else muted(th, "0");
            ImGui::TableNextColumn();
            ImGui::Text("%u", r.textured);
            ImGui::TableNextColumn();
            if (r.ot_min < 0) muted(th, "—");
            else if (r.ot_min == r.ot_max) ImGui::Text("%d", r.ot_min);
            else ImGui::Text("%d..%d", r.ot_min, r.ot_max);
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(r.ops.c_str());
            ImGui::TableNextColumn();
            if (r.stp.empty()) muted(th, "—");
            else ImGui::TextUnformatted(r.stp.c_str());
        }
        ImGui::EndTable();
    }

    if (model.frm_func_sel < 0 ||
        model.frm_func_sel >= static_cast<int>(s.funcs.size()))
        return;

    const FrameFuncRow& r = s.funcs[model.frm_func_sel];
    ImGui::Separator();
    ImGui::Text("%s", r.func.c_str());
    ImGui::SameLine();
    muted(th, "  %u packet(s), %u drawing, %u semi, %u textured",
          r.packets, r.drawing, r.semi, r.textured);
    if (r.has_bbox)
        muted(th, "bbox in draw space: [%d, %d] .. [%d, %d]",
              r.bbox[0], r.bbox[1], r.bbox[2], r.bbox[3]);
    if (!r.ras.empty()) {
        std::string line;
        for (size_t i = 0; i < r.ras.size() && i < 8; ++i) {
            char b[16];
            std::snprintf(b, sizeof(b), "0x%08X", r.ras[i]);
            if (!line.empty()) line += "  ";
            line += b;
        }
        muted(th, "callers observed at $ra: %s%s", line.c_str(),
              r.ras.size() > 8 ? "  …" : "");
    }

    // Naming goes through the same CLI the Functions tab uses, so symbols.toml
    // has exactly one writer and a headless run produces the same file.
    const std::string root = model.selected_root();
    ImGui::TextColored(th.text_muted, "Name this address (written to symbols.toml)");
    ImGui::SetNextItemWidth(280.f);
    const bool submit = ImGui::InputText("##frm_rename", model.frm_rename,
                                         sizeof(model.frm_rename),
                                         ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::SameLine();
    ImGui::SetNextItemWidth(260.f);
    ImGui::InputTextWithHint("##frm_rename_note", "note (optional)",
                             model.frm_rename_note, sizeof(model.frm_rename_note));
    ImGui::SameLine();
    const bool save = accent_button(th, "Save name",
                                    !model.busy.load() && model.frm_rename[0] != '\0');
    if ((save || submit) && model.frm_rename[0] && !root.empty()) {
        char pc[16];
        std::snprintf(pc, sizeof(pc), "0x%08X", r.addr);
        std::vector<std::string> args = {"analyze", "set-symbol", "--root", root,
                                         "--pc", pc, "--name", model.frm_rename,
                                         "--status", "guessed"};
        if (model.frm_rename_note[0]) {
            args.push_back("--note");
            args.push_back(model.frm_rename_note);
        } else {
            args.push_back("--note");
            args.push_back("observed issuing GP0 primitives (frame capture)");
        }
        const int which = &s == &model.frm_a ? 0 : 1;
        run_project_studio_async(model, args, [&model, which](RunResult) {
            load_analysis(model, model.selected_root());
            rebuild_fn_view(model);
            name_frame_funcs(model, which == 0 ? model.frm_a : model.frm_b);
        });
        model.frm_rename[0] = '\0';
        model.frm_rename_note[0] = '\0';
    }
    muted(th, "The name is a static symbol for an address this frame observed "
              "executing. It is not evidence about the call graph.");
}

void draw_diff_pane(StudioModel& model, const Theme& th) {
    FrameDiff& d = model.frm_diff;
    if (!d.loaded) {
        muted(th, "%s", d.error.empty()
                            ? "Pick a reference (A) and a suspect (B) capture, then Diff."
                            : d.error.c_str());
        return;
    }
    if (d.headlines.empty()) {
        ImGui::TextColored(th.good,
                           "No function stopped drawing and no blend mode disappeared.");
    } else {
        for (const auto& hl : d.headlines)
            wrapped(th.bad, "• %s", hl.c_str());
    }
    ImGui::Separator();

    if (ImGui::BeginTable("##frm_modes", 3,
                          ImGuiTableFlags_Borders | ImGuiTableFlags_SizingFixedFit)) {
        ImGui::TableSetupColumn("blend mode");
        ImGui::TableSetupColumn("A");
        ImGui::TableSetupColumn("B");
        ImGui::TableHeadersRow();
        std::vector<std::string> all;
        for (const auto& [m, n] : d.modes_a) { (void)n; all.push_back(m); }
        for (const auto& [m, n] : d.modes_b) {
            (void)n;
            if (std::find(all.begin(), all.end(), m) == all.end()) all.push_back(m);
        }
        for (const auto& m : all) {
            uint32_t a = 0, b = 0;
            for (const auto& [k, v] : d.modes_a) if (k == m) a = v;
            for (const auto& [k, v] : d.modes_b) if (k == m) b = v;
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(m.c_str());
            ImGui::TableNextColumn();
            ImGui::Text("%u", a);
            ImGui::TableNextColumn();
            if (a > 0 && b == 0) ImGui::TextColored(th.bad, "%u", b);
            else ImGui::Text("%u", b);
        }
        ImGui::EndTable();
    }
    ImGui::Spacing();

    const ImGuiTableFlags flags = ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                                  ImGuiTableFlags_ScrollY | ImGuiTableFlags_SizingStretchProp;
    if (ImGui::BeginTable("##frm_diff", 6, flags, ImVec2(0, 0))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("function", ImGuiTableColumnFlags_WidthFixed, 100.f);
        ImGui::TableSetupColumn("verdict", ImGuiTableColumnFlags_WidthStretch, 1.4f);
        ImGui::TableSetupColumn("A prims", ImGuiTableColumnFlags_WidthFixed, 70.f);
        ImGui::TableSetupColumn("A semi", ImGuiTableColumnFlags_WidthFixed, 70.f);
        ImGui::TableSetupColumn("B prims", ImGuiTableColumnFlags_WidthFixed, 70.f);
        ImGui::TableSetupColumn("B semi", ImGuiTableColumnFlags_WidthFixed, 70.f);
        ImGui::TableHeadersRow();
        for (const auto& r : d.rows) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(r.func.c_str());
            ImGui::TableNextColumn();
            const bool bad = r.verdict == "stopped drawing" ||
                             r.verdict == "lost semi-transparency";
            if (bad) ImGui::TextColored(th.bad, "%s", r.verdict.c_str());
            else ImGui::TextUnformatted(r.verdict.c_str());
            ImGui::TableNextColumn();
            ImGui::Text("%u", r.a_prims);
            ImGui::TableNextColumn();
            ImGui::Text("%u", r.a_semi);
            ImGui::TableNextColumn();
            ImGui::Text("%u", r.b_prims);
            ImGui::TableNextColumn();
            ImGui::Text("%u", r.b_semi);
        }
        ImGui::EndTable();
    }
}

void draw_layers_pane(StudioModel& model, const Theme& th) {
    FrameLayers& l = model.frm_layers;
    if (!l.loaded) {
        muted(th, "%s", l.error.empty()
                            ? "Render layers for a capture to see one image per function."
                            : l.error.c_str());
        return;
    }
    ImGui::Text("frame %u", l.frame);
    ImGui::SameLine();
    muted(th, "  %zu layer(s)  —  %s", l.layers.size(), l.dir.c_str());
    muted(th, "Layers render over neutral grey so additive and subtractive "
              "blends stay visible; the composite starts from black, as the GPU does. "
              "Textures are not sampled — textured primitives show their command colour.");
    ImGui::Separator();

    const float avail = ImGui::GetContentRegionAvail().x;
    if (ensure_texture(l.dir, l.composite) && l.composite.tw > 0) {
        const float w = std::min(avail, static_cast<float>(l.composite.tw) * 2.f);
        const float h = w * static_cast<float>(l.composite.th) /
                        static_cast<float>(l.composite.tw);
        ImGui::TextColored(th.accent, "composite");
        ImGui::Image(tex_id(l.composite.tex), ImVec2(w, h));
    }
    ImGui::Spacing();

    const float cell = 190.f;
    const int cols = std::max(1, static_cast<int>(avail / (cell + 12.f)));
    int col = 0;
    for (int i = 0; i < static_cast<int>(l.layers.size()); ++i) {
        FrameLayer& layer = l.layers[i];
        if (!ensure_texture(l.dir, layer)) continue;
        if (col) ImGui::SameLine();
        ImGui::PushID(i);
        ImGui::BeginGroup();
        const float w = cell;
        const float h = layer.tw > 0
                            ? w * static_cast<float>(layer.th) / static_cast<float>(layer.tw)
                            : w * 0.75f;
        const bool sel = model.frm_layer_sel == i;
        if (sel) {
            ImGui::PushStyleColor(ImGuiCol_Button, th.accent_button);
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered, th.accent_button_hovered);
        } else {
            ImGui::PushStyleColor(ImGuiCol_Button, th.panel);
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered, th.panel_hovered);
        }
        if (ImGui::ImageButton("##layer", tex_id(layer.tex), ImVec2(w, h))) {
            model.frm_layer_sel = i;
            // Selecting a layer selects the same function in the attribution
            // table, so the two panes always talk about the same thing.
            FrameSummary& s = model.frm_b.loaded ? model.frm_b : model.frm_a;
            for (int k = 0; k < static_cast<int>(s.funcs.size()); ++k)
                if (s.funcs[k].func == layer.func) model.frm_func_sel = k;
        }
        ImGui::PopStyleColor(2);
        ImGui::TextUnformatted(layer.func.c_str());
        if (layer.semi)
            ImGui::TextColored(th.accent, "%u prim  %u semi", layer.prims, layer.semi);
        else
            muted(th, "%u prim", layer.prims);
        if (!layer.stp.empty()) muted(th, "%s", layer.stp.c_str());
        ImGui::EndGroup();
        ImGui::PopID();
        if (++col >= cols) col = 0;
    }
}

void draw_opcodes_pane(const Theme& th, const FrameSummary& s) {
    if (!s.loaded) {
        muted(th, "No capture loaded.");
        return;
    }
    muted(th, "Every GP0 opcode the frame issued, including the environment "
              "commands that set draw mode. The +semi suffix is bit 25 of the "
              "command word — the primitive's own semi-transparency flag.");
    if (ImGui::BeginTable("##frm_ops", 2,
                          ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_ScrollY | ImGuiTableFlags_SizingFixedFit)) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("opcode");
        ImGui::TableSetupColumn("count");
        ImGui::TableHeadersRow();
        for (const auto& [op, n] : s.ops) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            if (op.find("+semi") != std::string::npos)
                ImGui::TextColored(th.accent, "%s", op.c_str());
            else
                ImGui::TextUnformatted(op.c_str());
            ImGui::TableNextColumn();
            ImGui::Text("%u", n);
        }
        ImGui::EndTable();
    }
}

} // namespace

void draw_frames(StudioModel& model, const Theme& th, SDL_Window* /*window*/) {
    const std::string root = model.selected_root();
    if (root.empty()) {
        muted(th, "Select a game repo above.");
        return;
    }
    sync_dirs(model, root);

    const bool busy = model.busy.load();
    const std::string capture_tool = gpu_tool_path(root, kCaptureTool);
    const bool have_tools = !capture_tool.empty();

    // ---- runtime connection -------------------------------------------------
    DebugClient& dbg = shared_debug_client();
    const auto snap = dbg.snapshot();

    left_label("Runtime", 90.f);
    ImGui::SetNextItemWidth(130.f);
    ImGui::InputText("##frm_host", model.frm_host, sizeof(model.frm_host));
    ImGui::SameLine();
    ImGui::SetNextItemWidth(80.f);
    ImGui::InputInt("##frm_port", &model.frm_port, 0, 0);
    ImGui::SameLine();
    if (!dbg.running()) {
        if (accent_button(th, "Connect")) {
            dbg.start(model.frm_host, model.frm_port);
            model.frm_connected = true;
        }
    } else {
        if (ImGui::Button("Disconnect")) {
            dbg.stop();
            model.frm_connected = false;
        }
    }
    ImGui::SameLine();
    switch (snap.state) {
    case DebugClient::State::Connected:
        ImGui::TextColored(th.good, "connected — frame %llu",
                           static_cast<unsigned long long>(snap.frame));
        break;
    case DebugClient::State::Connecting:
        ImGui::TextColored(th.warn, "%s",
                           snap.error.empty() ? "connecting…" : snap.error.c_str());
        break;
    default:
        muted(th, "not connected");
        break;
    }

    const bool live = snap.state == DebugClient::State::Connected;

    // "Not connected" is a useless diagnosis on its own: the overwhelmingly
    // common cause is a Release build with no debug server compiled in, which
    // no amount of retrying will fix. Say which it is.
    if (!live) {
        const auto dbg = probe_debug_tools(root, model.build_dir);
        left_label("", 90.f);
        if (!dbg.configured) {
            muted(th, "%s", dbg.summary.c_str());
        } else if (dbg.enabled) {
            ImGui::TextColored(th.text_muted,
                               "%s  —  start the game, then Connect.", dbg.summary.c_str());
        } else {
            wrapped(th.warn, "%s", dbg.summary.c_str());
            left_label("", 90.f);
            ImGui::TextColored(th.warn,
                               "Build tab -> Debug tools -> \"Configure for debugging\", "
                               "then Configure + Build.");
        }
    }
    // There is deliberately no Pause / Step / Run-to-frame here. psx-runtime
    // REMOVED those commands — runtime/src/debug_server.c registers them only as
    // handlers that return an error — because pause-step-read synthesizes a
    // snapshot instead of reading the history the runtime already records, and
    // it once turned a dropped client connection into an apparent freeze.
    //
    // The replacement suits this job better anyway. The GP0 ring holds ~1M
    // packets, several hundred frames, so you play until the bug is on screen
    // and then reach BACKWARDS for that frame and the ones before it. A glitch
    // you cannot reliably stop on is exactly the case pause loses.
    poll_ring(model, dbg);
    poll_pause(model, dbg);

    // ---- the game ----------------------------------------------------------
    // Launching from here rather than the Build tab on purpose. Build's Launch
    // streams the game's stdout through a job slot it holds for the game's whole
    // lifetime, which disables every control that only works WHILE a game runs.
    // This one detaches, writes to frames.log, and takes no slot at all.
    {
        const bool running = model.frm_launch_pid > 0 &&
                             process_alive(model.frm_launch_pid);
        if (!running && model.frm_launch_pid > 0) model.frm_launch_pid = 0;

        left_label("Game", 90.f);
        const std::string exe = find_runtime_exe(
            (fs::path(root) / model.build_dir).string());
        if (running) {
            ImGui::TextColored(th.good, "running (pid %ld)", model.frm_launch_pid);
            ImGui::SameLine();
            if (ImGui::Button("Stop game")) {
                process_stop(model.frm_launch_pid);
                model.frm_launch_pid = 0;
            }
        } else if (exe.empty()) {
            ImGui::TextColored(th.warn, "no built game in %s — build it first",
                               model.build_dir);
        } else {
            muted(th, "not running");
            ImGui::SameLine();
            if (accent_button(th, "Launch game")) {
                model.frm_launch_log = (fs::path(root) / "frames.log").string();
                std::string err;
                const long pid = spawn_detached_logged(
                    exe, {}, root, model.frm_launch_log, {}, &err);
                model.frm_launch_pid = pid;
                model.frm_launch_error = pid ? "" : err;
                if (pid) {
                    // Give it a moment, then connect without another click:
                    // launching and then not being connected is the state
                    // nobody wants to be left in.
                    model.frm_status = "launched — press Connect once it is up";
                }
            }
        }
        ImGui::SameLine();
        muted(th, " · log: %s", model.frm_launch_log.empty()
                                    ? (fs::path(root) / "frames.log").string().c_str()
                                    : model.frm_launch_log.c_str());
        if (!model.frm_launch_error.empty()) {
            left_label("", 90.f);
            ImGui::TextColored(th.bad, "launch failed: %s",
                               model.frm_launch_error.c_str());
        }
    }

    left_label("Ring", 90.f);
    if (model.frm_ring_valid && model.frm_ring_total > 0) {
        ImGui::TextColored(th.good, "frames %u..%u capturable",
                           model.frm_ring_oldest, model.frm_ring_newest);
        ImGui::SameLine();
        muted(th, " · %llu / %u packets recorded",
              (unsigned long long)model.frm_ring_total, model.frm_ring_capacity);
    } else if (model.frm_ring_valid) {
        muted(th, "nothing recorded yet — no GP0 packets have been issued.");
    } else if (live) {
        muted(th, "%s", model.frm_ring_error.empty()
                            ? "reading GP0 ring…"
                            : model.frm_ring_error.c_str());
    } else {
        muted(th, "connect to a running game to see what can be captured.");
    }
    ImGui::SameLine();
    ImGui::BeginDisabled(!live);
    if (ImGui::SmallButton("Refresh")) model.frm_ring_next_poll = 0.0;
    ImGui::EndDisabled();

    // ---- time controls ------------------------------------------------------
    // Pause / Step / Run-to are back, but only as a debug-server capability:
    // the runtime's park gate lives behind #ifndef PSX_NO_DEBUG_TOOLS and is
    // bound to no key or pad input, so a player build has no way to reach it.
    // They park by NOT holding the emulator hostage — see
    // debug_server_wait_if_paused(), which keeps servicing commands, keeps
    // feeding the starvation watchdog, and auto-resumes if Studio goes quiet.
    //
    // For looking at what was DRAWN, prefer "at frame" below: the GP0 ring
    // reaches backwards, which pausing cannot.
    left_label("Time", 90.f);
    ImGui::BeginDisabled(!live);
    const PauseState& ps = model.frm_pause;
    if (ps.paused) {
        if (accent_button(th, "Continue")) {
            dbg.send(R"("cmd":"continue")", "continue");
            model.frm_pause_next_poll = 0.0;
        }
    } else {
        if (ImGui::Button("Pause")) {
            /* Ten minutes, not the runtime's 60 s default. A person looking at a
             * frame is the whole reason to pause, and thinking about it for more
             * than a minute is normal; the guard only exists to stop a DEAD
             * client wedging the game, and Studio's ping keeps it alive anyway. */
            dbg.send(R"("cmd":"pause","timeout_ms":600000)", "pause");
            model.frm_pause_next_poll = 0.0;
        }
    }
    ImGui::SameLine();
    if (ImGui::Button("Step")) {
        char cmd[64];
        std::snprintf(cmd, sizeof(cmd), "\"cmd\":\"step\",\"n\":%d",
                      model.frm_step_n < 1 ? 1 : model.frm_step_n);
        dbg.send(cmd, "step");
        model.frm_pause_next_poll = 0.0;
    }
    ImGui::SameLine();
    ImGui::SetNextItemWidth(60.f);
    ImGui::InputInt("##frm_stepn", &model.frm_step_n, 0, 0);
    if (model.frm_step_n < 1) model.frm_step_n = 1;
    ImGui::SameLine();
    muted(th, "frames");
    ImGui::SameLine();
    if (ImGui::Button("Run to")) {
        char cmd[80];
        std::snprintf(cmd, sizeof(cmd), "\"cmd\":\"run_to_frame\",\"frame\":%d",
                      model.frm_run_to);
        dbg.send(cmd, "run_to_frame");
        model.frm_pause_next_poll = 0.0;
    }
    ImGui::SameLine();
    ImGui::SetNextItemWidth(90.f);
    ImGui::InputInt("##frm_runto", &model.frm_run_to, 0, 0);
    ImGui::EndDisabled();

    ImGui::SameLine();
    if (!live) {
        muted(th, " ");
    } else if (ps.paused) {
        ImGui::TextColored(th.warn, "PAUSED at frame %llu",
                           (unsigned long long)ps.frame);
    } else if (ps.stepping > 0) {
        ImGui::TextColored(th.accent, "stepping (%d left)", ps.stepping);
    } else if (ps.run_to) {
        ImGui::TextColored(th.accent, "running to %u", ps.run_to);
    } else if (!ps.supported) {
        ImGui::TextColored(th.warn, "this runtime has no pause gate — rebuild it");
    } else {
        muted(th, "running");
    }
    if (live && ps.auto_resumed && !ps.paused) {
        left_label("", 90.f);
        muted(th, "the last pause auto-resumed: the runtime stopped hearing from "
                  "Studio for %u ms and released itself rather than wedging",
              ps.timeout_ms);
    }

    left_label("", 90.f);
    ImGui::BeginDisabled(!live);
    muted(th, "savestate slot");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(70.f);
    ImGui::InputInt("##frm_slot", &model.frm_slot, 0, 0);
    if (model.frm_slot < 1) model.frm_slot = 1;
    if (model.frm_slot > 10) model.frm_slot = 10;
    ImGui::SameLine();
    char sscmd[96];
    if (ImGui::Button("Save state")) {
        std::snprintf(sscmd, sizeof(sscmd),
                      "\"cmd\":\"savestate\",\"op\":\"save\",\"slot\":%d",
                      model.frm_slot);
        dbg.send(sscmd, "savestate save");
        model.frm_status = "saved state to slot " + std::to_string(model.frm_slot);
    }
    ImGui::SameLine();
    if (ImGui::Button("Load state")) {
        std::snprintf(sscmd, sizeof(sscmd),
                      "\"cmd\":\"savestate\",\"op\":\"load\",\"slot\":%d",
                      model.frm_slot);
        dbg.send(sscmd, "savestate load");
        model.frm_status = "loaded state from slot " + std::to_string(model.frm_slot);
    }
    ImGui::SameLine();
    if (ImGui::Checkbox("turbo", &model.frm_turbo)) {
        char tcmd[64];
        std::snprintf(tcmd, sizeof(tcmd), "\"cmd\":\"turbo\",\"enabled\":%d",
                      model.frm_turbo ? 1 : 0);
        dbg.send(tcmd, "turbo");
    }
    ImGui::EndDisabled();
    ImGui::SameLine();
    muted(th, " · debug-server only — no key or pad binding, and absent entirely "
              "from a PSX_DEBUG_TOOLS=OFF build");

    ImGui::Separator();

    // ---- capture ------------------------------------------------------------
    left_label("Capture", 90.f);
    ImGui::SetNextItemWidth(140.f);
    ImGui::InputTextWithHint("##frm_tag", "tag", model.frm_tag, sizeof(model.frm_tag));
    ImGui::SameLine();
    ImGui::SetNextItemWidth(100.f);
    ImGui::InputInt("##frm_count", &model.frm_count, 0, 0);
    ImGui::SameLine();
    muted(th, "packets");
    ImGui::SameLine();
    ImGui::Checkbox("screenshot", &model.frm_shot);
    ImGui::SameLine();
    ImGui::SetNextItemWidth(110.f);
    ImGui::InputInt("##frm_at", &model.frm_at_frame, 0, 0);
    ImGui::SameLine();
    {
        // Reaching backwards is the whole point, so say plainly when the frame
        // asked for has already fallen out of the ring. A dump of an evicted
        // frame comes back empty, which reads exactly like "this frame drew
        // nothing" — a conclusion you might act on.
        const bool have_span = model.frm_ring_valid && model.frm_ring_total > 0;
        const bool out_of_ring =
            model.frm_at_frame > 0 && have_span &&
            (model.frm_at_frame < static_cast<int>(model.frm_ring_oldest) ||
             model.frm_at_frame > static_cast<int>(model.frm_ring_newest));
        if (out_of_ring)
            ImGui::TextColored(th.bad, "frame not in the ring (%u..%u)",
                               model.frm_ring_oldest, model.frm_ring_newest);
        else
            muted(th, "at frame (0 = newest)");
    }

    auto do_capture = [&](const char* tag) {
        std::vector<std::string> args = {
            "--host", model.frm_host,
            "--port", std::to_string(model.frm_port),
            "--tag", tag,
            "--out", model.frm_dir,
            "--count", std::to_string(std::max(1, model.frm_count)),
            "--summary",
        };
        if (model.frm_at_frame > 0) {
            args.push_back("--frame");
            args.push_back(std::to_string(model.frm_at_frame));
        }
        if (!model.frm_shot) args.push_back("--no-shot");
        const std::string want(tag);
        run_python_script_async(model, capture_tool, args,
                                [&model, want](RunResult r) {
            if (!r.ok()) {
                model.frm_status = "capture failed: " + tool_error(r);
                return;
            }
            scan_frame_tags(model);
            for (int i = 0; i < static_cast<int>(model.frm_tags.size()); ++i) {
                if (model.frm_tags[i] != want) continue;
                // A capture always becomes the suspect (B). The reference is
                // something you chose earlier and should not be overwritten by
                // taking another look at the broken frame.
                model.frm_sel_b = i;
                load_selected(model, 1);
            }
            model.frm_status = "captured " + want;
        }, true, JobSlot::Frames);
    };

    // Gate on the FRAMES slot and on the connection — not on the project slot.
    // The project slot is held for as long as a game runs, so gating on it
    // disabled capture at exactly the moment capture was possible.
    const bool frames_busy = model.busy_frames.load();
    const bool can_capture = have_tools && !frames_busy && live;
    ImGui::SameLine();
    if (accent_button(th, "Capture", can_capture && model.frm_tag[0] != '\0'))
        do_capture(model.frm_tag);
    ImGui::SameLine();
    ImGui::BeginDisabled(!can_capture);
    if (ImGui::Button("as good")) do_capture("good");
    ImGui::SameLine();
    if (ImGui::Button("as bad")) do_capture("bad");
    ImGui::EndDisabled();
    ImGui::SameLine();
    if (ImGui::Button("Rescan")) scan_frame_tags(model);

    if (have_tools && !live) {
        left_label("", 90.f);
        muted(th, "capture needs a connected game — launch it above, then Connect");
    }
    if (!have_tools) {
        ImGui::TextColored(th.warn,
                           "psxrecomp/tools/%s not found under this project — the GP0 "
                           "tools live in the engine submodule, so check it out with "
                           "`git submodule update --init`.", kCaptureTool);
    }
    muted(th, "captures in %s", model.frm_dir);

    // ---- selection ----------------------------------------------------------
    left_label("Compare", 90.f);
    ImGui::SetNextItemWidth(180.f);
    if (ImGui::BeginCombo("##frm_a", tag_at(model, model.frm_sel_a))) {
        for (int i = 0; i < static_cast<int>(model.frm_tags.size()); ++i) {
            if (ImGui::Selectable(model.frm_tags[i].c_str(), model.frm_sel_a == i)) {
                model.frm_sel_a = i;
                load_selected(model, 0);
            }
        }
        ImGui::EndCombo();
    }
    ImGui::SameLine();
    muted(th, "reference (A)   vs");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(180.f);
    if (ImGui::BeginCombo("##frm_b", tag_at(model, model.frm_sel_b))) {
        for (int i = 0; i < static_cast<int>(model.frm_tags.size()); ++i) {
            if (ImGui::Selectable(model.frm_tags[i].c_str(), model.frm_sel_b == i)) {
                model.frm_sel_b = i;
                load_selected(model, 1);
            }
        }
        ImGui::EndCombo();
    }
    ImGui::SameLine();
    muted(th, "suspect (B)");

    ImGui::SameLine();
    const bool can_diff = have_tools && !frames_busy &&
                          model.frm_sel_a >= 0 && model.frm_sel_b >= 0 &&
                          model.frm_sel_a != model.frm_sel_b;
    if (accent_button(th, "Diff", can_diff)) {
        const std::string diff_tool = gpu_tool_path(root, kDiffTool);
        const std::string a = (fs::path(model.frm_dir) /
                               (model.frm_tags[model.frm_sel_a] + ".json")).string();
        const std::string b = (fs::path(model.frm_dir) /
                               (model.frm_tags[model.frm_sel_b] + ".json")).string();
        const std::string outp = (fs::path(model.frm_dir) / "diff.json").string();
        run_python_script_async(model, diff_tool, {a, b, "--json", outp},
                                [&model, outp](RunResult r) {
            if (r.ok()) {
                load_frame_diff(model.frm_diff, outp);
                model.frm_pane = 1;
            } else {
                model.frm_diff = FrameDiff{};
                model.frm_diff.error = "diff failed: " + tool_error(r);
                model.frm_status = model.frm_diff.error;
            }
        }, true, JobSlot::Frames);
    }
    ImGui::SameLine();
    const bool can_layers = have_tools && !frames_busy && model.frm_sel_b >= 0;
    if (accent_button(th, "Render layers of B", can_layers)) {
        const std::string tool = gpu_tool_path(root, kLayersTool);
        const std::string tag = model.frm_tags[model.frm_sel_b];
        const std::string dump = (fs::path(model.frm_dir) / (tag + ".json")).string();
        const std::string outd = (fs::path(model.frm_dir) / (tag + "-layers")).string();
        std::vector<std::string> args = {dump, "--out", outd,
                                         "--max-layers",
                                         std::to_string(std::max(1, model.frm_max_layers)),
                                         "--order",
                                         model.frm_layer_order == 1 ? "ot" : "issue"};
        if (model.frm_tex_tint) args.push_back("--tex-tint");
        run_python_script_async(model, tool, args, [&model, outd](RunResult r) {
            release_layer_textures(model.frm_layers);
            model.frm_layers = FrameLayers{};
            if (r.ok()) {
                load_frame_layers(model.frm_layers, outd);
                model.frm_layer_sel = -1;
                model.frm_pane = 2;
            } else {
                model.frm_layers.error = "layer render failed: " + tool_error(r);
                model.frm_status = model.frm_layers.error;
            }
        }, true, JobSlot::Frames, {"numpy", "PIL"});
    }
    ImGui::SameLine();
    ImGui::SetNextItemWidth(110.f);
    ImGui::Combo("##frm_order", &model.frm_layer_order, "issue order\0OT rank\0");
    ImGui::SameLine();
    ImGui::Checkbox("tint textures", &model.frm_tex_tint);

    // ---- oracle -------------------------------------------------------------
    // Machine-wide, installed under the RetComM data root — one build serves
    // every title, which is why none of this takes a --root.
    //
    // Two of them, and they are not interchangeable. DuckStation answers about
    // frames and memory (VRAM, GPU/DMA/IRQ state, breakpoints, pause/step);
    // Beetle answers about traces (wtrace, rtrace, fntrace, SIO, CD commands)
    // and registers none of DuckStation's. Neither is a superset, so this is a
    // choice about what you are investigating, not a preference.
    load_oracle_caps(model);
    if (!model.frm_oracle_queried) refresh_oracle(model, root);

    const OracleStatus& orc = model.frm_oracle;
    const bool orc_busy = model.busy_global.load();
    const std::string orc_tool = oracle_tool_path(model, root);

    left_label("Oracle", 90.f);
    {
        ImGui::SetNextItemWidth(140.f);
        int kind = model.frm_oracle_kind == OracleKind::Beetle ? 1 : 0;
        ImGui::BeginDisabled(orc_busy);
        if (ImGui::Combo("##orc_kind", &kind, "DuckStation\0Beetle PSX\0")) {
            const OracleKind want = kind == 1 ? OracleKind::Beetle
                                              : OracleKind::DuckStation;
            if (want != model.frm_oracle_kind) {
                model.frm_oracle_kind = want;
                // The old status describes the other emulator on the other
                // port. Drop it rather than let a stale "answering" survive the
                // switch and send the next tool at a socket nothing is bound to.
                model.frm_oracle = OracleStatus{};
                model.frm_oracle_queried = false;
                model.frm_oracle_note.clear();
                model.append_log(std::string("Oracle: ") + oracle_display(want) +
                                 " (" + oracle_tool(want) + ")");
            }
        }
        ImGui::EndDisabled();
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
            ImGui::SetTooltip(
                "DuckStation — frames and memory: VRAM, GPU/DMA/IRQ state,\n"
                "breakpoints, and the pause/step park the display-list tools need.\n\n"
                "Beetle PSX — traces: write/read/function/SIO/CD-command traces\n"
                "and the SPU rings. No pause and no VRAM readback.\n\n"
                "Neither is a superset. A tool that needs the other one is greyed\n"
                "out below with the reason.");
        }
        ImGui::SameLine();
    }
    if (!orc.valid) {
        muted(th, "%s", orc.error.empty() ? "checking…" : orc.error.c_str());
    } else if (orc.answering) {
        ImGui::TextColored(th.good, "running — answering on %d", orc.port);
        // A PC breakpoint pauses DuckStation, and a paused oracle serves its
        // debug socket from a Qt idle timer — roughly 1 Hz with no gamepad
        // attached. Every tool then times out, which looks like a network
        // fault rather than a parked emulator, so give it an obvious undo.
        ImGui::SameLine();
        if (ImGui::SmallButton("Resume##orc")) {
            oracle_debug_client().send(R"("cmd":"continue")", "continue");
            model.frm_status = "sent continue to the oracle";
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Un-park the oracle. A breakpoint hit leaves it "
                              "paused, and a paused oracle answers about once a "
                              "second — every later run then times out.");
    } else if (orc.running) {
        ImGui::TextColored(th.warn, "port %d is busy but not answering as an oracle",
                           orc.port);
    } else if (orc.installed) {
        ImGui::TextColored(th.text_muted, "installed, not running  ·  %s",
                           orc.root.c_str());
    } else {
        wrapped(th.warn, "not installed  (%s)", orc.state.c_str());
    }
    if (!orc.blockers.empty()) {
        // Beetle's pinned beetle-psx tree is missing hooks that the committed
        // patches do not carry (docs/beetle-linux.md says so outright). Naming
        // them here is the difference between "the build failed" and a wall of
        // C++ errors nobody traces back to a patch that was never complete.
        std::string names;
        for (const std::string& b : orc.blockers) {
            if (!names.empty()) names += ", ";
            names += b;
        }
        wrapped(th.warn, "missing upstream hooks: %s", names.c_str());
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
            ImGui::SetTooltip(
                "The committed beetle_*.patch files do not carry these hooks —\n"
                "docs/beetle-linux.md records that they have to be re-landed by\n"
                "hand from a prior beetle-psx tree. Traces that depend on them\n"
                "will not fire.");
        }
    }

    ImGui::SameLine();
    ImGui::BeginDisabled(orc_tool.empty() || orc_busy);
    if (!orc.installed) {
        if (accent_button(th, (std::string("Set up ") +
                              oracle_display(model.frm_oracle_kind)).c_str())) {
            model.frm_oracle_note =
                "building the oracle — this takes about ten minutes; the rest of "
                "Studio stays usable";
            run_python_script_async(model, orc_tool, {"all"},
                                    [&model](RunResult r) {
                                        model.frm_oracle_note =
                                            r.ok() ? "oracle build finished"
                                                   : "oracle build failed — see the log";
                                        refresh_oracle(model, model.selected_root());
                                    },
                                    true, JobSlot::Global);
        }
    } else if (!orc.answering) {
        const std::string disc = game_disc_for(root);
        const bool need_bios = model.frm_oracle_kind == OracleKind::Beetle;
        const std::string bios = need_bios ? game_bios_for(root) : std::string();
        ImGui::BeginDisabled(disc.empty() || (need_bios && bios.empty()));
        if (accent_button(th, "Start oracle")) {
            model.frm_oracle_note = "starting the oracle on " + disc;
            // Boots from a COPY of the game's memory card, so the oracle can
            // reach the same scene as the runtime it is being compared with.
            // Without it any breakpoint on overlay code never fires: that
            // overlay is only resident partway into the game.
            start_oracle(model, orc_tool, root, disc);
        }
        ImGui::EndDisabled();
        if (disc.empty()) {
            ImGui::SameLine();
            muted(th, "no [game] disc in game.toml — the oracle must boot the "
                      "same image psx-runtime does");
        } else if (need_bios && bios.empty()) {
            ImGui::SameLine();
            muted(th, "no BIOS under psxrecomp/bios/ — Beetle loads one itself "
                      "and exits without it");
        }
    } else {
        if (ImGui::Button("Stop oracle")) {
            run_python_script_async(model, orc_tool, {"stop"},
                                    [&model](RunResult) {
                                        refresh_oracle(model, model.selected_root());
                                    },
                                    true, JobSlot::Global);
        }
    }
    ImGui::SameLine();
    if (ImGui::Button("Refresh##orc")) refresh_oracle(model, root);
    ImGui::EndDisabled();

    if (orc_busy) {
        ImGui::SameLine();
        ImGui::TextColored(th.accent, "working…");
    }
    if (orc.valid && !orc.installed && orc.container_needed) {
        left_label("", 90.f);
        if (orc.container_engine.empty())
            ImGui::TextColored(th.bad,
                               "This host (%s) is one upstream's build refuses, and "
                               "no podman/docker is installed to build it somewhere "
                               "supported.", orc.container_reason.c_str());
        else
            muted(th, "builds inside ubuntu:22.04 via %s — upstream's own CI image "
                      "— because its CMake refuses this host (%s)",
                  orc.container_engine.c_str(), orc.container_reason.c_str());
    }
    if (!model.frm_oracle_note.empty()) {
        left_label("", 90.f);
        muted(th, "%s", model.frm_oracle_note.c_str());
    }

    // Parity asks the one question that splits the search space in half before
    // any of the above matters: did the guest run the same on both emulators?
    const std::string parity_tool = gpu_tool_path(root, kParityTool);
    ImGui::BeginDisabled(parity_tool.empty() || busy || !live || !orc.answering);
    if (ImGui::Button("DuckStation parity at this frame")) {
        const std::string outd = (fs::path(model.frm_dir) / "parity").string();
        // Wait for the effect on both sides. An image diff of a scene that
        // does not contain it answers a different question — and every other
        // tool here had to learn the same lesson.
        std::vector<std::string> args = {"--host", model.frm_host,
                                         "--native-port", std::to_string(model.frm_port),
                                         "--out", outd,
                                         "--wait-for", std::string(model.frm_writer_class).substr(
                                             0, std::string(model.frm_writer_class)
                                                    .find('|'))};
        if (model.frm_at_frame > 0) {
            // Only DuckStation is driven to this frame — the native run cannot
            // be steered, so it is sampled where it already is.
            args.push_back("--frame");
            args.push_back(std::to_string(model.frm_at_frame));
        }
        const std::string pj = (fs::path(outd) / "parity.json").string();
        run_python_script_async(model, parity_tool, args, [&model, pj](RunResult r) {
            if (!r.ok()) {
                model.frm_status = "parity failed (is DuckStation running with "
                                   "the oracle patch on port 4371?)";
                return;
            }
            // Lead with alignment. Every other number in the report is
            // conditional on the two emulators having been on the same frame.
            const std::string a = parity_alignment(pj);
            model.frm_status = a.empty()
                ? "parity run complete — verdict is in the log"
                : ("parity: " + a);
        });
    }
    ImGui::EndDisabled();
    if (!parity_tool.empty() && !orc.answering) {
        ImGui::SameLine();
        muted(th, "needs the oracle running — start it above");
    }

    if (!model.frm_status.empty()) {
        left_label("", 90.f);
        const bool bad = model.frm_status.find("failed") != std::string::npos ||
                         model.frm_status.find("NOT ALIGNED") != std::string::npos;
        if (bad) {
            ImGui::PushStyleColor(ImGuiCol_Text, th.bad);
            ImGui::TextWrapped("%s", model.frm_status.c_str());
            ImGui::PopStyleColor();
        }
        else muted(th, "%s", model.frm_status.c_str());
        if (bad) {
            ImGui::SameLine();
            if (ImGui::SmallButton("clear##status")) model.frm_status.clear();
        }
    }

    // ---- what to do next ----------------------------------------------------
    // Capture / Diff / Layers is a sequence, and a row of buttons does not say
    // which one you are supposed to press first. Name the single next action for
    // the state actually on screen.
    {
        const bool have_a = model.frm_sel_a >= 0;
        const bool have_b = model.frm_sel_b >= 0;
        const char* step = nullptr;
        if (!have_tools)
            step = "Check out the psxrecomp submodule — the capture tools live there.";
        else if (!live)
            step = (model.frm_launch_pid > 0)
                       ? "1 / 4  ·  The game is running — press Connect above."
                       : "1 / 4  ·  Press \"Launch game\", then Connect. Launch from "
                         "HERE rather than the Build tab: Build's Launch holds a job "
                         "slot for the whole time the game runs.";
        else if (model.frm_tags.empty())
            step = "2 / 4  ·  While the scene still looks RIGHT, press \"as good\". "
                   "Then play on until it breaks and press \"as bad\". Nothing has to "
                   "be paused — the ring holds the last few hundred frames.";
        else if (!have_a || !have_b || model.frm_sel_a == model.frm_sel_b)
            step = "3 / 4  ·  Capture a second frame, then pick reference (A) and "
                   "suspect (B) in the two dropdowns. To reach back in time, put a "
                   "frame from the Ring span into \"at frame\" before capturing.";
        else if (!model.frm_diff.loaded)
            step = "4 / 4  ·  Press Diff. It names the functions that stopped drawing "
                   "and the blend modes that vanished.";
        else
            step = "Press \"Render layers of B\" to see one image per guest function, "
                   "or click a function in Attribution to name it.";
        left_label("Next", 90.f);
        ImGui::PushStyleColor(ImGuiCol_Text, th.accent);
        ImGui::TextWrapped("%s", step);
        ImGui::PopStyleColor();
    }

    ImGui::Separator();

    // ---- panes --------------------------------------------------------------
    FrameSummary& shown = model.frm_b.loaded ? model.frm_b : model.frm_a;

    // A finished Diff / Render-layers job asks for its pane by setting
    // frm_pane. Read and clear that request BEFORE the tab bar: SetSelected is
    // a one-shot, and the currently-active tab's body draws before the later
    // tabs are even declared, so anything that reads frm_pane inside a tab body
    // would consume the request before the tab it was meant for saw it.
    const int want_pane = model.frm_pane;
    model.frm_pane = -1;
    const auto sel_if = [&](int pane) -> ImGuiTabItemFlags {
        return want_pane == pane ? ImGuiTabItemFlags_SetSelected : 0;
    };

    if (ImGui::BeginTabBar("##frm_panes")) {
        if (ImGui::BeginTabItem("Attribution", nullptr, sel_if(0))) {
            draw_attribution(model, th, shown);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Diff", nullptr, sel_if(1))) {
            draw_diff_pane(model, th);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Layers", nullptr, sel_if(2))) {
            draw_layers_pane(model, th);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Opcodes", nullptr, sel_if(3))) {
            draw_opcodes_pane(th, shown);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Scan", nullptr, sel_if(5))) {
            draw_scan_pane(model, th, root);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Display list", nullptr, sel_if(6))) {
            draw_dlist_pane(model, th, root);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Shading", nullptr, sel_if(8))) {
            draw_shading_pane(model, th, root);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Colours", nullptr, sel_if(7))) {
            draw_colour_pane(model, th, root);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Oracle", nullptr, sel_if(4))) {
            draw_oracle_pane(model, th, root);
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }
}

} // namespace retcomm::studio
