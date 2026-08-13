#include "studio/studio_model.hpp"
#include "studio/studio_runner.hpp"
#include "studio/studio_theme.hpp"

#include <nlohmann/json.hpp>

#include "imgui.h"
#include "imgui_impl_opengl3.h"
#include "imgui_impl_sdl3.h"
#if defined(IMGUI_ENABLE_FREETYPE)
#include "misc/freetype/imgui_freetype.h"
#endif

#include <SDL3/SDL.h>
#include <SDL3/SDL_dialog.h>
#include <SDL3/SDL_opengl.h>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

#define STB_IMAGE_IMPLEMENTATION
#include "stb_image.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using retcomm::studio::RunResult;
using retcomm::studio::StudioModel;
using retcomm::studio::Theme;

namespace {

fs::path find_asset_file(const char* kind, const char* filename, const fs::path& exe_dir) {
    std::error_code ec;
    auto try_file = [&](const fs::path& p) -> fs::path {
        if (p.empty()) return {};
        const fs::path c = fs::weakly_canonical(p, ec);
        const fs::path& use = (!ec && !c.empty()) ? c : p;
        if (fs::is_regular_file(use, ec)) return use;
        return {};
    };
    std::vector<fs::path> dirs;
    if (const char* env = std::getenv("RETCOMM_STUDIO_ASSETS")) {
        dirs.push_back(fs::path(env));
        dirs.push_back(fs::path(env) / kind);
    }
    if (const char* appdir = std::getenv("APPDIR")) {
        const fs::path ad(appdir);
        dirs.push_back(ad / "usr" / "share" / "retcomm-studio" / kind);
        dirs.push_back(ad / "usr" / "share" / "retcomm-studio" / "assets");
        dirs.push_back(ad / "usr" / "bin" / kind);
    }
    if (const char* base = SDL_GetBasePath()) {
        fs::path b(base);
        dirs.push_back(b / kind);
        dirs.push_back(b / ".." / "share" / "retcomm-studio" / kind);
        dirs.push_back(b / ".." / "share" / "retcomm-studio" / "assets");
        fs::path walk = b;
        for (int i = 0; i < 6 && !walk.empty(); ++i) {
            dirs.push_back(walk / "assets" / kind);
            dirs.push_back(walk / "assets");
            dirs.push_back(walk / kind);
            walk = walk.parent_path();
        }
    }
    if (!exe_dir.empty()) {
        dirs.push_back(exe_dir / kind);
        dirs.push_back(exe_dir / "assets" / kind);
        dirs.push_back(exe_dir / ".." / "assets" / kind);
    }
    for (const auto& d : dirs) {
        if (auto hit = try_file(d / filename); !hit.empty()) return hit;
        if (std::strcmp(kind, "fonts") == 0) {
            if (auto hit = try_file(d / filename); !hit.empty()) return hit;
        }
    }
    return {};
}

void load_fonts(const fs::path& exe_dir) {
    ImGuiIO& io = ImGui::GetIO();
#if defined(IMGUI_ENABLE_FREETYPE)
    io.Fonts->FontBuilderFlags |= ImGuiFreeTypeBuilderFlags_LoadColor;
#endif
    const fs::path regular = find_asset_file("fonts", "LatoLatin-Regular.ttf", exe_dir);
    ImFontConfig cfg;
    cfg.OversampleH = 2;
    cfg.OversampleV = 2;
    static const ImWchar kRanges[] = {0x0020, 0x00FF, 0x2010, 0x2027, 0};
    constexpr float kBody = 17.0f;
    bool loaded = false;
    if (!regular.empty()) {
        loaded = io.Fonts->AddFontFromFileTTF(regular.string().c_str(), kBody, &cfg, kRanges) !=
                 nullptr;
    }
    if (!loaded) {
        cfg.SizePixels = kBody;
        io.Fonts->AddFontDefault(&cfg);
    }
}

void accent_button(const Theme& th) {
    ImGui::PushStyleColor(ImGuiCol_Button, th.accent_button);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, th.accent_button_hovered);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, th.accent_button_active);
}

void accent_button_pop() { ImGui::PopStyleColor(3); }

// Layout helpers — ImGui default labels sit to the RIGHT of widgets, which
// overflows when SameLine chains follow. Keep labels on the left and size
// fields from remaining content width.
const char* imgui_visible_label(const char* label, std::string* scratch) {
    scratch->assign(label ? label : "");
    const auto hash = scratch->find("##");
    if (hash != std::string::npos) scratch->resize(hash);
    return scratch->c_str();
}

float widget_label_width(const char* label) {
    std::string scratch;
    const char* vis = imgui_visible_label(label, &scratch);
    return ImGui::CalcTextSize(vis).x + ImGui::GetStyle().FramePadding.x * 2.f;
}

float trailing_checkbox_width(const char* label) {
    std::string scratch;
    const char* vis = imgui_visible_label(label, &scratch);
    const ImGuiStyle& s = ImGui::GetStyle();
    return ImGui::GetFrameHeight() + s.ItemInnerSpacing.x + ImGui::CalcTextSize(vis).x +
           s.ItemSpacing.x;
}

void left_label(const char* label, float col_w) {
    ImGui::AlignTextToFramePadding();
    const float x0 = ImGui::GetCursorPosX();
    ImGui::TextUnformatted(label);
    ImGui::SameLine(0.f, 0.f);
    const float pad = col_w - (ImGui::GetCursorPosX() - x0);
    ImGui::Dummy(ImVec2(pad > 0.f ? pad : ImGui::GetStyle().ItemSpacing.x, 0.f));
    ImGui::SameLine();
}

// Returns true when the browse button is clicked (not on text edits).
bool path_row(const char* id, const char* label, char* buf, size_t buf_n, float label_w,
              const char* browse_label) {
    left_label(label, label_w);
    const float browse_w = widget_label_width(browse_label) + ImGui::GetStyle().ItemSpacing.x;
    float field_w = ImGui::GetContentRegionAvail().x - browse_w;
    if (field_w < 80.f) field_w = 80.f;
    ImGui::SetNextItemWidth(field_w);
    ImGui::InputText(id, buf, buf_n);
    ImGui::SameLine();
    return ImGui::Button(browse_label);
}

void field_row(const char* id, const char* label, char* buf, size_t buf_n, float label_w) {
    left_label(label, label_w);
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x);
    ImGui::InputText(id, buf, buf_n);
}

void int_field_row(const char* id, const char* label, int* v, float label_w, float field_w = 100.f) {
    left_label(label, label_w);
    ImGui::SetNextItemWidth(field_w);
    ImGui::InputInt(id, v);
}

