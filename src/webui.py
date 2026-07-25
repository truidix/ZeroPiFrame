#!/usr/bin/env python3
"""
ZeroPiFrame Web UI
Reachable at http://zeropiframe.local:8080
"""

import os
import re
import sys
import json
import subprocess
import logging
from pathlib import Path
from datetime import datetime

import yaml
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory

# Import sync.py from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from sync import test_nextcloud, test_immich, cached_files, cache_size_gb
from hw import hdmi_audio_supported, hardware_h264_encoder_available
import i18n

CONFIG_PATH  = Path('/opt/zeropiframe/config.yaml')
CACHE_DIR    = Path('/var/lib/zeropiframe/cache')
LOG_FILE     = Path('/var/log/zeropiframe-sync.log')
CURRENT_STATE_PATH = Path('/var/lib/zeropiframe/current.json')
LAST_UPDATE_PATH   = Path('/var/lib/zeropiframe/last_update.json')

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [webui] %(levelname)s: %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates', static_folder='static')

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def save_config(cfg: dict):
    with open(CONFIG_PATH, 'w') as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
    # Notify the slideshow about the new config (no restart needed)
    subprocess.run(['pkill', '-HUP', '-f', 'slideshow.py'],
                   capture_output=True)


def current_language(cfg: dict | None = None) -> str:
    """Reads the configured UI language, falling back to English if unset
    or if the configured code has no matching translations file."""
    cfg = cfg if cfg is not None else load_config()
    return i18n.normalize_language(cfg.get('language'))


@app.context_processor
def inject_i18n():
    """Makes t() (translation lookup) and the list of available languages
    available in every template automatically, without having to pass them
    explicitly to every single render_template() call."""
    lang = current_language()
    return {
        't': i18n.make_translator(lang),
        'current_lang': lang,
        'available_languages': i18n.available_languages(),
    }


@app.route('/api/set_language', methods=['POST'])
def api_set_language():
    t = i18n.make_translator(current_language())
    data = request.get_json(silent=True) or {}
    lang = data.get('language', '')
    if lang not in i18n.available_languages():
        return jsonify({'ok': False, 'message': t('api.unknown_language', lang=lang)})
    cfg = load_config()
    cfg['language'] = lang
    save_config(cfg)
    return jsonify({'ok': True})


def get_service_status(name: str) -> str:
    r = subprocess.run(['systemctl', 'is-active', name],
                       capture_output=True, text=True)
    return r.stdout.strip()


def is_service_enabled(name: str) -> bool:
    """True if the service/timer starts automatically on boot.

    Used to correctly render the auto-sync toggle in the web UI,
    regardless of whether the timer happens to be active/inactive right now.
    """
    r = subprocess.run(['systemctl', 'is-enabled', name],
                       capture_output=True, text=True)
    return r.stdout.strip() == 'enabled'


# Example line from sync.py:
# 2026-07-19 18:26:58,543 [sync] INFO: Sync completed in 0.1s - +0 new, -0 deleted, 0 unchanged
_LAST_SYNC_RE = re.compile(
    r'^(?P<ts>[\d-]+ [\d:]+),\d+ \[sync\] INFO: Sync completed in '
    r'(?P<duration>[\d.]+)s - \+(?P<added>\d+) new, -(?P<removed>\d+) deleted, '
    r'(?P<skipped>\d+) unchanged'
)


def get_last_sync() -> dict | None:
    """Reads the last 'Sync completed' line and returns the timestamp,
    duration, and new/deleted/unchanged counters as structured data
    (instead of just the raw timestamp string as before)."""
    try:
        lines = LOG_FILE.read_text().splitlines()
        for line in reversed(lines):
            m = _LAST_SYNC_RE.match(line)
            if m:
                return {
                    'timestamp': m.group('ts'),
                    'duration':  float(m.group('duration')),
                    'added':     int(m.group('added')),
                    'removed':   int(m.group('removed')),
                    'skipped':   int(m.group('skipped')),
                }
        return None
    except Exception:
        return None


def get_current_media() -> dict | None:
    """Reads whatever slideshow.py last recorded as currently on screen
    (see _write_current_state() there). Returns None if the slideshow
    hasn't written anything yet (e.g. not started), or the file is
    missing/unreadable for any other reason - the status page just shows
    "no media yet" in that case rather than erroring."""
    try:
        return json.loads(CURRENT_STATE_PATH.read_text())
    except Exception:
        return None


