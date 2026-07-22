#!/usr/bin/env python3
"""
Photoframe Sync
Synchronizes images from Nextcloud (WebDAV) or Immich (REST API)
into the local cache. Started periodically via a systemd timer.
"""

import os
import sys
import json
import time
import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Optional

import yaml
import requests
from requests.auth import HTTPBasicAuth
from PIL import Image, ImageOps

from hw import hardware_h264_encoder_available

LOG_FORMAT = '%(asctime)s [sync] %(levelname)s: %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
                    handlers=[
                        logging.StreamHandler(sys.stdout),
                        logging.FileHandler('/var/log/photoframe-sync.log'),
                    ])
log = logging.getLogger(__name__)

CONFIG_PATH = Path('/opt/photoframe/config.yaml')
CACHE_DIR   = Path('/var/lib/photoframe/cache')
# Written by slideshow.py (see its _write_current_state) purely for the web
# UI's status page - reused here as a cheap way to tell whether a video is
# playing right now, see _slideshow_playing_video() below.
CURRENT_STATE_PATH = Path('/var/lib/photoframe/current.json')
SUPPORTED_IMAGES = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
SUPPORTED_VIDEOS = {'.mp4', '.mkv', '.mov', '.avi', '.m4v', '.webm'}
SUPPORTED        = SUPPORTED_IMAGES | SUPPORTED_VIDEOS

# Caps the resolution of cached images at sync time. slideshow.py already
# decodes JPEGs at a reduced resolution via PIL's draft() mode, but that
# only works for JPEG - PNG/BMP/WebP have no equivalent scaled-decode
# mode in Pillow and are always decoded at full native resolution, on
# every single slideshow display cycle (paid again and again, not just
# once, and this is local Pi CPU work regardless of media_processing_mode -
# offloading the actual resize to a remote service doesn't change how
# expensive it is to decode+scale the RESULT on every display cycle).
# A 12-108 MP phone photo/screenshot is far more resolution than any
# picture-frame display needs, and was directly implicated in on-device
# OOM kills of the slideshow process.
#
# Set to match this frame's actual connected display (1920x1080) rather
# than a generic 4K cap - there's no benefit to decoding/scaling photos
# larger than the screen can show, and every pixel above that is pure
# wasted CPU time on every single slideshow cycle, forever. Adjust this
# if the display is ever changed to something higher-resolution.
MAX_DIMENSION_PX = 1920

# Video derivative just for this frame's own cache - independent of whatever
# quality/transcode settings the source server (Immich/Nextcloud) uses for
# everyone else who views the same library (phones, laptops, ...). Caps the
# shorter side at 720px and the bitrate at ~4.5 Mbit/s (Immich's own
# suggested value for 720p H.264) using the Pi's hardware H.264 ENCODER
# (v4l2m2m - the same SoC block that also does decode), which only takes a
# few seconds per video, done once at sync time, rather than lowering
# quality for every other device sharing the same source library. Falls
# back to a slow software encode if the hardware encoder isn't available
# for some reason, and is skipped entirely for videos already within both
# limits (no point re-encoding, and re-encoding something already small
# would only cost quality for nothing).
#
# Opt-in, off by default (see "video_transcode_enabled" below and the web
# UI's Display tab) - it does cost real quality on the copy the frame
# keeps, so it shouldn't happen silently; only worth turning on if videos
# are actually stuttering.
VIDEO_MAX_SHORT_SIDE_PX = 720
VIDEO_MAX_BITRATE_KBPS  = 4500
# Caps the output frame rate - a lot of phone/action-cam footage is shot
# at 50/60fps, which is more motion smoothness than a picture-frame
# slideshow needs and directly means more frames the Pi's hardware
# DECODER has to churn through per second of playback, regardless of
# which machine did the encoding. 30fps is a clean 1:2 ratio against the
# common 60fps source rate (even frame dropping, no judder), and phone
# footage shot natively at 24/25/30fps passes through untouched since
# ffmpeg's -r only drops/duplicates frames when the source exceeds it.
VIDEO_MAX_FPS = 30

# Set once by main() before any sync work starts, from the "Media
# processing" config (web UI's Display tab). Read by _downscale_if_needed()
# and _transcode_video_if_needed() instead of threading two more
# parameters through every call site across both NextcloudSync and
# ImmichSync (unlike `mode`, which only affects one function, this affects
# both, so a settings snapshot is less invasive than parameter-threading
# it through four call sites for no real benefit).
#
# Exists because the Pi's own H.264 hardware ENCODER was confirmed broken
# on-device (produces corrupted/green output regardless of content,
# resolution, or which encoder tool is used - see this project's own
# debugging history), and software-encoding on the Pi's weak CPU is slow
# and OOM-risky on large sources. When enabled, both photo downscaling and
# video transcoding are sent over the LAN to a separate, more powerful
# machine instead (see remote-transcode-service/ in this repo) - the *hardware*
# and software video_transcode_mode choice becomes irrelevant in this case,
# since neither of the Pi's own encode paths gets used at all.
REMOTE = {'enabled': False, 'url': '', 'api_key': ''}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}

# ---------------------------------------------------------------------------
# Network check
# ---------------------------------------------------------------------------

def is_reachable(host: str, port: int = 80, timeout: int = 5) -> bool:
    """Checks whether a host is reachable (TCP connect, no ICMP)."""
    import socket
    try:
        socket.setdefaulttimeout(timeout)
        with socket.create_connection((host, port)):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def _slideshow_playing_video() -> bool:
    """True if the slideshow is currently playing a video, per the status
    file it writes for the web UI's status page.

    Hardware video decode (slideshow/mpv) and hardware video encode (the
    transcode step below, for newly synced videos) share the same SoC
    codec block and the same small, already-tight CMA memory pool - this
    device swaps/stutters under load from either one alone (see the video
    playback investigation). Running both at once would only make things
    worse, so sync is deferred entirely rather than risk it; there's
    always a next timer cycle. Best-effort: any error reading the state
    file (missing, corrupt, stale) is treated as "not playing" so a
    status-file hiccup can never permanently block sync.
    """
    try:
        data = json.loads(CURRENT_STATE_PATH.read_text())
        return data.get('type') == 'video'
    except Exception:
        return False


def extract_host_port(url: str) -> tuple[str, int]:
    """Extracts host and port from a URL."""
    from urllib.parse import urlparse
    p    = urlparse(url)
    host = p.hostname or p.path
    port = p.port or (443 if p.scheme == 'https' else 80)
    return host, port

# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def cache_size_gb() -> float:
    # .part files (incomplete downloads, see _download_atomic) don't count
    # toward the cache, so an aborted download doesn't permanently count
    # against the cache limit.
    total = sum(f.stat().st_size for f in CACHE_DIR.iterdir()
                if f.is_file() and not f.name.endswith('.part'))
    return total / (1024 ** 3)


