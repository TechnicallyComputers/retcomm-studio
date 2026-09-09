// studio_frames_data.cpp — loaders for the Frames tab's artifacts.
//
// Deliberately free of ImGui and OpenGL so it can be built and tested with no
// GPU and no window, exactly like studio_analysis.cpp. Everything the tab
// displays comes through here, so a test over these functions is a test over
// what the tab can possibly show.

#include "studio/studio_frames.hpp"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cctype>
#include <cstring>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#ifndef _WIN32
#include <unistd.h>
#endif

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace retcomm::studio {

namespace {

bool read_json(const std::string& path, json& out, std::string& err) {
    std::error_code ec;
    if (!fs::is_regular_file(path, ec)) {
        err = "not found: " + path;
        return false;
    }
    std::ifstream in(path);
    if (!in) {
        err = "cannot open: " + path;
        return false;
    }
    try {
        in >> out;
    } catch (const std::exception& e) {
        err = std::string("malformed JSON in ") + path + ": " + e.what();
        return false;
    }
    return true;
}

uint32_t parse_hex(const std::string& s) {
    if (s.empty()) return 0;
    return static_cast<uint32_t>(std::strtoul(s.c_str(), nullptr, 16));
}

// Counts object -> "PolyG4+semi x12, Rect x3", biggest first, capped so a row
// stays readable. The tail count is shown rather than silently dropped.
std::string join_counts(const json& obj, size_t max_terms = 4) {
    if (!obj.is_object() || obj.empty()) return {};
    std::vector<std::pair<std::string, uint64_t>> v;
    for (auto it = obj.begin(); it != obj.end(); ++it)
        v.emplace_back(it.key(), it.value().is_number() ? it.value().get<uint64_t>() : 0);
    std::sort(v.begin(), v.end(), [](const auto& a, const auto& b) {
        if (a.second != b.second) return a.second > b.second;
        return a.first < b.first;
    });
    std::string out;
    for (size_t i = 0; i < v.size() && i < max_terms; ++i) {
        if (!out.empty()) out += ", ";
        out += v[i].first + " x" + std::to_string(v[i].second);
    }
    if (v.size() > max_terms)
        out += " (+" + std::to_string(v.size() - max_terms) + " more)";
    return out;
}

std::vector<std::pair<std::string, uint32_t>> counts_desc(const json& obj) {
    std::vector<std::pair<std::string, uint32_t>> v;
    if (!obj.is_object()) return v;
    for (auto it = obj.begin(); it != obj.end(); ++it)
        v.emplace_back(it.key(), it.value().is_number() ? it.value().get<uint32_t>() : 0u);
    std::sort(v.begin(), v.end(), [](const auto& a, const auto& b) {
        if (a.second != b.second) return a.second > b.second;
        return a.first < b.first;
    });
    return v;
}

int opt_int(const json& j, const char* key, int fallback) {
    if (!j.contains(key) || j[key].is_null()) return fallback;
    return j[key].get<int>();
}

} // namespace

namespace {

bool cmake_bool(std::string v) {
    for (auto& ch : v) ch = static_cast<char>(::toupper(static_cast<unsigned char>(ch)));
    return v == "ON" || v == "1" || v == "TRUE" || v == "YES" || v == "Y";
}

// Minimal, targeted TOML read: the [runtime] table's debug_port. Studio has no
// TOML parser and does not need one — a full parse here would be a second
// implementation of a schema the runtime already owns.
bool game_toml_debug_port(const fs::path& game_toml, int& out) {
    std::ifstream in(game_toml);
    if (!in) return false;
    std::string line;
    bool in_runtime = false;
    while (std::getline(in, line)) {
        const size_t hash = line.find('#');
        std::string body = hash == std::string::npos ? line : line.substr(0, hash);
        const size_t b = body.find_first_not_of(" \t");
        if (b == std::string::npos) continue;
        body = body.substr(b);
        if (body[0] == '[') {
            in_runtime = body.rfind("[runtime]", 0) == 0;
            continue;
        }
        if (!in_runtime) continue;
        if (body.rfind("debug_port", 0) != 0) continue;
        const size_t eq = body.find('=');
        if (eq == std::string::npos) continue;
        const int v = std::atoi(body.c_str() + eq + 1);
        if (v <= 0 || v > 65535) return false;
        out = v;
        return true;
    }
    return false;
}

} // namespace

namespace {

// A line with any trailing comment removed, leading whitespace trimmed.
//
// Quote-aware, because these files carry absolute paths from a user's dump
// library and a '#' is legal in a directory name. Cutting at the first '#'
// unconditionally would truncate such a path to something that does not exist,
// and the symptom -- "disc not found" for a disc that is right there -- names
// nothing that would lead you here.
std::string toml_body(const std::string& line) {
    bool quoted = false;
    size_t end = line.size();
    for (size_t i = 0; i < line.size(); ++i) {
        if (line[i] == '"' && (i == 0 || line[i - 1] != '\\')) quoted = !quoted;
        else if (line[i] == '#' && !quoted) { end = i; break; }
    }
    std::string body = line.substr(0, end);
    const size_t b = body.find_first_not_of(" \t");
    return b == std::string::npos ? std::string() : body.substr(b);
}

// Does `body` assign `key`, as opposed to merely starting with its letters?
//
// A prefix test is not enough in [game]: `disc`, `discs` and `disc_serials`
// all live there, so looking for "disc" matched `disc_serials = ["SCUS-94163",
// ...]` and handed back a serial where a path was expected. The next character
// has to be whitespace or the '='.
bool toml_key_is(const std::string& body, const char* key) {
    const size_t n = std::strlen(key);
    if (body.compare(0, n, key) != 0) return false;
    if (body.size() == n) return false;
    const char c = body[n];
    return c == '=' || c == ' ' || c == '\t';
}

// Minimal, targeted TOML read, same approach as the debug_port lookup: pull one
// key out of one table rather than growing a parser for a schema the runtime
// already owns.
bool game_toml_string(const fs::path& game_toml, const char* table,
                      const char* key, std::string& out) {
    std::ifstream in(game_toml);
    if (!in) return false;
    std::string line;
    bool in_table = false;
    const std::string want_table = std::string("[") + table + "]";
    while (std::getline(in, line)) {
        const std::string body = toml_body(line);
        if (body.empty()) continue;
        if (body[0] == '[') {
            in_table = body.rfind(want_table, 0) == 0;
            continue;
        }
        if (!in_table || !toml_key_is(body, key)) continue;
        const size_t eq = body.find('=');
        if (eq == std::string::npos) continue;
        const size_t q1 = body.find('"', eq);
        if (q1 == std::string::npos) continue;
        const size_t q2 = body.find('"', q1 + 1);
        if (q2 == std::string::npos) continue;
        out = body.substr(q1 + 1, q2 - q1 - 1);
        return !out.empty();
    }
    return false;
}

// The same read for an integer value: settings.toml's `[disc] selected`.
bool game_toml_int(const fs::path& toml, const char* table, const char* key,
                   int& out) {
    std::ifstream in(toml);
    if (!in) return false;
    std::string line;
    bool in_table = false;
    const std::string want_table = std::string("[") + table + "]";
    while (std::getline(in, line)) {
        const std::string body = toml_body(line);
        if (body.empty()) continue;
        if (body[0] == '[') {
            in_table = body.rfind(want_table, 0) == 0;
            continue;
        }
        if (!in_table || !toml_key_is(body, key)) continue;
        const size_t eq = body.find('=');
        if (eq == std::string::npos) continue;
        out = std::atoi(body.c_str() + eq + 1);
        return true;
    }
    return false;
}

// An array-of-strings value: `[game] discs` and `[game] disc_serials`.
//
// Both are written one entry per line by probe_disc.py and may be hand-edited
// onto one line, so this accumulates from the '=' until the closing ']'
// wherever it falls. Still not a TOML parser -- it takes the quoted strings out
// of that span, which is the only shape the schema documents for these keys.
bool game_toml_string_array(const fs::path& toml, const char* table,
                            const char* key, std::vector<std::string>& out) {
    std::ifstream in(toml);
    if (!in) return false;
    std::string line;
    bool in_table = false, collecting = false;
    std::string span;
    const std::string want_table = std::string("[") + table + "]";
    while (std::getline(in, line)) {
        const std::string body = toml_body(line);
        if (!collecting) {
            if (body.empty()) continue;
            if (body[0] == '[') {
                in_table = body.rfind(want_table, 0) == 0;
                continue;
            }
            if (!in_table || !toml_key_is(body, key)) continue;
            const size_t open = body.find('[', body.find('='));
            if (open == std::string::npos) return false;   // not an array
            span = body.substr(open + 1);
            collecting = true;
        } else {
            span += "\n" + body;
        }
        // Unquoted ']' ends the array. A path may legally contain one, so the
        // scan has to know which side of a quote it is on.
        bool quoted = false;
        for (size_t i = 0; i < span.size(); ++i) {
            if (span[i] == '"' && (i == 0 || span[i - 1] != '\\')) quoted = !quoted;
            else if (span[i] == ']' && !quoted) { span.resize(i); collecting = false; break; }
        }
        if (!collecting) break;
    }
    if (span.empty()) return false;
    out.clear();
    for (size_t i = 0; i < span.size(); ++i) {
        if (span[i] != '"') continue;
        const size_t start = i + 1;
        size_t end = start;
        while (end < span.size() && !(span[end] == '"' && span[end - 1] != '\\')) ++end;
        if (end >= span.size()) break;
        if (end > start) out.push_back(span.substr(start, end - start));
        i = end;
    }
    return !out.empty();
}

} // namespace

namespace {
// game.toml writes these as quoted hex strings ("0x80010000"), not integers.
bool game_toml_hex(const fs::path& toml, const char* table, const char* key,
                   uint32_t& out) {
    std::string v;
    if (!game_toml_string(toml, table, key, v)) return false;
    out = static_cast<uint32_t>(std::strtoul(v.c_str(), nullptr, 0));
    return out != 0;
}
} // namespace

uint32_t game_text_end(const std::string& root) {
    if (root.empty()) return 0;
    const fs::path toml = fs::path(root) / "game.toml";
    uint32_t base = 0, size = 0;
    if (!game_toml_hex(toml, "game", "load_address", base)) return 0;
    if (!game_toml_hex(toml, "game", "text_size", size)) return 0;
    return base + size;
}

std::vector<DiscEntry> game_discs_for(const std::string& root) {
    std::vector<DiscEntry> out;
    if (root.empty()) return out;
    const fs::path toml = fs::path(root) / "game.toml";

    // `discs` first, then `disc`. That is the loader's own precedence -- the
    // schema calls `disc` "sugar for discs = [disc]" -- and it is the half that
    // was missing here: probe_disc.py writes `discs` ONLY for a set, never both,
    // so reading `disc` alone reported a verified three-disc title as having no
    // image at all and greyed out Start oracle on exactly the games that need a
    // disc chosen.
    std::vector<std::string> rel;
    if (!game_toml_string_array(toml, "game", "discs", rel)) {
        std::string one;
        if (!game_toml_string(toml, "game", "disc", one)) return out;
        rel.push_back(one);
    }
    std::vector<std::string> serials;
    game_toml_string_array(toml, "game", "disc_serials", serials);

    std::error_code ec;
    out.reserve(rel.size());
    for (size_t i = 0; i < rel.size(); ++i) {
        DiscEntry e;
        fs::path path(rel[i]);
        if (!path.is_absolute()) path = fs::path(root) / path;
        e.path = path.string();
        e.label = path.stem().string();
        e.present = fs::is_regular_file(path, ec);
        if (i < serials.size()) e.serial = serials[i];
        out.push_back(std::move(e));
    }
    return out;
}

std::string game_disc_for(const std::string& root) {
    const std::vector<DiscEntry> discs = game_discs_for(root);
    if (discs.empty() || !discs.front().present) return {};
    return discs.front().path;
}

namespace {
// The launcher-written settings.toml, which the runtime keeps NEXT TO ITS
// EXECUTABLE -- not at the project root. Empty when this project has no built
// binary to sit beside.
fs::path settings_toml_for(const StudioModel& model, const std::string& root) {
    const std::string exe = selected_game_exe(model, root);
    if (exe.empty()) return {};
    const fs::path p = fs::path(exe).parent_path() / "settings.toml";
    std::error_code ec;
    return fs::is_regular_file(p, ec) ? p : fs::path{};
}
} // namespace

int runtime_selected_disc(const StudioModel& model, const std::string& root) {
    const fs::path settings = settings_toml_for(model, root);
    if (settings.empty()) return 0;
    int n = 0;
    if (!game_toml_int(settings, "disc", "selected", n)) return 0;
    return n > 0 ? n : 0;
}

MemcardRef game_memcard_for(const StudioModel& model, const std::string& root) {
    MemcardRef ref;
    if (root.empty()) return ref;
    std::error_code ec;

    // Asked in the order of what is most likely to be the card the runtime
    // actually opened. settings.toml is written BY the runtime and carries an
    // absolute [memcard] card1, so when it exists it is not a guess.
    const fs::path settings = settings_toml_for(model, root);
    if (!settings.empty()) {
        std::string card1;
        if (game_toml_string(settings, "memcard", "card1", card1)) {
            fs::path p(card1);
            if (!p.is_absolute()) p = fs::path(root) / p;
            if (fs::is_regular_file(p, ec)) {
                ref.path = p.string();
                ref.source = "settings.toml [memcard] card1";
                ref.present = true;
                return ref;
            }
        }
        std::string dir;
        if (game_toml_string(settings, "memcard", "dir", dir)) {
            fs::path p(dir);
            if (!p.is_absolute()) p = fs::path(root) / p;
            p /= "card1.mcd";
            if (fs::is_regular_file(p, ec)) {
                ref.path = p.string();
                ref.source = "settings.toml [memcard] dir";
                ref.present = true;
                return ref;
            }
        }
    }

    // game.toml's compiled-in default. `memcard_dir` is relative to the project
    // root and defaults to "." -- which is why a hardcoded "saves/" missed
    // every project that did not override it.
    {
        std::string dir;
        if (!game_toml_string(fs::path(root) / "game.toml", "runtime",
                              "memcard_dir", dir))
            dir = ".";
        fs::path p(dir);
        if (!p.is_absolute()) p = fs::path(root) / p;
        p /= "card1.mcd";
        if (fs::is_regular_file(p, ec)) {
            ref.path = p.string();
            ref.source = "game.toml [runtime] memcard_dir";
            ref.present = true;
            return ref;
        }
    }

    // Where the cards sat before memcard_dir was consulted at all. Kept so a
    // project that has one but names it nowhere still gets a save-carrying
    // oracle rather than a silent empty card.
    for (const char* rel : {"saves/card1.mcd", "psxrecomp/card1.mcd"}) {
        const fs::path p = fs::path(root) / rel;
        if (fs::is_regular_file(p, ec)) {
            ref.path = p.string();
            ref.source = rel;
            ref.present = true;
            return ref;
        }
    }
    return ref;
}

// beetle_oracle.py answers in its own shape — it manages a different build
// (a libretro core plus a frontend, no container, no BIOS staging), so it has
// different things to report. Translated here rather than forced into
// DuckStation's schema: a manager that has no `state` should not be made to
// invent one.
static OracleStatus parse_beetle_status(const json& j) {
    OracleStatus st;
    st.kind = OracleKind::Beetle;
    st.root = j.value("root", "");
    st.app = st.root;
    st.port = j.value("port", 4380);
    st.built = j.value("built", false);
    st.installed = j.value("installed", false);
    // `listening` is a bound socket. beetle_oracle.py does not own the process
    // the way duckstation_oracle.py does (no pidfile, no launcher), so running
    // and answering are the same observation here and both come from the port.
    st.running = j.value("listening", false);
    st.answering = st.running;
    if (j.contains("known_blocker") && j["known_blocker"].is_array()) {
        for (const auto& b : j["known_blocker"])
            if (b.is_string()) st.blockers.push_back(b.get<std::string>());
    }
    if (j.contains("manifest") && j["manifest"].is_object())
        st.upstream_base = j["manifest"].value("upstream_base", "");
    st.state = st.answering  ? "answering"
               : st.installed ? "installed"
               : st.built     ? "built"
               : j.value("src", false) ? "fetched"
                                       : "absent";
    st.valid = true;
    return st;
}

OracleStatus parse_oracle_status(const std::string& json_text) {
    OracleStatus st;
    if (json_text.empty()) {
        st.error = "no reply from the oracle manager";
        return st;
    }
    try {
        // The tool prints progress before the JSON on some paths; take the
        // object, not the whole stream.
        const size_t brace = json_text.find('{');
        if (brace == std::string::npos) {
            st.error = "the oracle manager printed no JSON: " +
                       json_text.substr(0, 200);
            return st;
        }
        const json j = json::parse(json_text.substr(brace));
        if (j.value("oracle", "") == "beetle") return parse_beetle_status(j);
        if (j.value("kind", "") != "psxrecomp-oracle-status") {
            st.error = "unexpected reply from the oracle manager";
            return st;
        }
        st.state = j.value("state", "");
        st.root = j.value("root", "");
        st.app = j.value("app", "");
        st.launcher = j.value("launcher", "");
        st.upstream_base = j.value("upstream_base", "");
        st.running_detail = j.value("running_detail", "");
        st.port = j.value("port", 4371);
        st.installed = j.value("installed", false);
        st.built = j.value("built", false);
        st.running = j.value("running", false);
        st.answering = j.value("answering", false);
        st.container_needed = j.value("container_needed", false);
        if (j.contains("container_engine") && j["container_engine"].is_string())
            st.container_engine = j["container_engine"].get<std::string>();
        if (j.contains("container_reason") && j["container_reason"].is_string())
            st.container_reason = j["container_reason"].get<std::string>();
        st.valid = true;
    } catch (const std::exception& e) {
        st.error = std::string("unreadable oracle status (") + e.what() + ")";
    }
    return st;
}

PauseState parse_pause_state(const std::string& reply) {
    PauseState st;
    if (reply.empty()) {
        st.error = "no reply";
        return st;
    }
    try {
        const json j = json::parse(reply);
        if (!j.value("ok", false)) {
            const std::string err = j.value("error", std::string("failed"));
            // An older runtime answers "unknown command"; that is a build
            // without the pause gate, not a failure worth alarming about.
            if (err.find("unknown command") != std::string::npos)
                st.supported = false;
            st.error = err;
            return st;
        }
        st.paused = j.value("paused", false);
        st.auto_resumed = j.value("auto_resumed", false);
        st.stepping = j.value("stepping", 0);
        st.run_to = j.value("run_to", 0u);
        st.frame = j.value("frame", 0ull);
        st.timeout_ms = j.value("timeout_ms", 0u);
        st.valid = true;
    } catch (const std::exception& e) {
        st.error = std::string("unreadable pause_state (") + e.what() + ")";
    }
    return st;
}

RingSpan parse_ring_stats(const std::string& reply) {
    RingSpan span;
    if (reply.empty()) {
        span.error = "gpu_ring_stats: empty reply";
        return span;
    }
    try {
        const json j = json::parse(reply);
        if (!j.value("ok", false)) {
            span.error = "gpu_ring_stats: " + j.value("error", std::string("failed"));
            return span;
        }
        span.oldest = j.value("oldest_frame", 0u);
        span.newest = j.value("newest_frame", 0u);
        span.total = j.value("total", 0ull);
        span.capacity = j.value("capacity", 0u);
        // A reply that omits the span entirely is not a psx-runtime reply, and
        // treating it as "ring empty" would send the caller looking for a bug
        // in the game instead of in what it is talking to.
        if (!j.contains("oldest_frame") || !j.contains("newest_frame")) {
            span.error = "gpu_ring_stats: reply carried no frame span — is this a "
                         "psx-runtime debug server?";
            return span;
        }
        span.valid = true;
    } catch (const std::exception& e) {
        span.error = std::string("gpu_ring_stats: unreadable reply (") + e.what() + ")";
    }
    return span;
}

DebugToolsInfo probe_debug_tools(const std::string& root, const std::string& build_dir) {
    DebugToolsInfo info;
    if (root.empty()) {
        info.summary = "No game repo selected.";
        return info;
    }
    fs::path bdir(build_dir.empty() ? "build-release" : build_dir);
    if (!bdir.is_absolute()) bdir = fs::path(root) / bdir;
    const fs::path cache = bdir / "CMakeCache.txt";
    info.cache_path = cache.string();

    int port = 0;
    if (game_toml_debug_port(fs::path(root) / "game.toml", port)) {
        info.port = port;
        info.port_from_game_toml = true;
    }

    std::error_code ec;
    if (!fs::is_regular_file(cache, ec)) {
        info.summary = "Not configured yet — no CMakeCache.txt in " + bdir.string() +
                       ". Configure with debug tools ON before the Frames tab can "
                       "connect.";
        return info;
    }
    info.configured = true;

    std::ifstream in(cache);
    std::string line;
    while (std::getline(in, line)) {
        if (line.rfind("PSX_DEBUG_TOOLS:", 0) == 0 ||
            line.rfind("SNESRECOMP_ENABLE_TRACE:", 0) == 0) {
            // Two frameworks, one question. psxrecomp gates the debug server on
            // PSX_DEBUG_TOOLS; snesrecomp gates it on SNESRECOMP_ENABLE_TRACE.
            const size_t eq = line.find('=');
            if (eq != std::string::npos) {
                info.enabled = cmake_bool(line.substr(eq + 1));
                info.from_cache = true;
            }
        } else if (line.rfind("CMAKE_BUILD_TYPE:", 0) == 0) {
            const size_t eq = line.find('=');
            if (eq != std::string::npos) info.build_type = line.substr(eq + 1);
        }
    }

    if (!info.from_cache) {
        // The option was never evaluated into this cache. Fall back to the same
        // rule the framework's cmake applies, and say that it is inferred.
        // snesrecomp's SNESRECOMP_ENABLE_TRACE is plain OFF by default — it does
        // not vary with build type the way PSX_DEBUG_TOOLS does.
        std::error_code sec;
        const bool is_snes = fs::exists(fs::path(root) / "snesrecomp", sec);
        info.enabled = is_snes
            ? false
            : !(info.build_type == "Release" || info.build_type == "MinSizeRel");
    }

    const std::string bt = info.build_type.empty() ? "(unset)" : info.build_type;
    if (info.enabled) {
        info.summary = "Debug server ON — " + bt + " build, port " +
                       std::to_string(info.port) +
                       (info.port_from_game_toml ? " (game.toml [runtime] debug_port)"
                                                 : " (compiled default)") +
                       (info.from_cache ? "" : "  [inferred: debug-tools option not in cache]");
    } else {
        info.summary = "Debug server OFF — this " + bt +
                       " build never calls debug_server_init(), so nothing listens on "
                       "any port. Set Debug tools to ON and re-Configure." +
                       std::string(info.from_cache ? ""
                                                   : "  [inferred: debug-tools option "
                                                     "not in cache]");
    }
    return info;
}

ExecModeInfo probe_exec_mode(const std::string& root, const std::string& build_dir) {
    ExecModeInfo info;
    if (root.empty()) {
        info.summary = "No game repo selected.";
        return info;
    }

    std::error_code ec;
    const fs::path runner_cmake = fs::path(root) / "snesrecomp" / "runner" / "runner.cmake";
    if (!fs::is_regular_file(runner_cmake, ec)) {
        info.summary = "Not a snesrecomp port — no snesrecomp/runner/runner.cmake.";
        return info;
    }
    {
        // Ask the port's own pinned framework whether the option exists, rather
        // than assuming every SNES port is on a framework new enough to have
        // it. A grep is enough: the name appears in runner.cmake only where the
        // cache variable is declared.
        std::ifstream rc(runner_cmake);
        std::string line;
        while (std::getline(rc, line)) {
            if (line.find("SNESRECOMP_EXECUTION_DEFAULT") != std::string::npos) {
                info.supported = true;
                break;
            }
        }
    }
    if (!info.supported) {
        info.summary = "This port's pinned snesrecomp has no SNESRECOMP_EXECUTION_DEFAULT "
                       "(the option lands in feat/execution-mode-policy). Update the "
                       "snesrecomp pin to choose a policy here.";
        return info;
    }

    fs::path bdir(build_dir.empty() ? "build-release" : build_dir);
    if (!bdir.is_absolute()) bdir = fs::path(root) / bdir;
    const fs::path cache = bdir / "CMakeCache.txt";
    info.cache_path = cache.string();

    if (!fs::is_regular_file(cache, ec)) {
        info.summary = "Not configured yet — no CMakeCache.txt in " + bdir.string() +
                       ". The framework default is \"off\" (the faithful floor).";
        return info;
    }
    info.configured = true;

    std::ifstream in(cache);
    std::string line;
    while (std::getline(in, line)) {
        if (line.rfind("SNESRECOMP_EXECUTION_DEFAULT:", 0) == 0) {
            const size_t eq = line.find('=');
            if (eq != std::string::npos) {
                info.mode = line.substr(eq + 1);
                info.from_cache = true;
            }
            break;
        }
    }
    if (!info.from_cache) info.mode = "off";

    // Deliberately "starts from", not "runs": SNESRECOMP_EXECUTION_MODE and
    // SNESRECOMP_FORCE_FLOOR still decide the policy in the launched process,
    // and a tab that claimed otherwise would be wrong every time someone used
    // the Env box below.
    info.summary = "Builds start from policy \"" + info.mode + "\"" +
                   (info.from_cache ? "" : "  [inferred: option not in cache]") +
                   ". SNESRECOMP_EXECUTION_MODE / SNESRECOMP_FORCE_FLOOR still "
                   "override it at launch.";
    return info;
}

namespace {

// Score a candidate the way buildops.py's find_runtime_exe() does, so both
// launch paths agree on which binary is "the game".
int exe_score(const fs::path& p, const fs::path& build_dir) {
    const std::string name = p.filename().string();
    std::string lower = name;
    for (auto& ch : lower) ch = static_cast<char>(::tolower(static_cast<unsigned char>(ch)));
    for (const char* ext : {".so", ".dll", ".dylib", ".a", ".lib", ".pdb",
                            ".cmake", ".ninja", ".txt", ".json"})
        if (lower.size() > std::strlen(ext) &&
            lower.compare(lower.size() - std::strlen(ext), std::strlen(ext), ext) == 0)
            return -1;
    if (lower == "cmake" || lower == "ninja" || lower == "cpack" || lower == "ctest")
        return -1;
    int score = 0;
    if (lower.find("recompil") != std::string::npos) score += 100;
    if (name == "psx-runtime" || name == "psx-runtime.exe") score += 50;
    if (p.parent_path() == build_dir) score += 20;
    return score;
}

bool is_executable_file(const fs::path& p) {
    std::error_code ec;
    if (!fs::is_regular_file(p, ec)) return false;
#ifdef _WIN32
    return true;
#else
    return ::access(p.string().c_str(), X_OK) == 0;
#endif
}

} // namespace

// The executable the BUILD TAB would run, for this project.
//
// Both Diagnostics pages used to call find_runtime_exe() on the build
// directory and stop there, which quietly ignored the Build tab's explicit
// Exe override: you would point Build at one binary, launch from Diagnostics,
// and get whichever one the directory scan happened to rank first. Same
// resolution order as buildops.launch() (`exe_path = Path(exe) if exe else
// find_runtime_exe(bdir)`), so all three launch paths agree on what "the
// build" means.
std::string selected_game_exe(const StudioModel& model, const std::string& root) {
    std::error_code ec;
    if (model.build_exe[0]) {
        fs::path e(model.build_exe);
        if (!e.is_absolute() && !root.empty()) e = fs::path(root) / e;
        if (fs::is_regular_file(e, ec)) return e.string();
        // A stale override is worth falling back from rather than failing on:
        // the directory scan still finds a real binary, and the Build tab is
        // where that field gets corrected.
    }
    if (root.empty()) return {};
    return find_runtime_exe((fs::path(root) / model.build_dir).string());
}

std::string find_runtime_exe(const std::string& build_dir) {
    if (build_dir.empty()) return {};
    std::error_code ec;
    const fs::path bdir = fs::path(build_dir);
    if (!fs::is_directory(bdir, ec)) return {};

    int best_score = -1;
    fs::path best;
    const auto consider = [&](const fs::path& p) {
        if (!is_executable_file(p)) return;
        const int sc = exe_score(p, bdir);
        if (sc > best_score) {
            best_score = sc;
            best = p;
        }
    };
    for (const auto& e : fs::directory_iterator(bdir, ec)) {
        if (ec) break;
        if (e.is_regular_file()) consider(e.path());
    }
    // Multi-config generators put the product one level down.
    for (const char* sub : {"Release", "RelWithDebInfo", "Debug", "MinSizeRel"}) {
        const fs::path d = bdir / sub;
        if (!fs::is_directory(d, ec)) continue;
        for (const auto& e : fs::directory_iterator(d, ec)) {
            if (ec) break;
            if (e.is_regular_file()) consider(e.path());
        }
    }
    return best_score >= 0 ? best.string() : std::string();
}

std::string frames_dir_for(const std::string& root) {
    if (root.empty()) return {};
    return (fs::path(root) / "analysis" / "frames").string();
}

// Studio's own copy of the PSX analysis tools, or empty when it cannot be
// found. RETCOMM_STUDIO_TOOLKIT is exported by resolve_runtime(), so this
// needs no plumbing through the twenty gpu_tool_path() call sites.
fs::path studio_analysis_dir() {
    const char* env = std::getenv("RETCOMM_STUDIO_TOOLKIT");
    if (!env || !*env) return {};
    std::error_code ec;
    const fs::path d = fs::path(env).parent_path() / "psx_analysis";
    return fs::is_directory(d, ec) ? d : fs::path{};
}

// Load the generated capability tables. Failure is not fatal and must not be:
// an empty table gates nothing, so a missing or unreadable oracle_caps.json
// costs you the greying, not the buttons.
void load_oracle_caps(StudioModel& model) {
    if (model.oracle_caps_loaded) return;
    model.oracle_caps_loaded = true;
    const fs::path dir = studio_analysis_dir();
    if (dir.empty()) {
        model.oracle_caps_error = "no psx_analysis directory beside the toolkit";
        return;
    }
    const fs::path path = dir / "oracle_caps.json";
    json j;
    std::string err;
    if (!read_json(path.string(), j, err)) {
        model.oracle_caps_error = err;
        return;
    }
    try {
        for (auto it = j.at("oracles").begin(); it != j.at("oracles").end(); ++it) {
            std::set<std::string> cmds;
            for (const auto& c : it.value().at("commands"))
                if (c.is_string()) cmds.insert(c.get<std::string>());
            model.oracle_caps[it.key()] = std::move(cmds);
        }
        if (j.contains("tools")) {
            for (auto it = j.at("tools").begin(); it != j.at("tools").end(); ++it) {
                std::vector<std::string> needs;
                for (const char* key : {"duckstation", "beetle"}) {
                    const std::string field = std::string("needs_") + key;
                    if (it.value().contains(field) && !it.value()[field].empty())
                        needs.push_back(key);
                }
                if (!needs.empty()) model.oracle_tool_needs[it.key()] = std::move(needs);
            }
        }
    } catch (const std::exception& e) {
        model.oracle_caps_error = std::string("unreadable oracle_caps.json (") + e.what() + ")";
    }
}

// Why `tool` cannot run against the selected oracle, or empty when it can.
//
// Only a command that exactly ONE oracle registers and psx-runtime does NOT
// can decide this — everything else a tool sends may have gone to the runtime,
// and pinning on a shared command greys out the wrong half. gen_oracle_caps.py
// does that subtraction; this just reads the answer.
std::string oracle_tool_blocker(const StudioModel& model, const std::string& tool) {
    auto it = model.oracle_tool_needs.find(tool);
    if (it == model.oracle_tool_needs.end()) return {};
    const std::string want = oracle_key(model.frm_oracle_kind);
    for (const std::string& need : it->second)
        if (need == want) return {};
    if (it->second.empty()) return {};
    const std::string needed = it->second.front();
    return std::string(tool) + " needs commands only " +
           (needed == "beetle" ? "Beetle PSX" : "DuckStation") +
           " serves — switch the oracle above, or it will fail with "
           "\"unknown command\"";
}

std::string gpu_tool_path(const std::string& root, const std::string& tool) {
    std::error_code ec;
    // Studio's copy first, deliberately. These tools were maintained on a
    // psxrecomp feature branch and were once lost outright to a
    // `git pull --rebase` in a game's submodule; worse, which of them a
    // project HAD depended on the framework revision that port happened to
    // pin, so the same button worked in one repo and was greyed out in the
    // next. Shipping them with Studio makes the answer the same everywhere
    // and independent of anything merging upstream.
    const fs::path own = studio_analysis_dir();
    if (!own.empty()) {
        const fs::path mine = own / tool;
        if (fs::is_regular_file(mine, ec)) return mine.string();
    }
    if (root.empty()) return {};
    // Fall back to the project's engine checkout for a tool Studio does not
    // ship — and so a psxrecomp working tree still wins for someone actively
    // developing one of these against their own edits.
    const fs::path p = fs::path(root) / "psxrecomp" / "tools" / tool;
    if (fs::is_regular_file(p, ec)) return p.string();
    // A checkout that vendored the engine elsewhere, or the engine repo opened
    // directly as the project root.
    const fs::path alt = fs::path(root) / "tools" / tool;
    if (fs::is_regular_file(alt, ec)) return alt.string();
    return {};
}

bool load_frame_summary(FrameSummary& out, const std::string& path) {
    out = FrameSummary{};
    out.path = path;
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-gpu-frame-summary") {
        out.error = path + ": not a psx-gpu-frame-summary";
        return false;
    }
    out.label = j.value("label", "");
    out.dump = j.value("dump", "");
    out.frame = j.value("frame", 0u);
    out.packets = j.value("packets", 0u);
    out.drawing = j.value("drawing", 0u);
    out.truncated = j.value("truncated", 0u);
    out.capped = j.value("capped", false);
    if (j.contains("area") && j["area"].is_array() && j["area"].size() == 4)
        for (int i = 0; i < 4; ++i) out.area[i] = j["area"][i].get<int>();

    out.ops = counts_desc(j.value("ops", json::object()));
    out.modes = counts_desc(j.value("modes", json::object()));

    for (const auto& f : j.value("funcs", json::array())) {
        FrameFuncRow r;
        r.func = f.value("func", "0x00000000");
        r.addr = parse_hex(r.func);
        r.packets = f.value("packets", 0u);
        r.drawing = f.value("drawing", 0u);
        r.semi = f.value("semi", 0u);
        r.textured = f.value("textured", 0u);
        r.ot_min = opt_int(f, "ot_min", -1);
        r.ot_max = opt_int(f, "ot_max", -1);
        r.ops = join_counts(f.value("ops", json::object()));
        r.stp = join_counts(f.value("stp_modes", json::object()), 3);
        for (const auto& ra : f.value("ras", json::array()))
            if (ra.is_string()) r.ras.push_back(parse_hex(ra.get<std::string>()));
        if (f.contains("bbox") && f["bbox"].is_array() && f["bbox"].size() == 4) {
            for (int i = 0; i < 4; ++i) r.bbox[i] = f["bbox"][i].get<int>();
            r.has_bbox = true;
        }
        out.funcs.push_back(std::move(r));
    }
    out.loaded = true;
    return true;
}

