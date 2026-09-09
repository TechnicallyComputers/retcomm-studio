// snes_load_test.cpp — the SNES Diagnostics tab's data path, with no GPU and no
// window.
//
//   ./build/snes_load_test                    # embedded fixtures only
//   ./build/snes_load_test <host> <port>      # also talk to a running runtime
//
// The second form is the one that matters. It drives the exact code the Palette
// and APU panes drive — SnesDebugClient over the bare line protocol, then
// parse_cgram — so a protocol drift between snesrecomp and this viewer fails
// here instead of showing up as an empty swatch grid nobody can explain.

#include "studio/studio_snes.hpp"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <vector>
#include <thread>

namespace fs = std::filesystem;
using namespace retcomm::studio;

namespace {

int failures = 0;
void check(bool ok, const char* what) {
    std::printf("  %s  %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++failures;
}

// A real dump_cgram reply is one contiguous hex string, not space-separated
// bytes — the shape that broke my first parser.
std::string fixture_cgram(uint16_t fill, int fill_count) {
    std::string hex;
    for (int i = 0; i < 256; ++i) {
        const uint16_t v = i < fill_count ? fill : static_cast<uint16_t>(i * 7);
        char buf[8];
        std::snprintf(buf, sizeof(buf), "%02x%02x", v & 0xFF, (v >> 8) & 0xFF);
        hex += buf;
    }
    return "{\"len\":512,\"hex\":\"" + hex + "\"}";
}

void test_fixtures() {
    std::printf("fixtures\n");

    CgramView v;
    check(parse_cgram(v, fixture_cgram(0x03F8, 73)), "parse_cgram accepts a real reply");
    check(v.loaded, "loaded");
    check(v.top_value == 0x03F8, "most-repeated value found");
    check(v.top_count == 73, "census counts the fill");
    // #C6FF00 is the colour that cost a day on Gundam Wing; a 5->8 bit
    // replication is what makes it that and not #C6F800.
    check(bgr555_to_rgb(0x03F8) == 0xC6FF00u, "BGR555 0x03F8 -> #C6FF00");
    check(bgr555_to_rgb(0x7FFF) == 0xFFFFFFu, "BGR555 0x7FFF -> white");
    check(bgr555_to_rgb(0x0000) == 0x000000u, "BGR555 0x0000 -> black");

    CgramView bad;
    check(!parse_cgram(bad, "{\"ok\":false,\"error\":\"nope\"}"), "refusal is not a parse");
    check(!bad.error.empty(), "refusal carries an error");
    check(!parse_cgram(bad, "not json at all"), "garbage is rejected");
    check(!parse_cgram(bad, "{\"hex\":\"0011\"}"), "a short dump is rejected");

    CgramView a, b;
    parse_cgram(a, fixture_cgram(0x03F8, 73));
    parse_cgram(b, fixture_cgram(0x03F8, 73));
    check(cgram_diff_count(a, b) == 0, "identical dumps diff to 0");
    parse_cgram(b, fixture_cgram(0x03F8, 70));
    check(cgram_diff_count(a, b) == 3, "a 3-entry difference is counted as 3");
    check(cgram_diff_count(a, CgramView{}) == -1, "an unloaded side diffs to -1");

    // The raster CSV is the oracle's answer to "at which scanline", so a
    // silently-dropped row is a split that looks like it never happened.
    {
        const std::string path =
            (fs::temp_directory_path() / ".snes_load_test_raster.csv").string();
        {
            std::FILE* f = std::fopen(path.c_str(), "wb");
            if (!f) {
                check(false, "raster csv fixture is writable");
                return;
            }
            std::fputs("frame,scanline,hclock,addr,reg,value\n", f);
            std::fputs("2500,0,12,212C,TM,17\n", f);
            std::fputs("2500,96,340,2131,CGADSUB,3F\n", f);
            std::fputs("garbage,row,here\n", f);
            std::fputs("2500,224,4,2100,INIDISP,8F\n", f);
            std::fclose(f);
        }
        std::vector<RasterRow> rows;
        std::string err;
        check(load_raster_csv(rows, path, &err), "raster csv loads");
        check(rows.size() == 3, "3 good rows, malformed one skipped");
        if (rows.size() == 3) {
            check(rows[1].scanline == 96, "mid-frame scanline preserved");
            check(rows[1].reg == "CGADSUB", "register name preserved");
            check(rows[2].value == "8F", "value preserved");
        }
        std::remove(path.c_str());
        std::vector<RasterRow> none;
        check(!load_raster_csv(
                  none, (fs::temp_directory_path() / ".no_such_raster.csv").string(), &err),
              "a missing csv is an error, not an empty success");
    }

    // The loop-compare loader is checked against a REAL artifact when one is
    // present: a fixture I invent proves only that I can parse my own guess.
    {
        LoopCompare lc;
        const char* env = std::getenv("RETCOMM_LOOP_COMPARE");
        const std::string real =
            env && *env
                ? std::string(env)
                : (fs::path(
                       "/home/alex/Documents/GitHub/GundamWingEndlessDuelSNESRecomp")
                   / "analysis" / "diagnostics" / "loop_compare.json")
                      .string();
        if (fs::exists(real)) {
            check(load_loop_compare(lc, real), "loop_compare: real artifact loads");
            // A partial artifact (one visit) is a legitimate thing to
            // find: the tool writes after every visit precisely so an
            // abandoned run leaves something behind.
            check(!lc.visits.empty(), "loop_compare: at least one visit");
            check(!lc.visits[0].irq_chain.empty(), "loop_compare: chain parsed");
            std::printf("     %d visits, %d diffs, selector %s\n",
                        (int)lc.visits.size(), (int)lc.diffs.size(),
                        lc.selector.c_str());
        } else {
            std::printf("  skip  loop_compare: no artifact at %s\n", real.c_str());
        }
        LoopCompare missing;
        check(!load_loop_compare(
                  missing,
                  (fs::temp_directory_path() / ".no_such_loop_compare.json").string()),
              "loop_compare: a missing file is an error, not an empty success");
        check(!missing.error.empty(), "loop_compare: missing file carries an error");
    }

    MesenStatus ms = parse_mesen_status("{\"installed\":true,\"binary\":\"/usr/bin/mesen-ce\","
                                        "\"provider\":\"system\"}");
    check(ms.installed, "mesen status: installed");
    check(ms.binary == "/usr/bin/mesen-ce", "mesen status: binary");
    ms = parse_mesen_status("");
    check(!ms.installed && !ms.error.empty(), "empty status is an error, not a yes");
}

void test_live(const char* host, int port) {
    std::printf("live runtime %s:%d\n", host, port);
    SnesDebugClient& c = snes_debug_client();
    c.start(host, port);

    // Give the worker a moment to connect and take its first heartbeat.
    SnesDebugClient::Snapshot snap;
    for (int i = 0; i < 60; ++i) {
        snap = c.snapshot();
        if (snap.state == SnesDebugClient::State::Connected && snap.frame > 0) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }
    check(snap.state == SnesDebugClient::State::Connected, "connected");
    check(snap.frame > 0, "frame counter is advancing");
    if (snap.state != SnesDebugClient::State::Connected) {
        std::printf("  (%s)\n", snap.error.c_str());
        c.stop();
        return;
    }
    std::printf("  frame %llu\n", static_cast<unsigned long long>(snap.frame));

    c.request("dump_cgram", "cgram");
    std::string reply;
    for (int i = 0; i < 60 && !c.take_reply("cgram", reply); ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    check(!reply.empty(), "dump_cgram answered");

    CgramView v;
    const bool ok = parse_cgram(v, reply);
    check(ok, "dump_cgram parsed");
    if (ok) {
        std::printf("  CGRAM: %d distinct, %d zero, most repeated 0x%04X x%d (#%06X)\n",
                    v.distinct, v.zero_count, v.top_value, v.top_count,
                    bgr555_to_rgb(v.top_value));
        check(v.distinct > 1, "more than one colour — a 1-colour CGRAM is a dead PPU");
    }

    c.request("get_apu_state", "apu");
    reply.clear();
    for (int i = 0; i < 40 && !c.take_reply("apu", reply); ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    check(!reply.empty(), "get_apu_state answered");

    c.stop();
}

} // namespace

int main(int argc, char** argv) {
    test_fixtures();
    if (argc >= 3) test_live(argv[1], std::atoi(argv[2]));
    else std::printf("live runtime: skipped (pass <host> <port> to include it)\n");
    std::printf("%s\n", failures ? "FAILURES" : "all ok");
    return failures ? 1 : 0;
}
