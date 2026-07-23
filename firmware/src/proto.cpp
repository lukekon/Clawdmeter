#include "proto.h"
#include "theme.h"
#include "dither.h"
#include "hal/board_caps.h"
#include <esp_heap_caps.h>
#include <cstdio>
#include <cstring>
#include <cmath>

// Departure Mono (tabular numerics, full °/—) + Styrene (letter-spaced eyebrows).
// Departure is generated into firmware/src; grammar matches PitCrew web.
LV_FONT_DECLARE(font_departure_72);
LV_FONT_DECLARE(font_departure_48);
LV_FONT_DECLARE(font_departure_32);
LV_FONT_DECLARE(font_departure_20);
LV_FONT_DECLARE(font_styrene_20);
LV_FONT_DECLARE(font_styrene_16);
LV_FONT_DECLARE(font_styrene_14);

// ── Views ───────────────────────────────────────────────────────────────────
enum { PV_SYS = 0, PV_CPU, PV_GPU, PV_RAM, PV_CLAUDE, PV_GROK, PV_COUNT };
static int       s_view = PV_SYS;
static lv_obj_t* s_scr  = nullptr;

// Canvas buffers in PSRAM — LVGL does not own them.
static void* s_bufs[16];
static int   s_nbuf = 0;

// ── Placeholder vitals ──────────────────────────────────────────────────────
static const float CPU_PCT = 38.0f;
static const float GPU_PCT = 62.0f;
static const float RAM_PCT = 57.0f;
// Claude's three independent limits (mirrors claude.ai usage panel).
static const float CLAUDE_SESSION = 63.0f;  // 5h session
static const float CLAUDE_WEEKLY  = 63.0f;  // 7d all-models weekly
static const float CLAUDE_FABLE   = 92.0f;  // separate Weekly Fable cap
static const float GROK_WEEKLY    = 21.0f;

// Per-core mock (12 threads) — CPU columns heatmap.
static const float CORES[12] = {
    0.42f, 0.38f, 0.71f, 0.15f, 0.88f, 0.55f,
    0.22f, 0.61f, 0.45f, 0.33f, 0.90f, 0.18f
};
static const int NCORES = 12;

// Claude model split of *today's usage share* (not limit fullness).
// Heavy Fable share matches the 92% Weekly Fable wall.
static const float MODEL_SPLIT[4] = { 0.22f, 0.15f, 0.08f, 0.55f };

// 7-day series (normalised 0..1 of daily ceiling).
static const float WEEK_CLAUDE[7] = { 0.42f, 0.61f, 0.35f, 0.88f, 0.55f, 0.70f, 0.73f };
static const float WEEK_GROK[7]   = { 0.12f, 0.18f, 0.09f, 0.28f, 0.15f, 0.22f, 0.21f };

// Grok keeps a roomier single-gauge rhythm (Claude is denser — own layout).
enum {
    AI_GAUGE_Y  = 44,
    AI_GAUGE_AS = 185,
    AI_TAG_Y    = 248,
    AI_SPEND_Y  = 322,
    AI_TODAY_Y  = 360,
    AI_RESET_Y  = 382,
    AI_7DAY_Y   = 406,
    AI_SPARK_Y  = 424,
    AI_SPARK_H  = 28,
};

// Limit-meter fill: coral at rest, warn as the cap approaches (not heat bands).
static lv_color_t limit_fill(float pct) {
    if (pct >= 95.0f) return PC_RED;
    if (pct >= 85.0f) return PC_ORANGE;
    return PC_CLAUDE;
}

// ── Load heat band (PitCrew heatSeed — never brand blue) ────────────────────
// >=0.78 red · >=0.55 orange · >=0.30 yellow · else green
static lv_color_t band(float pct) {
    if (pct >= 78.0f) return PC_RED;
    if (pct >= 55.0f) return PC_ORANGE;
    if (pct >= 30.0f) return PC_YELLOW;
    return PC_GREEN;
}

