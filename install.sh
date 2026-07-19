#!/bin/bash
# Photoframe – Installations-Skript
# Ausführen als: sudo bash install.sh
# Getestet auf: Raspberry Pi OS Lite 64-bit (Trixie / Debian 13), Pi Zero 2 W
#
# Trixie-spezifische Anpassungen gegenüber Bookworm:
#   - Boot-Partition liegt unter /boot/firmware/ (nicht /boot/)
#   - Python 3.13: pip nutzt virtualenv statt --break-system-packages
#   - pygame wird als System-Paket installiert, venv mit --system-site-packages

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Bitte als root ausführen: sudo bash install.sh"

# Benutzername: explizit übergeben oder aus SUDO_USER ableiten
# Verwendung: sudo bash install.sh [benutzername]
# Beispiel:   sudo bash install.sh david
FRAME_USER="${1:-${SUDO_USER:-frame}}"
id "$FRAME_USER" &>/dev/null || error "Benutzer '$FRAME_USER' existiert nicht. Verwendung: sudo bash install.sh <benutzername>"
info "Photoframe-Benutzer: $FRAME_USER"

INSTALL_DIR="/opt/photoframe"
VENV_DIR="$INSTALL_DIR/venv"
CACHE_DIR="/var/lib/photoframe/cache"
LOG_DIR="/var/log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)/src"

# Trixie: Boot-Partition unter /boot/firmware/
BOOT_DIR="/boot/firmware"
[[ -d "$BOOT_DIR" ]] || BOOT_DIR="/boot"   # Fallback für ältere Images
info "Boot-Verzeichnis: $BOOT_DIR"

# ---------------------------------------------------------------------------
info "0/10 Laufende Photoframe-Dienste pausieren"
# ---------------------------------------------------------------------------
# Der Zero 2 W hat wenig Reserve: apt/pip/venv-Arbeit während gleichzeitig
# die Slideshow (pygame/KMSDRM + Pillow-Decodes, hält zudem die
# Display-Hoheit) und/oder ein Sync (Downloads, Bildverarbeitung) laufen,
# macht die Installation spürbar langsamer und lässt den Pi "hängen"
# wirken. Falls das hier eine erneute Installation/ein Update auf einem
# bereits laufenden Photoframe ist, werden die Dienste daher zuerst
# gestoppt – Schritt 10/10 am Ende aktiviert und startet sie unabhängig
# davon ohnehin wieder, das hier ist rein für die Dauer der Installation.
# Bei einer frischen Erstinstallation existieren die Units noch nicht,
# "is-active" ist dann einfach false und es passiert nichts.
SLIDESHOW_WAS_ACTIVE=0
for svc in photoframe-slideshow photoframe-sync.timer photoframe-sync photoframe-webui; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        info "Stoppe $svc für die Dauer der Installation"
        [[ "$svc" == "photoframe-slideshow" ]] && SLIDESHOW_WAS_ACTIVE=1
        systemctl stop "$svc" 2>/dev/null || true
    fi
done

# ---------------------------------------------------------------------------
info "0b/10 Hinweisbild während der Installation anzeigen"
# ---------------------------------------------------------------------------
# Nur relevant, wenn die Slideshow oben gerade lief (sonst war der Bildschirm
# schon vorher schwarz/Konsole – nichts zu "ersetzen"). `fbi` ist ein
# winziger Framebuffer-Bildbetrachter (ein einzelnes Bild, kein Rendering,
# keine Animation) – deutlich weniger RAM/CPU als die volle
# pygame/Slideshow-Instanz, aber verhindert trotzdem, dass der Bildschirm
# während der (teils mehrminütigen) Installation einfach schwarz bleibt
# oder die Text-Konsole zeigt.
FBI_PID=""
if [[ "$SLIDESHOW_WAS_ACTIVE" -eq 1 ]]; then
    if ! command -v fbi &>/dev/null; then
        apt-get install -y --no-install-recommends fbi \
            > /tmp/photoframe-fbi-install.log 2>&1 || true
    fi
    if command -v fbi &>/dev/null && [[ -e /dev/fb0 ]]; then
        fbi -T 1 -d /dev/fb0 -a --noverbose \
            "$SCRIPT_DIR/assets/update-please-wait.png" \
            < /dev/null > /tmp/photoframe-fbi.log 2>&1 &
        FBI_PID=$!
        disown "$FBI_PID" 2>/dev/null || true
        info "Hinweisbild wird angezeigt (PID $FBI_PID)"
        # Sicherheitsnetz: falls das Skript vorzeitig abbricht (set -e bei
        # einem Fehler in einem späteren Schritt), soll das Hinweisbild
        # trotzdem nicht für immer stehen bleiben und einen erfolgreichen
        # Abschluss vortäuschen, der nie stattgefunden hat.
        trap '[[ -n "$FBI_PID" ]] && kill "$FBI_PID" 2>/dev/null || true' EXIT
    else
        warn "fbi/Framebuffer nicht verfügbar – kein Hinweisbild während der Installation"
    fi
