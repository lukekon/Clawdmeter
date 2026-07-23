#include "dither.h"
#include <math.h>
#include <string.h>

// Exact Bayer matrix from dither-kit/dither-paint.ts (pre-normalized form used
// as integer, then /16 with +0.5 — same thresholds as BAYER there).
static const uint8_t BAYER[4][4] = {
    { 0,  8,  2, 10},
    {12,  4, 14,  6},
    { 3, 11,  1,  9},
    {15,  7, 13,  5},
};

static inline float clamp01(float a) {
    if (a < 0.0f) return 0.0f;
    if (a > 1.0f) return 1.0f;
    return a;
}

static inline uint16_t scale565(uint16_t c, float a) {
    a = clamp01(a);
    if (a <= 0.0f) return 0;
    uint16_t r = (c >> 11) & 0x1F, g = (c >> 5) & 0x3F, b = c & 0x1F;
    r = (uint16_t)(r * a + 0.5f);
    g = (uint16_t)(g * a + 0.5f);
    b = (uint16_t)(b * a + 0.5f);
    return (uint16_t)((r << 11) | (g << 5) | b);
}

// Bayer threshold in 0..1 — matches dither-paint BAYER[y&3][x&3].
static inline float bayer_t(int x, int y, int cell) {
    int cx = (cell > 0) ? (x / cell) : x;
    int cy = (cell > 0) ? (y / cell) : y;
    return (BAYER[cy & 3][cx & 3] + 0.5f) / 16.0f;
}

// paintColumn density→alpha (dither-paint.ts): k = (0.3+d*0.7)*(1+0.22*i).
static inline float cell_alpha(float density, float t, float intensity) {
    float k = (0.30f + density * 0.70f) * (1.0f + 0.22f * intensity);
    float lit_thresh = t;  // density > BAYER
    return (density > lit_thresh) ? k : k * DITHER_OFF_TIER;
}

// Horizontal / radial meter density: dense at start (base), dissolves toward edge.
// bits.tsx DitherMeter / DitherRadial: density = 0.95 - 0.5 * along.
static inline float along_density(float along) {
    return 0.95f - 0.50f * clamp01(along);
}

// Meter-style alpha (slightly different k curve than paintColumn — bits.tsx meter).
static inline float meter_alpha(float density, float t) {
    float k = 0.35f + density * 0.60f;
    return (density > t) ? k : k * DITHER_OFF_TIER;
}

void dither_clear(uint16_t* buf, int w, int h) {
    if (!buf || w <= 0 || h <= 0) return;
    memset(buf, 0, (size_t)w * (size_t)h * 2u);
}

// ── DitherMeter ─────────────────────────────────────────────────────────────

void dither_meter(uint16_t* buf, int w, int h, float f, lv_color_t color, int cell) {
    float v = clamp01(f);
    dither_meter_segs(buf, w, h, &v, &color, 1, 1.0f, cell);
}

void dither_meter_segs(uint16_t* buf, int w, int h,
                       const float* values, const lv_color_t* colors, int n,
                       float total, int cell) {
    dither_clear(buf, w, h);
    if (!buf || w <= 0 || h <= 0 || n <= 0 || total <= 0.0f) return;
    if (cell < 1) cell = 1;

    float xf = 0.0f;
    lv_color_t last = colors[0];
    for (int i = 0; i < n; i++) {
        if (values[i] <= 0.0f) continue;
        last = colors[i];
        uint16_t c = lv_color_to_u16(colors[i]);
        float seg_w = (values[i] / total) * (float)w;
        int x0 = (int)(xf + 0.5f);
        int x1 = (int)(xf + seg_w + 0.5f);
        if (x1 <= x0) x1 = x0 + 1;
        if (x1 > w) x1 = w;
        int span = x1 - x0;
        if (span < 1) span = 1;
        for (int x = x0; x < x1; x++) {
            float along = (float)(x - x0) / (float)span;
            float density = along_density(along);
            for (int y = 0; y < h; y++) {
                float t = bayer_t(x, y, cell);
                float a = meter_alpha(density, t);
                buf[y * w + x] = scale565(c, a);
            }
        }
        // Soft value-edge border column (BORDER_ALPHA).
        int be = x1 - 1;
        if (be >= x0 && be < w) {
            uint16_t edge = scale565(c, DITHER_BORDER_ALPHA);
            for (int y = 0; y < h; y++) buf[y * w + be] = edge;
        }
        xf += seg_w;
    }
    // Sparse track for the unfilled remainder — never a solid stain.
    uint16_t lc = lv_color_to_u16(last);
    int rx = (int)(xf + 0.5f);
    if (rx < 0) rx = 0;
    for (int x = rx; x < w; x++) {
        for (int y = 0; y < h; y++) {
            if (bayer_t(x, y, cell) > 0.25f) continue;
            buf[y * w + x] = scale565(lc, DITHER_SPECKLE);
        }
    }
}

