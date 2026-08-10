#include "proto.h"
#include "theme.h"
#include "dither.h"
#include "data.h"
#include "splash.h"
#include "logo_grok_lobe.h"
#include "logo_kimi.h"
#include "logo_codex_anim.h"
#include "hal/board_caps.h"
#include <esp_heap_caps.h>
#include <cstdio>
#include <cstring>
#include <cctype>
#include <cmath>

// Init an lv_image_dsc for an RGB565A8 asset (w*h RGB565 LE + w*h alpha).
static void proto_icon_dsc(lv_image_dsc_t* dsc, int w, int h, const uint8_t* data) {
    dsc->header.w = w;
    dsc->header.h = h;
    dsc->header.cf = LV_COLOR_FORMAT_RGB565A8;
    dsc->header.stride = w * 2;
    dsc->data = data;
    dsc->data_size = w * h * 3;
}

// Departure Mono (tabular numerics, full °/—) + Styrene (letter-spaced eyebrows).
// Departure is generated into firmware/src; grammar matches PitCrew web.
LV_FONT_DECLARE(font_departure_72);
LV_FONT_DECLARE(font_departure_48);
LV_FONT_DECLARE(font_departure_32);
LV_FONT_DECLARE(font_departure_24);   // market table — a six-row list, not a hero
LV_FONT_DECLARE(font_departure_20);
// The panel is 2.16" square: 480 px over 38.8 mm ≈ 12.4 px/mm. At a desk viewing
// distance a glanceable numeral needs ~3 mm of cap height ≈ dep_48/72; a label
// bottoms out around styrene_24. NOTHING below styrene_20 is legible here — the
// small tiers that used to live in this file were a screenshot-only illusion.
LV_FONT_DECLARE(font_styrene_28);
LV_FONT_DECLARE(font_styrene_24);
LV_FONT_DECLARE(font_styrene_20);
LV_FONT_DECLARE(font_styrene_16);
LV_FONT_DECLARE(font_styrene_14);

// ── Views ───────────────────────────────────────────────────────────────────
// Machine vitals, then the four AI providers, then the two views that aren't
// about this desk at all.
enum { PV_SYS = 0, PV_CPU, PV_GPU, PV_RAM, PV_CLAUDE, PV_CODEX, PV_KIMI, PV_GROK,
       PV_WEATHER, PV_MARKET, PV_COUNT };
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
static const float GROK_WEEKLY    = 21.0f;
// Kimi's two limits mirror Claude's (5h session + 7d weekly), from Moonshot's usage API.
static const float KIMI_SESSION   = 61.0f;  // 5h window
static const float KIMI_WEEKLY    = 12.0f;  // 7d window
// Codex has a single 7-day window (no session one), from the local rollout logs.
static const float CODEX_WEEKLY   = 7.0f;   // 7d window

// Per-core mock (12 threads) — CPU columns heatmap.
static const float CORES[12] = {
    0.42f, 0.38f, 0.71f, 0.15f, 0.88f, 0.55f,
    0.22f, 0.61f, 0.45f, 0.33f, 0.90f, 0.18f
};
static const int NCORES = 12;

// 7-day series (normalised 0..1 of daily ceiling) — spark placeholders for QA
// without the daemon.
static const float WEEK_CLAUDE[7] = { 0.42f, 0.61f, 0.35f, 0.88f, 0.55f, 0.70f, 0.73f };
static const float WEEK_GROK[7]   = { 0.12f, 0.18f, 0.09f, 0.28f, 0.15f, 0.22f, 0.21f };

// ── Live data (Phase B) ──────────────────────────────────────────────────────
// proto_update() stashes the latest payload; the views read through the accessors
// below, which fall back to the placeholder constants above whenever a field is
// absent (no daemon yet / partial payload) so screenshot QA still renders.
// Production builds never surface those constants: each AI view short-circuits
// to no_data() when its own provider gate is false.
static UsageData s_d;
static bool      s_has = false;

static bool hcpu() { return s_has && s_d.vitals.cpu_valid; }
static bool hgpu() { return s_has && s_d.vitals.gpu_valid; }
static bool hram() { return s_has && s_d.vitals.ram_valid; }
// Claude's fields exist only when the daemon's OAuth poll succeeded ("ok");
// a dead token sends the local-only payload without them, and parsed zero
// defaults must not read as a real 0% — the Claude view alone drops to no-data.
// Claude gates on the PRESENCE of a 5h/7d reading, not on the poll having just
// succeeded ("ok"). The OAuth token only stays fresh while Claude Code runs, so
// gating on "ok" blanked the view every time Luke stopped using Claude — the one
// view that went dark exactly when the other three kept showing their last
// numbers. The daemon now projects its last-good reading (see the sidecar in
// claude_usage_daemon_windows.py); "ol" flags live vs projected.
static bool hcl()  { return s_has && s_d.valid && s_d.claude_limits_valid; }

static float cpu_pct() { return hcpu() ? (float)s_d.vitals.cpu_pct : CPU_PCT; }
static float gpu_pct() { return hgpu() ? (float)s_d.vitals.gpu_pct : GPU_PCT; }
static float ram_pct() { return hram() ? (float)s_d.vitals.ram_pct : RAM_PCT; }

static float cl_session()   { return hcl() ? s_d.session_pct : CLAUDE_SESSION; }
static float cl_weekly()    { return hcl() ? s_d.weekly_pct  : CLAUDE_WEEKLY; }
// Grok gates on ITS OWN flag ("g" present) — it rode hcl() until the dead-
// Claude-token payload made that mean "Claude is live" and blanked Grok too.
static bool  hgr()          { return s_has && s_d.valid && s_d.grok_valid; }
static float grok_pct()     { return hgr() ? s_d.grok_week_pct : GROK_WEEKLY; }
static bool  hki()          { return s_has && s_d.valid && s_d.kimi_valid; }
static float ki_session()   { return hki() ? s_d.kimi_session_pct : KIMI_SESSION; }
static float ki_weekly()    { return hki() ? s_d.kimi_weekly_pct  : KIMI_WEEKLY; }
static bool  hcd()          { return s_has && s_d.valid && s_d.codex_valid; }
// Weather and market gate on their own blocks: both are outside the AI feed and
// either can be absent (no network, a dead endpoint) without touching the rest.
static bool  hwx()          { return s_has && s_d.valid && s_d.wx_valid; }
static bool  hmk()          { return s_has && s_d.valid && s_d.mk_valid; }
static float cd_weekly()    { return hcd() ? s_d.codex_weekly_pct : CODEX_WEEKLY; }

// model display name → its identity hue (one violet family: Opus/Sonnet/Haiku/Fable
// spaced across the spectrum · Grok red); default to the Claude coral.
static lv_color_t model_color(const char* m) {
    if (strstr(m, "OPUS"))   return PC_OPUS;
    if (strstr(m, "SONNET")) return PC_SONNET;
    if (strstr(m, "HAIKU"))  return PC_HAIKU;
    if (strstr(m, "FABLE"))  return PC_FABLE;
    if (strstr(m, "GROK"))   return PC_GROK;
    return PC_CLAUDE;
}

// mins → compact "45M" / "3H" / "4D" (em-dash when unknown).
static void fmt_dur(char* b, size_t n, int mins) {
    if (mins < 0)         snprintf(b, n, "\xE2\x80\x94");
    else if (mins < 60)   snprintf(b, n, "%dM", mins);
    else if (mins < 1440) snprintf(b, n, "%dH", (mins + 30) / 60);
    else                  snprintf(b, n, "%dD", (mins + 720) / 1440);
}

// Normalise a 7-sample $ series to 0..1 by its max into out[7]; returns out, or
// nullptr when the series is empty/all-zero (caller uses the placeholder then).
static const float* norm7(const float* src, float* out) {
    float mx = 0.0f;
    for (int i = 0; i < 7; i++) if (src[i] > mx) mx = src[i];
    if (mx <= 0.0f) return nullptr;
    for (int i = 0; i < 7; i++) out[i] = src[i] / mx;
    return out;
}

// Grok keeps a roomier single-gauge rhythm (Claude is denser — own layout).
enum {
    AI_GAUGE_Y  = 46,
    AI_GAUGE_AS = 230,
    AI_SPARK_H  = 22,
    // Dual-ring views (Claude · Kimi) share one geometry so they read as the
    // same layout. AI_DUAL_Y is set by the header mark, not by taste: the ring
    // canvases are opaque and drawn after it, so the band has to start below
    // the mark's ink (the mascot's bottom row lands at y101 at cell 6).
    AI_DUAL_Y   = 104,
    AI_DUAL_AS  = 192,
    AI_DUAL_GAP = 16,
};

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
    float       vals[32];      // 32, not 16: the market view's intraday spark
    lv_color_t  colors[32];    // carries a full session at 5-minute resolution
    int         n;
    float       max_v;
    float       total;             // meter_segs total
    dither_tile_t tiles[8];
    int         ntiles;
    int         thick;             // arc
    bool        use_bands;         // arc heat-band track
    float       live_alpha;        // spark live cell
    float       min_density;       // PK_LANE floor for a non-zero bucket
    float       reveal_scale;      // 0..1 entrance (applied at paint time)
};

static PaintJob  s_jobs[8];
static int       s_njobs = 0;
static float     s_reveal = 1.0f;
static float     s_live   = 1.0f;
static lv_timer_t* s_pulse = nullptr;
// Header mascot (Claude view) — ticked by the pulse timer so it animates.
static splash_mini_t s_mascot   = {};
static bool          s_mascot_on = false;
// Header knot (Codex view) — hi-res 4-bit bitmap animation (logo_codex_anim.h,
// generated by tools/gif_to_bitmap_c.py). The splash-mini engine's fixed 20x20
// cell grid made this logo look too blocky. Same lifecycle as the mascot:
// buffer reclaimed by free_bufs each render, ticked by the pulse timer.
// Frames carry a hold count (the gif's tail is a still, not a spin), so the
// tick advances only when the current frame's hold runs out.
static lv_obj_t*       s_codex_canvas = nullptr;
static uint32_t*       s_codex_buf    = nullptr;
static int             s_codex_frame  = 0;
static int             s_codex_hold   = 0;
static bool            s_codex_on     = false;

