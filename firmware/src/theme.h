#pragma once
#include <lvgl.h>

// Design tokens — single source of truth for UI colors. Anthropic-inspired
// dark palette, AMOLED-friendly (true black bg).
#define THEME_BG       lv_color_hex(0x000000)   // screen background
#define THEME_PANEL    lv_color_hex(0x1f1f1e)   // card/zone fill
#define THEME_TEXT     lv_color_hex(0xfaf9f5)   // primary text
#define THEME_DIM      lv_color_hex(0xb0aea5)   // secondary text
#define THEME_ACCENT   lv_color_hex(0xd97757)   // brand terra-cotta
#define THEME_GREEN    lv_color_hex(0x788c5d)
#define THEME_AMBER    lv_color_hex(0xd97757)
#define THEME_RED      lv_color_hex(0xc0392b)
#define THEME_BAR_BG   lv_color_hex(0x2a2a28)   // unfilled bar track

// Grok (xAI) bar palette — a sapphire→violet ramp, distinct from Claude's
// green/amber/red. Grok's own brand is black/white, which reads poorly as a
// bar fill, so its meters get their own cohesive deep-blue/purple family.
#define THEME_GROK_LOW   lv_color_hex(0x3b6ef5)  // sapphire (low utilisation)
#define THEME_GROK_MID   lv_color_hex(0x7a5cf0)  // violet   (mid)
#define THEME_GROK_HIGH  lv_color_hex(0xb04ce6)  // purple   (near the weekly limit)
