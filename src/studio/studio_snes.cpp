// studio_snes.cpp — the SNES Diagnostics tab.
//
// Two emulators, never one. The runtime under test and the Mesen oracle run as
// separate processes on the SAME ROM, and neither is ever paused to line them
// up: they are compared by SCENE, from history each side already keeps. That is
// the standing ruling in the workspace rules, and it is not a style preference
// — an in-process lockstep reference was measured desyncing, and pausing to
// synchronise two observers is what turns a dropped connection into an apparent
// freeze.
//
// The panes at the bottom are toolsets, one per question:
//
//   Capture    a frame bundle, then decode and attribute it   (the 3-step tool
//              pipeline in tools/snes_analysis)
//   Palette    what is in CGRAM right now, against a Mesen dump — the census
//              matters more than the swatches
//   Screen     ourframebuffer beside the oracle's, with a colour census, since
//              "screenshot before asserting anything about visible state" is a
//              hard stop in the rules
//   Raster     beam-stamped register writes from the oracle: the measurement
//              our own runtime cannot make
//   APU        sound state, which a silent host makes look identical to a
//              working one
//   Oracle     install / doctor / provider for Mesen itself
//
// Studio decodes nothing here. tools/snes_analysis owns the wire protocol, the
// asset decode and the attribution, so a headless capture and this tab can
// never disagree about what a frame contained.

#include "studio/studio_snes.hpp"
#include "studio/studio_frames.hpp"
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

const char* kCaptureTool = "snes_frame_capture.py";
const char* kDecodeTool = "snes_asset_decode.py";
const char* kAttributeTool = "snes_frame_attribute.py";
const char* kOracleTool = "mesen_oracle.py";
const char* kLoopTool = "snes_loop_compare.py";
const char* kSpriteDiffTool = "snes_sprite_diff.py";

// The Lua probes. Each answers a different question and they are NOT
// interchangeable, so the tab names the question rather than the file.
struct LuaScript {
    const char* file;
    const char* label;
    const char* what;
    bool        uses_dir;    // GW_DIR (a directory) vs GW_OUT (a single file)
};

const LuaScript kScripts[] = {
    {"mesen_frames.lua", "Frame run (watch-triggered)",
     "a RUN of consecutive frames, started when a WRAM marker matches "
     "(GW_WATCH=ADDR=VAL) rather than at a frame number, because Mesen's "
     "frame counter drifts by hundreds of frames between launches. Drive the "
     "emulator by hand; it captures itself. GW_OAM=1 adds the sprite table", true},
    {"mesen_scene_sample.lua", "Scene sample",
     "periodic PPU state + screenshots, so a run can be aligned by picture "
     "rather than by frame number", true},
    {"mesen_raster_trace.lua", "Raster trace",
     "every PPU/IRQ register write for one frame, stamped with scanline and "
     "hclock", false},
    {"mesen_vram_dump.lua", "VRAM / CGRAM dump",
     "VRAM, CGRAM and OAM at a chosen frame, with a screenshot to prove which "
     "scene it was", true},
    {"mesen_exec_probe.lua", "Exec probe",
     "does the reference ever EXECUTE a PC range — the other half of a \"we "
     "never reach this code\" claim", false},
};
constexpr int kScriptCount = static_cast<int>(sizeof(kScripts) / sizeof(kScripts[0]));

// ---- small UI helpers (mirrors of the Frames tab's, deliberately local) -----

void left_label(const char* text, float w) {
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(text);
    ImGui::SameLine(w);
}

// Colour AND wrap. ImGui ships TextColored and TextWrapped but not both, and
// an unwrapped line is not merely ugly here: it widens the content region, so
// the page grows a horizontal scrollbar and every column on it starts shifting
// under the mouse. These messages are the long ones by nature — a tool's
// stderr, a diagnosis, a path — so they are exactly the ones that must wrap.
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

void release_texture(unsigned& tex) {
    if (tex) {
        GLuint t = static_cast<GLuint>(tex);
        glDeleteTextures(1, &t);
        tex = 0;
    }
}

// Screenshots are re-taken rather than accumulated, so they are re-uploaded on
// every refresh instead of cached on first sight.
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

// Last few lines of a file, for surfacing a child process's dying words.
std::string tail_of_file(const std::string& path, int lines) {
    std::error_code ec;
    if (!fs::is_regular_file(path, ec)) return {};
    std::FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return {};
    std::fseek(f, 0, SEEK_END);
    long size = std::ftell(f);
    const long want = 8192;
    const long from = size > want ? size - want : 0;
    std::fseek(f, from, SEEK_SET);
    std::string buf;
    buf.resize(static_cast<size_t>(size - from));
    const size_t got = std::fread(buf.data(), 1, buf.size(), f);
    std::fclose(f);
    buf.resize(got);
    std::vector<std::string> out;
    size_t pos = 0;
    while (pos <= buf.size()) {
        const size_t nl = buf.find('\n', pos);
        std::string line = buf.substr(pos, nl == std::string::npos ? std::string::npos
                                                                  : nl - pos);
        while (!line.empty() && (line.back() == '\r' || line.back() == ' '))
            line.pop_back();
        if (!line.empty()) out.push_back(line);
        if (nl == std::string::npos) break;
        pos = nl + 1;
    }
    std::string joined;
    const int start = std::max(0, static_cast<int>(out.size()) - lines);
    for (int i = start; i < static_cast<int>(out.size()); ++i) {
        if (!joined.empty()) joined += " / ";
        joined += out[i];
    }
    return joined;
}

ImTextureID tex_id(unsigned tex) {
    return static_cast<ImTextureID>(static_cast<intptr_t>(tex));
}

// ---- paths -----------------------------------------------------------------

void sync_dirs(StudioModel& model, const std::string& root) {
    const std::string frames = (fs::path(root) / "analysis" / "frames").string();
    if (model.snes_frames_dir != frames) {
        model.snes_frames_dir = frames;
        scan_snes_bundles(model);
    }
    if (model.snes_lua_out[0] == '\0') {
        const std::string d = (fs::path(root) / "analysis" / "oracle").string();
        std::snprintf(model.snes_lua_out, sizeof(model.snes_lua_out), "%s", d.c_str());
    }
}

std::string build_path(const StudioModel& model, const std::string& root) {
    return (fs::path(root) / model.build_dir).string();
}

// The ROM the comparison is allowed to use. rom.cfg is what a Launch from here
// will boot, so pointing the oracle anywhere else silently compares two
// different games — the override exists only for a build that has not run yet.
std::string effective_rom(const StudioModel& model) {
    if (model.snes_rom_override[0]) return model.snes_rom_override;
    return model.snes_rom;
}