fi

# ---------------------------------------------------------------------------
info "1/10 System-Pakete installieren"
# ---------------------------------------------------------------------------
apt-get update -qq

apt-get install -y \
    python3 python3-pip python3-venv \
    python3-pygame \
    python3-yaml \
    mpv \
    iw \
    git curl avahi-daemon \
    --no-install-recommends

# vcgencmd (HDMI display_power on/off, used by the display schedule) shipped
# in libraspberrypi-bin on Bookworm; on Trixie that package was split into
# raspi-utils-core / raspi-utils-dt.
#
# `apt-cache show libraspberrypi-bin` still prints a stanza for the renamed
# package even though it has no installable candidate on Trixie, so a
# pre-check against apt-cache is unreliable. Instead we just attempt the
# install and fall back on failure (the `if` condition shields this from
# the script's `set -e`, so a failed first attempt doesn't abort the script).
if apt-get install -y --no-install-recommends libraspberrypi-bin \
       > /tmp/photoframe-vcgencmd-install.log 2>&1; then
    info "vcgencmd-Paket: libraspberrypi-bin"
else
    info "libraspberrypi-bin nicht installierbar (Trixie) – installiere raspi-utils-core/raspi-utils-dt"
    apt-get install -y --no-install-recommends raspi-utils-core raspi-utils-dt
fi

command -v vcgencmd &>/dev/null || \
    warn "vcgencmd nicht gefunden – HDMI-Zeitplan (An/Aus) wird nicht funktionieren"

# ---------------------------------------------------------------------------
info "2/10 Dateien installieren"
# ---------------------------------------------------------------------------
mkdir -p "$INSTALL_DIR/templates" "$INSTALL_DIR/static" "$CACHE_DIR"

cp "$SCRIPT_DIR/slideshow.py"          "$INSTALL_DIR/"
cp "$SCRIPT_DIR/sync.py"               "$INSTALL_DIR/"
cp "$SCRIPT_DIR/webui.py"              "$INSTALL_DIR/"
cp "$SCRIPT_DIR/templates/"*.html     "$INSTALL_DIR/templates/"
cp "$SCRIPT_DIR/static/"*.css         "$INSTALL_DIR/static/"
cp "$(dirname "$0")/requirements.txt" "$INSTALL_DIR/"

touch "$INSTALL_DIR/static/placeholder.png"
chmod -R 755 "$INSTALL_DIR"

# ---------------------------------------------------------------------------
info "3/10 Python virtualenv einrichten (Trixie / PEP 668)"
# ---------------------------------------------------------------------------
# pygame und PyYAML kommen als System-Paket (apt), Rest via pip in venv.
# --system-site-packages macht System-Pakete (pygame) im venv sichtbar.
python3 -m venv --system-site-packages "$VENV_DIR"

# Nur Pakete installieren die nicht als apt-Paket vorhanden sind
"$VENV_DIR/bin/pip" install --quiet \
    webdavclient3 \
    requests \
    Flask \
    Pillow

info "virtualenv: $VENV_DIR"

# ---------------------------------------------------------------------------
info "4/10 Konfiguration anlegen"
# ---------------------------------------------------------------------------
if [[ ! -f "$INSTALL_DIR/config.yaml" ]]; then
    cp "$(dirname "$0")/config.yaml.example" "$INSTALL_DIR/config.yaml"
    warn "config.yaml angelegt – bitte Credentials eintragen:"
    warn "  sudo nano $INSTALL_DIR/config.yaml"
else
    info "config.yaml existiert bereits, wird nicht überschrieben"