// Players 1..8 as a wider combo (replaces InputInt +/-).
bool players_combo(const char* id, int* players, float width = 140.f) {
    if (*players < 1) *players = 1;
    if (*players > 8) *players = 8;
    char preview[8];
    std::snprintf(preview, sizeof(preview), "%d", *players);
    ImGui::SetNextItemWidth(width);
    bool changed = false;
    if (ImGui::BeginCombo(id, preview, ImGuiComboFlags_HeightLarge)) {
        for (int p = 1; p <= 8; ++p) {
            char lab[8];
            std::snprintf(lab, sizeof(lab), "%d", p);
            const bool sel = (*players == p);
            if (ImGui::Selectable(lab, sel)) {
                *players = p;
                changed = true;
            }
            if (sel) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    return changed;
}

// Bulk parallel jobs 1..4 (dropdown; avoids InputInt +/-).
bool jobs_combo(const char* id, int* jobs, float width = 100.f) {
    if (*jobs < 1) *jobs = 1;
    if (*jobs > 4) *jobs = 4;
    char preview[8];
    std::snprintf(preview, sizeof(preview), "%d", *jobs);
    ImGui::SetNextItemWidth(width);
    bool changed = false;
    if (ImGui::BeginCombo(id, preview, ImGuiComboFlags_HeightLarge)) {
        for (int j = 1; j <= 4; ++j) {
            char lab[8];
            std::snprintf(lab, sizeof(lab), "%d", j);
            const bool sel = (*jobs == j);
            if (ImGui::Selectable(lab, sel)) {
                *jobs = j;
                changed = true;
            }
            if (sel) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    return changed;
}

void apply_window_icon(SDL_Window* window, const fs::path& exe_dir) {
    if (!window) return;
    const fs::path png = find_asset_file("", "retcomm-studio.png", exe_dir);
    if (png.empty()) return;
    int w = 0, h = 0, comp = 0;
    stbi_uc* pixels = stbi_load(png.string().c_str(), &w, &h, &comp, 4);
    if (!pixels || w <= 0 || h <= 0) {
        if (pixels) stbi_image_free(pixels);
        return;
    }
    SDL_Surface* surf =
        SDL_CreateSurfaceFrom(w, h, SDL_PIXELFORMAT_RGBA32, pixels, w * 4);
    if (surf) {
        SDL_SetWindowIcon(window, surf);
        SDL_DestroySurface(surf);
    }
    stbi_image_free(pixels);
}

bool branch_combo(const char* id, const char* label, char* buf, size_t buf_n, float label_w,
                  const std::vector<std::string>& items, float width = 220.f) {
    left_label(label, label_w);
    ImGui::SetNextItemWidth(width > 0.f ? width : ImGui::GetContentRegionAvail().x);
    const char* preview = buf[0] ? buf : "(select…)";
    bool changed = false;
    if (ImGui::BeginCombo(id, preview, ImGuiComboFlags_HeightLarge)) {
        bool have_cur = false;
        for (const auto& s : items) {
            if (s == buf) {
                have_cur = true;
                break;
            }
        }
        if (buf[0] && !have_cur) {
            if (ImGui::Selectable(buf, true)) changed = false;
            ImGui::Separator();
        }
        if (items.empty()) {
            ImGui::TextDisabled("(no branches yet — Refresh branches)");
        }
        for (const auto& s : items) {
            const bool sel = (s == buf);
            if (ImGui::Selectable(s.c_str(), sel)) {
                std::snprintf(buf, buf_n, "%s", s.c_str());
                changed = true;
            }
            if (sel) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    return changed;
}

void load_branches_json(StudioModel& model, const std::string& json_text, const std::string& root) {
    try {
        auto j = nlohmann::json::parse(json_text);
        auto fill = [](std::vector<std::string>& dst, const nlohmann::json& arr) {
            dst.clear();
            if (!arr.is_array()) return;
            for (const auto& v : arr) {
                if (v.is_string()) {
                    const std::string s = v.get<std::string>();
                    if (!s.empty()) dst.push_back(s);
                }
            }
        };
        std::lock_guard<std::mutex> lock(model.mu);
        fill(model.branches_game, j["game"]);
        fill(model.branches_psx, j["psxrecomp"]);
        fill(model.branches_ui, j["recomp-ui"]);
        fill(model.branches_net, j["recomp-net"]);
        fill(model.branches_rb, j["rbengine"]);
        model.branches_root = root;
        model.branches_loading = false;
        // Seed empty git_branch from current if blank.
        if (!model.git_branch[0] && !model.branches_game.empty()) {
            std::snprintf(model.git_branch, sizeof(model.git_branch), "%s",
                          model.branches_game.front().c_str());
        }
    } catch (const std::exception& ex) {
        model.branches_loading = false;
        model.append_log(std::string("[FAIL] branches parse: ") + ex.what());
    }
}

void refresh_branches(StudioModel& model, bool fetch = false) {
    if (model.branches_loading || model.busy.load()) return;
    model.branches_loading = true;
    const std::string root = model.selected_root();
    std::vector<std::string> args = {"git", "branches", "--json"};
    if (!root.empty()) {
        args.push_back("--root");
        args.push_back(root);
    }
    if (fetch) args.push_back("--fetch");
    retcomm::studio::run_project_studio_async(
        model, args,
        [&model, root](RunResult r) {
            if (!r.ok() && r.stdout_text.empty()) {
                model.branches_loading = false;
                model.append_log("[FAIL] git branches: " + r.stderr_text);
                return;
            }
            load_branches_json(model, r.stdout_text, root);
            model.set_status(root.empty() ? "Default module branches loaded"
                                          : "Branch lists updated");
        },
        false);
}

// Place a checkbox on the current line, wrapping if it would overflow.
void checkbox_wrapped(const char* label, bool* v) {
    const float need = trailing_checkbox_width(label);
    if (ImGui::GetCursorPosX() > ImGui::GetCursorStartPos().x &&
        ImGui::GetContentRegionAvail().x < need)
        ImGui::NewLine();
    ImGui::Checkbox(label, v);
    ImGui::SameLine();
}

void end_wrapped_line() {
    if (ImGui::GetCursorPosX() > ImGui::GetCursorStartPos().x) ImGui::NewLine();
}

void text_clipped(const char* text, const ImVec4& col) {
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::PushTextWrapPos(0.f); // wrap to window edge
    ImGui::TextUnformatted(text);
    ImGui::PopTextWrapPos();
    ImGui::PopStyleColor();
}

struct DialogCtx {
    StudioModel* model = nullptr;
    const char* target = nullptr;
};

void SDLCALL folder_callback(void* userdata, const char* const* filelist, int /*filter*/) {
    auto* ctx = static_cast<DialogCtx*>(userdata);
    if (!ctx || !ctx->model) return;
    {
        std::lock_guard<std::mutex> lock(ctx->model->pick_mu);
        if (filelist && filelist[0]) {
            ctx->model->pending_folder = filelist[0];
            ctx->model->pending_pick_target = ctx->target ? ctx->target : "";
        }
    }
    delete ctx;
}

void SDLCALL file_callback(void* userdata, const char* const* filelist, int /*filter*/) {
    auto* ctx = static_cast<DialogCtx*>(userdata);
    if (!ctx || !ctx->model) return;
    {
        std::lock_guard<std::mutex> lock(ctx->model->pick_mu);
        ctx->model->file_pick_busy = false;
        if (filelist && filelist[0]) {
            ctx->model->pending_file = filelist[0];
            ctx->model->pending_pick_target = ctx->target ? ctx->target : "";
        }
    }
    delete ctx;
}

void pick_folder(StudioModel& model, SDL_Window* window, const char* target) {
    auto* ctx = new DialogCtx{&model, target};
    SDL_ShowOpenFolderDialog(folder_callback, ctx, window, nullptr, false);
}

void pick_file(StudioModel& model, SDL_Window* window, const char* target, const char* filter_name,
               const char* pattern) {
    {
        std::lock_guard<std::mutex> lock(model.pick_mu);
        if (model.file_pick_busy) return;
        model.file_pick_busy = true;
    }
    auto* ctx = new DialogCtx{&model, target};
    SDL_DialogFileFilter filters[1];
    filters[0].name = filter_name;
    filters[0].pattern = pattern;
    SDL_ShowOpenFileDialog(file_callback, ctx, window, filters, 1, nullptr, false);
}

void begin_export_activity_log(StudioModel& model, SDL_Window* window) {
    if (!window) return;
    {
        std::lock_guard<std::mutex> lock(model.pick_mu);
        if (model.file_pick_busy) return;
        model.file_pick_busy = true;
    }
#if defined(_WIN32)
    const char* home = std::getenv("USERPROFILE");
#else
    const char* home = std::getenv("HOME");
#endif
    static std::string default_loc;
    default_loc = (home && *home) ? (fs::path(home) / "retcomm-studio.log").string()
                                  : std::string("retcomm-studio.log");
    static SDL_DialogFileFilter filters[1];
    filters[0].name = "Log files";
    filters[0].pattern = "log";
    auto* ctx = new DialogCtx{&model, "export_log"};
    SDL_ShowSaveFileDialog(file_callback, ctx, window, filters, 1, default_loc.c_str());
}

void apply_pending_picks(StudioModel& model) {
    std::string folder, file, target;
    {
        std::lock_guard<std::mutex> lock(model.pick_mu);
        folder.swap(model.pending_folder);
        file.swap(model.pending_file);
        target.swap(model.pending_pick_target);
    }
    if (!folder.empty()) {
        if (target == "repo_add") {
            const std::string added = folder;
            retcomm::studio::run_project_studio_async(
                model, {"repos", "add", "--path", added, "--json"},
                [&model, added](RunResult r) {
                    std::string err;
                    if (r.ok() && retcomm::studio::load_repos_from_json(model, r.stdout_text, &err)) {
                        model.set_status("Repo added");
                        model.select_repo_by_path(added);
                    } else {
                        model.append_log("[FAIL] repos add: " + (err.empty() ? r.stderr_text : err));
                    }
                },
                false);
        } else if (target == "np_parent") {
            std::snprintf(model.np_parent, sizeof(model.np_parent), "%s", folder.c_str());
        }
    }
    if (!file.empty()) {
        if (target == "disc") {
            std::snprintf(model.disc_cue, sizeof(model.disc_cue), "%s", file.c_str());
        } else if (target == "np_disc") {
            std::snprintf(model.np_disc, sizeof(model.np_disc), "%s", file.c_str());
        } else if (target == "np_bios") {
            std::snprintf(model.np_bios, sizeof(model.np_bios), "%s", file.c_str());
        } else if (target == "export_log") {
            std::string body;
            {
                std::lock_guard<std::mutex> lock(model.mu);
                for (const auto& line : model.log_lines) {
                    if (!body.empty()) body.push_back('\n');
                    body += line;
                }
            }
            if (body.empty()) body = "(no activity yet)";
            std::ofstream out(file, std::ios::binary | std::ios::trunc);
            if (!out) {
                model.append_log("[FAIL] Export activity log: cannot write " + file);
                model.set_status("Export activity log failed");
            } else {
                out.write(body.data(), static_cast<std::streamsize>(body.size()));
                if (!body.empty() && body.back() != '\n') out.put('\n');
                if (!out) {
                    model.append_log("[FAIL] Export activity log while writing " + file);
                    model.set_status("Export activity log failed");
                } else {
                    model.append_log("[OK] Exported activity log → " + file);
                    model.set_status("Exported activity log");
                }
            }
        }
    }
}

void refresh_repos(StudioModel& model) {
    retcomm::studio::run_project_studio_async(
        model, {"repos", "list", "--json"},
        [&model](RunResult r) {
            std::string err;
            if (!retcomm::studio::load_repos_from_json(model, r.stdout_text, &err)) {
                model.append_log("[FAIL] repos list: " + (err.empty() ? r.stderr_text : err));
                model.set_status("Failed to load repo index");
            } else {
                model.set_status("Repo index loaded");
            }
        },
        false);
}

std::vector<std::string> migrate_common_args(StudioModel& model) {
    std::vector<std::string> a;
    if (model.disc_cue[0]) {
        a.push_back("--disc");
        a.push_back(model.disc_cue);
    }
    a.push_back("--players");
    a.push_back(std::to_string(model.players));
    if (model.zip_prefix[0]) {
        a.push_back("--zip-prefix");
        a.push_back(model.zip_prefix);
    }
    if (model.migrate_netplay) a.push_back("--enable-netplay");
    if (!model.migrate_ci) a.push_back("--no-ci");
    // probe_disc = bool(disc) and not no_probe — pass --no-probe unless user wants probe.
    if (!model.migrate_probe) a.push_back("--no-probe");
    if (model.migrate_force) a.push_back("--force");
    if (model.migrate_dry_run) a.push_back("--dry-run");
    return a;
}

void do_audit_plan(StudioModel& model) {
    const std::string root = model.selected_root();
    if (root.empty()) {
        model.append_log("[FAIL] No game repo selected");
        return;
    }
    std::vector<std::string> audit_args = {"audit", "--root", root, "--json"};
    retcomm::studio::run_project_studio_async(
        model, audit_args,
        [&model, root](RunResult r) {
            std::string err;
            if (!retcomm::studio::load_audit_from_json(model, r.stdout_text, &err)) {
                model.append_log("[FAIL] audit parse: " + (err.empty() ? r.stderr_text : err));
                return;
            }
            model.append_log("Audited " + root + " → " + model.audit_layout);
            model.set_status("Layout: " + model.audit_layout);
            std::vector<std::string> plan_args = {"plan", "--root", root, "--json"};
            auto common = migrate_common_args(model);
            // plan should not force dry-run into options oddly; dry-run is apply-only
            common.erase(std::remove(common.begin(), common.end(), "--dry-run"), common.end());
            plan_args.insert(plan_args.end(), common.begin(), common.end());
            retcomm::studio::run_project_studio_async(
                model, plan_args,
                [&model](RunResult pr) {
                    std::string perr;
                    if (!retcomm::studio::load_plan_from_json(model, pr.stdout_text, &perr)) {
                        model.append_log("[FAIL] plan parse: " +
                                         (perr.empty() ? pr.stderr_text : perr));
                    }
                },
                false);
        },
        false);
}

void do_apply(StudioModel& model) {
    const std::string root = model.selected_root();
    if (root.empty()) return;
    std::vector<std::string> only;
    {
        std::lock_guard<std::mutex> lock(model.mu);
        for (const auto& s : model.plan_steps)
            if (s.selected) only.push_back(s.op_id);
    }
    if (only.empty()) {
        model.append_log("[FAIL] No plan steps selected");
        return;
    }
    std::string only_csv;
    for (size_t i = 0; i < only.size(); ++i) {
        if (i) only_csv += ",";
        only_csv += only[i];
    }
    std::vector<std::string> args = {"apply", "--root", root, "--json", "--only", only_csv};
    auto common = migrate_common_args(model);
    args.insert(args.end(), common.begin(), common.end());
    model.append_log(std::string("--- ") + (model.migrate_dry_run ? "DRY-RUN" : "APPLY") + " (" +
                     std::to_string(only.size()) + " ops) ---");
    retcomm::studio::run_project_studio_async(model, args, [&model](RunResult r) {
        model.set_status(r.ok() ? "Apply finished OK" : "Apply finished with errors");
        if (!model.migrate_dry_run && r.ok()) do_audit_plan(model);
    });
}

void draw_header(StudioModel& model, const Theme& th, SDL_Window* window) {
    constexpr float kLabelW = 88.f;

    // Title row — push Check updates to the right using remaining width.
    ImGui::TextColored(th.accent, "RetComM Studio");
    ImGui::SameLine();
    ImGui::TextDisabled("v%s", model.version.c_str());
    {
        const float bw = widget_label_width("Check updates");
        ImGui::SameLine(0.f, 0.f);
        const float gap = ImGui::GetContentRegionAvail().x - bw;
        if (gap > ImGui::GetStyle().ItemSpacing.x)
            ImGui::Dummy(ImVec2(gap - ImGui::GetStyle().ItemSpacing.x, 0.f));
        ImGui::SameLine();
        ImGui::BeginDisabled(model.busy.load());
        if (ImGui::Button("Check updates")) {
            retcomm::studio::run_project_studio_async(model, {"updates", "check"}, nullptr);
        }
        ImGui::EndDisabled();
    }

    // Repo row — size combo from trailing button/checkbox widths.
    left_label("Game repo", kLabelW);
    {
        const ImGuiStyle& st = ImGui::GetStyle();
        const float trail = widget_label_width("Add…") + st.ItemSpacing.x +
                            widget_label_width("Remove") + st.ItemSpacing.x +
                            trailing_checkbox_width("Catalog only");
        float combo_w = ImGui::GetContentRegionAvail().x - trail;
        if (combo_w < 120.f) combo_w = 120.f;
        ImGui::SetNextItemWidth(combo_w);
        const char* preview = "(add a repo…)";
        if (model.selected_repo >= 0 &&
            model.selected_repo < static_cast<int>(model.repos.size()))
            preview = model.repos[static_cast<size_t>(model.selected_repo)].label.c_str();
        if (ImGui::BeginCombo("##repo", preview)) {
            for (int i = 0; i < static_cast<int>(model.repos.size()); ++i) {
                const bool sel = (i == model.selected_repo);
                if (ImGui::Selectable(model.repos[static_cast<size_t>(i)].label.c_str(), sel)) {
                    model.selected_repo = i;
                    const auto& e = model.repos[static_cast<size_t>(i)];
                    std::snprintf(model.disc_cue, sizeof(model.disc_cue), "%s", e.cue.c_str());
                    retcomm::studio::run_project_studio_async(
                        model, {"repos", "set-last", "--path", e.path, "--json"},
                        [&model](RunResult r) {
                            std::string err;
                            retcomm::studio::load_repos_from_json(model, r.stdout_text, &err);
                        },
                        false);
                    model.append_log("Selected repo: " + e.path);
                    refresh_branches(model, false);
                }
                if (sel) ImGui::SetItemDefaultFocus();
            }
            ImGui::EndCombo();
        }
        ImGui::SameLine();
        if (ImGui::Button("Add…")) pick_folder(model, window, "repo_add");
        ImGui::SameLine();
        if (ImGui::Button("Remove") && model.selected_repo >= 0) {
            const std::string path = model.selected_root();
            retcomm::studio::run_project_studio_async(
                model, {"repos", "remove", "--path", path, "--json"},
                [&model](RunResult r) {
                    std::string err;
                    retcomm::studio::load_repos_from_json(model, r.stdout_text, &err);
                },
                false);
        }
        ImGui::SameLine();
        if (ImGui::Checkbox("Catalog only", &model.catalog_only)) {
            retcomm::studio::run_project_studio_async(
                model,
                {"repos", "set-flags", "--catalog-only", model.catalog_only ? "1" : "0", "--json"},
                [&model](RunResult r) {
                    std::string err;
                    retcomm::studio::load_repos_from_json(model, r.stdout_text, &err);
                },
                false);
        }
    }

    if (model.selected_repo >= 0 && model.selected_repo < static_cast<int>(model.repos.size())) {
        text_clipped(model.repos[static_cast<size_t>(model.selected_repo)].path.c_str(),
                     th.text_muted);
    }
    text_clipped(model.status.c_str(), th.text_muted);
}

void draw_migrate(StudioModel& model, const Theme& th, SDL_Window* window) {
    constexpr float kLabelW = 88.f;
    ImGui::BeginDisabled(model.busy.load() || model.selected_root().empty());

    if (path_row("##disc", "Disc .cue", model.disc_cue, sizeof(model.disc_cue), kLabelW,
                 "Browse…##disc"))
        pick_file(model, window, "disc", "CUE files", "cue");

    // Options rows — Players combo (wider than old +/-) + zip field.
    {
        left_label("Players", kLabelW);
        if (players_combo("##players", &model.players, 140.f)) {
            if (model.players < 2) model.migrate_netplay = false;
        }
        ImGui::SameLine();
        left_label("Zip prefix", 80.f);
        float zw = ImGui::GetContentRegionAvail().x;
        if (zw < 80.f) {
            ImGui::NewLine();
            left_label("Zip prefix", kLabelW);
            zw = ImGui::GetContentRegionAvail().x;
        }
        ImGui::SetNextItemWidth(zw);
        ImGui::InputText("##zip", model.zip_prefix, sizeof(model.zip_prefix));
    }

    checkbox_wrapped("Netplay", &model.migrate_netplay);
    checkbox_wrapped("CI", &model.migrate_ci);
    checkbox_wrapped("Probe disc", &model.migrate_probe);
    checkbox_wrapped("Dry-run", &model.migrate_dry_run);
    checkbox_wrapped("Force", &model.migrate_force);
    end_wrapped_line();

    accent_button(th);
    if (ImGui::Button("Audit + Plan")) do_audit_plan(model);
    accent_button_pop();
    ImGui::SameLine();
    if (ImGui::Button("Apply selected")) do_apply(model);

    ImGui::EndDisabled();

    const float avail_x = ImGui::GetContentRegionAvail().x;
    const float avail_y = ImGui::GetContentRegionAvail().y;
    const bool stack = avail_x < 720.f;
    const float gap = ImGui::GetStyle().ItemSpacing.x;
    const float half_w = stack ? avail_x : (avail_x - gap) * 0.5f;
    const float pane_h = stack ? avail_y * 0.5f - gap * 0.5f : avail_y;

    ImGui::BeginChild("audit", ImVec2(half_w, pane_h), ImGuiChildFlags_Borders);
    ImGui::TextUnformatted("Audit");
    ImGui::Separator();
    if (ImGui::BeginTable("audit_tbl", 3,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_ScrollY |
                              ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("Status", ImGuiTableColumnFlags_WidthFixed, 56.f);
        ImGui::TableSetupColumn("Check", ImGuiTableColumnFlags_WidthStretch, 0.45f);
        ImGui::TableSetupColumn("Detail", ImGuiTableColumnFlags_WidthStretch, 0.55f);
        ImGui::TableHeadersRow();
        ImGuiListClipper clipper;
        clipper.Begin(static_cast<int>(model.audit_checks.size()));
        while (clipper.Step()) {
            for (int i = clipper.DisplayStart; i < clipper.DisplayEnd; ++i) {
                const auto& c = model.audit_checks[static_cast<size_t>(i)];
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                ImVec4 col = th.text_muted;
                if (c.status == "pass") col = th.good;
                else if (c.status == "fail") col = th.bad;
                else if (c.status == "warn") col = th.warn;
                ImGui::TextColored(col, "%s", c.status.c_str());
                ImGui::TableSetColumnIndex(1);
                ImGui::TextWrapped("%s", c.title.c_str());
                ImGui::TableSetColumnIndex(2);
                ImGui::TextWrapped("%s", c.detail.c_str());
            }
        }
        ImGui::EndTable();
    }
    ImGui::EndChild();
    if (!stack) ImGui::SameLine();
    ImGui::BeginChild("plan", ImVec2(stack ? avail_x : 0.f, pane_h), ImGuiChildFlags_Borders);
    ImGui::TextUnformatted("Plan");
    ImGui::Separator();
    if (model.plan_steps.empty()) {
        ImGui::TextColored(th.text_muted, "No migration steps (run Audit + Plan).");
    } else {
        for (auto& s : model.plan_steps) {
            ImGui::PushID(s.op_id.c_str());
            ImGui::Checkbox("##sel", &s.selected);
            ImGui::SameLine();
            ImGui::TextWrapped("%s — %s", s.op_id.c_str(), s.title.c_str());
            if (!s.detail.empty()) {
                ImGui::Indent();
                ImGui::TextColored(th.text_muted, "%s", s.detail.c_str());
                ImGui::Unindent();
            }
            ImGui::PopID();
        }
    }
    ImGui::EndChild();
}

void draw_new_project(StudioModel& model, const Theme& th, SDL_Window* window) {
    constexpr float kLabelW = 110.f;
    // First visit: load default module branch lists (ls-remote).
    if (model.branches_psx.empty() && !model.branches_loading)
        refresh_branches(model, false);
    ImGui::BeginChild("##np_scroll", ImVec2(0, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_None);
    ImGui::BeginDisabled(model.busy.load());
    if (path_row("##np_parent", "Parent folder", model.np_parent, sizeof(model.np_parent),
                 kLabelW, "…##np_parent"))
        pick_folder(model, window, "np_parent");
    field_row("##np_name", "Name", model.np_name, sizeof(model.np_name), kLabelW);
    if (path_row("##np_disc", "Disc .cue", model.np_disc, sizeof(model.np_disc), kLabelW,
                 "…##np_disc"))
        pick_file(model, window, "np_disc", "CUE files", "cue");
    if (path_row("##np_bios", "BIOS", model.np_bios, sizeof(model.np_bios), kLabelW,
                 "…##np_bios"))
        pick_file(model, window, "np_bios", "BIOS", "bin;rom");

    left_label("Players", kLabelW);
    if (players_combo("##np_players", &model.np_players, 140.f)) {
        if (model.np_players >= 2)
            model.np_netplay = true;
        else
            model.np_netplay = false;
    }
    field_row("##np_zip", "Zip prefix", model.np_zip, sizeof(model.np_zip), kLabelW);
    field_row("##np_region", "Region", model.np_region, sizeof(model.np_region), kLabelW);
    field_row("##np_desc", "Description", model.np_desc, sizeof(model.np_desc), kLabelW);
    field_row("##np_pub", "Publisher", model.np_publisher, sizeof(model.np_publisher), kLabelW);
    field_row("##np_year", "Year", model.np_year, sizeof(model.np_year), kLabelW);
    field_row("##np_lobby", "Lobby", model.np_lobby, sizeof(model.np_lobby), kLabelW);

    checkbox_wrapped("recomp-ui", &model.np_ui);
    checkbox_wrapped("Wizard", &model.np_wizard);
    checkbox_wrapped("Netplay##np", &model.np_netplay);
    checkbox_wrapped("CI##np", &model.np_ci);
    checkbox_wrapped("Boxart", &model.np_boxart);
    checkbox_wrapped("Stage", &model.np_stage);
    checkbox_wrapped("Generate", &model.np_generate);
    checkbox_wrapped("Build##np", &model.np_build);
    checkbox_wrapped("GitHub", &model.np_github);
    end_wrapped_line();

    {
        ImGui::BeginDisabled(model.branches_loading || model.busy.load());
        if (ImGui::Button(model.branches_loading ? "Loading branches…" : "Refresh branches##np"))
            refresh_branches(model, false);
        ImGui::EndDisabled();
        ImGui::SameLine();
        ImGui::TextDisabled("Module refs (scrollable)");
    }
    constexpr float kBranchW = 260.f;
    branch_combo("##np_psx", "psxrecomp ref", model.np_psx_ref, sizeof(model.np_psx_ref),
                 kLabelW, model.branches_psx, kBranchW);
    branch_combo("##np_ui", "recomp-ui ref", model.np_ui_ref, sizeof(model.np_ui_ref), kLabelW,
                 model.branches_ui, kBranchW);
    branch_combo("##np_net", "recomp-net ref", model.np_net_ref, sizeof(model.np_net_ref),
                 kLabelW, model.branches_net, kBranchW);
    branch_combo("##np_rb", "rbengine ref", model.np_rb_ref, sizeof(model.np_rb_ref), kLabelW,
                 model.branches_rb, kBranchW);

    if (model.np_disc[0] && ImGui::Button("Autofill meta")) {
        retcomm::studio::run_project_studio_async(
            model, {"lookup-disc-meta", "--disc", model.np_disc, "--json"},
            [&model](RunResult r) {
                try {
                    auto j = nlohmann::json::parse(r.stdout_text);
                    auto set = [&](char* buf, size_t n, const char* key) {
                        if (j.contains(key) && j[key].is_string()) {
                            const std::string v = j[key].get<std::string>();
                            if (!v.empty()) std::snprintf(buf, n, "%s", v.c_str());
                        }
                    };
                    set(model.np_name, sizeof(model.np_name), "name");
                    set(model.np_desc, sizeof(model.np_desc), "description");
                    set(model.np_publisher, sizeof(model.np_publisher), "publisher");
                    set(model.np_year, sizeof(model.np_year), "year");
                    set(model.np_region, sizeof(model.np_region), "region");
                    set(model.np_zip, sizeof(model.np_zip), "zip_prefix");
                    if (j.contains("players") && j["players"].is_number_integer()) {
                        model.np_players = j["players"].get<int>();
                    }
                    model.append_log("Autofill meta OK");
                } catch (const std::exception& ex) {
                    model.append_log(std::string("Autofill failed: ") + ex.what());
                }
            },
            false);
    }
    ImGui::SameLine();
    accent_button(th);
    if (ImGui::Button("Create project")) {
        if (!model.np_name[0] || !model.np_parent[0] || !model.np_disc[0]) {
            model.append_log("[FAIL] Need parent, name, and disc .cue");
        } else {
            std::vector<std::string> args = {
                "new-project",
                "--name",
                model.np_name,
                "--dir",
                model.np_parent,
                "--disc",
                model.np_disc,
                "--players",
                std::to_string(model.np_players),
            };
            if (model.np_bios[0]) {
                args.push_back("--bios");
                args.push_back(model.np_bios);
            }
            if (model.np_zip[0]) {
                args.push_back("--zip-prefix");
                args.push_back(model.np_zip);
            }
            if (model.np_desc[0]) {
                args.push_back("--description");
                args.push_back(model.np_desc);
            }
            if (model.np_publisher[0]) {
                args.push_back("--publisher");
                args.push_back(model.np_publisher);
            }
            if (model.np_year[0]) {
                args.push_back("--year");
                args.push_back(model.np_year);
            }
            if (model.np_region[0]) {
                args.push_back("--region");
                args.push_back(model.np_region);
            }
            if (model.np_lobby[0]) {
                args.push_back("--lobby-url");
                args.push_back(model.np_lobby);
            }
            if (!model.np_ui) args.push_back("--no-recomp-ui");
            if (!model.np_wizard) args.push_back("--no-wizard");
            if (model.np_netplay) args.push_back("--enable-netplay");
            if (!model.np_ci) args.push_back("--no-ci");
            if (!model.np_boxart) args.push_back("--no-fetch-boxart");
            if (model.np_stage) args.push_back("--stage-disc");
            if (model.np_generate) args.push_back("--generate");
            if (model.np_build) args.push_back("--enable-build");
            if (model.np_github) args.push_back("--create-github");
            if (model.np_psx_ref[0]) {
                args.push_back("--psxrecomp-ref");
                args.push_back(model.np_psx_ref);
            }
            if (model.np_ui_ref[0]) {
                args.push_back("--recomp-ui-ref");
                args.push_back(model.np_ui_ref);
            }
            if (model.np_net_ref[0] && std::strcmp(model.np_net_ref, "(default)") != 0) {
                args.push_back("--recomp-net-ref");
                args.push_back(model.np_net_ref);
            }
            if (model.np_rb_ref[0] && std::strcmp(model.np_rb_ref, "(default)") != 0) {
                args.push_back("--rbengine-ref");
                args.push_back(model.np_rb_ref);
            }
            model.append_log("--- New project setup ---");
            retcomm::studio::run_project_studio_async(model, args, [&model](RunResult r) {
                model.set_status(r.ok() ? "New project created" : "New project failed");
                refresh_repos(model);
            });
        }
    }
    accent_button_pop();
    ImGui::EndDisabled();
    ImGui::EndChild();
    (void)th;
}

void draw_git(StudioModel& model, const Theme& th) {
    constexpr float kLabelW = 100.f;
    const std::string root = model.selected_root();
    if (!root.empty() && model.branches_root != root && !model.branches_loading)
        refresh_branches(model, false);
    ImGui::BeginChild("##git_scroll", ImVec2(0, 0));
    ImGui::BeginDisabled(model.busy.load() || root.empty());

    // Action buttons wrap instead of overflowing.
    auto action = [](const char* label) -> bool {
        const float need = widget_label_width(label) + ImGui::GetStyle().ItemSpacing.x;
        if (ImGui::GetCursorPosX() > ImGui::GetCursorStartPos().x &&
            ImGui::GetContentRegionAvail().x < need)
            ImGui::NewLine();
        const bool hit = ImGui::Button(label);
        ImGui::SameLine();
        return hit;
    };
    if (action("Refresh status")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "status", "--root", root},
            [&model](RunResult) { model.set_status("Git status refreshed"); });
    }
    if (action("Ensure submodules")) {
        retcomm::studio::run_project_studio_async(
            model,
            {"git", "ensure-submodules", "--root", root, "--psxrecomp-branch",
             model.git_psx_branch, "--recomp-ui-branch", model.git_ui_branch},
            nullptr);
    }
    if (action("Ensure nested")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "ensure-nested", "--root", root}, nullptr);
    }
    if (action("Update submodules")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "update-submodules", "--root", root}, nullptr);
    }
    end_wrapped_line();

    {
        ImGui::BeginDisabled(model.branches_loading || model.busy.load() || root.empty());
        if (ImGui::Button(model.branches_loading ? "Loading branches…" : "Refresh branches##git"))
            refresh_branches(model, true);
        ImGui::EndDisabled();
    }
    {
        left_label("Branch", kLabelW);
        const float create_w = trailing_checkbox_width("Create");
        float fw = ImGui::GetContentRegionAvail().x - create_w;
        if (fw < 140.f) fw = 140.f;
        // Game branch combo sized to leave room for Create.
        ImGui::SetNextItemWidth(fw);
        const char* preview = model.git_branch[0] ? model.git_branch : "(select…)";
        if (ImGui::BeginCombo("##git_branch", preview, ImGuiComboFlags_HeightLarge)) {
            bool have = false;
            for (const auto& s : model.branches_game)
                if (s == model.git_branch) have = true;
            if (model.git_branch[0] && !have) ImGui::Selectable(model.git_branch, true);
            for (const auto& s : model.branches_game) {
                const bool sel = (s == model.git_branch);
                if (ImGui::Selectable(s.c_str(), sel))
                    std::snprintf(model.git_branch, sizeof(model.git_branch), "%s", s.c_str());
                if (sel) ImGui::SetItemDefaultFocus();
            }
            ImGui::EndCombo();
        }
        ImGui::SameLine();
        ImGui::Checkbox("Create", &model.git_create_branch);
    }
    if (ImGui::Button("Switch branch")) {
        std::vector<std::string> args = {"git", "switch", "--root", root, "--branch",
                                         model.git_branch};
        if (model.git_create_branch) args.push_back("--create");
        retcomm::studio::run_project_studio_async(model, args, nullptr);
    }

    constexpr float kBranchW = 260.f;
    branch_combo("##git_psx", "psxrecomp", model.git_psx_branch, sizeof(model.git_psx_branch),
                 kLabelW, model.branches_psx, kBranchW);
    branch_combo("##git_ui", "recomp-ui", model.git_ui_branch, sizeof(model.git_ui_branch),
                 kLabelW, model.branches_ui, kBranchW);
    branch_combo("##git_net", "recomp-net", model.git_net_branch, sizeof(model.git_net_branch),
                 kLabelW, model.branches_net, kBranchW);
    branch_combo("##git_rb", "rbengine", model.git_rb_branch, sizeof(model.git_rb_branch),
                 kLabelW, model.branches_rb, kBranchW);
    if (ImGui::Button("Switch modules")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "switch", "--root", root, "--modules"}, nullptr);
    }
    ImGui::SameLine();
    if (ImGui::Button("Switch nested")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "switch", "--root", root, "--nested"}, nullptr);
    }

    ImGui::Separator();
    field_row("##git_msg", "Commit msg", model.git_msg, sizeof(model.git_msg), kLabelW);
    if (action("Pull")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "pull", "--root", root, "--ff-only"}, nullptr);
    }
    if (action("Commit")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "commit", "--root", root, "--message", model.git_msg}, nullptr);
    }
    if (action("Push")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "push", "--root", root}, nullptr);
    }
    end_wrapped_line();

    ImGui::Separator();
    ImGui::TextUnformatted("Release");
    {
        left_label("Version", kLabelW);
        const float pub_w = trailing_checkbox_width("Publish");
        float fw = ImGui::GetContentRegionAvail().x - pub_w;
        if (fw < 80.f) fw = 80.f;
        ImGui::SetNextItemWidth(fw);
        ImGui::InputText("##rel_ver", model.release_version, sizeof(model.release_version));
        ImGui::SameLine();
        ImGui::Checkbox("Publish", &model.release_publish);
    }
    if (action("Install CI")) {
        retcomm::studio::run_project_studio_async(
            model, {"git", "install-ci", "--root", root}, nullptr);
    }
    if (action("Dispatch release")) {
        std::vector<std::string> args = {"git", "release", "--root", root};
        if (model.release_version[0]) {
            args.push_back("--version");
            args.push_back(model.release_version);
        }
        if (!model.release_publish) args.push_back("--no-publish");
        retcomm::studio::run_project_studio_async(model, args, nullptr);
    }
    end_wrapped_line();
    ImGui::EndDisabled();
    ImGui::EndChild();
    (void)th;
}