std::string scratch_dir(const StudioModel& model, const std::string& root) {
    (void)model;
    return (fs::path(root) / "analysis" / "oracle").string();
}

// ---- Mesen -----------------------------------------------------------------

void refresh_mesen(StudioModel& model) {
    const std::string tool = snes_tool_path(kOracleTool);
    if (tool.empty()) {
        model.snes_mesen.probed = true;
        model.snes_mesen.error = "tools/snes_analysis not found beside the toolkit";
        model.snes_mesen.summary = model.snes_mesen.error;
        return;
    }
    if (model.snes_mesen_probing) return;
    model.snes_mesen_probing = true;
    run_python_script_async(
        model, tool, {"status", "--json"},
        [&model](RunResult r) {
            model.snes_mesen_probing = false;
            model.snes_mesen = parse_mesen_status(r.stdout_text);
            if (!r.ok() && model.snes_mesen.error.empty()) {
                model.snes_mesen.error = tool_error(r);
                model.snes_mesen.summary = model.snes_mesen.error;
            }
        },
        /*log_stdout=*/false, JobSlot::Global);
}

void launch_mesen(StudioModel& model, const std::string& root) {
    const std::string rom = effective_rom(model);
    if (rom.empty()) {
        model.snes_mesen_error = "no ROM — run the game once so rom.cfg is written, "
                                 "or set the override above";
        return;
    }
    if (model.snes_mesen.binary.empty()) {
        model.snes_mesen_error = "Mesen is not installed — use the Oracle pane";
        return;
    }
    const LuaScript& s = kScripts[std::clamp(model.snes_mesen_script, 0, kScriptCount - 1)];
    const std::string lua = snes_tool_path(s.file);
    if (lua.empty()) {
        model.snes_mesen_error = std::string("missing ") + s.file;
        return;
    }
    const std::string out = model.snes_lua_out[0] ? model.snes_lua_out
                                                  : scratch_dir(model, root);
    std::error_code ec;
    fs::create_directories(out, ec);

    // The Lua probes take their parameters from the environment, because Mesen
    // passes no argv through to a script.
    std::vector<std::pair<std::string, std::string>> env;
    if (s.uses_dir) {
        env.push_back({"GW_DIR", out});
    } else {
        env.push_back({"GW_OUT", (fs::path(out) / (std::string(s.file) + ".out")).string()});
    }
    env.push_back({"GW_FRAME", std::to_string(model.snes_lua_frame)});
    env.push_back({"GW_SPAN", std::to_string(model.snes_lua_span)});

    model.snes_mesen_log = (fs::path(root) / "mesen.log").string();
    std::string err;
    const long pid = spawn_detached_logged(model.snes_mesen.binary, {rom, lua}, root,
                                           model.snes_mesen_log, env, &err);
    model.snes_mesen_pid = pid;
    model.snes_mesen_error = pid ? "" : err;
    if (pid) {
        model.snes_status = std::string("oracle started with ") + s.label;
        // Mesen rewrites settings.json on exit, so AllowIoOsAccess has to have
        // been set while it was STOPPED. If a script writes nothing, that is
        // very likely why — say so before it costs an hour.
        model.append_log("Mesen: launched " + std::string(s.file) + " -> " + out);
        model.append_log("Mesen: if the script writes nothing, check "
                         "Debug > ScriptWindow > AllowIoOsAccess (set it while "
                         "Mesen is NOT running — it rewrites settings.json on exit).");
    }
}

// ---- panes -----------------------------------------------------------------