fi

# ---------------------------------------------------------------------------
info "5/10 Benutzer-Berechtigungen setzen"
# ---------------------------------------------------------------------------
usermod -aG video,render "$FRAME_USER" 2>/dev/null || true

chown -R "$FRAME_USER:$FRAME_USER" "$CACHE_DIR"
chown -R "$FRAME_USER:$FRAME_USER" "$INSTALL_DIR"

touch "$LOG_DIR/photoframe-sync.log"
chown "$FRAME_USER:$FRAME_USER" "$LOG_DIR/photoframe-sync.log"
touch "$LOG_DIR/photoframe-slideshow.log"
chown "$FRAME_USER:$FRAME_USER" "$LOG_DIR/photoframe-slideshow.log"

# ---------------------------------------------------------------------------
info "6/10 Konsole / Framebuffer konfigurieren"
# ---------------------------------------------------------------------------
systemctl disable getty@tty1 2>/dev/null || true

CMDLINE="$BOOT_DIR/cmdline.txt"
if [[ -f "$CMDLINE" ]] && ! grep -q "vt.global_cursor_default=0" "$CMDLINE"; then
    sed -i 's/$/ vt.global_cursor_default=0/' "$CMDLINE"
    info "Cursor-Blinken deaktiviert (wirkt nach Neustart)"
fi

CONFIG="$BOOT_DIR/config.txt"
if [[ -f "$CONFIG" ]]; then
    cp "$CONFIG" "$CONFIG.photoframe.bak"

    if ! grep -q "^dtoverlay=vc4-kms-v3d" "$CONFIG"; then
        echo "dtoverlay=vc4-kms-v3d" >> "$CONFIG"
        info "vc4-kms-v3d (voller KMS-Treiber) zu $CONFIG hinzugefügt – ohne diesen bleibt der Screen schwarz"
    fi
    # Bildschirm ist dauerhaft angeschlossen: HDMI immer als angeschlossen
    # behandeln (sonst manchmal kein Bild nach Kaltstart) und keine
    # Overscan-Ränder/Boot-Regenbogen auf einem dedizierten Kiosk-Display.
    grep -q "^hdmi_force_hotplug=1" "$CONFIG" || echo "hdmi_force_hotplug=1" >> "$CONFIG"
    grep -q "^disable_overscan=1"  "$CONFIG" || echo "disable_overscan=1"  >> "$CONFIG"
    grep -q "^disable_splash=1"    "$CONFIG" || echo "disable_splash=1"    >> "$CONFIG"
    info "config.txt angepasst (Backup: $CONFIG.photoframe.bak)"
else
    warn "$CONFIG nicht gefunden – vc4-kms-v3d manuell prüfen"
    warn "Falls kein Bild erscheint: SDL_VIDEODRIVER=fbcon in den Service-Dateien setzen"
fi

# ---------------------------------------------------------------------------
info "7/10 Swap & WLAN-Energiesparmodus (512 MB RAM sind knapp)"
# ---------------------------------------------------------------------------
# Pillow-Decode großer Fotos + pygame + Flask + gelegentlich mpv nebeneinander
# können sich auf einem Zero 2 W mit 512 MB RAM eng werden. Etwas Swap als
# Sicherheitsnetz gegen OOM-Kills ist günstiger als ein abstürzender Dienst.
if [[ -f /etc/dphys-swapfile ]]; then
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
    grep -q "^CONF_SWAPSIZE=" /etc/dphys-swapfile || echo "CONF_SWAPSIZE=512" >> /etc/dphys-swapfile
    dphys-swapfile setup  >/dev/null 2>&1 || true
    systemctl restart dphys-swapfile 2>/dev/null || true
    info "Swap auf 512 MB gesetzt"
else
    warn "dphys-swapfile nicht gefunden – Swap manuell einrichten falls RAM knapp wird"
fi

# Der Zero 2 W hat kein Ethernet – WLAN-Powersave sorgt sonst für spürbare
# Verzögerungen/Aussetzer beim Sync und in der Web-UI.
cat > /etc/systemd/system/photoframe-wifi-powersave-off.service << 'EOF'
[Unit]
Description=Photoframe: WLAN-Energiesparmodus deaktivieren
After=network.target

