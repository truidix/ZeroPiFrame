#!/bin/bash
# ZeroPiFrame - Installation script
# Run as: sudo bash install.sh
# Tested on: Raspberry Pi OS Lite 64-bit (Trixie / Debian 13), Pi Zero 2 W
#
# Trixie-specific changes compared to Bookworm:
#   - Boot partition is located under /boot/firmware/ (not /boot/)
#   - Python 3.13: pip uses virtualenv instead of --break-system-packages
#   - pygame is installed as a system package, venv with --system-site-packages
#
# Quick code deploy (no apt/venv/ZeroPlay build - just for iterating on
# slideshow.py/sync.py/webui.py/templates/translations without waiting
# through the full install every time):
#   sudo bash install.sh --deploy
# Only use this once a full install has already been run at least once
# (needs the venv/systemd units/config from that to already exist). If
# requirements.txt changed, or you need the ZeroPlay/apt packages
# refreshed, run the full install instead.

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Please run as root: sudo bash install.sh"

# --deploy can appear anywhere in the arguments (before or after the
# optional username) - pulled out here so the username positional
# argument below still works unchanged either way.
DEPLOY_MODE=0
ARGS=()
for arg in "$@"; do
    if [[ "$arg" == "--deploy" ]]; then
        DEPLOY_MODE=1
    else
        ARGS+=("$arg")
    fi
done
set -- "${ARGS[@]}"

# Username: passed explicitly or derived from SUDO_USER
# Usage:  sudo bash install.sh [username]
# Example: sudo bash install.sh david
FRAME_USER="${1:-${SUDO_USER:-frame}}"
id "$FRAME_USER" &>/dev/null || error "User '$FRAME_USER' does not exist. Usage: sudo bash install.sh <username>"
info "ZeroPiFrame user: $FRAME_USER"

INSTALL_DIR="/opt/zeropiframe"
VENV_DIR="$INSTALL_DIR/venv"
CACHE_DIR="/var/lib/zeropiframe/cache"
LOG_DIR="/var/log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)/src"
# Repo root (parent of src/) - recorded below into $INSTALL_DIR/.source_dir
# so the web UI's "Check for updates" button (see update.sh, generated in
# step 9/10) knows where to `git pull` from without having to guess or
# hardcode a path - this repo can live anywhere (~/zeropiframe-src per
# SETUP.md, but nothing enforces that).
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# ---------------------------------------------------------------------------
# Deploy mode: copy the changed source files onto an already-installed
# system and restart the two long-running services (slideshow, web UI) so
# they pick up the new code - skips apt-get, the venv/pip install, the
# ZeroPlay source build, and every boot/config.txt/systemd-unit change,
# which are the slow parts and don't need to be redone just because a
# .py/.html/.json file changed. zeropiframe-sync doesn't need a restart
# here: it's a systemd oneshot triggered fresh by the timer each run, so
# simply having the updated sync.py in place is enough for its next run.
# ---------------------------------------------------------------------------
if [[ "$DEPLOY_MODE" -eq 1 ]]; then
    [[ -d "$INSTALL_DIR" && -x "$VENV_DIR/bin/python3" ]] || \
        error "$INSTALL_DIR (or its venv) doesn't exist yet - run a full install first: sudo bash install.sh"

    info "Deploy: copying source files"
    cp "$SCRIPT_DIR/slideshow.py"          "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/sync.py"               "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/webui.py"              "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/i18n.py"               "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/hw.py"                 "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/templates/"*.html     "$INSTALL_DIR/templates/"
    cp "$SCRIPT_DIR/static/"*.css         "$INSTALL_DIR/static/"
    cp "$SCRIPT_DIR/translations/"*.json  "$INSTALL_DIR/translations/"
    echo "$REPO_DIR" > "$INSTALL_DIR/.source_dir"

    chown -R "$FRAME_USER:$FRAME_USER" "$INSTALL_DIR"
    chmod -R 755 "$INSTALL_DIR"

    info "Deploy: restarting services"
    systemctl daemon-reload
    systemctl restart zeropiframe-webui
    # Only bounce the slideshow if it's actually supposed to be running -
    # a deploy shouldn't be what turns it on for someone who deliberately
    # disabled it.
    if systemctl is-enabled --quiet zeropiframe-slideshow 2>/dev/null || \
       systemctl is-active  --quiet zeropiframe-slideshow 2>/dev/null; then
        systemctl restart zeropiframe-slideshow
    fi

    info "Deploy complete - slideshow and web UI are running the updated code"
    exit 0
fi

# Trixie: boot partition under /boot/firmware/
BOOT_DIR="/boot/firmware"
[[ -d "$BOOT_DIR" ]] || BOOT_DIR="/boot"   # Fallback for older images
info "Boot directory: $BOOT_DIR"

# ---------------------------------------------------------------------------
# Migrating an existing "photoframe" install (this project's old name,
# before it was renamed to ZeroPiFrame/zeropiframe) - stops the old units,
# moves config.yaml and the photo cache over untouched (no re-entering
# credentials, no re-downloading everything), and removes the old
# systemd/sudoers files. Detected via the old install dir, cache dir, or
# any old unit file still being present - any one of those means this
# ran here before under the old name.
#
# The venv is deliberately NOT moved, just left behind for
# rm -rf below - a venv's own activate script and shebang lines hardcode
# its absolute path, so moving the directory would leave it silently
# broken. Step 3/10 further down builds a fresh one under the new path
# instead, which is barely slower than a plain directory move anyway.
#
# Old log files are deliberately left in place (not deleted) - only
# harmless historical records once the *-sync.log/*-slideshow.log names
# below start being written under the new names instead.
# ---------------------------------------------------------------------------
# Declared here (before step 0/10 below, which is the "normal" place
# this flag gets set) so that if the OLD slideshow was the one actually
# running, that fact survives into step 0b's notice-image decision -
# step 0/10 only ever sets this to 1, never unconditionally resets it,
# so setting it here first is safe either way.
SLIDESHOW_WAS_ACTIVE=0

