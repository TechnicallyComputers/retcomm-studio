#pragma once

#include "studio/studio_model.hpp"

#include <functional>
#include <string>
#include <vector>

namespace retcomm::studio {

// Which lock a job takes. See StudioModel::busy / busy_global.
enum class JobSlot {
    Project,  // scoped to the selected repo — serialised against other repo work
    Frames,   // observing a running game (capture / diff / layers)
    Global,   // machine-wide toolbox work — runs alongside project jobs
};

struct RunResult {
    int exit_code = -1;
    std::string stdout_text;
    std::string stderr_text;
    bool ok() const { return exit_code == 0; }
};

// Resolve toolkit dir + python executable into model.
bool resolve_runtime(StudioModel& model, std::string* err);

// Synchronous subprocess: python -m project_studio <args…>
// Streams lines to model.append_log while collecting full stdout/stderr.
RunResult run_project_studio(StudioModel& model, const std::vector<std::string>& args,
                             bool log_stdout = true);

// Background helper: sets busy, runs, clears busy, optional on_done on caller thread
// via a pending callback queue drained each frame.
using DoneFn = std::function<void(RunResult)>;

void run_project_studio_async(StudioModel& model, std::vector<std::string> args, DoneFn on_done,
                              bool log_stdout = true, bool allow_when_busy = false);

// Run a standalone python script (absolute path) with args.
// The Frames tab drives psxrecomp/tools/gpu_*.py this way: those tools belong
// to the engine that defines the debug protocol, not to Studio, so Studio
// shells out to whatever the selected project's checkout actually contains
// rather than shipping a second copy that can drift. Pass absolute paths --
// the child inherits Studio's working directory and nothing chdir()s, because
// a process-wide cwd change would race every other thread in the app.
// `requires` names python modules the script imports. Studio's default
// interpreter is the pinned toolchain one, which deliberately carries no
// third-party packages — so a tool needing numpy/Pillow would die with
// ModuleNotFoundError no matter how correct it is. When `requires` is given,
// the first interpreter that can import all of them is used instead, and if
// none can, the job fails with a message naming the missing modules rather
// than a traceback nobody sees.
void run_python_script_async(StudioModel& model, std::string script,
                             std::vector<std::string> args, DoneFn on_done,
                             bool log_stdout = true,
                             JobSlot slot = JobSlot::Project,
                             std::vector<std::string> requires_modules = {});

// Spawn a process detached, with stdout+stderr redirected to `logfile`.
//
// Takes NO job slot and streams nothing into the Activity log. That is the
// point: the game runs for as long as you play it, and a launcher that holds a
// lock for that whole time disables every control that only makes sense WHILE a
// game is running — which is how Studio ended up greying out Capture at exactly
// the moment capture would have worked.
//
// Returns the pid, or 0 with `err` set.
long spawn_detached_logged(const std::string& exe, const std::vector<std::string>& args,
                           const std::string& cwd, const std::string& logfile,
                           const std::vector<std::pair<std::string, std::string>>& env,
                           std::string* err);

// True if `pid` names a process that is still alive.
bool process_alive(long pid);

// Ask `pid` to exit (SIGTERM / TerminateProcess).
bool process_stop(long pid);

// Which interpreter can import all of `modules`, or "" if none can.
// Results are cached: probing costs a process launch each.
std::string python_with_modules(StudioModel& model,
                                const std::vector<std::string>& modules);

// Drain completed async jobs (call once per frame on UI thread).
void pump_async_jobs(StudioModel& model);

// Parse helpers (nlohmann JSON inside .cpp)
bool load_repos_from_json(StudioModel& model, const std::string& json_text, std::string* err);
bool load_audit_from_json(StudioModel& model, const std::string& json_text, std::string* err);
bool load_plan_from_json(StudioModel& model, const std::string& json_text, std::string* err);

} // namespace retcomm::studio
