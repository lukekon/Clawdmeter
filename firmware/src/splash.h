#pragma once
#include <stdint.h>
#include <lvgl.h>

// Initialize splash module. Creates the canvas widget inside `parent` and
// allocates the 480x480 pixel buffer (PSRAM).
void splash_init(lv_obj_t *parent);

// Advance animation frame if hold time elapsed. Call from main loop.
void splash_tick(void);

// Cycle to the next animation in the catalog.
void splash_next(void);

// Jump straight to a named animation (e.g. "dance bounce" for a celebration).
// No-op if the name isn't in the catalog. Resets the auto-rotate timer so the
// chosen animation holds rather than being cycled away immediately.
void splash_play(const char* anim_name);

// Show/hide the splash container.
void splash_show(void);
void splash_hide(void);

// Pick the next animation matching the current usage-rate group.
// Called automatically by splash_show(); also exposed so other modules can
// trigger a re-pick when the rate group changes mid-display.
void splash_pick_for_current_rate(void);

// True when splash is currently rendering (used to gate re-picks).
bool splash_is_active(void);

// Root container (so ui.cpp can attach a click event).
lv_obj_t* splash_get_root(void);

// Mini animated creature you can embed anywhere and re-point at a different
// animation at runtime — used by the idle screen ("expression sleep") and the
// reactive Clawd on the usage view (mood follows the burn rate). Each instance
// owns its own canvas + buffer, so several can coexist (unlike a single shared
// slot). Fields are private; hold one by value and pass its address.
struct splash_mini_t {
    lv_obj_t* canvas;     // the drawable — position it with lv_obj_align/set_pos
    uint16_t* buf;
    int       cell;
    int       w;
    const void* anim;     // const splash_anim_def_t* (opaque outside splash.cpp)
    uint16_t  frame;
    uint32_t  started;
};

// Render `anim_name` at ~px×px inside `parent`. Returns false (leaving canvas
// NULL) if the animation isn't found or allocation fails. Drive with
// splash_mini_tick_one(); re-point at another animation with splash_mini_set_anim().
bool splash_mini_init(splash_mini_t* m, lv_obj_t* parent, const char* anim_name, int px);
void splash_mini_set_anim(splash_mini_t* m, const char* anim_name);  // no-op if same/unknown
void splash_mini_tick_one(splash_mini_t* m);
