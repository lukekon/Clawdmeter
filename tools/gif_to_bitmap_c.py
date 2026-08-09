#!/usr/bin/env python3
"""Bakes the animated OpenAI logo gif into firmware/src/logo_codex_anim.h as a
hi-res bitmap animation: PX x PX, 4-bit grayscale (16 levels), two pixels per
byte. Played by header_codex_logo() in firmware/src/proto.cpp.

Why this exists: the splash-mini engine renders on a fixed 20x20 cell grid,
which made the knot look blocky on the Codex view. This path keeps full
detail with real anti-aliasing. The knot is NOT in the splash engine's
claudepix_data set - this is its only firmware representation.

Source gif: assets/openai_logo_animation.gif (white knot on black, 671x671;
scanlines -> arcs -> knot -> hold -> loop).

Two rules earn their keep here:

1. The crop is sized off the SETTLED KNOT, not the union of every frame, and
   frames too wide for it are SCALED DOWN to fit rather than clipped.
   The build-up phase sweeps arcs wider than the finished knot, so a union
   crop left the steady-state mark filling only ~64% of the canvas - it read
   as a small logo next to the Grok mark, which fills its box. But a fixed
   knot-sized crop chops the build-up's edges (Luke caught it on the device:
   "in a brief moment of the animation, the logo is cut off on the edges").
   So the crop is per-frame: each frame gets the smallest box that both holds
   its own ink and is at least the knot's box, smoothed into a non-increasing
   envelope so the mark only ever eases inward. The build-up therefore zooms
   gently in and settles at full size, and nothing is ever cut off.

2. Consecutive identical frames collapse into one entry + a hold count.
   The gif's "slow spin" tail is not a spin at all: frames 78..168 were
   byte-identical, i.e. 91 duplicate frames costing ~290 KB of flash to hold
   a still image. They are now one frame with a hold, so the device shows the
   same pause for a fraction of the flash.
"""

from pathlib import Path

from PIL import Image, ImageSequence

ROOT = Path(__file__).resolve().parent.parent
GIF = ROOT / "assets" / "openai_logo_animation.gif"
OUT = ROOT / "firmware" / "src" / "logo_codex_anim.h"

PX = 100                # canvas is PX x PX on-device
T_BG = 28               # luminance below this is background (bbox threshold)
KNOT_FRAC = 0.92        # settled knot fills this fraction of the canvas
SETTLED_FROM = 78       # frames from here on are the resting knot
HOLD_CAP = 60           # ticks; the gif rests ~15s on the knot, which reads as frozen


def bbox_of(frame: Image.Image):
    return frame.point(lambda v: 255 if v >= T_BG else 0).getbbox()


def union(boxes):
    xs0, ys0, xs1, ys1 = zip(*[b for b in boxes if b])
    return min(xs0), min(ys0), max(xs1), max(ys1)


def main() -> None:
    im = Image.open(GIF)
    raws = [f.convert("L").copy() for f in ImageSequence.Iterator(im)]
    W, H = im.size
    print(f"{GIF.name}: {len(raws)} frames, {W}x{H}")

    boxes = [bbox_of(f) for f in raws]
    # Drop any near-empty lead-in (no-op for the current gif).
    start = next(i for i, b in enumerate(boxes) if b)
    raws, boxes = raws[start:], boxes[start:]

    # The settled knot sets the target crop, centred on the knot (rule 1).
    kx0, ky0, kx1, ky1 = union(boxes[SETTLED_FROM:])
    cx, cy = (kx0 + kx1) / 2, (ky0 + ky1) / 2
    limit = min(cx, cy, W - cx, H - cy)          # can't crop past the source edge
    knot_half = min(max(kx1 - kx0, ky1 - ky0) / 2 / KNOT_FRAC, limit)
    print(f"settled knot {(kx0, ky0, kx1, ky1)} -> knot crop {2 * knot_half:.0f}px square")

    # Per-frame: grow the box past this frame's ink (1.06, enough margin that
    # LANCZOS ringing stays off the border too), then smooth into a
    # non-increasing envelope (backwards pass) so the mark only ever eases
    # inward and never pops outward mid-animation.
    halves = []
    for b in boxes:
        need = max(cx - b[0], cy - b[1], b[2] - cx, b[3] - cy) * 1.06
        halves.append(min(max(knot_half, need), limit))
    for i in range(len(halves) - 2, -1, -1):
        halves[i] = max(halves[i], halves[i + 1])
    print(f"crop envelope {2 * halves[0]:.0f}px -> {2 * halves[-1]:.0f}px "
          f"(build-up starts at {halves[-1] / halves[0] * 100:.0f}% scale, nothing clipped)")

    grids = []
    for f, h in zip(raws, halves):
        box = (int(cx - h), int(cy - h), int(cx + h), int(cy + h))
        grids.append(list(f.crop(box).resize((PX, PX), Image.LANCZOS).getdata()))

    # Collapse runs of identical frames into one entry + a hold count (rule 2).
    packed_frames, holds = [], []
    for g in grids:
        nib = [(v * 15 + 127) // 255 for v in g]
        row = bytes((nib[i] << 4) | nib[i + 1] for i in range(0, PX * PX, 2))
        if packed_frames and row == packed_frames[-1]:
            holds[-1] = min(holds[-1] + 1, HOLD_CAP)
        else:
            packed_frames.append(row)
            holds.append(1)
    print(f"{len(grids)} frames -> {len(packed_frames)} unique "
          f"(longest hold {max(holds)} ticks)")

    lines = [
        "#pragma once",
        "// Generated by tools/gif_to_bitmap_c.py from assets/openai_logo_animation.gif",
        "// - do not edit. PX x PX, 4-bit grayscale, two pixels per byte (hi nibble",
        "// first). Each frame carries a hold count in ticks: runs of identical",
        "// source frames are stored once (the gif's tail is a still hold, not a",
        "// spin). Played by header_codex_logo() in proto.cpp, which paints the",
        "// grey level as ALPHA on white so the mark composites over the view.",
        f"#define CODEX_ANIM_PX {PX}",
        f"#define CODEX_ANIM_FRAMES {len(packed_frames)}",
        f"#define CODEX_ANIM_FRAME_BYTES {PX * PX // 2}",
        "static const uint8_t codex_anim_holds[CODEX_ANIM_FRAMES] = {",
        "    " + ",".join(str(h) for h in holds),
        "};",
        "static const uint8_t codex_anim_frames[CODEX_ANIM_FRAMES][CODEX_ANIM_FRAME_BYTES] = {",
    ]
    for row in packed_frames:
        lines.append("    {" + ",".join(str(b) for b in row) + "},")
    lines.append("};")
    OUT.write_text("\n".join(lines) + "\n", encoding="ascii")
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB, "
          f"{len(packed_frames) * PX * PX // 2} bytes of flash)")


if __name__ == "__main__":
    main()
