#include <Arduino.h>
#include <Wire.h>
#include <lvgl.h>
#include <ArduinoJson.h>
#include <esp_heap_caps.h>

#include "data.h"
#include "ui.h"
#include "ble.h"
#include "splash.h"
#include "usage_rate.h"
#include "idle.h"
#include "idle_cfg.h"
#include "brightness.h"
#ifdef PITCREW_PROTO
#include "proto.h"
#endif

#include "hal/board_caps.h"
#include "hal/display_hal.h"
#include "hal/touch_hal.h"
#include "hal/input_hal.h"
#include "hal/power_hal.h"
#include "hal/imu_hal.h"
#include "hal/sound_hal.h"

static UsageData usage = {};

// ---- LVGL draw buffers (partial render mode) ----
// PSRAM-equipped boards (S3) can comfortably hold larger strips. PSRAM-free
// boards (e.g. ESP32-C6) allocate from internal SRAM, so we shrink the strip
// — 480×20 RGB565 = 19 KB × 2 buffers = 38 KB, fits beside everything else.
#ifdef BOARD_HAS_PSRAM
#define BUF_LINES 40
#define LV_BUF_CAPS (MALLOC_CAP_SPIRAM)
#else
#define BUF_LINES 20
#define LV_BUF_CAPS (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)
#endif
static uint16_t* buf1 = nullptr;
static uint16_t* buf2 = nullptr;

static uint32_t my_tick(void) { return millis(); }

static void my_flush_cb(lv_display_t* disp, const lv_area_t* area, uint8_t* px_map) {
    int32_t w = area->x2 - area->x1 + 1;
    int32_t h = area->y2 - area->y1 + 1;
    display_hal_draw_bitmap(area->x1, area->y1, w, h, (uint16_t*)px_map);
    lv_display_flush_ready(disp);
}

static void rounder_cb(lv_event_t* e) {
    lv_area_t* area = (lv_area_t*)lv_event_get_param(e);
    display_hal_round_area(&area->x1, &area->y1, &area->x2, &area->y2);
}

// Touch policy is driven by IDLE_WAKE_ON_TOUCH:
//   true  → a press edge while asleep wakes the device and the first touch is
//           swallowed (mirrors the button wake-consumption); a press while
//           awake counts as activity.
//   false → touch never counts as activity and is fully swallowed while the
//           panel is dark, so pets/sleeves can't wake it overnight and LVGL
//           can't quietly toggle splash<->usage on a black panel.
static void my_touch_cb(lv_indev_t* indev, lv_indev_data_t* data) {
    uint16_t x, y;
    bool pressed;
    touch_hal_read(&x, &y, &pressed);
    const bool raw_pressed = pressed;

    if (IDLE_WAKE_ON_TOUCH) {
        static bool touch_was = false;
        static bool touch_wake_swallowed = false;
        if (raw_pressed && !touch_was) {
            // Press edge — consume as wake if asleep.
            if (idle_consume_wake_press()) {
                touch_wake_swallowed = true;
                pressed = false;
            }
        } else if (!raw_pressed && touch_was) {
            // Release edge.
            if (touch_wake_swallowed) {
                touch_wake_swallowed = false;
                pressed = false;
            }
        } else if (raw_pressed && touch_wake_swallowed) {
            // Held finger through wake — keep hiding until release.
            pressed = false;
        }
        touch_was = raw_pressed;
    } else if (idle_is_asleep()) {
        pressed = false;
    }

    if (pressed) {
        data->point.x = x;
        data->point.y = y;
        data->state = LV_INDEV_STATE_PRESSED;
    } else {
        data->state = LV_INDEV_STATE_RELEASED;
    }
}

