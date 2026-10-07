#!/usr/bin/env python
"""Generate assets/vertab.ico -- the window / executable icon.

The icon is drawn here instead of being checked in as a binary so the build
only needs Pillow, and so the artwork can be tweaked in one place.

    python tools/make_icon.py
"""
import os

from PIL import Image, ImageDraw

SIZES = [16, 24, 32, 48, 64, 128, 256]

BG = (31, 34, 42, 255)        # dark slate card
TAB_DIM = (120, 132, 156, 255)  # inactive vertical tabs
TAB_ON = (247, 166, 70, 255)    # active vertical tab (journal-ish accent)
PAGE = (240, 242, 246, 255)     # note body


def draw_icon(size):
    """Draw one square frame: rounded card, vertical tab strip, note lines."""
    scale = 8  # supersample, then downscale for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = s * 0.06
    radius = s * 0.18
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=radius, fill=BG)

    # Vertical tab strip down the left side (the "VerTab" bit).
    tab_x0 = s * 0.17
    tab_w = s * 0.14
    top = s * 0.21
    gap = s * 0.055
    tab_h = (s * 0.58 - gap * 3) / 4
    for i in range(4):
        y0 = top + i * (tab_h + gap)
        d.rounded_rectangle(
            [tab_x0, y0, tab_x0 + tab_w, y0 + tab_h],
            radius=tab_w * 0.22,
            fill=TAB_ON if i == 1 else TAB_DIM,
        )

    # Note lines on the right side (the editor pane).
    line_x0 = s * 0.38
    line_h = s * 0.055
    widths = [0.44, 0.38, 0.44, 0.30]
    for i, w in enumerate(widths):
        y0 = top + i * (tab_h + gap)
        d.rounded_rectangle(
            [line_x0, y0, line_x0 + s * w, y0 + line_h],
            radius=line_h / 2,
            fill=PAGE,
        )

    return img.resize((size, size), Image.LANCZOS)


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(here, "assets")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "vertab.ico")

    frames = [draw_icon(n) for n in SIZES]
    frames[-1].save(out, format="ICO", sizes=[(n, n) for n in SIZES])
    # A PNG copy is handy for READMEs and non-Windows window icons.
    frames[-1].save(os.path.join(out_dir, "vertab.png"), format="PNG")
    print("wrote", out)


if __name__ == "__main__":
    main()