bool load_frame_diff(FrameDiff& out, const std::string& path) {
    out = FrameDiff{};
    out.path = path;
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-gpu-frame-diff") {
        out.error = path + ": not a psx-gpu-frame-diff";
        return false;
    }
    out.modes_a = counts_desc(j.value("modes_a", json::object()));
    out.modes_b = counts_desc(j.value("modes_b", json::object()));

    for (const auto& f : j.value("funcs", json::array())) {
        FrameDiffRow r;
        r.func = f.value("func", "");
        r.verdict = f.value("verdict", "");
        const auto& a = f.value("a", json::object());
        const auto& b = f.value("b", json::object());
        r.a_prims = a.value("prims", 0u);
        r.a_semi = a.value("semi", 0u);
        r.b_prims = b.value("prims", 0u);
        r.b_semi = b.value("semi", 0u);
        out.rows.push_back(std::move(r));
    }

    const auto& ops = j.value("ops", json::object());
    for (auto it = ops.begin(); it != ops.end(); ++it) {
        out.ops.emplace_back(it.key(), it.value().value("a", 0u), it.value().value("b", 0u));
    }
    std::sort(out.ops.begin(), out.ops.end(), [](const auto& x, const auto& y) {
        const long dx = static_cast<long>(std::get<2>(x)) - static_cast<long>(std::get<1>(x));
        const long dy = static_cast<long>(std::get<2>(y)) - static_cast<long>(std::get<1>(y));
        if (std::abs(dx) != std::abs(dy)) return std::abs(dx) > std::abs(dy);
        return std::get<0>(x) < std::get<0>(y);
    });

    // Headlines are derived, not read: they are the two findings that name a
    // bug on their own -- a blend mode that vanished, and a function that
    // stopped drawing. Everything else in the diff is context for those.
    for (const auto& [mode, a] : out.modes_a) {
        uint32_t b = 0;
        for (const auto& [m2, b2] : out.modes_b)
            if (m2 == mode) b = b2;
        if (a > 0 && b == 0)
            out.headlines.push_back("blend mode " + mode + " drew " + std::to_string(a) +
                                    " primitive(s) in A and none in B");
    }
    for (const auto& [mode, b] : out.modes_b) {
        uint32_t a = 0;
        for (const auto& [m2, a2] : out.modes_a)
            if (m2 == mode) a = a2;
        if (b > 0 && a == 0)
            out.headlines.push_back("blend mode " + mode + " is new in B (" +
                                    std::to_string(b) + " primitive(s))");
    }
    int stopped = 0;
    std::string first_stopped;
    for (const auto& r : out.rows) {
        if (r.verdict == "stopped drawing") {
            if (stopped == 0) first_stopped = r.func;
            ++stopped;
        }
    }
    if (stopped == 1)
        out.headlines.push_back(first_stopped + " drew in A and nothing in B");
    else if (stopped > 1)
        out.headlines.push_back(std::to_string(stopped) +
                                " functions drew in A and nothing in B (first: " +
                                first_stopped + ")");
    out.loaded = true;
    return true;
}