OLD_INSTALL_DIR="/opt/photoframe"
OLD_CACHE_PARENT="/var/lib/photoframe"
if [[ -d "$OLD_INSTALL_DIR" || -d "$OLD_CACHE_PARENT" ]] || \
   systemctl list-unit-files photoframe-slideshow.service &>/dev/null; then
    info "0a/10 Migrating an existing 'photoframe' install to zeropiframe"

    if systemctl is-active --quiet photoframe-slideshow 2>/dev/null; then
        SLIDESHOW_WAS_ACTIVE=1
    fi
    for svc in photoframe-slideshow photoframe-sync.timer photoframe-sync photoframe-webui \
               photoframe-hdmi-on.timer photoframe-hdmi-off.timer photoframe-shutdown.timer; do
        systemctl stop "$svc" 2>/dev/null || true
        systemctl disable "$svc" 2>/dev/null || true
    done
    rm -f /etc/systemd/system/photoframe-*.service /etc/systemd/system/photoframe-*.timer
    systemctl daemon-reload

    if [[ -f "$OLD_INSTALL_DIR/config.yaml" && ! -f "$INSTALL_DIR/config.yaml" ]]; then
        mkdir -p "$INSTALL_DIR"
        mv "$OLD_INSTALL_DIR/config.yaml" "$INSTALL_DIR/config.yaml"
        info "Migrated config.yaml -> $INSTALL_DIR/config.yaml"
    fi
    if [[ -d "$OLD_CACHE_PARENT/cache" && ! -d "$CACHE_DIR" ]]; then
        mkdir -p "$(dirname "$CACHE_DIR")"
        mv "$OLD_CACHE_PARENT/cache" "$CACHE_DIR"
        info "Migrated photo cache -> $CACHE_DIR (no re-download needed)"
    fi
    [[ -f "$OLD_CACHE_PARENT/current.json" && ! -f "$(dirname "$CACHE_DIR")/current.json" ]] && \
        mv "$OLD_CACHE_PARENT/current.json" "$(dirname "$CACHE_DIR")/current.json"

    rm -f /etc/sudoers.d/photoframe
    rm -rf "$OLD_INSTALL_DIR" /opt/photoframe-build
    # Only removes the old cache's PARENT dir if the cache subfolder is
    # actually gone from it (i.e. the move above succeeded, or there was
    # never one to move) - if $CACHE_DIR somehow already existed and the
    # move was skipped above, this leaves the untouched old photos in
    # place instead of silently deleting real data.
    if [[ ! -d "$OLD_CACHE_PARENT/cache" ]]; then
        rm -rf "$OLD_CACHE_PARENT"
    else
        warn "$OLD_CACHE_PARENT/cache still has files ($CACHE_DIR already existed) - left in place, not deleted"
    fi

    info "Migration complete - continuing with the regular zeropiframe install/update below"
fi

# ---------------------------------------------------------------------------
info "0/10 Pausing running ZeroPiFrame services"
# ---------------------------------------------------------------------------
# The Zero 2 W has little headroom: apt/pip/venv work while the slideshow
# (pygame/KMSDRM + Pillow decoding, which also holds display ownership)
# and/or a sync (downloads, image processing) are running at the same time
# noticeably slows down the installation and makes the Pi feel like it's
# "hanging". If this happens to be a reinstall/update on an
# already-running ZeroPiFrame, the services are therefore stopped first -
# step 10/10 at the end enables and restarts them again regardless, this
# here is purely for the duration of the installation. On a fresh
# first-time install the units don't exist yet, so "is-active" is simply
# false and nothing happens. (SLIDESHOW_WAS_ACTIVE is initialized above,
# before the migration step - not reset here, so a migration from the old
# "photoframe" naming isn't silently lost if that's what was running.)
for svc in zeropiframe-slideshow zeropiframe-sync.timer zeropiframe-sync zeropiframe-webui; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        info "Stopping $svc for the duration of the installation"
        [[ "$svc" == "zeropiframe-slideshow" ]] && SLIDESHOW_WAS_ACTIVE=1
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
# Killing fbi only stops it from drawing new frames - the pixels it
# already wrote stay resident in VT1's saved screen state (that's how the
# kernel VT subsystem preserves each console's own contents across
# switches) until something else overwrites them. Under normal continuous
# slideshow operation that's invisible, since pygame reclaims DRM master
# and redraws immediately - but slideshow.py's own chvt dance around
# video playback (BLANK_VT/CONSOLE_VT, see its play_video()) briefly
# re-exposes VT1's raw state with nothing yet drawn over it, which is
# exactly the gap this leftover "please wait" notice image was bleeding
# through into. Zeroing /dev/fb0 right after killing fbi - while VT1 is
# still the active console, since that's the VT fbi (-T 1) drew onto -
# ensures nothing stale is left behind for that gap to later reveal.
_clear_notice_fb() {
    [[ -e /dev/fb0 ]] && dd if=/dev/zero of=/dev/fb0 2>/dev/null
    true
}
if [[ "$SLIDESHOW_WAS_ACTIVE" -eq 1 ]]; then
    if ! command -v fbi &>/dev/null; then
        apt-get install -y --no-install-recommends fbi \
            > /tmp/zeropiframe-fbi-install.log 2>&1 || true
    fi
    # Pick the notice image matching the configured UI language, falling
    # back to English if config.yaml doesn't exist yet, has no 'language'
    # key, or there's no dedicated image for that language (e.g. a language
    # was added to the web UI but nobody has regenerated the placeholder
    # images for it yet via tools/generate_placeholder.py).
    NOTICE_LANG="en"
    if [[ -f /opt/zeropiframe/config.yaml ]]; then
        CONFIGURED_LANG="$(grep -m1 '^language:' /opt/zeropiframe/config.yaml 2>/dev/null \
            | sed -E "s/^language:[[:space:]]*[\"']?([A-Za-z_-]+)[\"']?.*/\1/")"
        [[ -n "$CONFIGURED_LANG" ]] && NOTICE_LANG="$CONFIGURED_LANG"
    fi
    NOTICE_IMAGE="$SCRIPT_DIR/assets/update-please-wait-$NOTICE_LANG.png"
    [[ -f "$NOTICE_IMAGE" ]] || NOTICE_IMAGE="$SCRIPT_DIR/assets/update-please-wait-en.png"
    if command -v fbi &>/dev/null && [[ -e /dev/fb0 ]]; then
        # -t + -1 bound fbi's own runtime as a hard safety net: if this
        # script's SSH session drops abruptly (so the EXIT trap below never
        # runs), fbi would otherwise sit on /dev/fb0 forever as an orphaned,
        # root-owned process - permanently stuck showing the notice image
        # even after the slideshow tries to take over the framebuffer again.
        # With only one image, -t alone just redisplays it forever every
        # 900s without ever exiting; -1/--once tells fbi not to loop, so it
        # quits once that single interval elapses. 900s (15 min) comfortably
        # covers a normal install (incl. slow apt-get on poor WiFi) while
        # still bounding the worst case.
        fbi -T 1 -t 900 -1 -d /dev/fb0 -a --noverbose \
            "$NOTICE_IMAGE" \
            < /dev/null > /tmp/zeropiframe-fbi.log 2>&1 &
        FBI_PID=$!
        disown "$FBI_PID" 2>/dev/null || true
        info "Notice image is being displayed (PID $FBI_PID)"
        # Safety net: if the script aborts prematurely (set -e on an error
        # in a later step), the notice image should still not stay on
        # screen forever, falsely suggesting a successful completion that
        # never actually happened. Also clears the leftover frame itself
        # (see _clear_notice_fb above) - not just the process.
        trap '[[ -n "$FBI_PID" ]] && { kill "$FBI_PID" 2>/dev/null; sleep 0.2; _clear_notice_fb; } || true' EXIT
    else
        warn "fbi/framebuffer not available - no notice image during installation"
    fi
