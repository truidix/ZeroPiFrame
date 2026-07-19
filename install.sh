#!/bin/bash
# Photoframe - Installation script
# Run as: sudo bash install.sh
# Tested on: Raspberry Pi OS Lite 64-bit (Trixie / Debian 13), Pi Zero 2 W
#
# Trixie-specific changes compared to Bookworm:
#   - Boot partition is located under /boot/firmware/ (not /boot/)
#   - Python 3.13: pip uses virtualenv instead of --break-system-packages
#   - pygame is installed as a system package, venv with --system-site-packages

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Please run as root: sudo bash install.sh"

# Username: passed explicitly or derived from SUDO_USER
# Usage:  sudo bash install.sh [username]
# Example: sudo bash install.sh david
FRAME_USER="${1:-${SUDO_USER:-frame}}"
id "$FRAME_USER" &>/dev/null || error "User '$FRAME_USER' does not exist. Usage: sudo bash install.sh <username>"
info "Photoframe user: $FRAME_USER"

INSTALL_DIR="/opt/photoframe"
VENV_DIR="$INSTALL_DIR/venv"
CACHE_DIR="/var/lib/photoframe/cache"
LOG_DIR="/var/log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)/src"

# Trixie: boot partition under /boot/firmware/
BOOT_DIR="/boot/firmware"
[[ -d "$BOOT_DIR" ]] || BOOT_DIR="/boot"   # Fallback for older images
info "Boot directory: $BOOT_DIR"

# ---------------------------------------------------------------------------
info "0/10 Pausing running Photoframe services"
# ---------------------------------------------------------------------------
# The Zero 2 W has little headroom: apt/pip/venv work while the slideshow
# (pygame/KMSDRM + Pillow decoding, which also holds display ownership)
# and/or a sync (downloads, image processing) are running at the same time
# noticeably slows down the installation and makes the Pi feel like it's
# "hanging". If this happens to be a reinstall/update on an
# already-running Photoframe, the services are therefore stopped first -
# step 10/10 at the end enables and restarts them again regardless, this
# here is purely for the duration of the installation. On a fresh
# first-time install the units don't exist yet, so "is-active" is simply
# false and nothing happens.
SLIDESHOW_WAS_ACTIVE=0
for svc in photoframe-slideshow photoframe-sync.timer photoframe-sync photoframe-webui; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        info "Stopping $svc for the duration of the installation"
        [[ "$svc" == "photoframe-slideshow" ]] && SLIDESHOW_WAS_ACTIVE=1
        systemctl stop "$svc" 2>/dev/null || true
    fi
done

# ---------------------------------------------------------------------------
info "0b/10 Showing a notice image during installation"
# ---------------------------------------------------------------------------
# Only relevant if the slideshow was actually running above (otherwise the
# screen was already black/console before this - nothing to "replace").
# `fbi` is a tiny framebuffer image viewer (a single image, no rendering,
# no animation) - noticeably less RAM/CPU than the full pygame/slideshow
# instance, but it still prevents the screen from simply staying black or
# showing the text console during the (sometimes multi-minute)
# installation.
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
        info "Notice image is being displayed (PID $FBI_PID)"
        # Safety net: if the script aborts prematurely (set -e on an error
        # in a later step), the notice image should still not stay on
        # screen forever, falsely suggesting a successful completion that
        # never actually happened.
        trap '[[ -n "$FBI_PID" ]] && kill "$FBI_PID" 2>/dev/null || true' EXIT
    else
        warn "fbi/framebuffer not available - no notice image during installation"
    fi
fi

# ---------------------------------------------------------------------------
info "1/10 Installing system packages"
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
    info "vcgencmd package: libraspberrypi-bin"
else
    info "libraspberrypi-bin not installable (Trixie) - installing raspi-utils-core/raspi-utils-dt"
    apt-get install -y --no-install-recommends raspi-utils-core raspi-utils-dt
fi

command -v vcgencmd &>/dev/null || \
    warn "vcgencmd not found - HDMI schedule (on/off) will not work"

# ---------------------------------------------------------------------------
info "2/10 Installing files"
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
info "3/10 Setting up Python virtualenv (Trixie / PEP 668)"
# ---------------------------------------------------------------------------
# pygame and PyYAML come as system packages (apt), everything else via pip
# in the venv. --system-site-packages makes system packages (pygame)
# visible inside the venv.
python3 -m venv --system-site-packages "$VENV_DIR"

# Only install packages that aren't already available as apt packages
"$VENV_DIR/bin/pip" install --quiet \
    webdavclient3 \
    requests \
    Flask \
    Pillow

