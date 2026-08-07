# Handoff — Clawdmeter → PitCrew companion redesign

You're picking up an in-progress redesign of **Clawdmeter**, an ESP32-S3 desk
display. Read this top to bottom before touching code. The immediate deliverable
is a **look prototype** (`-DPITCREW_PROTO`), not the production UI — we're nailing
the visual direction before converting the real screens.

## 1. What the device is

- **Board:** Waveshare ESP32-S3-Touch-AMOLED-2.16 — a **480×480** square AMOLED,
  capacitive touch, three physical buttons (left / right / PWR), USB-C powered
  and cabled to a Windows PC at all times.
- **Stack:** PlatformIO + Arduino-ESP32 (pioarduino core 3.x) + **LVGL 9**.
- **Repo root:** `C:\Users\konis\Clawdmeter` (native Windows path is required).
- **Firmware entry:** `firmware/src/main.cpp` (`setup()` / `loop()`), UI in
  `firmware/src/ui.cpp`. The codebase supports 5 board ports behind a HAL; you
  only care about `waveshare_amoled_216`. See `CLAUDE.md` for the full board map
  and hardware gotchas.
- A Windows Python daemon streams a small JSON payload to the device over the
  USB-CDC serial link (COM3) every 60s. (Not relevant to the look prototype, but
  it's how real data will arrive in Phase B.)

## 2. The redesign mandate

The device used to be an "AI usage meter" with a pixel-art mascot ("Clawd").
We're **retiring that** and reskinning the device as a **companion for PitCrew**
(Luke's PC-monitoring app, an IObit-replacement) in PitCrew's **"CarBase" visual
language**. Think: a small, honest, elegant system/AI dashboard on the desk.

### The CarBase visual language (follow this exactly)

- **Pure black canvas.** `#000` background (AMOLED — black = pixels off).
- **Bayer 4×4 ordered dither, alpha-only, over black.** This is the signature.
  Gauges are *textured*, not solid fills — the filled portion is a dithered
  field of the band color, the empty remainder is *sparse speckle* (never a solid
  block, never empty). Engine lives in `firmware/src/dither.{h,cpp}`:
  ```c
  void dither_meter(uint16_t* buf, int w, int h, float f, lv_color_t color, int cell); // horizontal proportion bar
  void dither_arc  (uint16_t* buf, int w, int h, float f, lv_color_t color, int thick, int cell); // 270° gauge, opens at bottom
  ```
  `cell` = screen px per Bayer cell (2–3 = the chunky look). You draw into an
  RGB565 canvas buffer and hand it to an `lv_canvas`.
- **Typography grammar — two voices:**
  - *Eyebrow/label voice:* uppercase, letter-spaced (~3px), **dim** (`PC_DIM`),
    small sans (Styrene). Field labels, units, view names.
  - *Data voice:* **monospace** for all numeric readouts (mono font today;
    Departure Mono in Phase C).
- **Hairline geometry.** Cards/dividers are 1px `PC_HAIR` borders, near-square
  corners (radius ≤ 2). Status indicators are **square** "LEDs" (radius 0), not
  round.
- **Honest nulls.** Missing data renders as an em-dash "—" in `PC_GREY`, never a
  fake zero. (e.g. a CPU with no temp sensor shows "TEMP — no sensor".)
- **Palette** (exact PitCrew dark-theme values, in `firmware/src/theme.h`):
  `PC_BG #000000`, `PC_CARD #0e0e0e`, `PC_HAIR #262626`, `PC_TEXT #ffffff`,
  `PC_DIM #9c9c96`, `PC_GREY #8c8c86` (nulls), `PC_BLUE #3b65f9` (accent),
  and gauge bands `PC_GREEN #34a868` / `PC_BLUE` / `PC_ORANGE #f09230` /
  `PC_RED #e94a20` at load thresholds **40 / 75 / 90**.

## 3. Current prototype state (what's on the device now)

`firmware/src/proto.{h,cpp}`, built and shown only under `-DPITCREW_PROTO`
(`ui_init()` renders the proto and no-ops the normal UI). It is **multi-view**:

- **4 views** — CPU · GPU · RAM · CLAUDE. Each is a **centered dither-arc hero**
  (232px, 270° ring) with the big value + "%" nested in the arc's open bottom,
  a domain eyebrow inside, and 1–3 supporting mono/eyebrow lines below. Lots of
  black air — one metric domain per screen.
- **Navigation:** the physical **left/right buttons cycle views** (via
  `ui_cycle_view()` → `proto_cycle()`); a **page-dot footer** shows position.
- **Placeholder data** — static mock values (Ryzen 9 5900X, RTX 3080 Ti, etc.)
  render **only in UI_SHOT QA builds**; in production each AI view short-circuits
  to a drawn-dash **NO LIVE DATA** state when its provider's gate is false (a dead
  Claude token → local-only payload → Claude view alone drops out). A desk gauge
  must never present invented numbers as live.
- **Screenshot seam:** serial command `pview <n>` jumps to a view (0–7) so it can
  be captured over USB without pressing buttons.

### Design history (don't repeat these)

- The **first** proto crammed CPU + GPU + RAM as three stacked cards onto ONE
  screen. Rejected: "only one view and everything is too crunched together." The
  current multi-view + one-domain-per-screen layout is the fix. **Keep it
  spacious.**
- **Font glyph coverage is limited.** The bundled fonts were generated with a
  narrow range: mono has `0x20–0x7E` + `·`(0xB7) + a few symbols; Styrene is
  **ASCII only** (`0x20–0x7E`). **Neither has `°`(U+00B0) or `—`(U+2014)** —
  they render as boxes. Until Departure Mono is generated (Phase C) with the
  full glyph set, **draw those marks** instead: the degree sign is a small hollow
  ring (`degree_ring()`), the em-dash a short bar (`dash_mark()`), both already
  in `proto.cpp`. Don't introduce a glyph the font doesn't have without checking
  its `-r` range in the font's `.c` header.

## 4. Build / flash / screenshot workflow

**You (the AI contributor) likely cannot flash — the physical device is on
Luke's PC.** Write buildable code and reason carefully about the 480×480 pixel
layout; Luke builds, flashes, and screenshots. If you *are* on Luke's machine:

```powershell
# pio lives here (not on PATH): %APPDATA%\Python\Python314\Scripts\pio.exe
$env:PLATFORMIO_BUILD_FLAGS="-DPITCREW_PROTO"
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"   # esptool crashes on cp1252
$pio = "$env:APPDATA\Python\Python314\Scripts\pio.exe"

# Build:
& $pio run -d firmware -e waveshare_amoled_216

# Flash (the daemon holds COM3 — KILL IT FIRST or upload gets "Access is denied"):
Get-CimInstance Win32_Process | ? { $_.ProcessId -ne $PID -and ($_.CommandLine -like '*claude_usage_daemon*' -or $_.CommandLine -like '*clawdmeter-watch*') } | % { Stop-Process -Id $_.ProcessId -Force }
& $pio run -d firmware -e waveshare_amoled_216 -t upload --upload-port COM3

# Screenshot a view (0–7) over USB, non-resetting. shot.py opens COM3 with
# DTR/RTS de-asserted (a plain open resets the ESP32 to splash), sends `pview N`,
# then the `screenshot` serial cmd, and saves an RGB565-LE framebuffer as PNG.
& C:\Users\konis\Clawdmeter\.venv\Scripts\python.exe shot.py out.png 0
```

A scheduled task revives the daemon every ~5 min and re-grabs COM3, so flash
promptly after killing it. The device build/flash cadence is ~40s each.

## 5. Roadmap (after the look is signed off)

- **Phase B — real data.** Add a `/api/device/vitals` endpoint to PitCrew
  (reshape its telemetry: CPU/GPU/RAM load, temps, power, VRAM; plus Claude
  `$`-spend, model-in-use, and real 7-day AI usage history from PitCrew's SQLite
  ledger). The Windows daemon (`daemon/claude_usage_daemon_windows.py`) GETs it
  best-effort over localhost and streams it to the device; the firmware drops
  those views if PitCrew is down.
- **Phase C — Departure Mono + full reskin.** Generate the LVGL bitmap font from
  `PitCrew/public/fonts/DepartureMono-Regular.woff2` **with the full glyph set
  (°, —, etc.)**, replace the drawn marks with real glyphs, rebuild every view,
  and retire the Clawd mascot (idle screen → a dithered PitCrew motif).

## 6. Files that matter

| File | Role |
|---|---|
| `firmware/src/proto.{h,cpp}` | The look prototype (multi-view). **Your main canvas.** |
| `firmware/src/dither.{h,cpp}` | Bayer-dither meter/arc engine. |
| `firmware/src/theme.h` | `PC_*` CarBase palette tokens. |
| `firmware/src/ui.cpp` | Production UI; `ui_cycle_view()` routes to `proto_cycle()` under the proto flag. |
| `firmware/src/main.cpp` | `setup`/`loop`, serial command dispatch (`pview`, `screenshot`). |
| `CLAUDE.md` | Board map, hardware pins, and 10 critical LVGL/ESP32 gotchas. |

## 7. Gotchas worth internalizing

- **PSRAM canvas buffers aren't owned by LVGL.** `proto.cpp` tracks every
  `mkbuf()` allocation and frees the set on each re-render (`free_bufs()` after
  `lv_obj_clean`), or you leak ~100KB per view change. Preserve that discipline.
- **LVGL 9 API** (not 8). `lv_screen_active()`, `lv_obj_set_style_*(obj, v, 0)`,
  `lv_canvas_set_buffer(cv, buf, w, h, LV_COLOR_FORMAT_RGB565)`, flex via
  `lv_obj_set_flex_flow/align`.
- **Even-aligned flush + no rotation** on this CO5300 panel — irrelevant to
  static layout but see `CLAUDE.md` #1/#6 if you touch the display path.
- Keep everything **behind `-DPITCREW_PROTO`** until the look is approved; don't
  modify the production `render_*` paths in `ui.cpp` yet.