fi

# ---------------------------------------------------------------------------
info "1/10 Installing system packages"
# ---------------------------------------------------------------------------
apt-get update -qq

# fbi: installed unconditionally so it's already present the next time
# step 0b needs it (on a future reinstall/update where the slideshow is
# actively running) - step 0b's own conditional apt-get install only
# covers that one run, not any run after it.
apt-get install -y \
    python3 python3-pip python3-venv \
    python3-pygame \
    python3-yaml \
    mpv \
    vlc-bin vlc-plugin-base \
    ffmpeg \
    iw \
    git curl avahi-daemon \
    fbi \
    --no-install-recommends

# ZeroPlay: a third video player option (alongside mpv/vlc, selectable in
# the web UI) - a lightweight, modern V4L2 M2M + DRM/KMS replacement for
# the deprecated omxplayer, purpose-built for exactly this device/OS combo
# rather than a general-purpose player where headless DRM output is a
# secondary feature (which is where VLC's playback-pacing bug turned up).
# Not packaged in apt - built from source here using the project's own
# documented manual-build steps (its README also offers a curl|bash
# installer, skipped in favor of the same auditable approach the rest of
# this script uses). Best-effort: mpv/VLC stay available and remain
# selectable either way, so a build failure here (e.g. a transient
# network hiccup cloning the repo) shouldn't abort the whole install.
apt-get install -y --no-install-recommends \
    gcc make pkgconf \
    libavformat-dev libavcodec-dev libavutil-dev libswresample-dev libswscale-dev \
    libdrm-dev libasound2-dev libcjson-dev libfreetype-dev \
    || warn "Could not install ZeroPlay build dependencies - ZeroPlay will not be available"

# libfreetype-dev above only provides the headers ZeroPlay needs to build
# WITH font support - it does not itself install any actual font files.
# Without the .ttf file ZeroPlay expects to find at runtime
# (/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf), it silently
# falls back to its built-in bitmap font for subtitles - harmless, but
# logs a "cannot load ... using bitmap font" line on every single video.
# fonts-dejavu-core is the actual font package, separate from the -dev
# build dependency above.
apt-get install -y --no-install-recommends fonts-dejavu-core \
    || warn "Could not install fonts-dejavu-core - ZeroPlay will use its bitmap font fallback for subtitles"

ZEROPLAY_SRC=/opt/zeropiframe-build/zeroplay
if command -v gcc &>/dev/null && command -v make &>/dev/null; then
    mkdir -p "$(dirname "$ZEROPLAY_SRC")"
    : > /tmp/zeropiframe-zeroplay-build.log
    if [[ -d "$ZEROPLAY_SRC/.git" ]]; then
        git -C "$ZEROPLAY_SRC" pull --ff-only >> /tmp/zeropiframe-zeroplay-build.log 2>&1
    else
        rm -rf "$ZEROPLAY_SRC"
        git clone --depth 1 https://github.com/HorseyofCoursey/zeroplay.git "$ZEROPLAY_SRC" \
            >> /tmp/zeropiframe-zeroplay-build.log 2>&1
    fi
    if [[ -d "$ZEROPLAY_SRC" ]] && (cd "$ZEROPLAY_SRC" \
            && make >> /tmp/zeropiframe-zeroplay-build.log 2>&1 \
            && make install >> /tmp/zeropiframe-zeroplay-build.log 2>&1); then
        info "ZeroPlay built and installed ($(command -v zeroplay || echo /usr/local/bin/zeroplay))"
    else
        warn "ZeroPlay build failed - see /tmp/zeropiframe-zeroplay-build.log - mpv/VLC remain available"
    fi
fi

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
       > /tmp/zeropiframe-vcgencmd-install.log 2>&1; then
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
mkdir -p "$INSTALL_DIR/templates" "$INSTALL_DIR/static" \
         "$INSTALL_DIR/translations" "$CACHE_DIR"

