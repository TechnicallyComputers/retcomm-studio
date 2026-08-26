// analysis_load_test.cpp — headless check of the Functions tab's data path.
//
// The tab itself needs a GPU and a window; the parse/filter/sort logic does
// not, and that is where the bugs live. This drives load_analysis() and
// rebuild_fn_view() against a fixture bundle and asserts on the results.
//
// Usage: analysis_load_test <repo-root-containing-analysis/>

#include "studio/studio_functions.hpp"

#include <map>

#include <cstdio>
#include <cstring>
#include <string>

using namespace retcomm::studio;

namespace {

int failures = 0;

void check(bool cond, const char* what) {
    if (!cond) {
        std::printf("  FAIL  %s\n", what);
        ++failures;
    } else {
        std::printf("  ok    %s\n", what);
    }
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::printf("usage: analysis_load_test <repo-root>\n");
        return 2;
    }
    const std::string root = argv[1];

    StudioModel model;
    const bool loaded = load_analysis(model, root);
    if (!loaded) {
        std::printf("load_analysis failed: %s\n", model.fn_error.c_str());
        return 1;
    }

    std::printf("image=%s functions=%zu edges=%zu indirect=%zu\n",
                model.fn_image.c_str(), model.fn_rows.size(),
                model.fn_edges.size(), model.fn_indirect.size());

    check(!model.fn_rows.empty(), "functions parsed");
    check(model.fn_stats.total == model.fn_rows.size(),
          "stats.total_functions matches the parsed row count");
    check(!model.fn_edges.empty(), "edges parsed");
    check(!model.fn_indirect.empty(), "indirect sites parsed");

    // Confidence buckets in stats must agree with the per-row ranks, otherwise
    // the banner and the table would tell different stories.
    unsigned counts[5] = {0, 0, 0, 0, 0};
    for (const auto& r : model.fn_rows) counts[r.conf_rank]++;
    check(counts[0] == model.fn_stats.verified, "verified count agrees");
    check(counts[1] == model.fn_stats.high, "high count agrees");
    check(counts[2] == model.fn_stats.medium, "medium count agrees");
    check(counts[3] == model.fn_stats.low, "low count agrees");
    check(counts[4] == model.fn_stats.data, "data count agrees");

    // Default view: data hidden, sorted by address.
    model.fn_hide_data = true;
    rebuild_fn_view(model);
    const size_t shown = model.fn_view.size();
    check(shown == model.fn_rows.size() - counts[4], "hide-data drops exactly the data rows");
    bool ascending = true;
    for (size_t i = 1; i < model.fn_view.size(); ++i)
        if (model.fn_rows[model.fn_view[i - 1]].addr >=
            model.fn_rows[model.fn_view[i]].addr)
            ascending = false;
    check(ascending, "default sort is ascending by address");

    // Sort by callers, descending — the "what should I name first" ordering.
    model.fn_sort_col = 4;
    model.fn_sort_desc = true;
    rebuild_fn_view(model);
    bool desc = true;
    for (size_t i = 1; i < model.fn_view.size(); ++i)
        if (model.fn_rows[model.fn_view[i - 1]].in_degree <
            model.fn_rows[model.fn_view[i]].in_degree)
            desc = false;
    check(desc, "caller-count sort is monotonically descending");
    if (!model.fn_view.empty()) {
        const auto& top = model.fn_rows[model.fn_view[0]];
        std::printf("  most-called: 0x%08X %s (%u callers)\n", top.addr,
                    top.name.c_str(), top.in_degree);
    }

