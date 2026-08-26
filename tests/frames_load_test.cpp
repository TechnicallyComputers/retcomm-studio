// frames_load_test.cpp — the Frames tab's data path, with no GPU and no window.
//
//   ./build/frames_load_test              # embedded fixtures
//   ./build/frames_load_test <frames-dir> # real artifacts from gpu_frame_*.py
//
// The second form is the one that matters: it loads exactly what the Python
// tools wrote, so a schema drift between the two halves fails here instead of
// showing up as an empty table in the UI.

#include "studio/studio_frames.hpp"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>

namespace fs = std::filesystem;
using namespace retcomm::studio;

namespace {

int failures = 0;
void check(bool ok, const char* what) {
    std::printf("  %s  %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++failures;
}

void write_file(const fs::path& p, const char* text) {
    fs::create_directories(p.parent_path());
    std::ofstream(p) << text;
}

const char* kSummary = R"J({
 "kind": "psx-gpu-frame-summary", "version": 1, "label": "bad", "frame": 41230,
 "dump": "bad.json", "packets": 812, "drawing": 640, "capped": false,
 "truncated": 2, "area": [0, 0, 320, 240],
 "ops": {"PolyG3": 16, "PolyG3+semi": 4, "draw_mode(E1)": 1},
 "modes": {"0.5B+0.5F": 4},
 "funcs": [
  {"func": "0x80041000", "packets": 16, "drawing": 16, "semi": 0, "textured": 0,
   "ops": {"PolyG3": 16}, "stp_modes": {}, "ot_min": 30, "ot_max": 30,
   "ras": ["0x80011000", "0x80011abc"], "bbox": [-24, -28, 560, 520]},
  {"func": "0x80042000", "packets": 4, "drawing": 4, "semi": 4, "textured": 1,
   "ops": {"PolyG3+semi": 4}, "stp_modes": {"0.5B+0.5F": 4},
   "ot_min": null, "ot_max": null, "ras": [], "bbox": null}
 ]})J";

const char* kDiff = R"J({
 "kind": "psx-gpu-frame-diff", "version": 1,
 "a": {"label": "good", "frame": 100, "packets": 31},
 "b": {"label": "bad", "frame": 140, "packets": 20},
 "funcs": [
  {"func": "0x80042000", "verdict": "stopped drawing",
   "a": {"prims": 10, "semi": 10, "tex": 0}, "b": {"prims": 0, "semi": 0, "tex": 0},
   "ras": []},
  {"func": "0x80041000", "verdict": "lost semi-transparency",
   "a": {"prims": 16, "semi": 16, "tex": 0}, "b": {"prims": 16, "semi": 0, "tex": 0},
   "ras": []}
 ],
 "keys": [],
 "ops": {"PolyG3": {"a": 0, "b": 16, "delta": 16},
         "PolyF4+semi": {"a": 10, "b": 0, "delta": -10}},
 "modes": {"B-F": {"a": 10, "b": 0, "delta": -10}},
 "modes_a": {"0.5B+0.5F": 16, "B-F": 10}, "modes_b": {}})J";

const char* kLayers = R"J({
 "kind": "psx-gpu-layers", "version": 1, "frame": 140, "label": "bad",
 "area": [0, 0, 320, 240], "scale": 1, "order": "issue", "tex_tint": false,
 "layer_backdrop": 128.0, "composite": "composite.png", "sheet": "sheet.png",
 "rendered": 18, "non_drawing": 2,
 "layers": [
  {"func": "0x80041000", "file": "layer-0x80041000.png", "prims": 16, "semi": 0,
   "textured": 0, "pixels": 38575, "bbox": [0,0,1,1], "ot_min": 30, "ot_max": 30,
   "ops": {"PolyG3": 16}, "stp_modes": {}, "ras": []}
 ]})J";

