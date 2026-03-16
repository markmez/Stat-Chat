#!/usr/bin/env python3
"""
App Store screenshot generator for StatChat.
Style: Snapchat — big screenshot, solid color, bold text, tight layout.
"""

import os
from PIL import Image, ImageDraw, ImageFont

CANVAS_W, CANVAS_H = 1320, 2868

# Screenshot: BIG. Fills most of the canvas.
SCREEN_W = 1200
SCREEN_H = 2300
CORNER_R = 86  # generous, like Snapchat
SIDE_PAD = (CANVAS_W - SCREEN_W) // 2  # 60px each side
SCREEN_X = SIDE_PAD
SCREEN_Y = CANVAS_H - SCREEN_H - 120  # blue strip at bottom

# Colors
DEEP_BLUE = (26, 64, 179)
WHITE = (255, 255, 255)

FONT_PATH = "/System/Library/Fonts/SFNS.ttf"

SCREENSHOTS = [
    {"file": "1_home.png",   "line1": "Quick, Verifiable",   "line2": "AI-Powered Answers"},
    {"file": "2_player.png", "line1": "Instant Access to",   "line2": "Every Player Stat"},
    {"file": "3_splits.png", "line1": "Stat Breakdowns &",   "line2": "Season Projections"},
    {"file": "4_search.png", "line1": "Conversational",      "line2": "AI Search"},
]


def rounded_rect_mask(w, h, radius):
    scale = 4
    mask = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (w * scale - 1, h * scale - 1)],
        radius=radius * scale, fill=255,
    )
    return mask.resize((w, h), Image.LANCZOS)


def draw_screen(canvas, screenshot_path):
    if os.path.exists(screenshot_path):
        shot = Image.open(screenshot_path).convert("RGBA")
        src_w, src_h = shot.size
        scale = SCREEN_W / src_w
        shot = shot.resize((SCREEN_W, int(src_h * scale)), Image.LANCZOS)
        if shot.height > SCREEN_H:
            shot = shot.crop((0, 0, SCREEN_W, SCREEN_H))
    else:
        shot = Image.new("RGBA", (SCREEN_W, SCREEN_H), WHITE + (255,))

    mask = rounded_rect_mask(SCREEN_W, SCREEN_H, CORNER_R)
    result = Image.new("RGBA", (SCREEN_W, SCREEN_H), (0, 0, 0, 0))
    result = Image.composite(shot, result, mask)
    canvas.paste(result, (SCREEN_X, SCREEN_Y), result)


def draw_text(canvas, line1, line2):
    draw = ImageDraw.Draw(canvas)

    try:
        font = ImageFont.truetype(FONT_PATH, 104)
        font.set_variation_by_axes([100, 96, 400, 900])  # Black weight
    except Exception:
        font = ImageFont.truetype(FONT_PATH, 104)

    bbox = draw.textbbox((0, 0), line1, font=font)
    line_h = bbox[3] - bbox[1]
    gap = 14

    # Text sits right above screenshot with small breathing room
    text_bottom = SCREEN_Y - 50
    y2 = text_bottom - line_h
    y1 = y2 - gap - line_h

    # Shift entire text block up — leave ~120px from top for breathing room
    top_target = 120  # pixels from top of canvas
    current_top = y1
    if current_top > top_target:
        shift = current_top - top_target
        y1 -= shift
        y2 -= shift

    draw.text((CANVAS_W // 2, y1), line1, fill=WHITE, font=font, anchor="mt")
    draw.text((CANVAS_W // 2, y2), line2, fill=WHITE, font=font, anchor="mt")


def generate(config, raw_dir, output_dir):
    # Solid background
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), DEEP_BLUE + (255,))
    draw_screen(canvas, os.path.join(raw_dir, config["file"]))
    draw_text(canvas, config["line1"], config["line2"])
    out = os.path.join(output_dir, config["file"].replace(".png", "_framed.png"))
    canvas.convert("RGB").save(out, "PNG")
    print(f"  -> {out}")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    raw, out = os.path.join(here, "raw"), os.path.join(here, "output")
    os.makedirs(raw, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    for c in SCREENSHOTS:
        print(f"  {c['file']}: {'ok' if os.path.exists(os.path.join(raw, c['file'])) else 'MISSING'}")
    print()
    for c in SCREENSHOTS:
        generate(c, raw, out)
    print("\nDone!")


if __name__ == "__main__":
    main()