def cleanup_orphaned_downloads():
    """Removes leftover .part files from a previous, aborted sync run."""
    for f in CACHE_DIR.glob('*.part'):
        log.info(f'Removed orphaned .part file: {f.name}')
        f.unlink(missing_ok=True)


def cached_files() -> dict[str, Path]:
    """Returns a dict {filename: Path} of all cached images."""
    return {f.name: f for f in CACHE_DIR.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED}


def enforce_cache_limit(max_gb: float) -> int:
    """Deletes the oldest images when the cache limit is exceeded.

    Returns the number of files deleted this way, so the caller can fold
    it into its own "Sync completed" summary - this used to delete and
    log each file individually ("Cache limit: deleting ...") but never
    reported the count anywhere else, silently undercounting the
    summary's "-N deleted" figure whenever a sync run added enough new
    photos to push the cache over its limit (e.g. right after adding a
    new album/folder while another one was removed).
    """
    total = cache_size_gb() * (1024 ** 3)
    limit = max_gb * (1024 ** 3)
    if total <= limit:
        return 0
    target = limit * 0.9
    files  = sorted(CACHE_DIR.iterdir(), key=lambda f: f.stat().st_mtime)
    evicted = 0
    for f in files:
        if total <= target:
            break
        try:
            size = f.stat().st_size
        except OSError:
            continue
        log.info(f'Cache limit: deleting {f.name}')
        f.unlink(missing_ok=True)
        total -= size
        evicted += 1
    return evicted


def _remote_post_file(endpoint: str, path: Path, data: dict, tmp_suffix: str) -> Optional[Path]:
    """Shared upload/download plumbing for both remote endpoints (see
    REMOTE and remote-transcode-service/ in this repo). Uploads `path` as
    multipart form data plus the given extra fields, streams the response
    into a sibling temp file (never buffers the whole response in memory -
    videos in particular can be hundreds of MB, and this Pi only has
    512MB RAM), and returns that temp file's path on success or None on
    any failure (network error, timeout, non-2xx response). The caller is
    responsible for the final os.replace() so it can log/handle failure
    however fits that call site.
    """
    tmp = path.with_name(path.name + tmp_suffix)
    try:
        with open(path, 'rb') as f:
            resp = requests.post(
                f"{REMOTE['url'].rstrip('/')}/{endpoint}",
                files={'file': (path.name, f)},
                data=data,
                headers={'X-API-Key': REMOTE['api_key']},
                timeout=1800,
                stream=True,
            )
        resp.raise_for_status()
        with open(tmp, 'wb') as out:
            for chunk in resp.iter_content(65536):
                out.write(chunk)
        return tmp
    except Exception as e:
        log.warning(f'Remote {endpoint} processing failed for {path.name} ({e}) - left as-is')
        tmp.unlink(missing_ok=True)
        return None


def _downscale_if_needed(path: Path) -> bool:
    """Shrinks a cached image in place if its longer side exceeds
    MAX_DIMENSION_PX. Returns True if the file was actually resized.

    Runs once per photo, here at sync time, instead of every download
    paying the full decode/resize cost of an oversized original on every
    single slideshow display cycle for the whole time it's in the
    rotation. Left untouched (returns False without error) for anything
    already at or below the cap, and for multi-frame (animated) images,
    which Pillow would otherwise silently collapse to a single frame.

    The "does this even need resizing" decision below always happens
    locally (cheap - PIL only reads the header for this) regardless of
    REMOTE['enabled']; only the actual resize work is sent to the remote
    transcode service when that's turned on, since there's no reason to
    pay a network round-trip for a photo that already fits.
    """
    try:
        with Image.open(path) as img:
            fmt = img.format
            w, h = img.size
            if max(w, h) <= MAX_DIMENSION_PX:
                return False
            if getattr(img, 'n_frames', 1) > 1:
                return False

        if REMOTE['enabled']:
            tmp = _remote_post_file('image', path, {'max_dimension': MAX_DIMENSION_PX},
                                     '.remote-resize-tmp')
            if tmp is None:
                return False
            os.replace(tmp, path)
            log.info(f'Remotely downscaled {path.name}: {w}x{h} -> (see service log for exact size)')
            return True

        with Image.open(path) as img:
            fmt = img.format
            # Bakes in EXIF rotation and drops the orientation tag, so the
            # saved file doesn't end up rotated twice when slideshow.py
            # applies its own exif_transpose() on top of this later.
            img = ImageOps.exif_transpose(img)
            if img.mode == 'P':
                img = img.convert('RGBA')

            scale = MAX_DIMENSION_PX / max(img.size)
            new_size = (max(1, round(img.width * scale)),
                       max(1, round(img.height * scale)))
            img = img.resize(new_size, Image.LANCZOS)
            img.info.pop('exif', None)

            tmp = path.with_name(path.name + '.resize-tmp')
            if fmt == 'JPEG':
                img.convert('RGB').save(tmp, format='JPEG', quality=90, optimize=True)
            else:
                img.save(tmp, format=fmt)
            os.replace(tmp, path)
        log.info(f'Downscaled {path.name}: {w}x{h} -> {new_size[0]}x{new_size[1]}')
        return True
    except Exception as e:
        log.warning(f'Could not downscale {path.name}: {e}')
        return False


def _needs_transcode_marker(path: Path) -> Path:
    """Marker file path used to remember that a video's transcode was
    deferred (see _transcode_video_if_needed) because the slideshow
    started playing a video mid-sync. Without this, a later sync run
    would just see the file already downloaded and skip it forever -
    sync()'s "already cached" check looks for this marker and retries the
    transcode (without re-downloading) instead of skipping when present.
    """
    return path.with_name(f'.{path.name}.needs-transcode')


def _probe_video(path: Path) -> Optional[tuple[int, int, Optional[int], Optional[float]]]:
    """Returns (width, height, bitrate_kbps, fps) for a video's first stream
    via ffprobe, or None if it can't be determined (corrupt/unsupported
    file - left untouched by the caller in that case, same fail-safe
    philosophy as _downscale_if_needed). bitrate_kbps is None if neither
    the stream nor the container reports one (falls back to the
    container-level bitrate since some files only set it there, not per-
    stream). fps is None if r_frame_rate is missing or malformed (e.g.
    "0/0", which ffprobe reports for some corrupt/still-image-as-video
    files) - treated as "fits" by the caller, same reasoning as an
    unknown bitrate.
    """
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height,bit_rate,r_frame_rate:format=bit_rate',
             '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=30, check=True
        )
        data    = json.loads(out.stdout)
        streams = data.get('streams') or [{}]
        stream  = streams[0]
        w, h = stream.get('width'), stream.get('height')
        if not w or not h:
            return None
        br = stream.get('bit_rate') or data.get('format', {}).get('bit_rate')

        fps = None
        raw_fps = stream.get('r_frame_rate')  # e.g. "30000/1001", "25/1", "0/0"
        if raw_fps:
            try:
                num, den = raw_fps.split('/')
                if float(den) > 0:
                    fps = float(num) / float(den)
            except Exception:
                pass

        return (int(w), int(h), int(int(br) / 1000) if br else None, fps)
    except Exception:
        return None


