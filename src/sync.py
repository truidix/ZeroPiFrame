#!/usr/bin/env python3
"""
Photoframe Sync
Synchronisiert Bilder von Nextcloud (WebDAV) oder Immich (REST-API)
in den lokalen Cache. Wird via systemd-Timer periodisch gestartet.
"""

import os
import sys
import time
import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Optional

import yaml
import requests
from requests.auth import HTTPBasicAuth

LOG_FORMAT = '%(asctime)s [sync] %(levelname)s: %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
                    handlers=[
                        logging.StreamHandler(sys.stdout),
                        logging.FileHandler('/var/log/photoframe-sync.log'),
                    ])
log = logging.getLogger(__name__)

CONFIG_PATH = Path('/opt/photoframe/config.yaml')
CACHE_DIR   = Path('/var/lib/photoframe/cache')
SUPPORTED_IMAGES = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
SUPPORTED_VIDEOS = {'.mp4', '.mkv', '.mov', '.avi', '.m4v', '.webm'}
SUPPORTED        = SUPPORTED_IMAGES | SUPPORTED_VIDEOS

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}

# ---------------------------------------------------------------------------
# Netzwerk-Check
# ---------------------------------------------------------------------------

def is_reachable(host: str, port: int = 80, timeout: int = 5) -> bool:
    """Prüft ob ein Host erreichbar ist (TCP-Connect, kein ICMP)."""
    import socket
    try:
        socket.setdefaulttimeout(timeout)
        with socket.create_connection((host, port)):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def extract_host_port(url: str) -> tuple[str, int]:
    """Extrahiert Host und Port aus einer URL."""
    from urllib.parse import urlparse
    p    = urlparse(url)
    host = p.hostname or p.path
    port = p.port or (443 if p.scheme == 'https' else 80)
    return host, port

# ---------------------------------------------------------------------------
# Cache-Verwaltung
# ---------------------------------------------------------------------------

def cache_size_gb() -> float:
    # .part-Dateien (unvollständige Downloads, s. _download_atomic) zählen
    # nicht zum Cache, damit ein abgebrochener Download nicht dauerhaft
    # gegen das Cache-Limit zählt.
    total = sum(f.stat().st_size for f in CACHE_DIR.iterdir()
                if f.is_file() and not f.name.endswith('.part'))
    return total / (1024 ** 3)


def cleanup_orphaned_downloads():
    """Entfernt .part-Reste eines vorherigen, abgebrochenen Sync-Laufs."""
    for f in CACHE_DIR.glob('*.part'):
        log.info(f'Verwaiste .part-Datei entfernt: {f.name}')
        f.unlink(missing_ok=True)


def cached_files() -> dict[str, Path]:
    """Gibt ein Dict {Dateiname: Path} aller gecachten Bilder zurück."""
    return {f.name: f for f in CACHE_DIR.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED}


def enforce_cache_limit(max_gb: float):
    """Löscht älteste Bilder wenn Cache-Limit überschritten."""
    total = cache_size_gb() * (1024 ** 3)
    limit = max_gb * (1024 ** 3)
    if total <= limit:
        return
    target = limit * 0.9
    files  = sorted(CACHE_DIR.iterdir(), key=lambda f: f.stat().st_mtime)
    for f in files:
        if total <= target:
            break
        try:
            size = f.stat().st_size
        except OSError:
            continue
        log.info(f'Cache-Limit: lösche {f.name}')
        f.unlink(missing_ok=True)
        total -= size


def _download_atomic(resp, dest: Path) -> int:
    """Streamt eine Response in eine temporäre Datei und benennt sie erst nach
    vollständigem Download atomar in den Zielnamen um.

    Verhindert, dass die Slideshow (die den Cache-Ordner unabhängig davon
    periodisch neu einliest) eine noch unvollständig heruntergeladene Datei
    zu Gesicht bekommt und ein kaputtes/verzerrtes Bild anzeigt.
    """
    tmp = dest.with_name(dest.name + '.part')
    size = 0
    with open(tmp, 'wb') as fh:
        for chunk in resp.iter_content(65536):
            fh.write(chunk)
            size += len(chunk)
    os.replace(tmp, dest)   # atomarer Rename auf demselben Dateisystem
    return size

# ---------------------------------------------------------------------------
# Nextcloud / WebDAV Sync
# ---------------------------------------------------------------------------

