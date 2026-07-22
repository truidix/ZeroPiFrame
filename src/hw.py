"""
Small, dependency-free hardware helpers shared between webui.py (deciding
whether to show the audio toggle / hardware-transcode option) and
slideshow.py/sync.py (deciding whether to actually pass audio flags to a
video player / attempt hardware transcoding). Deliberately has no pygame/
Flask imports so any of these processes can import it cheaply.
"""

import glob
import logging
import subprocess

log = logging.getLogger(__name__)


def _parse_edid_audio_capable(edid: bytes) -> bool:
    """Parses a raw EDID blob and returns whether it declares HDMI audio
    capability.

    HDMI (and DVI/DisplayPort-over-HDMI) audio capability is only ever
    declared in a CTA-861 ("CEA-861") extension block, never in the base
    128-byte EDID block. Byte 126 of the base block says how many
    extension blocks follow; a display with byte 126 == 0 has no
    extension block at all and therefore cannot declare any audio
    capability, full stop - this is the exact case confirmed on this
    project's own test hardware (a 2018 "B24W-7 LED" monitor with EDID
    Extension Flag = 0), which is why aplay/ZeroPlay/mpv all fail to open
    ANY audio device against it regardless of software configuration.

    Within a CTA-861 block, audio capability is declared either via the
    "Basic Audio" flag (byte 3, bit 6 - implies 2-channel 32/44.1/48kHz
    LPCM support without needing an explicit descriptor) or via an
    explicit Audio Data Block in the Data Block Collection (bytes 4 up to
    the DTD offset in byte 2) - a block whose header byte's top 3 bits
    (the tag) equal 1.
    """
    if len(edid) < 128:
        return False
    ext_count = edid[126]
    if ext_count == 0:
        return False
    for i in range(ext_count):
        start = 128 * (i + 1)
        block = edid[start:start + 128]
        if len(block) < 128 or block[0] != 0x02:
            continue  # not a CTA-861 extension block
        dtd_offset = block[2]
        if dtd_offset > 4 and (block[3] & 0x40):
            return True  # "Basic Audio" flag
        pos = 4
        while dtd_offset > 4 and pos < dtd_offset:
            header = block[pos]
            tag = (header >> 5) & 0x07
            length = header & 0x1f
            if tag == 1:  # Audio Data Block
                return True
            pos += 1 + length
    return False


def hdmi_audio_supported() -> bool:
    """Best-effort check of whether the currently connected HDMI display
    actually declares audio capability, independent of any specific video
    player or ALSA configuration - reads the raw EDID directly from
    /sys/class/drm/*/edid.

    Returns True (assume supported) if no readable/non-empty EDID was
    found at all - a false negative here would needlessly disable audio
    someone might actually have; a false positive just means audio might
    still fail to open, exactly like the situation this function exists
    to avoid repeating, rather than a new regression.

    If at least one connected display's EDID was read and NONE of them
    declare audio capability, returns False - this is treated as
    authoritative, since it was confirmed on real hardware that a
    display with no audio-capable EDID rejects every audio open attempt
    outright (ALSA error 524/ENOTSUPP), regardless of the requested
    format/rate/channels.
    """
    try:
        found_any_edid = False
        for path in glob.glob('/sys/class/drm/*/edid'):
            try:
                with open(path, 'rb') as f:
                    data = f.read()
            except Exception:
                continue
            if not data:
                continue  # empty EDID file = connector exists but nothing plugged in
            found_any_edid = True
            if _parse_edid_audio_capable(data):
                return True
        return not found_any_edid
    except Exception as e:
        log.warning(f'Could not determine HDMI audio capability from EDID: {e}')
        return True


_hw_encoder_available_cache = None


def hardware_h264_encoder_available() -> bool:
    """Checks whether ffmpeg's h264_v4l2m2m ENCODER (distinct from the
    decoder used for actual video playback) is genuinely usable on this
    device, by running a trivial real 1-frame encode rather than just
    grepping `ffmpeg -encoders` - that would report the encoder as
    available purely because ffmpeg was compiled with V4L2 M2M support,
    even if the underlying /dev/video11-style encode device or driver
    isn't actually present/working on this specific board. Confirmed
    during this project's own debugging that the encoder can fail for
    reasons unrelated to compile-time support (resource contention,
    driver quirks), so an actual probe encode is the only reliable check.

    Cached after the first call - this device's hardware capability
    doesn't change at runtime, so there's no reason to repeat a
    subprocess spawn on every check.
    """
    global _hw_encoder_available_cache
    if _hw_encoder_available_cache is not None:
        return _hw_encoder_available_cache
    try:
        result = subprocess.run(
            ['ffmpeg', '-y', '-f', 'lavfi', '-i', 'color=c=black:s=64x64:d=0.1',
             '-frames:v', '1', '-c:v', 'h264_v4l2m2m', '-f', 'null', '-'],
            capture_output=True, timeout=15)
        _hw_encoder_available_cache = (result.returncode == 0)
    except Exception as e:
        log.warning(f'Could not probe hardware H.264 encoder availability: {e}')
        _hw_encoder_available_cache = False
    return _hw_encoder_available_cache
