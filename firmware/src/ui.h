#pragma once
#include "data.h"
#include "ble.h"

enum screen_t {
    SCREEN_SPLASH,
    SCREEN_USAGE,
    SCREEN_COUNT,
};

void ui_init(void);
void ui_update(const UsageData* data);
void ui_tick_anim(void);
void ui_cycle_view(int dir);   // side-button view nav: -1 = prev (left), +1 = next (right)
#ifdef UI_SHOT
void ui_shot_set(int view, int metric);   // QA-only: jump to a view/metric for screenshots
#endif
void ui_show_screen(screen_t screen);
void ui_toggle_splash(void);
screen_t ui_get_current_screen(void);
void ui_update_ble_status(ble_state_t state, const char* name, const char* mac);
void ui_update_battery(int percent, bool charging);
