#!/usr/bin/env python3
"""
Photoframe Web-UI
Erreichbar unter http://photoframe.local:8080
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


def is_service_enabled(name: str) -> bool:
    """True wenn der Dienst/Timer beim Boot automatisch startet.

    Genutzt um den Auto-Sync-Umschalter im Web-UI korrekt darzustellen,
    unabhängig davon ob der Timer gerade zufällig aktiv/inaktiv ist.
    """
    r = subprocess.run(['systemctl', 'is-enabled', name],
                       capture_output=True, text=True)
    return r.stdout.strip() == 'enabled'


# Beispielzeile aus sync.py:
# 2026-07-19 18:26:58,543 [sync] INFO: Sync abgeschlossen in 0.1s – +0 neu, -0 gelöscht, 0 unverändert
_LAST_SYNC_RE = re.compile(
    r'^(?P<ts>[\d-]+ [\d:]+),\d+ \[sync\] INFO: Sync abgeschlossen in '
    r'(?P<duration>[\d.]+)s – \+(?P<added>\d+) neu, -(?P<removed>\d+) gelöscht, '
    r'(?P<skipped>\d+) unverändert'
)


def get_last_sync() -> dict | None:
    """Liest die letzte 'Sync abgeschlossen'-Zeile und gibt Zeitstempel,
    Dauer sowie neu/gelöscht/unverändert-Zähler strukturiert zurück
    (statt wie vorher nur den rohen Zeitstempel-String)."""
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
        if data.get('nc_pass'):           # Passwort nur überschreiben wenn neu eingegeben
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
        # Absicherung falls im Formular versehentlich alle abgewählt wurden
        # (kann bei JS-Fehlern/deaktiviertem JS passieren): Fallback statt
        # eine Slideshow ganz ohne Übergang zu speichern.
        sl['enabled_transitions']    = enabled or ['ken_burns']
        sl['transition']             = sl['enabled_transitions'][0]  # Altfeld, Rückwärtskompatibilität
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

        # HDMI-Zeitplan, Auto-Shutdown und Sync-Intervall via systemd-Timer
        # aktualisieren – ohne das hätte das Ändern dieser Felder im Web-UI
        # keinen Effekt auf die tatsächlich laufenden Timer.
        _update_hdmi_timers(cfg)
        _update_shutdown_timer(cfg)
        _update_sync_interval(cfg)

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
    # webui.py läuft absichtlich unprivilegiert; das Starten eines anderen
    # System-Dienstes braucht root. Ohne interaktive Sitzung kann PolicyKit
    # nicht nach einem Passwort fragen ("Interactive authentication
    # required"), daher über die eng begrenzte sudoers-Regel aus install.sh.
    result = subprocess.run(
        ['sudo', 'systemctl', 'start', 'photoframe-sync'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return jsonify({'ok': True, 'message': 'Sync gestartet'})
    return jsonify({'ok': False, 'message': result.stderr})


@app.route('/api/sync_stop', methods=['POST'])
def api_sync_stop():
    # Bricht einen gerade laufenden Sync-Lauf ab. Da photoframe-sync ein
    # Type=oneshot-Dienst ist, beendet "systemctl stop" den laufenden
    # Python-Prozess (SIGTERM) sauber, ohne den Timer selbst anzufassen –
    # der nächste geplante Sync läuft ganz normal weiter.
    result = subprocess.run(
        ['sudo', 'systemctl', 'stop', 'photoframe-sync'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return jsonify({'ok': True, 'message': 'Sync gestoppt'})
    return jsonify({'ok': False, 'message': result.stderr})


@app.route('/api/sync_toggle', methods=['POST'])
def api_sync_toggle():
    """Aktiviert/deaktiviert den zeitgesteuerten Auto-Sync dauerhaft
    (überlebt einen Reboot). Der manuelle "Sync jetzt"-Button funktioniert
    unabhängig davon immer weiter."""
    enabled = bool(request.get_json(silent=True) and request.get_json().get('enabled'))
    action  = 'enable' if enabled else 'disable'
    result = subprocess.run(
        ['sudo', '/opt/photoframe/apply-sync-enabled.sh', action],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return jsonify({'ok': False, 'message': result.stderr.strip()})
    return jsonify({'ok': True,
                    'message': 'Auto-Sync aktiviert' if enabled else 'Auto-Sync deaktiviert'})


@app.route('/api/slideshow_start', methods=['POST'])
def api_slideshow_start():
    result = subprocess.run(
        ['sudo', 'systemctl', 'start', 'photoframe-slideshow'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return jsonify({'ok': True, 'message': 'Slideshow gestartet'})
    return jsonify({'ok': False, 'message': result.stderr})


@app.route('/api/slideshow_stop', methods=['POST'])
def api_slideshow_stop():
    result = subprocess.run(
        ['sudo', 'systemctl', 'stop', 'photoframe-slideshow'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return jsonify({'ok': True, 'message': 'Slideshow gestoppt'})
    return jsonify({'ok': False, 'message': result.stderr})


@app.route('/api/slideshow_toggle', methods=['POST'])
def api_slideshow_toggle():
    """Aktiviert/deaktiviert den Slideshow-Dienst dauerhaft (überlebt einen
    Reboot) – analog zum Auto-Sync-Umschalter. "enable --now"/"disable --now"
    wirkt zusätzlich sofort auf den aktuell laufenden Prozess, die separaten
    Start/Stopp-Buttons für den aktuellen Lauf funktionieren davon
    unabhängig weiter."""
    enabled = bool(request.get_json(silent=True) and request.get_json().get('enabled'))
    result = subprocess.run(
        ['sudo', 'systemctl', 'enable' if enabled else 'disable', '--now', 'photoframe-slideshow'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return jsonify({'ok': False, 'message': result.stderr.strip()})
    return jsonify({'ok': True,
                    'message': 'Slideshow aktiviert' if enabled else 'Slideshow deaktiviert'})


@app.route('/api/display_power', methods=['POST'])
def api_display_power():
    power = request.get_json().get('power')
    val   = '1' if power == 'on' else '0'
    subprocess.run(['vcgencmd', 'display_power', val], capture_output=True)
    return jsonify({'ok': True})


@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    """Für externe Trigger (z.B. Home Assistant rest_command vor dem
    Abschalten einer Smart-Plug-Steckdose).

    Reihenfolge ist bewusst: HDMI zuerst aus (sofort, keine root-Rechte
    nötig – $FRAME_USER ist in der video-Gruppe), danach erst das geordnete
    Herunterfahren einleiten. Die Antwort kommt zurück sobald poweroff
    eingeleitet ist – der eigentliche Shutdown läuft im Hintergrund weiter.
    Der Aufrufer (z.B. eine HA-Automation) sollte danach noch ca. 20-30s
    warten, bevor er die Steckdose tatsächlich abschaltet.
    """
    subprocess.run(['vcgencmd', 'display_power', '0'], capture_output=True)

    result = subprocess.run(['sudo', 'systemctl', 'poweroff'],
                            capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f'Shutdown fehlgeschlagen: {result.stderr.strip()}')
        return jsonify({'ok': False, 'message': result.stderr.strip()})

    return jsonify({'ok': True, 'message': 'HDMI aus, Herunterfahren eingeleitet'})


@app.route('/api/restart_slideshow', methods=['POST'])
def api_restart_slideshow():
    subprocess.run(['sudo', 'systemctl', 'restart', 'photoframe-slideshow'],
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

    # photoframe-sync ist ein Type=oneshot ohne RemainAfterExit: während des
    # Laufs meldet systemd "activating", danach sofort "inactive" (nie
    # dauerhaft "active" wie bei einem Dauer-Dienst). Beide möglichen
    # "läuft gerade"-Zustände abdecken.
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
# HDMI-Timer aktualisieren
# ---------------------------------------------------------------------------

def _update_hdmi_timers(cfg: dict):
    """Wendet den HDMI-Zeitplan an (oder deaktiviert ihn).

    webui.py läuft unprivilegiert und darf/kann nicht selbst nach
    /etc/systemd/system/ schreiben. Stattdessen wird das root-Helper-Skript
    aus install.sh über die dafür eingerichtete sudoers-Regel aufgerufen –
    das Skript validiert die Zeit-Strings selbst, bevor es irgendetwas
    anfasst.
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
        log.error(f'HDMI-Zeitplan konnte nicht gesetzt werden: {result.stderr.strip()}')