[Service]
Type=oneshot
ExecStart=/sbin/iw dev wlan0 set power_save off
RemainAfterExit=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now photoframe-wifi-powersave-off 2>/dev/null || \
    warn "WLAN-Powersave konnte nicht deaktiviert werden (kein wlan0? per USB-LAN o.ä.?)"

# ---------------------------------------------------------------------------
info "8/10 systemd-Dienste einrichten"
# ---------------------------------------------------------------------------
PYTHON="$VENV_DIR/bin/python3"

cat > /etc/systemd/system/photoframe-slideshow.service << EOF
[Unit]
Description=Photoframe Slideshow
After=multi-user.target systemd-udev-settle.service

[Service]
User=$FRAME_USER
Group=video
WorkingDirectory=$INSTALL_DIR
Environment="SDL_VIDEODRIVER=kmsdrm"
Environment="SDL_VIDEO_KMSDRM_DEVICE=/dev/dri/card0"
Environment="SDL_AUDIODRIVER=dummy"
ExecStartPre=/bin/sleep 3
ExecStart=$PYTHON $INSTALL_DIR/slideshow.py
StandardOutput=append:$LOG_DIR/photoframe-slideshow.log
StandardError=append:$LOG_DIR/photoframe-slideshow.log
Restart=always
RestartSec=5
# Höhere Priorität als Sync/Web-UI: die Slideshow soll auch dann flüssig
# weiterlaufen, wenn der stündliche Sync im Hintergrund CPU/SD-Karte nutzt.
Nice=-5
IOSchedulingClass=best-effort
IOSchedulingPriority=2

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/photoframe-sync.service << EOF
[Unit]
Description=Photoframe Sync (einmaliger Lauf)
After=network-online.target
Wants=network-online.target

[Service]
User=$FRAME_USER
Type=oneshot
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON $INSTALL_DIR/sync.py
StandardOutput=append:$LOG_DIR/photoframe-sync.log
StandardError=append:$LOG_DIR/photoframe-sync.log
# Niedrigere Priorität: Downloads sollen die Slideshow nicht ausbremsen.
Nice=10
IOSchedulingClass=idle
EOF

cat > /etc/systemd/system/photoframe-sync.timer << 'EOF'
[Unit]
Description=Photoframe Sync Timer

[Timer]
OnBootSec=30sec
OnUnitActiveSec=60min
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/photoframe-webui.service << EOF
[Unit]
Description=Photoframe Web-UI
After=network.target

[Service]
User=$FRAME_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON $INSTALL_DIR/webui.py
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------------------
info "9/10 Sudo-Rechte für die Web-UI einrichten"
# ---------------------------------------------------------------------------
# photoframe-webui.service läuft absichtlich unprivilegiert als $FRAME_USER.
# Aber "Sync jetzt" und "Slideshow neu starten" im Web-UI rufen intern
# `systemctl start/restart` auf einen anderen System-Dienst auf – das
# verlangt normalerweise root, und da hier keine interaktive Desktop-Sitzung
# existiert, kann PolicyKit nicht nach einem Passwort fragen und lehnt sofort
# mit "Interactive authentication required" ab. Eine eng begrenzte
# sudoers-Regel für genau diese Befehle behebt das, ohne dem Web-UI-Prozess
# generell root-Rechte zu geben.
#
# Für den HDMI-Zeitplan (Display-Einstellungen) gilt dasselbe zusätzlich für
# das Schreiben der Timer-Unit-Dateien nach /etc/systemd/system/ – dafür
# gibt es ein eigenes root-Helper-Skript, das die Web-UI ebenfalls nur über
# sudo aufrufen darf.

cat > "$INSTALL_DIR/apply-hdmi-schedule.sh" << 'EOF'
#!/bin/bash
# Setzt (oder deaktiviert) den HDMI-Ein/Aus-Zeitplan. Läuft als root (via
# sudo, siehe /etc/sudoers.d/photoframe) - validiert seine Eingaben daher
# defensiv, bevor irgendetwas nach /etc/systemd/system/ geschrieben wird.
#
# Verwendung:
#   apply-hdmi-schedule.sh HH:MM HH:MM   (Einschaltzeit, Ausschaltzeit)
#   apply-hdmi-schedule.sh disable
set -euo pipefail

TIME_RE='^([01][0-9]|2[0-3]):[0-5][0-9]$'