info "virtualenv: $VENV_DIR"

# ---------------------------------------------------------------------------
info "4/10 Creating configuration"
# ---------------------------------------------------------------------------
if [[ ! -f "$INSTALL_DIR/config.yaml" ]]; then
    cp "$(dirname "$0")/config.yaml.example" "$INSTALL_DIR/config.yaml"
    warn "config.yaml created - please fill in credentials:"
    warn "  sudo nano $INSTALL_DIR/config.yaml"
else
    info "config.yaml already exists, not overwriting"
fi

# ---------------------------------------------------------------------------
info "5/10 Setting user permissions"
# ---------------------------------------------------------------------------
usermod -aG video,render "$FRAME_USER" 2>/dev/null || true

chown -R "$FRAME_USER:$FRAME_USER" "$CACHE_DIR"
chown -R "$FRAME_USER:$FRAME_USER" "$INSTALL_DIR"

touch "$LOG_DIR/photoframe-sync.log"
chown "$FRAME_USER:$FRAME_USER" "$LOG_DIR/photoframe-sync.log"
touch "$LOG_DIR/photoframe-slideshow.log"
chown "$FRAME_USER:$FRAME_USER" "$LOG_DIR/photoframe-slideshow.log"

# ---------------------------------------------------------------------------
info "6/10 Configuring console / framebuffer"
# ---------------------------------------------------------------------------
systemctl disable getty@tty1 2>/dev/null || true

CMDLINE="$BOOT_DIR/cmdline.txt"
if [[ -f "$CMDLINE" ]] && ! grep -q "vt.global_cursor_default=0" "$CMDLINE"; then
    sed -i 's/$/ vt.global_cursor_default=0/' "$CMDLINE"
    info "Cursor blinking disabled (takes effect after reboot)"
fi

CONFIG="$BOOT_DIR/config.txt"
if [[ -f "$CONFIG" ]]; then
    cp "$CONFIG" "$CONFIG.photoframe.bak"

    if ! grep -q "^dtoverlay=vc4-kms-v3d" "$CONFIG"; then
        echo "dtoverlay=vc4-kms-v3d" >> "$CONFIG"
        info "vc4-kms-v3d (full KMS driver) added to $CONFIG - without this the screen stays black"
    fi
    # The screen is permanently connected: always treat HDMI as connected
    # (otherwise sometimes no picture after a cold boot) and no overscan
    # borders/boot rainbow splash on a dedicated kiosk display.
    grep -q "^hdmi_force_hotplug=1" "$CONFIG" || echo "hdmi_force_hotplug=1" >> "$CONFIG"
    grep -q "^disable_overscan=1"  "$CONFIG" || echo "disable_overscan=1"  >> "$CONFIG"
    grep -q "^disable_splash=1"    "$CONFIG" || echo "disable_splash=1"    >> "$CONFIG"
    info "config.txt adjusted (backup: $CONFIG.photoframe.bak)"
else
    warn "$CONFIG not found - check vc4-kms-v3d manually"
    warn "If no picture appears: set SDL_VIDEODRIVER=fbcon in the service files"
fi

# ---------------------------------------------------------------------------
info "7/10 Swap & WiFi power-save mode (512 MB RAM is tight)"
# ---------------------------------------------------------------------------
# Decoding large photos with Pillow, plus pygame, Flask, and occasionally
# mpv all at once can get tight on a Zero 2 W with 512 MB RAM. A bit of
# swap as a safety net against OOM kills is cheaper than a crashing service.
if [[ -f /etc/dphys-swapfile ]]; then
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
    grep -q "^CONF_SWAPSIZE=" /etc/dphys-swapfile || echo "CONF_SWAPSIZE=512" >> /etc/dphys-swapfile
    dphys-swapfile setup  >/dev/null 2>&1 || true
    systemctl restart dphys-swapfile 2>/dev/null || true
    info "Swap set to 512 MB"
else
    warn "dphys-swapfile not found - set up swap manually if RAM gets tight"
fi

# The Zero 2 W has no Ethernet - WiFi power-save otherwise causes
# noticeable delays/dropouts during sync and in the web UI.
cat > /etc/systemd/system/photoframe-wifi-powersave-off.service << 'EOF'
[Unit]
Description=Photoframe: Disable WiFi power-save mode
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
    warn "Could not disable WiFi power-save (no wlan0? using USB-LAN or similar?)"