namespace {
std::string bbox_str(const json& j) {
    if (!j.is_array() || j.size() != 4) return {};
    char buf[64];
    std::snprintf(buf, sizeof(buf), "[%d, %d, %d, %d]", j[0].get<int>(),
                  j[1].get<int>(), j[2].get<int>(), j[3].get<int>());
    return buf;
}
} // namespace

std::vector<WtraceWriter> parse_wtrace(const std::string& reply, std::string* err) {
    std::vector<WtraceWriter> out;
    if (reply.empty()) {
        if (err) *err = "no reply";
        return out;
    }
    try {
        const json j = json::parse(reply);
        if (!j.value("ok", false)) {
            if (err) *err = "wtrace_dump: " + j.value("error", std::string("failed"));
            return out;
        }
        std::map<uint32_t, WtraceWriter> by_pc;
        for (const auto& e : j.value("entries", json::array())) {
            WtraceWriter w;
            const auto hex = [&](const char* k) -> uint32_t {
                if (!e.contains(k)) return 0;
                if (e[k].is_string())
                    return static_cast<uint32_t>(
                        std::strtoul(e[k].get<std::string>().c_str(), nullptr, 16));
                return e[k].get<uint32_t>();
            };
            w.pc = hex("pc");
            w.func = hex("func");
            w.ra = hex("ra");
            w.dma_ch = e.value("dma_ch", -1);
            auto it = by_pc.find(w.pc);
            if (it == by_pc.end()) {
                w.count = 1;
                by_pc.emplace(w.pc, w);
            } else {
                it->second.count++;
            }
        }
        for (auto& [pc, w] : by_pc) {
            (void)pc;
            out.push_back(w);
        }
        std::sort(out.begin(), out.end(),
                  [](const WtraceWriter& a, const WtraceWriter& b) {
                      return a.count > b.count;
                  });
        if (out.empty() && err)
            *err = "no writes recorded in that range yet — let the game run "
                   "through the effect once more";
    } catch (const std::exception& e) {
        if (err) *err = std::string("unreadable wtrace_dump (") + e.what() + ")";
    }
    return out;
}

