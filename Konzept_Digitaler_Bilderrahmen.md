# Digitaler Bilderrahmen – Konzept & Architektur

Zielgerät: Raspberry Pi Zero 2 W  
Basis-OS: Raspberry Pi OS Lite 64-bit (Bookworm)

---

## 1. Gesamtarchitektur

```
┌─────────────────────────────────────────────────────┐
│                  Raspberry Pi Zero 2 W               │
│                                                       │
│  ┌─────────────┐     ┌──────────────────────────┐   │
│  │  sync.py    │────▶│  /var/lib/photoframe/     │   │
│  │  (Dienst)   │     │  cache/                   │   │
│  └─────────────┘     │  ├── foto1.jpg            │   │
│        │             │  ├── foto2.jpg            │   │
│        ▼             │  └── ...                  │   │
│  ┌─────────────┐     └──────────────┬───────────┘   │
│  │  config.yaml│                    │                │
│  └─────────────┘     ┌──────────────▼───────────┐   │
│                       │  slideshow.py             │   │
│                       │  (Dienst)                 │   │
│                       └──────────────┬───────────┘   │
│                                      │                │
└──────────────────────────────────────┼────────────────┘
                                       │ Framebuffer
                                  ┌────▼────┐
                                  │  HDMI   │
                                  │ Monitor │
                                  └─────────┘
```

Das System besteht aus **zwei unabhängigen Diensten**:

- **`sync.py`** – lädt Bilder vom konfigurierten Server (Nextcloud oder Immich) in einen lokalen Cache-Ordner. Läuft periodisch im Hintergrund.
- **`slideshow.py`** – zeigt Bilder aus dem Cache-Ordner in zufälliger Reihenfolge direkt auf dem Framebuffer an. Läuft dauerhaft.

Beide Dienste sind **vollständig entkoppelt**. Die Slideshow läuft auch ohne Netzwerkverbindung, solange Bilder im Cache vorhanden sind.

---

## 2. Dateistruktur

```
/opt/photoframe/
├── config.yaml          # Konfiguration (Quelle, Credentials, Intervall)
├── sync.py              # Sync-Dienst
├── slideshow.py         # Slideshow-Dienst
├── requirements.txt     # Python-Abhängigkeiten
└── install.sh           # Setup-Skript

/var/lib/photoframe/
└── cache/               # Heruntergeladene Bilder (persistent)

/etc/systemd/system/
├── photoframe-sync.service
├── photoframe-sync.timer    # Sync alle X Minuten
└── photoframe-slideshow.service
```

---

## 3. Konfiguration (config.yaml)

```yaml
# Bildquelle: "nextcloud" oder "immich"
source: nextcloud

# --- Nextcloud / WebDAV ---
nextcloud:
  url: "https://meine-nextcloud.de/remote.php/dav/files/USERNAME/Fotos/"
  username: "mein_user"
  password: "mein_passwort"
  # Optional: Nur Unterordner synchronisieren
  # folders:
  #   - "Urlaub"
  #   - "Familie"

# --- Immich ---
immich:
  url: "http://192.168.1.100:2283"
  api_key: "mein-api-key"
  # Optional: Nur bestimmte Alben synchronisieren
  # albums:
  #   - "Bilderrahmen"
  # Oder: Alle Fotos
  all_photos: true

# --- Sync-Einstellungen ---
sync:
  interval_minutes: 60         # Wie oft synchronisiert wird
  max_cache_size_gb: 5         # Maximale Cache-Größe
  delete_removed: true         # Lokal löschen wenn auf Server gelöscht

# --- Slideshow-Einstellungen ---
slideshow:
  interval_seconds: 30         # Anzeigedauer pro Bild
  transition: "fade"           # "fade", "none"
  shuffle: true                # Zufällige Reihenfolge
  fit_mode: "contain"          # "contain" (Balken) oder "cover" (zuschneiden)
  background_color: "#000000"  # Hintergrundfarbe

# --- Display / Energiesparen ---
display:
  # HDMI ausschalten außerhalb dieser Zeiten (leer = immer an)
  on_time: "08:00"
  off_time: "23:00"
```

**Umschalten zwischen Nextcloud und Immich:** Einfach `source: nextcloud` auf `source: immich` ändern und den Sync-Dienst neu starten:
```bash
sudo systemctl restart photoframe-sync
```

