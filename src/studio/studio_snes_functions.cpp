// SNES Functions tab — what snesrecomp's analyzer proved, and what a human
// has done about it.
//
// The PSX Functions tab runs an analyzer and shows the result. This one only
// shows the result, because on SNES the analysis is not a separate step: it
// happens inside tools/regen.sh, which writes src/gen/program_manifest.json on
// its way to emitting C. There is no button here that re-derives anything, and
// adding one would mean a second analyser whose answer could disagree with the
// code that actually got built.
//
// Two files, two owners, and the split matters:
//
//   src/gen/program_manifest.json   generated. Every node, its disposition,
//                                   and for anything unproven, the reasons.
//                                   Read-only here.
//   recomp/symbols.toml             authored. Names a function and sets `emit`
//                                   to promote it into AOT codegen. The only
//                                   thing this tab writes.
//
// That second file is the human triage step PRINCIPLES.md requires: discovered
// coverage is written into config by a person, never auto-applied, so a
// mis-execution cannot launder itself into trusted static code. Promoting here
// edits the config and says to re-run regen — it does not reach into generated
// output.

#include "studio_snes.hpp"

#include <algorithm>
#include <cstdio>
#include <string>
#include <vector>

#include "imgui.h"
#include "studio_runner.hpp"
#include "studio_theme.hpp"

#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace retcomm::studio {
namespace {

void left_label(const char* text, float w) {
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(text);
    ImGui::SameLine(w);
}

void muted(const Theme& th, const char* fmt, ...) IM_FMTARGS(2);
void muted(const Theme& th, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    ImGui::PushStyleColor(ImGuiCol_Text, th.text_muted);
    ImGui::TextWrappedV(fmt, args);
    ImGui::PopStyleColor();
    va_end(args);
}

// Colour AND wrap — see the note on the same helper in studio_snes.cpp. A
// path or a captured error is long by nature, and unwrapped it widens the
// page instead of the message being readable.
void wrapped(const ImVec4& col, const char* fmt, ...) IM_FMTARGS(2);
void wrapped(const ImVec4& col, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::TextWrappedV(fmt, args);
    ImGui::PopStyleColor();
    va_end(args);
}

std::string json_str(const json& j, const char* key) {
    if (!j.contains(key) || !j[key].is_string()) return {};
    return j[key].get<std::string>();
}

// A node is interesting when it is NOT settled. Sorting those first means the
// worklist is what you see on open, rather than 34 rows of "fine" above the 3
// that need a decision.
bool unsettled(const SnesFunc& f) { return !f.reasons.empty(); }

const char* disposition_help(const std::string& d) {
    if (d == "lle_only")
        return "Interpreted. The analyzer could not prove enough about this "
               "node to emit it ahead of time — the reasons say what.";
    if (d == "aot_eligible")
        return "Provable ahead of time. It still only becomes AOT code when a "
               "symbols.toml entry sets emit = true.";
    return "";
}

} // namespace

bool parse_snes_functions(SnesFunctions& out, const std::string& json_text) {
    out = SnesFunctions{};
    if (json_text.empty()) {
        out.error = "no reply from project_studio analyze status";
        return false;
    }
    try {
        const size_t brace = json_text.find('{');
        if (brace == std::string::npos) {
            out.error = "analyze status printed no JSON: " + json_text.substr(0, 200);
            return false;
        }
        const json j = json::parse(json_text.substr(brace));
        out.root = json_str(j, "root");
        out.promoted = j.value("promoted", 0);
        const json& m = j.at("manifest");
        out.present = m.value("present", false);
        out.path = json_str(m, "path");
        if (!out.present) {
            out.error = json_str(m, "error");
            return true;  // a normal pre-regen state, not a failure to report
        }
        for (const auto& r : m.value("roots", json::array()))
            if (r.is_string()) out.roots.push_back(r.get<std::string>());
        if (m.contains("counts") && m["counts"].is_object()) {
            for (auto it = m["counts"].begin(); it != m["counts"].end(); ++it)
                out.counts[it.key()] = it.value().get<int>();
        }
        for (const auto& n : m.value("nodes", json::array())) {
            SnesFunc f;
            f.key = json_str(n, "key");
            f.pc = json_str(n, "pc");
            f.end = json_str(n, "end");
            f.disposition = json_str(n, "disposition");
            f.name = json_str(n, "name");
            f.bank = n.value("bank", 0);
            f.instructions = n.value("instructions", 0);
            f.demands = n.value("demands", 0);
            f.unresolved = n.value("unresolved", 0);
            if (n.contains("emit") && n["emit"].is_boolean())
                f.emit = n["emit"].get<bool>() ? 1 : 0;
            for (const auto& r : n.value("reasons", json::array()))
                if (r.is_string()) f.reasons.push_back(r.get<std::string>());
            out.funcs.push_back(std::move(f));
        }
        std::stable_sort(out.funcs.begin(), out.funcs.end(),
                         [](const SnesFunc& a, const SnesFunc& b) {
                             if (unsettled(a) != unsettled(b)) return unsettled(a);
                             return a.pc < b.pc;
                         });
        return true;
    } catch (const std::exception& e) {
        out.error = std::string("unreadable analyze status (") + e.what() + ")";
        return false;
    }
}