if [[ "${1:-}" == "disable" ]]; then
    systemctl disable --now photoframe-hdmi-on.timer photoframe-hdmi-off.timer 2>/dev/null || true
    exit 0
fi

ON_TIME="${1:-}"
OFF_TIME="${2:-}"

[[ "$ON_TIME"  =~ $TIME_RE ]] || { echo "Ungültige Einschaltzeit: $ON_TIME" >&2; exit 1; }
[[ "$OFF_TIME" =~ $TIME_RE ]] || { echo "Ungültige Ausschaltzeit: $OFF_TIME" >&2; exit 1; }

ON_H="${ON_TIME%%:*}";   ON_M="${ON_TIME##*:}"
OFF_H="${OFF_TIME%%:*}"; OFF_M="${OFF_TIME##*:}"

write_unit() {
    local label="$1" hh="$2" mm="$3" power="$4"
    local name="photoframe-hdmi-${label}"
    cat > "/etc/systemd/system/${name}.timer" << TIMER
[Unit]
Description=Photoframe HDMI ${label}

[Timer]
OnCalendar=*-*-* ${hh}:${mm}:00
Persistent=true

[Install]
WantedBy=timers.target
TIMER
    cat > "/etc/systemd/system/${name}.service" << SERVICE
[Unit]
Description=Photoframe HDMI ${label}

[Service]
Type=oneshot
ExecStart=/usr/bin/vcgencmd display_power ${power}
SERVICE
}

write_unit "on"  "$ON_H"  "$ON_M"  1
write_unit "off" "$OFF_H" "$OFF_M" 0

systemctl daemon-reload
systemctl enable --now photoframe-hdmi-on.timer photoframe-hdmi-off.timer
EOF
chown root:root "$INSTALL_DIR/apply-hdmi-schedule.sh"
chmod 700 "$INSTALL_DIR/apply-hdmi-schedule.sh"

# Auto-Shutdown-Zeitplan: für Betrieb an einer Zeitschaltuhr/Smart-Plug ohne
# geordnetes Herunterfahren. Statt den Strom roh zu kappen (Risiko für
# SD-Karten-Korruption bei jedem Aus), fährt der Pi sich selbst ein paar
# Minuten VOR der geplanten Abschaltzeit der Steckdose sauber herunter.
# Wichtig: Persistent=false (Standard) – sonst würde der Timer beim
# nächsten Boot denken, die verpasste Ausführung nachholen zu müssen, und
# sofort wieder herunterfahren.
cat > "$INSTALL_DIR/apply-shutdown-schedule.sh" << 'EOF'
#!/bin/bash
# Setzt (oder deaktiviert) den automatischen Shutdown-Zeitpunkt.
# Läuft als root (via sudo, siehe /etc/sudoers.d/photoframe).
#
# Verwendung:
#   apply-shutdown-schedule.sh HH:MM
#   apply-shutdown-schedule.sh disable
set -euo pipefail

TIME_RE='^([01][0-9]|2[0-3]):[0-5][0-9]$'

if [[ "${1:-}" == "disable" ]]; then
    systemctl disable --now photoframe-shutdown.timer 2>/dev/null || true
    exit 0
fi

SHUTDOWN_TIME="${1:-}"
[[ "$SHUTDOWN_TIME" =~ $TIME_RE ]] || { echo "Ungültige Zeit: $SHUTDOWN_TIME" >&2; exit 1; }
H="${SHUTDOWN_TIME%%:*}"; M="${SHUTDOWN_TIME##*:}"

cat > /etc/systemd/system/photoframe-shutdown.timer << TIMER
[Unit]
Description=Photoframe Auto-Shutdown

[Timer]
OnCalendar=*-*-* ${H}:${M}:00
Persistent=false

[Install]
WantedBy=timers.target
TIMER

cat > /etc/systemd/system/photoframe-shutdown.service << 'SERVICE'
[Unit]
Description=Photoframe Auto-Shutdown

[Service]
Type=oneshot
ExecStart=/sbin/poweroff
SERVICE

systemctl daemon-reload
systemctl enable --now photoframe-shutdown.timer
EOF
chown root:root "$INSTALL_DIR/apply-shutdown-schedule.sh"
chmod 700 "$INSTALL_DIR/apply-shutdown-schedule.sh"