// ── DitherRadial ────────────────────────────────────────────────────────────

static void arc_paint(uint16_t* buf, int w, int h, float f, lv_color_t color,
                      int thick, int cell,
                      const dither_band_t* bands, int nbands) {
    dither_clear(buf, w, h);
    if (!buf || w <= 0 || h <= 0) return;
    if (cell < 1) cell = 1;
    f = clamp01(f);
    uint16_t c = lv_color_to_u16(color);
    const float cx = w / 2.0f, cy = h / 2.0f;
    const float R  = ((w < h ? w : h) / 2.0f) - 1.0f;
    if (thick < 2) thick = 2;
    const float ri = R - (float)thick;
    const float start = 135.0f, sweep = 270.0f;

    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            float dx = (x + 0.5f) - cx, dy = (y + 0.5f) - cy;
            float r = sqrtf(dx * dx + dy * dy);
            if (r < ri || r > R) continue;
            float A = atan2f(dy, dx) * 57.2957795f;
            if (A < 0) A += 360.0f;
            // Progress 0..1 along the 270° sweep from lower-left (135°).
            float aa = A;
            if (aa < start) aa += 360.0f;
            float t = (aa - start) / sweep;
            if (t < 0.0f || t > 1.0f) continue;

            float bt = bayer_t(x, y, cell);
            if (f > 0.0f && t <= f) {
                float along = t / f;
                float density = along_density(along);
                float a = meter_alpha(density, bt);
                buf[y * w + x] = scale565(c, a);
            } else {
                // Sparse track; optional heat-band colouring of the remainder.
                if (bt > 0.25f) continue;
                lv_color_t tc = color;
                if (bands && nbands > 0) {
                    tc = bands[nbands - 1].color;
                    for (int i = 0; i < nbands; i++) {
                        if (t <= bands[i].up_to) { tc = bands[i].color; break; }
                    }
                }
                buf[y * w + x] = scale565(lv_color_to_u16(tc), DITHER_SPECKLE);
            }
        }
    }
}

void dither_arc(uint16_t* buf, int w, int h, float f, lv_color_t color,
                int thick, int cell) {
    arc_paint(buf, w, h, f, color, thick, cell, nullptr, 0);
}

void dither_arc_bands(uint16_t* buf, int w, int h, float f, lv_color_t color,
                      int thick, int cell,
                      const dither_band_t* bands, int nbands) {
    arc_paint(buf, w, h, f, color, thick, cell, bands, nbands);
}

// ── paintColumn (vertical fill, dense at floor) ─────────────────────────────