// Unpack the current 4-bit frame into the ARGB8888 canvas buffer: the grey
// level becomes ALPHA on white, so the mark composites over whatever it sits
// on instead of stamping an opaque black square (which would notch the ring
// canvases, drawn after it).
static void codex_render_frame() {
    if (!s_codex_buf) return;
    const uint8_t* packed = codex_anim_frames[s_codex_frame];
    for (int i = 0; i < CODEX_ANIM_FRAME_BYTES; i++) {
        uint32_t ah = (uint32_t)((packed[i] >> 4) * 17);   // 0..15 -> 0..255
        uint32_t al = (uint32_t)((packed[i] & 0xF) * 17);
        s_codex_buf[2 * i]     = (ah << 24) | 0x00FFFFFFu;
        s_codex_buf[2 * i + 1] = (al << 24) | 0x00FFFFFFu;
    }
    if (s_codex_canvas) lv_obj_invalidate(s_codex_canvas);
}

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
        float vs[32];
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
        float vs[32];
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
        float vs[32];
        for (int i = 0; i < j->n; i++) vs[i] = j->vals[i] * r;
        dither_lane(j->buf, j->w, j->h, vs, j->n, j->max_v, j->color, j->cell,
                    j->min_density);
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
    // Advance the header mascot (Claude view) at its own animation pace.
    if (s_mascot_on) splash_mini_tick_one(&s_mascot);
    // …and the Codex knot's, honouring each frame's hold count.
    if (s_codex_on && --s_codex_hold <= 0) {
        s_codex_frame = (s_codex_frame + 1) % CODEX_ANIM_FRAMES;
        s_codex_hold  = codex_anim_holds[s_codex_frame];
        codex_render_frame();
    }
}

