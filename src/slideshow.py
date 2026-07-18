#!/usr/bin/env python3
"""
Photoframe Slideshow
Zeigt Bilder aus dem Cache auf dem Framebuffer via pygame.
Unterstützte Transitions: none, fade, slide_left, slide_right,
slide_up, slide_down, wipe_left, ken_burns, zoom_in, dissolve
"""

import os
import sys
import time
import random
import signal
import logging
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import yaml
from PIL import Image, ImageOps, ImageFilter

# pygame muss vor dem Import konfiguriert werden
os.environ.setdefault('SDL_VIDEODRIVER', 'kmsdrm')
os.environ.setdefault('SDL_VIDEO_KMSDRM_DEVICE', '/dev/dri/card0')
os.environ['SDL_AUDIODRIVER'] = 'dummy'
os.environ['DISPLAY'] = ''

import pygame

# ---------------------------------------------------------------------------
# Konstanten
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
        # Defaults auffüllen
        for k, v in DEFAULT_CONFIG['slideshow'].items():
            cfg.setdefault('slideshow', {}).setdefault(k, v)
        return cfg
    except Exception as e:
        log.warning(f'Config nicht lesbar ({e}), nutze Defaults')
        return DEFAULT_CONFIG.copy()


def _sighup_handler(signum, frame):
    global _reload_flag
    _reload_flag = True
    log.info('SIGHUP empfangen – Config wird beim nächsten Bildwechsel neu geladen')


signal.signal(signal.SIGHUP, _sighup_handler)

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def hex_to_rgb(hex_color: str) -> tuple:
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def pil_to_surface(pil_img: Image.Image) -> pygame.Surface:
    """PIL Image → pygame Surface (RGB)."""
    if pil_img.mode != 'RGB':
        pil_img = pil_img.convert('RGB')
    return pygame.image.frombuffer(pil_img.tobytes(), pil_img.size, 'RGB').copy()


def load_and_fit(path: Path, w: int, h: int, fit_mode: str, bg: tuple) -> Image.Image:
    """Lädt Bild, korrigiert EXIF-Rotation und passt es an den Bildschirm an.

    Nutzt PIL's draft()-Modus: JPEGs werden vom Decoder direkt in einer
    reduzierten Auflösung dekodiert (libjpeg DCT-Scaling), statt komplett in
    voller Auflösung geladen und danach herunterskaliert zu werden. Bei
    modernen Handyfotos (12–48 MP) spart das auf einem Pi Zero 2 W erhebliche
    CPU-Zeit und vermeidet ein sichtbares Stocken beim Bildwechsel.
    """
    img = Image.open(path)
    # draft() ist nur für JPEG wirksam (no-op für PNG/GIF/WebP/BMP) und muss
    # vor jeder anderen Operation aufgerufen werden. Ziel etwas großzügiger
    # als der Screen wählen (Faktor 1.3), damit Ken-Burns-Pan/Zoom und
    # "cover"-Crops noch genug Bildmaterial haben.
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


def make_placeholder(w: int, h: int) -> pygame.Surface:
    """Einfaches Platzhalter-Bild wenn kein Cache vorhanden."""
    surf = pygame.Surface((w, h))
    surf.fill((20, 20, 20))
    font = pygame.font.SysFont('sans', 36)
    lines = ['Photoframe', '', 'Keine Bilder im Cache.', 'Bitte Sync konfigurieren:', 'http://photoframe.local:8080']
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
    """Sleep mit pygame-Event-Pump damit das Fenster nicht einfriert."""
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
    """Generische Slide-Transition mit Richtungsvektor."""
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