void load_snes_functions(StudioModel& model, const std::string& root) {
    if (root.empty()) {
        model.snf = SnesFunctions{};
        model.snf.error = "no game repo selected";
        return;
    }
    model.snf.loading = true;
    run_project_studio_async(
        model, {"analyze", "status", "--root", root},
        [&model](RunResult r) {
            SnesFunctions next;
            if (!parse_snes_functions(next, r.stdout_text) && next.error.empty())
                next.error = r.stderr_text;
            next.loading = false;
            model.snf = std::move(next);
        },
        /*log_stdout=*/false);
}

void draw_snes_functions(StudioModel& model, const Theme& th, SDL_Window* /*window*/) {
    const std::string root = model.selected_root();

    ImGui::TextWrapped(
        "Every function snesrecomp's analyzer proved from the ROM, and what it "
        "decided about each one. Discovery is not a step you run here: it "
        "happens inside tools/regen.sh, and this reads what that run concluded.");
    muted(th, "aot_eligible means it CAN be emitted ahead of time. It only IS "
              "emitted when recomp/symbols.toml says emit = true — promoting is "
              "a decision a person makes, which is why it lives in config and "
              "not in the analyzer.");
    ImGui::Separator();

    if (model.snf.root != root && !root.empty() && !model.snf.loading)
        load_snes_functions(model, root);

    ImGui::BeginDisabled(model.busy.load() || root.empty());
    if (ImGui::Button("Reload")) load_snes_functions(model, root);
    ImGui::EndDisabled();
    ImGui::SameLine();
    muted(th, "%s", model.snf.path.empty() ? "src/gen/program_manifest.json"
                                           : model.snf.path.c_str());

    if (model.snf.loading) {
        muted(th, "reading…");
        return;
    }
    if (!model.snf.present) {
        ImGui::TextColored(th.warn, "%s",
                           model.snf.error.empty()
                               ? "no analysis yet — run Build ▸ Regenerate C from ROM"
                               : model.snf.error.c_str());
        return;
    }
    if (!model.snf.error.empty())
        wrapped(th.warn, "%s", model.snf.error.c_str());

    // ---- summary ------------------------------------------------------------
    const int lle = model.snf.counts.count("lle_only") ? model.snf.counts.at("lle_only") : 0;
    const int aot =
        model.snf.counts.count("aot_eligible") ? model.snf.counts.at("aot_eligible") : 0;
    ImGui::Text("%d functions", static_cast<int>(model.snf.funcs.size()));
    ImGui::SameLine();
    ImGui::TextColored(th.good, "%d aot-eligible", aot);
    ImGui::SameLine();
    ImGui::TextColored(lle ? th.warn : th.text_muted, "%d interpreted", lle);
    ImGui::SameLine();
    muted(th, " ·  %d root%s  ·  %d promoted in symbols.toml",
          static_cast<int>(model.snf.roots.size()),
          model.snf.roots.size() == 1 ? "" : "s", model.snf.promoted);

    // ---- filters ------------------------------------------------------------
    // Not left_label() for the second label: that helper ends in SameLine(w),
    // and SameLine's argument is an ABSOLUTE x, not an offset. Used mid-row it
    // sends the cursor back to x=44 and the next widget lands on top of the
    // combo — which is precisely what it did here until a screenshot showed
    // the filter row with its dropdown painted over.
    left_label("Show", 60.f);
    ImGui::SetNextItemWidth(170.f);
    ImGui::Combo("##snf_show", &model.snf_show,
                 "all\0interpreted only\0aot-eligible\0named\0");
    ImGui::SameLine();
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted("Find");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(std::max(160.f, ImGui::GetContentRegionAvail().x - 8.f));
    ImGui::InputTextWithHint("##snf_filter", "name or address", model.snf_filter,
                             sizeof(model.snf_filter));

    std::string needle = model.snf_filter;
    std::transform(needle.begin(), needle.end(), needle.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });

    auto visible = [&](const SnesFunc& f) {
        if (model.snf_show == 1 && f.disposition != "lle_only") return false;
        if (model.snf_show == 2 && f.disposition != "aot_eligible") return false;
        if (model.snf_show == 3 && f.name.empty()) return false;
        if (needle.empty()) return true;
        std::string hay = f.pc + " " + f.name + " " + f.key;
        std::transform(hay.begin(), hay.end(), hay.begin(),
                       [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return hay.find(needle) != std::string::npos;
    };

    // ---- table --------------------------------------------------------------
    const float avail_y = ImGui::GetContentRegionAvail().y;
    const float table_h = std::max(160.f, avail_y * 0.62f);
    if (ImGui::BeginTable("snf", 7,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_ScrollY |
                              ImGuiTableFlags_BordersInnerV |
                              ImGuiTableFlags_SizingStretchProp,
                          ImVec2(0, table_h))) {
        ImGui::TableSetupColumn("PC", ImGuiTableColumnFlags_WidthFixed, 74.f);
        ImGui::TableSetupColumn("Bank", ImGuiTableColumnFlags_WidthFixed, 44.f);
        ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthStretch, 0.34f);
        ImGui::TableSetupColumn("Disposition", ImGuiTableColumnFlags_WidthFixed, 104.f);
        ImGui::TableSetupColumn("Insns", ImGuiTableColumnFlags_WidthFixed, 54.f);
        ImGui::TableSetupColumn("Emit", ImGuiTableColumnFlags_WidthFixed, 48.f);
        ImGui::TableSetupColumn("Why not", ImGuiTableColumnFlags_WidthStretch, 0.66f);
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableHeadersRow();

        for (int i = 0; i < static_cast<int>(model.snf.funcs.size()); ++i) {
            const SnesFunc& f = model.snf.funcs[static_cast<size_t>(i)];
            if (!visible(f)) continue;
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            if (ImGui::Selectable(f.pc.c_str(), model.snf_sel == i,
                                  ImGuiSelectableFlags_SpanAllColumns))
                model.snf_sel = i;
            ImGui::TableNextColumn();
            ImGui::Text("%02X", f.bank);
            ImGui::TableNextColumn();
            if (f.name.empty()) muted(th, "(unnamed)");
            else ImGui::TextUnformatted(f.name.c_str());
            ImGui::TableNextColumn();
            const bool interpreted = f.disposition == "lle_only";
            ImGui::TextColored(interpreted ? th.warn : th.good, "%s",
                               f.disposition.c_str());
            if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
                const char* help = disposition_help(f.disposition);
                if (help && *help) ImGui::SetTooltip("%s", help);
            }
            ImGui::TableNextColumn();
            ImGui::Text("%d", f.instructions);
            ImGui::TableNextColumn();
            if (f.emit < 0) muted(th, "—");
            else ImGui::TextColored(f.emit ? th.good : th.text_muted,
                                    f.emit ? "on" : "off");
            ImGui::TableNextColumn();
            if (f.reasons.empty()) {
                muted(th, "—");
            } else {
                std::string first = f.reasons.front();
                if (f.reasons.size() > 1)
                    first += "  (+" + std::to_string(f.reasons.size() - 1) + ")";
                ImGui::TextUnformatted(first.c_str());
            }
        }
        ImGui::EndTable();
    }

    // ---- detail + the one write this tab performs ---------------------------
    if (model.snf_sel < 0 || model.snf_sel >= static_cast<int>(model.snf.funcs.size()))
        return;
    const SnesFunc& sel = model.snf.funcs[static_cast<size_t>(model.snf_sel)];

    ImGui::Separator();
    // Wrapped: a reason string is a generated identifier and routinely longer
    // than the pane. Unwrapped it widens the content region and the table
    // above it starts scrolling sideways.
    ImGui::TextWrapped("%s..%s  ·  %s  ·  %d instructions  ·  %d demand%s (%d unresolved)",
                       sel.pc.c_str(), sel.end.c_str(), sel.key.c_str(),
                       sel.instructions, sel.demands, sel.demands == 1 ? "" : "s",
                       sel.unresolved);
    for (const std::string& r : sel.reasons) {
        ImGui::Bullet();
        ImGui::SameLine();
        ImGui::TextWrapped("%s", r.c_str());
    }
    if (!sel.reasons.empty()) {
        muted(th, "These are questions for discovery, not sites to hand-patch. A "
                  "call the analyzer cannot prove is a gap in the analyzer or in "
                  "recomp/*.cfg — fixing it there fixes every title that hits the "
                  "same shape.");
    }

    const bool can_write = !model.busy.load() && !root.empty();
    ImGui::BeginDisabled(!can_write);
    const bool promoted = sel.emit == 1;
    if (ImGui::Button(promoted ? "Hold interpreted (emit = false)"
                               : "Promote to AOT (emit = true)")) {
        const std::string pc = sel.pc;
        const std::string name = sel.name;
        std::vector<std::string> args = {"analyze", "set-symbol", "--root", root,
                                         "--pc", pc, "--emit",
                                         promoted ? "false" : "true"};
        if (!name.empty()) {
            args.push_back("--name");
            args.push_back(name);
        }
        run_project_studio_async(model, args, [&model, root](RunResult r) {
            if (!r.ok()) {
                model.set_status("symbols.toml edit failed");
                return;
            }
            model.set_status("symbols.toml updated — re-run regen to emit it");
            load_snes_functions(model, root);
        });
    }
    ImGui::EndDisabled();
    ImGui::SameLine();
    muted(th, "writes recomp/symbols.toml only. The C is not regenerated here — "
              "run Build ▸ Regenerate C from ROM to make it take effect.");
}

} // namespace retcomm::studio