cp "$SCRIPT_DIR/slideshow.py"          "$INSTALL_DIR/"
cp "$SCRIPT_DIR/sync.py"               "$INSTALL_DIR/"
cp "$SCRIPT_DIR/webui.py"              "$INSTALL_DIR/"
cp "$SCRIPT_DIR/i18n.py"               "$INSTALL_DIR/"
cp "$SCRIPT_DIR/hw.py"                 "$INSTALL_DIR/"
cp "$SCRIPT_DIR/templates/"*.html     "$INSTALL_DIR/templates/"
cp "$SCRIPT_DIR/static/"*.css         "$INSTALL_DIR/static/"
cp "$SCRIPT_DIR/translations/"*.json  "$INSTALL_DIR/translations/"
cp "$(dirname "$0")/requirements.txt" "$INSTALL_DIR/"
echo "$REPO_DIR" > "$INSTALL_DIR/.source_dir"

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
# The parent of $CACHE_DIR (/var/lib/zeropiframe) is where slideshow.py
# also writes current.json (the "currently displayed" status-page state
# file) - chown -R above only covers $CACHE_DIR itself, not its parent,
# which was otherwise left root-owned from the mkdir -p in step 2/10,
# causing a permission-denied on every single image/video change.
chown "$FRAME_USER:$FRAME_USER" "$(dirname "$CACHE_DIR")"
chown -R "$FRAME_USER:$FRAME_USER" "$INSTALL_DIR"

touch "$LOG_DIR/zeropiframe-sync.log"
chown "$FRAME_USER:$FRAME_USER" "$LOG_DIR/zeropiframe-sync.log"
touch "$LOG_DIR/zeropiframe-slideshow.log"
chown "$FRAME_USER:$FRAME_USER" "$LOG_DIR/zeropiframe-slideshow.log"

# ---------------------------------------------------------------------------
info "6/10 Configuring console / framebuffer"
# ---------------------------------------------------------------------------
# --now (not just disable) also stops an already-running getty@tty1
# immediately - matters on a reinstall/migration (like this one) where a
# getty may have been sitting on tty1 since an earlier boot, well before
# this line ever ran. Without --now, "disable" alone only prevents it
# from starting on the NEXT boot - the already-running instance (and
# whatever it's currently printing/prompting) would keep occupying tty1
# until a reboot, which is exactly what the chvt-based hiding below
# would then briefly reveal around every video.
systemctl disable --now getty@tty1 2>/dev/null || true

# Reserves VT7 as a permanently empty "blank" console - slideshow.py
# switches to it for the brief moment around every video (pygame
# releasing/reclaiming the DRM master) instead of letting tty1's
# deliberately-verbose console text flash on screen during normal
# playback (see slideshow.py's play_video() for the actual chvt calls).
# Masking (not just disabling) also blocks systemd's autovt@ mechanism,
# which would otherwise auto-spawn a getty here the first time anything
# switches to this VT.
systemctl mask --now getty@tty7.service autovt@tty7.service 2>/dev/null || true

# An earlier version of this script quieted the kernel/systemd boot and
# shutdown messages on the framebuffer console (tty1) via cmdline.txt
# flags (vt.global_cursor_default=0, quiet, loglevel=3, logo.nologo,
# consoleblank=0, systemd.show_status=0), meant to be covered by a
# dedicated boot/shutdown splash image. That splash was tried and then
# abandoned (see the comment near the end of step 8/10) after repeated
# on-device failures - so quieting the console just left it blank with
# nothing to look at instead, which isn't wanted on its own. Removes those
# flags again (on a reinstall/update of a Pi that still has them from an
# older install) to restore the normal, verbose console output.
CMDLINE="$BOOT_DIR/cmdline.txt"
if [[ -f "$CMDLINE" ]]; then
    ORIG_LINE="$(cat "$CMDLINE")"
    NEW_LINE=""
    for word in $ORIG_LINE; do
        case "$word" in
            "vt.global_cursor_default=0"|"quiet"|"loglevel=3"|"logo.nologo"|"consoleblank=0"|"systemd.show_status=0")
                continue ;;
        esac
        NEW_LINE+="$word "
    done
    NEW_LINE="${NEW_LINE% }"
    if [[ "$NEW_LINE" != "$ORIG_LINE" ]]; then
        cp "$CMDLINE" "$CMDLINE.zeropiframe-quiet.bak"
        printf '%s\n' "$NEW_LINE" > "$CMDLINE"
        info "cmdline.txt: removed boot-quieting flags (splash feature abandoned) - reboot to see normal console output again (backup: $CMDLINE.zeropiframe-quiet.bak)"
    fi
fi

CONFIG="$BOOT_DIR/config.txt"
if [[ -f "$CONFIG" ]]; then
    cp "$CONFIG" "$CONFIG.zeropiframe.bak"

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
    # Bluetooth isn't used by this project - disabling it at the hardware
    # level frees a little RAM (bluetoothd + hciuart, otherwise running
    # for nothing) and one less thing competing for CPU/power on an
    # already RAM-constrained device. Modest savings on their own, but a
    # free win with no downside if you don't need BT.
    grep -q "^dtoverlay=disable-bt" "$CONFIG" || echo "dtoverlay=disable-bt" >> "$CONFIG"
    info "config.txt adjusted (backup: $CONFIG.zeropiframe.bak)"
else
    warn "$CONFIG not found - check vc4-kms-v3d manually"
    warn "If no picture appears: set SDL_VIDEODRIVER=fbcon in the service files"
fi

systemctl disable --now hciuart.service bluetooth.service 2>/dev/null || true
systemctl mask hciuart.service bluetooth.service 2>/dev/null || true
# mpris-proxy (BlueZ's Bluetooth-media-to-D-Bus bridge) is a separate,
# per-user systemd unit - NOT gated by bluetooth.service/hciuart.service
# above, so it was still starting (and showing up as an OOM victim) even
# with those masked. --global masks it for every user's session, present
# and future.
systemctl --global mask mpris-proxy.service 2>/dev/null || true