def transition_zoom_in(screen, old_surf, new_surf, duration_ms=1500, **_):
    """Altes Bild zoomt heraus (wird kleiner), neues erscheint."""
    w, h  = screen.get_size()
    steps = 15
    delay = duration_ms / 1000 / steps
    bg    = pygame.Surface((w, h))
    bg.fill((0, 0, 0))
    for i in range(steps + 1):
        t     = i / steps
        scale = 1.0 - t * 0.15         # 100% → 85%
        nw    = int(w * scale)
        nh    = int(h * scale)
        shrunk = pygame.transform.smoothscale(old_surf, (nw, nh))
        alpha  = int(255 * (1 - t))
        shrunk.set_alpha(alpha)
        screen.blit(bg, (0, 0))
        screen.blit(shrunk, ((w - nw) // 2, (h - nh) // 2))
        # Neues Bild blendet gleichzeitig ein
        overlay = new_surf.copy()
        overlay.set_alpha(int(255 * t))
        screen.blit(overlay, (0, 0))
        pygame.display.flip()
        _tick(delay)


def transition_dissolve(screen, old_surf, new_surf, duration_ms=1500, **_):
    """Block-Dissolve: 16x16 Pixel-Blöcke werden zufällig enthüllt."""
    w, h       = screen.get_size()
    block      = 24
    cols       = (w + block - 1) // block
    rows       = (h + block - 1) // block
    blocks     = [(c, r) for c in range(cols) for r in range(rows)]
    random.shuffle(blocks)
    steps      = 12                    # Enthülle alle Blöcke in 12 Batches
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
    'zoom_in':     transition_zoom_in,
    'dissolve':    transition_dissolve,
}

# ---------------------------------------------------------------------------
# Ken Burns (eigene Anzeige-Schleife, kein klassischer Übergang)
# ---------------------------------------------------------------------------

def display_ken_burns(screen, img_pil: Image.Image, duration_s: int,
                      zoom: float, old_surf: pygame.Surface):
    """
    Zeigt das Bild mit langsamem Pan+Zoom-Effekt.
    Blendet am Anfang kurz ein (Fade-in aus old_surf).
    """
    w, h = screen.get_size()

    # Bild etwas größer skalieren als der Bildschirm (für Pan-Spielraum)
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

    # Für Ken Burns reicht BILINEAR (schneller als LANCZOS)
    img_scaled = img_pil.resize((fit_w, fit_h), Image.BILINEAR)

    max_ox = max(0, fit_w - w)
    max_oy = max(0, fit_h - h)

    sx = random.randint(0, max_ox)
    sy = random.randint(0, max_oy)
    ex = random.randint(0, max_ox)
    ey = random.randint(0, max_oy)

    # Kurzer Fade-in (0.8s)
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

    # Haupt-Pan-Animation (niedrige FPS genügt vollkommen)
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
# Video-Wiedergabe via mpv
# ---------------------------------------------------------------------------

def play_video(path: Path, audio: bool) -> pygame.Surface:
    """
    Spielt Video mit mpv ab (direkt auf KMS/DRM).
    pygame gibt den DRM-Master frei, mpv übernimmt, danach wird
    pygame neu initialisiert und ein frischer Screen zurückgegeben.
    """
    log.info(f'Video: {path.name}  [audio={audio}]')

    # pygame Display freigeben damit mpv DRM-Master übernehmen kann
    pygame.display.quit()

    cmd = [
        'mpv',
        '--vo=drm',
        '--hwdec=v4l2m2m',    # Hardware-Decode (H.264) auf RPi
        '--fullscreen',
        '--really-quiet',
        '--no-terminal',
        f'--ao={"alsa" if audio else "null"}',
        str(path),
    ]

    try:
        subprocess.run(cmd, timeout=3600)   # max 1h Sicherheits-Timeout
    except subprocess.TimeoutExpired:
        log.warning(f'Video-Timeout überschritten: {path.name}')
    except FileNotFoundError:
        log.error('mpv nicht gefunden – sudo apt install mpv')

    # pygame Display neu initialisieren
    pygame.display.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.NOFRAME)
    pygame.mouse.set_visible(False)
    return screen


# ---------------------------------------------------------------------------
# Haupt-Schleife
# ---------------------------------------------------------------------------

def get_media_list(video_enabled: bool) -> list:
    files = []
    for p in CACHE_DIR.iterdir():
        ext = p.suffix.lower()
        if ext in SUPPORTED_IMAGES:
            files.append(p)
        elif video_enabled and ext in SUPPORTED_VIDEOS:
            files.append(p)
    return files


# Ein einzelner Hintergrund-Worker reicht: er dekodiert währenddessen das
# nächste Bild vor, sodass load_and_fit() (Decode + LANCZOS-Resize eines
# potenziell hochauflösenden Fotos) beim eigentlichen Bildwechsel nicht mehr
# blockiert. Auf dem Pi Zero 2 W ist das der Unterschied zwischen einem
# spürbaren Ruckler bei jedem Bildwechsel und einem nahtlosen Übergang.
_prefetch_executor = ThreadPoolExecutor(max_workers=1)


def _submit_prefetch(path: Path, w: int, h: int, fit: str, bg: tuple):
    return _prefetch_executor.submit(load_and_fit, path, w, h, fit, bg)


