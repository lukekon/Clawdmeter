# Clawdmeter Phase B — real-data payload schema (proposal)

Wiring the PitCrew-companion proto (6 views, behind `-DPITCREW_PROTO`) to live
data. This is the payload/firmware contract to sign off **before** wiring.

## Key discovery — the Fable limit is NOT in the Messages-API headers

The daemon's `max_tokens:1` Haiku call exposes only the **all-models** limits:
`anthropic-ratelimit-unified-5h-utilization` (66–67%) and `-7d-utilization` (63%).
Calling with Fable / Opus / Sonnet returns **HTTP 429 with no rate-limit headers at
all** — a bare block, no percentage. So the separate Weekly-Fable wall is
unobtainable that way.

The real source (the same endpoint Claude Code's `/usage` panel uses) is:

```
GET https://api.anthropic.com/api/oauth/usage
  anthropic-version: 2023-06-01
  anthropic-beta: oauth-2025-04-20
  Authorization: Bearer <oauth token>   # same token the daemon already reads
```

Live response (trimmed) — this IS Luke's claude.ai panel:

```jsonc
"limits": [
  { "kind": "session",       "group": "session", "percent": 67, "severity": "normal",   "resets_at": "...", "is_active": false },
  { "kind": "weekly_all",    "group": "weekly",  "percent": 63, "severity": "normal",   "resets_at": "...", "is_active": false },
  { "kind": "weekly_scoped", "group": "weekly",  "percent": 92, "severity": "critical", "resets_at": "...", "is_active": true,
    "scope": { "model": { "display_name": "Fable" } } }
],
"spend": { "used": { "amount_minor": 0 }, "enabled": false }   // flat-rate → real $0 (see [[ai-subscriptions]])
```

Decision: **switch the Claude poll from the header-scrape to this GET.** It returns
all three limits as clean percents + reset timestamps + a `severity`, and the
scoped-weekly carries the driving model's `display_name` (so the label reads
"WEEKLY FABLE" or "WEEKLY OPUS" honestly — it is not hard-coded to Fable). The GET
doubles as the auth-liveness check (401/403 → same "run claude login" path). The
`max_tokens:1` call is retired.

## Proposed serial-JSON payload (extends today's single-letter schema)

Existing keys unchanged: `s/sr` session, `w/wr` weekly-all, `st/acct/ok`, chime `c`,
clock `t/tf`, Grok `g/gd/gwp/gdp/gwr/gdr`. New/changed below.

```jsonc
{
  // ── Claude limits (now from /api/oauth/usage) ─────────────────────────────
  "s": 67, "sr": 103,           // session % + reset mins   (five_hour)   [existing keys]
  "w": 63, "wr": 7020,          // weekly-all % + reset     (seven_day)   [existing keys]
  "kw": 92, "kwr": 7020,        // scoped weekly % + reset  (weekly_scoped) [NEW]
  "kwm": "Fable",               // scoped model display name → view label  [NEW]
                                //   (omit kw*/kwm entirely when no scoped weekly is active → device hides that bar)

  // ── Machine vitals (best-effort; whole block absent → SYS/CPU/GPU/RAM degrade) ──
  "cpu": { "p": 38, "n": "AMD RYZEN 9 5900X", "clk": 4350, "t": null,
           "c": [42,38,71,15,88,55,22,61,45,33,90,18] },   // p=%, clk=MHz, t=tempC|null, c=per-core %
  "gpu": { "p": 62, "n": "NVIDIA RTX 3080 Ti", "t": 71,
           "pw": 214, "pl": 350, "vu": 9100, "vt": 12288 }, // pw/pl=W, vu/vt=VRAM MB
  "ram": { "p": 57, "u": 19542536192, "tot": 34359738368 }, // u/tot=bytes (device formats GB)

  // ── Grok (unchanged, already flows) ───────────────────────────────────────
  "gwp": 21, "gwr": 6400, "g": 19, "gd": 0
}
```

Nested objects for cpu/gpu/ram (avoids prefix collisions with Grok's `g*`;
ArduinoJson parses nesting for free). Honest nulls preserved: `cpu.t: null` maps to
the existing "TEMP — no sensor" line; a missing `gpu` block → GPU view shows "—".

**Buffer:** this payload is ~500–700 B (per-core array + two device names dominate).
`CMD_BUF_SIZE` is 512 today → **bump to 1024** (one-line change in `main.cpp`, already
bumped once for the serial transport).

## Vitals transport — the one real fork (needs your call)

`scan-telemetry.ps1` is pure PowerShell (CIM + registry + nvidia-smi), standalone —
no PitCrew Node deps. Two ways for the daemon to get it. PitCrew's web server is
**not usually running** (it's dead on :3000 right now), which matters:

- **A. Daemon runs the PS script directly** (`powershell -File scan-telemetry.ps1 -Lite`,
  parse JSON). Works whenever the daemon runs — matches the daemon's existing
  "self-sufficient, needs only itself" design (same as how it computes Grok). Couples
  the daemon to the pitcrew repo path. **Recommended.**
- **B. HTTP GET `localhost:3000/api/scan/telemetry?lite=1`.** Clean project decoupling,
  but vitals go blank whenever the PitCrew app is closed (i.e. most of the time).
- **C. Add `/api/device/vitals` to PitCrew** (compact reshape, mirrors the existing
  `/api/device/usage`) + GET it. Same running-server caveat as B.

## Firmware wiring

- Extend `UsageData` (data.h) with: `scoped_weekly_pct/reset/model[]`, and a
  `vitals` sub-struct (cpu/gpu/ram fields + `int cores[24]`, `int ncores`,
  `bool cpu_temp_valid`, `bool gpu_valid`, `bool vitals_valid`).
- `parse_json` fills them (`doc["kw"]`, `doc["cpu"]["c"]` array, etc.).
- Add `proto_update(const UsageData*)`: stash a copy, re-render current view.
  Call it from `process_usage_json` under `#ifdef PITCREW_PROTO` (today `ui_update`
  no-ops in proto). proto.cpp views read the struct; **fall back to the current
  placeholder constants when a field is absent** so screenshot QA without a daemon
  still renders.

## Scope note — CLAUDE view secondary elements

The three limits + resets + Grok are fully covered above. These proto elements are
**not** in `/api/oauth/usage` and would need extra work (scan Claude Code transcripts
like PitCrew does): the **BY MODEL** share bar, the **IN USE <model>** tag, the
**$ TODAY** spend, and the **7-DAY** sparkline. Proposal: **defer these this phase**
(leave as placeholders or hide), land the three real limits first.
```
