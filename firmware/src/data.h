#pragma once
#include <Arduino.h>

struct UsageData {
    float session_pct;       // utilization 0-100 (5h window Pro/Max; spending % Enterprise)
    int session_reset_mins;  // minutes until reset
    float weekly_pct;        // 7-day utilization (Pro/Max only; 0 for Enterprise)
    int weekly_reset_mins;   // minutes until weekly reset (Pro/Max only)
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
    bool ok;                 // data parse succeeded
    bool valid;              // false until first successful parse
};
