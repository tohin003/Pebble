#!/usr/bin/env python3
"""Phase 33 (1.1.0, D57): the menu-bar glyphs for Stealth Lockdown, as template images (black on transparent; macOS
tints them for light and dark menu bars).

  make_tray_glyphs.py [--out src-tauri/icons]

tray-off.png: an outlined pebble with its two eyes. tray-on.png: a filled pebble inside a ring, its eyes cut out. The
shape is a change of form, not of colour, so it reads in a one-colour menu bar. Drawn in an 18-unit square (the
design preview's glyphs) at 8x and reduced to 36 px (tray-icon shows menu-bar icons 18 pt tall). The pebble outline
is the galet profile Avatar.vue draws (vendor/bloub/skins.ts).
"""
import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE, SUPER = 36, 8
UNITS = 18.0
SCALE = SIZE * SUPER / UNITS


def galet(cx, cy, radius, samples=96):
    raw = [1 + .075 * math.cos(2 * a + .5) + .035 * math.cos(3 * a + 2.1)
           for a in (i / samples * 2 * math.pi for i in range(samples))]
    peak = max(raw)
    return [((cx + r * 1.02 / peak * radius * math.cos(i / samples * 2 * math.pi)) * SCALE,
             (cy + r * 1.02 / peak * radius * math.sin(i / samples * 2 * math.pi)) * SCALE)
            for i, r in enumerate(raw)]


def capsule(cx, cy, w, h, degrees, samples=24):
    """A vertical capsule (an eye) w by h units, rotated about its centre."""
    r, half = w / 2, h / 2 - w / 2
    points = [(r * math.cos(math.pi + math.pi * i / samples), -half + r * math.sin(math.pi + math.pi * i / samples))
              for i in range(samples + 1)]
    points += [(r * math.cos(math.pi * i / samples), half + r * math.sin(math.pi * i / samples)) for i in range(samples + 1)]
    t = math.radians(degrees)
    return [((cx + x * math.cos(t) - y * math.sin(t)) * SCALE, (cy + x * math.sin(t) + y * math.cos(t)) * SCALE)
            for x, y in points]


def glyph(on):
    image = Image.new('L', (SIZE * SUPER, SIZE * SUPER), 0)
    draw = ImageDraw.Draw(image)
    if on:
        draw.ellipse([(9 - 8) * SCALE, (9 - 8) * SCALE, (9 + 8) * SCALE, (9 + 8) * SCALE], outline=255, width=round(1.2 * SCALE))
        draw.polygon(galet(9.0, 9.0, 4.7), fill=255)
        for eye in (capsule(9.75, 7.55, 1.3, 2.9, -13), capsule(11.75, 7.7, 1.1, 2.6, -13)):
            draw.polygon(eye, fill=0)
    else:
        outline = galet(9.1, 9.0, 6.4)
        draw.line(outline + outline[:1], fill=255, width=round(1.4 * SCALE), joint='curve')
        for eye in (capsule(10.0, 7.3, 1.6, 3.8, -13), capsule(12.6, 7.4, 1.4, 3.4, -13)):
            draw.polygon(eye, fill=255)
    alpha = image.resize((SIZE, SIZE), Image.LANCZOS)
    out = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
    out.putalpha(alpha)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--out', type=Path, default=Path(__file__).resolve().parents[1] / 'src-tauri/icons')
    args = parser.parse_args(argv)
    for name, on in (('tray-off.png', False), ('tray-on.png', True)):
        glyph(on).save(args.out / name, optimize=True)
        print(args.out / name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