# Sync-Intervall: photoframe-sync.timer wird oben mit einem festen
# OnUnitActiveSec=60min angelegt. Das Web-UI-Feld "Sync-Intervall" schreibt
# nur nach config.yaml – ohne dieses Helper-Skript hätte das Ändern des
# Wertes im Web-UI keinerlei Effekt auf den tatsächlichen Timer.
cat > "$INSTALL_DIR/apply-sync-interval.sh" << 'EOF'
#!/bin/bash
# Setzt das Sync-Intervall des photoframe-sync.timer.
# Läuft als root (via sudo, siehe /etc/sudoers.d/photoframe).
#
# Verwendung: apply-sync-interval.sh <Minuten>
set -euo pipefail

MINUTES="${1:-}"
[[ "$MINUTES" =~ ^[0-9]+$ ]] || { echo "Ungültiges Intervall: $MINUTES" >&2; exit 1; }
(( MINUTES >= 5 && MINUTES <= 1440 )) || { echo "Intervall muss zwischen 5 und 1440 Minuten liegen" >&2; exit 1; }

cat > /etc/systemd/system/photoframe-sync.timer << TIMER
[Unit]
Description=Photoframe Sync Timer

[Timer]
OnBootSec=30sec
OnUnitActiveSec=${MINUTES}min
Persistent=true

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
# restart statt reload: die neue OnUnitActiveSec-Periode soll sofort ab
# jetzt neu berechnet werden, statt erst nach Ablauf der alten Periode.
systemctl restart photoframe-sync.timer
EOF
chown root:root "$INSTALL_DIR/apply-sync-interval.sh"
chmod 700 "$INSTALL_DIR/apply-sync-interval.sh"

# Aktiviert/deaktiviert den zeitgesteuerten Auto-Sync dauerhaft (überlebt
# einen Reboot) – anders als ein bloßes "systemctl stop", das den Timer nur
# bis zum nächsten Boot pausieren würde, da er weiterhin enabled bliebe.
cat > "$INSTALL_DIR/apply-sync-enabled.sh" << 'EOF'
#!/bin/bash
# Aktiviert/deaktiviert den automatischen (zeitgesteuerten) Sync dauerhaft.
# Läuft als root (via sudo, siehe /etc/sudoers.d/photoframe).
#
# Verwendung: apply-sync-enabled.sh enable|disable
set -euo pipefail

ACTION="${1:-}"
case "$ACTION" in
  enable)
    systemctl enable --now photoframe-sync.timer
    ;;
  disable)
    systemctl disable --now photoframe-sync.timer
    ;;
  *)
    echo "Verwendung: apply-sync-enabled.sh enable|disable" >&2
    exit 1
    ;;
esac
EOF
chown root:root "$INSTALL_DIR/apply-sync-enabled.sh"
chmod 700 "$INSTALL_DIR/apply-sync-enabled.sh"

cat > /etc/sudoers.d/photoframe << EOF
# Erzeugt von install.sh – nur die konkreten Befehle, die photoframe-webui.service
# (läuft als $FRAME_USER) für "Sync jetzt/stoppen", "Slideshow starten/stoppen/neu
# starten", den HDMI-Zeitplan, den Auto-Shutdown-Zeitplan und das Ein-/Ausschalten
# des Auto-Sync-Timers benötigt. Keine generelle sudo/root-Freigabe.
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start photoframe-sync
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl stop photoframe-sync
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start photoframe-slideshow
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl stop photoframe-slideshow
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart photoframe-slideshow
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now photoframe-slideshow
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now photoframe-slideshow
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff
$FRAME_USER ALL=(root) NOPASSWD: $INSTALL_DIR/apply-hdmi-schedule.sh *
$FRAME_USER ALL=(root) NOPASSWD: $INSTALL_DIR/apply-shutdown-schedule.sh *
$FRAME_USER ALL=(root) NOPASSWD: $INSTALL_DIR/apply-sync-interval.sh *
$FRAME_USER ALL=(root) NOPASSWD: $INSTALL_DIR/apply-sync-enabled.sh *
EOF
chmod 440 /etc/sudoers.d/photoframe
visudo -c -f /etc/sudoers.d/photoframe || error "sudoers-Datei ungültig – bitte /etc/sudoers.d/photoframe prüfen"
info "sudoers-Regel angelegt: /etc/sudoers.d/photoframe"

