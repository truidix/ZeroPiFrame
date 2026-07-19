#!/usr/bin/env python3
"""
Photoframe Slideshow
Displays images from the cache on the framebuffer via pygame.
Supported transitions: none, fade, slide_left, slide_right,
slide_up, slide_down, wipe_left, ken_burns, zoom_in, dissolve
"""

import os
import sys
import time
import math
import random
import signal
import logging
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import yaml
from PIL import Image, ImageOps, ImageFilter

# pygame must be configured before it is imported
os.environ.setdefault('SDL_VIDEODRIVER', 'kmsdrm')
os.environ.setdefault('SDL_VIDEO_KMSDRM_DEVICE', '/dev/dri/card0')
os.environ['SDL_AUDIODRIVER'] = 'dummy'
os.environ['DISPLAY'] = ''

import pygame

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONFIG_PATH      = Path('/opt/photoframe/config.yaml')
CACHE_DIR        = Path('/var/lib/photoframe/cache')
PLACEHOLDER      = Path('/opt/photoframe/static/placeholder.png')
SUPPORTED_IMAGES = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp'}
SUPPORTED_VIDEOS = {'.mp4', '.mkv', '.mov', '.avi', '.m4v', '.webm'}
SUPPORTED        = SUPPORTED_IMAGES | SUPPORTED_VIDEOS
LOG_FORMAT       = '%(asctime)s [slideshow] %(levelname)s: %(message)s'

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_config: dict = {}
_reload_flag: bool = False

DEFAULT_CONFIG = {
    'slideshow': {
        'interval_seconds': 30,
        'transition': 'ken_burns',
        'transition_duration_ms': 1500,
        'ken_burns_zoom': 0.08,
        'shuffle': True,
        'fit_mode': 'contain',
        'background_color': '#000000',
        'video_enabled': True,
        'video_audio': False,
    }
}


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        # Fill in defaults
        for k, v in DEFAULT_CONFIG['slideshow'].items():
            cfg.setdefault('slideshow', {}).setdefault(k, v)
        return cfg
    except Exception as e:
        log.warning(f'Config not readable ({e}), using defaults')
        return DEFAULT_CONFIG.copy()


def _sighup_handler(signum, frame):
    global _reload_flag
    _reload_flag = True
    log.info('SIGHUP received - config will be reloaded on the next image change')


signal.signal(signal.SIGHUP, _sighup_handler)

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def hex_to_rgb(hex_color: str) -> tuple:
    """Converts "#rrggbb" into an (r,g,b) tuple.

    Falls back to black on an invalid value (e.g. a config.yaml that was
    hand-edited and broken) instead of crashing the slideshow - the
    background color is purely cosmetic, so crashing over it would be
    disproportionate.
    """
    try:
        h = hex_color.lstrip('#')
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    except Exception:
        log.warning(f'Invalid background color "{hex_color}", using black')
        return (0, 0, 0)


def pil_to_surface(pil_img: Image.Image) -> pygame.Surface:
    """PIL Image -> pygame Surface (RGB)."""
    if pil_img.mode != 'RGB':
        pil_img = pil_img.convert('RGB')
    return pygame.image.frombuffer(pil_img.tobytes(), pil_img.size, 'RGB').copy()


def load_and_fit(path: Path, w: int, h: int, fit_mode: str, bg: tuple) -> Image.Image:
    """Loads an image, corrects EXIF rotation and fits it to the screen.

    Uses PIL's draft() mode: JPEGs are decoded directly at a reduced
    resolution by the decoder (libjpeg DCT scaling) instead of being loaded
    at full resolution and downscaled afterwards. For modern phone photos
    (12-48 MP) this saves considerable CPU time on a Pi Zero 2 W and avoids
    a visible stutter on image change.
    """
    img = Image.open(path)
    # draft() only has an effect for JPEG (a no-op for PNG/GIF/WebP/BMP) and
    # must be called before any other operation. Choose the target a bit
    # more generously than the screen (factor 1.3) to leave some margin for
    # "cover" crops. Note: Ken Burns runs via load_for_ken_burns() below,
    # NOT via this function - see there for the reason.
    try:
        img.draft('RGB', (int(w * 1.3), int(h * 1.3)))
    except Exception:
        pass
    img = ImageOps.exif_transpose(img)
    img = img.convert('RGB')

    img_w, img_h = img.size
    screen_aspect = w / h
    img_aspect    = img_w / img_h

    if fit_mode == 'cover':
        if img_aspect > screen_aspect:
            new_h = h
            new_w = int(new_h * img_aspect)
        else:
            new_w = w
            new_h = int(new_w / img_aspect)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        cx  = (new_w - w) // 2
        cy  = (new_h - h) // 2
        img = img.crop((cx, cy, cx + w, cy + h))
    else:  # contain
        img.thumbnail((w, h), Image.LANCZOS)
        canvas = Image.new('RGB', (w, h), bg)
        ox = (w - img.width)  // 2
        oy = (h - img.height) // 2
        canvas.paste(img, (ox, oy))
        img = canvas

    return img


