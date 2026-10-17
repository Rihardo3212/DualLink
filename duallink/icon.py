"""Draws the DualLink icon (two streams merging into one) with Pillow."""
from PIL import Image, ImageDraw

BLUE = (77, 141, 255)
ORANGE = (255, 155, 61)
BG_ON = (20, 24, 33)
BG_OFF = (52, 55, 62)


def make_icon(size=256, on=True):
    s = 4  # supersample for smooth edges
    W = size * s
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, W - 1], radius=int(W * 0.22), fill=BG_ON if on else BG_OFF)
    lw = int(W * 0.085)
    blue = BLUE if on else (150, 150, 150)
    orange = ORANGE if on else (120, 120, 120)
    # two links coming from the top, merging into one arrow
    d.line([(W * 0.30, W * 0.18), (W * 0.30, W * 0.40), (W * 0.50, W * 0.60)], fill=blue, width=lw, joint="curve")
    d.line([(W * 0.70, W * 0.18), (W * 0.70, W * 0.40), (W * 0.50, W * 0.60)], fill=orange, width=lw, joint="curve")
    d.line([(W * 0.50, W * 0.58), (W * 0.50, W * 0.80)], fill=(235, 238, 245), width=lw)
    d.polygon([(W * 0.33, W * 0.68), (W * 0.67, W * 0.68), (W * 0.50, W * 0.86)], fill=(235, 238, 245))
    for x, c in ((0.30, blue), (0.70, orange)):
        r = lw * 0.5
        d.ellipse([W * x - r, W * 0.18 - r, W * x + r, W * 0.18 + r], fill=c)
    return img.resize((size, size), Image.LANCZOS)