void draw_capture_pane(StudioModel& model, const Theme& th, const std::string& root) {
    const std::string cap = snes_tool_path(kCaptureTool);
    if (cap.empty()) {
        muted(th, "tools/snes_analysis/%s not found beside the toolkit.", kCaptureTool);
        return;
    }
    const auto snap = snes_debug_client().snapshot();
    const bool live = snap.state == SnesDebugClient::State::Connected;

    ImGui::TextWrapped(
        "Capture reaches BACKWARDS: the runner keeps per-frame history, so you "
        "play until the bug is on screen and then take the frame it was on. "
        "Nothing is paused.");
    ImGui::Spacing();

    left_label("Tag", 90.f);
    ImGui::SetNextItemWidth(140.f);
    ImGui::InputText("##snes_tag", model.snes_capture_tag, sizeof(model.snes_capture_tag));
    ImGui::SameLine();
    muted(th, "· names the bundle — \"good\" and \"bad\" is the pair a diff wants");

    left_label("At frame", 90.f);
    ImGui::SetNextItemWidth(140.f);
    ImGui::InputInt("##snes_capframe", &model.snes_capture_frame, 0, 0);
    ImGui::SameLine();
    muted(th, "· 0 = newest in the ring");
    ImGui::SameLine();
    ImGui::Checkbox("include WRAM", &model.snes_capture_wram);

    left_label("", 90.f);
    if (accent_button(th, "Capture", live && !model.busy.load())) {
        std::vector<std::string> args = {
            "--host", model.snes_host,
            "--port", std::to_string(model.snes_port),
            "--tag", model.snes_capture_tag,
            "--out", model.snes_frames_dir,
        };
        if (model.snes_capture_frame > 0) {
            args.push_back("--frame");
            args.push_back(std::to_string(model.snes_capture_frame));
        }
        if (model.snes_capture_wram) args.push_back("--wram");
        run_python_script_async(
            model, cap, args,
            [&model](RunResult r) {
                model.snes_status = r.ok() ? "captured" : ("capture failed: " + tool_error(r));
                scan_snes_bundles(model);
            },
            true, JobSlot::Frames, {"PIL"});
    }
    if (!live) {
        ImGui::SameLine();
        muted(th, "connect to a running runtime first");
    }

    ImGui::Separator();
    ImGui::TextUnformatted("Bundles");
    ImGui::SameLine();
    if (ImGui::SmallButton("Rescan")) scan_snes_bundles(model);
    ImGui::SameLine();
    muted(th, "· %s", model.snes_frames_dir.c_str());

    if (model.snes_bundles.empty()) {
        muted(th, "nothing captured yet.");
        return;
    }
    if (ImGui::BeginTable("##snes_bundles", 5,
                          ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("tag");
        ImGui::TableSetupColumn("frame");
        ImGui::TableSetupColumn("decoded");
        ImGui::TableSetupColumn("attributed");
        ImGui::TableSetupColumn("");
        ImGui::TableHeadersRow();
        for (int i = 0; i < static_cast<int>(model.snes_bundles.size()); ++i) {
            const SnesBundle& b = model.snes_bundles[i];
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            if (ImGui::Selectable(b.tag.c_str(), model.snes_bundle_sel == i,
                                  ImGuiSelectableFlags_SpanAllColumns))
                model.snes_bundle_sel = i;
            ImGui::TableNextColumn();
            ImGui::Text("%llu", static_cast<unsigned long long>(b.frame));
            ImGui::TableNextColumn();
            if (b.decoded) ImGui::TextColored(th.good, "yes");
            else muted(th, "no");
            ImGui::TableNextColumn();
            if (b.attributed) ImGui::TextColored(th.good, "yes");
            else muted(th, "no");
            ImGui::TableNextColumn();
            ImGui::PushID(i);
            const std::string assets =
                (fs::path(model.snes_frames_dir).parent_path() / "assets").string();
            const std::string dec = snes_tool_path(kDecodeTool);
            const std::string att = snes_tool_path(kAttributeTool);
            if (ImGui::SmallButton("Decode") && !dec.empty()) {
                run_python_script_async(
                    model, dec, {b.path, "--out", assets},
                    [&model](RunResult r) {
                        model.snes_status = r.ok() ? "decoded"
                                                   : ("decode failed: " + tool_error(r));
                        scan_snes_bundles(model);
                    },
                    true, JobSlot::Project, {"PIL"});
            }
            ImGui::SameLine();
            if (ImGui::SmallButton("Attribute") && !att.empty()) {
                run_python_script_async(
                    model, att, {b.path, "--out", assets},
                    [&model](RunResult r) {
                        model.snes_status = r.ok() ? "attributed"
                                                   : ("attribute failed: " + tool_error(r));
                        scan_snes_bundles(model);
                    },
                    true, JobSlot::Project);
            }
            ImGui::PopID();
        }
        ImGui::EndTable();
    }
    (void)root;
}

void draw_palette_pane(StudioModel& model, const Theme& th) {
    SnesDebugClient& dbg = snes_debug_client();
    const auto snap = dbg.snapshot();
    const bool live = snap.state == SnesDebugClient::State::Connected;

    ImGui::TextWrapped(
        "A real palette repeats a colour a handful of times. One value repeated "
        "across unrelated palettes is a FILL — either a stale block the current "
        "scene never re-uploaded, or a bad write. The census below tells those "
        "apart faster than the swatches do.");
    ImGui::Spacing();

    if (accent_button(th, "Read CGRAM", live)) {
        dbg.request("dump_cgram", "cgram");
        model.snes_cgram_pending = true;
    }
    ImGui::SameLine();
    if (ImGui::Button("Load Mesen dump")) {
        // mesen_vram_dump.lua writes <tag>_cgram.bin beside its screenshot.
        const std::string dir = model.snes_lua_out;
        std::string found;
        std::error_code ec;
        if (fs::is_directory(dir, ec)) {
            for (const auto& e : fs::directory_iterator(dir, ec)) {
                if (!e.is_regular_file()) continue;
                const std::string n = e.path().filename().string();
                if (n.size() > 10 && n.rfind("_cgram.bin") == n.size() - 10)
                    found = e.path().string();   // last one wins: newest tag
            }
        }
        if (found.empty()) {
            model.snes_cgram_ref.error =
                "no *_cgram.bin in " + dir + " — run the VRAM/CGRAM dump script";
            model.snes_cgram_ref.loaded = false;
        } else {
            load_cgram_bin(model.snes_cgram_ref, found);
            model.snes_cgram_ref_path = found;
        }
    }
    ImGui::SameLine();
    muted(th, "· reference side, from mesen_vram_dump.lua");

    if (model.snes_cgram_pending) {
        std::string reply;
        if (dbg.take_reply("cgram", reply)) {
            model.snes_cgram_pending = false;
            parse_cgram(model.snes_cgram, reply);
        }
    }

    const auto census = [&](const CgramView& v, const char* who) {
        if (!v.loaded) {
            if (!v.error.empty()) wrapped(th.bad, "%s: %s", who, v.error.c_str());
            else muted(th, "%s: not read yet", who);
            return;
        }
        const uint32_t rgb = bgr555_to_rgb(v.top_value);
        ImGui::Text("%s: %d distinct, %d zero", who, v.distinct, v.zero_count);
        ImGui::SameLine();
        ImGui::TextUnformatted("· most repeated");
        ImGui::SameLine();
        ImGui::ColorButton("##top", ImVec4(((rgb >> 16) & 0xFF) / 255.f,
                                           ((rgb >> 8) & 0xFF) / 255.f,
                                           (rgb & 0xFF) / 255.f, 1.f),
                           ImGuiColorEditFlags_NoTooltip, ImVec2(14, 14));
        ImGui::SameLine();
        // 16 of one colour is a whole palette's worth and is the threshold at
        // which "repeated" stops being plausible art.
        if (v.top_count >= 16)
            ImGui::TextColored(th.warn, "0x%04X x%d  ← fill", v.top_value, v.top_count);
        else
            ImGui::Text("0x%04X x%d", v.top_value, v.top_count);
    };
    ImGui::Separator();
    census(model.snes_cgram, "ours");
    census(model.snes_cgram_ref, "oracle");

    const int diff = cgram_diff_count(model.snes_cgram, model.snes_cgram_ref);
    if (diff >= 0) {
        if (diff == 0) ImGui::TextColored(th.good, "CGRAM identical (256/256)");
        else ImGui::TextColored(diff > 32 ? th.bad : th.warn,
                                "%d of 256 entries differ", diff);
        muted(th, "Only meaningful if both sides were on the SAME scene — "
                  "compare the screenshots in the Screen pane first.");
    }

    if (!model.snes_cgram.loaded) return;
    ImGui::Separator();
    ImGui::TextUnformatted("Palettes 0-15  (row = palette, 16 entries each)");
    const bool have_ref = model.snes_cgram_ref.loaded;
    for (int p = 0; p < 16; ++p) {
        ImGui::Text("%2d", p);
        for (int i = 0; i < 16; ++i) {
            const int idx = p * 16 + i;
            const uint16_t v = model.snes_cgram.entry[idx];
            const uint32_t rgb = bgr555_to_rgb(v);
            ImGui::SameLine();
            ImGui::PushID(idx);
            const bool differs = have_ref && model.snes_cgram_ref.entry[idx] != v;
            if (differs)
                ImGui::PushStyleColor(ImGuiCol_Border, th.bad);
            ImGui::ColorButton("##sw",
                               ImVec4(((rgb >> 16) & 0xFF) / 255.f,
                                      ((rgb >> 8) & 0xFF) / 255.f,
                                      (rgb & 0xFF) / 255.f, 1.f),
                               ImGuiColorEditFlags_NoTooltip |
                                   (differs ? ImGuiColorEditFlags_None
                                            : ImGuiColorEditFlags_NoBorder),
                               ImVec2(16, 16));
            if (differs) ImGui::PopStyleColor();
            if (ImGui::IsItemHovered()) {
                ImGui::BeginTooltip();
                ImGui::Text("index %d  (pal %d, entry %d)", idx, p, i);
                ImGui::Text("ours   0x%04X  #%06X", v, rgb);
                if (have_ref) {
                    const uint16_t rv = model.snes_cgram_ref.entry[idx];
                    ImGui::Text("oracle 0x%04X  #%06X", rv, bgr555_to_rgb(rv));
                }
                ImGui::EndTooltip();
            }
            ImGui::PopID();
        }
    }
}

void draw_screen_pane(StudioModel& model, const Theme& th, const std::string& root) {
    SnesDebugClient& dbg = snes_debug_client();
    const auto snap = dbg.snapshot();
    const bool live = snap.state == SnesDebugClient::State::Connected;

    ImGui::TextWrapped(
        "Screenshot before asserting anything about visible state. Counters and "
        "ring flags are instrument state, not pixels — a runtime can report 60 "
        "fps and a rising frame counter while presenting nothing at all.");
    ImGui::Spacing();

    if (accent_button(th, "Grab ours", live)) {
        const std::string dir = scratch_dir(model, root);
        std::error_code ec;
        fs::create_directories(dir, ec);
        const std::string path = (fs::path(dir) / "ours.bmp").string();
        dbg.request("screenshot " + path, "shot");
        model.snes_shot_pending = true;
    }
    ImGui::SameLine();
    if (ImGui::Button("Load oracle shot")) {
        const std::string dir = model.snes_lua_out;
        std::string found;
        std::error_code ec;
        if (fs::is_directory(dir, ec)) {
            for (const auto& e : fs::directory_iterator(dir, ec)) {
                if (!e.is_regular_file()) continue;
                if (e.path().extension() == ".png") found = e.path().string();
            }
        }
        if (found.empty()) {
            model.snes_shot_ref_info = "no .png in " + dir +
                                       " — run the Scene sample script";
        } else if (reload_texture(found, model.snes_shot_ref)) {
            model.snes_shot_ref_info = fs::path(found).filename().string();
        } else {
            model.snes_shot_ref_info = "could not read " + found;
        }
    }

    if (model.snes_shot_pending) {
        std::string reply;
        if (dbg.take_reply("shot", reply)) {
            model.snes_shot_pending = false;
            const std::string path =
                (fs::path(scratch_dir(model, root)) / "ours.bmp").string();
            std::error_code ec;
            if (fs::is_regular_file(path, ec) && reload_texture(path, model.snes_shot))
                model.snes_shot_info = "frame " + std::to_string(snap.frame);
            else
                model.snes_shot_info = "runtime answered but no image was written";
        }
    }

    ImGui::Separator();
    const float avail = ImGui::GetContentRegionAvail().x;
    const float w = std::max(160.f, (avail - 24.f) * 0.5f);
    const auto show = [&](FrameLayer& img, const std::string& info, const char* who) {
        ImGui::BeginGroup();
        ImGui::TextUnformatted(who);
        if (img.tex && img.tw > 0) {
            const float h = w * static_cast<float>(img.th) / static_cast<float>(img.tw);
            ImGui::Image(tex_id(img.tex), ImVec2(w, h));
        } else {
            ImGui::Dummy(ImVec2(w, w * 0.875f));
        }
        if (!info.empty()) muted(th, "%s", info.c_str());
        ImGui::EndGroup();
    };
    show(model.snes_shot, model.snes_shot_info, "ours");
    ImGui::SameLine();
    show(model.snes_shot_ref, model.snes_shot_ref_info, "oracle");
}

void draw_raster_pane(StudioModel& model, const Theme& th) {
    ImGui::TextWrapped(
        "snesrecomp records register writes as (frame, addr, value) with no beam "
        "position, so it can say WHAT a game wrote but never at which scanline. "
        "For a title that raster-splits the screen that is the whole question, "
        "and only the oracle can answer it.");
    ImGui::Spacing();

    left_label("CSV", 90.f);
    ImGui::SetNextItemWidth(-140.f);
    static char path_buf[512];
    if (model.snes_raster_path.empty()) {
        const std::string guess =
            (fs::path(model.snes_lua_out) / "mesen_raster_trace.lua.out").string();
        std::snprintf(path_buf, sizeof(path_buf), "%s", guess.c_str());
    } else if (path_buf[0] == '\0') {
        std::snprintf(path_buf, sizeof(path_buf), "%s", model.snes_raster_path.c_str());
    }
    ImGui::InputText("##raster_path", path_buf, sizeof(path_buf));
    ImGui::SameLine();
    if (accent_button(th, "Load")) {
        model.snes_raster_path = path_buf;
        model.snes_raster_error.clear();
        std::string err;
        if (!load_raster_csv(model.snes_raster, model.snes_raster_path, &err))
            model.snes_raster_error = err;
        else if (!err.empty())
            model.snes_raster_error = err;
    }
    if (!model.snes_raster_error.empty())
        wrapped(th.warn, "%s", model.snes_raster_error.c_str());

    if (model.snes_raster.empty()) {
        muted(th, "Run the Raster trace script above (set \"At frame\" to the frame "
                  "you want), then Load.");
        return;
    }
    ImGui::Text("%d register writes", static_cast<int>(model.snes_raster.size()));
    if (ImGui::BeginTable("##raster", 5,
                          ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_ScrollY | ImGuiTableFlags_SizingStretchProp,
                          ImVec2(0, 320))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("frame");
        ImGui::TableSetupColumn("scanline");
        ImGui::TableSetupColumn("hclock");
        ImGui::TableSetupColumn("register");
        ImGui::TableSetupColumn("value");
        ImGui::TableHeadersRow();
        ImGuiListClipper clip;
        clip.Begin(static_cast<int>(model.snes_raster.size()));
        while (clip.Step()) {
            for (int i = clip.DisplayStart; i < clip.DisplayEnd; ++i) {
                const RasterRow& r = model.snes_raster[i];
                ImGui::TableNextRow();
                ImGui::TableNextColumn(); ImGui::Text("%d", r.frame);
                ImGui::TableNextColumn();
                // A write inside the visible window (0..224) is a mid-frame
                // change: that is a raster split, not a setup write.
                if (r.scanline > 0 && r.scanline < 225)
                    ImGui::TextColored(th.warn, "%d", r.scanline);
                else
                    ImGui::Text("%d", r.scanline);
                ImGui::TableNextColumn(); ImGui::Text("%d", r.hclock);
                ImGui::TableNextColumn();
                ImGui::Text("$%s %s", r.addr.c_str(), r.reg.c_str());
                ImGui::TableNextColumn(); ImGui::Text("%s", r.value.c_str());
            }
        }
        ImGui::EndTable();
    }
    muted(th, "Highlighted scanlines are inside the visible window — those are "
              "mid-frame writes, i.e. an actual split.");
}

void draw_apu_pane(StudioModel& model, const Theme& th) {
    SnesDebugClient& dbg = snes_debug_client();
    const auto snap = dbg.snapshot();
    const bool live = snap.state == SnesDebugClient::State::Connected;

    ImGui::TextWrapped(
        "A host that never opened an audio device looks identical to a working "
        "one from in here: the APU still runs and still produces samples, they "
        "just reach nothing. State below is necessary but NOT sufficient — the "
        "only proof is a recording of the output device.");
    ImGui::Spacing();

    if (accent_button(th, "Read APU state", live)) {
        dbg.request("get_apu_state", "apu");
        model.snes_apu_pending = true;
    }
    if (model.snes_apu_pending) {
        std::string reply;
        if (dbg.take_reply("apu", reply)) {
            model.snes_apu_pending = false;
            model.snes_apu = reply;
        }
    }
    if (model.snes_apu.empty()) {
        muted(th, "not read yet.");
    } else {
        ImGui::Separator();
        ImGui::TextWrapped("%s", model.snes_apu.c_str());
    }
    ImGui::Separator();
    ImGui::TextUnformatted("Proving sound actually plays");
    muted(th, "Stop every other emulator first, or you will attribute its audio "
              "to yours:");
    ImGui::TextWrapped(
        "  pactl list sink-inputs | grep application.name\n"
        "  parec --device=$(pactl get-default-sink).monitor \\\n"
        "        --format=s16le --rate=48000 --channels=2 --raw > cap.raw");
}

void draw_loops_pane(StudioModel& model, const Theme& th, const std::string& root) {
    const std::string tool = snes_tool_path(kLoopTool);
    if (tool.empty()) {
        ImGui::TextColored(th.bad, "tools/snes_analysis/%s not found beside the toolkit.",
                           kLoopTool);
        return;
    }
    const auto snap = snes_debug_client().snapshot();
    const bool live = snap.state == SnesDebugClient::State::Connected;

    ImGui::TextWrapped(
        "For a scene that renders right the FIRST time the attract loop reaches "
        "it and wrong every time after. A single capture cannot see that — both "
        "look like \"the demo\", and whichever you took is the one you believe. "
        "This catches the scene on each visit and diffs the visits.");
    ImGui::Spacing();

    left_label("Scene", 90.f);
    ImGui::SetNextItemWidth(70.f);
    ImGui::InputText("##loopsel", model.snes_loop_sel, sizeof(model.snes_loop_sel));
    ImGui::SameLine();
    muted(th, "= WRAM selector (hex)");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(60.f);
    ImGui::InputInt("##loopselval", &model.snes_loop_selval, 0, 0);
    ImGui::SameLine();
    muted(th, "value marking the scene");

    muted(th, "The runtime accepts ONE debug client. Studio drops its own "
              "connection while this runs and picks it back up after — so the "
              "frame counter above will go quiet for the duration.");
    ImGui::Spacing();

    left_label("Visits", 90.f);
    ImGui::SetNextItemWidth(70.f);
    ImGui::InputInt("##loopvisits", &model.snes_loop_visits, 0, 0);
    if (model.snes_loop_visits < 2) model.snes_loop_visits = 2;
    ImGui::SameLine();
    muted(th, "· one attract loop each — allow ~1 min per visit");

    const std::string outdir = (fs::path(root) / "analysis" / "diagnostics").string();
    left_label("", 90.f);
    if (accent_button(th, "Run comparison", live && !model.busy.load())) {
        // Release Studio's own connection for the duration. The runtime's
        // debug server holds exactly ONE client — accept() closes the previous
        // socket — so Studio's frame poll and the tool would evict each other
        // in a loop, and the tool dies on its first command. Measured exactly
        // that: "debug server closed the connection" at the first read_ram.
        // Reconnected in the callback, so the tab is live again when it ends.
        const std::string host = model.snes_host;
        const int port = model.snes_port;
        snes_debug_client().stop();
        model.snes_status = "loop comparison running — Studio's own connection "
                            "is released until it finishes";
        run_python_script_async(
            model, tool,
            {"--host", host,
             "--port", std::to_string(port),
             "--visits", std::to_string(model.snes_loop_visits),
             "--sel-addr", model.snes_loop_sel,
             "--sel-value", std::to_string(model.snes_loop_selval),
             "--out", outdir},
            [&model, outdir, host, port](RunResult r) {
                load_loop_compare(model.snes_loops,
                                  (fs::path(outdir) / "loop_compare.json").string());
                model.snes_status = r.ok() ? "loop comparison done"
                                           : ("comparison failed: " + tool_error(r));
                snes_debug_client().start(host, port);
            },
            true, JobSlot::Frames);
    }
    ImGui::SameLine();
    // Deliberately usable WHILE a run is in flight: the tool writes the file
    // after every visit, so the first loop's numbers are readable long before
    // the second loop comes round, and a run abandoned halfway still leaves
    // them behind.
    if (ImGui::Button("Load result (works mid-run)")) {
        load_loop_compare(model.snes_loops,
                          (fs::path(outdir) / "loop_compare.json").string());
    }
    if (!live) {
        ImGui::SameLine();
        muted(th, "connect to a running runtime first");
    }
    muted(th, "Each visit costs one pass of the attract loop (~1 min). Progress "
              "streams to the Activity log below, and the result file is "
              "rewritten after EVERY visit — press Load to read the first loop "
              "without waiting for the second.");

    const LoopCompare& lc = model.snes_loops;
    if (!lc.loaded) {
        ImGui::Separator();
        muted(th, "%s", lc.error.empty() ? "no result loaded yet." : lc.error.c_str());
        return;
    }

    ImGui::Separator();
    muted(th, "scene selector %s · %d visit(s)", lc.selector.c_str(),
          (int)lc.visits.size());

    // Differences first: this is the answer, the tables below are the working.
    ImGui::SeparatorText("What changed between visits");
    if (lc.diffs.empty()) {
        ImGui::TextColored(th.good, "nothing differed — the visits are identical.");
        muted(th, "If the picture still differs, the cause is outside what this "
                  "captures; widen the battery rather than trusting this as a "
                  "clean bill.");
    } else if (ImGui::BeginTable("##loopdiff", 3,
                                 ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                                     ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("field");
        ImGui::TableSetupColumn("first visit");
        ImGui::TableSetupColumn("later visit");
        ImGui::TableHeadersRow();
        for (const auto& d : lc.diffs) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::TextColored(th.warn, "%s", d.field.c_str());
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(d.visit1.c_str());
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(d.visit2.c_str());
        }
        ImGui::EndTable();
    }

    ImGui::SeparatorText("Per visit");
    for (const auto& v : lc.visits) {
        ImGui::PushID(v.visit);
        if (ImGui::CollapsingHeader(
                ("visit " + std::to_string(v.visit) + "  ·  entered frame " +
                 std::to_string((unsigned long long)v.entered_frame)).c_str(),
                v.visit == 1 ? ImGuiTreeNodeFlags_DefaultOpen : 0)) {
            ImGui::Text("IRQ chain over frames %d-%d:", v.frame_lo, v.frame_hi);
            for (const auto& kv : v.irq_chain) {
                ImGui::SameLine();
                if (kv.second == 0)
                    wrapped(v.chain_trustworthy ? th.bad : th.text_muted,
                            " %s=0", kv.first.c_str());
                else
                    ImGui::Text(" %s=%d", kv.first.c_str(), kv.second);
            }
            // A dead ring reports zero exactly like a chain that never ran.
            // One of those is a finding, the other a bug in the tooling, and
            // presenting them identically is how a tool invents evidence.
            if (!v.chain_trustworthy)
                wrapped(th.warn,
                        "block-trace ring is EMPTY (%d entries) — those zeros "
                        "are an unarmed instrument, not a measurement.",
                        v.block_ring_entries);
            ImGui::Text("CGRAM: %d distinct, most repeated %s x%d%s",
                        v.cgram_distinct, v.cgram_top_value.c_str(),
                        v.cgram_top_count, v.cgram_fill ? "  <- fill" : "");
            ImGui::Text("shadow $0900: %d distinct, most repeated %s x%d",
                        v.shadow_distinct, v.shadow_top_value.c_str(),
                        v.shadow_top_count);
            if (!v.top_present_in_shadow && v.cgram_fill)
                wrapped(th.bad,
                    "CGRAM's repeated colour is NOT in its source page — the "
                    "upload stopped, the source is fine.");
            ImGui::Text("frames that uploaded CGRAM: %d", v.upload_frames);
            if (!v.reg_writers.empty()) {
                ImGui::TextUnformatted("register writers:");
                for (const auto& w : v.reg_writers)
                    ImGui::BulletText("%s", w.c_str());
            }
        }
        ImGui::PopID();
    }
}

void draw_oracle_pane(StudioModel& model, const Theme& th) {
    const std::string tool = snes_tool_path(kOracleTool);
    if (tool.empty()) {
        wrapped(th.bad, "tools/snes_analysis/%s not found beside the toolkit.",
                kOracleTool);
        return;
    }
    ImGui::TextWrapped(
        "Mesen2 is the SNES oracle: a second, independent implementation with a "
        "debugger and a Lua API. It is consulted, never vendored into a port — "
        "the same rule the licensing notes apply to every reference emulator.");
    ImGui::Spacing();

    left_label("Status", 90.f);
    if (model.snes_mesen.probed) {
        if (model.snes_mesen.installed)
            wrapped(th.good, "%s", model.snes_mesen.summary.c_str());
        else
            wrapped(th.warn, "%s", model.snes_mesen.summary.c_str());
    } else {
        muted(th, model.snes_mesen_probing ? "checking…" : "not checked yet");
    }
    ImGui::SameLine();
    if (ImGui::SmallButton("Refresh##mesen")) refresh_mesen(model);

    left_label("Provider", 90.f);
    ImGui::SetNextItemWidth(200.f);
    ImGui::Combo("##mesen_provider", &model.snes_mesen_provider,
                 "release (pinned download)\0system (distro package)\0");
    ImGui::SameLine();
    muted(th, "· the pinned release is a July-2025 Ubuntu build; on a rolling "
              "distro it can abort in static init, and `system` is the way out");

    left_label("", 90.f);
    const bool busy = model.busy.load();
    if (accent_button(th, "Doctor", !busy)) {
        run_python_script_async(
            model, tool, {"doctor"},
            [&model](RunResult r) {
                model.snes_status = r.ok() ? "doctor: prerequisites ok"
                                           : ("doctor: " + tool_error(r));
            },
            true, JobSlot::Global);
    }
    ImGui::SameLine();
    if (accent_button(th, "Setup", !busy)) {
        run_python_script_async(
            model, tool,
            {"setup", "--provider",
             model.snes_mesen_provider == 1 ? "system" : "release"},
            [&model](RunResult r) {
                model.snes_status = r.ok() ? "Mesen installed"
                                           : ("setup failed: " + tool_error(r));
                model.snes_mesen.probed = false;
                refresh_mesen(model);
            },
            true, JobSlot::Global);
    }
    ImGui::SameLine();
    if (ImGui::Button("Where is it")) {
        run_python_script_async(
            model, tool, {"path", "--binary"},
            [&model](RunResult r) { model.append_log(r.stdout_text); },
            true, JobSlot::Global);
    }

    if (!model.snes_mesen.error.empty()) {
        left_label("", 90.f);
        wrapped(th.bad, "%s", model.snes_mesen.error.c_str());
    }

    ImGui::Separator();
    ImGui::TextUnformatted("Sprite diff against the oracle");
    {
        const std::string sd = snes_tool_path(kSpriteDiffTool);
        if (sd.empty()) {
            wrapped(th.bad, "tools/snes_analysis/%s not found beside the toolkit.",
                    kSpriteDiffTool);
        } else {
            ImGui::TextWrapped(
                "For \"a sprite is wrong and I cannot say why\". Captures the "
                "live runtime, gates on both being on the SAME screen, aligns "
                "on pixels rather than frame numbers, and for each differing "
                "frame says whether a sprite is MISSING from OAM (never "
                "emitted) or MISPLACED (emitted at the wrong coordinates).");
            ImGui::Spacing();
            ImGui::TextWrapped(
                "1. Drive Mesen by hand to the screen, capturing itself:");
            ImGui::TextUnformatted(
                "   GW_WATCH=0012=14 GW_COUNT=200 GW_OAM=1 GW_DIR=<dir> \\");
            ImGui::TextUnformatted(
                "     mesen-ce <rom> tools/snes_analysis/mesen_frames.lua");
            ImGui::TextWrapped(
                "2. Put snesrecomp on the same screen, then run:");
            ImGui::Text("   python3 %s --oracle <dir> --count 300", sd.c_str());
            ImGui::Spacing();
            wrapped(th.text_muted,
                "GW_WATCH is a WRAM marker, ADDR=VALUE in hex; 0012=14 is "
                "Gundam Wing's pre-fight screen. Capture more than one full "
                "animation cycle: a defect sitting at one phase of a long loop "
                "is invisible to a short sample AND to a periodicity check.");
        }
    }

    ImGui::Separator();
    ImGui::TextUnformatted("Before a Lua script can write anything");
    ImGui::TextWrapped(
        "Set Debug > Script Window > AllowIoOsAccess = true, and set it while "
        "Mesen is NOT running — it rewrites settings.json on exit and will "
        "otherwise clobber the change. Mesen is also single-instance: a second "
        "launch is swallowed by the first, which looks exactly like a script "
        "that never loaded.");
}

} // namespace

