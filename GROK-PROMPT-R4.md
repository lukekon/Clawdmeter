# Round 4 — Claude has a THIRD limit (Weekly Fable). Add it; keep the model bar.

Luke reviewed against his real claude.ai usage panel. Claude exposes **three** independent limits, not two: **Session (5h)**, **Weekly (7d)**, and a **separate Weekly Fable** cap. He's at 63/63 on the first two but **92% on Weekly Fable** (heavy Fable use) — the device was hiding the one wall he's about to hit. Fix the CLAUDE view; small placeholder fix on GROK. You own `proto.{h,cpp}`, `dither.{h,cpp}`, `theme.h`, behind `-DPITCREW_PROTO`. Claude flashes + shoots.

## 1. CLAUDE view — add the Weekly Fable limit, keep the model bar (Luke wants all of it)
Target composition, top→bottom (it's dense — economize sizing to fit cleanly on 480×480):
- **Two coral arcs** (`PC_CLAUDE`), smaller if needed (~160px): **SESSION** (left) and **WEEKLY** (right). Placeholder **63% / 63%**.
- **`IN USE · OPUS 4.8`** tag with coral LED (unchanged).
- **NEW — Weekly Fable limit bar.** A single horizontal dither limit meter labeled **`WEEKLY FABLE`** with a **`92%`** readout, placeholder frac 0.92. This is a *limit* (how full Fable's weekly cap is), distinct from the model bar below. Since 92% is near the cap, make it read **urgent** — fill coral but let the bar warn as it approaches full (e.g. shift toward `PC_ORANGE`/`PC_RED` past ~85%, or a brighter/heavier readout). Keep it clearly a limit, not a proportion.
- **Model-proportion bar** (keep it) — the 4-seg `BY MODEL` bar: Opus coral / Sonnet gold / Haiku grey / Fable violet. **Label it `BY MODEL`** so it's not confused with the Weekly Fable *limit* above (different Fable number: share-of-usage vs limit-fullness).
- **Spend / RESETS / 7-DAY sparkline** (coral, live cell) at the bottom as before. Placeholder resets: Session `1H`, Weekly/Fable `4D`.

This is the busiest view — prioritize legibility: shrink the arcs, tighten vertical gaps, use the smaller Departure sizes for secondary numbers. If it truly can't all fit with air, tell me what you'd cut rather than cramming.

## 2. GROK view — fix the placeholder $ (don't show $0)
Grok's "$0.00 TODAY" reads as "not used," which is wrong — Luke uses Grok heavily (this redesign runs through it). The device's Grok $ is a **hypothetical-at-API-rates activity figure** (flat-rate sub, real spend ~$0), computed from local logs in Phase B. For now just set a **realistic non-zero placeholder** (e.g. `$18.60 TODAY`) so the mockup isn't misleading. Layout otherwise unchanged (single deep-blue weekly arc, `IN USE · GROK 4.5` tag, no model bar, spend/resets/spark).

## Notes
Everything else (SYS vitals-only, CPU columns, GPU radial, RAM treemap, colors, Departure Mono, motion) stays as R3. Buildable under `-DPITCREW_PROTO`; `free_bufs()` on re-render; in-place re-dither for motion. Output updated `proto.{h,cpp}` (+ `theme.h`/`dither.{h,cpp}` if needed) and a note on what to verify on hardware — especially whether the dense CLAUDE view stays legible.