def run():
    global _config, _reload_flag

    _config = load_config()
    log.info('Starte Slideshow')

    # pygame initialisieren
    try:
        pygame.init()
        pygame.font.init()
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.NOFRAME)
        pygame.mouse.set_visible(False)
        log.info(f'Display: {screen.get_width()}x{screen.get_height()} via {os.environ.get("SDL_VIDEODRIVER")}')
    except Exception as e:
        log.error(f'pygame-Init fehlgeschlagen: {e}')
        log.error('Tipp: SDL_VIDEODRIVER=fbcon in der systemd-Unit versuchen')
        sys.exit(1)

    W, H = screen.get_size()
    current_surf = pygame.Surface((W, H))
    current_surf.fill((0, 0, 0))
    screen.blit(current_surf, (0, 0))
    pygame.display.flip()

    # {'future': Future|None, 'key': (path, w, h, fit, bg)|None}
    prefetch = {'future': None, 'key': None}

    while True:
        if _reload_flag:
            _config = load_config()
            _reload_flag = False
            log.info('Config neu geladen')

        sl      = _config.get('slideshow', {})
        t_name  = sl.get('transition', 'ken_burns')
        t_dur   = sl.get('transition_duration_ms', 1500)
        interval = sl.get('interval_seconds', 30)
        fit     = sl.get('fit_mode', 'contain')
        bg      = hex_to_rgb(sl.get('background_color', '#000000'))
        kb_zoom = sl.get('ken_burns_zoom', 0.08)
        shuffle = sl.get('shuffle', True)
        video_enabled = sl.get('video_enabled', True)
        video_audio   = sl.get('video_audio', False)

        images = get_media_list(video_enabled)

        if not images:
            log.info('Kein Cache – zeige Platzhalter')
            screen.blit(make_placeholder(W, H), (0, 0))
            pygame.display.flip()
            time.sleep(15)
            continue

        if shuffle:
            random.shuffle(images)
        else:
            images.sort(key=lambda p: p.name)

        for idx, media_path in enumerate(images):
            # Config-Reload zwischen Dateien berücksichtigen
            if _reload_flag:
                _config = load_config()
                _reload_flag = False
                sl       = _config.get('slideshow', {})
                t_name   = sl.get('transition', 'ken_burns')
                t_dur    = sl.get('transition_duration_ms', 1500)
                interval = sl.get('interval_seconds', 30)
                fit      = sl.get('fit_mode', 'contain')
                bg       = hex_to_rgb(sl.get('background_color', '#000000'))
                kb_zoom  = sl.get('ken_burns_zoom', 0.08)
                shuffle  = sl.get('shuffle', True)
                video_enabled = sl.get('video_enabled', True)
                video_audio   = sl.get('video_audio', False)

            # pygame-Events verarbeiten
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    pygame.quit()
                    sys.exit(0)
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    pygame.quit()
                    sys.exit(0)

            # --- Video ---
            if media_path.suffix.lower() in SUPPORTED_VIDEOS:
                screen = play_video(media_path, video_audio)
                W, H = screen.get_size()
                current_surf = pygame.Surface((W, H))
                current_surf.fill((0, 0, 0))
                continue

            # --- Bild ---
            # Wurde dieses Bild bereits im Hintergrund vorgeladen (während das
            # vorherige Bild angezeigt wurde)? Dann steht es hier quasi ohne
            # Wartezeit zur Verfügung. Andernfalls (erstes Bild, Config hat
            # sich geändert, Video davor) synchron laden wie bisher.
            key = (media_path, W, H, fit, bg)
            if prefetch['key'] == key and prefetch['future'] is not None:
                try:
                    img_pil = prefetch['future'].result()
                except Exception as e:
                    log.warning(f'Bild nicht ladbar {media_path.name}: {e}')
                    prefetch['future'] = prefetch['key'] = None
                    continue
            else:
                try:
                    img_pil = load_and_fit(media_path, W, H, fit, bg)
                except Exception as e:
                    log.warning(f'Bild nicht ladbar {media_path.name}: {e}')
                    continue
            prefetch['future'] = prefetch['key'] = None

            # Nächstes Bild direkt jetzt im Hintergrund vordekodieren – es hat
            # bis zum nächsten Bildwechsel (Ken-Burns-/Intervall-Dauer) Zeit.
            next_path = images[idx + 1] if idx + 1 < len(images) else None
            if next_path is not None and next_path.suffix.lower() in SUPPORTED_IMAGES:
                prefetch['key']    = (next_path, W, H, fit, bg)
                prefetch['future'] = _submit_prefetch(next_path, W, H, fit, bg)

            log.info(f'Zeige: {media_path.name}  [{t_name}]')

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


if __name__ == '__main__':
    run()
