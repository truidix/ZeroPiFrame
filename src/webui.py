#!/usr/bin/env python3
"""
Photoframe Web-UI
Erreichbar unter http://photoframe.local:8080
"""

import os
import sys
import subprocess
import logging
from pathlib import Path
from datetime import datetime

import yaml
from flask import Flask, render_template, request, jsonify, redirect, url_for

# sync.py im gleichen Verzeichnis importieren
sys.path.insert(0, str(Path(__file__).parent))
from sync import test_nextcloud, test_immich, cached_files, cache_size_gb

CONFIG_PATH  = Path('/opt/photoframe/config.yaml')
CACHE_DIR    = Path('/var/lib/photoframe/cache')
LOG_FILE     = Path('/var/log/photoframe-sync.log')

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [webui] %(levelname)s: %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates', static_folder='static')

# ---------------------------------------------------------------------------
# Config-Helpers
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
    # Slideshow über neue Config informieren (kein Neustart nötig)
    subprocess.run(['pkill', '-HUP', '-f', 'slideshow.py'],
                   capture_output=True)


def get_service_status(name: str) -> str:
    r = subprocess.run(['systemctl', 'is-active', name],
                       capture_output=True, text=True)
    return r.stdout.strip()


def get_last_sync() -> str:
    try:
        lines = LOG_FILE.read_text().splitlines()
        for line in reversed(lines):
            if 'Sync abgeschlossen' in line:
                return line.split('[sync]')[0].strip()
        return 'Noch kein Sync'
    except Exception:
        return 'Unbekannt'


def get_next_sync() -> str:
    r = subprocess.run(
        ['systemctl', 'show', 'photoframe-sync.timer', '--property=NextElapseUSecRealtime'],
        capture_output=True, text=True
    )
    # Vereinfachte Ausgabe
    for line in r.stdout.splitlines():
        if 'NextElapseUSecRealtime' in line:
            val = line.split('=')[1].strip()
            if val and val != '0':
                return val
    return 'Unbekannt'

# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    cfg          = load_config()
    slide_status = get_service_status('photoframe-slideshow')
    sync_status  = get_service_status('photoframe-sync')
    timer_status = get_service_status('photoframe-sync.timer')

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
        if data.get('nc_pass'):           # Passwort nur überschreiben wenn neu eingegeben
            cfg['nextcloud']['password'] = data.get('nc_pass', '')
        cfg['nextcloud']['verify_ssl'] = 'nc_verify_ssl' in data
        folders = [f.strip() for f in data.get('nc_folders', '').split('\n') if f.strip()]
        cfg['nextcloud']['folders']    = folders

        # Immich
        cfg.setdefault('immich', {})
        cfg['immich']['url']     = data.get('im_url', '').strip()
        if data.get('im_key'):
            cfg['immich']['api_key'] = data.get('im_key', '')
        albums = [a.strip() for a in data.get('im_albums', '').split('\n') if a.strip()]
        cfg['immich']['albums']      = albums
        cfg['immich']['all_photos']  = not albums

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
        sl['transition']            = data.get('transition', 'ken_burns')
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

        save_config(cfg)

        # HDMI-Zeitplan via systemd-Timer aktualisieren
        _update_hdmi_timers(cfg)

        return redirect(url_for('display') + '?saved=1')

    return render_template('display.html', cfg=cfg,
                           saved=request.args.get('saved'))

# ---------------------------------------------------------------------------
# AJAX-API
# ---------------------------------------------------------------------------

@app.route('/api/test_connection', methods=['POST'])
def api_test_connection():
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
        return jsonify({'ok': False, 'message': 'Unbekannter Quelltyp'})

    return jsonify({'ok': ok, 'message': msg})


@app.route('/api/sync_now', methods=['POST'])
def api_sync_now():
    result = subprocess.run(
        ['systemctl', 'start', 'photoframe-sync'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return jsonify({'ok': True, 'message': 'Sync gestartet'})
    return jsonify({'ok': False, 'message': result.stderr})


@app.route('/api/display_power', methods=['POST'])
def api_display_power():
    power = request.get_json().get('power')
    val   = '1' if power == 'on' else '0'
    subprocess.run(['vcgencmd', 'display_power', val], capture_output=True)
    return jsonify({'ok': True})


@app.route('/api/restart_slideshow', methods=['POST'])
def api_restart_slideshow():
    subprocess.run(['systemctl', 'restart', 'photoframe-slideshow'],
                   capture_output=True)
    return jsonify({'ok': True})


@app.route('/api/clear_cache', methods=['POST'])
def api_clear_cache():
    try:
        for f in CACHE_DIR.iterdir():
            if f.is_file():
                f.unlink()
        return jsonify({'ok': True, 'message': 'Cache geleert'})
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

    return jsonify({
        'slideshow': get_service_status('photoframe-slideshow'),
        'sync_timer': get_service_status('photoframe-sync.timer'),
        'n_images': n_images,
        'size_gb': round(size_gb, 2),
        'last_sync': get_last_sync(),
    })

# ---------------------------------------------------------------------------
# HDMI-Timer aktualisieren
# ---------------------------------------------------------------------------

def _update_hdmi_timers(cfg: dict):
    """Schreibt systemd-Timer für HDMI-Zeitplan."""
    on_time  = cfg.get('display', {}).get('on_time', '')
    off_time = cfg.get('display', {}).get('off_time', '')

    if not on_time or not off_time:
        subprocess.run(['systemctl', 'disable', 'photoframe-hdmi-on.timer'],
                       capture_output=True)
        subprocess.run(['systemctl', 'disable', 'photoframe-hdmi-off.timer'],
                       capture_output=True)
        return

    on_h,  on_m  = on_time.split(':')
    off_h, off_m = off_time.split(':')

    for name, h, m, cmd in [
        ('photoframe-hdmi-on',  on_h,  on_m,  'display_power 1'),
        ('photoframe-hdmi-off', off_h, off_m, 'display_power 0'),
    ]:
        timer_content = f"""[Unit]
Description=Photoframe HDMI {name.split('-')[-1]}

[Timer]
OnCalendar=*-*-* {h}:{m}:00
Persistent=true

[Install]
WantedBy=timers.target
"""
        service_content = f"""[Unit]
Description=Photoframe HDMI {name.split('-')[-1]}

[Service]
Type=oneshot
ExecStart=/usr/bin/vcgencmd {cmd}
"""
        Path(f'/etc/systemd/system/{name}.timer').write_text(timer_content)
        Path(f'/etc/systemd/system/{name}.service').write_text(service_content)

    subprocess.run(['systemctl', 'daemon-reload'], capture_output=True)
    for name in ['photoframe-hdmi-on.timer', 'photoframe-hdmi-off.timer']:
        subprocess.run(['systemctl', 'enable', '--now', name], capture_output=True)


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    # threaded=True: ohne das blockiert z. B. ein langsamer "Verbindung
    # testen"-Aufruf (bis zu 30s Timeout) die komplette Web-UI für alle
    # anderen Anfragen (nur ein CPU-Kern des Zero 2 W wird dafür gebraucht,
    # das Blockieren war rein durch den Single-Request-Dev-Server bedingt).
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
