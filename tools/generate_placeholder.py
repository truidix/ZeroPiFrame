#!/usr/bin/env python3
"""
Generates the framebuffer notice images shown via fbi instead of raw
console/systemd output, for every language that has a translation file in
src/translations/:

  - update-please-wait-<lang>.png : shown by install.sh (step 0b) while an
    install/update is running.
  - booting-<lang>.png            : shown by photoframe-boot-splash.service
    during boot, until the slideshow takes over the display.
  - shutting-down-<lang>.png      : shown by the
    /usr/lib/systemd/system-shutdown/ hook right before poweroff/reboot.

Run manually whenever the wording changes or a new language is added:
    python3 tools/generate_placeholder.py

Each screen writes one PNG per language (<basename>-<lang>.png). For the
default language (English), 'update-please-wait.png' (no suffix) is also
written, since install.sh's own step-0b logic (which has to be
self-contained - see its comments) still falls back to that legacy name.
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

# (output basename, heading key, line1 key, line2 key or None)
SCREENS = [
    ('update-please-wait', 'install.placeholder_heading', 'install.placeholder_line1', 'install.placeholder_line2'),
    ('booting', 'boot.splash_heading', 'boot.splash_line1', None),
    ('shutting-down', 'shutdown.splash_heading', 'shutdown.splash_line1', None),
]


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
        if not text:
            return
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text((cx - w / 2, y), text, font=font, fill=fill)

    centered_text(500, heading, heading_font, HEADING_COLOR)
    if line2:
        # Two subtitle lines: as originally laid out.
        centered_text(605, line1, sub_font, SUBTEXT_COLOR)
        centered_text(650, line2, sub_font, SUBTEXT_COLOR)
    else:
        # Single subtitle line: vertically centered where the two-line
        # pair would have been, so all screens share the same heading
        # position regardless of how many subtitle lines they have.
        centered_text(627, line1, sub_font, SUBTEXT_COLOR)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    print(f'Wrote {out_path} ({lang})')


def main():
    langs = sorted(p.stem for p in TRANSLATIONS_DIR.glob('*.json'))
    if not langs:
        raise SystemExit(f'No translation files found in {TRANSLATIONS_DIR}')

    for lang in langs:
        data = json.loads((TRANSLATIONS_DIR / f'{lang}.json').read_text(encoding='utf-8'))

        for basename, heading_key, line1_key, line2_key in SCREENS:
            heading = data.get(heading_key, basename)
            line1 = data.get(line1_key, '')
            line2 = data.get(line2_key, '') if line2_key else ''

            out_path = ASSETS_DIR / f'{basename}-{lang}.png'
            render(lang, heading, line1, line2, out_path)

            if basename == 'update-please-wait' and lang == DEFAULT_LANGUAGE:
                # Legacy/fallback filename install.sh's own step-0b logic
                # uses when the configured language has no dedicated image.
                render(lang, heading, line1, line2, ASSETS_DIR / 'update-please-wait.png')


if __name__ == '__main__':
    main()
