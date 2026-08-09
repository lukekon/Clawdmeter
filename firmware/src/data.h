#pragma once
#include <Arduino.h>

// Machine vitals from PitCrew's telemetry engine (via the daemon). Every field
// is best-effort: a *_valid flag false means "no reading" and the proto view
// falls back to its placeholder / shows an honest null rather than a fake number.
struct Vitals {
    bool valid;              // any of cpu/gpu/ram present in the payload
    // CPU
    bool cpu_valid;
    int  cpu_pct;
    char cpu_name[28];
    int  cpu_clk_mhz;
    bool cpu_temp_valid;     // desktops rarely expose a package temp → often false
    int  cpu_temp_c;
    int  cores[24];          // per-core load %
    int  ncores;
    // GPU (absent when no discrete GPU present)
    bool  gpu_valid;
    int   gpu_pct;
    char  gpu_name[28];
    bool  gpu_temp_valid;
    int   gpu_temp_c;
    float gpu_power_w;
    int   gpu_power_limit_w;
    int   gpu_vram_used_mb;
    int   gpu_vram_total_mb;
    // RAM
    bool      ram_valid;
    int       ram_pct;
    long long ram_used_bytes;
    long long ram_total_bytes;
    // Top memory-consuming processes — segments of the used block on the RAM view.
    int       ram_nseg;
    struct { char name[14]; long long bytes; } ram_segs[4];
};

struct UsageData {
    float session_pct;       // utilization 0-100 (5h window Pro/Max; spending % Enterprise)
    int session_reset_mins;  // minutes until reset
    float weekly_pct;        // 7-day utilization (Pro/Max only; 0 for Enterprise)
    int weekly_reset_mins;   // minutes until weekly reset (Pro/Max only)
    // The 5h/7d numbers above need a live OAuth token, which only Claude Code
    // refreshes while it runs — so an idle desk used to blank the Claude view.
    // The daemon now keeps a last-good reading and ages it off the wall clock
    // (as it already does for Kimi), so "s" is present either way:
    bool claude_limits_valid;  // "s" present → the view has numbers to draw
    bool claude_limits_live;   // "ol"=1 → those numbers came off a fresh poll
    char status[16];         // "allowed", "limited", etc.
    bool chime;              // play the session-reset chime; false unless daemon opts in
    bool enterprise;         // true = Enterprise spending-limit account
    int time_pct;            // 0-100: fraction of billing period elapsed (Enterprise)
    int period_days;         // total billing period length in days (Enterprise)
    char reset_date[12];     // formatted reset date e.g. "Jul 1" (Enterprise)
    long clock_epoch;        // local wall-clock epoch (s) from daemon; 0 = not provided
    int  clock_fmt;          // 12 or 24 (hour format from daemon); defaults to 24
    // Grok activity from PitCrew (via the daemon). Dollar figures are estimated
    // at API rates — an activity gauge, not a bill (SuperGrok is flat-rate).
    bool grok_valid;         // "g" field present → the device can show a Grok view
    float grok_week_usd;     // Grok CLI+Slate spend this week, $ at API rates
    float grok_today_usd;    // ...today
    float grok_week_pct;     // xAI's real weekly-limit utilisation % (0-100), from the Grok CLI
    float grok_today_pct;    // % of that weekly limit consumed today (Grok has no daily limit)
    int grok_week_reset_mins;  // minutes until the weekly limit resets (-1 = unknown)
    int grok_today_reset_mins; // minutes until local midnight ("today" resets there)
    bool grok_series_valid;      // "gx" present → 7-day Grok $ series available
    float grok_week_series[7];   // 7-day daily $ activity (index 6 = today)
    // Kimi (Moonshot) activity + real limits from the daemon. The 5h/7d limit %
    // come from Moonshot's /coding/v1/usages endpoint (mirrors claude.ai's dual
    // window); the $ figures are estimated at API rates — a gauge, not a bill
    // (Kimi Code membership is flat-rate). Absent → the device hides the Kimi view.
    bool kimi_valid;             // "km" field present → the device can show a Kimi view
    float kimi_session_pct;      // 5-hour window utilisation % (0-100)
    int kimi_session_reset_mins; // minutes until the 5h window resets (-1 = unknown)
    float kimi_weekly_pct;       // 7-day window utilisation % (0-100)
    int kimi_weekly_reset_mins;  // minutes until the 7d window resets (-1 = unknown)
    bool kimi_limits_valid;      // "kml"=1 → the 5h/7d % above are live (token was fresh)
    float kimi_week_usd;         // Kimi Code spend this week, $ at API rates
    float kimi_today_usd;        // ...today
    bool kimi_series_valid;      // "kmx" present → 7-day Kimi $ series available
    float kimi_week_series[7];   // 7-day daily $ activity (index 6 = today)
    char kimi_model[16];         // model in use now (e.g. "K3"); "" = idle
    // Codex (OpenAI) activity + weekly limit from the daemon — both read locally
    // from Codex's rollout logs (no endpoint, no token). $ figures are estimated
    // at API rates — a gauge, not a bill (the Codex plan is flat-rate). Codex
    // exposes only a 7-day window (no session/daily one), so the view is
    // single-ring like Grok. Absent → the device hides the Codex view.
    bool codex_valid;            // "cd" field present → the device can show a Codex view
    float codex_weekly_pct;      // 7-day window utilisation % (0-100)
    int codex_weekly_reset_mins; // minutes until the 7d window resets (-1 = unknown)
    float codex_week_usd;        // Codex spend this week, $ at API rates
    float codex_today_usd;       // ...today
    bool codex_series_valid;     // "cdx" present → 7-day Codex $ series available
    float codex_week_series[7];  // 7-day daily $ activity (index 6 = today)
    char codex_model[16];        // model in use now (e.g. "5.6 SOL"); "" = idle
    // Claude's model-scoped weekly limit (the "Weekly Fable"/"Weekly Opus" wall) —
    // NOT in the Messages-API headers; read from /api/oauth/usage. Absent → hide it.
    bool scoped_weekly_valid;
    float scoped_weekly_pct;
    int scoped_weekly_reset_mins;
    char scoped_weekly_model[16]; // driving model's display name (e.g. "Fable")
    // Claude Code transcript activity (the CLAUDE view extras). $ is at API rates —
    // an activity gauge, not a bill (flat-rate subscription).
    bool claude_extras_valid;
    float claude_today_usd;
    int  claude_nmodels;         // models in use right now (parallel sessions)
    char claude_models[3][16];   // e.g. {"OPUS 4.8","FABLE 5"}, most-recent first
    float claude_by[4];          // today's $ split: [Opus, Sonnet, Haiku, Fable]
    float claude_week[7];        // 7-day daily $ series (index 6 = today)
    Vitals vitals;               // CPU / GPU / RAM machine telemetry
    bool ok;                 // data parse succeeded
    bool valid;              // false until first successful parse
};