// Parse a JSON line into UsageData.
static bool parse_json(const char* json, UsageData* out) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, json);
    if (err) {
        Serial.printf("JSON parse error: %s\n", err.c_str());
        return false;
    }

    out->session_pct = doc["s"] | 0.0f;
    out->session_reset_mins = doc["sr"] | -1;
    out->weekly_pct = doc["w"] | 0.0f;
    out->weekly_reset_mins = doc["wr"] | -1;
    strlcpy(out->status, doc["st"] | "unknown", sizeof(out->status));
    out->chime = doc["c"] | false;   // absent (old daemon / chime off) → stay silent
    const char* acct = doc["acct"] | "pro";
    out->enterprise = (strcmp(acct, "ent") == 0);
    out->time_pct = doc["tp"] | 0;
    out->period_days = doc["pd"] | 30;
    strlcpy(out->reset_date, doc["rd"] | "", sizeof(out->reset_date));
    out->clock_epoch = doc["t"] | 0L;
    out->clock_fmt = doc["tf"] | 24;
    // Grok activity — absent → no Grok view (daemon omits it if it can't compute).
    out->grok_valid = !doc["g"].isNull();
    out->grok_week_usd = doc["g"] | 0.0f;
    out->grok_today_usd = doc["gd"] | 0.0f;
    out->grok_week_pct = doc["gwp"] | 0.0f;   // % of xAI's real weekly limit
    out->grok_today_pct = doc["gdp"] | 0.0f;  // % of that limit used today
    out->grok_week_reset_mins = doc["gwr"] | -1;
    out->grok_today_reset_mins = doc["gdr"] | -1;
    // Grok 7-day $ series for the sparkline ("gx":{"wk":[...]}).
    out->grok_series_valid = !doc["gx"].isNull();
    for (int i = 0; i < 7; i++) out->grok_week_series[i] = doc["gx"]["wk"][i] | 0.0f;

    // Kimi (Moonshot) — absent → no Kimi view. "km"=week $ gates the view; the
    // 5h/7d % ("kms"/"kmw") come from Moonshot's /usages endpoint and are only
    // trustworthy when "kml"=1 (the OAuth token was fresh when the daemon polled).
    out->kimi_valid = !doc["km"].isNull();
    out->kimi_week_usd = doc["km"] | 0.0f;
    out->kimi_today_usd = doc["kmd"] | 0.0f;
    out->kimi_session_pct = doc["kms"] | 0.0f;
    out->kimi_session_reset_mins = doc["kmsr"] | -1;
    out->kimi_weekly_pct = doc["kmw"] | 0.0f;
    out->kimi_weekly_reset_mins = doc["kmwr"] | -1;
    out->kimi_limits_valid = (doc["kml"] | 0) != 0;
    out->kimi_series_valid = !doc["kmx"].isNull();
    for (int i = 0; i < 7; i++) out->kimi_week_series[i] = doc["kmx"]["wk"][i] | 0.0f;
    strlcpy(out->kimi_model, doc["kmm"] | "", sizeof(out->kimi_model));

    // Codex (OpenAI) — absent → no Codex view. "cd"=week $ gates the view; the
    // 7-day % ("cdw") comes from the newest rate_limits record in the rollout
    // logs (read locally by the daemon — no token to go stale like Kimi's).
    out->codex_valid = !doc["cd"].isNull();
    out->codex_week_usd = doc["cd"] | 0.0f;
    out->codex_today_usd = doc["cdd"] | 0.0f;
    out->codex_weekly_pct = doc["cdw"] | 0.0f;
    out->codex_weekly_reset_mins = doc["cdwr"] | -1;
    out->codex_series_valid = !doc["cdx"].isNull();
    for (int i = 0; i < 7; i++) out->codex_week_series[i] = doc["cdx"]["wk"][i] | 0.0f;
    strlcpy(out->codex_model, doc["cdm"] | "", sizeof(out->codex_model));

    // Claude model-scoped weekly limit (Weekly-Fable/Opus) — absent → device hides it.
    out->scoped_weekly_valid = !doc["kw"].isNull();
    out->scoped_weekly_pct = doc["kw"] | 0.0f;
    out->scoped_weekly_reset_mins = doc["kwr"] | -1;
    strlcpy(out->scoped_weekly_model, doc["kwm"] | "", sizeof(out->scoped_weekly_model));

    // Claude Code transcript extras ("cx": spend / model / by-model / 7-day).
    out->claude_extras_valid = !doc["cx"].isNull();
    out->claude_today_usd = doc["cx"]["sp"] | 0.0f;
    out->claude_nmodels = 0;
    for (JsonVariantConst mv : doc["cx"]["mu"].as<JsonArrayConst>()) {
        if (out->claude_nmodels >= 3) break;
        strlcpy(out->claude_models[out->claude_nmodels], mv | "", 16);
        out->claude_nmodels++;
    }
    for (int i = 0; i < 4; i++) out->claude_by[i]   = doc["cx"]["by"][i] | 0.0f;
    for (int i = 0; i < 7; i++) out->claude_week[i]  = doc["cx"]["wk"][i] | 0.0f;

    // Machine vitals ("cpu"/"gpu"/"ram"). Each block optional; *_valid gates it.
    Vitals& v = out->vitals;
    JsonVariantConst cpu = doc["cpu"];
    v.cpu_valid = !cpu.isNull();
    v.cpu_pct = cpu["p"] | 0;
    strlcpy(v.cpu_name, cpu["n"] | "", sizeof(v.cpu_name));
    v.cpu_clk_mhz = cpu["clk"] | 0;
    v.cpu_temp_valid = v.cpu_valid && !cpu["t"].isNull();
    v.cpu_temp_c = cpu["t"] | 0;
    v.ncores = 0;
    for (JsonVariantConst c : cpu["c"].as<JsonArrayConst>()) {
        if (v.ncores >= 24) break;
        v.cores[v.ncores++] = c | 0;
    }
    JsonVariantConst gpu = doc["gpu"];
    v.gpu_valid = !gpu.isNull();
    v.gpu_pct = gpu["p"] | 0;
    strlcpy(v.gpu_name, gpu["n"] | "", sizeof(v.gpu_name));
    v.gpu_temp_valid = v.gpu_valid && !gpu["t"].isNull();
    v.gpu_temp_c = gpu["t"] | 0;
    v.gpu_power_w = gpu["pw"] | 0.0f;
    v.gpu_power_limit_w = gpu["pl"] | 0;
    v.gpu_vram_used_mb = gpu["vu"] | 0;
    v.gpu_vram_total_mb = gpu["vt"] | 0;
    JsonVariantConst ram = doc["ram"];
    v.ram_valid = !ram.isNull();
    v.ram_pct = ram["p"] | 0;
    v.ram_used_bytes = ram["u"] | 0LL;
    v.ram_total_bytes = ram["tot"] | 0LL;
    v.ram_nseg = 0;
    for (JsonVariantConst sg : ram["seg"].as<JsonArrayConst>()) {
        if (v.ram_nseg >= 4) break;
        strlcpy(v.ram_segs[v.ram_nseg].name, sg["n"] | "", sizeof(v.ram_segs[0].name));
        v.ram_segs[v.ram_nseg].bytes = sg["b"] | 0LL;
        v.ram_nseg++;
    }
    v.valid = v.cpu_valid || v.gpu_valid || v.ram_valid;

    out->ok = doc["ok"] | false;
    out->valid = true;
    return true;
}