static uint16_t* mkbuf(int w, int h) {
    void* p = heap_caps_malloc((size_t)w * h * 2, MALLOC_CAP_SPIRAM);
    if (s_nbuf < 16) s_bufs[s_nbuf++] = p;
    return (uint16_t*)p;
}

// ── Paint jobs — re-dither in place for reveal / live-cell pulse ────────────
// CRITICAL: never mkbuf per frame. Jobs hold the existing canvas buffer.

enum PaintKind {
    PK_METER = 0,
    PK_METER_SEGS,
    PK_ARC,
    PK_COLUMNS,
    PK_SPARK,
    PK_LANE,
    PK_TREEMAP
};

struct PaintJob {
    PaintKind   kind;
    uint16_t*   buf;
    lv_obj_t*   canvas;
    int         w, h, cell;
    float       f;                 // single-value forms
    lv_color_t  color;
    float       vals[16];
    lv_color_t  colors[16];
    int         n;
    float       max_v;
    float       total;             // meter_segs total
    dither_tile_t tiles[8];
    int         ntiles;
    int         thick;             // arc
    bool        use_bands;         // arc heat-band track
    float       live_alpha;        // spark live cell
    float       reveal_scale;      // 0..1 entrance (applied at paint time)
};

static PaintJob  s_jobs[8];
static int       s_njobs = 0;
static float     s_reveal = 1.0f;
static float     s_live   = 1.0f;
static lv_timer_t* s_pulse = nullptr;

static void free_bufs() {
    for (int i = 0; i < s_nbuf; i++) heap_caps_free(s_bufs[i]);
    s_nbuf = 0;
    s_njobs = 0;
}

// Anim var marker so we only kill our reveal anim.
static int s_anim_var = 0;

static void paint_job(PaintJob* j, float reveal) {
    if (!j || !j->buf) return;
    float r = reveal;
    if (r < 0) r = 0;
    if (r > 1) r = 1;

    switch (j->kind) {
    case PK_METER:
        dither_meter(j->buf, j->w, j->h, j->f * r, j->color, j->cell);
        break;
    case PK_METER_SEGS: {
        float vs[16];
        for (int i = 0; i < j->n; i++) vs[i] = j->vals[i] * r;
        dither_meter_segs(j->buf, j->w, j->h, vs, j->colors, j->n, j->total, j->cell);
        break;
    }
    case PK_ARC: {
        float f = j->f * r;
        if (j->use_bands) {
            dither_band_t bands[4] = {
                { 0.30f, PC_GREEN  },
                { 0.55f, PC_YELLOW },
                { 0.78f, PC_ORANGE },
                { 1.00f, PC_RED    },
            };
            dither_arc_bands(j->buf, j->w, j->h, f, j->color, j->thick, j->cell, bands, 4);
        } else {
            dither_arc(j->buf, j->w, j->h, f, j->color, j->thick, j->cell);
        }
        break;
    }
    case PK_COLUMNS: {
        float vs[16];
        for (int i = 0; i < j->n; i++) vs[i] = j->vals[i] * r;
        dither_columns(j->buf, j->w, j->h, vs, j->colors, j->n, j->max_v, j->cell);
        break;
    }
    case PK_SPARK: {
        // Sweep reveal: only the left (reveal * w) columns — rest stays black
        // after clear inside sparkline. Simpler: scale data by r.
        float data[32];
        int n = j->n < 32 ? j->n : 32;
        for (int i = 0; i < n; i++) data[i] = j->vals[i] * r;
        float live = (r >= 0.98f) ? j->live_alpha : 0.0f;
        dither_sparkline(j->buf, j->w, j->h, data, n, j->max_v, j->color, j->cell, live);
        break;
    }
    case PK_LANE: {
        float vs[16];
        for (int i = 0; i < j->n; i++) vs[i] = j->vals[i] * r;
        dither_lane(j->buf, j->w, j->h, vs, j->n, j->max_v, j->color, j->cell);
        break;
    }
    case PK_TREEMAP: {
        dither_tile_t t[8];
        int nt = j->ntiles < 8 ? j->ntiles : 8;
        for (int i = 0; i < nt; i++) {
            t[i] = j->tiles[i];
            // Ghost free space stays; filled tiles grow with reveal.
            if (!t[i].ghost) t[i].value *= (0.15f + 0.85f * r);
        }
        dither_treemap(j->buf, j->w, j->h, t, nt, j->cell);
        break;
    }
    }
    if (j->canvas) lv_obj_invalidate(j->canvas);
}