bool load_ram_parity(RamParity& out, const std::string& path) {
    out = RamParity{};
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-ram-parity") {
        out.error = path + ": not a psx-ram-parity result";
        return false;
    }
    out.addr = j.value("addr", "");
    out.length = j.value("length", 0);
    out.compared = j.value("compared", 0);
    out.identical = j.value("identical", false);
    out.differing_words = j.value("differing_words", 0);
    out.total_words = j.value("total_words", 0);
    out.native_frame = j.value("native_frame", 0ull);
    out.oracle_frame = j.value("oracle_frame", 0ull);
    out.frames_aligned = out.native_frame == out.oracle_frame;
    if (j.contains("first_difference") && j["first_difference"].is_string())
        out.first_difference = j["first_difference"].get<std::string>();
    out.loaded = true;
    return true;
}

bool load_frame_scan(FrameScan& out, const std::string& path) {
    out = FrameScan{};
    out.path = path;
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-gpu-frame-scan") {
        out.error = path + ": not a psx-gpu-frame-scan";
        return false;
    }
    if (j.contains("range") && j["range"].is_array() && j["range"].size() == 2) {
        out.lo = j["range"][0].get<int>();
        out.hi = j["range"][1].get<int>();
    }
    for (const auto& t : j.value("transitions", json::array())) {
        ScanTransition tr;
        tr.a = t.value("a", 0);
        tr.b = t.value("b", 0);
        tr.score = t.value("score", 0.0);
        for (const auto& ch : t.value("changes", json::array())) {
            ScanChange c;
            c.key = ch.value("key", "");
            c.a = ch.value("a", 0);
            c.b = ch.value("b", 0);
            c.delta = ch.value("delta", 0);
            c.cmax_a = ch.value("cmax_a", 0);
            c.cmax_b = ch.value("cmax_b", 0);
            if (ch.contains("bbox_a")) c.bbox_a = bbox_str(ch["bbox_a"]);
            if (ch.contains("bbox_b")) c.bbox_b = bbox_str(ch["bbox_b"]);
            if (ch.contains("src_b") && ch["src_b"].is_string())
                c.src_lo = ch["src_b"].get<std::string>();
            if (ch.contains("src_hi_b") && ch["src_hi_b"].is_string())
                c.src_hi = ch["src_hi_b"].get<std::string>();
            c.outside_draw_area = ch.value("outside_draw_area", false);
            c.outside_vram = ch.value("outside_vram", false);
            if (ch.contains("overflow") && ch["overflow"].is_array() &&
                ch["overflow"].size() == 4) {
                char b[128];
                std::snprintf(b, sizeof(b),
                              "left %d, top %d, right %d, bottom %d px over",
                              ch["overflow"][0].get<int>(), ch["overflow"][1].get<int>(),
                              ch["overflow"][2].get<int>(), ch["overflow"][3].get<int>());
                c.overflow = b;
            }
            tr.changes.push_back(std::move(c));
        }
        out.transitions.push_back(std::move(tr));
    }
    out.loaded = true;
    return true;
}

