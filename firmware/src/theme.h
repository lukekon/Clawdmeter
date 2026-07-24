#pragma once
#include <lvgl.h>

// Design tokens — single source of truth for UI colors. Anthropic-inspired
// dark palette, AMOLED-friendly (true black bg). Legacy Clawdmeter tokens
// kept for the non-proto production UI path.
#define THEME_BG       lv_color_hex(0x000000)   // screen background
#define THEME_PANEL    lv_color_hex(0x1f1f1e)   // card/zone fill
#define THEME_TEXT     lv_color_hex(0xfaf9f5)   // primary text
#define THEME_DIM      lv_color_hex(0xb0aea5)   // secondary text
#define THEME_ACCENT   lv_color_hex(0xd97757)   // brand terra-cotta
#define THEME_GREEN    lv_color_hex(0x788c5d)
#define THEME_AMBER    lv_color_hex(0xd97757)
#define THEME_RED      lv_color_hex(0xc0392b)
#define THEME_BAR_BG   lv_color_hex(0x2a2a28)   // unfilled bar track

// Legacy production Grok bar ramp (ui.cpp grok_bar_color only — not proto).
#define THEME_GROK_LOW   lv_color_hex(0x3b6ef5)
#define THEME_GROK_MID   lv_color_hex(0x7a5cf0)
#define THEME_GROK_HIGH  lv_color_hex(0xb04ce6)

// ── PitCrew "CarBase" palette (companion redesign / -DPITCREW_PROTO) ─────────
// Pure-black bg, hairline geometry, heat ramp for vitals, provider IDs for AI.
//
// Load heat (vitals only): >=78 red · >=55 orange · >=30 yellow · else green
// Provider identity (AI views only — never mixed with heat):
//   Claude = coral  PC_CLAUDE · Grok = deep blue PC_GROK
#define PC_BG       lv_color_hex(0x000000)   // page background (AMOLED black)
#define PC_CARD     lv_color_hex(0x0e0e0e)   // card / panel fill
#define PC_POPOVER  lv_color_hex(0x202020)   // raised surface
#define PC_FILL     lv_color_hex(0x181818)   // subtle fill
#define PC_HAIR     lv_color_hex(0x262626)   // hairline border
#define PC_TEXT     lv_color_hex(0xffffff)   // primary text
#define PC_DIM      lv_color_hex(0x9c9c96)   // muted / eyebrow / units
#define PC_BLUE     lv_color_hex(0x3b65f9)   // CarBase blue (treemap tile, etc.)
#define PC_ORANGE   lv_color_hex(0xf09230)   // warn / heat band
#define PC_YELLOW   lv_color_hex(0xe7c547)   // mid-load band
#define PC_RED      lv_color_hex(0xe94a20)   // hot band
#define PC_GREEN    lv_color_hex(0x34a868)   // ok / low-load band
#define PC_GREY     lv_color_hex(0x8c8c86)   // no-data / nulls / free ghost

// Provider + model identity (AI views only)
#define PC_CLAUDE   lv_color_hex(0xd97757)   // Anthropic coral — all Claude gauges
#define PC_GROK     lv_color_hex(0x2b4fe0)   // deep royal blue — all Grok gauges
// Model chips/segments — one violet family (near Fable), spaced enough to read
// apart. Rings no longer use these (they're heat-tiered); these are model identity.
#define PC_OPUS     lv_color_hex(0x8b5cf6)   // vivid violet (flagship)
#define PC_SONNET   lv_color_hex(0x4f5bd6)   // indigo / blue-violet
#define PC_HAIKU    lv_color_hex(0xb9a3e8)   // pale lavender
#define PC_FABLE    lv_color_hex(0x7a6ce4)   // mid violet
