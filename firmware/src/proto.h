#pragma once
#include <lvgl.h>
struct UsageData;

// Phase-A PitCrew companion look prototype — multi-view Dither Kit on device.
// Built only under -DPITCREW_PROTO. Phase B feeds it real data via proto_update();
// any field the payload omits falls back to the built-in placeholder.
//
// Views (pview 0..9 / side-button cycle):
//   0 SYS     — machine vitals only: CPU / GPU / RAM heat meters
//   1 CPU     — per-core DitherColumns heatmap + big load %
//   2 GPU     — DitherRadial load + VRAM meter
//   3 RAM     — DitherTreemap (used left, free ghost right)
//   4 CLAUDE  — session+weekly heat-tiered radials, IN USE chips, spend, spark
//   5 CODEX   — weekly OpenAI-teal radial, IN USE chip, spend, spark
//   6 KIMI    — session+weekly Kimi-blue radials, IN USE chip, spend, spark
//   7 GROK    — weekly deep-blue radial, spend (API-rate activity), spark
//   8 WEATHER — condition glyph + temperature headline + 12-hour type grid
//   9 MARKET  — stacked index / top-mover table with brand logos
void proto_render(lv_obj_t* scr);
void proto_cycle(int dir);
void proto_set_view(int v);
// Feed live usage/vitals; stashes a copy and re-renders the current view.
void proto_update(const UsageData* data);

// Install a mover's brand logo (RGB565A8, px*px*3 bytes) into one of three
// slots. Streamed by the daemon rather than baked, because today's movers are
// any three of a roster that changes when Luke trades — see proto.cpp. Matching
// at draw time is by SYMBOL, so slots may be filled in any order.
void proto_set_logo(int slot, const char* sym, int px, const uint8_t* data, size_t len);
