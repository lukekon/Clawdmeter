#pragma once
#include <lvgl.h>
struct UsageData;

// Phase-A PitCrew companion look prototype — multi-view Dither Kit on device.
// Built only under -DPITCREW_PROTO. Phase B feeds it real data via proto_update();
// any field the payload omits falls back to the built-in placeholder.
//
// Views (pview 0..5 / side-button cycle):
//   0 SYS    — machine vitals only: CPU / GPU / RAM heat meters
//   1 CPU    — per-core DitherColumns heatmap + big load %
//   2 GPU    — DitherRadial load + VRAM meter
//   3 RAM    — DitherTreemap (used left, free ghost right)
//   4 CLAUDE — session+weekly coral radials, Weekly Fable limit, BY MODEL, spark
//   5 GROK   — weekly deep-blue radial, spend (API-rate activity), spark
void proto_render(lv_obj_t* scr);
void proto_cycle(int dir);
void proto_set_view(int v);
// Feed live usage/vitals; stashes a copy and re-renders the current view.
void proto_update(const UsageData* data);
