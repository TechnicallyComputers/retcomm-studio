// json_null_test.cpp — the loaders must survive a null where a string goes.
//
// nlohmann's value() falls back only when the KEY IS ABSENT. A present-but-null
// key throws type_error.302, which is exactly what a SNES audit produces:
// boot_exe is null because a cartridge boots from its reset vector, not from a
// named executable. The whole audit failed to parse over that one field.

#include "studio/studio_runner.hpp"

#include <cstdio>
#include <string>

using namespace retcomm::studio;

namespace {
int failures = 0;
void check(bool cond, const char* what) {
    std::printf(cond ? "  ok    %s\n" : "  FAIL  %s\n", what);
    if (!cond) ++failures;
}
} // namespace

int main() {
    // A SNES audit, verbatim in shape: boot_exe null, fix_op null on a pass.
    const std::string audit = R"({
      "root": "/tmp/Zed",
      "layout": "scaffold-complete",
      "project_name": "ZedSNESRecomp",
      "boot_exe": null,
      "checks": [
        {"id":"framework","title":"snesrecomp/ checkout","status":"pass",
         "detail":"/tmp/Zed/snesrecomp","fix_op":null},
        {"id":"gitignore","title":".gitignore","status":"fail",
         "detail":null,"fix_op":"snes_merge_gitignore"}
      ]
    })";

    StudioModel model;
    std::string err;
    check(load_audit_from_json(model, audit, &err),
          err.empty() ? "an audit with null boot_exe parses" : err.c_str());
    check(model.audit_boot.empty(), "a null boot_exe reads as empty, not \"null\"");
    check(model.audit_checks.size() == 2, "both checks survived");
    if (model.audit_checks.size() == 2) {
        check(model.audit_checks[0].fix_op.empty(), "a null fix_op is empty");
        check(model.audit_checks[1].detail.empty(), "a null detail is empty");
        check(model.audit_checks[1].fix_op == "snes_merge_gitignore",
              "a real fix_op still arrives");
    }

    // Absent keys must behave the same as null ones.
    StudioModel m2;
    check(load_audit_from_json(m2, R"({"checks":[{"id":"x"}]})", &err),
          "an audit with everything absent parses");

    // Plan + repos share the hazard: every optional field here comes from a
    // Python dataclass that can hold None.
    StudioModel m3;
    check(load_plan_from_json(
              m3, R"({"steps":[{"op_id":"a","title":null,"detail":null}]})", &err),
          "a plan with null title/detail parses");
    check(!m3.plan_steps.empty() && m3.plan_steps[0].title.empty(),
          "a null plan title is empty");

    StudioModel m4;
    check(load_repos_from_json(
              m4, R"({"last":null,"repos":[{"path":"/tmp/Zed","name":null,"cue":null}]})", &err),
          "a repo list with null name/cue parses");
    check(m4.repos.size() == 1 && m4.repos[0].cue.empty(), "a null cue is empty");
    check(m4.repos.size() == 1 && m4.repos[0].label == "Zed",
          "a null name still yields a label from the path");

    std::printf(failures ? "FAILED\n" : "PASSED\n");
    return failures ? 1 : 0;
}
