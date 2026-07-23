# Round 3 — Luke's verdict: "much much better." Seven targeted changes.

R2 landed. Now refine. You own `proto.{h,cpp}`, `dither.{h,cpp}`, `theme.h`, behind `-DPITCREW_PROTO`. Claude flashes + shoots. Keep everything that's working (Departure Mono, density gradient, heat bands on vitals, treemap/columns/radial forms, killed LIVE). Changes below are exact.

## 1. SYS is machine vitals only — remove Claude & Grok rows
AI usage doesn't belong in a system-vitals view. **SYS goes back to 3 rows: CPU / GPU / RAM** (heat-banded DitherMeters). Delete the CLAUDE and GROK rows. Retune pitch so 3 rows sit with generous black air (they were cramped at 5). View count stays 6 (SYS/CPU/GPU/RAM/CLAUDE/GROK).

## 2. New color system — Claude is NOT blue; Grok is deep blue
Provider identity is separate from heat. Add tokens to `theme.h` and apply:
- **`PC_CLAUDE = 0xd97757`** (terra-cotta / coral — Anthropic brand). All Claude gauges, sparkline, and Claude chrome use this. **No blue anywhere on Claude.**
- **`PC_GROK = 0x2b4fe0`** (deep royal blue). All Grok gauges + sparkline. (Retire the R2 "Grok = red" — Luke wants deep blue.)
- Heat bands (CPU/GPU/RAM load) stay **green/yellow/orange/red at 30/55/78** — unchanged. `PC_BLUE` is now unused by load; fine.
- These never collide: coral/deep-blue appear only on the two AI views; heat colors only on vitals views.

## 3. CLAUDE view — TWO gauges (session + weekly), and add Fable to the model bar
- **Two arcs side by side**, both `PC_CLAUDE` coral: **SESSION on the LEFT, WEEKLY on the RIGHT** (session = 5h limit ~73%, weekly = 7d limit ~41% placeholder). Size them to fit as a pair (~185px each) with their eyebrows (`SESSION` / `WEEKLY`) and % readouts. This replaces R2's single arc.
- **Per-model bar must include Fable** — you're currently dropping it. Four segments, distinct non-blue hues:
  - **Opus = `PC_CLAUDE` coral** (flagship = brand), **Sonnet = `0xe7c547` gold**, **Haiku = `0x8c8c86` grey**, **Fable = `0x7a6ce4` violet**. Add `PC_SONNET/PC_HAIKU/PC_FABLE` tokens.
  - Model-in-use tag ("IN USE OPUS 4.8") keeps its LED in the active model's color.
- Keep below the bar, in this order (unchanged from R2): spend ($ TODAY), RESETS countdown, 7-DAY sparkline (coral) with the bright live cell.

## 4. GROK view — identical to CLAUDE except one gauge and no model bar
Mirror the CLAUDE layout **exactly** — same vertical rhythm, same spend/resets/sparkline positions — with only these differences (Grok reports only a weekly limit and is a single model):
- **One gauge, not two:** just **WEEKLY** (`PC_GROK` deep blue, ~21% placeholder). Place it where Claude's gauge pair sits (center it, same size band).
- **No per-model bar** (Grok is one model). Keep a model-in-use tag "IN USE GROK 4.5" for layout parity.
- Spend / RESETS / 7-DAY sparkline (deep blue, live cell) all in the **same positions as Claude**. $0.00 today is correct (flat-rate).

## 5. RAM treemap — more color variety, free tile on the RIGHT
- **Move the grey "free/unused" ghost tile to the RIGHT edge** (it's on the left now — reads backwards; used-on-left, free-on-right like a fuel gauge).
- **More hue variety in the used tiles.** Don't leave it orange+yellow. Break used RAM into more categories, each a distinct tile hue cycling PitCrew's treemap palette (blue/orange/yellow/green/red family) — e.g. **Apps, System, Cached, Buffers** as separate colored tiles, then **Free** as the grey speckle ghost on the right. Placeholder proportions are fine; keep the hairline borders + dense-at-floor tile fills. Relabel accordingly.

## Constraints
Buildable under `-DPITCREW_PROTO`; `free_bufs()` on re-render; in-place re-dither for motion (no per-frame `mkbuf`); keep the reveal + spark-pulse motion. Departure Mono for numerics, Styrene letter-spaced eyebrows. Output updated `proto.{h,cpp}` / `theme.h` (+ `dither.{h,cpp}` if needed) and a short note on anything to verify on hardware.
