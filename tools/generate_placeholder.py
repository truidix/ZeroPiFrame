#!/usr/bin/env python3
"""
Generates the 'update-please-wait' placeholder image (shown via fbi during
install.sh's install/update run, see step 0b) for every language that has a
translation file in src/translations/.

Run manually whenever the wording changes or a new language is added:
    python3 tools/generate_placeholder.py

Writes one PNG per language to src/assets/update-please-wait-<lang>.png,
and additionally writes the English version to the legacy unsuffixed name
src/assets/update-please-wait.png (install.sh's fallback when the
configured language has no matching image).
"""
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
TRANSLATIONS_DIR = REPO_ROOT / 'src' / 'translations'
ASSETS_DIR = REPO_ROOT / 'src' / 'assets'
DEFAULT_LANGUAGE = 'en'

WIDTH, HEIGHT = 1920, 1080
BG_COLOR = (17, 20, 24)          # matches --bg in style.css
ACCENT_COLOR = (68, 147, 248)    # matches --accent in style.css
HEADING_COLOR = (230, 235, 240)
SUBTEXT_COLOR = (138, 148, 160)  # close to --muted in style.css

FONT_BOLD = '/usr/local/lib/python3.10/dist-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf'
FONT_REGULAR = '/usr/local/lib/python3.10/dist-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf'


def draw_spinner_arc(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int):
    """Draws a partial ring (visually matches the CSS spinner used
    elsewhere in the web UI) - a static stand-in since fbi shows a single
    still image with no animation."""
    bbox = [cx - r, cy - r, cx + r, cy + r]
    width = 14
    draw.arc(bbox, start=-45, end=250, fill=ACCENT_COLOR, width=width)


def render(lang: str, heading: str, line1: str, line2: str, out_path: Path):
    img = Image.new('RGB', (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    cx = WIDTH // 2

    draw_spinner_arc(draw, cx, 400, 65)

    heading_font = ImageFont.truetype(FONT_BOLD, 64)
    sub_font = ImageFont.truetype(FONT_REGULAR, 34)

    def centered_text(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text((cx - w / 2, y), text, font=font, fill=fill)

    centered_text(500, heading, heading_font, HEADING_COLOR)
    centered_text(605, line1, sub_font, SUBTEXT_COLOR)
    centered_text(650, line2, sub_font, SUBTEXT_COLOR)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    print(f'Wrote {out_path} ({lang})')


def main():
    langs = sorted(p.stem for p in TRANSLATIONS_DIR.glob('*.json'))
    if not langs:
        raise SystemExit(f'No translation files found in {TRANSLATIONS_DIR}')

    for lang in langs:
        data = json.loads((TRANSLATIONS_DIR / f'{lang}.json').read_text(encoding='utf-8'))
        heading = data.get('install.placeholder_heading', 'Update running')
        line1 = data.get('install.placeholder_line1', '')
        line2 = data.get('install.placeholder_line2', '')

        out_path = ASSETS_DIR / f'update-please-wait-{lang}.png'
        render(lang, heading, line1, line2, out_path)

        if lang == DEFAULT_LANGUAGE:
            # Legacy/fallback filename install.sh uses when the configured
            # language has no dedicated image.
            render(lang, heading, line1, line2, ASSETS_DIR / 'update-please-wait.png')


if __name__ == '__main__':
    main()