void draw_bulk(StudioModel& model, const Theme& th) {
    constexpr float kLabelW = 72.f;
    ImGui::BeginDisabled(model.busy.load());
    left_label("Jobs", kLabelW);
    if (jobs_combo("##jobs", &model.bulk_jobs, 100.f)) {
        retcomm::studio::run_project_studio_async(
            model,
            {"repos", "set-flags", "--bulk-jobs", std::to_string(model.bulk_jobs), "--json"},
            nullptr, false);
    }
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
        ImGui::SetTooltip(
            "How many repos to work on at the same time.\n"
            "Higher = faster, but uses more of your computer.");
    }
    ImGui::SameLine();
    auto bulk_btn = [](const char* label) -> bool {
        const float need = widget_label_width(label) + ImGui::GetStyle().ItemSpacing.x;
        if (ImGui::GetCursorPosX() > ImGui::GetCursorStartPos().x &&
            ImGui::GetContentRegionAvail().x < need)
            ImGui::NewLine();
        const bool hit = ImGui::Button(label);
        ImGui::SameLine();
        return hit;
    };
    if (bulk_btn("Select all")) {
        for (auto& kv : model.bulk_selected) kv.second = true;
    }
    if (bulk_btn("Select none")) {
        for (auto& kv : model.bulk_selected) kv.second = false;
    }
    if (bulk_btn("Catalog only")) {
        retcomm::studio::run_project_studio_async(
            model, {"repos", "filter-catalog", "--json"},
            [&model](RunResult r) {
                try {
                    auto j = nlohmann::json::parse(r.stdout_text);
                    const auto note = j.value("note", "");
                    for (auto& kv : model.bulk_selected) kv.second = false;
                    int n = 0;
                    if (j.contains("paths") && j["paths"].is_array()) {
                        for (const auto& p : j["paths"]) {
                            const std::string path = p.get<std::string>();
                            auto it = model.bulk_selected.find(path);
                            if (it != model.bulk_selected.end()) {
                                it->second = true;
                                ++n;
                            }
                        }
                    }
                    if (!note.empty()) model.append_log(note);
                    model.set_status("Selected " + std::to_string(n) + " catalog repo(s)");
                } catch (const std::exception& ex) {
                    model.append_log(std::string("[FAIL] filter-catalog: ") + ex.what());
                }
            },
            false);
    }
    if (bulk_btn("Catalog + Contributors")) {
        retcomm::studio::run_project_studio_async(
            model, {"repos", "filter-catalog-contributors", "--json"},
            [&model](RunResult r) {
                try {
                    auto j = nlohmann::json::parse(r.stdout_text);
                    const auto note = j.value("note", "");
                    for (auto& kv : model.bulk_selected) kv.second = false;
                    int n = 0;
                    if (j.contains("paths") && j["paths"].is_array()) {
                        for (const auto& p : j["paths"]) {
                            const std::string path = p.get<std::string>();
                            auto it = model.bulk_selected.find(path);
                            if (it != model.bulk_selected.end()) {
                                it->second = true;
                                ++n;
                            }
                        }
                    }
                    if (!note.empty()) model.append_log(note);
                    model.set_status("Selected " + std::to_string(n) +
                                     " catalog contributor repo(s)");
                } catch (const std::exception& ex) {
                    model.append_log(std::string("[FAIL] filter-catalog-contributors: ") +
                                     ex.what());
                }
            },
            false);
    }
    if (bulk_btn("Refresh list")) refresh_repos(model);
    end_wrapped_line();

    checkbox_wrapped("Game", &model.bulk_tgt_game);
    checkbox_wrapped("Modules", &model.bulk_tgt_modules);
    checkbox_wrapped("psxrecomp", &model.bulk_tgt_psx);
    checkbox_wrapped("Nested", &model.bulk_tgt_nested);
    end_wrapped_line();
    field_row("##bulk_msg", "Message", model.bulk_msg, sizeof(model.bulk_msg), kLabelW);

    auto selected_paths = [&]() {
        std::vector<std::string> out;
        for (const auto& e : model.repos) {
            auto it = model.bulk_selected.find(e.path);
            if (it != model.bulk_selected.end() && it->second) out.push_back(e.path);
        }
        return out;
    };

    auto run_bulk = [&](const char* sub) {
        auto paths = selected_paths();
        if (paths.empty()) {
            model.append_log("[FAIL] No repos selected");
            return;
        }
        std::string select;
        for (size_t i = 0; i < paths.size(); ++i) {
            if (i) select += ",";
            select += paths[i];
        }
        std::vector<std::string> args = {"git", sub, "--select", select};
        if (std::strcmp(sub, "bulk-commit") == 0) {
            args.push_back("--message");
            args.push_back(model.bulk_msg);
        }
        if (model.bulk_tgt_game) args.push_back("--game");
        if (model.bulk_tgt_modules) args.push_back("--modules");
        if (model.bulk_tgt_psx) args.push_back("--psxrecomp");
        if (model.bulk_tgt_nested) args.push_back("--nested");
        model.append_log(std::string("--- Bulk ") + sub + " ---");
        retcomm::studio::run_project_studio_async(model, args, nullptr);
    };

    if (bulk_btn("Bulk status")) run_bulk("bulk-status");
    if (bulk_btn("Bulk pull")) run_bulk("bulk-pull");
    if (bulk_btn("Bulk push")) run_bulk("bulk-push");
    if (bulk_btn("Bulk commit")) run_bulk("bulk-commit");
    end_wrapped_line();

    ImGui::BeginChild("bulk_list", ImVec2(0, 0), ImGuiChildFlags_Borders);
    if (ImGui::BeginTable("bulk_tbl", 2,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_ScrollY |
                              ImGuiTableFlags_BordersInnerV |
                              ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("On", ImGuiTableColumnFlags_WidthFixed, 36.f);
        ImGui::TableSetupColumn("Repo", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableHeadersRow();
        ImGuiListClipper clipper;
        clipper.Begin(static_cast<int>(model.repos.size()));
        while (clipper.Step()) {
            for (int i = clipper.DisplayStart; i < clipper.DisplayEnd; ++i) {
                const auto& e = model.repos[static_cast<size_t>(i)];
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                bool* sel = &model.bulk_selected[e.path];
                ImGui::Checkbox(("##b" + std::to_string(i)).c_str(), sel);
                ImGui::TableSetColumnIndex(1);
                ImGui::TextWrapped("%s\n%s", e.label.c_str(), e.path.c_str());
            }
        }
        ImGui::EndTable();
    }
    ImGui::EndChild();
    ImGui::EndDisabled();
}

void draw_build(StudioModel& model, const Theme& th) {
    constexpr float kLabelW = 100.f;
    const std::string root = model.selected_root();
    ImGui::BeginChild("##build_scroll", ImVec2(0, 0));
    ImGui::BeginDisabled(model.busy.load() || root.empty());
    field_row("##bdir", "Build dir", model.build_dir, sizeof(model.build_dir), kLabelW);
    field_row("##btype", "Build type", model.build_type, sizeof(model.build_type), kLabelW);
    field_row("##btarget", "Target", model.build_target, sizeof(model.build_target), kLabelW);
    field_row("##bgen", "Generator", model.build_generator, sizeof(model.build_generator),
              kLabelW);
    field_row("##bjobs", "Jobs", model.build_jobs, sizeof(model.build_jobs), kLabelW);
    field_row("##bextra", "Extra cmake", model.build_extra, sizeof(model.build_extra), kLabelW);
    field_row("##bexe", "Exe", model.build_exe, sizeof(model.build_exe), kLabelW);
    field_row("##bargs", "Launch args", model.build_launch_args, sizeof(model.build_launch_args),
              kLabelW);
    left_label("Env", kLabelW);
    ImGui::InputTextMultiline("##benv", model.build_env, sizeof(model.build_env),
                              ImVec2(ImGui::GetContentRegionAvail().x, 100.f));

    auto base = [&](const char* sub) {
        std::vector<std::string> args = {"build", sub, "--root", root, "--build-dir",
                                         model.build_dir};
        if (model.build_type[0] && std::strcmp(sub, "configure") == 0) {
            args.push_back("--build-type");
            args.push_back(model.build_type);
        }
        if (model.build_generator[0] && std::strcmp(sub, "configure") == 0) {
            args.push_back("--generator");
            args.push_back(model.build_generator);
        }
        if (model.build_extra[0] && std::strcmp(sub, "configure") == 0) {
            args.push_back("--extra");
            args.push_back(model.build_extra);
        }
        if (model.build_target[0] && std::strcmp(sub, "compile") == 0) {
            args.push_back("--target");
            args.push_back(model.build_target);
        }
        if (model.build_jobs[0] && std::strcmp(sub, "compile") == 0) {
            args.push_back("--jobs");
            args.push_back(model.build_jobs);
        }
        return args;
    };

    auto build_btn = [](const char* label) -> bool {
        const float need = widget_label_width(label) + ImGui::GetStyle().ItemSpacing.x;
        if (ImGui::GetCursorPosX() > ImGui::GetCursorStartPos().x &&
            ImGui::GetContentRegionAvail().x < need)
            ImGui::NewLine();
        const bool hit = ImGui::Button(label);
        ImGui::SameLine();
        return hit;
    };
    accent_button(th);
    const bool cfg = build_btn("Configure");
    accent_button_pop();
    if (cfg) {
        retcomm::studio::run_project_studio_async(model, base("configure"), nullptr);
    }
    if (build_btn("Ensure BIOS")) {
        retcomm::studio::run_project_studio_async(
            model, {"build", "ensure-bios", "--root", root}, nullptr);
    }
    if (build_btn("Build")) {
        retcomm::studio::run_project_studio_async(model, base("compile"), nullptr);
    }
    if (build_btn("Configure + Build")) {
        retcomm::studio::run_project_studio_async(
            model, base("configure"), [&model, root](RunResult r) {
                if (!r.ok()) return;
                std::vector<std::string> args = {"build", "compile", "--root", root, "--build-dir",
                                                 model.build_dir, "--target", model.build_target};
                if (model.build_jobs[0]) {
                    args.push_back("--jobs");
                    args.push_back(model.build_jobs);
                }
                retcomm::studio::run_project_studio_async(model, args, nullptr);
            });
    }
    if (build_btn("Launch")) {
        std::vector<std::string> args = {"build", "run", "--root", root, "--build-dir",
                                         model.build_dir};
        if (model.build_exe[0]) {
            args.push_back("--exe");
            args.push_back(model.build_exe);
        }
        if (model.build_env[0]) {
            args.push_back("--env");
            args.push_back(model.build_env);
        }
        if (model.build_launch_args[0]) {
            args.push_back("--args");
            args.push_back(model.build_launch_args);
        }
        retcomm::studio::run_project_studio_async(model, args, nullptr);
    }
    if (build_btn("Stop")) {
        retcomm::studio::run_project_studio_async(model, {"build", "stop"}, nullptr);
    }
    end_wrapped_line();
    ImGui::EndDisabled();
    ImGui::EndChild();
    (void)th;
}

void draw_log_collapsed_bar(StudioModel& model, const Theme& th) {
    constexpr float kBarH = 40.f;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12.f, 6.f));
    ImGui::BeginChild("log_collapsed", ImVec2(0, kBarH), ImGuiChildFlags_Borders);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(10.f, 3.f));
    const float btn_h = ImGui::GetFrameHeight();
    const float y = std::max(0.f, (ImGui::GetContentRegionAvail().y - btn_h) * 0.5f);
    ImGui::SetCursorPosY(ImGui::GetCursorPosY() + y);
    ImGui::PushStyleColor(ImGuiCol_Text, th.text_muted);
    ImGui::TextUnformatted("ACTIVITY");
    ImGui::PopStyleColor();
    ImGui::SameLine();
    ImGui::TextColored(th.text_muted, "(collapsed)");
    {
        constexpr float kShowW = 64.f;
        const float right = ImGui::GetWindowContentRegionMax().x;
        ImGui::SameLine();
        ImGui::SetCursorPosX(std::max(ImGui::GetCursorPosX() + 8.f, right - kShowW));
        if (ImGui::Button("Show", ImVec2(kShowW, 0))) model.log_expanded = true;
    }
    ImGui::PopStyleVar();
    ImGui::EndChild();
    ImGui::PopStyleVar();
}