static void paint_column(uint16_t* buf, int w, int h, int x, int top, int floor,
                         uint16_t c, int cell, float intensity) {
    if (x < 0 || x >= w) return;
    if (floor > h) floor = h;
    if (top < 0) top = 0;
    int depth = floor - top;
    if (depth <= 0) {
        if (top >= 0 && top < h)
            buf[top * w + x] = scale565(c, DITHER_BORDER_ALPHA);
        return;
    }
    for (int y = top; y < floor; y++) {
        // Inverted falloff: 0 at the top line, 1 at the floor.
        float density = (float)(y - top) / (float)depth;
        float t = bayer_t(x, y, cell);
        float a = cell_alpha(density, t, intensity);
        buf[y * w + x] = scale565(c, a);
    }
    // Soft top border + feather row (paintColumn).
    buf[top * w + x] = scale565(c, DITHER_BORDER_ALPHA);
    if (depth > 1 && top + 1 < h)
        buf[(top + 1) * w + x] = scale565(c, DITHER_BORDER_ALPHA * 0.5f);
}

// ── DitherColumns ───────────────────────────────────────────────────────────

void dither_columns(uint16_t* buf, int w, int h,
                    const float* vals, const lv_color_t* colors, int n,
                    float max_v, int cell) {
    dither_clear(buf, w, h);
    if (!buf || n <= 0 || w <= 0 || h <= 0) return;
    if (cell < 1) cell = 1;
    if (max_v < 1e-6f) max_v = 1.0f;
    const float min_fill = 0.08f;
    float slot = (float)w / (float)n;
    int gap = (slot >= 5.0f) ? 1 : 0;

    for (int i = 0; i < n; i++) {
        int x0 = (int)(i * slot + 0.5f);
        int x1 = (int)((i + 1) * slot + 0.5f) - gap;
        if (x1 <= x0) x1 = x0 + 1;
        if (x1 > w) x1 = w;
        float v = vals[i];
        if (v <= 0.0f) {
            // Baseline speckle whisper.
            for (int x = x0; x < x1; x++) {
                if (bayer_t(x, h - 1, cell) > 0.30f) continue;
                buf[(h - 1) * w + x] = scale565(lv_color_to_u16(colors[i]), 0.38f);
            }
            continue;
        }
        float frac = min_fill + clamp01(v / max_v) * (1.0f - min_fill);
        int bh = (int)(frac * h + 0.5f);
        if (bh < 1) bh = 1;
        if (bh > h) bh = h;
        int top = h - bh;
        uint16_t c = lv_color_to_u16(colors[i]);
        for (int x = x0; x < x1; x++)
            paint_column(buf, w, h, x, top, h, c, cell, 0.0f);
    }
}

// ── DitherSparkline ─────────────────────────────────────────────────────────

void dither_sparkline(uint16_t* buf, int w, int h,
                      const float* data, int n, float max_v,
                      lv_color_t color, int cell, float live_alpha) {
    dither_clear(buf, w, h);
    if (!buf || w <= 0 || h <= 0) return;
    if (cell < 1) cell = 1;
    uint16_t c = lv_color_to_u16(color);
    uint16_t grey = lv_color_to_u16(lv_color_hex(0x8c8c86));

    if (n < 2) {
        for (int x = 0; x < w; x++) {
            if (bayer_t(x, h - 1, cell) > 0.30f) continue;
            buf[(h - 1) * w + x] = scale565(grey, 0.35f);
        }
        return;
    }
    if (max_v < 1e-6f) {
        max_v = data[0];
        for (int i = 1; i < n; i++) if (data[i] > max_v) max_v = data[i];
        if (max_v < 1e-6f) max_v = 1.0f;
    }

    // Linear-resample series to canvas width (dither-paint resample).
    int last_top = h - 1;
    for (int x = 0; x < w; x++) {
        float t = (w <= 1) ? 0.0f : (float)x / (float)(w - 1) * (float)(n - 1);
        int i = (int)t;
        float fr = t - (float)i;
        if (i >= n - 1) { i = n - 2; fr = 1.0f; }
        float v = data[i] + (data[i + 1] - data[i]) * fr;
        float frac = clamp01(v / max_v);
        int top = h - (int)(frac * h + 0.5f);
        if (frac > 0.0f && top > h - 1) top = h - 1;
        if (top < 0) top = 0;
        last_top = top;
        paint_column(buf, w, h, x, top, h, c, cell, 0.0f);
    }
    // Live cell — newest reading at full (or pulsed) opacity.
    int lx = w - 1;
    int ly = last_top;
    if (ly < 0) ly = 0;
    if (ly >= h) ly = h - 1;
    buf[ly * w + lx] = scale565(c, clamp01(live_alpha));
}

