#include "studio/studio_runner.hpp"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <thread>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

namespace retcomm::studio {
namespace {

struct PendingDone {
    DoneFn fn;
    RunResult result;
};

std::mutex g_done_mu;
std::vector<PendingDone> g_done_queue;

fs::path weakly_canonical_path(const fs::path& p) {
    std::error_code ec;
    fs::path c = fs::weakly_canonical(p, ec);
    return (!ec && !c.empty()) ? c : p;
}

bool looks_like_toolkit(const fs::path& dir) {
    std::error_code ec;
    return fs::is_directory(dir / "project_studio", ec) &&
           fs::is_regular_file(dir / "project_studio" / "__main__.py", ec);
}

fs::path find_toolkit_near(const fs::path& start) {
    fs::path walk = start;
    for (int i = 0; i < 8 && !walk.empty(); ++i) {
        const fs::path candidates[] = {
            walk / "toolkit",
            walk / "share" / "retcomm-studio" / "toolkit",
            walk / ".." / "share" / "retcomm-studio" / "toolkit",
            walk / "tools" / "new_project_layout",
            walk,
        };
        for (const auto& c : candidates) {
            if (looks_like_toolkit(c)) return weakly_canonical_path(c);
        }
        walk = walk.parent_path();
    }
    return {};
}

std::string find_system_python() {
#if defined(_WIN32)
    const char* candidates[] = {"python.exe", "python3.exe", "py.exe"};
#else
    const char* candidates[] = {"python3", "python"};
#endif
    for (const char* c : candidates) {
#if defined(_WIN32)
        std::string cmd = std::string("where ") + c + " >nul 2>nul";
        if (std::system(cmd.c_str()) == 0) return c;
#else
        std::string cmd = std::string("command -v ") + c + " >/dev/null 2>&1";
        if (std::system(cmd.c_str()) == 0) return c;
#endif
    }
#if defined(_WIN32)
    return "python";
#else
    return "python3";
#endif
}

fs::path retcomm_data_dir() {
    if (const char* env = std::getenv("RETCOMM_DATA_DIR")) {
        if (*env) return fs::path(env);
    }
#if defined(_WIN32)
    if (const char* local = std::getenv("LOCALAPPDATA")) {
        if (*local) return fs::path(local) / "retcomm";
    }
    if (const char* home = std::getenv("USERPROFILE")) {
        if (*home) return fs::path(home) / "AppData" / "Local" / "retcomm";
    }
#else
    if (const char* xdg = std::getenv("XDG_DATA_HOME")) {
        if (*xdg) return fs::path(xdg) / "retcomm";
    }
    if (const char* home = std::getenv("HOME")) {
        if (*home) return fs::path(home) / ".local" / "share" / "retcomm";
    }
#endif
    return {};
}

fs::path resolve_toolchain_root() {
    if (const char* env = std::getenv("RETCOMM_TOOLCHAIN")) {
        fs::path p(env);
        std::error_code ec;
        if (fs::is_directory(p, ec)) return weakly_canonical_path(p);
    }
    const fs::path data = retcomm_data_dir();
    if (data.empty()) return {};
    const fs::path base = data / "toolchains" / "cmake-clang-v1";
    std::error_code ec;
    const fs::path latest = base / "latest";
    if (fs::exists(latest, ec)) {
        fs::path resolved = weakly_canonical_path(latest);
        if (fs::is_directory(resolved, ec)) return resolved;
    }
    const fs::path path_file = base / "latest.path";
    if (fs::is_regular_file(path_file, ec)) {
        std::FILE* f = std::fopen(path_file.string().c_str(), "r");
        if (f) {
            char buf[4096] = {};
            if (std::fgets(buf, sizeof(buf), f)) {
                std::string line(buf);
                while (!line.empty() && (line.back() == '\n' || line.back() == '\r'))
                    line.pop_back();
                if (!line.empty()) {
                    fs::path p(line);
                    if (fs::is_directory(p, ec)) {
                        std::fclose(f);
                        return weakly_canonical_path(p);
                    }
                }
            }
            std::fclose(f);
        }
    }
    return {};
}

std::string find_toolchain_python() {
    if (const char* env = std::getenv("RETCOMM_PYTHON")) {
        if (*env) {
            std::error_code ec;
            if (fs::is_regular_file(env, ec)) return env;
        }
    }
    const fs::path root = resolve_toolchain_root();
    if (root.empty()) return {};
    const fs::path candidates[] = {
#if defined(_WIN32)
        root / "python" / "python.exe",
        root / "python" / "bin" / "python.exe",
#else
        root / "python" / "bin" / "python3",
        root / "python" / "bin" / "python",
#endif
    };
    std::error_code ec;
    for (const auto& c : candidates) {
        if (fs::is_regular_file(c, ec)) return weakly_canonical_path(c).string();
    }
    return {};
}

std::string find_python() {
    const std::string tc = find_toolchain_python();
    if (!tc.empty()) return tc;
    return find_system_python();
}

void prepend_env_path(const char* key, const fs::path& value) {
#if defined(_WIN32)
    std::string cur;
    char buf[32768];
    DWORD n = GetEnvironmentVariableA(key, buf, sizeof(buf));
    if (n > 0 && n < sizeof(buf)) cur = buf;
    std::string next = value.string();
    if (!cur.empty()) next += ";" + cur;
    SetEnvironmentVariableA(key, next.c_str());
#else
    const char* cur = std::getenv(key);
    std::string next = value.string();
    if (cur && *cur) {
        next += ":";
        next += cur;
    }
    setenv(key, next.c_str(), 1);
#endif
}

#if defined(_WIN32)

RunResult run_process(const std::string& exe, const std::vector<std::string>& args,
                      StudioModel* log_model, bool log_stdout) {
    RunResult out;
    std::string cmdline = "\"" + exe + "\"";
    for (const auto& a : args) {
        cmdline += " \"";
        for (char ch : a) {
            if (ch == '"') cmdline += "\\\"";
            else cmdline += ch;
        }
        cmdline += "\"";
    }

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    HANDLE out_r = nullptr, out_w = nullptr;
    HANDLE err_r = nullptr, err_w = nullptr;
    if (!CreatePipe(&out_r, &out_w, &sa, 0) || !CreatePipe(&err_r, &err_w, &sa, 0)) {
        out.stderr_text = "CreatePipe failed";
        return out;
    }
    SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(err_r, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOA si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = out_w;
    si.hStdError = err_w;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);

    PROCESS_INFORMATION pi{};
    std::vector<char> cmd_mutable(cmdline.begin(), cmdline.end());
    cmd_mutable.push_back('\0');
    BOOL ok = CreateProcessA(nullptr, cmd_mutable.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW,
                             nullptr, nullptr, &si, &pi);
    CloseHandle(out_w);
    CloseHandle(err_w);
    if (!ok) {
        out.stderr_text = "CreateProcess failed";
        CloseHandle(out_r);
        CloseHandle(err_r);
        return out;
    }

    auto read_pipe = [&](HANDLE h, std::string& dest, bool to_log) {
        char buf[4096];
        DWORD n = 0;
        std::string line_acc;
        while (ReadFile(h, buf, sizeof(buf), &n, nullptr) && n > 0) {
            dest.append(buf, buf + n);
            if (!to_log || !log_model) continue;
            line_acc.append(buf, buf + n);
            size_t pos;
            while ((pos = line_acc.find('\n')) != std::string::npos) {
                std::string line = line_acc.substr(0, pos);
                if (!line.empty() && line.back() == '\r') line.pop_back();
                log_model->append_log(line);
                line_acc.erase(0, pos + 1);
            }
        }
        if (to_log && log_model && !line_acc.empty()) log_model->append_log(line_acc);
    };

    // Interleave by draining both (simple sequential is OK for CLI JSON tools).
    read_pipe(out_r, out.stdout_text, log_stdout);
    read_pipe(err_r, out.stderr_text, true);
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    out.exit_code = static_cast<int>(code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    CloseHandle(out_r);
    CloseHandle(err_r);
    return out;
}

#else

RunResult run_process(const std::string& exe, const std::vector<std::string>& args,
                      StudioModel* log_model, bool log_stdout) {
    RunResult out;
    int out_pipe[2];
    int err_pipe[2];
    if (pipe(out_pipe) != 0 || pipe(err_pipe) != 0) {
        out.stderr_text = "pipe failed";
        return out;
    }
    pid_t pid = fork();
    if (pid < 0) {
        out.stderr_text = "fork failed";
        return out;
    }
    if (pid == 0) {
        dup2(out_pipe[1], STDOUT_FILENO);
        dup2(err_pipe[1], STDERR_FILENO);
        close(out_pipe[0]);
        close(out_pipe[1]);
        close(err_pipe[0]);
        close(err_pipe[1]);
        std::vector<char*> argv;
        argv.push_back(const_cast<char*>(exe.c_str()));
        for (const auto& a : args) argv.push_back(const_cast<char*>(a.c_str()));
        argv.push_back(nullptr);
        execvp(exe.c_str(), argv.data());
        std::fprintf(stderr, "execvp failed: %s\n", exe.c_str());
        _exit(127);
    }
    close(out_pipe[1]);
    close(err_pipe[1]);

    auto read_fd = [&](int fd, std::string& dest, bool to_log) {
        char buf[4096];
        std::string line_acc;
        ssize_t n;
        while ((n = read(fd, buf, sizeof(buf))) > 0) {
            dest.append(buf, buf + n);
            if (!to_log || !log_model) continue;
            line_acc.append(buf, buf + n);
            size_t pos;
            while ((pos = line_acc.find('\n')) != std::string::npos) {
                std::string line = line_acc.substr(0, pos);
                if (!line.empty() && line.back() == '\r') line.pop_back();
                log_model->append_log(line);
                line_acc.erase(0, pos + 1);
            }
        }
        if (to_log && log_model && !line_acc.empty()) log_model->append_log(line_acc);
        close(fd);
    };
    read_fd(out_pipe[0], out.stdout_text, log_stdout);
    read_fd(err_pipe[0], out.stderr_text, true);
    int status = 0;
    waitpid(pid, &status, 0);
    if (WIFEXITED(status)) out.exit_code = WEXITSTATUS(status);
    else out.exit_code = 1;
    return out;
}

#endif

} // namespace

bool resolve_runtime(StudioModel& model, std::string* err) {
    if (const char* env = std::getenv("RETCOMM_STUDIO_TOOLKIT")) {
        fs::path p(env);
        if (looks_like_toolkit(p)) {
            model.toolkit_dir = weakly_canonical_path(p);
        }
    }
    if (model.toolkit_dir.empty() && !model.exe_dir.empty()) {
        model.toolkit_dir = find_toolkit_near(model.exe_dir);
    }
    if (model.toolkit_dir.empty()) {
        model.toolkit_dir = find_toolkit_near(fs::current_path());
    }
    if (model.toolkit_dir.empty() || !looks_like_toolkit(model.toolkit_dir)) {
        if (err) *err = "Could not find Project Studio toolkit (tools/new_project_layout).";
        return false;
    }
    model.toolchain_root = resolve_toolchain_root();
    model.python_exe = find_python();
    model.toolchain_ready = !find_toolchain_python().empty();
    // Prefer toolchain bin on PATH for cmake/ninja/ccache when present.
    if (!model.toolchain_root.empty()) {
        prepend_env_path("PATH", model.toolchain_root / "bin");
        prepend_env_path("PATH", model.toolchain_root / "python" / "bin");
    }
    return true;
}

RunResult run_project_studio(StudioModel& model, const std::vector<std::string>& args,
                             bool log_stdout) {
    if (model.toolkit_dir.empty() || model.python_exe.empty()) {
        std::string e;
        if (!resolve_runtime(model, &e)) {
            RunResult r;
            r.stderr_text = e;
            model.append_log("[FAIL] " + e);
            return r;
        }
    }
    prepend_env_path("PYTHONPATH", model.toolkit_dir);
#if defined(_WIN32)
    SetEnvironmentVariableA("PYTHONUTF8", "1");
    SetEnvironmentVariableA("PYTHONIOENCODING", "utf-8");
    SetEnvironmentVariableA("RETCOMM_STUDIO_TOOLKIT", model.toolkit_dir.string().c_str());
#else
    setenv("PYTHONUTF8", "1", 1);
    setenv("PYTHONIOENCODING", "utf-8", 1);
    setenv("RETCOMM_STUDIO_TOOLKIT", model.toolkit_dir.string().c_str(), 1);
#endif

    std::vector<std::string> full = {"-m", "project_studio"};
    full.insert(full.end(), args.begin(), args.end());

    {
        std::string shown = model.python_exe + " -m project_studio";
        for (const auto& a : args) {
            shown += " ";
            shown += a;
        }
        model.append_log("$ " + shown);
    }

    return run_process(model.python_exe, full, &model, log_stdout);
}

void run_project_studio_async(StudioModel& model, std::vector<std::string> args, DoneFn on_done,
                              bool log_stdout) {
    if (model.busy.exchange(true)) {
        model.append_log("[FAIL] Another job is already running.");
        if (on_done) {
            RunResult r;
            r.exit_code = 1;
            r.stderr_text = "busy";
            on_done(std::move(r));
        }
        return;
    }
    std::thread([&model, args = std::move(args), on_done = std::move(on_done), log_stdout]() mutable {
        RunResult r = run_project_studio(model, args, log_stdout);
        model.busy.store(false);
        if (on_done) {
            std::lock_guard<std::mutex> lock(g_done_mu);
            g_done_queue.push_back(PendingDone{std::move(on_done), std::move(r)});
        }
    }).detach();
}

void pump_async_jobs(StudioModel& /*model*/) {
    std::vector<PendingDone> local;
    {
        std::lock_guard<std::mutex> lock(g_done_mu);
        local.swap(g_done_queue);
    }
    for (auto& p : local) {
        if (p.fn) p.fn(std::move(p.result));
    }
}

bool load_repos_from_json(StudioModel& model, const std::string& json_text, std::string* err) {
    try {
        auto j = nlohmann::json::parse(json_text);
        std::lock_guard<std::mutex> lock(model.mu);
        model.repos.clear();
        model.catalog_only = j.value("catalog_only", false);
        model.bulk_jobs = j.value("bulk_jobs", 2);
        model.log_height = j.value("log_height", 160);
        model.log_h_pref = static_cast<float>(std::max(100, model.log_height));
        const std::string last = j.value("last", "");
        for (const auto& r : j.at("repos")) {
            RepoEntry e;
            e.path = r.value("path", "");
            e.name = r.value("name", "");
            e.cue = r.value("cue", "");
            e.label = r.value("label", "");
            e.in_catalog = r.value("in_catalog", false);
            e.players = r.value("players", 2);
            if (e.players < 1) e.players = 1;
            if (e.players > 8) e.players = 8;
            if (e.label.empty()) {
                e.label = e.name.empty() ? fs::path(e.path).filename().string() : e.name;
            }
            if (!e.path.empty()) model.repos.push_back(std::move(e));
        }
        if (!last.empty()) model.select_repo_by_path(last);
        else if (!model.repos.empty() && model.selected_repo < 0) model.selected_repo = 0;
        // Seed bulk selection
        for (const auto& e : model.repos) {
            if (!model.bulk_selected.count(e.path)) model.bulk_selected[e.path] = true;
        }
        if (model.selected_repo >= 0 &&
            model.selected_repo < static_cast<int>(model.repos.size())) {
            const auto& sel = model.repos[static_cast<size_t>(model.selected_repo)];
            std::snprintf(model.disc_cue, sizeof(model.disc_cue), "%s", sel.cue.c_str());
            model.apply_selected_players();
        }
        return true;
    } catch (const std::exception& ex) {
        if (err) *err = ex.what();
        return false;
    }
}

bool load_audit_from_json(StudioModel& model, const std::string& json_text, std::string* err) {
    try {
        auto j = nlohmann::json::parse(json_text);
        std::lock_guard<std::mutex> lock(model.mu);
        model.audit_checks.clear();
        model.audit_layout = j.value("layout", "");
        model.audit_boot = j.value("boot_exe", "");
        if (model.audit_boot == "null") model.audit_boot.clear();
        for (const auto& c : j.at("checks")) {
            AuditCheck a;
            a.id = c.value("id", "");
            a.title = c.value("title", "");
            a.status = c.value("status", "");
            a.detail = c.value("detail", "");
            if (c.contains("fix_op") && !c["fix_op"].is_null())
                a.fix_op = c.value("fix_op", "");
            model.audit_checks.push_back(std::move(a));
        }
        return true;
    } catch (const std::exception& ex) {
        if (err) *err = ex.what();
        return false;
    }
}

bool load_plan_from_json(StudioModel& model, const std::string& json_text, std::string* err) {
    try {
        auto j = nlohmann::json::parse(json_text);
        std::lock_guard<std::mutex> lock(model.mu);
        model.plan_steps.clear();
        for (const auto& s : j.at("steps")) {
            PlanStep p;
            p.op_id = s.value("op_id", "");
            p.title = s.value("title", "");
            p.detail = s.value("detail", "");
            p.selected = s.value("selected", true);
            model.plan_steps.push_back(std::move(p));
        }
        return true;
    } catch (const std::exception& ex) {
        if (err) *err = ex.what();
        return false;
    }
}

} // namespace retcomm::studio