---

## 4. Protokollanbindung im Detail

### 4.1 Nextcloud via WebDAV

WebDAV ist ein HTTP-Erweiterungsprotokoll. Jede Nextcloud-Instanz stellt automatisch einen WebDAV-Endpunkt bereit.

**Wie es funktioniert:**
```
sync.py                          Nextcloud-Server
    │                                    │
    │── PROPFIND /dav/files/user/Fotos ─▶│  (Verzeichnislisting)
    │◀─ XML-Liste aller Dateien ─────────│
    │                                    │
    │  Für jede neue/geänderte Datei:    │
    │── GET /dav/files/user/Fotos/x.jpg ▶│
    │◀─ Bilddaten ───────────────────────│
    │                                    │
    │  Speichern in /var/lib/.../cache/  │
```

`sync.py` vergleicht die Dateiliste vom Server mit dem lokalen Cache über Dateiname + Änderungsdatum (ETag). Nur neue oder geänderte Bilder werden heruntergeladen.

**Python-Bibliothek:** `webdavclient3`

```python
from webdav3.client import Client

options = {
    'webdav_hostname': config['nextcloud']['url'],
    'webdav_login': config['nextcloud']['username'],
    'webdav_password': config['nextcloud']['password']
}
client = Client(options)
files = client.list()           # Verzeichnislisting
client.download_file(remote_path, local_path)  # Download
```

---

### 4.2 Immich via REST-API

Immich bietet eine vollständige REST-API. Authentifizierung erfolgt über einen API-Key (in Immich unter Einstellungen → API-Schlüssel erstellen).

**Wie es funktioniert:**
```
sync.py                          Immich-Server
    │                                    │
    │── GET /api/albums ────────────────▶│  (Alben-Liste)
    │◀─ JSON: [{id, name, assetCount}] ─│
    │                                    │
    │── GET /api/albums/{albumId} ──────▶│  (Assets eines Albums)
    │◀─ JSON: [{id, originalFileName}] ─│
    │                                    │
    │  Für jedes neue Asset:             │
    │── GET /api/assets/{id}/original ──▶│  (Originalbild)
    │◀─ Bilddaten ───────────────────────│
    │                                    │
    │  Speichern in /var/lib/.../cache/  │
```

**Python-Beispiel:**
```python
import requests

headers = {"x-api-key": config['immich']['api_key']}
base_url = config['immich']['url']

# Alle Assets abrufen
assets = requests.get(f"{base_url}/api/assets", headers=headers).json()

# Oder: Bestimmtes Album
albums = requests.get(f"{base_url}/api/albums", headers=headers).json()
album_id = albums[0]['id']
album_assets = requests.get(
    f"{base_url}/api/albums/{album_id}", headers=headers
).json()['assets']

# Bild herunterladen
for asset in album_assets:
    img_data = requests.get(
        f"{base_url}/api/assets/{asset['id']}/original",
        headers=headers
    ).content
    with open(f"/var/lib/photoframe/cache/{asset['id']}.jpg", 'wb') as f:
        f.write(img_data)
```

---

## 5. Offline-Modus

Der Offline-Modus ist **automatisch** — es ist kein besonderer Modus, da Slideshow und Sync voneinander getrennt sind.

**Logik in `sync.py`:**

```
Sync-Dienst startet (alle 60 Min via systemd-Timer)
         │
         ▼
  WLAN verfügbar?
  (ping zum Server)
    /         \
  Ja           Nein
   │            │
   ▼            ▼
Verbinde    Überspringe
zum Server  diesen Sync-
und sync    Durchlauf
   │            │
   └─────┬──────┘
         ▼
  Slideshow läuft weiter
  mit vorhandenen Bildern
```

**In `sync.py` konkret:**
```python
import subprocess

def is_online(host):
    """Prüft ob der Server erreichbar ist."""
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "3", host],
        capture_output=True
    )
    return result.returncode == 0

if not is_online(server_host):
    print("Kein Netzwerk – Sync übersprungen, Slideshow läuft offline weiter")
    exit(0)

# ... normaler Sync
```

**Verhalten:**
- Beim ersten Start ohne Netz: Slideshow zeigt Platzhalter-Bild ("Keine Bilder vorhanden")
- Nach erstem erfolgreichen Sync: Cache ist gefüllt, Offline-Betrieb unbegrenzt möglich
- Netz kommt wieder: Nächster Timer-Durchlauf synct automatisch neu hinzugefügte Bilder