# ---------------------------------------------------------------------------
info "7/10 Swap & WiFi power-save mode (512 MB RAM is tight)"
# ---------------------------------------------------------------------------
# Decoding large photos with Pillow, plus pygame, Flask, and occasionally
# mpv all at once can get tight on a Zero 2 W with 512 MB RAM - confirmed
# on-device via dmesg OOM kills of the slideshow process. More swap
# headroom helps before the kernel OOM killer triggers at all.
#
# Raspberry Pi OS Trixie replaced the classic dphys-swapfile mechanism
# with "rpi-swap" (compressed RAM-based swap via zram, config under
# /etc/rpi/swap.conf.d/) - /etc/dphys-swapfile no longer exists there at
# all, so the old sed-based approach silently did nothing on Trixie.
# Bookworm and older still use dphys-swapfile, so both are handled here.
if [[ -f /etc/rpi/swap.conf ]] || dpkg -s rpi-swap &>/dev/null; then
    mkdir -p /etc/rpi/swap.conf.d
    cat > /etc/rpi/swap.conf.d/zeropiframe.conf << 'EOF'
[Zram]
RamMultiplier=2
MaxSizeMiB=1024
EOF
    info "rpi-swap (zram) configured for up to ~1024 MB - takes effect after a reboot (zram is set up by a generator at early boot, daemon-reload alone isn't enough)"
elif [[ -f /etc/dphys-swapfile ]]; then
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=1024/' /etc/dphys-swapfile
    grep -q "^CONF_SWAPSIZE=" /etc/dphys-swapfile || echo "CONF_SWAPSIZE=1024" >> /etc/dphys-swapfile
    dphys-swapfile setup  >/dev/null 2>&1 || true
    systemctl restart dphys-swapfile 2>/dev/null || true
    info "Swap set to 1024 MB (dphys-swapfile)"
else
    warn "No known swap mechanism (rpi-swap or dphys-swapfile) found - set up swap manually if RAM gets tight"
fi

# The Zero 2 W has no Ethernet - WiFi power-save otherwise causes
# noticeable delays/dropouts during sync and in the web UI.
cat > /etc/systemd/system/zeropiframe-wifi-powersave-off.service << 'EOF'
[Unit]
Description=ZeroPiFrame: Disable WiFi power-save mode
After=network.target

[Service]
Type=oneshot
ExecStart=/sbin/iw dev wlan0 set power_save off
RemainAfterExit=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now zeropiframe-wifi-powersave-off 2>/dev/null || \
    warn "Could not disable WiFi power-save (no wlan0? using USB-LAN or similar?)"

# ---------------------------------------------------------------------------
info "8/10 Setting up systemd services"
# ---------------------------------------------------------------------------
PYTHON="$VENV_DIR/bin/python3"

cat > /etc/systemd/system/zeropiframe-slideshow.service << EOF
[Unit]
Description=ZeroPiFrame Slideshow
After=multi-user.target systemd-udev-settle.service
# Crash-loop protection: if the service somehow keeps failing and
# restarting (Restart=always/RestartSec=5 below), stop retrying after
# StartLimitBurst failures within StartLimitIntervalSec instead of
# hammering the SD card / CPU forever. Needs a manual
# "systemctl reset-failed zeropiframe-slideshow" (or a reboot) to try again.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
User=$FRAME_USER
Group=video
WorkingDirectory=$INSTALL_DIR
Environment="SDL_VIDEODRIVER=kmsdrm"
Environment="SDL_VIDEO_KMSDRM_DEVICE=/dev/dri/card0"
Environment="SDL_AUDIODRIVER=dummy"
# On-device OOM kills were observed hitting this process (512 MB RAM is
# tight, especially with mpv running for video playback at the same
# time). Making the kernel strongly prefer NOT to kill this one means a
# genuine memory crunch instead takes out the disposable mpv child
# process (that one video is skipped, already handled gracefully) rather
# than the whole slideshow going black until Restart=always catches up.
OOMScoreAdjust=-500
ExecStartPre=/bin/sleep 3
ExecStart=$PYTHON $INSTALL_DIR/slideshow.py
StandardOutput=append:$LOG_DIR/zeropiframe-slideshow.log
StandardError=append:$LOG_DIR/zeropiframe-slideshow.log
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

cat > /etc/systemd/system/zeropiframe-sync.service << EOF
[Unit]
Description=ZeroPiFrame Sync (one-off run)
After=network-online.target
Wants=network-online.target

[Service]
User=$FRAME_USER
Type=oneshot
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON $INSTALL_DIR/sync.py
# No StandardOutput/StandardError redirect here (unlike zeropiframe-slideshow
# below): sync.py's own logging.basicConfig() already attaches a FileHandler
# directly on $LOG_DIR/zeropiframe-sync.log, on top of its StreamHandler(stdout).
# Redirecting systemd's stdout/stderr into that same file as well caused every
# single log line to be written twice, byte-identical down to the millisecond
# timestamp - confirmed on-device via `grep <text> zeropiframe-sync.log`
# showing exact duplicate lines. journald still captures stdout/stderr by
# default without this line, so `journalctl -u zeropiframe-sync` keeps working.
# Lower priority: downloads shouldn't slow down the slideshow.
Nice=10
IOSchedulingClass=idle
EOF

cat > /etc/systemd/system/zeropiframe-sync.timer << 'EOF'
[Unit]
Description=ZeroPiFrame Sync Timer

[Timer]
OnBootSec=30sec
OnUnitActiveSec=60min
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/zeropiframe-webui.service << EOF
[Unit]
Description=ZeroPiFrame Web-UI
After=network.target
# Crash-loop protection: see the matching comment on
# zeropiframe-slideshow.service above.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
User=$FRAME_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON $INSTALL_DIR/webui.py
Restart=always
RestartSec=5
# Previously Nice=5 (lower priority than default, no I/O priority, no OOM
# protection) - backwards for an admin interface, whose whole point is
# staying reachable exactly when the device is under heavy load (a big
# sync/remote-transcode upload, or slideshow decode work), not less.
# Matches slideshow's own priority now rather than competing with it from
# behind - a quick web request is cheap enough that this doesn't take
# anything meaningful away from slideshow smoothness, but it does stop
# webui from being the first thing starved of CPU or picked off by the
# OOM killer under pressure.
Nice=-5
IOSchedulingClass=best-effort
IOSchedulingPriority=2
OOMScoreAdjust=-400