bool load_display_list(DisplayList& out, const std::string& path) {
    out = DisplayList{};
    out.path = path;
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-display-list") {
        out.error = path + ": not a psx-display-list";
        return false;
    }
    out.root = j.value("root", "");
    out.nodes = j.value("nodes", 0);
    out.drawing = j.value("drawing", 0);
    for (const auto& c : j.value("classes", json::array()))
        out.classes.emplace_back(c.value("key", ""), c.value("count", 0));
    for (const auto& p : j.value("prims", json::array())) {
        DisplayPrim d;
        d.op = p.value("op", "");
        d.blend = p.value("blend", "");
        d.src = p.value("src", "");
        d.cmax = p.value("cmax", 0);
        d.verts = p.value("verts", "");
        d.colors = p.value("colors", "");
        out.prims.push_back(std::move(d));
    }
    out.loaded = true;
    return true;
}

namespace {
ColourStats colour_stats(const json& j) {
    ColourStats c;
    if (!j.is_object()) return c;
    c.vertices = j.value("vertices", 0);
    c.peak = j.value("peak", 0);
    c.p50 = j.value("p50", 0);
    c.p90 = j.value("p90", 0);
    c.mean = j.value("mean", 0.0);
    if (j.contains("hist") && j["hist"].is_array())
        for (const auto& v : j["hist"]) c.hist.push_back(v.get<double>());
    c.loaded = true;
    return c;
}
}  // namespace