void draw_log(StudioModel& model, const Theme& th, float height, SDL_Window* window) {
    if (height < 60.f) height = 60.f;
    ImGui::BeginChild("log", ImVec2(0, height), ImGuiChildFlags_Borders);
    ImGui::PushStyleColor(ImGuiCol_Text, th.text_muted);
    ImGui::TextUnformatted("ACTIVITY");
    ImGui::PopStyleColor();

    std::vector<std::string> lines;
    {
        std::lock_guard<std::mutex> lock(model.mu);
        lines = model.log_lines;
    }

    ImGui::SameLine();
    {
        const float hide_w =
            ImGui::CalcTextSize("Hide").x + ImGui::GetStyle().FramePadding.x * 2.f;
        const float export_w =
            ImGui::CalcTextSize("Export").x + ImGui::GetStyle().FramePadding.x * 2.f;
        const float copy_w =
            ImGui::CalcTextSize("Copy").x + ImGui::GetStyle().FramePadding.x * 2.f;
        const float clear_w =
            ImGui::CalcTextSize("Clear").x + ImGui::GetStyle().FramePadding.x * 2.f;
        const float gap = ImGui::GetStyle().ItemSpacing.x;
        const float right = ImGui::GetWindowContentRegionMax().x;
        ImGui::SetCursorPosX(std::max(
            ImGui::GetCursorPosX(),
            right - hide_w - gap - export_w - gap - copy_w - gap - clear_w));
        if (ImGui::SmallButton("Hide")) model.log_expanded = false;
        ImGui::SameLine();
        bool file_busy = false;
        {
            std::lock_guard<std::mutex> lock(model.pick_mu);
            file_busy = model.file_pick_busy;
        }
        ImGui::BeginDisabled(file_busy || window == nullptr);
        if (ImGui::SmallButton("Export")) begin_export_activity_log(model, window);
        ImGui::EndDisabled();
        ImGui::SameLine();
        if (ImGui::SmallButton("Copy")) {
            constexpr size_t kCopyLines = 100;
            const size_t n = lines.size();
            const size_t start = n > kCopyLines ? n - kCopyLines : 0;
            std::string clip;
            for (size_t i = start; i < n; ++i) {
                if (!clip.empty()) clip.push_back('\n');
                clip += lines[i];
            }
            ImGui::SetClipboardText(clip.empty() ? "(no activity yet)" : clip.c_str());
        }
        ImGui::SameLine();
        if (ImGui::SmallButton("Clear")) {
            std::lock_guard<std::mutex> lock(model.mu);
            model.log_lines.clear();
        }
    }
    ImGui::Separator();

    ImGui::BeginChild("activity_scroll", ImVec2(0, 0), ImGuiChildFlags_None);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(4, 2));
    bool stick = false;
    {
        std::lock_guard<std::mutex> lock(model.mu);
        stick = model.log_scroll_bottom;
        ImGuiListClipper clipper;
        clipper.Begin(static_cast<int>(model.log_lines.size()));
        while (clipper.Step()) {
            for (int i = clipper.DisplayStart; i < clipper.DisplayEnd; ++i) {
                const std::string& line = model.log_lines[static_cast<size_t>(i)];
                ImVec4 col = th.text;
                if (line.find("[FAIL]") != std::string::npos ||
                    line.find("error:") != std::string::npos)
                    col = th.bad;
                else if (line.find("[OK]") != std::string::npos)
                    col = th.good;
                else if (!line.empty() && line[0] == '$')
                    col = th.text_muted;
                ImGui::TextColored(col, "%s", line.c_str());
            }
        }
        if (stick) {
            ImGui::SetScrollHereY(1.0f);
            model.log_scroll_bottom = false;
        }
    }
    ImGui::PopStyleVar();
    ImGui::EndChild();
    ImGui::EndChild();
}

} // namespace