def get_last_update() -> dict | None:
    """Reads the outcome of the last "Check for updates" run (written by
    update.sh, generated by install.sh - see api_update() below for why
    this file, rather than the request/response, is how the result gets
    back to the page). None if an update has never been run."""
    try:
        return json.loads(LAST_UPDATE_PATH.read_text())
    except Exception:
        return None


def get_next_sync() -> str:
    r = subprocess.run(
        ['systemctl', 'show', 'zeropiframe-sync.timer', '--property=NextElapseUSecRealtime'],
        capture_output=True, text=True
    )
    # Simplified output
    for line in r.stdout.splitlines():
        if 'NextElapseUSecRealtime' in line:
            val = line.split('=')[1].strip()
            if val and val != '0':
                return val
    return 'Unknown'

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    cfg          = load_config()
    slide_status = get_service_status('zeropiframe-slideshow')
    sync_status  = get_service_status('zeropiframe-sync')
    timer_status = get_service_status('zeropiframe-sync.timer')
    sync_running = sync_status in ('active', 'activating')
    sync_enabled = is_service_enabled('zeropiframe-sync.timer')
    slideshow_enabled = is_service_enabled('zeropiframe-slideshow')

    try:
        n_images = len(cached_files())
        size_gb  = cache_size_gb()
    except Exception:
        n_images = 0
        size_gb  = 0.0

    return render_template('index.html',
        cfg=cfg,
        slide_status=slide_status,
        sync_status=sync_status,
        timer_status=timer_status,
        sync_running=sync_running,
        sync_enabled=sync_enabled,
        slideshow_enabled=slideshow_enabled,
        n_images=n_images,
        size_gb=f'{size_gb:.2f}',
        last_sync=get_last_sync(),
        current_media=get_current_media(),
        last_update=get_last_update(),
    )


@app.route('/media/<path:filename>')
def media_file(filename):
    """Serves a single cached photo/video file as-is, for the status
    page's "currently displayed" preview. send_from_directory guards
    against path traversal (e.g. "../") on its own, and also handles
    conditional/range requests, which the browser's <video> tag needs to
    be able to seek/play an mp4 properly."""
    return send_from_directory(CACHE_DIR, filename)


@app.route('/sources', methods=['GET', 'POST'])
def sources():
    cfg = load_config()

    if request.method == 'POST':
        data   = request.form
        source = data.get('source', 'nextcloud')

        cfg['source'] = source

        # Nextcloud
        cfg.setdefault('nextcloud', {})
        cfg['nextcloud']['url']        = data.get('nc_url', '').strip()
        cfg['nextcloud']['username']   = data.get('nc_user', '').strip()
        if data.get('nc_pass'):           # Only overwrite password if a new one was entered
            cfg['nextcloud']['password'] = data.get('nc_pass', '')
        cfg['nextcloud']['verify_ssl'] = 'nc_verify_ssl' in data
        folders = [f.strip() for f in data.get('nc_folders', '').split('\n') if f.strip()]
        cfg['nextcloud']['folders']    = folders
        cfg['nextcloud']['min_resolution_px'] = int(data.get('nc_min_res', 0) or 0)

        # Immich
        cfg.setdefault('immich', {})
        cfg['immich']['url']     = data.get('im_url', '').strip()
        if data.get('im_key'):
            cfg['immich']['api_key'] = data.get('im_key', '')
        albums = [a.strip() for a in data.get('im_albums', '').split('\n') if a.strip()]
        cfg['immich']['albums']      = albums
        cfg['immich']['all_photos']  = not albums
        cfg['immich']['min_resolution_px'] = int(data.get('im_min_res', 0) or 0)

        save_config(cfg)
        return redirect(url_for('sources') + '?saved=1')

    return render_template('sources.html', cfg=cfg,
                           saved=request.args.get('saved'))


_audio_supported_cache = None


def _cached_hdmi_audio_supported() -> bool:
    """Same rationale/caching as slideshow.py's copy: the connected
    display doesn't change at runtime, so there's no need to re-read/
    re-parse the EDID on every single page load."""
    global _audio_supported_cache
    if _audio_supported_cache is None:
        _audio_supported_cache = hdmi_audio_supported()
    return _audio_supported_cache