// intro=true plays the reveal sweep (view entrance); intro=false paints fully
// revealed at once (a data refresh must not replay the entrance every ~60s).
// Either way the pulse timer is (re)armed so the spark/mascot keep animating.
static void start_motion(bool intro) {
    // Kill prior motion tied to our anim var.
    lv_anim_delete(&s_anim_var, reveal_exec);
    if (s_pulse) { lv_timer_delete(s_pulse); s_pulse = nullptr; }

    s_live = 1.0f;
    if (intro) {
        s_reveal = 0.0f;
        repaint_all();

        lv_anim_t a;
        lv_anim_init(&a);
        lv_anim_set_var(&a, &s_anim_var);
        lv_anim_set_values(&a, 0, 1000);
        lv_anim_set_exec_cb(&a, reveal_exec);
        lv_anim_set_path_cb(&a, lv_anim_path_ease_out);  // ~easeOutCubic family
        lv_anim_set_duration(&a, 420);
        lv_anim_start(&a);
    } else {
        s_reveal = 1.0f;
        repaint_all();
    }

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

// Hero numeral + unit (Departure Mono big, unit dim), screen-centered.
static void hero_pct(lv_obj_t* scr, int pct, int y, const lv_font_t* num_font) {
    char num[8];
    snprintf(num, sizeof num, "%d", pct);
    lv_obj_t* row = crow(scr, y, 8);
    rtext(row, num, PC_TEXT, num_font);
    lv_obj_t* u = rtext(row, "%", PC_DIM, &font_styrene_28);
    lv_obj_set_style_text_letter_space(u, 0, 0);
}

// % readout with the NUMERAL centred on cx (the "%" hangs to the right, so the
// number itself reads as centred in the ring — centring the whole row instead
// pushes the number off to the left).
static void pct_col(lv_obj_t* scr, int pct, int cx, int y, const lv_font_t* num_font) {
    char num[8];
    snprintf(num, sizeof num, "%d", pct);
    lv_obj_t* row = lv_obj_create(scr);
    lv_obj_remove_style_all(row);
    lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(row, 6, 0);
    lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t* nl = rtext(row, num, PC_TEXT, num_font);
    lv_obj_t* u = rtext(row, "%", PC_DIM, &font_styrene_24);
    lv_obj_set_style_text_letter_space(u, 0, 0);
    lv_obj_update_layout(row);
    lv_obj_set_pos(row, cx - lv_obj_get_width(nl) / 2, y);
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

// ── Chrome: view name + page dots only (no fake LIVE) ───────────────────────

static void chrome(lv_obj_t* scr, const char* name) {
    const int W = board_caps().width;
    lv_obj_set_style_bg_color(scr, PC_BG, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    // name == nullptr → the view draws its own header mark (mascot / logo).
    if (name) {
        lv_obj_t* hdr = lv_label_create(scr);
        lv_label_set_text(hdr, name);
        lv_obj_set_style_text_font(hdr, &font_styrene_24, 0);
        lv_obj_set_style_text_color(hdr, PC_DIM, 0);
        lv_obj_set_style_text_letter_space(hdr, 4, 0);
        lv_obj_set_pos(hdr, 28, 18);
    }

    const int n = PV_COUNT, dot = 10, gap = 14;
    const int total = n * dot + (n - 1) * gap;
    int x0 = (W - total) / 2;
    for (int i = 0; i < n; i++) {
        lv_obj_t* d = lv_obj_create(scr);
        lv_obj_set_size(d, dot, dot);
        lv_obj_set_pos(d, x0 + i * (dot + gap), 448);
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
    j->n = n < 32 ? n : 32;
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
    const int mh = 30;

    struct Row { const char* name; float pct; };
    Row rows[] = {
        { "CPU", cpu_pct() },
        { "GPU", gpu_pct() },
        { "RAM", ram_pct() },
    };

    // 3 rows, hero-sized. pitch 130: y=50/180/310, last meter ends 418.
    const int y0 = 50, pitch = 130;
    for (int i = 0; i < 3; i++) {
        int y = y0 + i * pitch;
        float pct = rows[i].pct;
        lv_color_t col = band(pct);

        // Label optically centred on the big numeral to its right.
        eyebrow_at(scr, rows[i].name, PC_DIM, &font_styrene_28, mx, y + 14);

        char num[8];
        snprintf(num, sizeof num, "%d", (int)(pct + 0.5f));
        lv_obj_t* row = lv_obj_create(scr);
        lv_obj_remove_style_all(row);
        lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_column(row, 8, 0);
        lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_align(row, LV_ALIGN_TOP_RIGHT, -mx, y - 8);
        rtext(row, num, PC_TEXT, &font_departure_72);
        rtext(row, "%", PC_DIM, &font_styrene_28);

        job_meter(scr, mx, y + 78, mw, mh, pct / 100.0f, col);
    }
}

// CPU — big load % + per-core DitherColumns (the PitCrew heatmap, not a lone arc).
static void view_cpu(lv_obj_t* scr) {
    chrome(scr, "CPU");
    eyebrow_c(scr, "LOAD", PC_DIM, &font_styrene_24, 54);
    hero_pct(scr, (int)(cpu_pct() + 0.5f), 84, &font_departure_72);
    // Part name is reference info, read once — it stays the smallest thing here.
    eyebrow_c(scr, (hcpu() && s_d.vitals.cpu_name[0]) ? s_d.vitals.cpu_name
                                                      : "AMD Ryzen 9 5900X",
              PC_DIM, &font_styrene_20, 168);

    // Per-core columns — each core band-colored by its own load. Live cores when
    // present (up to 24 threads on this box), else the 12-thread mock.
    const int W = board_caps().width;
    const int cw = W - 56, ch = 150;
    const float* cores = (hcpu() && s_d.vitals.ncores > 0) ? nullptr : CORES;
    const int    ncores = (hcpu() && s_d.vitals.ncores > 0) ? s_d.vitals.ncores : NCORES;
    uint16_t* b = mkbuf(cw, ch);
    PaintJob* j = add_job();
    if (j && b) {
        j->kind = PK_COLUMNS;
        j->buf = b; j->w = cw; j->h = ch; j->cell = 2;
        j->n = ncores > 16 ? 16 : ncores;   // PaintJob holds up to 16 columns
        j->max_v = 1.0f;
        for (int i = 0; i < j->n; i++) {
            float load = cores ? cores[i] : (s_d.vitals.cores[i] / 100.0f);
            j->vals[i] = load;
            j->colors[i] = band(load * 100.0f);
        }
        j->canvas = canvas_at(scr, b, cw, ch, 28, 210);
        paint_job(j, 0);
    }

    eyebrow_c(scr, "PER CORE", PC_DIM, &font_styrene_24, 372);
    // One footer line: clock, plus package temp when the box exposes one. The old
    // permanent "TEMP — no sensor" row was clutter on a box that never has one.
    char foot[24];
    float ghz = (hcpu() && s_d.vitals.cpu_clk_mhz > 0) ? s_d.vitals.cpu_clk_mhz / 1000.0f
                                                       : 4.35f;
    if (hcpu() && s_d.vitals.cpu_temp_valid)
        snprintf(foot, sizeof foot, "%d\xC2\xB0""C  \xC2\xB7  %.2f GHz",
                 s_d.vitals.cpu_temp_c, ghz);
    else
        snprintf(foot, sizeof foot, "%.2f GHz", ghz);
    dep_c(scr, foot, PC_TEXT, &font_departure_32, 404);
}

// GPU — DitherRadial (load cycle) + VRAM capacity meter.
static void view_gpu(lv_obj_t* scr) {
    chrome(scr, "GPU");
    const int W = board_caps().width;
    const int AS = 230, ay = 44;
    int ax = (W - AS) / 2;

    const float gp = gpu_pct();
    uint16_t* b = mkbuf(AS, AS);
    PaintJob* j = add_job();
    if (j && b) {
        j->kind = PK_ARC;
        j->buf = b; j->w = AS; j->h = AS; j->cell = 2;
        j->f = gp / 100.0f;
        j->color = band(gp);
        j->thick = 24;
        j->use_bands = true;  // track pre-shows heat thresholds
        j->canvas = canvas_at(scr, b, AS, AS, ax, ay);
        paint_job(j, 0);
    }

    eyebrow_c(scr, "LOAD", PC_DIM, &font_styrene_24, ay + 76);
    hero_pct(scr, (int)(gp + 0.5f), ay + 106, &font_departure_72);

    eyebrow_c(scr, (hgpu() && s_d.vitals.gpu_name[0]) ? s_d.vitals.gpu_name
                                                      : "NVIDIA RTX 3080 Ti",
              PC_DIM, &font_styrene_20, 288);
    // "<temp>°C · <power> W" — em-dash for temp when the GPU reports none.
    char line[32];
    if (hgpu()) {
        // Single-spaced separators — at dep_48 the padded form ran edge to edge.
        if (s_d.vitals.gpu_temp_valid)
            snprintf(line, sizeof line, "%d\xC2\xB0""C \xC2\xB7 %d W",
                     s_d.vitals.gpu_temp_c, (int)(s_d.vitals.gpu_power_w + 0.5f));
        else
            snprintf(line, sizeof line, "\xE2\x80\x94 \xC2\xB7 %d W",
                     (int)(s_d.vitals.gpu_power_w + 0.5f));
    } else {
        snprintf(line, sizeof line, "71\xC2\xB0""C \xC2\xB7 214 W");
    }
    dep_c(scr, line, PC_TEXT, &font_departure_48, 314);

    // VRAM used / total — real MB → GB.
    float vu_gb = hgpu() ? s_d.vitals.gpu_vram_used_mb  / 1024.0f : 9.1f;
    float vt_gb = hgpu() ? s_d.vitals.gpu_vram_total_mb / 1024.0f : 12.0f;
    if (vt_gb <= 0) vt_gb = 12.0f;
    char vram_lbl[28];
    snprintf(vram_lbl, sizeof vram_lbl, "VRAM  %.1f / %.0f GB", vu_gb, vt_gb);
    eyebrow_c(scr, vram_lbl, PC_DIM, &font_styrene_24, 374);
    float vram = vu_gb / vt_gb;
    job_meter(scr, 40, 406, W - 80, 28, vram, band(vram * 100.0f));
}

// RAM — DitherTreemap: used tiles left (varied hues), free ghost strip on RIGHT.
static void view_ram(lv_obj_t* scr) {
    chrome(scr, "RAM");
    eyebrow_c(scr, "IN USE", PC_DIM, &font_styrene_24, 44);
    hero_pct(scr, (int)(ram_pct() + 0.5f), 74, &font_departure_72);

    const float GB = 1073741824.0f;
    float used_gb  = hram() ? s_d.vitals.ram_used_bytes  / GB : 18.2f;
    float total_gb = hram() ? s_d.vitals.ram_total_bytes / GB : 32.0f;
    if (total_gb <= 0) total_gb = 32.0f;
    float free_gb = total_gb - used_gb;
    if (free_gb < 0) free_gb = 0;

    char ut[24];
    snprintf(ut, sizeof ut, "%.1f / %.0f GB", used_gb, total_gb);
    dep_c(scr, ut, PC_TEXT, &font_departure_48, 156);

    const int W = board_caps().width;
    const int tw = W - 48, th = 160;
    uint16_t* b = mkbuf(tw, th);
    PaintJob* j = add_job();
    if (j && b) {
        j->kind = PK_TREEMAP;
        j->buf = b; j->w = tw; j->h = th; j->cell = 2;
        if (hram() && s_d.vitals.ram_nseg > 0) {
            // Real segments = the top memory-consuming processes, then whatever
            // else is used, then the FREE ghost strip. Honest and subdivided.
            lv_color_t pal[4] = { PC_BLUE, PC_YELLOW, PC_GREEN, PC_ORANGE };
            int n = s_d.vitals.ram_nseg;
            float seg_sum = 0.0f;
            for (int i = 0; i < n; i++) {
                float gb = s_d.vitals.ram_segs[i].bytes / GB;
                j->tiles[i] = { gb, pal[i % 4], false };
                seg_sum += gb;
            }
            j->ntiles = n;
            float other = used_gb - seg_sum;
            if (other > 0.05f) j->tiles[j->ntiles++] = { other, PC_HAIKU, false };
            j->tiles[j->ntiles++] = { free_gb, PC_GREY, true };  // Free → right strip
        } else if (hram()) {
            // No per-process data — honest used tile + FREE ghost.
            j->tiles[0] = { used_gb, PC_BLUE, false };
            j->tiles[1] = { free_gb, PC_GREY, true  };
            j->ntiles = 2;
        } else {
            // No daemon — the approved multi-hue mock (placeholder categories).
            j->tiles[0] = { 7.5f,  PC_BLUE,   false };
            j->tiles[1] = { 4.0f,  PC_ORANGE, false };
            j->tiles[2] = { 4.2f,  PC_YELLOW, false };
            j->tiles[3] = { 2.5f,  PC_GREEN,  false };
            j->tiles[4] = { 13.8f, PC_GREY,   true  };
            j->ntiles = 5;
        }
        j->canvas = canvas_at(scr, b, tw, th, 24, 224);
        paint_job(j, 0);
    }

    // The free-GB figure is already implied by "used / total" + the ghost tile,
    // so the footer is just the legend — at a size that can actually be read.
    // Styrene is ASCII-only — a "\xC2\xB7" here renders as a tofu box. Group the
    // words with space instead (Departure carries the real middle dot).
    const char* ram_legend = (hram() && s_d.vitals.ram_nseg > 0) ? "TOP PROCESSES     FREE"
                             : hram()                             ? "IN USE     FREE"
                             : "APPS   CACHED   FREE";
    eyebrow_c(scr, ram_legend, PC_DIM, &font_styrene_24, 398);
}

// Column-centered eyebrow (for dual-arc labels).
static void brow_col(lv_obj_t* scr, const char* t, int cx, int y) {
    lv_obj_t* e = lv_label_create(scr);
    lv_label_set_text(e, t);
    lv_obj_set_style_text_font(e, &font_styrene_24, 0);
    lv_obj_set_style_text_color(e, PC_DIM, 0);
    lv_obj_set_style_text_letter_space(e, 3, 0);
    lv_obj_update_layout(e);
    lv_obj_set_pos(e, cx - lv_obj_get_width(e) / 2, y);
}

// ── Header marks (replace the text header on the AI views) ───────────────────
//
// All four AI views carry a brand mark in the top-left, and they must read as
// the SAME SIZE. What matters is the INK, not the box: the Grok lobe fills its
// bitmap edge to edge (92x88 of ink at 2x), while the mascot's creature covers
// only ~17x14 of its 20x20 grid and the Codex knot only ~64% of the old crop —
// both looked shrunken beside it. So each mark is sized to land near 92x88 of
// ink, which for the mascot and the knot means a bigger box than that.
//
// The ring canvases are opaque and are created AFTER the mark, so wherever the
// two rectangles overlap the ring wins. The rule is therefore: a mark's INK
// must stay out of the ring canvas rect — blank margins may overlap freely.
// That is what sets AI_DUAL_Y below.
static void header_mascot(lv_obj_t* scr) {
    // splash_mini quantises to (px/20)*20 — 120 is cell 6, which puts the
    // creature's 17x14-cell body at 102x84 of ink, matching the Grok lobe.
    // Ink sits at (12,18)-(113,101) inside the box, clear of the ring band.
    if (splash_mini_init(&s_mascot, scr, "idle look around", 120)) {
        lv_obj_set_pos(s_mascot.canvas, 0, 0);
        if (s_nbuf < 16) s_bufs[s_nbuf++] = s_mascot.buf;  // free_bufs reclaims it each render
        s_mascot_on = true;  // the pulse timer now animates it
    }
}

static void header_grok_logo(lv_obj_t* scr) {
    static lv_image_dsc_t dsc;
    proto_icon_dsc(&dsc, GROK_LOGO_LOBE_WIDTH, GROK_LOGO_LOBE_HEIGHT, grok_logo_lobe_data);
    lv_obj_t* img = lv_image_create(scr);
    lv_image_set_src(img, &dsc);
    // The baked asset is 46×44. Upscale 2× (256=100%) to a ~92 px header mark,
    // scaling from the top-left so set_pos still anchors it. The Grok ring is
    // centred (left edge x≈125), so a 92 px logo at x20 clears it with room.
    lv_image_set_pivot(img, 0, 0);
    lv_image_set_scale(img, 512);
    lv_obj_set_pos(img, 20, 10);
}

static void header_kimi_logo(lv_obj_t* scr) {
    static lv_image_dsc_t dsc;
    proto_icon_dsc(&dsc, KIMI_LOGO_WIDTH, KIMI_LOGO_HEIGHT, kimi_logo_data);
    lv_obj_t* img = lv_image_create(scr);
    lv_image_set_src(img, &dsc);
    // Baked 46×46, but the "K" only inks 41x39 of it — at the old 445 (~80 px
    // box) that read as 71x68, visibly smaller than the Grok lobe. 573 gives a
    // ~103 px box whose ink is 92x87, matching it. Ink lands at (11,4)-(101,91),
    // above the dual-ring band.
    lv_image_set_pivot(img, 0, 0);
    lv_image_set_scale(img, 573);   // 46 → ~103 px
    lv_obj_set_pos(img, 0, 4);
}

static void header_codex_logo(lv_obj_t* scr) {
    // Animated like the Claude mascot, but from a hi-res bitmap: the gif is
    // baked into logo_codex_anim.h (tools/gif_to_bitmap_c.py) and advanced by
    // the pulse timer. The bake now crops to the SETTLED knot, so the knot fills
    // ~92% of the 100 px canvas (it used to fill 64% of an 80 px one — a 51 px
    // mark next to Grok's 92 px lobe). ARGB8888, so it composites rather than
    // stamping a black square. At (16,8) on a single-ring view (ring left edge
    // x≈125) it sits in the same corner as the Grok lobe.
#ifdef BOARD_HAS_PSRAM
    const uint32_t caps = MALLOC_CAP_SPIRAM;
#else
    const uint32_t caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
#endif
    s_codex_buf = (uint32_t*)heap_caps_malloc(CODEX_ANIM_PX * CODEX_ANIM_PX * 4, caps);
    if (!s_codex_buf) return;
    s_codex_canvas = lv_canvas_create(scr);
    lv_canvas_set_buffer(s_codex_canvas, s_codex_buf, CODEX_ANIM_PX, CODEX_ANIM_PX,
                         LV_COLOR_FORMAT_ARGB8888);
    lv_obj_set_pos(s_codex_canvas, 16, 8);
    if (s_nbuf < 16) s_bufs[s_nbuf++] = s_codex_buf;  // free_bufs reclaims it each render
    s_codex_frame = 0;
    s_codex_hold  = codex_anim_holds[0];
    codex_render_frame();
    s_codex_on = true;  // the pulse timer now animates it
}

// A model identity chip: colour square + name, packed tight (its own sub-row).
static lv_obj_t* model_chip(lv_obj_t* parent, const char* name, lv_color_t col,
                            int sq, const lv_font_t* font) {
    lv_obj_t* c = lv_obj_create(parent);
    lv_obj_remove_style_all(c);
    lv_obj_set_size(c, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(c, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(c, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(c, 7, 0);
    lv_obj_clear_flag(c, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t* led = lv_obj_create(c);
    lv_obj_set_size(led, sq, sq);
    lv_obj_set_style_bg_color(led, col, 0);
    lv_obj_set_style_bg_opa(led, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(led, 0, 0);
    lv_obj_set_style_border_width(led, 0, 0);
    lv_obj_clear_flag(led, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t* l = rtext(c, name, PC_DIM, font);
    lv_obj_set_style_text_letter_space(l, 2, 0);
    return c;
}

// Idle marker for the IN USE row — a DRAWN dash, not the U+2014 glyph (the
// bundled fonts lack it; it renders as a box — HANDOFF §3).
static void dash_chip(lv_obj_t* row) {
    lv_obj_t* d = lv_obj_create(row);
    lv_obj_remove_style_all(d);
    lv_obj_set_size(d, 18, 4);
    lv_obj_set_style_bg_color(d, PC_GREY, 0);
    lv_obj_set_style_bg_opa(d, LV_OPA_COVER, 0);
    lv_obj_clear_flag(d, LV_OBJ_FLAG_SCROLLABLE);
}

// "IN USE" + one chip per model running now (parallel sessions → several chips).
static void inuse_chips(lv_obj_t* scr, int y) {
    lv_obj_t* row = crow(scr, y, 14);
    lv_obj_t* lbl = rtext(row, "IN USE", PC_DIM, &font_styrene_24);
    lv_obj_set_style_text_letter_space(lbl, 3, 0);
    if (hcl() && s_d.claude_nmodels > 0) {
        for (int i = 0; i < s_d.claude_nmodels; i++)
            model_chip(row, s_d.claude_models[i], model_color(s_d.claude_models[i]), 14, &font_styrene_24);
    } else if (!hcl()) {
        model_chip(row, "OPUS 4.8", PC_OPUS, 14, &font_styrene_24);
        model_chip(row, "FABLE 5",  PC_FABLE, 14, &font_styrene_24);
    } else {
        dash_chip(row);  // idle: nothing recent
    }
}

// Rasterise a small clock face (ring + two hands) into an RGB565 canvas buffer.
// Replaces the word "RESETS" so the countdown value can be bigger. Drawn rather
// than a font glyph — no bundled font carries a clock, and this stays crisp at
// any size. Background is true black (invisible over the AMOLED-black screen).
static inline void px565(uint16_t* b, int D, int x, int y, uint16_t c) {
    if (x >= 0 && x < D && y >= 0 && y < D) b[y * D + x] = c;
}
static void draw_clock(uint16_t* b, int D, lv_color_t col) {
    const uint16_t c = lv_color_to_u16(col);
    for (int i = 0; i < D * D; i++) b[i] = 0x0000;
    const float cx = (D - 1) / 2.0f, cy = (D - 1) / 2.0f;
    const float r = D / 2.0f - 1.5f;
    // 2px-thick rim.
    for (float t = 0; t < 6.2832f; t += 0.03f) {
        float ct = cosf(t), st = sinf(t);
        px565(b, D, (int)lroundf(cx + r * ct),        (int)lroundf(cy + r * st),        c);
        px565(b, D, (int)lroundf(cx + (r - 1) * ct),  (int)lroundf(cy + (r - 1) * st),  c);
    }
    // Hands: minute → 12 o'clock (up), hour → 4 o'clock (down-right). 2px each.
    for (float s = 0; s <= 1.0f; s += 0.03f) {
        int mx = (int)lroundf(cx), my = (int)lroundf(cy - s * r * 0.78f);
        px565(b, D, mx, my, c); px565(b, D, mx + 1, my, c);
        int hx = (int)lroundf(cx + s * r * 0.50f * 0.87f);
        int hy = (int)lroundf(cy + s * r * 0.50f * 0.50f);
        px565(b, D, hx, hy, c); px565(b, D, hx, hy + 1, c);
    }
}

// A clock icon + the countdown value, centred under a ring column. The clock
// stands in for the old "RESETS" word, buying room for a larger numeral.
static void reset_col(lv_obj_t* scr, int mins, int cx, int y) {
    char d[8];
    fmt_dur(d, sizeof d, mins);
    lv_obj_t* row = lv_obj_create(scr);
    lv_obj_remove_style_all(row);
    lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(row, 8, 0);
    lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
    const int D = 28;
    uint16_t* b = mkbuf(D, D);
    if (b) {
        draw_clock(b, D, PC_DIM);
        lv_obj_t* cv = lv_canvas_create(row);
        lv_canvas_set_buffer(cv, b, D, D, LV_COLOR_FORMAT_RGB565);
    }
    rtext(row, d, PC_DIM, &font_departure_32);
    lv_obj_update_layout(row);
    lv_obj_set_pos(row, cx - lv_obj_get_width(row) / 2, y);
}

// ── Drawn marks for the weather / market views ──────────────────────────────
// Same reasoning as draw_clock(): no bundled font carries a sun, a cloud or a
// bolt, and baking a bitmap per condition would cost flash for something that
// is a handful of circles and lines. Everything below rasterises into an RGB565
// canvas over the black page, so the "background" is simply left black.
static void disc(uint16_t* b, int D, float cx, float cy, float r, lv_color_t col) {
    const uint16_t c = lv_color_to_u16(col);
    const int y0 = (int)(cy - r) - 1, y1 = (int)(cy + r) + 1;
    for (int y = y0; y <= y1; y++) {
        for (int x = (int)(cx - r) - 1; x <= (int)(cx + r) + 1; x++) {
            const float dx = x - cx, dy = y - cy;
            if (dx * dx + dy * dy <= r * r) px565(b, D, x, y, c);
        }
    }
}

static void stroke(uint16_t* b, int D, float x0, float y0, float x1, float y1,
                   float w, lv_color_t col) {
    const float len = sqrtf((x1 - x0) * (x1 - x0) + (y1 - y0) * (y1 - y0));
    const int steps = (int)(len * 2) + 1;
    for (int i = 0; i <= steps; i++) {
        const float t = (float)i / steps;
        disc(b, D, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, w / 2.0f, col);
    }
}

// A cloud used by several conditions: three lobes over a flat base.
static void draw_cloud(uint16_t* b, int D, float cx, float cy, float s, lv_color_t col) {
    disc(b, D, cx - 0.26f * s, cy + 0.04f * s, 0.20f * s, col);
    disc(b, D, cx + 0.02f * s, cy - 0.10f * s, 0.27f * s, col);
    disc(b, D, cx + 0.28f * s, cy + 0.06f * s, 0.18f * s, col);
    const uint16_t c = lv_color_to_u16(col);
    for (int y = (int)(cy + 0.02f * s); y <= (int)(cy + 0.24f * s); y++)
        for (int x = (int)(cx - 0.44f * s); x <= (int)(cx + 0.44f * s); x++)
            px565(b, D, x, y, c);
}

// WMO weather code -> the glyph we draw. Grouped the way a person reads a sky,
// not the way the code table is numbered.
enum WxGlyph { WXG_CLEAR = 0, WXG_CLOUD, WXG_RAIN, WXG_SNOW, WXG_STORM };

static WxGlyph wx_glyph(int code) {
    if (code >= 95) return WXG_STORM;
    if ((code >= 71 && code <= 77) || code == 85 || code == 86) return WXG_SNOW;
    if ((code >= 51 && code <= 67) || (code >= 80 && code <= 82)) return WXG_RAIN;
    if (code >= 2) return WXG_CLOUD;      // 2-3 cloudy, 45/48 fog
    return WXG_CLEAR;                     // 0-1 clear / mainly clear
}

static void draw_weather_glyph(uint16_t* b, int D, int code, bool is_day) {
    for (int i = 0; i < D * D; i++) b[i] = 0x0000;
    const float S = D;
    const float cx = D / 2.0f, cy = D / 2.0f;
    const WxGlyph g = wx_glyph(code);
    if (g == WXG_CLEAR) {
        if (is_day) {
            disc(b, D, cx, cy, 0.22f * S, PC_SUN);
            for (int i = 0; i < 8; i++) {          // eight rays
                const float a = i * 0.7853982f;
                stroke(b, D, cx + cosf(a) * 0.30f * S, cy + sinf(a) * 0.30f * S,
                       cx + cosf(a) * 0.44f * S, cy + sinf(a) * 0.44f * S, 0.05f * S, PC_SUN);
            }
        } else {
            // Crescent: a disc with an offset disc punched back out in black.
            disc(b, D, cx, cy, 0.34f * S, PC_NIGHT);
            disc(b, D, cx + 0.16f * S, cy - 0.14f * S, 0.30f * S, lv_color_hex(0x000000));
        }
        return;
    }
    if (g == WXG_CLOUD) {
        if (is_day) disc(b, D, cx + 0.26f * S, cy - 0.26f * S, 0.15f * S, PC_SUN);
        draw_cloud(b, D, cx, cy - 0.04f * S, S, PC_DIM);
        return;
    }
    draw_cloud(b, D, cx, cy - 0.16f * S, S, PC_DIM);
    if (g == WXG_RAIN) {
        for (int i = -1; i <= 1; i++)
            stroke(b, D, cx + i * 0.22f * S + 0.04f * S, cy + 0.20f * S,
                   cx + i * 0.22f * S - 0.04f * S, cy + 0.42f * S, 0.05f * S, PC_RAIN);
    } else if (g == WXG_SNOW) {
        for (int i = -1; i <= 1; i++) {           // three six-point flakes
            const float fx = cx + i * 0.22f * S, fy = cy + 0.30f * S;
            for (int k = 0; k < 3; k++) {
                const float a = k * 1.0471976f;
                stroke(b, D, fx - cosf(a) * 0.07f * S, fy - sinf(a) * 0.07f * S,
                       fx + cosf(a) * 0.07f * S, fy + sinf(a) * 0.07f * S, 0.035f * S, PC_TEXT);
            }
        }
    } else {                                       // WXG_STORM — one bolt
        stroke(b, D, cx + 0.06f * S, cy + 0.16f * S, cx - 0.08f * S, cy + 0.32f * S,
               0.06f * S, PC_YELLOW);
        stroke(b, D, cx - 0.08f * S, cy + 0.32f * S, cx + 0.06f * S, cy + 0.32f * S,
               0.06f * S, PC_YELLOW);
        stroke(b, D, cx + 0.06f * S, cy + 0.32f * S, cx - 0.06f * S, cy + 0.48f * S,
               0.06f * S, PC_YELLOW);
    }
}

// Degree ring — a small hollow superscript circle. The bundled fonts have no
// U+00B0 (it renders as a box), so the unit is drawn, like the clock face.
static void degree_mark(lv_obj_t* row, lv_color_t col, int d) {
    uint16_t* b = mkbuf(d, d);
    if (!b) return;
    for (int i = 0; i < d * d; i++) b[i] = 0x0000;
    disc(b, d, d / 2.0f, d / 2.0f, d / 2.0f - 0.5f, col);
    disc(b, d, d / 2.0f, d / 2.0f, d / 2.0f - 2.5f, lv_color_hex(0x000000));
    lv_obj_t* cv = lv_canvas_create(row);
    lv_canvas_set_buffer(cv, b, d, d, LV_COLOR_FORMAT_RGB565);
}

// Temperature -> the cold..hot ramp. Separate from band(): that one means LOAD,
// and a 30°F morning is not "healthy green".
static lv_color_t temp_color(float f) {
    if (f >= 95.0f) return PC_HOT;
    if (f >= 82.0f) return PC_WARM;
    if (f >= 66.0f) return PC_MILD;
    if (f >= 48.0f) return PC_COOL;
    return PC_COLD;
}

static const char* wx_word(int code, bool is_day) {
    switch (wx_glyph(code)) {
        case WXG_STORM: return "STORMS";
        case WXG_SNOW:  return "SNOW";
        case WXG_RAIN:  return code >= 80 ? "SHOWERS" : "RAIN";
        case WXG_CLOUD: return (code == 45 || code == 48) ? "FOG" : "CLOUDY";
        default:        return is_day ? "CLEAR" : "CLEAR NIGHT";
    }
}

// Honest no-data state, production builds only (UI_SHOT keeps the placeholder
// constants so QA screenshots still render populated views): a provider with
// no live reading gets this instead of invented numbers — the placeholder
// constants once read as live Kimi quota for a whole afternoon (61%/12%
// while kimi.com said 11%/87%). The dash is DRAWN (a bar): the bundled fonts
// have no U+2014 glyph — it renders as a box (see HANDOFF §3).
static void no_data(lv_obj_t* scr) {
    lv_obj_t* bar = lv_obj_create(scr);
    lv_obj_remove_style_all(bar);
    lv_obj_set_size(bar, 56, 6);
    lv_obj_set_style_bg_color(bar, PC_GREY, 0);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, 0);
    lv_obj_align(bar, LV_ALIGN_TOP_MID, 0, 210);
    lv_obj_t* t = rtext(crow(scr, 260, 0), "NO LIVE DATA", PC_DIM, &font_styrene_24);
    lv_obj_set_style_text_letter_space(t, 3, 0);
}

// CLAUDE — the two all-model limits as heat-tiered rings (Session 5h · Weekly 7d),
// then the same footer as the Grok view (models-in-use · today's $ · 7-day spark).
// Intentionally a mirror of view_grok with one extra ring — the model-scoped
// Weekly-Fable limit was dropped for that symmetry (its data is still in the
// payload, so it can return as a third ring if wanted).
static void view_claude(lv_obj_t* scr) {
    chrome(scr, nullptr);
    header_mascot(scr);
#ifndef UI_SHOT
    if (!hcl()) { no_data(scr); return; }
#endif
    const int W = board_caps().width;

    // ── Dual arcs: SESSION (5h) left, WEEKLY (7d) right ──────────────────────
    const int AS = AI_DUAL_AS, ay = AI_DUAL_Y, gap = AI_DUAL_GAP;
    const int pair = AS * 2 + gap;
    const int x0 = (W - pair) / 2;
    const int xL = x0, xR = x0 + AS + gap;
    const int cxL = xL + AS / 2, cxR = xR + AS / 2;

    // Rings are heat-tiered by % (green→yellow→orange→red), not brand coral.
    const float sp = cl_session(), wp = cl_weekly();
    job_arc(scr, xL, ay, AS, sp / 100.0f, band(sp), 18, false);
    job_arc(scr, xR, ay, AS, wp / 100.0f, band(wp), 18, false);
    // Inner stack: % on top, label under it, reset tucked into the open 6-o'clock
    // gap. The stack sits HIGH — the ring's inner opening narrows fast below the
    // midline, so "SESSION" (the widest label) must stay near centre or the arc
    // clips its ends. % centred just above the midline keeps the label at ~dy30.
    pct_col(scr, (int)(sp + 0.5f), cxL, ay + 58, &font_departure_48);
    pct_col(scr, (int)(wp + 0.5f), cxR, ay + 58, &font_departure_48);
    brow_col(scr, "SESSION", cxL, ay + 110);
    brow_col(scr, "WEEKLY",  cxR, ay + 110);
    reset_col(scr, hcl() ? s_d.session_reset_mins : 63,   cxL, ay + 140);
    reset_col(scr, hcl() ? s_d.weekly_reset_mins  : 7000, cxR, ay + 140);

    // ── Footer (identical rhythm to view_grok) ───────────────────────────────
    inuse_chips(scr, 300);
    {
        char spend[16] = "$4.35";
        if (hcl() && s_d.claude_extras_valid)
            snprintf(spend, sizeof spend, "$%.2f", s_d.claude_today_usd);
        lv_obj_t* row = crow(scr, 350, 12);
        rtext(row, spend, PC_TEXT, &font_departure_48);
        lv_obj_t* t = rtext(row, "TODAY", PC_DIM, &font_styrene_24);
        lv_obj_set_style_text_letter_space(t, 3, 0);
    }
    float sk[7];
    const float* claude_sk = (hcl() && s_d.claude_extras_valid) ? norm7(s_d.claude_week, sk)
                                                                : nullptr;
    job_spark(scr, 32, 412, W - 64, AI_SPARK_H, claude_sk ? claude_sk : WEEK_CLAUDE,
              7, lv_color_hex(0x9298a2));  // neutral grey (Luke: no coral on the spark)
}

// CODEX — OpenAI's single weekly limit as one heat-tiered radial (Codex exposes
// no session window: rate_limits.secondary is always null), read by the daemon
// from Codex's local rollout logs. Same rhythm as the Grok view, with the OpenAI
// knot, a live IN USE chip like Kimi's, and an OpenAI-teal spark. The $ figures
// are activity at API rates (flat-rate plan ⇒ real bill ~$0), see [[ai-subscriptions]].
static void view_codex(lv_obj_t* scr) {
    chrome(scr, nullptr);
    header_codex_logo(scr);
#ifndef UI_SHOT
    if (!hcd()) { no_data(scr); return; }
#endif
    const int W = board_caps().width;
    const int AS = AI_GAUGE_AS, ay = AI_GAUGE_Y;
    const int ax = (W - AS) / 2;
    const int cx = ax + AS / 2;

    // Ring heat-tiered by %. Inner stack mirrors Grok: % on top (hero dep_72),
    // WEEKLY under it, reset tucked into the open bottom.
    const float cw = cd_weekly();
    job_arc(scr, ax, ay, AS, cw / 100.0f, band(cw), 20, false);
    pct_col(scr, (int)(cw + 0.5f), cx, ay + 72, &font_departure_72);
    brow_col(scr, "WEEKLY", cx, ay + 150);
    reset_col(scr, hcd() ? s_d.codex_weekly_reset_mins : 9993, cx, ay + 184);

    // IN USE — live model chip like the Kimi view (em-dash when idle).
    {
        lv_obj_t* row = crow(scr, 300, 14);
        lv_obj_t* lbl = rtext(row, "IN USE", PC_DIM, &font_styrene_24);
        lv_obj_set_style_text_letter_space(lbl, 3, 0);
        const char* m = (hcd() && s_d.codex_model[0]) ? s_d.codex_model : "5.6 SOL";
        if (hcd() && !s_d.codex_model[0])
            dash_chip(row);  // idle
        else
            model_chip(row, m, PC_CODEX, 14, &font_styrene_24);
    }
    {
        char spend[16] = "$65.00";
        if (hcd()) snprintf(spend, sizeof spend, "$%.2f", s_d.codex_today_usd);
        lv_obj_t* row = crow(scr, 350, 12);
        rtext(row, spend, PC_TEXT, &font_departure_48);
        lv_obj_t* t = rtext(row, "TODAY", PC_DIM, &font_styrene_24);
        lv_obj_set_style_text_letter_space(t, 3, 0);
    }
    float sk[7];
    const float* codex_sk = (hcd() && s_d.codex_series_valid) ? norm7(s_d.codex_week_series, sk)
                                                              : nullptr;
    job_spark(scr, 32, 412, W - 64, AI_SPARK_H, codex_sk ? codex_sk : WEEK_GROK, 7, PC_CODEX);
}

// KIMI — Moonshot's two real limits as heat-tiered rings (Session 5h · Weekly 7d),
// read from /coding/v1/usages. Same rhythm as the Claude view (dual arcs + footer),
// with the Kimi mark and a Kimi-blue spark. The $ figures are activity at API rates
// (flat-rate membership ⇒ real bill ~$0), see [[ai-subscriptions]].
static void view_kimi(lv_obj_t* scr) {
    chrome(scr, nullptr);
    header_kimi_logo(scr);
#ifndef UI_SHOT
    if (!hki()) { no_data(scr); return; }
#endif
    const int W = board_caps().width;

    // Identical ring geometry to view_claude. SESSION (5h) left, WEEKLY (7d) right.
    const int AS = AI_DUAL_AS, ay = AI_DUAL_Y, gap = AI_DUAL_GAP;
    const int pair = AS * 2 + gap;
    const int x0 = (W - pair) / 2;
    const int xL = x0, xR = x0 + AS + gap;
    const int cxL = xL + AS / 2, cxR = xR + AS / 2;

    const float sp = ki_session(), wp = ki_weekly();
    job_arc(scr, xL, ay, AS, sp / 100.0f, band(sp), 18, false);
    job_arc(scr, xR, ay, AS, wp / 100.0f, band(wp), 18, false);
    pct_col(scr, (int)(sp + 0.5f), cxL, ay + 58, &font_departure_48);
    pct_col(scr, (int)(wp + 0.5f), cxR, ay + 58, &font_departure_48);
    brow_col(scr, "SESSION", cxL, ay + 110);
    brow_col(scr, "WEEKLY",  cxR, ay + 110);
    reset_col(scr, hki() ? s_d.kimi_session_reset_mins : 240,  cxL, ay + 140);
    reset_col(scr, hki() ? s_d.kimi_weekly_reset_mins  : 9878, cxR, ay + 140);

    // ── Footer (same rhythm as Claude/Grok) ──────────────────────────────────
    {
        lv_obj_t* row = crow(scr, 300, 14);
        lv_obj_t* lbl = rtext(row, "IN USE", PC_DIM, &font_styrene_24);
        lv_obj_set_style_text_letter_space(lbl, 3, 0);
        const char* m = (hki() && s_d.kimi_model[0]) ? s_d.kimi_model : "K3";
        if (hki() && !s_d.kimi_model[0])
            dash_chip(row);  // idle
        else
            model_chip(row, m, PC_KIMI, 14, &font_styrene_24);
    }
    {
        char spend[16] = "$6.80";
        if (hki()) snprintf(spend, sizeof spend, "$%.2f", s_d.kimi_today_usd);
        lv_obj_t* row = crow(scr, 350, 12);
        rtext(row, spend, PC_TEXT, &font_departure_48);
        lv_obj_t* t = rtext(row, "TODAY", PC_DIM, &font_styrene_24);
        lv_obj_set_style_text_letter_space(t, 3, 0);
    }
    float sk[7];
    const float* kimi_sk = (hki() && s_d.kimi_series_valid) ? norm7(s_d.kimi_week_series, sk)
                                                            : nullptr;
    job_spark(scr, 32, 412, W - 64, AI_SPARK_H, kimi_sk ? kimi_sk : WEEK_GROK, 7, PC_KIMI);
}

// GROK — roomy single weekly radial (deep blue); hypothetical API-rate activity $.
static void view_grok(lv_obj_t* scr) {
    chrome(scr, nullptr);
    header_grok_logo(scr);
#ifndef UI_SHOT
    if (!hgr()) { no_data(scr); return; }
#endif
    const int W = board_caps().width;
    const int AS = AI_GAUGE_AS, ay = AI_GAUGE_Y;
    const int ax = (W - AS) / 2;
    const int cx = ax + AS / 2;

    // Ring heat-tiered by %. Inner stack mirrors Claude: % on top (hero dep_72,
    // Grok has just one limit), WEEKLY under it, reset tucked into the open bottom.
    const float gw = grok_pct();
    job_arc(scr, ax, ay, AS, gw / 100.0f, band(gw), 20, false);
    pct_col(scr, (int)(gw + 0.5f), cx, ay + 72, &font_departure_72);
    brow_col(scr, "WEEKLY", cx, ay + 150);
    reset_col(scr, hgr() ? s_d.grok_week_reset_mins : 6400, cx, ay + 184);

    // IN USE — same size/style as the Claude view.
    {
        lv_obj_t* row = crow(scr, 300, 14);
        lv_obj_t* lbl = rtext(row, "IN USE", PC_DIM, &font_styrene_24);
        lv_obj_set_style_text_letter_space(lbl, 3, 0);
        model_chip(row, "GROK 4.5", PC_GROK, 14, &font_styrene_24);
    }

    // Hypothetical-at-API-rates activity (flat-rate sub ⇒ real bill ~$0), see
    // [[ai-subscriptions]]. The "7 DAY" caption is gone — the spark under a $
    // figure reads as history without needing a label at an unreadable size.
    {
        char spend[16] = "$18.60";
        if (hgr()) snprintf(spend, sizeof spend, "$%.2f", s_d.grok_today_usd);
        lv_obj_t* row = crow(scr, 350, 12);
        rtext(row, spend, PC_TEXT, &font_departure_48);
        lv_obj_t* t = rtext(row, "TODAY", PC_DIM, &font_styrene_24);
        lv_obj_set_style_text_letter_space(t, 3, 0);
    }
    float sk[7];
    const float* grok_sk = (hgr() && s_d.grok_series_valid) ? norm7(s_d.grok_week_series, sk)
                                                            : nullptr;
    job_spark(scr, 32, 412, W - 64, AI_SPARK_H, grok_sk ? grok_sk : WEEK_GROK, 7, PC_GROK);
}

// WEATHER — no gauge. Four views already open with a 270-degree ring and Luke
// was right that a fifth would be wallpaper; the first cut also ended in a row
// of dithered columns, which he cut too. So this view is editorial rather than
// instrumented: the sky itself is the hero at 180px, the temperature reads
// beside it like a headline, and everything below is plain type on a hairline
// grid. It is the one view on the device with no dithered form at all, which is
// exactly what makes it legible as "not the machine, the world outside".
static void hour_label(char* out, size_t n, int hour24) {
    const int h = hour24 % 24;
    const int h12 = (h % 12) == 0 ? 12 : (h % 12);
    snprintf(out, n, "%d%c", h12, h < 12 ? 'A' : 'P');
}

// label above, value below, centred on cx — the grid both lower rows share.
static void wx_cell(lv_obj_t* scr, int cx, int y, const char* label,
                    const char* value, lv_color_t vcol, bool degree) {
    lv_obj_t* l = rtext(scr, label, PC_DIM, &font_styrene_20);
    lv_obj_set_style_text_letter_space(l, 3, 0);
    lv_obj_update_layout(l);
    lv_obj_set_pos(l, cx - lv_obj_get_width(l) / 2, y);

    lv_obj_t* row = lv_obj_create(scr);
    lv_obj_remove_style_all(row);
    lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_column(row, 3, 0);
    lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
    rtext(row, value, vcol, &font_departure_32);
    if (degree) degree_mark(row, PC_DIM, 10);
    lv_obj_update_layout(row);
    lv_obj_set_pos(row, cx - lv_obj_get_width(row) / 2, y + 26);
}

static void wx_rule(lv_obj_t* scr, int y, int W) {
    lv_obj_t* hr = lv_obj_create(scr);
    lv_obj_remove_style_all(hr);
    lv_obj_set_size(hr, W - 52, 1);
    lv_obj_set_style_bg_color(hr, PC_HAIR, 0);
    lv_obj_set_style_bg_opa(hr, LV_OPA_COVER, 0);
    lv_obj_clear_flag(hr, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_pos(hr, 26, y);
}

static void view_weather(lv_obj_t* scr) {
    chrome(scr, "WEATHER");
    const int W = board_caps().width;
    const bool day = !hwx() || s_d.wx_is_day;
    const int code = hwx() ? s_d.wx_code : 0;
#ifndef UI_SHOT
    if (!hwx()) { no_data(scr); return; }
#endif

    // Sun times, top-right: the drawn half-sun over a horizon, then the clock
    // time. Before dawn it shows the sunrise instead — the next event, not a
    // time that already passed.
    {
        const bool before_dawn = hwx() && s_d.wx_daylight_pct <= 0 && day;
        const char* when = hwx() ? (before_dawn ? s_d.wx_sunrise : s_d.wx_sunset) : "7:56";
        lv_obj_t* row = lv_obj_create(scr);
        lv_obj_remove_style_all(row);
        lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_column(row, 8, 0);
        lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
        const int D = 30;
        uint16_t* b = mkbuf(D, D);
        if (b) {
            for (int i = 0; i < D * D; i++) b[i] = 0x0000;
            const uint16_t c = lv_color_to_u16(PC_DIM);
            const float dome_y = D * 0.58f, r = D * 0.26f;
            disc(b, D, D / 2.0f, dome_y, r, PC_DIM);
            for (int y = (int)dome_y; y < D; y++)
                for (int x = 0; x < D; x++) px565(b, D, x, y, 0x0000);
            for (int x = 0; x < D; x++) {
                px565(b, D, x, (int)dome_y + 4, c);
                px565(b, D, x, (int)dome_y + 5, c);
            }
            lv_obj_t* cv = lv_canvas_create(row);
            lv_canvas_set_buffer(cv, b, D, D, LV_COLOR_FORMAT_RGB565);
        }
        rtext(row, when, PC_DIM, &font_departure_32);
        lv_obj_update_layout(row);
        lv_obj_set_pos(row, W - 26 - lv_obj_get_width(row), 14);
    }

    // The sky, big. 180px is the largest mark on the device by a distance, and
    // deliberately so — at a glance across the room the glyph IS the reading.
    {
        const int D = 180;
        uint16_t* b = mkbuf(D, D);
        if (b) {
            draw_weather_glyph(b, D, code, day);
            canvas_at(scr, b, D, D, 18, 66);
        }
    }

    // Temperature as a headline beside it, with the condition word beneath.
    {
        const float t = hwx() ? s_d.wx_temp : 86;
        char n[8];
        snprintf(n, sizeof n, "%d", (int)lroundf(t));
        lv_obj_t* row = lv_obj_create(scr);
        lv_obj_remove_style_all(row);
        lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
        lv_obj_set_style_pad_column(row, 6, 0);
        lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
        rtext(row, n, temp_color(t), &font_departure_72);
        degree_mark(row, PC_DIM, 20);
        lv_obj_set_pos(row, 222, 96);

        lv_obj_t* w = rtext(scr, wx_word(code, day), PC_DIM, &font_styrene_28);
        lv_obj_set_style_text_letter_space(w, 4, 0);
        lv_obj_set_pos(w, 224, 186);
    }

    wx_rule(scr, 250, W);

    // Today's envelope: what it feels like now, and the day's ends.
    {
        char f[8], hi[8], lo[8];
        snprintf(f,  sizeof f,  "%d", (int)lroundf(hwx() ? s_d.wx_feels : 95));
        snprintf(hi, sizeof hi, "%d", (int)lroundf(hwx() ? s_d.wx_hi : 87));
        snprintf(lo, sizeof lo, "%d", (int)lroundf(hwx() ? s_d.wx_lo : 72));
        wx_cell(scr, W / 6,     272, "FEELS", f,  PC_TEXT, true);
        wx_cell(scr, W / 2,     272, "HIGH",  hi, PC_TEXT, true);
        wx_cell(scr, 5 * W / 6, 272, "LOW",   lo, PC_TEXT, true);
    }

    wx_rule(scr, 344, W);

    // The next twelve hours, every other hour, as type rather than columns —
    // each temperature carries its own colour off the cold->hot ramp, which is
    // all the "chart" a six-value series needs.
    {
        static const float MOCK_T[12] = {86,86,87,85,84,81,78,76,74,73,71,70};
        const bool live = hwx() && s_d.wx_nhours > 0;
        const int have = live ? s_d.wx_nhours : 12;
        const float* temps = live ? s_d.wx_hourly : MOCK_T;
        const int h0 = live ? s_d.wx_hour0 : 15;
        for (int k = 0; k < 6; k++) {
            const int i = k * 2;
            if (i >= have) break;
            char lbl[6], val[8];
            hour_label(lbl, sizeof lbl, h0 + i);
            snprintf(val, sizeof val, "%d", (int)lroundf(temps[i]));
            wx_cell(scr, W * (2 * k + 1) / 12, 368, lbl, val, temp_color(temps[i]), false);
        }
    }
}

// MARKET — a stacked list, in the shape of the TradingView widget Luke reads on
// his phone: one row per instrument, symbol left, last price and today's move
// right. The first cut had a hero sparkline and a direction triangle; both are
// gone at his call. Six rows in two groups — the three indexes, then the three
// biggest movers in his own book (ranked by percent, from the top 25 positions
// by value).
//
// Column geometry is set by Departure Mono's advance width at dep_24 — 244/16 =
// 15.25px per glyph, every glyph, since it is a monospace: the price is
// right-aligned at PRICE_R and the move at MOVE_R, leaving the symbol the span
// from the left margin to PRICE_R minus the widest price. Index prices drop
// their cents (a 7,757.64 that moves 48 points a day does not need them);
// smaller share prices keep them, so six glyphs (91px) is the widest either
// column ever gets and PRICE_R sits far enough left to leave a real gutter.
//
// Vertical rhythm is ONE pitch for all six rows — an earlier cut used 52 for the
// indexes and 44 for the movers, and the mismatch was visible. The MOVERS group
// is the anchor: MK_GAP absorbs any change to MK_TOP, so pulling the index block
// up toward the header (as here) leaves the lower half exactly where it was.
enum {
    MK_LEFT    = 26,
    MK_LOGO    = 28,                              // mover logo box, left of the symbol
    MK_SYM_X   = MK_LEFT + MK_LOGO + 12,          // symbols clear the logo column
    MK_PRICE_R = 340,                             // both value columns sit hard right,
    MK_MOVE_R  = 462,                             // leaving the name column wide.
                                                  // 340, not further right: a six-glyph
                                                  // price (133.11) and a six-glyph move
                                                  // (+15.83) collide otherwise.
    MK_TOP     = 76,                              // first index row
    MK_PITCH   = 48,                              // every row, both groups
    MK_ROW_H   = 24,                              // dep_24 band
    MK_GAP     = 34,                              // index block -> rule
    MK_RULE_Y  = MK_TOP + 2 * MK_PITCH + MK_ROW_H + MK_GAP,   // 230
    MK_BROW_Y  = MK_RULE_Y + 14,                              // 244
    MK_MOVE_Y  = MK_BROW_Y + 20 + 20,                         // 284 — first mover row
};

// ── Mover brand logos ───────────────────────────────────────────────────────
// Streamed from the daemon rather than baked into flash, because the answer to
// "what are today's three biggest movers" is not knowable at build time — it is
// any three of Luke's ~25 priced positions, and the roster itself changes when
// he trades. Baking would mean a reflash every time he buys something.
//
// So the daemon resolves ticker -> company -> favicon (Wikidata for the domain,
// Google's favicon service for the image), caches the result on disk, and
// pushes the three current ones over the serial link with `mklogo` whenever the
// set changes or the device may have rebooted. Three RGB565A8 slots live in
// PSRAM for the life of the process — NOT registered with s_bufs, which
// free_bufs() reclaims on every render.
#define MK_LOGO_SLOTS 3
struct MkLogo {
    char     sym[12];
    int      px;
    uint8_t* data;      // RGB565A8: px*px*2 colour bytes then px*px alpha
    size_t   cap;
};
static MkLogo s_mk_logos[MK_LOGO_SLOTS] = {};

// Descriptors must outlive the draw, so they are static per slot rather than
// stack temporaries (LVGL keeps the pointer, it does not copy the header).
static lv_image_dsc_t* mk_logo_dsc(int px, const uint8_t* data) {
    static lv_image_dsc_t dscs[MK_LOGO_SLOTS];
    static int next = 0;
    lv_image_dsc_t* d = &dscs[next];
    next = (next + 1) % MK_LOGO_SLOTS;
    proto_icon_dsc(d, px, px, data);
    return d;
}

// Find the logo for a symbol, or nullptr. Matching by SYMBOL rather than slot
// index means a reordered top-three (the common case — they shuffle all day)
// never draws the wrong brand next to a ticker.
static const uint8_t* mk_logo_for(const char* sym, int* px_out) {
    for (int i = 0; i < MK_LOGO_SLOTS; i++) {
        if (s_mk_logos[i].data && s_mk_logos[i].sym[0] &&
            strcmp(s_mk_logos[i].sym, sym) == 0) {
            *px_out = s_mk_logos[i].px;
            return s_mk_logos[i].data;
        }
    }
    return nullptr;
}

static void render_view(lv_obj_t* scr, bool intro);   // defined with the public API

void proto_set_logo(int slot, const char* sym, int px, const uint8_t* data, size_t len) {
    if (slot < 0 || slot >= MK_LOGO_SLOTS || !sym || px <= 0 || px > 64) return;
    const size_t need = (size_t)px * px * 3;
    if (!data || len != need) return;
    MkLogo& L = s_mk_logos[slot];
    if (L.cap < need) {
        heap_caps_free(L.data);
#ifdef BOARD_HAS_PSRAM
        L.data = (uint8_t*)heap_caps_malloc(need, MALLOC_CAP_SPIRAM);
#else
        L.data = (uint8_t*)heap_caps_malloc(need, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
#endif
        L.cap = L.data ? need : 0;
    }
    if (!L.data) return;
    memcpy(L.data, data, need);
    L.px = px;
    strlcpy(L.sym, sym, sizeof(L.sym));
    if (s_scr && s_view == PV_MARKET) render_view(s_scr, false);   // show it now
}

// "26690.62" -> "26,691" / "133.11" -> "133.11". Grouping is hand-rolled:
// newlib on the ESP32 has no locale to lean on for %'d.
static void fmt_price(char* out, size_t n, float v) {
    if (v < 1000.0f) { snprintf(out, n, "%.2f", v); return; }
    char raw[16];
    snprintf(raw, sizeof raw, "%ld", (long)(v + 0.5f));
    const int len = (int)strlen(raw);
    int w = 0;
    for (int i = 0; i < len && w < (int)n - 1; i++) {
        if (i > 0 && (len - i) % 3 == 0) out[w++] = ',';
        if (w < (int)n - 1) out[w++] = raw[i];
    }
    out[w] = '\0';
}

// One list row: an optional brand logo, the symbol, then the price and today's
// move right-aligned in their own columns. The values carry the view, so they
// run a size above the names (dep_24 against styrene_20) rather than matching
// them — this is still a six-row table you scan, not a hero numeral.
//
// `y` is the row's text-box top for the names; the numbers sit 3px higher so the
// two fonts share a baseline (styrene_20 is a 21px box, dep_24 a 24px one, both
// with a 4px descent).
static void mk_row(lv_obj_t* scr, int y, const char* sym, float price, float pct,
                   const uint8_t* logo, int logo_px) {
    const int vy = y - 3;
    if (logo && logo_px > 0) {
        lv_image_dsc_t* dsc = mk_logo_dsc(logo_px, logo);
        if (dsc) {
            lv_obj_t* img = lv_image_create(scr);
            lv_image_set_src(img, dsc);
            lv_obj_set_pos(img, MK_LEFT, y + (MK_ROW_H - logo_px) / 2);
        }
    }
    lv_obj_t* s = rtext(scr, sym, PC_TEXT, &font_styrene_20);
    lv_obj_set_style_text_letter_space(s, 3, 0);
    lv_obj_update_layout(s);
    lv_obj_set_pos(s, logo ? MK_SYM_X : MK_LEFT, y + 1);

    char p[16];
    fmt_price(p, sizeof p, price);
    lv_obj_t* pl = rtext(scr, p, PC_DIM, &font_departure_24);
    lv_obj_update_layout(pl);
    lv_obj_set_pos(pl, MK_PRICE_R - lv_obj_get_width(pl), vy);

    char v[12];
    snprintf(v, sizeof v, "%+.2f", pct);
    lv_obj_t* row = lv_obj_create(scr);
    lv_obj_remove_style_all(row);
    lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_END);
    lv_obj_set_style_pad_column(row, 2, 0);
    lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
    rtext(row, v, pct >= 0 ? PC_UP : PC_DOWN, &font_departure_24);
    rtext(row, "%", PC_DIM, &font_styrene_16);
    lv_obj_update_layout(row);
    lv_obj_set_pos(row, MK_MOVE_R - lv_obj_get_width(row), vy);
}

// Section heads, double-struck. Styrene ships one weight in this build, so bold
// is faked the way a renderer fakes it when a family has no bold cut: draw the
// word twice, a pixel apart. At 4bpp the overlap composites to a solid stem and
// both edges keep their antialiasing.
static void mk_head(lv_obj_t* scr, const char* t, int x, int y, int ls) {
    for (int i = 0; i < 2; i++) {
        lv_obj_t* l = rtext(scr, t, PC_DIM, &font_styrene_24);
        lv_obj_set_style_text_letter_space(l, ls, 0);
        lv_obj_set_pos(l, x + i, y);
    }
}

static void view_market(lv_obj_t* scr) {
    chrome(scr, nullptr);                       // the head below is drawn bold
    mk_head(scr, "MARKET", 28, 18, 4);
    const int W = board_caps().width;
#ifndef UI_SHOT
    if (!hmk()) { no_data(scr); return; }
#endif
    // Session state, top-right: a clock and a duration, no words. The words went
    // through two rounds — "CLOSED 18H" read as "closed FOR 18 hours" when it
    // meant "opens in 18", and "OPENS IN" then ate the width — so the clock now
    // carries "time until the session flips" the same way it does on the AI
    // views. WHICH way it flips is the colour: green while the market is trading
    // and counting down to the close, dim while it is shut.
    if (hmk() && s_d.mk_status[0]) {
        const bool open = strcmp(s_d.mk_status, "OPEN") == 0;
        const lv_color_t col = open ? PC_UP : PC_DIM;
        char d[8];
        fmt_dur(d, sizeof d, s_d.mk_countdown_mins);
        lv_obj_t* row = lv_obj_create(scr);
        lv_obj_remove_style_all(row);
        lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER,
                              LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_column(row, 8, 0);
        lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
        const int D = 22;
        uint16_t* b = mkbuf(D, D);
        if (b) {
            draw_clock(b, D, col);
            lv_obj_t* cv = lv_canvas_create(row);
            lv_canvas_set_buffer(cv, b, D, D, LV_COLOR_FORMAT_RGB565);
        }
        lv_obj_t* t = rtext(row, d, col, &font_styrene_24);
        lv_obj_set_style_text_letter_space(t, 3, 0);
        lv_obj_update_layout(row);
        lv_obj_set_pos(row, W - 26 - lv_obj_get_width(row), 18);
    }

    {
        static const char* MOCK_N[3] = {"S&P 500", "NASDAQ", "RUSSELL"};
        static const float MOCK_P[3] = {7757.64f, 26690.62f, 3034.49f};
        static const float MOCK_C[3] = {0.62f, 1.30f, 1.10f};
        const bool live = hmk() && s_d.mk_nix > 0;
        const int n = live ? s_d.mk_nix : 3;
        for (int i = 0; i < n && i < 3; i++)
            mk_row(scr, MK_TOP + i * MK_PITCH,
                   live ? s_d.mk_ix_name[i] : MOCK_N[i],
                   live ? s_d.mk_ix_price[i] : MOCK_P[i],
                   live ? s_d.mk_ix_pct[i]   : MOCK_C[i],
                   nullptr, 0);
    }

    // Hairline + eyebrow: the movers are a different KIND of row (his book, not
    // the market), and the rule is what says so without a word of explanation.
    {
        lv_obj_t* hr = lv_obj_create(scr);
        lv_obj_remove_style_all(hr);
        lv_obj_set_size(hr, W - 2 * MK_LEFT, 1);
        lv_obj_set_style_bg_color(hr, PC_HAIR, 0);
        lv_obj_set_style_bg_opa(hr, LV_OPA_COVER, 0);
        lv_obj_clear_flag(hr, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_pos(hr, MK_LEFT, MK_RULE_Y);
        mk_head(scr, "TOP MOVERS", MK_LEFT, MK_BROW_Y, 3);
    }

    {
        static const char* MOCK_S[3] = {"SPCX", "NLR", "TOL"};
        static const float MOCK_P[3] = {133.11f, 92.40f, 118.75f};
        static const float MOCK_C[3] = {15.83f, 3.47f, -2.48f};
        const bool live = hmk() && s_d.mk_nmv > 0;
        const int n = live ? s_d.mk_nmv : 3;
        for (int i = 0; i < n && i < 3; i++) {
            const char* sym = live ? s_d.mk_mv_sym[i] : MOCK_S[i];
            int lpx = 0;
            const uint8_t* logo = mk_logo_for(sym, &lpx);
            mk_row(scr, MK_MOVE_Y + i * MK_PITCH, sym,
                   live ? s_d.mk_mv_price[i] : MOCK_P[i],
                   live ? s_d.mk_mv_pct[i]   : MOCK_C[i],
                   logo, lpx);
        }
    }
}

// ── Public API ──────────────────────────────────────────────────────────────

// Rebuild the current view. intro=true plays the entrance sweep (fresh view);
// intro=false paints it fully revealed (a silent data refresh).
static void render_view(lv_obj_t* scr, bool intro) {
    s_scr = scr;
    lv_anim_delete(&s_anim_var, reveal_exec);
    if (s_pulse) { lv_timer_delete(s_pulse); s_pulse = nullptr; }

    // Mascot canvas/buffer are about to be freed; stop ticking until a view that
    // draws it (Claude) re-inits it. Prevents ticking a freed buffer on other views.
    s_mascot_on = false;
    s_codex_on = false;  // same for the Codex knot (Codex view re-inits it)
    s_codex_canvas = nullptr;
    s_codex_buf = nullptr;

    lv_obj_clean(scr);
    free_bufs();

    switch (s_view) {
        case PV_CPU:    view_cpu(scr);    break;
        case PV_GPU:    view_gpu(scr);    break;
        case PV_RAM:    view_ram(scr);    break;
        case PV_CLAUDE: view_claude(scr); break;
        case PV_CODEX:  view_codex(scr);  break;
        case PV_KIMI:   view_kimi(scr);   break;
        case PV_GROK:   view_grok(scr);   break;
        case PV_WEATHER: view_weather(scr); break;
        case PV_MARKET:  view_market(scr);  break;
        default:        view_sys(scr);    break;
    }
    start_motion(intro);
}

void proto_render(lv_obj_t* scr) { render_view(scr, true); }

void proto_cycle(int dir) {
    if (!s_scr) return;
    s_view = (s_view + dir + PV_COUNT) % PV_COUNT;
    render_view(s_scr, true);   // view change → play the entrance
}

void proto_set_view(int v) {
    if (!s_scr) return;
    s_view = ((v % PV_COUNT) + PV_COUNT) % PV_COUNT;
    render_view(s_scr, true);   // explicit view jump → play the entrance
}

void proto_update(const UsageData* data) {
    if (!data || !data->valid) return;
    s_d = *data;
    s_has = true;
    if (s_scr) render_view(s_scr, false);   // silent refresh — no entrance replay
}