bool load_colour_parity(ColourParity& out, const std::string& path) {
    out = ColourParity{};
    out.path = path;
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-colour-parity") {
        out.error = path + ": not a psx-colour-parity";
        return false;
    }
    if (j.contains("class") && j["class"].is_string())
        out.klass = j["class"].get<std::string>();
    if (j.contains("verdict") && j["verdict"].is_string())
        out.verdict = j["verdict"].get<std::string>();
    if (j.contains("native")) out.native = colour_stats(j["native"]);
    if (j.contains("oracle")) out.oracle = colour_stats(j["oracle"]);
    out.overlap = j.value("overlap", 0.0);
    out.sample_ratio = j.value("sample_ratio", 1.0);
    out.loaded = true;
    return true;
}

namespace {
// Null-safe string read.
//
// nlohmann's value<std::string>() THROWS when the key exists but holds null,
// and an uncaught throw here takes the whole Studio down -- which is exactly
// what happened: a report wrote "block_leader": null for a probe that had not
// fired, and clicking the row aborted the process. A viewer must never die on
// the content of a file it is shown; a missing field is a missing field.
std::string jstr(const json& j, const char* key, const char* dflt = "") {
    if (!j.is_object()) return dflt;
    auto it = j.find(key);
    if (it == j.end() || !it->is_string()) return dflt;
    return it->get<std::string>();
}

// Same for integers: a null here throws just as readily.
long long jint(const json& j, const char* key, long long dflt = 0) {
    if (!j.is_object()) return dflt;
    auto it = j.find(key);
    if (it == j.end() || !it->is_number_integer()) return dflt;
    return it->get<long long>();
}
}  // namespace

