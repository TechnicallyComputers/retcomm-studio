#pragma once

#include "studio/studio_model.hpp"

#include <cstdint>
#include <map>

struct SDL_Window;

namespace retcomm::studio {

// Theme is forward-declared rather than included: studio_analysis.cpp includes
// this header and must stay free of ImGui so it can be built and tested with
// no GPU or window.
struct Theme;

// Load <root>/analysis/*.json into the model. Returns false and fills
// model.fn_error when the bundle is absent or malformed.
bool load_analysis(StudioModel& model, const std::string& root);

// Recompute model.fn_view from the current filters and sort.
void rebuild_fn_view(StudioModel& model);

// Copy the live trace's per-address call tally onto fn_rows[].live_calls so the
// table can sort by it. No-op unless `consumed` has moved since the last call;
// returns true when it did.
bool apply_live_counts(StudioModel& model, const std::map<uint32_t, uint64_t>& calls,
                       uint64_t consumed);

void draw_functions(StudioModel& model, const Theme& th, SDL_Window* window);

} // namespace retcomm::studio