---

## 6. Bildanzeige (slideshow.py)

Bilder werden direkt auf den Linux **Framebuffer** gezeichnet — kein X11, kein Desktop, kein Browser. Das ist ressourcenschonend und startet schnell.

**Technologie:** Python + `Pillow` (Bildverarbeitung) + `framebuffer` direkt oder über `fbi`

**Ablauf:**
```python
from PIL import Image
import random, os, time

CACHE_DIR = "/var/lib/photoframe/cache"

def get_screen_size():
    # Lese Auflösung aus /sys/class/graphics/fb0/virtual_size
    ...

def show_image(path, screen_w, screen_h):
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)   # EXIF-Rotation korrigieren!
    img.thumbnail((screen_w, screen_h), Image.LANCZOS)
    # Zentrieren auf schwarzem Hintergrund
    canvas = Image.new("RGB", (screen_w, screen_h), (0, 0, 0))
    offset = ((screen_w - img.width) // 2, (screen_h - img.height) // 2)
    canvas.paste(img, offset)
    # Auf Framebuffer schreiben
    with open("/dev/fb0", "wb") as fb:
        fb.write(canvas.tobytes())

while True:
    photos = [f for f in os.listdir(CACHE_DIR) if f.endswith(('.jpg','.png'))]
    if not photos:
        show_placeholder()
        time.sleep(30)
        continue
    random.shuffle(photos)
    for photo in photos:
        show_image(os.path.join(CACHE_DIR, photo), screen_w, screen_h)
        time.sleep(config['slideshow']['interval_seconds'])
```

---

## 7. Installation auf Raspberry Pi Zero 2 W

### Schritt 1: OS flashen