def _transcode_video_if_needed(path: Path, mode: str = 'hardware') -> bool:
    """Re-encodes a cached video in place to VIDEO_MAX_SHORT_SIDE_PX /
    VIDEO_MAX_BITRATE_KBPS if it exceeds either. See the module-level
    comment above those constants for why this happens locally instead of
    via the source server's own transcode settings. Returns True if the
    file was actually re-encoded.

    `mode` ("hardware" or "software", the web UI's "Encoding" choice under
    "Re-encode videos locally") picks exactly ONE encode path - unlike
    this function's earlier design, there is no automatic hardware ->
    software fallback here anymore. That auto-fallback used to mean
    someone who deliberately wants the fast hardware path could
    unexpectedly get stuck with a multi-minute software encode (and its
    real OOM risk on this device, see SW_PROFILE_ARGS's comment below)
    with no way to opt out. An explicit, hard choice is more predictable:
    hardware mode either works (fast) or leaves the file untouched
    (logged, not silently absorbed into a slow path); software mode never
    even attempts the hardware encoder.

    Checked again here, not just once at the start of the whole sync run
    (see _slideshow_playing_video() and main()): a sync run that photos-
    first, videos-last (see sync() below) can take several minutes once
    it reaches the video/transcode step, which is plenty of time for the
    slideshow to start playing a video on its own in the meantime. Caught
    exactly this happening live: the slideshow decoding a video and this
    function's ffmpeg encode both running at once, right after a reboot,
    contending for the same hardware codec block/CMA pool - which made
    the hardware encoder fail and silently fall back to a slow software
    encode (225% CPU for several minutes), not an actual memory leak.
    """
    if _slideshow_playing_video():
        log.info(f'Slideshow started playing a video mid-sync - deferring '
                 f'transcode of {path.name} to a future sync run rather than '
                 f'contend for the same hardware codec block/CMA pool')
        # Without this marker, the next sync run would just see the file
        # already downloaded and skip it forever - it would never actually
        # get transcoded until a full cache clear. sync()'s "already
        # exists" skip checks for this marker and retries the transcode
        # (without re-downloading) instead of skipping when it's present.
        _needs_transcode_marker(path).touch()
        return False
    _needs_transcode_marker(path).unlink(missing_ok=True)

    info = _probe_video(path)
    if info is None:
        log.info(f'Could not probe {path.name} (ffprobe failed) - left as-is')
        # Transient (corrupt-in-transit download, momentary resource
        # contention) rather than a permanent "this file is fine" state -
        # re-touch the marker so a future sync run tries again instead of
        # silently giving up on this file forever. Found the hard way:
        # without this, ANY failure here (or in the transcode attempts
        # below) left files permanently stuck at their original size,
        # since the marker was already cleared above the moment we
        # decided not to defer for slideshow contention - the "unchanged
        # etag" skip path in sync() only ever retries when this marker
        # exists.
        _needs_transcode_marker(path).touch()
        return False
    w, h, bitrate_kbps, fps = info

    short_side = min(w, h)
    fits_res   = short_side <= VIDEO_MAX_SHORT_SIDE_PX
    # Unknown bitrate/fps is treated as "fits" - nothing to go on, and
    # re-encoding purely based on resolution (if that already fits too)
    # would cost quality for no measurable gain.
    fits_bitrate = bitrate_kbps is None or bitrate_kbps <= VIDEO_MAX_BITRATE_KBPS * 1.15
    # +0.5 tolerance: common "30fps" sources actually report 29.97
    # (30000/1001) - without this, those would be re-encoded for a
    # meaningless 0.03fps difference.
    fits_fps = fps is None or fps <= VIDEO_MAX_FPS + 0.5
    if fits_res and fits_bitrate and fits_fps:
        log.info(f'{path.name}: {w}x{h}'
                 f'{f" @ {bitrate_kbps}kbps" if bitrate_kbps else " (bitrate unknown)"}'
                 f'{f" {fps:.0f}fps" if fps else ""} already within '
                 f'{VIDEO_MAX_SHORT_SIDE_PX}px/{VIDEO_MAX_BITRATE_KBPS}kbps/{VIDEO_MAX_FPS}fps - no transcode needed')
        return False

    if REMOTE['enabled']:
        # `mode` (hardware/software) is irrelevant here - neither of the
        # Pi's own encode paths gets used when remote processing is on.
        # The remote service does its own 32-alignment/Main-profile/no-
        # B-frames handling (see remote-transcode-service/app.py) since
        # those constraints exist for the Pi's DECODER, which plays the
        # result back regardless of which machine produced it.
        tmp = _remote_post_file('video', path,
                                 {'short_side': VIDEO_MAX_SHORT_SIDE_PX,
                                  'bitrate_kbps': VIDEO_MAX_BITRATE_KBPS,
                                  'max_fps': VIDEO_MAX_FPS},
                                 '.remote-transcode-tmp.mp4')
        if tmp is None:
            # Transient (service unreachable/misconfigured/temporarily
            # down) - see the identical comment on the ffprobe-failure
            # path above for why this re-touches the marker instead of
            # leaving the file permanently untranscoded.
            _needs_transcode_marker(path).touch()
            return False
        os.replace(tmp, path)
        log.info(f'Remotely transcoded {path.name}: {w}x{h}'
                 f'{f" @ {bitrate_kbps}kbps" if bitrate_kbps else ""}'
                 f'{f" {fps:.0f}fps" if fps else ""} -> '
                 f'capped at {VIDEO_MAX_SHORT_SIDE_PX}px/{VIDEO_MAX_BITRATE_KBPS}kbps/{VIDEO_MAX_FPS}fps')
        return True

    # Both output dimensions are forced to a multiple of 32 (via ffmpeg's
    # scale filter "-32" auto-dimension, and by rounding the short-side
    # target itself down to the nearest 32) - confirmed on-device that the
    # Pi's h264_v4l2m2m hardware encoder corrupts the NV12 chroma plane
    # for any frame whose height/width isn't 32-aligned: the padding
    # rows/columns come out zeroed instead of neutral (128), which decodes
    # as a solid green band/patch baked permanently into the output file
    # (not a playback artifact - reproduced by directly inspecting the
    # transcoded file with ffplay). Applied unconditionally, including the
    # "already fits, just need a bitrate cut" case that used to skip
    # scaling entirely (bare `format=nv12`) - that path left the source's
    # original, possibly non-32-aligned dimensions untouched, so it was
    # just as exposed to the same corruption. Also applied on the
    # software (libx264) path even though it isn't known to need this -
    # keeps a given video's dimensions identical no matter which mode
    # encoded it, so flipping the hardware/software toggle later never
    # forces a second, differently-cropped re-encode.
    _ALIGN = 32

    def _align_down(n: int) -> int:
        return max(_ALIGN, (n // _ALIGN) * _ALIGN)

    if w >= h:
        target_h = _align_down(min(h, VIDEO_MAX_SHORT_SIDE_PX))
        vf = f'scale=-{_ALIGN}:{target_h},format=nv12'
    else:
        target_w = _align_down(min(w, VIDEO_MAX_SHORT_SIDE_PX))
        vf = f'scale={target_w}:-{_ALIGN},format=nv12'

    tmp = path.with_name(path.name + '.transcode-tmp.mp4')

    def _run(video_codec: str, extra: list[str]) -> bool:
        cmd = ['ffmpeg', '-y', '-i', str(path), '-vf', vf, '-r', str(VIDEO_MAX_FPS),
               '-c:v', video_codec, *extra,
               '-c:a', 'aac', '-b:a', '128k',
               '-f', 'mp4', '-movflags', '+faststart', str(tmp)]
        subprocess.run(cmd, capture_output=True, timeout=600, check=True)
        return True

    # No B-frames on both paths: confirmed via upstream ZeroPlay issue
    # tracker (github.com/HorseyofCoursey/zeroplay, issue #14) that the
    # Pi's bcm2835-codec hardware DECODER can permanently stall on
    # specific frames of High-profile/B-frame H.264 - a hardware/driver
    # limitation, not a player bug (reproduced with mpv AND ZeroPlay on
    # our own transcoded files).
    #
    # Main profile is additionally forced ONLY on the software (libx264)
    # path. libx264 defaults to High profile with B-frames if not told
    # otherwise - exactly the combination flagged - so it needs an
    # explicit override. The hardware (v4l2m2m) encoder does NOT get
    # -profile:v main: confirmed on-device that asking for it makes ffmpeg
    # fail outright (exit code 234) every time - the Pi's H.264 hardware
    # ENCODER (unlike its decoder, which handles up to High@L4.1 fine) has
    # always been Baseline-only by design, a long-standing, documented
    # limitation of this SoC's encode block, unrelated to the decoder
    # stall bug above. Baseline is structurally simpler than Main (no
    # CABAC, no B-frames possible at all), so it should be at least as
    # safe against the same decoder stall - letting the hardware encoder
    # use its own native profile (rather than forcing one it can't do)
    # means it can actually succeed again instead of failing every time.
    SW_PROFILE_ARGS = ['-profile:v', 'main', '-bf', '0']

    def _log_profile(label: str):
        """Best-effort: logs the profile ffprobe sees in the freshly
        encoded file, purely so a future "it's stalling again" report has
        the actual profile to go on instead of having to guess which path
        produced the file."""
        info = None
        try:
            out = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=profile', '-of',
                 'default=noprint_wrappers=1:nokey=1', str(tmp)],
                capture_output=True, text=True, timeout=30, check=True).stdout
            info = out.strip()
        except Exception:
            pass
        log.info(f'{label} encode profile: {info or "unknown"}')

    # Defensive fallback: the web UI (see webui.py) already hides/disables
    # the "Hardware" option when hardware_h264_encoder_available() is
    # False, so this should only ever trigger from a hand-edited
    # config.yaml or a device swap since the option was last selected -
    # not the normal path for anyone using the UI as intended.
    effective_mode = mode
    if mode == 'hardware' and not hardware_h264_encoder_available():
        log.warning(f'video_transcode_mode is "hardware" but no working hardware '
                    f'H.264 encoder was detected on this device - using software instead')
        effective_mode = 'software'

    try:
        if effective_mode == 'hardware':
            # A few seconds per video on this SoC's own encode block, vs.
            # minutes of software x264 on the same weak CPU that's also
            # supposed to be running the slideshow/web UI in the
            # background. No -profile:v here - see the comment above
            # SW_PROFILE_ARGS for why forcing one just makes this fail
            # outright.
            _run('h264_v4l2m2m', ['-b:v', f'{VIDEO_MAX_BITRATE_KBPS}k', '-bf', '0'])
            _log_profile('Hardware')
        else:
            # ultrafast preset forces cabac=0, which silently downgrades
            # the actual encoded profile to Constrained Baseline no matter
            # what -profile:v is asked for - defeating the whole point of
            # SW_PROFILE_ARGS above. The fix is NOT to switch to a slower
            # preset (tried "superfast" - confirmed on-device it raised
            # peak memory enough to get this exact software encode
            # SIGKILLed by the OOM killer on a 1080p source, on a device
            # that was already known to be tight on RAM during video
            # encode/decode). Instead, force just the one setting ultrafast
            # disables back on via -x264-params, keeping every other
            # ultrafast/low-resource choice (ref=1, no trellis, dia motion
            # search, etc.) - confirmed via testing to still produce a
            # genuine Main-profile bitstream at ultrafast's original,
            # OOM-safe resource footprint.
            _run('libx264', ['-preset', 'ultrafast', '-crf', '23',
                             '-maxrate', f'{VIDEO_MAX_BITRATE_KBPS}k',
                             '-bufsize', f'{VIDEO_MAX_BITRATE_KBPS * 2}k',
                             *SW_PROFILE_ARGS, '-x264-params', 'cabac=1'])
            _log_profile('Software')
    except Exception as e:
        log.warning(f'Could not transcode {path.name} via {effective_mode} encode '
                    f'({e}) - left as-is')
        # Transient (OOM-killed encode, momentary resource contention) -
        # see the identical comment on the ffprobe-failure path above for
        # why this re-touches the marker instead of leaving the file
        # permanently untranscoded.
        _needs_transcode_marker(path).touch()
        tmp.unlink(missing_ok=True)
        return False

    os.replace(tmp, path)
    log.info(f'Transcoded {path.name}: {w}x{h}'
             f'{f" @ {bitrate_kbps}kbps" if bitrate_kbps else ""}'
             f'{f" {fps:.0f}fps" if fps else ""} -> '
             f'capped at {VIDEO_MAX_SHORT_SIDE_PX}px/{VIDEO_MAX_BITRATE_KBPS}kbps/{VIDEO_MAX_FPS}fps')
    return True