def _update_shutdown_timer(cfg: dict):
    """Setzt (oder deaktiviert) den Auto-Shutdown-Zeitpunkt.

    Gedacht für Betrieb an einer Zeitschaltuhr/Smart-Plug ohne geordnetes
    Herunterfahren: der Pi fährt sich selbst kurz VOR der geplanten
    Abschaltzeit der Steckdose sauber herunter, statt dass der Strom roh
    gekappt wird (Risiko für SD-Karten-Korruption). Läuft wie der
    HDMI-Zeitplan über ein root-Helper-Skript via sudo.
    """
    shutdown_time = cfg.get('display', {}).get('shutdown_time', '')
    script = '/opt/photoframe/apply-shutdown-schedule.sh'

    if not shutdown_time:
        subprocess.run(['sudo', script, 'disable'], capture_output=True)
        return

    result = subprocess.run(['sudo', script, shutdown_time],
                            capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f'Auto-Shutdown konnte nicht gesetzt werden: {result.stderr.strip()}')


def _update_sync_interval(cfg: dict):
    """Wendet das konfigurierte Sync-Intervall auf photoframe-sync.timer an.

    Ohne diesen Aufruf würde das Feld "Sync-Intervall" im Web-UI nur in
    config.yaml landen, aber nie tatsächlich den laufenden systemd-Timer
    beeinflussen (der wurde bei der Installation einmalig mit einem festen
    Wert angelegt).
    """
    minutes = cfg.get('sync', {}).get('interval_minutes', 60)
    script  = '/opt/photoframe/apply-sync-interval.sh'

    result = subprocess.run(['sudo', script, str(minutes)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f'Sync-Intervall konnte nicht gesetzt werden: {result.stderr.strip()}')


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    # threaded=True: ohne das blockiert z. B. ein langsamer "Verbindung
    # testen"-Aufruf (bis zu 30s Timeout) die komplette Web-UI für alle
    # anderen Anfragen (nur ein CPU-Kern des Zero 2 W wird dafür gebraucht,
    # das Blockieren war rein durch den Single-Request-Dev-Server bedingt).
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
