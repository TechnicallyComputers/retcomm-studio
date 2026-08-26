#pragma once
// studio_debug.hpp — live connection to the runtime's TCP debug server.
//
// This is the one place Studio talks to a RUNNING game. Everything else in the
// app is a viewer over artifacts produced out-of-process, and the Functions tab
// in particular rests on a claim — that what it shows was proven from the
// executable alone — which a live connection sits right next to.
//
// The rule that keeps both honest is the same one the analyzer applies to
// overlay captures: dynamic evidence is always LABELLED and never silently
// merged into a static claim. "60 static callers" and "4,812 observed calls"
// are different columns with different provenance, and the comparison between
// them is the point — a function with observed calls and no static caller was
// reached indirectly, which is exactly the gap static analysis reports and
// cannot close on its own.
//
// Threading: every socket operation happens on a worker thread. A hung or dead
// runtime must never stall a frame of the UI.

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace retcomm::studio {

class DebugClient {
public:
    enum class State { Disconnected, Connecting, Connected, Failed };

    struct Snapshot {
        State       state = State::Disconnected;
        uint64_t    frame = 0;
        std::string error;
        std::string last_command;
        std::string last_response;
        // func addr -> times observed entering. Populated only while live
        // tracing is on; cleared when it is reset.
        std::map<uint32_t, uint64_t> calls;
        // func addr -> distinct return addresses seen calling it. A caller the
        // static call graph does not know about is an indirect edge.
        std::map<uint32_t, std::vector<uint32_t>> callers;
        uint64_t entry_total = 0;
        uint64_t consumed = 0;
        bool     live_trace = false;
    };

    ~DebugClient();

    void start(std::string host, int port);
    void stop();
    bool running() const { return running_.load(); }

    // Queue a raw command. `fields` is the JSON body without id/braces, e.g.
    // R"("cmd":"fntrace_arm","target":"0x8002A724")".
    // The reply is kept only as a truncated string for the status line.
    void send(std::string fields, std::string label);

    // Queue a command whose FULL reply is retained under `label` until taken.
    // Frame dumps run to megabytes; send()'s status-line truncation would
    // silently eat them, so anything a caller means to parse comes through
    // here instead.
    void request(std::string fields, std::string label);

    // Take (and clear) a retained reply. False when it has not arrived yet.
    bool take_reply(const std::string& label, std::string& out);

    // True while a request with this label is queued or in flight.
    bool pending(const std::string& label) const;

    // Turn the per-function entry tally on/off over [lo, hi).
    void set_live_trace(bool on, uint32_t lo, uint32_t hi);
    void reset_counts();

    Snapshot snapshot();

private:
    struct Job {
        std::string fields;
        std::string label;
        bool        retain = false;
    };

    void worker();
    bool ensure_connected();
    bool exchange(const std::string& fields, std::string& response);
    bool poll_frame();   // false = nothing answered
    void poll_entries();
    void close_socket();

    std::thread             thread_;
    std::atomic<bool>       running_{false};
    std::atomic<bool>       quit_{false};

    mutable std::mutex      mu_;
    std::condition_variable cv_;
    Snapshot                snap_;
    std::vector<Job>        outbox_;
    std::map<std::string, std::string> replies_;   // label -> full reply
    std::vector<std::string>           in_flight_;

    std::string host_ = "127.0.0.1";
    int         port_ = 4370;
    int         sock_ = -1;
    int         next_id_ = 1;
    std::string rx_;               // leftover bytes between newline-framed replies

    std::atomic<bool>     trace_on_{false};
    std::atomic<uint32_t> trace_lo_{0};
    std::atomic<uint32_t> trace_hi_{0};
    uint64_t              cursor_ = 0;   // next fn_entry seq to consume
};

// One connection per Studio process, shared by every tab that talks to a
// running game. Two clients would mean two sockets racing the same debug
// server, and the runtime's answers are stateful (fn_filter, cursors).
DebugClient& shared_debug_client();

// A second client, for the DuckStation oracle on its own port. Separate from
// the game's client because the two are independent emulators: connecting to
// one must not disturb the other, and both are polled at once.
DebugClient& oracle_debug_client();

} // namespace retcomm::studio