[Install]
WantedBy=multi-user.target
EOF

# Log rotation: neither zeropiframe-sync.service nor
# zeropiframe-slideshow.service reopen/truncate their log file on a signal,
# so a plain rotation (rename + reopen) would leave them writing into the
# renamed, now-unrotated file forever. copytruncate sidesteps that: it
# copies the current content out and truncates the original file in place,
# which works with any process regardless of whether it supports SIGHUP.
cat > /etc/logrotate.d/zeropiframe << EOF
$LOG_DIR/zeropiframe-sync.log $LOG_DIR/zeropiframe-slideshow.log {
    weekly
    maxsize 20M
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
EOF

# REVERTED: an earlier version of this script also baked a splash into
# the initramfs itself (via /etc/initramfs-tools/hooks+scripts/init-premount)
# to cover the gap before boot. That caused a real boot failure on actual
# hardware (fbi erroring against /dev/fb0, apparently a device-numbering/
# timing quirk with vc4-kms-v3d, and/or the larger initramfs overflowing
# the small /boot/firmware partition) - confirmed and recovered via SSH.
#
# A later version replaced that with zeropiframe-boot-splash.service (a
# regular systemd unit) plus a /usr/lib/systemd/system-shutdown/ hook for
# the shutdown side. After four rounds of on-device diagnostics (fb0
# device-swap race, fbi exiting immediately under a systemd-assigned TTY,
# a Conflicts= directive silently dropped from the boot transaction, and a
# misunderstood fbi flag) it still wasn't reliably showing the splash, so
# this whole feature was abandoned as not worth the ongoing
# real-hardware risk for a purely cosmetic effect. Booting/shutting down
# now simply shows the console (quieted by the cmdline.txt flags in step
# 6/10) instead of a splash image.
#
# Cleans up leftovers from any of the above on a reinstall/update, in case
# this is running on a system that still has them (safe to run even if
# there's nothing to clean up).
if [[ -f /etc/initramfs-tools/hooks/zeropiframe-splash || -f /etc/initramfs-tools/scripts/init-premount/zeropiframe-splash ]]; then
    warn "Removing a previously installed initramfs boot-splash hook (reverted - caused boot failures)"
    rm -f /etc/initramfs-tools/hooks/zeropiframe-splash
    rm -f /etc/initramfs-tools/scripts/init-premount/zeropiframe-splash
    command -v update-initramfs &>/dev/null && update-initramfs -u || true
fi

if systemctl list-unit-files zeropiframe-boot-splash.service &>/dev/null; then
    warn "Removing a previously installed zeropiframe-boot-splash.service (abandoned - never reliably displayed the splash)"
    systemctl disable --now zeropiframe-boot-splash.service 2>/dev/null || true
    rm -f /etc/systemd/system/zeropiframe-boot-splash.service
fi
if [[ -f /usr/lib/systemd/system-shutdown/zeropiframe-splash.sh ]]; then
    warn "Removing a previously installed shutdown splash hook (abandoned along with the boot splash)"
    rm -f /usr/lib/systemd/system-shutdown/zeropiframe-splash.sh
fi
if [[ -f "$INSTALL_DIR/resolve-notice-image.sh" ]]; then
    rm -f "$INSTALL_DIR/resolve-notice-image.sh"
fi

# ---------------------------------------------------------------------------
info "9/10 Setting up sudo permissions for the web UI"
# ---------------------------------------------------------------------------
# zeropiframe-webui.service deliberately runs unprivileged as $FRAME_USER.
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
# /etc/sudoers.d/zeropiframe) - therefore validates its inputs defensively
# before anything is written to /etc/systemd/system/.
#
# Usage:
#   apply-hdmi-schedule.sh HH:MM HH:MM   (on time, off time)
#   apply-hdmi-schedule.sh disable
set -euo pipefail

TIME_RE='^([01][0-9]|2[0-3]):[0-5][0-9]$'

if [[ "${1:-}" == "disable" ]]; then
    systemctl disable --now zeropiframe-hdmi-on.timer zeropiframe-hdmi-off.timer 2>/dev/null || true
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
    local name="zeropiframe-hdmi-${label}"
    cat > "/etc/systemd/system/${name}.timer" << TIMER
[Unit]
Description=ZeroPiFrame HDMI ${label}

[Timer]
OnCalendar=*-*-* ${hh}:${mm}:00
Persistent=true

[Install]
WantedBy=timers.target
TIMER
    cat > "/etc/systemd/system/${name}.service" << SERVICE
[Unit]
Description=ZeroPiFrame HDMI ${label}

[Service]
Type=oneshot
ExecStart=/usr/bin/vcgencmd display_power ${power}
SERVICE
}

write_unit "on"  "$ON_H"  "$ON_M"  1
write_unit "off" "$OFF_H" "$OFF_M" 0

systemctl daemon-reload
systemctl enable --now zeropiframe-hdmi-on.timer zeropiframe-hdmi-off.timer
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
# Runs as root (via sudo, see /etc/sudoers.d/zeropiframe).
#
# Usage:
#   apply-shutdown-schedule.sh HH:MM
#   apply-shutdown-schedule.sh disable
set -euo pipefail

TIME_RE='^([01][0-9]|2[0-3]):[0-5][0-9]$'

if [[ "${1:-}" == "disable" ]]; then
    systemctl disable --now zeropiframe-shutdown.timer 2>/dev/null || true
    exit 0
fi

SHUTDOWN_TIME="${1:-}"
[[ "$SHUTDOWN_TIME" =~ $TIME_RE ]] || { echo "Invalid time: $SHUTDOWN_TIME" >&2; exit 1; }
H="${SHUTDOWN_TIME%%:*}"; M="${SHUTDOWN_TIME##*:}"

