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
info "1/7 System-Pakete installieren"
# ---------------------------------------------------------------------------
apt-get update -qq
apt-get install -y \
    python3 python3-pip python3-venv \
    python3-pygame \
    python3-yaml \
    libraspberrypi-bin \
    mpv \
    git curl avahi-daemon \
    --no-install-recommends

# ---------------------------------------------------------------------------
info "2/7 Dateien installieren"
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
info "3/7 Python virtualenv einrichten (Trixie / PEP 668)"
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
info "4/7 Konfiguration anlegen"
# ---------------------------------------------------------------------------
if [[ ! -f "$INSTALL_DIR/config.yaml" ]]; then
    cp "$(dirname "$0")/config.yaml.example" "$INSTALL_DIR/config.yaml"
    warn "config.yaml angelegt – bitte Credentials eintragen:"
    warn "  sudo nano $INSTALL_DIR/config.yaml"
else
    info "config.yaml existiert bereits, wird nicht überschrieben"
fi

# ---------------------------------------------------------------------------
info "5/7 Benutzer-Berechtigungen setzen"
# ---------------------------------------------------------------------------
usermod -aG video,render "$FRAME_USER" 2>/dev/null || true

chown -R "$FRAME_USER:$FRAME_USER" "$CACHE_DIR"
chown -R "$FRAME_USER:$FRAME_USER" "$INSTALL_DIR"

touch "$LOG_DIR/photoframe-sync.log"
chown "$FRAME_USER:$FRAME_USER" "$LOG_DIR/photoframe-sync.log"

# ---------------------------------------------------------------------------
info "6/7 Konsole / Framebuffer konfigurieren"
# ---------------------------------------------------------------------------
systemctl disable getty@tty1 2>/dev/null || true

CMDLINE="$BOOT_DIR/cmdline.txt"
if [[ -f "$CMDLINE" ]] && ! grep -q "vt.global_cursor_default=0" "$CMDLINE"; then
    sed -i 's/$/ vt.global_cursor_default=0/' "$CMDLINE"
    info "Cursor-Blinken deaktiviert (wirkt nach Neustart)"
fi

CONFIG="$BOOT_DIR/config.txt"
if [[ -f "$CONFIG" ]] && ! grep -q "dtoverlay=vc4-kms-v3d" "$CONFIG"; then
    warn "vc4-kms-v3d nicht in $CONFIG gefunden"
    warn "Falls kein Bild erscheint: SDL_VIDEODRIVER=fbcon in den Service-Dateien setzen"
fi

# ---------------------------------------------------------------------------
info "7/7 systemd-Dienste einrichten"
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
Restart=always
RestartSec=5

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

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable photoframe-slideshow photoframe-sync.timer photoframe-webui
systemctl start  photoframe-sync.timer photoframe-webui

if grep -q "DEIN_API_KEY\|mein_passwort" "$INSTALL_DIR/config.yaml" 2>/dev/null; then
    warn "Config noch nicht ausgefüllt – Slideshow startet nach dem Ausfüllen:"
    warn "  sudo systemctl start photoframe-slideshow"
else
    systemctl start photoframe-slideshow
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
echo "  Sync-Log:   $LOG_DIR/photoframe-sync.log"
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