// ── DitherLane ──────────────────────────────────────────────────────────────

void dither_lane(uint16_t* buf, int w, int h,
                 const float* vals, int n, float max_v,
                 lv_color_t color, int cell) {
    dither_clear(buf, w, h);
    if (!buf || n <= 0 || w <= 0 || h <= 0) return;
    if (cell < 1) cell = 1;
    if (max_v < 1e-6f) {
        max_v = vals[0];
        for (int i = 1; i < n; i++) if (vals[i] > max_v) max_v = vals[i];
        if (max_v < 1e-6f) max_v = 1.0f;
    }
    uint16_t c = lv_color_to_u16(color);
    uint16_t grey = lv_color_to_u16(lv_color_hex(0x8c8c86));
    float slot = (float)w / (float)n;

    for (int i = 0; i < n; i++) {
        int x0 = (int)(i * slot + 0.5f);
        int x1 = (int)((i + 1) * slot + 0.5f) - (slot >= 4.0f ? 1 : 0);
        if (x1 <= x0) x1 = x0 + 1;
        if (x1 > w) x1 = w;
        float v = vals[i];
        if (v <= 0.0f) {
            for (int x = x0; x < x1; x++)
                for (int y = 0; y < h; y++) {
                    if (bayer_t(x, y, cell) > 0.16f) continue;
                    buf[y * w + x] = scale565(grey, 0.30f);
                }
            continue;
        }
        float density = clamp01(0.4f + 0.6f * (v / max_v));
        for (int x = x0; x < x1; x++)
            for (int y = 0; y < h; y++) {
                float t = bayer_t(x, y, cell);
                float k = 0.35f + density * 0.60f;
                float a = (density > t) ? k : k * DITHER_OFF_TIER;
                buf[y * w + x] = scale565(c, a);
            }
    }
}

// ── DitherTreemap (squarified) ──────────────────────────────────────────────

typedef struct { float x, y, w, h; } trect_t;

static float worst_aspect(float sum, float mn, float mx, float side) {
    float s2 = sum * sum, side2 = side * side;
    float a = (side2 * mx) / s2;
    float b = s2 / (side2 * mn);
    return a > b ? a : b;
}

// values must be sorted descending. Writes n rects into out[].
static void squarify(const float* values, int n,
                     float x, float y, float w, float h, trect_t* out) {
    float total = 0;
    for (int i = 0; i < n; i++) total += values[i];
    if (total <= 0 || w <= 0 || h <= 0) {
        for (int i = 0; i < n; i++) out[i] = { x, y, 0, 0 };
        return;
    }
    float areas[16];
    if (n > 16) n = 16;
    for (int i = 0; i < n; i++) areas[i] = (values[i] / total) * w * h;

    int i = 0;
    while (i < n) {
        int horiz = (w >= h);
        float side = horiz ? h : w;
        float rowSum = areas[i], rowMin = areas[i], rowMax = areas[i];
        int end = i + 1;
        float worst = worst_aspect(rowSum, rowMin, rowMax, side);
        while (end < n) {
            float a = areas[end];
            float ns = rowSum + a;
            float nmin = rowMin < a ? rowMin : a;
            float nmax = rowMax > a ? rowMax : a;
            float nw = worst_aspect(ns, nmin, nmax, side);
            if (nw > worst) break;
            rowSum = ns; rowMin = nmin; rowMax = nmax; worst = nw;
            end++;
        }
        float thick = side > 0 ? rowSum / side : 0;
        float off = 0;
        for (int j = i; j < end; j++) {
            float len = thick > 0 ? areas[j] / thick : 0;
            if (horiz) out[j] = { x, y + off, thick, len };
            else       out[j] = { x + off, y, len, thick };
            off += len;
        }
        if (horiz) { x += thick; w -= thick; }
        else       { y += thick; h -= thick; }
        i = end;
    }
}

