#!/usr/bin/env python3
"""Phase 33 (1.1.0, D46, D57): turn scripts/render_veil_media.mjs's frames into the README's Veil GIFs.

  render_veil_gif.py FRAMES_DIR OUT_DIR [--every 2] [--chrome PATH]

Each kept frame is drawn by headless Chrome on the theme's page background (tokens.css --bg) and the frames become
OUT_DIR/07-stealth-veil-light.gif and 07-stealth-veil.gif (dark), at fps / every frames per second. Nothing is drawn by
hand: the frames are Avatar.vue's own SVG.
"""
import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
PAD = 40


def background(theme):
    tokens = (ROOT / 'src/tokens.css').read_text()
    block = tokens[tokens.index(':root {'):] if theme == 'light' else tokens[tokens.index(':root[data-theme="dark"]'):]
    return re.search(r'--bg:\s*([^;]+);', block.split('}')[0]).group(1).strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('frames', type=Path)
    parser.add_argument('out', type=Path)
    parser.add_argument('--every', type=int, default=2)
    parser.add_argument('--chrome', default=CHROME)
    args = parser.parse_args(argv)
    meta = json.loads((args.frames / 'frames.json').read_text())
    side = meta['size'] + 2 * PAD
    args.out.mkdir(parents=True, exist_ok=True)
    for theme, name in (('light', '07-stealth-veil-light.gif'), ('dark', '07-stealth-veil.gif')):
        target = args.out / name
        if target.exists():
            raise SystemExit('exists: ' + str(target))
        images = []
        with tempfile.TemporaryDirectory(prefix='pebble-veil-', dir='/private/tmp') as scratch:
            for index, frame in enumerate(sorted((args.frames / theme).glob('*.svg'))):
                if index % args.every:
                    continue
                page = Path(scratch) / 'frame.html'
                page.write_text(f'<!doctype html><html><body style="margin:0;background:{background(theme)};'
                                f'display:grid;place-items:center;width:{side}px;height:{side}px">{frame.read_text()}</body></html>')
                shot = Path(scratch) / f'{index:03d}.png'
                subprocess.run([args.chrome, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
                                '--force-device-scale-factor=1', f'--window-size={side},{side}', f'--screenshot={shot}',
                                page.as_uri()], check=True, capture_output=True, timeout=60)
                images.append(Image.open(shot).convert('RGB').copy())
        frame_ms = round(1000 * args.every / meta['fps'])
        palette = [image.quantize(colors=128, method=Image.Quantize.MEDIANCUT) for image in images]
        palette[0].save(target, save_all=True, append_images=palette[1:], duration=frame_ms, loop=0, optimize=True, disposal=1)
        print(json.dumps({'gif': str(target), 'frames': len(images), 'frame_ms': frame_ms, 'bytes': target.stat().st_size}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