int main(int argc, char** argv) {
    (void)argc;

    if (!SDL_Init(SDL_INIT_VIDEO)) {
        std::fprintf(stderr, "SDL_Init failed: %s\n", SDL_GetError());
        return 1;
    }

    const char* glsl = "#version 150";
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_FLAGS, 0);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_CORE);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 3);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 2);
    SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 1);
    SDL_GL_SetAttribute(SDL_GL_DEPTH_SIZE, 24);
    SDL_GL_SetAttribute(SDL_GL_STENCIL_SIZE, 8);

    SDL_Window* window =
        SDL_CreateWindow("RetComM Studio", 1280, 860, SDL_WINDOW_OPENGL | SDL_WINDOW_RESIZABLE |
                                                          SDL_WINDOW_HIGH_PIXEL_DENSITY);
    if (!window) {
        std::fprintf(stderr, "SDL_CreateWindow failed: %s\n", SDL_GetError());
        return 1;
    }
    SDL_GLContext gl = SDL_GL_CreateContext(window);
    if (!gl) {
        std::fprintf(stderr, "SDL_GL_CreateContext failed: %s\n", SDL_GetError());
        return 1;
    }
    SDL_GL_MakeCurrent(window, gl);
    SDL_GL_SetSwapInterval(1);

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
    io.IniFilename = nullptr;

    StudioModel model;