static void repaint_all() {
    for (int i = 0; i < s_njobs; i++) {
        if (s_jobs[i].kind == PK_SPARK) s_jobs[i].live_alpha = s_live;
        paint_job(&s_jobs[i], s_reveal);
    }
}

static void reveal_exec(void* /*var*/, int32_t v) {
    s_reveal = v / 1000.0f;
    repaint_all();
}

static void pulse_cb(lv_timer_t* /*t*/) {
    // Quiet pulse: 0.50 → 1.0 → 0.50 over ~1.2s. Looks good frozen mid-cycle.
    static float phase = 0.0f;
    phase += 0.12f;
    if (phase > 6.283185f) phase -= 6.283185f;
    s_live = 0.50f + 0.50f * (0.5f + 0.5f * sinf(phase));
    for (int i = 0; i < s_njobs; i++) {
        if (s_jobs[i].kind != PK_SPARK) continue;
        s_jobs[i].live_alpha = s_live;
        paint_job(&s_jobs[i], s_reveal);
    }
}

static void start_motion() {
    // Kill prior motion tied to our anim var.
    lv_anim_delete(&s_anim_var, reveal_exec);
    if (s_pulse) { lv_timer_delete(s_pulse); s_pulse = nullptr; }

    s_reveal = 0.0f;
    s_live   = 1.0f;
    repaint_all();

    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, &s_anim_var);
    lv_anim_set_values(&a, 0, 1000);
    lv_anim_set_exec_cb(&a, reveal_exec);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);  // ~easeOutCubic family
    lv_anim_set_duration(&a, 420);
    lv_anim_start(&a);

    s_pulse = lv_timer_create(pulse_cb, 80, nullptr);
}

static PaintJob* add_job() {
    if (s_njobs >= 8) return nullptr;
    PaintJob* j = &s_jobs[s_njobs++];
    memset(j, 0, sizeof(*j));
    j->live_alpha = 1.0f;
    j->max_v = 1.0f;
    j->total = 1.0f;
    j->cell = 2;
    return j;
}

// Create canvas + register paint job. Initial paint at reveal=0 (then motion).
static lv_obj_t* canvas_at(lv_obj_t* scr, uint16_t* buf, int w, int h, int x, int y) {
    lv_obj_t* cv = lv_canvas_create(scr);
    lv_canvas_set_buffer(cv, buf, w, h, LV_COLOR_FORMAT_RGB565);
    lv_obj_set_pos(cv, x, y);
    return cv;
}

// ── Text helpers ────────────────────────────────────────────────────────────

static lv_obj_t* eyebrow_c(lv_obj_t* p, const char* t, lv_color_t col,
                           const lv_font_t* font, int y) {
    lv_obj_t* l = lv_label_create(p);
    lv_label_set_text(l, t);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, col, 0);
    lv_obj_set_style_text_letter_space(l, 4, 0);  // ~0.14em tracking at 20–28px
    lv_obj_align(l, LV_ALIGN_TOP_MID, 0, y);
    return l;
}

static lv_obj_t* eyebrow_at(lv_obj_t* p, const char* t, lv_color_t col,
                            const lv_font_t* font, int x, int y) {
    lv_obj_t* l = lv_label_create(p);
    lv_label_set_text(l, t);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, col, 0);
    lv_obj_set_style_text_letter_space(l, 4, 0);
    lv_obj_set_pos(l, x, y);
    return l;
}

static lv_obj_t* dep_c(lv_obj_t* p, const char* t, lv_color_t col,
                       const lv_font_t* font, int y) {
    lv_obj_t* l = lv_label_create(p);
    lv_label_set_text(l, t);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, col, 0);
    lv_obj_align(l, LV_ALIGN_TOP_MID, 0, y);
    return l;
}

