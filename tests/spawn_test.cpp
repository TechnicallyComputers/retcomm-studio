// spawn_test.cpp — the Frames tab's Launch path.
//
// A game runs for as long as you play it. Studio's Build-tab Launch streams its
// stdout through a job slot held for that entire time, which is why Capture was
// greyed out at exactly the moment capture would have worked. The Frames tab
// launches detached instead, writing to frames.log and taking no slot — so this
// checks the properties that behaviour depends on: it really detaches, output
// really lands in the file, and liveness/stop really track the process.
#include "studio/studio_runner.hpp"
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <thread>
#include <chrono>
namespace fs = std::filesystem;
using namespace retcomm::studio;
int failures = 0;
void check(bool ok, const char* what) {
    std::printf("  %s  %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++failures;
}
int main(int argc, char** argv) {
    const std::string exe = argc > 1 ? argv[1] : "/bin/sh";
    const fs::path dir = fs::temp_directory_path() / "retcomm_spawn_test";
    fs::remove_all(dir); fs::create_directories(dir);
    const std::string log = (dir / "frames.log").string();
    std::string err;
    long pid = spawn_detached_logged("/bin/sh",
        {"-c", "echo hello-from-game; echo to-stderr 1>&2; sleep 30"},
        dir.string(), log, {{"FRAMES_TEST", "1"}}, &err);
    check(pid > 0, "spawns a detached process");
    std::this_thread::sleep_for(std::chrono::milliseconds(600));
    check(process_alive(pid), "reports it alive");
    std::ifstream in(log);
    std::string body((std::istreambuf_iterator<char>(in)), {});
    check(body.find("hello-from-game") != std::string::npos,
          "stdout lands in the log file");
    check(body.find("to-stderr") != std::string::npos,
          "stderr lands in the same file");
    check(process_stop(pid), "stop signals it");
    std::this_thread::sleep_for(std::chrono::milliseconds(800));
    check(!process_alive(pid), "and it is gone afterwards");
    check(!process_alive(0) && !process_alive(-1), "bogus pids are not alive");
    long bad = spawn_detached_logged("/nonexistent/binary", {}, dir.string(), log, {}, &err);
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    check(bad == 0 || !process_alive(bad), "a missing binary does not leave a process");
    fs::remove_all(dir);
    std::printf("%s\n", failures ? "FAILED" : "PASSED");
    return failures ? 1 : 0;
}
