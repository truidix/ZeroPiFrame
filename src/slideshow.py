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
import math
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
    """Wandelt "#rrggbb" in ein (r,g,b)-Tupel um.

    Fällt bei ungültigem Wert (z.B. eine von Hand kaputt editierte
    config.yaml) auf Schwarz zurück statt die Slideshow abstürzen zu
    lassen – die Hintergrundfarbe ist rein kosmetisch, ein Absturz deswegen
    wäre unverhältnismäßig.
    """
    try:
        h = hex_color.lstrip('#')
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    except Exception:
        log.warning(f'Ungültige Hintergrundfarbe "{hex_color}", verwende Schwarz')
        return (0, 0, 0)


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
    # als der Screen wählen (Faktor 1.3) für "cover"-Crops mit etwas
    # Spielraum. Hinweis: Ken Burns läuft über load_for_ken_burns() unten,
    # NICHT über diese Funktion – siehe dort für den Grund.
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
    """Lädt ein Bild für Ken Burns OHNE es vorher auf Bildschirmgröße
    zuzuschneiden – anders als load_and_fit() bleibt hier das native
    Seitenverhältnis (und die native Auflösung, soweit vom JPEG-Decoder
    sinnvoll bereitgestellt) erhalten.

    Grund: display_ken_burns() braucht ohnehin ein etwas größeres Bild als
    den Bildschirm (Zoom-Spielraum fürs Schwenken). Würde man zuerst wie bei
    den anderen Übergängen exakt auf Bildschirmgröße zuschneiden und DANACH
    für den Zoom-Spielraum nochmal hochskalieren, entstehen zwei
    verlustbehaftete Schritte statt einem – das Bild wird unnötig weicher,
    gerade bei Fotos die schon knapp an der Bildschirmauflösung liegen.
    Stattdessen wird hier einmal direkt von (möglichst) nativer Auflösung
    auf die tatsächlich benötigte Zoom-Leinwand skaliert (siehe
    display_ken_burns, dort mit LANCZOS statt BILINEAR).
    """
    img = Image.open(path)
    try:
        img.draft('RGB', (canvas_w, canvas_h))
    except Exception:
        pass
    img = ImageOps.exif_transpose(img)
    return img.convert('RGB')


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
    """Kreisförmige Blende: neues Bild wird von der Mitte ausgehend enthüllt."""
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
    """Vorhang-Effekt: altes Bild teilt sich in der Mitte und schiebt nach
    links/rechts auseinander, das neue Bild wird dahinter sichtbar."""
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
    """Venetian Blinds: das Bild wird in vertikale Streifen zerlegt, die
    gemeinsam zu einer Seite hin aufklappen und den Blick auf das neue Bild
    freigeben."""
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
            cut = int(h * t)          # von oben wachsender "aufgeklappter" Teil
            if cut < h:
                rect = pygame.Rect(x0, cut, sw, h - cut)
                screen.blit(old_surf, (x0, cut), rect)
        pygame.display.flip()
        _tick(delay)


def transition_flash(screen, old_surf, new_surf, duration_ms=1500, **_):
    """Kurzer Weißblitz zwischen den Bildern (Kamerablitz-Effekt)."""
    w, h  = screen.get_size()
    white = pygame.Surface((w, h))
    white.fill((255, 255, 255))
    steps = max(6, int(duration_ms / 1000 * 12))
    half  = steps // 2
    delay = duration_ms / 1000 / steps
    # Alt -> Weiß
    for i in range(half + 1):
        alpha = int(255 * i / half)
        overlay = white.copy()
        overlay.set_alpha(alpha)
        screen.blit(old_surf, (0, 0))
        screen.blit(overlay, (0, 0))
        pygame.display.flip()
        _tick(delay)
    # Weiß -> Neu
    for i in range(half + 1):
        alpha = int(255 * (1 - i / half))
        overlay = white.copy()
        overlay.set_alpha(alpha)
        screen.blit(new_surf, (0, 0))
        screen.blit(overlay, (0, 0))
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