# ---------------------------------------------------------------------------
info "8/10 Setting up systemd services"
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
# Higher priority than sync/web UI: the slideshow should keep running
# smoothly even when the hourly sync is using CPU/SD card in the
# background.
Nice=-5
IOSchedulingClass=best-effort
IOSchedulingPriority=2

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/photoframe-sync.service << EOF
[Unit]
Description=Photoframe Sync (one-off run)
After=network-online.target
Wants=network-online.target

[Service]
User=$FRAME_USER
Type=oneshot
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON $INSTALL_DIR/sync.py
StandardOutput=append:$LOG_DIR/photoframe-sync.log
StandardError=append:$LOG_DIR/photoframe-sync.log
# Lower priority: downloads shouldn't slow down the slideshow.
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
info "9/10 Setting up sudo permissions for the web UI"
# ---------------------------------------------------------------------------
# photoframe-webui.service deliberately runs unprivileged as $FRAME_USER.
# But "Sync now" and "Restart slideshow" in the web UI internally call
# `systemctl start/restart` on another system service - that normally
# requires root, and since there's no interactive desktop session here,
# PolicyKit can't prompt for a password and immediately refuses with
# "Interactive authentication required". A tightly scoped sudoers rule for
# exactly these commands fixes that, without giving the web UI process
# general root privileges.
#
# The same applies for the HDMI schedule (display settings), additionally
# for writing the timer unit files to /etc/systemd/system/ - there's a
# dedicated root helper script for that, which the web UI is likewise only
# allowed to invoke via sudo.

cat > "$INSTALL_DIR/apply-hdmi-schedule.sh" << 'EOF'
#!/bin/bash
# Sets (or disables) the HDMI on/off schedule. Runs as root (via sudo, see
# /etc/sudoers.d/photoframe) - therefore validates its inputs defensively
# before anything is written to /etc/systemd/system/.
#
# Usage:
#   apply-hdmi-schedule.sh HH:MM HH:MM   (on time, off time)
#   apply-hdmi-schedule.sh disable
set -euo pipefail

TIME_RE='^([01][0-9]|2[0-3]):[0-5][0-9]$'

if [[ "${1:-}" == "disable" ]]; then
    systemctl disable --now photoframe-hdmi-on.timer photoframe-hdmi-off.timer 2>/dev/null || true
    exit 0
fi

ON_TIME="${1:-}"
OFF_TIME="${2:-}"

[[ "$ON_TIME"  =~ $TIME_RE ]] || { echo "Invalid on-time: $ON_TIME" >&2; exit 1; }
[[ "$OFF_TIME" =~ $TIME_RE ]] || { echo "Invalid off-time: $OFF_TIME" >&2; exit 1; }

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

# Auto-shutdown schedule: for operation on a timer switch/smart plug
# without an orderly shutdown. Instead of cutting power raw (risk of SD
# card corruption on every power-off), the Pi shuts itself down cleanly a
# few minutes BEFORE the plug's scheduled power-off time.
# Important: Persistent=false (the default) - otherwise the timer would
# think, on the next boot, that it needs to catch up on the missed run,
# and shut down again immediately.
cat > "$INSTALL_DIR/apply-shutdown-schedule.sh" << 'EOF'
#!/bin/bash
# Sets (or disables) the automatic shutdown time.
# Runs as root (via sudo, see /etc/sudoers.d/photoframe).
#
# Usage:
#   apply-shutdown-schedule.sh HH:MM
#   apply-shutdown-schedule.sh disable
set -euo pipefail

TIME_RE='^([01][0-9]|2[0-3]):[0-5][0-9]$'

if [[ "${1:-}" == "disable" ]]; then
    systemctl disable --now photoframe-shutdown.timer 2>/dev/null || true
    exit 0
fi

SHUTDOWN_TIME="${1:-}"
[[ "$SHUTDOWN_TIME" =~ $TIME_RE ]] || { echo "Invalid time: $SHUTDOWN_TIME" >&2; exit 1; }
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

# Sync interval: photoframe-sync.timer is created above with a fixed
# OnUnitActiveSec=60min. The web UI's "Sync interval" field only writes to
# config.yaml - without this helper script, changing the value in the web
# UI would have no effect at all on the actual timer.
cat > "$INSTALL_DIR/apply-sync-interval.sh" << 'EOF'
#!/bin/bash
# Sets the sync interval of photoframe-sync.timer.
# Runs as root (via sudo, see /etc/sudoers.d/photoframe).
#
# Usage: apply-sync-interval.sh <minutes>
set -euo pipefail

