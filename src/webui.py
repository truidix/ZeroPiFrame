#!/usr/bin/env python3
"""
Photoframe Web UI
Reachable at http://photoframe.local:8080
"""

import os
import re
import sys
import subprocess
import logging
from pathlib import Path
from datetime import datetime

import yaml
from flask import Flask, render_template, request, jsonify, redirect, url_for

# Import sync.py from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from sync import test_nextcloud, test_immich, cached_files, cache_size_gb
import i18n

CONFIG_PATH  = Path('/opt/photoframe/config.yaml')
CACHE_DIR    = Path('/var/lib/photoframe/cache')
LOG_FILE     = Path('/var/log/photoframe-sync.log')

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


def get_next_sync() -> str:
    r = subprocess.run(
        ['systemctl', 'show', 'photoframe-sync.timer', '--property=NextElapseUSecRealtime'],
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
    slide_status = get_service_status('photoframe-slideshow')
    sync_status  = get_service_status('photoframe-sync')
    timer_status = get_service_status('photoframe-sync.timer')
    sync_running = sync_status in ('active', 'activating')
    sync_enabled = is_service_enabled('photoframe-sync.timer')
    slideshow_enabled = is_service_enabled('photoframe-slideshow')

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
    )


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


@app.route('/slideshow', methods=['GET', 'POST'])
def slideshow():
    cfg = load_config()

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
        sl['fit_mode']              = data.get('fit_mode', 'contain')
        sl['background_color']      = data.get('bg_color', '#000000')
        sl['video_enabled']         = 'video_enabled' in data
        sl['video_audio']           = 'video_audio' in data

        save_config(cfg)
        return redirect(url_for('slideshow') + '?saved=1')

    return render_template('slideshow.html', cfg=cfg,
                           saved=request.args.get('saved'))


@app.route('/display', methods=['GET', 'POST'])
def display():
    cfg = load_config()

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

    return render_template('display.html', cfg=cfg,
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
        ['sudo', 'systemctl', 'start', 'photoframe-sync'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return jsonify({'ok': True, 'message': t('api.sync_started')})
    return jsonify({'ok': False, 'message': result.stderr})


@app.route('/api/sync_stop', methods=['POST'])
def api_sync_stop():
    t = i18n.make_translator(current_language())
    # Aborts a sync run that's currently in progress. Since photoframe-sync
    # is a Type=oneshot service, "systemctl stop" cleanly terminates the
    # running Python process (SIGTERM) without touching the timer itself -
    # the next scheduled sync still runs normally.
    result = subprocess.run(
        ['sudo', 'systemctl', 'stop', 'photoframe-sync'],
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
        ['sudo', '/opt/photoframe/apply-sync-enabled.sh', action],
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
        ['sudo', 'systemctl', 'start', 'photoframe-slideshow'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return jsonify({'ok': True, 'message': t('api.slideshow_started')})
    return jsonify({'ok': False, 'message': result.stderr})


@app.route('/api/slideshow_stop', methods=['POST'])
def api_slideshow_stop():
    t = i18n.make_translator(current_language())
    result = subprocess.run(
        ['sudo', 'systemctl', 'stop', 'photoframe-slideshow'],
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
        ['sudo', 'systemctl', 'enable' if enabled else 'disable', '--now', 'photoframe-slideshow'],
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
    subprocess.run(['sudo', 'systemctl', 'restart', 'photoframe-slideshow'],
                   capture_output=True)
    return jsonify({'ok': True})


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

    # photoframe-sync is a Type=oneshot service without RemainAfterExit:
    # while it's running, systemd reports "activating", then immediately
    # "inactive" afterwards (never a lasting "active" like a long-running
    # service). Cover both possible "currently running" states.
    sync_state   = get_service_status('photoframe-sync')
    sync_running = sync_state in ('active', 'activating')

    return jsonify({
        'slideshow': get_service_status('photoframe-slideshow'),
        'sync_timer': get_service_status('photoframe-sync.timer'),
        'sync_running': sync_running,
        'sync_enabled': is_service_enabled('photoframe-sync.timer'),
        'slideshow_enabled': is_service_enabled('photoframe-slideshow'),
        'n_images': n_images,
        'size_gb': round(size_gb, 2),
        'last_sync': get_last_sync(),
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
    script   = '/opt/photoframe/apply-hdmi-schedule.sh'

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
    script = '/opt/photoframe/apply-shutdown-schedule.sh'

    if not shutdown_time:
        subprocess.run(['sudo', script, 'disable'], capture_output=True)
        return

    result = subprocess.run(['sudo', script, shutdown_time],
                            capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f'Could not set auto-shutdown: {result.stderr.strip()}')


def _update_sync_interval(cfg: dict):
    """Applies the configured sync interval to photoframe-sync.timer.

    Without this call, the "sync interval" field in the web UI would only
    end up in config.yaml but never actually affect the running systemd
    timer (which was set up once, with a fixed value, at install time).
    """
    minutes = cfg.get('sync', {}).get('interval_minutes', 60)
    script  = '/opt/photoframe/apply-sync-interval.sh'

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