def _download_atomic(resp, dest: Path) -> int:
    """Streams a response into a temporary file and only atomically renames
    it to the target name after the download has fully completed.

    Prevents the slideshow (which independently re-reads the cache folder
    periodically) from ever seeing a still-incompletely-downloaded file and
    displaying a broken/corrupted image.
    """
    tmp = dest.with_name(dest.name + '.part')
    size = 0
    with open(tmp, 'wb') as fh:
        for chunk in resp.iter_content(65536):
            fh.write(chunk)
            size += len(chunk)
    os.replace(tmp, dest)   # atomic rename on the same filesystem
    return size

# ---------------------------------------------------------------------------
# Nextcloud / WebDAV Sync
# ---------------------------------------------------------------------------

class NextcloudSync:
    def __init__(self, cfg: dict):
        self.url      = cfg['url'].rstrip('/')
        self.username = cfg['username']
        self.password = cfg['password']
        self.folders  = cfg.get('folders', [])   # empty = root folder
        self.session  = requests.Session()
        self.session.auth = HTTPBasicAuth(self.username, self.password)
        self.session.verify = cfg.get('verify_ssl', True)
        # Unlike Immich, WebDAV/PROPFIND doesn't provide image resolution in
        # its metadata - so the file has to be downloaded first before it
        # can be checked whether it's below the minimum resolution. If it
        # gets filtered out, a .lowres marker file with the ETag is kept, so
        # it isn't downloaded again on every sync as long as the file on
        # the server doesn't change.
        self.min_resolution_px = cfg.get('min_resolution_px', 0) or 0

    def _below_min_resolution(self, path: Path) -> bool:
        """True if the locally downloaded image is below the configured
        minimum resolution. Uses PIL's Image.open(), which only reads the
        header (no full decode) - fast even with many/large files. If the
        resolution can't be determined (corrupt/unknown file), it is NOT
        filtered out when in doubt, so as not to lose a valid photo.
        """
        if not self.min_resolution_px:
            return False
        try:
            with Image.open(path) as img:
                w, h = img.size
        except Exception:
            return False
        return min(w, h) < self.min_resolution_px

    def list_remote(self, path: str = '') -> list[dict]:
        """Lists files via WebDAV PROPFIND."""
        from xml.etree import ElementTree as ET

        url  = f'{self.url}/{path}' if path else self.url
        body = '''<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:displayname/>
    <d:getlastmodified/>
    <d:getcontenttype/>
    <d:getetag/>
    <d:getcontentlength/>
    <d:resourcetype/>
  </d:prop>
</d:propfind>'''
        resp = self.session.request(
            'PROPFIND', url,
            data=body,
            headers={'Depth': '1', 'Content-Type': 'application/xml'},
            timeout=30
        )
        resp.raise_for_status()

        ns   = {'d': 'DAV:'}
        tree = ET.fromstring(resp.text)
        items = []
        for response in tree.findall('d:response', ns):
            href      = response.findtext('d:href', '', ns)
            prop      = response.find('d:propstat/d:prop', ns)
            if prop is None:
                continue
            restype   = prop.find('d:resourcetype/d:collection', ns)
            is_dir    = restype is not None
            name      = href.rstrip('/').split('/')[-1]
            etag      = (prop.findtext('d:getetag', '', ns) or '').strip('"')
            ctype     = prop.findtext('d:getcontenttype', '', ns) or ''
            items.append({'name': name, 'href': href,
                          'is_dir': is_dir, 'etag': etag,
                          'content_type': ctype})
        return items

    def collect_files(self, path: str = '') -> list[dict]:
        """Recursively collects all image files."""
        result = []
        try:
            items = self.list_remote(path)
        except Exception as e:
            log.warning(f'WebDAV PROPFIND failed for "{path}": {e}')
            return result

        for item in items[1:]:   # items[0] is the folder itself
            if item['is_dir']:
                sub = item['href'].split(self.url.split('//')[1].split('/')[0])[-1]
                result.extend(self.collect_files(sub))
            else:
                ext = Path(item['name']).suffix.lower()
                if ext in SUPPORTED:
                    result.append(item)
        return result

    def sync(self, existing: dict[str, Path], max_gb: float,
             delete_removed: bool, transcode_enabled: bool = False,
             transcode_mode: str = 'hardware') -> tuple[int, int, int]:
        added = removed = skipped = 0

        if self.min_resolution_px:
            log.info(f'Minimum resolution active: shorter side >= {self.min_resolution_px}px')
            # Remove images already in the cache that fall below the
            # minimum resolution (which may have been enabled or raised
            # afterwards) from the cache. Image.open() only reads the
            # header for this, no full decode - negligible cost even with
            # many files.
            for name, path in list(existing.items()):
                if path.suffix.lower() not in SUPPORTED_IMAGES:
                    continue
                if self._below_min_resolution(path):
                    path.unlink(missing_ok=True)
                    (CACHE_DIR / f'.{name}.etag').unlink(missing_ok=True)
                    del existing[name]
                    log.info(f'Removed (below minimum resolution): {name}')
                    removed += 1

        if self.folders:
            remote_files = []
            for folder in self.folders:
                remote_files.extend(self.collect_files(folder))
        else:
            remote_files = self.collect_files()

        # Photos first, videos last (stable sort - order within each group
        # is otherwise unchanged): videos are the slow part of a sync run
        # (local transcode via _transcode_video_if_needed, see below), so
        # this gets new photos into the cache and visible in the slideshow
        # as early as possible instead of interleaved behind whatever
        # videos happen to come first in the server's listing.
        remote_files.sort(key=lambda rf: Path(rf['name']).suffix.lower() in SUPPORTED_VIDEOS)

        # Determine the running cache size once instead of rescanning the
        # entire cache folder for every file (O(n) instead of O(n^2) for
        # large libraries - saves noticeable CPU/IO on the slow SD card).
        total_bytes = cache_size_gb() * (1024 ** 3)
        limit_bytes = max_gb * (1024 ** 3)

        skipped_low_res = 0
        remote_names = set()
        for rf in remote_files:
            # Derive a unique cache filename from the FULL WebDAV path, not
            # just the base name: cameras/phones often assign filenames
            # following a generic scheme (IMG_0001.JPG, DSC_0001.JPG, ...)
            # that repeats across different folders/time periods. With only
            # the base name as the cache key, two completely different
            # photos from two folders would collide on the same local
            # filename - the second sync pass would then simply overwrite
            # the first photo, which would irrevocably disappear from the
            # rotation (feels like "it keeps showing the same images", even
            # though in reality part of the library never made it into the
            # cache).
            path_hash = hashlib.md5(rf['href'].encode('utf-8')).hexdigest()[:10]
            safe_name = f'{path_hash}_{rf["name"]}'
            remote_names.add(safe_name)

            local_path   = CACHE_DIR / safe_name
            etag_file    = CACHE_DIR / f'.{safe_name}.etag'
            lowres_marker = CACHE_DIR / f'.{safe_name}.lowres'

            # Check whether the file is new or changed
            if local_path.exists() and etag_file.exists():
                cached_etag = etag_file.read_text().strip()
                if cached_etag == rf['etag'] and rf['etag']:
                    # Unchanged, but if a previous sync had to defer this
                    # video's transcode (slideshow was playing at the
                    # time - see _transcode_video_if_needed), retry it now
                    # instead of silently skipping it forever.
                    if transcode_enabled and Path(safe_name).suffix.lower() in SUPPORTED_VIDEOS \
                            and _needs_transcode_marker(local_path).exists():
                        pre_size = local_path.stat().st_size
                        if _transcode_video_if_needed(local_path, transcode_mode):
                            total_bytes += local_path.stat().st_size - pre_size
                    skipped += 1
                    continue

            # Was this file (with an unchanged ETag) already marked as too
            # low resolution? Then don't download it again just to discard
            # it once more afterwards.
            if self.min_resolution_px and lowres_marker.exists():
                marked_etag = lowres_marker.read_text().strip()
                if marked_etag == rf['etag'] and rf['etag']:
                    skipped_low_res += 1
                    continue

            if total_bytes >= limit_bytes:
                log.warning(f'Cache limit reached, skipping {safe_name}')
                continue

            # Download (atomic via .part file + rename, see _download_atomic)
            try:
                dl_url = self.url + '/' + rf['name'] if '/' not in rf['href'] \
                         else rf['href'] if rf['href'].startswith('http') \
                         else f"https://{self.url.split('//')[1].split('/')[0]}{rf['href']}"
                resp = self.session.get(dl_url, timeout=60, stream=True)
                resp.raise_for_status()
                old_size = local_path.stat().st_size if local_path.exists() else 0
                new_size = _download_atomic(resp, local_path)
                total_bytes += new_size - old_size

                # Only checkable AFTER the download: unlike the Immich
                # metadata, WebDAV/PROPFIND doesn't provide image
                # resolution in advance, so filtering can't happen before
                # the download here as it does for Immich.
                if Path(safe_name).suffix.lower() in SUPPORTED_IMAGES \
                        and self._below_min_resolution(local_path):
                    local_path.unlink(missing_ok=True)
                    total_bytes -= new_size
                    if rf['etag']:
                        lowres_marker.write_text(rf['etag'])
                    etag_file.unlink(missing_ok=True)
                    log.info(f'Discarded due to minimum resolution: {safe_name}')
                    skipped_low_res += 1
                    continue

                lowres_marker.unlink(missing_ok=True)
                if Path(safe_name).suffix.lower() in SUPPORTED_IMAGES:
                    pre_size = local_path.stat().st_size
                    if _downscale_if_needed(local_path):
                        total_bytes += local_path.stat().st_size - pre_size
                elif transcode_enabled and Path(safe_name).suffix.lower() in SUPPORTED_VIDEOS:
                    pre_size = local_path.stat().st_size
                    if _transcode_video_if_needed(local_path, transcode_mode):
                        total_bytes += local_path.stat().st_size - pre_size
                if rf['etag']:
                    etag_file.write_text(rf['etag'])
                log.info(f'Downloaded: {safe_name}')
                added += 1
            except Exception as e:
                log.error(f'Download error {safe_name}: {e}')
                local_path.with_name(local_path.name + '.part').unlink(missing_ok=True)

        if skipped_low_res:
            log.info(f'{skipped_low_res} file(s) skipped due to minimum resolution')
            skipped += skipped_low_res

        if delete_removed:
            for name, path in list(existing.items()):
                if name not in remote_names:
                    path.unlink(missing_ok=True)
                    (CACHE_DIR / f'.{name}.etag').unlink(missing_ok=True)
                    _needs_transcode_marker(path).unlink(missing_ok=True)
                    log.info(f'Deleted (no longer on server): {name}')
                    removed += 1
            # Clean up orphaned .lowres markers (file deleted on server)
            for marker in CACHE_DIR.glob('.*.lowres'):
                orig_name = marker.name[1:-len('.lowres')]
                if orig_name not in remote_names:
                    marker.unlink(missing_ok=True)

        return added, removed, skipped