// Run a usage JSON payload through the rate tracker + UI. Shared by both
// transports: the BLE RX characteristic and (on desktops where BLE is
// unreliable) the USB-serial data link. Returns false if the JSON didn't parse.
static bool process_usage_json(const char* json) {
    if (!parse_json(json, &usage)) return false;
#ifdef PITCREW_PROTO
    // The proto owns the whole screen and has none of the normal UI's widgets
    // (ui_init early-returns in proto mode). Feed the proto and skip the rate/
    // splash/ui_update plumbing entirely — touching those uncreated widgets
    // (e.g. via ui_show_screen) stack-overflows the loop task.
    proto_update(&usage);
    return true;
#else
    int g_before = usage_rate_group();
    bool session_reset = usage_rate_sample(usage.session_pct);
    int g_after = usage_rate_group();
    // 5-hour session limit refilled → chime so the user knows they can use
    // Claude again (no-op on boards without a buzzer). Gated on the daemon's
    // opt-in `chime` config; the `buzz` serial cmd ignores it.
    if (session_reset && usage.chime) {
        Serial.println("session reset detected — chime");
        sound_hal_play_reset();
    }
    if (g_after != g_before) {
        Serial.printf("usage rate: group %d -> %d (s=%.2f%%)\n",
            g_before, g_after, usage.session_pct);
        if (splash_is_active()) splash_pick_for_current_rate();
    }
    ui_update(&usage);
    return true;
#endif
}

