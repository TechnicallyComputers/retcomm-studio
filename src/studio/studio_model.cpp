#include "studio/studio_model.hpp"

#include <cstdint>
#include <cstdio>

namespace retcomm::studio {

void StudioModel::append_log(std::string line) {
    std::lock_guard<std::mutex> lock(mu);
    log_lines.push_back(std::move(line));
    if (log_lines.size() > kMaxLogLines) {
        const size_t drop = log_lines.size() - kMaxLogLines;
        log_lines.erase(log_lines.begin(), log_lines.begin() + static_cast<std::ptrdiff_t>(drop));
    }
    ++log_revision;
    log_scroll_bottom = true;
}

void StudioModel::set_status(std::string s) {
    std::lock_guard<std::mutex> lock(mu);
    status = std::move(s);
}

std::string StudioModel::selected_root() const {
    if (selected_repo < 0 || selected_repo >= static_cast<int>(repos.size())) return {};
    return repos[static_cast<size_t>(selected_repo)].path;
}

void StudioModel::select_repo_by_path(const std::string& path) {
    for (int i = 0; i < static_cast<int>(repos.size()); ++i) {
        if (repos[static_cast<size_t>(i)].path == path) {
            selected_repo = i;
            return;
        }
    }
}

void StudioModel::coerce_catalog_only_selection() {
    if (!catalog_only) return;
    const bool ok = selected_repo >= 0 && selected_repo < static_cast<int>(repos.size()) &&
                    repos[static_cast<size_t>(selected_repo)].in_catalog;
    if (ok) return;
    for (int i = 0; i < static_cast<int>(repos.size()); ++i) {
        if (!repos[static_cast<size_t>(i)].in_catalog) continue;
        selected_repo = i;
        const auto& e = repos[static_cast<size_t>(i)];
        std::snprintf(disc_cue, sizeof(disc_cue), "%s", e.cue.c_str());
        apply_selected_players();
        return;
    }
}

void StudioModel::reset_for_platform_switch() {
    // Everything here is keyed to one console's repos. Leaving any of it
    // behind means the first SNES frame shows a PSX path, a PSX branch list,
    // or an audit of a repo that is no longer selectable.
    repos.clear();
    selected_repo = -1;
    disc_cue[0] = '\0';
    build_exe[0] = '\0';
    git_branch[0] = '\0';
    branches_game.clear();
    branches_psx.clear();
    branches_ui.clear();
    branches_net.clear();
    branches_rb.clear();
    branches_root.clear();
    branches_loading = false;
    audit_checks.clear();
    plan_steps.clear();
    bulk_selected.clear();
    git_summary.clear();
    // Functions / Frames data belongs to a specific PSX executable.
    fn_rows.clear();
    fn_edges.clear();
    fn_indirect.clear();
    fn_view.clear();
    fn_view_dirty = true;
    fn_selected = -1;
    fn_loaded_root.clear();
    fn_error.clear();
}

void StudioModel::apply_platform_defaults() {
    // New Project defaults that are the SCAFFOLDER's, not Studio's, so what the
    // page offers matches what a terminal run of that wizard would have taken.
    //
    // "Copy ROM" is --copy-rom on N64, and n64lle's setup_project.sh
    // deliberately SYMLINKS the dump into roms/ instead: "a per-game repo must
    // never hold ROM bytes, and a link makes that impossible to do by
    // accident." --copy-rom is for a dump on removable media. Carrying the PSX
    // habit of staging the image would quietly copy an 8-64 MiB cartridge into
    // every new repo, against the framework's own stated default.
    //
    // Called from the picker rather than reset_for_platform_switch(), which
    // runs with `platform` already cleared to None and so has no console to
    // read a default from.
    np_stage = !platform_is_cartridge(platform);

    // The Env box is a prompt as much as a field, so its example has to name
    // variables that exist on THIS console. Replaced only while the box is
    // still all-comment — the moment anything real is typed, or a repo's saved
    // env is loaded over it, the first character is no longer '#' and this
    // leaves it alone.
    bool env_is_placeholder = true;
    for (const char* c = build_env; *c; ++c) {
        if (*c == '\n' || *c == ' ' || *c == '\t') continue;
        if (*c == '#') {                       // skip to the end of the line
            while (*c && *c != '\n') ++c;
            if (!*c) break;
            continue;
        }
        env_is_placeholder = false;
        break;
    }
    if (env_is_placeholder) {
        // N64_RSP_EXEC is the one that changes what a run MEANS here: with RSP
        // execution off no graphics task runs, every frame is blank, and a
        // harvest silently under-covers by ~69%. game.toml
        // [runtime].rsp_execution already turns it on; naming it here is about
        // the override, not about setting it.
        std::snprintf(build_env, sizeof(build_env), "%s",
                      platform == Platform::N64
                          ? "# KEY=VALUE pairs (space or newline separated)\n"
                            "# Example:\n"
                            "# N64_RSP_EXEC=1 N64_DEBUG_PORT=0\n"
                          : "# KEY=VALUE pairs (space or newline separated)\n"
                            "# Example:\n"
                            "# RBE_CROSS_OS_PACING_DIAG=1 PSX_RB_ZERO_DELAY=0\n");
    }
}

void StudioModel::apply_selected_players() {
    if (selected_repo < 0 || selected_repo >= static_cast<int>(repos.size())) return;
    int n = repos[static_cast<size_t>(selected_repo)].players;
    if (n < 1) n = 1;
    if (n > 8) n = 8;
    players = n;
    if (players < 2) migrate_netplay = false;
}

} // namespace retcomm::studio