cat > /etc/systemd/system/zeropiframe-shutdown.timer << TIMER
[Unit]
Description=ZeroPiFrame Auto-Shutdown

[Timer]
OnCalendar=*-*-* ${H}:${M}:00
Persistent=false

[Install]
WantedBy=timers.target
TIMER

cat > /etc/systemd/system/zeropiframe-shutdown.service << 'SERVICE'
[Unit]
Description=ZeroPiFrame Auto-Shutdown

[Service]
Type=oneshot
ExecStart=/sbin/poweroff
SERVICE

systemctl daemon-reload
systemctl enable --now zeropiframe-shutdown.timer
EOF
chown root:root "$INSTALL_DIR/apply-shutdown-schedule.sh"
chmod 700 "$INSTALL_DIR/apply-shutdown-schedule.sh"

# Sync interval: zeropiframe-sync.timer is created above with a fixed
# OnUnitActiveSec=60min. The web UI's "Sync interval" field only writes to
# config.yaml - without this helper script, changing the value in the web
# UI would have no effect at all on the actual timer.
cat > "$INSTALL_DIR/apply-sync-interval.sh" << 'EOF'
#!/bin/bash
# Sets the sync interval of zeropiframe-sync.timer.
# Runs as root (via sudo, see /etc/sudoers.d/zeropiframe).
#
# Usage: apply-sync-interval.sh <minutes>
set -euo pipefail

MINUTES="${1:-}"
[[ "$MINUTES" =~ ^[0-9]+$ ]] || { echo "Invalid interval: $MINUTES" >&2; exit 1; }
(( MINUTES >= 5 && MINUTES <= 1440 )) || { echo "Interval must be between 5 and 1440 minutes" >&2; exit 1; }

cat > /etc/systemd/system/zeropiframe-sync.timer << TIMER
[Unit]
Description=ZeroPiFrame Sync Timer

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
systemctl restart zeropiframe-sync.timer
EOF
chown root:root "$INSTALL_DIR/apply-sync-interval.sh"
chmod 700 "$INSTALL_DIR/apply-sync-interval.sh"

# Permanently enables/disables the scheduled auto-sync (survives a
# reboot) - unlike a plain "systemctl stop", which would only pause the
# timer until the next boot since it would remain enabled.
cat > "$INSTALL_DIR/apply-sync-enabled.sh" << 'EOF'
#!/bin/bash
# Permanently enables/disables the automatic (scheduled) sync.
# Runs as root (via sudo, see /etc/sudoers.d/zeropiframe).
#
# Usage: apply-sync-enabled.sh enable|disable
set -euo pipefail

ACTION="${1:-}"
case "$ACTION" in
  enable)
    systemctl enable --now zeropiframe-sync.timer
    ;;
  disable)
    systemctl disable --now zeropiframe-sync.timer
    ;;
  *)
    echo "Usage: apply-sync-enabled.sh enable|disable" >&2
    exit 1
    ;;
esac
EOF
chown root:root "$INSTALL_DIR/apply-sync-enabled.sh"
chmod 700 "$INSTALL_DIR/apply-sync-enabled.sh"

# "Check for updates" in the web UI: git-pulls the source checkout this
# was installed from and re-runs install.sh, either in --deploy mode
# (fast - just the .py/.html/.json files + a restart of the two services)
# or as a full install (slow - also apt/venv/ZeroPlay/boot config; needed
# when requirements.txt or anything install.sh itself does outside its
# DEPLOY_MODE branch has changed). The web UI offers both as separate
# buttons - see the "mode" argument below. Reads the checkout's location
# from $INSTALL_DIR/.source_dir (written above / near the top of this
# script) rather than having it baked in here, so this keeps working even
# if the repo is later moved. The git pull itself runs as whichever user
# owns that checkout (normally $FRAME_USER, since sudo -u as root can drop
# to any user) rather than as root, so it doesn't leave root-owned files
# behind in a directory a normal user needs to keep working with.
cat > "$INSTALL_DIR/update.sh" << 'EOF'
#!/bin/bash
# Pulls the latest source and deploys/installs it. Runs as root (via
# sudo, see /etc/sudoers.d/zeropiframe), triggered by the web UI's
# "Check for updates" card (see api_update() in webui.py).
#
# Usage: update.sh deploy|full
#
# Deliberately does NOT propagate failures back to its caller via a
# non-zero exit in the way a stricter script might: webui.py launches
# this detached and never looks at its exit code (it can't - both modes
# end by restarting zeropiframe-webui itself, which would otherwise race
# the very process trying to read that exit code). $STATUS_FILE is the
# actual result channel, polled by the web UI page afterwards.
set -uo pipefail

INSTALL_DIR="/opt/zeropiframe"
STATUS_FILE="/var/lib/zeropiframe/last_update.json"
LOG_FILE="/var/log/zeropiframe-update.log"
SRC_FILE="$INSTALL_DIR/.source_dir"
MODE="${1:-deploy}"

mkdir -p "$(dirname "$STATUS_FILE")"

write_status() {
    # $1/$2 are always one of the fixed, known-safe strings below - never
    # attacker- or repo-controlled - so plain quoting here is fine.
    printf '{"ok": %s, "message": "%s", "timestamp": "%s"}\n' \
        "$1" "$2" "$(date '+%Y-%m-%d %H:%M:%S')" > "$STATUS_FILE"
}

