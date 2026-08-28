"""Regenerate gui/packaging/apiary_gui.ico.

Three flat-top hexagons in a triangular cluster (one top, two bottom),
filled accent blue on transparent background. Multi-resolution ICO with
sizes [16, 32, 48, 128, 256] embedded.

Anti-aliasing via 4x supersample + LANCZOS downsample.

Build-time only — Pillow is a one-shot dep, not part of the runtime gui group.

    poetry run python gui/packaging/make_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUTPUT = Path(__file__).resolve().parent / "apiary_gui.ico"

ACCENT_RGBA = (88, 166, 255, 255)  # #58a6ff
SUPERSAMPLE = 4  # render N× then downsample for smooth edges
SIZES = [256, 128, 48, 32, 16]


def _flat_top_hex(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    # Flat-top hexagon: vertices at 0°, 60°, 120°, 180°, 240°, 300°.
    return [
        (cx + r * math.cos(math.radians(60 * k)), cy + r * math.sin(math.radians(60 * k)))
        for k in range(6)
    ]


def _render(size: int) -> Image.Image:
    s = size * SUPERSAMPLE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Layout: equilateral triangle of hex centers. The cluster is sized to
    # leave a small margin around the canvas. Tuned visually so the icon
    # reads well at 16px (where each hex is ~5 device px across).
    margin = s * 0.08
    work = s - 2 * margin
    r = work * 0.21  # hex circumscribed radius
    apothem = r * math.sqrt(3) / 2
    gap = r * 0.18  # visible space between adjacent hexes

    # Centers form an equilateral triangle. Distance between centers is
    # 2*apothem + gap (so opposing flat edges have `gap` between them).
    d = 2 * apothem + gap
    triangle_h = d * math.sqrt(3) / 2

    cluster_h = triangle_h + 2 * r  # top vertex of top hex to bottom flat of bottom row
    cx0 = s / 2
    cy0 = (s - cluster_h) / 2 + r  # vertical center of the top hex

    centers = [
        (cx0, cy0),
        (cx0 - d / 2, cy0 + triangle_h),
        (cx0 + d / 2, cy0 + triangle_h),
    ]

    for cx, cy in centers:
        draw.polygon(_flat_top_hex(cx, cy, r), fill=ACCENT_RGBA)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    images = [_render(sz) for sz in SIZES]
    # Pillow writes a multi-resolution ICO when given `sizes=` plus the
    # largest base image; passing `append_images` is the explicit form that
    # preserves each frame's individually-rendered (not just downsampled) art.
    images[0].save(
        OUTPUT,
        format="ICO",
        sizes=[(sz, sz) for sz in SIZES],
        append_images=images[1:],
    )
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes, sizes={SIZES})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