// ---- Serial command buffer ----
// Sized to hold a full usage JSON payload: on desktops where BLE won't stay
// connected, the daemon streams the same JSON over USB serial (a line starting
// with '{') instead of writing the BLE RX characteristic.
// Phase B adds vitals (per-core array + device names) + the three Claude limits +
// Grok/Claude 7-day series to the payload; Kimi and Codex then pushed a full line
// to ~950 B worst-case — past the old 1024's comfort zone. 1280 leaves headroom.
#define CMD_BUF_SIZE 1280
static char cmd_buf[CMD_BUF_SIZE];
static int cmd_pos = 0;

// USB-serial data link. Latches true on the first usage payload received over
// serial: from then on the cable is THE transport for this session, so the
// (unused, still-advertising) BLE radio must never flip the connection UI back
// to "pairing", and the display holds the last numbers between polls like a desk
// gauge. Data freshness (12h in ui.cpp) — not link state — decides usage vs idle,
// so the device keeps showing the last sync even while the daemon is stopped.
static bool serial_link_ever = false;

static void send_screenshot() {
#ifndef BOARD_HAS_PSRAM
    // A full RGB565 framebuffer doesn't fit in internal SRAM on PSRAM-free
    // boards (e.g. 480×480×2 = 460 KB). Capture is unsupported there.
    Serial.println("SCREENSHOT_UNSUPPORTED");
    return;
#else
    const uint32_t w = board_caps().width;
    const uint32_t h = board_caps().height;
    const uint32_t row_bytes = w * 2;
    const uint32_t buf_size = row_bytes * h;
    uint8_t* sbuf = (uint8_t*)heap_caps_malloc(buf_size, MALLOC_CAP_SPIRAM);
    if (!sbuf) {
        Serial.println("SCREENSHOT_ERR");
        return;
    }

    lv_draw_buf_t draw_buf;
    lv_draw_buf_init(&draw_buf, w, h, LV_COLOR_FORMAT_RGB565, row_bytes, sbuf, buf_size);

    lv_result_t res = lv_snapshot_take_to_draw_buf(lv_screen_active(), LV_COLOR_FORMAT_RGB565, &draw_buf);
    if (res != LV_RESULT_OK) {
        heap_caps_free(sbuf);
        Serial.println("SCREENSHOT_ERR");
        return;
    }

    Serial.printf("SCREENSHOT_START %lu %lu %lu\n",
        (unsigned long)w, (unsigned long)h, (unsigned long)buf_size);
    Serial.flush();
    Serial.write(sbuf, buf_size);
    Serial.flush();
    Serial.println();
    Serial.println("SCREENSHOT_END");
    heap_caps_free(sbuf);
#endif
}

