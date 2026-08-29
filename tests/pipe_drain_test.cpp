// pipe_drain_test.cpp — a child's two pipes must be drained concurrently.
//
// Studio used to read a child's stdout to EOF and only then read its stderr.
// That fails two ways, and this test pins both:
//
//   1. DEADLOCK. Once the child writes more than one pipe buffer (64 KiB on
//      Linux) to stderr, it blocks in write(2) while Studio blocks in read(1).
//      Neither ever moves. A cross-build is well past that.
//   2. SILENCE. Even under the threshold, no stderr line reaches the Activity
//      log until the child exits -- so a build that reports progress on stderr
//      (every cmake/ninja line does, the way Studio invokes them) shows an
//      empty log for its whole run and is indistinguishable from a hang. That
//      is what a MinGW cross-build looked like: one "+ bash …" line, then
//      nothing.
//
// Both are properties of the drain, not of any one tool, so this drives the
// real run_python_script_async path with a script whose output shape is chosen
// to expose them.
#include "studio/studio_runner.hpp"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <thread>

namespace fs = std::filesystem;
using namespace retcomm::studio;
using clk = std::chrono::steady_clock;

int failures = 0;
void check(bool ok, const char* what) {
    std::printf("  %s  %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++failures;
}

// Pump the callback queue until `done`, or give up. The timeout IS the
// deadlock assertion: the old drain never returns here.
bool pump_until(StudioModel& model, std::atomic<bool>& done, int timeout_ms) {
    const auto deadline = clk::now() + std::chrono::milliseconds(timeout_ms);
    while (!done.load() && clk::now() < deadline) {
        pump_async_jobs(model);
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    pump_async_jobs(model);
    return done.load();
}

size_t log_size(StudioModel& model) {
    std::lock_guard<std::mutex> lock(model.mu);
    return model.log_lines.size();
}

bool log_has(StudioModel& model, const char* needle) {
    std::lock_guard<std::mutex> lock(model.mu);
    for (const auto& l : model.log_lines)
        if (l.find(needle) != std::string::npos) return true;
    return false;
}

int main(int argc, char** argv) {
    const std::string python = argc > 1 ? argv[1] : "python3";
    const fs::path dir = fs::temp_directory_path() / "retcomm_pipe_drain_test";
    fs::remove_all(dir);
    fs::create_directories(dir);

    StudioModel model;
    model.python_exe = python;

    // ---- 1. a child that floods stderr must not wedge -------------------------
    // 2 MB is ~32x the pipe buffer: under the old drain this never completes.
    // stdout stays nearly empty on purpose -- that is the shape of the MinGW
    // script, which sends progress to stderr and only the final MINGW_ZIP= line
    // to stdout.
    const fs::path flood = dir / "flood.py";
    {
        std::ofstream f(flood);
        f << "import sys\n"
             "line = 'x' * 79 + '\\n'\n"
             "for _ in range(26000):\n"
             "    sys.stderr.write(line)\n"
             "sys.stderr.flush()\n"
             "sys.stdout.write('MINGW_ZIP=/tmp/done.zip\\n')\n";
    }
    std::atomic<bool> done{false};
    RunResult got;
    run_python_script_async(model, flood.string(), {},
                            [&](RunResult r) { got = std::move(r); done.store(true); });
    const bool finished = pump_until(model, done, 30000);
    check(finished, "2 MB of stderr does not deadlock the drain");
    if (finished) {
        check(got.exit_code == 0, "the flooding child exits 0");
        check(got.stderr_text.size() >= 2000000, "all of its stderr is captured");
        check(got.stdout_text.find("MINGW_ZIP=") != std::string::npos,
              "its stdout is captured too");
    }

    // ---- 2. stderr must reach the log DURING the run, not after --------------
    // The child speaks on stderr, then stays alive. A drain that serves stdout
    // first holds the line back for the full sleep; the log stays empty and the
    // build looks frozen.
    const fs::path slow = dir / "slow.py";
    {
        std::ofstream f(slow);
        f << "import sys, time\n"
             "sys.stderr.write('[1/60] Building C object early.c.obj\\n')\n"
             "sys.stderr.flush()\n"
             "time.sleep(4)\n"
             "sys.stderr.write('[60/60] Linking\\n')\n";
    }
    {
        std::lock_guard<std::mutex> lock(model.mu);
        model.log_lines.clear();
    }
    std::atomic<bool> done2{false};
    run_python_script_async(model, slow.string(), {},
                            [&](RunResult) { done2.store(true); });

    // Give it well under the child's lifetime to speak.
    const auto t0 = clk::now();
    bool streamed_early = false;
    while (clk::now() - t0 < std::chrono::milliseconds(2000)) {
        pump_async_jobs(model);
        if (log_has(model, "[1/60]")) { streamed_early = true; break; }
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    check(streamed_early, "an early stderr line reaches the log while the child still runs");
    check(!done2.load(), "…and it did so before the child exited");
    check(pump_until(model, done2, 20000), "the slow child completes");
    check(log_size(model) >= 2, "the closing stderr line lands too");

    fs::remove_all(dir);
    std::printf("%s\n", failures ? "FAILED" : "PASSED");
    return failures ? 1 : 0;
}