def load_for_ken_burns(path: Path, canvas_w: int, canvas_h: int) -> Image.Image:
    """Loads an image for Ken Burns WITHOUT first cropping it to screen
    size - unlike load_and_fit(), this preserves the native aspect ratio
    (and native resolution, as far as usefully provided by the JPEG
    decoder).

    Reason: display_ken_burns() needs a somewhat larger image than the
    screen anyway (zoom margin for panning). If we first cropped exactly to
    screen size as with the other transitions, and THEN upscaled again for
    the zoom margin, we'd get two lossy steps instead of one - the image
    would end up unnecessarily softer, especially for photos that are
    already close to the screen resolution. Instead, this scales once
    directly from (as close as possible to) native resolution to the zoom
    canvas actually needed (see display_ken_burns, which uses LANCZOS
    there instead of BILINEAR).
    """
    img = Image.open(path)
    try:
        img.draft('RGB', (canvas_w, canvas_h))
    except Exception:
        pass
    img = ImageOps.exif_transpose(img)
    return img.convert('RGB')


def make_placeholder(w: int, h: int) -> pygame.Surface:
    """Simple placeholder image when no cache is present."""
    surf = pygame.Surface((w, h))
    surf.fill((20, 20, 20))
    font = pygame.font.SysFont('sans', 36)
    lines = ['Photoframe', '', 'No images in cache.', 'Please configure sync:', 'http://photoframe.local:8080']
    y = h // 2 - len(lines) * 25
    for line in lines:
        text = font.render(line, True, (180, 180, 180))
        rect = text.get_rect(center=(w // 2, y))
        surf.blit(text, rect)
        y += 50
    return surf

# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def _tick(delay_s: float):
    """Sleep with a pygame event pump so the window doesn't freeze."""
    pygame.event.pump()
    time.sleep(delay_s)


def transition_none(screen, old_surf, new_surf, **_):
    screen.blit(new_surf, (0, 0))
    pygame.display.flip()


def transition_fade(screen, old_surf, new_surf, duration_ms=1500, **_):
    steps = 18
    delay = duration_ms / 1000 / steps
    overlay = new_surf.copy()
    for i in range(steps + 1):
        alpha = int(255 * i / steps)
        overlay.set_alpha(alpha)
        screen.blit(old_surf, (0, 0))
        screen.blit(overlay, (0, 0))
        pygame.display.flip()
        _tick(delay)


def _slide(screen, old_surf, new_surf, duration_ms, dx, dy):
    """Generic slide transition with a direction vector."""
    w, h  = screen.get_size()
    steps = 20
    delay = duration_ms / 1000 / steps
    for i in range(steps + 1):
        t = i / steps
        ox = int(dx * w * t)
        oy = int(dy * h * t)
        screen.blit(old_surf, (-ox, -oy))
        screen.blit(new_surf, (w * dx - ox if dx else -ox,
                                h * dy - oy if dy else -oy))
        pygame.display.flip()
        _tick(delay)


def transition_slide_left (screen, old_surf, new_surf, duration_ms=1500, **_):
    _slide(screen, old_surf, new_surf, duration_ms, dx=1,  dy=0)

def transition_slide_right(screen, old_surf, new_surf, duration_ms=1500, **_):
    _slide(screen, old_surf, new_surf, duration_ms, dx=-1, dy=0)

def transition_slide_up   (screen, old_surf, new_surf, duration_ms=1500, **_):
    _slide(screen, old_surf, new_surf, duration_ms, dx=0,  dy=1)

def transition_slide_down (screen, old_surf, new_surf, duration_ms=1500, **_):
    _slide(screen, old_surf, new_surf, duration_ms, dx=0,  dy=-1)


def transition_wipe_left(screen, old_surf, new_surf, duration_ms=1500, **_):
    w, h  = screen.get_size()
    steps = 20
    delay = duration_ms / 1000 / steps
    for i in range(steps + 1):
        x = int(w * i / steps)
        screen.blit(old_surf, (0, 0))
        screen.blit(new_surf, (0, 0), pygame.Rect(0, 0, x, h))
        pygame.display.flip()
        _tick(delay)


def transition_wipe_right(screen, old_surf, new_surf, duration_ms=1500, **_):
    w, h  = screen.get_size()
    steps = 20
    delay = duration_ms / 1000 / steps
    for i in range(steps + 1):
        x = int(w * i / steps)
        screen.blit(old_surf, (0, 0))
        screen.blit(new_surf, (w - x, 0), pygame.Rect(w - x, 0, x, h))
        pygame.display.flip()
        _tick(delay)


def transition_wipe_up(screen, old_surf, new_surf, duration_ms=1500, **_):
    w, h  = screen.get_size()
    steps = 20
    delay = duration_ms / 1000 / steps
    for i in range(steps + 1):
        y = int(h * i / steps)
        screen.blit(old_surf, (0, 0))
        screen.blit(new_surf, (0, h - y), pygame.Rect(0, h - y, w, y))
        pygame.display.flip()
        _tick(delay)


def transition_wipe_down(screen, old_surf, new_surf, duration_ms=1500, **_):
    w, h  = screen.get_size()
    steps = 20
    delay = duration_ms / 1000 / steps
    for i in range(steps + 1):
        y = int(h * i / steps)
        screen.blit(old_surf, (0, 0))
        screen.blit(new_surf, (0, 0), pygame.Rect(0, 0, w, y))
        pygame.display.flip()
        _tick(delay)


def transition_iris(screen, old_surf, new_surf, duration_ms=1500, **_):
    """Circular iris: the new image is revealed outward from the center."""
    w, h   = screen.get_size()
    cx, cy = w // 2, h // 2
    max_r  = int(math.hypot(w, h) / 2) + 2
    steps  = 20
    delay  = duration_ms / 1000 / steps
    for i in range(steps + 1):
        t = i / steps
        r = int(max_r * t)
        mask = pygame.Surface((w, h), pygame.SRCALPHA)
        mask.fill((0, 0, 0, 0))
        pygame.draw.circle(mask, (255, 255, 255, 255), (cx, cy), r)
        reveal = new_surf.copy()
        reveal.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        screen.blit(old_surf, (0, 0))
        screen.blit(reveal, (0, 0))
        pygame.display.flip()
        _tick(delay)


def transition_barn_door(screen, old_surf, new_surf, duration_ms=1500, **_):
    """Curtain effect: the old image splits down the middle and slides
    apart to the left/right, revealing the new image behind it."""
    w, h  = screen.get_size()
    steps = 20
    delay = duration_ms / 1000 / steps
    half  = w // 2
    for i in range(steps + 1):
        t  = i / steps
        dx = int(half * t)
        screen.blit(new_surf, (0, 0))
        left  = old_surf.subsurface(pygame.Rect(0, 0, half, h))
        right = old_surf.subsurface(pygame.Rect(half, 0, w - half, h))
        screen.blit(left,  (-dx, 0))
        screen.blit(right, (half + dx, 0))
        pygame.display.flip()
        _tick(delay)


def transition_blinds(screen, old_surf, new_surf, duration_ms=1500, **_):
    """Venetian blinds: the image is broken up into vertical slats that
    open together toward one side, revealing the new image."""
    w, h        = screen.get_size()
    n_slats     = 12
    slat_w      = w // n_slats
    steps       = 20
    delay       = duration_ms / 1000 / steps
    for i in range(steps + 1):
        t = i / steps
        screen.blit(new_surf, (0, 0))
        for s in range(n_slats):
            x0 = s * slat_w
            sw = slat_w if s < n_slats - 1 else w - x0
            cut = int(h * t)          # portion "opened" that grows from the top
            if cut < h:
                rect = pygame.Rect(x0, cut, sw, h - cut)
                screen.blit(old_surf, (x0, cut), rect)
        pygame.display.flip()
        _tick(delay)


def transition_flash(screen, old_surf, new_surf, duration_ms=1500, **_):
    """Brief white flash between images (camera flash effect)."""
    w, h  = screen.get_size()
    white = pygame.Surface((w, h))
    white.fill((255, 255, 255))
    steps = max(6, int(duration_ms / 1000 * 12))
    half  = steps // 2
    delay = duration_ms / 1000 / steps
    # Old -> White
    for i in range(half + 1):
        alpha = int(255 * i / half)
        overlay = white.copy()
        overlay.set_alpha(alpha)
        screen.blit(old_surf, (0, 0))
        screen.blit(overlay, (0, 0))
        pygame.display.flip()
        _tick(delay)
    # White -> New
    for i in range(half + 1):
        alpha = int(255 * (1 - i / half))
        overlay = white.copy()
        overlay.set_alpha(alpha)
        screen.blit(new_surf, (0, 0))
        screen.blit(overlay, (0, 0))
        pygame.display.flip()
        _tick(delay)


def transition_zoom_in(screen, old_surf, new_surf, duration_ms=1500, **_):
    """Old image zooms out (shrinks), new one appears."""
    w, h  = screen.get_size()
    steps = 15
    delay = duration_ms / 1000 / steps
    bg    = pygame.Surface((w, h))
    bg.fill((0, 0, 0))
    for i in range(steps + 1):
        t     = i / steps
        scale = 1.0 - t * 0.15         # 100% -> 85%
        nw    = int(w * scale)
        nh    = int(h * scale)
        shrunk = pygame.transform.smoothscale(old_surf, (nw, nh))
        alpha  = int(255 * (1 - t))
        shrunk.set_alpha(alpha)
        screen.blit(bg, (0, 0))
        screen.blit(shrunk, ((w - nw) // 2, (h - nh) // 2))
        # New image fades in at the same time
        overlay = new_surf.copy()
        overlay.set_alpha(int(255 * t))
        screen.blit(overlay, (0, 0))
        pygame.display.flip()
        _tick(delay)


def transition_dissolve(screen, old_surf, new_surf, duration_ms=1500, **_):
    """Block dissolve: 16x16 pixel blocks are revealed in random order."""
    w, h       = screen.get_size()
    block      = 24
    cols       = (w + block - 1) // block
    rows       = (h + block - 1) // block
    blocks     = [(c, r) for c in range(cols) for r in range(rows)]
    random.shuffle(blocks)
    steps      = 12                    # Reveal all blocks over 12 batches
    batch_size = max(1, len(blocks) // steps)
    delay      = duration_ms / 1000 / steps

    screen.blit(old_surf, (0, 0))
    pygame.display.flip()

    for step in range(steps):
        batch = blocks[step * batch_size:(step + 1) * batch_size]
        for (c, r) in batch:
            x, y = c * block, r * block
            screen.blit(new_surf, (x, y), pygame.Rect(x, y, block, block))
        pygame.display.flip()
        _tick(delay)

    screen.blit(new_surf, (0, 0))
    pygame.display.flip()


TRANSITIONS = {
    'none':        transition_none,
    'fade':        transition_fade,
    'slide_left':  transition_slide_left,
    'slide_right': transition_slide_right,
    'slide_up':    transition_slide_up,
    'slide_down':  transition_slide_down,
    'wipe_left':   transition_wipe_left,
    'wipe_right':  transition_wipe_right,
    'wipe_up':     transition_wipe_up,
    'wipe_down':   transition_wipe_down,
    'zoom_in':     transition_zoom_in,
    'dissolve':    transition_dissolve,
    'iris':        transition_iris,
    'barn_door':   transition_barn_door,
    'blinds':      transition_blinds,
    'flash':       transition_flash,
}

# 'ken_burns' is not an entry in TRANSITIONS (it has its own display loop
# rather than a blit transition), but still counts as a valid, selectable
# option.
ALL_TRANSITION_NAMES = set(TRANSITIONS.keys()) | {'ken_burns'}


def get_enabled_transitions(sl: dict) -> list:
    """Reads the list of transitions enabled in the web UI from the config.

    Backwards compatible with old configs that only know a single
    `transition` field (the earlier radio-button selection): if
    `enabled_transitions` is missing, a single-item list is built from it.
    Unknown/deprecated names are silently dropped; if nothing is left at
    the end, falls back to ken_burns, so the slideshow is never left
    without any transition at all.
    """
    raw = sl.get('enabled_transitions')
    if not raw:
        raw = [sl.get('transition', 'ken_burns')]
    enabled = [t for t in raw if t in ALL_TRANSITION_NAMES]
    return enabled or ['ken_burns']


def pick_transition(enabled: list) -> str:
    """Picks the transition for the next image from the pool of enabled
    transitions.

    With exactly one enabled transition, the same one is used
    deterministically every time (as before this function was introduced).
    With several, one is picked at random for each image change, so the
    transitions alternate.
    """
    if not enabled:
        return 'ken_burns'
    if len(enabled) == 1:
        return enabled[0]
    return random.choice(enabled)

# ---------------------------------------------------------------------------
# Ken Burns (its own display loop, not a classic transition)
# ---------------------------------------------------------------------------

def display_ken_burns(screen, img_pil: Image.Image, duration_s: int,
                      zoom: float, old_surf: pygame.Surface):
    """
    Displays the image with a slow pan+zoom effect.
    Briefly fades in at the start (fade-in from old_surf).
    """
    w, h = screen.get_size()

    # Scale the image a bit larger than the screen (to leave pan margin)
    extra        = 1.0 + max(0.04, zoom)
    canvas_w     = int(w * extra)
    canvas_h     = int(h * extra)

    img_w, img_h = img_pil.size
    img_aspect   = img_w / img_h
    screen_aspect = w / h

    if img_aspect > screen_aspect:
        fit_h = canvas_h
        fit_w = int(fit_h * img_aspect)
    else:
        fit_w = canvas_w
        fit_h = int(fit_w / img_aspect)

    # LANCZOS instead of BILINEAR: img_pil now arrives (via
    # load_for_ken_burns) at native resolution rather than already
    # pre-cropped to screen size, so the somewhat more expensive but
    # noticeably sharper scaling is worthwhile here - it happens only once
    # per image, not per frame.
    img_scaled = img_pil.resize((fit_w, fit_h), Image.LANCZOS)

    max_ox = max(0, fit_w - w)
    max_oy = max(0, fit_h - h)

    sx = random.randint(0, max_ox)
    sy = random.randint(0, max_oy)
    ex = random.randint(0, max_ox)
    ey = random.randint(0, max_oy)

    # Brief fade-in (0.8s)
    fade_steps = 10
    fade_delay = 0.8 / fade_steps
    overlay    = None
    for i in range(fade_steps + 1):
        t     = i / fade_steps
        cx    = sx + int((ex - sx) * (t * 0.05))
        cy    = sy + int((ey - sy) * (t * 0.05))
        crop  = img_scaled.crop((cx, cy, cx + w, cy + h))
        surf  = pil_to_surface(crop)
        surf.set_alpha(int(255 * t))
        screen.blit(old_surf, (0, 0))
        screen.blit(surf, (0, 0))
        pygame.display.flip()
        _tick(fade_delay)

    # Main pan animation (low FPS is entirely sufficient)
    fps          = 3
    total_frames = max(1, int(duration_s * fps))
    frame_delay  = 1.0 / fps

    for frame in range(total_frames):
        pygame.event.pump()
        t   = frame / total_frames
        cx  = sx + int((ex - sx) * t)
        cy  = sy + int((ey - sy) * t)
        crop = img_scaled.crop((cx, cy, cx + w, cy + h))
        surf = pil_to_surface(crop)
        screen.blit(surf, (0, 0))
        pygame.display.flip()
        _tick(frame_delay)

    return pil_to_surface(img_scaled.crop((ex, ey, ex + w, ey + h)))


# ---------------------------------------------------------------------------
# Video playback via mpv
# ---------------------------------------------------------------------------

def play_video(path: Path, audio: bool) -> pygame.Surface:
    """
    Plays a video with mpv (directly on KMS/DRM).
    pygame releases the DRM master, mpv takes over, and afterwards pygame
    is reinitialized and a fresh screen is returned.
    """
    log.info(f'Video: {path.name}  [audio={audio}]')

    # Release the pygame display so mpv can take over the DRM master
    pygame.display.quit()

    cmd = [
        'mpv',
        '--vo=drm',
        '--hwdec=v4l2m2m',    # Hardware decode (H.264) on RPi
        '--fullscreen',
        '--really-quiet',
        '--no-terminal',
        f'--ao={"alsa" if audio else "null"}',
        str(path),
    ]

    try:
        subprocess.run(cmd, timeout=3600)   # max 1h safety timeout
    except subprocess.TimeoutExpired:
        log.warning(f'Video timeout exceeded: {path.name}')
    except FileNotFoundError:
        log.error('mpv not found - sudo apt install mpv')

    # Reinitialize the pygame display
    pygame.display.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.NOFRAME)
    pygame.mouse.set_visible(False)
    return screen


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def get_media_list(video_enabled: bool) -> list:
    files = []
    try:
        entries = list(CACHE_DIR.iterdir())
    except FileNotFoundError:
        # Cache directory doesn't exist yet (e.g. freshly installed, not a
        # single sync has run yet) - not an error, just treat it as "no
        # images yet" instead of crashing the slideshow (which would
        # otherwise happen again right away on every restart).
        return files
    for p in entries:
        ext = p.suffix.lower()
        if ext in SUPPORTED_IMAGES:
            files.append(p)
        elif video_enabled and ext in SUPPORTED_VIDEOS:
            files.append(p)
    return files


def shuffled_avoiding_recent(images: list, recent: list) -> list:
    """Shuffles `images` randomly - but avoids letting the most recently
    shown images land right back at the front of the new pass.

    Background: random.shuffle() on the whole list is genuinely uniformly
    random, but two independent random passes in a row can, purely by
    chance, place exactly the most recently shown images back at the start
    of the next pass. To viewers this feels like "it keeps showing the same
    images", even though the randomness itself is correct - this is
    especially noticeable with smaller libraries. This function therefore
    swaps, as far as the library size allows, starting slots that collide
    with recently shown images for images further back in the new shuffle.
    """
    pool = images[:]
    random.shuffle(pool)
    if not recent or len(pool) < 3:
        return pool

    # Treat at most half the library as "recently shown" - otherwise
    # nothing would be left to swap with for small libraries.
    cap = max(1, min(len(recent), len(pool) // 2))
    avoid = set(recent[-cap:])
    if len(avoid) >= len(pool):
        return pool  # practically the whole library is "recently shown"

    i = 0
    while i < cap and pool[i] in avoid:
        for j in range(cap, len(pool)):
            if pool[j] not in avoid:
                pool[i], pool[j] = pool[j], pool[i]
                break
        i += 1
    return pool


# A single background worker is enough: it pre-decodes the next image in
# the meantime, so that load_and_fit() (decode + LANCZOS resize of a
# potentially high-resolution photo) no longer blocks at the actual image
# change. On the Pi Zero 2 W this is the difference between a noticeable
# stutter on every image change and a seamless transition.
_prefetch_executor = ThreadPoolExecutor(max_workers=1)


def _submit_prefetch(path: Path, w: int, h: int, fit: str, bg: tuple,
                      t_name: str, canvas_w: int, canvas_h: int):
    if t_name == 'ken_burns':
        return _prefetch_executor.submit(load_for_ken_burns, path, canvas_w, canvas_h)
    return _prefetch_executor.submit(load_and_fit, path, w, h, fit, bg)


def _recover_display(w: int, h: int):
    """Rebuilds the pygame display after an error, as a safety measure.

    A pygame/SDL error (e.g. an aborted video playback, a broken
    transition) can leave the display surface in an undefined state.
    Instead of crashing the process over it - which would cause KMSDRM to
    release ownership of the display and the text console to flash on
    screen until systemd restarts the service - this attempts to
    reinitialize the display within the running process, so the screen
    never falls back to the console at all.
    """
    try:
        pygame.display.quit()
    except Exception:
        pass
    try:
        pygame.display.init()
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.NOFRAME)
        pygame.mouse.set_visible(False)
    except Exception:
        log.error('Display could not be reinitialized after error')
        screen = pygame.display.set_mode((w, h))
    current_surf = pygame.Surface(screen.get_size())
    current_surf.fill((0, 0, 0))
    screen.blit(current_surf, (0, 0))
    try:
        pygame.display.flip()
    except Exception:
        pass
    return screen, current_surf


def run():
    global _config, _reload_flag

    _config = load_config()
    log.info('Starting slideshow')
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize pygame
    try:
        pygame.init()
        pygame.font.init()
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.NOFRAME)
        pygame.mouse.set_visible(False)
        log.info(f'Display: {screen.get_width()}x{screen.get_height()} via {os.environ.get("SDL_VIDEODRIVER")}')
    except Exception as e:
        log.error(f'pygame init failed: {e}')
        log.error('Tip: try SDL_VIDEODRIVER=fbcon in the systemd unit')
        sys.exit(1)

    W, H = screen.get_size()
    current_surf = pygame.Surface((W, H))
    current_surf.fill((0, 0, 0))
    screen.blit(current_surf, (0, 0))
    pygame.display.flip()

    # {'future': Future|None, 'key': (path, w, h, fit, bg)|None}
    prefetch = {'future': None, 'key': None}
    # The transition for the next image is already chosen during prefetch
    # (see pick_transition) and cached here, so that loading and display
    # use the same transition.
    current_t_name = None
    # History of recently shown images for shuffled_avoiding_recent() -
    # persists across individual passes, so the transition between two
    # passes doesn't immediately show the same images again.
    recent_shown = []

    while True:
        # The entire loop body is deliberately wrapped in a try/except: it
        # is the last line of defense. All known error sources (loading,
        # video, display) are already individually safeguarded (see
        # above), but an additional, unforeseen error caught here still
        # prevents the whole process from crashing - and with it, the text
        # console briefly flashing on screen until systemd (Restart=always)
        # restarts it.
        try:
            if _reload_flag:
                _config = load_config()
                _reload_flag = False
                current_t_name = None
                log.info('Config reloaded')

            sl      = _config.get('slideshow', {})
            enabled_transitions = get_enabled_transitions(sl)
            t_dur   = sl.get('transition_duration_ms', 1500)
            interval = sl.get('interval_seconds', 30)
            fit     = sl.get('fit_mode', 'contain')
            bg      = hex_to_rgb(sl.get('background_color', '#000000'))
            kb_zoom = sl.get('ken_burns_zoom', 0.08)
            kb_extra = 1.0 + max(0.04, kb_zoom)
            canvas_w = int(W * kb_extra)
            canvas_h = int(H * kb_extra)
            shuffle = sl.get('shuffle', True)
            video_enabled = sl.get('video_enabled', True)
            video_audio   = sl.get('video_audio', False)

            images = get_media_list(video_enabled)

            if not images:
                log.info('No cache - showing placeholder')
                screen.blit(make_placeholder(W, H), (0, 0))
                pygame.display.flip()
                time.sleep(15)
                continue

            if shuffle:
                images = shuffled_avoiding_recent(images, recent_shown)
            else:
                images.sort(key=lambda p: p.name)

            for idx, media_path in enumerate(images):
                # Account for a config reload between files
                if _reload_flag:
                    _config = load_config()
                    _reload_flag = False
                    current_t_name = None
                    sl       = _config.get('slideshow', {})
                    enabled_transitions = get_enabled_transitions(sl)
                    t_dur    = sl.get('transition_duration_ms', 1500)
                    interval = sl.get('interval_seconds', 30)
                    fit      = sl.get('fit_mode', 'contain')
                    bg       = hex_to_rgb(sl.get('background_color', '#000000'))
                    kb_zoom  = sl.get('ken_burns_zoom', 0.08)
                    kb_extra = 1.0 + max(0.04, kb_zoom)
                    canvas_w = int(W * kb_extra)
                    canvas_h = int(H * kb_extra)
                    shuffle  = sl.get('shuffle', True)
                    video_enabled = sl.get('video_enabled', True)
                    video_audio   = sl.get('video_audio', False)

                # Process pygame events
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        pygame.quit()
                        sys.exit(0)
                    if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                        pygame.quit()
                        sys.exit(0)

                # --- Video ---
                if media_path.suffix.lower() in SUPPORTED_VIDEOS:
                    try:
                        screen = play_video(media_path, video_audio)
                        W, H = screen.get_size()
                        current_surf = pygame.Surface((W, H))
                        current_surf.fill((0, 0, 0))
                    except Exception as e:
                        log.exception(f'Video playback failed ({media_path.name}): {e}')
                        screen, current_surf = _recover_display(W, H)
                        W, H = screen.get_size()
                    continue

                # --- Image ---
                # Was this image already prefetched in the background
                # (while the previous image was being displayed)? Then it's
                # available here with virtually no wait time. Otherwise
                # (first image, config changed, video before it) load it
                # synchronously as before.
                # Transition for THIS image: either already determined
                # during prefetch in the last pass (current_t_name), or -
                # if none has been determined yet (very first image, config
                # just reloaded) - chosen now from the pool of enabled
                # transitions. Must be determined BEFORE loading, because
                # load_for_ken_burns() and load_and_fit() deliver different
                # image formats.
                if current_t_name is None:
                    current_t_name = pick_transition(enabled_transitions)
                t_name = current_t_name

                key = (media_path, W, H, fit, bg, t_name, canvas_w, canvas_h)
                if prefetch['key'] == key and prefetch['future'] is not None:
                    try:
                        img_pil = prefetch['future'].result()
                    except Exception as e:
                        log.warning(f'Image not loadable {media_path.name}: {e}')
                        prefetch['future'] = prefetch['key'] = None
                        current_t_name = None
                        continue
                else:
                    try:
                        if t_name == 'ken_burns':
                            img_pil = load_for_ken_burns(media_path, canvas_w, canvas_h)
                        else:
                            img_pil = load_and_fit(media_path, W, H, fit, bg)
                    except Exception as e:
                        log.warning(f'Image not loadable {media_path.name}: {e}')
                        current_t_name = None
                        continue
                prefetch['future'] = prefetch['key'] = None

                # Determine the transition for the NEXT image already now,
                # so that background prefetching uses the load mode
                # matching (Ken Burns vs. normal fit) exactly the
                # transition that will actually be used at the next image
                # change.
                next_path   = images[idx + 1] if idx + 1 < len(images) else None
                next_t_name = pick_transition(enabled_transitions)
                if next_path is not None and next_path.suffix.lower() in SUPPORTED_IMAGES:
                    prefetch['key']    = (next_path, W, H, fit, bg, next_t_name, canvas_w, canvas_h)
                    prefetch['future'] = _submit_prefetch(next_path, W, H, fit, bg, next_t_name, canvas_w, canvas_h)
                current_t_name = next_t_name

                if shuffle:
                    recent_shown.append(media_path)
                    cap = max(1, min(len(images) // 2, 30))
                    if len(recent_shown) > cap:
                        recent_shown = recent_shown[-cap:]

                log.info(f'Showing: {media_path.name}  [{t_name}]')

                # The actual display (Ken Burns or a blit transition) is
                # deliberately safeguarded separately: a single faulty
                # frame (e.g. a pygame/SDL error in the middle of a
                # transition) should never end the whole process -
                # otherwise the text console would briefly flash on screen
                # until systemd restarts the slideshow. Instead, only this
                # one image is skipped and the display rebuilt if needed.
                try:
                    if t_name == 'ken_burns':
                        current_surf = display_ken_burns(
                            screen, img_pil, interval, kb_zoom, current_surf
                        )
                    else:
                        new_surf = pil_to_surface(img_pil)
                        fn       = TRANSITIONS.get(t_name, transition_fade)
                        fn(screen, current_surf, new_surf, duration_ms=t_dur)
                        current_surf = new_surf
                        elapsed = 0
                        while elapsed < interval:
                            pygame.event.pump()
                            time.sleep(0.5)
                            elapsed += 0.5
                            if _reload_flag:
                                break
                except Exception as e:
                    log.exception(f'Display error for {media_path.name} (transition {t_name}): {e}')
                    screen, current_surf = _recover_display(W, H)
                    W, H = screen.get_size()

        except Exception as e:
            # Everything else not already specifically caught above (e.g.
            # an error evaluating the config or shuffling the image list).
            # Brief pause, then the main loop continues normally instead of
            # ending the process.
            log.exception(f'Unexpected error in the main loop, slideshow continues running: {e}')
            time.sleep(2)


if __name__ == '__main__':
    run()