@app.route('/slideshow', methods=['GET', 'POST'])
def slideshow():
    cfg = load_config()
    audio_supported = _cached_hdmi_audio_supported()

    if request.method == 'POST':
        data = request.form
        cfg.setdefault('slideshow', {})
        sl = cfg['slideshow']

        sl['interval_seconds']      = int(data.get('interval', 30))
        enabled = data.getlist('enabled_transitions')
        # Safety net in case the form accidentally had all transitions
        # deselected (can happen with JS errors/JS disabled): fall back
        # instead of saving a slideshow with no transition at all.
        sl['enabled_transitions']    = enabled or ['ken_burns']
        sl['transition']             = sl['enabled_transitions'][0]  # Legacy field, backward compatibility
        sl['transition_duration_ms'] = int(data.get('t_duration', 1500))
        sl['ken_burns_zoom']        = float(data.get('kb_zoom', 0.08))
        sl['shuffle']               = data.get('order') == 'shuffle'
        sl['fit_mode']              = data.get('fit_mode') if data.get('fit_mode') in ('contain', 'cover', 'smart') else 'contain'
        sl['smart_fit_percent']     = max(0, min(100, int(data.get('smart_fit_percent', 30) or 30)))
        sl['background_color']      = data.get('bg_color', '#000000')
        sl['video_enabled']         = 'video_enabled' in data
        # Forced off, regardless of what the form submitted, if the
        # connected display doesn't declare audio support at all - the
        # toggle is already hidden/disabled client-side in this case (see
        # slideshow.html), this is the server-side enforcement of the
        # same rule so a stale page load or a hand-crafted request can't
        # re-enable something that can never actually produce sound.
        sl['video_audio']           = audio_supported and 'video_audio' in data
        sl['video_player']          = data.get('video_player', 'mpv') if data.get('video_player') in ('mpv', 'vlc', 'zeroplay') else 'mpv'
        sl['show_photo_info']      = 'show_photo_info' in data

        save_config(cfg)
        return redirect(url_for('slideshow') + '?saved=1')

    return render_template('slideshow.html', cfg=cfg,
                           audio_supported=audio_supported,
                           saved=request.args.get('saved'))


@app.route('/display', methods=['GET', 'POST'])
def display():
    cfg = load_config()
    # hardware_h264_encoder_available() caches itself (see hw.py) - this
    # device's capability can't change at runtime, so no need for a
    # webui-local cache wrapper on top of it (unlike the EDID audio check,
    # which lives in hw.py without its own module-level cache and so gets
    # one here in webui.py instead).
    hw_capable = hardware_h264_encoder_available()

    if request.method == 'POST':
        data = request.form
        cfg.setdefault('display', {})
        cfg.setdefault('sync', {})

        schedule_enabled = 'schedule_enabled' in data
        cfg['display']['on_time']  = data.get('on_time', '08:00') if schedule_enabled else ''
        cfg['display']['off_time'] = data.get('off_time', '23:00') if schedule_enabled else ''
        cfg['sync']['interval_minutes']   = int(data.get('sync_interval', 60))
        cfg['sync']['max_cache_size_gb']  = float(data.get('max_cache', 5))
        cfg['sync']['delete_removed']     = 'delete_removed' in data
        transcode_enabled = 'video_transcode_enabled' in data
        cfg['sync']['video_transcode_enabled'] = transcode_enabled
        # Explicit hardware/software choice - no automatic fallback during
        # normal use (see sync.py's _transcode_video_if_needed docstring).
        # Forced to "software" server-side if this device can't actually
        # do hardware encoding, mirroring the audio toggle's enforcement
        # pattern - the UI already hides/disables "Hardware" in that case,
        # so this is just a defensive backstop against stale form posts.
        requested_mode = data.get('video_transcode_mode', 'hardware')
        cfg['sync']['video_transcode_mode'] = requested_mode if hw_capable else 'software'

        # "Media processing": local (photos via PIL, video via the
        # hardware/software choice above) or remote (both sent to a
        # separate machine - see sync.py's REMOTE and
        # remote-transcode-service/ in this repo). The web UI only shows
        # this choice while "Re-encode videos locally" is checked, so it's
        # forced back to "local" server-side when that's off - same
        # defensive-backstop pattern as hw_capable above. This does NOT
        # affect the separate, always-on photo downscaling safety cap
        # (guards against OOM on huge phone photos, see sync.py's
        # MAX_DIMENSION_PX) - that keeps running locally regardless of
        # video_transcode_enabled, exactly as it always has.
        requested_media_mode = data.get('media_processing_mode', 'local')
        cfg['sync']['media_processing_mode'] = requested_media_mode if transcode_enabled else 'local'
        cfg['sync']['remote_service_url']     = data.get('remote_service_url', '').strip()
        cfg['sync']['remote_service_api_key'] = data.get('remote_service_api_key', '').strip()

        shutdown_enabled = 'shutdown_enabled' in data
        cfg['display']['shutdown_time'] = data.get('shutdown_time', '22:55') if shutdown_enabled else ''

        save_config(cfg)

        # Push the HDMI schedule, auto-shutdown, and sync interval to the
        # systemd timers - without this, changing these fields in the web
        # UI would have no effect on the timers actually running.
        _update_hdmi_timers(cfg)
        _update_shutdown_timer(cfg)
        _update_sync_interval(cfg)

        return redirect(url_for('display') + '?saved=1')

    return render_template('display.html', cfg=cfg, hw_capable=hw_capable,
                           saved=request.args.get('saved'))

