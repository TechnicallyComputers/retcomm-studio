#include "studio/studio_model.hpp"

#include <cstdio>

namespace retcomm::studio {

void StudioModel::append_log(std::string line) {
    std::lock_guard<std::mutex> lock(mu);
    log_lines.push_back(std::move(line));
    if (log_lines.size() > kMaxLogLines) {
        const size_t drop = log_lines.size() - kMaxLogLines;
        log_lines.erase(log_lines.begin(), log_lines.begin() + static_cast<std::ptrdiff_t>(drop));
    }
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

void StudioModel::apply_selected_players() {
    if (selected_repo < 0 || selected_repo >= static_cast<int>(repos.size())) return;
    int n = repos[static_cast<size_t>(selected_repo)].players;
    if (n < 1) n = 1;
    if (n > 8) n = 8;
    players = n;
    if (players < 2) migrate_netplay = false;
}

} // namespace retcomm::studio
