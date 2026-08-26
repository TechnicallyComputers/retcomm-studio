// studio_analysis.cpp — reading psxrecomp's analysis bundle into the model.
//
// Deliberately free of ImGui so the parse/filter/sort path can be exercised
// headlessly (see tests/analysis_load_test.cpp). Drawing lives in
// studio_functions.cpp.

#include "studio/studio_functions.hpp"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;

namespace retcomm::studio {

namespace {

int confidence_rank(const std::string& c) {
    if (c == "verified") return 0;
    if (c == "high") return 1;
    if (c == "medium") return 2;
    if (c == "data") return 4;
    return 3;
}

std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

bool read_file(const fs::path& p, std::string& out) {
    std::ifstream in(p, std::ios::binary);
    if (!in) return false;
    out.assign(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
    return true;
}

std::string addr_str(uint32_t a) {
    char buf[16];
    std::snprintf(buf, sizeof(buf), "%08X", a);
    return buf;
}

} // namespace

// ---------------------------------------------------------------------------
bool load_analysis(StudioModel& model, const std::string& root) {
    model.fn_rows.clear();
    model.fn_edges.clear();
    model.fn_indirect.clear();
    model.fn_stats = {};
    model.fn_image.clear();
    model.fn_error.clear();
    model.fn_selected = -1;
    model.fn_view_dirty = true;
    model.fn_loaded_root = root;
    // Fresh rows carry live_calls = 0. Invalidate the applied marker or the
    // next sync would decide it had nothing to do and leave the Live column
    // reading zero against a trace that is still running.
    model.fn_live_applied = UINT64_MAX;

    if (root.empty()) {
        model.fn_error = "No repository selected.";
        return false;
    }
    const fs::path dir = fs::path(root) / "analysis";
    std::string text;
    if (!read_file(dir / "analysis.json", text)) {
        model.fn_error = "No analysis yet — press Analyze to run static discovery.";
        return false;
    }

    try {
        auto j = nlohmann::json::parse(text);
        model.fn_image = j.value("image", "");
        const auto& s = j["stats"];
        auto gi = [&](const char* k) { return s.value(k, 0u); };
        model.fn_stats.total = gi("total_functions");
        model.fn_stats.instructions = gi("total_instructions");
        model.fn_stats.bytes_covered = gi("bytes_covered");
        model.fn_stats.bytes_image = gi("bytes_image");
        model.fn_stats.verified = gi("verified");
        model.fn_stats.high = gi("high");
        model.fn_stats.medium = gi("medium");
        model.fn_stats.low = gi("low");
        model.fn_stats.data = gi("data");
        model.fn_stats.reachable = gi("reachable");
        model.fn_stats.orphans = gi("orphans");
        model.fn_stats.named = gi("named");
        model.fn_stats.direct_edges = gi("direct_edges");
        model.fn_stats.tables = gi("jump_tables_resolved");
        model.fn_stats.table_targets = gi("jump_table_targets");
        model.fn_stats.unresolved = gi("indirect_unresolved");
        model.fn_stats.partial_functions = gi("partial_functions");
        model.fn_stats.undecoded_words = gi("undecoded_words");

        model.fn_rows.reserve(j["functions"].size());
        for (const auto& f : j["functions"]) {
            FnRow r;
            r.addr = f.value("addr", 0u);
            r.end = f.value("end", 0u);
            r.size = f.value("size", 0u);
            r.instructions = f.value("instructions", 0u);
            r.name = f.value("name", "");
            r.user_named = f.value("user_named", false);
            r.status = f.value("status", "");
            r.note = f.value("note", "");
            r.confidence = f.value("confidence", "low");
            r.confidence_reason = f.value("confidence_reason", "");
            r.reachable = f.value("reachable", false);
            r.address_taken = f.value("address_taken", false);
            r.partial = f.value("partial", false);
            r.is_data = f.value("is_data", false);
            r.bios_call = f.value("bios_call", "");
            r.in_degree = f.value("in_degree", 0u);
            r.out_degree = f.value("out_degree", 0u);
            r.unresolved_indirect = f.value("unresolved_indirect", 0u);
            r.blocks = f.value("blocks", 0u);
            if (f.contains("sig")) {
                const auto& sg = f["sig"];
                r.args = sg.value("args", 0);
                r.returns = sg.value("returns", false);
                r.leaf = sg.value("leaf", false);
                r.gte = sg.value("gte", false);
                r.syscall = sg.value("syscall", false);
                r.mmio = sg.value("mmio", false);
                r.stack_frame = sg.value("stack_frame", 0);
                r.saved = sg.value("saved", "");
                r.sig_confident = sg.value("confident", true);
                r.prototype = sg.value("prototype", "");
            }
            r.conf_rank = confidence_rank(r.confidence);
            model.fn_rows.push_back(std::move(r));
        }
    } catch (const std::exception& e) {
        model.fn_error = std::string("analysis.json: ") + e.what();
        return false;
    }

    if (read_file(dir / "edges.json", text)) {
        try {
            auto j = nlohmann::json::parse(text);
            model.fn_edges.reserve(j["edges"].size());
            for (const auto& e : j["edges"])
                model.fn_edges.push_back({e.value("from", 0u), e.value("pc", 0u),
                                          e.value("to", 0u), e.value("kind", "")});
        } catch (const std::exception&) {
            // Edges are an enhancement to the detail pane, not a prerequisite:
            // a bad edges.json must not take the whole tab down.
            model.fn_edges.clear();
        }
    }
    model.ws_sites.clear();
    model.ws_w_imms.clear();
    model.ws_h_imms.clear();
    model.ws_extent_funcs.clear();
    model.ws_loaded = false;
    model.ws_toml_path.clear();
    if (read_file(dir / "widescreen_sites.json", text)) {
        try {
            auto j = nlohmann::json::parse(text);
            model.ws_discovered = j.value("imms_discovered", false);
            for (const auto& v : j.value("w_imms", nlohmann::json::array()))
                model.ws_w_imms.push_back(v.get<uint32_t>());
            for (const auto& v : j.value("h_imms", nlohmann::json::array()))
                model.ws_h_imms.push_back(v.get<uint32_t>());
            for (const auto& v : j.value("extent_funcs", nlohmann::json::array()))
                model.ws_extent_funcs.push_back(v.get<uint32_t>());
            for (const auto& c : j.value("candidates", nlohmann::json::array())) {
                WsSite w;
                w.pc = c.value("pc", 0u);
                w.func = c.value("func", 0u);
                w.partner = c.value("partner", 0u);
                w.confidence = c.value("confidence", 0);
                w.kind = c.value("kind", "");
                w.instr = c.value("instr", "");
                w.evidence = c.value("evidence", "");
                model.ws_sites.push_back(std::move(w));
            }
            model.ws_loaded = true;
            const fs::path toml = dir / "widescreen_sites.toml";
            if (fs::exists(toml)) model.ws_toml_path = toml.string();
        } catch (const std::exception&) {
            model.ws_sites.clear();
        }
    }
    if (read_file(dir / "indirect.json", text)) {
        try {
            auto j = nlohmann::json::parse(text);
            for (const auto& s : j["sites"]) {
                FnIndirect ind;
                ind.func = s.value("func", 0u);
                ind.pc = s.value("pc", 0u);
                ind.reg = s.value("reg", 0u);
                ind.kind = s.value("kind", "");
                ind.classification = s.value("classification", "");
                ind.table_base = s.value("table_base", 0u);
                ind.table_count = s.value("table_count", 0u);
                for (const auto& t : s.value("targets", nlohmann::json::array()))
                    ind.targets.push_back(t.get<uint32_t>());
                for (const auto& c : s.value("context", nlohmann::json::array()))
                    ind.context.push_back(c.get<std::string>());
                model.fn_indirect.push_back(std::move(ind));
            }
        } catch (const std::exception&) {
            model.fn_indirect.clear();
        }
    }
    return true;
}

// ---------------------------------------------------------------------------
// Fold the live trace's per-address tally onto the rows. Cheap to call every
// frame: it does nothing until the client has drained a new batch. Returns true
// when the numbers moved, so a Live-sorted view can be rebuilt in step with them.
bool apply_live_counts(StudioModel& model, const std::map<uint32_t, uint64_t>& calls,
                       uint64_t consumed) {
    if (model.fn_live_applied == consumed) return false;
    model.fn_live_applied = consumed;
    for (auto& r : model.fn_rows) {
        const auto it = calls.find(r.addr);
        r.live_calls = it == calls.end() ? 0 : it->second;
    }
    return true;
}

void rebuild_fn_view(StudioModel& model) {
    model.fn_view.clear();
    model.fn_view.reserve(model.fn_rows.size());
    const std::string needle = lower(model.fn_filter);

    for (int i = 0; i < static_cast<int>(model.fn_rows.size()); ++i) {
        const FnRow& r = model.fn_rows[i];
        if (model.fn_hide_data && r.is_data) continue;
        if (model.fn_conf_filter > 0 && r.conf_rank != model.fn_conf_filter - 1) continue;
        if (model.fn_only_named && !r.user_named) continue;
        if (model.fn_only_orphans && r.reachable) continue;
        if (model.fn_only_unresolved && r.unresolved_indirect == 0) continue;
        if (!needle.empty()) {
            // Address matching without the "0x" is what people actually type
            // when they paste a PC out of a crash log or a debugger.
            const std::string hex = lower(addr_str(r.addr));
            if (lower(r.name).find(needle) == std::string::npos &&
                hex.find(needle) == std::string::npos &&
                lower(r.bios_call).find(needle) == std::string::npos &&
                lower(r.note).find(needle) == std::string::npos)
                continue;
        }
        model.fn_view.push_back(i);
    }

    const auto& rows = model.fn_rows;
    const int col = model.fn_sort_col;
    std::stable_sort(model.fn_view.begin(), model.fn_view.end(),
                     [&](int a, int b) {
                         const FnRow& x = rows[a];
                         const FnRow& y = rows[b];
                         bool less;
                         switch (col) {
                         case 1: less = x.name < y.name; break;
                         case 2: less = x.size < y.size; break;
                         case 3: less = x.conf_rank < y.conf_rank; break;
                         case 4: less = x.in_degree < y.in_degree; break;
                         case 5: less = x.live_calls < y.live_calls; break;
                         default: less = x.addr < y.addr; break;
                         }
                         if (col != 0 &&
                             ((col == 1 && x.name == y.name) ||
                              (col == 2 && x.size == y.size) ||
                              (col == 3 && x.conf_rank == y.conf_rank) ||
                              (col == 4 && x.in_degree == y.in_degree) ||
                              (col == 5 && x.live_calls == y.live_calls)))
                             return x.addr < y.addr;
                         return less;
                     });
    if (model.fn_sort_desc)
        std::reverse(model.fn_view.begin(), model.fn_view.end());
    model.fn_view_dirty = false;
}


} // namespace retcomm::studio