// ---- the tab ---------------------------------------------------------------

void draw_snes(StudioModel& model, const Theme& th, SDL_Window* /*window*/) {
    const std::string root = model.selected_root();
    if (root.empty()) {
        muted(th, "Select a game repo above.");
        return;
    }
    sync_dirs(model, root);
    if (!model.snes_mesen.probed && !model.snes_mesen_probing) refresh_mesen(model);

    const std::string bdir = build_path(model, root);
    model.snes_rom = snes_rom_for(bdir);

    // ---- runtime connection -------------------------------------------------
    SnesDebugClient& dbg = snes_debug_client();
    const auto snap = dbg.snapshot();

    left_label("Runtime", 90.f);
    ImGui::SetNextItemWidth(130.f);
    ImGui::InputText("##snes_host", model.snes_host, sizeof(model.snes_host));
    ImGui::SameLine();
    ImGui::SetNextItemWidth(80.f);
    ImGui::InputInt("##snes_port", &model.snes_port, 0, 0);
    ImGui::SameLine();
    if (!dbg.running()) {
        if (accent_button(th, "Connect")) dbg.start(model.snes_host, model.snes_port);
    } else {
        if (ImGui::Button("Disconnect")) dbg.stop();
    }
    ImGui::SameLine();
    switch (snap.state) {
    case SnesDebugClient::State::Connected:
        ImGui::TextColored(th.good, "connected — frame %llu",
                           static_cast<unsigned long long>(snap.frame));
        break;
    case SnesDebugClient::State::Connecting:
        wrapped(th.warn, "%s",
                snap.error.empty() ? "connecting…" : snap.error.c_str());
        break;
    case SnesDebugClient::State::Failed:
        wrapped(th.bad, "%s", snap.error.c_str());
        break;
    default:
        muted(th, "not connected");
        break;
    }
    const bool live = snap.state == SnesDebugClient::State::Connected;

    // "Not connected" is a useless diagnosis on its own. The overwhelmingly
    // common cause is a build with SNESRECOMP_ENABLE_TRACE off, which opens no
    // port at all and which no amount of retrying will fix.
    if (!live) {
        const auto probe = probe_debug_tools(root, model.build_dir);
        left_label("", 90.f);
        if (!probe.configured) {
            muted(th, "%s", probe.summary.c_str());
        } else if (probe.enabled) {
            ImGui::TextColored(th.text_muted, "%s — start the game, then Connect.",
                               probe.summary.c_str());
        } else {
            wrapped(th.warn, "%s", probe.summary.c_str());
            left_label("", 90.f);
            wrapped(th.warn,
                    "Build tab -> Debug tools -> turn the TCP debug server ON, "
                    "then Configure + Build.");
        }
    }

    // ---- the game -----------------------------------------------------------
    // Launched from here rather than the Build tab on purpose: Build's Launch
    // holds a job slot for as long as the game runs, which disables every
    // control that only works WHILE a game is running.
    {
        const bool running =
            model.snes_launch_pid > 0 && process_alive(model.snes_launch_pid);
        if (!running && model.snes_launch_pid > 0) model.snes_launch_pid = 0;

        left_label("Game", 90.f);
        const std::string exe = selected_game_exe(model, root);
        if (running) {
            ImGui::TextColored(th.good, "running (pid %ld)", model.snes_launch_pid);
            ImGui::SameLine();
            if (ImGui::Button("Stop game")) {
                process_stop(model.snes_launch_pid);
                model.snes_launch_pid = 0;
            }
        } else if (exe.empty()) {
            wrapped(th.warn, "no built game in %s — build it first",
                               model.build_dir);
        } else {
            muted(th, "not running");
            ImGui::SameLine();
            if (accent_button(th, "Launch game")) {
                model.snes_launch_log = (fs::path(root) / "snes-diag.log").string();
                // Flags first, then the ROM: mmx23_host_main.inc accepts
                // --launcher only as argv[1], and MetalWarriors stops scanning
                // flags at the first non-flag argument. ROM-first hid the flag
                // from both and they booted straight past the launcher.
                std::vector<std::string> args = {"--launcher"};
                const std::string rom = effective_rom(model);
                if (!rom.empty()) args.push_back(rom);
                std::vector<std::pair<std::string, std::string>> env = {
                    {"SNESRECOMP_DEBUG_PORT", std::to_string(model.snes_port)},
                };
                std::string err;
                const long pid = spawn_detached_logged(exe, args, root,
                                                       model.snes_launch_log, env, &err);
                model.snes_launch_pid = pid;
                model.snes_launch_error = pid ? "" : err;
                if (pid) model.snes_status = "launched — press Connect once it is up";
            }
        }
        ImGui::SameLine();
        muted(th, " · log: %s", model.snes_launch_log.empty()
                                    ? (fs::path(root) / "snes-diag.log").string().c_str()
                                    : model.snes_launch_log.c_str());
        if (!model.snes_launch_error.empty()) {
            left_label("", 90.f);
            wrapped(th.bad, "launch failed: %s", model.snes_launch_error.c_str());
        }
    }

    // ---- the ROM ------------------------------------------------------------
    // Both processes must boot the SAME image or the comparison is theatre.
    left_label("ROM", 90.f);
    if (!model.snes_rom.empty()) {
        wrapped(th.good, "%s", model.snes_rom.c_str());
        ImGui::SameLine();
        muted(th, " · from %s/rom.cfg", model.build_dir);
    } else {
        ImGui::SetNextItemWidth(-100.f);
        ImGui::InputTextWithHint("##snes_rom", "path to the .sfc the build runs",
                                 model.snes_rom_override, sizeof(model.snes_rom_override));
        ImGui::SameLine();
        muted(th, "no rom.cfg yet");
    }

    // ---- the oracle ---------------------------------------------------------
    {
        const bool running =
            model.snes_mesen_pid > 0 && process_alive(model.snes_mesen_pid);
        if (!running && model.snes_mesen_pid > 0) {
            // Mesen exiting on its own is the interesting case, not the boring
            // one: the pinned release is a July-2025 Ubuntu build and aborts in
            // static init on a rolling distro. Without this the tab said "not
            // running" with no error, which is indistinguishable from a button
            // that did nothing.
            model.snes_mesen_pid = 0;
            const std::string dying = tail_of_file(model.snes_mesen_log, 3);
            if (!dying.empty()) {
                model.snes_mesen_error = "Mesen exited immediately: " + dying;
                if (dying.find("bad_cast") != std::string::npos ||
                    dying.find("terminate called") != std::string::npos) {
                    model.snes_mesen_error +=
                        "  —  this is the pinned release binary aborting in "
                        "static init. Switch Provider to \"system\" and re-run "
                        "Setup in the Oracle pane.";
                }
            }
        }
        model.snes_mesen_was_running = running;

        left_label("Oracle", 90.f);
        if (running) {
            ImGui::TextColored(th.good, "Mesen running (pid %ld)", model.snes_mesen_pid);
            ImGui::SameLine();
            if (ImGui::Button("Stop oracle")) {
                process_stop(model.snes_mesen_pid);
                model.snes_mesen_pid = 0;
            }
        } else if (!model.snes_mesen.installed) {
            wrapped(th.warn, "%s",
                    model.snes_mesen.summary.empty()
                                   ? "checking Mesen…"
                                   : model.snes_mesen.summary.c_str());
        } else {
            muted(th, "not running");
            ImGui::SameLine();
            if (accent_button(th, "Launch oracle")) launch_mesen(model, root);
        }
        if (!model.snes_mesen_error.empty()) {
            left_label("", 90.f);
            wrapped(th.bad, "%s", model.snes_mesen_error.c_str());
        }

        left_label("Probe", 90.f);
        ImGui::SetNextItemWidth(200.f);
        std::string items;
        for (int i = 0; i < kScriptCount; ++i) {
            items += kScripts[i].label;
            items.push_back('\0');
        }
        items.push_back('\0');
        ImGui::Combo("##snes_lua", &model.snes_mesen_script, items.c_str());
        ImGui::SameLine();
        ImGui::SetNextItemWidth(90.f);
        ImGui::InputInt("at frame##lua", &model.snes_lua_frame, 0, 0);
        ImGui::SameLine();
        ImGui::SetNextItemWidth(70.f);
        ImGui::InputInt("span##lua", &model.snes_lua_span, 0, 0);
        left_label("", 90.f);
        muted(th, "%s", kScripts[std::clamp(model.snes_mesen_script, 0,
                                            kScriptCount - 1)].what);
        left_label("Output", 90.f);
        ImGui::SetNextItemWidth(-8.f);
        ImGui::InputText("##snes_luaout", model.snes_lua_out, sizeof(model.snes_lua_out));
    }

    // ---- what to do next ----------------------------------------------------
    {
        const char* step = nullptr;
        if (snes_tool_path(kCaptureTool).empty())
            step = "tools/snes_analysis is missing beside the toolkit — that is "
                   "where every tool on this tab lives.";
        else if (!live)
            step = (model.snes_launch_pid > 0)
                       ? "1 / 4  ·  The game is running — press Connect."
                       : "1 / 4  ·  Launch the game, then Connect. A build with "
                         "the TCP debug server off opens no port at all.";
        else if (!model.snes_mesen.installed)
            step = "2 / 4  ·  Install Mesen in the Oracle pane. Without a second "
                   "implementation there is nothing to compare against, and a "
                   "wrong-looking frame stays an opinion.";
        else if (model.snes_mesen_pid <= 0)
            step = "3 / 4  ·  Launch the oracle on the same ROM. Do NOT pause "
                   "either side to line them up — sample both and match by "
                   "scene.";
        else
            step = "4 / 4  ·  Take a screenshot of each in the Screen pane and "
                   "confirm they are on the same scene BEFORE trusting any "
                   "palette or register diff.";
        left_label("Next", 90.f);
        ImGui::PushStyleColor(ImGuiCol_Text, th.accent);
        ImGui::TextWrapped("%s", step);
        ImGui::PopStyleColor();
    }

    if (!model.snes_status.empty()) {
        left_label("Status", 90.f);
        muted(th, "%s", model.snes_status.c_str());
        ImGui::SameLine();
        if (ImGui::SmallButton("clear##snes_status")) model.snes_status.clear();
    }

    ImGui::Separator();

    // ---- panes --------------------------------------------------------------
    const int want = model.snes_pane;
    model.snes_pane = -1;
    const auto sel_if = [&](int pane) -> ImGuiTabItemFlags {
        return want == pane ? ImGuiTabItemFlags_SetSelected : 0;
    };

    if (ImGui::BeginTabBar("##snes_panes")) {
        if (ImGui::BeginTabItem("Capture", nullptr, sel_if(0))) {
            draw_capture_pane(model, th, root);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Palette", nullptr, sel_if(1))) {
            draw_palette_pane(model, th);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Screen", nullptr, sel_if(2))) {
            draw_screen_pane(model, th, root);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Raster", nullptr, sel_if(3))) {
            draw_raster_pane(model, th);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("APU", nullptr, sel_if(4))) {
            draw_apu_pane(model, th);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Loops", nullptr, sel_if(6))) {
            draw_loops_pane(model, th, root);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Oracle", nullptr, sel_if(5))) {
            draw_oracle_pane(model, th);
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }
}

} // namespace retcomm::studio