static lv_obj_t* crow(lv_obj_t* p, int y, int pad_col) {
    lv_obj_t* r = lv_obj_create(p);
    lv_obj_remove_style_all(r);
    lv_obj_set_size(r, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(r, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(r, pad_col, 0);
    lv_obj_clear_flag(r, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_align(r, LV_ALIGN_TOP_MID, 0, y);
    return r;
}

static lv_obj_t* rtext(lv_obj_t* row, const char* t, lv_color_t col, const lv_font_t* f) {
    lv_obj_t* l = lv_label_create(row);
    lv_label_set_text(l, t);
    lv_obj_set_style_text_font(l, f, 0);
    lv_obj_set_style_text_color(l, col, 0);
    return l;
}

static void hairline(lv_obj_t* p, int y, int x0, int x1) {
    lv_obj_t* h = lv_obj_create(p);
    lv_obj_remove_style_all(h);
    lv_obj_set_size(h, x1 - x0, 1);
    lv_obj_set_pos(h, x0, y);
    lv_obj_set_style_bg_color(h, PC_HAIR, 0);
    lv_obj_set_style_bg_opa(h, LV_OPA_COVER, 0);
    lv_obj_clear_flag(h, LV_OBJ_FLAG_SCROLLABLE);
}

// Hero numeral + unit (Departure Mono big, unit dim), screen-centered.
static void hero_pct(lv_obj_t* scr, int pct, int y, const lv_font_t* num_font) {
    char num[8];
    snprintf(num, sizeof num, "%d", pct);
    lv_obj_t* row = crow(scr, y, 6);
    rtext(row, num, PC_TEXT, num_font);
    lv_obj_t* u = rtext(row, "%", PC_DIM, &font_styrene_20);
    lv_obj_set_style_text_letter_space(u, 0, 0);
}

// % readout centered on column at (cx, y) — for dual-arc Claude gauges.
static void pct_col(lv_obj_t* scr, int pct, int cx, int y, const lv_font_t* num_font) {
    char num[8];
    snprintf(num, sizeof num, "%d", pct);
    lv_obj_t* row = lv_obj_create(scr);
    lv_obj_remove_style_all(row);
    lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(row, 4, 0);
    lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
    rtext(row, num, PC_TEXT, num_font);
    lv_obj_t* u = rtext(row, "%", PC_DIM, &font_styrene_16);
    lv_obj_set_style_text_letter_space(u, 0, 0);
    lv_obj_update_layout(row);
    lv_obj_set_pos(row, cx - lv_obj_get_width(row) / 2, y);
}

// One radial gauge job at (x,y).
static void job_arc(lv_obj_t* scr, int x, int y, int as, float frac,
                    lv_color_t col, int thick, bool heat_bands) {
    uint16_t* b = mkbuf(as, as);
    PaintJob* j = add_job();
    if (!j || !b) return;
    j->kind = PK_ARC;
    j->buf = b; j->w = as; j->h = as; j->cell = 2;
    j->f = frac; j->color = col; j->thick = thick; j->use_bands = heat_bands;
    j->canvas = canvas_at(scr, b, as, as, x, y);
    paint_job(j, 0);
}

// Model-in-use tag with square LED in the active model's colour.
static void model_tag(lv_obj_t* scr, const char* label, lv_color_t led_col, int y) {
    lv_obj_t* tag = crow(scr, y, 8);
    lv_obj_t* led = lv_obj_create(tag);
    lv_obj_set_size(led, 8, 8);
    lv_obj_set_style_bg_color(led, led_col, 0);
    lv_obj_set_style_bg_opa(led, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(led, 0, 0);
    lv_obj_set_style_border_width(led, 0, 0);
    lv_obj_clear_flag(led, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t* m = rtext(tag, label, PC_DIM, &font_styrene_14);
    lv_obj_set_style_text_letter_space(m, 3, 0);
}

// ── Chrome: view name + page dots only (no fake LIVE) ───────────────────────

static void chrome(lv_obj_t* scr, const char* name) {
    const int W = board_caps().width;
    lv_obj_set_style_bg_color(scr, PC_BG, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    lv_obj_t* hdr = lv_label_create(scr);
    lv_label_set_text(hdr, name);
    lv_obj_set_style_text_font(hdr, &font_styrene_16, 0);
    lv_obj_set_style_text_color(hdr, PC_DIM, 0);
    lv_obj_set_style_text_letter_space(hdr, 4, 0);
    lv_obj_set_pos(hdr, 28, 24);

    const int n = PV_COUNT, dot = 6, gap = 12;
    const int total = n * dot + (n - 1) * gap;
    int x0 = (W - total) / 2;
    for (int i = 0; i < n; i++) {
        lv_obj_t* d = lv_obj_create(scr);
        lv_obj_set_size(d, dot, dot);
        lv_obj_set_pos(d, x0 + i * (dot + gap), 452);
        lv_obj_set_style_bg_color(d, i == s_view ? PC_TEXT : PC_HAIR, 0);
        lv_obj_set_style_bg_opa(d, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(d, 0, 0);
        lv_obj_set_style_border_width(d, 0, 0);
        lv_obj_clear_flag(d, LV_OBJ_FLAG_SCROLLABLE);
    }
}

// ── Shared meter / spark helpers ────────────────────────────────────────────

static void job_meter(lv_obj_t* scr, int x, int y, int w, int h,
                      float frac, lv_color_t col) {
    uint16_t* b = mkbuf(w, h);
    PaintJob* j = add_job();
    if (!j || !b) return;
    j->kind = PK_METER;
    j->buf = b; j->w = w; j->h = h; j->cell = 2;
    j->f = frac; j->color = col;
    j->canvas = canvas_at(scr, b, w, h, x, y);
    paint_job(j, 0);
}

static void job_spark(lv_obj_t* scr, int x, int y, int w, int h,
                      const float* data, int n, lv_color_t col) {
    uint16_t* b = mkbuf(w, h);
    PaintJob* j = add_job();
    if (!j || !b) return;
    j->kind = PK_SPARK;
    j->buf = b; j->w = w; j->h = h; j->cell = 2;
    j->n = n < 16 ? n : 16;
    for (int i = 0; i < j->n; i++) j->vals[i] = data[i];
    j->max_v = 1.0f;
    j->color = col;
    j->live_alpha = 1.0f;
    j->canvas = canvas_at(scr, b, w, h, x, y);
    paint_job(j, 0);
}

// ── Views ───────────────────────────────────────────────────────────────────

// SYS — machine vitals only (CPU / GPU / RAM). AI has its own views.
static void view_sys(lv_obj_t* scr) {
    chrome(scr, "SYS");
    const int W = board_caps().width;
    const int mx = 36, mw = W - 72;
    const int mh = 20;

    struct Row { const char* name; float pct; };
    Row rows[] = {
        { "CPU", CPU_PCT },
        { "GPU", GPU_PCT },
        { "RAM", RAM_PCT },
    };

    // 3 rows with generous black air. pitch 110: y=90/200/310, meters end ~330.
    const int y0 = 90, pitch = 110;
    for (int i = 0; i < 3; i++) {
        int y = y0 + i * pitch;
        float pct = rows[i].pct;
        lv_color_t col = band(pct);

        eyebrow_at(scr, rows[i].name, PC_DIM, &font_styrene_20, mx, y);

        char num[8];
        snprintf(num, sizeof num, "%d", (int)(pct + 0.5f));
        lv_obj_t* row = lv_obj_create(scr);
        lv_obj_remove_style_all(row);
        lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_column(row, 4, 0);
        lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_align(row, LV_ALIGN_TOP_RIGHT, -mx, y - 6);
        rtext(row, num, PC_TEXT, &font_departure_48);
        rtext(row, "%", PC_DIM, &font_styrene_20);

        job_meter(scr, mx, y + 48, mw, mh, pct / 100.0f, col);
    }
}

// CPU — big load % + per-core DitherColumns (the PitCrew heatmap, not a lone arc).
static void view_cpu(lv_obj_t* scr) {
    chrome(scr, "CPU");
    eyebrow_c(scr, "LOAD", PC_DIM, &font_styrene_16, 56);
    hero_pct(scr, (int)(CPU_PCT + 0.5f), 86, &font_departure_72);
    dep_c(scr, "AMD RYZEN 9 5900X", PC_DIM, &font_departure_20, 172);

    // 12-core columns — each core band-colored by its own load.
    const int W = board_caps().width;
    const int cw = W - 56, ch = 140;
    uint16_t* b = mkbuf(cw, ch);
    PaintJob* j = add_job();
    if (j && b) {
        j->kind = PK_COLUMNS;
        j->buf = b; j->w = cw; j->h = ch; j->cell = 2;
        j->n = NCORES;
        j->max_v = 1.0f;
        for (int i = 0; i < NCORES; i++) {
            j->vals[i] = CORES[i];
            j->colors[i] = band(CORES[i] * 100.0f);
        }
        j->canvas = canvas_at(scr, b, cw, ch, 28, 220);
        paint_job(j, 0);
    }

    eyebrow_c(scr, "PER CORE", PC_DIM, &font_styrene_14, 372);
    // Honest null for temp — Departure has em-dash.
    dep_c(scr, "TEMP  \xE2\x80\x94  no sensor", PC_GREY, &font_departure_20, 400);
    dep_c(scr, "4.35 GHz", PC_TEXT, &font_departure_20, 428);
}

// GPU — DitherRadial (load cycle) + VRAM capacity meter.
static void view_gpu(lv_obj_t* scr) {
    chrome(scr, "GPU");
    const int W = board_caps().width;
    const int AS = 220, ay = 52;
    int ax = (W - AS) / 2;

    uint16_t* b = mkbuf(AS, AS);
    PaintJob* j = add_job();
    if (j && b) {
        j->kind = PK_ARC;
        j->buf = b; j->w = AS; j->h = AS; j->cell = 2;
        j->f = GPU_PCT / 100.0f;
        j->color = band(GPU_PCT);
        j->thick = 22;
        j->use_bands = true;  // track pre-shows heat thresholds
        j->canvas = canvas_at(scr, b, AS, AS, ax, ay);
        paint_job(j, 0);
    }

    eyebrow_c(scr, "LOAD", PC_DIM, &font_styrene_16, ay + 72);
    hero_pct(scr, (int)(GPU_PCT + 0.5f), ay + 100, &font_departure_48);

    hairline(scr, 292, 72, W - 72);
    eyebrow_c(scr, "NVIDIA RTX 3080 Ti", PC_DIM, &font_styrene_16, 308);
    // Departure has ° — real glyph.
    dep_c(scr, "71\xC2\xB0""C  \xC2\xB7  214 W", PC_TEXT, &font_departure_32, 340);

    eyebrow_c(scr, "VRAM  9.1 / 12 GB", PC_DIM, &font_styrene_14, 388);
    float vram = 9.1f / 12.0f;
    job_meter(scr, 40, 418, W - 80, 18, vram, band(vram * 100.0f));
}

// RAM — DitherTreemap: used tiles left (varied hues), free ghost strip on RIGHT.
static void view_ram(lv_obj_t* scr) {
    chrome(scr, "RAM");
    eyebrow_c(scr, "IN USE", PC_DIM, &font_styrene_16, 52);
    hero_pct(scr, (int)(RAM_PCT + 0.5f), 80, &font_departure_72);
    dep_c(scr, "18.2 / 32 GB", PC_TEXT, &font_departure_32, 168);

    const int W = board_caps().width;
    const int tw = W - 48, th = 168;
    uint16_t* b = mkbuf(tw, th);
    PaintJob* j = add_job();
    if (j && b) {
        j->kind = PK_TREEMAP;
        j->buf = b; j->w = tw; j->h = th; j->cell = 2;
        // Used categories — distinct treemap palette hues (not heat-banded).
        // Free is ghost; dither_treemap pins it to the right edge.
        j->tiles[0] = { 7.5f,  PC_BLUE,   false };  // Apps
        j->tiles[1] = { 4.0f,  PC_ORANGE, false };  // System
        j->tiles[2] = { 4.2f,  PC_YELLOW, false };  // Cached
        j->tiles[3] = { 2.5f,  PC_GREEN,  false };  // Buffers
        j->tiles[4] = { 13.8f, PC_GREY,   true  };  // Free → right strip
        j->ntiles = 5;
        j->canvas = canvas_at(scr, b, tw, th, 24, 220);
        paint_job(j, 0);
    }

    eyebrow_c(scr, "APPS  SYSTEM  CACHED  BUFFERS  FREE", PC_DIM, &font_styrene_14, 400);
    dep_c(scr, "AVAILABLE  13.8 GB", PC_TEXT, &font_departure_20, 424);
}

// Column-centered eyebrow (for dual-arc labels).
static void brow_col(lv_obj_t* scr, const char* t, int cx, int y) {
    lv_obj_t* e = lv_label_create(scr);
    lv_label_set_text(e, t);
    lv_obj_set_style_text_font(e, &font_styrene_14, 0);
    lv_obj_set_style_text_color(e, PC_DIM, 0);
    lv_obj_set_style_text_letter_space(e, 3, 0);
    lv_obj_update_layout(e);
    lv_obj_set_pos(e, cx - lv_obj_get_width(e) / 2, y);
}

// CLAUDE — three limits (session + weekly arcs, Weekly Fable bar) + BY MODEL + footer.
// Dense on purpose: arcs shrunk to ~158px so Fable limit + model bar both fit with air.
static void view_claude(lv_obj_t* scr) {
    chrome(scr, "CLAUDE");
    const int W = board_caps().width;
    const int mx = 28;
    const int mw = W - 56;

    // ── Dual coral arcs: SESSION (5h) left, WEEKLY (7d) right ────────────────
    const int AS = 158, ay = 40, gap = 16;
    const int pair = AS * 2 + gap;
    const int x0 = (W - pair) / 2;
    const int xL = x0, xR = x0 + AS + gap;
    const int cxL = xL + AS / 2, cxR = xR + AS / 2;

    job_arc(scr, xL, ay, AS, CLAUDE_SESSION / 100.0f, PC_CLAUDE, 14, false);
    job_arc(scr, xR, ay, AS, CLAUDE_WEEKLY  / 100.0f, PC_CLAUDE, 14, false);
    brow_col(scr, "SESSION", cxL, ay + 48);
    brow_col(scr, "WEEKLY",  cxR, ay + 48);
    // Secondary Departure size so the pair doesn't dominate the denser stack.
    pct_col(scr, (int)(CLAUDE_SESSION + 0.5f), cxL, ay + 74, &font_departure_20);
    pct_col(scr, (int)(CLAUDE_WEEKLY  + 0.5f), cxR, ay + 74, &font_departure_20);

    // ── In-use tag ──────────────────────────────────────────────────────────
    model_tag(scr, "IN USE  OPUS 4.8", PC_OPUS, 208);

    // ── Weekly Fable LIMIT (cap fullness — not share-of-usage) ───────────────
    // 92% is near the wall: orange fill + bright readout.
    {
        const float fp = CLAUDE_FABLE;
        lv_color_t fcol = limit_fill(fp);
        eyebrow_at(scr, "WEEKLY FABLE", PC_DIM, &font_styrene_14, mx, 234);

        char num[8];
        snprintf(num, sizeof num, "%d", (int)(fp + 0.5f));
        lv_obj_t* row = lv_obj_create(scr);
        lv_obj_remove_style_all(row);
        lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_column(row, 3, 0);
        lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_align(row, LV_ALIGN_TOP_RIGHT, -mx, 230);
        // Heavier readout colour when urgent.
        rtext(row, num, fcol, &font_departure_20);
        rtext(row, "%", fcol, &font_styrene_14);

        job_meter(scr, mx, 256, mw, 12, fp / 100.0f, fcol);
    }

    // ── BY MODEL proportion (usage share — distinct from the Fable *limit*) ─
    eyebrow_at(scr, "BY MODEL", PC_DIM, &font_styrene_14, mx, 280);
    {
        lv_color_t mc[4] = { PC_OPUS, PC_SONNET, PC_HAIKU, PC_FABLE };
        uint16_t* mb = mkbuf(mw, 12);
        PaintJob* mj = add_job();
        if (mj && mb) {
            mj->kind = PK_METER_SEGS;
            mj->buf = mb; mj->w = mw; mj->h = 12; mj->cell = 2;
            mj->n = 4;
            for (int i = 0; i < 4; i++) {
                mj->vals[i] = MODEL_SPLIT[i];
                mj->colors[i] = mc[i];
            }
            mj->total = 1.0f;
            mj->canvas = canvas_at(scr, mb, mw, 12, mx, 302);
            paint_job(mj, 0);
        }
    }

    // ── Spend / resets / spark (tightened footer) ───────────────────────────
    dep_c(scr, "$4.35", PC_TEXT, &font_departure_20, 328);
    eyebrow_c(scr, "TODAY", PC_DIM, &font_styrene_14, 352);
    // Session resets soon; weekly + fable share the 4d window.
    dep_c(scr, "RESETS  1H  \xC2\xB7  4D", PC_DIM, &font_departure_20, 372);
    eyebrow_c(scr, "7 DAY", PC_DIM, &font_styrene_14, 396);
    job_spark(scr, mx, 416, mw, 24, WEEK_CLAUDE, 7, PC_CLAUDE);
}

// GROK — roomy single weekly radial (deep blue); hypothetical API-rate activity $.
static void view_grok(lv_obj_t* scr) {
    chrome(scr, "GROK");
    const int W = board_caps().width;
    const int AS = AI_GAUGE_AS, ay = AI_GAUGE_Y;
    const int ax = (W - AS) / 2;
    const int cx = ax + AS / 2;

    job_arc(scr, ax, ay, AS, GROK_WEEKLY / 100.0f, PC_GROK, 16, false);
    brow_col(scr, "WEEKLY", cx, ay + 58);
    pct_col(scr, (int)(GROK_WEEKLY + 0.5f), cx, ay + 88, &font_departure_32);

    model_tag(scr, "IN USE  GROK 4.5", PC_GROK, AI_TAG_Y);

    // Hypothetical-at-API-rates activity (flat-rate sub ⇒ real bill ~$0). Non-zero
    // so the mock doesn't read as "unused."
    dep_c(scr, "$18.60", PC_TEXT, &font_departure_32, AI_SPEND_Y);
    eyebrow_c(scr, "TODAY", PC_DIM, &font_styrene_14, AI_TODAY_Y);
    dep_c(scr, "RESETS  4D 11H", PC_DIM, &font_departure_20, AI_RESET_Y);
    eyebrow_c(scr, "7 DAY", PC_DIM, &font_styrene_14, AI_7DAY_Y);
    job_spark(scr, 32, AI_SPARK_Y, W - 64, AI_SPARK_H, WEEK_GROK, 7, PC_GROK);
}

// ── Public API ──────────────────────────────────────────────────────────────

void proto_render(lv_obj_t* scr) {
    s_scr = scr;
    lv_anim_delete(&s_anim_var, reveal_exec);
    if (s_pulse) { lv_timer_delete(s_pulse); s_pulse = nullptr; }

    lv_obj_clean(scr);
    free_bufs();

    switch (s_view) {
        case PV_CPU:    view_cpu(scr);    break;
        case PV_GPU:    view_gpu(scr);    break;
        case PV_RAM:    view_ram(scr);    break;
        case PV_CLAUDE: view_claude(scr); break;
        case PV_GROK:   view_grok(scr);   break;
        default:        view_sys(scr);    break;
    }
    start_motion();
}

void proto_cycle(int dir) {
    if (!s_scr) return;
    s_view = (s_view + dir + PV_COUNT) % PV_COUNT;
    proto_render(s_scr);
}

void proto_set_view(int v) {
    if (!s_scr) return;
    s_view = ((v % PV_COUNT) + PV_COUNT) % PV_COUNT;
    proto_render(s_scr);
}
