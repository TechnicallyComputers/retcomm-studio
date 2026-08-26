// studio_functions.cpp — the Functions tab.
//
// Discovery triage, naming, and export — not a reverse-engineering IDE. The
// division of labour is deliberate: psxrecomp-analyze proves things about the
// executable and writes JSON; this tab makes the results navigable and lets a
// name be attached to an address in one keystroke. Anything deeper (decompiler
// view, type recovery, patching) belongs in Ghidra, which is why Export is a
// first-class button rather than an afterthought.
//
// Two constraints shaped the code below:
//
//   * Row counts are in the thousands, so the table is clipped
//     (ImGuiListClipper) and the filtered order is cached in model.fn_view
//     rather than recomputed per frame.
//   * Studio never analyses anything itself. Every mutation goes back out
//     through `python -m project_studio analyze …`, so the CLI stays the single
//     source of truth and a headless run behaves identically.

#include "studio/studio_functions.hpp"
#include "studio/studio_theme.hpp"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <filesystem>
#include <map>
#include <string>
#include <vector>

#include "imgui.h"
#include "studio/studio_debug.hpp"
#include "studio/studio_runner.hpp"

namespace fs = std::filesystem;

namespace retcomm::studio {

namespace {

ImVec4 confidence_color(const Theme& th, int rank) {
    switch (rank) {
    case 0: return th.good;
    case 1: return ImVec4(0.55f, 0.85f, 0.70f, 1.f);
    case 2: return th.warn;
    case 4: return th.text_muted;
    default: return th.bad;
    }
}

const char* kConfNames[] = {"verified", "high", "medium", "low"};

} // namespace

// ---------------------------------------------------------------------------
namespace {

const FnRow* find_row(const StudioModel& model, uint32_t addr) {
    for (const auto& r : model.fn_rows)
        if (r.addr == addr) return &r;
    return nullptr;
}

int find_index(const StudioModel& model, uint32_t addr) {
    for (int i = 0; i < static_cast<int>(model.fn_rows.size()); ++i)
        if (model.fn_rows[i].addr == addr) return i;
    return -1;
}

void select_addr(StudioModel& model, uint32_t addr) {
    const int idx = find_index(model, addr);
    if (idx < 0) return;
    model.fn_selected = idx;
    std::snprintf(model.fn_rename, sizeof(model.fn_rename), "%s",
                  model.fn_rows[idx].name.c_str());
    std::snprintf(model.fn_rename_note, sizeof(model.fn_rename_note), "%s",
                  model.fn_rows[idx].note.c_str());
}

// One clickable "0x8001234 <name>" cell that jumps the selection.
void link_row(StudioModel& model, const Theme& th, uint32_t addr, const char* suffix,
              const char* id) {
    const FnRow* t = find_row(model, addr);
    char label[196];
    std::snprintf(label, sizeof(label), "0x%08X  %s%s##%s", addr,
                  t ? t->name.c_str() : "(outside the image)", suffix, id);
    ImGui::PushStyleColor(ImGuiCol_Text, t ? th.text : th.text_muted);
    if (ImGui::Selectable(label) && t) select_addr(model, addr);
    ImGui::PopStyleColor();
}

void refresh_after_write(StudioModel& model) {
    const uint32_t keep = (model.fn_selected >= 0 &&
                           model.fn_selected < static_cast<int>(model.fn_rows.size()))
                              ? model.fn_rows[model.fn_selected].addr
                              : 0;
    load_analysis(model, model.selected_root());
    rebuild_fn_view(model);
    if (keep) select_addr(model, keep);
}

} // namespace

// ---------------------------------------------------------------------------
void draw_functions(StudioModel& model, const Theme& th, SDL_Window* /*window*/) {
    const std::string root = model.selected_root();
    const bool busy = model.busy.load();

    if (root != model.fn_loaded_root) {
        load_analysis(model, root);
        rebuild_fn_view(model);
    }

    // ---- run row -----------------------------------------------------------
    ImGui::BeginDisabled(busy || root.empty());
    ImGui::PushStyleColor(ImGuiCol_Button, th.accent_button);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, th.accent_button_hovered);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, th.accent_button_active);
    const bool run = ImGui::Button("Analyze");
    ImGui::PopStyleColor(3);
    if (run) {
        std::vector<std::string> args = {"analyze", "run", "--root", root};
        if (model.fn_exe_override[0]) {
            args.push_back("--exe");
            args.push_back(model.fn_exe_override);
        }
        if (model.fn_exact) args.push_back("--exact");
        if (model.fn_with_refs) args.push_back("--refs");
        if (model.fn_emit_ghidra) args.push_back("--emit-ghidra");
        if (model.fn_emit_symbol_addrs) args.push_back("--emit-symbol-addrs");
        if (model.fn_widescreen) args.push_back("--widescreen");
        if (model.fn_emit_symbols) {
            args.push_back("--emit-symbols");
            args.push_back("--min-confidence");
            args.push_back(kConfNames[std::clamp(model.fn_min_conf, 0, 3)]);
        }
        run_project_studio_async(model, args, [&model](RunResult) {
            load_analysis(model, model.selected_root());
            rebuild_fn_view(model);
        });
    }
    ImGui::SameLine();
    if (ImGui::Button("Reload")) {
        load_analysis(model, root);
        rebuild_fn_view(model);
    }
    ImGui::SameLine();
    ImGui::Checkbox("Exact (reachable only)", &model.fn_exact);
    ImGui::SameLine();
    ImGui::Checkbox("Data refs", &model.fn_with_refs);
    ImGui::SameLine();
    ImGui::Checkbox("Ghidra script", &model.fn_emit_ghidra);
    ImGui::SameLine();
    ImGui::Checkbox("Symbol map", &model.fn_emit_symbol_addrs);
    ImGui::SameLine();
    ImGui::Checkbox("Widescreen sites", &model.fn_widescreen);

    ImGui::Checkbox("Merge into symbols.toml at", &model.fn_emit_symbols);
    ImGui::SameLine();
    ImGui::SetNextItemWidth(110.f);
    ImGui::BeginDisabled(!model.fn_emit_symbols);
    ImGui::Combo("##minconf", &model.fn_min_conf, kConfNames, 4);
    ImGui::EndDisabled();
    ImGui::SameLine();
    ImGui::TextColored(th.text_muted, "or better");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(220.f);
    ImGui::InputTextWithHint("##exeov", "EXE override (blank = game.toml)",
                             model.fn_exe_override, sizeof(model.fn_exe_override));
    ImGui::EndDisabled();

    // ---- live runtime connection -------------------------------------------
    DebugClient& dbg = shared_debug_client();
    const DebugClient::Snapshot ds = dbg.snapshot();
    {
        const bool connected = ds.state == DebugClient::State::Connected;
        const char* label =
            connected ? "connected"
                      : (ds.state == DebugClient::State::Connecting ? "waiting for runtime…"
                                                                    : "not connected");
        ImGui::TextColored(connected ? th.good : th.text_muted, "Runtime: %s", label);
        if (connected) {
            ImGui::SameLine();
            ImGui::TextColored(th.text_muted, "· frame %llu",
                               (unsigned long long)ds.frame);
        }
        ImGui::SameLine();
        if (!dbg.running()) {
            if (ImGui::SmallButton("Connect")) dbg.start(model.dbg_host, model.dbg_port);
        } else {
            if (ImGui::SmallButton("Disconnect")) {
                dbg.stop();
                model.dbg_live_trace = false;
            }
        }
        ImGui::SameLine();
        ImGui::BeginDisabled(!connected || model.fn_rows.empty());
        if (ImGui::Checkbox("Live trace", &model.dbg_live_trace)) {
            // Trace the whole image: the interesting result is which functions
            // execute that the static call graph never reaches.
            uint32_t lo = 0xFFFFFFFFu, hi = 0;
            for (const auto& r : model.fn_rows) {
                lo = std::min(lo, r.addr);
                hi = std::max(hi, r.end);
            }
            dbg.set_live_trace(model.dbg_live_trace, lo, hi);
        }
        ImGui::EndDisabled();
        if (model.dbg_live_trace) {
            ImGui::SameLine();
            ImGui::TextColored(th.text_muted, "· %llu entries observed",
                               (unsigned long long)ds.consumed);
            ImGui::SameLine();
            if (ImGui::SmallButton("Reset counts")) dbg.reset_counts();
        }
        if (!ds.error.empty() && ds.state != DebugClient::State::Connected) {
            ImGui::SameLine();
            ImGui::TextColored(th.text_muted, "(%s)", ds.error.c_str());
        }
    }

    ImGui::Separator();

    if (model.fn_rows.empty()) {
        ImGui::TextColored(th.text_muted, "%s",
                           model.fn_error.empty()
                               ? "No functions loaded."
                               : model.fn_error.c_str());
        ImGui::TextWrapped(
            "Analyze reads only the boot executable. No emulator run, trace, or "
            "overlay capture is consulted, so whatever it cannot prove is "
            "reported as a gap rather than filled in.");
        return;
    }

    // ---- stats banner ------------------------------------------------------
    const FnStats& s = model.fn_stats;
    ImGui::Text("%s", model.fn_image.c_str());
    ImGui::SameLine();
    ImGui::TextColored(th.text_muted, "· %u functions · %u instructions · %.1f%% of image covered",
                       s.total, s.instructions,
                       s.bytes_image ? 100.0 * s.bytes_covered / s.bytes_image : 0.0);
    ImGui::TextColored(th.good, "verified %u", s.verified);
    ImGui::SameLine();
    ImGui::TextColored(ImVec4(0.55f, 0.85f, 0.70f, 1.f), "high %u", s.high);
    ImGui::SameLine();
    ImGui::TextColored(th.warn, "medium %u", s.medium);
    ImGui::SameLine();
    ImGui::TextColored(th.bad, "low %u", s.low);
    ImGui::SameLine();
    ImGui::TextColored(th.text_muted,
                       "| named %u · orphans %u · jump tables %u (%u targets) · "
                       "unresolved indirect %u",
                       s.named, s.orphans, s.tables, s.table_targets, s.unresolved);

    // ---- filters -----------------------------------------------------------
    ImGui::SetNextItemWidth(220.f);
    if (ImGui::InputTextWithHint("##fnfilter", "filter: name, address, note",
                                 model.fn_filter, sizeof(model.fn_filter)))
        model.fn_view_dirty = true;
    ImGui::SameLine();
    ImGui::SetNextItemWidth(120.f);
    const char* conf_items[] = {"all", "verified", "high", "medium", "low", "data"};
    if (ImGui::Combo("##conf", &model.fn_conf_filter, conf_items, 6))
        model.fn_view_dirty = true;
    ImGui::SameLine();
    if (ImGui::Checkbox("named", &model.fn_only_named)) model.fn_view_dirty = true;
    ImGui::SameLine();
    if (ImGui::Checkbox("orphans", &model.fn_only_orphans)) model.fn_view_dirty = true;
    ImGui::SameLine();
    if (ImGui::Checkbox("has indirect", &model.fn_only_unresolved))
        model.fn_view_dirty = true;
    ImGui::SameLine();
    if (ImGui::Checkbox("hide data", &model.fn_hide_data)) model.fn_view_dirty = true;
    ImGui::SameLine();
    ImGui::TextColored(th.text_muted, "%zu shown", model.fn_view.size());

    // Fold the trace's tally onto the rows before the view is built, so a
    // Live-sorted table reorders as the counts come in rather than freezing at
    // whatever the order was when you clicked the header.
    const bool tracing = ds.live_trace || ds.consumed > 0;
    if (apply_live_counts(model, ds.calls, ds.consumed) && model.fn_sort_col == 5)
        model.fn_view_dirty = true;

    if (model.fn_view_dirty) rebuild_fn_view(model);

    // ---- table + side pane -------------------------------------------------
    const float pane_w = std::max(320.f, ImGui::GetContentRegionAvail().x * 0.40f);
    const float table_w = ImGui::GetContentRegionAvail().x - pane_w -
                          ImGui::GetStyle().ItemSpacing.x;

    ImGui::BeginChild("##fn_table", ImVec2(table_w, 0), false);
    // Live is ALWAYS a column, populated or not. A column that appears only
    // once data arrives cannot be sorted on before you have data, and it makes
    // the column indices the sort spec reports mean different things depending
    // on whether a game happens to be running — which silently reinterprets a
    // saved sort. Empty is a legitimate value here: "nothing observed yet" is
    // exactly what you want to see next to a static call graph.
    if (ImGui::BeginTable("fn_tbl", 7,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_ScrollY |
                              ImGuiTableFlags_BordersInnerV |
                              ImGuiTableFlags_Sortable |
                              ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("Address", ImGuiTableColumnFlags_WidthFixed, 78.f);
        ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthStretch, 0.42f);
        ImGui::TableSetupColumn("Conf", ImGuiTableColumnFlags_WidthFixed, 64.f);
        ImGui::TableSetupColumn("Size", ImGuiTableColumnFlags_WidthFixed, 58.f);
        ImGui::TableSetupColumn("In", ImGuiTableColumnFlags_WidthFixed, 40.f);
        ImGui::TableSetupColumn("Live", ImGuiTableColumnFlags_WidthFixed, 62.f);
        ImGui::TableSetupColumn("Signature", ImGuiTableColumnFlags_WidthStretch, 0.28f);
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableHeadersRow();

        if (ImGuiTableSortSpecs* sort = ImGui::TableGetSortSpecs()) {
            if (sort->SpecsDirty && sort->SpecsCount > 0) {
                model.fn_sort_col = sort->Specs[0].ColumnIndex;
                model.fn_sort_desc =
                    sort->Specs[0].SortDirection == ImGuiSortDirection_Descending;
                model.fn_view_dirty = true;
                sort->SpecsDirty = false;
            }
        }

        // Thousands of rows: only build the visible ones.
        ImGuiListClipper clipper;
        clipper.Begin(static_cast<int>(model.fn_view.size()));
        while (clipper.Step()) {
            for (int v = clipper.DisplayStart; v < clipper.DisplayEnd; ++v) {
                const int i = model.fn_view[v];
                const FnRow& r = model.fn_rows[i];
                ImGui::TableNextRow();
                ImGui::PushID(i);

                ImGui::TableSetColumnIndex(0);
                char sel[32];
                std::snprintf(sel, sizeof(sel), "%08X", r.addr);
                if (ImGui::Selectable(sel, model.fn_selected == i,
                                      ImGuiSelectableFlags_SpanAllColumns))
                    select_addr(model, r.addr);

                ImGui::TableSetColumnIndex(1);
                ImGui::PushStyleColor(ImGuiCol_Text,
                                      r.user_named ? th.accent : th.text);
                ImGui::TextUnformatted(r.name.c_str());
                ImGui::PopStyleColor();

                ImGui::TableSetColumnIndex(2);
                ImGui::TextColored(confidence_color(th, r.conf_rank), "%s",
                                   r.confidence.c_str());

                ImGui::TableSetColumnIndex(3);
                ImGui::Text("%u", r.size);

                ImGui::TableSetColumnIndex(4);
                ImGui::Text("%u", r.in_degree);

                ImGui::TableSetColumnIndex(5);
                if (r.live_calls == 0) {
                    // "—" and "0" are different claims. Without a trace armed
                    // nothing has been watched, so no count exists; with one
                    // armed, zero means watched-and-never-entered, which is a
                    // real result worth sorting against.
                    ImGui::TextColored(th.text_muted, tracing ? "0" : "—");
                    if (!tracing && ImGui::IsItemHovered())
                        ImGui::SetTooltip("No live trace armed — nothing has been "
                                          "observed yet. Connect to a running game and "
                                          "tick Live trace.");
                } else {
                    // Observed but statically unreachable = an indirect edge
                    // the analyzer reported as a gap. That is the finding.
                    const bool indirect_only = !r.reachable || r.in_degree == 0;
                    ImGui::TextColored(indirect_only ? th.warn : th.text, "%llu",
                                       (unsigned long long)r.live_calls);
                    if (indirect_only && ImGui::IsItemHovered())
                        ImGui::SetTooltip(
                            "Executed %llu time(s) but has no proven static caller "
                            "— reached indirectly.",
                            (unsigned long long)r.live_calls);
                }

                ImGui::TableSetColumnIndex(6);
                ImGui::TextColored(th.text_muted, "%s",
                                   r.bios_call.empty() ? r.prototype.c_str()
                                                       : r.bios_call.c_str());

                if (ImGui::BeginPopupContextItem("##fnctx")) {
                    ImGui::TextColored(th.text_muted, "0x%08X  %s", r.addr, r.name.c_str());
                    ImGui::Separator();
                    const bool live = ds.state == DebugClient::State::Connected;
                    ImGui::BeginDisabled(!live);
                    char cmd[192];
                    if (ImGui::MenuItem("Arm function trace")) {
                        std::snprintf(cmd, sizeof(cmd),
                                      "\"cmd\":\"fntrace_arm\",\"target\":\"0x%08X\"",
                                      r.addr);
                        dbg.send(cmd, "fntrace_arm");
                    }
                    if (ImGui::MenuItem("Filter trace to this function")) {
                        std::snprintf(cmd, sizeof(cmd),
                                      "\"cmd\":\"fn_filter\",\"lo\":\"0x%08X\","
                                      "\"hi\":\"0x%08X\"", r.addr, r.end);
                        dbg.send(cmd, "fn_filter");
                    }
                    if (ImGui::MenuItem("Watch writes in its frame")) {
                        std::snprintf(cmd, sizeof(cmd),
                                      "\"cmd\":\"wtrace_range\",\"lo\":\"0x%08X\","
                                      "\"hi\":\"0x%08X\"", r.addr, r.end);
                        dbg.send(cmd, "wtrace_range");
                    }
                    ImGui::Separator();
                    // Intrusive: stops the game. Kept visually apart from the
                    // read-only verbs above so it is not a slip of the mouse.
                    ImGui::PushStyleColor(ImGuiCol_Text, th.warn);
                    if (ImGui::MenuItem("Break here (pauses the game)")) {
                        std::snprintf(cmd, sizeof(cmd),
                                      "\"cmd\":\"pc_break\",\"addr\":\"0x%08X\"",
                                      r.addr);
                        dbg.send(cmd, "pc_break");
                    }
                    ImGui::PopStyleColor();
                    ImGui::EndDisabled();
                    if (!live)
                        ImGui::TextColored(th.text_muted, "(connect to a running game)");
                    ImGui::Separator();
                    if (ImGui::MenuItem("Copy address")) {
                        char a[16];
                        std::snprintf(a, sizeof(a), "0x%08X", r.addr);
                        ImGui::SetClipboardText(a);
                    }
                    ImGui::EndPopup();
                }
                ImGui::PopID();
            }
        }
        ImGui::EndTable();
    }
    ImGui::EndChild();

    ImGui::SameLine();
    ImGui::BeginChild("##fn_pane", ImVec2(0, 0), true);

    if (ImGui::BeginTabBar("##fnpane_tabs")) {
        if (ImGui::BeginTabItem("Detail")) {
            model.fn_pane = 0;
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Indirect worklist")) {
            model.fn_pane = 1;
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Widescreen")) {
            model.fn_pane = 2;
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }

    if (model.fn_pane == 2) {
        // Widescreen bring-up otherwise means locating exact instruction
        // addresses by hand. Every candidate here already satisfies the
        // instruction form code_generator.cpp demands, so pasting one can never
        // break the build — only playtesting decides whether it helps.
        if (!model.ws_loaded) {
            ImGui::TextWrapped(
                "No widescreen scan yet. Tick \"Widescreen sites\" and press "
                "Analyze.");
            ImGui::EndChild();
            return;
        }
        auto imms = [](const std::vector<uint32_t>& v) {
            std::string s2;
            for (size_t i = 0; i < v.size(); ++i)
                s2 += (i ? ", " : "") + std::string("0x") +
                      [&] { char b[8]; std::snprintf(b, sizeof(b), "%X", v[i]); return std::string(b); }();
            return s2;
        };
        ImGui::TextColored(th.text_muted, "screen_w_imms = [%s]", imms(model.ws_w_imms).c_str());
        ImGui::TextColored(th.text_muted, "screen_h_imms = [%s]  (%s)",
                           imms(model.ws_h_imms).c_str(),
                           model.ws_discovered ? "discovered" : "configured");
        ImGui::Text("%zu function(s) carry the screen-extent signature",
                    model.ws_extent_funcs.size());
        ImGui::TextColored(th.text_muted,
                           "auto_screen_x = true handles those with no per-address list.");
        if (!model.ws_toml_path.empty()) {
            ImGui::Spacing();
            if (ImGui::Button("Copy game.toml block")) {
                std::ifstream in(model.ws_toml_path, std::ios::binary);
                std::string body((std::istreambuf_iterator<char>(in)),
                                 std::istreambuf_iterator<char>());
                ImGui::SetClipboardText(body.c_str());
                model.append_log("[OK] widescreen block copied to clipboard");
            }
            ImGui::SameLine();
            ImGui::TextColored(th.text_muted, "%s", model.ws_toml_path.c_str());
        }
        ImGui::Separator();

        const char* kinds[] = {"bias_sites", "range_sites", "screen_x_sites", "a1_sites"};
        for (const char* k : kinds) {
            std::vector<const WsSite*> v;
            for (const auto& w : model.ws_sites)
                if (w.kind == k) v.push_back(&w);
            if (v.empty()) continue;
            char hdr[96];
            std::snprintf(hdr, sizeof(hdr), "%s  (%zu)", k, v.size());
            if (!ImGui::CollapsingHeader(hdr, ImGuiTreeNodeFlags_DefaultOpen)) continue;
            for (const WsSite* w : v) {
                ImGui::PushID(static_cast<int>(w->pc));
                char row[220];
                std::snprintf(row, sizeof(row), "0x%08X  %3d%%  %s", w->pc,
                              w->confidence, w->instr.c_str());
                if (ImGui::Selectable(row)) select_addr(model, w->func);
                if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", w->evidence.c_str());
                ImGui::PopID();
            }
        }
        ImGui::EndChild();
        return;
    }

    if (model.fn_pane == 1) {
        // The worklist: every transfer static analysis could not prove, with the
        // instruction window that produced it. Resolving one usually means
        // adding a seed and re-running, which is why the addresses are
        // selectable straight into the table.
        ImGui::TextWrapped(
            "Transfers with no proven target set. These are the coverage gap — "
            "nothing here is guessed from a runtime trace.");
        ImGui::Separator();
        int shown = 0;
        for (const auto& ind : model.fn_indirect) {
            if (ind.classification != "unresolved") continue;
            ImGui::PushID(static_cast<int>(ind.pc));
            const FnRow* owner = find_row(model, ind.func);
            char hdr[220];
            std::snprintf(hdr, sizeof(hdr), "0x%08X  %s $%u  in %s", ind.pc,
                          ind.kind.c_str(), ind.reg,
                          owner ? owner->name.c_str() : "?");
            if (ImGui::TreeNode(hdr)) {
                if (owner && ImGui::SmallButton("Select owner"))
                    select_addr(model, ind.func);
                ImGui::PushStyleColor(ImGuiCol_Text, th.text_muted);
                for (const auto& line : ind.context)
                    ImGui::TextUnformatted(line.c_str());
                ImGui::PopStyleColor();
                ImGui::TreePop();
            }
            ImGui::PopID();
            if (++shown >= 400) {
                ImGui::TextColored(th.text_muted,
                                   "… %u more not listed (raise the cap or filter "
                                   "in the table)",
                                   model.fn_stats.unresolved - 400);
                break;
            }
        }
        if (shown == 0)
            ImGui::TextColored(th.good, "No unresolved indirect transfers.");

        // Recovered tables are the other half of the story and belong next to
        // the failures, so the ratio is visible rather than buried in stats.
        ImGui::Separator();
        ImGui::TextColored(th.text_muted, "Recovered jump tables");
        for (const auto& ind : model.fn_indirect) {
            if (ind.classification.rfind("jump_table", 0) != 0) continue;
            const FnRow* owner = find_row(model, ind.func);
            ImGui::PushID(static_cast<int>(ind.pc) ^ 0x5A5A);
            char hdr[220];
            std::snprintf(hdr, sizeof(hdr), "0x%08X  %u cases at 0x%08X  [%s]  in %s",
                          ind.pc, ind.table_count, ind.table_base,
                          ind.classification.c_str(),
                          owner ? owner->name.c_str() : "?");
            if (ImGui::TreeNode(hdr)) {
                for (size_t k = 0; k < ind.targets.size(); ++k) {
                    char id[32];
                    std::snprintf(id, sizeof(id), "t%zu", k);
                    char idx[16];
                    std::snprintf(idx, sizeof(idx), "  [%zu]", k);
                    link_row(model, th, ind.targets[k], idx, id);
                }
                ImGui::TreePop();
            }
            ImGui::PopID();
        }
        ImGui::EndChild();
        return;
    }

    // ---- detail pane -------------------------------------------------------
    if (model.fn_selected < 0 ||
        model.fn_selected >= static_cast<int>(model.fn_rows.size())) {
        ImGui::TextColored(th.text_muted, "Select a function.");
        ImGui::EndChild();
        return;
    }
    const FnRow& r = model.fn_rows[model.fn_selected];

    ImGui::TextColored(th.accent, "0x%08X", r.addr);
    ImGui::SameLine();
    ImGui::Text("%s", r.name.c_str());
    ImGui::TextColored(confidence_color(th, r.conf_rank), "%s", r.confidence.c_str());
    ImGui::SameLine();
    ImGui::TextColored(th.text_muted, "— %s", r.confidence_reason.c_str());

    if (!r.bios_call.empty())
        ImGui::TextColored(th.good, "PSX kernel call %s", r.bios_call.c_str());

    ImGui::Separator();
    ImGui::TextWrapped("%s", r.prototype.c_str());
    if (!r.sig_confident)
        ImGui::TextColored(th.warn,
                           "Signature is approximate here (loop or partial decode).");
    ImGui::TextColored(th.text_muted,
                       "%u bytes · %u instructions · %u blocks · frame %d · saved %s",
                       r.size, r.instructions, r.blocks, r.stack_frame,
                       r.saved.empty() ? "none" : r.saved.c_str());
    ImGui::TextColored(th.text_muted, "%s%s%s%s%s%s%s",
                       r.leaf ? "leaf " : "", r.gte ? "gte " : "",
                       r.mmio ? "mmio " : "", r.syscall ? "syscall " : "",
                       r.address_taken ? "address-taken " : "",
                       r.reachable ? "" : "orphan ", r.partial ? "partial " : "");

    // ---- rename ------------------------------------------------------------
    ImGui::Separator();
    ImGui::TextColored(th.text_muted, "Name (written to symbols.toml)");
    ImGui::SetNextItemWidth(-1.f);
    const bool submit_name =
        ImGui::InputText("##rename", model.fn_rename, sizeof(model.fn_rename),
                         ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::SetNextItemWidth(-1.f);
    ImGui::InputTextWithHint("##renamenote", "note (optional)", model.fn_rename_note,
                             sizeof(model.fn_rename_note));
    ImGui::SetNextItemWidth(140.f);
    const char* st_items[] = {"guessed", "confirmed", "hot"};
    ImGui::Combo("##renamestatus", &model.fn_rename_status, st_items, 3);
    ImGui::SameLine();

    ImGui::BeginDisabled(busy);
    const bool save = ImGui::Button("Save name") || submit_name;
    ImGui::SameLine();
    const bool clear = ImGui::Button("Clear");
    ImGui::EndDisabled();

    char pc_text[16];
    std::snprintf(pc_text, sizeof(pc_text), "0x%08X", r.addr);
    if (save && model.fn_rename[0]) {
        std::vector<std::string> args = {
            "analyze", "set-symbol", "--root", root, "--pc", pc_text,
            "--name", model.fn_rename, "--status", st_items[model.fn_rename_status]};
        if (model.fn_rename_note[0]) {
            args.push_back("--note");
            args.push_back(model.fn_rename_note);
        }
        run_project_studio_async(model, args,
                                 [&model](RunResult) { refresh_after_write(model); });
    }
    if (clear) {
        run_project_studio_async(
            model, {"analyze", "clear-symbol", "--root", root, "--pc", pc_text},
            [&model](RunResult) { refresh_after_write(model); });
    }

    // ---- callers / callees -------------------------------------------------
    ImGui::Separator();
    const uint32_t self = r.addr;
    ImGui::TextColored(th.text_muted, "Callers (%u)", r.in_degree);
    {
        std::map<uint32_t, std::string> seen;
        for (const auto& e : model.fn_edges)
            if (e.to == self && e.from) seen[e.from] = e.kind;
        int n = 0;
        for (const auto& [from, kind] : seen) {
            char id[24];
            std::snprintf(id, sizeof(id), "cr%d", n);
            std::string suffix = (kind == "direct") ? "" : "  [" + kind + "]";
            link_row(model, th, from, suffix.c_str(), id);
            if (++n >= 60) {
                ImGui::TextColored(th.text_muted, "… %zu more", seen.size() - 60);
                break;
            }
        }
        // A pointer-table entry has no owning function; surface it rather than
        // letting the function look uncalled.
        for (const auto& e : model.fn_edges) {
            if (e.to == self && e.from == 0) {
                ImGui::TextColored(th.warn,
                                   "pointer table entry at 0x%08X (no owning function)",
                                   e.pc);
                break;
            }
        }
        if (seen.empty() && r.in_degree == 0)
            ImGui::TextColored(th.text_muted, "  none proven statically");
    }

    ImGui::Spacing();
    ImGui::TextColored(th.text_muted, "Callees (%u)", r.out_degree);
    {
        std::map<uint32_t, std::string> seen;
        for (const auto& e : model.fn_edges)
            if (e.from == self) seen[e.to] = e.kind;
        int n = 0;
        for (const auto& [to, kind] : seen) {
            char id[24];
            std::snprintf(id, sizeof(id), "ce%d", n);
            std::string suffix = (kind == "direct") ? "" : "  [" + kind + "]";
            link_row(model, th, to, suffix.c_str(), id);
            if (++n >= 60) {
                ImGui::TextColored(th.text_muted, "… %zu more", seen.size() - 60);
                break;
            }
        }
        if (seen.empty()) ImGui::TextColored(th.text_muted, "  none");
    }

    if (r.unresolved_indirect) {
        ImGui::Spacing();
        ImGui::TextColored(th.warn, "%u unresolved indirect transfer(s) here",
                           r.unresolved_indirect);
        ImGui::TextColored(th.text_muted,
                           "The callee list above is therefore incomplete.");
    }

    ImGui::EndChild();
}

} // namespace retcomm::studio