# 'ken_burns' ist kein Eintrag in TRANSITIONS (eigene Anzeige-Schleife statt
# einer Blit-Transition), zählt aber als gültige, wählbare Option.
ALL_TRANSITION_NAMES = set(TRANSITIONS.keys()) | {'ken_burns'}


def get_enabled_transitions(sl: dict) -> list:
    """Liest die Liste der im Web-UI aktivierten Übergänge aus der Config.

    Rückwärtskompatibel zu alten Configs, die nur ein einzelnes `transition`-
    Feld kennen (frühere Radio-Auswahl): fehlt `enabled_transitions`, wird
    daraus eine Einzel-Liste. Unbekannte/veraltete Namen werden stillschweigend
    verworfen; bleibt am Ende nichts übrig, wird auf ken_burns zurückgefallen,
    damit die Slideshow nie ganz ohne Übergang dasteht.
    """
    raw = sl.get('enabled_transitions')
    if not raw:
        raw = [sl.get('transition', 'ken_burns')]
    enabled = [t for t in raw if t in ALL_TRANSITION_NAMES]
    return enabled or ['ken_burns']


def pick_transition(enabled: list) -> str:
    """Wählt den Übergang für das nächste Bild aus dem aktivierten Pool.

    Bei genau einem aktivierten Übergang wird (wie vor Einführung dieser
    Funktion) deterministisch immer derselbe verwendet. Bei mehreren wird
    pro Bildwechsel zufällig gewählt, sodass sich die Übergänge abwechseln.
    """
    if not enabled:
        return 'ken_burns'
    if len(enabled) == 1:
        return enabled[0]
    return random.choice(enabled)

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

    # LANCZOS statt BILINEAR: img_pil kommt jetzt (via load_for_ken_burns)
    # nativ-aufgelöst rein statt schon auf Bildschirmgröße vorgeschnitten,
    # daher lohnt sich hier die etwas teurere, aber deutlich schärfere
    # Skalierung – sie passiert nur einmal pro Bild, nicht pro Frame.
    img_scaled = img_pil.resize((fit_w, fit_h), Image.LANCZOS)

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
    try:
        entries = list(CACHE_DIR.iterdir())
    except FileNotFoundError:
        # Cache-Verzeichnis existiert noch nicht (z.B. frisch installiert,
        # noch kein einziger Sync gelaufen) – kein Fehler, einfach als
        # "noch keine Bilder" behandeln statt der Slideshow abstürzen zu
        # lassen (das würde sonst bei jedem Neustart sofort wieder passieren).
        return files
    for p in entries:
        ext = p.suffix.lower()
        if ext in SUPPORTED_IMAGES:
            files.append(p)
        elif video_enabled and ext in SUPPORTED_VIDEOS:
            files.append(p)
    return files