bool load_packet_writers(PacketWriters& out, const std::string& path) {
    out = PacketWriters{};
    out.path = path;
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-packet-writers") {
        out.error = path + ": not a psx-packet-writers report";
        return false;
    }
    out.klass = jstr(j, "class");
    out.absent = j.value("absent", false);
    out.note = jstr(j, "note");
    out.lo = jstr(j, "lo");
    out.hi = jstr(j, "hi");
    out.packets = (int)jint(j, "packets");
    out.colour_words = (int)jint(j, "colour_words");
    out.vertex_words = (int)jint(j, "vertex_words");
    out.writes = (int)jint(j, "writes");
    out.unmapped = (int)jint(j, "unmapped");
    out.aliased = j.value("aliased", false);
    out.no_writes = j.value("no_writes", false);
    out.mixed_writers = (int)jint(j, "mixed_writers");
    for (const auto& w : j.value("writers", json::array())) {
        PacketWriter p;
        p.pc = w.value("pc", "");
        p.func = w.value("func", "");
        p.colour = w.value("colour", 0);
        p.vertex = w.value("vertex", 0);
        p.uv = w.value("uv", 0);
        p.colour_only = w.value("colour_only", false);
        if (j.contains("listings") && j["listings"].contains(p.pc)) {
            for (const auto& l : j["listings"][p.pc]) {
                DisasmLine d;
                d.pc = l.value("pc", "");
                d.word = l.value("word", "");
                d.text = l.value("text", "");
                d.is_target = l.value("is_target", false);
                p.listing.push_back(std::move(d));
            }
        }
        out.writers.push_back(std::move(p));
    }
    for (const auto& c : j.value("present", json::array()))
        out.present.emplace_back(c.value("key", ""), c.value("count", 0));
    out.loaded = true;
    return true;
}

bool load_gte_check(GteCheck& out, const std::string& path) {
    out = GteCheck{};
    out.path = path;
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-gte-check") {
        out.error = path + ": not a psx-gte-check report";
        return false;
    }
    out.verdict = j.value("verdict", "");
    out.note = j.value("note", "");
    out.checked = (int)jint(j, "intpl_checked");
    out.bad = (int)jint(j, "intpl_bad");
    out.nintpl_total = j.value("nintpl_total", 0ull);
    out.nsat_total = j.value("nsat_total", 0ull);
    out.nproj_last = j.value("nproj_last", 0ull);
    for (const auto& f : j.value("frames", json::array())) {
        GteFrameStat g;
        g.frame = f.value("frame", 0u);
        g.nproj = f.value("nproj", 0u);
        g.nsat = f.value("nsat", 0u);
        g.nflat = f.value("nflat", 0u);
        g.nintpl = f.value("nintpl", 0u);
        g.nintpl_tiny = f.value("nintpl_tiny", 0u);
        out.frames.push_back(g);
    }
    out.loaded = true;
    return true;
}

std::string parity_alignment(const std::string& path) {
    json j;
    std::string err;
    if (!read_json(path, j, err)) return {};
    if (j.value("kind", "") != "psx-gpu-parity") return {};
    const uint64_t a = j.value("native_frame", 0ull);
    const uint64_t b = j.value("duckstation_frame", 0ull);
    char buf[320];
    if (a == b && a != 0) {
        std::snprintf(buf, sizeof(buf),
                      "both parked on frame %llu — the pixel diff is meaningful",
                      (unsigned long long)a);
        return buf;
    }
    std::snprintf(buf, sizeof(buf),
                  "NOT ALIGNED: psx-runtime %llu vs oracle %llu. A pixel diff "
                  "between different frames says nothing.",
                  (unsigned long long)a, (unsigned long long)b);
    return buf;
}

bool load_colour_inputs(ColourInputs& out, const std::string& path) {
    out = ColourInputs{};
    out.path = path;
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-colour-inputs") {
        out.error = path + ": not a psx-colour-inputs report";
        return false;
    }
    out.pc = jstr(j, "pc");
    out.verdict = jstr(j, "verdict");
    out.source_addr = jstr(j, "source_addr");
    out.first_difference = jstr(j, "first_difference");
    out.native_bytes = jstr(j, "native_bytes");
    out.oracle_bytes = jstr(j, "oracle_bytes");
    out.scale = (int)jint(j, "oracle_scale");
    out.differing = (int)jint(j, "differing_bytes");
    out.source_identical = j.value("source_identical", false);
    out.native_source_addr = jstr(j, "native_source_addr");
    out.address_delta = (int)jint(j, "address_delta");
    out.partial_len = (int)jint(j, "partial_match_len");
    out.partial_stride = (int)jint(j, "partial_stride");
    out.compared_by = jstr(j, "compared_by");
    out.native_scale = (int)jint(j, "native_scale");
    out.phase_aligned = j.value("phase_aligned", false);
    out.region_differing = (int)jint(j, "region_differing_bytes");
    out.region_bytes = (int)jint(j, "region_bytes");
    out.region_first_difference = jstr(j, "region_first_difference");
    out.region_shape = jstr(j, "region_shape");
    for (const auto& c : j.value("region_clusters", json::array())) {
        ColourInputs::Cluster k;
        k.addr = jstr(c, "addr");
        k.native = jstr(c, "native");
        k.oracle = jstr(c, "oracle");
        k.length = (int)jint(c, "length");
        out.region_clusters.push_back(std::move(k));
    }
    if (j.contains("region") && j["region"].is_array() && j["region"].size() == 2) {
        out.region_lo = j["region"][0].get<std::string>();
        out.region_hi = j["region"][1].get<std::string>();
    }
    if (j.contains("native_regs")) {
        out.native_s4 = jstr(j["native_regs"], "s4");
        out.native_s6 = jstr(j["native_regs"], "s6");
    }
    if (j.contains("native_probe")) {
        out.native_block = jstr(j["native_probe"], "block_leader");
        out.probe_error = jstr(j["native_probe"], "error");
    }
    for (const auto& h : j.value("partial_hits", json::array()))
        if (h.is_string()) out.partial_hits.push_back(h.get<std::string>());
    if (j.contains("note") && out.verdict == "table-absent-on-native")
        out.error.clear();
    if (j.contains("error")) out.error = j["error"].get<std::string>();
    out.loaded = true;
    return true;
}

bool load_lockstep(Lockstep& out, const std::string& path) {
    out = Lockstep{};
    out.path = path;
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-lockstep") {
        out.error = path + ": not a psx-lockstep report (a report written "
                           "before div_kind was split out will say \"none\" here)";
        return false;
    }
    out.mode = jstr(j, "mode");
    out.verdict = j.value("verdict", "");
    out.found = j.value("found", 0) != 0;
    // The DIVERGENCE type, which the engine also calls "kind". It is kept under
    // a separate name in the report precisely so it cannot overwrite the
    // document type above; older files that predate that fix have "none" there.
    out.kind = jstr(j, "div_kind");
    if (j.contains("window") && j["window"].is_array() && j["window"].size() == 2) {
        out.window_lo = j["window"][0].get<int>();
        out.window_hi = j["window"][1].get<int>();
    }
    out.checked = j.value("segments_checked", j.value("blocks_checked", 0ull));
    for (const char* k : {"skipped_irq", "skipped_overflow", "skipped_unhandled",
                          "skipped_conflict", "skipped_disabled"})
        out.skipped += j.value(k, 0ull);
    if (j.contains("frame")) out.frame = std::to_string(j["frame"].get<uint64_t>());
    out.block = j.value("entry", j.value("block", std::string{}));
    out.pc = jstr(j, "pc");
    out.addr = jstr(j, "addr");
    out.expected = jstr(j, "interp_expected");
    out.actual = jstr(j, "compiled_actual");
    out.reg = (int)jint(j, "reg", -1);
    out.meaning = jstr(j, "meaning");
    out.dominant_skip = jstr(j, "dominant_skip");
    for (const auto& t : j.value("trace", json::array()))
        if (t.is_string()) out.trace.push_back(t.get<std::string>());
    if (j.contains("error")) out.error = j["error"].get<std::string>();
    out.loaded = true;
    return true;
}