# ---------------------------------------------------------------------------
info "10/10 Dienste aktivieren"
# ---------------------------------------------------------------------------
systemctl daemon-reload
systemctl enable photoframe-slideshow photoframe-sync.timer photoframe-webui
# restart statt start: bringt sowohl frisch installierte als auch (Schritt
# 0/10) für die Installation pausierte Dienste zuverlässig wieder hoch –
# und sorgt bei einer erneuten Installation (z.B. um einen Fix einzuspielen)
# dafür, dass bereits laufende Dienste den neuen Code auch tatsächlich
# übernehmen, statt unverändert weiterzulaufen.
systemctl restart photoframe-sync.timer photoframe-webui

# Hinweisbild (falls in Schritt 0b gestartet) erst jetzt beenden – möglichst
# knapp bevor die echte Slideshow die Display-Hoheit zurückbekommt, damit
# der Bildschirm so kurz wie möglich dazwischen leer/Konsole zeigt.
if [[ -n "$FBI_PID" ]] && kill -0 "$FBI_PID" 2>/dev/null; then
    kill "$FBI_PID" 2>/dev/null || true
    wait "$FBI_PID" 2>/dev/null || true
fi

# Prüft NUR die Felder der aktuell aktiven Quelle (source: nextcloud/immich)
# auf Platzhalterwerte – ein einfaches grep über die ganze Datei (frühere
# Version) schlug fälschlich an, sobald irgendwo im File noch ein
# Platzhalter stand, selbst in der GERADE NICHT verwendeten Quelle (z.B.
# Immich konfiguriert und funktionsfähig, aber der ungenutzte
# Nextcloud-Abschnitt enthält noch "mein_passwort" aus der Vorlage). Das
# ließ die Slideshow bei jedem Reinstall/Update fälschlich deaktiviert,
# obwohl die Konfiguration für die tatsächlich genutzte Quelle längst
# vollständig war.
CONFIG_READY=0
if [[ -f "$INSTALL_DIR/config.yaml" ]]; then
    if python3 - "$INSTALL_DIR/config.yaml" << 'PYEOF'
import sys
import yaml

with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f) or {}

source = cfg.get('source', 'nextcloud')

if source == 'immich':
    im = cfg.get('immich', {}) or {}
    ok = bool(im.get('url')) and bool(im.get('api_key')) \
         and im.get('api_key') != 'DEIN_API_KEY_HIER'
else:
    nc = cfg.get('nextcloud', {}) or {}
    ok = bool(nc.get('url')) and bool(nc.get('username')) and bool(nc.get('password')) \
         and nc.get('username') != 'mein_benutzer' and nc.get('password') != 'mein_passwort'

sys.exit(0 if ok else 1)
PYEOF
    then
        CONFIG_READY=1
    fi
fi

if [[ "$CONFIG_READY" -eq 1 ]]; then
    systemctl restart photoframe-slideshow
else
    warn "Config für die aktive Quelle ($(grep -oP '^source:\s*\K\S+' "$INSTALL_DIR/config.yaml" 2>/dev/null || echo nextcloud)) noch nicht ausgefüllt – Slideshow startet nach dem Ausfüllen:"
    warn "  sudo systemctl start photoframe-slideshow"
fi

# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Installation abgeschlossen!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "  Web-UI:     http://$(hostname).local:8080"
echo "  Config:     $INSTALL_DIR/config.yaml"
echo "  Cache:      $CACHE_DIR"
echo "  Sync-Log:       $LOG_DIR/photoframe-sync.log"
echo "  Slideshow-Log:  $LOG_DIR/photoframe-slideshow.log"
echo "  Python:     $PYTHON"
echo ""
echo "  Dienste prüfen:"
echo "    sudo systemctl status photoframe-slideshow"
echo "    sudo systemctl status photoframe-webui"
echo "    sudo journalctl -u photoframe-sync -f"
echo ""
[[ -f "$INSTALL_DIR/config.yaml" ]] && \
    grep -q "mein_passwort\|DEIN_API_KEY" "$INSTALL_DIR/config.yaml" && \
    echo -e "${YELLOW}  ⚠ Bitte jetzt Config ausfüllen: sudo nano $INSTALL_DIR/config.yaml${NC}"
echo ""
