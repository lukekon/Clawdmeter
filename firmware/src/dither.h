#pragma once
#include <lvgl.h>
#include <stdint.h>
#include <stdbool.h>

// PitCrew Dither Kit on device — Bayer 4×4 ordered dither, alpha-only over black.
// Matches components/dither-kit/dither-paint.ts + components/bits.tsx:
//   density gradient (dense at base → dissolves toward the value edge),
//   OFF_TIER 0.4, BORDER_ALPHA 0.72, empty remainder = sparse speckle (never a stain).
// `cell` = screen px per Bayer cell (2 = PitCrew CELL).

#define DITHER_OFF_TIER     0.40f
#define DITHER_BORDER_ALPHA 0.72f
#define DITHER_SPECKLE      0.22f

void dither_clear(uint16_t* buf, int w, int h);

// Horizontal proportion meter (DitherMeter). Dense at fill start, dissolves
// toward the value edge; soft border column; remainder = sparse track.
void dither_meter(uint16_t* buf, int w, int h, float f, lv_color_t color, int cell);

// Multi-segment horizontal meter (stacked model split, etc.). Each segment
// gets its own density gradient; remainder speckles in the last segment colour.
void dither_meter_segs(uint16_t* buf, int w, int h,
                       const float* values, const lv_color_t* colors, int n,
                       float total, int cell);

// 270° DitherRadial: opens at bottom, sweeps from lower-left. Density dense at
// start → dissolves toward the tip. Optional track bands colour the remainder.
typedef struct {
    float      up_to;   // 0..1 along the sweep
    lv_color_t color;
} dither_band_t;

void dither_arc(uint16_t* buf, int w, int h, float f, lv_color_t color,
                int thick, int cell);
void dither_arc_bands(uint16_t* buf, int w, int h, float f, lv_color_t color,
                      int thick, int cell,
                      const dither_band_t* bands, int nbands);

// DitherColumns — vertical per-entry bars, dense at floor, soft top edge.
// vals[i] in 0..1 (or absolute with max); colors[i] per column.
void dither_columns(uint16_t* buf, int w, int h,
                    const float* vals, const lv_color_t* colors, int n,
                    float max_v, int cell);

// DitherSparkline — area series (dense at baseline), bright live cell at the
// newest sample. live_alpha 0..1 modulates the live cell (pulse).
// data[i] absolute; max_v ceiling (pass 1.0 for already-normalised).
void dither_sparkline(uint16_t* buf, int w, int h,
                      const float* data, int n, float max_v,
                      lv_color_t color, int cell, float live_alpha);

// DitherLane — one-row density strip; value drives how solid each bucket is.
void dither_lane(uint16_t* buf, int w, int h,
                 const float* vals, int n, float max_v,
                 lv_color_t color, int cell);

// DitherTreemap tile: value drives area; ghost = sparse unclaimed remainder.
typedef struct {
    float      value;
    lv_color_t color;
    bool       ghost;
} dither_tile_t;

void dither_treemap(uint16_t* buf, int w, int h,
                    const dither_tile_t* tiles, int n, int cell);