def shuffled_avoiding_recent(images: list, recent: list) -> list:
    """Mischt `images` zufällig – vermeidet dabei aber, dass die zuletzt
    gezeigten Bilder direkt wieder ganz vorne im neuen Durchlauf landen.

    Hintergrund: random.shuffle() auf die komplette Liste ist tatsächlich
    gleichverteilt zufällig, aber zwei unabhängige Zufalls-Durchläufe
    hintereinander können völlig zufällig genau die zuletzt gezeigten Bilder
    wieder an den Anfang des nächsten Durchlaufs setzen. Für Betrachter
    fühlt sich das an wie "es zeigt ständig dieselben Bilder", obwohl der
    Zufall an sich korrekt ist – gerade bei kleineren Bibliotheken fällt das
    besonders auf. Diese Funktion tauscht daher, soweit die Bibliotheksgröße
    es zulässt, mit den zuletzt gezeigten Bildern kollidierende Startplätze
    gegen Bilder weiter hinten in der neuen Mischung.
    """
    pool = images[:]
    random.shuffle(pool)
    if not recent or len(pool) < 3:
        return pool

    # Höchstens die Hälfte der Bibliothek als "kürzlich gezeigt" behandeln –
    # sonst bliebe bei kleinen Bibliotheken nichts mehr zum Tauschen übrig.
    cap = max(1, min(len(recent), len(pool) // 2))
    avoid = set(recent[-cap:])
    if len(avoid) >= len(pool):
        return pool  # praktisch die ganze Bibliothek "kürzlich gezeigt"

    i = 0
    while i < cap and pool[i] in avoid:
        for j in range(cap, len(pool)):
            if pool[j] not in avoid:
                pool[i], pool[j] = pool[j], pool[i]
                break
        i += 1
    return pool


# Ein einzelner Hintergrund-Worker reicht: er dekodiert währenddessen das
# nächste Bild vor, sodass load_and_fit() (Decode + LANCZOS-Resize eines
# potenziell hochauflösenden Fotos) beim eigentlichen Bildwechsel nicht mehr
# blockiert. Auf dem Pi Zero 2 W ist das der Unterschied zwischen einem
# spürbaren Ruckler bei jedem Bildwechsel und einem nahtlosen Übergang.
_prefetch_executor = ThreadPoolExecutor(max_workers=1)


def _submit_prefetch(path: Path, w: int, h: int, fit: str, bg: tuple,
                      t_name: str, canvas_w: int, canvas_h: int):
    if t_name == 'ken_burns':
        return _prefetch_executor.submit(load_for_ken_burns, path, canvas_w, canvas_h)
    return _prefetch_executor.submit(load_and_fit, path, w, h, fit, bg)


def _recover_display(w: int, h: int):
    """Baut die pygame-Anzeige nach einem Fehler sicherheitshalber neu auf.

    Ein pygame/SDL-Fehler (z.B. abgebrochene Video-Wiedergabe, ein
    kaputter Übergang) kann die Display-Oberfläche in einem undefinierten
    Zustand hinterlassen. Statt den Prozess deswegen abstürzen zu lassen –
    KMSDRM gibt dann die Display-Hoheit frei und die Text-Konsole blitzt auf
    dem Bildschirm auf, bis systemd den Dienst neu startet – wird hier
    versucht, die Anzeige im laufenden Prozess neu zu initialisieren, ohne
    dass der Bildschirm je auf die Konsole zurückfällt.
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
        log.error('Display konnte nach Fehler nicht neu initialisiert werden')
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
    log.info('Starte Slideshow')
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

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
    # Übergang des jeweils nächsten Bildes wird schon beim Vorladen gewählt
    # (siehe pick_transition) und hier zwischengespeichert, damit Laden und
    # Anzeigen denselben Übergang verwenden.
    current_t_name = None
    # Verlauf zuletzt gezeigter Bilder für shuffled_avoiding_recent() – lebt
    # über einzelne Durchläufe hinweg, damit der Übergang zwischen zwei
    # Durchläufen nicht dieselben Bilder direkt wieder zeigt.
    recent_shown = []

    while True:
        # Der komplette Schleifenkörper ist bewusst in ein try/except
        # gefasst: er ist die letzte Verteidigungslinie. Alle bekannten
        # Fehlerquellen (Laden, Video, Anzeige) sind bereits einzeln
        # abgesichert (s.o.), aber ein hier zusätzlich gefangener,
        # unvorhergesehener Fehler verhindert trotzdem, dass der ganze
        # Prozess abstürzt – und damit, dass kurz die Text-Konsole auf dem
        # Bildschirm aufblitzt, bis systemd (Restart=always) neu startet.
        try:
            if _reload_flag:
                _config = load_config()
                _reload_flag = False
                current_t_name = None
                log.info('Config neu geladen')

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
                log.info('Kein Cache – zeige Platzhalter')
                screen.blit(make_placeholder(W, H), (0, 0))
                pygame.display.flip()
                time.sleep(15)
                continue

            if shuffle:
                images = shuffled_avoiding_recent(images, recent_shown)
            else:
                images.sort(key=lambda p: p.name)

            for idx, media_path in enumerate(images):
                # Config-Reload zwischen Dateien berücksichtigen
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
                    try:
                        screen = play_video(media_path, video_audio)
                        W, H = screen.get_size()
                        current_surf = pygame.Surface((W, H))
                        current_surf.fill((0, 0, 0))
                    except Exception as e:
                        log.exception(f'Video-Wiedergabe fehlgeschlagen ({media_path.name}): {e}')
                        screen, current_surf = _recover_display(W, H)
                        W, H = screen.get_size()
                    continue

                # --- Bild ---
                # Wurde dieses Bild bereits im Hintergrund vorgeladen (während das
                # vorherige Bild angezeigt wurde)? Dann steht es hier quasi ohne
                # Wartezeit zur Verfügung. Andernfalls (erstes Bild, Config hat
                # sich geändert, Video davor) synchron laden wie bisher.
                # Übergang für DIESES Bild: entweder schon beim Vorladen im
                # letzten Durchlauf festgelegt (current_t_name), oder – falls
                # noch keiner feststeht (allererstes Bild, gerade neu geladene
                # Config) – jetzt aus dem Pool der aktivierten Übergänge wählen.
                # Muss VOR dem Laden feststehen, weil load_for_ken_burns() und
                # load_and_fit() unterschiedliche Bildformate liefern.
                if current_t_name is None:
                    current_t_name = pick_transition(enabled_transitions)
                t_name = current_t_name

                key = (media_path, W, H, fit, bg, t_name, canvas_w, canvas_h)
                if prefetch['key'] == key and prefetch['future'] is not None:
                    try:
                        img_pil = prefetch['future'].result()
                    except Exception as e:
                        log.warning(f'Bild nicht ladbar {media_path.name}: {e}')
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
                        log.warning(f'Bild nicht ladbar {media_path.name}: {e}')
                        current_t_name = None
                        continue
                prefetch['future'] = prefetch['key'] = None

                # Übergang für das NÄCHSTE Bild schon jetzt festlegen, damit das
                # Vorladen im Hintergrund mit dem passenden Lademodus (Ken Burns
                # vs. normaler Fit) für genau den Übergang passiert, der beim
                # nächsten Bildwechsel auch tatsächlich verwendet wird.
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

                log.info(f'Zeige: {media_path.name}  [{t_name}]')

                # Die eigentliche Anzeige (Ken Burns oder ein Blit-Übergang) ist
                # bewusst separat abgesichert: ein einzelner fehlerhafter Frame
                # (z.B. ein pygame/SDL-Fehler mitten in einem Übergang) soll nie
                # den ganzen Prozess beenden – sonst blitzt kurz die Text-Konsole
                # auf, bis systemd die Slideshow neu startet. Stattdessen wird
                # nur dieses eine Bild übersprungen und die Anzeige bei Bedarf
                # neu aufgebaut.
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
                    log.exception(f'Anzeige-Fehler bei {media_path.name} (Übergang {t_name}): {e}')
                    screen, current_surf = _recover_display(W, H)
                    W, H = screen.get_size()

        except Exception as e:
            # Alles andere, was oben nicht schon gezielt abgefangen wurde
            # (z.B. ein Fehler in der Konfigurationsauswertung oder beim
            # Mischen der Bilderliste). Kurze Pause, dann geht die
            # Haupt-Schleife normal weiter statt den Prozess zu beenden.
            log.exception(f'Unerwarteter Fehler in der Haupt-Schleife, Slideshow läuft weiter: {e}')
            time.sleep(2)


if __name__ == '__main__':
    run()
