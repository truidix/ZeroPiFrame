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
from PIL import Image

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
        # Anders als bei Immich liefert WebDAV/PROPFIND keine Bildauflösung
        # in den Metadaten – die Datei muss also erst heruntergeladen werden,
        # bevor geprüft werden kann, ob sie unter der Mindestauflösung liegt.
        # Wird sie herausgefiltert, bleibt eine .lowres-Markerdatei mit dem
        # ETag stehen, damit sie nicht bei jedem Sync erneut heruntergeladen
        # wird, solange sich die Datei auf dem Server nicht ändert.
        self.min_resolution_px = cfg.get('min_resolution_px', 0) or 0

    def _below_min_resolution(self, path: Path) -> bool:
        """True wenn das lokal heruntergeladene Bild unter der konfigurierten
        Mindestauflösung liegt. Nutzt PIL's Image.open(), das nur den Header
        liest (kein voller Decode) – auch bei vielen/großen Dateien schnell.
        Kann die Auflösung nicht ermittelt werden (kaputte/unbekannte Datei),
        wird im Zweifel NICHT gefiltert, um kein gültiges Foto zu verlieren.
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

        if self.min_resolution_px:
            log.info(f'Mindestauflösung aktiv: kleinere Seite >= {self.min_resolution_px}px')
            # Bereits gecachte Bilder, die die (ggf. nachträglich aktivierte
            # oder erhöhte) Mindestauflösung unterschreiten, aus dem Cache
            # entfernen. Image.open() liest hierfür nur den Header, kein
            # voller Decode – auch bei vielen Dateien vernachlässigbar teuer.
            for name, path in list(existing.items()):
                if path.suffix.lower() not in SUPPORTED_IMAGES:
                    continue
                if self._below_min_resolution(path):
                    path.unlink(missing_ok=True)
                    (CACHE_DIR / f'.{name}.etag').unlink(missing_ok=True)
                    del existing[name]
                    log.info(f'Entfernt (Mindestauflösung unterschritten): {name}')
                    removed += 1

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

        skipped_low_res = 0
        remote_names = set()
        for rf in remote_files:
            # Eindeutigen Cache-Dateinamen aus dem VOLLEN WebDAV-Pfad ableiten,
            # nicht nur dem Basisnamen: Kameras/Handys vergeben Dateinamen oft
            # nach einem generischen Schema (IMG_0001.JPG, DSC_0001.JPG, ...),
            # das sich über verschiedene Ordner/Zeiträume hinweg wiederholt.
            # Mit nur dem Basisnamen als Cache-Key würden zwei völlig
            # verschiedene Fotos aus zwei Ordnern auf denselben lokalen
            # Dateinamen kollidieren – der zweite Sync-Durchlauf würde dann
            # das erste Foto einfach überschreiben, das dadurch unwiderruflich
            # aus der Rotation verschwindet (fühlt sich an wie "es zeigt
            # immer wieder dieselben Bilder", obwohl in Wahrheit ein Teil der
            # Bibliothek nie in den Cache geschafft hat).
            path_hash = hashlib.md5(rf['href'].encode('utf-8')).hexdigest()[:10]
            safe_name = f'{path_hash}_{rf["name"]}'
            remote_names.add(safe_name)

            local_path   = CACHE_DIR / safe_name
            etag_file    = CACHE_DIR / f'.{safe_name}.etag'
            lowres_marker = CACHE_DIR / f'.{safe_name}.lowres'

            # Prüfe ob Datei neu oder verändert ist
            if local_path.exists() and etag_file.exists():
                cached_etag = etag_file.read_text().strip()
                if cached_etag == rf['etag'] and rf['etag']:
                    skipped += 1
                    continue

            # War diese Datei (bei unverändertem ETag) bereits als zu
            # niedrig aufgelöst markiert? Dann nicht erneut herunterladen,
            # nur um sie danach doch wieder zu verwerfen.
            if self.min_resolution_px and lowres_marker.exists():
                marked_etag = lowres_marker.read_text().strip()
                if marked_etag == rf['etag'] and rf['etag']:
                    skipped_low_res += 1
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

                # Erst NACH dem Download prüfbar: WebDAV/PROPFIND liefert
                # anders als die Immich-Metadaten keine Bildauflösung im
                # Voraus, daher kann hier nicht wie bei Immich vor dem
                # Download gefiltert werden.
                if Path(safe_name).suffix.lower() in SUPPORTED_IMAGES \
                        and self._below_min_resolution(local_path):
                    local_path.unlink(missing_ok=True)
                    total_bytes -= new_size
                    if rf['etag']:
                        lowres_marker.write_text(rf['etag'])
                    etag_file.unlink(missing_ok=True)
                    log.info(f'Wegen Mindestauflösung verworfen: {safe_name}')
                    skipped_low_res += 1
                    continue

                lowres_marker.unlink(missing_ok=True)
                if rf['etag']:
                    etag_file.write_text(rf['etag'])
                log.info(f'Heruntergeladen: {safe_name}')
                added += 1
            except Exception as e:
                log.error(f'Download-Fehler {safe_name}: {e}')
                local_path.with_name(local_path.name + '.part').unlink(missing_ok=True)

        if skipped_low_res:
            log.info(f'{skipped_low_res} Datei(en) wegen Mindestauflösung übersprungen')
            skipped += skipped_low_res

        if delete_removed:
            for name, path in list(existing.items()):
                if name not in remote_names:
                    path.unlink(missing_ok=True)
                    (CACHE_DIR / f'.{name}.etag').unlink(missing_ok=True)
                    log.info(f'Gelöscht (nicht mehr auf Server): {name}')
                    removed += 1
            # Verwaiste .lowres-Marker aufräumen (Datei auf Server gelöscht)
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
        self.albums    = cfg.get('albums', [])   # leer = alle Fotos
        self.all_photos = cfg.get('all_photos', True)
        # Assets, deren kleinere Seite unter diesem Wert liegt, werden gar
        # nicht erst heruntergeladen (0/None = kein Filter). Bekämpft
        # Pixelbrei durch Hochskalieren von Originalen, die selbst schon
        # niedrig aufgelöst sind (alte Handyfotos, WhatsApp-Komprimierung,
        # Screenshots) – die Immich-"original"-Datei ist immer schon die
        # bestmögliche Qualität, es gibt keine weitere "Qualitätsstufe"
        # jenseits davon zu wählen.
        self.min_resolution_px = cfg.get('min_resolution_px', 0) or 0
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

    def _post(self, endpoint: str, json_body: dict, **kwargs) -> dict | list:
        resp = requests.post(
            f'{self.base_url}{endpoint}',
            headers=self.headers,
            json=json_body,
            timeout=30,
            **kwargs
        )
        resp.raise_for_status()
        return resp.json()

    def get_assets(self) -> list[dict]:
        """Gibt alle relevanten Assets zurück.

        Läuft komplett über POST /api/search/metadata: aktuelle Immich-Versionen
        haben GET /api/assets (Listing aller Fotos) entfernt, und
        GET /api/albums/{id} liefert seitdem nur noch Metadaten + assetCount,
        keine eingebettete Asset-Liste mehr. Der Suchendpunkt deckt beide
        Fälle ab – ohne albumIds-Filter die komplette Bibliothek, mit Filter
        nur die Assets der angegebenen Alben (OR-verknüpft, falls mehrere).
        """
        album_ids = None
        if self.albums:
            log.info(f'Album-Filter aktiv, konfiguriert: {self.albums}')
            all_albums = self._get('/api/albums')
            name_to_id = {a['albumName']: a['id'] for a in all_albums}
            log.info(f'Für diesen API-Key sichtbare Alben ({len(name_to_id)}): '
                     f'{sorted(name_to_id) or "(keine)"}')
            album_ids = []
            for album_name in self.albums:
                aid = name_to_id.get(album_name)
                if not aid:
                    log.warning(f'Immich-Album nicht gefunden: "{album_name}" '
                               f'(Groß-/Kleinschreibung und Leerzeichen müssen exakt passen)')
                    continue
                log.info(f'Immich-Album gefunden: "{album_name}" -> {aid}')
                album_ids.append(aid)
            if not album_ids:
                log.warning('Kein konfiguriertes Album gefunden – Sync liefert 0 Assets')
                return []
        else:
            log.info('Kein Album-Filter konfiguriert (immich.albums ist leer) – '
                     'suche in der GESAMTEN Bibliothek des API-Key-Accounts. '
                     'Achtung: bei einem dedizierten/geteilten Account, der selbst '
                     'keine eigenen Fotos besitzt, liefert das immer 0 Assets – '
                     'in dem Fall muss albums: [...] gesetzt sein.')

        if self.min_resolution_px:
            log.info(f'Mindestauflösung aktiv: kleinere Seite >= {self.min_resolution_px}px')

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
            # Nur Bilder und Videos (keine Audio/Sonstiges)
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
            log.info(f'{skipped_low_res} Asset(s) wegen Mindestauflösung übersprungen')
        return assets

    def _below_min_resolution(self, asset: dict) -> bool:
        """True wenn das Asset unter der konfigurierten Mindestauflösung liegt.

        Videos werden nie herausgefiltert (Auflösung ist dort weniger
        kritisch, die width/height-Semantik ist zudem uneinheitlicher).
        Fehlt width/height (manche älteren/importierten Assets haben das
        nicht gepflegt), wird das Asset im Zweifel NICHT gefiltert, um nicht
        versehentlich gültige Fotos zu verlieren.
        """
        if not self.min_resolution_px or asset.get('type') != 'IMAGE':
            return False
        w, h = asset.get('width'), asset.get('height')
        if not w or not h:
            return False
        return min(w, h) < self.min_resolution_px

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
