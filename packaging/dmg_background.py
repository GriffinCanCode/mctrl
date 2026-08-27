"""Render the backdrop for the installer window.

Two resolutions, because Finder reads the image's pixel size as point size: the
1x file sets the window geometry and the 2x file is what a Retina display shows.
"""

from __future__ import annotations

import math
from pathlib import Path

import CoreText
import Quartz
from Foundation import NSAttributedString
from icon import OUT, fill, gradient, new_context, rgb, stroke_style, write_png

WIDTH, HEIGHT = 660, 420
# Where build_app's AppleScript parks the two icons, in window points.
APP_X, ALIAS_X, ICON_Y = 165, 495, 218


def text(ctx, body: str, size: float, weight: str, cx: float, y: float, colour) -> None:
    font = CoreText.CTFontCreateWithName(f"SFPro-{weight}", size, None)
    if font is None:  # older systems ship the display face under another name
        font = CoreText.CTFontCreateWithName(f"HelveticaNeue-{weight}", size, None)
    attributed = NSAttributedString.alloc().initWithString_attributes_(
        body,
        {
            CoreText.kCTFontAttributeName: font,
            CoreText.kCTForegroundColorAttributeName: Quartz.CGColorCreateGenericRGB(*colour),
            CoreText.kCTKernAttributeName: size * 0.012,
        },
    )
    line = CoreText.CTLineCreateWithAttributedString(attributed)
    width, *_ = CoreText.CTLineGetTypographicBounds(line, None, None, None)
    # The canvas is y-down; the text matrix flips glyphs back the right way up.
    Quartz.CGContextSetTextMatrix(ctx, Quartz.CGAffineTransformMakeScale(1, -1))
    Quartz.CGContextSetTextPosition(ctx, cx - width / 2, y)
    CoreText.CTLineDraw(line, ctx)


def render(scale: int = 1) -> Path:
    w, h = WIDTH * scale, HEIGHT * scale
    ctx = new_context(w, h)
    gradient(ctx, "#221A4E", "#0B0820", w / 2, 0, h)

    space = Quartz.CGColorSpaceCreateDeviceRGB()
    halo = Quartz.CGGradientCreateWithColorComponents(
        space, (*rgb("#6D5BFF", 0.30), *rgb("#6D5BFF", 0.0)), (0.0, 1.0), 2
    )
    Quartz.CGContextDrawRadialGradient(
        ctx, halo, (w * 0.5, h * 0.30), 0, (w * 0.5, h * 0.30), w * 0.55, 0
    )

    text(ctx, "MindControl", 27 * scale, "Semibold", w / 2, 74 * scale, (1, 1, 1, 0.96))
    text(
        ctx,
        "Drag the app into your Applications folder",
        13 * scale,
        "Regular",
        w / 2,
        102 * scale,
        (1, 1, 1, 0.52),
    )
    text(
        ctx,
        "Then open it once to grant Camera and Accessibility",
        11.5 * scale,
        "Regular",
        w / 2,
        370 * scale,
        (1, 1, 1, 0.34),
    )

    # Three chevrons pointing at the Applications alias, fading as they go.
    mid_x, mid_y = (APP_X + ALIAS_X) / 2 * scale, ICON_Y * scale
    for i, alpha in enumerate((0.20, 0.38, 0.62)):
        x = mid_x + (i - 1) * 22 * scale
        stroke_style(ctx, 4.5 * scale, (1, 1, 1, alpha))
        Quartz.CGContextMoveToPoint(ctx, x - 5 * scale, mid_y - 11 * scale)
        Quartz.CGContextAddLineToPoint(ctx, x + 6 * scale, mid_y)
        Quartz.CGContextAddLineToPoint(ctx, x - 5 * scale, mid_y + 11 * scale)
        Quartz.CGContextStrokePath(ctx)

    # A ring under each icon, so the two drop targets read as a pair.
    for cx in (APP_X, ALIAS_X):
        fill(ctx, (1, 1, 1, 0.05))
        Quartz.CGContextAddArc(ctx, cx * scale, mid_y, 74 * scale, 0, 2 * math.pi, 0)
        Quartz.CGContextFillPath(ctx)

    suffix = "@2x" if scale > 1 else ""
    return write_png(ctx, OUT / f"dmg-background{suffix}.png")


if __name__ == "__main__":
    for factor in (1, 2):
        print(render(factor))