MINUTES="${1:-}"
[[ "$MINUTES" =~ ^[0-9]+$ ]] || { echo "Invalid interval: $MINUTES" >&2; exit 1; }
(( MINUTES >= 5 && MINUTES <= 1440 )) || { echo "Interval must be between 5 and 1440 minutes" >&2; exit 1; }

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
# restart instead of reload: the new OnUnitActiveSec period should be
# recalculated starting right now, instead of only after the old period
# elapses.
systemctl restart photoframe-sync.timer
EOF
chown root:root "$INSTALL_DIR/apply-sync-interval.sh"
chmod 700 "$INSTALL_DIR/apply-sync-interval.sh"

# Permanently enables/disables the scheduled auto-sync (survives a
# reboot) - unlike a plain "systemctl stop", which would only pause the
# timer until the next boot since it would remain enabled.
cat > "$INSTALL_DIR/apply-sync-enabled.sh" << 'EOF'
#!/bin/bash
# Permanently enables/disables the automatic (scheduled) sync.
# Runs as root (via sudo, see /etc/sudoers.d/photoframe).
#
# Usage: apply-sync-enabled.sh enable|disable
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
    echo "Usage: apply-sync-enabled.sh enable|disable" >&2
    exit 1
    ;;
esac
EOF
chown root:root "$INSTALL_DIR/apply-sync-enabled.sh"
chmod 700 "$INSTALL_DIR/apply-sync-enabled.sh"

cat > /etc/sudoers.d/photoframe << EOF
# Generated by install.sh - only the specific commands that
# photoframe-webui.service (running as $FRAME_USER) needs for "sync
# now/stop", "start/stop/restart slideshow", the HDMI schedule, the
# auto-shutdown schedule, and enabling/disabling the auto-sync timer. No
# general sudo/root grant.
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
visudo -c -f /etc/sudoers.d/photoframe || error "sudoers file invalid - please check /etc/sudoers.d/photoframe"
info "sudoers rule created: /etc/sudoers.d/photoframe"

# ---------------------------------------------------------------------------
info "10/10 Enabling services"
# ---------------------------------------------------------------------------
systemctl daemon-reload
systemctl enable photoframe-slideshow photoframe-sync.timer photoframe-webui
# restart instead of start: reliably brings up both freshly installed
# services and ones that were paused for the installation (step 0/10) -
# and, on a reinstall (e.g. to roll out a fix), ensures that already
# running services actually pick up the new code instead of continuing to
# run unchanged.
systemctl restart photoframe-sync.timer photoframe-webui

# Only stop the notice image now (if it was started in step 0b) - as close
# as possible to right before the real slideshow regains display
# ownership, so the screen shows blank/console in between for as short a
# time as possible.
if [[ -n "$FBI_PID" ]] && kill -0 "$FBI_PID" 2>/dev/null; then
    kill "$FBI_PID" 2>/dev/null || true
    wait "$FBI_PID" 2>/dev/null || true
fi

# Checks ONLY the fields of the currently active source (source:
# nextcloud/immich) for placeholder values - a simple grep over the whole
# file (earlier version) falsely triggered as soon as a placeholder was
# still present anywhere in the file, even in the source that's NOT
# currently in use (e.g. Immich configured and working, but the unused
# Nextcloud section still contains "mein_passwort" from the template).
# This falsely left the slideshow disabled on every reinstall/update, even
# though the configuration for the source actually in use had long been
# complete.
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
    warn "Config for the active source ($(grep -oP '^source:\s*\K\S+' "$INSTALL_DIR/config.yaml" 2>/dev/null || echo nextcloud)) not filled in yet - the slideshow will start once it's filled in:"
    warn "  sudo systemctl start photoframe-slideshow"
fi

# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "  Web-UI:         http://$(hostname).local:8080"
echo "  Config:         $INSTALL_DIR/config.yaml"
echo "  Cache:          $CACHE_DIR"
echo "  Sync log:       $LOG_DIR/photoframe-sync.log"
echo "  Slideshow log:  $LOG_DIR/photoframe-slideshow.log"
echo "  Python:         $PYTHON"
echo ""
echo "  Check services:"
echo "    sudo systemctl status photoframe-slideshow"
echo "    sudo systemctl status photoframe-webui"
echo "    sudo journalctl -u photoframe-sync -f"
echo ""
[[ -f "$INSTALL_DIR/config.yaml" ]] && \
    grep -q "mein_passwort\|DEIN_API_KEY" "$INSTALL_DIR/config.yaml" && \
    echo -e "${YELLOW}  ⚠ Please fill in the config now: sudo nano $INSTALL_DIR/config.yaml${NC}"
echo ""
