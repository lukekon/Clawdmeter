# Round 2 — reground in PitCrew's REAL identity, then push

Luke's verdict on round 1: the multi-view bones are fine, but it "still looks like generic bars and gauges, not PitCrew." He's right, and the cause is precise — the palette *values* in `theme.h` are already correct (they're PitCrew's real dark tokens), but the proto diverges from PitCrew's Dither Kit in four concrete ways plus three product notes. Fix all of them. You own `proto.{h,cpp}`, `dither.{h,cpp}`, `theme.h`, all behind `-DPITCREW_PROTO`. Claude flashes + screenshots for Luke.

**Ground truth = PitCrew itself** (`C:\Users\konis\pitcrew`). Its design language is the **Dither Kit** (`components/dither-kit/`, `components/bits.tsx`) + **Departure Mono**, on pure black. Match *that*, not a paraphrase. Key facts pulled from its source:

## 1. Fix the load-band colors (biggest tell)
`band()` in `proto.cpp` currently returns **blue** for mid-load at thresholds 40/75/90. Wrong on both counts:
- **Blue (`PC_BLUE 0x3b65f9`) is PitCrew's BRAND/Opus color — never a load band.** Seeing blue bars for "medium CPU" is the #1 reason it doesn't read as PitCrew.
- PitCrew's real heat ramp (`pc-diagram-panel.tsx`, `treemap.tsx`): **≥0.78 red · ≥0.55 orange · ≥0.30 yellow · else green.** Yellow (`PC_YELLOW 0x e7c547`) is in the palette and currently used nowhere.

→ Rewrite `band()` to **green / yellow / orange / red at 30 / 55 / 78** (percent). No blue in load. Blue stays reserved for Claude/Opus identity.

## 2. Give the dither its density gradient (the signature texture)
PitCrew's `paintColumn` (`dither-kit/dither-paint.ts`) computes **`density = (y - top) / depth`** — fills are **dense at the base and dissolve toward the value edge**, alpha `(0.3 + density*0.7)*(1+0.22*intensity)`, off-cells ×`OFF_TIER 0.4`, borders at `0.72`, empty remainder = sparse speckle (never a solid remainder — "reads as a stain").
Right now `dither_meter`/`dither_arc` light every filled cell at a **flat** 0.80/0.82. That's why it looks like plain alpha bars.
→ Make density a **gradient along the fill axis** (base→value), keeping OFF_TIER 0.4 / border 0.72 / speckle 0.22 as they are. This one change makes every existing chart instantly read as Dither Kit.

## 3. Break out of "just meter + arc" — use PitCrew's real chart forms
You're using 2 of ~8 Dither Kit forms. Bring in more where the data fits, so each view has its own form instead of the same bar everywhere:
- **DitherColumns** (vertical per-cell bars, `bits.tsx:1475`) — CPU as a **per-core column heatmap** (fake 8–16 cores). Far more "PitCrew" than one arc.
- **DitherTreemap** (`treemap.tsx`, `bits.tsx:1117`) — RAM or disk as **squarified dithered tiles** (each tile a mini dense-at-floor bar + hairline). Distinctive; nothing else on the device looks like it.
- **DitherSparkline** with a **bright full-alpha "live cell" at the newest point** (`bits.tsx:853`) — replace the homemade 7-day bars with this; the pulsing live cell is a PitCrew signature.
- **DitherLane** (single text-line-tall density strip, `bits.tsx:1249`) — good for a compact history row.
- Keep the **270° DitherRadial** for CPU/GPU load and the Claude session — that IS a PitCrew form; just feed it the gradient (#2) and correct bands (#1).
Pick forms per view; don't put all of them on one screen. Goal: SYS/CPU/GPU/RAM/CLAUDE/GROK each feel visually distinct.

## 4. Text is too small — size for desk glance
PitCrew's web sizes (11px eyebrows, 24px KPIs) are for a monitor; this panel is read across a desk. Keep PitCrew's **grammar** — sans **uppercase letter-spaced eyebrows** (tracking ~0.14em) + **Departure Mono numerics with tabular figures** — but scale hero numerals up dramatically. **Assume Departure Mono is available next flash** (Claude is generating the LVGL font now, full glyph set incl. digits/%/°/—); design as if the hero numbers are big Departure Mono. For sizing in this round use the largest bundled faces (`mono_32`, `tiempos_56/34`, `styrene_28/24/20`); bump every eyebrow ~2 tiers. Fewer, bigger elements per view.

## 5. Kill the "LIVE" gimmick
Remove the always-on green LED + "LIVE" label from `chrome()`. It's a vibecoded fake status. PitCrew's only "live" mark is a small **pulsing square in a state color shown when data is actually streaming** — don't fake it on placeholder data; just drop it. Keep the page dots.

## 6. AI usage is showing Opus only — mirror PitCrew's model breakout
The CLAUDE view reads "OPUS 4.8 · 73%", which looks like *Opus's* usage. PitCrew's AI-usage panel (`components/usage-panel.tsx`) **pools all Claude models + Grok** in its headline and then breaks out **per-model** with a fixed color map: **Opus = blue, Sonnet = yellow, Haiku = grey, Grok = red, Fable = orange.**
→ Reframe CLAUDE as **total Claude** (the session % is the plan-wide limit — inherently all models, not Opus). Demote "OPUS 4.8" to a small *model-in-use* tag. Add a small **per-model split** (a DitherColumns or stacked lane in blue/yellow/grey) so it's visibly Opus+Sonnet+Haiku, not one number. Spend + 7-day sparkline stay.

## 7. Add the GROK view + split AI on SYS
- **6th view `GROK`**, mirroring CLAUDE but as the **red** provider (`PC_RED`, xAI) — weekly-limit %, reset, spend, 7-day sparkline. `PV_COUNT` → 6; page dots follow. (Retire the old sapphire→violet `THEME_GROK_*` idea — PitCrew's map makes Grok **red**.)
- On **SYS**, split the merged "AI 73%" row into **CLAUDE (blue) + GROK (red)** rows → CPU/GPU/RAM/CLAUDE/GROK = 5 rows; retune pitch. Placeholder Grok ~21%.

## Motion — use PitCrew's actual vocabulary, not arbitrary animation
PitCrew's motion is "fast & quiet": **sweep-in entrances** (`easeOutCubic`), a **pulsing live cell** on sparklines, and a **bloom glow** (blurred additive `plus-lighter` copy) on the hot/active metric — no rAF churn, charts repaint on data. Use `lv_anim`/`lv_timer` (LVGL's handler already runs — no plumbing changes); cancel prior anims on view re-render. **CRITICAL: re-dither in place into the existing canvas buffer — never `mkbuf` per frame (leaks ~100 KB/frame → OOM).** Pick resting states that look good frozen (Claude's screenshots are single frames).

## Constraints
Buildable under `-DPITCREW_PROTO`; keep `free_bufs()` on re-render; the drawn `°`/`—` marks can go once Departure Mono lands but keep them working until then; don't touch non-proto files beyond what's needed. Output updated `proto.{h,cpp}` / `dither.{h,cpp}` / `theme.h` + a short note on what you'd want verified on hardware.
