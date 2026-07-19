#!/usr/bin/env python3
"""
Photoframe Sync
Synchronizes images from Nextcloud (WebDAV) or Immich (REST API)
into the local cache. Started periodically via a systemd timer.
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
             delete_removed: bool) -> tuple[int, int, int]:
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
             delete_removed: bool) -> tuple[int, int, int]:
        added = removed = skipped = 0

        try:
            assets = self.get_assets()
        except Exception as e:
            log.error(f'Immich API error: {e}')
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
                log.warning(f'Cache limit reached, skipping {safe_name}')
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

    try:
        cfg = load_config()
    except Exception as e:
        log.error(f'Config not readable: {e}')
        sys.exit(1)

    source       = cfg.get('source', 'nextcloud')
    sync_cfg     = cfg.get('sync', {})
    max_gb       = sync_cfg.get('max_cache_size_gb', 5)
    delete_rem   = sync_cfg.get('delete_removed', True)

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

    start = time.time()
    try:
        if source == 'nextcloud':
            syncer = NextcloudSync(src_cfg)
        else:
            syncer = ImmichSync(src_cfg)

        added, removed, skipped = syncer.sync(existing, max_gb, delete_rem)
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