class NextcloudSync:
    def __init__(self, cfg: dict):
        self.url      = cfg['url'].rstrip('/')
        self.username = cfg['username']
        self.password = cfg['password']
        self.folders  = cfg.get('folders', [])   # leer = Root-Ordner
        self.session  = requests.Session()
        self.session.auth = HTTPBasicAuth(self.username, self.password)
        self.session.verify = cfg.get('verify_ssl', True)

    def list_remote(self, path: str = '') -> list[dict]:
        """Listet Dateien per WebDAV PROPFIND auf."""
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
        """Rekursiv alle Bild-Dateien sammeln."""
        result = []
        try:
            items = self.list_remote(path)
        except Exception as e:
            log.warning(f'WebDAV PROPFIND fehlgeschlagen für "{path}": {e}')
            return result

        for item in items[1:]:   # items[0] ist der Ordner selbst
            if item['is_dir']:
                sub = item['href'].split(self.url.split('//')[1].split('/')[0])[-1]
                result.extend(self.collect_files(sub))
            else:
                ext = Path(item['name']).suffix.lower()
                if ext in SUPPORTED:
                    result.append(item)
        return result

    def sync(self, existing: dict[str, Path], max_gb: float,
             delete_removed: bool) -> tuple[int, int, int]:
        added = removed = skipped = 0

        if self.folders:
            remote_files = []
            for folder in self.folders:
                remote_files.extend(self.collect_files(folder))
        else:
            remote_files = self.collect_files()

        # Laufende Cache-Größe einmalig ermitteln statt bei jeder Datei den
        # kompletten Cache-Ordner neu zu scannen (O(n) statt O(n²) bei
        # großen Bibliotheken – spart auf der langsamen SD-Karte spürbar CPU/IO).
        total_bytes = cache_size_gb() * (1024 ** 3)
        limit_bytes = max_gb * (1024 ** 3)

        remote_names = set()
        for rf in remote_files:
            # Dateinamen ggf. anpassen um Kollisionen zu vermeiden
            safe_name = rf['name']
            remote_names.add(safe_name)

            local_path = CACHE_DIR / safe_name
            etag_file  = CACHE_DIR / f'.{safe_name}.etag'

            # Prüfe ob Datei neu oder verändert ist
            if local_path.exists() and etag_file.exists():
                cached_etag = etag_file.read_text().strip()
                if cached_etag == rf['etag'] and rf['etag']:
                    skipped += 1
                    continue

            if total_bytes >= limit_bytes:
                log.warning(f'Cache-Limit erreicht, überspringe {safe_name}')
                continue

            # Download (atomar über .part-Datei + Rename, s. _download_atomic)
            try:
                dl_url = self.url + '/' + rf['name'] if '/' not in rf['href'] \
                         else rf['href'] if rf['href'].startswith('http') \
                         else f"https://{self.url.split('//')[1].split('/')[0]}{rf['href']}"
                resp = self.session.get(dl_url, timeout=60, stream=True)
                resp.raise_for_status()
                old_size = local_path.stat().st_size if local_path.exists() else 0
                new_size = _download_atomic(resp, local_path)
                total_bytes += new_size - old_size
                if rf['etag']:
                    etag_file.write_text(rf['etag'])
                log.info(f'Heruntergeladen: {safe_name}')
                added += 1
            except Exception as e:
                log.error(f'Download-Fehler {safe_name}: {e}')
                local_path.with_name(local_path.name + '.part').unlink(missing_ok=True)

        if delete_removed:
            for name, path in list(existing.items()):
                if name not in remote_names:
                    path.unlink(missing_ok=True)
                    (CACHE_DIR / f'.{name}.etag').unlink(missing_ok=True)
                    log.info(f'Gelöscht (nicht mehr auf Server): {name}')
                    removed += 1

        return added, removed, skipped

# ---------------------------------------------------------------------------
# Immich Sync
# ---------------------------------------------------------------------------