// Paint one tile rect into the buffer (dense-at-floor fill or ghost speckle).
static void paint_tile(uint16_t* buf, int w, int h,
                       int x0, int y0, int x1, int y1,
                       lv_color_t color, bool ghost, int cell) {
    if (x1 <= x0) x1 = x0 + 1;
    if (y1 <= y0) y1 = y0 + 1;
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 > w) x1 = w;
    if (y1 > h) y1 = h;
    uint16_t c = lv_color_to_u16(color);
    int depth = (y1 - 1) - y0;
    if (depth < 1) depth = 1;
    for (int y = y0; y < y1; y++) {
        for (int x = x0; x < x1; x++) {
            bool border = (x == x0 || x == x1 - 1 || y == y0 || y == y1 - 1);
            if (border) {
                buf[y * w + x] = scale565(c, DITHER_BORDER_ALPHA);
                continue;
            }
            if (ghost) {
                if (bayer_t(x, y, cell) > 0.22f) continue;
                buf[y * w + x] = scale565(c, 0.24f);
                continue;
            }
            float density = (float)(y - y0) / (float)depth;
            float t = bayer_t(x, y, cell);
            float a = cell_alpha(density, t, 0.0f);
            buf[y * w + x] = scale565(c, a);
        }
    }
}

void dither_treemap(uint16_t* buf, int w, int h,
                    const dither_tile_t* tiles, int n, int cell) {
    dither_clear(buf, w, h);
    if (!buf || n <= 0 || w <= 0 || h <= 0) return;
    if (cell < 1) cell = 1;
    if (n > 16) n = 16;

    // Split used vs ghost (free). Free is pinned to the RIGHT edge so the map
    // reads like a fuel gauge: used left → free right. Squarify only the used
    // tiles in the remaining left region.
    dither_tile_t used[16];
    float used_vals[16];
    int nu = 0;
    float free_sum = 0.0f, used_sum = 0.0f;
    lv_color_t free_col = lv_color_hex(0x8c8c86);
    for (int i = 0; i < n; i++) {
        if (tiles[i].value <= 0.0f) continue;
        if (tiles[i].ghost) {
            free_sum += tiles[i].value;
            free_col = tiles[i].color;
        } else {
            used[nu] = tiles[i];
            used_vals[nu] = tiles[i].value;
            used_sum += tiles[i].value;
            nu++;
        }
    }
    float total = used_sum + free_sum;
    if (total <= 0.0f) return;

    int free_w = 0;
    if (free_sum > 0.0f) {
        free_w = (int)((free_sum / total) * (float)w + 0.5f);
        if (free_w < 8) free_w = 8;
        if (free_w > w - 16) free_w = w - 16;
    }
    int used_w = w - free_w;

    // Sort used descending, then squarify into the left rect.
    for (int a = 0; a < nu; a++)
        for (int b = a + 1; b < nu; b++)
            if (used_vals[b] > used_vals[a]) {
                float tv = used_vals[a]; used_vals[a] = used_vals[b]; used_vals[b] = tv;
                dither_tile_t tt = used[a]; used[a] = used[b]; used[b] = tt;
            }

    if (nu > 0 && used_w > 0) {
        trect_t rects[16];
        squarify(used_vals, nu, 0, 0, (float)used_w, (float)h, rects);
        for (int i = 0; i < nu; i++) {
            int x0 = (int)(rects[i].x + 0.5f);
            int y0 = (int)(rects[i].y + 0.5f);
            int x1 = (int)(rects[i].x + rects[i].w + 0.5f);
            int y1 = (int)(rects[i].y + rects[i].h + 0.5f);
            paint_tile(buf, w, h, x0, y0, x1, y1, used[i].color, false, cell);
        }
    }

    if (free_w > 0) {
        paint_tile(buf, w, h, used_w, 0, w, h, free_col, true, cell);
    }
}
