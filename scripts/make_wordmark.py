#!/usr/bin/env python3
"""Draw the OPEN PIN wordmark as SVG (app/assets/wordmark-{black,white}.svg).

The letters follow the construction of the earlier "PLAVD" wordmark: one
stroke weight (168 units on a 958-unit cap height), large outer corners
(radius 290), small inner corners (radius 20), square stem ends, the L's
slanted terminal (reused on the E's bars) and its rounded foot corner
(radius 250, reused on the E). N is the same arch as PLAVD's A.

Writes the SVGs and, with ImageMagick (`magick`) on PATH, the PNGs the app and
README use: wordmark-{black,white}.png at the SVG's full size, and
wordmark-header-{black,white}.png (29 px tall, 11 px of ink, as before).

    python3 scripts/make_wordmark.py            # into app/assets
"""

import math
import shutil
import subprocess
import sys
from pathlib import Path

CAP = 958  # cap height
STROKE = 168  # stem and bar weight
R_OUT = 290  # outer corner radius (P bowl, D, arch)
R_IN = 20  # inner corner radius
R_FOOT = 250  # L / E bottom-left corner
SLANT = 50 / 160  # L terminal: dx per unit of dy
TOP = 4  # top margin inside the canvas
HEIGHT = 967  # canvas height (as the old wordmark)


def rounded(points):
    """SVG path for a closed polygon; each point is (x, y, radius)."""
    n = len(points)
    out = []
    for i, (x, y, r) in enumerate(points):
        px, py, _ = points[i - 1]
        nx, ny, _ = points[(i + 1) % n]
        if r == 0:
            out.append(("L", x, y))
            continue
        d1 = (x - px, y - py)
        d2 = (nx - x, ny - y)
        l1 = math.hypot(*d1)
        l2 = math.hypot(*d2)
        u1 = (d1[0] / l1, d1[1] / l1)
        u2 = (d2[0] / l2, d2[1] / l2)
        # interior angle between the two edges
        cosang = -(u1[0] * u2[0] + u1[1] * u2[1])
        ang = math.acos(max(-1.0, min(1.0, cosang)))
        t = r / math.tan(ang / 2)
        a = (x - u1[0] * t, y - u1[1] * t)
        b = (x + u2[0] * t, y + u2[1] * t)
        sweep = 1 if (u1[0] * u2[1] - u1[1] * u2[0]) > 0 else 0
        out.append(("L", *a))
        out.append(("A", r, sweep, *b))
    d = []
    for k, seg in enumerate(out):
        if seg[0] == "L":
            d.append(f"{'M' if k == 0 else 'L'}{seg[1]:.1f} {seg[2]:.1f}")
        else:
            _, r, sweep, bx, by = seg
            d.append(f"A{r} {r} 0 0 {sweep} {bx:.1f} {by:.1f}")
    return "".join(d) + "Z"


def rect(x0, y0, x1, y1, r):
    return [(x0, y0, r), (x1, y0, r), (x1, y1, r), (x0, y1, r)]


def letter_o(w=910):
    s = STROKE
    return [rect(0, 0, w, CAP, R_OUT), rect(s, s, w - s, CAP - s, R_IN)], w


def letter_p(w=928):
    s = STROKE
    bowl = 636
    outer = [(0, 0, 0), (w, 0, R_OUT), (w, bowl, R_OUT), (s, bowl, R_IN), (s, CAP, 0), (0, CAP, 0)]
    return [outer, rect(s, s, w - s, bowl - s, R_IN)], w


def letter_e(w=870, mid=800):
    s = STROKE
    cut = s * SLANT
    m0 = (CAP - s) / 2
    outer = [
        (0, 0, 0),
        (w, 0, 0),
        (w - cut, s, 0),
        (s, s, R_IN),
        (s, m0, R_IN),
        (mid, m0, 0),
        (mid - cut, m0 + s, 0),
        (s, m0 + s, R_IN),
        (s, CAP - s, R_IN),
        (w, CAP - s, 0),
        (w - cut, CAP, 0),
        (0, CAP, R_FOOT),
    ]
    return [outer], w


def letter_n(w=910):
    s = STROKE
    outer = [
        (0, CAP, 0),
        (0, 0, R_OUT),
        (w, 0, R_OUT),
        (w, CAP, 0),
        (w - s, CAP, 0),
        (w - s, s, R_IN),
        (s, s, R_IN),
        (s, CAP, 0),
    ]
    return [outer], w


def letter_i():
    return [rect(0, 0, STROKE, CAP, 0)], STROKE


# (letter, gap before it). Gaps follow PLAVD's spacing (~250 between straight
# sides), opened up after the E's terminals and closed a little after round O.
LAYOUT = [
    (letter_o, 1),
    (letter_p, 235),
    (letter_e, 250),
    (letter_n, 300),
    (letter_p, 650),
    (letter_i, 250),
    (letter_n, 250),
]


def build(fill):
    x = 0
    paths = []
    for make, gap in LAYOUT:
        x += gap
        shapes, w = make()
        d = "".join(rounded(shape) for shape in shapes)
        paths.append(f'<path transform="translate({x} {TOP})" d="{d}"/>')
        x += w
    width = x + 1
    body = "\n  ".join(paths)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {HEIGHT}" '
        f'width="{width}" height="{HEIGHT}" role="img" aria-label="Open Pin">\n'
        f'  <g fill="{fill}" fill-rule="evenodd">\n  {body}\n  </g>\n</svg>\n'
    )


def rasterise(svg, png, ink_height=None, pad=(0, 0)):
    """SVG -> PNG with a transparent background via ImageMagick."""
    cmd = ["magick", "-background", "none", "-density", "96", str(svg)]
    if ink_height:
        cmd += [
            "-trim",
            "+repage",
            "-resize",
            f"x{ink_height}",
            "-bordercolor",
            "none",
            "-border",
            f"{pad[0]}x{pad[1]}",
        ]
    subprocess.run(cmd + [f"PNG32:{png}"], check=True)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "app/assets"
    for colour, fill in (("black", "#000"), ("white", "#fff")):
        svg = out / f"wordmark-{colour}.svg"
        svg.write_text(build(fill))
        print("wrote", svg)
        if shutil.which("magick"):
            rasterise(svg, out / f"wordmark-{colour}.png")
            rasterise(svg, out / f"wordmark-header-{colour}.png", ink_height=11, pad=(10, 9))
            print("wrote", out / f"wordmark-{colour}.png", out / f"wordmark-header-{colour}.png")
    if not shutil.which("magick"):
        print("ImageMagick not found: PNGs not updated", file=sys.stderr)