# ---------------------------------------------------------------------------
# Immich Sync
# ---------------------------------------------------------------------------

class ImmichSync:
    def __init__(self, cfg: dict):
        self.base_url  = cfg['url'].rstrip('/')
        self.api_key   = cfg['api_key']
        self.albums    = cfg.get('albums', [])   # empty = all photos
        self.all_photos = cfg.get('all_photos', True)
        # Assets whose shorter side is below this value aren't downloaded
        # at all (0/None = no filter). Fights pixelated blur from
        # upscaling originals that are already low resolution themselves
        # (old phone photos, WhatsApp compression, screenshots) - the
        # Immich "original" file is always already the best possible
        # quality, there's no further "quality tier" beyond it to choose.
        self.min_resolution_px = cfg.get('min_resolution_px', 0) or 0
        self.headers   = {'x-api-key': self.api_key,
                          'Accept': 'application/json'}
        # A shared, keep-alive session instead of a bare requests.get/post
        # per call - a sync run can touch hundreds of assets, and without
        # this every single one of those (metadata calls plus every photo/
        # video download) would open its own fresh TCP connection and, for
        # an HTTPS Immich instance, redo the TLS handshake from scratch -
        # real, avoidable CPU/latency cost on a weak CPU repeated hundreds
        # of times per sync. NextcloudSync already did this; ImmichSync
        # didn't.
        self.session   = requests.Session()
        self.session.headers.update(self.headers)
        # Logged at most once per sync run (see sync() below) - avoids
        # spamming one warning per asset when the underlying cause (an API
        # key without the "asset.view" permission) affects the whole
        # library at once rather than being asset-specific.
        self._warned_403_fallback = False

    def _get(self, endpoint: str, **kwargs) -> dict | list:
        resp = self.session.get(
            f'{self.base_url}{endpoint}',
            timeout=30,
            **kwargs
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, json_body: dict, **kwargs) -> dict | list:
        resp = self.session.post(
            f'{self.base_url}{endpoint}',
            json=json_body,
            timeout=30,
            **kwargs
        )
        resp.raise_for_status()
        return resp.json()

    def get_assets(self) -> list[dict]:
        """Returns all relevant assets.

        Runs entirely through POST /api/search/metadata: current Immich
        versions have removed GET /api/assets (listing all photos), and
        GET /api/albums/{id} now only returns metadata + assetCount, no
        longer an embedded asset list. The search endpoint covers both
        cases - without an albumIds filter the whole library, with a
        filter only the assets of the given albums (OR-combined if there
        are several).
        """
        album_ids = None
        if self.albums:
            log.info(f'Album filter active, configured: {self.albums}')
            all_albums = self._get('/api/albums')
            name_to_id = {a['albumName']: a['id'] for a in all_albums}
            log.info(f'Albums visible to this API key ({len(name_to_id)}): '
                     f'{sorted(name_to_id) or "(none)"}')
            album_ids = []
            for album_name in self.albums:
                aid = name_to_id.get(album_name)
                if not aid:
                    log.warning(f'Immich album not found: "{album_name}" '
                               f'(capitalization and whitespace must match exactly)')
                    continue
                log.info(f'Immich album found: "{album_name}" -> {aid}')
                album_ids.append(aid)
            if not album_ids:
                log.warning('No configured album found - sync returns 0 assets')
                return []
        else:
            log.info('No album filter configured (immich.albums is empty) - '
                     'searching the ENTIRE library of the API key account. '
                     'Note: for a dedicated/shared account that does not own '
                     'any photos itself, this always returns 0 assets - in '
                     'that case albums: [...] must be set.')

        if self.min_resolution_px:
            log.info(f'Minimum resolution active: shorter side >= {self.min_resolution_px}px')

        assets = []
        skipped_low_res = 0
        page = 1
        size = 500
        while True:
            body = {'page': page, 'size': size}
            if album_ids:
                body['albumIds'] = album_ids
            data  = self._post('/api/search/metadata', body)
            items = data.get('assets', {}).get('items', [])
            if not items:
                break
            # Only images and videos (no audio/other)
            for a in items:
                if a.get('type') not in ('IMAGE', 'VIDEO'):
                    continue
                if self._below_min_resolution(a):
                    skipped_low_res += 1
                    continue
                assets.append(a)
            if len(items) < size:
                break
            page += 1

        if skipped_low_res:
            log.info(f'{skipped_low_res} asset(s) skipped due to minimum resolution')
        return assets

    def _below_min_resolution(self, asset: dict) -> bool:
        """True if the asset is below the configured minimum resolution.

        Videos are never filtered out (resolution is less critical there,
        and the width/height semantics are also less consistent). If
        width/height is missing (some older/imported assets don't have it
        populated), the asset is NOT filtered out when in doubt, so as not
        to accidentally lose valid photos.
        """
        if not self.min_resolution_px or asset.get('type') != 'IMAGE':
            return False
        w, h = asset.get('width'), asset.get('height')
        if not w or not h:
            return False
        return min(w, h) < self.min_resolution_px

    def sync(self, existing: dict[str, Path], max_gb: float,
             delete_removed: bool, transcode_enabled: bool = False,
             transcode_mode: str = 'hardware') -> tuple[int, int, int]:
        added = removed = skipped = 0

        try:
            assets = self.get_assets()
        except Exception as e:
            log.error(f'Immich API error: {e}')
            return 0, 0, 0

        # Photos first, videos last (stable sort - order within each group
        # is otherwise unchanged): videos are the slow part of a sync run
        # (local transcode via _transcode_video_if_needed, see below), so
        # this gets new photos into the cache and visible in the slideshow
        # as early as possible instead of interleaved behind whatever
        # videos happen to come first in Immich's listing.
        assets.sort(key=lambda a: a.get('type') != 'IMAGE')

        total_bytes = cache_size_gb() * (1024 ** 3)
        limit_bytes = max_gb * (1024 ** 3)

        remote_ids = set()
        for asset in assets:
            asset_id   = asset['id']
            asset_type = asset.get('type', 'IMAGE')
            orig_name  = asset.get('originalFileName', f'{asset_id}.jpg')

            # Photos: request Immich's own pre-generated "preview" JPEG
            # (shortest edge ~1440px, server config permitting) instead of
            # the original file. Normalizes every source format (HEIC,
            # RAW, huge PNG screenshots, ...) to a plain, right-sized JPEG
            # server-side - the original's extension no longer applies,
            # so it's always cached as .jpg here.
            #
            # Videos: request Immich's transcoded-video endpoint. Immich
            # automatically transcodes anything that isn't already H.264
            # to H.264/AAC in the background shortly after upload, which
            # is exactly the format this Pi's hardware decoder needs -
            # remuxed to MP4 unless the source container already was
            # mp4/mov/ogg/webm, so .mp4 is the closest, simplest cache
            # extension to use consistently (mpv detects the real
            # container from the file content regardless).
            # Caveat (accepted, not handled here): this endpoint just
            # serves whatever is currently on disk at Immich's end - if
            # the background transcode job hasn't finished yet (e.g. a
            # video synced within moments of being uploaded), we'd get
            # the untranscoded original instead, and since an already-
            # cached file is never re-checked, it would stay that way.
            if asset_type == 'IMAGE':
                ext = '.jpg'
                fetch_path = f'/api/assets/{asset_id}/thumbnail?size=preview'
            else:
                ext = '.mp4'
                fetch_path = f'/api/assets/{asset_id}/video/playback'

            safe_name  = f'{asset_id}{ext}'
            remote_ids.add(safe_name)

            local_path = CACHE_DIR / safe_name

            if local_path.exists():
                # Already cached, but if a previous sync had to defer this
                # video's transcode (slideshow was playing at the time -
                # see _transcode_video_if_needed), retry it now instead of
                # silently skipping it forever.
                if transcode_enabled and asset_type == 'VIDEO' \
                        and _needs_transcode_marker(local_path).exists():
                    pre_size = local_path.stat().st_size
                    if _transcode_video_if_needed(local_path, transcode_mode):
                        total_bytes += local_path.stat().st_size - pre_size
                skipped += 1
                continue

            if total_bytes >= limit_bytes:
                log.warning(f'Cache limit reached, skipping {safe_name}')
                continue

            try:
                resp = self.session.get(
                    f'{self.base_url}{fetch_path}',
                    timeout=120,
                    stream=True
                )
                if resp.status_code == 403:
                    # The API key most likely only has the "asset.download"
                    # permission, not "asset.view" - the latter is required
                    # for /thumbnail and /video/playback but not /original.
                    # Rather than failing every single asset in the
                    # library over a permissions gap, fall back to the
                    # original file (losing the format-normalization/
                    # smaller-size benefit, but still syncing).
                    if not self._warned_403_fallback:
                        log.warning(
                            f'{fetch_path} -> 403 Forbidden - the API key '
                            f'likely lacks the "asset.view" permission '
                            f'(only /original worked before, which only '
                            f'needs "asset.download"). Falling back to '
                            f'/original for this and any further affected '
                            f'assets this run - fix the API key\'s '
                            f'permissions in Immich to get the smaller '
                            f'preview/transcoded variants back.'
                        )
                        self._warned_403_fallback = True
                    fetch_path = f'/api/assets/{asset_id}/original'
                    resp = self.session.get(
                        f'{self.base_url}{fetch_path}',
                        timeout=120,
                        stream=True
                    )
                resp.raise_for_status()
                new_size = _download_atomic(resp, local_path)
                total_bytes += new_size
                if asset_type == 'IMAGE':
                    pre_size = local_path.stat().st_size
                    if _downscale_if_needed(local_path):
                        total_bytes += local_path.stat().st_size - pre_size
                elif transcode_enabled:
                    pre_size = local_path.stat().st_size
                    if _transcode_video_if_needed(local_path, transcode_mode):
                        total_bytes += local_path.stat().st_size - pre_size
                log.info(f'Downloaded: {orig_name} -> {safe_name}')
                added += 1
            except Exception as e:
                log.error(f'Download error {safe_name}: {e}')
                local_path.with_name(local_path.name + '.part').unlink(missing_ok=True)
                local_path.unlink(missing_ok=True)

        if delete_removed:
            for name, path in list(existing.items()):
                if name not in remote_ids:
                    path.unlink(missing_ok=True)
                    _needs_transcode_marker(path).unlink(missing_ok=True)
                    log.info(f'Deleted: {name}')
                    removed += 1

        return added, removed, skipped

