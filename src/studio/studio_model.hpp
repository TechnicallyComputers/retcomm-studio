#pragma once

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <map>
#include <mutex>
#include <string>
#include <vector>

namespace retcomm::studio {

namespace fs = std::filesystem;

struct RepoEntry {
    std::string path;
    std::string name;
    std::string cue;
    std::string label;
    bool in_catalog = false;
    int players = 2; // from game.toml / CMakeLists MAX_PLAYERS
};

struct AuditCheck {
    std::string id;
    std::string title;
    std::string status; // pass|fail|warn|skip
    std::string detail;
    std::string fix_op;
};

struct PlanStep {
    std::string op_id;
    std::string title;
    std::string detail;
    bool selected = true;
};

struct StudioModel {
    std::mutex mu;

    fs::path exe_dir;
    fs::path toolkit_dir;
    fs::path toolchain_root;
    std::string python_exe;
    bool toolchain_ready = false;
    bool toolchain_gate_open = false;
    bool update_prompt_open = false;
    bool startup_update_started = false;
    std::string update_prompt_msg;
    bool update_studio_avail = false;
    bool update_toolchain_avail = false;
    std::string status = "Ready";
    std::string version;

    // Header / index
    std::vector<RepoEntry> repos;
    int selected_repo = -1;
    bool catalog_only = false;
    int bulk_jobs = 2;
    int log_height = 160;
    char disc_cue[1024] = {};
    char zip_prefix[128] = {};
    char github_owner[128] = {};
    char github_repo[128] = {};
    int players = 2;
    bool migrate_netplay = false;
    bool migrate_ci = true;
    bool migrate_probe = false;
    bool migrate_dry_run = true;
    bool migrate_force = false;

    std::vector<AuditCheck> audit_checks;
    std::string audit_layout;
    std::string audit_boot;
    std::vector<PlanStep> plan_steps;

    // New project
    char np_name[256] = {};
    char np_parent[1024] = {};
    char np_disc[1024] = {};
    char np_bios[1024] = {};
    char np_zip[128] = {};
    char np_desc[512] = {};
    char np_publisher[256] = {};
    char np_year[32] = {};
    char np_region[64] = "USA";
    char np_lobby[256] = "netplay.retcomm.net";
    char np_psx_ref[128] = "master";
    char np_ui_ref[128] = "master";
    char np_net_ref[128] = "(default)";
    char np_rb_ref[128] = "(default)";
    int np_players = 2;
    bool np_ui = true;
    bool np_wizard = true;
    bool np_netplay = true;
    bool np_ci = true;
    bool np_boxart = true;
    bool np_stage = true;
    bool np_generate = true;
    bool np_build = true;
    bool np_github = false;
    char np_gh_vis[32] = "private";
    char np_gh_owner[128] = "TechnicallyComputers";
    char np_gh_repo[256] = {};

    // Git
    std::string git_summary;
    char git_branch[256] = {};
    char git_psx_branch[128] = "master";
    char git_ui_branch[128] = "master";
    char git_net_branch[128] = "main";
    char git_rb_branch[128] = "main";
    char git_msg[512] = {};
    char git_sub_msg[256] = "chore: update submodule";
    bool git_remote_update = false;
    bool git_create_branch = false;
    // Scope filters for Switch / Pull / Commit / Push (like Bulk targets).
    bool git_tgt_game = true;
    bool git_tgt_modules = false;
    bool git_tgt_nested = false;
    // Populated by `git branches --json` (game + modules / defaults).
    std::vector<std::string> branches_game;
    std::vector<std::string> branches_psx;
    std::vector<std::string> branches_ui;
    std::vector<std::string> branches_net;
    std::vector<std::string> branches_rb;
    std::string branches_root; // root these lists were fetched for
    bool branches_loading = false;
    int git_pull_mode = 0; // 0=ff-only 1=rebase 2=merge 3=reset
    char release_version[64] = {};
    int release_bump = 0; // 0=patch 1=minor 2=major
    bool release_publish = true;

    // Bulk
    std::map<std::string, bool> bulk_selected;
    char bulk_msg[256] = "chore: sync";
    bool bulk_tgt_game = true;
    bool bulk_tgt_modules = false;
    bool bulk_tgt_psx = false;
    bool bulk_tgt_nested = false;
    char bulk_game_branch[128] = "(default)";
    char bulk_psx_branch[128] = "(default)";
    char bulk_ui_branch[128] = "(default)";
    char bulk_net_branch[128] = "(default)";
    char bulk_rb_branch[128] = "(default)";
    bool bulk_create_branch = false;
    bool bulk_set_tracking = true;
    bool bulk_reuse_emitters = true;
    int bulk_pull_mode = 0; // 0=ff-only 1=rebase 2=merge 3=reset

    // Build
    char build_dir[256] = "build-release";
    char build_type[64] = "Release";
    char build_target[128] = "psx-runtime";
    char build_generator[128] = {};
    char build_jobs[32] = {};
    char build_extra[512] = {};
    char build_exe[1024] = {};
    char build_launch_args[512] = {};
    char build_env[4096] =
        "# KEY=VALUE pairs (space or newline separated)\n"
        "# Example:\n"
        "# RBE_CROSS_OS_PACING_DIAG=1 PSX_RB_ZERO_DELAY=0\n";

    // Windows MinGW cross-build (Build tab — Linux host only)
    char mingw_build_dir[256] = "build-mingw";
    bool mingw_setup_host = false;
    bool mingw_package = false;
    bool mingw_ensure = false;
    bool mingw_dynamic = false;
    // Source zip path for Bundle+Export save dialog (set after package-only).
    std::string mingw_export_src;

    // Generate ROM + BIOS C dialog (Build tab)
    bool gen_popup_open = false;
    int gen_bios_mode = 0; // 0=OpenBIOS 1=SCPH1001 dump
    char gen_scph_path[1024] = {};

    // Log (ring)
    static constexpr size_t kMaxLogLines = 4000;
    std::vector<std::string> log_lines;
    bool log_scroll_bottom = true;
    bool log_expanded = true;
    float log_h_pref = 160.f;

    std::atomic<bool> busy{false};
    std::atomic<bool> request_exit{false};

    // Pending file/folder picks from SDL dialogs (applied on main thread).
    std::mutex pick_mu;
    std::string pending_folder;
    std::string pending_file;
    std::string pending_pick_target; // disc|np_parent|np_disc|np_bios|repo_add|build_exe|export_log|build_scph|export_mingw_zip
    bool file_pick_busy = false;

    void append_log(std::string line);
    void set_status(std::string s);
    std::string selected_root() const;
    void select_repo_by_path(const std::string& path);
    // When Catalog only is on, ensure selected_repo is a catalog-backed entry.
    void coerce_catalog_only_selection();
    // Apply detected player count from the selected repo to the Migrate UI.
    void apply_selected_players();
};

} // namespace retcomm::studio