# ---------------------------------------------------------------------------
# AJAX API
# ---------------------------------------------------------------------------

@app.route('/api/test_connection', methods=['POST'])
def api_test_connection():
    t = i18n.make_translator(current_language())
    data = request.get_json()
    kind = data.get('type')

    if kind == 'nextcloud':
        ok, msg = test_nextcloud({
            'url':        data.get('url', ''),
            'username':   data.get('username', ''),
            'password':   data.get('password', ''),
            'verify_ssl': data.get('verify_ssl', True),
        })
    elif kind == 'immich':
        ok, msg = test_immich({
            'url':     data.get('url', ''),
            'api_key': data.get('api_key', ''),
        })
    else:
        return jsonify({'ok': False, 'message': t('api.unknown_source_type')})

    return jsonify({'ok': ok, 'message': msg})


@app.route('/api/sync_now', methods=['POST'])
def api_sync_now():
    t = i18n.make_translator(current_language())
    # webui.py deliberately runs unprivileged; starting another system
    # service requires root. Without an interactive session, PolicyKit can't
    # prompt for a password ("Interactive authentication required"), so this
    # goes through the tightly scoped sudoers rule set up by install.sh.
    result = subprocess.run(
        ['sudo', 'systemctl', 'start', 'zeropiframe-sync'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return jsonify({'ok': True, 'message': t('api.sync_started')})
    return jsonify({'ok': False, 'message': result.stderr})


@app.route('/api/sync_stop', methods=['POST'])
def api_sync_stop():
    t = i18n.make_translator(current_language())
    # Aborts a sync run that's currently in progress. Since zeropiframe-sync
    # is a Type=oneshot service, "systemctl stop" cleanly terminates the
    # running Python process (SIGTERM) without touching the timer itself -
    # the next scheduled sync still runs normally.
    result = subprocess.run(
        ['sudo', 'systemctl', 'stop', 'zeropiframe-sync'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return jsonify({'ok': True, 'message': t('api.sync_stopped')})
    return jsonify({'ok': False, 'message': result.stderr})


@app.route('/api/sync_toggle', methods=['POST'])
def api_sync_toggle():
    """Enables/disables the scheduled auto-sync permanently (survives a
    reboot). The manual "Sync now" button keeps working independently of
    this."""
    t = i18n.make_translator(current_language())
    enabled = bool(request.get_json(silent=True) and request.get_json().get('enabled'))
    action  = 'enable' if enabled else 'disable'
    result = subprocess.run(
        ['sudo', '/opt/zeropiframe/apply-sync-enabled.sh', action],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return jsonify({'ok': False, 'message': result.stderr.strip()})
    return jsonify({'ok': True,
                    'message': t('api.auto_sync_enabled') if enabled else t('api.auto_sync_disabled')})


@app.route('/api/slideshow_start', methods=['POST'])
def api_slideshow_start():
    t = i18n.make_translator(current_language())
    result = subprocess.run(
        ['sudo', 'systemctl', 'start', 'zeropiframe-slideshow'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return jsonify({'ok': True, 'message': t('api.slideshow_started')})
    return jsonify({'ok': False, 'message': result.stderr})


@app.route('/api/slideshow_stop', methods=['POST'])
def api_slideshow_stop():
    t = i18n.make_translator(current_language())
    result = subprocess.run(
        ['sudo', 'systemctl', 'stop', 'zeropiframe-slideshow'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return jsonify({'ok': True, 'message': t('api.slideshow_stopped')})
    return jsonify({'ok': False, 'message': result.stderr})


@app.route('/api/slideshow_toggle', methods=['POST'])
def api_slideshow_toggle():
    """Enables/disables the slideshow service permanently (survives a
    reboot) - analogous to the auto-sync toggle. "enable --now"/
    "disable --now" also takes effect immediately on the currently running
    process; the separate start/stop buttons for the current run keep
    working independently of this."""
    t = i18n.make_translator(current_language())
    enabled = bool(request.get_json(silent=True) and request.get_json().get('enabled'))
    result = subprocess.run(
        ['sudo', 'systemctl', 'enable' if enabled else 'disable', '--now', 'zeropiframe-slideshow'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return jsonify({'ok': False, 'message': result.stderr.strip()})
    return jsonify({'ok': True,
                    'message': t('api.slideshow_enabled') if enabled else t('api.slideshow_disabled')})


@app.route('/api/display_power', methods=['POST'])
def api_display_power():
    power = request.get_json().get('power')
    val   = '1' if power == 'on' else '0'
    subprocess.run(['vcgencmd', 'display_power', val], capture_output=True)
    return jsonify({'ok': True})


@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    """For external triggers (e.g. a Home Assistant rest_command before
    switching off a smart-plug outlet).

    The order is deliberate: HDMI off first (immediate, no root needed -
    $FRAME_USER is in the video group), only then start the orderly
    shutdown. The response comes back as soon as poweroff has been
    initiated - the actual shutdown keeps running in the background.
    The caller (e.g. an HA automation) should wait roughly 20-30s
    afterwards before actually switching off the outlet.
    """
    t = i18n.make_translator(current_language())
    subprocess.run(['vcgencmd', 'display_power', '0'], capture_output=True)

    result = subprocess.run(['sudo', 'systemctl', 'poweroff'],
                            capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f'Shutdown failed: {result.stderr.strip()}')
        return jsonify({'ok': False, 'message': result.stderr.strip()})

    return jsonify({'ok': True, 'message': t('api.shutdown_initiated')})


@app.route('/api/restart_slideshow', methods=['POST'])
def api_restart_slideshow():
    subprocess.run(['sudo', 'systemctl', 'restart', 'zeropiframe-slideshow'],
                   capture_output=True)
    return jsonify({'ok': True})


@app.route('/api/update', methods=['POST'])
def api_update():
    """Triggers "git pull" + either a deploy or a full install (see
    update.sh, generated by install.sh) via the tightly scoped sudoers
    rule set up for that purpose - which of the two is picked by the
    "mode" field in the request body ("deploy", the default, or "full").
    The sudoers rule only permits these two exact invocations (see
    install.sh) - anything else is rejected here before it ever reaches
    sudo.

    Launched detached (subprocess.Popen, not .run()) and not waited on -
    both modes restart zeropiframe-webui itself partway through, which
    would otherwise tear down the very process handling this request
    before it could send a response. This response only confirms the
    update *started*; write an "in progress" status right away so a
    concurrent page load has something better to show than the previous
    (possibly long-stale) result, then rely on update.sh itself to
    overwrite that with the real outcome via LAST_UPDATE_PATH once it's
    done - polled by the page afterwards through /api/update_status,
    after the restarted webui is back up to answer it.
    """
    t = i18n.make_translator(current_language())
    mode = (request.get_json(silent=True) or {}).get('mode', 'deploy')
    if mode not in ('deploy', 'full'):
        return jsonify({'ok': False, 'message': f'Invalid update mode: {mode}'})

    try:
        LAST_UPDATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_UPDATE_PATH.write_text(json.dumps({
            'ok': None,
            'message': t('api.update_full_in_progress') if mode == 'full'
                       else t('api.update_in_progress'),
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }))
    except Exception as e:
        log.warning(f'Could not write in-progress update status: {e}')

    try:
        subprocess.Popen(['sudo', '/opt/zeropiframe/update.sh', mode],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as e:
        log.error(f'Could not start update: {e}')
        return jsonify({'ok': False, 'message': str(e)})

    message = t('api.update_full_started') if mode == 'full' else t('api.update_started')
    return jsonify({'ok': True, 'message': message})


@app.route('/api/update_status')
def api_update_status():
    return jsonify(get_last_update() or {'ok': None, 'message': None, 'timestamp': None})


@app.route('/api/clear_cache', methods=['POST'])
def api_clear_cache():
    t = i18n.make_translator(current_language())
    try:
        for f in CACHE_DIR.iterdir():
            if f.is_file():
                f.unlink()
        return jsonify({'ok': True, 'message': t('api.cache_cleared')})
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)})


@app.route('/api/status')
def api_status():
    try:
        n_images = len(cached_files())
        size_gb  = cache_size_gb()
    except Exception:
        n_images = 0
        size_gb  = 0.0

    # zeropiframe-sync is a Type=oneshot service without RemainAfterExit:
    # while it's running, systemd reports "activating", then immediately
    # "inactive" afterwards (never a lasting "active" like a long-running
    # service). Cover both possible "currently running" states.
    sync_state   = get_service_status('zeropiframe-sync')
    sync_running = sync_state in ('active', 'activating')

    return jsonify({
        'slideshow': get_service_status('zeropiframe-slideshow'),
        'sync_timer': get_service_status('zeropiframe-sync.timer'),
        'sync_running': sync_running,
        'sync_enabled': is_service_enabled('zeropiframe-sync.timer'),
        'slideshow_enabled': is_service_enabled('zeropiframe-slideshow'),
        'n_images': n_images,
        'size_gb': round(size_gb, 2),
        'last_sync': get_last_sync(),
        'current_media': get_current_media(),
    })

# ---------------------------------------------------------------------------
# Update HDMI timers
# ---------------------------------------------------------------------------

def _update_hdmi_timers(cfg: dict):
    """Applies the HDMI schedule (or disables it).

    webui.py runs unprivileged and isn't allowed/able to write to
    /etc/systemd/system/ itself. Instead, the root helper script from
    install.sh is invoked via the sudoers rule set up for that purpose -
    the script validates the time strings itself before touching anything.
    """
    on_time  = cfg.get('display', {}).get('on_time', '')
    off_time = cfg.get('display', {}).get('off_time', '')
    script   = '/opt/zeropiframe/apply-hdmi-schedule.sh'

    if not on_time or not off_time:
        subprocess.run(['sudo', script, 'disable'], capture_output=True)
        return

    result = subprocess.run(['sudo', script, on_time, off_time],
                            capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f'Could not set HDMI schedule: {result.stderr.strip()}')


def _update_shutdown_timer(cfg: dict):
    """Sets (or disables) the auto-shutdown time.

    Intended for operation on a timer/smart-plug outlet without an orderly
    shutdown: the Pi shuts itself down cleanly shortly BEFORE the outlet's
    scheduled power-off time, instead of having the power cut raw (risk of
    SD card corruption). Works like the HDMI schedule, via a root helper
    script through sudo.
    """
    shutdown_time = cfg.get('display', {}).get('shutdown_time', '')
    script = '/opt/zeropiframe/apply-shutdown-schedule.sh'

    if not shutdown_time:
        subprocess.run(['sudo', script, 'disable'], capture_output=True)
        return

    result = subprocess.run(['sudo', script, shutdown_time],
                            capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f'Could not set auto-shutdown: {result.stderr.strip()}')


def _update_sync_interval(cfg: dict):
    """Applies the configured sync interval to zeropiframe-sync.timer.

    Without this call, the "sync interval" field in the web UI would only
    end up in config.yaml but never actually affect the running systemd
    timer (which was set up once, with a fixed value, at install time).
    """
    minutes = cfg.get('sync', {}).get('interval_minutes', 60)
    script  = '/opt/zeropiframe/apply-sync-interval.sh'

    result = subprocess.run(['sudo', script, str(minutes)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f'Could not set sync interval: {result.stderr.strip()}')


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    # threaded=True: without this, a slow "test connection" call (up to a
    # 30s timeout) would block the entire web UI for all other requests
    # (only one CPU core of the Zero 2 W is needed for this - the blocking
    # was purely caused by the single-request dev server).
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