    // Address filter without the 0x prefix — how a PC gets pasted in practice.
    // ---- Live column ------------------------------------------------------
    // The observed-call tally is a sort key like any other, and it exists
    // before any game runs: every row reads zero until a trace is armed.
    {
        std::map<uint32_t, uint64_t> calls;
        check(apply_live_counts(model, calls, 0), "first sync applies even when empty");
        check(!apply_live_counts(model, calls, 0),
              "a sync with no new entries does nothing");
        bool all_zero = true;
        for (const auto& r : model.fn_rows)
            if (r.live_calls != 0) all_zero = false;
        check(all_zero, "with no trace, every row reads zero rather than being absent");

        // Three arbitrary rows, deliberately not the most-called statically, so
        // a Live sort cannot accidentally agree with the caller-count sort.
        const size_t n = model.fn_rows.size();
        check(n >= 3, "bundle has enough rows to test the ordering");
        const uint32_t a0 = model.fn_rows[n / 2].addr;
        const uint32_t a1 = model.fn_rows[n / 3].addr;
        const uint32_t a2 = model.fn_rows[n / 5].addr;
        calls[a0] = 7;
        calls[a1] = 4812;
        calls[a2] = 31;
        check(apply_live_counts(model, calls, 3), "a new batch is folded onto the rows");

        model.fn_conf_filter = 0;
        model.fn_hide_data = false;
        model.fn_sort_col = 5;
        model.fn_sort_desc = true;
        model.fn_view_dirty = true;
        rebuild_fn_view(model);
        bool desc = true;
        for (size_t i = 1; i < model.fn_view.size(); ++i)
            if (model.fn_rows[model.fn_view[i - 1]].live_calls <
                model.fn_rows[model.fn_view[i]].live_calls)
                desc = false;
        check(desc, "live-count sort is monotonically descending");
        check(!model.fn_view.empty() && model.fn_rows[model.fn_view[0]].addr == a1,
              "the most-executed function sorts to the top");

        model.fn_sort_desc = false;
        model.fn_view_dirty = true;
        rebuild_fn_view(model);
        check(!model.fn_view.empty() && model.fn_rows[model.fn_view.back()].addr == a1,
              "ascending puts it at the bottom");
        // Untraced rows all tie at zero; the address tiebreak keeps the order
        // stable instead of shuffling every rebuild.
        bool tie_ordered = true;
        for (size_t i = 1; i < model.fn_view.size(); ++i) {
            const auto& x = model.fn_rows[model.fn_view[i - 1]];
            const auto& y = model.fn_rows[model.fn_view[i]];
            if (x.live_calls == 0 && y.live_calls == 0 && x.addr >= y.addr)
                tie_ordered = false;
        }
        check(tie_ordered, "rows tied at zero stay ordered by address");

        // A reload wipes the counts; the marker must not claim they are current.
        load_analysis(model, root);
        check(model.fn_live_applied == UINT64_MAX,
              "reloading the bundle invalidates the applied marker");
        check(apply_live_counts(model, calls, 3),
              "so the next sync re-applies onto the fresh rows");
        rebuild_fn_view(model);
    }

    model.fn_hide_data = true;
    model.fn_sort_col = 0;
    model.fn_sort_desc = false;
    if (!model.fn_rows.empty()) {
        char hex[16];
        std::snprintf(hex, sizeof(hex), "%08X", model.fn_rows.front().addr);
        std::snprintf(model.fn_filter, sizeof(model.fn_filter), "%s", hex);
        rebuild_fn_view(model);
        check(model.fn_view.size() == 1 &&
                  model.fn_rows[model.fn_view[0]].addr == model.fn_rows.front().addr,
              "bare-hex address filter finds exactly that function");
        model.fn_filter[0] = '\0';
    }

    // Orphan filter must be the complement of reachable.
    model.fn_only_orphans = true;
    rebuild_fn_view(model);
    bool all_orphans = true;
    for (int i : model.fn_view)
        if (model.fn_rows[i].reachable) all_orphans = false;
    check(all_orphans, "orphan filter yields only unreachable functions");
    model.fn_only_orphans = false;

    // Every recovered jump table must name at least two targets; a one-entry
    // "table" would mean the recoverer accepted noise.
    size_t tables = 0;
    bool tables_sane = true;
    for (const auto& s : model.fn_indirect) {
        if (s.classification.rfind("jump_table", 0) != 0) continue;
        ++tables;
        if (s.targets.size() < 2 || s.table_base == 0) tables_sane = false;
    }
    std::printf("  jump tables: %zu\n", tables);
    check(tables_sane, "every recovered jump table has a base and >= 2 targets");

    // Edge endpoints must resolve, except pointer-table edges (from == 0) and
    // calls that leave the image (BIOS vectors).
    size_t dangling_from = 0;
    for (const auto& e : model.fn_edges) {
        if (e.from == 0) continue;
        bool found = false;
        for (const auto& r : model.fn_rows)
            if (r.addr == e.from) { found = true; break; }
        if (!found) ++dangling_from;
    }
    check(dangling_from == 0, "every edge source resolves to a known function");

    std::printf("%s\n", failures ? "FAILED" : "PASSED");
    return failures ? 1 : 0;
}
