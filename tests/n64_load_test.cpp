// n64_load_test — the N64 Diagnostics tab's parsers, headless.
//
// Every fixture below is REAL output, captured on 2026-09-09 from the tools
// themselves against a live n64lle checkout — not hand-written to match the
// parser. A parser tested only against invented input tests the invention.
//
//   ./build/n64_load_test

#include "studio/studio_n64.hpp"

#include <cstdio>
#include <string>

using namespace retcomm::studio;

static int failures = 0;

static void check(bool cond, const char* what) {
    if (!cond) {
        std::printf("FAIL: %s\n", what);
        ++failures;
    }
}

// --------------------------------------------------------------------------
// oracle status
// --------------------------------------------------------------------------

// Verbatim `n64_oracle.py status --json` against a built, on-pin oracle.
static const char* kStatusBuilt = R"({
 "root": "/home/alex/.local/share/retcomm/oracle/n64ref",
 "n64lle": "/home/alex/Documents/GitHub/GloverRecomp/n64lle",
 "installed": true,
 "binary": "/home/alex/Documents/GitHub/GloverRecomp/n64lle/build-oracle/n64ref/n64ref",
 "build_dir": "/home/alex/Documents/GitHub/GloverRecomp/n64lle/build-oracle",
 "ares_pinned": "0aafd85789215e84e1e43415c07d4c88461b7899",
 "ares_present": "0aafd85789215e84e1e43415c07d4c88461b7899",
 "pin_ok": true,
 "patches": [
  "0001-deterministic-random-seed.patch",
  "0002-sp-dma-write-capture.patch",
  "0003-rdp-cmd-capture.patch"
 ],
 "running_pid": 0,
 "running_port": 0,
 "logfile": "/home/alex/.local/share/retcomm/oracle/n64ref/oracle.log",
 "manifest": {}
})";

static void test_oracle_status() {
    const N64OracleStatus st = parse_n64_oracle_status(kStatusBuilt);
    check(st.probed, "status: probed");
    check(st.installed, "status: installed");
    check(st.pin_ok, "status: pin_ok");
    check(st.patches.size() == 3, "status: all three patches listed");
    check(st.ares_pinned == st.ares_present, "status: pinned == present");
    check(st.running_pid == 0, "status: not running");
    check(st.summary.find("on-pin") != std::string::npos,
          "status: summary says on-pin");

    // An oracle that is BUILT but off-pin is the dangerous state: it will
    // answer every query and grade every gate, with a tree nothing in the
    // evidence trail describes. The summary must not read as merely a warning.
    std::string off = kStatusBuilt;
    const auto pos = off.find("\"pin_ok\": true");
    off.replace(pos, std::string("\"pin_ok\": true").size(), "\"pin_ok\": false");
    const N64OracleStatus bad = parse_n64_oracle_status(off);
    check(bad.installed && !bad.pin_ok, "off-pin: installed but pin_ok false");
    check(bad.summary.find("OFF-PIN") != std::string::npos,
          "off-pin: summary shouts OFF-PIN");

    const N64OracleStatus empty = parse_n64_oracle_status("");
    check(!empty.installed && !empty.error.empty(), "empty status: error, not installed");
    const N64OracleStatus junk = parse_n64_oracle_status("not json at all");
    check(!junk.installed && !junk.error.empty(), "junk status: error, not installed");
}

// --------------------------------------------------------------------------
// gate results
// --------------------------------------------------------------------------

// Verbatim `n64_gates.py --json` over one real pass and one real SKIP. The
// skip is the case that matters: n64lle's gates exit 77 when an anchor ROM is
// absent, and folding that into "passed" is how a suite reads green without
// having run.
static const char* kGatesMixed = R"({
 "results": [
  {
   "name": "cosim_gate2_oracle",
   "status": "Passed",
   "seconds": 55.83
  },
  {
   "name": "rsp_attract_invariant_snap",
   "status": "Skipped",
   "seconds": 0.05
  }
 ],
 "passed": 1,
 "failed": 0,
 "skipped": 1,
 "matched": 2,
 "ctest_exit": 0
})";

static void test_gate_results() {
    N64GateRun run;
    parse_n64_gate_json(run, kGatesMixed);
    check(run.ran, "gates: ran");
    check(run.passed == 1, "gates: one pass");
    check(run.skipped == 1, "gates: one skip");
    check(run.failed == 0, "gates: no failures");
    check(run.results.size() == 2, "gates: two rows");
    check(run.results[0].name == "cosim_gate2_oracle", "gates: first row name");
    check(run.results[1].status == "Skipped", "gates: skip kept as skip");
    check(run.summary.find("skipped") != std::string::npos,
          "gates: summary names the skip");
    check(run.summary.find("FAILED") == std::string::npos,
          "gates: a skip is not reported as a failure");

    // The tool reports its own inability to run as {"error": ...}; that must
    // surface as an error rather than as a clean zero-result pass.
    N64GateRun err;
    parse_n64_gate_json(err, R"({"error": "no configured build at /nope"})");
    check(!err.error.empty(), "gates: tool error surfaces");
    check(err.passed == 0 && err.results.empty(), "gates: no phantom results");

    N64GateRun none;
    parse_n64_gate_json(none, R"({"results": [], "passed": 0, "failed": 0, "skipped": 0})");
    check(!none.error.empty(), "gates: empty selection is an error, not a pass");

    N64GateRun empty;
    parse_n64_gate_json(empty, "");
    check(!empty.error.empty(), "gates: no output is an error");
}

// --------------------------------------------------------------------------

static void test_catalogue() {
    const auto& g = n64_gates();
    check(!g.empty(), "catalogue: non-empty");
    // Determinism first is load-bearing, not cosmetic: a red gate2 makes every
    // row below it meaningless, so it must be the row a reader sees first.
    check(std::string(g.front().ctest_name) == "cosim_gate2_oracle",
          "catalogue: oracle determinism leads");
    int needs_oracle = 0, standalone = 0;
    for (const auto& row : g) (row.needs_oracle ? needs_oracle : standalone)++;
    check(needs_oracle > 0, "catalogue: has oracle-backed gates");
    check(standalone > 0,
          "catalogue: keeps gates that run without the oracle, so the pane is "
          "useful before Setup");
    for (const auto& row : g) {
        check(row.label && row.label[0], "catalogue: every row has a label");
        check(row.what && row.what[0],
              "catalogue: every row says what a pass proves");
    }
}

int main() {
    test_oracle_status();
    test_gate_results();
    test_catalogue();
    if (failures) {
        std::printf("%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("n64_load_test: all checks passed\n");
    return 0;
}
