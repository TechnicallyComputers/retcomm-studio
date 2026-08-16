#pragma once

#include "studio/studio_model.hpp"

#include <functional>
#include <string>
#include <vector>

namespace retcomm::studio {

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

// Drain completed async jobs (call once per frame on UI thread).
void pump_async_jobs(StudioModel& model);

// Parse helpers (nlohmann JSON inside .cpp)
bool load_repos_from_json(StudioModel& model, const std::string& json_text, std::string* err);
bool load_audit_from_json(StudioModel& model, const std::string& json_text, std::string* err);
bool load_plan_from_json(StudioModel& model, const std::string& json_text, std::string* err);

} // namespace retcomm::studio