{
    echo "=== Update ($MODE) started $(date '+%Y-%m-%d %H:%M:%S') ==="

    if [[ "$MODE" != "deploy" && "$MODE" != "full" ]]; then
        echo "Invalid mode: $MODE (expected 'deploy' or 'full')"
        write_status false "Invalid update mode: $MODE"
        exit 1
    fi

    if [[ ! -f "$SRC_FILE" ]]; then
        echo "No source directory recorded ($SRC_FILE) - run a full install first"
        write_status false "No source directory recorded - run a full install first"
        exit 1
    fi
    REPO_DIR="$(cat "$SRC_FILE")"
    if [[ ! -d "$REPO_DIR/.git" ]]; then
        echo "$REPO_DIR is missing or not a git checkout"
        write_status false "Recorded source directory is missing or not a git checkout"
        exit 1
    fi

    REPO_OWNER="$(stat -c '%U' "$REPO_DIR")"
    echo "Repo: $REPO_DIR (owner: $REPO_OWNER)"

    PULL_OUTPUT="$(sudo -u "$REPO_OWNER" git -C "$REPO_DIR" pull --ff-only 2>&1)"
    PULL_STATUS=$?
    echo "$PULL_OUTPUT"
    if [[ $PULL_STATUS -ne 0 ]]; then
        write_status false "git pull failed - see $LOG_FILE"
        exit 1
    fi

    INSTALL_OK=1
    if [[ "$MODE" == "full" ]]; then
        bash "$REPO_DIR/install.sh" "$REPO_OWNER" || INSTALL_OK=0
    else
        bash "$REPO_DIR/install.sh" --deploy "$REPO_OWNER" || INSTALL_OK=0
    fi

    if [[ "$INSTALL_OK" -eq 0 ]]; then
        if [[ "$MODE" == "full" ]]; then
            write_status false "Full install failed - see $LOG_FILE"
        else
            write_status false "Deploy failed - see $LOG_FILE"
        fi
        exit 1
    fi

    if [[ "$PULL_OUTPUT" == *"Already up to date"* ]]; then
        write_status true "Already up to date ($MODE re-run anyway)"
    elif [[ "$MODE" == "full" ]]; then
        write_status true "Updated and fully reinstalled"
    else
        write_status true "Updated and deployed successfully"
    fi
    echo "=== Update ($MODE) finished $(date '+%Y-%m-%d %H:%M:%S') ==="
} >> "$LOG_FILE" 2>&1
EOF
chown root:root "$INSTALL_DIR/update.sh"
chmod 700 "$INSTALL_DIR/update.sh"

cat > /etc/sudoers.d/zeropiframe << EOF
# Generated by install.sh - only the specific commands that
# zeropiframe-webui.service (running as $FRAME_USER) needs for "sync
# now/stop", "start/stop/restart slideshow", the HDMI schedule, the
# auto-shutdown schedule, enabling/disabling the auto-sync timer, and
# checking for updates. No general sudo/root grant.
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start zeropiframe-sync
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl stop zeropiframe-sync
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start zeropiframe-slideshow
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl stop zeropiframe-slideshow
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart zeropiframe-slideshow
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now zeropiframe-slideshow
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now zeropiframe-slideshow
$FRAME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff
$FRAME_USER ALL=(root) NOPASSWD: $INSTALL_DIR/apply-hdmi-schedule.sh *
$FRAME_USER ALL=(root) NOPASSWD: $INSTALL_DIR/apply-shutdown-schedule.sh *
$FRAME_USER ALL=(root) NOPASSWD: $INSTALL_DIR/apply-sync-interval.sh *
$FRAME_USER ALL=(root) NOPASSWD: $INSTALL_DIR/apply-sync-enabled.sh *
$FRAME_USER ALL=(root) NOPASSWD: $INSTALL_DIR/update.sh deploy
$FRAME_USER ALL=(root) NOPASSWD: $INSTALL_DIR/update.sh full
EOF
chmod 440 /etc/sudoers.d/zeropiframe
visudo -c -f /etc/sudoers.d/zeropiframe || error "sudoers file invalid - please check /etc/sudoers.d/zeropiframe"
info "sudoers rule created: /etc/sudoers.d/zeropiframe"

# ---------------------------------------------------------------------------
info "10/10 Enabling services"
# ---------------------------------------------------------------------------
systemctl daemon-reload
systemctl enable zeropiframe-slideshow zeropiframe-sync.timer zeropiframe-webui
# restart instead of start: reliably brings up both freshly installed
# services and ones that were paused for the installation (step 0/10) -
# and, on a reinstall (e.g. to roll out a fix), ensures that already
# running services actually pick up the new code instead of continuing to
# run unchanged.
systemctl restart zeropiframe-sync.timer zeropiframe-webui

# Only stop the notice image now (if it was started in step 0b) - as close
# as possible to right before the real slideshow regains display
# ownership, so the screen shows blank/console in between for as short a
# time as possible.
if [[ -n "$FBI_PID" ]] && kill -0 "$FBI_PID" 2>/dev/null; then
    kill "$FBI_PID" 2>/dev/null || true
    wait "$FBI_PID" 2>/dev/null || true
    # Not just stopping fbi - clearing what it left behind in VT1's saved
    # console state (see _clear_notice_fb's comment, step 0b above).
    _clear_notice_fb
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
    systemctl restart zeropiframe-slideshow
else
    warn "Config for the active source ($(grep -oP '^source:\s*\K\S+' "$INSTALL_DIR/config.yaml" 2>/dev/null || echo nextcloud)) not filled in yet - the slideshow will start once it's filled in:"
    warn "  sudo systemctl start zeropiframe-slideshow"
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
echo "  Sync log:       $LOG_DIR/zeropiframe-sync.log"
echo "  Slideshow log:  $LOG_DIR/zeropiframe-slideshow.log"
echo "  Python:         $PYTHON"
echo ""
echo "  Check services:"
echo "    sudo systemctl status zeropiframe-slideshow"
echo "    sudo systemctl status zeropiframe-webui"
echo "    sudo journalctl -u zeropiframe-sync -f"
echo ""
[[ -f "$INSTALL_DIR/config.yaml" ]] && \
    grep -q "mein_passwort\|DEIN_API_KEY" "$INSTALL_DIR/config.yaml" && \
    echo -e "${YELLOW}  ⚠ Please fill in the config now: sudo nano $INSTALL_DIR/config.yaml${NC}"
echo ""
