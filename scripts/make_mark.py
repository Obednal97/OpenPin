#!/usr/bin/env python3
"""Draw the Open Pin mark (the V with two dots) as vector paths.

Writes app/assets/icon-{black,white}.svg and the matching PNGs. The geometry
was fitted to the original bitmap mark (99% pixel overlap) so the shape is
unchanged; only the edges are clean now. Run from the repo root:

    python3 scripts/make_mark.py            # SVGs only (no dependencies)
    python3 scripts/make_mark.py --png      # also PNGs (needs Pillow)

Coordinates are in the mark's own units: 1182.62 wide, 1046 tall, axis at
x = 591.31, y down. Both arms are straight lines cut flat at the top; the
outer bottom is a pair of cubic curves meeting level at the axis; the inner
bottom is flat with 74-unit fillets; the dots are equal circles on the axis.
"""

import math
import sys
from pathlib import Path

W, H = 1182.62, 1046.0
AX = 591.31  # vertical axis
# Half-widths from the axis as a function of y: outer edge, inner edge.
OUTER_TOP, OUTER_SLOPE = 591.306, -0.302682
INNER_TOP, INNER_SLOPE = 390.810, -0.289536
# Outer bottom: the outer edge runs straight to y=CURVE_Y, then a cubic eases
# into the bottom point, leaving along the edge (handle CURVE_H1) and arriving
# level (handle CURVE_H2).
CURVE_Y, CURVE_H1, CURVE_H2 = 789.0, 232.0, 96.0
INNER_BOTTOM, FILLET = 868.0, 74.0
DOTS = ((325.5, 126.2), (631.6, 126.2))  # (centre y, radius), centred on the axis

OUT = Path(__file__).resolve().parent.parent / "app" / "assets"
COLOURS = {"black": "#0e0e0e", "white": "#ffffff"}


def _n(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def geometry():
    """Points of the outline (left half mirrored) and the fillet tangent points."""
    k = math.hypot(1, OUTER_SLOPE)
    p0 = (OUTER_TOP + OUTER_SLOPE * CURVE_Y, CURVE_Y)
    p1 = (p0[0] + CURVE_H1 * OUTER_SLOPE / k, p0[1] + CURVE_H1 / k)
    p2 = (CURVE_H2, H)
    ki = math.hypot(1, INNER_SLOPE)
    yc = INNER_BOTTOM - FILLET
    dc = INNER_TOP + INNER_SLOPE * yc - FILLET * ki  # fillet centre (half-width)
    s = (dc - INNER_SLOPE * yc - INNER_TOP) / ki
    tp = (dc - s / ki, yc + s * INNER_SLOPE / ki)  # tangent point on the inner edge
    return p0, p1, p2, dc, tp


def path_d():
    p0, p1, p2, dc, tp = geometry()
    L = lambda d: AX - d  # noqa: E731
    R = lambda d: AX + d  # noqa: E731
    return " ".join(
        [
            f"M{_n(L(OUTER_TOP))} 0",
            f"L{_n(L(p0[0]))} {_n(p0[1])}",
            f"C{_n(L(p1[0]))} {_n(p1[1])} {_n(L(p2[0]))} {_n(p2[1])} {_n(AX)} {_n(H)}",
            f"C{_n(R(p2[0]))} {_n(p2[1])} {_n(R(p1[0]))} {_n(p1[1])} {_n(R(p0[0]))} {_n(p0[1])}",
            f"L{_n(R(OUTER_TOP))} 0",
            f"L{_n(R(INNER_TOP))} 0",
            f"L{_n(R(tp[0]))} {_n(tp[1])}",
            f"A{_n(FILLET)} {_n(FILLET)} 0 0 1 {_n(R(dc))} {_n(INNER_BOTTOM)}",
            f"L{_n(L(dc))} {_n(INNER_BOTTOM)}",
            f"A{_n(FILLET)} {_n(FILLET)} 0 0 1 {_n(L(tp[0]))} {_n(tp[1])}",
            f"L{_n(L(INNER_TOP))} 0Z",
        ]
    )


def svg(colour):
    dots = "".join(f'<circle cx="{_n(AX)}" cy="{_n(y)}" r="{_n(r)}"/>' for y, r in DOTS)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_n(W)} {_n(H)}" '
        f'width="{_n(W)}" height="{_n(H)}"><title>Open Pin</title>'
        f'<g fill="{COLOURS[colour]}"><path d="{path_d()}"/>{dots}</g></svg>\n'
    )


def outline(steps=64):
    """The outline as a polygon (for the PNG render)."""
    p0, p1, p2, dc, tp = geometry()
    p3 = (0.0, H)

    def bez(t):
        u = 1 - t
        return tuple(
            u**3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t**3 * d
            for a, b, c, d in zip(p0, p1, p2, p3, strict=True)
        )

    curve = [bez(i / steps) for i in range(steps + 1)]
    yc = INNER_BOTTOM - FILLET
    a0 = math.atan2(tp[1] - yc, tp[0] - dc)
    arc = [
        (dc + FILLET * math.cos(a), yc + FILLET * math.sin(a))
        for a in (a0 + (math.pi / 2 - a0) * i / steps for i in range(steps + 1))
    ]
    pts = [(AX - OUTER_TOP, 0)] + [(AX - x, y) for x, y in curve]
    pts += [(AX + x, y) for x, y in reversed(curve)] + [(AX + OUTER_TOP, 0), (AX + INNER_TOP, 0)]
    pts += [(AX + x, y) for x, y in arc] + [(AX - x, y) for x, y in reversed(arc)]
    return pts + [(AX - INNER_TOP, 0)]


def png(colour, height=1046, ss=8):
    from PIL import Image, ImageDraw

    scale = height / H
    w, h = round(W * scale), height
    big = Image.new("L", (w * ss, h * ss), 0)
    draw = ImageDraw.Draw(big)
    f = scale * ss
    draw.polygon([(x * f, y * f) for x, y in outline()], fill=255)
    for y, r in DOTS:
        draw.ellipse([(AX - r) * f, (y - r) * f, (AX + r) * f, (y + r) * f], fill=255)
    alpha = big.resize((w, h), Image.LANCZOS)
    rgb = tuple(int(COLOURS[colour][i : i + 2], 16) for i in (1, 3, 5))
    img = Image.new("RGBA", (w, h), rgb + (0,))
    img.putalpha(alpha)
    return img


def main():
    for colour in COLOURS:
        (OUT / f"icon-{colour}.svg").write_text(svg(colour))
        print("wrote", OUT / f"icon-{colour}.svg")
        if "--png" in sys.argv:
            png(colour).save(OUT / f"icon-{colour}.png", optimize=True)
            print("wrote", OUT / f"icon-{colour}.png")


if __name__ == "__main__":
    main()