int run_fixtures() {
    const fs::path dir = fs::temp_directory_path() / "retcomm_frames_load_test";
    fs::remove_all(dir);
    write_file(dir / "bad.summary.json", kSummary);
    write_file(dir / "diff.json", kDiff);
    write_file(dir / "bad-layers" / "layers.json", kLayers);

    FrameSummary s;
    check(load_frame_summary(s, (dir / "bad.summary.json").string()),
          "loads a summary");
    check(s.frame == 41230 && s.packets == 812 && s.drawing == 640,
          "reads the frame totals");
    check(s.truncated == 2, "carries the truncated-packet count");
    check(s.funcs.size() == 2, "reads both functions");
    check(s.funcs[0].addr == 0x80041000u, "parses the function address");
    check(s.funcs[0].ops == "PolyG3 x16", "joins the opcode counts");
    check(s.funcs[0].ras.size() == 2 && s.funcs[0].ras[1] == 0x80011abcu,
          "parses observed return addresses");
    check(s.funcs[0].has_bbox && s.funcs[0].bbox[0] == -24,
          "keeps a negative bbox coordinate (off-screen geometry is real)");
    check(!s.funcs[1].has_bbox, "a null bbox is absent, not zero");
    check(s.funcs[1].ot_min == -1 && s.funcs[1].ot_max == -1,
          "a null OT rank reads as -1, not 0");
    check(s.funcs[1].stp == "0.5B+0.5F x4", "joins the blend modes");
    check(!s.modes.empty() && s.modes[0].first == "0.5B+0.5F",
          "reads the frame's blend-mode histogram");
    check(!s.ops.empty() && s.ops[0].first == "PolyG3" && s.ops[0].second == 16,
          "sorts the opcode histogram by count");

    FrameDiff d;
    check(load_frame_diff(d, (dir / "diff.json").string()), "loads a diff");
    check(d.rows.size() == 2, "reads both changed functions");
    check(d.rows[0].verdict == "stopped drawing", "keeps the verdict");
    check(d.rows[1].a_semi == 16 && d.rows[1].b_semi == 0,
          "reads the semi counts on both sides");
    bool saw_mode = false, saw_stopped = false;
    for (const auto& h : d.headlines) {
        if (h.find("B-F") != std::string::npos) saw_mode = true;
        if (h.find("0x80042000") != std::string::npos) saw_stopped = true;
    }
    check(saw_mode, "headlines the blend mode that vanished");
    check(saw_stopped, "headlines the function that stopped drawing");
    check(!d.ops.empty() && std::get<0>(d.ops[0]) == "PolyG3",
          "sorts opcode deltas by magnitude");

    FrameLayers l;
    check(load_frame_layers(l, (dir / "bad-layers").string()), "loads a layer index");
    check(l.frame == 140 && l.layers.size() == 1, "reads the layer list");
    check(l.layers[0].pixels == 38575, "reads a layer's covered pixel count");
    check(l.composite.file == "composite.png" && l.sheet.file == "sheet.png",
          "finds the composite and the contact sheet");
    check(l.layers[0].tex == 0, "uploads no texture without a GL context");

    FrameSummary missing;
    check(!load_frame_summary(missing, (dir / "nope.summary.json").string()),
          "a missing file fails cleanly");
    check(!missing.error.empty(), "and says why");

    write_file(dir / "wrong.summary.json", R"J({"kind":"something-else"})J");
    FrameSummary wrong;
    check(!load_frame_summary(wrong, (dir / "wrong.summary.json").string()),
          "a foreign JSON file is rejected by kind");

    StudioModel model;
    std::snprintf(model.frm_dir, sizeof(model.frm_dir), "%s", dir.string().c_str());
    scan_frame_tags(model);
    check(model.frm_tags.size() == 2, "scans *.summary.json into tags");
    check(model.frm_tags[0] == "bad" && model.frm_tags[1] == "wrong",
          "tags are the basenames, sorted");

    // Names come from an analysis bundle; an address inside a function must be
    // reported with its offset so it never reads as an entry point.
    FnRow fn;
    fn.addr = 0x80041000u;
    fn.end = 0x80041200u;
    fn.name = "LandEffect_DrawRays";
    model.fn_rows.push_back(fn);
    name_frame_funcs(model, s);
    check(s.funcs[0].name == "LandEffect_DrawRays", "names an exact entry match");
    s.funcs[0].addr = 0x80041040u;
    name_frame_funcs(model, s);
    check(s.funcs[0].name == "LandEffect_DrawRays+0x40",
          "an interior address is named with its offset");
    check(s.funcs[1].name.empty(), "an unknown address stays unnamed");

    // Tool discovery: the GP0 tools must come from the project's own engine
    // checkout, never from a copy shipped with Studio, or a dump and the tool
    // that reads it can disagree about the wire format.
    const fs::path proj = dir / "proj";
    check(frames_dir_for(proj.string()) == (proj / "analysis" / "frames").string(),
          "captures land in <root>/analysis/frames");
    check(gpu_tool_path(proj.string(), "gpu_frame_capture.py").empty(),
          "no tool path before the engine submodule is checked out");
    write_file(proj / "psxrecomp" / "tools" / "gpu_frame_capture.py", "# stub\n");
    check(gpu_tool_path(proj.string(), "gpu_frame_capture.py") ==
              (proj / "psxrecomp" / "tools" / "gpu_frame_capture.py").string(),
          "finds the tool in the engine submodule");
    check(gpu_tool_path(proj.string(), "gpu_frame_layers.py").empty(),
          "a tool that is not there is reported as absent, not guessed");
    const fs::path engine = dir / "engine";
    write_file(engine / "tools" / "gpu_frame_capture.py", "# stub\n");
    check(gpu_tool_path(engine.string(), "gpu_frame_capture.py") ==
              (engine / "tools" / "gpu_frame_capture.py").string(),
          "also works with the engine repo opened directly");
    check(gpu_tool_path("", "gpu_frame_capture.py").empty(),
          "an empty root yields no tool path");

    // ---- RAM parity verdict ------------------------------------------------
    // The verdict is the product: it says which half of the codebase the bug is
    // in, so both outcomes have to survive the round trip.
    {
        write_file(dir / "parity_same.json", R"J({
 "kind":"psx-ram-parity","version":1,"addr":"0x0010D078","length":2269,
 "native_frame":5000,"oracle_frame":5000,"compared":2269,
 "differing_bytes":0,"differing_words":0,"total_words":567,"identical":true,
 "runs":[]})J");
        RamParity rp;
        check(load_ram_parity(rp, (dir / "parity_same.json").string()),
              "loads an identical verdict");
        check(rp.identical && rp.differing_words == 0, "and it reads as identical");
        check(rp.frames_aligned, "with both emulators on the same frame");

        write_file(dir / "parity_diff.json", R"J({
 "kind":"psx-ram-parity","version":1,"addr":"0x0010D078","length":2269,
 "native_frame":5000,"oracle_frame":7777,"compared":2269,
 "differing_bytes":813,"differing_words":252,"total_words":567,
 "identical":false,"first_difference":"0x0010D080","runs":[]})J");
        RamParity d;
        check(load_ram_parity(d, (dir / "parity_diff.json").string()),
              "loads a differing verdict");
        check(!d.identical && d.differing_words == 252 &&
                  d.first_difference == "0x0010D080",
              "with the word count and the first differing address");
        check(!d.frames_aligned,
              "and notices the two were on different frames — otherwise the "
              "difference is just elapsed time");

        RamParity bad2;
        write_file(dir / "parity_wrong.json", R"J({"kind":"something-else"})J");
        check(!load_ram_parity(bad2, (dir / "parity_wrong.json").string()),
              "a foreign document is rejected by kind");
        check(!load_ram_parity(bad2, (dir / "nope.json").string()),
              "and a missing one fails cleanly");
    }

    // ---- where does a PC live? ---------------------------------------------
    // The writer PCs that came out of a real trace were all ABOVE the boot
    // executable's text, which is why the Functions tab had nothing for them:
    // the code is in a runtime-loaded overlay and psxrecomp-analyze only reads
    // the boot EXE. The tab has to be able to say that rather than show a blank.
    {
        const fs::path g2 = dir / "textend";
        write_file(g2 / "game.toml",
                   "[game]\nname = \"X\"\n"
                   "load_address = \"0x80010000\"\n"
                   "text_size = \"0x0002F000\"\n");
        const uint32_t end = game_text_end(g2.string());
        check(end == 0x8003F000u, "reads the boot EXE's text end from game.toml");
        check(0x8002E7A8u < end, "the game entry point is inside it");
        // The two clusters a real write-trace produced.
        check(0x80056FE4u >= end && 0x80061474u >= end,
              "the observed writer PCs are above it — overlay territory");
        write_file(g2 / "nofields.toml", "[game]\nname = \"X\"\n");
        check(game_text_end((dir / "no-such-root").string()) == 0,
              "a project without those fields yields 0, not a bogus boundary");
        check(game_text_end("") == 0, "and so does an empty root");
    }

    // ---- ring scan ---------------------------------------------------------
    // The scan answers "which two frames should I diff", which the plain A/B
    // diff assumes you already know.
    {
        write_file(dir / "scan.json", R"J({
 "kind":"psx-gpu-frame-scan","version":1,"range":[40900,41230],
 "transitions":[
  {"a":41076,"b":41077,"score":4750.0,"changes":[
    {"key":"PolyG3+semi|B+F","a":0,"b":64,"delta":64,
     "bbox_a":null,"bbox_b":[133,-12,203,119],
     "cmax_a":0,"cmax_b":199,
     "src_a":null,"src_b":"0x0010D05C","src_hi_b":"0x0010E6E4",
     "outside_draw_area":true,"outside_vram":true,
     "overflow":[148,180,140,196]}]},
  {"a":41069,"b":41070,"score":0.0,"changes":[]}
 ]})J");
        FrameScan sc;
        check(load_frame_scan(sc, (dir / "scan.json").string()), "loads a scan");
        check(sc.lo == 40900 && sc.hi == 41230, "carries the scanned range");
        check(sc.transitions.size() == 2, "reads every transition");
        const ScanTransition& t = sc.transitions[0];
        check(t.a == 41076 && t.b == 41077, "names the frame pair");
        check(t.changes.size() == 1 && t.changes[0].delta == 64,
              "and what moved across it");
        check(t.changes[0].bbox_a.empty() && t.changes[0].bbox_b == "[133, -12, 203, 119]",
              "an absent bbox stays absent; a present one renders");
        check(t.changes[0].src_lo == "0x0010D05C" &&
                  t.changes[0].src_hi == "0x0010E6E4",
              "carries the packet buffer RANGE — wtrace watches a range, and one "
              "address would only catch the first packet");
        check(t.changes[0].cmax_b == 199, "and the peak colour");

        check(t.changes[0].outside_vram && t.changes[0].outside_draw_area,
              "flags geometry that left VRAM — the finding, not a statistic");
        check(t.changes[0].overflow.find("left 148") != std::string::npos,
              "and says how far out, per edge");

        // ---- who writes the packet buffer ----------------------------------
        // The answer function attribution cannot give on a DMA game.
        {
            std::string err;
            auto w = parse_wtrace(
                R"J({"ok":true,"entries":[)J"
                R"J({"addr":"0x00115078","pc":"0x8004A1C0","func":"0x8004A100","ra":"0x8004B222","dma_ch":-1},)J"
                R"J({"addr":"0x0011507C","pc":"0x8004A1C0","func":"0x8004A100","ra":"0x8004B222","dma_ch":-1},)J"
                R"J({"addr":"0x00115080","pc":"0x8004A1C0","func":"0x8004A100","ra":"0x8004B222","dma_ch":-1},)J"
                R"J({"addr":"0x00115084","pc":"0x8004C000","func":"0x8004BF00","ra":"0x8004C990","dma_ch":2}])J"
                R"J(})J", &err);
            check(w.size() == 2, "collapses per-write rows into code sites");
            check(w[0].pc == 0x8004A1C0u && w[0].count == 3,
                  "busiest writer first, with its write count");
            check(w[0].dma_ch < 0 && w[1].dma_ch == 2,
                  "and distinguishes a CPU store from a DMA fill");
            check(w[1].func == 0x8004BF00u, "carrying the enclosing function");

            std::string e2;
            auto none = parse_wtrace(R"J({"ok":true,"entries":[]})J", &e2);
            check(none.empty() && e2.find("let the game run") != std::string::npos,
                  "an empty trace says to run the effect again, not 'no writer'");
            std::string e3;
            parse_wtrace(R"J({"ok":false,"error":"no ranges armed"})J", &e3);
            check(e3.find("no ranges armed") != std::string::npos,
                  "and surfaces the server's own refusal");
        }

        // ---- display list walked out of RAM --------------------------------
        // The other half of the same question: the scan says WHAT changed, this
        // says what the game built to make it. Distinct from the ring, and the
        // only view available on the oracle, which records no packets at all.
        write_file(dir / "dlist.json", R"J({
 "kind":"psx-display-list","version":1,"root":"0x00115078","port":4370,
 "nodes":66,"drawing":64,
 "classes":[{"key":"PolyG4+semi|B+F","count":64}],
 "prims":[
  {"op":"PolyG4+semi","blend":"B+F","src":"0x0010D05C","cmax":199,
   "verts":"(133,-12) (203,-12) (133,119)","colors":"(199, 40, 20) (12, 12, 12)"},
  {"op":"PolyG4+semi","blend":"B+F","src":"0x0010D080","cmax":24,
   "verts":"(10,10)","colors":"(24, 20, 18)"}
 ]})J");
        {
            DisplayList dl;
            check(load_display_list(dl, (dir / "dlist.json").string()),
                  "loads a walked display list");
            check(dl.root == "0x00115078" && dl.nodes == 66 && dl.drawing == 64,
                  "carries the root and the node counts");
            check(dl.classes.size() == 1 && dl.classes[0].second == 64,
                  "and the class histogram");
            check(dl.prims.size() == 2 && dl.prims[0].blend == "B+F",
                  "reads each primitive's blend mode");
            check(dl.prims[0].cmax == 199 && dl.prims[1].cmax == 24,
                  "and its peak colour — the number that separates 'built "
                  "different colours' from 'blended them differently'");
            check(dl.prims[0].src == "0x0010D05C",
                  "keeping the source address, which is what the write trace "
                  "takes to name the code that built it");

            DisplayList wrong;
            write_file(dir / "wrongdl.json", R"J({"kind":"something-else"})J");
            check(!load_display_list(wrong, (dir / "wrongdl.json").string()),
                  "a foreign document is rejected by kind");
            check(!load_display_list(wrong, (dir / "nodl.json").string()),
                  "a missing display list fails cleanly");
        }

        // ---- colour distribution across emulators --------------------------
        write_file(dir / "colour_parity.json", R"J({
 "kind":"psx-colour-parity","version":1,"class":"PolyG4+semi|B+F",
 "native":{"vertices":256,"peak":199,"mean":88.5,"p50":80,"p90":170},
 "oracle":{"vertices":248,"peak":40,"mean":21.0,"p50":19,"p90":34},
 "verdict":"differ"})J");
        {
            ColourParity cp;
            check(load_colour_parity(cp, (dir / "colour_parity.json").string()),
                  "loads a colour comparison");
            check(cp.klass == "PolyG4+semi|B+F", "carries the class compared");
            check(cp.native.peak == 199 && cp.oracle.peak == 40,
                  "and both peaks");
            check(cp.native.loaded && cp.oracle.loaded &&
                      cp.native.vertices == 256,
                  "with the sample size, so a verdict drawn from four vertices "
                  "is visibly not one drawn from four hundred");
            check(cp.verdict == "differ", "and the verdict");

            // A side that sampled nothing must not read as "peaked at zero" —
            // that is a different claim from "no data", and acting on it would
            // send you looking for a bug in the wrong emulator.
            write_file(dir / "cp_empty.json", R"J({
 "kind":"psx-colour-parity","version":1,"class":"PolyG4+semi|B+F",
 "native":{"vertices":256,"peak":199,"mean":88.5,"p50":80,"p90":170}})J");
            ColourParity half;
            check(load_colour_parity(half, (dir / "cp_empty.json").string()),
                  "a one-sided comparison still loads");
            check(half.native.loaded && !half.oracle.loaded,
                  "and the unsampled side stays unloaded rather than zeroed");
            check(half.verdict.empty(), "with no verdict claimed");

            ColourParity wrong;
            write_file(dir / "wrongcp.json", R"J({"kind":"something-else"})J");
            check(!load_colour_parity(wrong, (dir / "wrongcp.json").string()),
                  "a foreign document is rejected by kind");
        }

        // ---- packet colour writers -----------------------------------------
        write_file(dir / "packet_writers.json", R"J({
 "kind":"psx-packet-writers","version":1,"class":"PolyG4+semi|B+F","absent":false,
 "packets":64,"lo":"0x0010D1C4","hi":"0x0010E6E4",
 "colour_words":256,"vertex_words":256,"writes":2048,"unmapped":7,
 "writers":[
  {"pc":"0x80056FA8","func":"0x00000F40","colour":181,"vertex":0,"uv":0,"colour_only":true},
  {"pc":"0x800570FC","func":"0x00000F40","colour":90,"vertex":91,"uv":0,"colour_only":false}
 ]})J");
        {
            PacketWriters w;
            check(load_packet_writers(w, (dir / "packet_writers.json").string()),
                  "loads a packet-writer report");
            check(w.packets == 64 && w.colour_words == 256,
                  "carries the packet and field counts");
            check(w.writers.size() == 2 && w.writers[0].colour_only,
                  "flags the instruction that writes colour and never geometry "
                  "— the one to read when colours are wrong and geometry is not");
            check(w.unmapped == 7,
                  "keeps the count of writes it could NOT attribute, so a walk "
                  "that raced a rebuild is visible rather than silently partial");
            check(!w.absent, "a populated report is not an absent one");
            check(!w.aliased, "a clean report is not flagged unreliable");
        }

        // A report where the buffer moved mid-trace. Every instruction comes
        // back near 50/50 colour:vertex, which reads exactly like "this code
        // writes both fields" and is entirely an artefact of stale addresses.
        // It has to arrive flagged, or it is worse than no report at all.
        write_file(dir / "pw_aliased.json", R"J({
 "kind":"psx-packet-writers","version":1,"class":"PolyG4+semi|B+F","absent":false,
 "packets":64,"writes":2048,"unmapped":223,"aliased":true,"mixed_writers":10,
 "writers":[
  {"pc":"0x80068C68","func":"0x00000F40","colour":93,"vertex":89,"uv":0,"colour_only":false},
  {"pc":"0x80068C70","func":"0x00000F40","colour":93,"vertex":88,"uv":0,"colour_only":false}
 ]})J");
        {
            PacketWriters w;
            check(load_packet_writers(w, (dir / "pw_aliased.json").string()),
                  "an aliased report loads");
            check(w.aliased && w.mixed_writers == 10,
                  "and arrives flagged, with the count of even-split "
                  "instructions that gave it away");
            check(!w.writers.empty() && !w.writers[0].colour_only,
                  "no row claims to be colour-only in an aliased report");
        }

        // An absent class is a FINDING, not an error: it means the effect was
        // not on screen, and reporting on whatever else was in the buffer would
        // be worse than saying so.
        write_file(dir / "pw_absent.json", R"J({
 "kind":"psx-packet-writers","version":1,"class":"PolyG4+semi|B+F","absent":true,
 "note":"no PolyG4+semi in the current list",
 "present":[{"key":"PolyFT4","count":576}],"writers":[]})J");
        {
            PacketWriters w;
            check(load_packet_writers(w, (dir / "pw_absent.json").string()),
                  "an absent-class report still loads");
            check(w.absent && w.writers.empty(), "and is marked absent");
            check(w.present.size() == 1 && w.present[0].second == 576,
                  "carrying what WAS drawing, so the reason is obvious");
        }

        // ---- GTE self-check -------------------------------------------------
        write_file(dir / "gte_check.json", R"J({
 "kind":"psx-gte-check","version":1,"what":"intpl","verdict":"not-used",
 "intpl_checked":0,"intpl_bad":0,"nintpl_total":0,"nsat_total":0,"nproj_last":1081,
 "note":"no INTPL records",
 "frames":[{"frame":2500,"nproj":1081,"nsat":0,"nflat":207,"nintpl":0,"nintpl_tiny":0}]})J");
        {
            GteCheck g;
            check(load_gte_check(g, (dir / "gte_check.json").string()),
                  "loads a GTE self-check");
            check(g.verdict == "not-used",
                  "an operation the game never issues is distinct from one that "
                  "passes — 'unused' and 'correct' point at different next steps");
            check(g.nproj_last == 1081 && g.nsat_total == 0,
                  "carries the projection and saturation counters");
            check(g.frames.size() == 1 && g.frames[0].nintpl == 0,
                  "and the per-frame rows");

            GteCheck bad2;
            write_file(dir / "wronggte.json", R"J({"kind":"something-else"})J");
            check(!load_gte_check(bad2, (dir / "wronggte.json").string()),
                  "a foreign document is rejected by kind");
        }

        // A trace that ran and recorded nothing. The packets were walked, so
        // they are on screen; an empty writers[] here means the trace window
        // was wrong. Reporting it as a finished search with no results would be
        // the most misleading thing the tool could do.
        write_file(dir / "pw_nowrites.json", R"J({
 "kind":"psx-packet-writers","version":1,"class":"PolyG4+semi|B+F","absent":false,
 "packets":64,"writes":0,"unmapped":0,"no_writes":true,
 "note":"no writes were recorded in the traced window","writers":[]})J");
        {
            PacketWriters w;
            check(load_packet_writers(w, (dir / "pw_nowrites.json").string()),
                  "an empty-trace report loads");
            check(w.no_writes && !w.absent,
                  "and is distinguished from the class simply not being drawn");
            check(w.packets == 64 && w.writers.empty(),
                  "the packets were found even though no writes were");
        }

        // ---- parity alignment ----------------------------------------------
        // Alignment decides whether anything else in the report means anything,
        // so it is the headline rather than a footnote.
        write_file(dir / "parity_ok.json", R"J({
 "kind":"psx-gpu-parity","version":1,"native_frame":3370,"duckstation_frame":3370})J");
        write_file(dir / "parity_bad.json", R"J({
 "kind":"psx-gpu-parity","version":1,"native_frame":4950,"duckstation_frame":4464})J");
        {
            const std::string ok = parity_alignment((dir / "parity_ok.json").string());
            check(ok.find("3370") != std::string::npos &&
                      ok.find("NOT ALIGNED") == std::string::npos,
                  "equal frames read as aligned");
            const std::string bad2 = parity_alignment((dir / "parity_bad.json").string());
            check(bad2.find("NOT ALIGNED") != std::string::npos,
                  "different frames are called out, not left for the reader to "
                  "notice in a warning line");
            check(bad2.find("4950") != std::string::npos &&
                      bad2.find("4464") != std::string::npos,
                  "naming both frames");
            check(parity_alignment((dir / "wrongscan.json").string()).empty(),
                  "a foreign document yields nothing rather than a false verdict");
        }

        // ---- colour inputs ---------------------------------------------------
        // The two verdicts point in OPPOSITE directions — one says look
        // upstream, the other says look at this routine — so conflating them
        // would send the reader the wrong way with full confidence.
        write_file(dir / "ci_differs.json", R"J({
 "kind":"psx-colour-inputs","version":1,"pc":"0x8006844C",
 "source_addr":"0x00115060","oracle_scale":96,"source_identical":false,
 "differing_bytes":9,"first_difference":"0x00115064",
 "native_bytes":"65000000","oracle_bytes":"074d4d00","verdict":"source-differs"})J");
        {
            ColourInputs c;
            check(load_colour_inputs(c, (dir / "ci_differs.json").string()),
                  "loads a colour-input comparison");
            check(c.verdict == "source-differs" && !c.source_identical,
                  "a differing source points UPSTREAM");
            check(c.differing == 9 && c.first_difference == "0x00115064",
                  "naming how many bytes and where the first one is");
            check(c.scale == 96, "and the scale the oracle was using");
        }

        write_file(dir / "ci_same.json", R"J({
 "kind":"psx-colour-inputs","version":1,"pc":"0x8006844C",
 "source_addr":"0x00115060","oracle_scale":128,"source_identical":true,
 "native_bytes":"074d4d00","oracle_bytes":"074d4d00","verdict":"source-matches"})J");
        {
            ColourInputs c;
            check(load_colour_inputs(c, (dir / "ci_same.json").string()),
                  "loads a matching-source comparison");
            check(c.verdict == "source-matches" && c.source_identical,
                  "a matching source points at the ROUTINE instead");
            check(c.differing == 0, "with no differing byte count");
        }

        // The oracle never reaching the PC is a normal outcome for overlay
        // code, not a crash — it must arrive as an explained error.
        write_file(dir / "ci_nohit.json", R"J({
 "kind":"psx-colour-inputs","version":1,"pc":"0x8006844C",
 "error":"the oracle never reached 0x8006844C within 20s"})J");
        {
            ColourInputs c;
            check(load_colour_inputs(c, (dir / "ci_nohit.json").string()),
                  "a no-hit report still loads");
            check(!c.error.empty() && c.verdict.empty(),
                  "carrying the reason and claiming no verdict");
        }

        // ---- lockstep --------------------------------------------------------
        // "found: false" comes from two different situations, and only one of
        // them is good news. The counters are what separate them.
        write_file(dir / "ls_clean.json", R"J({
 "kind":"psx-lockstep","version":1,"mode":"lockstep","verdict":"clean",
 "found":0,"blocks_checked":4211,"window":[10,130]})J");
        write_file(dir / "ls_nothing.json", R"J({
 "kind":"psx-lockstep","version":1,"mode":"lockstep","verdict":"inconclusive",
 "found":0,"blocks_checked":0,"window":[10,130]})J");
        write_file(dir / "ls_div.json", R"J({
 "kind":"psx-lockstep","version":1,"mode":"lockstep_func","verdict":"diverged",
 "found":1,"segments_checked":900,"window":[0,120],"frame":41230,
 "entry":"0x80068440","pc":"0x8006844C","addr":"0x1F800264","reg":-1,
 "interp_expected":"0x000000F8","compiled_actual":"0x0000001B",
 "meaning":"the same address was written with a different VALUE",
 "skipped_irq":4,"skipped_unhandled":2,
 "trace":["W4:1F800264=000000F8","R4:1F800264=0000001B"]})J");
        {
            Lockstep a, b, c;
            check(load_lockstep(a, (dir / "ls_clean.json").string()) &&
                      a.verdict == "clean" && a.checked == 4211,
                  "a checked-and-quiet run reads as clean");
            check(load_lockstep(b, (dir / "ls_nothing.json").string()) &&
                      b.verdict == "inconclusive" && b.checked == 0,
                  "a run that checked NOTHING is inconclusive, not clean — the "
                  "same 'found:false' a real pass produces");
            check(load_lockstep(c, (dir / "ls_div.json").string()),
                  "loads a divergence");
            check(c.found && c.pc == "0x8006844C",
                  "naming the PC where compiled and interpreted disagreed");
            check(c.expected == "0x000000F8" && c.actual == "0x0000001B",
                  "and both values, which is what makes it actionable");
            check(c.skipped == 6, "summing every skip reason");
            check(!c.meaning.empty(),
                  "carrying the plain-English reading, so the GUI does not keep "
                  "a second copy of that table and drift from it");
            check(c.trace.size() == 2, "with the memory ops leading up to it");

            Lockstep wrong;
            write_file(dir / "wrongls.json", R"J({"kind":"something-else"})J");
            check(!load_lockstep(wrong, (dir / "wrongls.json").string()),
                  "a foreign document is rejected — note `kind` is the document "
                  "type here and the DIVERGENCE type inside, so the check has "
                  "to happen before anything else is read");
        }

        // A comparison made through psx-runtime's OWN pointer is a different
        // claim from one made by searching RAM for matching bytes: the first
        // knows which table its code reads, the second can only guess.
        write_file(dir / "ci_own.json", R"J({
 "kind":"psx-colour-inputs","version":1,"pc":"0x8006844C",
 "source_addr":"0x000E4BF8","oracle_scale":128,"compared_by":"native-own-pointer",
 "native_source_addr":"0x000ECBF8","address_delta":32768,
 "native_regs":{"s4":"0x800ECC04","s6":"0x00000080"},
 "native_probe":{"block_leader":"0x8006842C","frame":9001,"samples_seen":12},
 "source_identical":true,"verdict":"source-matches",
 "native_bytes":"f8505000","oracle_bytes":"f8505000"})J");
        {
            ColourInputs c;
            check(load_colour_inputs(c, (dir / "ci_own.json").string()),
                  "loads a comparison made through both pointers");
            check(c.compared_by == "native-own-pointer",
                  "recording HOW the two were compared, since a content search "
                  "cannot know which table psx-runtime actually reads");
            check(c.native_s4 == "0x800ECC04" && c.native_s6 == "0x00000080",
                  "carrying psx-runtime's own registers");
            check(c.native_block == "0x8006842C",
                  "and the block they were read at, since that is not the "
                  "instruction that was asked about");
            check(c.address_delta == 32768,
                  "a +0x8000 delta is the double buffer, not a divergence");
        }

        write_file(dir / "ci_noprobe.json", R"J({
 "kind":"psx-colour-inputs","version":1,"pc":"0x8006844C",
 "source_addr":"0x000E4BF8","oracle_scale":128,
 "native_probe":{"error":"no candidate fired"},
 "partial_match_len":4,"partial_stride":24,"verdict":"table-rearranged"})J");
        {
            ColourInputs c;
            check(load_colour_inputs(c, (dir / "ci_noprobe.json").string()),
                  "loads a run where the probe could not fire");
            check(!c.probe_error.empty() && c.compared_by.empty(),
                  "the fallback is visibly a fallback, not presented as the "
                  "same quality of answer");
        }

        // ---- range writers ---------------------------------------------------
        write_file(dir / "range_writers.json", R"J({
 "kind":"psx-range-writers","version":1,"lo":"0x000E0BF8","hi":"0x000E6628",
 "frames":1,"frames_advanced":1,"writes":412,
 "writers":[
  {"pc":"0x8006C120","writes":384,"lo":"0x000E2600","hi":"0x000E4700",
   "common":[{"value":"0x00F85050","count":96},{"value":"0x00080808","count":96}]},
  {"pc":"0x8006C15C","writes":28,"lo":"0x000E2718","hi":"0x000E2740","common":[]}
 ],
 "listings":{"0x8006C120":[
  {"pc":"0x8006C118","word":"0x00000000","text":"nop","is_target":false},
  {"pc":"0x8006C120","word":"0xACE20004","text":"sw $v0,4($a3)","is_target":true}]}})J");
        {
            RangeWriters r;
            check(load_range_writers(r, (dir / "range_writers.json").string()),
                  "loads a range-writer report");
            check(r.writes == 412 && r.writers.size() == 2,
                  "carrying the totals and each instruction");
            check(r.writers[0].writes == 384 &&
                      r.writers[0].lo == "0x000E2600",
                  "with the span each one touched — a store walking an array is "
                  "a different thing from one hitting a single field");
            check(r.writers[0].common.find("0x00F85050") != std::string::npos,
                  "and the values it wrote, which separate clearing from "
                  "computing");
            check(r.listing_pc == "0x8006C120" && r.listing.size() == 2,
                  "code for the busiest writer, captured while parked");
            check(r.listing[1].is_target, "with the store itself marked");

            RangeWriters wrong;
            write_file(dir / "wrongrw.json", R"J({"kind":"something-else"})J");
            check(!load_range_writers(wrong, (dir / "wrongrw.json").string()),
                  "a foreign document is rejected by kind");
        }

        FrameScan bad;
        check(!load_frame_scan(bad, (dir / "nope.json").string()),
              "a missing scan fails cleanly");
        write_file(dir / "wrongscan.json", R"J({"kind":"something-else"})J");
        check(!load_frame_scan(bad, (dir / "wrongscan.json").string()),
              "a foreign document is rejected by kind");
    }

    // ---- pause state -------------------------------------------------------
    {
        PauseState ps = parse_pause_state(
            R"J({"id":1,"ok":true,"paused":true,"stepping":0,"run_to":0,)J"
            R"J("frame":922,"timeout_ms":60000,"auto_resumed":false})J");
        check(ps.valid && ps.paused, "reads a parked runtime");
        check(ps.frame == 922 && ps.timeout_ms == 60000, "carries frame and timeout");
        check(ps.supported, "and counts as supported");
    }
    {
        PauseState ps = parse_pause_state(
            R"J({"ok":true,"paused":false,"stepping":3,"run_to":0,"frame":10,)J"
            R"J("auto_resumed":false})J");
        check(ps.valid && !ps.paused && ps.stepping == 3, "reads a step in progress");
    }
    {
        // The property that matters most: a park that released itself because
        // Studio went quiet must be visible, or the Pause button lies.
        PauseState ps = parse_pause_state(
            R"J({"ok":true,"paused":false,"stepping":0,"run_to":0,"frame":2318,)J"
            R"J("timeout_ms":3000,"auto_resumed":true})J");
        check(ps.valid && !ps.paused && ps.auto_resumed,
              "an auto-resumed park is reported as such");
    }
    {
        // A runtime built before the pause gate answers "unknown command".
        // That is a build difference, not a fault.
        PauseState ps = parse_pause_state(
            R"J({"ok":false,"error":"unknown command: pause_state"})J");
        check(!ps.valid && !ps.supported,
              "an older runtime is reported unsupported, not broken");
        PauseState other = parse_pause_state(R"J({"ok":false,"error":"no cpu"})J");
        check(!other.valid && other.supported,
              "a different failure is still 'supported', just failed");
        check(!parse_pause_state("").valid && !parse_pause_state("{{").valid,
              "empty and malformed replies are rejected");
    }

    // ---- finding the game binary -------------------------------------------
    // Must agree with buildops.py's find_runtime_exe(), or the Frames tab
    // launches something different from what the Build tab would.
    {
        const fs::path b = dir / "bdir";
        check(find_runtime_exe(b.string()).empty(), "no build dir yields nothing");
        write_file(b / "libfoo.so", "x");
        write_file(b / "CMakeCache.txt", "x");
        check(find_runtime_exe(b.string()).empty(),
              "libraries and cmake files are not the game");
        write_file(b / "Some_Game__Recompiled", "x");
        fs::permissions(b / "Some_Game__Recompiled", fs::perms::owner_exec,
                        fs::perm_options::add);
        check(find_runtime_exe(b.string()) == (b / "Some_Game__Recompiled").string(),
              "picks the Recompiled product");
        // psx-runtime scores lower than a *Recompiled* product, matching
        // buildops.py, so a build dir with both still launches the game.
        write_file(b / "psx-runtime", "x");
        fs::permissions(b / "psx-runtime", fs::perms::owner_exec,
                        fs::perm_options::add);
        check(find_runtime_exe(b.string()) == (b / "Some_Game__Recompiled").string(),
              "a Recompiled product outranks psx-runtime");
        check(find_runtime_exe("").empty(), "an empty build dir path yields nothing");
    }

    // ---- oracle status -----------------------------------------------------
    // The oracle is machine-wide; Studio only reads what the tool reports, so
    // this parse is the entire contract between them.
    {
        OracleStatus o = parse_oracle_status(
            R"J({"kind":"psxrecomp-oracle-status","version":1,"state":"answering",)J"
            R"J("root":"/home/u/.local/share/retcomm/oracle/duckstation",)J"
            R"J("app":"/home/u/.local/share/retcomm/oracle/duckstation/app",)J"
            R"J("launcher":"/home/u/.local/share/retcomm/oracle/duckstation/app/run-oracle",)J"
            R"J("upstream_base":"ffb33c28","port":4371,"installed":true,"built":true,)J"
            R"J("running":true,"answering":true,"container_needed":true,)J"
            R"J("container_engine":"podman","container_reason":"ID_LIKE=arch",)J"
            R"J("running_detail":"yes — answering on 4371"})J");
        check(o.valid, "reads a status document");
        check(o.state == "answering" && o.answering && o.running,
              "carries the state ladder");
        check(o.port == 4371 && o.installed && o.built, "and the install facts");
        check(o.container_engine == "podman" && o.container_reason == "ID_LIKE=arch",
              "and why a container is needed");
        check(o.root.find("retcomm") != std::string::npos &&
                  o.root.find("psxrecomp") == std::string::npos,
              "install root is the shared data root, not a game repo");
    }
    {
        OracleStatus o = parse_oracle_status(
            R"J({"kind":"psxrecomp-oracle-status","state":"absent","installed":false})J");
        check(o.valid && !o.installed && o.state == "absent",
              "an absent oracle parses as absent, not as an error");
    }
    {
        // The tool logs progress before the JSON on some paths.
        OracleStatus o = parse_oracle_status(
            "[oracle] checking\n{\"kind\":\"psxrecomp-oracle-status\",\"state\":\"built\"}");
        check(o.valid && o.state == "built", "skips leading log noise");
    }
    {
        check(!parse_oracle_status("").valid, "an empty reply is not a status");
        check(!parse_oracle_status("boom").valid, "nor is garbage");
        OracleStatus w = parse_oracle_status(R"J({"kind":"something-else"})J");
        check(!w.valid && !w.error.empty(), "a foreign document is rejected by kind");
    }

    // ---- the disc the oracle must boot -------------------------------------
    // Parity is meaningless unless both emulators run the SAME image.
    {
        const fs::path g = dir / "gameroot";
        write_file(g / "game.toml",
                   "[game]\nname = \"X\"\n"
                   "disc = \"disc/Game (USA).cue\"\n\n"
                   "[prepare_disc]\ndisc = \"disc/wrong.cue\"\n");
        check(game_disc_for(g.string()).empty(),
              "a disc that does not exist yields nothing, not a bad path");
        write_file(g / "disc" / "Game (USA).cue", "FILE\n");
        check(game_disc_for(g.string()) == (g / "disc" / "Game (USA).cue").string(),
              "resolves [game] disc against the repo root");
        write_file(g / "game.toml", "[prepare_disc]\ndisc = \"disc/Game (USA).cue\"\n");
        check(game_disc_for(g.string()).empty(),
              "reads the [game] table only, not a same-named key elsewhere");
        check(game_disc_for("").empty(), "an empty root yields nothing");
    }

    // ---- GP0 ring span -----------------------------------------------------
    // The replacement for pause/step: which frames can still be reached.
    {
        RingSpan r = parse_ring_stats(
            R"J({"id":1,"ok":true,"total":812344,"capacity":1048576,"max_words":12,)J"
            R"J("oldest_frame":40918,"newest_frame":41230})J");
        check(r.valid, "reads a gpu_ring_stats reply");
        check(r.oldest == 40918 && r.newest == 41230, "carries the capturable span");
        check(r.total == 812344 && r.capacity == 1048576, "and the ring occupancy");
        check(r.error.empty(), "with no error");
    }
    {
        RingSpan r = parse_ring_stats(R"J({"id":1,"ok":false,"error":"no gpu"})J");
        check(!r.valid && r.error.find("no gpu") != std::string::npos,
              "an ok:false reply surfaces the server's own message");
    }
    {
        // A stub answering bare {"ok":true} must not read as "ring empty" —
        // that would send you hunting a bug in the game instead of in whatever
        // is actually listening on the port.
        RingSpan r = parse_ring_stats(R"J({"id":1,"ok":true})J");
        check(!r.valid, "ok with no span is rejected, not read as an empty ring");
        check(r.error.find("psx-runtime") != std::string::npos,
              "and points at the peer rather than the game");
    }
    {
        RingSpan r = parse_ring_stats("not json at all");
        check(!r.valid && !r.error.empty(), "garbage is reported, never thrown");
        RingSpan e = parse_ring_stats("");
        check(!e.valid && !e.error.empty(), "so is an empty reply");
    }
    {
        // A genuinely empty ring is valid and distinguishable: total 0.
        RingSpan r = parse_ring_stats(
            R"J({"ok":true,"total":0,"capacity":1048576,"oldest_frame":0,)J"
            R"J("newest_frame":0})J");
        check(r.valid && r.total == 0,
              "a ring that has recorded nothing is valid with total 0");
    }

    // ---- debug-tools probe -------------------------------------------------
    // The question every "why won't it connect" starts with. A Release build
    // with PSX_DEBUG_TOOLS OFF opens no port at all, and no amount of retrying
    // the connection will change that.
    const fs::path dbgroot = dir / "dbg";
    auto cache = [&](const char* build_type, const char* tools) {
        std::string text = std::string("CMAKE_BUILD_TYPE:STRING=") + build_type + "\n";
        if (tools) text += std::string("PSX_DEBUG_TOOLS:BOOL=") + tools + "\n";
        text += "CMAKE_GENERATOR:INTERNAL=Ninja\n";
        write_file(dbgroot / "b" / "CMakeCache.txt", text.c_str());
    };

    {
        DebugToolsInfo i = probe_debug_tools(dbgroot.string(), "b");
        check(!i.configured && !i.enabled, "an unconfigured build dir is not enabled");
        check(i.summary.find("Not configured") != std::string::npos,
              "and says it is unconfigured rather than blaming the connection");
        check(i.port == 4370, "falls back to the compiled default port");
    }
    cache("Release", "OFF");
    {
        DebugToolsInfo i = probe_debug_tools(dbgroot.string(), "b");
        check(i.configured && !i.enabled && i.from_cache,
              "Release + PSX_DEBUG_TOOLS=OFF has no debug server");
        check(i.summary.find("debug_server_init") != std::string::npos,
              "and explains that nothing listens");
    }
    cache("Release", "ON");
    {
        DebugToolsInfo i = probe_debug_tools(dbgroot.string(), "b");
        check(i.enabled && i.build_type == "Release",
              "Release + PSX_DEBUG_TOOLS=ON is the combination that works");
    }
    cache("Debug", nullptr);
    {
        DebugToolsInfo i = probe_debug_tools(dbgroot.string(), "b");
        check(i.enabled && !i.from_cache,
              "with no cache entry, Debug infers ON the way runtime.cmake does");
        check(i.summary.find("inferred") != std::string::npos,
              "and marks the value as inferred rather than read");
    }
    cache("MinSizeRel", nullptr);
    check(!probe_debug_tools(dbgroot.string(), "b").enabled,
          "MinSizeRel infers OFF, matching runtime.cmake");

    // The port is a RUNTIME setting from game.toml [runtime], not a build flag.
    cache("Release", "ON");
    write_file(dbgroot / "game.toml",
               "[game]\nname = \"x\"\ndebug_port = 9999\n\n"
               "[runtime]\nwindow_title = \"x\"\ndebug_port = 4390  # comment\n\n"
               "[video]\ndebug_port = 1234\n");
    {
        DebugToolsInfo i = probe_debug_tools(dbgroot.string(), "b");
        check(i.port == 4390 && i.port_from_game_toml,
              "reads debug_port from the [runtime] table only");
    }
    write_file(dbgroot / "game.toml", "[runtime]\n# debug_port = 4390\n");
    check(probe_debug_tools(dbgroot.string(), "b").port == 4370,
          "a commented-out debug_port is not a debug_port");
    check(probe_debug_tools("", "b").configured == false,
          "an empty root probes cleanly");

    fs::remove_all(dir);
    return failures;
}

