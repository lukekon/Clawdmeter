# Prompt for Grok 4.5

Paste this to Grok (running in the `C:\Users\konis\Clawdmeter` repo, or paste
`HANDOFF-pitcrew-redesign.md` alongside it if Grok can't read the file itself).

---

You're contributing to an ESP32-S3 desk display's UI redesign. **First read
`HANDOFF-pitcrew-redesign.md` in the repo root, end to end** — it has the full
context: the device (480×480 AMOLED, LVGL 9), the "CarBase" visual language
(pure-black canvas, Bayer-dither gauges, mono-data + letter-spaced-sans grammar,
hairline geometry, honest "—" nulls), the current 4-view prototype, the build
workflow, and the font/PSRAM gotchas. Don't skip it — the constraints there are
load-bearing.

## The task

Take the look prototype further. It currently has 4 spacious "arc-hero" views
(CPU · GPU · RAM · CLAUDE) — one metric domain per screen, a centered 270°
dither arc, a page-dot footer, side-button nav. It reads clean but it's early.
**Push the design and implement your version** in `firmware/src/proto.{h,cpp}`,
keeping everything behind `-DPITCREW_PROTO`.

You have latitude. Some directions worth considering (pick and justify — don't
do all of them):

- **Sharpen the visual system.** Is the arc-hero the right hero for every view,
  or should some domains (RAM, spend) use the horizontal dither *meter* instead
  for contrast? Refine spacing, weight, the dither `cell`/thickness, the way the
  value nests in the arc.
- **A stronger overview.** Should there be a compact all-systems "at a glance"
  view that *doesn't* re-crunch (the crammed single-screen version was already
  rejected — read why in the handoff)?
- **The Claude/AI view.** This is the soul of the device. Session usage %,
  model-in-use, `$`-spend, and a real 7-day history sparkline (dithered?) are the
  intended data. Design that view to feel like the centerpiece.
- **Motion / state ideas** the CarBase language could support on an always-on
  desk display (subtle, honest — no gratuitous animation).

## Hard constraints

- **LVGL 9** C++ (not v8 API). Target board is 480×480.
- **Keep it spacious** — one domain per screen, generous black margins. The prior
  "everything crunched onto one screen" version was rejected.
- **Font glyph coverage is narrow** — the bundled fonts have no `°` or `—`. Draw
  those as geometric marks (see `degree_ring()` / `dash_mark()` in `proto.cpp`).
  Don't emit a glyph you haven't confirmed exists in the font's `-r` range.
- **Free your PSRAM canvas buffers** on re-render (see `mkbuf`/`free_bufs` — LVGL
  doesn't own canvas memory; leaking ~100KB per view change will crash the
  device).
- It must **compile** under `-DPITCREW_PROTO`. Use only the `PC_*` palette tokens
  from `theme.h` and the `dither_meter`/`dither_arc` primitives from `dither.h`.
- **You can't flash the device — Luke will.** So reason precisely about the
  480×480 pixel layout (positions, sizes, overlaps) rather than relying on
  iterate-and-screenshot. Placeholder data is fine (this is a look proto).

## Deliverable

1. A short **design rationale** (a few paragraphs): what you changed and why, in
   terms of the CarBase language.
2. The **implementation** — edited `firmware/src/proto.{h,cpp}` (and only those,
   plus the `ui.cpp`/`main.cpp` seams if you add a new nav/serial hook), that
   compiles under `-DPITCREW_PROTO`.
3. A note on **anything you'd want to verify on hardware** (dither density at
   this DPI, contrast, glyph marks) and any open design questions for Luke.

Be bold and opinionated — the point of this exercise is to see your design
judgment, not a safe copy of what's there.