static void check_serial_cmd() {
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') {
            cmd_buf[cmd_pos] = '\0';
            if (cmd_buf[0] == '{') {
                // Usage payload over USB serial — transport-equivalent to a BLE
                // RX write. No owner/encryption checks: the cable is the link.
                bool first = !serial_link_ever;
                (void)first;
                if (process_usage_json(cmd_buf)) {
                    serial_link_ever = true;
#ifndef PITCREW_PROTO
                    // Show "connected" off the cable and, on the very first
                    // payload, surface the usage screen so a reflash visibly lands.
                    // Skipped in proto: it owns the screen and has no such widgets.
                    ui_update_ble_status(BLE_STATE_CONNECTED, "USB", "serial");
                    if (first && ui_get_current_screen() == SCREEN_SPLASH)
                        ui_show_screen(SCREEN_USAGE);
#endif
                    Serial.println("{\"ack\":true}");
                } else {
                    Serial.println("{\"err\":true}");
                }
            }
            else if (strcmp(cmd_buf, "screenshot") == 0) send_screenshot();
            else if (strcmp(cmd_buf, "buzz") == 0)  sound_hal_play_reset();
#ifdef UI_SHOT
            else if (strncmp(cmd_buf, "shot ", 5) == 0) {
                int v = 0, m = 0;
                sscanf(cmd_buf + 5, "%d %d", &v, &m);
                ui_shot_set(v, m);
            }
#endif
#ifdef PITCREW_PROTO
            else if (strncmp(cmd_buf, "pview ", 6) == 0) proto_set_view(atoi(cmd_buf + 6));
            // Relative view cycle — lets a host-side input (e.g. mouse side buttons)
            // flip views over serial exactly like the device's physical side buttons.
            else if (strcmp(cmd_buf, "pnext") == 0) proto_cycle(1);
            else if (strcmp(cmd_buf, "pprev") == 0) proto_cycle(-1);
#endif
            cmd_pos = 0;
        } else if (cmd_pos < CMD_BUF_SIZE - 1) {
            cmd_buf[cmd_pos++] = c;
        }
    }
}

// Each board provides this. Must bring up the shared I2C bus (Wire.begin
// with the board's SDA/SCL pins) and any board-private hardware that has
// to settle before display/touch (e.g. an IO expander gating the LCD
// reset line). Called exactly once at the start of setup().
extern "C" void board_init(void);