// With a repo root instead of a frames dir, report what its build would do.
int run_probe(const std::string& root) {
    const DebugToolsInfo i = probe_debug_tools(root, "build-release");
    std::printf("  %s\n", i.summary.c_str());
    std::printf("  cache: %s\n", i.cache_path.c_str());
    return 0;
}

int run_real(const std::string& dir) {
    std::printf("  -- real artifacts in %s --\n", dir.c_str());
    int loaded = 0;
    std::error_code ec;
    for (const auto& e : fs::directory_iterator(dir, ec)) {
        const std::string name = e.path().filename().string();
        if (name.size() > 13 &&
            name.compare(name.size() - 13, 13, ".summary.json") == 0) {
            FrameSummary s;
            const bool ok = load_frame_summary(s, e.path().string());
            std::printf("  %s  %s: frame %u, %u packets, %zu function(s)\n",
                        ok ? "ok  " : "FAIL", name.c_str(), s.frame, s.packets,
                        s.funcs.size());
            if (!ok || s.funcs.empty()) ++failures;
            ++loaded;
        }
    }
    const fs::path diff = fs::path(dir) / "diff.json";
    if (fs::is_regular_file(diff, ec)) {
        FrameDiff d;
        const bool ok = load_frame_diff(d, diff.string());
        std::printf("  %s  diff.json: %zu changed function(s), %zu headline(s)\n",
                    ok ? "ok  " : "FAIL", d.rows.size(), d.headlines.size());
        if (!ok) ++failures;
        for (const auto& h : d.headlines) std::printf("        • %s\n", h.c_str());
        ++loaded;
    }
    for (const auto& e : fs::directory_iterator(dir, ec)) {
        if (!e.is_directory()) continue;
        if (!fs::is_regular_file(e.path() / "layers.json", ec)) continue;
        FrameLayers l;
        const bool ok = load_frame_layers(l, e.path().string());
        std::printf("  %s  %s/layers.json: %zu layer(s)\n", ok ? "ok  " : "FAIL",
                    e.path().filename().string().c_str(), l.layers.size());
        if (!ok) ++failures;
        ++loaded;
    }
    const fs::path dl = fs::path(dir) / "dlist.json";
    if (fs::is_regular_file(dl, ec)) {
        DisplayList d;
        const bool ok = load_display_list(d, dl.string());
        std::printf("  %s  dlist.json: root %s, %d node(s), %zu primitive(s)\n",
                    ok ? "ok  " : "FAIL", d.root.c_str(), d.nodes,
                    d.prims.size());
        for (const auto& c : d.classes)
            std::printf("        • %s x%d\n", c.first.c_str(), c.second);
        if (!ok) ++failures;
        ++loaded;
    }
    const fs::path cp = fs::path(dir) / "colour_parity.json";
    if (fs::is_regular_file(cp, ec)) {
        ColourParity c;
        const bool ok = load_colour_parity(c, cp.string());
        // A side that sampled nothing prints as "—", not as 0: "peaked at zero"
        // is a finding, "no data" is not, and confusing the two sends you
        // looking for a bug in whichever emulator was merely not sampled.
        char nb[16], ob[16];
        const auto peak = [](char* b, size_t n, const ColourStats& st) -> const char* {
            if (!st.loaded || st.vertices == 0) return "—";
            std::snprintf(b, n, "%d", st.peak);
            return b;
        };
        std::printf("  %s  colour_parity.json: %s — native peak %s, oracle peak %s\n",
                    ok ? "ok  " : "FAIL",
                    c.verdict.empty() ? "(no verdict)" : c.verdict.c_str(),
                    peak(nb, sizeof(nb), c.native),
                    peak(ob, sizeof(ob), c.oracle));
        if (!ok) ++failures;
        ++loaded;
    }
    // The newer reports, loaded from whatever is actually on disk. This is the
    // mode that catches schema drift — and it is how a report containing
    // "block_leader": null (a probe that had not fired) would have been caught
    // before it aborted the Studio.
    struct { const char* file; const char* what; } extra[] = {
        {"packet_writers.json", "packet writers"},
        {"gte_check.json", "gte check"},
        {"colour_inputs.json", "colour inputs"},
        {"lockstep.json", "lockstep"},
    };
    for (const auto& e : extra) {
        const fs::path f = fs::path(dir) / e.file;
        if (!fs::is_regular_file(f, ec)) continue;
        bool ok = false;
        std::string detail;
        if (std::string(e.file) == "packet_writers.json") {
            PacketWriters w; ok = load_packet_writers(w, f.string());
            detail = std::to_string(w.writers.size()) + " writer(s)";
        } else if (std::string(e.file) == "gte_check.json") {
            GteCheck g; ok = load_gte_check(g, f.string());
            detail = g.verdict;
        } else if (std::string(e.file) == "colour_inputs.json") {
            ColourInputs c; ok = load_colour_inputs(c, f.string());
            detail = c.verdict + (c.compared_by.empty()
                                  ? " (content search)"
                                  : " (" + c.compared_by + ")");
        } else {
            Lockstep l; ok = load_lockstep(l, f.string());
            detail = l.verdict;
        }
        std::printf("  %s  %s: %s\n", ok ? "ok  " : "FAIL", e.file,
                    detail.c_str());
        if (!ok) ++failures;
        ++loaded;
    }

    if (loaded == 0) {
        std::printf("  FAIL  nothing loadable in %s\n", dir.c_str());
        ++failures;
    }
    return failures;
}

} // namespace

int main(int argc, char** argv) {
    if (argc > 2 && std::string(argv[1]) == "--probe") run_probe(argv[2]);
    else if (argc > 1) run_real(argv[1]);
    else run_fixtures();
    std::printf("%s\n", failures ? "FAILED" : "PASSED");
    return failures ? 1 : 0;
}