#if defined(RETCOMM_STUDIO_VERSION)
    model.version = RETCOMM_STUDIO_VERSION;
#else
    model.version = "0.0.0";
#endif
    if (argc > 0 && argv[0] && argv[0][0] != '\0') {
        std::error_code ec;
        model.exe_dir = fs::weakly_canonical(fs::path(argv[0]).parent_path(), ec);
        if (ec || model.exe_dir.empty()) model.exe_dir = fs::path(argv[0]).parent_path();
    }
    apply_window_icon(window, model.exe_dir);
    load_fonts(model.exe_dir);

    const Theme th = retcomm::studio::crt_theme();
    retcomm::studio::apply_imgui_style(th);

    ImGui_ImplSDL3_InitForOpenGL(window, gl);
    ImGui_ImplOpenGL3_Init(glsl);

    {
        std::string err;
        if (!retcomm::studio::resolve_runtime(model, &err)) {
            model.append_log("[FAIL] " + err);
            model.set_status(err);
        } else {
            model.append_log("Toolkit: " + model.toolkit_dir.string());
            model.append_log("Python: " + model.python_exe);
            {
#if defined(_WIN32)
                const char* home = std::getenv("USERPROFILE");
#else
                const char* home = std::getenv("HOME");
#endif
                const fs::path parent =
                    (home && *home) ? fs::path(home) / "Documents" / "GitHub" : fs::path(".");
                std::snprintf(model.np_parent, sizeof(model.np_parent), "%s",
                              parent.string().c_str());
            }
            refresh_repos(model);
        }
    }

    bool running = true;
    while (running) {
        SDL_Event e;
        while (SDL_PollEvent(&e)) {
            ImGui_ImplSDL3_ProcessEvent(&e);
            if (e.type == SDL_EVENT_QUIT) running = false;
            if (e.type == SDL_EVENT_WINDOW_CLOSE_REQUESTED &&
                e.window.windowID == SDL_GetWindowID(window))
                running = false;
        }
        if (model.request_exit.load()) running = false;

        apply_pending_picks(model);
        retcomm::studio::pump_async_jobs(model);

        ImGui_ImplOpenGL3_NewFrame();
        ImGui_ImplSDL3_NewFrame();
        ImGui::NewFrame();

        const ImGuiViewport* vp = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(vp->WorkPos);
        ImGui::SetNextWindowSize(vp->WorkSize);
        ImGui::Begin("##studio", nullptr,
                     ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
                         ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoBringToFrontOnFocus |
                         ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);

        draw_header(model, th, window);
        ImGui::Separator();

        const float spacing = ImGui::GetStyle().ItemSpacing.y;
        const float avail = ImGui::GetContentRegionAvail().y;
        constexpr float kMinLog = 64.f;
        constexpr float kCollapsed = 40.f;
        constexpr float kMinBody = 180.f;
        constexpr float kSplitH = 6.f;
        float log_h = 0.f;
        float body_h = 0.f;
        if (model.log_expanded) {
            const float chrome = kSplitH + spacing * 2.f;
            const float max_log = std::max(kMinLog, avail - kMinBody - chrome);
            log_h = std::clamp(model.log_h_pref, kMinLog, max_log);
            body_h = avail - log_h - chrome;
            if (body_h < kMinBody) body_h = kMinBody;
        } else {
            body_h = avail - kCollapsed - spacing;
            if (body_h < kMinBody) body_h = kMinBody;
        }

        ImGui::BeginChild("body", ImVec2(0, body_h), ImGuiChildFlags_None);
        if (ImGui::BeginTabBar("##tabs")) {
            if (ImGui::BeginTabItem("Migrate")) {
                draw_migrate(model, th, window);
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("New Project")) {
                draw_new_project(model, th, window);
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("Git / GitHub")) {
                draw_git(model, th);
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("Bulk")) {
                draw_bulk(model, th);
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("Build")) {
                draw_build(model, th);
                ImGui::EndTabItem();
            }
            ImGui::EndTabBar();
        }
        ImGui::EndChild();

        if (model.log_expanded) {
            ImGui::InvisibleButton("##logsash", ImVec2(-1, kSplitH));
            if (ImGui::IsItemActive()) {
                const float chrome = kSplitH + spacing * 2.f;
                model.log_h_pref = std::clamp(model.log_h_pref - io.MouseDelta.y, kMinLog,
                                              std::max(kMinLog, avail - kMinBody - chrome));
                log_h = std::clamp(model.log_h_pref, kMinLog,
                                   std::max(kMinLog, avail - kMinBody - chrome));
            }
            if (ImGui::IsItemHovered() || ImGui::IsItemActive())
                ImGui::SetMouseCursor(ImGuiMouseCursor_ResizeNS);
            draw_log(model, th, log_h, window);
        } else {
            draw_log_collapsed_bar(model, th);
        }
        ImGui::End();

        ImGui::Render();
        glViewport(0, 0, (int)(io.DisplaySize.x * io.DisplayFramebufferScale.x),
                   (int)(io.DisplaySize.y * io.DisplayFramebufferScale.y));
        glClearColor(th.background.x, th.background.y, th.background.z, 1.f);
        glClear(GL_COLOR_BUFFER_BIT);
        ImGui_ImplOpenGL3_RenderDrawData(ImGui::GetDrawData());
        SDL_GL_SwapWindow(window);
    }

    ImGui_ImplOpenGL3_Shutdown();
    ImGui_ImplSDL3_Shutdown();
    ImGui::DestroyContext();
    SDL_GL_DestroyContext(gl);
    SDL_DestroyWindow(window);
    SDL_Quit();
    return 0;
}