1. [Raspberry Pi Imager](https://www.raspberrypi.com/software/) herunterladen
2. **Raspberry Pi OS Lite (64-bit)** auswählen
3. Vor dem Flashen über das Zahnrad-Menü konfigurieren:
   - Hostname: `photoframe`
   - SSH aktivieren
   - WLAN-Credentials eintragen
4. Auf SD-Karte flashen, einlegen, starten

### Schritt 2: Verbinden & Grundsetup

```bash
# SSH verbinden (nach ~60s Boot-Zeit)
ssh pi@photoframe.local

# System aktualisieren
sudo apt update && sudo apt upgrade -y
```

### Schritt 3: Projekt installieren

```bash
# Abhängigkeiten installieren
sudo apt install -y python3-pip python3-pillow git

# Projekt klonen (oder Dateien kopieren)
sudo git clone https://github.com/YOURREPO/photoframe /opt/photoframe

# Python-Abhängigkeiten
sudo pip3 install webdavclient3 requests pyyaml --break-system-packages

# Konfiguration anlegen
sudo cp /opt/photoframe/config.yaml.example /opt/photoframe/config.yaml
sudo nano /opt/photoframe/config.yaml   # Credentials eintragen

# Cache-Verzeichnis
sudo mkdir -p /var/lib/photoframe/cache
sudo chown pi:pi /var/lib/photoframe/cache
```

### Schritt 4: Framebuffer-Zugriff

```bash
# Nutzer zur video-Gruppe hinzufügen (für /dev/fb0 Zugriff)
sudo usermod -aG video pi

# Konsolenausgabe auf dem Framebuffer deaktivieren
# (sonst erscheint der Login-Prompt über den Bildern)
sudo systemctl disable getty@tty1
```

### Schritt 5: systemd-Dienste einrichten

**`/etc/systemd/system/photoframe-slideshow.service`:**
```ini
[Unit]
Description=Photoframe Slideshow
After=multi-user.target

[Service]
User=pi
ExecStart=/usr/bin/python3 /opt/photoframe/slideshow.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/photoframe-sync.service`:**
```ini
[Unit]
Description=Photoframe Sync (einmaliger Lauf)
After=network-online.target
Wants=network-online.target

[Service]
User=pi
Type=oneshot
ExecStart=/usr/bin/python3 /opt/photoframe/sync.py
```

**`/etc/systemd/system/photoframe-sync.timer`:**
```ini
[Unit]
Description=Photoframe Sync Timer

[Timer]
OnBootSec=30sec         # 30s nach Boot einmal synken
OnUnitActiveSec=60min   # Danach alle 60 Minuten

[Install]
WantedBy=timers.target
```

### Schritt 6: Dienste aktivieren & starten

```bash
sudo systemctl daemon-reload
sudo systemctl enable photoframe-slideshow
sudo systemctl enable photoframe-sync.timer
sudo systemctl start photoframe-slideshow
sudo systemctl start photoframe-sync.timer

# Status prüfen
sudo systemctl status photoframe-slideshow
sudo journalctl -u photoframe-sync -f   # Sync-Log live
```

### Schritt 7: HDMI-Zeitplan (optional)

```bash
# /etc/systemd/system/hdmi-off.service
[Unit]
Description=HDMI ausschalten
[Service]
ExecStart=/usr/bin/vcgencmd display_power 0

# /etc/systemd/system/hdmi-on.service  
[Unit]
Description=HDMI einschalten
[Service]
ExecStart=/usr/bin/vcgencmd display_power 1

# Timer für 23:00 Uhr aus / 08:00 Uhr an
# (analog zu Sync-Timer konfigurieren)
```

---

## 8. Zusammenfassung: Was passiert nach dem Start?

```
Boot
 │
 ├── [30s] photoframe-sync startet → prüft Netz
 │         → Ja: lädt neue Bilder in Cache
 │         → Nein: überspringt, Cache bleibt erhalten
 │
 ├── [sofort] photoframe-slideshow startet
 │            → zeigt Bilder aus Cache in Endlosschleife
 │            → kein Cache: Platzhalter-Bild
 │
 └── [jede Stunde] Sync-Timer feuert erneut
                   → nur neue/geänderte Bilder werden geladen
                   → laufende Slideshow merkt es beim nächsten Shuffle
```

**Gesamter Boot-bis-Bild-Zeit auf Zero 2 W:** ~25–35 Sekunden

---

## 9. Transitions

Alle Übergänge laufen auf dem Zero 2 W direkt auf dem Framebuffer via Pillow. Da die CPU begrenzt ist, wird die Framerate der Transition bewusst niedrig gehalten (8–12 FPS) — bei Fotoübergängen ist das nicht störend.

### Übersicht

| Transition | CPU-Last | Beschreibung | Empfehlung |
|---|---|---|---|
| `none` | ★☆☆ | Sofortiger Schnitt | Minimal, schnell |
| `fade` | ★★☆ | Überblendung (Crossfade) | **Standard-Wahl** |
| `slide_left` | ★★☆ | Neues Bild schiebt von rechts herein | Dynamisch |
| `slide_right` | ★★☆ | Neues Bild schiebt von links herein | Dynamisch |
| `slide_up` | ★★☆ | Neues Bild schiebt von unten herein | Dynamisch |
| `slide_down` | ★★☆ | Neues Bild schiebt von oben herein | Dynamisch |
| `ken_burns` | ★★☆ | Langsames Pan+Zoom über das Bild während der Anzeigedauer | **Highlight** |
| `wipe_left` | ★☆☆ | Neues Bild wird von links nach rechts enthüllt (harter Rand) | Klassisch |
| `zoom_in` | ★★★ | Altes Bild zoomt heraus, neues zoomt herein | Dramatisch |
| `dissolve` | ★★★ | Pixel-für-Pixel zufälliges Auflösen | Organisch |

### Details zu den wichtigsten Transitions

**`fade` (Crossfade):**
Beide Bilder werden mit `Image.blend(img_a, img_b, alpha)` schrittweise gemischt. Alpha läuft von 0.0 → 1.0 in ~15 Schritten (~1.5s). Performanteste animierte Transition.

**`ken_burns`:**
Kein harter Übergang — das Bild selbst bewegt sich während der gesamten Anzeigedauer. Es wird ein zufälliger Start- und Endausschnitt gewählt (z.B. leichte Vergrößerung von 100% auf 110% + leichte Verschiebung). Sieht professionell aus, CPU-Last ist moderat weil die Schrittweite pro Frame gering ist.

```
Startframe: Ausschnitt bei (x=0,   y=0,   w=1920, h=1080)
Endframe:   Ausschnitt bei (x=50,  y=30,  w=1820, h=1020)
→ 60 Frames über 30 Sekunden = 2 FPS genügt für flüssigen Eindruck
```

**`wipe_left`:**
Frame N: Zeige erste N/total * Bildbreite Pixel des neuen Bildes, Rest des alten Bildes. Sehr geringe CPU-Last weil nur ein paste-Aufruf pro Frame.

**`zoom_in` und `dissolve`:**
Höhere CPU-Last durch viele resize-Operationen (zoom) bzw. Maskengenerierung (dissolve). Auf dem Zero 2 W machbar, aber Framerate kann auf 5–6 FPS sinken. Trotzdem visuell ansprechend da es sich um Fotos handelt.

### Konfiguration in config.yaml

```yaml
slideshow:
  transition: "ken_burns"        # Transition wählen
  transition_duration_ms: 1500   # Dauer des Übergangs (außer ken_burns)
  ken_burns_zoom: 0.08           # Zoom-Faktor für Ken Burns (0.05–0.15)
```

---

## 10. Web-Interface (webui.py)

Das Web-Interface läuft als dritter systemd-Dienst auf Port 8080 und ist über `http://photoframe.local:8080` erreichbar. Es verwendet **Flask** (leichtgewichtiges Python-Webframework) ohne externe JavaScript-Frameworks — nur reines HTML/CSS mit minimalem JavaScript.

### Architektur

```
Browser (PC/Smartphone im selben WLAN)
         │  HTTP :8080
         ▼
    webui.py (Flask)
         │
         ├── Liest/schreibt config.yaml
         ├── Sendet SIGHUP an slideshow.py → Config neu laden
         └── Ruft systemctl auf → Sync manuell starten
```

Die Web-UI kommuniziert mit den anderen Diensten nicht über eine API, sondern über:
- **Config-Datei**: Einstellungen werden in `config.yaml` geschrieben
- **SIGHUP-Signal**: `slideshow.py` fängt SIGHUP ab und lädt Config neu (kein Neustart nötig)
- **systemctl**: Manueller Sync-Start via `subprocess.run(["systemctl", "start", "photoframe-sync"])`

### Seiten & Funktionen

#### Startseite / Status (`/`)
```
┌─────────────────────────────────────────┐
│  📷 Photoframe                    [🔄]  │
├─────────────────────────────────────────┤
│  Status                                 │
│  ● Slideshow:  läuft                    │
│  ● Sync:       letzter Lauf: vor 12min  │
│  ● Netzwerk:   verbunden                │
│  ● Cache:      247 Bilder / 1.2 GB      │
│                                         │
│  Aktuelles Bild: DSC_0042.jpg           │
│  Nächster Sync: in 48 Minuten           │
│                              [Jetzt synken] │
└─────────────────────────────────────────┘
```

#### Quellen-Verwaltung (`/sources`)
```
┌─────────────────────────────────────────┐
│  Bildquellen                            │
├─────────────────────────────────────────┤
│  Aktive Quelle:  [Nextcloud ▼]          │
│                                         │
│  ┌─ Nextcloud ──────────────────────┐  │
│  │ URL:       [________________]    │  │
│  │ Benutzer:  [________________]    │  │
│  │ Passwort:  [****************]    │  │
│  │ Ordner:    [________________]    │  │
│  │            [Verbindung testen]   │  │
│  └──────────────────────────────────┘  │
│                                         │
│  ┌─ Immich ─────────────────────────┐  │
│  │ Server-URL: [________________]   │  │
│  │ API-Key:    [****************]   │  │
│  │ Alben:      [Alle ▼]             │  │
│  │             [Verbindung testen]  │  │
│  └──────────────────────────────────┘  │
│                                         │
│                          [Speichern]    │
└─────────────────────────────────────────┘
```

"Verbindung testen" ruft per AJAX die Route `/api/test_connection` auf und gibt sofortiges Feedback ob Credentials korrekt sind — ohne zu speichern.

#### Slideshow-Einstellungen (`/slideshow`)
```
┌─────────────────────────────────────────┐
│  Slideshow                              │
├─────────────────────────────────────────┤
│  Anzeigedauer:   [30] Sekunden          │
│                                         │
│  Übergang:       [Ken Burns       ▼]    │
│  (Vorschau-GIF des gewählten Effekts)   │
│                                         │
│  Übergangsdauer: [1500] ms              │
│                                         │
│  Reihenfolge:    ● Zufällig             │
│                  ○ Alphabetisch         │
│                  ○ Nach Datum           │
│                                         │
│  Bildanpassung:  ● Contain (Balken)     │
│                  ○ Cover (zuschneiden)  │
│                                         │
│  Hintergrund:    [■] #000000            │
│                                         │
│                          [Speichern]    │
└─────────────────────────────────────────┘
```

#### Display-Einstellungen (`/display`)
```
┌─────────────────────────────────────────┐
│  Display & Energie                      │
├─────────────────────────────────────────┤
│  Display-Zeitplan                       │
│  An:  [08:00]   Aus: [23:00]            │
│  ○ Zeitplan deaktivieren (immer an)     │
│                                         │
│  Bildschirm jetzt:  [An] [Aus]          │
│                                         │
│  Sync-Intervall: [60] Minuten           │
│  Max. Cache:     [5] GB                 │
│                                         │
│  [Cache leeren (247 Bilder / 1.2 GB)]   │
│                          [Speichern]    │
└─────────────────────────────────────────┘
```

### Technischer Aufbau webui.py

```python
from flask import Flask, render_template, request, jsonify
import yaml, subprocess, signal, os

app = Flask(__name__)
CONFIG_PATH = "/opt/photoframe/config.yaml"

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def save_config(config):
    with open(CONFIG_PATH, 'w') as f:
        yaml.dump(config, f)
    # Slideshow über neue Config informieren (kein Neustart)
    subprocess.run(["pkill", "-HUP", "-f", "slideshow.py"])

@app.route('/')
def index():
    config = load_config()
    cache_count = len(os.listdir("/var/lib/photoframe/cache"))
    return render_template('index.html', config=config, cache_count=cache_count)

@app.route('/api/test_connection', methods=['POST'])
def test_connection():
    data = request.json
    source_type = data.get('type')  # 'nextcloud' oder 'immich'
    # Verbindungstest ohne zu speichern
    try:
        if source_type == 'nextcloud':
            # WebDAV PROPFIND mit Timeout
            ...
        elif source_type == 'immich':
            # GET /api/server-info mit API-Key
            ...
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route('/api/sync_now', methods=['POST'])
def sync_now():
    subprocess.run(["systemctl", "start", "photoframe-sync"])
    return jsonify({"ok": True})

@app.route('/api/display', methods=['POST'])
def set_display():
    power = request.json.get('power')  # 'on' oder 'off'
    subprocess.run(["vcgencmd", "display_power", "1" if power == 'on' else "0"])
    return jsonify({"ok": True})
```

### systemd-Dienst für Web-UI

```ini
[Unit]
Description=Photoframe Web UI
After=network.target

[Service]
User=pi
ExecStart=/usr/bin/python3 /opt/photoframe/webui.py
Restart=always

[Install]
WantedBy=multi-user.target
```

### Aktualisierte Dateistruktur

```
/opt/photoframe/
├── config.yaml
├── sync.py
├── slideshow.py
├── webui.py              ← NEU
├── templates/            ← NEU
│   ├── base.html
│   ├── index.html
│   ├── sources.html
│   ├── slideshow.html
│   └── display.html
├── static/               ← NEU
│   └── style.css
└── requirements.txt      ← Flask hinzufügen
```

### Aktualisierte Abhängigkeiten (requirements.txt)

```
Pillow>=10.0.0
webdavclient3>=3.14.6
requests>=2.31.0
PyYAML>=6.0
Flask>=3.0.0
```

---

## 11. Gesamtarchitektur (aktualisiert)

```
┌──────────────────────────────────────────────────────┐
│                 Raspberry Pi Zero 2 W                 │
│                                                        │
│  ┌──────────┐   config.yaml   ┌──────────────────┐   │
│  │ webui.py │◀──────────────▶│   sync.py         │   │
│  │  :8080   │    SIGHUP ────▶│   slideshow.py    │   │
│  └──────────┘                └────────┬─────────┘   │
│       ▲                               │               │
│       │ HTTP                    /var/lib/             │
│  Browser am PC                 photoframe/cache/      │
│                                       │               │
└───────────────────────────────────────┼───────────────┘
                                        │ Framebuffer /dev/fb0
                                   ┌────▼────┐
                                   │ Monitor │
                                   └─────────┘
```