class ImmichSync:
    def __init__(self, cfg: dict):
        self.base_url  = cfg['url'].rstrip('/')
        self.api_key   = cfg['api_key']
        self.albums    = cfg.get('albums', [])   # leer = alle Fotos
        self.all_photos = cfg.get('all_photos', True)
        self.headers   = {'x-api-key': self.api_key,
                          'Accept': 'application/json'}

    def _get(self, endpoint: str, **kwargs) -> dict | list:
        resp = requests.get(
            f'{self.base_url}{endpoint}',
            headers=self.headers,
            timeout=30,
            **kwargs
        )
        resp.raise_for_status()
        return resp.json()

    def get_assets(self) -> list[dict]:
        """Gibt alle relevanten Assets zurück."""
        if self.albums:
            assets = []
            all_albums = self._get('/api/albums')
            name_to_id = {a['albumName']: a['id'] for a in all_albums}
            for album_name in self.albums:
                album_id = name_to_id.get(album_name)
                if not album_id:
                    log.warning(f'Immich-Album nicht gefunden: {album_name}')
                    continue
                album_data = self._get(f'/api/albums/{album_id}')
                assets.extend(album_data.get('assets', []))
            return assets
        else:
            # Alle Fotos (paginiert)
            assets = []
            page   = 1
            size   = 500
            while True:
                batch = self._get('/api/assets', params={'page': page, 'size': size})
                if not batch:
                    break
                # Nur Bilder (keine Videos)
                assets.extend(a for a in batch if a.get('type') in ('IMAGE', 'VIDEO'))
                if len(batch) < size:
                    break
                page += 1
            return assets

    def sync(self, existing: dict[str, Path], max_gb: float,
             delete_removed: bool) -> tuple[int, int, int]:
        added = removed = skipped = 0

        try:
            assets = self.get_assets()
        except Exception as e:
            log.error(f'Immich API Fehler: {e}')
            return 0, 0, 0

        total_bytes = cache_size_gb() * (1024 ** 3)
        limit_bytes = max_gb * (1024 ** 3)

        remote_ids = set()
        for asset in assets:
            asset_id   = asset['id']
            orig_name  = asset.get('originalFileName', f'{asset_id}.jpg')
            ext        = Path(orig_name).suffix.lower()
            if ext not in SUPPORTED:
                ext = '.jpg'
            safe_name  = f'{asset_id}{ext}'
            remote_ids.add(safe_name)

            local_path = CACHE_DIR / safe_name

            if local_path.exists():
                skipped += 1
                continue

            if total_bytes >= limit_bytes:
                log.warning(f'Cache-Limit erreicht, überspringe {safe_name}')
                continue

            try:
                resp = requests.get(
                    f'{self.base_url}/api/assets/{asset_id}/original',
                    headers=self.headers,
                    timeout=120,
                    stream=True
                )
                resp.raise_for_status()
                new_size = _download_atomic(resp, local_path)
                total_bytes += new_size
                log.info(f'Heruntergeladen: {orig_name} → {safe_name}')
                added += 1
            except Exception as e:
                log.error(f'Download-Fehler {safe_name}: {e}')
                local_path.with_name(local_path.name + '.part').unlink(missing_ok=True)
                local_path.unlink(missing_ok=True)

        if delete_removed:
            for name, path in list(existing.items()):
                if name not in remote_ids:
                    path.unlink(missing_ok=True)
                    log.info(f'Gelöscht: {name}')
                    removed += 1

        return added, removed, skipped

# ---------------------------------------------------------------------------
# Verbindungstest (auch von Web-UI genutzt)
# ---------------------------------------------------------------------------

def test_nextcloud(cfg: dict) -> tuple[bool, str]:
    """Testet Nextcloud-Verbindung ohne zu synken."""
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
            return True, 'Verbindung erfolgreich'
        return False, f'HTTP {resp.status_code}'
    except Exception as e:
        return False, str(e)


def test_immich(cfg: dict) -> tuple[bool, str]:
    """Testet Immich-Verbindung ohne zu synken."""
    try:
        resp = requests.get(
            f"{cfg['url'].rstrip('/')}/api/server/about",
            headers={'x-api-key': cfg['api_key']},
            timeout=10
        )
        if resp.status_code == 200:
            data    = resp.json()
            version = data.get('version', '?')
            return True, f'Immich {version} – Verbindung OK'
        return False, f'HTTP {resp.status_code} – API-Key prüfen'
    except Exception as e:
        return False, str(e)

# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def main():
    log.info('=== Sync gestartet ===')

    try:
        cfg = load_config()
    except Exception as e:
        log.error(f'Config nicht lesbar: {e}')
        sys.exit(1)

    source       = cfg.get('source', 'nextcloud')
    sync_cfg     = cfg.get('sync', {})
    max_gb       = sync_cfg.get('max_cache_size_gb', 5)
    delete_rem   = sync_cfg.get('delete_removed', True)

    # Netzwerk prüfen
    if source == 'nextcloud':
        src_cfg = cfg.get('nextcloud', {})
        url     = src_cfg.get('url', '')
    else:
        src_cfg = cfg.get('immich', {})
        url     = src_cfg.get('url', '')

    if not url:
        log.error(f'Keine URL für Quelle "{source}" konfiguriert')
        sys.exit(1)

    host, port = extract_host_port(url)
    if not is_reachable(host, port):
        log.info(f'Host {host}:{port} nicht erreichbar – Sync übersprungen (Offline-Modus)')
        sys.exit(0)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_orphaned_downloads()
    existing = cached_files()
    log.info(f'Cache: {len(existing)} Dateien / {cache_size_gb():.2f} GB')
    log.info(f'Quelle: {source.upper()} @ {url}')

    start = time.time()
    try:
        if source == 'nextcloud':
            syncer = NextcloudSync(src_cfg)
        else:
            syncer = ImmichSync(src_cfg)

        added, removed, skipped = syncer.sync(existing, max_gb, delete_rem)
    except Exception as e:
        log.error(f'Sync-Fehler: {e}', exc_info=True)
        sys.exit(1)

    enforce_cache_limit(max_gb)

    elapsed = time.time() - start
    log.info(f'Sync abgeschlossen in {elapsed:.1f}s – '
             f'+{added} neu, -{removed} gelöscht, {skipped} unverändert')
    log.info(f'Cache jetzt: {len(cached_files())} Dateien / {cache_size_gb():.2f} GB')


if __name__ == '__main__':
    main()