void setup() {
    // Phase B's payload grew to ~600-1000 B (vitals + three Claude limits + series).
    // The USB-CDC RX ring defaults to 256 B, so a single daemon write() of the full
    // line overran it and dropped bytes → truncated JSON ("IncompleteInput"). Enlarge
    // the ring (must be set before begin) so the whole line is buffered in one burst.
    Serial.setRxBufferSize(2048);
    Serial.begin(115200);
    delay(300);
    Serial.println("{\"ready\":true}");

    board_init();

    display_hal_init();
    display_hal_begin();
    idle_init();        // takes over panel brightness and starts the idle timer
    brightness_init();  // load the user's saved brightness level and apply via idle

    power_hal_init();
    imu_hal_init();
    sound_hal_init();
    touch_hal_init();

    // ---- LVGL ----
    const int W = board_caps().width;
    const int H = board_caps().height;

    lv_init();
    lv_tick_set_cb(my_tick);

    buf1 = (uint16_t*)heap_caps_malloc(W * BUF_LINES * 2, LV_BUF_CAPS);
    buf2 = (uint16_t*)heap_caps_malloc(W * BUF_LINES * 2, LV_BUF_CAPS);

    lv_display_t* disp = lv_display_create(W, H);
    lv_display_set_color_format(disp, LV_COLOR_FORMAT_RGB565);
    lv_display_set_flush_cb(disp, my_flush_cb);
    lv_display_set_buffers(disp, buf1, buf2, W * BUF_LINES * 2,
                           LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_add_event_cb(disp, rounder_cb, LV_EVENT_INVALIDATE_AREA, NULL);

    lv_indev_t* indev = lv_indev_create();
    lv_indev_set_type(indev, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(indev, my_touch_cb);

    ble_init();
    input_hal_init();

    ui_init();
    ui_update_battery(power_hal_battery_pct(), power_hal_is_charging());

#ifdef UI_SHOT
    // QA harness: fake a connected host + live payload and land on the usage
    // screen so `screenshot` can capture each view over USB (no BLE needed).
    // Drive views with the serial command "shot <view> <metric>".
    ui_update_ble_status(BLE_STATE_CONNECTED, "Clawdmeter", "shot");
    {
        UsageData f = {};
        f.session_pct = 74;  f.session_reset_mins = 158;
        f.weekly_pct  = 9;   f.weekly_reset_mins  = 9878;
        strlcpy(f.status, "allowed", sizeof(f.status));
        f.grok_valid = true;
        f.grok_week_pct  = 1;  f.grok_week_reset_mins  = 8449;
        f.grok_today_pct = 1;  f.grok_today_reset_mins = 182;
        f.grok_week_usd = 89;  f.grok_today_usd = 0;
        f.kimi_valid = true;   f.kimi_limits_valid = true;
        f.kimi_session_pct = 61; f.kimi_session_reset_mins = 240;
        f.kimi_weekly_pct  = 12; f.kimi_weekly_reset_mins  = 9878;
        f.kimi_week_usd = 34;  f.kimi_today_usd = 6.8f;
        strlcpy(f.kimi_model, "K3", sizeof(f.kimi_model));
        f.codex_valid = true;
        f.codex_weekly_pct = 7;  f.codex_weekly_reset_mins = 9993;
        f.codex_week_usd = 65;   f.codex_today_usd = 65;
        strlcpy(f.codex_model, "5.6 SOL", sizeof(f.codex_model));
        f.ok = true; f.valid = true;
        // Seed the session sparkline with a fake wobbly climb so QA screenshots
        // show a trend line, not a single point.
        for (int i = 0; i < 40; i++) {
            f.session_pct = 25 + i * 1.1f + ((i % 4) - 1.5f) * 5;
            if (f.session_pct < 0) f.session_pct = 0;
            ui_update(&f);
        }
        f.session_pct = 74;
        ui_update(&f);
    }
    ui_show_screen(SCREEN_USAGE);
#else
    ui_update_ble_status(ble_get_state(), ble_get_device_name(), ble_get_mac_address());
    ui_show_screen(SCREEN_SPLASH);
#endif

    Serial.printf("Dashboard ready (%s, %dx%d), waiting for data on BLE...\n",
        board_caps().name, W, H);
}

static ble_state_t last_ble_state = BLE_STATE_INIT;

// Hold-to-pair gesture: hold the PWR button ~3s, then RELEASE → clear all BLE
// bonds and re-advertise. Clearing on *release* (not while held) is deliberate:
// holding to power the device OFF (AXP hardware shutdown at 8s) must not wipe
// the bond — a power-off hold never releases before shutdown. To stop a
// "chicken-out" release just before 8s from pairing, the gesture disarms at 6s.
//
//   ~1.5s long-press edge → PENDING
//   3.0s (+1500)          → ARMED   (release from here clears bonds)
//   6.0s (+4500)          → DISARMED (no clear; AXP powers off at 8s)
#define PAIR_ARM_AFTER_LONG_MS    1500   // 3.0s total
#define PAIR_DISARM_AFTER_LONG_MS 4500   // 6.0s total
enum pair_state_t { PAIR_IDLE, PAIR_PENDING, PAIR_ARMED };
static pair_state_t pair_state        = PAIR_IDLE;
static uint32_t     pair_long_seen_ms = 0;

static void pair_tick(void) {
    if (pair_state == PAIR_IDLE && power_hal_pwr_long_pressed()) {
        pair_state = PAIR_PENDING;
        pair_long_seen_ms = millis();
        (void)power_hal_pwr_released();  // drain any stale release edge
        Serial.println("PWR long-press: hold to ~3s then release to pair");
        return;
    }
    if (pair_state == PAIR_IDLE) return;

    if (power_hal_pwr_released()) {
        if (pair_state == PAIR_ARMED) {
            Serial.println("Pair: released in window — clearing bonds, advertising");
            ble_clear_bonds();
        } else {
            Serial.println("Pair: released too early — cancelled");
        }
        pair_state = PAIR_IDLE;
        return;
    }

    uint32_t held = millis() - pair_long_seen_ms;
    if (pair_state == PAIR_PENDING && held >= PAIR_ARM_AFTER_LONG_MS) {
        pair_state = PAIR_ARMED;
        Serial.println("Pair: armed — release to pair");
    } else if (pair_state == PAIR_ARMED && held >= PAIR_DISARM_AFTER_LONG_MS) {
        pair_state = PAIR_IDLE;  // power-off territory; don't pair
        Serial.println("Pair: disarmed (holding toward power-off)");
    }
}

void loop() {
    idle_tick();
    lv_timer_handler();
    ui_tick_anim();
    ble_tick();
    power_hal_tick();
    imu_hal_tick();
    sound_hal_tick();
    splash_tick();
    // Rotation transition (blank + ramp) would fight the idle fade — skip
    // ticks while the panel is dark. A rotation that happens during sleep
    // is detected by the next tick after wake and ramped in then.
    if (!idle_is_asleep()) display_hal_tick();

    // ---- Physical buttons ----
    //   PRIMARY (left)    → previous usage view
    //   SECONDARY (right) → next usage view (only if the board has a 2nd button)
    //   PWR               → on splash: cycle animations; on usage: cycle brightness;
    //                       hold ~3s + release: pairing mode
    // First press from sleep is consumed as a wake-only event by
    // idle_consume_wake_press(); the normal action fires from the second press.
    // Activity bookkeeping happens inside idle_consume_wake_press so no separate
    // idle_note_activity() call is needed here. We act on the rising edge only —
    // the buttons no longer send HID keys, so releases carry no action.
    {
        static bool primary_was = false;
        bool primary_now = input_hal_is_held(INPUT_BTN_PRIMARY);
        if (primary_now && !primary_was) {
            if (!idle_consume_wake_press()) ui_cycle_view(-1);  // left = previous view
        }
        primary_was = primary_now;

        if (board_caps().button_count >= 2) {
            static bool secondary_was = false;
            bool secondary_now = input_hal_is_held(INPUT_BTN_SECONDARY);
            if (secondary_now && !secondary_was) {
                if (!idle_consume_wake_press()) ui_cycle_view(+1);  // right = next view
            }
            secondary_was = secondary_now;
        }

        if (power_hal_pwr_pressed()) {
            if (!idle_consume_wake_press()) {
                // On splash: cycle animations. On the usage view: cycle
                // screen brightness (single non-splash view, no more screens).
                if (ui_get_current_screen() == SCREEN_SPLASH) splash_next();
                else                                          brightness_cycle();
            }
        }

        pair_tick();
    }

#ifndef UI_SHOT
    // Once the USB-serial link has delivered data it owns the connection UI;
    // don't let the (unused, still-advertising) BLE radio flip it to "pairing".
    if (!serial_link_ever) {
        ble_state_t bs = ble_get_state();
        if (bs != last_ble_state) {
            last_ble_state = bs;
            ui_update_ble_status(bs, ble_get_device_name(), ble_get_mac_address());
        }
    }
#endif

    static int  last_pct      = -2;
    static bool last_charging = false;
    int  pct      = power_hal_battery_pct();
    bool charging = power_hal_is_charging();
    if (pct != last_pct || charging != last_charging) {
        last_pct = pct;
        last_charging = charging;
        ui_update_battery(pct, charging);
    }

    check_serial_cmd();

    if (ble_has_data()) {
        if (process_usage_json(ble_get_data())) ble_send_ack();
        else                                    ble_send_nack();
    }

    delay(5);
}