# ---------------------------------------------------------------------------
# Connection test (also used by the web UI)
# ---------------------------------------------------------------------------

def test_nextcloud(cfg: dict) -> tuple[bool, str]:
    """Tests the Nextcloud connection without syncing."""
    try:
        session = requests.Session()
        session.auth = HTTPBasicAuth(cfg['username'], cfg['password'])
        session.verify = cfg.get('verify_ssl', True)
        resp = session.request(
            'PROPFIND', cfg['url'],
            data='<d:propfind xmlns:d="DAV:"><d:prop/></d:propfind>',
            headers={'Depth': '0', 'Content-Type': 'application/xml'},
            timeout=10
        )
        if resp.status_code in (200, 207):
            return True, 'Connection successful'
        return False, f'HTTP {resp.status_code}'
    except Exception as e:
        return False, str(e)


def test_immich(cfg: dict) -> tuple[bool, str]:
    """Tests the Immich connection without syncing."""
    try:
        resp = requests.get(
            f"{cfg['url'].rstrip('/')}/api/server/about",
            headers={'x-api-key': cfg['api_key']},
            timeout=10
        )
        if resp.status_code == 200:
            data    = resp.json()
            version = data.get('version', '?')
            return True, f'Immich {version} - connection OK'
        return False, f'HTTP {resp.status_code} - check API key'
    except Exception as e:
        return False, str(e)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    log.info('=== Sync started ===')

    if _slideshow_playing_video():
        log.info('Slideshow is currently playing a video - skipping this sync '
                 'cycle entirely (hardware decode + the local video transcode '
                 'step both use the same limited CMA memory/codec block, and '
                 'this device is already tight on both) - will retry next '
                 'timer interval')
        sys.exit(0)

    try:
        cfg = load_config()
    except Exception as e:
        log.error(f'Config not readable: {e}')
        sys.exit(1)

    source       = cfg.get('source', 'nextcloud')
    sync_cfg     = cfg.get('sync', {})
    max_gb       = sync_cfg.get('max_cache_size_gb', 5)
    delete_rem   = sync_cfg.get('delete_removed', True)
    # Off by default - see the module-level comment on VIDEO_MAX_SHORT_SIDE_PX
    # for why this exists, and the web UI's Display tab for the toggle.
    transcode_en = sync_cfg.get('video_transcode_enabled', False)
    # Explicit choice, no automatic fallback during normal operation - see
    # _transcode_video_if_needed()'s docstring. The web UI already hides/
    # disables "hardware" when hw.hardware_h264_encoder_available() is
    # False, so this should normally always be a value the device can
    # actually honor.
    transcode_mode = sync_cfg.get('video_transcode_mode', 'hardware')

    # "Media processing" (web UI's Display tab): local (default, uses
    # video_transcode_mode above for video, plain PIL for photos) or
    # remote (both photos and video get sent to a separate machine - see
    # REMOTE's module-level comment and remote-transcode-service/ in this
    # repo). Populates the module-level REMOTE dict that
    # _downscale_if_needed() and _transcode_video_if_needed() read,
    # instead of threading two more parameters through every sync() call
    # site in both NextcloudSync and ImmichSync.
    media_processing_mode = sync_cfg.get('media_processing_mode', 'local')
    REMOTE['enabled'] = (media_processing_mode == 'remote')
    REMOTE['url']     = sync_cfg.get('remote_service_url', '')
    REMOTE['api_key'] = sync_cfg.get('remote_service_api_key', '')
    if REMOTE['enabled'] and not REMOTE['url']:
        log.warning('media_processing_mode is "remote" but remote_service_url is '
                    'empty - remote calls will fail until this is set in the web UI')

    # Check network
    if source == 'nextcloud':
        src_cfg = cfg.get('nextcloud', {})
        url     = src_cfg.get('url', '')
    else:
        src_cfg = cfg.get('immich', {})
        url     = src_cfg.get('url', '')

    if not url:
        log.error(f'No URL configured for source "{source}"')
        sys.exit(1)

    host, port = extract_host_port(url)
    if not is_reachable(host, port):
        log.info(f'Host {host}:{port} not reachable - sync skipped (offline mode)')
        sys.exit(0)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_orphaned_downloads()
    existing = cached_files()
    log.info(f'Cache: {len(existing)} files / {cache_size_gb():.2f} GB')
    log.info(f'Source: {source.upper()} @ {url}')
    # Reflects where encoding actually happens - previously always printed
    # transcode_mode (hardware/software) even when media_processing_mode
    # was "remote", which was flat-out wrong and confused debugging a
    # real issue (grepping this log line looked fine while videos were
    # silently never being transcoded for an unrelated reason).
    if transcode_en:
        backend = f'remote ({REMOTE["url"]})' if REMOTE['enabled'] else transcode_mode
        log.info(f'Video transcoding: enabled ({backend})')
    else:
        log.info('Video transcoding: disabled')

    start = time.time()
    try:
        if source == 'nextcloud':
            syncer = NextcloudSync(src_cfg)
        else:
            syncer = ImmichSync(src_cfg)

        added, removed, skipped = syncer.sync(existing, max_gb, delete_rem, transcode_en, transcode_mode)
    except Exception as e:
        log.error(f'Sync error: {e}', exc_info=True)
        sys.exit(1)

    evicted = enforce_cache_limit(max_gb)
    if evicted:
        log.info(f'{evicted} file(s) evicted due to cache limit')
        removed += evicted

    elapsed = time.time() - start
    log.info(f'Sync completed in {elapsed:.1f}s - '
             f'+{added} new, -{removed} deleted, {skipped} unchanged')
    log.info(f'Cache now: {len(cached_files())} files / {cache_size_gb():.2f} GB')


if __name__ == '__main__':
    main()
