"""Render the app icon.

CoreGraphics rather than an image library because an app icon is vector work:
superelliptical corners, gradients and round-capped strokes all need to survive
being resampled down to 16 points in the Dock.

    python3 packaging/icon.py               # every concept, into build/icons/
    python3 packaging/icon.py gaze          # just one
    python3 packaging/icon.py gaze --plate  # with corners, for macOS 15 and older
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import Quartz
from Foundation import NSURL

CANVAS = 1024
# Apple's pre-Tahoe template: the body sits in 824pt of a 1024pt canvas and the
# rest is the shadow's room to breathe.
BODY = 824
SQUIRCLE_N = 4.6

# macOS 26 rounds, masks and shadows an app icon itself, so it wants the artwork
# edge to edge. Hand it one that already has rounded corners and transparent
# margins and it reads that as a picture, insetting it into a dark plate. Older
# systems draw the icns as-is, so they need the corners drawn in: --plate.
BLEED = True

OUT = Path(__file__).resolve().parent.parent / "build" / "icons"


# --------------------------------------------------------------------- plumbing


def rgb(value: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    v = value.lstrip("#")
    return (*(int(v[i : i + 2], 16) / 255 for i in (0, 2, 4)), alpha)


def new_context(width: int, height: int | None = None):
    height = width if height is None else height
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, width, height, 8, 0, space, Quartz.kCGImageAlphaPremultipliedLast
    )
    # Work in top-left origin, y down, which is how the layouts below are written.
    Quartz.CGContextTranslateCTM(ctx, 0, height)
    Quartz.CGContextScaleCTM(ctx, 1, -1)
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
    Quartz.CGContextSetAllowsAntialiasing(ctx, True)
    return ctx


def write_png(ctx, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Quartz.CGBitmapContextCreateImage(ctx)
    dest = Quartz.CGImageDestinationCreateWithURL(
        NSURL.fileURLWithPath_(str(path)), "public.png", 1, None
    )
    Quartz.CGImageDestinationAddImage(dest, image, None)
    Quartz.CGImageDestinationFinalize(dest)
    return path


def gradient(ctx, top: str, bottom: str, x: float, y0: float, y1: float) -> None:
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    grad = Quartz.CGGradientCreateWithColorComponents(
        space, (*rgb(top), *rgb(bottom)), (0.0, 1.0), 2
    )
    Quartz.CGContextDrawLinearGradient(ctx, grad, (x, y0), (x, y1), 0)


def squircle(rect: tuple[float, float, float, float], steps: int = 1440):
    """Superellipse path — the continuous corner macOS uses, not a rounded rect."""
    x, y, w, h = rect
    cx, cy, a, b = x + w / 2, y + h / 2, w / 2, h / 2
    path = Quartz.CGPathCreateMutable()
    for i in range(steps):
        t = 2 * math.pi * i / steps
        cos_t, sin_t = math.cos(t), math.sin(t)
        px = cx + a * math.copysign(abs(cos_t) ** (2 / SQUIRCLE_N), cos_t)
        py = cy + b * math.copysign(abs(sin_t) ** (2 / SQUIRCLE_N), sin_t)
        (Quartz.CGPathAddLineToPoint if i else Quartz.CGPathMoveToPoint)(path, None, px, py)
    Quartz.CGPathCloseSubpath(path)
    return path


def body_path(rect: tuple[float, float, float, float]):
    if not BLEED:
        return squircle(rect)
    path = Quartz.CGPathCreateMutable()
    Quartz.CGPathAddRect(path, None, ((rect[0], rect[1]), (rect[2], rect[3])))
    return path


def tile(ctx, top: str, bottom: str, sheen: float = 0.17) -> tuple[float, float, float]:
    """Draw the icon body. Returns the (centre x, centre y, edge length) it filled."""
    edge = CANVAS if BLEED else BODY
    inset = (CANVAS - edge) / 2
    rect = (inset, inset if BLEED else inset - 6, edge, edge)

    Quartz.CGContextSaveGState(ctx)
    if not BLEED:
        Quartz.CGContextSetShadowWithColor(
            ctx, (0, 16), 34, Quartz.CGColorCreateGenericRGB(0, 0, 0, 0.34)
        )
    Quartz.CGContextAddPath(ctx, body_path(rect))
    Quartz.CGContextSetFillColorWithColor(ctx, Quartz.CGColorCreateGenericRGB(*rgb(bottom)))
    Quartz.CGContextFillPath(ctx)
    Quartz.CGContextRestoreGState(ctx)

    Quartz.CGContextSaveGState(ctx)
    Quartz.CGContextAddPath(ctx, body_path(rect))
    Quartz.CGContextClip(ctx)
    gradient(ctx, top, bottom, CANVAS / 2, rect[1], rect[1] + edge)

    # Light falling on the top face.
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    glow = Quartz.CGGradientCreateWithColorComponents(
        space, (1, 1, 1, sheen, 1, 1, 1, 0.0), (0.0, 1.0), 2
    )
    Quartz.CGContextDrawRadialGradient(
        ctx, glow, (CANVAS / 2, rect[1]), 0, (CANVAS / 2, rect[1]), edge * 0.82, 0
    )
    Quartz.CGContextRestoreGState(ctx)

    if not BLEED:
        # The hairline where the light catches the rim. Under Tahoe the system
        # lights its own edge, and a second one drawn here reads as a seam.
        Quartz.CGContextSaveGState(ctx)
        Quartz.CGContextAddPath(ctx, squircle((rect[0] + 2, rect[1] + 2, edge - 4, edge - 4)))
        Quartz.CGContextSetStrokeColorWithColor(ctx, Quartz.CGColorCreateGenericRGB(1, 1, 1, 0.22))
        Quartz.CGContextSetLineWidth(ctx, 3)
        Quartz.CGContextStrokePath(ctx)
        Quartz.CGContextRestoreGState(ctx)

    return CANVAS / 2, rect[1] + edge / 2, edge


def glow(ctx, colour: str, radius: float) -> None:
    Quartz.CGContextSetShadowWithColor(
        ctx, (0, 0), radius, Quartz.CGColorCreateGenericRGB(*rgb(colour, 0.85))
    )


def stroke_style(ctx, width: float, colour=(1, 1, 1, 1)) -> None:
    Quartz.CGContextSetStrokeColorWithColor(ctx, Quartz.CGColorCreateGenericRGB(*colour))
    Quartz.CGContextSetLineWidth(ctx, width)
    Quartz.CGContextSetLineCap(ctx, Quartz.kCGLineCapRound)
    Quartz.CGContextSetLineJoin(ctx, Quartz.kCGLineJoinRound)


def fill(ctx, colour) -> None:
    Quartz.CGContextSetFillColorWithColor(ctx, Quartz.CGColorCreateGenericRGB(*colour))


def circle(ctx, cx: float, cy: float, r: float, mode) -> None:
    Quartz.CGContextAddArc(ctx, cx, cy, r, 0, 2 * math.pi, 0)
    mode(ctx)


def cursor(ctx, cx: float, cy: float, height: float, colour=(1, 1, 1, 1)) -> None:
    """The system arrow pointer, drawn from its own 21x33 grid."""
    pts = [(0, 0), (0, 30.5), (8.2, 22.6), (13.4, 33.0), (18.6, 30.6), (13.4, 20.6), (22.4, 20.6)]
    k = height / 33.0
    ox, oy = cx - 11.2 * k, cy - 16.5 * k
    path = Quartz.CGPathCreateMutable()
    for i, (px, py) in enumerate(pts):
        (Quartz.CGPathAddLineToPoint if i else Quartz.CGPathMoveToPoint)(
            path, None, ox + px * k, oy + py * k
        )
    Quartz.CGPathCloseSubpath(path)
    Quartz.CGContextAddPath(ctx, path)
    fill(ctx, colour)
    Quartz.CGContextFillPath(ctx)


# --------------------------------------------------------------------- concepts


def gaze(ctx) -> None:
    """An eye whose pupil is the pointer: look at it and the cursor is there."""
    cx, cy, b = tile(ctx, "#5B4BF5", "#2A1B8C")
    accent = "#38E8FF"
    w, h = b * 0.315, b * 0.205

    Quartz.CGContextSaveGState(ctx)
    glow(ctx, accent, 44)
    lens = Quartz.CGPathCreateMutable()
    Quartz.CGPathMoveToPoint(lens, None, cx - w, cy)
    Quartz.CGPathAddQuadCurveToPoint(lens, None, cx, cy - 2 * h, cx + w, cy)
    Quartz.CGPathAddQuadCurveToPoint(lens, None, cx, cy + 2 * h, cx - w, cy)
    Quartz.CGContextAddPath(ctx, lens)
    stroke_style(ctx, b * 0.043)
    Quartz.CGContextStrokePath(ctx)
    Quartz.CGContextRestoreGState(ctx)

    Quartz.CGContextSaveGState(ctx)
    glow(ctx, accent, 30)
    fill(ctx, rgb(accent))
    circle(ctx, cx, cy, b * 0.138, Quartz.CGContextFillPath)
    Quartz.CGContextRestoreGState(ctx)

    # The arrow has to survive a 32pt Dock tile, so it fills most of the iris and
    # keeps a gap of tile colour around it rather than touching the rim.
    cursor(ctx, cx + b * 0.010, cy, b * 0.216, rgb("#1B1160"))


def pinch(ctx) -> None:
    """Two arcs closing on a spark: the pinch that clicks."""
    cx, cy, b = tile(ctx, "#7B3FF2", "#160B2E")
    accent = "#3DF0FF"
    r = b * 0.30

    Quartz.CGContextSaveGState(ctx)
    stroke_style(ctx, b * 0.062)
    for start in (math.radians(48), math.radians(228)):
        Quartz.CGContextAddArc(ctx, cx, cy, r, start, start + math.radians(84), 0)
        Quartz.CGContextStrokePath(ctx)
    Quartz.CGContextRestoreGState(ctx)

    Quartz.CGContextSaveGState(ctx)
    stroke_style(ctx, b * 0.020, (*rgb(accent)[:3], 0.45))
    circle(ctx, cx, cy, b * 0.175, Quartz.CGContextStrokePath)
    Quartz.CGContextRestoreGState(ctx)

    Quartz.CGContextSaveGState(ctx)
    glow(ctx, accent, 60)
    fill(ctx, rgb(accent))
    circle(ctx, cx, cy, b * 0.078, Quartz.CGContextFillPath)
    Quartz.CGContextRestoreGState(ctx)


def lock(ctx) -> None:
    """The pointer inside a reticle: gaze throws it, the rings settle it."""
    cx, cy, b = tile(ctx, "#12BFA8", "#08305E")
    accent = "#7CFCE4"

    Quartz.CGContextSaveGState(ctx)
    stroke_style(ctx, b * 0.018, (1, 1, 1, 0.30))
    circle(ctx, cx, cy, b * 0.355, Quartz.CGContextStrokePath)
    stroke_style(ctx, b * 0.024, (*rgb(accent)[:3], 0.75))
    for quadrant in range(4):
        start = math.radians(28 + 90 * quadrant)
        Quartz.CGContextAddArc(ctx, cx, cy, b * 0.262, start, start + math.radians(34), 0)
        Quartz.CGContextStrokePath(ctx)
    Quartz.CGContextRestoreGState(ctx)

    Quartz.CGContextSaveGState(ctx)
    glow(ctx, "#FFFFFF", 34)
    cursor(ctx, cx, cy, b * 0.33)
    Quartz.CGContextRestoreGState(ctx)


def hand(ctx) -> None:
    """A hand abstracted to bars, with the gaze dot it hands off from."""
    cx, cy, b = tile(ctx, "#5E5CE6", "#1C1C28")
    accent = "#8AF0FF"
    bar, gap = b * 0.088, b * 0.042
    base = cy + b * 0.20
    heights = (0.30, 0.365, 0.335, 0.255)

    Quartz.CGContextSaveGState(ctx)
    stroke_style(ctx, bar)
    span = 4 * bar + 3 * gap
    for i, height in enumerate(heights):
        x = cx - span / 2 + bar / 2 + i * (bar + gap) + b * 0.045
        Quartz.CGContextMoveToPoint(ctx, x, base)
        Quartz.CGContextAddLineToPoint(ctx, x, base - b * height)
        Quartz.CGContextStrokePath(ctx)

    thumb_x, thumb_y = cx - span / 2 - bar * 0.10, base - b * 0.02
    Quartz.CGContextMoveToPoint(ctx, thumb_x, thumb_y)
    Quartz.CGContextAddLineToPoint(ctx, thumb_x - b * 0.145, thumb_y - b * 0.135)
    Quartz.CGContextStrokePath(ctx)
    Quartz.CGContextRestoreGState(ctx)

    Quartz.CGContextSaveGState(ctx)
    glow(ctx, accent, 46)
    fill(ctx, rgb(accent))
    circle(ctx, cx + b * 0.045, cy - b * 0.275, b * 0.058, Quartz.CGContextFillPath)
    Quartz.CGContextRestoreGState(ctx)


CONCEPTS = {"gaze": gaze, "pinch": pinch, "lock": lock, "hand": hand}


def render(name: str, into: Path = OUT) -> Path:
    ctx = new_context(CANVAS)
    CONCEPTS[name](ctx)
    return write_png(ctx, into / f"{name}.png")


def main(argv: list[str]) -> int:
    global BLEED
    BLEED = "--plate" not in argv
    names = [a for a in argv if not a.startswith("-")] or list(CONCEPTS)
    unknown = [n for n in names if n not in CONCEPTS]
    if unknown:
        print(f"unknown concept(s): {', '.join(unknown)}; have {', '.join(CONCEPTS)}")
        return 2
    for name in names:
        print(render(name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