bool load_range_writers(RangeWriters& out, const std::string& path) {
    out = RangeWriters{};
    out.path = path;
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-range-writers") {
        out.error = path + ": not a psx-range-writers report";
        return false;
    }
    out.lo = jstr(j, "lo");
    out.hi = jstr(j, "hi");
    out.writes = (int)jint(j, "writes");
    out.frames_advanced = (int)jint(j, "frames_advanced");
    out.error = jstr(j, "error");
    for (const auto& w : j.value("writers", json::array())) {
        RangeWriter r;
        r.pc = jstr(w, "pc");
        r.lo = jstr(w, "lo");
        r.hi = jstr(w, "hi");
        r.writes = (int)jint(w, "writes");
        std::string c;
        for (const auto& v : w.value("common", json::array())) {
            if (!c.empty()) c += "  ";
            c += jstr(v, "value") + " x" + std::to_string(jint(v, "count"));
        }
        r.common = c;
        out.writers.push_back(std::move(r));
    }
    // Only the busiest writer's code is shown; the rest are a click away.
    if (!out.writers.empty() && j.contains("listings")) {
        const std::string& pc = out.writers.front().pc;
        if (j["listings"].contains(pc)) {
            out.listing_pc = pc;
            for (const auto& l : j["listings"][pc]) {
                DisasmLine d;
                d.pc = jstr(l, "pc");
                d.word = jstr(l, "word");
                d.text = jstr(l, "text");
                d.is_target = l.value("is_target", false);
                out.listing.push_back(std::move(d));
            }
        }
    }
    out.loaded = true;
    return true;
}

namespace {
ScaleSide scale_side(const json& j) {
    ScaleSide s;
    if (!j.is_object()) return s;
    s.samples = (int)jint(j, "samples");
    s.distinct = (int)jint(j, "distinct");
    s.min = (int)jint(j, "min");
    s.max = (int)jint(j, "max");
    s.constant = j.value("constant", false);
    s.neutral_only = j.value("neutral_only", false);
    s.median_step = (int)jint(j, "median_step");
    s.max_step = (int)jint(j, "max_step");
    for (const auto& v : j.value("values", json::array())) {
        if (!s.values.empty()) s.values += " ";
        s.values += std::to_string(v.get<long long>());
    }
    s.loaded = true;
    return s;
}
}  // namespace

bool load_scale_trace(ScaleTrace& out, const std::string& path) {
    out = ScaleTrace{};
    out.path = path;
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-scale-trace") {
        out.error = path + ": not a psx-scale-trace report";
        return false;
    }
    out.verdict = jstr(j, "verdict");
    out.note = jstr(j, "note");
    out.granularity = jstr(j, "granularity");
    out.pc = jstr(j, "pc");
    out.reg = jstr(j, "reg");
    out.native_error = jstr(j, "native_error");
    out.oracle_error = jstr(j, "oracle_error");
    if (j.contains("native")) out.native = scale_side(j["native"]);
    if (j.contains("oracle")) out.oracle = scale_side(j["oracle"]);
    if (j.contains("error")) out.error = jstr(j, "error");
    out.loaded = true;
    return true;
}

bool load_class_census(ClassCensus& out, const std::string& path) {
    out = ClassCensus{};
    out.path = path;
    json j;
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-class-census") {
        out.error = path + ": not a psx-class-census report";
        return false;
    }
    out.verdict = jstr(j, "verdict");
    out.samples = (int)jint(j, "samples");
    out.error = jstr(j, "error");
    for (const auto& r : j.value("classes", json::array())) {
        CensusRow c;
        c.key = jstr(r, "key");
        c.native_max = (int)jint(r, "native_max");
        c.oracle_max = (int)jint(r, "oracle_max");
        c.native_med = (int)jint(r, "native_med");
        c.oracle_med = (int)jint(r, "oracle_med");
        out.rows.push_back(std::move(c));
    }
    for (const auto& a : j.value("absent_on_native", json::array()))
        if (a.is_string()) out.absent.push_back(a.get<std::string>());
    out.loaded = true;
    return true;
}

bool load_frame_layers(FrameLayers& out, const std::string& dir) {
    out = FrameLayers{};
    out.dir = dir;
    json j;
    const std::string path = (fs::path(dir) / "layers.json").string();
    if (!read_json(path, j, out.error)) return false;
    if (j.value("kind", "") != "psx-gpu-layers") {
        out.error = path + ": not a psx-gpu-layers index";
        return false;
    }
    out.frame = j.value("frame", 0u);
    out.label = j.value("label", "");
    out.composite.file = j.value("composite", "composite.png");
    out.composite.func = "composite";
    if (j.contains("sheet") && j["sheet"].is_string()) {
        out.sheet.file = j["sheet"].get<std::string>();
        out.sheet.func = "sheet";
    }
    for (const auto& l : j.value("layers", json::array())) {
        FrameLayer f;
        f.func = l.value("func", "");
        f.file = l.value("file", "");
        f.prims = l.value("prims", 0u);
        f.semi = l.value("semi", 0u);
        f.textured = l.value("textured", 0u);
        f.pixels = l.value("pixels", 0u);
        f.stp = join_counts(l.value("stp_modes", json::object()), 3);
        out.layers.push_back(std::move(f));
    }
    out.loaded = true;
    return true;
}

void scan_frame_tags(StudioModel& model) {
    model.frm_tags.clear();
    if (model.frm_dir[0] == '\0') return;
    std::error_code ec;
    if (!fs::is_directory(model.frm_dir, ec)) return;
    static const std::string kSuffix = ".summary.json";
    for (const auto& e : fs::directory_iterator(model.frm_dir, ec)) {
        if (ec) break;
        if (!e.is_regular_file()) continue;
        const std::string name = e.path().filename().string();
        if (name.size() <= kSuffix.size()) continue;
        if (name.compare(name.size() - kSuffix.size(), kSuffix.size(), kSuffix) != 0)
            continue;
        model.frm_tags.push_back(name.substr(0, name.size() - kSuffix.size()));
    }
    std::sort(model.frm_tags.begin(), model.frm_tags.end());
    model.frm_sel_a = std::min(model.frm_sel_a,
                               static_cast<int>(model.frm_tags.size()) - 1);
    model.frm_sel_b = std::min(model.frm_sel_b,
                               static_cast<int>(model.frm_tags.size()) - 1);
}

void name_frame_funcs(StudioModel& model, FrameSummary& summary) {
    if (model.fn_rows.empty()) return;
    for (auto& r : summary.funcs) {
        r.name.clear();
        // Exact entry match first; that is the only case where the analyser's
        // name is unambiguously about this address.
        for (const auto& fn : model.fn_rows) {
            if (fn.addr == r.addr) {
                r.name = fn.name;
                break;
            }
        }
        if (!r.name.empty()) continue;
        // Otherwise, the function whose [addr, end) contains it. Reported with
        // an offset so nobody reads it as an entry point.
        for (const auto& fn : model.fn_rows) {
            if (r.addr > fn.addr && fn.end > r.addr) {
                r.name = fn.name + "+0x" +
                         [&] {
                             char b[16];
                             std::snprintf(b, sizeof(b), "%X", r.addr - fn.addr);
                             return std::string(b);
                         }();
                break;
            }
        }
    }
}

} // namespace retcomm::studio
